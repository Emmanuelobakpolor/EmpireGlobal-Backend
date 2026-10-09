"""Emails to customers about money moving on their account."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction as db_transaction

logger = logging.getLogger(__name__)

BALANCE_LABELS = {
    'savings_balance': 'Savings balance',
    'investment_balance': 'Investment balance',
    'outstanding_loan': 'Outstanding loan',
}


def naira(amount):
    return f'₦{float(amount):,.2f}'


def _send_after_commit(subject, message, recipient):
    """Send once the database change is saved, and never let a mail problem undo it."""
    def send():
        try:
            send_mail(subject, message, None, [recipient])
        except Exception:  # noqa: BLE001 - email is best-effort; the in-app notification still exists
            logger.exception('Could not send "%s" email', subject)

    db_transaction.on_commit(send)


def _transaction_link(txn):
    return f'{settings.FRONTEND_URL}/customer/transactions/{txn.reference}'


def send_deposit_confirmed(txn, balance_field=None, new_balance=None):
    customer = txn.customer
    lines = [
        f'Hi {customer.full_name},',
        '',
        f'Your deposit of {naira(txn.amount)} has been confirmed and credited.',
        '',
        f'Product:    {txn.product_name}',
        f'Amount:     {naira(txn.amount)}',
        f'Reference:  {txn.reference}',
    ]
    if balance_field and new_balance is not None:
        lines += ['', f'Your {BALANCE_LABELS.get(balance_field, "balance").lower()} is now {naira(new_balance)}.']
    lines += ['', f'View it in your account: {_transaction_link(txn)}', '',
              'If you did not make this payment, contact support immediately.']
    _send_after_commit(f'Deposit confirmed: {naira(txn.amount)} ({txn.reference})', '\n'.join(lines), customer.email)


def send_deposit_rejected(txn, reason):
    customer = txn.customer
    message = '\n'.join([
        f'Hi {customer.full_name},',
        '',
        f'We could not confirm your payment of {naira(txn.amount)} for {txn.product_name} ({txn.reference}).',
        '',
        f'Reason: {reason}',
        '',
        'Nothing has been credited to your account. You can upload a clearer receipt or contact support.',
        f'View it in your account: {_transaction_link(txn)}',
    ])
    _send_after_commit(f'Payment not confirmed ({txn.reference})', message, customer.email)


def _withdrawal_lines(wd):
    lines = [
        f'Plan:        {wd.plan.product_name} ({wd.plan.reference})',
        f'Amount:      {naira(wd.amount)}',
    ]
    if wd.penalty:
        lines.append(f'Penalty:     {naira(wd.penalty)}')
    lines += [
        f'You receive: {naira(wd.payout_amount)}',
        f'Paid to:     {wd.bank_name} · {wd.account_number} ({wd.account_name})',
        f'Reference:   {wd.reference}',
    ]
    return lines


def _withdrawal_link():
    return f'{settings.FRONTEND_URL}/customer/withdrawals'


def send_withdrawal_requested(wd):
    message = '\n'.join([
        f'Hi {wd.customer.full_name},', '',
        'We received your withdrawal request. We will review it and email you at each step.', '',
        *_withdrawal_lines(wd),
        f'Earliest payout: {wd.earliest_payout_date:%d %b %Y}', '',
        f'Track it here: {_withdrawal_link()}', '',
        'If you did not request this, contact support immediately and change your password.',
    ])
    _send_after_commit(f'Withdrawal request received ({wd.reference})', message, wd.customer.email)


def send_withdrawal_approved(wd, new_balance):
    message = '\n'.join([
        f'Hi {wd.customer.full_name},', '',
        f'Your withdrawal has been approved. {naira(wd.payout_amount)} will be sent to your bank account '
        f'from {wd.earliest_payout_date:%d %b %Y}.', '',
        *_withdrawal_lines(wd),
        *([f'Remaining balance: {naira(new_balance)}'] if new_balance is not None else []), '',
        "We'll email you again once the money has been sent.",
    ])
    _send_after_commit(f'Withdrawal approved: {naira(wd.payout_amount)} ({wd.reference})', message, wd.customer.email)


def send_withdrawal_rejected(wd):
    message = '\n'.join([
        f'Hi {wd.customer.full_name},', '',
        f'Your withdrawal request {wd.reference} for {naira(wd.amount)} was not approved.', '',
        f'Reason: {wd.rejection_reason}', '',
        'Nothing has been taken from your account. Contact support if you have questions.',
    ])
    _send_after_commit(f'Withdrawal not approved ({wd.reference})', message, wd.customer.email)


def send_withdrawal_paid(wd):
    message = '\n'.join([
        f'Hi {wd.customer.full_name},', '',
        f'{naira(wd.payout_amount)} has been sent to your bank account.', '',
        *_withdrawal_lines(wd),
        f'Transfer ref: {wd.payout_reference}', '',
        'It may take a short while to show in your bank account. If it has not arrived within 24 hours, '
        'contact support with the references above.',
    ])
    _send_after_commit(f'Withdrawal paid: {naira(wd.payout_amount)} ({wd.reference})', message, wd.customer.email)
