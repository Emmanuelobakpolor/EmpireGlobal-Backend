import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import NextOfKinField, person_name

from . import facilities, investments
from .facilities import add_months  # noqa: F401 - imported from here by views
from .files import validate_upload
from .models import (
    ApplicationDocument,
    BankAccount,
    FacilityAssignment,
    InterestOption,
    Notification,
    ProductType,
    Repayment,
    Transaction,
)

NEEDS_NEXT_OF_KIN = {ProductType.SAVINGS, ProductType.INVESTMENT}
GUARANTOR_FIELDS = ['fullName', 'phone', 'address', 'relationship', 'idType', 'idNumber']


def money(value):
    # Numbers, not strings, so the React app can keep doing arithmetic on them
    return float(value)


def naira(value):
    return f'₦{float(value):,.0f}'


class TransactionSerializer(serializers.ModelSerializer):
    """A transaction as the React app expects it (the customer's view)."""

    id = serializers.CharField(source='reference', read_only=True)
    customerId = serializers.CharField(source='customer.public_id', read_only=True)
    customerName = serializers.CharField(source='customer.full_name', read_only=True)
    productId = serializers.CharField(source='product_id')
    productName = serializers.CharField(source='product_name')
    productType = serializers.CharField(source='product_type')
    amount = serializers.SerializerMethodField()
    date = serializers.DateTimeField(source='created_at', format='%Y-%m-%d')
    termMonths = serializers.IntegerField(source='term_months')
    startDate = serializers.DateField(source='start_date')
    endDate = serializers.DateField(source='end_date')
    nextOfKin = serializers.JSONField(source='next_of_kin')
    termsAcceptedAt = serializers.DateTimeField(source='terms_accepted_at')
    rejectionReason = serializers.CharField(source='rejection_reason')
    # The collection account the customer was told to pay into
    paymentAccount = serializers.JSONField(source='paid_to')
    application = serializers.SerializerMethodField()
    receipt = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    # Loans and hire-purchase: terms, the customer's account and the repayment schedule
    facility = serializers.SerializerMethodField()
    # Investments: how interest is paid, where to, and its schedule
    investment = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            'id', 'reference', 'customerId', 'customerName', 'productId', 'productName', 'productType', 'amount',
            'date', 'termMonths', 'startDate', 'endDate', 'status', 'application', 'nextOfKin', 'termsAcceptedAt',
            'rejectionReason', 'paymentAccount', 'receipt', 'timeline', 'facility', 'investment',
        ]

    admin_view = False

    def get_investment(self, txn):
        return investments.as_dict(txn, admin=self.admin_view) if investments.earns_interest(txn) else None

    def repayment_data(self, rep, today, account):
        revealed = facilities.is_revealed(rep, today)
        return {
            'id': rep.pk,
            'reference': f'{rep.facility.reference}-R{rep.number:02d}',
            'number': rep.number,
            'dueDate': rep.due_date.isoformat(),
            'amount': money(rep.amount),
            'state': facilities.repayment_state(rep, today),
            # Where to pay appears only as the due date gets near, and stays until it's paid
            'paymentAccount': account if revealed else None,
            'opensOn': (rep.due_date - timedelta(days=facilities.REPAYMENT_NOTICE_DAYS)).isoformat(),
            'paidTo': rep.paid_to,
            'paidAt': rep.paid_at,
            'rejectionReason': rep.rejection_reason,
            'receipt': {
                'fileName': rep.receipt_name, 'size': rep.receipt_size, 'uploadedAt': rep.receipt_uploaded_at,
                'url': f'/api/repayments/{rep.pk}/receipt/' if rep.receipt else None,
            } if rep.receipt_uploaded_at else None,
        }

    def get_facility(self, txn):
        if not txn.is_facility:
            return None
        today = timezone.localdate()
        reps = list(txn.repayments.all())
        account = facilities.collection_account(txn) if any(facilities.is_revealed(r, today) for r in reps) else None
        repayments = [self.repayment_data(r, today, account) for r in reps]
        paid = sum((r.amount for r in reps if r.status == Repayment.Status.PAID), Decimal(0))
        unpaid = [r for r in repayments if r['state'] != 'paid']
        return {
            'status': txn.facility_status,
            'repaymentFrequency': txn.repayment_frequency,
            'interestRate': float(txn.interest_rate) if txn.interest_rate is not None else None,
            'totalRepayable': money(txn.total_repayable) if txn.total_repayable is not None else None,
            'amountPaid': money(paid),
            'outstanding': money(txn.total_repayable - paid) if txn.total_repayable is not None else None,
            'disbursementAccount': txn.disbursement_account,
            'disbursedAt': txn.disbursed_at,
            'disbursementReference': txn.disbursement_reference,
            'nextRepayment': unpaid[0] if unpaid else None,
            'overdueCount': sum(1 for r in repayments if r['state'] == 'overdue'),
            'noticeDays': facilities.REPAYMENT_NOTICE_DAYS,
            'repayments': repayments,
        }

    def get_amount(self, txn):
        return money(txn.amount)

    def get_application(self, txn):
        if not txn.application:
            return None
        # Uploaded files are listed under documents; the guarantor's ID sits with the guarantor
        app = {**txn.application, 'documents': {}}
        for doc in txn.documents.all():
            if doc.key == ApplicationDocument.GUARANTOR_ID:
                app['guarantor'] = {**app.get('guarantor', {}), 'idDocument': doc.as_dict()}
            else:
                app['documents'][doc.key] = doc.as_dict()
        return app

    def get_receipt(self, txn):
        if not txn.receipt_uploaded_at:
            return None
        return {
            'fileName': txn.receipt_name,
            'size': txn.receipt_size,
            'uploadedAt': txn.receipt_uploaded_at,
            'paidTo': txn.paid_to,
            # Seeded demo slips have a name but no stored file
            'url': f'/api/transactions/{txn.reference}/receipt/' if txn.receipt else None,
        }

    def get_timeline(self, txn):
        if txn.is_facility:
            return self.facility_timeline(txn)
        decided = txn.decided_at
        steps = [
            {'label': 'Application Submitted' if txn.application else 'Transaction Created', 'done': True, 'date': txn.created_at},
            {'label': 'Payment Instructions Generated', 'done': True, 'date': txn.created_at},
            {'label': 'Receipt Uploaded', 'done': bool(txn.receipt_uploaded_at), 'date': txn.receipt_uploaded_at},
            {'label': 'Awaiting Verification', 'done': bool(decided), 'date': decided,
             'current': bool(txn.receipt_uploaded_at) and not decided},
            {'label': 'Payment Approved', 'done': txn.status == Transaction.Status.APPROVED,
             'date': decided if txn.status == Transaction.Status.APPROVED else None},
        ]
        if txn.status == Transaction.Status.REJECTED:
            steps.append({'label': 'Payment Rejected', 'done': True, 'rejected': True, 'date': decided})
        return self.format_steps(steps)

    def facility_timeline(self, txn):
        status = txn.facility_status
        decided = txn.decided_at
        approved = txn.status == Transaction.Status.APPROVED
        completed_at = None
        if status == 'completed':
            completed_at = max((r.paid_at for r in txn.repayments.all() if r.paid_at), default=None)
        steps = [
            {'label': 'Application Submitted', 'done': True, 'date': txn.created_at},
            {'label': 'Under Review', 'done': bool(decided), 'date': decided, 'current': status == 'under_review'},
        ]
        if txn.status == Transaction.Status.REJECTED:
            steps.append({'label': 'Application Rejected', 'done': True, 'rejected': True, 'date': decided})
            return self.format_steps(steps)
        steps += [
            {'label': 'Application Approved', 'done': approved, 'date': decided if approved else None},
            {'label': 'Loan Disbursed' if txn.product_type == ProductType.LOAN else 'Item Delivered',
             'done': bool(txn.disbursed_at), 'date': txn.disbursed_at, 'current': status == 'awaiting_disbursement'},
            {'label': 'Repaying', 'done': status == 'completed', 'date': None, 'current': status == 'active'},
            {'label': 'Fully Repaid', 'done': status == 'completed', 'date': completed_at},
        ]
        return self.format_steps(steps)

    def format_steps(self, steps):
        for step in steps:
            if step.get('date') is None:
                step.pop('date', None)
            else:
                step['date'] = serializers.DateTimeField().to_representation(step['date'])
        return steps


