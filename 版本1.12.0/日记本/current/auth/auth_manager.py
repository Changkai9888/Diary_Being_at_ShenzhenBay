import json
import os
import re
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash
from utils.paths import DATA_ROOT
from utils.safe_files import atomic_write_json, data_file_lock


class AuthManager:
    def __init__(self, default_user=None):
        self.file_path = str(DATA_ROOT / 'auth' / 'users.json')
        self.default_user = default_user
        self._init_storage()

    def _init_storage(self):
        with data_file_lock:
            if not os.path.exists(self.file_path):
                user_root = os.path.join(os.path.dirname(os.path.dirname(self.file_path)), 'user-data')
                if (os.path.exists(os.path.join(user_root, 'user_map.json'))
                        or (os.path.isdir(user_root) and any(re.fullmatch(r'user[0-9]+', name)
                                                            for name in os.listdir(user_root)))):
                    raise RuntimeError('账号文件缺失，但已有用户映射或数据；请恢复原 users.json')
                self._save_users({})

    def _load_users(self):
        with data_file_lock:
            if not os.path.exists(self.file_path):
                self._init_storage()
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise RuntimeError('账号文件损坏，请恢复原 users.json')
            return data

    def _save_users(self, users):
        atomic_write_json(self.file_path, users)

    def validate_user(self, username, password):
        with data_file_lock:
            users = self._load_users()
            user = users.get(username)

            if not user:
                return '用户不存在'

            if user.get('locked_until') and datetime.now().timestamp() < user['locked_until']:
                return '账户已锁定'

            if check_password_hash(user['password_hash'], password):
                user['failed_attempts'] = 0
                self._save_users(users)
                return 'success'

            user['failed_attempts'] = int(user.get('failed_attempts', 0)) + 1
            if user['failed_attempts'] >= 3:
                user['locked_until'] = (datetime.now() + timedelta(hours=1)).timestamp()
            self._save_users(users)
            return '密码错误'

    def register_user(self, username, password, confirm_password):
        name = str(username or '').strip()
        pwd = str(password or '')
        cpwd = str(confirm_password or '')

        if not name:
            return '用户名不能为空'
        if not pwd:
            return '密码不能为空'
        if pwd != cpwd:
            return '两次输入的密码不一致'

        with data_file_lock:
            users = self._load_users()
            if name in users:
                return '用户名已存在'

            users[name] = {
                'password_hash': generate_password_hash(pwd),
                'locked_until': None,
                'failed_attempts': 0,
            }
            self._save_users(users)
            return 'success'

    def change_password(self, username, old_password, new_password, confirm_password):
        name = str(username or '').strip()
        old_pwd = str(old_password or '')
        new_pwd = str(new_password or '')
        confirm_pwd = str(confirm_password or '')

        if not name:
            return '用户未登录'
        if not old_pwd:
            return '旧密码不能为空'
        if not new_pwd:
            return '新密码不能为空'
        if new_pwd != confirm_pwd:
            return '两次输入的新密码不一致'

        with data_file_lock:
            users = self._load_users()
            user = users.get(name)
            if not user:
                return '用户不存在'
            if not check_password_hash(user.get('password_hash', ''), old_pwd):
                return '旧密码错误'

            user['password_hash'] = generate_password_hash(new_pwd)
            user['failed_attempts'] = 0
            user['locked_until'] = None
            self._save_users(users)
            return 'success'
