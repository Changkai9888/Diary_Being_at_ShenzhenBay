import hashlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape
import zipfile

import alibabacloud_oss_v2 as oss
import requests

from utils.oss_backup import (BackupService, CHINA, OSSStore, create_snapshot,
                              process_lock, snapshot_database)
from utils.safe_files import atomic_write_json, atomic_write_text


class MemoryStore:
    def __init__(self, now):
        self.now = now
        self.objects = {}
        self.created_at = {}
        self.metadata_reads = []
        self.verified = []
        self.deleted = []
        self.fail_upload = False
        self.fail_verify = False
        self.fail_delete = False
        self.after_upload = None

    def upload_verified(self, archive, key):
        if self.fail_upload:
            raise OSError('offline')
        self.objects[key] = archive.read_bytes()
        self.created_at[key] = self.now()
        if self.after_upload:
            self.after_upload()

    def snapshot_keys(self):
        return list(self.objects)

    def snapshot_created_at(self, key):
        self.metadata_reads.append(key)
        return self.created_at[key]

    def verify(self, key):
        self.verified.append(key)
        if self.fail_verify:
            raise ValueError('corrupt retained snapshot')
        with zipfile.ZipFile(io.BytesIO(self.objects[key])) as archive:
            assert archive.testzip() is None

    def delete(self, key):
        if self.fail_delete:
            raise OSError('delete denied')
        self.deleted.append(key)
        del self.objects[key]
        self.created_at.pop(key, None)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'user-data'
        self.data.mkdir()
        atomic_write_json(self.data / 'user_map.json', {'user0': 'test'})
        self.now = datetime(2026, 9, 1, 4, 0, tzinfo=CHINA)
        self.store = MemoryStore(now=lambda: self.now)
        self.service = BackupService(self.root, config={'enabled': True}, store_factory=lambda c: self.store,
                                     now=lambda: self.now)

    def test_schedule_restart_and_failed_attempt_are_once_per_day(self):
        self.now = self.now.replace(hour=3, minute=59)
        self.assertFalse(self.service.run_due())
        self.now = self.now.replace(hour=4, minute=0)
        self.assertTrue(self.service.run_due())
        restarted = BackupService(self.root, config={'enabled': True}, store_factory=lambda c: self.store,
                                  now=lambda: self.now)
        self.assertFalse(restarted.run_due())
        self.now += timedelta(days=1)
        self.store.fail_upload = True
        self.assertFalse(restarted.run_due())
        self.store.fail_upload = False
        self.assertFalse(restarted.run_due())
        self.assertEqual(len(self.store.objects), 1)

    def test_retention_minimum_seven_and_failure_preserves_everything(self):
        for _ in range(7):
            self.assertTrue(self.service.run_due())
            self.now += timedelta(days=1)
        previous = set(self.store.objects)
        last = self.service.status()['last_success_at']
        self.store.fail_upload = True
        self.assertFalse(self.service.run_due())
        self.assertEqual(set(self.store.objects), previous)
        self.assertEqual(self.service.status()['last_success_at'], last)
        self.assertEqual(self.store.deleted, [])
        self.assertEqual(self.store.metadata_reads, [])
        self.now += timedelta(days=2)
        self.store.fail_upload = False
        self.store.objects['shenzhen-bay-diary/snapshots/unrelated.zip'] = b'do not delete'
        self.assertTrue(self.service.run_due())
        self.assertEqual(self.store.deleted, [min(previous)])
        self.assertEqual(len(self.store.objects), 8)  # seven snapshots + unrelated object

    def test_manual_backup_before_four_does_not_skip_daily_slot(self):
        self.now = self.now.replace(hour=3)
        self.assertTrue(self.service.run_due(force=True))
        self.now = self.now.replace(hour=4)
        self.assertTrue(self.service.run_due())
        self.assertEqual(len(self.store.objects), 2)

    def test_cleanup_failure_preserves_success_time_and_extra_versions(self):
        for _ in range(7):
            self.service.run_due()
            self.now += timedelta(days=1)
        self.now += timedelta(seconds=1)
        self.store.fail_delete = True
        self.assertTrue(self.service.run_due())
        self.assertEqual(len(self.store.objects), 8)
        self.assertTrue(self.service.status()['healthy'])
        self.assertEqual(self.service._read_state()['cleanup_error'], 'OSError')

    def test_corrupt_retained_version_blocks_all_deletions(self):
        for _ in range(7):
            self.service.run_due()
            self.now += timedelta(days=1)
        self.now += timedelta(seconds=1)
        self.store.fail_verify = True
        self.assertTrue(self.service.run_due())
        self.assertEqual(len(self.store.objects), 8)
        self.assertFalse(self.store.deleted)

    def seed_snapshots(self, ages):
        """Use deliberately reverse key order to make timestamp ordering observable."""
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('user-data/note.txt', 'offline retention fixture')
        keys = []
        for index, age in enumerate(ages):
            key = ('shenzhen-bay-diary/snapshots/diary-20260901T040000000000-'
                   f'{len(ages) - index:032x}.zip')
            self.store.objects[key] = payload.getvalue()
            self.store.created_at[key] = self.now - age
            keys.append(key)
        return keys

    def test_twenty_same_day_successes_are_all_retained(self):
        for _ in range(20):
            self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(len(self.store.objects), 20)
        self.assertFalse(self.store.deleted)
        self.assertFalse(self.store.verified)

    def test_exact_seven_days_is_retained_until_one_second_later(self):
        keys = self.seed_snapshots([timedelta(days=7)] * 7)
        self.assertTrue(self.service.run_due(force=True))
        self.assertFalse(self.store.deleted)
        self.assertEqual(len(self.store.objects), 8)
        self.now += timedelta(seconds=1)
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(self.store.deleted, sorted(keys)[:2])
        self.assertEqual(len(self.store.objects), 7)

    def test_many_old_versions_are_pruned_by_publication_time_and_stop_at_age_boundary(self):
        keys = self.seed_snapshots([timedelta(days=n) for n in range(20, 0, -1)])
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(self.store.deleted, keys[:13])
        self.assertEqual(len(self.store.objects), 8)  # Seven-day boundary stays protected.
        self.assertTrue(set(keys[13:]).issubset(self.store.objects))

    def test_all_old_versions_are_pruned_only_until_seven_remain(self):
        keys = self.seed_snapshots([timedelta(days=n) for n in range(30, 10, -1)])
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(self.store.deleted, keys[:14])
        self.assertEqual(len(self.store.objects), 7)

    def test_fewer_than_or_exactly_seven_versions_remain_even_when_old(self):
        self.seed_snapshots([timedelta(days=30)] * 6)
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(len(self.store.objects), 7)
        self.assertFalse(self.store.metadata_reads)
        self.assertFalse(self.store.deleted)
        newest = self.service._read_state()['last_success_key']
        self.store.objects.pop(newest)
        self.store.created_at.pop(newest)
        self.store.objects.pop(next(iter(self.store.objects)))
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(len(self.store.objects), 6)
        self.assertFalse(self.store.deleted)

    def test_mixed_versions_keep_all_in_window_and_verify_every_retained_version(self):
        old = self.seed_snapshots([timedelta(days=n) for n in range(20, 17, -1)])
        new = []
        for _ in range(12):
            self.assertTrue(self.service.run_due(force=True))
            new.append(self.service._read_state()['last_success_key'])
        self.assertEqual(set(self.store.objects), set(new))
        self.assertEqual(self.store.deleted, old)
        # The first old-version cleanup leaves seven versions; a subsequent cleanup
        # must also validate every retained object when that set grows beyond seven.
        self.store.deleted.clear()
        self.store.verified.clear()
        old_key = old[0]
        self.store.objects[old_key] = self.store.objects[new[0]]
        self.store.created_at[old_key] = self.now - timedelta(days=20)
        self.assertTrue(self.service.run_due(force=True))
        retained = set(self.store.objects)
        self.assertEqual(self.store.deleted, [old_key])
        self.assertEqual(set(self.store.verified), retained)
        self.assertGreater(len(retained), 7)

    def test_invalid_or_missing_publication_time_blocks_all_deletions(self):
        keys = self.seed_snapshots([timedelta(days=30)] * 8)
        for invalid in (None, '2026-01-01T00:00:00Z', datetime(2026, 1, 1)):
            with self.subTest(invalid=invalid):
                self.store.created_at[keys[0]] = invalid
                self.assertTrue(self.service.run_due(force=True))
                self.assertFalse(self.store.deleted)
                self.assertFalse(self.store.verified)
                self.assertEqual(self.service._read_state()['cleanup_error'], 'ValueError')
        self.store.created_at.pop(keys[0])
        self.assertTrue(self.service.run_due(force=True))
        self.assertFalse(self.store.deleted)
        self.assertEqual(self.service._read_state()['cleanup_error'], 'KeyError')

    def test_corrupt_young_version_blocks_deletion_even_with_more_than_seven_retained(self):
        keys = self.seed_snapshots([timedelta(days=30)] * 2 + [timedelta(hours=1)] * 9)
        self.store.objects[keys[-1]] = b'corrupt retained archive'
        self.assertTrue(self.service.run_due(force=True))
        self.assertFalse(self.store.deleted)
        self.assertIn(keys[-1], self.store.verified)
        self.assertEqual(len(self.store.objects), 12)
        self.assertEqual(self.service._read_state()['cleanup_error'], 'BadZipFile')

    def test_future_publication_time_is_retained_while_other_old_versions_can_expire(self):
        keys = self.seed_snapshots([timedelta(days=30)] * 8 + [timedelta(seconds=-30)])
        self.assertTrue(self.service.run_due(force=True))
        self.assertEqual(len(self.store.objects), 7)
        self.assertIn(keys[-1], self.store.objects)
        self.assertEqual(self.store.deleted, sorted(keys[:8])[:3])

    def test_unrelated_and_temporary_objects_never_count_towards_retention(self):
        self.seed_snapshots([timedelta(days=30)] * 6)
        foreign = ['shenzhen-bay-diary/snapshots/unrelated.zip',
                   'shenzhen-bay-diary/_uploading/partial.zip',
                   'other-project/snapshots/diary-20260901T040000000000-' + 'a' * 32 + '.zip']
        for key in foreign:
            self.store.objects[key] = b'never inspect or delete'
        self.assertTrue(self.service.run_due(force=True))
        self.assertFalse(self.store.metadata_reads)
        self.assertFalse(self.store.deleted)
        self.assertTrue(set(foreign).issubset(self.store.objects))

    def test_upload_failure_never_reads_metadata_or_deletes_old_versions(self):
        self.seed_snapshots([timedelta(days=30)] * 10)
        previous = dict(self.store.objects)
        self.store.fail_upload = True
        self.assertFalse(self.service.run_due(force=True))
        self.assertEqual(self.store.objects, previous)
        self.assertFalse(self.store.metadata_reads)
        self.assertFalse(self.store.verified)
        self.assertFalse(self.store.deleted)

    def test_success_timestamp_is_completion_and_staleness_boundary(self):
        self.assertFalse(self.service.status()['healthy'])
        self.store.after_upload = lambda: setattr(self, 'now', self.now + timedelta(minutes=8))
        self.service.run_due()
        self.assertEqual(self.service.status()['last_success_at'], self.now.isoformat())
        self.now += timedelta(hours=30)
        self.assertTrue(self.service.status()['healthy'])
        self.now += timedelta(seconds=1)
        self.assertFalse(self.service.status()['healthy'])
        self.assertIn('04:08:00', self.service.status()['last_success_display'])

    def test_single_instance_lock(self):
        with process_lock(self.service.runtime / 'backup.lock') as acquired:
            self.assertTrue(acquired)
            self.assertFalse(self.service.run_due())
        self.assertTrue(self.service.run_due())

    def test_changed_destination_does_not_reuse_success_status(self):
        self.service.run_due()
        other = BackupService(self.root, config={'prefix': 'different/'}, now=lambda: self.now)
        self.assertFalse(other.status()['healthy'])

    def test_empty_or_corrupt_source_never_uploads(self):
        (self.data / 'user_map.json').unlink()
        self.assertFalse(self.service.run_due(force=True))
        (self.data / 'diary.db').write_bytes(b'broken database')
        self.assertFalse(self.service.run_due(force=True))
        self.assertFalse(self.store.objects)

    def test_wal_snapshot_restore_manifest_and_exclusions(self):
        diary = self.data / 'user0' / 'diary-data'
        diary.mkdir(parents=True)
        db = diary / 'diary.db'
        connection = sqlite3.connect(db)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE notes (body TEXT)')
        connection.execute('INSERT INTO notes VALUES (?)', ('已提交的日记',))
        connection.commit()
        connection.execute('INSERT INTO notes VALUES (?)', ('尚未提交',))
        atomic_write_text(diary / '待提升属性.txt', '完整内容')
        (diary / '日志导出_20260929.xlsx').write_bytes(b'generated')
        (diary / '2025.xlsx').write_bytes(b'legacy source')
        (diary / '.atomic-partial.tmp').write_bytes(b'partial')
        (self.root / 'music.mp3').write_bytes(b'excluded')
        with tempfile.TemporaryDirectory() as work:
            archive = create_snapshot(self.data, Path(work))
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                self.assertIn('user-data/user0/diary-data/2025.xlsx', names)
                self.assertFalse(any(n.endswith(('-wal', '-shm', '.tmp')) or '日志导出' in n for n in names))
                manifest = json.loads(z.read('manifest.json'))
                for item in manifest['files']:
                    self.assertEqual(hashlib.sha256(z.read(item['path'])).hexdigest(), item['sha256'])
                restored = Path(work) / 'restored.db'
                restored.write_bytes(z.read('user-data/user0/diary-data/diary.db'))
            with closing(sqlite3.connect(restored)) as check:
                self.assertEqual(check.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                self.assertEqual(check.execute('SELECT body FROM notes').fetchall(), [('已提交的日记',)])
        connection.rollback()
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM notes').fetchone(), (1,))

    def test_online_snapshot_allows_concurrent_writer(self):
        db = self.data / 'diary.db'
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE pairs (a INTEGER, b INTEGER)')
            conn.executemany('INSERT INTO pairs VALUES (?, ?)', [(n, n) for n in range(20000)])
        stop = threading.Event()
        started = threading.Event()
        errors = []

        def writer():
            try:
                with closing(sqlite3.connect(db, timeout=2)) as conn:
                    while not stop.is_set():
                        conn.execute('UPDATE pairs SET a=a+1, b=b+1 WHERE rowid <= 20')
                        conn.commit()
                        started.set()
                        stop.wait(0.002)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=writer)
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            snapshot_database(db, self.root / 'copy.db')
        finally:
            stop.set()
            worker.join(3)
        self.assertFalse(errors)
        with closing(sqlite3.connect(self.root / 'copy.db')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pairs WHERE a != b').fetchone(), (0,))
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))