class AdminTransactionSerializer(TransactionSerializer):
    """Adds the internal slip review trail, which customers don't see."""

    slipTrail = serializers.SerializerMethodField()
    admin_view = True

    class Meta(TransactionSerializer.Meta):
        fields = TransactionSerializer.Meta.fields + ['slipTrail']

    def get_slipTrail(self, txn):
        return [
            {'action': e.action, 'by': e.actor_name, 'role': e.actor_role, 'at': e.at, **({'note': e.note} if e.note else {})}
            for e in txn.slip_events.all()
        ]

    def repayment_data(self, rep, today, account):
        data = super().repayment_data(rep, today, account)
        data['trail'] = [
            {'action': e.action, 'by': e.actor_name, 'role': e.actor_role, 'at': e.at, **({'note': e.note} if e.note else {})}
            for e in rep.events.all()
        ]
        return data


def _text(value, max_length=255):
    return value.strip()[:max_length] if isinstance(value, str) else ''


class CreateTransactionSerializer(serializers.Serializer):
    """Checks a new transaction against the live product: limits, plan length, terms and the
    documents its application needs. Files arrive alongside as `document.<key>`."""

    productId = serializers.CharField(max_length=32)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=1)
    termMonths = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=120)
    application = serializers.JSONField(required=False, allow_null=True)
    nextOfKin = NextOfKinField(required=False, allow_null=True)
    termsAccepted = serializers.BooleanField(required=False, default=False)
    # Loans and hire-purchase: how they'll repay, and their own bank account
    repaymentFrequency = serializers.CharField(required=False, allow_blank=True, max_length=16)
    # Loans: where it's paid out to. Investments and savings plans: where interest is sent.
    disbursementAccount = serializers.JSONField(required=False, allow_null=True)
    # Investments: how interest is paid
    interestOption = serializers.CharField(required=False, allow_blank=True, max_length=16)

    def validate_productId(self, code):
        from catalog.models import Product

        product = Product.objects.filter(code=code, status=Product.Status.ACTIVE).first()
        if not product:
            raise serializers.ValidationError('This product is not available.')
        return product

    def validate(self, attrs):
        product = attrs['productId']
        errors = {}

        amount = attrs['amount']
        if not product.min_amount <= amount <= product.max_amount:
            errors['amount'] = [f'Amount must be between {naira(product.min_amount)} and {naira(product.max_amount)}.']

        options = product.term_options or []
        term = attrs.get('termMonths')
        if product.type == ProductType.INVESTMENT:
            # Every investment is a 12-month contract
            options, term = [investments.TERM_MONTHS], investments.TERM_MONTHS
        if not options:
            term = None  # Open-ended plan
        elif len(options) == 1:
            term = term or options[0]
        if options and term not in options:
            errors['termMonths'] = [f'Choose a plan length of {", ".join(map(str, options))} months.']
        attrs['termMonths'] = term

        if product.clauses and not attrs.get('termsAccepted'):
            errors['termsAccepted'] = ['Accept the terms and clauses to continue.']
        if product.type in NEEDS_NEXT_OF_KIN and not attrs.get('nextOfKin'):
            errors['nextOfKin'] = ['Next of kin is required for this product.']

        if product.needs_application:
            attrs['application'], attrs['documents'] = self.check_application(product, attrs.get('application') or {}, errors)
            frequencies = facilities.repayment_frequencies(product.frequency)
            frequency = attrs.get('repaymentFrequency') or (frequencies[0] if len(frequencies) == 1 else '')
            if frequency not in frequencies:
                errors['repaymentFrequency'] = [f'Choose how you will repay: {", ".join(frequencies)}.']
            attrs['repaymentFrequency'] = frequency
            attrs['disbursementAccount'] = self.check_bank_account(attrs.get('disbursementAccount'), errors)
        else:
            attrs['application'], attrs['documents'] = None, []
            attrs['repaymentFrequency'], attrs['disbursementAccount'] = '', None

        option = attrs.get('interestOption') or ''
        attrs['interestOption'] = ''
        if product.type == ProductType.INVESTMENT:
            if option not in InterestOption.values:
                errors['interestOption'] = ['Choose how you want your interest paid: upfront, monthly or at maturity.']
            attrs['interestOption'] = option
        elif product.type == ProductType.SAVINGS and term:
            # Savings earn interest paid at maturity (an open-ended plan has no maturity)
            attrs['interestOption'] = InterestOption.MATURITY
        if product.type == ProductType.INVESTMENT or attrs['interestOption']:
            # Where interest is sent
            attrs['disbursementAccount'] = self.check_bank_account(self.initial_data.get('disbursementAccount'), errors)

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def check_application(self, product, raw, errors):
        files = self.context.get('files', {})
        guarantor = raw.get('guarantor') if isinstance(raw.get('guarantor'), dict) else {}
        clean_guarantor = {k: _text(guarantor.get(k)) for k in GUARANTOR_FIELDS}
        missing = [k for k, v in clean_guarantor.items() if not v]
        if missing:
            errors['guarantor'] = [f'Guarantor details are incomplete: {", ".join(missing)}.']
        else:
            try:
                clean_guarantor['fullName'] = person_name(clean_guarantor['fullName'])
            except serializers.ValidationError:
                errors['guarantor'] = ["Enter the guarantor's first name and last name."]
        application = {'guarantor': clean_guarantor}

        if product.type == ProductType.HIRE_PURCHASE:
            item = raw.get('item') if isinstance(raw.get('item'), dict) else {}
            category, description = _text(item.get('category'), 100), _text(item.get('description'), 500)
            if category not in (product.item_categories or []) or not description:
                errors['item'] = ['Choose an eligible item and describe it.']
            application['item'] = {'category': category, 'description': description}

        documents = []
        specs = product.document_specs() + [{'key': ApplicationDocument.GUARANTOR_ID, 'label': 'Guarantor ID'}]
        for spec in specs:
            upload = files.get(f'document.{spec["key"]}')
            if not upload:
                errors.setdefault('documents', []).append(f'{spec["label"]} is required.')
                continue
            try:
                validate_upload(upload)
            except serializers.ValidationError as exc:
                errors.setdefault('documents', []).append(f'{spec["label"]}: {exc.detail[0]}')
                continue
            documents.append((spec, upload))
        return application, documents


    def check_bank_account(self, raw, errors):
        """The customer's own account: where a loan is paid out to, or investment interest sent."""
        raw = raw if isinstance(raw, dict) else {}
        account = {
            'bankName': _text(raw.get('bankName'), 100),
            'accountNumber': _text(raw.get('accountNumber'), 20),
            'accountName': _text(raw.get('accountName'), 150),
        }
        problems = []
        if not account['bankName']:
            problems.append('Bank name is required.')
        if not (len(account['accountNumber']) == 10 and account['accountNumber'].isdigit()):
            problems.append('Enter the 10-digit NUBAN account number.')
        if not account['accountName']:
            problems.append('Account name is required.')
        if problems:
            errors['disbursementAccount'] = problems
        return account


class ReceiptSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, file):
        return validate_upload(file)


class ReviewSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=500, default='')


class RecommendSerializer(ReviewSerializer):
    decision = serializers.ChoiceField(choices=['approve', 'reject'])


class ApproveSerializer(ReviewSerializer):
    # Loans and hire-purchase: the rate to charge, when it isn't the product's own
    interestRate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100,
                                            required=False, allow_null=True)


class InterestRateSerializer(ReviewSerializer):
    interestRate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100)


class DisburseSerializer(serializers.Serializer):
    reference = serializers.CharField(max_length=100)


class InterestPaymentListSerializer(serializers.Serializer):
    """An investment's interest payment, in the admin list of payments to send."""

    def to_representation(self, p):
        txn = p.plan
        return {
            'id': p.pk,
            'reference': f'{txn.reference}-I{p.number:02d}',
            'number': p.number,
            'count': p.count,
            'planReference': txn.reference,
            'productName': txn.product_name,
            'customerId': txn.customer.public_id,
            'customerName': txn.customer.full_name,
            'agentCode': txn.customer.agent_code or None,
            'option': txn.interest_option,
            'dueDate': p.due_date.isoformat(),
            'amount': money(p.amount),
            'state': investments.payment_state(p),
            'payoutAccount': txn.disbursement_account,
            'paidAt': p.paid_at,
            'paidBy': p.paid_by_name,
            'payoutReference': p.payout_reference,
        }


