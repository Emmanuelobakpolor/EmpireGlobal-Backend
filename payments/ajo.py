"""Ajo (Esusu): the customer pays a fixed contribution every day or week for a cycle (1 month or
more) and gets everything saved back at the end, less the collector's charge of one contribution.

The customer only watches: staff do everything else.

1. The customer joins (the contribution, how often and the cycle length) and the cycle starts that
   day, with every contribution scheduled to the end date. Nothing is paid in the app.
2. Staff record each contribution (cash, or a transfer seen in the bank). An Admin's record waits
   for a Super Admin's confirmation; a Super Admin's is confirmed at once. Confirmation credits the
   customer's Ajo balance.
3. When the cycle ends, an admin arranges the payout: a withdrawal of everything saved, less the
   charge (withdrawal_rules.quote), which is then approved and marked paid like any other.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Role, User
from core import audit

from .facilities import add_months, collection_account, repayment_frequencies
from .models import (
    Contribution,
    ContributionEvent,
    Notification,
    ProductType,
    RepaymentFrequency,
    Transaction,
    Withdrawal,
    WithdrawalEvent,
)
from .notify import customer_link, notify, notify_admins

FREQUENCIES = (RepaymentFrequency.DAILY, RepaymentFrequency.WEEKLY, RepaymentFrequency.MONTHLY)
ROLE_LABELS = {Role.SUPER_ADMIN: 'Super Admin', Role.ADMIN: 'Admin'}


class AjoError(Exception):
    def __init__(self, message, code, field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def naira(amount):
    return f'₦{Decimal(amount):,.0f}'


def is_ajo(txn):
    return txn.product_type == ProductType.AJO


def admin_link(txn):
    return f'/admin/ajo/{txn.reference}'


def contribution_frequencies(product):
    """How often a product's plans can be paid into, read from its frequency text. Daily when it names none."""
    found = [f for f in repayment_frequencies(product.frequency) if f in FREQUENCIES]
    return found or [RepaymentFrequency.DAILY.value]


def due_dates(start, term_months, frequency):
    """Every contribution's due date, from the first (on `start`) to the last before the cycle ends."""
    end = add_months(start, term_months or 1)
    if frequency == RepaymentFrequency.MONTHLY:
        return [add_months(start, i) for i in range(term_months or 1)]
    step = timedelta(days=7 if frequency == RepaymentFrequency.WEEKLY else 1)
    dates, day = [], start
    while day < end:
        dates.append(day)
        day += step
    return dates


def saved(txn):
    """What the customer has paid in: every confirmed contribution."""
    return sum((c.amount for c in txn.contributions.all() if c.status == Contribution.Status.PAID), Decimal(0))


def service_charge(txn, total=None):
    """The collector's charge: one contribution per cycle (never more than was saved)."""
    total = saved(txn) if total is None else total
    return min(txn.amount, total)


def has_ended(txn, today=None):
    return bool(txn.end_date) and (today or timezone.localdate()) >= txn.end_date


def contribution_state(c, today=None):
    today = today or timezone.localdate()
    if c.status == Contribution.Status.PAID:
        return 'paid'
    if c.status == Contribution.Status.PENDING:
        return 'pending'
    if has_ended(c.plan, today):
        return 'missed'
    if c.due_date < today:
        return 'overdue'
    if c.due_date == today:
        return 'due'
    return 'upcoming'


def join(customer, product, amount, term_months, frequency, actor):
    """Start an Ajo plan for `customer`: by the customer in the app, or by staff (`actor`)."""
    now = timezone.now()
    today = timezone.localdate()
    txn = Transaction.objects.create(
        customer=customer, product_id=product.code, product_name=product.name, product_type=product.type,
        amount=amount, term_months=term_months, start_date=today, end_date=add_months(today, term_months),
        terms_accepted_at=now if product.clauses else None, repayment_frequency=frequency, created_at=now,
    )
    start(txn, actor)
    return txn


