"""临时数据验证账号和用户目录映射的并发隔离，不读取正式数据。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Response, session

import app as web
import auth.auth_manager as auth_module


def concurrent_calls(functions):
    gate = threading.Barrier(len(functions))

    def call(function):
        gate.wait(timeout=10)
        return function()

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(call, functions))


class UserMapIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.user_root = self.root / 'user-data'
        self.map_path = self.user_root / 'user_map.json'
        for target in (patch.object(web, 'USER_DATA_ROOT', str(self.user_root)),
                       patch.object(web, 'USER_MAP_PATH', str(self.map_path)),
                       patch.object(web, 'GLOBAL_MUSIC_DIR', str(self.root / 'music-data')),
                       patch.dict(web.app.config, TESTING=True)):
            target.start()
            self.addCleanup(target.stop)

    def write_map(self, text):
        self.user_root.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(text, encoding='utf-8')

    def test_concurrent_registration_and_first_login_allocate_unique_stable_ids(self):
        original_load = web._load_user_map

        def slow_load():
            result = original_load()
            time.sleep(0.005)  # Expose a read/allocate/write race if the outer lock is removed.
            return result

        def register(index):
            return web._register_user_profile(f'account-{index}')

        def first_login(index):
            with web.app.test_request_context('/'):
                session['username'] = f'account-{index}'
                return web._setup_current_user_paths()

        functions = [(lambda i=i: register(i) if i % 2 else first_login(i)) for i in range(8)]
        with patch.object(web, '_load_user_map', side_effect=slow_load):
            allocated = concurrent_calls(functions)
        self.assertEqual(len(set(allocated)), 8)
        user_map = web._load_user_map()
        self.assertEqual(set(user_map.values()), {f'account-{i}' for i in range(8)})
        for index, user_id in enumerate(allocated):
            self.assertEqual(user_map[user_id], f'account-{index}')
            self.assertTrue((self.user_root / user_id / 'diary-data').is_dir())
            self.assertEqual(web._register_user_profile(f'account-{index}'), user_id)

    def test_invalid_mapping_fails_closed_without_creating_or_reassigning_data(self):
        invalid = ['{', '[]', '{"../user0":"alice"}', '{"user0":""}',
                   '{"user0":"alice","user1":"alice"}',
                   '{"user0":"alice","user0":"bob"}',
                   '{"user0":123}', '{"user0":" alice"}',
                   '{"user00":"alice"}', '{"user1000000000":"alice"}']
        for content in invalid:
            with self.subTest(content=content):
                self.write_map(content)
                with self.assertRaises(RuntimeError):
                    web._register_user_profile('new-account')
                with web.app.test_request_context('/'):
                    session['username'] = 'new-account'
                    with self.assertRaises(RuntimeError):
                        web._setup_current_user_paths()
                self.assertEqual(self.map_path.read_text(encoding='utf-8'), content)
                self.assertEqual([p.name for p in self.user_root.iterdir()], ['user_map.json'])

    def test_missing_or_empty_mapping_cannot_reassign_existing_user_directory(self):
        existing = self.user_root / 'user0'
        existing.mkdir(parents=True)
        private_record = existing / 'record.txt'
        private_record.write_text('original-user', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            web._register_user_profile('another-account')
        self.assertFalse(self.map_path.exists())
        self.write_map('{}')
        with self.assertRaises(RuntimeError):
            web._register_user_profile('another-account')
        self.assertEqual(self.map_path.read_text(encoding='utf-8'), '{}')
        self.assertEqual(private_record.read_text(encoding='utf-8'), 'original-user')

    def test_true_empty_installation_allocates_user0(self):
        self.assertEqual(web._register_user_profile('first'), 'user0')
        self.assertEqual(web._load_user_map(), {'user0': 'first'})

    def test_exhausted_user_id_range_does_not_write_an_unsupported_mapping(self):
        original = '{"user999999999":"last-account"}'
        self.write_map(original)
        with self.assertRaises(RuntimeError):
            web._register_user_profile('another-account')
        self.assertEqual(self.map_path.read_text(encoding='utf-8'), original)

    def test_gateway_reuses_identity_resolved_during_authentication(self):
        client = web.app.test_client()
        with client.session_transaction() as login:
            login.update(authenticated=True, username='alice', expires_at=time.time() + 3600)
        with patch.object(web, '_setup_current_user_paths', return_value='user7') as setup, \
                patch.object(web, '_resolve_user_id_by_username') as resolve, \
                patch.object(web.workspace_gateway, 'forward', return_value=Response('ok')) as forward:
            self.assertEqual(client.get('/cognitive/api/state').status_code, 200)
        setup.assert_called_once_with()
        resolve.assert_not_called()
        forward.assert_called_once_with('api/state', 'user7')


class AccountStorageIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in (patch.object(auth_module, 'DATA_ROOT', self.root),
                       patch.object(auth_module, 'generate_password_hash', side_effect=lambda p: 'hash:' + p),
                       patch.object(auth_module, 'check_password_hash', side_effect=lambda h, p: h == 'hash:' + p)):
            target.start()
            self.addCleanup(target.stop)
        self.manager = auth_module.AuthManager()

    def test_concurrent_registration_preserves_every_account(self):
        original_load = self.manager._load_users

        def slow_load():
            users = original_load()
            time.sleep(0.005)
            return users

        with patch.object(self.manager, '_load_users', side_effect=slow_load):
            results = concurrent_calls([
                lambda i=i: self.manager.register_user(f'account-{i}', 'password', 'password')
                for i in range(8)])
        self.assertEqual(results, ['success'] * 8)
        self.assertEqual(set(self.manager._load_users()), {f'account-{i}' for i in range(8)})
        self.assertEqual(self.manager.register_user('account-0', 'different', 'different'), '用户名已存在')

    def test_simultaneous_password_updates_and_registration_preserve_changes(self):
        for index in range(4):
            self.assertEqual(self.manager.register_user(f'old-{index}', 'old', 'old'), 'success')
        functions = [lambda i=i: self.manager.change_password(f'old-{i}', 'old', 'new', 'new')
                     for i in range(4)]
        functions += [lambda i=i: self.manager.register_user(f'new-{i}', 'password', 'password')
                      for i in range(4)]
        self.assertEqual(concurrent_calls(functions), ['success'] * 8)
        accounts = self.manager._load_users()
        self.assertEqual(len(accounts), 8)
        self.assertTrue(all(accounts[f'old-{i}']['password_hash'] == 'hash:new' for i in range(4)))

    def test_concurrent_failed_logins_preserve_lockout_counter(self):
        self.manager.register_user('alice', 'correct', 'correct')
        results = concurrent_calls([lambda: self.manager.validate_user('alice', 'wrong') for _ in range(8)])
        self.assertEqual(results.count('密码错误'), 3)
        self.assertEqual(results.count('账户已锁定'), 5)
        self.assertEqual(self.manager._load_users()['alice']['failed_attempts'], 3)

    def test_interrupted_atomic_account_replace_keeps_original_and_cleans_temporary(self):
        self.manager.register_user('alice', 'password', 'password')
        path = Path(self.manager.file_path)
        original = path.read_bytes()
        with patch('utils.safe_files.os.replace', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                self.manager.register_user('bob', 'password', 'password')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('.atomic-*')), [])
        self.assertEqual(self.manager.validate_user('alice', 'password'), 'success')

    def test_invalid_account_storage_is_not_replaced_with_empty_accounts(self):
        path = Path(self.manager.file_path)
        for content in ('[]', '{'):
            with self.subTest(content=content):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises((RuntimeError, ValueError)):
                    self.manager.register_user('alice', 'password', 'password')
                self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_missing_account_file_cannot_recreate_accounts_over_existing_profile(self):
        path = Path(self.manager.file_path)
        path.unlink()
        user_root = self.root / 'user-data'
        (user_root / 'user0').mkdir(parents=True)
        (user_root / 'user_map.json').write_text('{"user0":"alice"}', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.manager.register_user('alice', 'different', 'different')
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
