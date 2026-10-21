"""Loans and hire-purchase: credit facilities the customer repays by instalments.

1. The customer applies with documents, a guarantor (and the item, for hire-purchase), how they
   want to repay, and their own bank account. Nothing is paid at this point.
2. Admins recommend; a Super Admin approves (setting the interest rate) or rejects.
3. An admin pays the loan into the customer's account, or delivers the item, and records the
   transfer reference or delivery note. The repayment schedule starts that day.
4. As each instalment comes due, the customer is shown the collection account to pay into,
   pays, and uploads the receipt. Admins recommend; a Super Admin confirms it. Once every
   instalment is confirmed the facility is complete (and a hire-purchase item is theirs).

Interest is flat: a percent of the amount, charged once whatever the term. It starts at the
product's rate (DEFAULT_INTEREST_RATE when the product has none), and a Super Admin can change it
on approval or at any time until the facility is fully repaid.

Fees (legal, inspection and application) start at the product's (DEFAULT_FEES when it has none)
and are fixed by the Super Admin on approval; the inspection fee depends on where the customer is.
A loan's fees come off the payout (the customer still repays on the full amount); hire-purchase has
nothing to pay out, so its fees are added to what's repaid.
"""

import math
from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
from django.db.models import F, Sum
from django.utils import timezone

from accounts.models import Role, User
from core import audit

from . import emails
from .models import (
    BankAccount,
    FacilityStatus,
    Notification,
    ProductType,
    Repayment,
    RepaymentEvent,
    RepaymentFrequency,
    SlipEvent,
    Transaction,
)
from .notify import customer_link, notify, notify_admins

# Collection account details appear this many days before an instalment is due
REPAYMENT_NOTICE_DAYS = 7
# Percent of the amount, for products without a rate of their own
DEFAULT_INTEREST_RATE = Decimal('13')
# Legal fee is a percent of the amount; inspection and application are naira
DEFAULT_FEES = {'legalPercent': Decimal('1'), 'inspection': Decimal('2000'), 'application': Decimal('3000')}
ROLE_LABELS = {Role.SUPER_ADMIN: 'Super Admin', Role.ADMIN: 'Admin'}
CENT = Decimal('0.01')

FREQUENCY_WORDS = [
    ('daily', RepaymentFrequency.DAILY),
    ('weekly', RepaymentFrequency.WEEKLY),
    ('monthly', RepaymentFrequency.MONTHLY),
    ('one-time', RepaymentFrequency.ONCE),
    ('once', RepaymentFrequency.ONCE),
]


