"""Local cognitive workspace server. Run: python cognitive_workspace/server.py"""
import argparse
import json
import os
import re
from types import SimpleNamespace
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import UUID

if __package__:
    from .backend.engine import Engine
    from .backend.provider import DeepSeekProvider
    from .backend.store import Store
else:
    from backend.engine import Engine
    from backend.provider import DeepSeekProvider
    from backend.store import Store

ROOT = Path(__file__).resolve().parent
IDENTITY_FILE = ROOT.parent / "identity" / "service.json"
USER_ID = re.compile(r"user(?:0|[1-9][0-9]{0,8})\Z")
MOUNT_PREFIX = re.compile(r"/(?:[A-Za-z0-9_-]+/)*cognitive\Z")


def loopback_host(value):
    if value not in ("127.0.0.1", "localhost"):
        raise argparse.ArgumentTypeError("host must be 127.0.0.1 or localhost")
    return value
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}


class DataLock:
    """Prevent two server processes sharing the same SQLite/budget directory."""
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SystemExit("This data directory already has a running server.") from None

    def close(self):
        self.file.close()


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, provider, *, gateway_token="", data_dir=None):
        self.gateway_token = gateway_token
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        if gateway_token and self.data_dir is None:
            raise ValueError("Gateway mode requires data_dir")
        self._workspace_lock = threading.Lock()
        self._workspaces = {"user0": self._context(store, provider)}
        # Preserve the standalone API used by existing integrations and tests.
        default = self._workspaces["user0"]
        self.store, self.provider, self.engine = default.store, default.provider, default.engine
        self.csrf_token = default.csrf_token
        super().__init__(address, Handler)

    @staticmethod
    def _context(store, provider):
        context = SimpleNamespace(store=store, provider=provider, engine=Engine(store, provider),
                                  csrf_token=secrets.token_urlsafe(32))
        for request in store.get_pending_requests():
            text = "上次服务在回复完成前关闭，原问题已保存。可点击重试继续。"
            message = store.append_message(request["workspace_id"], "assistant", text,
                request_id=request["request_id"], status="failed", metrics={"error": text})
            store.put_request(request["request_id"], request["workspace_id"], request["content"], "failed",
                assistant_message_id=message["id"], error=text)
        return context

    def workspace(self, user_id):
        if not USER_ID.fullmatch(user_id):
            raise ValueError("Invalid user identifier")
        with self._workspace_lock:
            if user_id not in self._workspaces:
                directory = (self.data_dir / "users" / user_id).resolve()
                # Refuse symlinks/junctions that redirect a tenant outside its own directory.
                expected = self.data_dir / "users" / user_id
                if directory != expected or not directory.is_relative_to(self.data_dir):
                    raise ValueError("Unsafe user data directory")
                directory.mkdir(parents=True, exist_ok=True)
                if any((directory / part).resolve() != directory / part
                       for part in ("workspace.sqlite3", "calls")):
                    raise ValueError("Unsafe user data path")
                default = self._workspaces["user0"].provider
                provider = DeepSeekProvider(directory / "calls", api_key=default.api_key,
                    endpoint=default.endpoint, max_calls=default.max_calls, max_cost_cny=default.max_cost_cny)
                self._workspaces[user_id] = self._context(Store(directory / "workspace.sqlite3"), provider)
            return self._workspaces[user_id]

    def identity(self, authenticated=False):
        identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(identity, dict) or type(identity.get("identity_protocol")) is not int or identity["identity_protocol"] != 1:
            raise ValueError("Unsupported service identity protocol")
        service_id = identity.get("service_id", "")
        if not isinstance(service_id, str) or not service_id.startswith("urn:uuid:"):
            raise ValueError("Invalid service identity")
        if service_id != "urn:uuid:" + str(UUID(service_id[len("urn:uuid:"):])):
            raise ValueError("Invalid service identity")
        if identity.get("service_role") != "workspace" or identity.get("service_name") != "认知工作区":
            raise ValueError("Invalid workspace service role or name")
        return {**identity, "ready": True,
                "capabilities": (["gateway-user-v1"] if self.gateway_token else []) + ["relative-assets-v1"],
                "gateway_authenticated": bool(self.gateway_token and authenticated)}

    def server_close(self):
        super().server_close()
        for context in self._workspaces.values():
            context.engine.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CognitiveWorkspace/0.1"

    def log_message(self, fmt, *args):
        # Never log payloads, credentials, or private conversation text to the terminal.
        pass

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def reply(self, status, value):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def gateway_authenticated(self):
        return bool(self.server.gateway_token) and secrets.compare_digest(
            self.headers.get("X-SZD-Gateway-Token", "").encode("utf-8"),
            self.server.gateway_token.encode("utf-8"))

    def allowed(self, mutate=False, identity=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in hosts:
            self.reply(403, {"error": "仅允许从本机预览地址访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.reply(403, {"error": "不接受跨站请求。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "不接受跨站请求。"})
            return False
        if identity:
            return True
        self.user_id, self.prefix = "user0", ""
        if self.server.gateway_token:
            user_id = self.headers.get("X-SZD-User", "")
            prefix = self.headers.get("X-SZD-Prefix", "")
            if not self.gateway_authenticated() or not USER_ID.fullmatch(user_id):
                self.reply(403, {"error": "需要有效的日记网关身份。"})
                return False
            if len(prefix) > 512 or not MOUNT_PREFIX.fullmatch(prefix):
                self.reply(403, {"error": "日记网关挂载路径不合法。"})
                return False
            self.user_id, self.prefix = user_id, prefix
        elif any(self.headers.get(key) is not None for key in
                 ("X-SZD-User", "X-SZD-Gateway-Token", "X-SZD-Prefix")):
            self.reply(403, {"error": "独立模式不接受网关用户身份。"})
            return False
        if mutate or urlsplit(self.path).path.startswith("/api/"):
            self.workspace = self.server.workspace(self.user_id)
        if mutate and not secrets.compare_digest(self.headers.get("X-Workspace-Token", "").encode("utf-8"),
                                                  self.workspace.csrf_token.encode("utf-8")):
            self.reply(403, {"error": "连接状态已过期，请刷新页面后重试。"})
            return False
        return True

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须为 JSON")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 200000:
            raise ValueError("请求内容为空或超过大小限制")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求必须为对象")
        return data

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if not self.allowed(identity=path == "/__service__/identity"):
                return
            if path == "/__service__/identity":
                self.reply(200, self.server.identity(self.gateway_authenticated()))
            elif path in STATIC:
                file, mime = STATIC[path]
                data = (ROOT / file).read_bytes()
                self.send_response(200)
                self.headers_common()
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                store = self.workspace.store
                self.reply(200, {"workspaces": [store.get_workspace(w["id"]) for w in store.list_workspaces()],
                    "profile": store.get_profile(), "csrf_token": self.workspace.csrf_token,
                    "api_configured": self.workspace.provider.configured, "busy_workspaces": self.workspace.engine.busy(),
                    "memory_status": self.workspace.engine.memory_status(), "budget": self.workspace.provider.budget_status(),
                    "gateway_mode": bool(self.server.gateway_token), "user_id": self.user_id,
                    "diary_url": self.prefix[:-len("/cognitive")] + "/" if self.prefix else None,
                    "session_status_url": self.prefix[:-len("/cognitive")] + "/session_status" if self.prefix else None,
                    "login_url": self.prefix[:-len("/cognitive")] + "/login" if self.prefix else None})
            elif path == "/api/metrics":
                self.reply(200, self.workspace.provider.budget_status())
            else:
                self.reply(404, {"error": "未找到页面或接口"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.reply(500, {"error": "读取失败，请检查服务端数据或调用日志。"})

    def do_POST(self):
        self.mutate("POST")

    def do_PATCH(self):
        self.mutate("PATCH")

    def do_PUT(self):
        self.mutate("PUT")

    def mutate(self, method):
        try:
            if not self.allowed(mutate=True):
                return
            data, path = self.body(), urlsplit(self.path).path
            store = self.workspace.store
            if path == "/api/workspaces" and method == "POST":
                self.reply(201, store.create_workspace(data.get("name"), data.get("purpose", "")))
            elif path.startswith("/api/workspaces/") and method == "PATCH":
                wid = path.rsplit("/", 1)[-1]
                if wid in self.workspace.engine.busy():
                    self.reply(409, {"error": "当前回复尚未结束，请稍后编辑工作区。"})
                    return
                self.reply(200, store.update_workspace(wid, data))
            elif path == "/api/profile" and method == "PUT":
                self.reply(200, store.update_profile_notes(data.get("notes", "")))
            elif path == "/api/chat" and method == "POST":
                self.chat(data)
            else:
                self.reply(404, {"error": "未找到接口"})
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except KeyError:
            self.reply(404, {"error": "工作区或记录不存在"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.reply(500, {"error": "操作失败，已保存的原始记录不受影响。"})

    def chat(self, data):
        wid, content, rid = data.get("workspace_id"), data.get("content"), data.get("request_id")
        model = data.get("model", "auto")
        if not isinstance(wid, str) or not isinstance(content, str) or not content.strip() or len(content) > 16000:
            raise ValueError("请输入有效消息，单次最多16000字")
        if not isinstance(rid, str) or not 8 <= len(rid) <= 120 or model not in ("auto", "flash", "pro"):
            raise ValueError("请求ID或模型不合法")
        workspace = self.workspace.store.get_workspace(wid)
        if workspace["archived"]:
            raise ValueError("请先恢复归档工作区")
        if not self.workspace.provider.configured:
            self.reply(503, {"error": "服务端未配置 DEEPSEEK_API_KEY，请设置后重新启动。"})
            return
        if not self.workspace.engine.acquire(wid):
            self.reply(409, {"error": "此工作区正在回复，请等待当前回复完成。"})
            return
        connected = True
        try:
            self.send_response(200)
            self.headers_common()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            def emit(event, value):
                nonlocal connected
                if connected:
                    try:
                        self.wfile.write(f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        connected = False  # Finish and persist even if the tab closes.
            try:
                self.workspace.engine.chat(wid, content, model, rid, emit, retry=data.get("retry") is True)
            except (KeyError, ValueError) as error:
                emit("error", {"message": str(error), "request_id": rid, "retryable": False})
            except Exception:
                emit("error", {"message": "本次回复处理失败，请刷新查看已保存记录。", "request_id": rid, "retryable": True})
        finally:
            self.workspace.engine.release(wid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=loopback_host, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-token-file", type=Path,
                        help="Enable diary gateway mode using a UTF-8 shared-secret file")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    gateway_token = ""
    if args.gateway_token_file is not None:
        gateway_token = args.gateway_token_file.read_text(encoding="utf-8-sig").strip()
        if not 32 <= len(gateway_token) <= 4096 or any(c.isspace() for c in gateway_token):
            parser.error("gateway token must contain 32 to 4096 non-whitespace characters")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    directory_lock = DataLock(args.data_dir / "server.lock")
    store = Store(args.data_dir / "workspace.sqlite3")
    provider = DeepSeekProvider(args.data_dir / "calls")
    server = WorkspaceServer((args.host, args.port), store, provider,
                             gateway_token=gateway_token, data_dir=args.data_dir)
    print(f"Cognitive workspace: http://127.0.0.1:{args.port}", flush=True)
    print(f"API configured: {provider.configured}; data: {args.data_dir.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        directory_lock.close()


if __name__ == "__main__":
    main()
