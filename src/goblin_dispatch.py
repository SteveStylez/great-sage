#!/usr/bin/env python3
"""
Goblin Rider Dispatch Tracker
Monitors background coding sub-agents (Goblin Riders) for heartbeat/completion
and logs results to D1 via the Sage Bridge.

Usage:
  python3 goblin_dispatch.py --status         # show all riders this session
  python3 goblin_dispatch.py --timeout-check  # scan and mark timeouts
  python3 goblin_dispatch.py --report         # summary counts by status
  python3 goblin_dispatch.py --init           # create D1 table (run once)
"""

import json
import logging
import os
import sys
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Bridge config (all secrets from environment) ─────────────────────────────
BRIDGE_URL = os.getenv("GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query")
BRIDGE_KEY = os.getenv("GS_BRIDGE_KEY", "")
DB_ID      = os.getenv("GS_D1_DB_ID", "")
USER_AGENT = "GreatSage-GoblinDispatch/1.0"

# ── Telegram config (all credentials from environment) ───────────────────────
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_PATH = os.path.join(
    os.path.expanduser("~"), "Library", "Logs", "Stylez", "goblin_dispatch.log"
)
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [goblin_dispatch] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("goblin_dispatch")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS goblin_dispatch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rider_name TEXT,
  task_description TEXT,
  output_file TEXT,
  status TEXT DEFAULT 'running',
  started_at TEXT,
  completed_at TEXT,
  result_summary TEXT,
  session_id TEXT
)
""".strip()


# ── Bridge helper ─────────────────────────────────────────────────────────────
def _bridge_query(sql: str, params: Optional[list] = None) -> dict:
    """POST a SQL query to the Sage Bridge and return parsed JSON."""
    payload = json.dumps({
        "sql": sql,
        "params": params or [],
        "db_id": DB_ID,
    }).encode("utf-8")

    req = urllib.request.Request(
        BRIDGE_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-GS-Key": BRIDGE_KEY,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"D1 error: {data['error']}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("Bridge HTTP %d: %s", e.code, body[:300])
        raise
    except urllib.error.URLError as e:
        log.error("Bridge network error: %s", e.reason)
        raise
    except json.JSONDecodeError as exc:
        log.error("Bridge returned non-JSON response")
        raise RuntimeError("Bridge returned non-JSON") from exc


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Telegram helper ───────────────────────────────────────────────────────────
def _send_telegram(message: str) -> bool:
    """Send a Telegram message via the bot. Returns True on success."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram not configured — skipping notification")
        return False
    try:
        payload = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


# ── Table init ────────────────────────────────────────────────────────────────
def init_table() -> None:
    """Create goblin_dispatch table in D1 if it does not exist."""
    _bridge_query(CREATE_TABLE_SQL)
    log.info("Table 'goblin_dispatch' ensured in D1.")
    print("[goblin_dispatch] Table 'goblin_dispatch' ensured in D1.")


# ── Public API ────────────────────────────────────────────────────────────────
def register_rider(
    rider_name: str,
    task_description: str,
    output_file: str,
    session_id: str,
) -> int:
    """
    Register a new Goblin Rider job as 'running'.
    Returns the auto-assigned row id.
    """
    sql = (
        "INSERT INTO goblin_dispatch "
        "(rider_name, task_description, output_file, status, started_at, session_id) "
        "VALUES (?, ?, ?, 'running', ?, ?) "
        "RETURNING id"
    )
    params = [rider_name, task_description, output_file, _now_utc(), session_id]
    result = _bridge_query(sql, params)

    # D1 RETURNING rows come back as a list or a dict with 'results'
    if isinstance(result, list) and result:
        row = result[0]
    elif isinstance(result, dict):
        rows = result.get("results", result.get("rows", [result]))
        row = rows[0] if rows else result
    else:
        raise RuntimeError(f"Unexpected RETURNING response: {result}")

    rider_id = row.get("id") if isinstance(row, dict) else None
    if rider_id is None:
        # Fallback: fetch last insert rowid
        last = _bridge_query(
            "SELECT id FROM goblin_dispatch WHERE rider_name=? AND session_id=? "
            "ORDER BY id DESC LIMIT 1",
            [rider_name, session_id],
        )
        rows2 = last if isinstance(last, list) else last.get("results", [last])
        rider_id = rows2[0]["id"] if rows2 else -1

    log.info("Registered rider '%s' → id=%s", rider_name, rider_id)
    print(f"[goblin_dispatch] Registered rider '{rider_name}' → id={rider_id}")
    return int(rider_id)


def mark_complete(rider_id: int, result_summary: str) -> None:
    """Mark a Rider as completed with a result summary."""
    sql = (
        "UPDATE goblin_dispatch "
        "SET status='completed', completed_at=?, result_summary=? "
        "WHERE id=?"
    )
    _bridge_query(sql, [_now_utc(), result_summary, rider_id])
    log.info("Rider id=%d marked completed", rider_id)
    print(f"[goblin_dispatch] Rider id={rider_id} → completed")


