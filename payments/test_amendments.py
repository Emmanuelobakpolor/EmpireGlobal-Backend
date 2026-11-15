from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from core.models import AuditLog

from .models import InterestPayment, Notification, Repayment
from .test_ajo import AjoTestBase
from .test_facilities import FacilityTestBase
from .test_investments import InvestmentTestBase
from .tests import BANK, PASSWORD

EARLIER = date(2026, 3, 10)


class AmendMixin:
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

    def amend(self, txn, action, user=None, **body):
        return self.post(self.client_for(user or self.boss), f'/api/admin/transactions/{txn.reference}/{action}/', body)

    def move(self, txn, start=EARLIER, user=None, **body):
        return self.amend(txn, 'contract-date', user, startDate=start.isoformat(), **body)


class SavingsAmendTests(AmendMixin, InvestmentTestBase):
    def savings(self, **overrides):
        """A 6-month Monthly Savings plan of ₦20,000, approved."""
        txn = self.paid_transaction(**overrides)
        self.approve(txn)
        txn.refresh_from_db()
        return txn

    def invested(self, option):
        txn = self.invest(option)
        self.approve(txn)
        txn.refresh_from_db()
        return txn

    def test_contract_date_moves_the_plan_and_its_interest(self):
        txn = self.savings()
        res = self.move(txn, note='Paid in cash in March')
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()['transaction']
        self.assertEqual((data['startDate'], data['endDate']), ('2026-03-10', '2026-09-10'))
        self.assertEqual(data['investment']['payments'][0]['dueDate'], '2026-09-10')
        log = AuditLog.objects.get(action='Contract Date Changed', reference=txn.reference)
        self.assertIn('Paid in cash in March', log.details)
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Contract date updated').exists())

    def test_monthly_interest_follows_the_new_date(self):
        txn = self.invested('monthly')
        self.move(txn)
        dates = list(txn.interest_payments.values_list('due_date', flat=True))
        self.assertEqual((dates[0], dates[-1]), (date(2026, 4, 10), date(2027, 3, 10)))
        # Backdated payments are due straight away
        self.assertEqual(self.pay(txn.interest_payments.get(number=1)).status_code, 200)

    def test_contract_date_checks(self):
        txn = self.savings()
        self.assertEqual(self.move(txn, user=self.admin).status_code, 403)
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.assertEqual(self.move(txn, tomorrow).json()['code'], 'future_date')
        self.assertEqual(self.move(txn, txn.start_date).json()['code'], 'unchanged')
        # Not started until its payment is approved
        self.assertEqual(self.move(self.paid_transaction()).json()['code'], 'not_started')

    def test_amount_before_approval_is_what_gets_credited(self):
        txn = self.paid_transaction()
        res = self.amend(txn, 'amount', amount=25000, note='Slip shows 25,000')
        self.assertEqual(res.json()['transaction']['amount'], 25000.0)
        self.approve(txn)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('25000'))

    def test_amount_after_approval_moves_the_balance_and_interest(self):
        txn = self.savings()
        before = txn.interest_payments.get().amount
        res = self.amend(txn, 'amount', amount=30000)
        self.assertEqual(res.status_code, 200, res.content)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.savings_balance, Decimal('30000'))
        self.assertEqual(txn.interest_payments.get().amount, before * 3 / 2)
        self.assertTrue(AuditLog.objects.filter(action='Payment Amount Changed', reference=txn.reference).exists())
        # And back down again
        self.amend(txn, 'amount', amount=20000)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.savings_balance, txn.interest_payments.get().amount), (Decimal('20000'), before))

    def test_upfront_interest_already_paid_gets_a_top_up(self):
        txn = self.invested('upfront')
        self.pay(txn.interest_payments.get())
        self.assertEqual(self.amend(txn, 'amount', amount=150000).status_code, 200)
        extra = txn.interest_payments.get(number=2)
        self.assertEqual((extra.amount, extra.status), (Decimal('15000'), InterestPayment.Status.SCHEDULED))
        # Less than the interest already paid earns
        self.assertEqual(self.amend(txn, 'amount', amount=50000).json()['code'], 'interest_overpaid')

    def test_amount_checks(self):
        txn = self.savings()
        self.assertEqual(self.amend(txn, 'amount', self.admin, amount=30000).status_code, 403)
        self.assertEqual(self.amend(txn, 'amount', amount=float(txn.amount)).json()['code'], 'unchanged')
        self.assertIn('amount', self.amend(txn, 'amount', amount=0).json())
        res = self.post(self.client_for(self.customer), '/api/withdrawals/', {
            'planReference': txn.reference, 'amount': float(txn.amount), 'password': PASSWORD, **BANK})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.amend(txn, 'amount', amount=30000).json()['code'], 'withdrawal_pending')
        self.assertEqual(self.move(txn).json()['code'], 'withdrawal_pending')


