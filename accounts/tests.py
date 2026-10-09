import re
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Agent

from .models import EmailOTP, Role, Status, User

PASSWORD = 'Str0ng-Passw0rd!'
SIGNUP = {
    'fullName': 'Adewale Alao',
    'email': 'Adewale@Example.com',
    'phone': '+234 803 123 4567',
    'agentCode': ' ag-1001 ',
    'password': PASSWORD,
}


def last_code():
    return re.search(r'code is (\d{6})', mail.outbox[-1].body).group(1)


@override_settings(
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    # Most tests sign admins in as setup; two-factor sign-in has its own tests below
    ADMIN_TWO_FACTOR=False,
)
class AuthTestBase(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.get('/api/auth/csrf/')

    # Like the SPA, send the current csrftoken cookie: Django rotates it on login
    def post(self, url, data=None):
        return self.client.post(url, data or {}, format='json', HTTP_X_CSRFTOKEN=self.csrf())

    def patch(self, url, data):
        return self.client.patch(url, data, format='json', HTTP_X_CSRFTOKEN=self.csrf())

    def csrf(self):
        return self.client.cookies['csrftoken'].value

    def make_user(self, email='customer@example.com', role=Role.CUSTOMER, status=Status.ACTIVE):
        return User.objects.create_user(email, PASSWORD, full_name='Test User', role=role, status=status,
                                        email_verified=status != Status.PENDING)

    def expire_cooldown(self, user):
        EmailOTP.objects.filter(user=user).update(created_at=timezone.now() - timedelta(minutes=2))


class RegistrationTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        Agent.objects.create(code='AG-1001', name='Kunle', phone='0803', location='Ikorodu')
        Agent.objects.create(code='AG-1009', name='Old', phone='0803', location='Ikeja', status='inactive')

    def test_agent_code_must_belong_to_active_agent(self):
        for code, message in [('AG-9999', 'not found'), ('ag-1009', 'no longer active')]:
            res = self.post('/api/auth/register/', {**SIGNUP, 'agentCode': code})
            self.assertEqual(res.status_code, 400)
            self.assertIn(message, res.json()['agentCode'][0])
        self.assertFalse(User.objects.exists())
        # The code is optional
        self.assertEqual(self.post('/api/auth/register/', {**SIGNUP, 'agentCode': ''}).status_code, 201)

    def test_register_then_verify_signs_in(self):
        res = self.post('/api/auth/register/', SIGNUP)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()['email'], 'adewale@example.com')

        user = User.objects.get(email='adewale@example.com')
        self.assertEqual(user.status, Status.PENDING)
        self.assertEqual(user.agent_code, 'AG-1001')
        self.assertTrue(user.public_id.startswith('EMP-'))
        self.assertEqual(len(mail.outbox), 1)

        self.assertEqual(self.client.get('/api/auth/me/').status_code, 403)
        res = self.post('/api/auth/verify-email/', {'email': 'adewale@example.com', 'code': last_code()})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['status'], 'active')
        self.assertEqual(self.client.get('/api/auth/me/').json()['user']['email'], 'adewale@example.com')

        from payments.models import Notification
        user = User.objects.get(email='adewale@example.com')
        welcome = Notification.objects.get(recipient=user)
        self.assertEqual((welcome.title, welcome.link), ('Welcome to Empire Global', '/customer/products'))

    def test_admins_hear_about_new_customers(self):
        from payments.models import Notification
        staff = self.make_user('staff@empireglobal.com', role=Role.ADMIN)
        self.post('/api/auth/register/', SIGNUP)
        self.post('/api/auth/verify-email/', {'email': 'adewale@example.com', 'code': last_code()})
        note = Notification.objects.get(recipient=staff)
        customer = User.objects.get(email='adewale@example.com')
        self.assertEqual((note.title, note.link), ('New customer registered', f'/admin/customers/{customer.public_id}'))

    def test_csrf_is_required_for_anonymous_posts(self):
        res = APIClient(enforce_csrf_checks=True).post('/api/auth/register/', SIGNUP, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_weak_password_rejected(self):
        res = self.post('/api/auth/register/', {**SIGNUP, 'password': '12345678'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('password', res.json())

    def test_existing_active_email_rejected(self):
        self.make_user('adewale@example.com')
        res = self.post('/api/auth/register/', SIGNUP)
        self.assertEqual(res.json()['code'], 'email_taken')

    def test_wrong_code_counts_attempts_then_locks(self):
        self.post('/api/auth/register/', SIGNUP)
        code = last_code()
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.post('/api/auth/verify-email/', {'email': SIGNUP['email'], 'code': wrong}).json()['code'], 'otp_invalid')
        res = self.post('/api/auth/verify-email/', {'email': SIGNUP['email'], 'code': code})
        self.assertEqual(res.json()['code'], 'otp_locked')

    def test_expired_code_rejected(self):
        self.post('/api/auth/register/', SIGNUP)
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        res = self.post('/api/auth/verify-email/', {'email': SIGNUP['email'], 'code': last_code()})
        self.assertEqual(res.json()['code'], 'otp_expired')

    def test_resend_has_cooldown_and_replaces_old_code(self):
        self.post('/api/auth/register/', SIGNUP)
        first = last_code()
        self.assertEqual(self.post('/api/auth/resend-otp/', {'email': SIGNUP['email']}).status_code, 429)

        self.expire_cooldown(User.objects.get())
        self.assertEqual(self.post('/api/auth/resend-otp/', {'email': SIGNUP['email']}).status_code, 200)
        second = last_code()
        if first != second:
            res = self.post('/api/auth/verify-email/', {'email': SIGNUP['email'], 'code': first})
            self.assertEqual(res.json()['code'], 'otp_invalid')
        res = self.post('/api/auth/verify-email/', {'email': SIGNUP['email'], 'code': second})
        self.assertEqual(res.status_code, 200)

    def test_otp_stored_hashed(self):
        self.post('/api/auth/register/', SIGNUP)
        self.assertNotIn(last_code(), EmailOTP.objects.get().code_hash)


class LoginTests(AuthTestBase):
    def test_login_logout(self):
        self.make_user()
        res = self.post('/api/auth/login/', {'email': 'CUSTOMER@example.com', 'password': PASSWORD})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['user']['role'], 'customer')
        self.assertEqual(self.post('/api/auth/logout/').status_code, 204)
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 403)

    def test_bad_password_and_unknown_email_look_the_same(self):
        self.make_user()
        a = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': 'nope'})
        b = self.post('/api/auth/login/', {'email': 'ghost@example.com', 'password': 'nope'})
        self.assertEqual(a.status_code, 401)
        self.assertEqual(a.json(), b.json())

    def test_suspended_customer_blocked(self):
        self.make_user(status=Status.SUSPENDED)
        res = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        self.assertEqual(res.json()['code'], 'account_inactive')

    def test_unverified_login_resends_code(self):
        self.make_user(status=Status.PENDING)
        res = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()['code'], 'email_not_verified')
        self.assertEqual(len(mail.outbox), 1)

    def test_portals_only_accept_their_own_accounts(self):
        self.make_user()
        self.make_user('admin@empireglobal.com', role=Role.ADMIN)
        self.assertEqual(self.post('/api/admin/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD}).status_code, 401)
        self.assertEqual(self.post('/api/auth/login/', {'email': 'admin@empireglobal.com', 'password': PASSWORD}).status_code, 401)
        res = self.post('/api/admin/auth/login/', {'email': 'admin@empireglobal.com', 'password': PASSWORD})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['user']['id'].startswith('ADM-'))

    def test_inactive_admin_blocked(self):
        self.make_user('admin@empireglobal.com', role=Role.ADMIN, status=Status.INACTIVE)
        res = self.post('/api/admin/auth/login/', {'email': 'admin@empireglobal.com', 'password': PASSWORD})
        self.assertEqual(res.json()['code'], 'account_inactive')

    def test_login_is_throttled(self):
        for _ in range(10):
            self.post('/api/auth/login/', {'email': 'x@example.com', 'password': 'nope'})
        self.assertEqual(self.post('/api/auth/login/', {'email': 'x@example.com', 'password': 'nope'}).status_code, 429)


class ProfileAndPasswordTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})

    def test_update_profile_ignores_read_only_fields(self):
        res = self.patch('/api/auth/me/', {'fullName': 'New Name', 'phone': '0800', 'role': 'super_admin', 'email': 'x@y.com'})
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual((self.user.full_name, self.user.phone, self.user.role, self.user.email),
                         ('New Name', '0800', Role.CUSTOMER, 'customer@example.com'))

    def test_change_password_keeps_session(self):
        res = self.post('/api/auth/change-password/', {'currentPassword': 'wrong', 'newPassword': 'An0ther-Str0ng!'})
        self.assertEqual(res.json()['code'], 'invalid_password')
        res = self.post('/api/auth/change-password/', {'currentPassword': PASSWORD, 'newPassword': 'An0ther-Str0ng!'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

    def test_password_reset_flow(self):
        self.post('/api/auth/logout/')
        unknown = self.post('/api/auth/password-reset/', {'email': 'ghost@example.com'})
        res = self.post('/api/auth/password-reset/', {'email': 'customer@example.com'})
        self.assertEqual(res.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)

        uid, token = re.search(r'/reset-password\?uid=([\w-]+)&token=([\w-]+)', mail.outbox[0].body).groups()
        res = self.post('/api/auth/password-reset/confirm/', {'uid': uid, 'token': token, 'password': 'Brand-New-Pass9'})
        self.assertEqual(res.status_code, 200)
        # Token is single use
        res = self.post('/api/auth/password-reset/confirm/', {'uid': uid, 'token': token, 'password': 'Brand-New-Pass10'})
        self.assertEqual(res.json()['code'], 'reset_link_invalid')
        res = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': 'Brand-New-Pass9'})
        self.assertEqual(res.status_code, 200)


class ConsoleBackendTests(TestCase):
    def test_prints_readable_email(self):
        import io
        from .mail import ConsoleEmailBackend
        from django.core.mail import EmailMessage

        stream = io.StringIO()
        link = 'http://localhost:5173/reset-password?uid=OA&token=abc-123'
        sent = ConsoleEmailBackend(stream=stream).send_messages([
            EmailMessage('Reset', f'Your code is 042436.\n{link}', 'from@example.com', ['to@example.com']),
        ])
        output = stream.getvalue()
        self.assertEqual(sent, 1)
        self.assertIn('To:      to@example.com', output)
        self.assertIn('Your code is 042436.', output)
        self.assertIn(link, output)  # not quoted-printable encoded


class ConsoleBackendEncodingTests(TestCase):
    def test_naira_sign_on_a_console_that_cannot_print_it(self):
        import io
        from django.core.mail import EmailMessage
        from .mail import ConsoleEmailBackend

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252')
        sent = ConsoleEmailBackend(stream=stream).send_messages([
            EmailMessage('Deposit confirmed: ₦25,000.00', 'Amount: ₦25,000.00', 'from@example.com', ['to@example.com']),
        ])
        stream.flush()
        self.assertEqual(sent, 1)
        self.assertIn('Amount: NGN 25,000.00', raw.getvalue().decode('cp1252'))


class ResendBackendTests(TestCase):
    @override_settings(MAILERS={'default': {
        'BACKEND': 'accounts.mail.ResendEmailBackend', 'OPTIONS': {'api_key': 're_test_key'},
    }})
    def test_posts_message_to_resend_api(self):
        import json
        from unittest import mock
        from django.core.mail import send_mail

        with mock.patch('urllib.request.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"id": "x"}'
            send_mail('Subject', 'Body', 'Empire <no-reply@example.com>', ['to@example.com'], html_message='<p>Body</p>')

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.resend.com/emails')
        self.assertEqual(request.get_header('Authorization'), 'Bearer re_test_key')
        self.assertEqual(json.loads(request.data), {
            'from': 'Empire <no-reply@example.com>', 'to': ['to@example.com'],
            'subject': 'Subject', 'text': 'Body', 'html': '<p>Body</p>',
        })

    def test_requires_api_key(self):
        from .mail import ResendEmailBackend
        with self.assertRaises(ValueError):
            ResendEmailBackend(api_key='')


NEXT_OF_KIN = {'fullName': 'Bola Alao', 'phone': '0802', 'relationship': 'Spouse', 'address': '1 Marina, Lagos'}


class NextOfKinTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})

    def test_save_next_of_kin(self):
        res = self.patch('/api/auth/me/', {'nextOfKin': {**NEXT_OF_KIN, 'extra': 'dropped'}})
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.next_of_kin, NEXT_OF_KIN)

    def test_incomplete_next_of_kin_rejected(self):
        res = self.patch('/api/auth/me/', {'nextOfKin': {**NEXT_OF_KIN, 'address': ' '}})
        self.assertEqual(res.status_code, 400)
        self.assertIn('address', res.json()['nextOfKin'])


class AdminManagementTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.super_admin = self.make_user('boss@empireglobal.com', role=Role.SUPER_ADMIN)
        self.admin = self.make_user('staff@empireglobal.com', role=Role.ADMIN)

    def login(self, email, password=PASSWORD):
        res = self.post('/api/admin/auth/login/', {'email': email, 'password': password})
        self.assertEqual(res.status_code, 200)

    def url(self, user):
        return f'/api/admin/admins/{user.public_id}/'

    def delete(self, url):
        return self.client.delete(url, HTTP_X_CSRFTOKEN=self.csrf())

    def test_only_super_admins_manage_admins(self):
        self.assertEqual(self.client.get('/api/admin/admins/').status_code, 403)
        self.login('staff@empireglobal.com')
        self.assertEqual(self.client.get('/api/admin/admins/').status_code, 403)
        self.assertEqual(self.post('/api/admin/admins/', {}).status_code, 403)
        self.post('/api/auth/logout/')
        self.make_user()
        self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        self.assertEqual(self.client.get('/api/admin/admins/').status_code, 403)

    def test_create_admin_who_can_then_log_in(self):
        self.login('boss@empireglobal.com')
        res = self.post('/api/admin/admins/', {
            'fullName': 'New Admin', 'email': 'New@EmpireGlobal.com', 'role': 'admin', 'password': 'Temp-Pass-123',
        })
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()['admin']['status'], 'active')
        self.assertNotIn('password', res.json()['admin'])
        self.assertEqual(len(self.client.get('/api/admin/admins/').json()['admins']), 3)

        self.post('/api/auth/logout/')
        self.login('new@empireglobal.com', 'Temp-Pass-123')

    def test_create_validates(self):
        self.login('boss@empireglobal.com')
        res = self.post('/api/admin/admins/', {'fullName': 'X', 'email': 'staff@empireglobal.com', 'role': 'customer', 'password': ''})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(set(res.json()), {'email', 'role'})
        self.assertEqual(res.json()['email'], ['An account with this email already exists.'])
        res = self.post('/api/admin/admins/', {'fullName': 'X', 'email': 'STAFF@empireglobal.com', 'role': 'admin', 'password': 'Temp-Pass-123'})
        self.assertIn('email', res.json())
        res = self.post('/api/admin/admins/', {'fullName': 'X', 'email': 'x@empireglobal.com', 'role': 'admin', 'password': '123'})
        self.assertIn('password', res.json())

    def test_deactivate_signs_admin_out_and_blocks_login(self):
        other = APIClient(enforce_csrf_checks=True)
        other.get('/api/auth/csrf/')
        other.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD}, format='json',
                   HTTP_X_CSRFTOKEN=other.cookies['csrftoken'].value)
        self.assertEqual(other.get('/api/auth/me/').status_code, 200)

        self.login('boss@empireglobal.com')
        self.assertEqual(self.patch(self.url(self.admin), {'status': 'inactive'}).status_code, 200)
        self.assertEqual(other.get('/api/auth/me/').status_code, 403)

        self.post('/api/auth/logout/')
        res = self.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD})
        self.assertEqual(res.json()['code'], 'account_inactive')

    def test_super_admin_cannot_demote_deactivate_or_delete_self(self):
        self.login('boss@empireglobal.com')
        self.assertIn('role', self.patch(self.url(self.super_admin), {'role': 'admin'}).json())
        self.assertIn('status', self.patch(self.url(self.super_admin), {'status': 'inactive'}).json())
        self.assertEqual(self.delete(self.url(self.super_admin)).json()['code'], 'cannot_delete_self')
        # Editing their own name is fine
        self.assertEqual(self.patch(self.url(self.super_admin), {'fullName': 'The Boss'}).status_code, 200)

    def test_edit_password_and_delete(self):
        self.login('boss@empireglobal.com')
        res = self.patch(self.url(self.admin), {'role': 'super_admin', 'password': 'Fresh-Pass-456'})
        self.assertEqual(res.status_code, 200)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, Role.SUPER_ADMIN)
        self.assertTrue(self.admin.check_password('Fresh-Pass-456'))
        self.assertEqual(self.delete(self.url(self.admin)).status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.admin.pk).exists())

    def test_customers_are_not_reachable_through_admin_endpoints(self):
        customer = self.make_user()
        self.login('boss@empireglobal.com')
        self.assertEqual(self.patch(self.url(customer), {'role': 'admin'}).status_code, 404)
        self.assertEqual(self.delete(self.url(customer)).status_code, 404)


class CustomerAdminTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.customer = self.make_user()
        self.make_user('staff@empireglobal.com', role=Role.ADMIN)

    def url(self, user=None):
        return f'/api/admin/customers/{(user or self.customer).public_id}/'

    def test_customers_cannot_list_customers(self):
        self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        self.assertEqual(self.client.get('/api/admin/customers/').status_code, 403)

    def test_admin_lists_and_suspends_customer(self):
        self.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD})
        customers = self.client.get('/api/admin/customers/').json()['customers']
        self.assertEqual([c['id'] for c in customers], [self.customer.public_id])

        # Only the status is writable here
        res = self.patch(self.url(), {'status': 'suspended', 'fullName': 'Hacked', 'role': 'super_admin'})
        self.assertEqual(res.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.status, self.customer.full_name, self.customer.role),
                         (Status.SUSPENDED, 'Test User', Role.CUSTOMER))

        self.post('/api/auth/logout/')
        res = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        self.assertEqual(res.json()['code'], 'account_inactive')

    def test_status_rules(self):
        pending = self.make_user('new@example.com', status=Status.PENDING)
        self.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD})
        self.assertEqual(self.patch(self.url(pending), {'status': 'active'}).status_code, 400)
        self.assertEqual(self.patch(self.url(), {'status': 'inactive'}).status_code, 400)


class LockoutTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()

    def fail(self, times):
        for _ in range(times):
            res = self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': 'wrong'})
        return res

    def login(self, password=PASSWORD):
        return self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': password})

    def test_five_wrong_passwords_lock_the_account(self):
        self.assertEqual(self.fail(4).json()['code'], 'invalid_credentials')
        res = self.fail(1)
        self.assertEqual(res.json()['code'], 'account_locked')
        self.assertGreater(res.json()['retryAfter'], 0)
        # Even the right password is refused while locked
        cache.clear()
        self.assertEqual(self.login().json()['code'], 'account_locked')

    def test_correct_password_resets_the_count(self):
        self.fail(4)
        self.assertEqual(self.login().status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_count, 0)

    def test_lock_expires(self):
        self.fail(5)
        cache.clear()
        User.objects.filter(pk=self.user.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.login().status_code, 200)

    def test_password_reset_unlocks(self):
        self.fail(5)
        cache.clear()
        self.post('/api/auth/password-reset/', {'email': 'customer@example.com'})
        uid, token = re.search(r'uid=([\w-]+)&token=([\w-]+)', mail.outbox[-1].body).groups()
        self.post('/api/auth/password-reset/confirm/', {'uid': uid, 'token': token, 'password': 'Brand-New-Pass9'})
        self.assertEqual(self.login('Brand-New-Pass9').status_code, 200)


