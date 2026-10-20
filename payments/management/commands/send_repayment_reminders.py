from django.core.management.base import BaseCommand

from payments.facilities import send_reminders
from payments.investments import send_due_notices


class Command(BaseCommand):
    help = ('Remind customers of loan and hire-purchase instalments coming due, and flag overdue ones. '
            'Also tell admins about investment interest coming due. Each is sent once, so it is safe to '
            'run as often as you like (e.g. daily).')

    def handle(self, *args, **options):
        count = send_reminders()
        self.stdout.write(self.style.SUCCESS(f'Sent reminders for {count} facilit{"y" if count == 1 else "ies"}.'))
        due = send_due_notices()
        self.stdout.write(self.style.SUCCESS(f'Told admins about {due} interest payment(s) coming due.'))
