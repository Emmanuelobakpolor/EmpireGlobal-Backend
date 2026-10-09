from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Role, Status, User

from .models import Agent, PlatformSettings

PASSWORD = 'Str0ng-Passw0rd!'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], ADMIN_TWO_FACTOR=False)
class CoreTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.get('/api/auth/csrf/')
        for email, role in [('boss@empireglobal.com', Role.SUPER_ADMIN), ('staff@empireglobal.com', Role.ADMIN),
                            ('customer@example.com', Role.CUSTOMER)]:
            User.objects.create_user(email, PASSWORD, full_name='Test', role=role, status=Status.ACTIVE)

    def csrf(self):
        return self.client.cookies['csrftoken'].value

    def post(self, url, data=None):
        return self.client.post(url, data or {}, format='json', HTTP_X_CSRFTOKEN=self.csrf())

    def patch(self, url, data):
        return self.client.patch(url, data, format='json', HTTP_X_CSRFTOKEN=self.csrf())

    def login(self, email):
        url = '/api/auth/login/' if email == 'customer@example.com' else '/api/admin/auth/login/'
        self.assertEqual(self.post(url, {'email': email, 'password': PASSWORD}).status_code, 200)


class AgentTests(CoreTestBase):
    def test_codes_are_sequential(self):
        self.assertEqual(Agent.objects.create(name='A', phone='1', location='X').code, 'AG-1001')
        Agent.objects.create(code='ag-1007', name='B', phone='1', location='X')
        self.assertEqual(Agent.objects.create(name='C', phone='1', location='X').code, 'AG-1008')

    def test_any_admin_manages_agents(self):
        self.login('staff@empireglobal.com')
        res = self.post('/api/admin/agents/', {'name': ' Kunle ', 'phone': '0803', 'location': 'Ikorodu', 'status': 'inactive', 'code': 'AG-1'})
        self.assertEqual(res.status_code, 201)
        agent = res.json()['agent']
        self.assertEqual((agent['code'], agent['name'], agent['status']), ('AG-1001', 'Kunle', 'active'))

        res = self.patch('/api/admin/agents/ag-1001/', {'location': 'Ikeja', 'status': 'inactive', 'code': 'AG-5'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.json()['agent']['code'], res.json()['agent']['location'], res.json()['agent']['status']),
                         ('AG-1001', 'Ikeja', 'inactive'))
        self.assertEqual(len(self.client.get('/api/admin/agents/').json()['agents']), 1)

    def test_validation(self):
        self.login('staff@empireglobal.com')
        res = self.post('/api/admin/agents/', {'name': ' ', 'phone': '', 'location': 'X'})
        self.assertEqual(set(res.json()), {'name', 'phone'})
        self.assertEqual(self.patch('/api/admin/agents/AG-1001/', {'name': 'X'}).status_code, 404)

    def test_customers_and_anonymous_cannot_manage_agents(self):
        self.assertEqual(self.client.get('/api/admin/agents/').status_code, 403)
        self.login('customer@example.com')
        self.assertEqual(self.client.get('/api/admin/agents/').status_code, 403)
        self.assertEqual(self.post('/api/admin/agents/', {'name': 'A', 'phone': '1', 'location': 'X'}).status_code, 403)


class PlatformSettingsTests(CoreTestBase):
    def test_public_settings_show_support_contacts_only(self):
        res = self.client.get('/api/settings/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.json()['settings']), {'platformName', 'supportEmail', 'supportPhone'})

    def test_admins_read_but_only_super_admins_write(self):
        self.login('staff@empireglobal.com')
        self.assertEqual(self.client.get('/api/admin/settings/').json()['settings']['timezone'], 'Africa/Lagos')
        self.assertEqual(self.patch('/api/admin/settings/', {'platformName': 'X'}).status_code, 403)

        self.post('/api/auth/logout/')
        self.login('boss@empireglobal.com')
        res = self.patch('/api/admin/settings/', {'supportEmail': 'help@empireglobal.com', 'timezone': 'UTC'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(PlatformSettings.load().support_email, 'help@empireglobal.com')
        self.assertEqual(self.client.get('/api/settings/').json()['settings']['supportEmail'], 'help@empireglobal.com')

    def test_settings_validation(self):
        self.login('boss@empireglobal.com')
        res = self.patch('/api/admin/settings/', {'supportEmail': 'nope', 'timezone': 'Mars/Base', 'platformName': ' '})
        self.assertEqual(set(res.json()), {'supportEmail', 'timezone', 'platformName'})

    def test_customers_cannot_read_admin_settings(self):
        self.login('customer@example.com')
        self.assertEqual(self.client.get('/api/admin/settings/').status_code, 403)

    def test_single_row(self):
        PlatformSettings.load()
        PlatformSettings(platform_name='Other').save()
        self.assertEqual(PlatformSettings.objects.count(), 1)
