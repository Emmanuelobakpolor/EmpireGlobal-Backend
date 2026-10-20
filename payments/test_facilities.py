from datetime import date, timedelta
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from .facilities import repayment_frequencies, schedule, send_reminders
from .models import Notification, Repayment, Transaction
from .tests import BANK, GUARANTOR, HP_DOCUMENTS, HP_ITEM, PNG, PaymentTestBase


class FacilityTestBase(PaymentTestBase):
    def apply(self, **overrides):
        """A House Rent Loan (p13): ₦120,000 over 3 months, repaid monthly."""
        body = {'product': 'p13', 'amount': 120000, 'termMonths': 3, 'repaymentFrequency': 'monthly',
                'documents': ['passport', 'proofOfAddress', 'proofOfId', 'applicationForm', 'proofOfIncome',
                              'bankStatement', 'guarantorId'], **overrides}
        res = self.create_application(**body)
        self.assertEqual(res.status_code, 201, res.content)
        return Transaction.objects.get(reference=res.json()['transaction']['reference'])

    def admin_url(self, txn, action):
        return f'/api/admin/transactions/{txn.reference}/{action}/'

    def approve(self, txn, rate=5):
        res = self.post(self.client_for(self.boss), self.admin_url(txn, 'approve'), {'interestRate': rate})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()['transaction']

    def disburse(self, txn, reference='TRF-001'):
        res = self.post(self.client_for(self.admin), self.admin_url(txn, 'disburse'), {'reference': reference})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()['transaction']

    def active_loan(self, **overrides):
        txn = self.apply(**overrides)
        self.approve(txn)
        self.disburse(txn)
        txn.refresh_from_db()
        return txn

    def customer_view(self, txn):
        return self.client_for(self.customer).get(f'/api/transactions/{txn.reference}/').json()['transaction']

    def upload_repayment(self, rep):
        return self.post(self.client_for(self.customer), f'/api/repayments/{rep.pk}/upload-receipt/',
                         {'file': SimpleUploadedFile('slip.png', PNG)}, format='multipart')

    def bring_due(self, rep, days=2):
        Repayment.objects.filter(pk=rep.pk).update(due_date=timezone.localdate() + timedelta(days=days))


class TermsTests(FacilityTestBase):
    def test_frequencies_come_from_the_product_text(self):
        self.assertEqual(repayment_frequencies('Daily, Weekly or Monthly'), ['daily', 'weekly', 'monthly'])
        self.assertEqual(repayment_frequencies('One-time on payday'), ['once'])
        self.assertEqual(repayment_frequencies(''), ['monthly'])

    def test_schedules_split_the_total(self):
        start = date(2026, 1, 31)
        monthly = schedule(Decimal('100000'), 3, 'monthly', start)
        self.assertEqual([d for d, _ in monthly], [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])
        self.assertEqual(sum(a for _, a in monthly), Decimal('100000'))
        self.assertEqual([a for _, a in monthly], [Decimal('33333.33'), Decimal('33333.33'), Decimal('33333.34')])
        self.assertEqual(len(schedule(Decimal('1000'), 1, 'weekly', date(2026, 3, 1))), 5)
        self.assertEqual(len(schedule(Decimal('1000'), 1, 'daily', date(2026, 3, 1))), 31)
        self.assertEqual(schedule(Decimal('1000'), 1, 'once', start), [(date(2026, 2, 28), Decimal('1000'))])


