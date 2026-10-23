from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from .ajo import due_dates
from .models import Contribution, Notification, Transaction, Withdrawal
from .tests import BANK, PASSWORD, PaymentTestBase

AJO = 'p17'
PAYOUT_ACCOUNT = {'bankName': 'GTBank', 'accountNumber': '0123456789', 'accountName': 'Test User'}


class AjoTestBase(PaymentTestBase):
    def setUp(self):
        super().setUp()
        self.clients = {}

    def client_for(self, user):
        # One signed-in client per person, so the login rate limit isn't reached
        if user is None:
            return super().client_for(None)
        if user.pk not in self.clients:
            self.clients[user.pk] = super().client_for(user)
        return self.clients[user.pk]

    def join(self, amount=5000, frequency='daily', term=1, **overrides):
        res = self.create(productId=AJO, amount=amount, contributionFrequency=frequency, termMonths=term,
                          nextOfKin=None, disbursementAccount=None, **overrides)
        self.assertEqual(res.status_code, 201, res.content)
        return Transaction.objects.get(reference=res.json()['transaction']['reference'])

    def view(self, txn, user=None):
        user = user or self.customer
        url = f'/api/{"admin/" if user.is_admin else ""}transactions/{txn.reference}/'
        return self.client_for(user).get(url).json()['transaction']

    def contribution(self, txn, number):
        return txn.contributions.get(number=number)

    def act(self, user, contribution, action, body=None):
        return self.post(self.client_for(user), f'/api/admin/contributions/{contribution.pk}/{action}/', body)

    def record(self, user, txn, number, method='cash', **body):
        return self.act(user, self.contribution(txn, number), 'record',
                        {'method': method, 'receivedOn': timezone.localdate().isoformat(), **body})

    def end_cycle(self, txn):
        Transaction.objects.filter(pk=txn.pk).update(end_date=timezone.localdate())
        txn.refresh_from_db()

    def arrange(self, txn, user=None, **body):
        return self.post(self.client_for(user or self.admin), f'/api/admin/transactions/{txn.reference}/payout/',
                         {**PAYOUT_ACCOUNT, **body})


class ScheduleTests(AjoTestBase):
    def test_due_dates(self):
        start = date(2026, 9, 1)
        daily = due_dates(start, 1, 'daily')
        self.assertEqual((len(daily), daily[0], daily[-1]), (30, start, date(2026, 9, 30)))
        weekly = due_dates(start, 1, 'weekly')
        self.assertEqual((len(weekly), weekly[-1]), (5, date(2026, 9, 29)))
        self.assertEqual(due_dates(start, 3, 'monthly'), [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)])

    def test_product_offers_its_frequencies(self):
        product = next(p for p in self.client.get('/api/products/').json()['products'] if p['id'] == AJO)
        self.assertEqual((product['type'], product['contributionFrequencies']), ('ajo', ['daily', 'weekly']))

    def test_frequency_is_required(self):
        res = self.create(productId=AJO, amount=5000, termMonths=1, nextOfKin=None, disbursementAccount=None)
        self.assertIn('contributionFrequency', res.json())
        res = self.create(productId=AJO, amount=5000, termMonths=1, contributionFrequency='monthly', nextOfKin=None)
        self.assertIn('contributionFrequency', res.json())


class JoinTests(AjoTestBase):
    def test_joining_starts_the_cycle_with_nothing_to_pay_in_the_app(self):
        txn = self.join()
        today = timezone.localdate()
        self.assertEqual((txn.status, txn.start_date, txn.paid_to), ('approved', today, None))
        self.assertEqual(txn.contributions.count(), (txn.end_date - today).days)
        self.assertFalse(txn.contributions.filter(status='paid').exists())
        data = self.view(txn)
        self.assertEqual((data['ajo']['state'], data['ajo']['saved'], data['ajo']['paidCount']), ('active', 0.0, 0))
        self.assertEqual(data['timeline'][0]['label'], 'Joined')
        # The customer sees nothing internal about each contribution
        self.assertNotIn('recordedBy', data['ajo']['contributions'][0])
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Ajo plan started').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='New Ajo plan').exists())
        # No receipts from the customer
        self.assertEqual(self.upload(self.client_for(self.customer), txn.reference).json()['code'],
                         'ajo_recorded_by_staff')


