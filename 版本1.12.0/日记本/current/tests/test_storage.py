"""使用临时数据验证：备份 data、换版本、放回 data、身份保持不变。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class StorageTests(unittest.TestCase):
    def test_replacing_all_code_preserves_accounts_diary_settings_and_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            code = root / 'current'
            storage = code / 'data'
            unrelated = root / 'unrelated'
            unrelated.mkdir()
            shutil.copytree(SOURCE, code, ignore=shutil.ignore_patterns('__pycache__', 'tests', '*.pyc', 'data'))
            shutil.copytree(SOURCE.parent / 'identity', root / 'identity')
            identity_before = hashes(root / 'identity')
            (storage / 'AI-tools').mkdir(parents=True)
            (storage / 'AI-tools/API_KEY.json').write_text('{"QWEN_API_KEY":"test-not-a-real-key"}', encoding='utf-8')
            (storage / 'music-data').mkdir()
            (storage / 'music-data/test.mp3').write_bytes(b'test-audio')
            (storage / 'oss_backup_config.json').write_text('{"enabled":false}', encoding='utf-8')
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            env.pop('DIARY_DATA_ROOT', None)
            env.pop('DIARY_IDENTITY_DIR', None)
            env.pop('QWEN_API_KEY', None)
            script = '''
from pathlib import Path
import sys, json
sys.path.insert(0, sys.argv[1])
import app as web
from utils.paths import DATA_ROOT
web.app.config['TESTING'] = True
client = web.app.test_client()
phase = sys.argv[2]
if phase == 'write':
    result = client.post('/register', data={'username':'upgrade-test','password':'test-pass','confirm_password':'test-pass'})
    assert result.status_code == 302
result = client.post('/login', data={'username':'upgrade-test','password':'test-pass'}, headers={'Accept':'application/json'})
assert result.status_code == 200, result.data
if phase == 'write':
    assert client.post('/save_log', json={'date':'2026-09-30','心得备注':'survives-code-replacement',
        '在途任务':'persistent-current-tasks', '待提升属性':'persistent-attributes'}).json['status'] == 'success'
    assert client.post('/save_user_ui_settings', json={'ui_sound_enabled':False,'ui_fx_enabled':True}).status_code == 200
    # 历史提示词文件保留，但不再由新版界面或接口读写。
    (DATA_ROOT/'user-data/user0/ai_prompt_defaults.json').write_text('{"system_prompt_1":"legacy-prompt"}', encoding='utf-8')
    state = web.backup_service.state_path
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"last_success_at":"2026-09-29T04:00:00+08:00"}', encoding='utf-8')
assert client.get('/get_log?date=2026-09-30').json['心得备注'] == 'survives-code-replacement'
assert client.get('/get_log?date=2026-10-01').json['在途任务'] == 'persistent-current-tasks'
assert client.get('/get_log?date=2026-10-01').json['待提升属性'] == 'persistent-attributes'
assert client.get('/get_user_ui_settings').json['settings']['ui_sound_enabled'] is False
assert json.loads((DATA_ROOT/'user-data/user0/ai_prompt_defaults.json').read_text(encoding='utf-8'))['system_prompt_1'] == 'legacy-prompt'
assert json.loads((DATA_ROOT/'AI-tools/API_KEY.json').read_text(encoding='utf-8'))['QWEN_API_KEY'] == 'test-not-a-real-key'
for old_route in ('/get_ai_prompt_defaults','/analyze_ai','/save_prompt_part','/chat_ai'):
    assert client.post(old_route, json={}).status_code == 404
assert Path(web.auth.file_path).is_relative_to(DATA_ROOT)
assert Path(web.GLOBAL_MUSIC_DIR) == DATA_ROOT/'music-data'
assert (DATA_ROOT/'music-data/test.mp3').read_bytes() == b'test-audio'
assert web.backup_service.root == DATA_ROOT
assert web.backup_service._read_state()['last_success_at'] == '2026-09-29T04:00:00+08:00'
exported = client.post('/export_logs_excel').json
assert exported['status'] == 'success', exported
assert Path(exported['output_path']).is_relative_to(DATA_ROOT)
if phase == 'read':
    assert client.get('/__service__/identity').json['app_version'] == 't12-test'
'''
            def run(phase):
                result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(code), phase],
                                        cwd=unrelated, env=env, capture_output=True, text=True,
                                        encoding='utf-8', timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr)

            code_before = {p: h for p, h in hashes(code).items() if not p.startswith('data' + os.sep)}
            run('write')
            self.assertEqual({p: h for p, h in hashes(code).items() if not p.startswith('data' + os.sep)},
                             code_before, '业务文件只能写进 data')
            persisted = hashes(storage)
            shutil.copytree(storage, root / 'saved-data')
            # 只删除本测试临时目录里的 current，不接触真实部署目录。
            self.assertTrue(code.resolve().is_relative_to(root))
            self.assertEqual(code.name, 'current')
            shutil.rmtree(code)
            release = root / 't12'
            shutil.copytree(SOURCE, release, ignore=shutil.ignore_patterns('__pycache__', 'tests', '*.pyc', 'data'))
            release.rename(code)
            shutil.copytree(root / 'saved-data', code / 'data')
            version = code / 'utils/service_identity.py'
            version.write_text(version.read_text(encoding='utf-8').replace("APP_VERSION = 't11'", "APP_VERSION = 't12-test'"), encoding='utf-8')
            self.assertEqual(hashes(storage), persisted, '放回的正式数据必须与备份完全一致')
            run('read')
            self.assertEqual(hashes(root / 'identity'), identity_before)
            self.assertEqual(list(unrelated.iterdir()), [])

    def test_paths_follow_version_not_working_directory_and_identity_stays_outside(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            code = root / 'any-code-name'
            (code / 'utils').mkdir(parents=True)
            shutil.copy2(SOURCE / 'utils/paths.py', code / 'utils/paths.py')
            script = 'import sys; sys.path.insert(0,sys.argv[1]); from utils.paths import DATA_ROOT; print(DATA_ROOT)'
            for value, expected in (('data', code / 'data'), ('persistent', code / 'persistent'),
                                    (str(root / 'elsewhere'), root / 'elsewhere')):
                env = dict(os.environ, DIARY_DATA_ROOT=value)
                result = subprocess.run([sys.executable, '-c', script, str(code)], cwd=root,
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout.strip()), expected)
            result = subprocess.run([sys.executable, '-c', script, str(code)], cwd=root,
                                    env=dict(os.environ, DIARY_DATA_ROOT=str(code)),
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, '-c', script, str(code)], cwd=root,
                                    env=dict(os.environ, DIARY_IDENTITY_DIR=str(code / 'identity')),
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
