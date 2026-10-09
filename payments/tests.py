import json
import shutil
import tempfile
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Role, Status, User
from core.models import AuditLog

from .models import ApplicationDocument, BankAccount, Notification, SlipEvent, Transaction

PASSWORD = 'Str0ng-Passw0rd!'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
PDF = b'%PDF-1.4\n' + b'\x00' * 64
NEXT_OF_KIN = {'fullName': 'Bola Alao', 'phone': '0802', 'relationship': 'Spouse', 'address': '1 Marina'}
GUARANTOR = {'fullName': 'Kemi Ade', 'phone': '0803', 'address': '2 Broad St', 'relationship': 'Friend',
             'idType': 'NIN', 'idNumber': '12345678901'}
# Salary Advance (p7) needs the four standard documents, its two extra ones and the guarantor's ID
LOAN_DOCUMENTS = ['passport', 'proofOfAddress', 'proofOfId', 'applicationForm', 'proofOfIncome',
                  'proofOfEmployment', 'guarantorId']
HP_DOCUMENTS = ['passport', 'proofOfAddress', 'proofOfId', 'applicationForm', 'guarantorId']
MEDIA = tempfile.mkdtemp()


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    ADMIN_TWO_FACTOR=False,
    MEDIA_ROOT=MEDIA,
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
)
class PaymentTestBase(TestCase):
    """The product catalogue and collection accounts come from the data migrations."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.customer = self.user('customer@example.com', Role.CUSTOMER)
        self.other = self.user('other@example.com', Role.CUSTOMER)
        self.admin = self.user('staff@empireglobal.com', Role.ADMIN, 'Michael Bassey')
        self.boss = self.user('boss@empireglobal.com', Role.SUPER_ADMIN, 'Sarah Johnson')
        self.client = self.client_for(None)

    def user(self, email, role, name='Test User'):
        return User.objects.create_user(email, PASSWORD, full_name=name, role=role, status=Status.ACTIVE)

    def client_for(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get('/api/auth/csrf/')
        if user:
            url = '/api/admin/auth/login/' if user.is_admin else '/api/auth/login/'
            res = client.post(url, {'email': user.email, 'password': PASSWORD}, format='json',
                              HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
            assert res.status_code == 200, res.content
        return client

    def post(self, client, url, data=None, **kwargs):
        fmt = kwargs.pop('format', 'json')
        return client.post(url, data or {}, format=fmt, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value, **kwargs)

    def create(self, client=None, **overrides):
        """A Monthly Savings (p2) transaction."""
        body = {'productId': 'p2', 'amount': 20000, 'termMonths': 6, 'nextOfKin': NEXT_OF_KIN, 'termsAccepted': True,
                **overrides}
        return self.post(client or self.client_for(self.customer), '/api/transactions/', body)

    def create_application(self, client=None, product='p7', documents=LOAN_DOCUMENTS, **overrides):
        """A loan or hire-purchase application, sent as multipart with its documents."""
        payload = {'productId': product, 'amount': 50000, 'application': {'guarantor': GUARANTOR}, 'termsAccepted': True,
                   **overrides}
        form = {'payload': json.dumps(payload)}
        for key in documents:
            form[f'document.{key}'] = SimpleUploadedFile(f'{key}.pdf', PDF)
        return self.post(client or self.client_for(self.customer), '/api/transactions/', form, format='multipart')

    def upload(self, client, reference, content=PNG, name='slip.png'):
        return self.post(client, f'/api/transactions/{reference}/upload-receipt/',
                         {'file': SimpleUploadedFile(name, content)}, format='multipart')

    def paid(self, response):
        ref = response.json()['transaction']['reference']
        self.assertEqual(self.upload(self.client_for(self.customer), ref).status_code, 200)
        return Transaction.objects.get(reference=ref)

    def paid_transaction(self, **overrides):
        return self.paid(self.create(**overrides))


class CustomerTransactionTests(PaymentTestBase):
    def test_create_uses_the_live_product(self):
        res = self.create(productName='Fake name', productType='investment')
        self.assertEqual(res.status_code, 201)
        txn = res.json()['transaction']
        self.assertRegex(txn['reference'], r'^EMP-SAV-\d{8}-\d{4}$')
        self.assertEqual(txn['id'], txn['reference'])
        # Name and type come from the catalogue, not the request
        self.assertEqual((txn['productName'], txn['productType']), ('Monthly Savings Investment Plan (MSP)', 'savings'))
        self.assertEqual((txn['status'], txn['amount'], txn['termMonths']), ('draft', 20000.0, 6))
        self.assertIsNotNone(txn['endDate'])
        self.assertIsNotNone(txn['termsAcceptedAt'])
        self.assertIsNone(txn['receipt'])
        self.assertNotIn('slipTrail', txn)
        self.assertEqual([s['label'] for s in txn['timeline']][:2], ['Transaction Created', 'Payment Instructions Generated'])

    def test_payment_account_is_fixed_at_creation(self):
        txn = self.create().json()['transaction']
        self.assertEqual(txn['paymentAccount']['accountNumber'], '3005085503')
        # Changing the collection account later doesn't change what this customer was told
        BankAccount.objects.filter(code='ba2').update(account_number='9999999999')
        again = self.client_for(self.customer).get(f'/api/transactions/{txn["reference"]}/').json()['transaction']
        self.assertEqual(again['paymentAccount']['accountNumber'], '3005085503')
        loan = self.create_application().json()['transaction']
        self.assertEqual(loan['paymentAccount']['accountNumber'], '3002971322')

    def test_product_rules(self):
        cases = [
            ({'amount': 0}, 'amount'),
            ({'amount': 4999}, 'amount'),            # below MSP's minimum
            ({'amount': 10_000_001}, 'amount'),      # above its maximum
            ({'productId': 'p999'}, 'productId'),
            ({'termMonths': 5}, 'termMonths'),       # not one of the plan's lengths
            ({'termsAccepted': False}, 'termsAccepted'),
            ({'nextOfKin': None}, 'nextOfKin'),
        ]
        for overrides, field in cases:
            self.assertIn(field, self.create(**overrides).json(), overrides)
        self.assertFalse(Transaction.objects.exists())

    def test_disabled_product_cannot_be_used(self):
        from catalog.models import Product
        Product.objects.filter(code='p2').update(status='disabled')
        self.assertIn('productId', self.create().json())

    def test_open_ended_and_single_term_products(self):
        thrift = self.create(productId='p12', amount=1000, nextOfKin=None, termMonths=6).json()['transaction']
        self.assertEqual((thrift['termMonths'], thrift['endDate']), (None, None))
        loan = self.create_application().json()['transaction']
        self.assertEqual(loan['termMonths'], 1)

    def test_customers_only_see_their_own(self):
        ref = self.create().json()['transaction']['reference']
        other = self.client_for(self.other)
        self.assertEqual(other.get('/api/transactions/').json()['transactions'], [])
        self.assertEqual(other.get(f'/api/transactions/{ref}/').status_code, 404)
        self.assertEqual(other.get(f'/api/transactions/{ref}/receipt/').status_code, 404)
        self.assertEqual(self.upload(other, ref).status_code, 404)

    def test_admins_and_anonymous_cannot_use_customer_endpoints(self):
        self.assertEqual(self.client.get('/api/transactions/').status_code, 403)
        self.assertEqual(self.create(self.client_for(self.admin)).status_code, 403)

    def test_upload_receipt_moves_to_pending_and_can_be_downloaded(self):
        client = self.client_for(self.customer)
        ref = self.create(client).json()['transaction']['reference']
        txn = self.upload(client, ref).json()['transaction']
        self.assertEqual(txn['status'], 'pending')
        self.assertEqual(txn['receipt']['fileName'], 'slip.png')
        self.assertEqual(txn['receipt']['paidTo']['bankName'], 'GTBank')
        self.assertTrue(txn['timeline'][3]['current'])

        res = client.get(txn['receipt']['url'])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'image/png')
        self.assertEqual(b''.join(res.streaming_content), PNG)
        # Stored under a random name, not the customer's
        self.assertNotIn('slip', Transaction.objects.get(reference=ref).receipt.name)

    def test_receipt_validation(self):
        client = self.client_for(self.customer)
        ref = self.create(client).json()['transaction']['reference']
        self.assertIn('file', self.upload(client, ref, content=b'MZ\x90\x00 not an image', name='slip.png').json())
        self.assertIn('file', self.upload(client, ref, content=PNG, name='slip.exe').json())
        self.assertIn('file', self.upload(client, ref, content=PNG + b'\x00' * (5 * 1024 * 1024), name='big.png').json())
        self.assertEqual(self.upload(client, ref, content=PDF, name='slip.pdf').status_code, 200)

    def test_cannot_replace_receipt_after_decision(self):
        txn = self.paid_transaction()
        self.post(self.client_for(self.boss), f'/api/admin/transactions/{txn.reference}/approve/')
        res = self.upload(self.client_for(self.customer), txn.reference)
        self.assertEqual(res.json()['code'], 'already_decided')


class ApplicationDocumentTests(PaymentTestBase):
    def test_loan_application_stores_every_document(self):
        res = self.create_application()
        self.assertEqual(res.status_code, 201, res.content)
        app = res.json()['transaction']['application']
        self.assertEqual(set(app['documents']), set(LOAN_DOCUMENTS) - {'guarantorId'})
        self.assertEqual(app['documents']['proofOfIncome']['label'], 'Proof of income')
        self.assertEqual(app['guarantor']['fullName'], 'Kemi Ade')
        self.assertEqual(app['guarantor']['idDocument']['fileName'], 'guarantorId.pdf')
        self.assertEqual(ApplicationDocument.objects.count(), len(LOAN_DOCUMENTS))

        url = app['documents']['passport']['url']
        self.assertEqual(self.client_for(self.customer).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.admin).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.other).get(url).status_code, 404)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_missing_or_bad_documents_create_nothing(self):
        res = self.create_application(documents=LOAN_DOCUMENTS[:-2])
        self.assertIn('Proof of employment is required.', res.json()['documents'])
        self.assertIn('Guarantor ID is required.', res.json()['documents'])

        payload = {'productId': 'p7', 'amount': 50000, 'application': {'guarantor': GUARANTOR}}
        form = {'payload': json.dumps(payload), **{f'document.{k}': SimpleUploadedFile(f'{k}.pdf', PDF) for k in LOAN_DOCUMENTS}}
        form['document.passport'] = SimpleUploadedFile('passport.png', b'not really a png')
        res = self.post(self.client_for(self.customer), '/api/transactions/', form, format='multipart')
        self.assertTrue(any(e.startswith('Passport photograph:') for e in res.json()['documents']))
        self.assertFalse(Transaction.objects.exists())
        self.assertFalse(ApplicationDocument.objects.exists())

    def test_guarantor_and_item_are_checked(self):
        res = self.create_application(application={'guarantor': {**GUARANTOR, 'idNumber': ''}})
        self.assertIn('guarantor', res.json())
        res = self.create_application(product='p9', documents=HP_DOCUMENTS, termMonths=6,
                                      application={'guarantor': GUARANTOR, 'item': {'category': 'Tricycle', 'description': 'Keke'}})
        self.assertIn('item', res.json())
        res = self.create_application(product='p9', documents=HP_DOCUMENTS, termMonths=6,
                                      application={'guarantor': GUARANTOR, 'item': {'category': 'Phones & gadgets', 'description': 'Galaxy A15'}})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['transaction']['application']['item']['category'], 'Phones & gadgets')


class ReviewTests(PaymentTestBase):
    def url(self, txn, action=''):
        return f'/api/admin/transactions/{txn.reference}/{action + "/" if action else ""}'

    def test_admins_see_all_with_trail(self):
        self.paid_transaction()
        res = self.client_for(self.admin).get('/api/admin/transactions/')
        self.assertEqual(len(res.json()['transactions']), 1)
        self.assertEqual(res.json()['transactions'][0]['slipTrail'], [])
        self.assertEqual(self.client_for(self.customer).get('/api/admin/transactions/').status_code, 403)

    def test_admin_can_view_receipt(self):
        txn = self.paid_transaction()
        res = self.client_for(self.admin).get(f'/api/transactions/{txn.reference}/receipt/')
        self.assertEqual(res.status_code, 200)

    def test_views_are_recorded_once_per_window(self):
        txn = self.paid_transaction()
        admin = self.client_for(self.admin)
        self.assertTrue(self.post(admin, self.url(txn, 'view')).json()['recorded'])
        res = self.post(admin, self.url(txn, 'view'))
        self.assertFalse(res.json()['recorded'])
        trail = res.json()['transaction']['slipTrail']
        self.assertEqual([(s['action'], s['by'], s['role']) for s in trail], [('viewed', 'Michael Bassey', 'Admin')])
        self.assertEqual(AuditLog.objects.filter(action='Payment Slip Viewed').count(), 1)

    def test_full_review_credits_balance_once(self):
        txn = self.paid_transaction(amount=20000)
        admin, boss = self.client_for(self.admin), self.client_for(self.boss)

        res = self.post(admin, self.url(txn, 'recommend'), {'decision': 'approve', 'note': 'Matches'})
        self.assertEqual(res.json()['transaction']['slipTrail'][-1]['note'], 'Matches')
        # Admins can't give the final decision
        self.assertEqual(self.post(admin, self.url(txn, 'approve')).status_code, 403)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, 0)

        res = self.post(boss, self.url(txn, 'approve'), {'note': 'OK'})
        self.assertEqual(res.json()['transaction']['status'], 'approved')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('20000'))

        # Approving again must not credit twice
        self.assertEqual(self.post(boss, self.url(txn, 'approve')).json()['code'], 'already_decided')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('20000'))

        me = self.client_for(self.customer).get('/api/auth/me/').json()['user']
        self.assertEqual((me['savingsBalance'], me['totalBalance']), (20000.0, 20000.0))
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Payment approved').exists())
        actions = list(AuditLog.objects.filter(reference=txn.reference).values_list('action', 'actor_name'))
        self.assertIn(('Payment Approved', 'Sarah Johnson'), actions)
        self.assertIn(('Payment Recommended for Approval', 'Michael Bassey'), actions)

    def test_balance_by_product_type(self):
        boss = self.client_for(self.boss)
        investment = self.paid_transaction(productId='p3', amount=100000, termMonths=12)
        loan = self.paid(self.create_application(amount=10000))
        hire = self.paid(self.create_application(
            product='p9', documents=HP_DOCUMENTS, termMonths=6, amount=30000,
            application={'guarantor': GUARANTOR, 'item': {'category': 'Phones & gadgets', 'description': 'Phone'}}))
        for txn in (investment, loan, hire):
            self.assertEqual(self.post(boss, self.url(txn, 'approve')).status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.investment_balance, self.customer.outstanding_loan, self.customer.savings_balance),
                         (Decimal('100000'), Decimal('10000'), Decimal('0')))

    def test_reject(self):
        txn = self.paid_transaction()
        boss = self.client_for(self.boss)
        body = self.post(boss, self.url(txn, 'reject'), {'note': 'Blurry slip'}).json()['transaction']
        self.assertEqual((body['status'], body['rejectionReason']), ('rejected', 'Blurry slip'))
        self.assertEqual(body['timeline'][-1]['label'], 'Payment Rejected')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, 0)
        self.assertEqual(self.post(boss, self.url(txn, 'approve')).json()['code'], 'already_decided')
        self.assertIn('Blurry slip', Notification.objects.get(recipient=self.customer, title='Payment rejected').message)

    def test_cannot_review_without_receipt(self):
        txn = Transaction.objects.get(reference=self.create().json()['transaction']['reference'])
        self.assertEqual(self.post(self.client_for(self.boss), self.url(txn, 'approve')).json()['code'], 'no_receipt')
        res = self.post(self.client_for(self.admin), self.url(txn, 'recommend'), {'decision': 'approve'})
        self.assertEqual(res.json()['code'], 'no_receipt')

    def test_trail_survives_admin_deletion(self):
        txn = self.paid_transaction()
        self.post(self.client_for(self.admin), self.url(txn, 'recommend'), {'decision': 'reject'})
        self.admin.delete()
        event = SlipEvent.objects.get()
        self.assertEqual((event.actor, event.actor_name), (None, 'Michael Bassey'))


class BankAccountTests(PaymentTestBase):
    URL = '/api/admin/bank-accounts/'

    def test_admins_read_but_only_super_admins_change(self):
        admin = self.client_for(self.admin)
        data = admin.get(self.URL).json()
        self.assertEqual([a['id'] for a in data['accounts']], ['ba1', 'ba2'])
        self.assertEqual(data['assignments']['loan'], 'ba1')
        body = {'bankName': 'Zenith', 'accountName': 'Empire', 'accountNumber': '0123456789'}
        self.assertEqual(self.post(admin, self.URL, body).status_code, 403)
        self.assertEqual(admin.patch(f'{self.URL}ba1/', {'status': 'inactive'}, format='json',
                                     HTTP_X_CSRFTOKEN=admin.cookies['csrftoken'].value).status_code, 403)
        self.assertEqual(self.client_for(self.customer).get(self.URL).status_code, 403)

    def test_super_admin_manages_accounts_and_assignments(self):
        boss = self.client_for(self.boss)
        csrf = lambda: boss.cookies['csrftoken'].value  # noqa: E731
        res = self.post(boss, self.URL, {'bankName': 'Zenith', 'accountName': 'Empire', 'accountNumber': '0123456789'})
        self.assertEqual(res.status_code, 201)
        new = res.json()['accounts'][-1]['id']
        self.assertIn('accountNumber', self.post(boss, self.URL, {'bankName': 'X', 'accountName': 'Y', 'accountNumber': '12'}).json())

        res = boss.put(f'{self.URL}assignments/savings/', {'accountId': new, 'facilityName': 'Savings'}, format='json',
                       HTTP_X_CSRFTOKEN=csrf())
        self.assertEqual(res.json()['assignments']['savings'], new)
        self.assertEqual(self.create().json()['transaction']['paymentAccount']['bankName'], 'Zenith')

        # Deactivated or removed accounts fall back to the default
        boss.patch(f'{self.URL}{new}/', {'status': 'inactive'}, format='json', HTTP_X_CSRFTOKEN=csrf())
        self.assertEqual(self.create().json()['transaction']['paymentAccount']['accountNumber'], '3005085503')
        res = boss.delete(f'{self.URL}{new}/', HTTP_X_CSRFTOKEN=csrf())
        self.assertNotIn('savings', res.json()['assignments'])
        self.assertEqual(boss.put(f'{self.URL}assignments/crypto/', {'accountId': ''}, format='json',
                                  HTTP_X_CSRFTOKEN=csrf()).status_code, 404)
        actions = set(AuditLog.objects.values_list('action', flat=True))
        self.assertTrue({'Bank Account Added', 'Bank Account Assigned', 'Bank Account Deactivated', 'Bank Account Removed'} <= actions)


class AuditLogTests(PaymentTestBase):
    def test_actions_are_logged_by_the_server(self):
        boss = self.client_for(self.boss)
        self.post(boss, '/api/admin/agents/', {'name': 'Kunle', 'phone': '0803', 'location': 'Ikorodu'})
        boss.patch(f'/api/admin/customers/{self.customer.public_id}/', {'status': 'suspended'}, format='json',
                   HTTP_X_CSRFTOKEN=boss.cookies['csrftoken'].value)
        logs = self.client_for(self.admin).get('/api/admin/audit-logs/').json()['auditLogs']
        by_action = {log['action']: log for log in logs}
        self.assertEqual(by_action['Agent Created']['user'], 'Sarah Johnson')
        self.assertEqual(by_action['Agent Created']['role'], 'Super Admin')
        self.assertEqual(by_action['Customer Suspended']['reference'], self.customer.public_id)
        self.assertIn('Admin Signed In', by_action)

    def test_customers_cannot_read_audit_logs(self):
        self.assertEqual(self.client_for(self.customer).get('/api/admin/audit-logs/').status_code, 403)


class NotificationTests(PaymentTestBase):
    def titles(self, user):
        return list(Notification.objects.filter(recipient=user).values_list('title', flat=True))

    def test_list_and_mark_read(self):
        Notification.objects.create(recipient=self.customer, title='A', message='a')
        n2 = Notification.objects.create(recipient=self.customer, title='B', message='b')
        Notification.objects.create(recipient=self.other, title='C', message='c')
        client = self.client_for(self.customer)
        data = client.get('/api/notifications/').json()
        self.assertEqual([n['title'] for n in data['notifications']], ['B', 'A'])
        self.assertEqual(data['unreadCount'], 2)

        self.post(client, f'/api/notifications/{n2.pk}/read/')
        self.assertTrue(Notification.objects.get(pk=n2.pk).read)
        self.post(client, '/api/notifications/read-all/')
        self.assertEqual(Notification.objects.filter(recipient=self.customer, read=False).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.other, read=False).count(), 1)
        # Someone else's notification can't be touched
        other = Notification.objects.get(recipient=self.other)
        self.post(client, f'/api/notifications/{other.pk}/read/')
        self.assertFalse(Notification.objects.get(pk=other.pk).read)

    def test_admins_have_notifications_too(self):
        Notification.objects.create(recipient=self.admin, title='For admin', message='x', link='/admin/payments/X')
        data = self.client_for(self.admin).get('/api/notifications/').json()
        self.assertEqual(data['notifications'][0]['link'], '/admin/payments/X')
        self.assertEqual(self.client.get('/api/notifications/').status_code, 403)

    def test_receipt_upload_notifies_customer_and_admins(self):
        txn = self.paid_transaction()
        receipt = Notification.objects.get(recipient=self.customer, title='Receipt received')
        self.assertEqual((receipt.kind, receipt.link), ('receipt', f'/customer/transactions/{txn.reference}'))
        for admin in (self.admin, self.boss):
            note = Notification.objects.get(recipient=admin, title='New payment slip to verify')
            self.assertEqual(note.link, f'/admin/payments/{txn.reference}')
            self.assertIn(txn.reference, note.message)
        self.assertNotIn('New payment slip to verify', self.titles(self.other))

    def test_inactive_admins_are_not_notified(self):
        User.objects.filter(pk=self.admin.pk).update(status=Status.INACTIVE)
        self.paid_transaction()
        self.assertEqual(self.titles(self.admin), [])

    def test_recommendation_goes_to_super_admins_and_decision_back_to_recommender(self):
        txn = self.paid_transaction()
        self.post(self.client_for(self.admin), f'/api/admin/transactions/{txn.reference}/recommend/', {'decision': 'approve'})
        self.assertIn('Payment awaiting your final approval', self.titles(self.boss))
        self.assertNotIn('Payment awaiting your final approval', self.titles(self.admin))

        self.post(self.client_for(self.boss), f'/api/admin/transactions/{txn.reference}/approve/')
        self.assertIn('Payment you reviewed was approved', self.titles(self.admin))
        self.assertIn('Payment approved', self.titles(self.customer))
        # The Super Admin who decided isn't told about their own decision
        self.assertNotIn('Payment you reviewed was approved', self.titles(self.boss))

    def test_application_notifies_both_sides(self):
        txn = Transaction.objects.get(reference=self.create_application().json()['transaction']['reference'])
        self.assertIn('Application received', self.titles(self.customer))
        note = Notification.objects.get(recipient=self.boss, title='New loan application')
        self.assertEqual((note.kind, note.link), ('application', f'/admin/transactions/{txn.reference}'))


class DepositEmailTests(PaymentTestBase):
    def decide(self, txn, action, note=''):
        # captureOnCommitCallbacks runs the emails that are sent once the decision is saved
        with self.captureOnCommitCallbacks(execute=True):
            return self.post(self.client_for(self.boss), f'/api/admin/transactions/{txn.reference}/{action}/', {'note': note})

    def test_approval_emails_the_customer_with_new_balance(self):
        txn = self.paid_transaction(amount=20000)
        mail.outbox.clear()
        self.decide(txn, 'approve')
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['customer@example.com'])
        self.assertIn('Deposit confirmed', email.subject)
        self.assertIn('₦20,000.00', email.body)
        self.assertIn('Your savings balance is now', email.body)
        self.assertIn(txn.reference, email.body)

    def test_rejection_emails_the_reason(self):
        txn = self.paid_transaction()
        mail.outbox.clear()
        self.decide(txn, 'reject', 'Amount on slip is wrong')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Amount on slip is wrong', mail.outbox[0].body)

    def test_mail_failure_does_not_undo_the_approval(self):
        from unittest import mock
        txn = self.paid_transaction(amount=20000)
        with mock.patch('payments.emails.send_mail', side_effect=OSError('smtp down')):
            res = self.decide(txn, 'approve')
        self.assertEqual(res.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('20000'))

    def test_no_email_when_the_decision_fails(self):
        txn = self.paid_transaction()
        self.decide(txn, 'approve')
        mail.outbox.clear()
        self.assertEqual(self.decide(txn, 'approve').json()['code'], 'already_decided')
        self.assertEqual(mail.outbox, [])
