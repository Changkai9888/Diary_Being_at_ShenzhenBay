import time
import unittest
from unittest.mock import patch

import app as web


class SessionTests(unittest.TestCase):
    def setUp(self):
        web.app.config['TESTING'] = True
        self.client = web.app.test_client()
        for target in ('_setup_current_user_paths',):
            mock = patch.object(web, target)
            mock.start()
            self.addCleanup(mock.stop)
        validate = patch.object(web.auth, 'validate_user', return_value='success')
        validate.start()
        self.addCleanup(validate.stop)

    def login(self, json_response=True):
        return self.client.post('/login', data={'username': 'test', 'password': 'test'},
                                headers={'Accept': 'application/json'} if json_response else {})

    def test_service_owns_root_without_diary_prefix(self):
        self.assertEqual(web.DEFAULT_PORT, 8081)
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/login').status_code, 200)
        self.assertEqual(self.client.get('/backup_status').status_code, 401)
        for path in ('/diary/', '/diary/login'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('Location', response.headers)

    def test_bookmark_login_and_return_to_diary(self):
        redirect = self.client.get('/', follow_redirects=True)
        self.assertEqual(redirect.request.path, '/login')
        self.assertIn('<title>登录日记本 · 深圳湾日记</title>', redirect.get_data(as_text=True))
        logged_in = self.login()
        self.assertEqual(logged_in.json['redirect'], '/')
        self.assertIn('Path=/', logged_in.headers['Set-Cookie'])
        response = self.client.get(logged_in.json['redirect'])
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('<title>日记本 · 深圳湾日记</title>', page)
        self.assertIn('href="/cognitive/"', page)
        self.assertIn('/static/js/session_guard.js', page)
        self.assertIn('data-diary-base="/"', page)
        self.assertIn('data-status-url="/backup_status"', page)
        self.assertIn('id="diaryWorkspace" inert', page)

    def test_normal_html_form_login_redirects(self):
        response = self.login(json_response=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/')

    def test_heartbeat_does_not_extend_expiry_or_refresh_cookie(self):
        before = time.time()
        self.login()
        with self.client.session_transaction(path='/') as session:
            expiry = session['expires_at']
            self.assertAlmostEqual(expiry - before, 86400, delta=3)
        for _ in range(3):
            response = self.client.get('/session_status')
            self.assertEqual(response.json['expires_at'], expiry)
            self.assertNotIn('Set-Cookie', response.headers)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_expired_session_rejects_write_before_touching_diary(self):
        self.login()
        with self.client.session_transaction(path='/') as session:
            session['expires_at'] = time.time() - 1
        with patch.object(web.LogManager, 'save_log') as save:
            response = self.client.post('/save_log', json={'date': '2026-09-29'})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json['code'], 'session_expired')
            self.assertTrue(response.json['redirect'].startswith('/login'))
            save.assert_not_called()
        self.assertEqual(self.client.get('/session_status').status_code, 401)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_session_without_deadline_requires_fresh_login(self):
        with self.client.session_transaction(path='/') as session:
            session['authenticated'] = True
            session['username'] = 'legacy'
        self.assertEqual(self.client.get('/session_status').status_code, 401)

    def test_logout_clears_session_and_assets_remain_public(self):
        self.login()
        response = self.client.post('/logout')
        self.assertEqual(response.headers['Location'], '/login')
        self.assertEqual(self.client.get('/session_status').status_code, 401)
        with self.client.get('/static/js/session_guard.js') as asset:
            self.assertEqual(asset.status_code, 200)
        registration = self.client.get('/register').get_data(as_text=True)
        self.assertIn('href="/login"', registration)


if __name__ == '__main__':
    unittest.main()