def start(txn, actor):
    """The plan has just been made: the cycle starts today with every contribution scheduled."""
    customer = txn.customer
    by_staff = actor.pk != customer.pk
    today = timezone.localdate()
    dates = due_dates(today, txn.term_months, txn.repayment_frequency)
    Contribution.objects.bulk_create([
        Contribution(plan=txn, number=i, due_date=due, amount=txn.amount) for i, due in enumerate(dates, start=1)
    ])
    txn.status = Transaction.Status.APPROVED
    txn.decided_at = timezone.now()
    txn.start_date = today
    txn.end_date = add_months(today, txn.term_months or 1)
    txn.save(update_fields=['status', 'decided_at', 'start_date', 'end_date'])
    each = f'{len(dates)} {txn.repayment_frequency} contributions of {naira(txn.amount)}'
    who = f"Started {customer.full_name}'s" if by_staff else f'{customer.full_name} joined'
    audit.record(actor, 'Ajo Plan Started' if by_staff else 'Ajo Plan Joined',
                 f'{who} {txn.product_name}, running {today:%d %b %Y} to {txn.end_date:%d %b %Y}: {each}.',
                 reference=txn.reference, status='success', agent_code=customer.agent_code)
    notify(customer, 'Ajo plan started',
           f'Your {txn.product_name} ({txn.reference}) has started: {each}, ending {txn.end_date:%d %b %Y}. '
           'Our staff will record each contribution you make, and your Ajo balance updates as they do.',
           Notification.Kind.PAYMENT, Notification.Type.SUCCESS, link=customer_link(txn))
    notify_admins('New Ajo plan',
                  f'{customer.full_name} {"was signed up to" if by_staff else "joined"} {txn.product_name}'
                  f'{" by " + actor.full_name if by_staff else ""}: {each} ({txn.reference}).',
                  Notification.Kind.PAYMENT, link=admin_link(txn), exclude=actor if by_staff else None)
    return dates


# ---- Recording a contribution ----

def _locked(c):
    return Contribution.objects.select_for_update().select_related('plan__customer').get(pk=c.pk)


def _event(c, actor, action, note=''):
    ContributionEvent.objects.create(contribution=c, action=action, actor=actor, actor_name=actor.full_name,
                                     actor_role=ROLE_LABELS.get(actor.role, actor.role), note=note[:500])


def _audit(actor, c, action, status, details):
    audit.record(actor, action, details, reference=c.reference, status=status,
                 agent_code=c.plan.customer.agent_code)


def record_payment(admin, c, method, received_on, reference, note=''):
    """Staff post a payment taken in cash or seen in the bank. A Super Admin's posting confirms it;
    an Admin's waits for a Super Admin."""
    with db_transaction.atomic():
        c = _locked(c)
        txn = c.plan
        if c.status == Contribution.Status.PAID:
            raise AjoError('This contribution has already been confirmed.', 'already_paid')
        if c.status == Contribution.Status.PENDING:
            raise AjoError('A payment for this contribution is already waiting for confirmation.', 'already_recorded')
        if has_ended(txn):
            raise AjoError(f'This cycle ended on {txn.end_date:%d %b %Y}; no more contributions can be recorded.',
                           'cycle_ended')
        if received_on > timezone.localdate():
            raise AjoError('The payment date cannot be in the future.', 'future_date', 'receivedOn')
        c.paid_to = collection_account(txn) if method == Contribution.Method.TRANSFER else None
        c.method = method
        c.recorded_by = admin
        c.recorded_by_name = admin.full_name
        c.received_on = received_on
        c.payment_reference = reference.strip()[:100]
        c.rejection_reason = ''
        c.status = Contribution.Status.PENDING
        c.save()
        how = 'cash' if method == Contribution.Method.CASH else 'transfer'
        _event(c, admin, 'recorded', f'{how.title()} received {received_on:%d %b %Y}'
               + (f', reference {c.payment_reference}' if c.payment_reference else '') + (f'. {note}' if note else ''))
        _audit(admin, c, 'Ajo Contribution Recorded', 'info',
               f'Recorded a {how} payment of {naira(c.amount)} for contribution {c.number} of {txn.product_name} '
               f'for {txn.customer.full_name}.')
        if admin.role == Role.SUPER_ADMIN:
            return _confirm(c, admin, note)
        notify_admins('Ajo contribution awaiting your final approval',
                      f'{admin.full_name} recorded a {how} payment of {naira(c.amount)} from {txn.customer.full_name} '
                      f'({c.reference}).', Notification.Kind.PAYMENT, link=admin_link(txn),
                      roles=(Role.SUPER_ADMIN,), exclude=admin)
    return c


# ---- Review ----

def _require_reviewable(c):
    if c.status == Contribution.Status.PAID:
        raise AjoError('This contribution has already been confirmed.', 'already_paid')
    if c.status != Contribution.Status.PENDING:
        raise AjoError('No payment has been recorded for this contribution.', 'no_payment')


