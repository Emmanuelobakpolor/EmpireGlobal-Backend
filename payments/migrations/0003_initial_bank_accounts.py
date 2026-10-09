import json
from pathlib import Path

from django.db import migrations

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'initial_bank_accounts.json'


def load(apps, schema_editor):
    BankAccount = apps.get_model('payments', 'BankAccount')
    FacilityAssignment = apps.get_model('payments', 'FacilityAssignment')
    if BankAccount.objects.exists():
        return
    data = json.loads(FIXTURE.read_text('utf-8'))
    accounts = {}
    for a in data['accounts']:
        accounts[a['id']] = BankAccount.objects.create(
            code=a['id'], bank_name=a['bankName'], account_name=a['accountName'],
            account_number=a['accountNumber'], notes=a.get('notes', ''), status=a.get('status', 'active'),
        )
    for facility, code in data['assignments'].items():
        FacilityAssignment.objects.create(facility=facility, account=accounts[code])


class Migration(migrations.Migration):
    """Empire Global's collection accounts (previously hard-coded in the React app)."""

    dependencies = [('payments', '0002_bankaccount_facilityassignment_applicationdocument')]
    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