class FacilityAmendTests(AmendMixin, FacilityTestBase):
    def repay(self, rep, user=None, action='approve', **body):
        return self.post(self.client_for(user or self.boss), f'/api/admin/repayments/{rep.pk}/{action}/', body)

    def pay_in(self, txn, number):
        """The customer's receipt for an instalment, waiting to be confirmed."""
        rep = txn.repayments.get(number=number)
        Repayment.objects.filter(pk=rep.pk).update(due_date=timezone.localdate())
        res = self.upload_repayment(rep)
        self.assertEqual(res.status_code, 200, res.content)
        return rep

    def rows(self, response):
        return response.json()['transaction']['facility']['repayments']

    def test_contract_date_moves_the_repayment_schedule(self):
        txn = self.active_loan()
        self.assertEqual(self.move(txn, user=self.admin).status_code, 403)
        res = self.move(txn)
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()['transaction']
        self.assertEqual([r['dueDate'] for r in self.rows(res)], ['2026-04-10', '2026-05-10', '2026-06-10'])
        self.assertEqual((data['startDate'], data['endDate']), ('2026-03-10', '2026-06-10'))

    def test_no_contract_date_before_disbursement(self):
        txn = self.apply()
        self.approve(txn)
        self.assertEqual(self.move(txn).json()['code'], 'not_started')

    def test_paying_more_on_an_instalment_lowers_the_rest(self):
        txn = self.active_loan()
        total = txn.total_repayable
        each = txn.repayments.get(number=1).amount
        rep = self.pay_in(txn, 1)
        self.assertEqual(self.repay(rep, self.admin, 'amount', amount=1).status_code, 403)
        # Nothing was paid on the second yet
        self.assertEqual(self.repay(txn.repayments.get(number=2), action='amount', amount=1).json()['code'], 'no_payment')

        more = each + 5000
        res = self.repay(rep, action='amount', amount=float(more), note='Paid extra')
        self.assertEqual(res.status_code, 200, res.content)
        amounts = [Decimal(str(r['amount'])) for r in self.rows(res)]
        self.assertEqual((amounts[0], amounts[1], sum(amounts)), (more, (total - more) / 2, total))
        self.assertEqual(self.rows(res)[0]['trail'][-1]['action'], 'amount_changed')

        self.repay(rep)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, total - more)
        # Corrected again after it was confirmed
        self.repay(rep, action='amount', amount=float(each))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, total - each)
        self.assertEqual(self.repay(rep, action='amount', amount=float(total) + 1).json()['code'], 'too_much')

    def test_paying_everything_at_once_completes_the_facility(self):
        txn = self.active_loan()
        rep = self.pay_in(txn, 1)
        res = self.repay(rep, action='amount', amount=float(txn.total_repayable))
        self.assertEqual(len(self.rows(res)), 1)
        res = self.repay(rep)
        self.assertEqual(res.json()['transaction']['facility']['status'], 'completed')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, 0)

    def test_a_shortfall_on_the_last_instalment_is_carried_over(self):
        txn = self.active_loan()
        for number in (1, 2):
            self.repay(self.pay_in(txn, number))
        last = self.pay_in(txn, 3)
        res = self.repay(last, action='amount', amount=float(last.amount - 2000))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([(r['number'], r['amount'], r['state'] == 'paid') for r in self.rows(res)[3:]],
                         [(4, 2000.0, False)])
        self.assertEqual(self.repay(last).json()['transaction']['facility']['status'], 'active')


class AjoAmendTests(AmendMixin, AjoTestBase):
    def test_contract_date_moves_the_cycle_to_that_calendar_month(self):
        txn = self.join(term=2)
        self.record(self.boss, txn, 1)
        res = self.move(txn, date(2026, 9, 17))
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()['transaction']
        self.assertEqual((data['startDate'], data['endDate']), ('2026-09-01', '2026-11-01'))
        rows = data['ajo']['contributions']
        # September and October, with the contribution already paid still the first
        self.assertEqual((len(rows), rows[0]['dueDate'], rows[0]['state'], rows[-1]['dueDate']),
                         (61, '2026-09-01', 'paid', '2026-10-31'))
        self.assertEqual(self.move(txn, date(2026, 9, 2)).json()['code'], 'unchanged')

    def test_amount_is_not_for_ajo(self):
        txn = self.join()
        self.assertEqual(self.amend(txn, 'amount', amount=9000).json()['code'], 'not_a_deposit')