class TrackedResponse(requests.Response):
    was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class FakeOSSHTTP:
    """Real V2 SDK, signing, serialization, CRC and pagination; no network or real keys."""
    def __init__(self, now):
        self.now = now
        self.objects = {}
        self.events = []
        self.corrupt = False
        self.bad_final_head = False
        self.upload_failures = 0
        self.list_calls = 0
        self.responses = []

    @staticmethod
    def object_headers(payload, headers):
        crc = oss.crc.Crc64(0)
        crc.update(payload)
        return {**headers, 'Content-Length': str(len(payload)),
                'x-oss-hash-crc64ecma': str(crc.sum64()), 'ETag': '"test-etag"'}

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        key = unquote(parsed.path.lstrip('/'))
        query = parse_qs(parsed.query)
        headers = requests.structures.CaseInsensitiveDict(kwargs['headers'])
        assert headers['Authorization'].startswith('OSS4-HMAC-SHA256 ')
        assert kwargs['timeout'] == (30, 30)
        assert kwargs['verify'] is True
        payload = b''
        response_headers = {}
        status = 200
        if method == 'PUT' and 'x-oss-copy-source' not in headers:
            if self.upload_failures:
                self.upload_failures -= 1
                raise requests.ConnectionError('simulated temporary disconnect')
            self.events.append(('upload', key))
            data = kwargs['data']
            content = data.read() if hasattr(data, 'read') else b''.join(data)
            assert headers['x-oss-object-acl'] == 'private'
            assert headers['x-oss-server-side-encryption'] == 'AES256'
            assert headers['x-oss-storage-class'] == 'Standard'
            assert headers['x-oss-meta-sha256'] == hashlib.sha256(content).hexdigest()
            stored_headers = {'x-oss-meta-sha256': headers['x-oss-meta-sha256'],
                              'Last-Modified': format_datetime(self.now().astimezone(timezone.utc), usegmt=True)}
            self.objects[key] = (content, stored_headers)
            response_headers = self.object_headers(content, stored_headers)
        elif method == 'PUT':
            self.events.append(('promote', key))
            assert headers['x-oss-forbid-overwrite'] == 'true'
            assert headers['x-oss-metadata-directive'] == 'COPY'
            assert headers['x-oss-object-acl'] == 'private'
            assert headers['x-oss-server-side-encryption'] == 'AES256'
            source = unquote(headers['x-oss-copy-source']).lstrip('/').split('/', 1)[1]
            content, source_headers = self.objects[source]
            self.objects[key] = (content, {**source_headers,
                'Last-Modified': format_datetime(self.now().astimezone(timezone.utc), usegmt=True)})
            modified = self.now().astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            payload = (f'<CopyObjectResult><LastModified>{modified}</LastModified>'
                       '<ETag>"test-etag"</ETag></CopyObjectResult>').encode()
        elif method == 'GET' and query.get('list-type') == ['2']:
            self.list_calls += 1
            keys = sorted(k for k in self.objects if k.startswith(query['prefix'][0]))
            offset = int(query.get('continuation-token', ['0'])[0])
            selected = keys[offset:offset + 2]
            more = offset + 2 < len(keys)
            content_xml = ''.join(f'<Contents><Key>{escape(k)}</Key><Size>12</Size>'
                                  '<LastModified>2026-09-29T04:00:00.000Z</LastModified>'
                                  '</Contents>' for k in selected)
            next_xml = f'<NextContinuationToken>{offset + 2}</NextContinuationToken>' if more else ''
            payload = (f'<ListBucketResult><IsTruncated>{str(more).lower()}</IsTruncated>'
                       f'{next_xml}{content_xml}</ListBucketResult>').encode()
        elif method in ('GET', 'HEAD'):
            content, stored_headers = self.objects[key]
            response_headers = self.object_headers(content, stored_headers)
            if method == 'GET':
                self.events.append(('download', key))
                payload = b'corrupt' if self.corrupt else content
            elif self.bad_final_head and 'snapshots/' in key:
                response_headers['x-oss-hash-crc64ecma'] = '1'
        elif method == 'DELETE':
            self.events.append(('delete', key))
            self.objects.pop(key, None)
            status = 204
        else:
            raise AssertionError(f'Unexpected SDK request: {method} {url}')
        response = TrackedResponse()
        response.status_code = status
        response.url = url
        response.reason = 'OK'
        response.headers.update(response_headers)
        response.headers['x-oss-request-id'] = 'offline-test-request'
        response.raw = io.BytesIO(payload)
        self.responses.append(response)
        return response


