"""Create exactly one fictional account in an empty, local diary data directory.

This script never imports app.py, starts a server, or contacts a cloud service.
It refuses to replace existing data. The generated data/ stays out of Git.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
DIARY = ROOT / '日记本' / 'current'
DEMO_USERNAME = 'demo_guest'
DEMO_PASSWORD = 'ExampleOnly-42!'


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='在空数据目录初始化一名虚构示例用户')
    parser.add_argument('--data-root', type=Path, default=DIARY / 'data')
    args = parser.parse_args()
    destination = args.data_root.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        parser.error('目标数据目录不是空目录，已停止；不会覆盖现有账号或数据。')

    # Explicitly bind storage before importing the application storage helpers.
    os.environ['DIARY_DATA_ROOT'] = str(destination)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(DIARY))
    from werkzeug.security import generate_password_hash
    from utils.excel_manager import LogManager

    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / 'auth' / 'users.json', {
        DEMO_USERNAME: {
            'password_hash': generate_password_hash(DEMO_PASSWORD),
            'locked_until': None,
            'failed_attempts': 0,
        },
    })
    write_json(destination / 'user-data' / 'user_map.json', {'user0': DEMO_USERNAME})
    user = destination / 'user-data' / 'user0'
    write_json(user / 'ui_settings.json', {'ui_sound_enabled': True, 'ui_fx_enabled': True})
    audio_files = []
    music = destination / 'music-data'
    music.mkdir(parents=True, exist_ok=True)
    for audio in sorted((DIARY / 'assets' / 'welcome-audio').iterdir()):
        if audio.is_file() and audio.suffix.lower() in {'.mp3', '.wav', '.ogg', '.m4a'}:
            shutil.copyfile(audio, music / audio.name)
            audio_files.append(audio.name)
    write_json(user / 'welcome_audio.json', {'files': audio_files, 'weights': [1] * len(audio_files)})
    LogManager.set_data_dir(user / 'diary-data')
    LogManager._init_db()
    # An example installation never inherits anyone else's cloud destination.
    write_json(destination / 'oss_backup_config.json', {'enabled': False})
    print('已创建唯一示例用户：' + DEMO_USERNAME)
    print('示例密码：' + DEMO_PASSWORD)
    print('日记、资产、在途任务均为空；认知工作区没有导入对话。')
    print('公开部署前请更改示例密码，或从空数据目录自行注册。')


if __name__ == '__main__':
    main()
