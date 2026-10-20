from flask import Blueprint, current_app, Flask, g, render_template, request, jsonify, session, redirect, url_for, send_file
from datetime import datetime, timedelta
import calendar
import json
import os
import random
import re
import shutil
import time
import pandas as pd
from utils.lunar_handler import LunarConverter
from utils.excel_manager import LogManager
from auth.auth_manager import AuthManager
from flask_login import login_required
from utils.oss_backup import BackupService
from utils.safe_files import atomic_write_json, atomic_write_text, data_file_lock
from utils.service_identity import SERVICE_ID, IDENTITY_PROTOCOL, SERVICE_INFO, APP_VERSION, LocalProxyPrefix
from utils.paths import DATA_ROOT, IDENTITY_ROOT
from utils.workspace_gateway import WorkspaceGateway, persistent_secret

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PORT = 8081
app = Flask(__name__, static_folder=None)
app.wsgi_app = LocalProxyPrefix(app.wsgi_app)
app.config['SERVICE_READY'] = True


@app.get('/__service__/identity')
def service_identity():
    # 应用级路由，不进入日记 Blueprint 的登录检查或备份启动逻辑。
    ready = bool(app.config['SERVICE_READY'])
    response = jsonify(service_id=SERVICE_ID, identity_protocol=IDENTITY_PROTOCOL,
                       app_version=APP_VERSION, ready=ready,
                       service_name=SERVICE_INFO['service_name'],
                       service_role=SERVICE_INFO['service_role'],
                       gateway_service_id=SERVICE_INFO['gateway_service_id'],
                       gateway_name=SERVICE_INFO['gateway_name'],
                       services=[{'name': '日记本', 'role': 'gateway', 'mount': '/'},
                                 {'name': '认知工作区', 'role': 'workspace', 'mount': '/cognitive/'}],
                       capabilities=['forwarded-prefix-v1', 'service-hierarchy-v1'])
    response.status_code = 200 if ready else 503
    response.headers['Cache-Control'] = 'no-store'
    return response


diary = Blueprint('diary', __name__, url_prefix='',
                  template_folder='templates', static_folder='static', static_url_path='/static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or persistent_secret(DATA_ROOT / '.session-secret')
app.config['DATA_PATH'] = os.path.join(DATA_ROOT, 'user-data', '_current', 'diary-data')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['SESSION_REFRESH_EACH_REQUEST'] = False
app.config['SESSION_COOKIE_NAME'] = 'diary_session'
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# 初始化认证模块
auth = AuthManager()

USER_DATA_ROOT = os.path.join(DATA_ROOT, 'user-data')
USER_MAP_PATH = os.path.join(USER_DATA_ROOT, 'user_map.json')
USER_ID_PATTERN = re.compile(r'user(?:0|[1-9][0-9]{0,8})\Z')
backup_service = BackupService(DATA_ROOT, config=None if (DATA_ROOT / 'oss_backup_config.json').exists()
                               else {'enabled': False})
GLOBAL_MUSIC_DIR = os.path.join(DATA_ROOT, 'music-data')
BUNDLED_MUSIC_DIR = os.path.join(PROJECT_ROOT, 'assets', 'welcome-audio')
DEFAULT_USER_SETTINGS = {'ui_sound_enabled': True, 'ui_fx_enabled': True}
DEFAULT_WELCOME_AUDIO_CONFIG = {'files': [], 'weights': []}


# 子服务配置与入口属于深圳湾日记自身，不依赖最外层主站。
workspace_gateway = WorkspaceGateway(
    os.environ.get('SZD_COGNITIVE_CONFIG') or IDENTITY_ROOT / 'subservices.json',
    DATA_ROOT / '.subservices')


def _load_user_map():
    with data_file_lock:
        try:
            user_entries = set()
            if os.path.isdir(USER_DATA_ROOT):
                with os.scandir(USER_DATA_ROOT) as entries:
                    user_entries = {entry.name for entry in entries
                                    if re.fullmatch(r'user[0-9]+', entry.name)}
            if not os.path.exists(USER_MAP_PATH):
                # Existing user directories must never be reassigned as a new installation.
                if user_entries:
                    raise RuntimeError('用户映射缺失，但已有用户数据；请恢复原 user_map.json')
                return {}

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('用户映射包含重复用户编号')
                    result[key] = value
                return result

            with open(USER_MAP_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f, object_pairs_hook=unique_object)
            if not isinstance(data, dict):
                raise ValueError('用户映射必须是 JSON 对象')
            usernames = set()
            for uid, username in data.items():
                if (not isinstance(uid, str) or not USER_ID_PATTERN.fullmatch(uid)
                        or not isinstance(username, str) or not username.strip()
                        or username != username.strip() or username in usernames):
                    raise ValueError('用户映射必须包含有效且唯一的用户编号与用户名')
                usernames.add(username)
            if user_entries - data.keys():
                raise ValueError('已有用户数据缺少对应映射，不能重新分配')
            return data
        except (OSError, ValueError) as exc:
            raise RuntimeError('无法读取有效用户映射；请检查或恢复原 user_map.json') from exc


def _save_user_map(user_map):
    os.makedirs(USER_DATA_ROOT, exist_ok=True)
    atomic_write_json(USER_MAP_PATH, user_map)


def _get_user_dir(user_id):
    return os.path.join(USER_DATA_ROOT, user_id)


def _get_user_diary_dir(user_id):
    return os.path.join(_get_user_dir(user_id), 'diary-data')


def _get_user_settings_path(user_id):
    return os.path.join(_get_user_dir(user_id), 'ui_settings.json')


def _get_user_welcome_audio_config_path(user_id):
    return os.path.join(_get_user_dir(user_id), 'welcome_audio.json')


def _write_json_if_missing(path, content):
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, content)