class OSSVerificationTests(unittest.TestCase):
    def setUp(self):
        credentials = patch.dict(os.environ, {'OSS_ACCESS_KEY_ID': 'test-id',
                                              'OSS_ACCESS_KEY_SECRET': 'test-secret',
                                              'OSS_SESSION_TOKEN': ''})
        credentials.start()
        self.addCleanup(credentials.stop)
        self.now = datetime(2026, 9, 1, 4, tzinfo=CHINA)
        self.http = FakeOSSHTTP(now=lambda: self.now)
        network = patch('requests.sessions.Session.request', side_effect=self.http.request)
        network.start()
        self.addCleanup(network.stop)
        self.store = OSSStore({'bucket': 'test-bucket', 'region': 'cn-beijing',
                               'endpoint': 'https://oss-cn-beijing.aliyuncs.com',
                               'prefix': 'shenzhen-bay-diary/'})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / 'snapshot.zip'
        self.archive.write_bytes(b'test payload')
        self.key = 'shenzhen-bay-diary/snapshots/test.zip'

    def test_download_verified_before_promotion(self):
        self.store.upload_verified(self.archive, self.key)
        events = [event[0] for event in self.http.events]
        self.assertEqual(events, ['upload', 'download', 'promote', 'delete'])
        self.assertEqual(list(self.http.objects), [self.key])
        self.store.verify(self.key)
        self.assertTrue(all(r.was_closed for r in self.http.responses))

    def test_corrupt_upload_is_never_promoted(self):
        self.http.corrupt = True
        with self.assertRaises((ValueError, oss.exceptions.BaseError)):
            self.store.upload_verified(self.archive, self.key)
        self.assertFalse(self.http.objects)
        self.assertNotIn('promote', [e[0] for e in self.http.events])

    def test_final_crc_failure_is_not_reported_as_success(self):
        self.http.bad_final_head = True
        with self.assertRaises(ValueError):
            self.store.upload_verified(self.archive, self.key)

    def test_sdk_paginates_all_versions_and_limits_prefix(self):
        for index in range(9):
            self.http.objects[f'shenzhen-bay-diary/snapshots/{index}.zip'] = (b'test', {})
        self.http.objects['other-project/never-delete.zip'] = (b'test', {})
        self.http.objects['shenzhen-bay-diary/_uploading/partial.zip'] = (b'test', {})
        self.assertEqual(len(self.store.snapshot_keys()), 9)
        self.assertEqual(self.http.list_calls, 5)

    def test_sdk_lists_empty_prefix(self):
        self.assertEqual(self.store.snapshot_keys(), [])

    def test_snapshot_created_at_uses_final_object_head_publication_time(self):
        self.store.upload_verified(self.archive, self.key)
        expected = self.now.astimezone(timezone.utc)
        self.now += timedelta(days=30)
        self.assertEqual(self.store.snapshot_created_at(self.key), expected)

    def test_snapshot_created_at_missing_or_invalid_head_fails_closed(self):
        for modified in (None, 'invalid-date'):
            with self.subTest(modified=modified):
                headers = {} if modified is None else {'Last-Modified': modified}
                self.http.objects[self.key] = (b'test', headers)
                with self.assertRaises((ValueError, oss.exceptions.BaseError)):
                    self.store.snapshot_created_at(self.key)

    def test_sdk_retries_temporary_disconnect_before_upload(self):
        self.http.upload_failures = 1
        self.store.upload_verified(self.archive, self.key)
        self.assertEqual(list(self.http.objects), [self.key])

    def test_real_sdk_retention_keeps_seven_day_window_and_preserves_success_on_failure(self):
        root = Path(self.temp.name)
        atomic_write_json(root / 'user-data' / 'user_map.json', {'user0': 'test'})
        service = BackupService(root, config={'enabled': True}, store_factory=lambda c: self.store, now=lambda: self.now)
        for _ in range(8):
            self.assertTrue(service.run_due())
            self.now += timedelta(days=1)
        self.assertEqual(len(self.http.objects), 8)  # Exactly seven days old is protected.
        self.now += timedelta(seconds=1)
        self.assertTrue(service.run_due())
        previous = set(self.http.objects)
        self.assertEqual(len(previous), 7)
        last = service.status()['last_success_at']
        self.http.corrupt = True
        self.now += timedelta(days=1)
        self.assertFalse(service.run_due())
        self.assertEqual(set(self.http.objects), previous)
        self.assertEqual(service.status()['last_success_at'], last)


