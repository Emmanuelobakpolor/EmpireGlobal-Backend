from django.db import migrations

LUMP_SUM = {'allowed': True, 'partial_after_months': None, 'early_penalty_percent': 5, 'early_penalty_days': 30,
            'notice_working_days': 5, 'payout': 'any'}
PERIODIC = {'allowed': True, 'partial_after_months': 8, 'early_penalty_percent': 5, 'early_penalty_days': 30,
            'notice_working_days': 5, 'payout': 'any'}

# Confirmed with Empire Global; loans and hire-purchase hold no money, so they get no rules
RULES = {
    'p3': LUMP_SUM, 'p4': LUMP_SUM,
    'p2': PERIODIC, 'p1': PERIODIC, 'p11': PERIODIC,
    'p5': {'allowed': True, 'partial_after_months': 0, 'notice_hours': 24, 'payout': 'month_end'},
    'p6': {'allowed': True, 'partial_after_months': 0, 'payout': 'quarter_end'},
    'p12': {'allowed': True, 'partial_after_months': 0, 'payout': 'any'},
}


def apply(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    for code, rules in RULES.items():
        Product.objects.filter(code=code, withdrawal_rules={}).update(withdrawal_rules=rules)


class Migration(migrations.Migration):
    """Withdrawal rules for the official catalogue (see payments/withdrawal_rules.py)."""

    dependencies = [('catalog', '0003_product_withdrawal_rules')]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
