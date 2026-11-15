"""Corrections a Super Admin makes to a contract that's already on the books.

  change_start_date        The contract really started on another day (often before it was entered
                           here). Its end date and everything scheduled on it move with it.
  change_amount            The customer paid a different amount on a savings plan or investment
                           than the transaction says (usually more). The balance and the interest
                           still to pay follow the new amount.
  change_repayment_amount  The customer paid a different amount on a loan or hire-purchase
                           instalment. What's left of the facility is spread over the instalments
                           still to pay.

Each one is written to the audit log and the customer is told.
"""

from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import F, Max
from django.utils import timezone

from accounts.models import User
from core import audit

from . import ajo, facilities, investments
from .facilities import CENT, add_months, split
from .models import (
    BALANCE_FIELD_BY_TYPE,
    FACILITY_TYPES,
    FacilityStatus,
    InterestOption,
    InterestPayment,
    Notification,
    ProductType,
    Repayment,
    RepaymentEvent,
    Transaction,
    Withdrawal,
)
from .notify import customer_link, notify
from .withdrawal_rules import ACTIVE_STATUSES, withdrawn_from


class AmendError(Exception):
    def __init__(self, message, code, field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def naira(amount):
    return f'₦{Decimal(amount):,.2f}'


def _locked(txn):
    return Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)


def _audit(actor, txn, action, details, reference=None):
    audit.record(actor, action, details, reference=reference or txn.reference, status='warning',
                 agent_code=txn.customer.agent_code)


def _with_note(text, note):
    return text + (f' Note: {note}' if note else '')


def _require_no_pending_withdrawal(txn):
    # What a pending request would pay was worked out on the contract as it stands
    if txn.withdrawals.filter(status=Withdrawal.Status.PENDING).exists():
        raise AmendError('A withdrawal request on this plan is being reviewed. Decide it first.', 'withdrawal_pending')


# ---- The contract date ----

def has_started(txn):
    """Loans and hire-purchase start when they're disbursed; everything else when its payment is approved."""
    if txn.product_type in FACILITY_TYPES:
        return txn.facility_status in (FacilityStatus.ACTIVE, FacilityStatus.COMPLETED)
    return txn.status == Transaction.Status.APPROVED


def change_start_date(txn, super_admin, start, note=''):
    note = note.strip()
    if start > timezone.localdate():
        raise AmendError('The contract date cannot be in the future.', 'future_date', 'startDate')
    with db_transaction.atomic():
        txn = _locked(txn)
        if not has_started(txn):
            raise AmendError("This contract hasn't started yet, so it has no date to change.", 'not_started')
        if ajo.is_ajo(txn):
            # Ajo cycles run on calendar months
            start = ajo.cycle_start(start)
        if start == txn.start_date:
            raise AmendError(f'The contract already starts on {start:%d %b %Y}.', 'unchanged', 'startDate')
        _require_no_pending_withdrawal(txn)

        old_start = txn.start_date
        if txn.is_facility:
            _move_repayments(txn, start)
        elif ajo.is_ajo(txn):
            try:
                ajo.reschedule(txn, start)
            except ajo.AjoError as exc:
                raise AmendError(exc.message, exc.code, exc.field) from exc
        else:
            txn.start_date = start
            txn.end_date = add_months(start, txn.term_months) if txn.term_months else None
            txn.save(update_fields=['start_date', 'end_date'])
            _move_interest(txn)

        ends = f', ending {txn.end_date:%d %b %Y}' if txn.end_date else ''
        _audit(super_admin, txn, 'Contract Date Changed', _with_note(
            f"Changed the start of {txn.customer.full_name}'s {txn.product_name} from {old_start:%d %b %Y} to "
            f'{start:%d %b %Y}{ends}.', note))
        notify(txn.customer, 'Contract date updated',
               f'Your {txn.product_name} ({txn.reference}) now runs from {start:%d %b %Y}{ends}.',
               Notification.Kind.ACCOUNT, link=customer_link(txn))
    return txn