def _initialize_bundled_welcome_audio():
    """新安装使用内置欢迎音频，已有同名音频和用户设置保持原样。"""
    if not os.path.isdir(BUNDLED_MUSIC_DIR):
        return
    os.makedirs(GLOBAL_MUSIC_DIR, exist_ok=True)
    for name in sorted(os.listdir(BUNDLED_MUSIC_DIR)):
        source = os.path.join(BUNDLED_MUSIC_DIR, name)
        if not os.path.isfile(source) or not name.lower().endswith(('.mp3', '.wav', '.ogg', '.m4a')):
            continue
        try:
            with open(source, 'rb') as src, open(os.path.join(GLOBAL_MUSIC_DIR, name), 'xb') as dst:
                shutil.copyfileobj(src, dst)
        except FileExistsError:
            pass


def _ensure_user_folder(user_id):
    user_dir = _get_user_dir(user_id)
    diary_dir = _get_user_diary_dir(user_id)
    os.makedirs(diary_dir, exist_ok=True)
    _write_json_if_missing(_get_user_settings_path(user_id), DEFAULT_USER_SETTINGS)

    welcome_cfg_path = _get_user_welcome_audio_config_path(user_id)
    if not os.path.exists(welcome_cfg_path):
        available = []
        if os.path.isdir(GLOBAL_MUSIC_DIR):
            exts = ('.mp3', '.wav', '.ogg', '.m4a')
            available = sorted([n for n in os.listdir(GLOBAL_MUSIC_DIR) if n.lower().endswith(exts)])
        cfg = dict(DEFAULT_WELCOME_AUDIO_CONFIG)
        cfg['files'] = available
        cfg['weights'] = [1] * len(available)
        _write_json_if_missing(welcome_cfg_path, cfg)


def _resolve_user_id_by_username(username):
    uname = str(username or '').strip()
    if not uname:
        return None
    user_map = _load_user_map()
    for uid, name in user_map.items():
        if str(name) == uname:
            return uid
    return None


def _allocate_next_user_id(user_map):
    max_idx = -1
    for uid in user_map.keys():
        s = str(uid or '')
        if not s.startswith('user'):
            continue
        num = s[4:]
        if num.isdigit():
            max_idx = max(max_idx, int(num))
    return f'user{max_idx + 1}'


def _register_user_profile(username):
    username = str(username or '').strip()
    if not username:
        raise ValueError('missing_session_username')
    with data_file_lock:
        user_map = _load_user_map()
        for uid, name in user_map.items():
            if name == username:
                _ensure_user_folder(uid)
                return uid
        new_user_id = _allocate_next_user_id(user_map)
        if not USER_ID_PATTERN.fullmatch(new_user_id):
            raise RuntimeError('用户编号范围已用尽，不能继续分配账号数据目录')
        user_map[new_user_id] = username
        _save_user_map(user_map)
        _ensure_user_folder(new_user_id)
        return new_user_id


def _setup_current_user_paths():
    username = (session.get('username') or '').strip()
    if not username:
        raise ValueError('missing_session_username')
    # Registration and first-login repair share one read/allocate/write transaction.
    user_id = _register_user_profile(username)

    diary_dir = _get_user_diary_dir(user_id)
    LogManager.set_data_dir(diary_dir)
    return user_id


def _get_current_user_diary_dir():
    user_id = _setup_current_user_paths()
    return _get_user_diary_dir(user_id)


