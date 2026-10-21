"""Interest on investments and savings plans. Rates are a percent a year of the capital, earned
pro-rata over the plan's length.

Investments run 12 months, and the customer chooses when they invest how interest is paid:

  upfront   30%, paid at the start of the contract
  monthly   36% a year, paid as 3% of the capital each month for 12 months
  maturity  42%, paid at the end of the year alongside the capital

Savings plans (1-24 months) earn 15% a year, paid at maturity: 7.5% over 6 months, 30% over 24.

On a savings plan, a partial withdrawal before maturity (allowed from month 8) drops the plan to
12% a year on the capital left; the payment due at maturity is recalculated when it's approved.
Investments allow no partial withdrawals before maturity. Ending either early (a full withdrawal)
forfeits all interest: what was paid is deducted from the payout (withdrawal_rules.quote) and the
rest is cancelled.

Those are the defaults; a product can set its own (`Product.interest_rates`), and a Super Admin can
set the plan's rate when approving its payment. Approval starts the plan and schedules the
payments. As each one comes due admins are told, transfer it to the customer's own bank account
and record the transfer reference.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
from django.db.models import Max, Sum
from django.utils import timezone

from core import audit

from . import emails
from .facilities import CENT, add_months, split
from .models import InterestOption, InterestPayment, Notification, ProductType, Withdrawal
from .notify import customer_link, notify, notify_admins

TERM_MONTHS = 12  # Every investment
PARTIAL = 'partial'  # Savings: the rate after a partial withdrawal before maturity
DEFAULT_RATES = {
    ProductType.INVESTMENT: {
        InterestOption.UPFRONT: Decimal('30'),
        InterestOption.MONTHLY: Decimal('36'),
        InterestOption.MATURITY: Decimal('42'),
    },
    ProductType.SAVINGS: {
        InterestOption.MATURITY: Decimal('15'),
        PARTIAL: Decimal('12'),
    },
}
INTEREST_TYPES = tuple(DEFAULT_RATES)
# Admins are told this many days before a payment is due
DUE_NOTICE_DAYS = 3


class InvestmentError(Exception):
    def __init__(self, message, code, field=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


def naira(amount):
    return f'₦{Decimal(amount):,.2f}'


def admin_link(txn):
    return f'/admin/transactions/{txn.reference}'


def earns_interest(txn):
    return txn.product_type in INTEREST_TYPES and bool(txn.interest_option)


def plan_word(txn):
    return 'investment' if txn.product_type == ProductType.INVESTMENT else 'savings plan'


# ---- Terms ----

def product_rates(product):
    """{option or 'partial': yearly rate} for a product: its own rates, or the defaults for its type."""
    defaults = DEFAULT_RATES.get(product.type, {}) if product is not None else {}
    own = (product.interest_rates if product is not None else None) or {}
    return {option: Decimal(str(own[option])) if own.get(option) is not None else rate
            for option, rate in defaults.items()}


def interest_for(capital, rate, term_months):
    """Interest on `capital` at `rate`% a year over `term_months`."""
    return (Decimal(capital) * Decimal(rate) / 100 * (term_months or 0) / 12).quantize(CENT, ROUND_HALF_UP)


def schedule(capital, rate, option, start, term_months=TERM_MONTHS):
    """[(due date, amount)] for a plan of `capital` starting on `start`."""
    total = interest_for(capital, rate, term_months)
    if option == InterestOption.UPFRONT:
        return [(start, total)]
    if option == InterestOption.MONTHLY:
        dates = [add_months(start, i) for i in range(1, term_months + 1)]
        return list(zip(dates, split(total, term_months)))
    return [(add_months(start, term_months), total)]


def interest_paid(txn):
    return txn.interest_payments.filter(status=InterestPayment.Status.PAID).aggregate(s=Sum('amount'))['s'] or Decimal(0)


def payment_state(payment, today=None):
    today = today or timezone.localdate()
    if payment.status != InterestPayment.Status.SCHEDULED:
        return payment.status
    return 'due' if payment.due_date <= today else 'upcoming'


def is_early(txn, today=None):
    today = today or timezone.localdate()
    return bool(txn.end_date) and today < txn.end_date


def partial_rate(txn):
    return txn.partial_interest_rate if txn.partial_interest_rate is not None else txn.interest_rate


def reprice(txn, capital_left):
    """What a partial withdrawal before maturity, leaving `capital_left`, does to the interest:
    (new total at the partial rate, already paid, paid above the new total)."""
    total = interest_for(capital_left, partial_rate(txn), txn.term_months)
    paid = interest_paid(txn)
    return total, paid, max(Decimal(0), paid - total)


# ---- Approval: the plan starts ----

def start(txn, super_admin, interest_rate=None):
    """Called inside the approval of the plan's payment: fix the rates, start the plan today and
    schedule the interest."""
    from catalog.models import Product

    rates = product_rates(Product.objects.filter(code=txn.product_id).first())
    txn.interest_rate = Decimal(interest_rate) if interest_rate is not None else rates[txn.interest_option]
    txn.partial_interest_rate = rates.get(PARTIAL)
    if txn.product_type == ProductType.INVESTMENT:
        txn.term_months = TERM_MONTHS
    txn.start_date = timezone.localdate()
    txn.end_date = add_months(txn.start_date, txn.term_months)
    txn.save(update_fields=['interest_rate', 'partial_interest_rate', 'start_date', 'end_date', 'term_months'])
    plan = schedule(txn.amount, txn.interest_rate, txn.interest_option, txn.start_date, txn.term_months)
    InterestPayment.objects.bulk_create([
        InterestPayment(plan=txn, number=i, due_date=due, amount=amount)
        for i, (due, amount) in enumerate(plan, start=1)
    ])
    audit.record(super_admin, 'Interest Scheduled',
                 f'{txn.customer.full_name}\'s {txn.product_name} of {naira(txn.amount)} started at '
                 f'{emails.percent(txn.interest_rate)} a year, paid {txn.get_interest_option_display().lower()}: '
                 f'{naira(sum(a for _, a in plan))} interest in {len(plan)} payment(s).',
                 reference=txn.reference, status='success', agent_code=txn.customer.agent_code)
    return plan


# ---- Paying interest ----

def record_payment(payment, admin, reference):
    """An admin has transferred a due interest payment to the customer."""
    reference = reference.strip()
    if not reference:
        raise InvestmentError('Enter the bank transfer reference.', 'reference_required', 'reference')
    with db_transaction.atomic():
        payment = InterestPayment.objects.select_for_update().select_related('plan__customer').get(pk=payment.pk)
        txn = payment.plan
        if payment.status == InterestPayment.Status.PAID:
            raise InvestmentError('This interest payment has already been recorded.', 'already_paid')
        if payment.status == InterestPayment.Status.CANCELLED:
            raise InvestmentError(f'This {plan_word(txn)} was ended early, so this interest is no longer paid.',
                                  'cancelled')
        if payment.due_date > timezone.localdate():
            raise InvestmentError(f'This payment is due on {payment.due_date:%d %b %Y}.', 'not_due_yet')
        # A withdrawal being reviewed may deduct interest paid; don't move it underneath
        if txn.withdrawals.filter(status=Withdrawal.Status.PENDING).exists():
            raise InvestmentError(f'A withdrawal request on this {plan_word(txn)} is being reviewed. Decide it first.',
                                  'withdrawal_pending')
        payment.status = InterestPayment.Status.PAID
        payment.paid_at = timezone.now()
        payment.paid_by_name = admin.full_name
        payment.payout_reference = reference[:100]
        payment.paid_to = txn.disbursement_account
        payment.save(update_fields=['status', 'paid_at', 'paid_by_name', 'payout_reference', 'paid_to'])

        account = txn.disbursement_account or {}
        audit.record(admin, 'Interest Paid',
                     f'Sent {naira(payment.amount)} interest ({payment.reference}) on {txn.product_name} to '
                     f'{txn.customer.full_name} ({account.get("bankName", "")} {account.get("accountNumber", "")}). '
                     f'Transfer reference: {payment.payout_reference}.',
                     reference=payment.reference, status='success', agent_code=txn.customer.agent_code)
        notify(txn.customer, 'Interest paid',
               f'{naira(payment.amount)} interest on {txn.product_name} has been sent to your '
               f'{account.get("bankName", "bank")} account ({payment.reference}).',
               Notification.Kind.PAYMENT, Notification.Type.SUCCESS, link=customer_link(txn))
        emails.send_interest_paid(payment)
    return payment


def cancel_remaining(txn, actor):
    """The plan was ended early: no more interest is paid."""
    cancelled = txn.interest_payments.filter(status=InterestPayment.Status.SCHEDULED).update(
        status=InterestPayment.Status.CANCELLED)
    if cancelled:
        audit.record(actor, 'Interest Cancelled',
                     f'{cancelled} interest payment(s) on {txn.customer.full_name}\'s {txn.product_name} cancelled: '
                     f'the {plan_word(txn)} was ended early.', reference=txn.reference, status='warning',
                     agent_code=txn.customer.agent_code)
    return cancelled


def apply_partial_withdrawal(txn, capital_left, actor):
    """Called inside the approval of a partial withdrawal before maturity: the plan now earns the
    partial rate on `capital_left`. What's left to pay is spread over the payments still to come."""
    total, paid, _ = reprice(txn, capital_left)
    owed = max(Decimal(0), total - paid)
    old_rate = txn.interest_rate
    scheduled = list(txn.interest_payments.select_for_update().filter(status=InterestPayment.Status.SCHEDULED))
    if owed <= 0:
        InterestPayment.objects.filter(pk__in=[p.pk for p in scheduled]).update(status=InterestPayment.Status.CANCELLED)
    elif scheduled:
        for payment, amount in zip(scheduled, split(owed, len(scheduled))):
            payment.amount = amount
        InterestPayment.objects.bulk_update(scheduled, ['amount'])
    else:
        number = (txn.interest_payments.aggregate(n=Max('number'))['n'] or 0) + 1
        InterestPayment.objects.create(plan=txn, number=number, due_date=txn.end_date, amount=owed)
    txn.interest_rate = partial_rate(txn)
    txn.save(update_fields=['interest_rate'])

    audit.record(actor, 'Interest Reduced',
                 f'{txn.customer.full_name}\'s {txn.product_name} now earns {emails.percent(txn.interest_rate)} a year '
                 f'(was {emails.percent(old_rate)}) on {naira(capital_left)} after a partial withdrawal: '
                 f'{naira(total)} interest in all, {naira(owed)} still to pay.',
                 reference=txn.reference, status='warning', agent_code=txn.customer.agent_code)
    notify(txn.customer, 'Interest updated',
           f'After your partial withdrawal, {txn.product_name} earns {emails.percent(txn.interest_rate)} a year on '
           f'the {naira(capital_left)} left: {naira(total)} interest in all, {naira(owed)} still to be paid to you.',
           Notification.Kind.PAYMENT, link=customer_link(txn))


