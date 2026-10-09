import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ProductType(models.TextChoices):
    SAVINGS = 'savings', 'Savings'
    INVESTMENT = 'investment', 'Investment'
    THRIFT = 'thrift', 'Thrift'
    LOAN = 'loan', 'Loan'
    HIRE_PURCHASE = 'hire-purchase', 'Hire-Purchase'


REFERENCE_CODES = {
    ProductType.SAVINGS: 'SAV',
    ProductType.INVESTMENT: 'INV',
    ProductType.THRIFT: 'THR',
    ProductType.LOAN: 'LN',
    ProductType.HIRE_PURCHASE: 'HP',
}

# Which customer balance an approved payment credits. Hire-purchase pays for goods,
# so it doesn't move a balance.
BALANCE_FIELD_BY_TYPE = {
    ProductType.SAVINGS: 'savings_balance',
    ProductType.THRIFT: 'savings_balance',
    ProductType.INVESTMENT: 'investment_balance',
    ProductType.LOAN: 'outstanding_loan',
}


def receipt_path(instance, filename):
    # Random names: the customer's file name is kept separately, never used on disk
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f'receipts/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{ext}'


class Transaction(models.Model):
    class Status(models.TextChoices):
        # Created, payment instructions shown, no receipt yet
        DRAFT = 'draft', 'Draft'
        # Receipt uploaded, awaiting verification
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    FINAL = (Status.APPROVED, Status.REJECTED)

    reference = models.CharField(max_length=32, unique=True, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='transactions')

    # Snapshot of the product when the transaction was made (the catalogue can change later)
    product_id = models.CharField(max_length=32)
    product_name = models.CharField(max_length=200)
    product_type = models.CharField(max_length=16, choices=ProductType.choices)

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    term_months = models.PositiveSmallIntegerField(null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    # Loan and hire-purchase applications: documents, guarantor, item
    application = models.JSONField(null=True, blank=True)
    next_of_kin = models.JSONField(null=True, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    receipt = models.FileField(upload_to=receipt_path, blank=True)
    receipt_name = models.CharField(max_length=255, blank=True)
    receipt_size = models.PositiveIntegerField(null=True, blank=True)
    receipt_uploaded_at = models.DateTimeField(null=True, blank=True)
    # The collection account shown to the customer when the transaction was created,
    # so later changes to bank accounts can't alter what they were told
    paid_to = models.JSONField(null=True, blank=True)

    decided_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.reference

    @property
    def is_final(self):
        return self.status in self.FINAL

    @classmethod
    def new_reference(cls, product_type):
        code = REFERENCE_CODES.get(product_type, 'TXN')
        day = timezone.localdate().strftime('%Y%m%d')
        while True:
            reference = f'EMP-{code}-{day}-{secrets.randbelow(9000) + 1000}'
            if not cls.objects.filter(reference=reference).exists():
                return reference

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self.new_reference(self.product_type)
        super().save(*args, **kwargs)


class SlipEvent(models.Model):
    """One step in a payment slip's review: who viewed, recommended or decided, and when."""

    class Action(models.TextChoices):
        VIEWED = 'viewed', 'Viewed slip'
        RECOMMENDED_APPROVAL = 'recommended_approval', 'Recommended approval'
        RECOMMENDED_REJECTION = 'recommended_rejection', 'Recommended rejection'
        APPROVED = 'approved', 'Final approval'
        REJECTED = 'rejected', 'Final rejection'

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='slip_events')
    action = models.CharField(max_length=32, choices=Action.choices)
    # Name and role are copied so the trail still reads correctly if the admin is later removed
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    actor_name = models.CharField(max_length=150)
    actor_role = models.CharField(max_length=16)
    note = models.CharField(max_length=500, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['at', 'pk']


class Notification(models.Model):
    """A message for one person (customer or admin), created by the server when something happens."""

    class Type(models.TextChoices):
        SUCCESS = 'success', 'Success'
        ERROR = 'error', 'Error'
        INFO = 'info', 'Info'
        WARNING = 'warning', 'Warning'

    class Kind(models.TextChoices):
        # Decides the icon in the app
        PAYMENT = 'payment', 'Payment'
        RECEIPT = 'receipt', 'Receipt'
        APPLICATION = 'application', 'Application'
        CUSTOMER = 'customer', 'Customer'
        ACCOUNT = 'account', 'Account'
        WITHDRAWAL = 'withdrawal', 'Withdrawal'

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=150)
    message = models.CharField(max_length=500)
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.INFO)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.ACCOUNT)
    # Where opening the notification takes the reader, e.g. /customer/transactions/EMP-SAV-...
    link = models.CharField(max_length=200, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['recipient', 'read'])]