class ApplicationTests(FacilityTestBase):
    def test_application_needs_the_customers_bank_account_and_frequency(self):
        res = self.create_application(disbursementAccount={'bankName': 'GTBank', 'accountNumber': '123', 'accountName': ''})
        self.assertEqual(res.json()['disbursementAccount'],
                         ['Enter the 10-digit NUBAN account number.', 'Account name is required.'])
        res = self.create_application(product='p9', documents=HP_DOCUMENTS, termMonths=6, repaymentFrequency='yearly',
                                      application={'guarantor': GUARANTOR, 'item': HP_ITEM})
        self.assertIn('repaymentFrequency', res.json())
        self.assertFalse(Transaction.objects.exists())

    def test_application_is_under_review_with_nothing_to_pay(self):
        txn = self.customer_view(self.apply())
        self.assertEqual((txn['status'], txn['paymentAccount']), ('pending', None))
        self.assertEqual(txn['facility']['status'], 'under_review')
        self.assertEqual(txn['facility']['repaymentFrequency'], 'monthly')
        self.assertEqual(txn['facility']['disbursementAccount'], BANK)
        self.assertEqual(txn['facility']['repayments'], [])
        self.assertEqual(txn['timeline'][0]['label'], 'Application Submitted')
        # The old pay-then-upload flow doesn't apply
        res = self.upload(self.client_for(self.customer), txn['reference'])
        self.assertEqual(res.json()['code'], 'facility_repayment')

    def test_review_without_receipt_and_rate_is_fixed_on_approval(self):
        txn = self.apply()
        res = self.post(self.client_for(self.admin), self.admin_url(txn, 'recommend'), {'decision': 'approve'})
        self.assertEqual(res.status_code, 200, res.content)
        # Admins can't approve or disburse before approval
        self.assertEqual(self.post(self.client_for(self.admin), self.admin_url(txn, 'approve')).status_code, 403)
        res = self.post(self.client_for(self.admin), self.admin_url(txn, 'disburse'), {'reference': 'X'})
        self.assertEqual(res.json()['code'], 'not_awaiting_disbursement')
        data = self.approve(txn, rate=5)
        # ₦120,000 + 5% flat, whatever the term
        self.assertEqual(data['facility']['totalRepayable'], 126000.0)
        self.assertEqual((data['status'], data['facility']['status']), ('approved', 'awaiting_disbursement'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, 0)
        self.assertIn('Application approved', self.titles())
        self.assertIn('Application you reviewed was approved', [n.title for n in self.admin.notifications.all()])

    def test_default_rate_is_used_when_the_product_has_none(self):
        # The House Rent Loan has no rate of its own
        txn = self.apply()
        data = self.post(self.client_for(self.boss), self.admin_url(txn, 'approve')).json()['transaction']
        self.assertEqual((data['facility']['interestRate'], data['facility']['totalRepayable']), (13.0, 135600.0))
        product = self.client.get('/api/products/').json()['products']
        self.assertEqual(next(p for p in product if p['id'] == 'p13')['effectiveInterestRate'], 13.0)

    def test_product_rate_is_used_when_none_given(self):
        from catalog.models import Product
        Product.objects.filter(code='p13').update(interest_rate=Decimal('2.5'))
        txn = self.apply()
        data = self.post(self.client_for(self.boss), self.admin_url(txn, 'approve')).json()['transaction']
        self.assertEqual((data['facility']['interestRate'], data['facility']['totalRepayable']), (2.5, 123000.0))

    def test_rejection(self):
        txn = self.apply()
        res = self.post(self.client_for(self.boss), self.admin_url(txn, 'reject'), {'note': 'Income too low'})
        data = res.json()['transaction']
        self.assertEqual((data['status'], data['facility']['status'], data['rejectionReason']),
                         ('rejected', 'rejected', 'Income too low'))
        self.assertIn('Application not approved', self.titles())

    def titles(self):
        return list(Notification.objects.filter(recipient=self.customer).values_list('title', flat=True))


class RepaymentTests(FacilityTestBase):
    def test_disbursement_starts_the_schedule(self):
        txn = self.apply()
        self.approve(txn)
        data = self.disburse(txn, 'TRF-778')
        facility = data['facility']
        self.assertEqual((facility['status'], facility['disbursementReference']), ('active', 'TRF-778'))
        self.assertEqual([r['amount'] for r in facility['repayments']], [42000.0, 42000.0, 42000.0])
        self.assertEqual(facility['repayments'][0]['reference'], f'{txn.reference}-R01')
        self.assertEqual(data['endDate'], facility['repayments'][-1]['dueDate'])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, Decimal('126000'))
        # Disbursing twice isn't possible
        res = self.post(self.client_for(self.admin), self.admin_url(txn, 'disburse'), {'reference': 'again'})
        self.assertEqual(res.json()['code'], 'not_awaiting_disbursement')

    def test_payment_account_shows_only_when_due_is_near(self):
        txn = self.active_loan()
        first = txn.repayments.get(number=1)
        # A month away: no account yet, and no early upload
        view = self.customer_view(txn)['facility']['repayments'][0]
        self.assertEqual((view['state'], view['paymentAccount']), ('upcoming', None))
        self.assertEqual(self.upload_repayment(first).json()['code'], 'not_due_yet')

        self.bring_due(first)
        view = self.customer_view(txn)['facility']['repayments'][0]
        self.assertEqual(view['state'], 'due')
        self.assertEqual(view['paymentAccount']['accountNumber'], '3002971322')
        self.assertIsNone(self.customer_view(txn)['facility']['repayments'][1]['paymentAccount'])

    def test_repayment_review_and_completion(self):
        txn = self.active_loan()
        admin, boss = self.client_for(self.admin), self.client_for(self.boss)
        reps = list(txn.repayments.all())
        for rep in reps:
            self.bring_due(rep, days=0)

        first = reps[0]
        # Nothing to review before a receipt
        self.assertEqual(self.post(admin, f'/api/admin/repayments/{first.pk}/recommend/', {'decision': 'approve'})
                         .json()['code'], 'no_receipt')
        res = self.upload_repayment(first)
        self.assertEqual(res.json()['transaction']['facility']['repayments'][0]['state'], 'pending')
        self.assertEqual(self.post(admin, f'/api/admin/repayments/{first.pk}/approve/').status_code, 403)
        self.post(admin, f'/api/admin/repayments/{first.pk}/recommend/', {'decision': 'approve', 'note': 'Seen'})
        res = self.post(boss, f'/api/admin/repayments/{first.pk}/approve/')
        facility = res.json()['transaction']['facility']
        self.assertEqual(facility['repayments'][0]['state'], 'paid')
        self.assertEqual([e['action'] for e in facility['repayments'][0]['trail']], ['recommended_approval', 'approved'])
        self.assertEqual((facility['amountPaid'], facility['outstanding']), (42000.0, 84000.0))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, Decimal('84000'))
        self.assertEqual(self.upload_repayment(first).json()['code'], 'already_paid')

        # A rejected receipt leaves the instalment unpaid, to be paid again
        second = reps[1]
        self.upload_repayment(second)
        res = self.post(boss, f'/api/admin/repayments/{second.pk}/reject/', {'note': 'Blurry'})
        view = res.json()['transaction']['facility']['repayments'][1]
        self.assertEqual((view['state'], view['rejectionReason']), ('due', 'Blurry'))

        for rep in reps[1:]:
            self.upload_repayment(rep)
            self.post(boss, f'/api/admin/repayments/{rep.pk}/approve/')
        txn.refresh_from_db()
        self.assertEqual(txn.facility_status, 'completed')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, 0)
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Fully repaid').exists())

    def test_receipts_are_private(self):
        txn = self.active_loan()
        rep = txn.repayments.get(number=1)
        self.bring_due(rep)
        self.upload_repayment(rep)
        url = f'/api/repayments/{rep.pk}/receipt/'
        self.assertEqual(self.client_for(self.customer).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.admin).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.other).get(url).status_code, 404)
        res = self.post(self.client_for(self.other), f'/api/repayments/{rep.pk}/upload-receipt/',
                        {'file': SimpleUploadedFile('slip.png', PNG)}, format='multipart')
        self.assertEqual(res.status_code, 404)

    def test_hire_purchase_doesnt_touch_the_loan_balance(self):
        txn = self.active_loan(product='p9', documents=HP_DOCUMENTS, termMonths=6, amount=60000,
                               application={'guarantor': GUARANTOR, 'item': HP_ITEM})
        self.assertEqual(txn.repayments.count(), 6)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, 0)
        self.assertEqual(self.customer_view(txn)['timeline'][3]['label'], 'Item Delivered')


