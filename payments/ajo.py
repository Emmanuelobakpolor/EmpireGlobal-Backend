"""Ajo (Esusu): the customer pays a fixed contribution every day or week for a cycle (1 month or
more) and gets what they saved back, less the collector's charge of one contribution for each
month they saved in.

1. The customer joins (the contribution, how often and the cycle length). Cycles run on calendar
   months: the cycle starts on the 1st of the month they join in, with every contribution
   scheduled to the end date. Nothing is paid in the app.
2. Staff record each contribution (cash, or a transfer seen in the bank). An Admin's record waits
   for a Super Admin's confirmation; a Super Admin's is confirmed at once. Confirmation credits the
   customer's Ajo balance.
3. The customer can ask to withdraw part or all of what's saved at any time (staff can arrange it
   for a customer without the app). Each withdrawal first settles the charge still owed
   (withdrawal_rules.quote), and is then approved and marked paid like any other.
"""

from dataclasses import dataclass
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
# Withdrawals that still hold money aside on the plan
ACTIVE_WITHDRAWALS = (Withdrawal.Status.PENDING, Withdrawal.Status.APPROVED, Withdrawal.Status.PAID)
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


def cycle_start(day):
    """Cycles run on calendar months: the 1st of the month `day` falls in."""
    return day.replace(day=1)


def month_of_cycle(txn, day):
    """Which month of the cycle `day` falls in: 0 for the first."""
    start = txn.start_date
    months = (day.year - start.year) * 12 + day.month - start.month - (1 if day.day < start.day else 0)
    return max(0, months)


def saved(txn, contributions=None):
    """What the customer has paid in: every confirmed contribution."""
    contributions = txn.contributions.all() if contributions is None else contributions
    return sum((c.amount for c in contributions if c.status == Contribution.Status.PAID), Decimal(0))


def service_charge(txn, contributions=None):
    """The collector's charge so far: one contribution for each month of the cycle the customer
    has saved in (never more than was saved)."""
    contributions = list(txn.contributions.all()) if contributions is None else contributions
    months = {month_of_cycle(txn, c.due_date) for c in contributions if c.status == Contribution.Status.PAID}
    return min(txn.amount * len(months), saved(txn, contributions))


@dataclass
class Position:
    """Where a plan's money stands."""

    saved: Decimal        # Every confirmed contribution
    charge: Decimal       # The service charge so far
    charge_due: Decimal   # The part of it no withdrawal has settled yet
    withdrawn: Decimal    # Taken out or asked for (charges included)
    balance: Decimal      # Saved, less approved withdrawals: the plan's share of the Ajo balance
    available: Decimal    # Saved, less every withdrawal in progress
    net: Decimal          # What a withdrawal of everything available would pay


def position(txn, contributions=None, exclude=None):
    """The plan's savings, charge and withdrawals; `exclude` leaves one withdrawal out of the sums."""
    contributions = list(txn.contributions.all()) if contributions is None else contributions
    total = saved(txn, contributions)
    active = [w for w in txn.withdrawals.all()
              if w.status in ACTIVE_WITHDRAWALS and (exclude is None or w.pk != exclude.pk)]
    withdrawn = sum((w.amount for w in active), Decimal(0))
    deducted = sum((w.amount for w in active if w.status != Withdrawal.Status.PENDING), Decimal(0))
    charge = service_charge(txn, contributions)
    available = max(Decimal(0), total - withdrawn)
    due = min(available, max(Decimal(0), charge - sum((w.service_charge for w in active), Decimal(0))))
    return Position(saved=total, charge=charge, charge_due=due, withdrawn=withdrawn, balance=total - deducted,
                    available=available, net=available - due)


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
    first = cycle_start(timezone.localdate())
    txn = Transaction.objects.create(
        customer=customer, product_id=product.code, product_name=product.name, product_type=product.type,
        amount=amount, term_months=term_months, start_date=first, end_date=add_months(first, term_months),
        terms_accepted_at=now if product.clauses else None, repayment_frequency=frequency, created_at=now,
    )
    start(txn, actor)
    return txn


