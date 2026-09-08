"""The whole database: one SQLite file, three tables.

Concurrency matters in exactly one place -- two members of the same group
running `gpulease start` at the same moment must not launch two instances --
and `claim()` handles it with BEGIN IMMEDIATE, which takes the write lock
before reading. Everything else is a plain statement.

Connections are opened per operation rather than shared. At this scale (tens of
requests a minute) that costs nothing and removes every question about SQLite
and threads.
"""

import contextlib
import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path

from . import config

# ---------------------------------------------------------------- states
#
#   STOPPED  --start-->  PROVISIONING  --(sshd answers)-->  RUNNING
#      ^                      |                               |
#      |                      v                               v
#      +------------- STOPPING <-------- stop / reaper -------+
#
# STOPPED and FAILED are the only states a new start is allowed from.

STOPPED = "STOPPED"
PROVISIONING = "PROVISIONING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
FAILED = "FAILED"

STARTABLE = (STOPPED, FAILED)
LIVE = (PROVISIONING, RUNNING, STOPPING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL,
    token_sha256 TEXT NOT NULL,
    disabled     INTEGER NOT NULL DEFAULT 0
);

-- One row per (group, assignment). This is the entire notion of "who has what".
CREATE TABLE IF NOT EXISTS sessions (
    group_id         TEXT NOT NULL,
    assignment_id    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'STOPPED',
    session_id       TEXT,
    instance_id      TEXT,   -- rank 0. The whole list lives in `nodes`.
    host             TEXT,   -- rank 0's public address, likewise.
    nodes            TEXT,   -- JSON [{rank, instance_id, private_ip, host}]
    node_count       INTEGER NOT NULL DEFAULT 1,
    private_key      TEXT,
    started_at       INTEGER,
    expires_at       INTEGER,
    started_by       TEXT,
    gpu_seconds_used INTEGER NOT NULL DEFAULT 0,
    starts_used      INTEGER NOT NULL DEFAULT 0,
    last_stop_reason TEXT,
    last_stopped_at  INTEGER,
    last_error       TEXT,
    PRIMARY KEY (group_id, assignment_id)
);

