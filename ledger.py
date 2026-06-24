#!/usr/bin/env python3
"""
ledger - a tiny claim board so multiple agents stop stepping on each other.

When you run more than one agent in the same workspace (cron jobs, subagents,
a couple of terminals), they don't know what the others are doing. Two of them
reindex the same table. One overwrites a file the other is still writing. You
find out at 3am.

This is the smallest thing that fixes it: a shared ledger. Before an agent
does a job, it *claims* it. If someone else already holds that claim, the claim
fails and the agent moves on. When it's done, it releases. That's the whole idea.

Storage is one SQLite file. No server, no daemon, no dependencies - just Python.
Claims are atomic across processes because SQLite does the locking for us.

  ledger claim "reindex wallets"      # take it, prints an id
  ledger beat <id>                    # still alive, keep it
  ledger done <id>                    # finished, release
  ledger drop <id>                    # gave up, release
  ledger ls                           # who's holding what
  ledger log                          # what happened recently
  ledger gc                           # reap claims that died mid-job

The database lives at ./.agent-ledger.db by default (one per workspace).
Override with $LEDGER_DB. Your agent's name comes from $LEDGER_OWNER, then
$AGENT_NAME, then host:pid.
"""
import os
import sys
import time
import json
import uuid
import socket
import sqlite3
import argparse
from datetime import datetime, timezone

# A claim whose owner hasn't checked in for this long is presumed dead and can
# be taken over. Tune per workspace with $LEDGER_TTL (seconds).
DEFAULT_TTL = int(os.environ.get("LEDGER_TTL", "900"))  # 15 minutes


def now() -> int:
    return int(time.time())


def db_path() -> str:
    return os.environ.get("LEDGER_DB", os.path.join(os.getcwd(), ".agent-ledger.db"))


def whoami() -> str:
    name = os.environ.get("LEDGER_OWNER") or os.environ.get("AGENT_NAME")
    if name:
        return name
    return f"{socket.gethostname()}:{os.getpid()}"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claims (
            id         TEXT PRIMARY KEY,
            task       TEXT NOT NULL,
            owner      TEXT NOT NULL,
            status     TEXT NOT NULL,           -- active | done | dropped | expired
            note       TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            beat_at    INTEGER NOT NULL
        )
        """
    )
    # The trick that makes claiming atomic: only one *active* claim per task can
    # exist at a time. A second claim on the same task hits this index and fails,
    # even from another process, because SQLite serializes the write.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_task
        ON claims(task) WHERE status = 'active'
        """
    )
    return conn


def reap_stale(conn: sqlite3.Connection, ttl: int, task: str = None) -> int:
    """Mark abandoned claims as expired. Returns how many were reaped."""
    cutoff = now() - ttl
    if task is None:
        cur = conn.execute(
            "UPDATE claims SET status='expired', updated_at=? "
            "WHERE status='active' AND beat_at < ?",
            (now(), cutoff),
        )
    else:
        cur = conn.execute(
            "UPDATE claims SET status='expired', updated_at=? "
            "WHERE status='active' AND task=? AND beat_at < ?",
            (now(), task, cutoff),
        )
    return cur.rowcount


# --- commands ---------------------------------------------------------------


def cmd_claim(args) -> int:
    conn = connect()
    owner = whoami()
    task = args.task
    deadline = now() + args.wait

    while True:
        # Take over a claim whose owner went silent before we try ours.
        reap_stale(conn, args.ttl, task)
        try:
            cid = uuid.uuid4().hex[:12]
            t = now()
            conn.execute(
                "INSERT INTO claims(id, task, owner, status, note, created_at, updated_at, beat_at) "
                "VALUES(?,?,?,'active',?,?,?,?)",
                (cid, task, owner, args.note, t, t, t),
            )
            print(cid)
            return 0
        except sqlite3.IntegrityError:
            held = conn.execute(
                "SELECT owner, beat_at FROM claims WHERE task=? AND status='active'",
                (task,),
            ).fetchone()
            if args.wait and now() < deadline:
                time.sleep(min(2, max(0, deadline - now())))
                continue
            who = held["owner"] if held else "someone"
            ago = now() - held["beat_at"] if held else 0
            sys.stderr.write(f"held by {who} (last seen {ago}s ago)\n")
            return 1


def _resolve(conn, cid_or_task):
    """Accept either a claim id or the exact task string."""
    row = conn.execute(
        "SELECT * FROM claims WHERE id=? AND status='active'", (cid_or_task,)
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM claims WHERE task=? AND status='active'", (cid_or_task,)
    ).fetchone()


def _release(args, status: str) -> int:
    conn = connect()
    row = _resolve(conn, args.id)
    if not row:
        sys.stderr.write("no active claim by that id or task\n")
        return 1
    conn.execute(
        "UPDATE claims SET status=?, updated_at=? WHERE id=?",
        (status, now(), row["id"]),
    )
    return 0


def cmd_done(args):
    return _release(args, "done")


def cmd_drop(args):
    return _release(args, "dropped")


def cmd_beat(args) -> int:
    conn = connect()
    row = _resolve(conn, args.id)
    if not row:
        sys.stderr.write("no active claim by that id or task\n")
        return 1
    conn.execute("UPDATE claims SET beat_at=? WHERE id=?", (now(), row["id"]))
    return 0


def _fmt_age(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def cmd_ls(args) -> int:
    conn = connect()
    reap_stale(conn, args.ttl)
    rows = conn.execute(
        "SELECT * FROM claims WHERE status='active' ORDER BY created_at"
    ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    if not rows:
        print("nothing claimed")
        return 0
    print(f"{'ID':<14}{'OWNER':<22}{'HELD':<7}TASK")
    for r in rows:
        print(f"{r['id']:<14}{r['owner'][:21]:<22}{_fmt_age(now()-r['created_at']):<7}{r['task']}")
    return 0


def cmd_log(args) -> int:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM claims ORDER BY updated_at DESC LIMIT ?", (args.limit,)
    ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    if not rows:
        print("empty")
        return 0
    for r in rows:
        when = datetime.fromtimestamp(r["updated_at"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"{when}  {r['status']:<8}{r['owner'][:18]:<20}{r['task']}")
    return 0


def cmd_gc(args) -> int:
    conn = connect()
    n = reap_stale(conn, args.ttl)
    print(f"reaped {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledger", description="shared claim board for agents")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL,
                   help=f"seconds before a silent claim is reclaimable (default {DEFAULT_TTL})")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("claim", help="take a task")
    c.add_argument("task")
    c.add_argument("--note", default=None, help="optional context")
    c.add_argument("--wait", type=int, default=0, metavar="SECS",
                   help="wait up to SECS for the task to free instead of failing")
    c.set_defaults(func=cmd_claim)

    for name, fn, helptext in [
        ("done", cmd_done, "release a finished claim"),
        ("drop", cmd_drop, "release without finishing"),
        ("beat", cmd_beat, "heartbeat: prove you're still working"),
    ]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id", help="claim id or the exact task string")
        s.set_defaults(func=fn)

    s = sub.add_parser("ls", help="list active claims", aliases=["status"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("log", help="recent history")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("gc", help="reap claims whose owner went silent")
    s.set_defaults(func=cmd_gc)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
