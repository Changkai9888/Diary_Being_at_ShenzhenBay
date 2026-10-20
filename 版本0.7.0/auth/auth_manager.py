import json
import os
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# 
class AuthManager:
    def __init__(self, default_user,key):
        self.file_path = 'auth/users.json'
        self.default_user = default_user
        self.key=key
        self._init_default_user()
    def _init_default_user(self):
        if not os.path.exists(self.file_path):
            users = {
                self.default_user: {
                    "password_hash": generate_password_hash(self.key),
                    "locked_until": None,
                    "failed_attempts": 0
                }
            }
            self._save_users(users)

    def _load_users(self):
        with open(self.file_path) as f:
            return json.load(f)
    
    def _save_users(self, users):
        with open(self.file_path, 'w') as f:
            json.dump(users, f, indent=2)

    def validate_user(self, username, password):
        users = self._load_users()
        user = users.get(username)
        
        if not user:
            return "用户不存在"
            
        if user.get('locked_until') and datetime.now().timestamp() < user['locked_until']:
            return "账户已锁定"
        
        if check_password_hash(user['password_hash'], password):
            user['failed_attempts'] = 0
            self._save_users(users)
            return "success"
        
        user['failed_attempts'] += 1
        if user['failed_attempts'] >= 3:
            user['locked_until'] = (datetime.now() + timedelta(hours=1)).timestamp()
        self._save_users(users)
        return "密码错误"