-- Append-only history: end-of-semester reporting and dispute evidence.
CREATE TABLE IF NOT EXISTS session_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    group_id      TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    session_id    TEXT,
    event         TEXT NOT NULL,
    detail        TEXT,
    seconds       INTEGER
);
CREATE INDEX IF NOT EXISTS session_log_group ON session_log (group_id, ts);
"""


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    cx = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA busy_timeout=15000")
    return cx


@contextlib.contextmanager
def read():
    cx = connect()
    try:
        yield cx
    finally:
        cx.close()


@contextlib.contextmanager
def write():
    """BEGIN IMMEDIATE .. COMMIT.

    IMMEDIATE takes the write lock at the start rather than on the first write,
    which is what makes a read-then-write block atomic against another process
    doing the same thing.
    """
    cx = connect()
    try:
        cx.execute("BEGIN IMMEDIATE")
        yield cx
        cx.execute("COMMIT")
    except Exception:
        # Suppressed on purpose: if BEGIN itself failed there is no transaction
        # to roll back, and letting that secondary error propagate would hide
        # the one that actually matters.
        with contextlib.suppress(sqlite3.Error):
            cx.execute("ROLLBACK")
        raise
    finally:
        cx.close()


def _migrate(cx) -> None:
    """Bring an existing database up to the current schema.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    so every added column needs a line here. Adding one is cheap; discovering
    at 2am that a deployed database is missing it is not.
    """
    columns = {r["name"] for r in cx.execute("PRAGMA table_info(sessions)")}
    if "starts_used" not in columns:
        cx.execute("ALTER TABLE sessions ADD COLUMN starts_used INTEGER NOT NULL DEFAULT 0")
        # Left at 0 for rows that predate the column. Sessions taken under the
        # old unlimited-restarts policy are not retroactively charged against a
        # limit that did not exist when they happened.
    if "nodes" not in columns:
        cx.execute("ALTER TABLE sessions ADD COLUMN nodes TEXT")
    if "node_count" not in columns:
        # 1, not the configured value: a row written before multi-node existed
        # describes a session that really did have one instance, and billing it
        # for two retroactively would be wrong. `session_nodes()` reads the old
        # singular columns for exactly the same reason, so no backfill is
        # needed here.
        cx.execute("ALTER TABLE sessions ADD COLUMN node_count INTEGER NOT NULL DEFAULT 1")


def init() -> None:
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with read() as cx:
        cx.executescript(SCHEMA)
        _migrate(cx)
    # The file holds every live session's SSH private key and every student's
    # token hash. SQLite creates it 0644 minus umask; don't rely on the umask.
    for suffix in ("", "-wal", "-shm"):
        sibling = Path(str(path) + suffix)
        if sibling.exists():
            sibling.chmod(0o600)


def _log(cx, group_id, assignment_id, session_id, event, detail=None, seconds=None):
    cx.execute(
        "INSERT INTO session_log (ts, group_id, assignment_id, session_id, event, detail, seconds)"
        " VALUES (?,?,?,?,?,?,?)",
        (now(), group_id, assignment_id, session_id, event, detail, seconds),
    )


# ---------------------------------------------------------------- auth


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def authenticate(token: str):
    """Tokens look like `<student_id>.<secret>`.

    The student_id prefix means the lookup is a primary-key hit. Only the
    SHA-256 of the secret is stored, so a stolen database grants nobody access.
    """
    if not token or "." not in token:
        return None
    student_id, secret = token.strip().split(".", 1)
    with read() as cx:
        row = cx.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
    if not row or row["disabled"]:
        return None
    if not hmac.compare_digest(row["token_sha256"], hash_secret(secret)):
        return None
    return row


def upsert_student(cx, student_id, name, group_id, token_sha256=None):
    if token_sha256 is None:
        cx.execute(
            "UPDATE students SET name = ?, group_id = ? WHERE student_id = ?",
            (name, group_id, student_id),
        )
    else:
        cx.execute(
            "INSERT INTO students (student_id, name, group_id, token_sha256) VALUES (?,?,?,?)"
            " ON CONFLICT(student_id) DO UPDATE SET"
            "   name = excluded.name, group_id = excluded.group_id,"
            "   token_sha256 = excluded.token_sha256",
            (student_id, name, group_id, token_sha256),
        )


def get_student(cx, student_id):
    return cx.execute(
        "SELECT * FROM students WHERE student_id = ?", (student_id,)
    ).fetchone()


# ---------------------------------------------------------------- sessions


def get_session(group_id, assignment_id):
    with read() as cx:
        return cx.execute(
            "SELECT * FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()


def all_sessions():
    with read() as cx:
        return cx.execute("SELECT * FROM sessions").fetchall()


def claim(group_id, assignment_id, session_id, student_id, expires_cap, quota_seconds,
          max_starts, node_count=1, min_start_seconds=0):
    """Try to take the group's session slot for a new launch.

    One transaction does five jobs: it enforces the per-assignment start limit
    and the GPU-hour budget, it decides how long this session's lease may be,
    it stops a second group member from launching a duplicate instance, and it
    claims the session. All of it has to be decided against the same snapshot,
    or two simultaneous requests could each conclude they were the group's one
    allowed start.

    `expires_cap` is the latest this lease may end for reasons that are not the
    budget -- MAX_SESSION_HOURS, and the deadline. The budget shortens it
    further: a group with forty minutes of node-hours left gets a forty-minute
    lease, and the reaper's ordinary "lease expired" path is then what enforces
    the budget. That is the whole of budget enforcement; nothing accrues
    mid-session and no reaper case knows about the quota.

    Returns (ok, reason, row) where reason is
    "" | "live" | "spent" | "quota" | "exhausted" and row is the existing
    session when we lost.
    """
    with write() as cx:
        row = cx.execute(
            "SELECT * FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()
        if row and row["status"] in LIVE:
            return False, "live", row
        if row and max_starts and row["starts_used"] >= max_starts:
            return False, "spent", row

        # Node-seconds, matching what accrue_and_close charges. The row is
        # never LIVE here, so the column is the group's whole usage.
        used = (row["gpu_seconds_used"] or 0) if row else 0
        remaining = quota_seconds - used
        if remaining <= 0:
            return False, "quota", row
        if remaining < min_start_seconds:
            # Enough budget to be charged for, not enough to be useful.
            return False, "exhausted", row

        # Budget is node-seconds; a lease is wall-clock seconds. Floor division
        # so rounding can only ever end the session early.
        expires = min(expires_cap, now() + remaining // max(1, node_count))

        cx.execute(
            "INSERT INTO sessions"
            "  (group_id, assignment_id, status, session_id, started_at, expires_at,"
            "   started_by, gpu_seconds_used, starts_used, node_count)"
            " VALUES (?,?,?,?,?,?,?,0,1,?)"
            " ON CONFLICT(group_id, assignment_id) DO UPDATE SET"
            "   status = excluded.status, session_id = excluded.session_id,"
            "   started_at = excluded.started_at, expires_at = excluded.expires_at,"
            "   started_by = excluded.started_by,"
            "   node_count = excluded.node_count,"
            "   starts_used = sessions.starts_used + 1,"
            "   host = NULL, nodes = NULL, private_key = NULL, last_error = NULL",
            (group_id, assignment_id, PROVISIONING, session_id, now(), expires, student_id,
             node_count),
        )
        _log(cx, group_id, assignment_id, session_id, "start", f"requested by {student_id}")
        return True, "", None


def refund_start(group_id, assignment_id):
    """Give back a start that never became a usable session.

    A launch that failed on capacity or a bad AMI is the system's problem, not
    the group's, and burning their one lease on it would be indefensible.
    """
    with write() as cx:
        cx.execute(
            "UPDATE sessions SET starts_used = MAX(starts_used - 1, 0)"
            " WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        )


def grant_starts(group_id, assignment_id, extra=1):
    """Instructor override: hand a group another attempt."""
    with write() as cx:
        cur = cx.execute(
            "SELECT starts_used FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()
        if cur is None:
            return None
        cx.execute(
            "UPDATE sessions SET starts_used = MAX(starts_used - ?, 0)"
            " WHERE group_id = ? AND assignment_id = ?",
            (extra, group_id, assignment_id),
        )
        _log(cx, group_id, assignment_id, None, "grant", f"+{extra} start(s)")
        return cx.execute(
            "SELECT starts_used FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()["starts_used"]


def set_usage(group_id, assignment_id, seconds):
    """Instructor override: set a group's consumed node-seconds outright.

    The way back in once a group has spent its budget, now that unlimited
    starts make `grant_starts` largely beside the point. Refuses while the
    session is live, because `accrue_and_close` is about to add this session's
    time to whatever it finds and would undo the correction.
    """
    with write() as cx:
        row = cx.execute(
            "SELECT * FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()
        if row is None:
            return None
        if row["status"] in LIVE:
            raise ValueError(
                f"group {group_id} has a live session ({row['status']}); "
                f"stop it first or its time will be added on top of this"
            )
        seconds = max(0, int(seconds))
        cx.execute(
            "UPDATE sessions SET gpu_seconds_used = ?"
            " WHERE group_id = ? AND assignment_id = ?",
            (seconds, group_id, assignment_id),
        )
        _log(
            cx, group_id, assignment_id, None, "budget",
            f"usage set to {seconds / 3600.0:.2f} node-hours "
            f"(was {(row['gpu_seconds_used'] or 0) / 3600.0:.2f})",
        )
        return seconds


def _set(group_id, assignment_id, **fields):
    # Column names are interpolated, values are bound. Every caller below passes
    # literal keyword names, so there is no path from a request to `fields`.
    # Keep it that way.
    cols = ", ".join(f"{k} = ?" for k in fields)
    with write() as cx:
        cx.execute(
            f"UPDATE sessions SET {cols} WHERE group_id = ? AND assignment_id = ?",
            (*fields.values(), group_id, assignment_id),
        )


def set_key(group_id, assignment_id, private_key):
    _set(group_id, assignment_id, private_key=private_key)


def node_count(row) -> int:
    """How many instances this session is supposed to have.

    Read from the row, never from config: the configured value can change
    mid-session, and everything downstream -- billing, the reaper's
    partial-cluster check -- has to mean "what this group was actually given".
    """
    try:
        return max(1, int(row["node_count"] or 1))
    except (KeyError, IndexError, TypeError, ValueError):
        return 1


def session_nodes(row) -> list:
    """This session's nodes, rank order, as [{rank, instance_id, private_ip, host}].

    Falls back to the singular instance_id/host columns, which is what a row
    written before multi-node looks like. That fallback is why the migration
    does not have to rewrite any existing data.
    """
    if row is None:
        return []
    raw = None
    try:
        raw = row["nodes"]
    except (KeyError, IndexError):
        pass
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        # Only dicts: a hand-edited or truncated row must not turn every
        # /session for this group into a 500.
        if isinstance(parsed, list):
            clean = [n for n in parsed if isinstance(n, dict) and n.get("instance_id")]
            if clean:
                return clean
    if row["instance_id"]:
        return [{
            "rank": 0,
            "instance_id": row["instance_id"],
            "private_ip": None,
            "host": row["host"],
        }]
    return []


def node_instance_ids(row) -> list:
    return [n["instance_id"] for n in session_nodes(row) if n.get("instance_id")]


def set_nodes(group_id, assignment_id, nodes, status=None):
    """Record the whole cluster, optionally promoting it in the same write.

    The only place `nodes`, `instance_id` and `host` are *set* (`mark_stopped`
    clears them). instance_id/host are rank 0, kept in step here rather than by
    any caller: two independent representations of one fact is exactly how they
    drift apart.
    """
    nodes = sorted(nodes, key=lambda n: n.get("rank", 0))
    head = nodes[0] if nodes else {}
    fields = {
        "nodes": json.dumps(nodes),
        "instance_id": head.get("instance_id"),
        "host": head.get("host"),
    }
    if status is not None:
        fields["status"] = status
    _set(group_id, assignment_id, **fields)


def set_running(group_id, assignment_id, nodes):
    """Promote to RUNNING and record the addresses in one transaction."""
    set_nodes(group_id, assignment_id, nodes, status=RUNNING)


def set_failed(group_id, assignment_id, error):
    _set(group_id, assignment_id, status=FAILED, last_error=str(error)[:500], private_key=None)


def usage_seconds(row, at=None) -> int:
    """Node-seconds used, including the session running right now. For display.

    Nothing that makes a decision calls this. The budget is charged at stop by
    `accrue_and_close` and checked at start by `claim`, both of which read the
    column directly; this exists so a student watching a live session is not
    shown a figure frozen at their last stop.

    Keep the arithmetic identical to `accrue_and_close`, or the number a group
    watches climb will not be the number they are eventually billed.
    """
    if row is None:
        return 0
    used = row["gpu_seconds_used"] or 0
    if row["status"] in LIVE:
        elapsed = max(0, (at or now()) - (row["started_at"] or now()))
        used += elapsed * node_count(row)
    return used


def accrue_and_close(group_id, assignment_id, reason, new_status=STOPPING):
    """Bill this session to the group and leave the live states.

    Idempotent: a second call finds a non-live status and does nothing, which is
    what lets the reaper run as often as it likes.

    Returns wall-clock seconds, but bills node-seconds: a two-node session
    costs twice as much per minute as a one-node session did, and a quota
    counted in wall-clock hours would quietly stop meaning anything.
    """
    with write() as cx:
        row = cx.execute(
            "SELECT * FROM sessions WHERE group_id = ? AND assignment_id = ?",
            (group_id, assignment_id),
        ).fetchone()
        if not row or row["status"] not in LIVE:
            return 0
        elapsed = max(0, now() - (row["started_at"] or now()))
        nodes = node_count(row)
        billed = elapsed * nodes
        cx.execute(
            "UPDATE sessions SET status = ?, gpu_seconds_used = gpu_seconds_used + ?,"
            "  last_stop_reason = ?, last_stopped_at = ?"
            " WHERE group_id = ? AND assignment_id = ?",
            (new_status, billed, reason, now(), group_id, assignment_id),
        )
        _log(
            cx, group_id, assignment_id, row["session_id"], "stop",
            f"{reason} ({nodes} node{'s' if nodes != 1 else ''})", billed,
        )
        return elapsed


def mark_stopped(group_id, assignment_id):
    """Final state. Drops the session key, which is what actually revokes access."""
    _set(
        group_id,
        assignment_id,
        status=STOPPED,
        session_id=None,
        host=None,
        nodes=None,
        private_key=None,
        expires_at=None,
    )


if __name__ == "__main__":  # python -m gpulease.db init
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init()
        print(f"database ready at {config.DB_PATH}")
    else:
        sys.exit("usage: python -m gpulease.db init")