class NotificationSerializer(serializers.ModelSerializer):
    date = serializers.DateTimeField(source='created_at')

    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'type', 'kind', 'link', 'read', 'date']


# ---- Bank accounts ----

FACILITIES = [FacilityAssignment.DEFAULT] + [t.value for t in ProductType]


class BankAccountSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='code', read_only=True)
    bankName = serializers.CharField(source='bank_name', max_length=100)
    accountName = serializers.CharField(source='account_name', max_length=150)
    accountNumber = serializers.RegexField(r'^\d{10}$', source='account_number',
                                           error_messages={'invalid': 'Enter the 10-digit NUBAN account number.'})
    notes = serializers.CharField(max_length=300, required=False, allow_blank=True)
    createdAt = serializers.DateTimeField(source='created_at', format='%Y-%m-%d', read_only=True)

    class Meta:
        model = BankAccount
        fields = ['id', 'bankName', 'accountName', 'accountNumber', 'notes', 'status', 'createdAt']

    def validate_bankName(self, value):
        if not value.strip():
            raise serializers.ValidationError('Bank name is required.')
        return value.strip()

    def validate_accountName(self, value):
        if not value.strip():
            raise serializers.ValidationError('Account name is required.')
        return value.strip()


def assignments_dict():
    return dict(FacilityAssignment.objects.values_list('facility', 'account__code'))


