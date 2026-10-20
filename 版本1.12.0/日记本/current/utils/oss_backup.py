"""Daily online snapshots, owned and started by app.py (no second program)."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile

from utils.safe_files import atomic_write_json, data_file_lock

LOG = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
KEEP = 7
MIN_RETENTION = timedelta(days=7)
DEFAULTS = {
    'enabled': False,
    'bucket': 'your-backup-bucket',
    'region': 'cn-beijing',
    'endpoint': 'https://oss-cn-beijing.aliyuncs.com',
    'prefix': 'shenzhen-bay-diary/',
}


def china_now():
    return datetime.now(CHINA)


@contextmanager
def process_lock(path):
    """OS releases the lock even on crash; multiple local workers cannot upload twice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                pass
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def snapshot_database(source, destination):
    """Never copy a live .db or its WAL; SQLite handles concurrent transactions."""
    deadline = time.monotonic() + 120

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite snapshot timed out; source is untouched')

    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=1)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=64, progress=progress, sleep=0.05)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite snapshot failed integrity_check')


def core_files(root):
    if not root.is_dir():
        raise ValueError('user-data directory is missing')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('user-data must be a real directory')
    files = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        # Do not follow user-created links or include temporary files / SQLite sidecars.
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Links inside user-data are not supported')
        if any(part.startswith('.') for part in relative.parts) or not path.is_file():
            continue
        if path.suffix.lower() not in {'.db', '.sqlite', '.sqlite3', '.json', '.txt', '.xlsx'}:
            continue
        # Excel exports are derived data. Keep legacy source workbooks even before migration.
        if path.suffix.lower() == '.xlsx' and path.name.startswith(('日志导出', '日记导出')):
            continue
        files.append(path)
    if not files:
        raise ValueError('No core files; refusing an empty backup')
    return files


def create_snapshot(root, workdir):
    """Per-file consistent snapshots; no global lock across all users or network calls."""
    archive = workdir / 'snapshot.zip'
    manifest = {'format': 1, 'snapshot_started_at': china_now().isoformat(), 'files': []}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for index, source in enumerate(core_files(root)):
            name = 'user-data/' + source.relative_to(root).as_posix()
            if source.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
                copy = workdir / f'database-{index}.db'
                snapshot_database(source, copy)
                payload = copy.read_bytes()
            else:
                # Application text writers use this brief lock and atomic replacement.
                with data_file_lock:
                    before = source.stat()
                    payload = source.read_bytes()
                    after = source.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ValueError('File changed during snapshot; try next scheduled run')
                if source.suffix.lower() == '.json':
                    json.loads(payload)
            output.writestr(name, payload)
            manifest['files'].append({'path': name, 'size': len(payload),
                                      'sha256': hashlib.sha256(payload).hexdigest()})
        output.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None:
            raise ValueError('Snapshot ZIP failed validation')
    return archive