def _migrate_legacy_user0_data():
    legacy_diary_root = os.path.join(DATA_ROOT, 'diary-data', 'user0')
    if not os.path.isdir(legacy_diary_root):
        return

    os.makedirs(USER_DATA_ROOT, exist_ok=True)
    user_map = _load_user_map()
    if 'user0' not in user_map:
        users = auth._load_users()
        if not users:
            return
        first_username = next(iter(users.keys()))
        user_map['user0'] = first_username
        _save_user_map(user_map)

    new_diary_root = _get_user_diary_dir('user0')
    os.makedirs(new_diary_root, exist_ok=True)
    if os.path.isdir(legacy_diary_root):
        for name in os.listdir(legacy_diary_root):
            src = os.path.join(legacy_diary_root, name)
            dst = os.path.join(new_diary_root, name)
            if not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                except Exception:
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        pass
        try:
            if not os.listdir(legacy_diary_root):
                os.rmdir(legacy_diary_root)
            parent = os.path.join(DATA_ROOT, 'diary-data')
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except Exception:
            pass

    _ensure_user_folder('user0')


def _read_user_settings(user_id):
    path = _get_user_settings_path(user_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {
            'ui_sound_enabled': bool(data.get('ui_sound_enabled', True)),
            'ui_fx_enabled': bool(data.get('ui_fx_enabled', True)),
        }
    except Exception:
        return dict(DEFAULT_USER_SETTINGS)


def _write_user_settings(user_id, settings):
    path = _get_user_settings_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, settings)


def _pick_welcome_audio_filename_for_user(user_id):
    path = _get_user_welcome_audio_config_path(user_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        cfg = dict(DEFAULT_WELCOME_AUDIO_CONFIG)

    candidates = []
    weights = []
    for idx, name in enumerate(cfg.get('files') or []):
        file_name = str(name or '').strip()
        if not file_name:
            continue
        abs_path = os.path.join(GLOBAL_MUSIC_DIR, file_name)
        if not os.path.isfile(abs_path):
            continue
        w_list = cfg.get('weights') or []
        raw_w = w_list[idx] if idx < len(w_list) else 1
        try:
            w = float(raw_w)
        except Exception:
            w = 1
        if w <= 0:
            continue
        candidates.append(file_name)
        weights.append(w)

    if not candidates and os.path.isdir(GLOBAL_MUSIC_DIR):
        exts = ('.mp3', '.wav', '.ogg', '.m4a')
        candidates = sorted([n for n in os.listdir(GLOBAL_MUSIC_DIR) if n.lower().endswith(exts)])
        weights = [1] * len(candidates)

    if not candidates:
        return None
    return random.choices(candidates, weights=weights, k=1)[0]


_initialize_bundled_welcome_audio()
_migrate_legacy_user0_data()

@diary.before_request
def check_auth():
    # Also supports WSGI workers, where __main__ is not executed.
    # Startup itself does not perform network work on the request thread.
    if not current_app.testing:
        backup_service.start()
    # Unregistered paths, including '/', stay 404 instead of claiming another site's home.
    if request.endpoint not in ['diary.login', 'diary.register', 'diary.static']:
        locked_until = session.get('locked_until')
        if locked_until and datetime.now().timestamp() < float(locked_until):
            session.clear()
            return _login_required_response()
        if locked_until and datetime.now().timestamp() >= float(locked_until):
            session.pop('locked_until', None)
        expiry = session.get('expires_at')
        if (not session.get('authenticated') or not session.get('username')
                or not isinstance(expiry, (int, float)) or not time.time() < expiry):
            session.clear()
            return _login_required_response()
        if request.endpoint != 'diary.session_status':
            g.user_id = _setup_current_user_paths()


def _login_required_response():
    target = url_for('diary.login', next=url_for('diary.index'))
    if (request.endpoint in ('diary.index', 'diary.logout')
            or (request.endpoint == 'cognitive_proxy' and request.method in ('GET', 'HEAD')
                and request.view_args.get('subpath', '') in ('', 'index.html'))):
        return redirect(target)
    return jsonify({'status': 'error', 'code': 'session_expired',
                    'message': '登录已过期，请重新登录', 'redirect': target}), 401


@diary.after_request
def prevent_private_page_cache(response):
    if request.endpoint != 'diary.static':
        response.headers['Cache-Control'] = 'no-store'
    return response


@diary.route('/session_status', methods=['GET'])
def session_status():
    return jsonify({'authenticated': True, 'expires_at': session['expires_at'],
                    'server_now': time.time(),
                    'user_id': _resolve_user_id_by_username(session['username'])})

@diary.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        result = auth.validate_user(username, password)
        
        if result == "success":
            session.clear()
            session.permanent = True
            session['authenticated'] = True
            session['username'] = username
            session['expires_at'] = time.time() + current_app.permanent_session_lifetime.total_seconds()
            if request.accept_mimetypes.best == 'application/json':
                return jsonify({'status': 'success', 'redirect': url_for('diary.index')})
            return redirect(url_for('diary.index'))
            
        session['failed_attempts'] = session.get('failed_attempts', 0) + 1
        if session['failed_attempts'] >= 3:
            session['locked_until'] = (datetime.now() + timedelta(hours=1)).timestamp()
        return result, 401
    return render_template(
        'login.html',
        registered=request.args.get('registered') == '1',
        prefill_username=request.args.get('username') or ''
    )


@diary.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        result = auth.register_user(username, password, confirm_password)
        if result == 'success':
            _register_user_profile(username)
            return redirect(url_for('diary.login', registered='1', username=username))

        return render_template('register.html', error=result), 400

    return render_template('register.html', error='')


@diary.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('diary.login'))


