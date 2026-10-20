import contextvars
from contextlib import closing
import glob
import os
import sqlite3
import threading
from datetime import datetime
from utils.paths import DATA_ROOT

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(DATA_ROOT, 'user-data', 'user0', 'diary-data')
LEGACY_DATA_DIR = os.path.join(DATA_ROOT, 'data')

DEFAULT_FIELDS = [
    '天气',
    '心情',
    '醒来状态',
    '心率',
    'HP',
    'San',
    '起床体重',
    '腰围',
    '用药与健康建议',
    '计划与摘要',
    '上午',
    '下午',
    '晚上',
    '几天没做试验',
    '心得备注',
]

FIELD_TO_COL = {
    '天气': 'weather',
    '心情': 'mood',
    '醒来状态': 'wake_state',
    '心率': 'heart_rate',
    'HP': 'hp',
    'San': 'san',
    '起床体重': 'weight',
    '腰围': 'waist',
    '用药与健康建议': 'health_advice',
    '计划与摘要': 'plan_summary',
    '上午': 'morning',
    '下午': 'afternoon',
    '晚上': 'evening',
    '几天没做试验': 'days_no_test',
    '心得备注': 'note',
}
COL_TO_FIELD = {v: k for k, v in FIELD_TO_COL.items()}
CURRENT_STATE_FIELDS = {'待提升属性': 'improvement_attributes', '在途任务': 'in_flight_tasks'}


def _empty_log(date_str):
    log = {'date': date_str}
    for field in DEFAULT_FIELDS:
        log[field] = ''
    return log


