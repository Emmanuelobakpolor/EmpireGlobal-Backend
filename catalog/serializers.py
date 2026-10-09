import re

from rest_framework import serializers

from .models import Product

HIRE_PURCHASE_CATEGORIES = ['Phones & gadgets', 'Laptops & computers', 'Home appliances', 'Other electronics',
                            'Tricycle', 'Bike']


def string_list(value, field, max_items=30, max_length=500):
    if not isinstance(value, list) or len(value) > max_items:
        raise serializers.ValidationError(f'{field} must be a list of at most {max_items} items.')
    items = []
    for item in value:
        if not isinstance(item, str) or len(item) > max_length:
            raise serializers.ValidationError(f'Each {field.lower()} entry must be text under {max_length} characters.')
        if item.strip():
            items.append(item.strip())
    return items


class ProductSerializer(serializers.ModelSerializer):
    """A product in the shape the React app uses (camelCase, amounts as numbers)."""

    id = serializers.CharField(source='code', read_only=True)
    minAmount = serializers.DecimalField(source='min_amount', max_digits=14, decimal_places=2, min_value=1)
    maxAmount = serializers.DecimalField(source='max_amount', max_digits=14, decimal_places=2, min_value=1)
    termOptions = serializers.JSONField(source='term_options', required=False, allow_null=True)
    expectedReturn = serializers.CharField(source='expected_return', required=False, allow_blank=True, allow_null=True,
                                           max_length=150)
    requiredDocuments = serializers.JSONField(source='required_documents', required=False)
    itemCategories = serializers.JSONField(source='item_categories', required=False)
    benefits = serializers.JSONField(required=False)
    clauses = serializers.JSONField(required=False)
    requirements = serializers.JSONField(required=False)

    class Meta:
        model = Product
        fields = ['id', 'name', 'category', 'type', 'description', 'minAmount', 'maxAmount', 'duration', 'frequency',
                  'termOptions', 'expectedReturn', 'benefits', 'clauses', 'requirements', 'requiredDocuments',
                  'itemCategories', 'status']

    def to_representation(self, product):
        data = super().to_representation(product)
        data['minAmount'] = float(product.min_amount)
        data['maxAmount'] = float(product.max_amount)
        data['expectedReturn'] = product.expected_return or None
        return data

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Product name is required.')
        return value

    def validate_description(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Description is required.')
        return value

    def validate_expectedReturn(self, value):
        return (value or '').strip()

    def validate_termOptions(self, value):
        if value in (None, []):
            return None
        if not isinstance(value, list) or not all(isinstance(m, int) and 1 <= m <= 120 for m in value):
            raise serializers.ValidationError('Plan lengths must be whole numbers of months between 1 and 120.')
        return sorted(set(value))

    def validate_benefits(self, value):
        return string_list(value, 'Benefits')

    def validate_clauses(self, value):
        return string_list(value, 'Clauses')

    def validate_requirements(self, value):
        return string_list(value, 'Requirements')

    def validate_requiredDocuments(self, value):
        if not isinstance(value, list) or len(value) > 10:
            raise serializers.ValidationError('Required documents must be a list of at most 10.')
        docs = []
        for doc in value:
            if (not isinstance(doc, dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{1,39}', str(doc.get('key', '')))
                    or not isinstance(doc.get('label'), str) or not doc['label'].strip()):
                raise serializers.ValidationError('Each required document needs a key and a label.')
            docs.append({'key': doc['key'], 'label': doc['label'].strip()[:150],
                         **({'hint': str(doc['hint'])[:200]} if doc.get('hint') else {})})
        return docs

    def validate_itemCategories(self, value):
        if not isinstance(value, list) or any(c not in HIRE_PURCHASE_CATEGORIES for c in value):
            raise serializers.ValidationError(f'Item categories must be from: {", ".join(HIRE_PURCHASE_CATEGORIES)}.')
        return value

    def validate(self, attrs):
        low = attrs.get('min_amount', getattr(self.instance, 'min_amount', None))
        high = attrs.get('max_amount', getattr(self.instance, 'max_amount', None))
        if low is not None and high is not None and high <= low:
            raise serializers.ValidationError({'maxAmount': ['Maximum must be greater than minimum.']})
        product_type = attrs.get('type', getattr(self.instance, 'type', None))
        categories = attrs.get('item_categories', getattr(self.instance, 'item_categories', []))
        if product_type == 'hire-purchase' and not categories:
            raise serializers.ValidationError({'itemCategories': ['Choose at least one eligible item category.']})
        return attrs