@diary.route('/change_password', methods=['POST'])
def change_password():
    try:
        username = (session.get('username') or '').strip()
        if not username:
            return jsonify({'status': 'error', 'message': '用户未登录'}), 401

        payload = request.json or {}
        old_password = payload.get('old_password') or ''
        new_password = payload.get('new_password') or ''
        confirm_password = payload.get('confirm_password') or ''

        result = auth.change_password(username, old_password, new_password, confirm_password)
        if result != 'success':
            return jsonify({'status': 'error', 'message': result}), 400

        session.clear()
        return jsonify({'status': 'success', 'message': '密码修改成功，请重新登录', 'redirect': url_for('diary.login')})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@diary.route('/')
#@login_required
def index():
    return render_template(
        'index.html',
        default_date=datetime.now().strftime("%Y-%m-%d"),
        current_username=session.get('username') or '',
        backup_status=backup_service.status(),
        session_config={'expires_at': session['expires_at'], 'server_now': time.time(),
                        'status_url': url_for('diary.session_status'),
                        'login_url': url_for('diary.login', next=url_for('diary.index'))},
    )


@diary.route('/backup_status', methods=['GET'])
def backup_status():
    response = jsonify(backup_service.status())
    response.headers['Cache-Control'] = 'no-store'
    return response

@diary.route('/get_calendar')
def get_calendar_data():
    year = int(request.args.get('year'))
    month = int(request.args.get('month'))
    
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdays2calendar(year, month)
    
    lunar_data = []
    for week in weeks:
        new_week = []
        for day, _ in week:
            if day == 0:
                new_week.append({'solar': '', 'lunar': '', 'date': ''})
            else:
                lunar = LunarConverter.solar_to_lunar(year, month, day)
                new_week.append({
                    'solar': day,
                    'lunar': lunar['cn_day'],
                    'date': f"{year}-{month:02d}-{day:02d}",
                    'is_today': LunarConverter.is_today(year, month, day)
                })
        lunar_data.append(new_week)
    
    return jsonify({
        'weeks': lunar_data,
        'month_title': f"{year}年{month}月"
    })

@diary.route('/get_log', methods=['GET'])
def get_log():
    date_str = request.args.get('date')
    return jsonify(LogManager.load_log(date_str))

@diary.route('/save_log', methods=['POST'])
def save_log():
    try:
        data = request.json or {}
        LogManager.save_log(data)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@diary.route('/export_logs_excel', methods=['POST'])
