#!/usr/bin/env python3
"""
daemon_watchdog.py - RISE Holdings Daemon Health Monitor
Scans all com.stylez/stevestylez/rise LaunchAgents, detects restart loops,
D1 abuse risk, and generates actionable reports.

Usage:
    python3 ~/workspace/code/daemon_watchdog.py           # Full report
    python3 ~/workspace/code/daemon_watchdog.py --arise    # Compact 10-line boot summary
"""

import argparse
import glob
import os
import plistlib
import re
import subprocess
from datetime import datetime

LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
REPORT_PATH = os.path.expanduser("~/workspace/reports/daemon_watchdog_report.md")
# Kept in sync with gs_health_monitor.py's DAEMON_PREFIXES — union of both fleets so
# neither monitor is blind to daemons the other one covers.
PREFIXES = ("com.stylez.", "com.stevestylez.", "com.rise.", "com.greatsage.")


def parse_plists():
    """Read all matching plists and extract config."""
    daemons = []
    for plist_path in sorted(glob.glob(os.path.join(LAUNCH_AGENTS_DIR, "*.plist"))):
        basename = os.path.basename(plist_path)
        if not any(basename.startswith(p) for p in PREFIXES):
            continue
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue

        label = data.get("Label", basename.replace(".plist", ""))
        prog_args = data.get("ProgramArguments", [])
        keep_alive = data.get("KeepAlive", False)
        throttle = data.get("ThrottleInterval", 10)  # macOS default is 10s
        start_interval = data.get("StartInterval")
        run_at_load = data.get("RunAtLoad", False)
        calendar = data.get("StartCalendarInterval")

        # Resolve script path from ProgramArguments
        script_path = None
        for arg in prog_args:
            if arg.endswith(".py") or arg.endswith(".sh"):
                script_path = arg
                break

        daemons.append({
            "label": label,
            "plist_path": plist_path,
            "prog_args": prog_args,
            "keep_alive": keep_alive,
            "throttle": throttle,
            "start_interval": start_interval,
            "run_at_load": run_at_load,
            "calendar": calendar,
            "script_path": script_path,
        })
    return daemons


def check_script_for_exit(script_path):
    """Scan script for sys.exit/exit() calls that cause restart loops with KeepAlive."""
    if not script_path or not os.path.isfile(script_path):
        return False, []
    try:
        with open(script_path, "r", errors="replace") as f:
            content = f.read()
    except Exception:
        return False, []

    patterns = [
        r'\bsys\.exit\s*\(',
        r'\bexit\s*\(\s*[0-9]*\s*\)',
        r'\bos\._exit\s*\(',
        r'\braise\s+SystemExit',
    ]
    hits = []
    for pat in patterns:
        for m in re.finditer(pat, content):
            # Get line number
            line_no = content[:m.start()].count("\n") + 1
            hits.append((line_no, m.group()))
    return len(hits) > 0, hits


def check_script_for_d1(script_path):
    """Check if script makes D1/bridge calls."""
    if not script_path or not os.path.isfile(script_path):
        return False
    try:
        with open(script_path, "r", errors="replace") as f:
            content = f.read()
    except Exception:
        return False

    d1_indicators = [
        "sage-bridge", "d1", "D1", "wrangler",
        "/query", "execute_sql", "cloudflare",
        "workers.dev",
    ]
    content_lower = content.lower()
    return any(ind.lower() in content_lower for ind in d1_indicators)