class OSSStore:
    def __init__(self, config):
        import alibabacloud_oss_v2 as oss
        missing = [name for name in ('OSS_ACCESS_KEY_ID', 'OSS_ACCESS_KEY_SECRET')
                   if not os.environ.get(name)]
        if missing:
            LOG.error('OSS credentials missing: %s; configure them in the server environment', ', '.join(missing))
            raise ValueError('Missing OSS credentials')
        self.oss = oss
        cfg = oss.config.load_default()
        cfg.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        cfg.region = config['region']
        cfg.endpoint = config['endpoint']
        cfg.signature_version = 'v4'
        cfg.connect_timeout = 30
        cfg.readwrite_timeout = 30
        cfg.retry_max_attempts = 3
        cfg.disable_upload_crc64_check = False
        cfg.disable_download_crc64_check = False
        self.client = oss.Client(cfg)
        self.bucket_name = config['bucket']
        self.prefix = config['prefix']

    def _head(self, key):
        return self.client.head_object(self.oss.HeadObjectRequest(bucket=self.bucket_name, key=key))

    def _download_hash(self, key):
        result = self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket_name, key=key))
        digest = hashlib.sha256()
        size = 0
        # V2 owns the download stream in result.body, not in the result itself.
        with closing(result.body) as body:
            for block in body.iter_bytes():
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    def upload_verified(self, archive, final_key):
        """Stage, download/hash-check, then publish one final snapshot object."""
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        temporary = self.prefix + '_uploading/' + uuid.uuid4().hex + '.zip'
        try:
            self.client.put_object_from_file(self.oss.PutObjectRequest(
                bucket=self.bucket_name, key=temporary, acl='private', storage_class='Standard',
                server_side_encryption='AES256', metadata={'sha256': digest},
                content_type='application/zip'), str(archive))
            remote_hash, downloaded = self._download_hash(temporary)
            if downloaded != archive.stat().st_size or remote_hash != digest:
                raise ValueError('OSS downloaded snapshot hash mismatch')
            staged = self._head(temporary)
            self.client.copy_object(self.oss.CopyObjectRequest(
                bucket=self.bucket_name, key=final_key,
                source_bucket=self.bucket_name, source_key=temporary,
                forbid_overwrite=True, metadata_directive='COPY',
                acl='private', server_side_encryption='AES256'))
            final = self._head(final_key)
            if (final.content_length != archive.stat().st_size
                    or (final.metadata or {}).get('sha256') != digest
                    or final.hash_crc64 is None or final.hash_crc64 != staged.hash_crc64):
                raise ValueError('OSS final snapshot verification failed')
        finally:
            # Never delete a retained snapshot here, even after a network error.
            try:
                self.delete(temporary)
            except Exception:
                LOG.warning('OSS temporary upload cleanup failed; retained versions untouched')

    def snapshot_keys(self):
        paginator = self.client.list_objects_v2_paginator()
        request = self.oss.ListObjectsV2Request(
            bucket=self.bucket_name, prefix=self.prefix + 'snapshots/')
        return [item.key for page in paginator.iter_page(request) for item in (page.contents or [])]

    def snapshot_created_at(self, key):
        """Use the final OSS object's publication time, not a filename or local mtime."""
        instant = self._head(key).last_modified
        if (not isinstance(instant, datetime) or instant.tzinfo is None
                or instant.utcoffset() is None):
            raise ValueError('Snapshot publication time is missing or invalid; refusing cleanup')
        return instant.astimezone(timezone.utc)

    def verify(self, key):
        """Before pruning, confirm a retained snapshot is readable and intact."""
        metadata = self._head(key)
        expected = (metadata.metadata or {}).get('sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Retained snapshot has no checksum; refusing cleanup')
        digest, size = self._download_hash(key)
        if size != metadata.content_length or digest != expected:
            raise ValueError('Retained snapshot failed verification; refusing cleanup')

    def delete(self, key):
        self.client.delete_object(self.oss.DeleteObjectRequest(bucket=self.bucket_name, key=key))


