#!/usr/bin/env python3
"""
gs_health_monitor.py -- Great Sage Health Monitor
====================================================
Day 4 of the GS Evolution Roadmap.

Checks every daemon for actual OUTPUT production, not just running status.
Detects restart loops, silent failures, stale logs, and resource waste.
Reports to D1, event bus, and optionally Telegram.

Architecture:
  LaunchAgent plist scan -> Process check (pgrep) ->
  Log freshness check -> Output volume check ->
  Classify: HEALTHY / STALE / SILENT / RESTART_LOOP / DEAD ->
  Report to D1 + local SQLite + optional Telegram alert

CLI:
  python3 gs_health_monitor.py                    # one-shot health check
  python3 gs_health_monitor.py --daemon           # run every 15 min
  python3 gs_health_monitor.py --json             # output as JSON
  python3 gs_health_monitor.py --fix              # attempt auto-fixes for common issues
  python3 gs_health_monitor.py --history          # show health history

Integrates with:
  - gs_event_bus.py (writes health events)
  - gs_autonomous_loop.py (can trigger investigation tasks)
  - D1 bridge (reports to daemon_health table)
"""

import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import plistlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Tuple

# GS_AGENT_BUS_PATCHED

# GS_PUBSUB_PATCHED
try:
    from gs_pubsub import Publisher as _Pub
    _gs_pub = _Pub("gs_health")
    _gs_pub.emit_debug("system.daemon_crash".replace("crash","start"), {"daemon": "gs_health", "status": "starting"})
except Exception as _pse:
    _gs_pub = None
    import logging as _plog; _plog.getLogger("gs_health").warning(f"pubsub unavailable: {_pse}")
import sys as _gs_bus_sys
from pathlib import Path as _GSPath
_gs_bus_sys.path.insert(0, str(_GSPath(__file__).resolve().parent))

# Agent bus: construct the client here (cheap, no I/O), but do NOT register() at
# import time — register() makes a network call, and importing this module (e.g.
# for its helper functions) should never have that side effect. Registration is
# deferred to _ensure_bus_registered(), called lazily once we actually start
# running a health check.
_gs_bus = None
_gs_bus_registered = False
try:
    from gs_agent_bus import AgentBus as _AgentBus
    _gs_bus = _AgentBus('gs_health')
except Exception as _gs_bus_err:
    _gs_bus = None
    import logging as _gs_log
    _gs_log.getLogger('gs_health').warning(f'agent bus unavailable: {_gs_bus_err}')


def _ensure_bus_registered() -> None:
    """Register on the agent bus on first use, not at import time."""
    global _gs_bus_registered
    if _gs_bus is not None and not _gs_bus_registered:
        try:
            _gs_bus.register(capabilities=['system_health', 'daemon_watch', 'alerting'])
        except Exception as e:
            import logging as _gs_log
            _gs_log.getLogger('gs_health').warning(f'agent bus register failed: {e}')
        _gs_bus_registered = True

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOME = Path.home()
LAUNCHAGENTS_DIR = HOME / "Library" / "LaunchAgents"
STYLEZ_DIR = HOME / "Library" / "Stylez"
LOGS_DIRS = [
    HOME / "Library" / "Stylez" / "logs",
    HOME / "Library" / "Logs" / "Stylez",
    HOME / "Library" / "Logs",
]

DB_PATH = HOME / "workspace" / "code" / "gs_health_monitor.db"
LOG_FILE = HOME / "Library" / "Stylez" / "logs" / "gs_health_monitor.log"

BRIDGE_URL = os.getenv("GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query")
BRIDGE_KEY = os.getenv("GS_BRIDGE_KEY", "")

# Health thresholds
STALE_LOG_HOURS = 6          # log not updated in 6h = stale
RESTART_LOOP_THRESHOLD = 30  # ThrottleInterval <= 30s with KeepAlive = restart loop risk
SILENT_LOG_BYTES = 10        # log under 10 bytes = silent failure

