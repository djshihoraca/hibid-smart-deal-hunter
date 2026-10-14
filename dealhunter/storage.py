"""User-scoped SQLite records; no passwords, API keys, or HiBid credentials."""
import json
import errno
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class StorageError(OSError):
    """Safe diagnostic metadata, never database contents or raw configured paths."""
    def __init__(self, stage, cause):
        self.stage = stage
        self.reason = storage_reason(cause)
        super().__init__(f"{stage}: {self.reason}")


def storage_reason(error):
    if isinstance(error, StorageError):
        return error.reason
    sqlite_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    if sqlite_code == sqlite3.SQLITE_READONLY:
        return "database-read-only"
    if sqlite_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return "database-locked"
    if sqlite_code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return "database-invalid"
    if isinstance(error, PermissionError) or getattr(error, "errno", None) in (errno.EACCES, errno.EPERM):
        return "permission-denied"
    if getattr(error, "errno", None) == errno.EROFS:
        return "filesystem-read-only"
    if isinstance(error, (FileExistsError, NotADirectoryError, IsADirectoryError, ValueError)):
        return "invalid-path"
    if getattr(error, "errno", None) in (errno.ENOSPC, errno.EDQUOT):
        return "storage-full"
    if isinstance(error, sqlite3.Error):
        return "database-unavailable"
    return "filesystem-unavailable"


def temporary_database_path():
    # Each explicit recovery gets a private directory; no shared predictable /tmp file.
    return Path(tempfile.mkdtemp(prefix="hibid-private-")) / "dealhunter.sqlite3"


def database_path(configured=None, legacy_directory=None):
    """Respect custom storage and existing history; new installs use user storage."""
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("DEAL_HUNTER_DB must be a nonempty filesystem path")
        return Path(configured).expanduser()
    if legacy_directory is not None:
        legacy = Path(legacy_directory) / "data" / "dealhunter.sqlite3"
        try:
            legacy.stat()
        except FileNotFoundError:
            pass
        else:
            return legacy
    return Path.home() / ".local" / "share" / "hibid-smart-deal-hunter" / "dealhunter.sqlite3"


class Store:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise StorageError("create-directory", exc) from exc
        try:
            with self.connect() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner, name));
                CREATE TABLE IF NOT EXISTS inspections (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS photos (
                    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL, content BLOB NOT NULL);
                """)
        except sqlite3.Error as exc:
            raise StorageError("initialize-database", exc) from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise StorageError("protect-database", exc) from exc

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def owner(owner):
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("Authenticated owner required")
        return owner

    def watchlists(self, owner):
        with self.connect() as conn:
            return [{"name": name, **json.loads(payload)} for name, payload in conn.execute(
                "SELECT name, payload FROM watchlists WHERE owner=? ORDER BY name", (self.owner(owner),))]

    def save_watchlist(self, owner, name, payload):
        if not name.strip():
            raise ValueError("Watchlist name is required")
        with self.connect() as conn:
            conn.execute("""INSERT INTO watchlists(owner,name,payload,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(owner,name) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                (self.owner(owner), name.strip(), json.dumps(payload), utcnow()))

    def save_inspection(self, owner, payload, photos=()):
        with self.connect() as conn:
            cursor = conn.execute("INSERT INTO inspections(owner,payload,created_at) VALUES(?,?,?)",
                                  (self.owner(owner), json.dumps(payload), utcnow()))
            record_id = cursor.lastrowid
            conn.executemany("INSERT INTO photos VALUES(?,?,?)", [(record_id, n, b) for n, b in photos])
            return record_id

    def history(self, owner):
        with self.connect() as conn:
            return [{"id": i, "created_at": t, **json.loads(p)} for i, p, t in conn.execute(
                "SELECT id,payload,created_at FROM inspections WHERE owner=? ORDER BY id DESC LIMIT 200",
                (self.owner(owner),))]

    def photos(self, owner, record_id):
        with self.connect() as conn:
            return list(conn.execute("""SELECT p.filename,p.content FROM photos p JOIN inspections i
                ON i.id=p.inspection_id WHERE i.owner=? AND i.id=?""", (self.owner(owner), record_id)))