def send_due_notices(today=None):
    """Tell admins about interest payments coming due (once each). Safe to run as often as you like;
    returns how many payments they were told about."""
    today = today or timezone.localdate()
    due = list(InterestPayment.objects
               .filter(status=InterestPayment.Status.SCHEDULED, due_notified_at__isnull=True,
                       due_date__lte=today + timedelta(days=DUE_NOTICE_DAYS))
               .select_related('plan__customer'))
    for payment in due:
        txn = payment.plan
        account = txn.disbursement_account or {}
        when = 'today' if payment.due_date == today else (
            f'overdue since {payment.due_date:%d %b %Y}' if payment.due_date < today else f'on {payment.due_date:%d %b %Y}')
        notify_admins('Interest payment due',
                      f'Send {naira(payment.amount)} interest to {txn.customer.full_name} '
                      f'({account.get("bankName", "")} {account.get("accountNumber", "")}) for {txn.product_name}, '
                      f'due {when}, then record it ({payment.reference}).',
                      Notification.Kind.PAYMENT, Notification.Type.WARNING, link=admin_link(txn))
    InterestPayment.objects.filter(pk__in=[p.pk for p in due]).update(due_notified_at=timezone.now())
    return len(due)


def as_dict(txn, today=None, admin=False):
    """The plan's interest terms and schedule, for the API."""
    today = today or timezone.localdate()
    payments = list(txn.interest_payments.all())
    rows = []
    for p in payments:
        row = {
            'id': p.pk, 'reference': f'{txn.reference}-I{p.number:02d}', 'number': p.number,
            'dueDate': p.due_date.isoformat(), 'amount': float(p.amount), 'state': payment_state(p, today),
            'paidAt': p.paid_at, 'payoutReference': p.payout_reference, 'paidTo': p.paid_to,
        }
        if admin:
            row['paidBy'] = p.paid_by_name
        rows.append(row)
    paid = sum((p.amount for p in payments if p.status == InterestPayment.Status.PAID), Decimal(0))
    if payments:
        total = sum((p.amount for p in payments if p.status != InterestPayment.Status.CANCELLED), Decimal(0))
    elif txn.interest_rate is not None:
        total = interest_for(txn.amount, txn.interest_rate, txn.term_months)
    else:
        total = None
    upcoming = [r for r in rows if r['state'] in ('due', 'upcoming')]
    return {
        'option': txn.interest_option,
        'rate': float(txn.interest_rate) if txn.interest_rate is not None else None,
        'partialRate': float(txn.partial_interest_rate) if txn.partial_interest_rate is not None else None,
        'termMonths': txn.term_months,
        'totalInterest': float(total) if total is not None else None,
        'interestPaid': float(paid),
        'payoutAccount': txn.disbursement_account,
        'nextPayment': upcoming[0] if upcoming else None,
        'dueCount': sum(1 for r in rows if r['state'] == 'due'),
        'payments': rows,
    }