class FacilityError(Exception):
    def __init__(self, message, code, field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def naira(amount):
    return f'₦{Decimal(amount):,.0f}'


def admin_link(txn):
    return f'/admin/facilities/{txn.reference}'


def facility_word(txn):
    return 'hire-purchase' if txn.product_type == ProductType.HIRE_PURCHASE else 'loan'


# ---- Terms ----

def repayment_frequencies(frequency_text):
    """The repayment frequencies a product offers, read from its frequency text
    ("Daily, Weekly or Monthly", "One-time on payday"). Monthly when it names none."""
    text = (frequency_text or '').lower()
    found = []
    for word, value in FREQUENCY_WORDS:
        if word in text and value not in found:
            found.append(value)
    return found or [RepaymentFrequency.MONTHLY.value]


def product_rate(product):
    """The rate an application is priced at unless a Super Admin sets another."""
    if product is not None and product.interest_rate is not None:
        return product.interest_rate
    return DEFAULT_INTEREST_RATE


def total_repayable(amount, rate):
    interest = Decimal(amount) * Decimal(rate) / 100
    return (Decimal(amount) + interest).quantize(CENT, ROUND_HALF_UP)


def product_fees(product):
    """{legalPercent, inspection, application}: the product's own, or the defaults."""
    own = (product.fees if product is not None else None) or {}
    return {key: Decimal(str(own[key])) if own.get(key) is not None else value for key, value in DEFAULT_FEES.items()}


def fees_total(txn):
    return sum((f or Decimal(0) for f in (txn.legal_fee, txn.inspection_fee, txn.application_fee)), Decimal(0))


def facility_total(txn, rate):
    """What the customer repays at `rate`: hire-purchase adds its fees; a loan's come off the payout."""
    total = total_repayable(txn.amount, rate)
    return total + fees_total(txn) if txn.product_type == ProductType.HIRE_PURCHASE else total


def payout_amount(txn):
    """What a loan pays into the customer's account, after fees."""
    return txn.amount - fees_total(txn) if txn.product_type == ProductType.LOAN else None


def split(total, count):
    """`total` in `count` even amounts; the last takes any rounding."""
    each = (Decimal(total) / count).quantize(CENT, ROUND_HALF_UP)
    return [each] * (count - 1) + [Decimal(total) - each * (count - 1)]


def add_months(start, months):
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def schedule(total, term_months, frequency, start):
    """[(due date, amount)] splitting `total` evenly; the last instalment takes any rounding."""
    term_months = term_months or 1
    end = add_months(start, term_months)
    if frequency == RepaymentFrequency.ONCE:
        dates = [end]
    elif frequency == RepaymentFrequency.MONTHLY:
        dates = [add_months(start, i) for i in range(1, term_months + 1)]
    else:
        step = 1 if frequency == RepaymentFrequency.DAILY else 7
        count = max(1, math.ceil((end - start).days / step))
        dates = [start + timedelta(days=step * i) for i in range(1, count + 1)]
    return list(zip(dates, split(total, len(dates))))


def collection_account(txn):
    account = BankAccount.for_product_type(txn.product_type)
    return account.as_payment_details() if account else None


# ---- Where an instalment stands ----

def is_revealed(rep, today=None):
    """Whether the customer is shown where to pay: once the due date is near, and until it's paid."""
    today = today or timezone.localdate()
    return rep.status != Repayment.Status.PAID and rep.due_date <= today + timedelta(days=REPAYMENT_NOTICE_DAYS)


def repayment_state(rep, today=None):
    today = today or timezone.localdate()
    if rep.status == Repayment.Status.PAID:
        return 'paid'
    if rep.status == Repayment.Status.PENDING:
        return 'pending'
    if rep.due_date < today:
        return 'overdue'
    if is_revealed(rep, today):
        return 'due'
    return 'upcoming'


def amount_paid(txn):
    return txn.repayments.filter(status=Repayment.Status.PAID).aggregate(s=Sum('amount'))['s'] or Decimal(0)


# ---- Application review (called from services when the transaction is a facility) ----

def _slip_event(txn, actor, action, note=''):
    SlipEvent.objects.create(transaction=txn, action=action, actor=actor, actor_name=actor.full_name,
                             actor_role=ROLE_LABELS.get(actor.role, actor.role), note=note)


def _audit(actor, txn, action, status, details, reference=None):
    audit.record(actor, action, details, reference=reference or txn.reference, status=status,
                 agent_code=txn.customer.agent_code)


def approve_application(txn, super_admin, note='', interest_rate=None, fees=None):
    """Final approval of a locked, undecided application, at `interest_rate` and `fees`
    ({legalPercent, inspection, application}; any left out are the product's)."""
    from catalog.models import Product

    product = Product.objects.filter(code=txn.product_id).first()
    if interest_rate is None:
        interest_rate = product_rate(product)
    fees = {**product_fees(product), **{k: Decimal(v) for k, v in (fees or {}).items() if v is not None}}
    txn.legal_fee_percent = fees['legalPercent']
    txn.legal_fee = (txn.amount * fees['legalPercent'] / 100).quantize(CENT, ROUND_HALF_UP)
    txn.inspection_fee = fees['inspection'].quantize(CENT)
    txn.application_fee = fees['application'].quantize(CENT)
    if txn.product_type == ProductType.LOAN and payout_amount(txn) <= 0:
        raise FacilityError(f'The fees ({naira(fees_total(txn))}) would take the whole loan of {naira(txn.amount)}.',
                            'fees_too_high', 'inspectionFee')
    txn.interest_rate = Decimal(interest_rate)
    txn.total_repayable = facility_total(txn, txn.interest_rate)
    txn.status = Transaction.Status.APPROVED
    txn.facility_status = FacilityStatus.AWAITING_DISBURSEMENT
    txn.decided_at = timezone.now()
    txn.save(update_fields=['interest_rate', 'total_repayable', 'status', 'facility_status', 'decided_at',
                            'legal_fee_percent', 'legal_fee', 'inspection_fee', 'application_fee'])

    _slip_event(txn, super_admin, SlipEvent.Action.APPROVED, note)
    kind = facility_word(txn)
    _audit(super_admin, txn, 'Application Approved', 'success',
           f'Approved {txn.customer.full_name}\'s {txn.product_name} application of {naira(txn.amount)} at '
           f'{emails.percent(txn.interest_rate)} interest and {naira(fees_total(txn))} fees; '
           f'{naira(txn.total_repayable)} to repay.')
    if kind == 'loan':
        account = txn.disbursement_account or {}
        next_step = (f'We\'ll pay {naira(payout_amount(txn))} (after {naira(fees_total(txn))} fees) into your '
                     f'{account.get("bankName", "")} account {account.get("accountNumber", "")} shortly.')
    else:
        next_step = (f'We\'ll contact you to arrange delivery of your item. {naira(fees_total(txn))} fees are '
                     'included in your repayments.')
    notify(txn.customer, 'Application approved',
           f'Your {txn.product_name} application ({txn.reference}) was approved. {next_step} '
           f'Total to repay: {naira(txn.total_repayable)}.',
           Notification.Kind.APPLICATION, Notification.Type.SUCCESS, link=customer_link(txn))
    notify_admins(f'{"Loan" if kind == "loan" else "Item"} ready to {"disburse" if kind == "loan" else "deliver"}',
                  f'{txn.customer.full_name}\'s {txn.product_name} ({naira(txn.amount)}, {txn.reference}) was approved.',
                  Notification.Kind.APPLICATION, link=admin_link(txn), exclude=super_admin)
    emails.send_application_approved(txn)
    return txn


def reject_application(txn, super_admin, reason):
    txn.status = Transaction.Status.REJECTED
    txn.facility_status = FacilityStatus.REJECTED
    txn.decided_at = timezone.now()
    txn.rejection_reason = reason
    txn.save(update_fields=['status', 'facility_status', 'decided_at', 'rejection_reason'])
    _slip_event(txn, super_admin, SlipEvent.Action.REJECTED, reason)
    _audit(super_admin, txn, 'Application Rejected', 'error',
           f'Rejected {txn.customer.full_name}\'s {txn.product_name} application of {naira(txn.amount)}. '
           f'Reason: {reason}')
    notify(txn.customer, 'Application not approved',
           f'Your {txn.product_name} application ({txn.reference}) was not approved. Reason: {reason}',
           Notification.Kind.APPLICATION, Notification.Type.ERROR, link=customer_link(txn))
    emails.send_application_rejected(txn, reason)
    return txn


def change_interest_rate(txn, super_admin, rate, note=''):
    """Re-price an approved facility that isn't fully repaid. Confirmed instalments stand; what's
    left of the new total is spread evenly over the instalments still to pay."""
    rate = Decimal(rate)
    with db_transaction.atomic():
        txn = Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)
        if txn.facility_status not in (FacilityStatus.AWAITING_DISBURSEMENT, FacilityStatus.ACTIVE):
            raise FacilityError('The rate can only be changed once the application is approved and until it is '
                                'fully repaid.', 'rate_locked')
        if rate == txn.interest_rate:
            raise FacilityError(f'The rate is already {emails.percent(rate)}.', 'rate_unchanged', 'interestRate')

        old_rate, old_total = txn.interest_rate, txn.total_repayable
        new_total = facility_total(txn, rate)
        unpaid = []
        if txn.facility_status == FacilityStatus.ACTIVE:
            reps = list(txn.repayments.select_for_update())
            # The customer paid what a pending receipt shows; let it be decided before amounts move
            if any(r.status == Repayment.Status.PENDING for r in reps):
                raise FacilityError('Confirm or reject the repayment receipt waiting for review before changing '
                                    'the rate.', 'receipt_pending')
            paid = sum((r.amount for r in reps if r.status == Repayment.Status.PAID), Decimal(0))
            unpaid = [r for r in reps if r.status == Repayment.Status.SCHEDULED]
            if new_total - paid < CENT * len(unpaid):
                raise FacilityError(f'{naira(paid)} has already been repaid, so a total of {naira(new_total)} '
                                    'leaves nothing for the remaining instalments.', 'rate_too_low', 'interestRate')
            for rep, amount in zip(unpaid, split(new_total - paid, len(unpaid))):
                rep.amount = amount
            Repayment.objects.bulk_update(unpaid, ['amount'])
            if txn.product_type == ProductType.LOAN:
                User.objects.filter(pk=txn.customer_id).update(
                    outstanding_loan=F('outstanding_loan') + (new_total - old_total))

        txn.interest_rate = rate
        txn.total_repayable = new_total
        txn.save(update_fields=['interest_rate', 'total_repayable'])

        _audit(super_admin, txn, 'Interest Rate Changed', 'warning',
               f'Changed the interest on {txn.customer.full_name}\'s {txn.product_name} from '
               f'{emails.percent(old_rate)} to {emails.percent(rate)}; total to repay {naira(old_total)} → '
               f'{naira(new_total)}.' + (f' Note: {note}' if note else ''))
        each = f' Your remaining instalments are now about {naira(unpaid[0].amount)} each.' if unpaid else ''
        notify(txn.customer, 'Interest rate updated',
               f'The interest on {txn.product_name} ({txn.reference}) is now {emails.percent(rate)}. '
               f'Total to repay: {naira(new_total)}.{each}',
               Notification.Kind.APPLICATION, link=customer_link(txn))
        emails.send_interest_rate_changed(txn, old_rate, unpaid)
    return txn


