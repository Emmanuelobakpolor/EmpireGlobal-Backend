import re

from django.db import models
from django.utils import timezone

from payments.models import ProductType

# Standard documents every loan or hire-purchase application needs; a product can add more
STANDARD_DOCUMENTS = [
    {'key': 'passport', 'label': 'Passport photograph'},
    {'key': 'proofOfAddress', 'label': 'Proof of address'},
    {'key': 'proofOfId', 'label': 'Proof of ID'},
    {'key': 'applicationForm', 'label': 'Completed application form'},
]

NEEDS_APPLICATION = {ProductType.LOAN, ProductType.HIRE_PURCHASE}


class Product(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        DISABLED = 'disabled', 'Disabled'

    # Public id used by the React app and kept on transactions (p1, p2, ...)
    code = models.CharField(max_length=32, unique=True, editable=False)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, blank=True)
    type = models.CharField(max_length=16, choices=ProductType.choices)
    description = models.TextField()
    min_amount = models.DecimalField(max_digits=14, decimal_places=2)
    max_amount = models.DecimalField(max_digits=14, decimal_places=2)
    duration = models.CharField(max_length=100, blank=True)
    frequency = models.CharField(max_length=100, blank=True)
    # Plan lengths in months the customer can choose; null means open-ended (no expiry)
    term_options = models.JSONField(null=True, blank=True)
    expected_return = models.CharField(max_length=150, blank=True)
    benefits = models.JSONField(default=list, blank=True)
    clauses = models.JSONField(default=list, blank=True)
    requirements = models.JSONField(default=list, blank=True)
    # Documents needed on top of STANDARD_DOCUMENTS: [{key, label, hint}]
    required_documents = models.JSONField(default=list, blank=True)
    # Hire-purchase only: eligible item categories
    item_categories = models.JSONField(default=list, blank=True)
    # How money can be taken out of a plan; see payments/withdrawal_rules.py for the keys.
    # Empty means withdrawals aren't allowed.
    withdrawal_rules = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'pk']

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def needs_application(self):
        return self.type in NEEDS_APPLICATION

    def document_specs(self):
        """Every document an application for this product must include."""
        return STANDARD_DOCUMENTS + list(self.required_documents or [])

    @classmethod
    def next_code(cls):
        numbers = [int(m.group()) for c in cls.objects.values_list('code', flat=True) if (m := re.search(r'\d+', c))]
        return f'p{max(numbers, default=0) + 1}'

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self.next_code()
        if not self.position:
            self.position = (Product.objects.aggregate(models.Max('position'))['position__max'] or 0) + 1
        super().save(*args, **kwargs)