class RecordTests(AjoTestBase):
    def test_admin_records_and_a_super_admin_confirms(self):
        txn = self.join()
        res = self.record(self.admin, txn, 1)
        self.assertEqual(res.status_code, 200, res.content)
        row = res.json()['transaction']['ajo']['contributions'][0]
        self.assertEqual((row['state'], row['method'], row['recordedBy']), ('pending', 'cash', 'Michael Bassey'))
        self.assertEqual(self.record(self.admin, txn, 1).json()['code'], 'already_recorded')
        self.assertEqual(self.act(self.admin, self.contribution(txn, 1), 'approve').status_code, 403)
        self.assertEqual(self.act(self.admin, self.contribution(txn, 1), 'recommend', {'decision': 'approve'}).status_code, 200)
        res = self.act(self.boss, self.contribution(txn, 1), 'approve')
        self.assertEqual(res.status_code, 200, res.content)
        ajo = res.json()['transaction']['ajo']
        self.assertEqual((ajo['saved'], ajo['contributions'][0]['state']), (5000.0, 'paid'))
        self.assertEqual([e['action'] for e in ajo['contributions'][0]['trail']],
                         ['recorded', 'recommended_approval', 'approved'])
        # The customer's own Ajo balance, apart from their savings
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.ajo_balance, self.customer.savings_balance), (Decimal('5000'), 0))
        me = self.client_for(self.customer).get('/api/auth/me/').json()['user']
        self.assertEqual((me['ajoBalance'], me['totalBalance']), (5000.0, 5000.0))
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Contribution confirmed').exists())

    def test_super_admin_record_confirms_at_once(self):
        txn = self.join()
        res = self.record(self.boss, txn, 1, method='transfer', reference='TRF-99')
        row = res.json()['transaction']['ajo']['contributions'][0]
        self.assertEqual((row['state'], row['paymentReference']), ('paid', 'TRF-99'))

    def test_rejected_record_can_be_recorded_again(self):
        txn = self.join()
        self.record(self.admin, txn, 1)
        self.act(self.boss, self.contribution(txn, 1), 'reject', {'note': 'No cash in the till'})
        first = self.contribution(txn, 1)
        self.assertEqual((first.status, first.rejection_reason), ('scheduled', 'No cash in the till'))
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Contribution you recorded was rejected').exists())
        self.assertEqual(self.record(self.admin, txn, 1).status_code, 200)

    def test_record_checks(self):
        txn = self.join()
        self.assertIn('reference', self.record(self.admin, txn, 1, method='transfer').json())
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.record(self.admin, txn, 1, receivedOn=tomorrow).json()['code'], 'future_date')
        self.assertEqual(self.record(self.customer, txn, 1).status_code, 403)
        self.end_cycle(txn)
        self.assertEqual(self.record(self.admin, txn, 2).json()['code'], 'cycle_ended')


class PayoutTests(AjoTestBase):
    def test_customers_cannot_withdraw_or_see_ajo_as_withdrawable(self):
        txn = self.join()
        self.record(self.boss, txn, 1)
        self.end_cycle(txn)
        plans = self.client_for(self.customer).get('/api/withdrawals/plans/').json()['plans']
        self.assertNotIn(txn.reference, [p['reference'] for p in plans])
        res = self.post(self.client_for(self.customer), '/api/withdrawals/', {
            'planReference': txn.reference, 'amount': 5000, 'password': PASSWORD, **PAYOUT_ACCOUNT})
        self.assertEqual(res.json()['code'], 'ajo_paid_by_staff')

    def test_no_payout_before_the_cycle_ends(self):
        txn = self.join()
        self.record(self.boss, txn, 1)
        res = self.arrange(txn)
        self.assertEqual(res.json()['code'], 'not_payable')
        self.assertIn('cycle ends', res.json()['detail'])

    def test_admin_arranges_the_payout_less_one_contribution(self):
        txn = self.join()
        self.record(self.boss, txn, 1)
        self.record(self.boss, txn, 2)
        # A contribution still waiting for confirmation holds the payout back
        self.record(self.admin, txn, 3)
        self.end_cycle(txn)
        self.assertEqual(self.arrange(txn).json()['code'], 'not_payable')
        self.act(self.boss, self.contribution(txn, 3), 'approve')
        self.assertEqual(self.arrange(txn, accountNumber='123').status_code, 400)

        res = self.arrange(txn)
        self.assertEqual(res.status_code, 200, res.content)
        payout = res.json()['transaction']['ajo']['payout']
        self.assertEqual((payout['amount'], payout['serviceCharge'], payout['payoutAmount']), (15000.0, 5000.0, 10000.0))
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Ajo payout on its way').exists())
        # The customer sees it, but can't cancel it
        wd = Withdrawal.objects.get(reference=payout['reference'])
        res = self.post(self.client_for(self.customer), f'/api/withdrawals/{wd.reference}/cancel/')
        self.assertEqual(res.json()['code'], 'not_cancellable')
        self.assertEqual(self.arrange(txn).json()['code'], 'not_payable')

        res = self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{wd.reference}/approve/')
        self.assertEqual(res.status_code, 200, res.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.ajo_balance, 0)
        self.post(self.client_for(self.admin), f'/api/admin/withdrawals/{wd.reference}/mark-paid/',
                  {'payoutReference': 'TRF-OUT-1'})

        data = self.view(txn)
        self.assertEqual((data['ajo']['state'], data['ajo']['contributions'][3]['state']), ('paid_out', 'missed'))
        statement = data['ajo']['statement']
        self.assertEqual([(r['credit'], r['debit']) for r in statement],
                         [(5000.0, 0), (5000.0, 0), (5000.0, 0), (0, 5000.0), (0, 10000.0)])
        self.assertEqual(statement[-1]['balance'], 0)
        self.assertEqual([s['label'] for s in data['timeline'] if s['done']],
                         ['Joined', 'Saving', 'Cycle Ended', 'Payout Approved', 'Paid Out'])

    def test_nothing_saved_nothing_to_pay(self):
        txn = self.join()
        self.end_cycle(txn)
        self.assertEqual(self.arrange(txn).json()['code'], 'not_payable')
        self.assertEqual(Contribution.objects.filter(plan=txn, status='paid').count(), 0)