# ---- Disbursement ----

def disburse(txn, admin, reference):
    """The loan has been paid out (or the item delivered): start the repayment schedule."""
    reference = reference.strip()
    with db_transaction.atomic():
        txn = Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)
        if txn.facility_status != FacilityStatus.AWAITING_DISBURSEMENT:
            raise FacilityError('Only an approved application waiting to be disbursed can be marked disbursed.',
                                'not_awaiting_disbursement')
        if not reference:
            raise FacilityError('Enter the transfer reference or delivery note.', 'reference_required', 'reference')

        now = timezone.now()
        start = timezone.localdate()
        plan = schedule(txn.total_repayable, txn.term_months, txn.repayment_frequency, start)
        Repayment.objects.bulk_create([
            Repayment(facility=txn, number=i, due_date=due, amount=amount)
            for i, (due, amount) in enumerate(plan, start=1)
        ])
        txn.facility_status = FacilityStatus.ACTIVE
        txn.disbursed_at = now
        txn.disbursement_reference = reference[:100]
        # The repayment period runs from disbursement to the last instalment
        txn.start_date = start
        txn.end_date = plan[-1][0]
        txn.save(update_fields=['facility_status', 'disbursed_at', 'disbursement_reference', 'start_date', 'end_date'])
        if txn.product_type == ProductType.LOAN:
            User.objects.filter(pk=txn.customer_id).update(outstanding_loan=F('outstanding_loan') + txn.total_repayable)

        kind = facility_word(txn)
        first_due, first_amount = plan[0]
        what = 'Loan disbursed' if kind == 'loan' else 'Item delivered'
        _audit(admin, txn, what, 'success',
               f'{what}: {txn.product_name} for {txn.customer.full_name} '
               f'({naira(payout_amount(txn)) + " paid out of " if kind == "loan" else ""}{naira(txn.amount)}). '
               f'Reference: {txn.disbursement_reference}. {len(plan)} instalment(s) scheduled.')
        notify(txn.customer, what,
               (f'{naira(payout_amount(txn))} has been paid into your account for {txn.product_name}. '
                if kind == 'loan' else f'Your {txn.product_name} item has been delivered. ')
               + f'Your first repayment of {naira(first_amount)} is due on {first_due:%d %b %Y}.',
               Notification.Kind.APPLICATION, Notification.Type.SUCCESS, link=customer_link(txn))
        emails.send_facility_started(txn, plan)
    return txn


