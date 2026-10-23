"""Adds the Ajo (Esusu) product from the catalogue fixture, after the ASA in the Thrift Savings list."""

import json
from pathlib import Path

from django.db import migrations
from django.db.models import F

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'initial_products.json'
CODE = 'p17'


def forwards(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    # A new database gets the whole catalogue, this product included, from 0002
    if not Product.objects.exists() or Product.objects.filter(code=CODE).exists():
        return
    p = next(p for p in json.loads(FIXTURE.read_text('utf-8')) if p['id'] == CODE)
    after = Product.objects.filter(code='p12').first()
    position = after.position + 1 if after else (Product.objects.order_by('-position').first().position + 1)
    Product.objects.filter(position__gte=position).update(position=F('position') + 1)
    Product.objects.create(
        code=CODE, name=p['name'], category=p['category'], type=p['type'], description=p['description'],
        min_amount=p['minAmount'], max_amount=p['maxAmount'], duration=p['duration'], frequency=p['frequency'],
        term_options=p['termOptions'], expected_return='', benefits=p['benefits'], clauses=p['clauses'],
        withdrawal_rules=p['withdrawalRules'], status=p['status'], position=position,
    )


def backwards(apps, schema_editor):
    apps.get_model('catalog', 'Product').objects.filter(code=CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0010_product_type_ajo'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