def export_logs_excel():
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        export_result = LogManager.export_logs_to_excel(f"日志导出_{ts}.xlsx")
        return jsonify({
            'status': 'success',
            'message': '导出成功',
            'output_path': export_result['output_path'],
            'row_count': export_result['row_count'],
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _extract_asset_value(asset_line):
    line = str(asset_line or '').strip()
    if not line:
        return ''
    marker = ' | 记录时间: '
    if marker in line:
        return line.split(marker, 1)[0].strip()
    legacy_marker = '（记录于：'
    if legacy_marker in line:
        return line.split(legacy_marker, 1)[0].strip()
    return line


def _format_asset_entry(value):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return f"{value} | 记录时间: {ts}"


@diary.route('/get_last_asset', methods=['GET'])
def get_last_asset():
    data_dir = _get_current_user_diary_dir()
    asset_file = os.path.join(data_dir, '资产变化.txt')
    last_line = ''
    if os.path.exists(asset_file):
        try:
            with open(asset_file, 'r', encoding='utf-8') as f:
                lines = [ln.rstrip('\n') for ln in f if ln.strip() != '']
                if lines:
                    last_line = lines[-1]
        except Exception:
            last_line = ''
    return jsonify({'last_line': _extract_asset_value(last_line)})


@diary.route('/append_asset_if_changed', methods=['POST'])
def append_asset_if_changed():
    with data_file_lock:
        return _append_asset_if_changed()


def _append_asset_if_changed():
    payload = request.json or {}
    value = (payload.get('value') or '').strip()
    data_dir = _get_current_user_diary_dir()
    asset_file = os.path.join(data_dir, '资产变化.txt')
    os.makedirs(data_dir, exist_ok=True)

    # read last non-empty line
    last_line = ''
    last_value = ''
    if os.path.exists(asset_file):
        try:
            with open(asset_file, 'r', encoding='utf-8') as f:
                for ln in f:
                    if ln.strip():
                        last_line = ln.rstrip('\n')
            last_value = _extract_asset_value(last_line)
        except Exception:
            last_line = ''
            last_value = ''

    if not value:
        return jsonify({'appended': False, 'reason': 'empty_value', 'previous': last_value})

    if value != last_value:
        try:
            previous = ''
            if os.path.exists(asset_file):
                with open(asset_file, 'r', encoding='utf-8') as f:
                    previous = f.read()
            atomic_write_text(asset_file, previous + _format_asset_entry(value) + '\n')
            return jsonify({'appended': True, 'previous': last_value, 'new': value})
        except Exception as e:
            return jsonify({'appended': False, 'reason': str(e)})

    return jsonify({'appended': False, 'reason': 'no_change', 'previous': last_value})


@diary.route('/welcome-audio', methods=['GET'])
def welcome_audio():
    if not os.path.isdir(GLOBAL_MUSIC_DIR):
        return jsonify({'status': 'error', 'message': 'music_data_not_found'}), 404

    user_id = _setup_current_user_paths()
    selected = _pick_welcome_audio_filename_for_user(user_id)
    if not selected:
        return jsonify({'status': 'error', 'message': 'welcome_audio_not_found'}), 404

    response = send_file(os.path.join(GLOBAL_MUSIC_DIR, selected))
    # Ensure each page refresh fetches a fresh random audio selection.
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@diary.route('/get_user_ui_settings', methods=['GET'])
def get_user_ui_settings():
    try:
        user_id = _setup_current_user_paths()
        return jsonify({'status': 'success', 'settings': _read_user_settings(user_id)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@diary.route('/save_user_ui_settings', methods=['POST'])
def save_user_ui_settings():
    try:
        user_id = _setup_current_user_paths()
        payload = request.json or {}
        settings = {
            'ui_sound_enabled': bool(payload.get('ui_sound_enabled', True)),
            'ui_fx_enabled': bool(payload.get('ui_fx_enabled', True)),
        }
        _write_user_settings(user_id, settings)
        return jsonify({'status': 'success', 'settings': settings})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/cognitive', defaults={'subpath': ''},
           methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/cognitive/', defaults={'subpath': ''},
           methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/cognitive/<path:subpath>',
           methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def cognitive_proxy(subpath):
    auth_response = check_auth()
    if auth_response is not None:
        return auth_response
    if not subpath and not request.path.endswith('/'):
        return redirect(url_for('cognitive_proxy', subpath=''), code=308)
    return workspace_gateway.forward(subpath, g.user_id)


@app.after_request
def protect_workspace_cache(response):
    if request.endpoint == 'cognitive_proxy':
        response.headers['Cache-Control'] = 'no-store'
    return response


app.register_blueprint(diary)
# 保证 url_for 选择带末尾斜杠的工作区入口，同时保留原始 API 路径。
app.url_map.redirect_defaults = False


if __name__ == '__main__':
    import argparse
    import logging
    parser = argparse.ArgumentParser(description='日记服务及每日 OSS 备份')
    parser.add_argument('--backup-now', action='store_true', help='立即备份并校验一次，然后退出')
    parser.add_argument('--port', type=int, default=os.environ.get('DIARY_PORT', DEFAULT_PORT),
                        help='监听端口，默认 8081')
    parser.add_argument('--host', default=os.environ.get('DIARY_HOST', '0.0.0.0'),
                        help='监听地址；反向代理部署时可设置为 127.0.0.1')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    _migrate_legacy_user0_data()
    os.makedirs(USER_DATA_ROOT, exist_ok=True)
    if args.backup_now:
        raise SystemExit(0 if backup_service.run_due(force=True) else 1)
    backup_service.start()
    from waitress import serve
    serve(app, host=args.host, port=args.port, threads=8,
          max_request_body_size=100 * 1024 * 1024)