def recommend(admin, c, approve, note=''):
    with db_transaction.atomic():
        c = _locked(c)
        _require_reviewable(c)
        txn = c.plan
        _event(c, admin, 'recommended_approval' if approve else 'recommended_rejection', note)
        _audit(admin, c, f'Contribution Recommended for {"Approval" if approve else "Rejection"}',
               'info' if approve else 'warning',
               f'Recommended {"confirming" if approve else "rejecting"} contribution {c.number} of {txn.product_name} '
               f'({naira(c.amount)}) for {txn.customer.full_name}.' + (f' Note: {note}' if note else ''))
        notify_admins('Contribution awaiting your final approval' if approve else 'Contribution flagged for rejection',
                      f'{admin.full_name} recommended {"confirming" if approve else "rejecting"} '
                      f"{txn.customer.full_name}'s contribution of {naira(c.amount)} ({c.reference}).",
                      Notification.Kind.PAYMENT, Notification.Type.INFO if approve else Notification.Type.WARNING,
                      link=admin_link(txn), roles=(Role.SUPER_ADMIN,), exclude=admin)
    return c


def _confirm(c, super_admin, note=''):
    """Confirm a locked, pending contribution: credit the customer's Ajo balance."""
    txn = c.plan
    c.status = Contribution.Status.PAID
    c.paid_at = timezone.now()
    c.save(update_fields=['status', 'paid_at'])
    User.objects.filter(pk=txn.customer_id).update(ajo_balance=F('ajo_balance') + c.amount)
    _event(c, super_admin, 'approved', note)
    total = saved(txn)
    _audit(super_admin, c, 'Contribution Approved', 'success',
           f'Confirmed contribution {c.number} of {txn.product_name} ({naira(c.amount)}) for '
           f'{txn.customer.full_name}. Saved so far: {naira(total)}.')
    notify(txn.customer, 'Contribution confirmed',
           f'Your contribution of {naira(c.amount)} to {txn.product_name} (contribution {c.number}) was confirmed. '
           f'Saved so far: {naira(total)}.', Notification.Kind.PAYMENT, Notification.Type.SUCCESS,
           link=customer_link(txn))
    return c


def approve(super_admin, c, note=''):
    with db_transaction.atomic():
        c = _locked(c)
        _require_reviewable(c)
        return _confirm(c, super_admin, note)


def reject(super_admin, c, reason=''):
    """The money wasn't found: the contribution goes back to unpaid, to be recorded again."""
    reason = reason.strip() or 'Payment could not be verified.'
    with db_transaction.atomic():
        c = _locked(c)
        _require_reviewable(c)
        txn = c.plan
        c.status = Contribution.Status.SCHEDULED
        c.rejection_reason = reason[:500]
        c.save(update_fields=['status', 'rejection_reason'])
        _event(c, super_admin, 'rejected', reason)
        _audit(super_admin, c, 'Contribution Rejected', 'error',
               f'Rejected the payment recorded for contribution {c.number} of {txn.product_name} ({naira(c.amount)}) '
               f'for {txn.customer.full_name}. Reason: {reason}')
        if c.recorded_by_id and c.recorded_by_id != super_admin.pk:
            notify(c.recorded_by, 'Contribution you recorded was rejected',
                   f'{super_admin.full_name} rejected the payment you recorded for {txn.customer.full_name} '
                   f'({c.reference}). Reason: {reason}', Notification.Kind.PAYMENT, Notification.Type.ERROR,
                   link=admin_link(txn))
    return c


# ---- Payout ----

def arrange_payout(admin, txn, bank_name, account_number, account_name, note=''):
    """An admin starts the payout once the cycle has ended: a withdrawal of everything saved, less
    the charge, which then goes through approval and payment like any other."""
    from .withdrawal_rules import quote

    with db_transaction.atomic():
        txn = Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)
        if not is_ajo(txn):
            raise AjoError('Only Ajo plans are paid out here.', 'not_ajo')
        q = quote(txn)
        if not q.allowed:
            raise AjoError(q.reason, 'not_payable')
        wd = Withdrawal.objects.create(
            customer=txn.customer, plan=txn, kind=Withdrawal.Kind.FULL, amount=q.available, penalty=0,
            service_charge=q.service_charge, payout_amount=q.available - q.service_charge,
            earliest_payout_date=q.earliest_payout_date, bank_name=bank_name.strip(), account_number=account_number,
            account_name=account_name.strip(), note=note.strip()[:300],
        )
        WithdrawalEvent.objects.create(withdrawal=wd, action='requested', actor=admin, actor_name=admin.full_name,
                                       actor_role=ROLE_LABELS.get(admin.role, admin.role))
        audit.record(admin, 'Ajo Payout Arranged',
                     f"Arranged {txn.customer.full_name}'s {txn.product_name} payout: {naira(q.available)} saved, "
                     f'{naira(q.service_charge)} service charge, {naira(wd.payout_amount)} to {wd.bank_name} '
                     f'{wd.account_number}.', reference=wd.reference, status='info', agent_code=txn.customer.agent_code)
        notify(txn.customer, 'Ajo payout on its way',
               f'Your {txn.product_name} cycle is complete. {naira(wd.payout_amount)} ({naira(q.available)} saved, less '
               f'the {naira(q.service_charge)} service charge) is being arranged for your {wd.bank_name} account.',
               Notification.Kind.WITHDRAWAL, link=customer_link(txn))
        notify_admins('Ajo payout to approve',
                      f'{admin.full_name} arranged {txn.customer.full_name}\'s Ajo payout of {naira(wd.payout_amount)} '
                      f'({wd.reference}).', Notification.Kind.WITHDRAWAL, Notification.Type.WARNING,
                      link=f'/admin/withdrawals/{wd.reference}', exclude=admin)
    return wd