def start(txn, actor):
    """The plan has just been made: schedule every contribution of its cycle."""
    customer = txn.customer
    by_staff = actor.pk != customer.pk
    first = txn.start_date
    dates = due_dates(first, txn.term_months, txn.repayment_frequency)
    Contribution.objects.bulk_create([
        Contribution(plan=txn, number=i, due_date=due, amount=txn.amount) for i, due in enumerate(dates, start=1)
    ])
    txn.status = Transaction.Status.APPROVED
    txn.decided_at = timezone.now()
    txn.end_date = add_months(first, txn.term_months or 1)
    txn.save(update_fields=['status', 'decided_at', 'end_date'])
    each = f'{len(dates)} {txn.repayment_frequency} contributions of {naira(txn.amount)}'
    who = f"Started {customer.full_name}'s" if by_staff else f'{customer.full_name} joined'
    audit.record(actor, 'Ajo Plan Started' if by_staff else 'Ajo Plan Joined',
                 f'{who} {txn.product_name}, running {first:%d %b %Y} to {txn.end_date:%d %b %Y}: {each}.',
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


def reschedule(txn, first):
    """Move a locked plan's cycle to the month `first` falls in. Contributions keep their numbers
    (and what was recorded against them) and take the new cycle's dates."""
    first = cycle_start(first)
    dates = due_dates(first, txn.term_months, txn.repayment_frequency)
    rows = list(txn.contributions.select_for_update())
    extra = [c for c in rows if c.number > len(dates)]
    if any(c.status != Contribution.Status.SCHEDULED for c in extra):
        raise AjoError(f'A cycle starting {first:%d %b %Y} has {len(dates)} contributions, but more than that '
                       'have already been recorded on this plan.', 'too_many_recorded', 'startDate')
    Contribution.objects.filter(pk__in=[c.pk for c in extra]).delete()
    kept = [c for c in rows if c.number <= len(dates)]
    for c in kept:
        c.due_date = dates[c.number - 1]
    Contribution.objects.bulk_update(kept, ['due_date'])
    numbers = {c.number for c in kept}
    Contribution.objects.bulk_create([
        Contribution(plan=txn, number=i, due_date=due, amount=txn.amount)
        for i, due in enumerate(dates, start=1) if i not in numbers
    ])
    txn.start_date = first
    txn.end_date = add_months(first, txn.term_months or 1)
    txn.save(update_fields=['start_date', 'end_date'])


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


# ---- Withdrawals ----

def arrange_payout(admin, txn, bank_name, account_number, account_name, note='', amount=None):
    """An admin starts a withdrawal for the customer (e.g. one without the app): `amount` of what's
    saved, or everything. It then goes through approval and payment like any other."""
    from .withdrawal_rules import quote

    with db_transaction.atomic():
        txn = Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)
        if not is_ajo(txn):
            raise AjoError('Only Ajo plans are paid out here.', 'not_ajo')
        q = quote(txn)
        if not q.allowed:
            raise AjoError(q.reason, 'not_payable')
        if amount is not None:
            q = quote(txn, amount)
            if q.error:
                raise AjoError(q.error, 'invalid_amount', 'amount')
        taken = q.payout_amount + q.service_charge
        wd = Withdrawal.objects.create(
            customer=txn.customer, plan=txn, kind=q.kind, amount=taken, penalty=0,
            service_charge=q.service_charge, payout_amount=q.payout_amount,
            earliest_payout_date=q.earliest_payout_date, bank_name=bank_name.strip(), account_number=account_number,
            account_name=account_name.strip(), note=note.strip()[:300],
        )
        WithdrawalEvent.objects.create(withdrawal=wd, action='requested', actor=admin, actor_name=admin.full_name,
                                       actor_role=ROLE_LABELS.get(admin.role, admin.role))
        charge = f', {naira(q.service_charge)} service charge' if q.service_charge else ''
        audit.record(admin, 'Ajo Withdrawal Arranged',
                     f"Arranged a withdrawal from {txn.customer.full_name}'s {txn.product_name}: {naira(taken)} "
                     f'of their savings{charge}, {naira(wd.payout_amount)} to {wd.bank_name} {wd.account_number}.',
                     reference=wd.reference, status='info', agent_code=txn.customer.agent_code)
        notify(txn.customer, 'Ajo withdrawal on its way',
               f'{naira(wd.payout_amount)} from your {txn.product_name}'
               + (f' ({naira(taken)} of your savings, less the {naira(q.service_charge)} service charge)'
                  if q.service_charge else '')
               + f' is being arranged for your {wd.bank_name} account.',
               Notification.Kind.WITHDRAWAL, link=customer_link(txn))
        notify_admins('Ajo withdrawal to approve',
                      f'{admin.full_name} arranged an Ajo withdrawal of {naira(wd.payout_amount)} for '
                      f'{txn.customer.full_name} ({wd.reference}).', Notification.Kind.WITHDRAWAL,
                      Notification.Type.WARNING, link=f'/admin/withdrawals/{wd.reference}', exclude=admin)
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


