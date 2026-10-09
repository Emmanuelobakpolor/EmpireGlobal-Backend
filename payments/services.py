"""Payment slip review. Admins view and recommend; only a Super Admin's final decision
credits the customer or rejects the payment. Every step lands in the slip trail."""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Role, User
from core import audit

from .models import BALANCE_FIELD_BY_TYPE, Notification, SlipEvent, Transaction
from . import emails
from .notify import customer_link, notify, notify_admins, payment_review_link

ROLE_LABELS = {Role.SUPER_ADMIN: 'Super Admin', Role.ADMIN: 'Admin'}
# A repeat view by the same admin within this window isn't logged again
VIEW_DEDUPE = timedelta(minutes=10)


class ReviewError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def naira(amount):
    return f'₦{Decimal(amount):,.0f}'


def _event(txn, actor, action, note=''):
    return SlipEvent.objects.create(
        transaction=txn, action=action, actor=actor, actor_name=actor.full_name,
        actor_role=ROLE_LABELS.get(actor.role, actor.role), note=note,
    )


def record_view(txn, admin):
    """Log that an admin opened the slip, unless they did so very recently."""
    recent = txn.slip_events.filter(
        action=SlipEvent.Action.VIEWED, actor=admin, at__gte=timezone.now() - VIEW_DEDUPE,
    ).exists()
    if recent:
        return None
    event = _event(txn, admin, SlipEvent.Action.VIEWED)
    _audit(admin, txn, 'Payment Slip Viewed', 'info',
           f'Viewed {txn.product_name} payment slip of {naira(txn.amount)} for {txn.customer.full_name}.')
    return event


def _audit(actor, txn, action, status, details):
    audit.record(actor, action, details, reference=txn.reference, status=status, agent_code=txn.customer.agent_code)


def _locked(txn):
    return Transaction.objects.select_for_update().select_related('customer').get(pk=txn.pk)


def _require_reviewable(txn):
    if txn.is_final:
        raise ReviewError('A Super Admin has already made the final decision on this payment.', 'already_decided')
    if not txn.receipt_uploaded_at:
        raise ReviewError('The customer has not uploaded a receipt for this payment yet.', 'no_receipt')


def recommend(txn, admin, approve, note=''):
    with db_transaction.atomic():
        txn = _locked(txn)
        _require_reviewable(txn)
        action = SlipEvent.Action.RECOMMENDED_APPROVAL if approve else SlipEvent.Action.RECOMMENDED_REJECTION
        _event(txn, admin, action, note)
        _audit(admin, txn, f'Payment Recommended for {"Approval" if approve else "Rejection"}',
               'info' if approve else 'warning',
               f'Recommended {"approval" if approve else "rejection"} of {txn.product_name} payment of '
               f'{naira(txn.amount)} for {txn.customer.full_name}. Awaiting Super Admin final approval.'
               + (f' Note: {note}' if note else ''))
        notify_admins(
            'Payment awaiting your final approval' if approve else 'Payment flagged for rejection',
            f"{admin.full_name} recommended {'approving' if approve else 'rejecting'} {txn.customer.full_name}'s "
            f'{txn.product_name} payment of {naira(txn.amount)} ({txn.reference}).',
            Notification.Kind.PAYMENT, Notification.Type.INFO if approve else Notification.Type.WARNING,
            link=payment_review_link(txn), roles=(Role.SUPER_ADMIN,), exclude=admin,
        )
    return txn


def _tell_recommenders(txn, decider, approved):
    """Let the admins who recommended this payment know the final outcome."""
    recommender_ids = txn.slip_events.filter(action__startswith='recommended_').values_list('actor', flat=True)
    for admin in User.objects.filter(pk__in=recommender_ids).exclude(pk=decider.pk):
        notify(admin, f"Payment you reviewed was {'approved' if approved else 'rejected'}",
               f"{decider.full_name} gave the final decision on {txn.customer.full_name}'s {txn.product_name} "
               f'payment ({txn.reference}).', Notification.Kind.PAYMENT,
               Notification.Type.SUCCESS if approved else Notification.Type.ERROR, link=payment_review_link(txn))


def approve_payment(txn, super_admin, note=''):
    """Final approval: credit the customer's balance, all or nothing."""
    with db_transaction.atomic():
        txn = _locked(txn)
        _require_reviewable(txn)
        txn.status = Transaction.Status.APPROVED
        txn.decided_at = timezone.now()
        txn.save(update_fields=['status', 'decided_at'])

        field = BALANCE_FIELD_BY_TYPE.get(txn.product_type)
        if field:
            # F() update so two approvals at once can't overwrite each other
            User.objects.filter(pk=txn.customer_id).update(**{field: F(field) + txn.amount})

        _event(txn, super_admin, SlipEvent.Action.APPROVED, note)
        _audit(super_admin, txn, 'Payment Approved', 'success',
               f'Final approval of {txn.product_name} payment of {naira(txn.amount)} for {txn.customer.full_name}. '
               'Customer credited.')
        notify(txn.customer, 'Payment approved',
               f'Your {txn.product_name} payment of {naira(txn.amount)} ({txn.reference}) has been approved.',
               Notification.Kind.PAYMENT, Notification.Type.SUCCESS, link=customer_link(txn))
        _tell_recommenders(txn, super_admin, approved=True)
        new_balance = User.objects.values_list(field, flat=True).get(pk=txn.customer_id) if field else None
        emails.send_deposit_confirmed(txn, field, new_balance)
    return txn


def reject_payment(txn, super_admin, reason=''):
    reason = reason.strip() or 'Payment could not be verified.'
    with db_transaction.atomic():
        txn = _locked(txn)
        _require_reviewable(txn)
        txn.status = Transaction.Status.REJECTED
        txn.decided_at = timezone.now()
        txn.rejection_reason = reason
        txn.save(update_fields=['status', 'decided_at', 'rejection_reason'])
        _event(txn, super_admin, SlipEvent.Action.REJECTED, reason)
        _audit(super_admin, txn, 'Payment Rejected', 'error',
               f'Final rejection of {txn.product_name} payment of {naira(txn.amount)} for {txn.customer.full_name}. '
               f'Reason: {reason}')
        notify(txn.customer, 'Payment rejected',
               f'Your {txn.product_name} payment ({txn.reference}) was rejected. Reason: {reason}',
               Notification.Kind.PAYMENT, Notification.Type.ERROR, link=customer_link(txn))
        _tell_recommenders(txn, super_admin, approved=False)
        emails.send_deposit_rejected(txn, reason)
    return txn
