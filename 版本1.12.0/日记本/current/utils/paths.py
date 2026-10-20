"""身份在项目层保持稳定，业务数据默认跟随版本放在 data 中。"""
import os
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
# 数据相对版本目录，身份相对项目目录；都不依赖终端的当前目录。
DATA_ROOT = (CODE_ROOT / os.environ.get('DIARY_DATA_ROOT', 'data')).resolve()
IDENTITY_ROOT = (CODE_ROOT.parent / os.environ.get('DIARY_IDENTITY_DIR', 'identity')).resolve()
if DATA_ROOT == CODE_ROOT:
    raise ValueError('数据目录不能直接使用代码根目录，请使用 data 子目录')
if (IDENTITY_ROOT == CODE_ROOT or CODE_ROOT in IDENTITY_ROOT.parents
        or IDENTITY_ROOT == DATA_ROOT or DATA_ROOT in IDENTITY_ROOT.parents):
    raise ValueError('身份目录必须独立于版本目录和业务数据目录')
