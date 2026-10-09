"""Withdrawals: a customer asks to take money out of a plan, an admin recommends, a Super Admin
approves (the balance is deducted) or rejects, and an admin marks it paid once the bank transfer
has been sent. Every step notifies the people involved; the customer also gets emails."""

from django.db import transaction as db_transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Role, User
from core import audit

from . import emails
from .models import BALANCE_FIELD_BY_TYPE, Notification, Transaction, Withdrawal, WithdrawalEvent
from .notify import notify, notify_admins
from .withdrawal_rules import quote

ROLE_LABELS = {Role.SUPER_ADMIN: 'Super Admin', Role.ADMIN: 'Admin', Role.CUSTOMER: 'Customer'}
CUSTOMER_LINK = '/customer/withdrawals'


class WithdrawalError(Exception):
    def __init__(self, message, code, field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def naira(amount):
    return f'₦{float(amount):,.0f}'


def admin_link(wd):
    return f'/admin/withdrawals/{wd.reference}'


def _event(wd, actor, action, note=''):
    WithdrawalEvent.objects.create(withdrawal=wd, action=action, actor=actor, actor_name=actor.full_name,
                                   actor_role=ROLE_LABELS.get(actor.role, actor.role), note=note)


def _audit(actor, wd, action, status, details):
    audit.record(actor, action, details, reference=wd.reference, status=status, agent_code=wd.customer.agent_code)


def _locked(wd):
    return Withdrawal.objects.select_for_update().select_related('customer', 'plan').get(pk=wd.pk)


def request_withdrawal(customer, plan_reference, amount, bank_name, account_number, account_name, note=''):
    with db_transaction.atomic():
        # Locking the plan stops two requests from both spending the same money
        plan = Transaction.objects.select_for_update().filter(reference=plan_reference, customer=customer).first()
        if not plan:
            raise WithdrawalError('Plan not found.', 'plan_not_found', 'planReference')
        q = quote(plan, amount)
        if not q.allowed:
            raise WithdrawalError(q.reason, 'not_withdrawable', 'planReference')
        if q.error:
            raise WithdrawalError(q.error, 'invalid_amount', 'amount')
        wd = Withdrawal.objects.create(
            customer=customer, plan=plan, kind=q.kind, amount=q.payout_amount + q.penalty, penalty=q.penalty,
            payout_amount=q.payout_amount, earliest_payout_date=q.earliest_payout_date, bank_name=bank_name,
            account_number=account_number, account_name=account_name, note=note,
        )
        notify(customer, 'Withdrawal requested',
               f'We received your request to withdraw {naira(wd.amount)} from {plan.product_name} ({wd.reference}).',
               Notification.Kind.WITHDRAWAL, link=CUSTOMER_LINK)
        notify_admins('New withdrawal request',
                      f'{customer.full_name} wants to withdraw {naira(wd.amount)} from {plan.product_name} '
                      f'({wd.reference}).', Notification.Kind.WITHDRAWAL, Notification.Type.WARNING,
                      link=admin_link(wd))
        emails.send_withdrawal_requested(wd)
    return wd


def cancel(customer, wd):
    with db_transaction.atomic():
        wd = _locked(wd)
        if wd.customer_id != customer.pk:
            raise WithdrawalError('Withdrawal not found.', 'not_found')
        if wd.status != Withdrawal.Status.PENDING:
            raise WithdrawalError('Only a withdrawal that is still being reviewed can be cancelled.', 'not_cancellable')
        wd.status = Withdrawal.Status.CANCELLED
        wd.decided_at = timezone.now()
        wd.save(update_fields=['status', 'decided_at'])
        _event(wd, customer, 'cancelled')
    return wd


def _require_pending(wd):
    if wd.status != Withdrawal.Status.PENDING:
        raise WithdrawalError('This withdrawal has already been decided.', 'already_decided')


def recommend(admin, wd, approve, note=''):
    with db_transaction.atomic():
        wd = _locked(wd)
        _require_pending(wd)
        action = 'recommended_approval' if approve else 'recommended_rejection'
        _event(wd, admin, action, note)
        _audit(admin, wd, f'Withdrawal Recommended for {"Approval" if approve else "Rejection"}',
               'info' if approve else 'warning',
               f'Recommended {"approving" if approve else "rejecting"} {wd.customer.full_name}\'s withdrawal of '
               f'{naira(wd.amount)} from {wd.plan.product_name}.' + (f' Note: {note}' if note else ''))
        notify_admins('Withdrawal awaiting your final approval' if approve else 'Withdrawal flagged for rejection',
                      f'{admin.full_name} recommended {"approving" if approve else "rejecting"} '
                      f'{wd.customer.full_name}\'s withdrawal of {naira(wd.amount)} ({wd.reference}).',
                      Notification.Kind.WITHDRAWAL,
                      Notification.Type.INFO if approve else Notification.Type.WARNING,
                      link=admin_link(wd), roles=(Role.SUPER_ADMIN,), exclude=admin)
    return wd


def _tell_recommenders(wd, decider, outcome):
    ids = wd.events.filter(action__startswith='recommended_').values_list('actor', flat=True)
    for admin in User.objects.filter(pk__in=ids).exclude(pk=decider.pk):
        notify(admin, f'Withdrawal you reviewed was {outcome}',
               f'{decider.full_name} {outcome} {wd.customer.full_name}\'s withdrawal ({wd.reference}).',
               Notification.Kind.WITHDRAWAL, link=admin_link(wd))


def approve(super_admin, wd, note=''):
    """Final approval: take the amount off the plan and the customer's balance, all or nothing."""
    with db_transaction.atomic():
        wd = _locked(wd)
        _require_pending(wd)
        plan = Transaction.objects.select_for_update().get(pk=wd.plan_id)
        # Still there? Other withdrawals may have been approved since this was requested
        if quote(plan, exclude=wd).available < wd.amount:
            raise WithdrawalError('This plan no longer has enough money for this withdrawal.', 'insufficient_funds')
        field = BALANCE_FIELD_BY_TYPE.get(plan.product_type)
        customer = User.objects.select_for_update().get(pk=wd.customer_id)
        if not field or getattr(customer, field) < wd.amount:
            raise WithdrawalError("The customer's balance is lower than this withdrawal.", 'insufficient_funds')
        User.objects.filter(pk=customer.pk).update(**{field: F(field) - wd.amount})
        new_balance = getattr(customer, field) - wd.amount

        wd.status = Withdrawal.Status.APPROVED
        wd.decided_at = timezone.now()
        wd.save(update_fields=['status', 'decided_at'])
        _event(wd, super_admin, 'approved', note)
        _audit(super_admin, wd, 'Withdrawal Approved', 'success',
               f'Approved {customer.full_name}\'s withdrawal of {naira(wd.amount)} from {plan.product_name}. '
               f'Balance deducted; {naira(wd.payout_amount)} to be paid to {wd.bank_name} {wd.account_number}.')
        notify(customer, 'Withdrawal approved',
               f'Your withdrawal of {naira(wd.amount)} ({wd.reference}) was approved. {naira(wd.payout_amount)} '
               f'will be sent from {wd.earliest_payout_date:%d %b %Y}.',
               Notification.Kind.WITHDRAWAL, Notification.Type.SUCCESS, link=CUSTOMER_LINK)
        notify_admins('Withdrawal ready to pay',
                      f'Send {naira(wd.payout_amount)} to {wd.account_name} ({wd.bank_name} {wd.account_number}) '
                      f'from {wd.earliest_payout_date:%d %b %Y}, then mark {wd.reference} as paid.',
                      Notification.Kind.WITHDRAWAL, Notification.Type.WARNING, link=admin_link(wd))
        _tell_recommenders(wd, super_admin, 'approved')
        emails.send_withdrawal_approved(wd, new_balance)
    return wd


def reject(super_admin, wd, reason=''):
    reason = reason.strip() or 'The withdrawal could not be approved.'
    with db_transaction.atomic():
        wd = _locked(wd)
        _require_pending(wd)
        wd.status = Withdrawal.Status.REJECTED
        wd.rejection_reason = reason
        wd.decided_at = timezone.now()
        wd.save(update_fields=['status', 'rejection_reason', 'decided_at'])
        _event(wd, super_admin, 'rejected', reason)
        _audit(super_admin, wd, 'Withdrawal Rejected', 'error',
               f'Rejected {wd.customer.full_name}\'s withdrawal of {naira(wd.amount)}. Reason: {reason}')
        notify(wd.customer, 'Withdrawal not approved',
               f'Your withdrawal of {naira(wd.amount)} ({wd.reference}) was not approved. Reason: {reason}',
               Notification.Kind.WITHDRAWAL, Notification.Type.ERROR, link=CUSTOMER_LINK)
        _tell_recommenders(wd, super_admin, 'rejected')
        emails.send_withdrawal_rejected(wd)
    return wd


def mark_paid(admin, wd, payout_reference):
    payout_reference = payout_reference.strip()
    if not payout_reference:
        raise WithdrawalError('Enter the bank transfer reference.', 'reference_required', 'payoutReference')
    with db_transaction.atomic():
        wd = _locked(wd)
        if wd.status != Withdrawal.Status.APPROVED:
            raise WithdrawalError('Only an approved withdrawal can be marked as paid.', 'not_approved')
        wd.status = Withdrawal.Status.PAID
        wd.paid_at = timezone.now()
        wd.payout_reference = payout_reference[:100]
        wd.save(update_fields=['status', 'paid_at', 'payout_reference'])
        _event(wd, admin, 'paid', f'Transfer reference: {wd.payout_reference}')
        _audit(admin, wd, 'Withdrawal Paid', 'success',
               f'Sent {naira(wd.payout_amount)} to {wd.customer.full_name} ({wd.bank_name} {wd.account_number}). '
               f'Transfer reference: {wd.payout_reference}.')
        notify(wd.customer, 'Withdrawal paid',
               f'{naira(wd.payout_amount)} has been sent to your {wd.bank_name} account ({wd.reference}).',
               Notification.Kind.WITHDRAWAL, Notification.Type.SUCCESS, link=CUSTOMER_LINK)
        emails.send_withdrawal_paid(wd)
    return wd
