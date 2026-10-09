import json
from pathlib import Path

from django.db import migrations

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'initial_products.json'


def load(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    if Product.objects.exists():
        return
    for position, p in enumerate(json.loads(FIXTURE.read_text('utf-8')), start=1):
        Product.objects.create(
            code=p['id'], name=p['name'], category=p.get('category', ''), type=p['type'],
            description=p['description'], min_amount=p['minAmount'], max_amount=p['maxAmount'],
            duration=p.get('duration') or '', frequency=p.get('frequency') or '',
            term_options=p.get('termOptions'), expected_return=p.get('expectedReturn') or '',
            benefits=p.get('benefits') or [], clauses=p.get('clauses') or [],
            requirements=p.get('requirements') or [], required_documents=p.get('requiredDocuments') or [],
            item_categories=p.get('itemCategories') or [], status=p.get('status', 'active'), position=position,
        )


class Migration(migrations.Migration):
    """The official Empire Global product catalogue (previously hard-coded in the React app)."""

    dependencies = [('catalog', '0001_initial')]
    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