def _move_repayments(txn, start):
    """Every instalment takes its date on a schedule starting `start`; unpaid ones are reminded afresh."""
    reps = list(txn.repayments.select_for_update())
    for rep in reps:
        rep.due_date = facilities.instalment_date(start, txn.term_months, txn.repayment_frequency, rep.number)
        if rep.status != Repayment.Status.PAID:
            rep.reminded_at = rep.overdue_notified_at = None
    Repayment.objects.bulk_update(reps, ['due_date', 'reminded_at', 'overdue_notified_at'])
    txn.start_date = start
    txn.end_date = max((r.due_date for r in reps), default=add_months(start, txn.term_months or 1))
    txn.save(update_fields=['start_date', 'end_date'])


def _move_interest(txn):
    """Interest payments take their dates on the plan's new period; admins are told afresh as they come due."""
    if not investments.earns_interest(txn) or txn.interest_rate is None:
        return
    dates = [due for due, _ in investments.schedule(txn.amount, txn.interest_rate, txn.interest_option,
                                                    txn.start_date, txn.term_months)]
    payments = list(txn.interest_payments.select_for_update())
    for payment in payments:
        # One added after a partial withdrawal sits beyond the original schedule, at maturity
        payment.due_date = dates[payment.number - 1] if payment.number <= len(dates) else txn.end_date
        if payment.status == InterestPayment.Status.SCHEDULED:
            payment.due_notified_at = None
    InterestPayment.objects.bulk_update(payments, ['due_date', 'due_notified_at'])


# ---- The amount paid on a savings plan or investment ----

def change_amount(txn, super_admin, amount, note=''):
    note = note.strip()
    amount = Decimal(amount).quantize(CENT)
    with db_transaction.atomic():
        txn = _locked(txn)
        if txn.product_type in FACILITY_TYPES:
            raise AmendError('A loan or hire-purchase is repaid by instalments. Change the amount paid on the '
                             'instalment instead.', 'not_a_deposit')
        if ajo.is_ajo(txn):
            raise AmendError('An Ajo plan is paid in by contributions, recorded one at a time.', 'not_a_deposit')
        if txn.status == Transaction.Status.REJECTED:
            raise AmendError('This payment was rejected, so there is no amount to change.', 'rejected')
        if amount == txn.amount:
            raise AmendError(f'The amount is already {naira(amount)}.', 'unchanged', 'amount')

        old = txn.amount
        if txn.status == Transaction.Status.APPROVED:
            _reprice_plan(txn, amount)
        txn.amount = amount
        txn.save(update_fields=['amount'])

        _audit(super_admin, txn, 'Payment Amount Changed', _with_note(
            f"Changed the amount paid on {txn.customer.full_name}'s {txn.product_name} from {naira(old)} to "
            f'{naira(amount)}.', note))
        notify(txn.customer, 'Payment amount updated',
               f'The amount on your {txn.product_name} ({txn.reference}) was corrected from {naira(old)} to '
               f'{naira(amount)}.', Notification.Kind.PAYMENT, link=customer_link(txn))
    return txn


def _reprice_plan(txn, amount):
    """The plan's payment was already approved: move the customer's balance by the difference and
    re-work the interest still to pay on the new capital."""
    _require_no_pending_withdrawal(txn)
    if txn.withdrawals.filter(status__in=ACTIVE_STATUSES, kind=Withdrawal.Kind.FULL).exists():
        raise AmendError('This plan has been withdrawn in full, so its amount can no longer change.', 'plan_closed')
    withdrawn = withdrawn_from(txn)
    if amount <= withdrawn:
        raise AmendError(f'{naira(withdrawn)} has already been withdrawn from this plan, so the amount must be '
                         'more than that.', 'below_withdrawn', 'amount')

    field = BALANCE_FIELD_BY_TYPE.get(txn.product_type)
    if field:
        difference = amount - txn.amount
        customer = User.objects.select_for_update().get(pk=txn.customer_id)
        if getattr(customer, field) + difference < 0:
            raise AmendError("The customer's balance is lower than this reduction.", 'insufficient_funds', 'amount')
        User.objects.filter(pk=customer.pk).update(**{field: F(field) + difference})

    if not investments.earns_interest(txn) or txn.interest_rate is None:
        return
    total = investments.interest_for(amount - withdrawn, txn.interest_rate, txn.term_months)
    paid = investments.interest_paid(txn)
    owed = total - paid
    if owed < 0:
        raise AmendError(f'{naira(paid)} interest has already been paid on this plan, more than {naira(amount)} '
                         f'earns ({naira(total)}).', 'interest_overpaid', 'amount')
    scheduled = list(txn.interest_payments.select_for_update().filter(status=InterestPayment.Status.SCHEDULED))
    if scheduled:
        for payment, share in zip(scheduled, split(owed, len(scheduled))):
            payment.amount = share
        InterestPayment.objects.bulk_update(scheduled, ['amount'])
    elif owed > 0:
        # Everything scheduled was already paid (e.g. upfront interest): the extra is one more payment
        upfront = txn.interest_option == InterestOption.UPFRONT
        number = (txn.interest_payments.aggregate(n=Max('number'))['n'] or 0) + 1
        InterestPayment.objects.create(plan=txn, number=number, amount=owed,
                                       due_date=timezone.localdate() if upfront else txn.end_date)


