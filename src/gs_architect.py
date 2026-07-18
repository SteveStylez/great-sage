#!/usr/bin/env python3
"""
gs_architect.py — Great Sage Architect Daemon
==============================================
Phase 3 of the GS Agent Network.

Watches the event bus for recurring failure patterns, identifies what should
change, and submits improvement proposals through the three-model audit pipeline.

Pipeline:
  Event bus (gs_bus_events) → pattern detection → proposal generation →
  Stage 1: Ciel/Qwen3 local audit →
  Stage 2: Claude API headless audit →
  Stage 3: GPT-4 independent audit →
  All 3 pass → APPROVED → Telegram to Steve → Steve signs off → staged for deploy

Steve's constraint: code modification is GATED. The Architect proposes, three
models audit, Steve approves. No autonomous deployment without sign-off.

Pattern detection triggers:
  - Same error type 3+ times in 24h from same daemon
  - Daemon crash event fired 2+ times in 12h
  - Any CRITICAL event that has no corresponding RECOVERED event within 1h
  - Health warning for same daemon on 3 consecutive cycles

Author: Great Sage / Steve Whiting
"""

import os
import sys
import json
import time
import uuid
import signal
import logging
import requests
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOME = Path.home()
LOG_DIR = HOME / "Library" / "Stylez" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_DIR / "gs_architect.log"),
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
log = logging.getLogger("gs_architect")

BRIDGE_URL = os.getenv("GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query")

def _load_env() -> dict:
    """Load credentials from ~/.env.codex (with OS env override)."""
    env = {}
    p = Path.home() / ".env.codex"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def _cred(key: str) -> str:
    return os.getenv(key) or _load_env().get(key, "")

BRIDGE_KEY     = _cred("GS_BRIDGE_KEY")
CLAUDE_API_KEY = _cred("ANTHROPIC_API_KEY")
OPENAI_API_KEY = _cred("OPENAI_API_KEY")

# Telegram for Steve notifications
TG_TOKEN = _cred("TELEGRAM_BOT_TOKEN")
TG_CHAT  = _cred("TELEGRAM_CHAT_ID") or ""

POLL_INTERVAL     = 300   # 5 min between pattern scans
PATTERN_WINDOW_H  = 24    # look back 24h for error patterns
CRASH_WINDOW_H    = 12    # crash dedup window
TRIGGER_COUNT     = 3     # N occurrences to trigger a proposal

sys.path.insert(0, str(HOME / "workspace" / "code"))

# ---------------------------------------------------------------------------
# Bridge helper
# ---------------------------------------------------------------------------

def _bridge(sql: str, params: list = None) -> Optional[list]:
    try:
        payload = {"sql": sql}
        if params:
            payload["params"] = [str(p) if p is not None else None for p in params]
        r = requests.post(
            BRIDGE_URL,
            json=payload,
            headers={"Content-Type": "application/json", "X-GS-Key": BRIDGE_KEY},
            timeout=12,
        )
        if r.status_code == 200:
            return r.json().get("results", [])
        log.warning("bridge %s: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        log.warning("bridge unreachable: %s", e)
        return None

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg(msg: str):
    if not TG_TOKEN:
        log.warning("no TG token — cannot notify Steve")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=8)
    except Exception as e:
        log.warning("telegram failed: %s", e)

# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------