def mark_failed(rider_id: int, error_msg: str) -> None:
    """Mark a Rider as failed with an error message."""
    sql = (
        "UPDATE goblin_dispatch "
        "SET status='failed', completed_at=?, result_summary=? "
        "WHERE id=?"
    )
    _bridge_query(sql, [_now_utc(), error_msg, rider_id])
    log.info("Rider id=%d marked failed: %s", rider_id, error_msg[:100])
    print(f"[goblin_dispatch] Rider id={rider_id} → failed")


def get_active_riders() -> list:
    """Return all riders with status='running'."""
    sql = "SELECT * FROM goblin_dispatch WHERE status='running' ORDER BY started_at"
    result = _bridge_query(sql)
    rows = result if isinstance(result, list) else result.get("results", [])
    return rows


def check_timeouts(max_minutes: int = 30, notify: bool = True) -> list:
    """
    Find riders running longer than max_minutes and mark them 'timeout'.
    Sends a Telegram alert if any riders timed out and notify=True.
    Returns list of timed-out rider dicts.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=max_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    sql = (
        "SELECT * FROM goblin_dispatch "
        "WHERE status='running' AND started_at < ?"
    )
    result = _bridge_query(sql, [cutoff])
    stale = result if isinstance(result, list) else result.get("results", [])

    timed_out = []
    for row in stale:
        rider_id = row["id"]
        update_sql = (
            "UPDATE goblin_dispatch "
            "SET status='timeout', completed_at=?, result_summary=? "
            "WHERE id=?"
        )
        _bridge_query(
            update_sql,
            [_now_utc(), f"Auto-timeout after >{max_minutes}min", rider_id],
        )
        timed_out.append(row)
        log.warning(
            "Rider id=%d '%s' timed out after >%dmin",
            rider_id, row.get("rider_name"), max_minutes,
        )
        print(f"[goblin_dispatch] Rider id={rider_id} '{row.get('rider_name')}' → timeout")

    if timed_out and notify:
        names = ", ".join(r.get("rider_name", "?") for r in timed_out)
        _send_telegram(
            f"<b>Goblin Rider Timeout</b>\n"
            f"{len(timed_out)} rider(s) exceeded {max_minutes}min threshold.\n"
            f"Riders: {names}\n"
            f"Run <code>goblin_dispatch --status</code> to inspect."
        )

    return timed_out


def get_session_report(session_id: Optional[str] = None) -> str:
    """
    Return a formatted status block suitable for inclusion in ARISE boot output.
    If session_id is None, shows riders from the last 24 hours.
    """
    if session_id:
        sql = "SELECT * FROM goblin_dispatch WHERE session_id=? ORDER BY id"
        result = _bridge_query(sql, [session_id])
    else:
        sql = (
            "SELECT * FROM goblin_dispatch "
            "WHERE started_at >= datetime('now','-1 day') ORDER BY id DESC LIMIT 20"
        )
        result = _bridge_query(sql)

    rows = result if isinstance(result, list) else result.get("results", [])

    if not rows:
        return "--- GOBLIN RIDERS: none in last 24h ---"

    running  = [r for r in rows if r.get("status") == "running"]
    complete = [r for r in rows if r.get("status") == "completed"]
    failed   = [r for r in rows if r.get("status") in ("failed", "timeout")]

    lines = ["--- GOBLIN RIDERS ---"]
    lines.append(f"  running={len(running)}  completed={len(complete)}  failed/timeout={len(failed)}")

    if running:
        lines.append("  ACTIVE:")
        for r in running:
            lines.append(f"    [{r['id']}] {r.get('rider_name','?')} | started={r.get('started_at','?')[:16]}")

    if failed:
        lines.append("  NEEDS ATTENTION:")
        for r in failed:
            lines.append(
                f"    [{r['id']}] {r.get('rider_name','?')} | {r.get('status')} | "
                f"{str(r.get('result_summary',''))[:60]}"
            )

    return "\n".join(lines)


# ── CLI helpers ───────────────────────────────────────────────────────────────
def _fmt_row(row: dict) -> str:
    return (
        f"  [{row.get('id')}] {row.get('rider_name','?')} | "
        f"{row.get('status','?')} | "
        f"started={row.get('started_at','?')} | "
        f"file={row.get('output_file','?')} | "
        f"summary={str(row.get('result_summary',''))[:60]}"
    )


def cmd_status(session_id: Optional[str]) -> None:
    if session_id:
        sql = (
            "SELECT * FROM goblin_dispatch WHERE session_id=? ORDER BY id"
        )
        result = _bridge_query(sql, [session_id])
    else:
        sql = "SELECT * FROM goblin_dispatch ORDER BY id DESC LIMIT 50"
        result = _bridge_query(sql)

    rows = result if isinstance(result, list) else result.get("results", [])

    if not rows:
        print("[goblin_dispatch] No riders found.")
        return

    print(f"\n=== GOBLIN RIDER STATUS ({len(rows)} riders) ===")
    for row in rows:
        print(_fmt_row(row))
    print()


def cmd_timeout_check(max_minutes: int = 30) -> None:
    print(f"\n=== TIMEOUT CHECK (threshold={max_minutes}min) ===")
    timed_out = check_timeouts(max_minutes)
    if timed_out:
        print(f"Marked {len(timed_out)} rider(s) as timeout.")
    else:
        print("No stale riders found.")
    print()


def cmd_report() -> None:
    sql = (
        "SELECT status, COUNT(*) as cnt FROM goblin_dispatch "
        "GROUP BY status ORDER BY status"
    )
    result = _bridge_query(sql)
    rows = result if isinstance(result, list) else result.get("results", [])

    print("\n=== GOBLIN RIDER REPORT ===")
    totals: dict = {}
    for row in rows:
        status = row.get("status", "?")
        cnt = row.get("cnt", 0)
        totals[status] = cnt
        print(f"  {status:12s}: {cnt}")

    total_all = sum(totals.values())
    print(f"  {'TOTAL':12s}: {total_all}")
    print()


def cmd_session_report(session_id: Optional[str] = None) -> None:
    print(get_session_report(session_id))


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="goblin_dispatch",
        description="Goblin Rider Dispatch Tracker — monitors background GS sub-agents",
    )
    parser.add_argument("--init", action="store_true", help="Create D1 table")
    parser.add_argument("--status", action="store_true", help="Show all riders this session")
    parser.add_argument("--timeout-check", action="store_true", dest="timeout_check",
                        help="Scan and mark timed-out riders")
    parser.add_argument("--report", action="store_true", help="Summary counts by status")
    parser.add_argument("--arise-report", action="store_true", dest="arise_report",
                        help="Print rider status block formatted for ARISE boot output")
    parser.add_argument("--max-minutes", type=int, default=30,
                        help="Timeout threshold in minutes (default: 30)")
    parser.add_argument("--session", type=str,
                        default=os.getenv("SESSION_ID"),
                        help="Filter --status/--arise-report by session ID")
    parser.add_argument("--no-notify", action="store_true", dest="no_notify",
                        help="Suppress Telegram alerts on timeout-check")
    parser.add_argument("--register-rider", action="store_true", dest="register_rider",
                        help="Register a new rider (use with --rider-name)")
    parser.add_argument("--rider-name", type=str, default="unnamed_rider",
                        help="Name/description of the rider to register")
    parser.add_argument("--task-desc", type=str, default="",
                        help="Task description for the rider")
    parser.add_argument("--output-file", type=str, default="",
                        help="Expected output file for the rider")
    parser.add_argument("--mark-complete", type=int, default=None, dest="mark_complete",
                        metavar="RIDER_ID",
                        help="Mark rider as completed (use with --summary)")
    parser.add_argument("--mark-failed", type=int, default=None, dest="mark_failed",
                        metavar="RIDER_ID",
                        help="Mark rider as failed (use with --summary)")
    parser.add_argument("--summary", type=str, default="",
                        help="Result summary for --mark-complete/--mark-failed")
    parser.add_argument("--mark-all-stale", action="store_true", dest="mark_all_stale",
                        help="Mark all riders older than --max-minutes as completed (cleanup)")

    args = parser.parse_args()

    if args.init:
        init_table()
    elif args.mark_complete is not None:
        mark_complete(args.mark_complete, args.summary or "Completed")
    elif args.mark_failed is not None:
        mark_failed(args.mark_failed, args.summary or "Failed")
    elif args.mark_all_stale:
        stale = check_timeouts(args.max_minutes, notify=False)
        if stale:
            for r in stale:
                mark_complete(r["id"], f"Auto-completed (stale >{args.max_minutes}min)")
            print(f"[goblin_dispatch] Marked {len(stale)} stale riders as completed")
        else:
            print("[goblin_dispatch] No stale riders found")
    elif args.register_rider:
        session = args.session or os.getenv("SESSION_ID", "unknown")
        register_rider(
            rider_name=args.rider_name,
            task_description=args.task_desc or args.rider_name,
            output_file=args.output_file,
            session_id=session,
        )
    elif args.status:
        cmd_status(args.session)
    elif args.timeout_check:
        cmd_timeout_check(args.max_minutes)
    elif args.report:
        cmd_report()
    elif args.arise_report:
        cmd_session_report(args.session)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
