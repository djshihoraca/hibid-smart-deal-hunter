"""User-scoped SQLite records; no passwords, API keys, or HiBid credentials."""
import json
import errno
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from datetime import timedelta
from zoneinfo import ZoneInfo
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
    def __init__(self, path, timeout=15):
        self.path = Path(path)
        self.timeout = timeout
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
                CREATE TABLE IF NOT EXISTS auction_snapshots (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, lot_id TEXT NOT NULL,
                    payload TEXT NOT NULL, fetched_at TEXT NOT NULL, saved_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, agent TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                    result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS agent_actions (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL,
                    approved_at TEXT, expires_at TEXT, approval_reason TEXT, receipt TEXT);
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, action_id INTEGER,
                    event TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scout_budget (
                    owner TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL,
                    PRIMARY KEY(owner,day));
                CREATE TABLE IF NOT EXISTS slack_routes (
                    route TEXT PRIMARY KEY, activated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slack_outbox (
                    id INTEGER PRIMARY KEY, route TEXT NOT NULL, owner TEXT NOT NULL,
                    event_key TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                    message_ts TEXT, UNIQUE(route,event_key));
                CREATE TABLE IF NOT EXISTS slack_requests (
                    request_key TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_notes (
                    id INTEGER PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL);
                """)
                if 'receipt' not in {r[1] for r in conn.execute('PRAGMA table_info(agent_actions)')}:
                    conn.execute('ALTER TABLE agent_actions ADD COLUMN receipt TEXT')
        except sqlite3.Error as exc:
            raise StorageError("initialize-database", exc) from exc
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise StorageError("protect-database", exc) from exc

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=self.timeout)
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

    def save_auction_snapshot(self, owner, lot, fetched_at):
        with self.connect() as conn:
            return conn.execute("INSERT INTO auction_snapshots(owner,lot_id,payload,fetched_at,saved_at) VALUES(?,?,?,?,?)",
                                (self.owner(owner), str(lot['lot_id']), json.dumps(lot), fetched_at, utcnow())).lastrowid

    def auction_snapshots(self, owner):
        with self.connect() as conn:
            return [{"id": i, "lot": json.loads(p), "fetched_at": f, "saved_at": s}
                    for i, p, f, s in conn.execute("SELECT id,payload,fetched_at,saved_at FROM auction_snapshots WHERE owner=? ORDER BY id DESC LIMIT 200",
                                                 (self.owner(owner),))]

    def enqueue_agent_job(self, owner, agent, payload):
        from dealhunter.agents import REGISTRY
        if agent not in REGISTRY:
            raise ValueError('Unknown agent role')
        with self.connect() as conn:
            for dependency in payload.get('depends_on', []):
                if not conn.execute('SELECT id FROM agent_jobs WHERE owner=? AND id=?', (self.owner(owner), dependency)).fetchone():
                    raise ValueError('Dependencies must belong to the same user')
            return conn.execute('INSERT INTO agent_jobs(owner,agent,payload,created_at) VALUES(?,?,?,?)',
                                (self.owner(owner), agent, json.dumps(payload), utcnow())).lastrowid

    def claim_agent_job(self, owner):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT id,agent,payload FROM agent_jobs WHERE owner=? AND status='queued' ORDER BY id", (self.owner(owner),)).fetchall():
                payload = json.loads(row[2])
                states = [conn.execute('SELECT status FROM agent_jobs WHERE owner=? AND id=?', (owner, d)).fetchone() for d in payload.get('depends_on', [])]
                if any(not state or state[0] in ('failed', 'blocked') for state in states):
                    conn.execute("UPDATE agent_jobs SET status='blocked',error=?,finished_at=? WHERE owner=? AND id=?",
                                 ('An upstream agent failed; no dependent action was performed.', utcnow(), owner, row[0]))
                    continue
                if any(state[0] != 'succeeded' for state in states):
                    continue
                conn.execute("UPDATE agent_jobs SET status='running',started_at=? WHERE id=? AND owner=? AND status='queued'",
                             (utcnow(), row[0], owner))
                return {'id': row[0], 'agent': row[1], 'payload': payload}
            return None

    def finish_agent_job(self, owner, job_id, result=None, error=None):
        with self.connect() as conn:
            changed = conn.execute("UPDATE agent_jobs SET status=?,result=?,error=?,finished_at=? WHERE owner=? AND id=? AND status='running'",
                                   ('failed' if error else 'succeeded', json.dumps(result), error, utcnow(), self.owner(owner), job_id)).rowcount
            if not changed:
                raise ValueError('Job is not running for this user')

    def agent_jobs(self, owner):
        with self.connect() as conn:
            return [{'id': i, 'agent': a, 'status': s, 'created_at': t,
                     'result': json.loads(r) if r else None, 'error': e}
                    for i, a, s, t, r, e in conn.execute('SELECT id,agent,status,created_at,result,error FROM agent_jobs WHERE owner=? ORDER BY id DESC LIMIT 100', (self.owner(owner),))]

    def agent_job(self, owner, identifier):
        with self.connect() as conn:
            row = conn.execute('SELECT agent,status,result FROM agent_jobs WHERE owner=? AND id=?', (self.owner(owner), identifier)).fetchone()
            return {'agent': row[0], 'status': row[1], 'result': json.loads(row[2]) if row[2] else None} if row else None

    def reserve_scout_calls(self, owner, count, limit=10):
        if type(count) is not int or count < 1 or count > limit:
            raise ValueError('Scout daily request budget is insufficient')
        day = datetime.now(ZoneInfo('America/Toronto')).date().isoformat()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT calls FROM scout_budget WHERE owner=? AND day=?', (self.owner(owner), day)).fetchone()
            used = row[0] if row else 0
            if used + count > limit:
                raise ValueError('Scout daily request budget is exhausted')
            conn.execute('INSERT INTO scout_budget VALUES(?,?,?) ON CONFLICT(owner,day) DO UPDATE SET calls=excluded.calls', (owner, day, used + count))

    def propose_agent_action(self, owner, payload):
        from dealhunter.agents import scope_hash
        if payload.get('kind') not in ('bid', 'publish_ad'):
            raise ValueError('Unsupported action')
        fingerprint = scope_hash(payload)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute("SELECT id FROM agent_actions WHERE owner=? AND fingerprint=? AND status IN ('proposed','approved','executing','unknown')", (self.owner(owner), fingerprint)).fetchone()
            if existing:
                return existing[0]
            identifier = conn.execute('INSERT INTO agent_actions(owner,kind,payload,fingerprint,created_at) VALUES(?,?,?,?,?)',
                                      (owner, payload['kind'], json.dumps(payload), fingerprint, utcnow())).lastrowid
            conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, 'proposed', utcnow()))
            return identifier

    def agent_actions(self, owner):
        now = utcnow()
        with self.connect() as conn:
            expired = conn.execute("SELECT id FROM agent_actions WHERE owner=? AND status='approved' AND expires_at<=?", (self.owner(owner), now)).fetchall()
            for (identifier,) in expired:
                conn.execute("UPDATE agent_actions SET status='expired' WHERE id=? AND owner=?", (identifier, owner))
                conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, 'expired', now))
            return [{'id': i, 'kind': k, 'payload': json.loads(p), 'fingerprint': f, 'status': s,
                     'created_at': c, 'approved_at': a, 'expires_at': e, 'approval_reason': r,
                     'receipt': json.loads(receipt) if receipt else None}
                    for i, k, p, f, s, c, a, e, r, receipt in conn.execute('SELECT id,kind,payload,fingerprint,status,created_at,approved_at,expires_at,approval_reason,receipt FROM agent_actions WHERE owner=? ORDER BY id DESC LIMIT 100', (owner,))]

    def approve_agent_action(self, owner, identifier, expected_fingerprint, reason):
        from dealhunter.agents import scope_hash
        if not reason.strip():
            raise ValueError('An approval reason is required')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT payload,fingerprint,status,kind FROM agent_actions WHERE owner=? AND id=?", (self.owner(owner), identifier)).fetchone()
            if not row or row[2] != 'proposed' or row[1] != expected_fingerprint or scope_hash(json.loads(row[0])) != expected_fingerprint:
                raise ValueError('Action has changed, is not pending, or belongs to another user')
            payload = json.loads(row[0])
            if row[3] == 'publish_ad' and payload.get('owned') is not True:
                raise ValueError('Confirm ownership in a new ad draft before approving publication')
            if row[3] == 'publish_ad' and (payload.get('photos_authorized') is not True or not payload.get('photo_refs')):
                raise ValueError('Add authorized photos in the inspection and confirm photo rights in a new draft')
            now = datetime.now(timezone.utc)
            expires = now + (timedelta(minutes=10) if row[3] == 'bid' else timedelta(hours=24))
            conn.execute("UPDATE agent_actions SET status='approved',approved_at=?,expires_at=?,approval_reason=? WHERE id=? AND owner=?",
                         (now.isoformat(), expires.isoformat(), reason.strip(), identifier, owner))
            conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, 'approved; no external execution', now.isoformat()))

    def revoke_agent_action(self, owner, identifier):
        with self.connect() as conn:
            changed = conn.execute("UPDATE agent_actions SET status='revoked' WHERE owner=? AND id=? AND status IN ('proposed','approved')", (self.owner(owner), identifier)).rowcount
            if not changed:
                raise ValueError('Action is unavailable for revocation')
            conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, 'revoked', utcnow()))

    def start_agent_execution(self, owner, identifier, expected_fingerprint):
        from dealhunter.agents import scope_hash
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT payload,fingerprint,status,expires_at FROM agent_actions WHERE owner=? AND id=?", (self.owner(owner), identifier)).fetchone()
            if not row or row[2] != 'approved' or row[1] != expected_fingerprint or row[3] <= utcnow():
                raise ValueError('A current approval for this exact action is required')
            payload = json.loads(row[0])
            if scope_hash(payload) != expected_fingerprint:
                raise ValueError('Action content changed after approval')
            resource = payload.get('resource')
            for (other,) in conn.execute("SELECT payload FROM agent_actions WHERE owner=? AND id<>? AND status IN ('executing','unknown')", (owner, identifier)):
                if resource and json.loads(other).get('resource') == resource:
                    raise ValueError('Reconcile the previous uncertain action on this item first')
            conn.execute("UPDATE agent_actions SET status='executing' WHERE owner=? AND id=?", (owner, identifier))
            conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, 'execution started', utcnow()))
            return payload

    def finish_agent_execution(self, owner, identifier, receipt=None):
        status = 'succeeded' if receipt else 'unknown'
        with self.connect() as conn:
            changed = conn.execute("UPDATE agent_actions SET status=?,receipt=? WHERE owner=? AND id=? AND status='executing'",
                                   (status, json.dumps(receipt) if receipt else None, self.owner(owner), identifier)).rowcount
            if not changed:
                raise ValueError('Action is not executing for this user')
            conn.execute('INSERT INTO agent_events(owner,action_id,event,created_at) VALUES(?,?,?,?)', (owner, identifier, status, utcnow()))

    def activate_slack_route(self, route):
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO slack_routes VALUES(?,?)', (route, utcnow()))
            return conn.execute('SELECT activated_at FROM slack_routes WHERE route=?', (route,)).fetchone()[0]

    def enqueue_slack_notification(self, route, owner, event_key, payload):
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO slack_outbox(route,owner,event_key,payload,created_at) VALUES(?,?,?,?,?)',
                         (route, self.owner(owner), event_key, json.dumps(payload), utcnow()))

    def claim_slack_notification(self, route, owner):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT id,payload FROM slack_outbox WHERE route=? AND owner=? AND status='pending' ORDER BY id LIMIT 1",
                               (route, self.owner(owner))).fetchone()
            if not row:
                return None
            conn.execute("UPDATE slack_outbox SET status='sending' WHERE id=?", (row[0],))
            return {'id': row[0], 'payload': json.loads(row[1])}

    def finish_slack_notification(self, route, owner, identifier, message_ts=None):
        with self.connect() as conn:
            conn.execute("UPDATE slack_outbox SET status=?,message_ts=? WHERE route=? AND owner=? AND id=? AND status='sending'",
                         ('sent' if message_ts else 'unknown', message_ts, route, self.owner(owner), identifier))

    def slack_delivery_status(self, owner):
        with self.connect() as conn:
            return [{'status': status, 'count': count} for status, count in conn.execute(
                'SELECT status,COUNT(*) FROM slack_outbox WHERE owner=? GROUP BY status', (self.owner(owner),))]

    def claim_slack_request(self, key):
        with self.connect() as conn:
            return bool(conn.execute('INSERT OR IGNORE INTO slack_requests VALUES(?,?)', (key, utcnow())).rowcount)

    def add_agent_note(self, owner, source, content):
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError('A note must contain 1–4,000 characters')
        with self.connect() as conn:
            return conn.execute('INSERT INTO agent_notes(owner,source,content,created_at) VALUES(?,?,?,?)',
                                (self.owner(owner), source, content.strip(), utcnow())).lastrowid

    def agent_notes(self, owner):
        with self.connect() as conn:
            return [{'id': i, 'source': s, 'content': c, 'created_at': t} for i, s, c, t in conn.execute(
                'SELECT id,source,content,created_at FROM agent_notes WHERE owner=? ORDER BY id DESC LIMIT 100', (self.owner(owner),))]