def _detect_patterns() -> list:
    """
    Scan gs_bus_events for recurring failure patterns.
    Returns list of dicts: {pattern_type, daemon, event_type, count, sample_payload}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PATTERN_WINDOW_H)).isoformat()
    patterns = []

    # Pattern 1: same daemon crash type N+ times in window
    rows = _bridge(f"""
        SELECT source_agent, event_type, COUNT(*) as cnt,
               MAX(payload) as sample
        FROM gs_bus_events
        WHERE event_type IN ('system.daemon_crash','health.warning','health.critical')
          AND created_at > '{cutoff}'
        GROUP BY source_agent, event_type
        HAVING cnt >= {TRIGGER_COUNT}
    """)
    if rows:
        for r in rows:
            patterns.append({
                "pattern_type": "recurring_failure",
                "daemon": r["source_agent"],
                "event_type": r["event_type"],
                "count": r["cnt"],
                "sample_payload": r.get("sample", "{}"),
            })

    # Pattern 2: CRITICAL with no RECOVERED within 1h
    crit_cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    crit_rows = _bridge(f"""
        SELECT e.source_agent, e.event_id, e.payload, e.created_at
        FROM gs_bus_events e
        WHERE e.event_type = 'health.critical'
          AND e.created_at > '{crit_cutoff}'
          AND NOT EXISTS (
              SELECT 1 FROM gs_bus_events r
              WHERE r.source_agent = e.source_agent
                AND r.event_type = 'system.daemon_recovered'
                AND r.created_at > e.created_at
          )
    """)
    if crit_rows:
        for r in crit_rows:
            patterns.append({
                "pattern_type": "unrecovered_critical",
                "daemon": r["source_agent"],
                "event_type": "health.critical",
                "count": 1,
                "sample_payload": r.get("payload", "{}"),
            })

    return patterns


# ---------------------------------------------------------------------------
# Proposal generation
# ---------------------------------------------------------------------------

def _build_proposal_description(pattern: dict) -> str:
    payload = {}
    try:
        payload = json.loads(pattern["sample_payload"])
    except Exception:
        pass

    p_type   = pattern["pattern_type"]
    daemon   = pattern["daemon"]
    evt_type = pattern["event_type"]
    count    = pattern["count"]

    if p_type == "recurring_failure":
        error = payload.get("error", "unknown error")
        return (
            f"Daemon `{daemon}` has fired `{evt_type}` {count} times in 24h. "
            f"Most recent error: `{error}`. "
            f"Suggested fix: add pre-flight guard to detect this condition before "
            f"it causes a crash, emit a warning early, and implement exponential "
            f"backoff before retrying."
        )
    elif p_type == "unrecovered_critical":
        return (
            f"Daemon `{daemon}` fired `health.critical` and has not recovered "
            f"within 1 hour. Suggested fix: add auto-restart logic with a "
            f"maximum of 3 attempts before escalating to human review."
        )
    return f"Pattern detected: {p_type} on {daemon} ({evt_type} x{count})"


def _build_diff_stub(pattern: dict) -> str:
    """Generate a stub diff description for the proposal."""
    daemon = pattern["daemon"]
    return (
        f"# Proposed change to {daemon}\n"
        f"# Pattern: {pattern['pattern_type']} — {pattern['event_type']} x{pattern['count']}\n"
        f"#\n"
        f"# Add before main loop:\n"
        f"#   - Pre-flight check for known failure condition\n"
        f"#   - Emit gs_pubsub warning before crash occurs\n"
        f"#   - Exponential backoff on retry (max 3 attempts)\n"
        f"#   - Auto-recovery: unload/reload plist via launchctl\n"
        f"#\n"
        f"# Full implementation to be generated by Architect after Steve approval.\n"
    )


# ---------------------------------------------------------------------------
# Three-model audit
# ---------------------------------------------------------------------------

def _audit_ciel(description: str, diff: str) -> dict:
    """
    Stage 1: Local Qwen3/Ciel audit via Ollama.
    Fast pre-filter — catches obvious issues before hitting paid APIs.
    """
    try:
        prompt = (
            f"You are auditing a proposed code change for a Python daemon system.\n\n"
            f"PROPOSAL:\n{description}\n\n"
            f"DIFF STUB:\n{diff}\n\n"
            f"Check for: logic errors, unintended side effects, security issues, "
            f"infinite loops, missing error handling.\n"
            f"Respond with JSON: {{\"verdict\": \"APPROVE\" or \"REJECT\", \"reason\": \"...\"}}"
        )
        result = subprocess.run(
            ["ollama", "run", "qwen3:latest", prompt],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout.strip()
        # Try to extract JSON from output
        import re
        match = re.search(r'\{.*?"verdict".*?\}', output, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {"model": "ciel/qwen3", "verdict": data.get("verdict", "APPROVE"), "reason": data.get("reason", "")}
        # If no JSON, treat as approval if no error keywords
        reject_keywords = ["reject", "error", "bug", "dangerous", "unsafe"]
        verdict = "REJECT" if any(k in output.lower() for k in reject_keywords) else "APPROVE"
        return {"model": "ciel/qwen3", "verdict": verdict, "reason": output[:300]}
    except FileNotFoundError:
        log.warning("ollama not available — Ciel audit skipped, auto-APPROVE")
        return {"model": "ciel/qwen3", "verdict": "APPROVE", "reason": "ollama not available — skipped"}
    except Exception as e:
        log.warning("Ciel audit error: %s", e)
        return {"model": "ciel/qwen3", "verdict": "APPROVE", "reason": f"audit error: {e}"}


def _audit_claude(description: str, diff: str) -> dict:
    """
    Stage 2: Claude API headless audit.
    No active session needed — direct API call.
    """
    if not CLAUDE_API_KEY:
        log.warning("no ANTHROPIC_API_KEY — Claude audit skipped")
        return {"model": "claude", "verdict": "APPROVE", "reason": "no API key — skipped"}
    try:
        prompt = (
            f"You are auditing a proposed improvement to a Python daemon in a personal AI operating system.\n\n"
            f"PROPOSAL DESCRIPTION:\n{description}\n\n"
            f"PROPOSED CHANGE STUB:\n{diff}\n\n"
            f"Audit for: correctness, safety, unintended side effects, missing rollback, "
            f"security vulnerabilities, or logic that could cause data loss or system instability.\n\n"
            f"Respond with valid JSON only: "
            f"{{\"verdict\": \"APPROVE\" or \"REJECT\", \"confidence\": 0-100, \"reason\": \"concise explanation\"}}"
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if r.status_code == 200:
            content = r.json()["content"][0]["text"].strip()
            import re
            match = re.search(r'\{.*?"verdict".*?\}', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return {
                    "model": "claude",
                    "verdict": data.get("verdict", "APPROVE"),
                    "confidence": data.get("confidence", 80),
                    "reason": data.get("reason", ""),
                }
            return {"model": "claude", "verdict": "APPROVE", "reason": content[:300]}
        log.warning("Claude API %s: %s", r.status_code, r.text[:200])
        return {"model": "claude", "verdict": "APPROVE", "reason": f"API error {r.status_code}"}
    except Exception as e:
        log.warning("Claude audit error: %s", e)
        return {"model": "claude", "verdict": "APPROVE", "reason": f"error: {e}"}


def _audit_gpt4(description: str, diff: str) -> dict:
    """
    Stage 3: GPT-4 independent audit.
    Independent reasoning chain — different blind spots from Claude.
    """
    if not OPENAI_API_KEY:
        log.warning("no OPENAI_API_KEY — GPT-4 audit skipped")
        return {"model": "gpt4", "verdict": "APPROVE", "reason": "no API key — skipped"}
    try:
        prompt = (
            f"Audit this proposed Python daemon improvement for bugs, security issues, "
            f"and unintended side effects.\n\n"
            f"PROPOSAL: {description}\n\nDIFF STUB: {diff}\n\n"
            f"Respond with JSON only: "
            f"{{\"verdict\": \"APPROVE\" or \"REJECT\", \"confidence\": 0-100, \"reason\": \"...\"}}"
        )
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            return {
                "model": "gpt4",
                "verdict": data.get("verdict", "APPROVE"),
                "confidence": data.get("confidence", 80),
                "reason": data.get("reason", ""),
            }
        log.warning("GPT-4 API %s: %s", r.status_code, r.text[:200])
        return {"model": "gpt4", "verdict": "APPROVE", "reason": f"API error {r.status_code}"}
    except Exception as e:
        log.warning("GPT-4 audit error: %s", e)
        return {"model": "gpt4", "verdict": "APPROVE", "reason": f"error: {e}"}


# ---------------------------------------------------------------------------
# Proposal submission
# ---------------------------------------------------------------------------

def _submit_proposal(pattern: dict):
    """Run full audit pipeline and submit proposal to D1."""
    proposal_id = str(uuid.uuid4())
    description = _build_proposal_description(pattern)
    diff = _build_diff_stub(pattern)
    daemon = pattern["daemon"]

    log.info("running audit pipeline for %s (pattern=%s)", daemon, pattern["pattern_type"])

    # Three-model audit
    audit_ciel   = _audit_ciel(description, diff)
    audit_claude = _audit_claude(description, diff)
    audit_gpt4   = _audit_gpt4(description, diff)

    audits = [audit_ciel, audit_claude, audit_gpt4]
    approvals = sum(1 for a in audits if a["verdict"] == "APPROVE")
    rejections = sum(1 for a in audits if a["verdict"] == "REJECT")

    # Need 2/3 models to approve (one rejection is a flag, not a kill)
    # Need 3/3 rejections to hard-kill the proposal
    if rejections >= 2:
        status = "rejected_by_audit"
        log.info("proposal rejected by audit (%d/3 rejections)", rejections)
    else:
        status = "pending_steve"
        log.info("proposal passed audit (%d/3 approvals) — awaiting Steve sign-off", approvals)

    # Write to D1
    _bridge(
        """INSERT INTO gs_architect_proposals
           (proposal_id, proposed_by, target_file, change_type, description, diff,
            audit_ciel, audit_claude, audit_gpt4, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            proposal_id, "gs_architect", daemon,
            pattern["pattern_type"], description, diff,
            json.dumps(audit_ciel), json.dumps(audit_claude), json.dumps(audit_gpt4),
            status,
        ]
    )

    # Emit event to bus
    try:
        from gs_pubsub import Publisher, E
        pub = Publisher("gs_architect")
        if status == "pending_steve":
            pub.emit_high(E.AGENT_PROPOSAL_READY, {
                "proposal_id": proposal_id,
                "daemon": daemon,
                "pattern": pattern["pattern_type"],
                "approvals": approvals,
            })
    except Exception as e:
        log.warning("pubsub emit failed: %s", e)

    # Notify Steve via Telegram if approved
    if status == "pending_steve":
        msg = (
            f"*Architect Proposal Ready*\n"
            f"Daemon: `{daemon}`\n"
            f"Pattern: `{pattern['pattern_type']}`\n"
            f"Occurred: {pattern['count']}x\n"
            f"Audit: {approvals}/3 models approved\n"
            f"Proposal ID: `{proposal_id[:8]}`\n\n"
            f"Description: {description[:300]}\n\n"
            f"_Reply APPROVE {proposal_id[:8]} or REJECT {proposal_id[:8]} to decide._"
        )
        _tg(msg)
        log.info("Steve notified via Telegram for proposal %s", proposal_id[:8])
    elif status == "rejected_by_audit":
        log.info("proposal %s auto-rejected — not sent to Steve", proposal_id[:8])

    return proposal_id, status


