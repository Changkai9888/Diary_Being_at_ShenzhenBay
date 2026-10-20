"""SQLite persistence with immutable source messages and revisioned derived memory."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid


GLOBAL_WORKSPACE_ID = "global-coordinator"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value, name, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} must be text of at most {maximum} characters")
    if required and not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _status(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value):
        raise ValueError("Invalid status")
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    model TEXT NOT NULL DEFAULT 'auto', summary TEXT NOT NULL DEFAULT '',
    memory_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'standard' CHECK(kind IN ('standard','global'))
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')), text TEXT NOT NULL,
    created_at TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, metrics TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS messages_workspace_seq ON messages(workspace_id, seq);
CREATE TRIGGER IF NOT EXISTS messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'Source messages are append-only'); END;
CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'Source messages are append-only'); END;
CREATE TABLE IF NOT EXISTS memory_revisions (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id), version INTEGER NOT NULL,
    summary TEXT NOT NULL, purpose TEXT NOT NULL, source_ids TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, version)
);
CREATE TABLE IF NOT EXISTS profile_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), notes TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO profile_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS profile_facts (
    id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('explicit','hypothesis')), source_ids TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL REFERENCES workspaces(id), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_revisions (
    version INTEGER PRIMARY KEY, notes TEXT NOT NULL, learned TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    content TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    user_message_id TEXT REFERENCES messages(id), assistant_message_id TEXT REFERENCES messages(id),
    metrics TEXT NOT NULL DEFAULT '{}', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class Store:
    """One connection per operation; writes use immediate transactions for safe CAS."""

    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection(transaction=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            self._initialize_global_workspace(db)

    @staticmethod
    def _initialize_global_workspace(db):
        """Upgrade old stores without converting or rewriting user workspaces."""
        if "kind" not in {row["name"] for row in db.execute("PRAGMA table_info(workspaces)")}:
            db.execute("""ALTER TABLE workspaces ADD COLUMN kind TEXT NOT NULL
                          DEFAULT 'standard' CHECK(kind IN ('standard','global'))""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS workspaces_single_global
                      ON workspaces(kind) WHERE kind='global'""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS global_workspace_no_delete
                      BEFORE DELETE ON workspaces WHEN OLD.kind='global'
                      BEGIN SELECT RAISE(ABORT, 'Global coordinator cannot be deleted'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS global_workspace_protect_identity
                      BEFORE UPDATE ON workspaces WHEN OLD.kind='global' AND
                      (NEW.archived<>0 OR NEW.kind IS NOT OLD.kind OR NEW.id IS NOT OLD.id)
                      BEGIN SELECT RAISE(ABORT, 'Global coordinator cannot be archived or reclassified'); END""")
        existing = db.execute("SELECT kind FROM workspaces WHERE id=?", (GLOBAL_WORKSPACE_ID,)).fetchone()
        if existing is not None:
            if existing["kind"] != "global":
                raise ValueError("Reserved global workspace ID is already in use")
            return
        timestamp = _now()
        db.execute("""INSERT INTO workspaces(id,name,model,kind,created_at,updated_at)
                      VALUES(?,?,'pro','global',?,?)""",
                   (GLOBAL_WORKSPACE_ID, "全局统筹", timestamp, timestamp))
        db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                   (GLOBAL_WORKSPACE_ID, 0, "", "", "[]", "create", timestamp))

    @contextmanager
    def _connection(self, *, write=False, transaction=True):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            if transaction:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _workspace(db, workspace_id):
        row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return row

    @staticmethod
    def _meta(row):
        result = dict(row)
        result["archived"] = bool(result["archived"])
        return result

    @staticmethod
    def _message(row):
        result = dict(row)
        result.pop("seq", None)
        result["metrics"] = json.loads(result["metrics"])
        return result

    @staticmethod
    def _request(row):
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result["metrics"])
        return result

    @staticmethod
    def _source_ids(db, workspace_id, source_ids, *, user_only=False):
        if not isinstance(source_ids, list) or len(source_ids) > 1000:
            raise ValueError("source_ids must be a list of at most 1000 message IDs")
        if any(not isinstance(item, str) or len(item) > 100 for item in source_ids):
            raise ValueError("Invalid source ID")
        ids = list(dict.fromkeys(source_ids))
        for message_id in ids:
            row = db.execute("SELECT workspace_id,role FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["workspace_id"] != workspace_id:
                raise ValueError("Memory sources must belong to this workspace")
            if user_only and row["role"] != "user":
                raise ValueError("Profile facts must cite user messages")
        return ids

    @classmethod
    def _full_workspace(cls, db, workspace_id):
        result = cls._meta(cls._workspace(db, workspace_id))
        result["messages"] = [cls._message(row) for row in db.execute(
            "SELECT * FROM messages WHERE workspace_id=? ORDER BY seq", (workspace_id,))]
        revision = db.execute(
            "SELECT * FROM memory_revisions WHERE workspace_id=? ORDER BY version DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        result["memory"] = {
            "summary": result["summary"], "purpose": result["purpose"],
            "version": result["memory_version"],
            "source_ids": json.loads(revision["source_ids"]) if revision else [],
        }
        return result

    def list_workspaces(self):
        with self._connection() as db:
            return [self._meta(row) for row in db.execute(
                "SELECT * FROM workspaces ORDER BY (kind='global') DESC, updated_at DESC, rowid DESC")]

    def create_workspace(self, name, purpose=""):
        name = _text(name, "name", 120, required=True).strip()
        purpose = _text(purpose, "purpose", 24000)
        workspace_id, timestamp = str(uuid.uuid4()), _now()
        with self._connection(write=True) as db:
            db.execute("INSERT INTO workspaces(id,name,purpose,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (workspace_id, name, purpose, timestamp, timestamp))
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                       (workspace_id, 0, "", purpose, "[]", "create", timestamp))
            return self._full_workspace(db, workspace_id)

    def get_workspace(self, workspace_id):
        with self._connection() as db:
            return self._full_workspace(db, workspace_id)

    def update_workspace(self, workspace_id, fields):
        if not isinstance(fields, dict) or set(fields) - {"name", "purpose", "archived", "model"}:
            raise ValueError("Invalid workspace fields")
        values = dict(fields)
        if "name" in values:
            values["name"] = _text(values["name"], "name", 120, required=True).strip()
        if "purpose" in values:
            _text(values["purpose"], "purpose", 24000)
        if "archived" in values and type(values["archived"]) is not bool:
            raise ValueError("archived must be a boolean")
        if "model" in values and values["model"] not in ("auto", "flash", "pro"):
            raise ValueError("model must be auto, flash or pro")
        with self._connection(write=True) as db:
            current = self._workspace(db, workspace_id)
            if current["kind"] == "global" and values.get("archived"):
                raise ValueError("全局统筹不能归档")
            if not values:
                return self._full_workspace(db, workspace_id)
            timestamp = _now()
            if "purpose" in values and values["purpose"] != current["purpose"]:
                version = current["memory_version"] + 1
                previous = db.execute(
                    "SELECT source_ids FROM memory_revisions WHERE workspace_id=? AND version=?",
                    (workspace_id, current["memory_version"]),
                ).fetchone()
                db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                           (workspace_id, version, current["summary"], values["purpose"],
                            previous["source_ids"] if previous else "[]", "manual-purpose", timestamp))
                values["memory_version"] = version
            values["updated_at"] = timestamp
            columns = ",".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE workspaces SET {columns} WHERE id=?", (*values.values(), workspace_id))
            return self._full_workspace(db, workspace_id)

    def append_message(self, workspace_id, role, text, request_id="", status="complete", metrics=None):
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        _text(text, "text", 500000, required=status == "complete")
        _text(request_id, "request_id", 120)
        _status(status)
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("metrics must be an object")
        encoded_metrics = _json(metrics or {})
        message_id, timestamp = str(uuid.uuid4()), _now()
        with self._connection(write=True) as db:
            self._workspace(db, workspace_id)
            db.execute("""INSERT INTO messages(id,workspace_id,role,text,created_at,request_id,status,metrics)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (message_id, workspace_id, role, text, timestamp, request_id, status, encoded_metrics))
            db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (timestamp, workspace_id))
            return self._message(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def apply_memory(self, workspace_id, summary, purpose, source_ids, expected_version=None):
        _text(summary, "summary", 60000)
        _text(purpose, "purpose", 24000)
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError("expected_version must be a nonnegative integer")
        with self._connection(write=True) as db:
            current = self._workspace(db, workspace_id)
            if expected_version is not None and current["memory_version"] != expected_version:
                return False
            ids = self._source_ids(db, workspace_id, source_ids)
            version, timestamp = current["memory_version"] + 1, _now()
            db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                       (workspace_id, version, summary, purpose, _json(ids), "derived", timestamp))
            db.execute("UPDATE workspaces SET summary=?,purpose=?,memory_version=?,updated_at=? WHERE id=?",
                       (summary, purpose, version, timestamp, workspace_id))
            return True

    @staticmethod
    def _profile(db):
        state = dict(db.execute("SELECT notes,version FROM profile_state WHERE singleton=1").fetchone())
        state["learned"] = []
        for row in db.execute("SELECT * FROM profile_facts ORDER BY updated_at DESC,key"):
            fact = dict(row)
            fact["source_ids"] = json.loads(fact["source_ids"])
            state["learned"].append(fact)
        return state

    @classmethod
    def _profile_revision(cls, db, reason):
        db.execute("UPDATE profile_state SET version=version+1 WHERE singleton=1")
        profile = cls._profile(db)
        db.execute("INSERT INTO profile_revisions VALUES(?,?,?,?,?)",
                   (profile["version"], profile["notes"], _json(profile["learned"]), reason, _now()))
        return profile

    def get_profile(self):
        with self._connection() as db:
            return self._profile(db)

    def update_profile_notes(self, notes):
        _text(notes, "notes", 20000)
        with self._connection(write=True) as db:
            current = self._profile(db)
            if notes == current["notes"]:
                return current
            db.execute("UPDATE profile_state SET notes=? WHERE singleton=1", (notes,))
            return self._profile_revision(db, "manual-notes")

    def upsert_profile_facts(self, workspace_id, facts):
        if not isinstance(facts, list) or len(facts) > 100:
            raise ValueError("facts must be a list of at most 100 facts")
        with self._connection(write=True) as db:
            self._workspace(db, workspace_id)
            changed = False
            seen_keys = set()
            for fact in facts:
                if not isinstance(fact, dict) or not {"key", "text", "kind", "source_ids"} <= set(fact):
                    raise ValueError("Fact requires key, text, kind and source_ids")
                key = _text(fact["key"], "key", 120, required=True).strip()
                if key in seen_keys:
                    raise ValueError("A fact key may occur only once in a batch")
                seen_keys.add(key)
                text = _text(fact["text"], "fact text", 6000, required=True)
                if fact["kind"] not in ("explicit", "hypothesis"):
                    raise ValueError("Fact kind must be explicit or hypothesis")
                ids = self._source_ids(db, workspace_id, fact["source_ids"], user_only=True)
                if not ids:
                    raise ValueError("Profile facts require at least one user source")
                previous = db.execute("SELECT * FROM profile_facts WHERE key=?", (key,)).fetchone()
                if previous:
                    old_ids = json.loads(previous["source_ids"])
                    old_sequence = max(db.execute("SELECT seq FROM messages WHERE id=?", (item,)).fetchone()[0]
                                       for item in old_ids)
                    new_sequence = max(db.execute("SELECT seq FROM messages WHERE id=?", (item,)).fetchone()[0]
                                       for item in ids)
                    if new_sequence < old_sequence:
                        continue
                values = (text, fact["kind"], _json(ids), workspace_id)
                if previous and values == tuple(previous[field] for field in
                                               ("text", "kind", "source_ids", "source_workspace_id")):
                    continue
                if previous:
                    db.execute("""UPDATE profile_facts SET text=?,kind=?,source_ids=?,source_workspace_id=?,
                                  updated_at=? WHERE key=?""", (*values, _now(), key))
                else:
                    db.execute("INSERT INTO profile_facts VALUES(?,?,?,?,?,?,?)",
                               (str(uuid.uuid4()), key, *values, _now()))
                changed = True
            return self._profile_revision(db, "derived-facts") if changed else self._profile(db)

    def get_request(self, request_id):
        with self._connection() as db:
            return self._request(db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone())

    def begin_request(self, request_id, workspace_id, content, model):
        """Atomically bind a new request and its immutable user message."""
        _text(request_id, "request_id", 120, required=True)
        _text(content, "content", 16000, required=True)
        timestamp, message_id = _now(), str(uuid.uuid4())
        with self._connection(write=True) as db:
            workspace = self._workspace(db, workspace_id)
            if workspace["archived"]:
                raise ValueError("请先恢复归档工作区")
            if db.execute("SELECT 1 FROM requests WHERE request_id=?", (request_id,)).fetchone():
                raise ValueError("请求已存在，请刷新后重试")
            db.execute("INSERT INTO messages(id,workspace_id,role,text,created_at,request_id,status,metrics) VALUES(?,?,?,?,?,?,?,?)",
                       (message_id, workspace_id, "user", content, timestamp, request_id, "complete", "{}"))
            db.execute("INSERT INTO requests(request_id,workspace_id,content,status,model,user_message_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (request_id, workspace_id, content, "running", model, message_id, timestamp, timestamp))
            db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (timestamp, workspace_id))
            return self._message(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def get_pending_requests(self):
        """Return unfinished requests so startup can expose interrupted work for retry."""
        with self._connection() as db:
            return [self._request(row) for row in db.execute(
                """SELECT * FROM requests WHERE status NOT IN
                   ('complete','completed','done','success','failed','error','cancelled','canceled','interrupted')
                   ORDER BY created_at, rowid""")]

    def put_request(self, request_id, workspace_id, content, status, model="", user_message_id=None,
                    assistant_message_id=None, metrics=None, error=None):
        _text(request_id, "request_id", 120, required=True)
        _text(content, "content", 500000, required=True)
        _text(model, "model", 120)
        _status(status)
        if error is not None:
            _text(error, "error", 24000)
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("metrics must be an object")
        with self._connection(write=True) as db:
            self._workspace(db, workspace_id)
            previous = self._request(db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone())
            if previous and (previous["workspace_id"] != workspace_id or previous["content"] != content):
                raise ValueError("Request ID is already bound to different content or workspace")
            for message_id, role in ((user_message_id, "user"), (assistant_message_id, "assistant")):
                if message_id is not None:
                    row = db.execute("SELECT workspace_id,role FROM messages WHERE id=?", (message_id,)).fetchone()
                    if row is None or row["workspace_id"] != workspace_id or row["role"] != role:
                        raise ValueError("Invalid request message reference")
            timestamp = _now()
            record = previous or {"request_id": request_id, "workspace_id": workspace_id, "content": content,
                                  "model": "", "user_message_id": None, "assistant_message_id": None,
                                  "metrics": {}, "error": None, "created_at": timestamp}
            record.update(status=status, updated_at=timestamp)
            if model:
                record["model"] = model
            for key, value in (("user_message_id", user_message_id), ("assistant_message_id", assistant_message_id),
                               ("metrics", metrics), ("error", error)):
                if value is not None:
                    record[key] = value
            if status in ("complete", "completed", "done", "success") and error is None:
                record["error"] = None
            values = tuple(_json(record[key]) if key == "metrics" else record.get(key) for key in
                           ("request_id", "workspace_id", "content", "status", "model", "user_message_id",
                            "assistant_message_id", "metrics", "error", "created_at", "updated_at"))
            db.execute("""INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
                          status=excluded.status,model=excluded.model,user_message_id=excluded.user_message_id,
                          assistant_message_id=excluded.assistant_message_id,metrics=excluded.metrics,
                          error=excluded.error,updated_at=excluded.updated_at""", values)
            return record

    @staticmethod
    def _limit(value, maximum):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return value

    def read_messages(self, workspace_id, before_id=None, limit=20):
        self._limit(limit, 100)
        with self._connection() as db:
            self._workspace(db, workspace_id)
            before_seq = 9223372036854775807
            if before_id is not None:
                row = db.execute("SELECT seq FROM messages WHERE id=? AND workspace_id=?",
                                 (before_id, workspace_id)).fetchone()
                if row is None:
                    raise ValueError("before_id must be a message from this workspace")
                before_seq = row["seq"]
            rows = db.execute("SELECT * FROM messages WHERE workspace_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                              (workspace_id, before_seq, limit)).fetchall()
            return [self._message(row) for row in reversed(rows)]

    def search_messages(self, workspace_id, query, limit=6):
        _text(query, "query", 500)
        self._limit(limit, 40)
        with self._connection() as db:
            self._workspace(db, workspace_id)
            if not query.strip():
                return []
            rows = db.execute("""SELECT * FROM messages WHERE workspace_id=? AND instr(lower(text),lower(?))>0
                                 ORDER BY seq DESC LIMIT ?""", (workspace_id, query, limit)).fetchall()
            results = []
            for row in rows:
                result = self._message(row)
                raw = result.pop("text")
                match_at = raw.lower().find(query.lower())
                start = max(0, match_at - 100)
                end = min(len(raw), start + 600)
                result["snippet"] = ("…" if start else "") + raw[start:end] + ("…" if end < len(raw) else "")
                result["truncated"] = start > 0 or end < len(raw)
                results.append(result)
            return results