def withdrawal_dict(wd):
    return {'reference': wd.reference, 'status': wd.status, 'amount': float(wd.amount),
            'serviceCharge': float(wd.service_charge), 'payoutAmount': float(wd.payout_amount),
            'createdAt': wd.created_at, 'paidAt': wd.paid_at, 'payoutReference': wd.payout_reference}


def statement(txn, contributions, withdrawals):
    """Every movement on the plan in date order, with the running balance."""
    rows = [{'date': c.paid_at, 'description': f'Contribution {c.number}'
             + (' (cash)' if c.method == Contribution.Method.CASH else ''),
             'reference': c.reference, 'credit': float(c.amount), 'debit': 0}
            for c in contributions if c.status == Contribution.Status.PAID]
    for wd in withdrawals:
        # The balance moves when a withdrawal is approved
        if wd.status == Withdrawal.Status.PENDING or not wd.decided_at:
            continue
        if wd.service_charge:
            rows.append({'date': wd.decided_at, 'description': 'Service charge',
                         'reference': wd.reference, 'credit': 0, 'debit': float(wd.service_charge)})
        rows.append({'date': wd.decided_at, 'description': 'Withdrawal to your bank account',
                     'reference': wd.reference, 'credit': 0, 'debit': float(wd.payout_amount)})
    rows.sort(key=lambda r: r['date'])
    balance = 0.0
    for row in rows:
        balance += row['credit'] - row['debit']
        row['balance'] = round(balance, 2)
    return rows


def active_withdrawals(txn):
    """The plan's withdrawals that weren't rejected or cancelled, oldest first."""
    return sorted((w for w in txn.withdrawals.all() if w.status in ACTIVE_WITHDRAWALS),
                  key=lambda w: (w.created_at, w.pk))


def as_dict(txn, admin=False):
    today = timezone.localdate()
    contributions = list(txn.contributions.all())
    withdrawals = active_withdrawals(txn)
    p = position(txn, contributions)
    unpaid = [c for c in contributions if c.status != Contribution.Status.PAID]
    if not has_ended(txn, today):
        state = 'active'
    elif p.net > 0 or not withdrawals:
        state = 'ended'
    elif any(w.status != Withdrawal.Status.PAID for w in withdrawals):
        state = 'payout_requested'
    else:
        state = 'paid_out'
    return {
        'state': state,
        'frequency': txn.repayment_frequency,
        'contribution': float(txn.amount),
        'count': len(contributions),
        'paidCount': len(contributions) - len(unpaid),
        'saved': float(p.saved),
        # What saving every contribution would come to
        'target': float(txn.amount * len(contributions)),
        # One contribution for each month saved in, and the part of it no withdrawal has settled yet
        'serviceCharge': float(p.charge),
        'chargeDue': float(p.charge_due),
        'withdrawn': float(p.withdrawn),
        'balance': float(p.balance),
        # What can be asked for now, and what that would pay after the charge still owed
        'available': float(p.available),
        'netPayout': float(p.net),
        'cycleEnds': txn.end_date.isoformat() if txn.end_date else None,
        # Where a transfer goes, while the cycle is open (staff record it once it arrives)
        'paymentAccount': collection_account(txn) if state == 'active' else None,
        'nextContribution': contribution_dict(unpaid[0], today) if unpaid and state == 'active' else None,
        'withdrawals': [withdrawal_dict(w) for w in withdrawals],
        # The latest of them
        'payout': withdrawal_dict(withdrawals[-1]) if withdrawals else None,
        'contributions': [contribution_dict(c, today, admin) for c in contributions],
        'statement': statement(txn, contributions, withdrawals),
    }