# ---- Repayments ----

def _locked_repayment(rep):
    return Repayment.objects.select_for_update().select_related('facility__customer').get(pk=rep.pk)


def _repayment_event(rep, actor, action, note=''):
    RepaymentEvent.objects.create(repayment=rep, action=action, actor=actor, actor_name=actor.full_name,
                                  actor_role=ROLE_LABELS.get(actor.role, 'Customer'), note=note)


def upload_repayment_receipt(customer, rep, upload):
    with db_transaction.atomic():
        rep = _locked_repayment(rep)
        txn = rep.facility
        if txn.customer_id != customer.pk:
            raise FacilityError('Repayment not found.', 'not_found')
        if txn.facility_status != FacilityStatus.ACTIVE:
            raise FacilityError('This facility is not being repaid.', 'not_active')
        if rep.status == Repayment.Status.PAID:
            raise FacilityError('This instalment has already been confirmed.', 'already_paid')
        if not is_revealed(rep):
            raise FacilityError(f'Payment details for this instalment open {REPAYMENT_NOTICE_DAYS} days before it is due.',
                                'not_due_yet')
        old = rep.receipt.name if rep.receipt else None
        replacing = rep.status == Repayment.Status.PENDING
        rep.receipt = upload
        rep.receipt_name = upload.name[:255]
        rep.receipt_size = upload.size
        rep.receipt_uploaded_at = timezone.now()
        rep.paid_to = collection_account(txn)
        rep.rejection_reason = ''
        rep.status = Repayment.Status.PENDING
        rep.save()
        if old and old != rep.receipt.name:
            rep.receipt.storage.delete(old)
        notify(customer, 'Repayment receipt received',
               f'We received your receipt for instalment {rep.number} of {txn.product_name} ({naira(rep.amount)}). '
               'We\'ll let you know once it has been confirmed.', Notification.Kind.RECEIPT, link=customer_link(txn))
        notify_admins('Replaced repayment receipt to verify' if replacing else 'Repayment receipt to verify',
                      f'{customer.full_name} uploaded a receipt for instalment {rep.number} of {txn.product_name} '
                      f'({naira(rep.amount)}, {rep.reference}).',
                      Notification.Kind.RECEIPT, Notification.Type.WARNING, link=admin_link(txn))
    return rep


