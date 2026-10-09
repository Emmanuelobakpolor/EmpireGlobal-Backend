import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from payments.models import BankAccount, FacilityAssignment

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'initial_bank_accounts.json'


class Command(BaseCommand):
    help = ("Load Empire Global's collection accounts and which facility pays into each. Existing accounts "
            "(by id) and facilities that already have an account are left as they are.")

    def handle(self, *args, **options):
        data = json.loads(FIXTURE.read_text('utf-8'))
        added = assigned = 0
        with transaction.atomic():
            accounts = {}
            for a in data['accounts']:
                account, created = BankAccount.objects.get_or_create(code=a['id'], defaults={
                    'bank_name': a['bankName'], 'account_name': a['accountName'],
                    'account_number': a['accountNumber'], 'notes': a.get('notes', ''),
                    'status': a.get('status', 'active'),
                })
                accounts[a['id']] = account
                if created:
                    added += 1
                    self.stdout.write(f'added    {account.code}  {account}')
            for facility, code in data['assignments'].items():
                _, created = FacilityAssignment.objects.get_or_create(facility=facility, defaults={'account': accounts[code]})
                if created:
                    assigned += 1
                    self.stdout.write(f'assigned {facility:<14} -> {code}')
        self.stdout.write(self.style.SUCCESS(f'{added} account(s) added, {assigned} facility assignment(s) set.'))
