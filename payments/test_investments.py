from datetime import date, timedelta
from decimal import Decimal

from django.core import mail
from django.utils import timezone

from .investments import schedule, send_due_notices
from .models import InterestPayment, Notification, Transaction
from .tests import BANK, PASSWORD, PaymentTestBase


class InvestmentTestBase(PaymentTestBase):
    def invest(self, option='upfront', amount=100000, **overrides):
        """A One-Year Lump-Sum Investment (p3), paid for and awaiting approval."""
        res = self.create(productId='p3', amount=amount, interestOption=option, disbursementAccount=BANK, **overrides)
        self.assertEqual(res.status_code, 201, res.content)
        return self.paid(res)

    def approve(self, txn, **body):
        res = self.post(self.client_for(self.boss), f'/api/admin/transactions/{txn.reference}/approve/', body)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()['transaction']

    def pay(self, payment, reference='TRF-INT-1', client=None):
        return self.post(client or self.client_for(self.admin), f'/api/admin/interest-payments/{payment.pk}/pay/',
                         {'reference': reference})


class ScheduleTests(InvestmentTestBase):
    def test_the_three_options(self):
        start = date(2026, 1, 31)
        self.assertEqual(schedule(Decimal('100000'), 30, 'upfront', start), [(start, Decimal('30000.00'))])
        monthly = schedule(Decimal('100000'), 36, 'monthly', start)
        self.assertEqual(len(monthly), 12)
        self.assertEqual({a for _, a in monthly}, {Decimal('3000.00')})
        self.assertEqual((monthly[0][0], monthly[-1][0]), (date(2026, 2, 28), date(2027, 1, 31)))
        self.assertEqual(schedule(Decimal('100000'), 42, 'maturity', start), [(date(2027, 1, 31), Decimal('42000.00'))])

    def test_products_offer_the_default_rates(self):
        product = next(p for p in self.client.get('/api/products/').json()['products'] if p['id'] == 'p3')
        self.assertEqual(product['interestRates'], {'upfront': 30.0, 'monthly': 36.0, 'maturity': 42.0, 'partial': 12.0})
        self.assertEqual(product['termOptions'], [12])
        savings = next(p for p in self.client.get('/api/products/').json()['products'] if p['id'] == 'p2')
        self.assertEqual(savings['interestRates'], {'maturity': 15.0, 'partial': 12.0})

    def test_savings_interest_is_pro_rata(self):
        start = date(2026, 1, 31)
        self.assertEqual(schedule(Decimal('100000'), 15, 'maturity', start, 6), [(date(2026, 7, 31), Decimal('7500.00'))])
        self.assertEqual(schedule(Decimal('100000'), 15, 'maturity', start, 24)[0][1], Decimal('30000.00'))


class InvestTests(InvestmentTestBase):
    def test_option_and_payout_account_are_required(self):
        res = self.create(productId='p3', amount=100000, disbursementAccount=None)
        self.assertIn('interestOption', res.json())
        self.assertIn('disbursementAccount', res.json())
        res = self.create(productId='p4', amount=100000, termMonths=36, interestOption='monthly', disbursementAccount=BANK)
        # Every investment is 12 months, whatever was asked for
        self.assertEqual(res.json()['transaction']['termMonths'], 12)

    def test_approval_starts_the_schedule_at_the_options_rate(self):
        txn = self.invest('monthly')
        data = self.approve(txn)
        inv = data['investment']
        self.assertEqual((inv['option'], inv['rate'], inv['totalInterest']), ('monthly', 36.0, 36000.0))
        self.assertEqual([p['amount'] for p in inv['payments']], [3000.0] * 12)
        self.assertEqual(data['startDate'], timezone.localdate().isoformat())
        self.assertEqual(inv['payoutAccount'], BANK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.investment_balance, Decimal('100000'))

    def test_super_admin_can_set_another_rate(self):
        txn = self.invest('maturity')
        inv = self.approve(txn, interestRate=40)['investment']
        self.assertEqual((inv['rate'], inv['payments'][0]['amount']), (40.0, 40000.0))
        self.assertEqual(inv['payments'][0]['dueDate'], txn.start_date.replace(year=txn.start_date.year + 1).isoformat())


class PayInterestTests(InvestmentTestBase):
    def test_upfront_interest_is_due_at_once_and_admins_are_told(self):
        txn = self.invest('upfront')
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.approve(txn)
        self.assertIn('30,000.00', mail.outbox[-1].body)
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Interest payment due').exists())
        # Told once
        self.assertEqual(send_due_notices(), 0)

        payment = txn.interest_payments.get()
        self.assertEqual(self.pay(payment, '').status_code, 400)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.pay(payment)
        self.assertEqual(res.status_code, 200, res.content)
        inv = res.json()['transaction']['investment']
        self.assertEqual((inv['payments'][0]['state'], inv['payments'][0]['payoutReference'], inv['interestPaid']),
                         ('paid', 'TRF-INT-1', 30000.0))
        self.assertIn('TRF-INT-1', mail.outbox[0].body)
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Interest paid').exists())
        self.assertEqual(self.pay(payment).json()['code'], 'already_paid')

    def test_payments_are_only_recorded_once_due_and_by_admins(self):
        txn = self.invest('monthly')
        self.approve(txn)
        first = txn.interest_payments.get(number=1)
        self.assertEqual(self.pay(first).json()['code'], 'not_due_yet')
        self.assertEqual(self.pay(first, client=self.client_for(self.customer)).status_code, 403)
        InterestPayment.objects.filter(pk=first.pk).update(due_date=timezone.localdate())
        self.assertEqual(self.pay(first).status_code, 200)

    def test_admin_list(self):
        self.approve(self.invest('upfront'))
        rows = self.client_for(self.admin).get('/api/admin/interest-payments/').json()['payments']
        self.assertEqual([(r['amount'], r['state'], r['count']) for r in rows], [(30000.0, 'due', 1)])
        self.assertEqual(self.client_for(self.customer).get('/api/admin/interest-payments/').status_code, 403)