class WebTests(unittest.TestCase):
    def test_relocated_project_works_from_an_unrelated_working_directory(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as work:
            relocated = Path(work) / 'relocated-project'
            relocated.mkdir()
            shutil.copy2(source / 'app.py', relocated / 'app.py')
            shutil.copytree(source.parent / 'identity', relocated.parent / 'identity')
            for package in ('auth', 'utils'):
                (relocated / package).mkdir()
                for module in (source / package).glob('*.py'):
                    shutil.copy2(module, relocated / package / module.name)
            unrelated = Path(work) / 'unrelated-cwd'
            unrelated.mkdir()
            code = '''
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import app
from utils.excel_manager import DATA_DIR, LEGACY_DATA_DIR
assert Path(app.PROJECT_ROOT) == root
storage = root / 'data'
assert Path(app.auth.file_path) == storage / 'auth' / 'users.json'
assert Path(app.USER_DATA_ROOT) == storage / 'user-data'
assert app.backup_service.root == storage
assert Path(DATA_DIR) == storage / 'user-data' / 'user0' / 'diary-data'
assert Path(LEGACY_DATA_DIR) == storage / 'data'
assert Path(app.app.config['DATA_PATH']).is_relative_to(storage)
'''
            environment = dict(os.environ)
            environment.pop('DIARY_DATA_ROOT', None)
            environment.pop('DIARY_IDENTITY_DIR', None)
            result = subprocess.run([sys.executable, '-c', code, str(relocated)],
                                    cwd=unrelated, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((relocated / 'data' / 'auth' / 'users.json').is_file())
            self.assertEqual(list(unrelated.iterdir()), [])

    def test_status_auth_and_render_without_touching_user_files(self):
        import app as web
        web.app.config['TESTING'] = True
        status = {'healthy': False, 'label': '备份失败', 'last_success_at': None,
                  'last_success_display': '暂无', 'detail': 'test'}
        with patch.object(web.backup_service, 'status', return_value=status), \
                patch.object(web, '_setup_current_user_paths'):
            client = web.app.test_client()
            self.assertEqual(client.get('/backup_status').status_code, 401)
            with client.session_transaction(path='/') as session:
                session['authenticated'] = True
                session['username'] = 'test'
                session['expires_at'] = time.time() + 3600
            response = client.get('/backup_status')
            self.assertEqual(response.json, status)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            page = client.get('/').get_data(as_text=True)
            self.assertIn('backup-failed', page)
            self.assertIn('上次成功', page)
            self.assertLess(page.index('id="exportExcelBtn"'), page.index('id="backupStatus"'))


if __name__ == '__main__':
    unittest.main()