@override_settings(ADMIN_TWO_FACTOR=True)
class AdminTwoFactorTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user('staff@empireglobal.com', role=Role.ADMIN)

    def password_step(self):
        return self.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD})

    def admin_code(self):
        return re.search(r'sign-in code is (\d{6})', mail.outbox[-1].body).group(1)

    def verify(self, code):
        return self.post('/api/admin/auth/verify-code/', {'code': code})

    def test_password_alone_does_not_sign_in(self):
        res = self.password_step()
        self.assertEqual(res.status_code, 202)
        self.assertTrue(res.json()['twoFactorRequired'])
        self.assertEqual(res.json()['email'], 's****@empireglobal.com')
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 403)

        self.assertEqual(self.verify(self.admin_code()).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me/').json()['user']['email'], 'staff@empireglobal.com')

    def test_code_only_works_in_the_session_that_passed_the_password(self):
        self.password_step()
        code = self.admin_code()
        other = APIClient(enforce_csrf_checks=True)
        other.get('/api/auth/csrf/')
        res = other.post('/api/admin/auth/verify-code/', {'code': code}, format='json',
                         HTTP_X_CSRFTOKEN=other.cookies['csrftoken'].value)
        self.assertEqual(res.json()['code'], 'two_factor_expired')

    def test_wrong_codes_lock_the_code(self):
        self.password_step()
        code = self.admin_code()
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            self.verify(wrong)
        self.assertEqual(self.verify(code).json()['code'], 'otp_locked')

    def test_window_expires(self):
        self.password_step()
        session = self.client.session
        session['admin_2fa'] = {**session['admin_2fa'], 'expires': 0}
        session.save()
        self.assertEqual(self.verify(self.admin_code()).json()['code'], 'two_factor_expired')

    def test_deactivated_during_window(self):
        self.password_step()
        User.objects.filter(pk=self.admin.pk).update(status=Status.INACTIVE)
        self.assertEqual(self.verify(self.admin_code()).json()['code'], 'two_factor_expired')

    def test_resend(self):
        self.password_step()
        self.assertEqual(self.post('/api/admin/auth/resend-code/').status_code, 429)
        EmailOTP.objects.update(created_at=timezone.now() - timedelta(minutes=2))
        self.assertEqual(self.post('/api/admin/auth/resend-code/').status_code, 200)
        self.assertEqual(self.verify(self.admin_code()).status_code, 200)

    def test_customers_skip_two_factor(self):
        self.make_user()
        self.assertEqual(self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD}).status_code, 200)


class MustChangePasswordTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.boss = self.make_user('boss@empireglobal.com', role=Role.SUPER_ADMIN)
        self.post('/api/admin/auth/login/', {'email': 'boss@empireglobal.com', 'password': PASSWORD})

    def test_new_admin_must_choose_own_password(self):
        self.post('/api/admin/admins/', {'fullName': 'New', 'email': 'new@empireglobal.com', 'role': 'admin', 'password': 'Temp-Pass-123'})
        self.post('/api/auth/logout/')

        res = self.post('/api/admin/auth/login/', {'email': 'new@empireglobal.com', 'password': 'Temp-Pass-123'})
        self.assertTrue(res.json()['user']['mustChangePassword'])
        res = self.client.get('/api/admin/customers/')
        self.assertEqual(res.status_code, 403)
        self.assertIn('new password', res.json()['detail'])

        same = self.post('/api/auth/change-password/', {'currentPassword': 'Temp-Pass-123', 'newPassword': 'Temp-Pass-123'})
        self.assertEqual(same.json()['code'], 'same_password')
        res = self.post('/api/auth/change-password/', {'currentPassword': 'Temp-Pass-123', 'newPassword': 'My-Own-Pass-77'})
        self.assertFalse(res.json()['user']['mustChangePassword'])
        self.assertEqual(self.client.get('/api/admin/customers/').status_code, 200)

    def test_password_set_for_someone_else_is_temporary(self):
        staff = self.make_user('staff@empireglobal.com', role=Role.ADMIN)
        self.patch(f'/api/admin/admins/{staff.public_id}/', {'password': 'Temp-Pass-456'})
        self.patch(f'/api/admin/admins/{self.boss.public_id}/', {'password': 'Boss-Pass-789'})
        staff.refresh_from_db()
        self.boss.refresh_from_db()
        self.assertTrue(staff.must_change_password)
        self.assertFalse(self.boss.must_change_password)


