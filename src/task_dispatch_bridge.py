#!/usr/bin/env python3
"""
task_dispatch_bridge.py — GS Task Dispatch Bridge

Polls task_priority every 5 minutes for pending tasks.
Routes each task to the correct execution path based on assigned_to + task type.

Routes:
  efreet   → INSERT into efreet_queue (prompt + style extracted from title/description)
  veldora  → claude -p execution (caption/draft/content generation)
  claude-native / dispatched_to_native → skip (CN handles these)
  orchestrator / manual → skip (GS session handles these)

Marks tasks: dispatched → done (efreet) or done (veldora claude -p output saved)

LaunchAgent: com.stylez.task-dispatch-bridge
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config (all secrets from environment) ──────────────────────────────────────
BRIDGE_URL    = os.getenv("GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query")
BRIDGE_KEY    = os.getenv("GS_BRIDGE_KEY", "")
USER_AGENT    = "TaskDispatchBridge/1.0"

TG_BOT        = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT       = os.getenv("TG_CHAT_ID", "")

POLL_INTERVAL = 300      # 5 minutes
CLAUDE_BIN    = "/opt/homebrew/bin/claude"
OUTPUT_DIR    = Path.home() / "Documents/Creative/Social/GS_Dispatch"
LOG_PATH      = Path.home() / "Library/Logs/Stylez/task_dispatch_bridge.log"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Assignees this bridge handles
HANDLED_ASSIGNEES = {"efreet", "veldora"}

# Veldora content task keywords — triggers claude -p execution
CONTENT_KEYWORDS = [
    "draft", "caption", "copy", "write", "generate", "create",
    "sequence", "post", "story", "schedule"
]

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat()}] [DISPATCH] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ── Bridge ────────────────────────────────────────────────────────────────────
def bridge(sql: str, params=None):
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req  = urllib.request.Request(
        BRIDGE_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GS-Key": BRIDGE_KEY,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(text: str):
    body = json.dumps({"chat_id": TG_CHAT, "text": text[:4096]}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"TG failed: {e}")

# ── Efreet dispatch ───────────────────────────────────────────────────────────
STYLE_MAP = {
    "dark":      "dark_cinematic",
    "cinematic": "cinematic",
    "aesthetic": "dark_cinematic",
    "soh":       "dark_cinematic",
    "story of heartless": "dark_cinematic",
    "aditl":     "documentary",
    "cover art": "album_art",
    "portrait":  "portrait",
}

def extract_efreet_style(title: str, description: str) -> str:
    combined = (title + " " + description).lower()
    for kw, style in STYLE_MAP.items():
        if kw in combined:
            return style
    return "cinematic"

def extract_efreet_prompt(title: str, description: str) -> str:
    """Build a Higgsfield prompt from task title + description."""
    # Use description if substantive, else derive from title
    if description and len(description.strip()) > 40:
        return description.strip()
    # Derive from title: strip agent prefix
    prompt = re.sub(r"^(efreet|veldora|soh|aditl)\s*[\-—]\s*", "", title, flags=re.IGNORECASE).strip()
    # Remove meta-phrases
    prompt = re.sub(r"(generate|batch \d+-\d+|batch\s+\d+)", "", prompt, flags=re.IGNORECASE).strip()
    return prompt or title

def dispatch_to_efreet(task: dict) -> bool:
    title = task["title"]
    desc  = task.get("description") or ""
    style = extract_efreet_style(title, desc)
    prompt = extract_efreet_prompt(title, desc)

    log(f"Dispatching #{task['id']} to efreet_queue — style={style}, prompt={prompt[:60]}")
    try:
        bridge(
            "INSERT INTO efreet_queue (prompt, style, status, created_at) VALUES (?, ?, 'pending', datetime('now'))",
            [prompt, style]
        )
        bridge(
            "UPDATE task_priority SET status='dispatched', result=?, started_at=datetime('now') WHERE id=?",
            [f"Dispatched to efreet_queue — style={style}", task["id"]]
        )
        log(f"#{task['id']} dispatched to efreet_queue OK")
        return True
    except Exception as e:
        log(f"#{task['id']} efreet dispatch failed: {e}")
        return False

# ── Veldora claude -p execution ───────────────────────────────────────────────
VELDORA_SYSTEM = """You are Veldora, Steve Stylez's IG content agent.

Steve Stylez is an independent hip-hop artist from San Diego signed to RISE Holdings / Plaground Muzik.
His voice is raw, direct, street-grounded, no fluff. IG captions are concise — no hashtag soup, no emoji overload.
Gold standard: personal, honest, speaks to someone specific, ends with a quiet CTA or nothing at all.

Brand palette voice: dark, minimal, intentional. No hype language. No "🔥" filler."""

def run_claude_p(prompt: str, timeout: int = 120) -> str:
    """Generate content via qwen3:14b (Ollama local) — zero Anthropic token cost."""
    import urllib.request, json as _json
    payload = _json.dumps({
        "model": "qwen3:14b",
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 1200, "temperature": 0.7}
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read())
            return data.get("response", "").strip()
    except Exception as e:
        raise RuntimeError(f"qwen3:14b call failed: {e}")

def dispatch_veldora_content(task: dict) -> bool:
    title = task["title"]
    desc  = task.get("description") or ""

    # Build a specific claude -p prompt
    prompt = f"""{VELDORA_SYSTEM}

Task: {title}
{('Additional context: ' + desc) if desc and len(desc) > 20 else ''}

