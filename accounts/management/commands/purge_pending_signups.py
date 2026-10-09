from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Max, Q
from django.utils import timezone

from accounts.models import Role, Status, User


class Command(BaseCommand):
    help = 'Delete customer sign-ups that never confirmed their email.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours', type=int, default=settings.PENDING_SIGNUP_MAX_AGE_HOURS,
            help='Remove sign-ups with no activity for this many hours (default: %(default)s).',
        )
        parser.add_argument('--dry-run', action='store_true', help='List what would be removed without deleting.')

    def handle(self, *args, hours, dry_run, **options):
        cutoff = timezone.now() - timedelta(hours=hours)
        # Abandoned: still unverified, created before the cutoff, and no code requested since
        stale = (
            User.objects.filter(role=Role.CUSTOMER, status=Status.PENDING, date_joined__lt=cutoff)
            .annotate(last_code=Max('email_otps__created_at'))
            .filter(Q(last_code__isnull=True) | Q(last_code__lt=cutoff))
        )
        count = stale.count()
        for user in stale:
            self.stdout.write(f'{"would remove" if dry_run else "removing"}  {user.public_id}  {user.email}')
        if not dry_run:
            # Deleting through the queryset also removes their emailed codes
            User.objects.filter(pk__in=[u.pk for u in stale]).delete()
        verb = 'Would remove' if dry_run else 'Removed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {count} unverified sign-up{"" if count == 1 else "s"} older than {hours}h.'))