class BackupService:
    def __init__(self, project_root, config=None, store_factory=OSSStore, now=china_now):
        self.root = Path(project_root).resolve()
        self.config_error = False
        self.config = dict(DEFAULTS)
        try:
            config_path = self.root / 'oss_backup_config.json'
            if config is None and config_path.exists():
                config = json.loads(config_path.read_text(encoding='utf-8'))
            self.config.update(config or {})
            if not isinstance(self.config['enabled'], bool):
                raise ValueError('enabled must be true or false')
            for field in ('bucket', 'region', 'endpoint', 'prefix'):
                self.config[field] = os.environ.get('OSS_BACKUP_' + field.upper(), self.config[field])
            prefix = self.config['prefix'].strip('/')
            if not prefix or '..' in prefix or '\\' in prefix:
                raise ValueError('A dedicated backup prefix is required')
            self.config['prefix'] = prefix + '/'
            if not self.config['endpoint'].startswith('https://'):
                raise ValueError('HTTPS is required')
        except Exception:
            self.config_error = True
            LOG.error('Invalid oss_backup_config.json; backup disabled until corrected and restarted')
        identity = json.dumps({k: self.config[k] for k in ('bucket', 'region', 'endpoint', 'prefix')}, sort_keys=True)
        self.runtime = self.root / '.backup-runtime' / hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.state_path = self.runtime / 'state.json'
        self.store_factory = store_factory
        self.now = now
        self.stop_event = threading.Event()
        self.thread = None
        self.start_lock = threading.Lock()

    def _read_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self):
        state = self._read_state()
        last = state.get('last_success_at')
        try:
            instant = datetime.fromisoformat(last)
            age = (self.now() - instant).total_seconds()
            healthy = 0 <= age <= 30 * 3600
            formatted = instant.astimezone(CHINA).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            healthy, formatted, last = False, '暂无', None
        reason = '每天北京时间 04:00 备份；超过 30 小时未成功显示失败'
        if self.config_error or not self.config.get('enabled'):
            healthy = False
            reason = 'OSS 备份配置错误或已停用'
        return {'healthy': healthy, 'label': '备份成功' if healthy else '备份失败',
                'last_success_at': last, 'last_success_display': formatted, 'detail': reason}

    def run_due(self, force=False):
        """One attempt per China calendar day after 04:00; startup catches a missed slot."""
        if self.config_error or not self.config.get('enabled'):
            return False
        now = self.now().astimezone(CHINA)
        if now.hour < 4 and not force:
            return False
        with process_lock(self.runtime / 'backup.lock') as acquired:
            if not acquired:
                return False
            state = self._read_state()
            slot = now.date().isoformat()
            if not force and state.get('last_attempt_day') == slot:
                return False
            state['last_attempt_at'] = now.isoformat()
            # A manual verification before 04:00 must not consume the upcoming daily slot.
            if now.hour >= 4:
                state['last_attempt_day'] = slot
            # Persist before network work, so other workers/restarts do not duplicate the attempt.
            atomic_write_json(self.state_path, state)
            try:
                store = self.store_factory(self.config)
                with tempfile.TemporaryDirectory(prefix='snapshot-', dir=self.runtime) as work:
                    archive = create_snapshot(self.root / 'user-data', Path(work))
                    key = (self.config['prefix'] + 'snapshots/diary-'
                           + now.strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex + '.zip')
                    store.upload_verified(archive, key)
                state.update(last_success_at=self.now().isoformat(), last_success_key=key,
                             last_error=None, cleanup_error=None)
                atomic_write_json(self.state_path, state)
            except Exception as error:
                # Keep the previous verified success time; never prune after any failure above.
                state['last_error'] = type(error).__name__
                atomic_write_json(self.state_path, state)
                LOG.error('OSS backup failed (%s, code=%s, request_id=%s); old versions retained',
                          type(error).__name__, getattr(error, 'code', '-'),
                          getattr(error, 'request_id', '-'))
                return False
            try:
                pattern = re.compile(re.escape(self.config['prefix'])
                                     + r'snapshots/diary-\d{8}T\d{12}-[0-9a-f]{32}\.zip')
                keys = sorted({k for k in store.snapshot_keys() if pattern.fullmatch(k)})
                if key not in keys:
                    raise ValueError('New snapshot absent from listing; refusing cleanup')
                if len(keys) > KEEP:
                    cleanup_now = self.now()
                    if (not isinstance(cleanup_now, datetime) or cleanup_now.tzinfo is None
                            or cleanup_now.utcoffset() is None):
                        raise ValueError('Cleanup time must include a timezone')
                    cutoff = cleanup_now.astimezone(timezone.utc) - MIN_RETENTION
                    snapshots = []
                    for snapshot in keys:
                        created_at = store.snapshot_created_at(snapshot)
                        if (not isinstance(created_at, datetime) or created_at.tzinfo is None
                                or created_at.utcoffset() is None):
                            raise ValueError('Snapshot publication time is invalid; refusing cleanup')
                        snapshots.append((created_at.astimezone(timezone.utc), snapshot))
                    snapshots.sort()
                    removable = 0
                    # AND: leave at least seven versions, and protect every version
                    # aged seven days or less. Future timestamps are retained too.
                    while (len(snapshots) - removable > KEEP
                           and snapshots[removable][0] < cutoff):
                        removable += 1
                    expired = snapshots[:removable]
                    if any(snapshot == key for _, snapshot in expired):
                        raise ValueError('New snapshot has an invalid old publication time')
                    if expired:
                        # Validate the complete retained set before the first delete.
                        for _, kept in snapshots[removable:]:
                            store.verify(kept)
                        remaining = len(snapshots)
                        for _, old in expired:
                            store.delete(old)
                            remaining -= 1
                            LOG.info('OSS retention deleted expired snapshot %s; %d versions remain',
                                     old, remaining)
            except Exception as error:
                state['cleanup_error'] = type(error).__name__
                atomic_write_json(self.state_path, state)
                LOG.warning('OSS snapshot succeeded but pruning failed (%s); extra versions kept',
                            type(error).__name__)
            LOG.info('OSS snapshot verified successfully at %s', state['last_success_at'])
            return True

    def start(self):
        with self.start_lock:
            if self.thread is not None and self.thread.is_alive():
                return
            if self.config_error or not self.config.get('enabled'):
                return
            self.thread = threading.Thread(target=self._loop, name='oss-daily-backup', daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.run_due()
            except Exception as error:
                LOG.error('OSS scheduler error (%s); diary service continues', type(error).__name__)
            self.stop_event.wait(15)