def get_launchctl_status():
    """Parse launchctl list for our daemons."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return {}

    status_map = {}
    for line in result.stdout.strip().splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid_str, exit_status_str, label = parts
        if not any(label.startswith(p) for p in PREFIXES):
            continue
        pid = int(pid_str) if pid_str != "-" else None
        exit_status = int(exit_status_str) if exit_status_str != "-" else None
        status_map[label] = {"pid": pid, "exit_status": exit_status}
    return status_map


def get_process_start_time(pid):
    """Get process start time via ps."""
    if pid is None:
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5
        )
        raw = result.stdout.strip()
        if not raw:
            return None
        # Format: "Tue May 12 14:02:26 2026"
        # Handle extra whitespace
        raw = " ".join(raw.split())
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %Y")
    except Exception:
        return None


def estimate_restarts_per_hour(start_time, throttle):
    """If process started recently, estimate restart frequency."""
    if start_time is None:
        return 0.0
    age_seconds = (datetime.now() - start_time).total_seconds()
    if age_seconds < 0:
        return 0.0
    if age_seconds < 1:
        age_seconds = 1
    # If process is younger than 2x throttle, it likely just restarted
    effective_throttle = max(throttle, 10)
    if age_seconds <= effective_throttle * 2:
        # Process is very fresh = likely restarting frequently
        return 3600.0 / effective_throttle
    # Process has been alive a while = stable
    return 0.0


def classify_risk(daemon, has_exit, has_d1, restarts_hr, pid, exit_status, start_time):
    """Classify daemon risk level and determine action."""
    severity = "OK"
    actions = []

    throttle = daemon["throttle"]
    keep_alive = daemon["keep_alive"]

    # Check if process is stable (running for > 5 minutes)
    is_stable = False
    if pid and start_time:
        age_seconds = (datetime.now() - start_time).total_seconds()
        if age_seconds > 300:
            is_stable = True

    # CRITICAL: KeepAlive + sys.exit + low throttle
    if keep_alive and has_exit and throttle <= 30:
        if is_stable:
            # Process has sys.exit in code but hasn't actually crashed — structural risk only
            severity = "INFO"
            actions.append("STRUCTURAL: KeepAlive + sys.exit exists but process is stable")
        elif pid is None:
            # Not running at all — actively failing
            severity = "CRITICAL"
            actions.append("RESTART LOOP: KeepAlive + sys.exit + not running")
        else:
            # Running but very recently started — may be looping
            severity = "CRITICAL"
            actions.append("RESTART LOOP: KeepAlive + sys.exit + ThrottleInterval <= 30s")

    # HIGH: restart loop detected
    elif restarts_hr > 60:
        severity = "HIGH"
        actions.append(f"Rapid restarts: ~{restarts_hr:.0f}/hr")

    # MEDIUM: KeepAlive with exit calls (higher throttle)
    elif keep_alive and has_exit:
        if is_stable:
            severity = "INFO"
            actions.append("STRUCTURAL: KeepAlive + sys.exit exists but process is stable")
        else:
            severity = "MEDIUM"
            actions.append("KeepAlive + sys.exit (potential loop)")

    # Check for non-zero exit with KeepAlive
    if keep_alive and exit_status is not None and exit_status != 0 and pid is None:
        if severity in ("OK", "INFO"):
            severity = "MEDIUM"
        actions.append(f"Last exit status {exit_status}, not running")

    # D1 risk — only flag if actually looping, not for stable processes
    d1_risk = "NONE"
    if has_d1:
        if severity == "CRITICAL":
            d1_risk = "CRITICAL"
            actions.append("D1 calls in restart loop = billing risk")
        elif restarts_hr > 10:
            d1_risk = "HIGH"
            actions.append("D1 calls with frequent restarts")
        else:
            d1_risk = "LOW"

    # Script missing
    if daemon["script_path"] and not os.path.isfile(daemon["script_path"]):
        if severity == "OK":
            severity = "LOW"
        actions.append(f"Script missing: {daemon['script_path']}")

    if not actions:
        actions.append("No issues detected")

    return severity, d1_risk, actions


def build_report(daemons, status_map):
    """Build the full daemon health report."""
    now = datetime.now()
    rows = []

    for d in daemons:
        label = d["label"]
        status = status_map.get(label, {"pid": None, "exit_status": None})
        pid = status["pid"]
        exit_status = status["exit_status"]

        has_exit, exit_hits = check_script_for_exit(d["script_path"])
        has_d1 = check_script_for_d1(d["script_path"])
        start_time = get_process_start_time(pid)
        restarts_hr = estimate_restarts_per_hour(start_time, d["throttle"])

        # Detect restart loop by age vs throttle
        is_restart_loop = False
        if start_time and d["keep_alive"]:
            age = (now - start_time).total_seconds()
            if age < d["throttle"] * 2:
                is_restart_loop = True
                restarts_hr = 3600.0 / max(d["throttle"], 10)

        severity, d1_risk, actions = classify_risk(
            d, has_exit, has_d1, restarts_hr, pid, exit_status, start_time
        )

        pid_display = str(pid) if pid else "-"
        status_display = "RUNNING" if pid else "STOPPED"
        if is_restart_loop:
            status_display = "RESTART LOOP"

        script_display = os.path.basename(d["script_path"]) if d["script_path"] else "-"

        rows.append({
            "label": label,
            "pid": pid_display,
            "status": status_display,
            "restarts_hr": f"{restarts_hr:.0f}" if restarts_hr > 0 else "-",
            "d1_risk": d1_risk,
            "script": script_display,
            "actions": "; ".join(actions),
            "severity": severity,
            "keep_alive": d["keep_alive"],
            "throttle": d["throttle"],
            "has_exit": has_exit,
            "exit_hits": exit_hits,
            "has_d1": has_d1,
            "start_time": start_time,
        })

    return rows


def render_full_report(rows):
    """Render markdown report."""
    now = datetime.now()
    lines = []
    lines.append("# Daemon Watchdog Report")
    lines.append(f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Summary counts
    critical = sum(1 for r in rows if r["severity"] == "CRITICAL")
    high = sum(1 for r in rows if r["severity"] == "HIGH")
    medium = sum(1 for r in rows if r["severity"] == "MEDIUM")
    running = sum(1 for r in rows if r["pid"] != "-")
    stopped = sum(1 for r in rows if r["pid"] == "-")
    total = len(rows)

    lines.append("## Summary")
    lines.append(f"- Total daemons scanned: {total}")
    lines.append(f"- Running: {running} | Stopped: {stopped}")
    lines.append(f"- CRITICAL: {critical} | HIGH: {high} | MEDIUM: {medium}")
    lines.append("")

    # Critical/High section first (INFO = structural risk, not action-required)
    flagged = [r for r in rows if r["severity"] in ("CRITICAL", "HIGH")]
    if flagged:
        lines.append("## Flagged Daemons (Action Required)")
        lines.append("")
        for r in flagged:
            marker = "RED" if r["severity"] == "CRITICAL" else "ORANGE"
            lines.append(f"### [{marker}] {r['label']}")
            lines.append(f"- Severity: **{r['severity']}**")
            lines.append(f"- PID: {r['pid']} | Status: {r['status']}")
            lines.append(f"- KeepAlive: {r['keep_alive']} | ThrottleInterval: {r['throttle']}s")
            lines.append(f"- Script: {r['script']}")
            lines.append(f"- D1 Risk: {r['d1_risk']}")
            lines.append(f"- Est. Restarts/hr: {r['restarts_hr']}")
            if r["has_exit"] and r["exit_hits"]:
                lines.append(f"- Exit calls found: {', '.join(f'L{ln}: {txt}' for ln, txt in r['exit_hits'][:5])}")
            lines.append(f"- Action: {r['actions']}")
            lines.append("")

    # Full table
    lines.append("## Full Status Table")
    lines.append("")
    lines.append("| Label | PID | Status | Restarts/hr | D1 Risk | Script | Action Needed |")
    lines.append("|---|---|---|---|---|---|---|")

    # Sort: CRITICAL first, then HIGH, MEDIUM, LOW, OK
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3, "LOW": 4, "OK": 5}
    sorted_rows = sorted(rows, key=lambda r: severity_order.get(r["severity"], 5))

    for r in sorted_rows:
        sev_prefix = ""
        if r["severity"] == "CRITICAL":
            sev_prefix = "**[RED]** "
        elif r["severity"] == "HIGH":
            sev_prefix = "**[HIGH]** "
        elif r["severity"] == "MEDIUM":
            sev_prefix = "[MED] "
        elif r["severity"] == "INFO":
            sev_prefix = "[INFO] "

        lines.append(
            f"| {sev_prefix}{r['label']} | {r['pid']} | {r['status']} "
            f"| {r['restarts_hr']} | {r['d1_risk']} | {r['script']} "
            f"| {r['actions']} |"
        )

    lines.append("")

    # KeepAlive audit
    ka_daemons = [r for r in rows if r["keep_alive"]]
    if ka_daemons:
        lines.append("## KeepAlive Audit")
        lines.append(f"Daemons with KeepAlive=true: {len(ka_daemons)}")
        lines.append("")
        for r in ka_daemons:
            exit_flag = " + sys.exit FOUND" if r["has_exit"] else ""
            d1_flag = " + D1 CALLS" if r["has_d1"] else ""
            lines.append(f"- {r['label']} (throttle={r['throttle']}s){exit_flag}{d1_flag}")
        lines.append("")

    return "\n".join(lines)


def render_arise_summary(rows):
    """Compact 10-line summary for ARISE boot injection."""
    now = datetime.now()
    critical = [r for r in rows if r["severity"] == "CRITICAL"]
    high = [r for r in rows if r["severity"] == "HIGH"]
    medium = [r for r in rows if r["severity"] == "MEDIUM"]
    info = [r for r in rows if r["severity"] == "INFO"]
    running = sum(1 for r in rows if r["pid"] != "-")
    stopped = sum(1 for r in rows if r["pid"] == "-")
    total = len(rows)
    d1_risk = sum(1 for r in rows if r["d1_risk"] in ("CRITICAL", "HIGH"))

    lines = []
    lines.append(f"[WATCHDOG] {now.strftime('%H:%M')} | {total} daemons | {running} running | {stopped} stopped")
    lines.append(f"[WATCHDOG] CRITICAL:{len(critical)} HIGH:{len(high)} MEDIUM:{len(medium)} INFO:{len(info)} D1_RISK:{d1_risk}")

    # Show up to 6 lines of flagged daemons
    flagged = critical + high + medium
    for r in flagged[:6]:
        label_short = r["label"].replace("com.stylez.", "").replace("com.stevestylez.", "ss.").replace("com.rise.", "r.")
        lines.append(f"[WATCHDOG] {r['severity']:8s} {label_short} | {r['status']} | {r['restarts_hr']}/hr | D1:{r['d1_risk']} | {r['actions'][:60]}")

    if not flagged:
        lines.append("[WATCHDOG] All daemons nominal. No action required.")

    # Pad to at most 10 lines
    remaining = 10 - len(lines)
    if remaining > 0 and len(flagged) > 6:
        lines.append(f"[WATCHDOG] ...and {len(flagged) - 6} more flagged daemons. Run full report for details.")

    return "\n".join(lines[:10])


def main():
    parser = argparse.ArgumentParser(description="RISE Daemon Watchdog")
    parser.add_argument("--arise", action="store_true", help="Compact summary for ARISE boot")
    args = parser.parse_args()

    daemons = parse_plists()
    status_map = get_launchctl_status()
    rows = build_report(daemons, status_map)

    if args.arise:
        print(render_arise_summary(rows))
    else:
        report = render_full_report(rows)

        # Write report file
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w") as f:
            f.write(report)

        # Also print to stdout
        print(report)
        print(f"\nReport written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
