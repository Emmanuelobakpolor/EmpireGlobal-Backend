import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Product

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'initial_products.json'


class Command(BaseCommand):
    help = 'Load the official Empire Global product catalogue. Products that already exist (by id) are left as they are.'

    def handle(self, *args, **options):
        created = skipped = 0
        with transaction.atomic():
            for position, p in enumerate(json.loads(FIXTURE.read_text('utf-8')), start=1):
                if Product.objects.filter(code=p['id']).exists():
                    skipped += 1
                    continue
                Product.objects.create(
                    code=p['id'], name=p['name'], category=p.get('category', ''), type=p['type'],
                    description=p['description'], min_amount=p['minAmount'], max_amount=p['maxAmount'],
                    duration=p.get('duration') or '', frequency=p.get('frequency') or '',
                    term_options=p.get('termOptions'), expected_return=p.get('expectedReturn') or '',
                    benefits=p.get('benefits') or [], clauses=p.get('clauses') or [],
                    requirements=p.get('requirements') or [], required_documents=p.get('requiredDocuments') or [],
                    item_categories=p.get('itemCategories') or [], withdrawal_rules=p.get('withdrawalRules') or {}, status=p.get('status', 'active'), position=position,
                )
                created += 1
                self.stdout.write(f'added    {p["id"]:<4} {p["name"]}')
        self.stdout.write(self.style.SUCCESS(f'{created} product(s) added, {skipped} already there.'))