class SavingsTests(InvestmentTestBase):
    def test_savings_earn_15_percent_a_year_at_maturity(self):
        res = self.create(amount=100000, termMonths=6)
        self.assertEqual(res.status_code, 201, res.content)
        txn = self.paid(res)
        self.assertEqual(txn.interest_option, 'maturity')
        inv = self.approve(txn)['investment']
        self.assertEqual((inv['rate'], inv['partialRate'], inv['totalInterest']), (15.0, 12.0, 7500.0))
        self.assertEqual([(p['amount'], p['state']) for p in inv['payments']], [(7500.0, 'upcoming')])
        # Savings need somewhere to send the interest
        self.assertIn('disbursementAccount', self.create(disbursementAccount=None).json())


class EarlyTerminationTests(InvestmentTestBase):
    def withdraw(self, txn, amount):
        client = self.client_for(self.customer)
        return self.post(client, '/api/withdrawals/', {
            'planReference': txn.reference, 'amount': amount, 'bankName': 'GTBank', 'accountNumber': '0123456789',
            'accountName': 'Test User', 'password': PASSWORD})

    def test_interest_paid_comes_off_the_capital_and_the_rest_is_cancelled(self):
        txn = self.invest('monthly')
        self.approve(txn)
        # Past the 30-day penalty window, with two monthly payments made
        Transaction.objects.filter(pk=txn.pk).update(start_date=timezone.localdate() - timedelta(days=70))
        for payment in txn.interest_payments.filter(number__in=[1, 2]):
            InterestPayment.objects.filter(pk=payment.pk).update(due_date=timezone.localdate())
            self.pay(payment, f'TRF-{payment.number}')

        quote = self.post(self.client_for(self.customer), '/api/withdrawals/quote/',
                          {'planReference': txn.reference, 'amount': 100000}).json()['quote']
        self.assertEqual((quote['penalty'], quote['interestDeducted'], quote['payoutAmount']), (0.0, 6000.0, 94000.0))

        res = self.withdraw(txn, 100000)
        self.assertEqual(res.status_code, 201, res.content)
        wd = res.json()['withdrawal']
        self.assertEqual((wd['amount'], wd['interestDeducted'], wd['payoutAmount']), (100000.0, 6000.0, 94000.0))
        # While it's being reviewed, no more interest can be recorded
        third = txn.interest_payments.get(number=3)
        InterestPayment.objects.filter(pk=third.pk).update(due_date=timezone.localdate())
        self.assertEqual(self.pay(third).json()['code'], 'withdrawal_pending')

        res = self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{wd["reference"]}/approve/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(txn.interest_payments.filter(status='cancelled').count(), 10)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.investment_balance, 0)

    def test_partial_withdrawal_drops_savings_to_12_percent(self):
        # ₦100,000 for 12 months; ₦40,000 withdrawn in month 9
        txn = self.paid(self.create(amount=100000, termMonths=12))
        self.approve(txn)
        Transaction.objects.filter(pk=txn.pk).update(start_date=timezone.localdate() - timedelta(days=270))
        quote = self.post(self.client_for(self.customer), '/api/withdrawals/quote/',
                          {'planReference': txn.reference, 'amount': 40000}).json()['quote']
        self.assertEqual((quote['kind'], quote['interestRateAfter'], quote['interestDeducted'], quote['payoutAmount']),
                         ('partial', 12.0, 0.0, 40000.0))
        wd = self.withdraw(txn, 40000).json()['withdrawal']
        self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{wd["reference"]}/approve/')
        inv = self.customer_view(txn)['investment']
        # 12% of the ₦60,000 left, at maturity
        self.assertEqual((inv['rate'], inv['totalInterest'], inv['payments'][0]['amount']), (12.0, 7200.0, 7200.0))
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Interest updated').exists())

    def test_partial_withdrawal_on_an_investment_claws_back_interest_paid_above_12_percent(self):
        txn = self.invest('monthly')
        self.approve(txn)
        Transaction.objects.filter(pk=txn.pk).update(start_date=timezone.localdate() - timedelta(days=270))
        # Nine monthly payments of ₦3,000 made
        admin = self.client_for(self.admin)
        for payment in txn.interest_payments.filter(number__lte=9):
            InterestPayment.objects.filter(pk=payment.pk).update(due_date=timezone.localdate())
            self.assertEqual(self.pay(payment, f'TRF-{payment.number}', admin).status_code, 200)
        # Before month 8 only the whole plan could be withdrawn; now ₦40,000 leaves ₦60,000 at 12% = ₦7,200,
        # and ₦27,000 was paid, so ₦19,800 comes off
        wd = self.withdraw(txn, 40000).json()['withdrawal']
        self.assertEqual((wd['kind'], wd['interestDeducted'], wd['payoutAmount']), ('partial', 19800.0, 20200.0))
        self.post(self.client_for(self.boss), f'/api/admin/withdrawals/{wd["reference"]}/approve/')
        inv = self.customer_view(txn)['investment']
        self.assertEqual(inv['rate'], 12.0)
        self.assertEqual([p['state'] for p in inv['payments'][9:]], ['cancelled'] * 3)

    def customer_view(self, txn):
        return self.client_for(self.customer).get(f'/api/transactions/{txn.reference}/').json()['transaction']