class LogManager:
    _data_dir_var = contextvars.ContextVar('log_manager_data_dir', default=DATA_DIR)
    _lock = threading.Lock()
    _initialized_paths = set()

    @classmethod
    def set_data_dir(cls, data_dir):
        target = os.path.normpath(str(data_dir or '')).strip()
        if target:
            cls._data_dir_var.set(target)

    @classmethod
    def _get_data_dir(cls):
        current = cls._data_dir_var.get()
        return current or DATA_DIR

    @classmethod
    def _get_db_path(cls):
        return os.path.join(cls._get_data_dir(), 'diary.db')

    @classmethod
    def _get_attr_path(cls):
        return os.path.join(cls._get_data_dir(), '待提升属性.txt')

    @staticmethod
    def _validate_date(date_str):
        datetime.strptime(date_str, '%Y-%m-%d')

    @classmethod
    def _get_conn(cls):
        conn = sqlite3.connect(cls._get_db_path(), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def _migrate_from_excels(cls, conn):
        try:
            import pandas as pd
        except Exception:
            return

        current_dir = os.path.dirname(cls._get_db_path())
        excel_files = sorted(glob.glob(os.path.join(current_dir, '*.xlsx')))
        for path in excel_files:
            try:
                df = pd.read_excel(path)
            except Exception:
                continue

            if 'date' not in df.columns:
                continue

            for _, row in df.iterrows():
                date_str = str(row.get('date', '')).strip()
                if not date_str:
                    continue
                try:
                    cls._validate_date(date_str)
                except Exception:
                    continue

                values = [date_str]
                for field in DEFAULT_FIELDS:
                    v = row.get(field, '')
                    if pd.isna(v):
                        v = ''
                    values.append(str(v))

                conn.execute(
                    '''
                    INSERT INTO logs (
                        date, weather, mood, wake_state, heart_rate, hp, san, weight, waist,
                        health_advice, plan_summary, morning, afternoon, evening, days_no_test, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        weather=excluded.weather,
                        mood=excluded.mood,
                        wake_state=excluded.wake_state,
                        heart_rate=excluded.heart_rate,
                        hp=excluded.hp,
                        san=excluded.san,
                        weight=excluded.weight,
                        waist=excluded.waist,
                        health_advice=excluded.health_advice,
                        plan_summary=excluded.plan_summary,
                        morning=excluded.morning,
                        afternoon=excluded.afternoon,
                        evening=excluded.evening,
                        days_no_test=excluded.days_no_test,
                        note=excluded.note
                    ''',
                    values,
                )

    @classmethod
    def _init_db(cls):
        db_path = cls._get_db_path()
        if db_path in cls._initialized_paths:
            return

        current_dir = os.path.dirname(db_path)
        os.makedirs(current_dir, exist_ok=True)

        legacy_db = os.path.join(LEGACY_DATA_DIR, 'diary.db')
        if (not os.path.exists(db_path)) and os.path.exists(legacy_db):
            try:
                import shutil
                shutil.copy2(legacy_db, db_path)
            except Exception:
                pass

        with cls._lock:
            if db_path in cls._initialized_paths:
                return
            with closing(cls._get_conn()) as conn, conn:
                conn.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS logs (
                        date TEXT PRIMARY KEY,
                        weather TEXT NOT NULL DEFAULT '',
                        mood TEXT NOT NULL DEFAULT '',
                        wake_state TEXT NOT NULL DEFAULT '',
                        heart_rate TEXT NOT NULL DEFAULT '',
                        hp TEXT NOT NULL DEFAULT '',
                        san TEXT NOT NULL DEFAULT '',
                        weight TEXT NOT NULL DEFAULT '',
                        waist TEXT NOT NULL DEFAULT '',
                        health_advice TEXT NOT NULL DEFAULT '',
                        plan_summary TEXT NOT NULL DEFAULT '',
                        morning TEXT NOT NULL DEFAULT '',
                        afternoon TEXT NOT NULL DEFAULT '',
                        evening TEXT NOT NULL DEFAULT '',
                        days_no_test TEXT NOT NULL DEFAULT '',
                        note TEXT NOT NULL DEFAULT ''
                    )
                    '''
                )
                conn.execute('''CREATE TABLE IF NOT EXISTS user_state (
                    key TEXT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT ''
                )''')
                # 旧属性文本只迁移一次，保留原文件以便回退；以后以 SQLite 为准。
                existing = conn.execute('SELECT 1 FROM user_state WHERE key=?',
                                        ('improvement_attributes',)).fetchone()
                if existing is None:
                    attribute = ''
                    attr_path = cls._get_attr_path()
                    if os.path.exists(attr_path):
                        with open(attr_path, 'r', encoding='utf-8') as handle:
                            attribute = handle.read()
                    conn.execute('INSERT OR IGNORE INTO user_state (key, content) VALUES (?, ?)',
                                 ('improvement_attributes', attribute))
                conn.execute('INSERT OR IGNORE INTO user_state (key, content) VALUES (?, ?)',
                             ('in_flight_tasks', ''))
                row = conn.execute('SELECT COUNT(*) AS c FROM logs').fetchone()
                if row and row['c'] == 0:
                    cls._migrate_from_excels(conn)
                conn.commit()
            cls._initialized_paths.add(db_path)

    @classmethod
    def load_log(cls, date_str):
        cls._validate_date(date_str)
        cls._init_db()

        log = _empty_log(date_str)
        with closing(cls._get_conn()) as conn, conn:
            row = conn.execute(
                '''
                SELECT weather, mood, wake_state, heart_rate, hp, san, weight, waist,
                       health_advice, plan_summary, morning, afternoon, evening, days_no_test, note
                FROM logs WHERE date=?
                ''',
                (date_str,),
            ).fetchone()
            if row is None:
                # 仅为未保存日期提供默认草稿，不创建日志，也不从后面的日期取值。
                previous = conn.execute(
                    'SELECT plan_summary FROM logs WHERE date < ? ORDER BY date DESC LIMIT 1',
                    (date_str,),
                ).fetchone()
                if previous is not None:
                    # 已保存的空值同样有效，不能跳过它而恢复更早的旧计划。
                    log['计划与摘要'] = previous['plan_summary'] or ''

        if row:
            for col, field in COL_TO_FIELD.items():
                log[field] = row[col] or ''
        with closing(cls._get_conn()) as conn, conn:
            current = dict(conn.execute('SELECT key, content FROM user_state').fetchall())
        for field, key in CURRENT_STATE_FIELDS.items():
            log[field] = current.get(key, '')
        return log

    @classmethod
    def save_log(cls, data):
        date_str = data.get('date', '')
        cls._validate_date(date_str)
        cls._init_db()

        to_save = dict(data)
        current_state = [(key, str(to_save.pop(field) or ''))
                         for field, key in CURRENT_STATE_FIELDS.items() if field in to_save]
        values = [date_str]
        for field in DEFAULT_FIELDS:
            values.append(str(to_save.get(field, '') or ''))

        with cls._lock:
            with closing(cls._get_conn()) as conn, conn:
                conn.execute(
                    '''
                    INSERT INTO logs (
                        date, weather, mood, wake_state, heart_rate, hp, san, weight, waist,
                        health_advice, plan_summary, morning, afternoon, evening, days_no_test, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        weather=excluded.weather,
                        mood=excluded.mood,
                        wake_state=excluded.wake_state,
                        heart_rate=excluded.heart_rate,
                        hp=excluded.hp,
                        san=excluded.san,
                        weight=excluded.weight,
                        waist=excluded.waist,
                        health_advice=excluded.health_advice,
                        plan_summary=excluded.plan_summary,
                        morning=excluded.morning,
                        afternoon=excluded.afternoon,
                        evening=excluded.evening,
                        days_no_test=excluded.days_no_test,
                        note=excluded.note
                    ''',
                    tuple(values),
                )
                conn.executemany('''INSERT INTO user_state (key, content) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET content=excluded.content''', current_state)
                conn.commit()

    @classmethod
    def export_logs_to_excel(cls, output_filename='日志导出.xlsx'):
        cls._init_db()
        try:
            import pandas as pd
        except Exception as e:
            raise RuntimeError(f'pandas_unavailable: {e}')

        with closing(cls._get_conn()) as conn, conn:
            df = pd.read_sql_query(
                '''
                SELECT
                    date,
                    weather,
                    mood,
                    wake_state,
                    heart_rate,
                    hp,
                    san,
                    weight,
                    waist,
                    health_advice,
                    plan_summary,
                    morning,
                    afternoon,
                    evening,
                    days_no_test,
                    note
                FROM logs
                ORDER BY date ASC
                ''',
                conn,
            )

        rename_map = {'date': 'date'}
        rename_map.update(COL_TO_FIELD)
        df = df.rename(columns=rename_map)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')

        out_dir = os.path.dirname(cls._get_db_path())
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, output_filename)
        df.to_excel(output_path, index=False)
        return {'output_path': output_path, 'row_count': int(len(df))}