# Daemon prefixes we care about. Kept in sync with daemon_watchdog.py's PREFIXES —
# the two health layers previously watched different fleets (this one was missing
# com.rise., daemon_watchdog was missing com.greatsage.), so a daemon under either
# missing prefix was invisible to one of the two monitors. Union of both.
DAEMON_PREFIXES = ("com.stylez.", "com.greatsage.", "com.stevestylez.", "com.rise.")

# Known retired daemons (don't flag these)
RETIRED_DAEMONS = {
    "com.stylez.souei-agent",
    "com.stylez.auto_chat",
    "com.stylez.gs_autopilot",
}

POLL_INTERVAL = 900  # 15 minutes for daemon mode

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if level in ("ERROR", "WARN"):
        print(line, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_tables():
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS health_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                daemon_label TEXT NOT NULL,
                daemon_script TEXT DEFAULT '',
                status TEXT NOT NULL,
                pid INTEGER DEFAULT 0,
                log_path TEXT DEFAULT '',
                log_age_hours REAL DEFAULT -1,
                log_size_bytes INTEGER DEFAULT -1,
                log_last_line TEXT DEFAULT '',
                error_pattern TEXT DEFAULT '',
                throttle_interval INTEGER DEFAULT -1,
                keep_alive INTEGER DEFAULT 0,
                recommendation TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_hc_daemon ON health_checks(daemon_label);
            CREATE INDEX IF NOT EXISTS idx_hc_status ON health_checks(status);
            CREATE INDEX IF NOT EXISTS idx_hc_time ON health_checks(timestamp);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plist Scanner
# ---------------------------------------------------------------------------


def scan_plists() -> List[Dict[str, Any]]:
    """Scan LaunchAgents for GS-related daemons."""
    daemons = []

    if not LAUNCHAGENTS_DIR.exists():
        return daemons

    for plist_path in sorted(LAUNCHAGENTS_DIR.glob("*.plist")):
        label = plist_path.stem

        # Only our daemons
        if not any(label.startswith(p) for p in DAEMON_PREFIXES):
            continue

        # Skip retired
        if label in RETIRED_DAEMONS:
            continue

        try:
            with open(plist_path, "rb") as f:
                pdata = plistlib.load(f)
        except Exception:
            # Binary plist fallback
            try:
                result = subprocess.run(
                    ["plutil", "-convert", "json", "-o", "-", str(plist_path)],
                    capture_output=True, text=True, timeout=5,
                )
                pdata = json.loads(result.stdout) if result.returncode == 0 else {}
            except Exception:
                pdata = {}

        if not pdata:
            continue

        args = pdata.get("ProgramArguments", [])
        script_path = args[-1] if len(args) >= 2 else (args[0] if args else "")

        daemon = {
            "label": label,
            "plist_path": str(plist_path),
            "script": script_path,
            "program_args": args,
            "stdout_log": pdata.get("StandardOutPath", ""),
            "stderr_log": pdata.get("StandardErrorPath", ""),
            "keep_alive": bool(pdata.get("KeepAlive", False)),
            "throttle_interval": pdata.get("ThrottleInterval", -1),
            "run_at_load": pdata.get("RunAtLoad", False),
            "start_interval": pdata.get("StartInterval", -1),
            "disabled": pdata.get("Disabled", False),
        }

        # Handle KeepAlive as dict (e.g., {"Crashed": true})
        ka = pdata.get("KeepAlive", False)
        if isinstance(ka, dict):
            daemon["keep_alive"] = True
            daemon["keep_alive_detail"] = ka
        elif isinstance(ka, bool):
            daemon["keep_alive"] = ka

        daemons.append(daemon)

    return daemons


# ---------------------------------------------------------------------------
# Process Checker
# ---------------------------------------------------------------------------


def check_process(daemon: Dict[str, Any]) -> Dict[str, Any]:
    """Check if daemon process is running."""
    script = daemon["script"]
    label = daemon["label"]

    # Try launchctl list first. `launchctl list` output is tab-separated
    # "PID\tExitStatus\tLabel" — split on tabs and compare the label field for an
    # EXACT match, mirroring daemon_watchdog.py. The previous `if label in line`
    # substring check could match the wrong daemon (e.g. label "gs_health" would
    # also match a line for "com.stylez.gs_health_v2").
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            pid_str, exit_str, line_label = parts
            if line_label != label:
                continue
            pid = int(pid_str) if pid_str != "-" else 0
            exit_code = int(exit_str) if exit_str != "-" else -1
            return {"running": pid > 0, "pid": pid, "exit_code": exit_code}
    except Exception:
        pass

    # Fallback: pgrep
    if script:
        try:
            result = subprocess.run(
                ["pgrep", "-f", Path(script).name],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p.isdigit()]
            if pids:
                return {"running": True, "pid": pids[0], "exit_code": 0}
        except Exception:
            pass

    return {"running": False, "pid": 0, "exit_code": -1}


# ---------------------------------------------------------------------------
# Log Analyzer
# ---------------------------------------------------------------------------


def analyze_log(log_path: str) -> Dict[str, Any]:
    """Check log freshness, size, and last line."""
    result = {
        "exists": False,
        "size_bytes": 0,
        "age_hours": -1,
        "last_line": "",
        "error_pattern": "",
        "error_count": 0,
    }

    if not log_path:
        return result

    p = Path(log_path)
    if not p.exists():
        return result

    result["exists"] = True

    try:
        stat = p.stat()
        result["size_bytes"] = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age = datetime.now(timezone.utc) - mtime
        result["age_hours"] = round(age.total_seconds() / 3600, 1)
    except Exception:
        pass

    # Read last lines for error patterns
    try:
        with open(p, "r", errors="replace") as f:
            # Seek to last 4KB
            f.seek(max(0, result["size_bytes"] - 4096))
            tail = f.read()

        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        if lines:
            result["last_line"] = lines[-1][:200]

        # Count error patterns
        error_keywords = ["error", "ERROR", "fail", "FAIL", "exception", "traceback", "Traceback"]
        error_lines = [l for l in lines if any(kw in l for kw in error_keywords)]
        result["error_count"] = len(error_lines)

        if error_lines:
            # Extract most common error pattern
            result["error_pattern"] = error_lines[-1][:200]

    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Health Classifier
# ---------------------------------------------------------------------------


# Status constants
HEALTHY = "HEALTHY"
STALE = "STALE"
SILENT = "SILENT"
RESTART_LOOP = "RESTART_LOOP"
DEAD = "DEAD"
ERROR_LOOP = "ERROR_LOOP"
DISABLED = "DISABLED"


def classify_health(daemon: Dict, process: Dict, log_info: Dict) -> Tuple[str, str]:
    """Classify daemon health and generate recommendation.

    log_info is the already-computed primary log analysis (see run_health_check),
    passed in so it is not re-derived here. Previously this function ignored
    log_info and called analyze_log() again on both stdout/stderr paths, doubling
    the file I/O for every daemon on every check for no reason.
    """

    # Disabled
    if daemon.get("disabled"):
        return DISABLED, "Daemon is disabled in plist"

    # Dead — not running, not disabled
    if not process["running"]:
        if daemon["keep_alive"]:
            return RESTART_LOOP, "KeepAlive is true but process is not running — likely crash-looping"
        return DEAD, f"Process not running (exit code: {process['exit_code']})"

    # Running but check output quality — use the caller-supplied analysis.
    primary_log = log_info

    # Silent failure — running but 0 output
    if not primary_log["exists"] or primary_log["size_bytes"] <= SILENT_LOG_BYTES:
        return SILENT, "Running but no log output — possible silent failure or missing log path"

    # Stale — log not updated recently
    if primary_log["age_hours"] > STALE_LOG_HOURS:
        return STALE, f"Log not updated in {primary_log['age_hours']:.1f}h (threshold: {STALE_LOG_HOURS}h)"

    # Error loop — recent log is all errors
    if primary_log["error_count"] > 5:
        # Check if errors are the majority of recent output
        return ERROR_LOOP, f"High error density ({primary_log['error_count']} errors in tail): {primary_log['error_pattern'][:100]}"

    # Restart loop detection
    throttle = daemon.get("throttle_interval", -1)
    if isinstance(throttle, int) and 0 < throttle <= RESTART_LOOP_THRESHOLD and daemon["keep_alive"]:
        # Check if log shows rapid restarts
        if "BOOT" in primary_log.get("last_line", "") or "start" in primary_log.get("last_line", "").lower():
            return RESTART_LOOP, f"KeepAlive + ThrottleInterval={throttle}s + recent boot message"

    return HEALTHY, "Running with recent output"


# ---------------------------------------------------------------------------
# Full Health Check
# ---------------------------------------------------------------------------


def run_health_check(output_json: bool = False) -> List[Dict[str, Any]]:
    """Run a full health check on all daemons."""
    _ensure_bus_registered()
    ensure_tables()
    daemons = scan_plists()
    results = []

    for daemon in daemons:
        process = check_process(daemon)
        stdout_info = analyze_log(daemon["stdout_log"])
        stderr_info = analyze_log(daemon["stderr_log"])
        primary_log = stdout_info if stdout_info["exists"] else stderr_info

        status, recommendation = classify_health(daemon, process, primary_log)

        result = {
            "label": daemon["label"],
            "script": daemon["script"],
            "status": status,
            "pid": process["pid"],
            "running": process["running"],
            "exit_code": process["exit_code"],
            "log_path": daemon["stdout_log"] or daemon["stderr_log"],
            "log_age_hours": primary_log.get("age_hours", -1),
            "log_size_bytes": primary_log.get("size_bytes", 0),
            "log_last_line": primary_log.get("last_line", ""),
            "error_pattern": primary_log.get("error_pattern", ""),
            "error_count": primary_log.get("error_count", 0),
            "throttle_interval": daemon.get("throttle_interval", -1),
            "keep_alive": daemon.get("keep_alive", False),
            "recommendation": recommendation,
        }
        results.append(result)

        # Store to SQLite
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO health_checks "
                "(daemon_label, daemon_script, status, pid, log_path, "
                "log_age_hours, log_size_bytes, log_last_line, error_pattern, "
                "throttle_interval, keep_alive, recommendation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    result["label"], result["script"], status, result["pid"],
                    result["log_path"], result["log_age_hours"],
                    result["log_size_bytes"], result["log_last_line"][:500],
                    result["error_pattern"][:500],
                    result["throttle_interval"], int(result["keep_alive"]),
                    recommendation[:500],
                ],
            )
            conn.commit()
        finally:
            conn.close()

    # Report to D1
    _report_to_d1(results)

    if output_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        _print_report(results)

    return results


