"""计划与摘要为未保存日期提供只读草稿，全部使用隔离的临时数据库。"""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from utils import excel_manager
from utils.excel_manager import LogManager


class PlanInheritanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.users = self.root / 'user-data'
        self.diary = self.users / 'user0' / 'diary-data'
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

    def save_plan(self, date, text, **other):
        LogManager.save_log({'date': date, '计划与摘要': text, **other})

    def stored_plans(self):
        with closing(sqlite3.connect(self.diary / 'diary.db')) as conn:
            return conn.execute('SELECT date, plan_summary FROM logs ORDER BY date').fetchall()

    def test_missing_days_inherit_across_month_and_year_boundaries(self):
        self.save_plan('2025-12-30', '跨年计划\n保持原文')
        for date in ('2025-12-31', '2026-01-01', '2026-02-10'):
            with self.subTest(date=date):
                loaded = LogManager.load_log(date)
                self.assertEqual(loaded['计划与摘要'], '跨年计划\n保持原文')
                self.assertEqual(loaded['date'], date)
                self.assertEqual(loaded['上午'], '')
        self.assertEqual(self.stored_plans(), [('2025-12-30', '跨年计划\n保持原文')])

    def test_calendar_order_is_used_instead_of_save_order(self):
        self.save_plan('2026-10-18', '较近日期')
        self.save_plan('2026-10-15', '较早但最后保存')
        self.assertEqual(LogManager.load_log('2026-10-19')['计划与摘要'], '较近日期')

    def test_later_dates_cannot_fill_an_earlier_gap(self):
        self.save_plan('2026-10-18', '之前的计划')
        self.save_plan('2026-10-21', '未来的计划')
        self.assertEqual(LogManager.load_log('2026-10-20')['计划与摘要'], '之前的计划')
        self.assertEqual(LogManager.load_log('2026-10-17')['计划与摘要'], '')

    def test_no_source_displays_empty_and_does_not_create_a_log(self):
        for date in ('2026-10-19', '2030-01-01'):
            self.assertEqual(LogManager.load_log(date)['计划与摘要'], '')
        self.assertEqual(self.stored_plans(), [])

    def test_existing_date_keeps_its_own_plan_including_saved_empty(self):
        self.save_plan('2026-10-17', '旧计划')
        self.save_plan('2026-10-18', '', **{'心得备注': '主动保留空计划'})
        self.save_plan('2026-10-19', '独立计划')
        self.assertEqual(LogManager.load_log('2026-10-18')['计划与摘要'], '')
        self.assertEqual(LogManager.load_log('2026-10-18')['心得备注'], '主动保留空计划')
        self.assertEqual(LogManager.load_log('2026-10-19')['计划与摘要'], '独立计划')

    def test_empty_nearest_source_blocks_an_older_nonempty_plan(self):
        self.save_plan('2026-10-17', '不应复活的旧计划')
        self.save_plan('2026-10-18', '')
        self.assertEqual(LogManager.load_log('2026-10-20')['计划与摘要'], '')
        self.assertEqual(len(self.stored_plans()), 2)

    def test_unchanged_inherited_text_is_frozen_only_when_saved(self):
        self.save_plan('2026-10-19', '原计划')
        displayed = LogManager.load_log('2026-10-21')
        self.assertEqual(displayed['计划与摘要'], '原计划')
        self.assertEqual(self.stored_plans(), [('2026-10-19', '原计划')])
        LogManager.save_log(displayed)
        self.save_plan('2026-10-19', '修改来源')
        self.assertEqual(LogManager.load_log('2026-10-20')['计划与摘要'], '修改来源')
        self.assertEqual(LogManager.load_log('2026-10-21')['计划与摘要'], '原计划')
        self.assertEqual(LogManager.load_log('2026-10-22')['计划与摘要'], '原计划')
        self.assertEqual(self.stored_plans(), [
            ('2026-10-19', '修改来源'), ('2026-10-21', '原计划')])

    def test_unsaved_display_and_edits_never_become_a_source(self):
        self.save_plan('2026-10-17', '已保存计划')
        draft = LogManager.load_log('2026-10-18')
        draft['计划与摘要'] = '尚未保存的草稿'
        self.assertEqual(LogManager.load_log('2026-10-19')['计划与摘要'], '已保存计划')
        self.save_plan('2026-10-17', '来源更新')
        self.assertEqual(LogManager.load_log('2026-10-18')['计划与摘要'], '来源更新')
        self.assertEqual(self.stored_plans(), [('2026-10-17', '来源更新')])

    def test_only_plan_summary_is_inherited_and_accounts_are_isolated(self):
        self.save_plan('2026-10-17', '甲计划', **{'上午': '甲当天记录'})
        with self.user('user1'):
            self.assertEqual(LogManager.load_log('2026-10-18')['计划与摘要'], '')
            self.save_plan('2026-10-17', '乙计划', **{'上午': '乙当天记录'})
            loaded = LogManager.load_log('2026-10-18')
            self.assertEqual(loaded['计划与摘要'], '乙计划')
            self.assertEqual(loaded['上午'], '')
        loaded = LogManager.load_log('2026-10-18')
        self.assertEqual(loaded['计划与摘要'], '甲计划')
        self.assertEqual(loaded['上午'], '')

    def test_existing_legacy_blank_row_is_not_reclassified_as_unsaved(self):
        self.save_plan('2026-10-17', '更早计划')
        with closing(sqlite3.connect(self.diary / 'diary.db')) as conn, conn:
            conn.execute('INSERT INTO logs (date) VALUES (?)', ('2026-10-18',))
        LogManager._initialized_paths.clear()
        self.assertEqual(LogManager.load_log('2026-10-18')['计划与摘要'], '')
        self.assertEqual(LogManager.load_log('2026-10-19')['计划与摘要'], '')
        self.assertEqual(self.stored_plans(), [('2026-10-17', '更早计划'), ('2026-10-18', '')])


if __name__ == '__main__':
    unittest.main()
