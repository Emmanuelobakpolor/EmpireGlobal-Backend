"""Interest on investments and savings is now tracked and paid:

- Investments are 12-month contracts whose interest is paid upfront (30%), monthly (36% a year) or at
  maturity (42%), as the customer chooses.
- Savings plans earn 15% a year, paid at maturity; a partial withdrawal before maturity (from month
  8) drops that to 12% a year on the capital left.

The products' wording is updated to match (the old "18%" and "10%" figures go)."""

from django.db import migrations

PARTIAL_CLAUSE = ('Partial withdrawal can only be made after the 8th month of the contract; interest is then 12% a '
                  'year, calculated on your current invested capital only.')
INVESTMENT_RETURN = '30% upfront, 36% monthly or 42% at maturity'
INVESTMENT_BENEFIT = ('Interest your way: 30% upfront, 36% a year paid monthly (3% a month), or 42% at maturity '
                      'with your capital')
SAVINGS_RETURN = '15% a year'
SAVINGS_BENEFIT = '15% interest a year, paid at maturity'


def forwards(apps, schema_editor):
    Product = apps.get_model('catalog', 'Product')
    for product in Product.objects.filter(type='investment'):
        product.term_options = [12]
        product.duration = '12 months'
        product.expected_return = INVESTMENT_RETURN
        product.benefits = [INVESTMENT_BENEFIT if '18%' in b else b for b in product.benefits or []]
        product.save(update_fields=['term_options', 'duration', 'expected_return', 'benefits'])
    for product in Product.objects.filter(type='savings'):
        product.expected_return = SAVINGS_RETURN
        product.benefits = [SAVINGS_BENEFIT if '10%' in b else b for b in product.benefits or []]
        product.clauses = [PARTIAL_CLAUSE if c.startswith('Partial withdrawal') else c for c in product.clauses or []]
        product.save(update_fields=['expected_return', 'benefits', 'clauses'])


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_product_interest_rates'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
