from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Role, Status, User
from core.models import AuditLog

from .models import Notification, Transaction, Withdrawal
from .withdrawal_rules import add_working_days, quote

PASSWORD = 'Str0ng-Passw0rd!'
TODAY = date(2026, 10, 9)  # a Friday
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=dt_timezone.utc)
BANK = {'bankName': 'Access Bank', 'accountNumber': '0123456789', 'accountName': 'Chidi Okafor'}

PRODUCT_TYPES = {'p3': 'investment', 'p2': 'savings', 'p5': 'thrift', 'p6': 'thrift', 'p12': 'thrift', 'p7': 'loan'}


def plan(customer, code, amount=100000, start=date(2026, 1, 1), end=date(2027, 1, 1), status='approved'):
    """An approved plan on one of the official products (their rules come from the migrations)."""
    return Transaction.objects.create(
        customer=customer, product_id=code, product_name=f'Product {code}', product_type=PRODUCT_TYPES[code],
        amount=amount, start_date=start, end_date=end, status=status,
    )


class RulesTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user('c@example.com', PASSWORD, full_name='C', status=Status.ACTIVE)

    def q(self, txn, amount=None, today=TODAY):
        return quote(txn, amount, today=today, now=NOW)

    def test_lump_sum_before_maturity_is_full_termination_only(self):
        txn = plan(self.customer, 'p3', start=date(2026, 9, 25), end=date(2027, 9, 25))
        self.assertFalse(self.q(txn).partial_allowed)
        self.assertIn('whole remaining amount', self.q(txn, 50000).error)
        # Within 30 days of the start: 5% penalty
        full = self.q(txn, 100000)
        self.assertEqual((full.kind, full.penalty, full.payout_amount), ('full', Decimal('5000.00'), Decimal('95000.00')))
        # After 30 days: no penalty
        later = self.q(txn, 100000, today=date(2026, 11, 30))
        self.assertEqual(later.penalty, 0)

    def test_lump_sum_after_maturity_allows_any_amount(self):
        txn = plan(self.customer, 'p3', start=date(2025, 1, 1), end=date(2026, 1, 1))
        q = self.q(txn, 40000)
        self.assertEqual((q.error, q.kind, q.penalty), ('', 'partial', 0))

    def test_savings_partial_opens_after_month_8(self):
        txn = plan(self.customer, 'p2', start=date(2026, 3, 1), end=date(2027, 3, 1))
        self.assertTrue(self.q(txn, 10000).error)                                   # month 7
        self.assertEqual(self.q(txn, 10000, today=date(2026, 11, 1)).error, '')     # month 8 reached

    def test_notice_and_payout_dates(self):
        self.assertEqual(add_working_days(TODAY, 5), date(2026, 10, 16))            # skips the weekend
        self.assertEqual(self.q(plan(self.customer, 'p3')).earliest_payout_date, date(2026, 10, 16))
        self.assertEqual(self.q(plan(self.customer, 'p5', end=None)).earliest_payout_date, date(2026, 10, 10))  # 24h
        self.assertEqual(self.q(plan(self.customer, 'p6', end=None)).earliest_payout_date, date(2026, 12, 31))  # quarter end
        asa = self.q(plan(self.customer, 'p12', end=None), 500)
        self.assertEqual((asa.error, asa.earliest_payout_date, asa.penalty), ('', TODAY, 0))

    def test_loans_and_unapproved_plans_cannot_be_withdrawn(self):
        self.assertFalse(self.q(plan(self.customer, 'p7')).allowed)
        self.assertFalse(self.q(plan(self.customer, 'p12', end=None, status='pending')).allowed)

    def test_amount_limits(self):
        txn = plan(self.customer, 'p12', end=None, amount=10000)
        self.assertIn('at most', self.q(txn, 10001).error)
        self.assertTrue(self.q(txn, 0).error)