Execute this task fully. Produce the final output — captions, copy, or schedule as required.
Write in Steve's voice. Be specific to ADITL / SoH EP context. No meta-commentary, just the deliverable."""

    log(f"Executing #{task['id']} via claude -p — '{title[:60]}'")

    # Mark in-progress
    bridge(
        "UPDATE task_priority SET status='in-progress', started_at=datetime('now') WHERE id=?",
        [task["id"]]
    )

    try:
        output = run_claude_p(prompt, timeout=180)

        if not output or len(output) < 50:
            raise RuntimeError(f"Output too short ({len(output)} chars) — likely failed")

        # Save to file
        safe_title = re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        out_path = OUTPUT_DIR / f"{ts}_{safe_title}.md"
        out_path.write_text(f"# {title}\n**Task #{task['id']} | {ts}**\n\n{output}\n")

        bridge(
            "UPDATE task_priority SET status='done', result=?, completed_at=datetime('now') WHERE id=?",
            [str(out_path), task["id"]]
        )
        log(f"#{task['id']} done — saved to {out_path}")
        tg(f"✅ Veldora task #{task['id']} complete\n{title}\n→ {out_path.name}")
        return True

    except subprocess.TimeoutExpired:
        bridge(
            "UPDATE task_priority SET status='failed', result='claude -p timeout >180s — escalate to CN' WHERE id=?",
            [task["id"]]
        )
        log(f"#{task['id']} timeout")
        tg(f"⏱ Task #{task['id']} timed out — needs CN dispatch\n{title}")
        return False

    except Exception as e:
        bridge(
            "UPDATE task_priority SET status='failed', result=? WHERE id=?",
            [str(e)[:200], task["id"]]
        )
        log(f"#{task['id']} failed: {e}")
        return False

# ── Router ────────────────────────────────────────────────────────────────────
def is_content_task(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in CONTENT_KEYWORDS)

def route_task(task: dict) -> str:
    """Return 'efreet', 'veldora_content', or 'skip'."""
    assignee = (task.get("assigned_to") or "").lower()
    title    = task.get("title", "")

    if assignee == "efreet":
        return "efreet"
    if assignee == "veldora":
        if is_content_task(title):
            return "veldora_content"
        return "skip"  # non-content veldora tasks (intelligence, catalog) — daemon handles
    return "skip"

# ── Main loop ─────────────────────────────────────────────────────────────────
# ── System Health Check ──────────────────────────────────────────────────────
_last_health_alert = {}

def health_check():
    """Check production systems for 0-output conditions. TG alert if stalled."""
    alerts = []
    try:
        # Efreet: check if completions are increasing
        res = bridge("SELECT COUNT(*) as cnt FROM efreet_queue WHERE status='complete'")
        efreet_complete = res.get("results", [{}])[0].get("cnt", 0)
        res = bridge("SELECT COUNT(*) as cnt FROM efreet_queue WHERE status='timeout'")
        efreet_timeout = res.get("results", [{}])[0].get("cnt", 0)
        res = bridge("SELECT COUNT(*) as cnt FROM efreet_queue WHERE status='processing'")
        efreet_processing = res.get("results", [{}])[0].get("cnt", 0)
        if efreet_processing > 5:
            alerts.append(f"Efreet: {efreet_processing} stuck processing")
        if efreet_timeout > 50:
            alerts.append(f"Efreet: {efreet_timeout} timeouts (check daemon)")

        # CN: unfinished conversations
        res = bridge("SELECT COUNT(*) as cnt FROM the_codex_conversations_native WHERE unfinished=1")
        cn_unfinished = res.get("results", [{}])[0].get("cnt", 0)
        if cn_unfinished > 10:
            alerts.append(f"CN: {cn_unfinished} unfinished conversations")

        # Tasks: answer_missing
        res = bridge("SELECT COUNT(*) as cnt FROM task_priority WHERE status='answer_missing'")
        missing = res.get("results", [{}])[0].get("cnt", 0)
        if missing > 3:
            alerts.append(f"Tasks: {missing} answer_missing — CN output not ingested")

    except Exception as e:
        log(f"Health check error: {e}")

    if alerts:
        msg = "⚠ GS HEALTH ALERT:\n" + "\n".join(f"• {a}" for a in alerts)
        # Only alert once per unique alert set per hour
        alert_key = str(sorted(alerts))
        last = _last_health_alert.get(alert_key, 0)
        if time.time() - last > 3600:
            tg(msg)
            _last_health_alert[alert_key] = time.time()
            log(f"Health alert sent: {len(alerts)} issues")


def main():
    log(f"Task dispatch bridge starting. Poll={POLL_INTERVAL}s")

    cycle = 0
    while True:
        try:
            res = bridge(
                "SELECT id, title, description, priority, assigned_to FROM task_priority "
                "WHERE status='pending' AND assigned_to IN ('efreet','veldora') "
                "ORDER BY priority ASC, id ASC LIMIT 10"
            )
            tasks = res.get("results", [])

            if tasks:
                log(f"Found {len(tasks)} pending dispatchable task(s)")
            else:
                log("No pending dispatchable tasks")

            for task in tasks:
                route = route_task(task)
                log(f"#{task['id']} [{task.get('assigned_to')}] route={route} — {task['title'][:55]}")

                if route == "efreet":
                    dispatch_to_efreet(task)
                elif route == "veldora_content":
                    dispatch_veldora_content(task)
                else:
                    log(f"#{task['id']} skipped — no dispatch route")

                time.sleep(5)  # Brief pause between tasks

        except Exception as e:
            log(f"Main loop error: {e}")

        # Run health check every 6 cycles (~30 min)
        cycle += 1
        if cycle % 6 == 0:
            health_check()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
