from django.db import migrations, models


class Migration(migrations.Migration):
    """Notifications go to admins as well as customers: `customer` becomes `recipient`,
    and each one gets a link to open and a kind (for its icon)."""

    dependencies = [
        ('payments', '0003_initial_bank_accounts'),
    ]

    operations = [
        migrations.RenameField(model_name='notification', old_name='customer', new_name='recipient'),
        migrations.AddField(
            model_name='notification',
            name='kind',
            field=models.CharField(
                choices=[('payment', 'Payment'), ('receipt', 'Receipt'), ('application', 'Application'),
                         ('customer', 'Customer'), ('account', 'Account')],
                default='account', max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='link',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterModelOptions(name='notification', options={'ordering': ['-created_at', '-pk']}),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['recipient', 'read'], name='payments_no_recipie_0cfa89_idx'),
        ),
    ]
