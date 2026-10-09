import json
from decimal import Decimal
from calendar import monthrange
from datetime import date

from rest_framework import serializers

from accounts.serializers import NextOfKinField

from .files import validate_upload
from .models import ApplicationDocument, BankAccount, FacilityAssignment, Notification, ProductType, Transaction

NEEDS_NEXT_OF_KIN = {ProductType.SAVINGS, ProductType.INVESTMENT}
GUARANTOR_FIELDS = ['fullName', 'phone', 'address', 'relationship', 'idType', 'idNumber']


def add_months(start, months):
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


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

    class Meta:
        model = Transaction
        fields = [
            'id', 'reference', 'customerId', 'customerName', 'productId', 'productName', 'productType', 'amount',
            'date', 'termMonths', 'startDate', 'endDate', 'status', 'application', 'nextOfKin', 'termsAcceptedAt',
            'rejectionReason', 'paymentAccount', 'receipt', 'timeline',
        ]

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
        for step in steps:
            if step['date'] is None:
                del step['date']
            else:
                step['date'] = serializers.DateTimeField().to_representation(step['date'])
        return steps


class AdminTransactionSerializer(TransactionSerializer):
    """Adds the internal slip review trail, which customers don't see."""

    slipTrail = serializers.SerializerMethodField()

    class Meta(TransactionSerializer.Meta):
        fields = TransactionSerializer.Meta.fields + ['slipTrail']

    def get_slipTrail(self, txn):
        return [
            {'action': e.action, 'by': e.actor_name, 'role': e.actor_role, 'at': e.at, **({'note': e.note} if e.note else {})}
            for e in txn.slip_events.all()
        ]


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
        else:
            attrs['application'], attrs['documents'] = None, []

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


class ReceiptSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, file):
        return validate_upload(file)


class ReviewSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=500, default='')


class RecommendSerializer(ReviewSerializer):
    decision = serializers.ChoiceField(choices=['approve', 'reject'])


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