class InterestRateChangeTests(FacilityTestBase):
    def change(self, txn, rate, client=None):
        return self.post(client or self.client_for(self.boss), self.admin_url(txn, 'interest-rate'), {'interestRate': rate})

    def test_only_a_super_admin_can_change_it_once_approved(self):
        txn = self.apply()
        self.assertEqual(self.change(txn, 10).json()['code'], 'rate_locked')
        self.approve(txn)
        self.assertEqual(self.change(txn, 10, self.client_for(self.admin)).status_code, 403)
        self.assertEqual(self.change(txn, 5).json()['code'], 'rate_unchanged')

    def test_before_disbursement_it_reprices_the_total(self):
        txn = self.apply()
        self.approve(txn)
        res = self.change(txn, 10)
        self.assertEqual(res.status_code, 200, res.content)
        facility = res.json()['transaction']['facility']
        self.assertEqual((facility['interestRate'], facility['totalRepayable']), (10.0, 132000.0))
        self.assertEqual([r['amount'] for r in self.disburse(txn)['facility']['repayments']], [44000.0] * 3)
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Interest rate updated').exists())

    def test_while_repaying_it_spreads_the_rest_over_unpaid_instalments(self):
        txn = self.active_loan()
        first = txn.repayments.get(number=1)
        self.bring_due(first, days=0)
        self.upload_repayment(first)
        # A receipt under review holds the amounts in place
        self.assertEqual(self.change(txn, 13).json()['code'], 'receipt_pending')
        self.post(self.client_for(self.boss), f'/api/admin/repayments/{first.pk}/approve/')

        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            facility = self.change(txn, 13).json()['transaction']['facility']
        # ₦135,600 in all; ₦42,000 already paid leaves ₦93,600 over two instalments
        self.assertEqual([r['amount'] for r in facility['repayments']], [42000.0, 46800.0, 46800.0])
        self.assertEqual((facility['totalRepayable'], facility['outstanding']), (135600.0, 93600.0))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.outstanding_loan, Decimal('93600'))
        self.assertIn('46,800.00', mail.outbox[0].body)

        # The total can't drop below what's already been repaid
        txn.repayments.filter(number=2).update(status=Repayment.Status.PAID, amount=Decimal('80000'))
        self.assertEqual(self.change(txn, 0).json()['code'], 'rate_too_low')