def _require_reviewable_repayment(rep):
    if rep.status == Repayment.Status.PAID:
        raise FacilityError('This instalment has already been confirmed.', 'already_paid')
    if rep.status != Repayment.Status.PENDING:
        raise FacilityError('The customer has not uploaded a receipt for this instalment.', 'no_receipt')


def recommend_repayment(rep, admin, approve, note=''):
    with db_transaction.atomic():
        rep = _locked_repayment(rep)
        _require_reviewable_repayment(rep)
        txn = rep.facility
        _repayment_event(rep, admin, 'recommended_approval' if approve else 'recommended_rejection', note)
        _audit(admin, txn, f'Repayment Recommended for {"Approval" if approve else "Rejection"}',
               'info' if approve else 'warning',
               f'Recommended {"confirming" if approve else "rejecting"} instalment {rep.number} of {txn.product_name} '
               f'({naira(rep.amount)}) for {txn.customer.full_name}.' + (f' Note: {note}' if note else ''),
               reference=rep.reference)
        notify_admins('Repayment awaiting your final approval' if approve else 'Repayment flagged for rejection',
                      f'{admin.full_name} recommended {"confirming" if approve else "rejecting"} '
                      f'{txn.customer.full_name}\'s repayment of {naira(rep.amount)} ({rep.reference}).',
                      Notification.Kind.PAYMENT, Notification.Type.INFO if approve else Notification.Type.WARNING,
                      link=admin_link(txn), roles=(Role.SUPER_ADMIN,), exclude=admin)
    return rep


def approve_repayment(rep, super_admin, note=''):
    with db_transaction.atomic():
        rep = _locked_repayment(rep)
        _require_reviewable_repayment(rep)
        txn = Transaction.objects.select_for_update().select_related('customer').get(pk=rep.facility_id)
        rep.status = Repayment.Status.PAID
        rep.paid_at = timezone.now()
        rep.save(update_fields=['status', 'paid_at'])
        if txn.product_type == ProductType.LOAN:
            User.objects.filter(pk=txn.customer_id).update(outstanding_loan=F('outstanding_loan') - rep.amount)
        _repayment_event(rep, super_admin, 'approved', note)

        outstanding = txn.total_repayable - amount_paid(txn)
        completed = not txn.repayments.exclude(status=Repayment.Status.PAID).exists()
        _audit(super_admin, txn, 'Repayment Approved', 'success',
               f'Confirmed instalment {rep.number} of {txn.product_name} ({naira(rep.amount)}) for '
               f'{txn.customer.full_name}. Outstanding: {naira(outstanding)}.', reference=rep.reference)
        notify(txn.customer, 'Repayment confirmed',
               f'Your repayment of {naira(rep.amount)} for {txn.product_name} (instalment {rep.number}) was confirmed. '
               f'Outstanding: {naira(outstanding)}.',
               Notification.Kind.PAYMENT, Notification.Type.SUCCESS, link=customer_link(txn))
        if completed:
            txn.facility_status = FacilityStatus.COMPLETED
            txn.save(update_fields=['facility_status'])
            _audit(super_admin, txn, 'Facility Fully Repaid', 'success',
                   f'{txn.customer.full_name} has fully repaid {txn.product_name} ({naira(txn.total_repayable)}).')
            notify(txn.customer, 'Fully repaid',
                   f'You have fully repaid {txn.product_name} ({txn.reference}).'
                   + (' Ownership of the item is now yours.' if txn.product_type == ProductType.HIRE_PURCHASE else ''),
                   Notification.Kind.PAYMENT, Notification.Type.SUCCESS, link=customer_link(txn))
        emails.send_repayment_confirmed(rep, outstanding, completed)
    return rep