class ChangeEmailTests(AuthTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.post('/api/auth/login/', {'email': 'customer@example.com', 'password': PASSWORD})
        mail.outbox.clear()

    def request_change(self, email='New@Example.com', password=PASSWORD):
        return self.post('/api/auth/change-email/', {'newEmail': email, 'password': password})

    def change_code(self):
        return re.search(r'email address is (\d{6})', mail.outbox[0].body).group(1)

    def test_change_email_with_code_from_new_inbox(self):
        res = self.request_change()
        self.assertEqual(res.json()['email'], 'new@example.com')
        to_new, to_old = mail.outbox
        self.assertEqual((to_new.to, to_old.to), (['new@example.com'], ['customer@example.com']))
        # Nothing changes until the code is confirmed
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'customer@example.com')

        res = self.post('/api/auth/change-email/confirm/', {'code': self.change_code()})
        self.assertEqual(res.json()['user']['email'], 'new@example.com')
        self.assertEqual(mail.outbox[-1].to, ['customer@example.com'])
        self.post('/api/auth/logout/')
        self.assertEqual(self.post('/api/auth/login/', {'email': 'new@example.com', 'password': PASSWORD}).status_code, 200)

    def test_requires_password_and_free_address(self):
        self.assertIn('password', self.request_change(password='wrong').json()['errors'])
        self.assertIn('newEmail', self.request_change(email='customer@example.com').json()['errors'])
        self.make_user('taken@example.com')
        self.assertEqual(self.request_change(email='taken@example.com').json()['code'], 'email_taken')

    def test_wrong_code(self):
        self.request_change()
        code = self.change_code()
        wrong = '000000' if code != '000000' else '111111'
        self.assertEqual(self.post('/api/auth/change-email/confirm/', {'code': wrong}).json()['code'], 'otp_invalid')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'customer@example.com')

    def test_address_taken_before_confirming(self):
        self.request_change()
        code = self.change_code()
        self.make_user('new@example.com')
        self.assertEqual(self.post('/api/auth/change-email/confirm/', {'code': code}).json()['code'], 'email_taken')

    def test_admins_cannot_use_it(self):
        self.post('/api/auth/logout/')
        self.make_user('staff@empireglobal.com', role=Role.ADMIN)
        self.post('/api/admin/auth/login/', {'email': 'staff@empireglobal.com', 'password': PASSWORD})
        self.assertEqual(self.request_change().status_code, 403)


class PurgePendingSignupsTests(TestCase):
    def test_removes_only_stale_unverified_customers(self):
        from io import StringIO
        from django.core.management import call_command

        old = timezone.now() - timedelta(hours=72)
        stale = User.objects.create_user('stale@example.com', 'x', full_name='S', status=Status.PENDING, date_joined=old)
        retried = User.objects.create_user('retry@example.com', 'x', full_name='R', status=Status.PENDING, date_joined=old)
        EmailOTP.issue(retried)
        User.objects.create_user('fresh@example.com', 'x', full_name='F', status=Status.PENDING)
        User.objects.create_user('active@example.com', 'x', full_name='A', status=Status.ACTIVE, date_joined=old)

        call_command('purge_pending_signups', '--dry-run', stdout=StringIO())
        self.assertEqual(User.objects.count(), 4)
        call_command('purge_pending_signups', stdout=StringIO())
        self.assertEqual(set(User.objects.values_list('email', flat=True)),
                         {'retry@example.com', 'fresh@example.com', 'active@example.com'})
        self.assertFalse(User.objects.filter(pk=stale.pk).exists())


