import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import app as web
from utils.service_identity import load_identity, load_service_group


class IdentityTests(unittest.TestCase):
    def setUp(self):
        testing = patch.dict(web.app.config, TESTING=True)
        testing.start()
        self.addCleanup(testing.stop)
        self.client = web.app.test_client()

    def test_identity_is_public_stable_and_does_not_start_business_work(self):
        with patch.object(web.backup_service, 'start') as backup, \
                patch.object(web, '_setup_current_user_paths') as setup:
            response = self.client.get('/__service__/identity')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['service_id'], 'urn:uuid:c6e65645-aa71-479b-8f7e-fdd7e9efaeea')
        self.assertEqual(response.json['service_name'], '深圳湾日记')
        self.assertEqual(response.json['service_role'], 'service-group')
        self.assertEqual(response.json['gateway_service_id'], 'urn:uuid:e56ef2d9-4867-48f7-8ab4-662de0a790c3')
        self.assertIn('service-hierarchy-v1', response.json['capabilities'])
        self.assertEqual(response.json['identity_protocol'], 1)
        self.assertTrue(response.json['ready'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Set-Cookie', response.headers)
        backup.assert_not_called()
        setup.assert_not_called()

    def test_business_version_changes_without_changing_identity(self):
        first = self.client.get('/__service__/identity').json
        with patch.object(web, 'APP_VERSION', 't100'):
            second = self.client.get('/__service__/identity').json
        self.assertEqual(first['service_id'], second['service_id'])
        self.assertEqual(first['identity_protocol'], second['identity_protocol'])
        self.assertEqual(second['app_version'], 't100')

    def test_identity_is_loaded_from_project_file_without_generation_or_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'service.json'
            with self.assertRaises(RuntimeError):
                load_identity(path)
            self.assertFalse(path.exists())
            # 身份由文件决定，版本代码不另藏一个硬编码 ID。
            different = 'urn:uuid:cadb4afb-49d2-4f82-b9df-445c46120d8b'
            path.write_text(json.dumps({'service_id': different, 'identity_protocol': 1}), encoding='utf-8')
            self.assertEqual(load_identity(path), (different, 1))
            for value in ('not-json', '{}', '[]', json.dumps({'service_id': different, 'identity_protocol': 2})):
                path.write_text(value, encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    load_identity(path)

    def test_not_ready_is_distinct_from_identity_mismatch(self):
        with patch.dict(web.app.config, SERVICE_READY=False):
            response = self.client.get('/__service__/identity')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['service_id'], web.SERVICE_ID)
        self.assertFalse(response.json['ready'])

    def test_group_metadata_cannot_silently_fall_back_to_a_single_diary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'service.json'
            for changes in ({'service_role': 'diary'}, {'service_name': ''},
                            {'gateway_service_id': web.SERVICE_ID},
                            {'gateway_service_id': 'invalid'}, {'gateway_name': None}):
                record = dict(web.SERVICE_INFO, **changes)
                path.write_text(json.dumps(record), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    load_service_group(path)

    def test_standalone_and_local_proxy_paths(self):
        page = self.client.get('/register')
        self.assertIn('action="/register"', page.text)
        page = self.client.get('/register', headers={'X-Forwarded-Prefix': '/diary'},
                               environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        self.assertIn('action="/diary/register"', page.text)

    def test_remote_or_invalid_prefix_is_ignored(self):
        page = self.client.get('/register', headers={'X-Forwarded-Prefix': '/evil',
                                                     'X-Forwarded-For': '127.0.0.1'},
                               environ_overrides={'REMOTE_ADDR': '203.0.113.1'})
        self.assertIn('action="/register"', page.text)
        for prefix in ('//evil.example', '/../escape', '/diary, /evil'):
            page = self.client.get('/register', headers={'X-Forwarded-Prefix': prefix},
                                   environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertIn('action="/register"', page.text)


if __name__ == '__main__':
    unittest.main()
