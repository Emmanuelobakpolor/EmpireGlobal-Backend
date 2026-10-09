import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    CUSTOMER = 'customer', 'Customer'
    ADMIN = 'admin', 'Admin'
    SUPER_ADMIN = 'super_admin', 'Super Admin'


class Status(models.TextChoices):
    # Email sign-ups stay pending until their OTP is confirmed
    PENDING = 'pending', 'Pending verification'
    ACTIVE = 'active', 'Active'
    SUSPENDED = 'suspended', 'Suspended'
    INACTIVE = 'inactive', 'Inactive'


class AuthProvider(models.TextChoices):
    PASSWORD = 'password', 'Email & password'
    GOOGLE = 'google', 'Google'


ID_PREFIXES = {Role.CUSTOMER: 'EMP', Role.ADMIN: 'ADM', Role.SUPER_ADMIN: 'ADM'}


def normalize_agent_code(code):
    return (code or '').strip().upper()


def avatar_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f'avatars/{uuid.uuid4().hex}.{ext}'


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError('An email address is required.')
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault('role', Role.CUSTOMER)
        extra.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('role', Role.SUPER_ADMIN)
        extra.setdefault('status', Status.ACTIVE)
        extra.setdefault('email_verified', True)
        extra.setdefault('is_superuser', True)
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    # Public reference shown in the UI, e.g. EMP-84920 or ADM-10233
    public_id = models.CharField(max_length=16, unique=True, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32, blank=True)
    agent_code = models.CharField(max_length=16, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CUSTOMER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    auth_provider = models.CharField(max_length=16, choices=AuthProvider.choices, default=AuthProvider.PASSWORD)
    # Google's stable account ID, once the customer has signed in with Google
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True, editable=False)
    email_verified = models.BooleanField(default=False)
    # {fullName, phone, relationship, address}; required for savings and investment plans
    next_of_kin = models.JSONField(null=True, blank=True)
    # Set when a Super Admin chooses the password; the admin must pick their own before working
    must_change_password = models.BooleanField(default=False)
    # Per-account lockout after repeated wrong passwords
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    # A new address waiting for its confirmation code
    pending_email = models.EmailField(blank=True)
    # Customer balances. Only a Super Admin's final approval of a payment changes them
    # (see payments.services.approve_payment).
    savings_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    investment_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    outstanding_loan = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    date_joined = models.DateTimeField(default=timezone.now)

    # Display picture (Cloudinary in production, MEDIA_ROOT in development)
    avatar = models.FileField(upload_to=avatar_path, blank=True)
    avatar_updated_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    EMAIL_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']

    class Meta:
        ordering = ['-date_joined']

    def __str__(self):
        return f'{self.full_name} <{self.email}>'

    # Django's auth backends and admin read these, so derive them from role/status
    @property
    def is_active(self):
        return self.status == Status.ACTIVE

    @property
    def is_staff(self):
        return self.role == Role.SUPER_ADMIN

    @property
    def is_admin(self):
        return self.role in (Role.ADMIN, Role.SUPER_ADMIN)

    def lock_seconds_left(self):
        if not self.locked_until:
            return 0
        return max(0, int((self.locked_until - timezone.now()).total_seconds()))

    def record_failed_login(self):
        """Count a wrong password; lock the account once the limit is reached."""
        self.failed_login_count += 1
        if self.failed_login_count >= settings.LOGIN_MAX_FAILURES:
            self.locked_until = timezone.now() + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            self.failed_login_count = 0
        self.save(update_fields=['failed_login_count', 'locked_until'])

    def clear_failed_logins(self):
        if self.failed_login_count or self.locked_until:
            self.failed_login_count = 0
            self.locked_until = None
            self.save(update_fields=['failed_login_count', 'locked_until'])

    def save(self, *args, **kwargs):
        self.email = self.email.lower()
        self.agent_code = normalize_agent_code(self.agent_code)
        if not self.public_id:
            self.public_id = self._generate_public_id()
        super().save(*args, **kwargs)

    def _generate_public_id(self):
        prefix = ID_PREFIXES[self.role]
        while True:
            candidate = f'{prefix}-{secrets.randbelow(90000) + 10000}'
            if not User.objects.filter(public_id=candidate).exists():
                return candidate


class EmailOTP(models.Model):
    """A one-time emailed code. Only the hash is stored."""

    class Purpose(models.TextChoices):
        VERIFY_EMAIL = 'verify_email', 'Confirm sign-up'
        ADMIN_LOGIN = 'admin_login', 'Admin sign-in'
        CHANGE_EMAIL = 'change_email', 'Confirm new email'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_otps')
    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.VERIFY_EMAIL)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def issue(cls, user, purpose=Purpose.VERIFY_EMAIL):
        """Invalidate older codes for the same purpose and return (otp, plaintext_code)."""
        now = timezone.now()
        cls.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(used_at=now)
        code = ''.join(secrets.choice('0123456789') for _ in range(settings.OTP_LENGTH))
        otp = cls.objects.create(
            user=user,
            purpose=purpose,
            code_hash=make_password(code),
            expires_at=now + timedelta(seconds=settings.OTP_TTL_SECONDS),
        )
        return otp, code

    @classmethod
    def current(cls, user, purpose):
        """The latest unused code for this purpose, if any."""
        return cls.objects.filter(user=user, purpose=purpose, used_at__isnull=True).first()

    @classmethod
    def resend_wait(cls, user, purpose):
        """Seconds before another code for this purpose may be sent."""
        latest = cls.objects.filter(user=user, purpose=purpose).first()
        return latest.seconds_until_resend() if latest else 0

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def attempts_exhausted(self):
        return self.attempts >= settings.OTP_MAX_ATTEMPTS

    def seconds_until_resend(self):
        elapsed = (timezone.now() - self.created_at).total_seconds()
        return max(0, int(settings.OTP_RESEND_SECONDS - elapsed))

    def matches(self, code):
        return check_password(code, self.code_hash)