class CreateSuperuserTests(TestCase):
    def test_createsuperuser_makes_a_ready_super_admin(self):
        import os
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command

        env = {'DJANGO_SUPERUSER_PASSWORD': 'Owner-Pass-2026', 'DJANGO_SUPERUSER_FULL_NAME': 'Site Owner'}
        with mock.patch.dict(os.environ, env):
            call_command('createsuperuser', '--noinput', '--email', 'Owner@Example.com', stdout=StringIO())
        owner = User.objects.get()
        self.assertEqual((owner.email, owner.role, owner.status, owner.email_verified, owner.must_change_password),
                         ('owner@example.com', Role.SUPER_ADMIN, Status.ACTIVE, True, False))
        self.assertTrue(owner.public_id.startswith('ADM-'))
        self.assertTrue(owner.check_password('Owner-Pass-2026'))


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], DEBUG=True, SETUP_TOKEN='')
class SetupSuperAdminTests(TestCase):
    URL = '/api/setup/super-admin/'
    BODY = {'email': 'Owner@Example.com', 'fullName': 'Site Owner', 'password': 'Owner-Pass-2026'}

    def setUp(self):
        cache.clear()
        # Like Postman: no CSRF cookie or token
        self.client = APIClient(enforce_csrf_checks=True)

    def test_creates_first_super_admin_who_can_sign_in(self):
        res = self.client.post(self.URL, self.BODY, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.json()['user']['role'], res.json()['user']['email']), ('super_admin', 'owner@example.com'))
        owner = User.objects.get()
        self.assertTrue(owner.is_superuser and owner.email_verified and owner.status == Status.ACTIVE)
        # The endpoint doesn't sign anyone in
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 403)

        browser = APIClient(enforce_csrf_checks=True)
        browser.get('/api/auth/csrf/')
        with self.settings(ADMIN_TWO_FACTOR=False):
            res = browser.post('/api/admin/auth/login/', {'email': 'owner@example.com', 'password': 'Owner-Pass-2026'},
                               format='json', HTTP_X_CSRFTOKEN=browser.cookies['csrftoken'].value)
        self.assertEqual(res.status_code, 200)

    def test_only_works_once(self):
        self.client.post(self.URL, self.BODY, format='json')
        res = self.client.post(self.URL, {**self.BODY, 'email': 'attacker@example.com'}, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()['code'], 'setup_complete')
        self.assertEqual(User.objects.count(), 1)

    def test_validation(self):
        self.assertIn('fullName', self.client.post(self.URL, {**self.BODY, 'fullName': ' '}, format='json').json())
        self.assertIn('password', self.client.post(self.URL, {**self.BODY, 'password': 'password123'}, format='json').json())
        self.assertIn('email', self.client.post(self.URL, {**self.BODY, 'email': 'not-an-email'}, format='json').json())
        self.assertFalse(User.objects.exists())

    def test_ordinary_admins_dont_block_setup(self):
        User.objects.create_user('staff@example.com', PASSWORD, full_name='Staff', role=Role.ADMIN, status=Status.ACTIVE)
        self.assertEqual(self.client.post(self.URL, self.BODY, format='json').status_code, 201)

    @override_settings(DEBUG=False, SETUP_TOKEN='')
    def test_disabled_in_production_without_token(self):
        res = self.client.post(self.URL, self.BODY, format='json')
        self.assertEqual(res.json()['code'], 'setup_disabled')
        self.assertFalse(User.objects.exists())

    @override_settings(DEBUG=False, SETUP_TOKEN='s3cret-setup-token')
    def test_token_required_when_set(self):
        self.assertEqual(self.client.post(self.URL, self.BODY, format='json').json()['code'], 'invalid_setup_token')
        res = self.client.post(self.URL, self.BODY, format='json', HTTP_X_SETUP_TOKEN='wrong')
        self.assertEqual(res.status_code, 403)
        res = self.client.post(self.URL, self.BODY, format='json', HTTP_X_SETUP_TOKEN='s3cret-setup-token')
        self.assertEqual(res.status_code, 201)
