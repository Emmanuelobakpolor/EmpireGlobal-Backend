"""The partial-withdrawal clause no longer appears in the savings plans' terms and conditions."""

from django.db import migrations


def forwards(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    for product in Product.objects.all():
        clauses = [c for c in product.clauses or [] if not c.startswith('Partial withdrawal can only')]
        if clauses != (product.clauses or []):
            product.clauses = clauses
            product.save(update_fields=['clauses'])


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0008_product_fees'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