# ---- What the app shows ----

def _trail(c):
    return [{'action': e.action, 'by': e.actor_name, 'role': e.actor_role, 'at': e.at,
             **({'note': e.note} if e.note else {})} for e in c.events.all()]


def contribution_dict(c, today, admin=False):
    data = {
        'id': c.pk,
        'reference': c.reference,
        'number': c.number,
        'dueDate': c.due_date.isoformat(),
        'amount': float(c.amount),
        'state': contribution_state(c, today),
        'method': c.method or None,
        'paidAt': c.paid_at,
        'receivedOn': c.received_on.isoformat() if c.received_on else None,
    }
    if admin:
        data.update({
            'paidTo': c.paid_to,
            'paymentReference': c.payment_reference,
            'recordedBy': c.recorded_by_name,
            'rejectionReason': c.rejection_reason,
            'trail': _trail(c),
        })
    return data


def statement(txn, contributions, payout):
    """Every movement on the plan in date order, with the running balance."""
    rows = [{'date': c.paid_at, 'description': f'Contribution {c.number}'
             + (' (cash)' if c.method == Contribution.Method.CASH else ''),
             'reference': c.reference, 'credit': float(c.amount), 'debit': 0}
            for c in contributions if c.status == Contribution.Status.PAID]
    if payout is not None and payout.decided_at:
        if payout.service_charge:
            rows.append({'date': payout.decided_at, 'description': 'Service charge (one contribution)',
                         'reference': payout.reference, 'credit': 0, 'debit': float(payout.service_charge)})
        rows.append({'date': payout.decided_at, 'description': 'Payout to your bank account',
                     'reference': payout.reference, 'credit': 0, 'debit': float(payout.payout_amount)})
    rows.sort(key=lambda r: r['date'])
    balance = 0.0
    for row in rows:
        balance += row['credit'] - row['debit']
        row['balance'] = round(balance, 2)
    return rows


def as_dict(txn, admin=False):
    today = timezone.localdate()
    contributions = list(txn.contributions.all())
    total = sum((c.amount for c in contributions if c.status == Contribution.Status.PAID), Decimal(0))
    charge = service_charge(txn, total)
    payout = txn.withdrawals.exclude(status__in=('rejected', 'cancelled')).order_by('-created_at').first()
    unpaid = [c for c in contributions if c.status != Contribution.Status.PAID]
    if payout is not None:
        state = 'paid_out' if payout.status == 'paid' else 'payout_requested'
    elif has_ended(txn, today):
        state = 'ended'
    else:
        state = 'active'
    return {
        'state': state,
        'frequency': txn.repayment_frequency,
        'contribution': float(txn.amount),
        'count': len(contributions),
        'paidCount': len(contributions) - len(unpaid),
        'saved': float(total),
        # What saving every contribution would come to
        'target': float(txn.amount * len(contributions)),
        'serviceCharge': float(charge),
        'netPayout': float(total - charge),
        'cycleEnds': txn.end_date.isoformat() if txn.end_date else None,
        # Where a transfer goes, while the cycle is open (staff record it once it arrives)
        'paymentAccount': collection_account(txn) if state == 'active' else None,
        'nextContribution': contribution_dict(unpaid[0], today) if unpaid and state == 'active' else None,
        'payout': {'reference': payout.reference, 'status': payout.status, 'amount': float(payout.amount),
                   'serviceCharge': float(payout.service_charge), 'payoutAmount': float(payout.payout_amount),
                   'paidAt': payout.paid_at, 'payoutReference': payout.payout_reference} if payout else None,
        'contributions': [contribution_dict(c, today, admin) for c in contributions],
        'statement': statement(txn, contributions, payout),
    }
