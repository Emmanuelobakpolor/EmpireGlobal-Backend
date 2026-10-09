import base64
import json
import time
import urllib.error
from unittest import mock

from django.test import override_settings

from payments.models import Notification

from .models import AuthProvider, EmailOTP, Role, Status, User
from .tests import PASSWORD, AuthTestBase

CLIENT_ID = 'test-client.apps.googleusercontent.com'
DETAILS = {'fullName': 'Ada Obi', 'phone': '+234 803 000 0000', 'agentCode': ''}


def id_token(**overrides):
    claims = {'iss': 'https://accounts.google.com', 'aud': CLIENT_ID, 'sub': '1001', 'email': 'Ada@Gmail.com',
              'email_verified': True, 'name': 'Ada Obi', 'exp': time.time() + 300, **overrides}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return f'header.{body}.signature'


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID, GOOGLE_CLIENT_SECRET='secret')
class GoogleSignInTests(AuthTestBase):
    def google(self, **claims):
        with mock.patch('accounts.google._post_form', return_value={'id_token': id_token(**claims)}) as post:
            res = self.post('/api/auth/google/', {'code': 'one-time-code'})
        self.exchange = post
        return res

    def me(self):
        return self.client.get('/api/auth/me/')

    def test_config_exposes_only_the_public_client_id(self):
        res = self.client.get('/api/auth/google/').json()
        self.assertEqual(res, {'enabled': True, 'clientId': CLIENT_ID})
        with override_settings(GOOGLE_CLIENT_SECRET=''):
            self.assertEqual(self.client.get('/api/auth/google/').json(), {'enabled': False, 'clientId': None})
            self.assertEqual(self.post('/api/auth/google/', {'code': 'x'}).status_code, 503)

    def test_code_is_exchanged_with_the_secret_server_side(self):
        self.google()
        url, fields = self.exchange.call_args.args
        self.assertEqual(url, 'https://oauth2.googleapis.com/token')
        self.assertEqual(fields['client_secret'], 'secret')
        self.assertEqual(fields['redirect_uri'], 'postmessage')
        self.assertEqual(fields['code'], 'one-time-code')

    def test_new_customer_completes_profile_and_is_signed_in(self):
        res = self.google()
        self.assertEqual(res.json(), {'isNewUser': True, 'googleProfile': {'email': 'ada@gmail.com', 'fullName': 'Ada Obi'}})
        self.assertFalse(User.objects.filter(email='ada@gmail.com').exists(), 'nothing is created until the profile is done')
        self.assertEqual(self.me().status_code, 403)

        res = self.post('/api/auth/google/complete/', {**DETAILS, 'fullName': 'Ada N. Obi'})
        self.assertEqual(res.status_code, 201, res.content)
        user = User.objects.get(email='ada@gmail.com')
        self.assertEqual((user.full_name, user.status, user.auth_provider, user.google_sub, user.email_verified),
                         ('Ada N. Obi', Status.ACTIVE, AuthProvider.GOOGLE, '1001', True))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(self.me().json()['user']['authProvider'], 'google')
        self.assertTrue(Notification.objects.filter(recipient=user, title='Welcome to Empire Global').exists())

        # The sign-up can't be replayed from the same session
        self.assertEqual(self.post('/api/auth/google/complete/', DETAILS).status_code, 400)

    def test_complete_needs_a_google_identity_in_the_session(self):
        res = self.post('/api/auth/google/complete/', DETAILS)
        self.assertEqual(res.json()['code'], 'google_signup_expired')

    def test_complete_validates_details(self):
        self.google()
        res = self.post('/api/auth/google/complete/', {'fullName': ' ', 'phone': '', 'agentCode': 'AG-9999'})
        self.assertEqual(set(res.json()), {'fullName', 'phone', 'agentCode'})

    def test_expired_signup_window(self):
        self.google()
        session = self.client.session
        session['google_signup']['expires'] = time.time() - 1
        session.save()
        self.assertEqual(self.post('/api/auth/google/complete/', DETAILS).json()['code'], 'google_signup_expired')

    def test_existing_customer_is_signed_in_and_linked(self):
        user = self.make_user('ada@gmail.com')
        res = self.google()
        self.assertEqual(res.json()['user']['id'], user.public_id)
        user.refresh_from_db()
        self.assertEqual(user.google_sub, '1001')
        self.assertTrue(user.check_password(PASSWORD), 'their password still works')
        self.assertEqual(self.me().status_code, 200)

    def test_linked_account_is_found_even_after_an_email_change(self):
        user = self.make_user('new-address@example.com')
        User.objects.filter(pk=user.pk).update(google_sub='1001')
        self.assertEqual(self.google().json()['user']['id'], user.public_id)

    def test_email_linked_to_another_google_account_is_refused(self):
        user = self.make_user('ada@gmail.com')
        User.objects.filter(pk=user.pk).update(google_sub='9999')
        self.assertEqual(self.google().json()['code'], 'google_mismatch')
        self.assertEqual(self.me().status_code, 403)

    def test_admins_and_suspended_customers_are_refused(self):
        self.make_user('ada@gmail.com', role=Role.ADMIN)
        self.assertEqual(self.google().json()['code'], 'google_admin')
        self.assertEqual(self.google(sub='2', email='sus@gmail.com').status_code, 200)  # new: fine
        self.make_user('suspended@gmail.com', status=Status.SUSPENDED)
        self.assertEqual(self.google(sub='3', email='suspended@gmail.com').json()['code'], 'account_inactive')
        self.assertEqual(self.me().status_code, 403)

    def test_unverified_email_signup_is_taken_over_without_its_password(self):
        # Someone registered this address with a password but never proved they own it
        pending = self.make_user('ada@gmail.com', status=Status.PENDING)
        EmailOTP.issue(pending)
        self.assertTrue(self.google().json()['isNewUser'])
        self.assertEqual(self.post('/api/auth/google/complete/', DETAILS).status_code, 201)
        user = User.objects.get(email='ada@gmail.com')
        self.assertEqual(user.pk, pending.pk)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.email_otps.exists())

    def test_untrustworthy_tokens_are_refused(self):
        for claims in ({'aud': 'someone-else'}, {'iss': 'https://evil.example.com'}, {'email_verified': False},
                       {'exp': time.time() - 10}, {'email': ''}):
            res = self.google(**claims)
            self.assertEqual(res.json()['code'], 'google_failed', claims)
        self.assertEqual(self.me().status_code, 403)

    def test_google_rejecting_the_code(self):
        failure = urllib.error.HTTPError('https://oauth2.googleapis.com/token', 400, 'Bad Request', {}, None)
        with mock.patch('accounts.google._post_form', side_effect=failure):
            res = self.post('/api/auth/google/', {'code': 'used-code'})
        self.assertEqual(res.json()['code'], 'google_failed')

    def test_requires_csrf(self):
        res = self.client.post('/api/auth/google/', {'code': 'x'}, format='json')
        self.assertEqual(res.status_code, 403)

    def test_google_customers_cannot_sign_in_with_a_password(self):
        self.google()
        self.post('/api/auth/google/complete/', DETAILS)
        self.post('/api/auth/logout/')
        res = self.post('/api/auth/login/', {'email': 'ada@gmail.com', 'password': ''})
        self.assertNotEqual(res.status_code, 200)

    def test_google_customers_can_set_a_password_through_the_reset_email(self):
        from django.core import mail
        self.google()
        self.assertFalse(self.post('/api/auth/google/complete/', DETAILS).json()['user']['hasPassword'])
        mail.outbox.clear()
        self.post('/api/auth/password-reset/', {'email': 'ada@gmail.com'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/reset-password?uid=', mail.outbox[0].body)