# ---- The amount paid on a loan or hire-purchase instalment ----

def change_repayment_amount(rep, super_admin, amount, note=''):
    """Set what the customer actually paid on an instalment (one with a receipt waiting, or already
    confirmed). The rest of the facility is spread evenly over the instalments still to pay; when
    nothing is left they're dropped, and when none are left to carry a shortfall one is added."""
    note = note.strip()
    amount = Decimal(amount).quantize(CENT)
    with db_transaction.atomic():
        rep = Repayment.objects.select_for_update().get(pk=rep.pk)
        txn = _locked(rep.facility)
        if txn.facility_status != FacilityStatus.ACTIVE:
            raise AmendError('Instalment amounts can only be changed while the facility is being repaid.', 'not_active')
        if rep.status == Repayment.Status.SCHEDULED:
            raise AmendError('No payment has been made on this instalment yet.', 'no_payment')
        if amount == rep.amount:
            raise AmendError(f'The amount is already {naira(amount)}.', 'unchanged', 'amount')

        others = list(txn.repayments.select_for_update().exclude(pk=rep.pk))
        unpaid = [r for r in others if r.status == Repayment.Status.SCHEDULED]
        owed = txn.total_repayable - sum((r.amount for r in others if r.status != Repayment.Status.SCHEDULED), Decimal(0))
        left = owed - amount
        if left < 0:
            raise AmendError(f'That is more than the {naira(owed)} still owed on this facility.', 'too_much', 'amount')

        old = rep.amount
        rep.amount = amount
        rep.save(update_fields=['amount'])
        # Too little left to share out: it all goes on the next instalment
        keep = unpaid if left >= CENT * len(unpaid) else unpaid[:1 if left else 0]
        Repayment.objects.filter(pk__in=[r.pk for r in unpaid if r not in keep]).delete()
        if keep:
            for instalment, share in zip(keep, split(left, len(keep))):
                instalment.amount = share
            Repayment.objects.bulk_update(keep, ['amount'])
        elif left:
            number = max(r.number for r in [rep, *others]) + 1
            Repayment.objects.create(facility=txn, number=number, amount=left,
                                     due_date=max(timezone.localdate(), txn.end_date or rep.due_date))
        txn.end_date = txn.repayments.aggregate(d=Max('due_date'))['d']
        txn.save(update_fields=['end_date'])
        if rep.status == Repayment.Status.PAID and txn.product_type == ProductType.LOAN:
            User.objects.filter(pk=txn.customer_id).update(outstanding_loan=F('outstanding_loan') - (amount - old))

        RepaymentEvent.objects.create(
            repayment=rep, action='amount_changed', actor=super_admin, actor_name=super_admin.full_name,
            actor_role=facilities.ROLE_LABELS.get(super_admin.role, super_admin.role),
            note=_with_note(f'{naira(old)} to {naira(amount)}.', note)[:500])
        _audit(super_admin, txn, 'Repayment Amount Changed', _with_note(
            f'Changed the amount paid on instalment {rep.number} of {txn.customer.full_name}\'s {txn.product_name} '
            f'from {naira(old)} to {naira(amount)}; {naira(left)} is left to pay after it.', note),
               reference=rep.reference)
        notify(txn.customer, 'Repayment amount updated',
               f'Instalment {rep.number} of {txn.product_name} was recorded as {naira(amount)} (was {naira(old)}). '
               + (f'{naira(left)} is left to pay after it.' if left else 'Nothing is left to pay after it.'),
               Notification.Kind.PAYMENT, link=customer_link(txn))
        if rep.status == Repayment.Status.PAID:
            facilities.complete_if_repaid(txn, super_admin)
    return rep
