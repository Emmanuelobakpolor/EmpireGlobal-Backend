from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Role, Status, User
from core.models import AuditLog
from payments.models import Transaction

from .models import Product

PASSWORD = 'Str0ng-Passw0rd!'
NEW_PRODUCT = {
    'name': 'Festive Savings', 'type': 'savings', 'description': 'Save towards the holidays.',
    'minAmount': 1000, 'maxAmount': 500000, 'frequency': 'Weekly', 'termOptions': [6, 3, 6],
    'benefits': ['Bonus at maturity', ' '], 'clauses': [], 'requirements': [],
}


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], ADMIN_TWO_FACTOR=False)
class ProductTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user('staff@empireglobal.com', PASSWORD, full_name='Staff', role=Role.ADMIN,
                                              status=Status.ACTIVE)
        self.customer = User.objects.create_user('c@example.com', PASSWORD, full_name='C', status=Status.ACTIVE)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.get('/api/auth/csrf/')

    def csrf(self):
        return self.client.cookies['csrftoken'].value

    def login(self, user):
        url = '/api/admin/auth/login/' if user.is_admin else '/api/auth/login/'
        self.client.post(url, {'email': user.email, 'password': PASSWORD}, format='json', HTTP_X_CSRFTOKEN=self.csrf())

    def test_catalogue_is_loaded_and_public(self):
        Product.objects.filter(code='p12').update(status='disabled')
        products = APIClient().get('/api/products/').json()['products']
        self.assertEqual(len(products), 15)
        self.assertNotIn('p12', [p['id'] for p in products])
        first = products[0]
        self.assertEqual((first['id'], first['minAmount']), ('p3', 100000.0))
        self.assertIsInstance(first['benefits'], list)

    def test_admin_creates_and_edits_products(self):
        self.login(self.admin)
        res = self.client.post('/api/admin/products/', NEW_PRODUCT, format='json', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(res.status_code, 201, res.content)
        product = res.json()['product']
        self.assertEqual(product['id'], 'p17')
        self.assertEqual((product['termOptions'], product['benefits']), ([3, 6], ['Bonus at maturity']))

        res = self.client.patch('/api/admin/products/p17/', {'status': 'disabled'}, format='json', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(res.json()['product']['status'], 'disabled')
        self.assertEqual(len(self.client.get('/api/admin/products/').json()['products']), 17)
        self.assertTrue(AuditLog.objects.filter(action='Product Disabled', reference='Festive Savings').exists())

    def test_validation(self):
        self.login(self.admin)
        bad = {**NEW_PRODUCT, 'maxAmount': 500, 'termOptions': [0], 'type': 'hire-purchase', 'itemCategories': []}
        errors = self.client.post('/api/admin/products/', bad, format='json', HTTP_X_CSRFTOKEN=self.csrf()).json()
        self.assertIn('termOptions', errors)
        errors = self.client.post('/api/admin/products/', {**bad, 'termOptions': None}, format='json',
                                  HTTP_X_CSRFTOKEN=self.csrf()).json()
        self.assertIn('maxAmount', errors)
        res = self.client.post('/api/admin/products/', {**NEW_PRODUCT, 'type': 'hire-purchase', 'itemCategories': ['Yacht']},
                               format='json', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertIn('itemCategories', res.json())

    def test_products_in_use_cannot_be_deleted(self):
        Transaction.objects.create(customer=self.customer, product_id='p2', product_name='MSP', product_type='savings',
                                   amount=5000, start_date='2026-01-01')
        self.login(self.admin)
        res = self.client.delete('/api/admin/products/p2/', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(res.json()['code'], 'product_in_use')
        self.assertEqual(self.client.delete('/api/admin/products/p12/', HTTP_X_CSRFTOKEN=self.csrf()).status_code, 204)

    def test_customers_cannot_manage_products(self):
        self.login(self.customer)
        self.assertEqual(self.client.get('/api/admin/products/').status_code, 403)
        res = self.client.post('/api/admin/products/', NEW_PRODUCT, format='json', HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(res.status_code, 403)