@override_settings(
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    ADMIN_TWO_FACTOR=False,
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
)
class WithdrawalFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        make = lambda email, role, name: User.objects.create_user(  # noqa: E731
            email, PASSWORD, full_name=name, role=role, status=Status.ACTIVE, email_verified=True)
        self.customer = make('chidi@example.com', Role.CUSTOMER, 'Chidi Okafor')
        self.other = make('other@example.com', Role.CUSTOMER, 'Other')
        self.admin = make('staff@empireglobal.com', Role.ADMIN, 'Nora Staff')
        self.boss = make('boss@empireglobal.com', Role.SUPER_ADMIN, 'Bola Boss')
        # An Accessible Savings plan of ₦50,000, already credited
        self.plan = plan(self.customer, 'p12', amount=50000, end=None)
        User.objects.filter(pk=self.customer.pk).update(savings_balance=50000)

    def client_for(self, user):
        client = APIClient(enforce_csrf_checks=True)
        client.get('/api/auth/csrf/')
        url = '/api/admin/auth/login/' if user.is_admin else '/api/auth/login/'
        client.post(url, {'email': user.email, 'password': PASSWORD}, format='json',
                    HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        return client

    def post(self, client, url, data=None):
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(url, data or {}, format='json', HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)

    def request(self, amount=20000, password=PASSWORD, client=None):
        return self.post(client or self.client_for(self.customer), '/api/withdrawals/',
                         {'planReference': self.plan.reference, 'amount': amount, 'password': password, **BANK})

    def titles(self, user):
        return list(Notification.objects.filter(recipient=user).values_list('title', flat=True))

    def test_plans_and_quote(self):
        client = self.client_for(self.customer)
        plans = client.get('/api/withdrawals/plans/').json()['plans']
        self.assertEqual((len(plans), plans[0]['available'], plans[0]['partialAllowed']), (1, 50000.0, True))
        res = self.post(client, '/api/withdrawals/quote/', {'planReference': self.plan.reference, 'amount': 60000})
        self.assertIn('at most', res.json()['quote']['error'])

    def test_request_needs_password_and_valid_amount(self):
        self.assertIn('password', self.request(password='wrong').json()['errors'])
        self.assertIn('amount', self.request(amount=60000).json()['errors'])
        res = self.post(self.client_for(self.customer), '/api/withdrawals/',
                        {'planReference': self.plan.reference, 'amount': 1000, 'password': PASSWORD, **BANK, 'accountNumber': '12'})
        self.assertIn('accountNumber', res.json())
        self.assertFalse(Withdrawal.objects.exists())

    def test_request_notifies_and_emails(self):
        mail.outbox.clear()
        res = self.request()
        self.assertEqual(res.status_code, 201, res.content)
        wd = res.json()['withdrawal']
        self.assertEqual((wd['status'], wd['amount'], wd['payoutAmount']), ('pending', 20000.0, 20000.0))
        self.assertIn('Withdrawal requested', self.titles(self.customer))
        self.assertIn('New withdrawal request', self.titles(self.admin))
        self.assertEqual(mail.outbox[-1].to, ['chidi@example.com'])
        self.assertIn('Withdrawal request received', mail.outbox[-1].subject)

    def test_pending_requests_hold_the_money(self):
        self.request(30000)
        self.assertIn('at most', self.request(30000).json()['detail'])
        self.assertEqual(self.request(20000).status_code, 201)

    def test_full_flow_deducts_once_and_emails_on_paid(self):
        ref = self.request(20000).json()['withdrawal']['reference']
        admin, boss = self.client_for(self.admin), self.client_for(self.boss)
        base = f'/api/admin/withdrawals/{ref}'

        self.post(admin, f'{base}/recommend/', {'decision': 'approve', 'note': 'Account name matches'})
        self.assertIn('Withdrawal awaiting your final approval', self.titles(self.boss))
        self.assertEqual(self.post(admin, f'{base}/approve/').status_code, 403)
        self.assertEqual(self.post(admin, f'{base}/mark-paid/', {'payoutReference': 'X'}).json()['code'], 'not_approved')

        mail.outbox.clear()
        res = self.post(boss, f'{base}/approve/')
        self.assertEqual(res.json()['withdrawal']['status'], 'approved')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('30000'))
        self.assertIn('Withdrawal approved', mail.outbox[-1].subject)
        self.assertIn('Remaining balance: ₦30,000.00', mail.outbox[-1].body)
        self.assertEqual(self.post(boss, f'{base}/approve/').json()['code'], 'already_decided')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('30000'))
        self.assertIn('Withdrawal ready to pay', self.titles(self.admin))
        self.assertIn('Withdrawal you reviewed was approved', self.titles(self.admin))

        mail.outbox.clear()
        res = self.post(admin, f'{base}/mark-paid/', {'payoutReference': 'NIP-000123'})
        self.assertEqual(res.json()['withdrawal']['status'], 'paid')
        email = mail.outbox[-1]
        self.assertIn('Withdrawal paid', email.subject)
        self.assertIn('NIP-000123', email.body)
        self.assertIn('Access Bank · 0123456789', email.body)
        self.assertIn('Withdrawal paid', self.titles(self.customer))
        actions = set(AuditLog.objects.filter(reference=ref).values_list('action', flat=True))
        self.assertEqual(actions, {'Withdrawal Recommended for Approval', 'Withdrawal Approved', 'Withdrawal Paid'})
        trail = [e['action'] for e in admin.get(base + '/').json()['withdrawal']['trail']]
        self.assertEqual(trail, ['recommended_approval', 'approved', 'paid'])

    def test_reject_keeps_balance_and_emails_reason(self):
        ref = self.request().json()['withdrawal']['reference']
        mail.outbox.clear()
        self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{ref}/reject/', {'note': 'Name does not match'})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('50000'))
        self.assertIn('Name does not match', mail.outbox[-1].body)
        # A rejected request no longer holds money on the plan
        self.assertEqual(self.request(50000).status_code, 201)

    def test_cancel(self):
        client = self.client_for(self.customer)
        ref = self.request(client=client).json()['withdrawal']['reference']
        self.assertEqual(self.post(self.client_for(self.other), f'/api/withdrawals/{ref}/cancel/').status_code, 404)
        self.assertEqual(self.post(client, f'/api/withdrawals/{ref}/cancel/').json()['withdrawal']['status'], 'cancelled')
        self.assertEqual(self.post(client, f'/api/withdrawals/{ref}/cancel/').json()['code'], 'not_cancellable')

    def test_approval_refused_if_balance_is_short(self):
        ref = self.request(20000).json()['withdrawal']['reference']
        User.objects.filter(pk=self.customer.pk).update(savings_balance=1000)
        res = self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{ref}/approve/')
        self.assertEqual(res.json()['code'], 'insufficient_funds')
        self.assertEqual(Withdrawal.objects.get().status, 'pending')

    def test_privacy(self):
        self.request()
        self.assertEqual(self.client_for(self.other).get('/api/withdrawals/').json()['withdrawals'], [])
        self.assertEqual(self.client_for(self.customer).get('/api/admin/withdrawals/').status_code, 403)
        self.assertEqual(self.post(self.client_for(self.other), '/api/withdrawals/',
                                   {'planReference': self.plan.reference, 'amount': 100, 'password': PASSWORD, **BANK}
                                   ).json()['code'], 'plan_not_found')