def _report_to_d1(results: List[Dict]) -> None:
    """Report health summary to D1."""
    import urllib.request
    import urllib.error

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "healthy": sum(1 for r in results if r["status"] == HEALTHY),
        "stale": sum(1 for r in results if r["status"] == STALE),
        "silent": sum(1 for r in results if r["status"] == SILENT),
        "dead": sum(1 for r in results if r["status"] == DEAD),
        "error_loop": sum(1 for r in results if r["status"] == ERROR_LOOP),
        "restart_loop": sum(1 for r in results if r["status"] == RESTART_LOOP),
    }

    try:
        sql = (
            "INSERT INTO daemon_health_log (timestamp, total, healthy, stale, silent, dead, error_loop, restart_loop, details) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)"
        )

        # Ensure table exists
        create_sql = (
            "CREATE TABLE IF NOT EXISTS daemon_health_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, total INTEGER, healthy INTEGER, "
            "stale INTEGER, silent INTEGER, dead INTEGER, "
            "error_loop INTEGER, restart_loop INTEGER, details TEXT)"
        )

        # Create table
        create_payload = json.dumps({"sql": create_sql}).encode()
        req = urllib.request.Request(BRIDGE_URL, data=create_payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-GS-Key", BRIDGE_KEY)
        req.add_header("User-Agent", "GS-HealthMonitor/1.0")
        urllib.request.urlopen(req, timeout=8)

        # Insert report
        details = json.dumps([{"label": r["label"], "status": r["status"]} for r in results])
        insert_payload = json.dumps({
            "sql": sql,
            "params": [
                summary["timestamp"], summary["total"], summary["healthy"],
                summary["stale"], summary["silent"], summary["dead"],
                summary["error_loop"], summary["restart_loop"], details,
            ],
        }).encode()
        req2 = urllib.request.Request(BRIDGE_URL, data=insert_payload, method="POST")
        req2.add_header("Content-Type", "application/json")
        req2.add_header("X-GS-Key", BRIDGE_KEY)
        req2.add_header("User-Agent", "GS-HealthMonitor/1.0")
        urllib.request.urlopen(req2, timeout=8)

    except Exception as e:
        _log(f"D1 report failed: {e}", "WARN")


def _print_report(results: List[Dict]) -> None:
    """Print a human-readable health report."""
    # Sort: problems first
    priority = {RESTART_LOOP: 0, ERROR_LOOP: 1, DEAD: 2, SILENT: 3, STALE: 4, DISABLED: 5, HEALTHY: 6}
    results.sort(key=lambda r: priority.get(r["status"], 99))

    print("=" * 80)
    print("GS HEALTH MONITOR REPORT")
    print(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 80)

    # Summary
    statuses = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1

    print(f"\nDaemons scanned: {len(results)}")
    for s, count in sorted(statuses.items(), key=lambda x: priority.get(x[0], 99)):
        icon = {"HEALTHY": "+", "STALE": "~", "SILENT": "?", "DEAD": "X", "ERROR_LOOP": "!", "RESTART_LOOP": "!!", "DISABLED": "-"}
        print(f"  [{icon.get(s, '?')}] {s:15s} {count}")

    # Detail table
    print(f"\n{'Label':40s} {'Status':14s} {'PID':>6s} {'Log Age':>8s} {'Errors':>6s}")
    print("-" * 80)

    for r in results:
        age_str = f"{r['log_age_hours']:.1f}h" if r["log_age_hours"] >= 0 else "N/A"
        pid_str = str(r["pid"]) if r["pid"] else "-"
        errors_str = str(r["error_count"]) if r["error_count"] else "-"
        label_short = r["label"][:38]
        print(f"  {label_short:38s} {r['status']:14s} {pid_str:>6s} {age_str:>8s} {errors_str:>6s}")
        if r["status"] not in (HEALTHY, DISABLED):
            print(f"    -> {r['recommendation'][:70]}")

    print()


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


def attempt_fixes(results: List[Dict]) -> List[str]:
    """Attempt safe auto-fixes for common issues."""
    fixes = []

    for r in results:
        label = r["label"]
        status = r["status"]

        if status == DEAD and r.get("keep_alive"):
            # Try reloading the plist
            plist = LAUNCHAGENTS_DIR / f"{label}.plist"
            if plist.exists():
                try:
                    subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=5)
                    fixes.append(f"Reloaded {label}")
                except Exception as e:
                    fixes.append(f"Failed to reload {label}: {e}")

        elif status == STALE:
            # Restart the daemon
            plist = LAUNCHAGENTS_DIR / f"{label}.plist"
            if plist.exists():
                try:
                    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=5)
                    time.sleep(1)
                    subprocess.run(["launchctl", "load", str(plist)], capture_output=True, timeout=5)
                    fixes.append(f"Restarted stale daemon {label}")
                except Exception as e:
                    fixes.append(f"Failed to restart {label}: {e}")

    return fixes


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def show_history(limit: int = 20):
    """Show recent health check history."""
    ensure_tables()
    conn = _get_db()
    try:
        # Get unique check timestamps
        timestamps = conn.execute(
            "SELECT DISTINCT timestamp FROM health_checks ORDER BY timestamp DESC LIMIT ?",
            [limit],
        ).fetchall()

        if not timestamps:
            print("No health check history yet.")
            return

        for ts_row in timestamps:
            ts = ts_row["timestamp"]
            rows = conn.execute(
                "SELECT daemon_label, status, pid, recommendation "
                "FROM health_checks WHERE timestamp = ? ORDER BY status, daemon_label",
                [ts],
            ).fetchall()

            healthy = sum(1 for r in rows if r["status"] == HEALTHY)
            total = len(rows)
            problems = total - healthy

            print(f"\n[{ts}] {total} daemons | {healthy} healthy | {problems} problems")
            for r in rows:
                if r["status"] != HEALTHY:
                    print(f"  {r['status']:14s} {r['daemon_label']}")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------