class ReminderTests(FacilityTestBase):
    def test_reminders_are_sent_once(self):
        txn = self.active_loan()
        first, second = txn.repayments.get(number=1), txn.repayments.get(number=2)
        self.bring_due(first, days=3)
        Repayment.objects.filter(pk=second.pk).update(due_date=timezone.localdate() - timedelta(days=1))
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(send_reminders(), 2)
        titles = list(Notification.objects.filter(recipient=self.customer).values_list('title', flat=True))
        self.assertIn('Repayment due soon', titles)
        self.assertIn('Repayment overdue', titles)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('3002971322', mail.outbox[0].body)
        self.assertEqual(send_reminders(), 0)

    def test_customers_listing_their_transactions_triggers_reminders(self):
        txn = self.active_loan()
        self.bring_due(txn.repayments.get(number=1))
        self.client_for(self.customer).get('/api/transactions/')
        self.assertTrue(Notification.objects.filter(recipient=self.customer, title='Repayment due soon').exists())


class PersonNameTests(PaymentTestBase):
    def test_first_and_last_names_are_required(self):
        client = self.client_for(self.customer)
        res = client.patch('/api/auth/me/', {'fullName': 'Ade'}, format='json',
                           HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(res.json()['fullName'], ['Enter both a first name and a last name.'])
        res = client.patch('/api/auth/me/', {'fullName': '  Ade   Bola  Ojo '}, format='json',
                           HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        user = res.json()['user']
        self.assertEqual((user['fullName'], user['firstName'], user['lastName']), ('Ade Bola Ojo', 'Ade', 'Bola Ojo'))
        res = self.create(nextOfKin={'fullName': 'Bola', 'phone': '0802', 'relationship': 'Spouse', 'address': 'x'})
        self.assertIn('fullName', res.json()['nextOfKin'])
