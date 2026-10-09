import re

from django.db import IntegrityError, models, transaction
from django.utils import timezone


def normalize_agent_code(code):
    return (code or '').strip().upper()


class Agent(models.Model):
    """A field agent customers register under, by entering the agent's code."""

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        # Agents are deactivated rather than deleted, so customers stay linked to them
        INACTIVE = 'inactive', 'Inactive'

    FIRST_NUMBER = 1001

    code = models.CharField(max_length=16, unique=True, editable=False)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32)
    location = models.CharField(max_length=150)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.name} ({self.code})'

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @classmethod
    def next_code(cls):
        numbers = [int(m.group()) for c in cls.objects.values_list('code', flat=True) if (m := re.search(r'\d+', c))]
        return f'AG-{max(numbers, default=cls.FIRST_NUMBER - 1) + 1}'

    def save(self, *args, **kwargs):
        if self.code:
            self.code = normalize_agent_code(self.code)
            return super().save(*args, **kwargs)
        # Sequential codes (AG-1001, AG-1002, ...); retry if two admins create one at once
        for _ in range(5):
            self.code = self.next_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                self.code = ''
        raise IntegrityError('Could not allocate an agent code.')


class PlatformSettings(models.Model):
    """Platform-wide settings edited by Super Admins. There is only ever one row."""

    platform_name = models.CharField(max_length=100, default='Empire Global')
    support_email = models.EmailField(default='Info@empireglobalbenefits.com')
    support_phone = models.CharField(max_length=32, default='+234 9068410302')
    timezone = models.CharField(max_length=64, default='Africa/Lagos')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'platform settings'

    def __str__(self):
        return 'Platform settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class AuditLog(models.Model):
    """What an admin (or the system) did. Written by the server as actions happen; never edited."""

    class Status(models.TextChoices):
        SUCCESS = 'success', 'Success'
        INFO = 'info', 'Info'
        WARNING = 'warning', 'Warning'
        ERROR = 'error', 'Error'

    # Name and role are copied so entries stay readable if the account is later removed
    actor = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    actor_name = models.CharField(max_length=150)
    actor_role = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=100)
    reference = models.CharField(max_length=200, blank=True)
    agent_code = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INFO)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.action} by {self.actor_name}'