def document_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
    return f'applications/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{ext}'


class ApplicationDocument(models.Model):
    """A file attached to a loan or hire-purchase application (ID, proof of address, ...)."""

    # The guarantor's ID is stored under this key alongside the product's documents
    GUARANTOR_ID = 'guarantorId'

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='documents')
    key = models.CharField(max_length=40)
    label = models.CharField(max_length=150)
    file = models.FileField(upload_to=document_path)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['transaction', 'key'], name='one_document_per_key')]

    def as_dict(self):
        return {
            'label': self.label,
            'fileName': self.original_name,
            'size': self.size,
            'uploadedAt': self.uploaded_at.isoformat(),
            'url': f'/api/transactions/{self.transaction.reference}/documents/{self.key}/',
        }


class BankAccount(models.Model):
    """A collection account customers pay into."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'

    code = models.CharField(max_length=16, unique=True, editable=False)
    bank_name = models.CharField(max_length=100)
    account_name = models.CharField(max_length=150)
    account_number = models.CharField(max_length=20)
    notes = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['pk']

    def __str__(self):
        return f'{self.bank_name} · {self.account_number} ({self.account_name})'

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    def as_payment_details(self):
        return {'bankName': self.bank_name, 'accountName': self.account_name, 'accountNumber': self.account_number,
                'notes': self.notes}

    def save(self, *args, **kwargs):
        if not self.code:
            last = BankAccount.objects.order_by('-pk').first()
            self.code = f'ba{(last.pk if last else 0) + 1}'
            while BankAccount.objects.filter(code=self.code).exists():
                self.code = f'ba{int(self.code[2:]) + 1}'
        super().save(*args, **kwargs)

    @classmethod
    def for_product_type(cls, product_type):
        """The active account a facility pays into: its own, else the default, else any active one."""
        for facility in (product_type, FacilityAssignment.DEFAULT):
            assignment = FacilityAssignment.objects.filter(facility=facility).select_related('account').first()
            if assignment and assignment.account.is_active:
                return assignment.account
        return cls.objects.filter(status=cls.Status.ACTIVE).first()


class FacilityAssignment(models.Model):
    """Which account a facility (product type, or 'default') pays into."""

    DEFAULT = 'default'

    facility = models.CharField(max_length=16, unique=True)
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='assignments')


class Withdrawal(models.Model):
    """A customer taking money out of a plan, paid by bank transfer once a Super Admin approves."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPROVED = 'approved', 'Approved, awaiting payout'
        PAID = 'paid', 'Paid'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    class Kind(models.TextChoices):
        PARTIAL = 'partial', 'Partial withdrawal'
        FULL = 'full', 'Full withdrawal'

    reference = models.CharField(max_length=32, unique=True, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='withdrawals')
    plan = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name='withdrawals')
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # `amount` comes off the plan and balance; `payout_amount` is what's sent after any penalty
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    penalty = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payout_amount = models.DecimalField(max_digits=14, decimal_places=2)
    earliest_payout_date = models.DateField()
    bank_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=10)
    account_name = models.CharField(max_length=150)
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.CharField(max_length=500, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payout_reference = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if not self.reference:
            day = timezone.localdate().strftime('%Y%m%d')
            while True:
                reference = f'EMP-WD-{day}-{secrets.randbelow(9000) + 1000}'
                if not Withdrawal.objects.filter(reference=reference).exists():
                    self.reference = reference
                    break
        super().save(*args, **kwargs)


class WithdrawalEvent(models.Model):
    """One step in a withdrawal's review: recommended, approved, rejected, paid or cancelled."""

    withdrawal = models.ForeignKey(Withdrawal, on_delete=models.CASCADE, related_name='events')
    action = models.CharField(max_length=32)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    actor_name = models.CharField(max_length=150)
    actor_role = models.CharField(max_length=16)
    note = models.CharField(max_length=500, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['at', 'pk']
