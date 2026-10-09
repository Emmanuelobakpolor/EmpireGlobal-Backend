"""Withdrawal rules, enforced per plan (an approved transaction).

A product's `withdrawal_rules` (JSON) can hold:
  allowed               bool   Can money be taken out at all (loans and hire-purchase: no)
  partial_after_months  int    Partial withdrawals allowed from this many months after the start.
                               0 = any time. Missing/None = only once the plan has matured.
                               Before that, only the whole remaining amount can be withdrawn
                               ("early termination").
  early_penalty_percent number Penalty on an early termination made within...
  early_penalty_days    int    ...this many days of the plan's start
  notice_working_days   int    Earliest payout is this many working days (Mon-Fri) after the request
  notice_hours          int    Or this many hours after the request
  payout                str    'any' (default), 'month_end' or 'quarter_end': when payouts happen
Interest isn't tracked by the app, so terms like "interest is forfeited" have no amount to apply.
"""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum
from django.utils import timezone

from .serializers import add_months

# The rules for the official catalogue are set by catalog/migrations/0004_default_withdrawal_rules.py

CENT = Decimal('0.01')
# Withdrawals that still hold money aside on the plan (so two requests can't spend the same naira)
ACTIVE_STATUSES = ('pending', 'approved', 'paid')


def month_end(d):
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def quarter_end(d):
    last_month = ((d.month - 1) // 3 + 1) * 3
    return date(d.year, last_month, monthrange(d.year, last_month)[1])


def add_working_days(d, days):
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


@dataclass
class Quote:
    """What a withdrawal of `amount` from a plan would mean, or why it isn't possible."""

    available: Decimal = Decimal('0')
    allowed: bool = False
    partial_allowed: bool = False
    matured: bool = False
    kind: str = 'partial'            # 'partial' or 'full'
    penalty_percent: Decimal = Decimal('0')
    penalty: Decimal = Decimal('0')
    payout_amount: Decimal = Decimal('0')
    earliest_payout_date: date | None = None
    reason: str = ''                 # Why the plan can't be withdrawn from right now
    error: str = ''                  # Why this particular amount isn't accepted
    notes: list = field(default_factory=list)

    def as_dict(self):
        return {
            'available': float(self.available), 'allowed': self.allowed, 'partialAllowed': self.partial_allowed,
            'matured': self.matured, 'kind': self.kind, 'penaltyPercent': float(self.penalty_percent),
            'penalty': float(self.penalty), 'payoutAmount': float(self.payout_amount),
            'earliestPayoutDate': self.earliest_payout_date.isoformat() if self.earliest_payout_date else None,
            'reason': self.reason, 'error': self.error, 'notes': self.notes,
        }


def rules_for(txn):
    from catalog.models import Product

    product = Product.objects.filter(code=txn.product_id).first()
    return (product.withdrawal_rules if product else None) or {}


def withdrawn_from(txn, exclude=None):
    rows = txn.withdrawals.filter(status__in=ACTIVE_STATUSES)
    if exclude is not None:
        rows = rows.exclude(pk=exclude.pk)
    return rows.aggregate(total=Sum('amount'))['total'] or Decimal('0')


def quote(txn, amount=None, today=None, now=None, exclude=None):
    """Evaluate the plan's rules today; if `amount` is given, check it and price it."""
    now = now or timezone.now()
    today = today or timezone.localdate()
    rules = rules_for(txn)
    q = Quote()

    if txn.status != 'approved':
        q.reason = 'Only plans whose payment has been approved can be withdrawn from.'
        return q
    q.available = max(Decimal('0'), txn.amount - withdrawn_from(txn, exclude)).quantize(CENT)
    if not rules.get('allowed'):
        q.reason = 'This product has no withdrawals (nothing is saved or invested in it).'
        return q
    if q.available <= 0:
        q.reason = 'Everything on this plan has already been withdrawn or requested.'
        return q
    q.allowed = True

    start = txn.start_date
    # Open-ended plans (no end date) never "mature"; their partial rule decides on its own
    q.matured = bool(txn.end_date and today >= txn.end_date)
    partial_after = rules.get('partial_after_months')
    if q.matured or partial_after == 0:
        q.partial_allowed = True
    elif partial_after is not None:
        opens = add_months(start, partial_after)
        q.partial_allowed = today >= opens
        if not q.partial_allowed:
            q.notes.append(f'Partial withdrawals open on {opens:%d %b %Y}. Until then you can only withdraw the '
                           'whole plan (early termination).')
    elif txn.end_date:
        q.notes.append(f'Partial withdrawals open when the plan matures on {txn.end_date:%d %b %Y}. Until then you '
                       'can only withdraw the whole plan (early termination).')
    else:
        q.partial_allowed = True

    # Earliest payout date: notice first, then the payout schedule
    payout_day = today
    if rules.get('notice_working_days'):
        payout_day = add_working_days(today, int(rules['notice_working_days']))
        q.notes.append(f'{rules["notice_working_days"]} working days\' notice is needed before payout.')
    elif rules.get('notice_hours'):
        payout_day = (now + timedelta(hours=int(rules['notice_hours']))).date()
    schedule = rules.get('payout', 'any')
    if schedule == 'month_end':
        # Normally paid at month end; withdrawing sooner needs the notice period (e.g. 24 hours)
        if not rules.get('notice_hours'):
            payout_day = month_end(payout_day)
        q.notes.append('Normally paid at the end of the month; withdrawing sooner needs 24 hours\' notice.')
    elif schedule == 'quarter_end':
        payout_day = quarter_end(payout_day)
        q.notes.append('Paid out at the end of the quarter.')
    q.earliest_payout_date = payout_day

    if amount is None:
        return q

    amount = Decimal(amount).quantize(CENT)
    if amount <= 0:
        q.error = 'Enter an amount greater than zero.'
        return q
    if amount > q.available:
        q.error = f'You can withdraw at most ₦{q.available:,.2f} from this plan.'
        return q
    q.kind = 'full' if amount == q.available else 'partial'
    if q.kind == 'partial' and not q.partial_allowed:
        q.error = 'Only the whole remaining amount can be withdrawn from this plan right now.'
        return q

    early = q.kind == 'full' and not q.matured and txn.end_date is not None
    penalty_days = rules.get('early_penalty_days')
    if early and rules.get('early_penalty_percent') and penalty_days and (today - start).days < int(penalty_days):
        q.penalty_percent = Decimal(str(rules['early_penalty_percent']))
        q.penalty = (amount * q.penalty_percent / 100).quantize(CENT, ROUND_HALF_UP)
        q.notes.append(f'Ending the plan within {penalty_days} days of its start attracts a {q.penalty_percent:g}% '
                       'penalty, deducted from the amount.')
    q.payout_amount = amount - q.penalty
    return q
