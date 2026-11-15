"""Ajo's wording follows its new rules: cycles run on calendar months, the service charge is one
contribution for each month saved in, and savings can be withdrawn at any time. Only text still
as the catalogue shipped it is replaced, so an admin's own wording is left alone."""

from django.db import migrations

OLD_DESCRIPTION = ('Contribute a fixed amount every day or week for a cycle and collect everything you saved at the '
                   'end, less a service charge of one contribution.')
NEW_DESCRIPTION = ('Contribute a fixed amount every day or week for a cycle and collect what you saved, less a service '
                   'charge of one contribution a month.')
# Old clause: its replacement (None removes it)
CLAUSES = {
    'A service charge of one contribution is taken from your savings at the end of each cycle.':
        'A service charge of one contribution is taken from your savings for each month you save in.',
    'Your savings are paid out in one sum when the cycle ends, after a payout request is approved.':
        'You can request a withdrawal of part or all of your savings at any time; it is paid once approved.',
    'Savings cannot be withdrawn before the cycle ends.':
        'Each cycle runs on calendar months, from the 1st of the month you join in.',
}


def forwards(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    for product in Product.objects.filter(type='ajo'):
        clauses = [CLAUSES.get(c, c) for c in product.clauses or []]
        description = NEW_DESCRIPTION if product.description == OLD_DESCRIPTION else product.description
        if clauses != (product.clauses or []) or description != product.description:
            product.clauses, product.description = clauses, description
            product.save(update_fields=['clauses', 'description'])


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0011_ajo_product'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