def reject_repayment(rep, super_admin, reason=''):
    reason = reason.strip() or 'Payment could not be verified.'
    with db_transaction.atomic():
        rep = _locked_repayment(rep)
        _require_reviewable_repayment(rep)
        txn = rep.facility
        # Back to unpaid, so the customer can pay and upload again
        rep.status = Repayment.Status.SCHEDULED
        rep.rejection_reason = reason
        rep.save(update_fields=['status', 'rejection_reason'])
        _repayment_event(rep, super_admin, 'rejected', reason)
        _audit(super_admin, txn, 'Repayment Rejected', 'error',
               f'Rejected the receipt for instalment {rep.number} of {txn.product_name} ({naira(rep.amount)}) for '
               f'{txn.customer.full_name}. Reason: {reason}', reference=rep.reference)
        notify(txn.customer, 'Repayment not confirmed',
               f'We could not confirm your repayment for instalment {rep.number} of {txn.product_name}. '
               f'Reason: {reason}', Notification.Kind.PAYMENT, Notification.Type.ERROR, link=customer_link(txn))
        emails.send_repayment_rejected(rep, reason)
    return rep


# ---- Reminders ----

def send_reminders(customer=None, today=None):
    """Tell customers about instalments coming due (once each) and ones gone overdue (once each).
    Safe to run as often as you like; returns how many customers were reminded."""
    today = today or timezone.localdate()
    unpaid = Repayment.objects.filter(
        facility__facility_status=FacilityStatus.ACTIVE, status=Repayment.Status.SCHEDULED,
    ).select_related('facility__customer')
    if customer is not None:
        unpaid = unpaid.filter(facility__customer=customer)

    reminded = 0
    now = timezone.now()
    due_soon = unpaid.filter(reminded_at__isnull=True, due_date__gte=today,
                             due_date__lte=today + timedelta(days=REPAYMENT_NOTICE_DAYS))
    overdue = unpaid.filter(overdue_notified_at__isnull=True, due_date__lt=today)

    by_facility = {}
    for rep in due_soon:
        by_facility.setdefault(rep.facility, []).append(rep)
    for txn, reps in by_facility.items():
        first = min(reps, key=lambda r: r.due_date)
        more = f' and {len(reps) - 1} more this week' if len(reps) > 1 else ''
        notify(txn.customer, 'Repayment due soon',
               f'Instalment {first.number} of {txn.product_name} ({naira(first.amount)}) is due on '
               f'{first.due_date:%d %b %Y}{more}. Open it to see the account to pay into.',
               Notification.Kind.PAYMENT, Notification.Type.WARNING, link=customer_link(txn))
        emails.send_repayment_reminder(txn, reps, collection_account(txn))
        Repayment.objects.filter(pk__in=[r.pk for r in reps]).update(reminded_at=now)
        reminded += 1

    by_facility = {}
    for rep in overdue:
        by_facility.setdefault(rep.facility, []).append(rep)
    for txn, reps in by_facility.items():
        total = sum((r.amount for r in reps), Decimal(0))
        notify(txn.customer, 'Repayment overdue',
               f'{len(reps)} instalment(s) of {txn.product_name} totalling {naira(total)} are overdue. '
               'Please pay as soon as possible and upload your receipt.',
               Notification.Kind.PAYMENT, Notification.Type.ERROR, link=customer_link(txn))
        notify_admins('Repayment overdue',
                      f'{txn.customer.full_name} has {len(reps)} overdue instalment(s) on {txn.product_name} '
                      f'({naira(total)}, {txn.reference}).', Notification.Kind.PAYMENT, Notification.Type.WARNING,
                      link=admin_link(txn))
        Repayment.objects.filter(pk__in=[r.pk for r in reps]).update(overdue_notified_at=now)
        reminded += 1
    return reminded