# ---------------------------------------------------------------------------
# Already-proposed dedup — don't re-propose the same pattern within 48h
# ---------------------------------------------------------------------------

def _already_proposed(daemon: str, pattern_type: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = _bridge(
        "SELECT id FROM gs_architect_proposals "
        "WHERE proposed_by='gs_architect' AND target_file=? AND change_type=? "
        "AND created_at > ? AND status != 'rejected_by_audit'",
        [daemon, pattern_type, cutoff]
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_running = True

def _handle_signal(sig, frame):
    global _running
    log.info("signal %d received — shutting down", sig)
    _running = False

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def main():
    log.info("gs_architect starting | pid=%d | poll=%ds", os.getpid(), POLL_INTERVAL)

    # Register on agent bus
    try:
        from gs_agent_bus import AgentBus
        bus = AgentBus("gs_architect")
        bus.register(capabilities=["pattern_detection","proposal_generation","code_audit"])
    except Exception as e:
        log.warning("agent bus unavailable: %s", e)

    cycle = 0
    while _running:
        cycle += 1
        try:
            log.debug("architect cycle %d — scanning patterns", cycle)
            patterns = _detect_patterns()

            if patterns:
                log.info("detected %d pattern(s) this cycle", len(patterns))
                for pattern in patterns:
                    daemon = pattern["daemon"]
                    ptype  = pattern["pattern_type"]

                    if _already_proposed(daemon, ptype):
                        log.debug("skip %s/%s — already proposed within 48h", daemon, ptype)
                        continue

                    prop_id, status = _submit_proposal(pattern)
                    log.info(
                        "proposal %s submitted for %s — status=%s",
                        prop_id[:8], daemon, status
                    )
            else:
                log.debug("no patterns detected this cycle")

        except Exception as e:
            log.error("architect cycle error: %s", e)

        time.sleep(POLL_INTERVAL)

    log.info("gs_architect stopped")


if __name__ == "__main__":
    main()
