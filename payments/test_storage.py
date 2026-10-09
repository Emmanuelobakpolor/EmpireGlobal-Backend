from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from accounts.models import User
from core.storage import CloudinaryStorage

from .tests import PDF, PNG, PaymentTestBase


class AvatarTests(PaymentTestBase):
    def upload(self, client, content=PNG, name='me.png'):
        return client.post('/api/auth/me/avatar/', {'avatar': SimpleUploadedFile(name, content)}, format='multipart',
                           HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)

    def test_customer_uploads_replaces_and_removes_their_picture(self):
        client = self.client_for(self.customer)
        self.assertIsNone(client.get('/api/auth/me/').json()['user']['avatarUrl'])

        res = self.upload(client)
        self.assertEqual(res.status_code, 200, res.content)
        url = res.json()['user']['avatarUrl']
        self.assertTrue(url.startswith(f'/api/users/{self.customer.public_id}/avatar/?v='))
        picture = client.get(url)
        self.assertEqual(picture.status_code, 200)
        self.assertEqual(b''.join(picture.streaming_content), PNG)
        picture.close()

        first = User.objects.get(pk=self.customer.pk).avatar
        self.upload(client, name='new.png')
        self.assertFalse(first.storage.exists(first.name), 'the replaced picture is deleted')

        res = client.delete('/api/auth/me/avatar/', HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertIsNone(res.json()['user']['avatarUrl'])
        self.assertFalse(User.objects.get(pk=self.customer.pk).avatar)

    def test_only_images_are_accepted(self):
        client = self.client_for(self.customer)
        self.assertEqual(self.upload(client, PDF, 'me.pdf').status_code, 400)
        self.assertEqual(self.upload(client, b'not an image at all', 'me.png').status_code, 400)
        self.assertEqual(self.upload(client, PNG, 'me.gif').status_code, 400)

    def test_upload_requires_csrf(self):
        client = self.client_for(self.customer)
        res = client.post('/api/auth/me/avatar/', {'avatar': SimpleUploadedFile('me.png', PNG)}, format='multipart')
        self.assertEqual(res.status_code, 403)

    def test_admins_see_customer_pictures_other_customers_do_not(self):
        url = self.upload(self.client_for(self.customer)).json()['user']['avatarUrl']
        picture = self.client_for(self.admin).get(url)
        self.assertEqual(picture.status_code, 200)
        picture.close()
        self.assertEqual(self.client_for(self.other).get(url).status_code, 404)
        self.assertIn(self.client_for(None).get(url).status_code, (401, 403))

        listed = self.client_for(self.admin).get(f'/api/admin/customers/{self.customer.public_id}/').json()
        self.assertEqual(listed['customer']['avatarUrl'], url)

    def test_admin_can_set_their_own_picture(self):
        client = self.client_for(self.admin)
        self.assertEqual(self.upload(client).status_code, 200)
        self.assertIsNotNone(client.get('/api/auth/me/').json()['user']['avatarUrl'])


@override_settings(CLOUDINARY_URL='cloudinary://key:secret@demo-cloud', CLOUDINARY_FOLDER='eg')
class CloudinaryStorageTests(SimpleTestCase):
    def setUp(self):
        self.storage = CloudinaryStorage()

    def test_images_and_pdfs_map_to_private_assets(self):
        self.assertEqual(self.storage._locate('avatars/abc.PNG'), ('image', 'eg/avatars/abc', 'png'))
        self.assertEqual(self.storage._locate('receipts/2026/10/abc.pdf'), ('raw', 'eg/receipts/2026/10/abc.pdf', None))

    @mock.patch('cloudinary.uploader.upload')
    def test_save_uploads_as_authenticated(self, upload):
        name = self.storage.save('receipts/2026/10/abc.jpg', ContentFile(b'\xff\xd8\xff data'))
        self.assertEqual(name, 'receipts/2026/10/abc.jpg')
        kwargs = upload.call_args.kwargs
        self.assertEqual(kwargs['public_id'], 'eg/receipts/2026/10/abc')
        self.assertEqual(kwargs['resource_type'], 'image')
        self.assertEqual(kwargs['type'], 'authenticated')

    @mock.patch('urllib.request.urlopen')
    def test_open_downloads_through_a_signed_expiring_link(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b'%PDF-1.4 bytes'
        file = self.storage.open('applications/2026/10/abc.pdf')
        self.assertEqual(file.read(), b'%PDF-1.4 bytes')
        url = urlopen.call_args.args[0]
        self.assertTrue(url.startswith('https://api.cloudinary.com/v1_1/demo-cloud/raw/download?'))
        for part in ('type=authenticated', 'expires_at=', 'signature=', 'public_id=eg%2Fapplications'):
            self.assertIn(part, url)
        self.assertNotIn('secret', url)

    @mock.patch('cloudinary.uploader.destroy', side_effect=RuntimeError('network'))
    def test_delete_never_raises(self, destroy):
        self.storage.delete('avatars/abc.png')
        destroy.assert_called_once_with('eg/avatars/abc', resource_type='image', type='authenticated', invalidate=True)

    def test_avatar_url_is_signed_and_face_cropped(self):
        url = self.storage.avatar_url('avatars/abc.png')
        self.assertTrue(url.startswith('https://res.cloudinary.com/demo-cloud/image/authenticated/s--'))
        self.assertIn('c_fill,g_face,h_256,w_256', url)
        self.assertTrue(url.endswith('/eg/avatars/abc.png'))