class AssignmentSerializer(serializers.Serializer):
    accountId = serializers.CharField(allow_blank=True)

    def validate_accountId(self, code):
        if not code:
            return None
        account = BankAccount.objects.filter(code=code).first()
        if not account:
            raise serializers.ValidationError('Bank account not found.')
        if not account.is_active:
            raise serializers.ValidationError('Choose an active account.')
        return account


# ---- Withdrawals ----

class WithdrawalSerializer(serializers.Serializer):
    """A withdrawal as the customer sees it."""

    def to_representation(self, wd):
        return {
            'id': wd.reference,
            'reference': wd.reference,
            'customerId': wd.customer.public_id,
            'customerName': wd.customer.full_name,
            'planReference': wd.plan.reference,
            'productName': wd.plan.product_name,
            'productType': wd.plan.product_type,
            'kind': wd.kind,
            'amount': money(wd.amount),
            'penalty': money(wd.penalty),
            'interestDeducted': money(wd.interest_deducted),
            'payoutAmount': money(wd.payout_amount),
            'earliestPayoutDate': wd.earliest_payout_date.isoformat(),
            'bankName': wd.bank_name,
            'accountNumber': wd.account_number,
            'accountName': wd.account_name,
            'note': wd.note,
            'status': wd.status,
            'rejectionReason': wd.rejection_reason,
            'payoutReference': wd.payout_reference,
            'decidedAt': wd.decided_at,
            'paidAt': wd.paid_at,
            'createdAt': wd.created_at,
        }


class AdminWithdrawalSerializer(WithdrawalSerializer):
    """Adds the review trail and the customer's agent."""

    def to_representation(self, wd):
        data = super().to_representation(wd)
        data['agentCode'] = wd.customer.agent_code or None
        data['customerEmail'] = wd.customer.email
        data['trail'] = [
            {'action': e.action, 'by': e.actor_name, 'role': e.actor_role, 'at': e.at, **({'note': e.note} if e.note else {})}
            for e in wd.events.all()
        ]
        return data


class WithdrawalQuoteSerializer(serializers.Serializer):
    planReference = serializers.CharField(max_length=32)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)


class CreateWithdrawalSerializer(serializers.Serializer):
    planReference = serializers.CharField(max_length=32)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('0.01'))
    bankName = serializers.CharField(max_length=100)
    accountNumber = serializers.RegexField(r'^\d{10}$', error_messages={'invalid': 'Enter the 10-digit NUBAN account number.'})
    accountName = serializers.CharField(max_length=150)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')
    # Bank details change with every request, so the customer confirms it's really them
    password = serializers.CharField(trim_whitespace=False)

    def validate_bankName(self, value):
        if not value.strip():
            raise serializers.ValidationError('Bank name is required.')
        return value.strip()

    def validate_accountName(self, value):
        if not value.strip():
            raise serializers.ValidationError('Account name is required.')
        return value.strip()


class PayoutSerializer(serializers.Serializer):
    payoutReference = serializers.CharField(max_length=100)
