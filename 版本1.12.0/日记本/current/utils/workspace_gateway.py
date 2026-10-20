"""日记本管理下属工作区：独立进程、稳定身份、按登录用户转发。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlsplit

from flask import Response, request
import requests


HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}


def persistent_secret(path):
    """首次生成并持久保存，已有部署不会因重启更换秘密。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(secrets.token_urlsafe(48))
    value = path.read_text(encoding='utf-8').strip()
    if len(value) < 32:
        raise RuntimeError(f'服务秘密文件损坏，请检查 {path.name}')
    return value


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('工作区正在由另一个日记本进程接入，请稍后重试') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkspaceGateway:
    def __init__(self, config_path, runtime):
        self.config_path = Path(config_path)
        self.runtime = Path(runtime)
        self.worker = None
        self._lock = threading.Lock()

    def deployment(self):
        config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        parsed = urlsplit(config['origin'])
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.username or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not parsed.port):
            raise ValueError('工作区 origin 必须是 http://127.0.0.1:<端口>')
        return {**config, 'origin': config['origin'].rstrip('/'), 'port': parsed.port,
                'entrypoint': (self.config_path.parent / config['entrypoint']).resolve(),
                'python': str((self.config_path.parent / config['python']).resolve())
                if config.get('python') else sys.executable}

    def probe(self, config, token):
        try:
            with socket.create_connection(('127.0.0.1', config['port']), timeout=0.4):
                pass
        except OSError:
            try:
                with socket.socket() as reservation:
                    if os.name == 'nt':
                        reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    reservation.bind(('127.0.0.1', config['port']))
                return 'absent'
            except OSError:
                return 'unverified'
        try:
            with requests.Session() as client:
                client.trust_env = False
                with client.get(config['origin'] + '/__service__/identity',
                                headers={'X-SZD-Gateway-Token': token, 'Accept-Encoding': 'identity'},
                                timeout=(1, 2), allow_redirects=False, stream=True) as response:
                    body = response.raw.read(8193, decode_content=False)
                    if len(body) > 8192:
                        return 'unverified'
                    record = json.loads(body)
                    if (not isinstance(record, dict)
                            or record.get('service_id') != config['expected_service_id']
                            or type(record.get('identity_protocol')) is not int
                            or record['identity_protocol'] != 1
                            or record.get('service_role') != 'workspace'
                            or not isinstance(record.get('capabilities'), list)
                            or 'gateway-user-v1' not in record['capabilities']
                            or record.get('gateway_authenticated') is not True):
                        return 'conflict'
                    if response.status_code == 200 and record.get('ready') is True:
                        return 'ready'
                    if response.status_code == 503 and record.get('ready') is False:
                        return 'starting'
        except Exception:
            return 'unverified'
        return 'unverified'

    def ensure_ready(self, allow_start=False, timeout=30):
        with self._lock:
            config = self.deployment()
            token = persistent_secret(self.runtime / 'cognitive.token')
            state = self.probe(config, token)
            if state == 'ready':
                return config, token
            if state not in ('absent', 'starting') or (state == 'absent' and not allow_start):
                raise RuntimeError('认知工作区未就绪或身份不匹配；请检查子服务配置与日志')
            with process_lock(self.runtime / 'manager.lock'):
                state = self.probe(config, token)
                spawned = False
                confirmed = False
                try:
                    if state == 'absent' and allow_start:
                        if not config['entrypoint'].is_file():
                            raise RuntimeError('找不到认知工作区启动入口')
                        options = ({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
                                   else {'start_new_session': True})
                        with (self.runtime / 'cognitive.log').open('ab', buffering=0) as output:
                            self.worker = subprocess.Popen(
                                [config['python'], str(config['entrypoint']), '--host', '127.0.0.1',
                                 '--port', str(config['port']), '--gateway-token-file',
                                 str((self.runtime / 'cognitive.token').resolve())],
                                cwd=str(config['entrypoint'].parent),
                                env=dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8'),
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                close_fds=True, **options)
                        spawned = True
                    elif state not in ('ready', 'starting'):
                        raise RuntimeError('认知工作区端口被占用或身份无法确认')
                    deadline = time.monotonic() + timeout
                    while True:
                        state = self.probe(config, token)
                        if state == 'ready':
                            confirmed = True
                            return config, token
                        if state == 'conflict' or (state == 'unverified' and not spawned):
                            raise RuntimeError('认知工作区身份或部署凭证不匹配')
                        if spawned and self.worker.poll() is not None:
                            raise RuntimeError('认知工作区启动失败，请检查 cognitive.log')
                        if time.monotonic() >= deadline:
                            raise RuntimeError('等待认知工作区就绪超时')
                        time.sleep(0.15)
                finally:
                    # 已有服务和已确认的子服务保持独立运行，只回收本次失败的启动。
                    if spawned and not confirmed and self.worker.poll() is None:
                        self.worker.terminate()
                        try:
                            self.worker.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.worker.kill()
                            self.worker.wait(timeout=5)

    def forward(self, subpath, user_id):
        # 网关验证浏览器来源后才去掉 Origin，子服务仅接受部署凭证。
        origin = request.headers.get('Origin')
        if (request.headers.get('Sec-Fetch-Site') == 'cross-site'
                or (origin and (urlsplit(origin).scheme not in ('http', 'https')
                                or urlsplit(origin).netloc.lower() != request.host.lower()))):
            return Response('不接受跨站请求', status=403, mimetype='text/plain')
        try:
            config, token = self.ensure_ready(allow_start=request.method in ('GET', 'HEAD')
                                               and subpath in ('', 'index.html'))
        except (RuntimeError, OSError, ValueError, KeyError):
            return Response('认知工作区暂不可用。请检查子服务配置与 cognitive.log；可返回日记本继续使用。',
                            status=503, mimetype='text/plain')
        excluded = HOP_HEADERS | {'host', 'cookie', 'authorization', 'content-length', 'origin', 'forwarded'}
        excluded.update(h.strip().lower() for h in request.headers.get('Connection', '').split(','))
        headers = {key: value for key, value in request.headers
                   if key.lower() not in excluded
                   and not key.lower().startswith(('x-forwarded-', 'x-szd-'))}
        headers.update({'X-SZD-Gateway-Token': token, 'X-SZD-User': user_id,
                        'X-SZD-Prefix': request.script_root + '/cognitive'})
        query = request.query_string.decode('latin-1')
        url = config['origin'] + '/' + quote(subpath, safe='/') + (('?' + query) if query else '')
        client = requests.Session()
        client.trust_env = False
        try:
            upstream = client.request(request.method, url, headers=headers, data=request.get_data(),
                                      stream=True, allow_redirects=False, timeout=(3, 300))
        except requests.Timeout:
            client.close()
            return Response('认知工作区响应超时', status=504, mimetype='text/plain')
        except requests.RequestException:
            client.close()
            return Response('认知工作区连接失败', status=502, mimetype='text/plain')
        excluded = HOP_HEADERS | {'set-cookie'}
        excluded.update(h.strip().lower() for h in upstream.headers.get('Connection', '').split(','))
        response_headers = [(key, value) for key, value in upstream.raw.headers.items()
                            if key.lower() not in excluded]

        def close():
            upstream.close()
            client.close()

        if upstream._content_consumed:
            response_headers = [(k, v) for k, v in response_headers
                                if k.lower() not in {'content-length', 'content-encoding'}]
            payload = upstream.content
            close()
            return Response(payload, status=upstream.status_code, headers=response_headers)

        def stream():
            try:
                if upstream.headers.get('Content-Type', '').startswith('text/event-stream'):
                    read = getattr(upstream.raw, 'read1', None)
                    while True:
                        # urllib3 1.x 没有 read1；逐字节回退避免等满缓冲区才发出首个事件。
                        chunk = (read(64 * 1024, decode_content=False) if read
                                 else upstream.raw.read(1, decode_content=False))
                        if not chunk:
                            break
                        yield chunk
                else:
                    yield from upstream.raw.stream(64 * 1024, decode_content=False)
            finally:
                close()

        response = Response(stream(), status=upstream.status_code, headers=response_headers)
        response.call_on_close(close)
        return response
