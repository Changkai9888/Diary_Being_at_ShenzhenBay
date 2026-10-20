"""当前任务/属性与每日日记共同持久化、隔离和备份；全部使用临时数据。"""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from utils import excel_manager
from utils.excel_manager import LogManager
from utils.oss_backup import create_snapshot


class CurrentStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'user-data'
        self.diary = self.users / 'user0' / 'diary-data'
        # 不允许旧版数据库回退触及运行中的正式数据。
        legacy = patch.object(excel_manager, 'LEGACY_DATA_DIR', str(self.root / 'no-legacy'))
        legacy.start()
        self.addCleanup(legacy.stop)
        initialized = patch.object(LogManager, '_initialized_paths', set())
        initialized.start()
        self.addCleanup(initialized.stop)
        token = LogManager._data_dir_var.set(str(self.diary))
        self.addCleanup(LogManager._data_dir_var.reset, token)

    @contextmanager
    def user(self, user_id):
        token = LogManager._data_dir_var.set(str(self.users / user_id / 'diary-data'))
        try:
            yield
        finally:
            LogManager._data_dir_var.reset(token)

    def test_latest_state_is_shared_across_dates_while_daily_notes_are_independent(self):
        LogManager.save_log({'date': '2026-10-16', '心得备注': '昨日记录',
                             '在途任务': '旧任务', '待提升属性': '旧属性'})
        LogManager.save_log({'date': '2026-10-17', '心得备注': '今日记录',
                             '在途任务': '最新任务\n第二行', '待提升属性': '最新属性'})
        for date, note in [('2026-10-16', '昨日记录'), ('2026-10-17', '今日记录'),
                           ('2030-01-01', '')]:
            with self.subTest(date=date):
                loaded = LogManager.load_log(date)
                self.assertEqual(loaded['心得备注'], note)
                self.assertEqual(loaded['在途任务'], '最新任务\n第二行')
                self.assertEqual(loaded['待提升属性'], '最新属性')
        with closing(sqlite3.connect(self.diary / 'diary.db')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM logs').fetchone(), (2,))
            self.assertEqual(dict(conn.execute('SELECT key, content FROM user_state')), {
                'in_flight_tasks': '最新任务\n第二行', 'improvement_attributes': '最新属性'})
            # 截面只有两个当前值，不以日期为键重复保存。
            self.assertEqual({row[1] for row in conn.execute('PRAGMA table_info(user_state)')},
                             {'key', 'content'})

    def test_omitted_state_is_retained_and_explicit_empty_content_clears_it(self):
        LogManager.save_log({'date': '2026-10-16', '在途任务': '任务', '待提升属性': '属性'})
        LogManager.save_log({'date': '2026-10-17', '心得备注': '只改日记'})
        unchanged = LogManager.load_log('2026-10-17')
        self.assertEqual((unchanged['在途任务'], unchanged['待提升属性']), ('任务', '属性'))
        LogManager.save_log({'date': '2026-10-17', '在途任务': ''})
        changed = LogManager.load_log('2026-10-16')
        self.assertEqual((changed['在途任务'], changed['待提升属性']), ('', '属性'))
        LogManager.save_log({'date': '2026-10-18', '待提升属性': ''})
        self.assertEqual(LogManager.load_log('2026-10-17')['待提升属性'], '')

    def test_user_state_never_crosses_account_boundaries(self):
        LogManager.save_log({'date': '2026-10-17', '在途任务': '甲任务', '待提升属性': '甲属性'})
        with self.user('user1'):
            empty = LogManager.load_log('2026-10-17')
            self.assertEqual((empty['在途任务'], empty['待提升属性']), ('', ''))
            LogManager.save_log({'date': '2026-10-17', '在途任务': '乙任务', '待提升属性': '乙属性'})
            self.assertEqual(LogManager.load_log('2026-10-16')['在途任务'], '乙任务')
        original = LogManager.load_log('2026-10-17')
        self.assertEqual((original['在途任务'], original['待提升属性']), ('甲任务', '甲属性'))

    def test_legacy_attributes_migrate_once_without_rewriting_or_later_overriding_state(self):
        self.diary.mkdir(parents=True)
        legacy = self.diary / '待提升属性.txt'
        original = '保留原文\n  缩进与空行\n\n最后一行  '
        legacy.write_text(original, encoding='utf-8')
        self.assertEqual(LogManager.load_log('2026-10-17')['待提升属性'], original)
        LogManager.save_log({'date': '2026-10-17', '待提升属性': '修改后的当前属性'})
        self.assertEqual(legacy.read_text(encoding='utf-8'), original)
        # 模拟服务重启；保留的旧文件不得重新覆盖数据库中的更新或明确清空。
        LogManager._initialized_paths.clear()
        self.assertEqual(LogManager.load_log('2026-10-18')['待提升属性'], '修改后的当前属性')
        LogManager.save_log({'date': '2026-10-18', '待提升属性': ''})
        LogManager._initialized_paths.clear()
        self.assertEqual(LogManager.load_log('2026-10-19')['待提升属性'], '')
        self.assertEqual(legacy.read_text(encoding='utf-8'), original)

    def test_state_write_failure_rolls_back_daily_log_and_all_current_state(self):
        LogManager.save_log({'date': '2026-10-17', '心得备注': '原日记',
                             '在途任务': '原任务', '待提升属性': '原属性'})
        with closing(sqlite3.connect(self.diary / 'diary.db')) as conn, conn:
            conn.execute('''CREATE TRIGGER reject_task_update BEFORE UPDATE ON user_state
                WHEN NEW.key='in_flight_tasks' AND NEW.content='reject'
                BEGIN SELECT RAISE(ABORT, 'simulated state write failure'); END''')
        with self.assertRaises(sqlite3.DatabaseError):
            LogManager.save_log({'date': '2026-10-17', '心得备注': '不应留下的新日记',
                                 '在途任务': 'reject', '待提升属性': '不应留下的新属性'})
        restored = LogManager.load_log('2026-10-17')
        self.assertEqual((restored['心得备注'], restored['在途任务'], restored['待提升属性']),
                         ('原日记', '原任务', '原属性'))

    def test_snapshot_zip_restores_latest_state_and_daily_logs_from_same_database(self):
        LogManager.save_log({'date': '2026-10-16', '心得备注': '保留昨日',
                             '在途任务': '旧任务', '待提升属性': '旧属性'})
        LogManager.save_log({'date': '2026-10-17', '心得备注': '保留今日',
                             '在途任务': '最新任务', '待提升属性': '最新属性'})
        work = self.root / 'backup-work'
        work.mkdir()
        archive = create_snapshot(self.users, work)
        restored_dir = self.root / 'restored'
        restored_dir.mkdir()
        with zipfile.ZipFile(archive) as source:
            self.assertIsNone(source.testzip())
            (restored_dir / 'diary.db').write_bytes(
                source.read('user-data/user0/diary-data/diary.db'))
        token = LogManager._data_dir_var.set(str(restored_dir))
        try:
            for date, note in [('2026-10-16', '保留昨日'), ('2026-10-17', '保留今日')]:
                restored = LogManager.load_log(date)
                self.assertEqual((restored['心得备注'], restored['在途任务'], restored['待提升属性']),
                                 (note, '最新任务', '最新属性'))
        finally:
            LogManager._data_dir_var.reset(token)
        with closing(sqlite3.connect(restored_dir / 'diary.db')) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])


if __name__ == '__main__':
    unittest.main()
