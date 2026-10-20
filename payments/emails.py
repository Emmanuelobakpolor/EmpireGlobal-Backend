"""Emails to customers about money moving on their account."""

import logging
from decimal import Decimal

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


def percent(rate):
    """13.00 → '13%', 12.50 → '12.5%'."""
    return f'{Decimal(rate).normalize():f}%'


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


def send_deposit_confirmed(txn, balance_field=None, new_balance=None, interest_plan=None):
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
    if interest_plan:
        account = txn.disbursement_account or {}
        lines += ['', f'Your investment runs from {txn.start_date:%d %b %Y} to {txn.end_date:%d %b %Y} at '
                      f'{percent(txn.interest_rate)} a year, paid {txn.get_interest_option_display().lower()}. '
                      f'Interest is sent to {account.get("bankName", "")} {account.get("accountNumber", "")}:']
        lines += [f'  {due:%d %b %Y}   {naira(amount)}' for due, amount in interest_plan]
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
    if wd.interest_deducted:
        lines.append(f'Interest already paid, forfeited: {naira(wd.interest_deducted)}')
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


# ---- Loans and hire-purchase ----

def _facility_lines(txn):
    lines = [
        f'Product:        {txn.product_name}',
        f'Amount:         {naira(txn.amount)}',
        f'Term:           {txn.term_months} month(s), repaid {txn.get_repayment_frequency_display().lower()}',
    ]
    if txn.interest_rate is not None:
        lines += [f'Interest:       {percent(txn.interest_rate)} of the amount (flat)',
                  f'Total to repay: {naira(txn.total_repayable)}']
    lines.append(f'Reference:      {txn.reference}')
    return lines


def send_application_approved(txn):
    account = txn.disbursement_account or {}
    if txn.product_type == 'loan':
        next_step = (f'We will pay {naira(txn.amount)} into your {account.get("bankName", "")} account '
                     f'{account.get("accountNumber", "")} ({account.get("accountName", "")}) shortly.')
    else:
        next_step = 'We will contact you to arrange delivery of your item.'
    message = '\n'.join([
        f'Hi {txn.customer.full_name},', '',
        f'Your {txn.product_name} application has been approved.', '',
        *_facility_lines(txn), '',
        next_step,
        'Your repayment schedule starts on the day it is ' + ('paid out.' if txn.product_type == 'loan' else 'delivered.'),
        f'View it in your account: {_transaction_link(txn)}',
    ])
    _send_after_commit(f'Application approved ({txn.reference})', message, txn.customer.email)


def send_application_rejected(txn, reason):
    message = '\n'.join([
        f'Hi {txn.customer.full_name},', '',
        f'Your {txn.product_name} application ({txn.reference}) for {naira(txn.amount)} was not approved.', '',
        f'Reason: {reason}', '',
        'Contact support if you have questions.',
    ])
    _send_after_commit(f'Application not approved ({txn.reference})', message, txn.customer.email)


def send_facility_started(txn, plan):
    loan = txn.product_type == 'loan'
    first_due, first_amount = plan[0]
    message = '\n'.join([
        f'Hi {txn.customer.full_name},', '',
        (f'{naira(txn.amount)} has been paid into your bank account.' if loan
         else f'Your {txn.product_name} item has been delivered.'), '',
        *_facility_lines(txn),
        f'{"Transfer ref" if loan else "Delivery note"}:   {txn.disbursement_reference}', '',
        f'Instalments:    {len(plan)} of about {naira(first_amount)}',
        f'First due:      {first_due:%d %b %Y}',
        f'Last due:       {plan[-1][0]:%d %b %Y}', '',
        'The account to pay each instalment into appears in your account a few days before it is due, '
        'and we will remind you.',
        f'View your repayment schedule: {_transaction_link(txn)}',
    ])
    _send_after_commit(f'{"Loan disbursed" if loan else "Item delivered"} ({txn.reference})', message,
                       txn.customer.email)


def send_interest_rate_changed(txn, old_rate, unpaid):
    lines = [
        f'Hi {txn.customer.full_name},', '',
        f'The interest on your {txn.product_name} has changed from {percent(old_rate)} to '
        f'{percent(txn.interest_rate)}.', '',
        *_facility_lines(txn),
    ]
    if unpaid:
        lines += ['', 'Your remaining instalments:']
        lines += [f'  #{r.number:<3} due {r.due_date:%d %b %Y}   {naira(r.amount)}' for r in unpaid]
    lines += ['', f'View your repayment schedule: {_transaction_link(txn)}']
    _send_after_commit(f'Interest rate updated ({txn.reference})', '\n'.join(lines), txn.customer.email)


def send_interest_paid(payment):
    txn = payment.plan
    account = txn.disbursement_account or {}
    message = '\n'.join([
        f'Hi {txn.customer.full_name},', '',
        f'{naira(payment.amount)} interest on your {txn.product_name} has been sent to your bank account.', '',
        f'Paid to:        {account.get("bankName", "")} · {account.get("accountNumber", "")} ({account.get("accountName", "")})',
        f'Transfer ref:   {payment.payout_reference}',
        f'Reference:      {payment.reference}', '',
        'It may take a short while to show in your bank account.',
        f'View your investment: {_transaction_link(txn)}',
    ])
    _send_after_commit(f'Interest paid: {naira(payment.amount)} ({payment.reference})', message, txn.customer.email)


def send_repayment_reminder(txn, repayments, account):
    lines = [f'Hi {txn.customer.full_name},', '', f'You have a repayment coming up on {txn.product_name}:', '']
    for rep in sorted(repayments, key=lambda r: r.due_date):
        lines.append(f'  Instalment {rep.number}: {naira(rep.amount)} due {rep.due_date:%d %b %Y} '
                     f'(narration {rep.reference})')
    if account:
        lines += ['', 'Pay into:',
                  f'  Bank:           {account["bankName"]}',
                  f'  Account number: {account["accountNumber"]}',
                  f'  Account name:   {account["accountName"]}']
    lines += ['', f'Then upload your receipt here: {_transaction_link(txn)}']
    _send_after_commit(f'Repayment due soon ({txn.reference})', '\n'.join(lines), txn.customer.email)


def send_repayment_confirmed(rep, outstanding, completed):
    txn = rep.facility
    lines = [
        f'Hi {txn.customer.full_name},', '',
        f'Your repayment of {naira(rep.amount)} for {txn.product_name} (instalment {rep.number}) has been confirmed.', '',
        f'Outstanding: {naira(outstanding)}',
    ]
    if completed:
        lines += ['', 'You have now fully repaid this facility. Thank you!']
        if txn.product_type == 'hire-purchase':
            lines.append('Ownership of the item is now yours.')
    lines += ['', f'View it in your account: {_transaction_link(txn)}']
    _send_after_commit(f'Repayment confirmed: {naira(rep.amount)} ({rep.reference})', '\n'.join(lines),
                       txn.customer.email)


def send_repayment_rejected(rep, reason):
    txn = rep.facility
    message = '\n'.join([
        f'Hi {txn.customer.full_name},', '',
        f'We could not confirm your repayment of {naira(rep.amount)} for {txn.product_name} '
        f'(instalment {rep.number}, {rep.reference}).', '',
        f'Reason: {reason}', '',
        'The instalment is still unpaid. You can upload a clearer receipt or contact support.',
        f'View it in your account: {_transaction_link(txn)}',
    ])
    _send_after_commit(f'Repayment not confirmed ({rep.reference})', message, txn.customer.email)