TG_ALERT_THRESHOLD = 10  # alert when problem count exceeds this


def _tg_alert(msg: str) -> None:
    """Send Telegram alert. Reads token/chat from env or .env file."""
    import subprocess
    env_file = os.path.expanduser("~/Library/Stylez/.env")
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not tok or not chat) and os.path.exists(env_file):
        for line in open(env_file):
            if not tok and line.startswith("TELEGRAM_BOT_TOKEN="):
                tok = line.strip().split("=", 1)[1].strip('"\'')
            elif not chat and line.startswith("TELEGRAM_CHAT_ID="):
                chat = line.strip().split("=", 1)[1].strip('"\'')
    if not tok or not chat:
        return
    try:
        subprocess.run(
            ["curl", "-s", f"https://api.telegram.org/bot{tok}/sendMessage",
             "-d", f"chat_id={chat}", "-d", f"text={msg[:1000]}"],
            timeout=8, capture_output=True
        )
    except Exception:
        pass


class HealthMonitorDaemon:
    def __init__(self):
        self.running = True
        self._last_alert_count = -1
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, signum, frame):
        _log(f"Signal {signum}, shutting down")
        self.running = False

    def run(self):
        _log("=== Health Monitor daemon starting ===")
        while self.running:
            try:
                results = run_health_check(output_json=False)
                problems = [r for r in results if r["status"] not in (HEALTHY, DISABLED)]
                problem_count = len(problems)
                if problems:
                    _log(f"Health check: {problem_count} problems out of {len(results)} daemons", "WARN")
                    if problem_count > TG_ALERT_THRESHOLD and problem_count != self._last_alert_count:
                        names = ", ".join(p["label"] for p in problems[:5])
                        _tg_alert(
                            f"GS HEALTH ALERT: {problem_count}/{len(results)} daemons degraded.\n"
                            f"Top problems: {names}{'...' if problem_count > 5 else ''}"
                        )
                        self._last_alert_count = problem_count
                else:
                    _log(f"Health check: all {len(results)} daemons healthy")
                    self._last_alert_count = -1
            except Exception as e:
                _log(f"Health check failed: {e}", "ERROR")

            # Sleep in 1s chunks for responsive shutdown
            for _ in range(POLL_INTERVAL):
                if not self.running:
                    break
                time.sleep(1)

        _log("=== Health Monitor daemon stopped ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="GS Health Monitor — checks daemon output, not just running status",
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daemon", action="store_true", help="Run as persistent monitor (every 15 min)")
    group.add_argument("--json", action="store_true", help="Output health report as JSON")
    group.add_argument("--fix", action="store_true", help="Attempt auto-fixes for common issues")
    group.add_argument("--history", action="store_true", help="Show health check history")

    args = parser.parse_args()

    if args.daemon:
        daemon = HealthMonitorDaemon()
        daemon.run()
    elif args.json:
        run_health_check(output_json=True)
    elif args.fix:
        results = run_health_check(output_json=False)
        fixes = attempt_fixes(results)
        if fixes:
            print("\nAuto-fixes applied:")
            for f in fixes:
                print(f"  {f}")
        else:
            print("\nNo auto-fixes needed.")
    elif args.history:
        show_history()
    else:
        run_health_check(output_json=False)


if __name__ == "__main__":
    main()
