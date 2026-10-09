import time

from django.conf import settings
from django.contrib.auth import login, logout, password_validation, update_session_auth_hash
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.views import APIView

from . import emails
from core import audit
from payments.files import serve_private_file

from .models import EmailOTP, Role, Status, User
from .serializers import (
    AvatarSerializer,
    ChangeEmailSerializer,
    ChangePasswordSerializer,
    CodeSerializer,
    EmailSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    RegisterSerializer,
    SetupSuperAdminSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)

Purpose = EmailOTP.Purpose

INVALID_CREDENTIALS = 'Invalid email or password.'
SIGNUP_EXPIRED = 'No pending sign-up was found for this email. Please register again.'
# Session key holding an admin who passed the password step but not the emailed code yet
ADMIN_2FA_SESSION_KEY = 'admin_2fa'


def error(message, code, http_status=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({'detail': message, 'code': code, **extra}, status=http_status)


def cooldown_error(wait):
    return error(f'Please wait {wait}s before requesting another code.', 'otp_cooldown',
                 status.HTTP_429_TOO_MANY_REQUESTS, retryAfter=wait)


def code_sent(email, http_status=status.HTTP_200_OK, **extra):
    return Response(
        {'email': email, 'resendIn': settings.OTP_RESEND_SECONDS, 'otpLength': settings.OTP_LENGTH, **extra},
        status=http_status,
    )


def check_code(otp, code):
    """Validate an emailed code. Returns an error Response, or None once the code is used up."""
    if otp.is_expired:
        return error('This code has expired. Request a new one.', 'otp_expired')
    if otp.attempts_exhausted:
        return error('Too many incorrect attempts. Request a new code.', 'otp_locked')
    if not otp.matches(code):
        otp.attempts += 1
        otp.save(update_fields=['attempts'])
        return error('The code you entered is incorrect. Please try again.', 'otp_invalid')
    otp.used_at = timezone.now()
    otp.save(update_fields=['used_at'])
    return None


class CsrfAPIView(APIView):
    """DRF skips CSRF checks for anonymous requests; enforce them on every unsafe
    request so login and sign-up forms cannot be submitted cross-site."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method not in SAFE_METHODS:
            SessionAuthentication().enforce_csrf(request)


class PublicAPIView(CsrfAPIView):
    permission_classes = [AllowAny]


@method_decorator(ensure_csrf_cookie, name='dispatch')
class CsrfView(PublicAPIView):
    """Called once by the SPA on load to receive the csrftoken cookie."""

    def get(self, request):
        return Response({'csrfToken': get_token(request)})


# ---- Sign-up ----

def _send_signup_code(user):
    _, code = EmailOTP.issue(user, Purpose.VERIFY_EMAIL)
    emails.send_verification_code(user, code)


class RegisterView(PublicAPIView):
    throttle_scope = 'auth_register'

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            user = User.objects.select_for_update().filter(email=data['email']).first()
            if user and user.status != Status.PENDING:
                return error('An account with this email already exists.', 'email_taken',
                             errors={'email': ['An account with this email already exists.']})
            if user:
                # Registering again before verifying replaces the earlier details
                wait = EmailOTP.resend_wait(user, Purpose.VERIFY_EMAIL)
                if wait:
                    return cooldown_error(wait)
            else:
                user = User(email=data['email'], role=Role.CUSTOMER, status=Status.PENDING)
            user.full_name = data['fullName']
            user.phone = data['phone']
            user.agent_code = data.get('agentCode', '')
            user.set_password(data['password'])
            user.save()
            _send_signup_code(user)

        return code_sent(user.email, status.HTTP_201_CREATED)


class ResendOtpView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email=serializer.validated_data['email'], status=Status.PENDING).first()
        if not user:
            return error(SIGNUP_EXPIRED, 'signup_not_found', status.HTTP_404_NOT_FOUND)
        wait = EmailOTP.resend_wait(user, Purpose.VERIFY_EMAIL)
        if wait:
            return cooldown_error(wait)
        _send_signup_code(user)
        return code_sent(user.email)


class VerifyEmailView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            user = User.objects.select_for_update().filter(email=data['email'], status=Status.PENDING).first()
            otp = user and EmailOTP.current(user, Purpose.VERIFY_EMAIL)
            if not otp:
                return error(SIGNUP_EXPIRED, 'signup_not_found', status.HTTP_404_NOT_FOUND)
            failed = check_code(otp, data['code'])
            if failed:
                return failed
            user.status = Status.ACTIVE
            user.email_verified = True
            user.save(update_fields=['status', 'email_verified'])
            from payments.models import Notification
            from payments.notify import notify, notify_admins
            notify(user, 'Welcome to Empire Global',
                   'Your account is ready. Browse our products to start saving, investing or apply for a loan.',
                   Notification.Kind.ACCOUNT, Notification.Type.SUCCESS, link='/customer/products')
            notify_admins('New customer registered',
                          f'{user.full_name} ({user.email}) signed up'
                          + (f' with agent {user.agent_code}.' if user.agent_code else '.'),
                          Notification.Kind.CUSTOMER, link=f'/admin/customers/{user.public_id}')

        login(request, user)
        return Response({'user': UserSerializer(user).data})


# ---- Sign-in ----

def _locked_error(user):
    minutes = max(1, -(-user.lock_seconds_left() // 60))
    return error(
        f'Too many failed sign-in attempts. Try again in {minutes} minute{"s" if minutes != 1 else ""}, '
        'or reset your password.',
        'account_locked', status.HTTP_403_FORBIDDEN, retryAfter=user.lock_seconds_left(),
    )


class LoginView(PublicAPIView):
    throttle_scope = 'auth_login'
    admin_portal = False

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = User.objects.filter(email=data['email']).first()
        if user is None:
            # Hash anyway so response timing does not reveal which emails exist
            User().set_password(data['password'])
            return error(INVALID_CREDENTIALS, 'invalid_credentials', status.HTTP_401_UNAUTHORIZED)
        if user.lock_seconds_left():
            return _locked_error(user)
        if not user.check_password(data['password']):
            user.record_failed_login()
            if user.lock_seconds_left():
                return _locked_error(user)
            return error(INVALID_CREDENTIALS, 'invalid_credentials', status.HTTP_401_UNAUTHORIZED)
        # Each portal only accepts its own accounts
        if user.is_admin != self.admin_portal:
            return error(INVALID_CREDENTIALS, 'invalid_credentials', status.HTTP_401_UNAUTHORIZED)
        user.clear_failed_logins()

        if user.status == Status.PENDING:
            wait = EmailOTP.resend_wait(user, Purpose.VERIFY_EMAIL)
            if not wait:
                _send_signup_code(user)
            return error('Please verify your email to finish signing up. We have sent you a code.',
                         'email_not_verified', status.HTTP_403_FORBIDDEN, email=user.email,
                         resendIn=wait or settings.OTP_RESEND_SECONDS)
        if user.status != Status.ACTIVE:
            message = ('This admin account has been deactivated. Contact a Super Admin.' if self.admin_portal
                       else 'Your account has been suspended. Please contact support.')
            return error(message, 'account_inactive', status.HTTP_403_FORBIDDEN)

        if self.admin_portal and settings.ADMIN_TWO_FACTOR:
            return self.start_two_factor(request, user)

        login(request, user)
        if self.admin_portal:
            audit.record(user, 'Admin Signed In', 'Signed in with password.', reference=user.public_id)
        return Response({'user': UserSerializer(user).data})

    def start_two_factor(self, request, user):
        # Not signed in yet: remember who passed the password step, then email a code
        request.session[ADMIN_2FA_SESSION_KEY] = {
            'user_id': user.pk,
            'expires': time.time() + settings.ADMIN_TWO_FACTOR_WINDOW_SECONDS,
        }
        wait = EmailOTP.resend_wait(user, Purpose.ADMIN_LOGIN)
        if not wait:
            _, code = EmailOTP.issue(user, Purpose.ADMIN_LOGIN)
            emails.send_admin_login_code(user, code)
        return Response(
            {'twoFactorRequired': True, 'email': mask_email(user.email),
             'resendIn': wait or settings.OTP_RESEND_SECONDS, 'otpLength': settings.OTP_LENGTH},
            status=status.HTTP_202_ACCEPTED,
        )


class AdminLoginView(LoginView):
    admin_portal = True


def mask_email(email):
    """a****@empireglobal.com: enough to recognise, not enough to harvest."""
    local, _, domain = email.partition('@')
    return f'{local[:1]}{"*" * max(3, len(local) - 1)}@{domain}'


def _pending_admin(request):
    """The admin waiting for their sign-in code, if their window is still open."""
    pending = request.session.get(ADMIN_2FA_SESSION_KEY)
    if not pending or pending.get('expires', 0) < time.time():
        return None
    user = User.objects.filter(pk=pending.get('user_id')).first()
    if not user or not user.is_admin or user.status != Status.ACTIVE:
        return None
    return user


SIGN_IN_AGAIN = 'Your sign-in has expired. Please enter your email and password again.'


class AdminVerifyTwoFactorView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = CodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _pending_admin(request)
        otp = user and EmailOTP.current(user, Purpose.ADMIN_LOGIN)
        if not otp:
            return error(SIGN_IN_AGAIN, 'two_factor_expired', status.HTTP_401_UNAUTHORIZED)
        failed = check_code(otp, serializer.validated_data['code'])
        if failed:
            return failed

        request.session.pop(ADMIN_2FA_SESSION_KEY, None)
        login(request, user)
        audit.record(user, 'Admin Signed In', 'Signed in with password and emailed code.', reference=user.public_id)
        return Response({'user': UserSerializer(user).data})


class AdminResendTwoFactorView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        user = _pending_admin(request)
        if not user:
            return error(SIGN_IN_AGAIN, 'two_factor_expired', status.HTTP_401_UNAUTHORIZED)
        wait = EmailOTP.resend_wait(user, Purpose.ADMIN_LOGIN)
        if wait:
            return cooldown_error(wait)
        _, code = EmailOTP.issue(user, Purpose.ADMIN_LOGIN)
        emails.send_admin_login_code(user, code)
        return code_sent(mask_email(user.email))


class LogoutView(CsrfAPIView):
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---- Signed-in account ----

class MeView(CsrfAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'user': UserSerializer(request.user).data})

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'user': serializer.data})


class AvatarView(CsrfAPIView):
    """Upload (POST, multipart `avatar`) or remove (DELETE) the signed-in user's display picture."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = 'uploads'

    def post(self, request):
        serializer = AvatarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        old = user.avatar.name if user.avatar else None
        user.avatar = serializer.validated_data['avatar']
        user.avatar_updated_at = timezone.now()
        user.save(update_fields=['avatar', 'avatar_updated_at'])
        if old and old != user.avatar.name:
            user.avatar.storage.delete(old)
        return Response({'user': UserSerializer(user).data})

    def delete(self, request):
        user = request.user
        if user.avatar:
            user.avatar.delete(save=False)
            user.avatar_updated_at = timezone.now()
            user.save(update_fields=['avatar', 'avatar_updated_at'])
        return Response({'user': UserSerializer(user).data})


class AvatarFileView(CsrfAPIView):
    """A user's display picture, for themselves or an admin (local storage; Cloudinary links go direct)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, public_id):
        if request.user.public_id != public_id and not request.user.is_admin:
            raise Http404
        user = get_object_or_404(User, public_id=public_id)
        if not user.avatar:
            raise Http404
        if hasattr(user.avatar.storage, 'avatar_url'):
            return HttpResponseRedirect(user.avatar.storage.avatar_url(user.avatar.name))
        response = serve_private_file(user.avatar, f'{user.public_id}-avatar.{user.avatar.name.rsplit(".", 1)[-1]}')
        # The URL carries a version, so the browser may keep it for a while
        response['Cache-Control'] = 'private, max-age=86400'
        return response


class ChangePasswordView(CsrfAPIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = 'auth_login'

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        if not user.check_password(data['currentPassword']):
            return error('Your current password is incorrect.', 'invalid_password',
                         errors={'currentPassword': ['Your current password is incorrect.']})
        if data['newPassword'] == data['currentPassword']:
            return error('Choose a password different from your current one.', 'same_password',
                         errors={'newPassword': ['Choose a password different from your current one.']})
        try:
            password_validation.validate_password(data['newPassword'], user)
        except DjangoValidationError as exc:
            return error(exc.messages[0], 'weak_password', errors={'newPassword': exc.messages})

        user.set_password(data['newPassword'])
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])
        # Keep this session; every other session is signed out by the hash change
        update_session_auth_hash(request, user)
        if user.is_admin:
            audit.record(user, 'Password Changed', 'An admin changed their own password.', reference=user.public_id)
        return Response({'user': UserSerializer(user).data})


class CustomersOnly(IsAuthenticated):
    message = 'Admin emails are changed by a Super Admin.'

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == Role.CUSTOMER


class ChangeEmailView(CsrfAPIView):
    """Step 1: confirm the password and send a code to the new address."""

    permission_classes = [CustomersOnly]
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = ChangeEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        if not user.check_password(data['password']):
            return error('Your password is incorrect.', 'invalid_password',
                         errors={'password': ['Your password is incorrect.']})
        new_email = data['newEmail']
        if new_email == user.email:
            return error('This is already your email address.', 'same_email',
                         errors={'newEmail': ['This is already your email address.']})
        if User.objects.filter(email=new_email).exists():
            return error('An account with this email already exists.', 'email_taken',
                         errors={'newEmail': ['An account with this email already exists.']})
        wait = EmailOTP.resend_wait(user, Purpose.CHANGE_EMAIL)
        if wait:
            return cooldown_error(wait)

        user.pending_email = new_email
        user.save(update_fields=['pending_email'])
        _, code = EmailOTP.issue(user, Purpose.CHANGE_EMAIL)
        emails.send_email_change_code(user, new_email, code)
        emails.send_email_change_requested(user, new_email)
        return code_sent(new_email)


class ResendChangeEmailView(CsrfAPIView):
    permission_classes = [CustomersOnly]
    throttle_scope = 'auth_otp'

    def post(self, request):
        user = request.user
        if not user.pending_email:
            return error('There is no email change in progress.', 'no_email_change')
        wait = EmailOTP.resend_wait(user, Purpose.CHANGE_EMAIL)
        if wait:
            return cooldown_error(wait)
        _, code = EmailOTP.issue(user, Purpose.CHANGE_EMAIL)
        emails.send_email_change_code(user, user.pending_email, code)
        return code_sent(user.pending_email)


class ConfirmChangeEmailView(CsrfAPIView):
    """Step 2: the code from the new inbox proves the customer owns it."""

    permission_classes = [CustomersOnly]
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = CodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            otp = EmailOTP.current(user, Purpose.CHANGE_EMAIL)
            if not user.pending_email or not otp:
                return error('There is no email change in progress.', 'no_email_change')
            failed = check_code(otp, serializer.validated_data['code'])
            if failed:
                return failed
            # Someone may have registered the address since the code was sent
            if User.objects.filter(email=user.pending_email).exclude(pk=user.pk).exists():
                user.pending_email = ''
                user.save(update_fields=['pending_email'])
                return error('An account with this email already exists.', 'email_taken')

            old_email = user.email
            user.email = user.pending_email
            user.pending_email = ''
            user.email_verified = True
            user.save(update_fields=['email', 'pending_email', 'email_verified'])

        emails.send_email_changed(user, old_email)
        return Response({'user': UserSerializer(user).data})


# ---- Forgotten password ----

class PasswordResetRequestView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email=serializer.validated_data['email'], status=Status.ACTIVE).first()
        if user and user.has_usable_password():
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            path = '/admin/reset-password' if user.is_admin else '/reset-password'
            emails.send_password_reset_link(user, f'{settings.FRONTEND_URL}{path}?uid={uid}&token={token}')
        # Same answer either way, so this cannot be used to discover accounts
        return Response({'detail': 'If an account exists for this email, a reset link has been sent.'})


class PasswordResetConfirmView(PublicAPIView):
    throttle_scope = 'auth_otp'

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            user = User.objects.get(pk=force_str(urlsafe_base64_decode(data['uid'])), status=Status.ACTIVE)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            user = None
        if user is None or not default_token_generator.check_token(user, data['token']):
            return error('This reset link is invalid or has expired. Please request a new one.', 'reset_link_invalid')

        try:
            password_validation.validate_password(data['password'], user)
        except DjangoValidationError as exc:
            return error(exc.messages[0], 'weak_password', errors={'password': exc.messages})

        # Changing the hash also invalidates the token and signs out existing sessions.
        # The owner chose this password and proved their inbox, so lift any lockout too.
        user.set_password(data['password'])
        user.must_change_password = False
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=['password', 'must_change_password', 'failed_login_count', 'locked_until'])
        return Response({'detail': 'Your password has been reset. You can now sign in.'})


# ---- First-run setup ----

class SetupSuperAdminView(APIView):
    """Creates the first Super Admin (e.g. from Postman) on a fresh install.

    It only works while no Super Admin exists, so it can't be used to add or take over admin
    accounts later. Outside DEBUG an X-Setup-Token header matching SETUP_TOKEN is also required.
    No session or cookie is used, so CSRF doesn't apply and it can be called without a browser.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = 'setup'

    def post(self, request):
        import hmac

        token = settings.SETUP_TOKEN
        if not settings.DEBUG and not token:
            return error('First-run setup is disabled. Set SETUP_TOKEN on the server to enable it.',
                         'setup_disabled', status.HTTP_403_FORBIDDEN)
        if token and not hmac.compare_digest(request.headers.get('X-Setup-Token', ''), token):
            return error('Invalid setup token.', 'invalid_setup_token', status.HTTP_403_FORBIDDEN)
        if User.objects.filter(role=Role.SUPER_ADMIN).exists():
            return error('A Super Admin already exists. Sign in and add admins from Admin Management.',
                         'setup_complete', status.HTTP_409_CONFLICT)

        serializer = SetupSuperAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            # Checked again inside the transaction so two requests at once can't both succeed
            if User.objects.select_for_update().filter(role=Role.SUPER_ADMIN).exists():
                return error('A Super Admin already exists.', 'setup_complete', status.HTTP_409_CONFLICT)
            admin = User.objects.create_superuser(data['email'], data['password'], full_name=data['fullName'])
        audit.record(admin, 'Super Admin Created', 'First Super Admin created through first-run setup.',
                     reference=admin.public_id, status='success')
        return Response({
            'user': UserSerializer(admin).data,
            'detail': 'Super Admin created. Sign in at /admin/login with this email and password.',
        }, status=status.HTTP_201_CREATED)
