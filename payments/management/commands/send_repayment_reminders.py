from django.core.management.base import BaseCommand

from payments.facilities import send_reminders


class Command(BaseCommand):
    help = ('Remind customers of loan and hire-purchase instalments coming due, and flag overdue ones. '
            'Each instalment is reminded once, so it is safe to run as often as you like (e.g. daily).')

    def handle(self, *args, **options):
        count = send_reminders()
        self.stdout.write(self.style.SUCCESS(f'Sent reminders for {count} facilit{"y" if count == 1 else "ies"}.'))
