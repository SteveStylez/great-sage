#!/bin/bash
# ARISE Boot Protocol v1 - SESSION_STATE-first boot
# Renamed from EVOLVE. Same architecture, new identity.
# Primary: reads SESSION_STATE.md (instant context from last DIABLO)
# Secondary: lightweight D1 verification (counts only, not full queries)
# Tertiary: identity + execute protocol
# Layer 6: CODEX DISPATCH - route eligible tasks to OpenAI Codex Bridge

BRIDGE="${GS_BRIDGE_URL:-https://your-bridge.example.workers.dev/query}"
KEY="${GS_BRIDGE_KEY:?set GS_BRIDGE_KEY in your environment}"
DB="${GS_D1_DB_ID:?set GS_D1_DB_ID in your environment}"
SESSION_STATE="$HOME/Library/Stylez/SESSION_STATE.md"
AGENTS_DIR="$HOME/.claude/agents"

query_d1() {
  local sql="$1"
  local result
  result=$(curl -s --max-time 8 -X POST "$BRIDGE" \
    -H "Content-Type: application/json" \
    -H "X-GS-Key: $KEY" \
    -H "User-Agent: ARISE/1.0" \
    -d "{\"sql\":\"$sql\",\"db_id\":\"$DB\"}" 2>/dev/null)
  [ $? -ne 0 ] && return 1
  echo "$result"
}

echo "=== ARISE BOOT PROTOCOL v1 ==="
echo "Timestamp: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""

# ═══════════════════════════════════════════════════════════════
# LAYER 1: SESSION STATE (instant - one file read)
# ═══════════════════════════════════════════════════════════════
if [ -f "$SESSION_STATE" ]; then
  echo "--- SESSION STATE ---"
  cat "$SESSION_STATE"
  echo ""
else
  echo "--- SESSION STATE: NOT FOUND (cold boot) ---"
  echo "No SESSION_STATE.md - running full D1 reconstruction."
  echo ""
fi

# ═══════════════════════════════════════════════════════════════
# LAYER 1.1: D1 SESSION SIGNAL CHECK (kill completed background TTYs)
# Background agents write session_done_<tty> keys to D1 on completion.
# On boot, read those keys, kill matching TTY sessions, delete the keys.
# ═══════════════════════════════════════════════════════════════
SIGNAL_RESULT=$(query_d1 "SELECT key FROM directives WHERE key LIKE 'session_done_%' LIMIT 10" 2>/dev/null)
if [ -n "$SIGNAL_RESULT" ] && echo "$SIGNAL_RESULT" | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    if rows:
        for r in rows:
            key = r.get('key','')
            tty = key.replace('session_done_','')
            if tty:
                print(tty)
except: pass
" 2>/dev/null | while read TTY_NAME; do
  # Find and kill the matching ttyd-spawned Claude session.
  # SECURITY: TTY_NAME comes from a D1 row (directives table) reachable by anyone
  # holding the shared bridge key. The old `ps aux | grep -i "claude.*$TTY_NAME"`
  # (a) fed TTY_NAME straight into an unescaped grep -E pattern, so a value like
  # "ttyd.*" or "." could match/kill unrelated processes, and (b) captured every
  # matching PID into one unquoted variable, so `kill "$TTY_PID"` broke if more
  # than one process matched. Escape the pattern for literal matching and kill
  # every exact match individually.
  ESCAPED_TTY=$(python3 -c "import re,sys; print(re.escape(sys.argv[1]))" "$TTY_NAME" 2>/dev/null)
  if [ -n "$ESCAPED_TTY" ]; then
    TTY_PIDS=$(pgrep -f "claude.*${ESCAPED_TTY}" 2>/dev/null)
    for TTY_PID in $TTY_PIDS; do
      kill "$TTY_PID" 2>/dev/null
      echo "  [SIGNAL] Killed completed session $TTY_NAME (PID $TTY_PID)"
    done
  fi
  # Delete the signal key from D1
  query_d1 "DELETE FROM directives WHERE key='session_done_$TTY_NAME'" >/dev/null 2>&1
done
then
  : # signals processed
fi

# ═══════════════════════════════════════════════════════════════
# LAYER 1.5: SHION ACADEMIC BRIEFING (Shion owns all deadlines)
# ═══════════════════════════════════════════════════════════════
SHION_ACADEMIC="$HOME/Library/Stylez/shion_academic.json"
if [ -f "$SHION_ACADEMIC" ]; then
  echo "--- SHION ACADEMIC BRIEFING ---"
  python3 -c "
import json, datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')

with open('$SHION_ACADEMIC') as f:
    data = json.load(f)

# Use the IANA zone (handles PDT/PST DST transitions) instead of a hardcoded
# UTC-7 offset, which silently became wrong-by-an-hour every winter (PST=UTC-8).
now = datetime.datetime.now(PACIFIC)
print(f'  Source: {data.get(\"source\",\"unknown\")} | Updated: {data.get(\"last_updated\",\"?\")}')
print()

urgent = []
upcoming = []
for a in data.get('assignments', []):
    if a.get('submitted'):
        continue
    try:
        raw_due = a['due'].replace('Z', '+00:00')
        due = datetime.datetime.fromisoformat(raw_due).astimezone(PACIFIC)
        hours = (due - now).total_seconds() / 3600
        if hours < 0:
            continue
        entry = f\"{a['course']} | {a['title']} | Due: {due.strftime('%a %b %d %I:%M %p')} | {hours:.0f}h left\"
        if a.get('draft_status') == 'done':
            entry += f\" | DRAFT DONE ({a.get('draft_words',0)}w)\"
        else:
            entry += ' | NOT STARTED'
        if hours <= 120:
            urgent.append((hours, entry))
        else:
            upcoming.append((hours, entry))
    except:
        pass

if urgent:
    print('  URGENT (next 5 days):')
    for h, e in sorted(urgent):
        marker = '!!' if h < 48 else '>'
        print(f'    {marker} {e}')
    print()

if upcoming:
    print('  UPCOMING:')
    for h, e in sorted(upcoming):
        print(f'    - {e}')
    print()

locs = data.get('draft_locations', {})
if locs:
    print('  DRAFT LOCATIONS:')
    for k, v in locs.items():
        print(f'    {k}: {v}')
    print()
" 2>/dev/null
else
  echo "--- SHION ACADEMIC: NO DATA (shion_academic.json not found) ---"
fi
echo ""

# ═══════════════════════════════════════════════════════════════
# LAYER 2-6: PARALLEL D1 QUERIES + LOCAL CHECKS
# All 6 D1 queries fire simultaneously as background subshells.
# Local checks (daemon count, watchdog, agents) run in parallel too.
# Results land in temp files, then we print in order.
# ═══════════════════════════════════════════════════════════════

TMPDIR_ARISE=$(mktemp -d /tmp/arise_boot.XXXXXX)

# --- Fire all D1 queries in parallel ---
query_d1 "SELECT status, COUNT(*) as cnt FROM task_priority WHERE status IN ('pending','in_progress','running') GROUP BY status" > "$TMPDIR_ARISE/tasks.json" 2>/dev/null &
query_d1 "SELECT status, COUNT(*) as cnt FROM efreet_queue GROUP BY status" > "$TMPDIR_ARISE/efreet.json" 2>/dev/null &
query_d1 "SELECT COUNT(*) as total, SUM(CASE WHEN LENGTH(transcript)>10 THEN 1 ELSE 0 END) as done FROM veldora_intel" > "$TMPDIR_ARISE/veldora.json" 2>/dev/null &
query_d1 "SELECT key, directive, authority FROM directives ORDER BY created_at DESC LIMIT 10" > "$TMPDIR_ARISE/directives.json" 2>/dev/null &
query_d1 "SELECT id,priority,title,assigned_to FROM task_priority WHERE status='pending' ORDER BY priority ASC, created_at DESC LIMIT 8" > "$TMPDIR_ARISE/pending.json" 2>/dev/null &
query_d1 "SELECT id, priority, title FROM task_priority WHERE status='pending' AND (assigned_to='codex' OR LOWER(title) LIKE '%build%' OR LOWER(title) LIKE '%generate%' OR LOWER(title) LIKE '%script%' OR LOWER(title) LIKE '%batch%' OR LOWER(title) LIKE '%draft%' OR LOWER(title) LIKE '%create%') ORDER BY priority ASC, created_at DESC LIMIT 6" > "$TMPDIR_ARISE/codex.json" 2>/dev/null &

# --- Fire local checks in parallel too ---
(ps aux | grep -E "\.py.*(daemon|--daemon|brain|listener|telegram|canvas)" | grep -v grep | wc -l | tr -d ' ' > "$TMPDIR_ARISE/daemon_count.txt") &
WATCHDOG="$HOME/workspace/code/daemon_watchdog.py"
([ -f "$WATCHDOG" ] && python3 "$WATCHDOG" --arise > "$TMPDIR_ARISE/watchdog.txt" 2>/dev/null || echo "(watchdog offline)" > "$TMPDIR_ARISE/watchdog.txt") &
(python3 ~/Library/Stylez/goblin_dispatch.py --arise-report > "$TMPDIR_ARISE/goblin.txt" 2>/dev/null || echo "--- GOBLIN RIDERS: status unavailable ---" > "$TMPDIR_ARISE/goblin.txt") &
([ -f "$HOME/Library/Stylez/codex_identity_boot.py" ] && python3 $HOME/Library/Stylez/codex_identity_boot.py 2>/dev/null | head -40 > "$TMPDIR_ARISE/identity.txt" || echo "  (identity boot offline)" > "$TMPDIR_ARISE/identity.txt") &

# --- Wait for ALL parallel jobs to finish ---
wait

# --- Now print results in order ---

# LAYER 2: LIVE VERIFICATION
echo "--- LIVE VERIFICATION ---"

cat "$TMPDIR_ARISE/tasks.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    parts = [f\"{r['status']}:{r['cnt']}\" for r in rows]
    print(f\"  Tasks: {' | '.join(parts) if parts else 'queue empty'}\")
except: print('  Tasks: query failed')
" 2>/dev/null

cat "$TMPDIR_ARISE/efreet.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    parts = [f\"{r['status']}:{r['cnt']}\" for r in rows]
    print(f\"  Efreet: {' | '.join(parts)}\")
except: print('  Efreet: query failed')
" 2>/dev/null

cat "$TMPDIR_ARISE/veldora.json" 2>/dev/null | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin).get('results',[{}])[0]
    t,d = r.get('total',0), r.get('done',0)
    pct = int(d/t*100) if t else 0
    print(f\"  Veldora: {d}/{t} ({pct}%)\")
except: print('  Veldora: query failed')
" 2>/dev/null

echo "  Daemons running: $(cat "$TMPDIR_ARISE/daemon_count.txt" 2>/dev/null || echo '?')"

# LAYER 2.5: DAEMON WATCHDOG
watchdog_out=$(cat "$TMPDIR_ARISE/watchdog.txt" 2>/dev/null)
if [ -n "$watchdog_out" ] && [ "$watchdog_out" != "(watchdog offline)" ]; then
  echo ""
  echo "--- DAEMON COST WATCHDOG ---"
  echo "$watchdog_out"
fi
echo ""

# LAYER 3: DIRECTIVES
echo "--- DIRECTIVES ---"
cat "$TMPDIR_ARISE/directives.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    for r in rows:
        if isinstance(r, dict):
            print(f\"  [{r.get('authority','?')}] {r.get('key','?')}: {r.get('directive','')[:120]}\")
except: pass
" 2>/dev/null
echo ""

# LAYER 4: PENDING TASKS
echo "--- PENDING TASKS (top 8) ---"
cat "$TMPDIR_ARISE/pending.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    for r in rows:
        print(f\"  P{r.get('priority','?')} [{r.get('assigned_to','?')}] #{r.get('id','?')} {r.get('title','')[:75]}\")
    if not rows: print('  (queue empty)')
except: print('  parse error')
" 2>/dev/null
echo ""

# LAYER 4.5: GOBLIN RIDER STATUS
echo "--- GOBLIN RIDER STATUS ---"
cat "$TMPDIR_ARISE/goblin.txt" 2>/dev/null
echo ""

# LAYER 4.7: CHROMADB RAG - relevant past research for pending tasks
CHROMADB_PATH="$HOME/workspace/data/gs_memory_chromadb"
if [ -d "$CHROMADB_PATH" ]; then
  echo "--- CHROMADB MEMORY (top research relevant to pending tasks) ---"
  # Query ChromaDB for context on current pending task titles
  PENDING_TITLES=$(cat "$TMPDIR_ARISE/pending.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])[:3]
    titles = ' '.join(r.get('title','') for r in rows if r.get('title'))
    print(titles[:200] if titles else 'RISE Holdings monetization strategy')
except: print('RISE Holdings monetization strategy')
" 2>/dev/null)
  if [ -n "$PENDING_TITLES" ]; then
    timeout 10 python3 "$HOME/workspace/code/gs_autonomous_loop.py" --query "$PENDING_TITLES" 2>/dev/null | head -20
  fi
  echo ""
fi

# LAYER 5: IDENTITY + AVAILABLE AGENTS
echo "--- IDENTITY ---"
cat "$TMPDIR_ARISE/identity.txt" 2>/dev/null
echo ""

echo "--- AVAILABLE AGENTS ---"
if [ -d "$AGENTS_DIR" ]; then
  for f in "$AGENTS_DIR"/*.md; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .md)
    [ "$name" = "AGENT_TEAMS" ] && continue
    desc=$(grep "^description:" "$f" | head -1 | sed 's/description: //')
    echo "  $name: $desc"
  done
fi
echo ""

# Context push for daemons (5s timeout - this call has been observed to hang)
if [ -f "$HOME/Library/Stylez/gs_context.py" ]; then
  python3 $HOME/Library/Stylez/gs_context.py --push &
  CTX_PID=$!
  ( sleep 5 && kill $CTX_PID 2>/dev/null ) &
  wait $CTX_PID 2>/dev/null
fi

# LAYER 6: CODEX DISPATCH
echo "--- CODEX DISPATCH ---"
cat "$TMPDIR_ARISE/codex.json" 2>/dev/null | python3 -c "
import sys, json
try:
    rows = json.load(sys.stdin).get('results',[])
    if not rows:
        print('  No Codex-eligible tasks. All in-session.')
    else:
        print('  ROUTE THESE TO CODEX BRIDGE AGENT (background):')
        for r in rows:
            print(f\"  → P{r.get('priority','?')} #{r.get('id','?')} {r.get('title','')[:80]}\")
except: print('  Codex dispatch: query failed')
" 2>/dev/null
echo ""

# Cleanup temp files
rm -rf "$TMPDIR_ARISE"

echo "=== ARISE BOOT COMPLETE ==="
echo ""
echo "--- THE GOBLIN RIDERS (TGR) - DELEGATION FRAMEWORK ---"
echo "TGRs = Steve's autonomous daemon fleet + dispatched agent squads."
echo "Named: The Goblin Riders. Each TGR takes a mission, executes, reports back to GS+Steve."
echo ""
echo "TGR SQUADS (daemons):"
echo "  TGR-Intel    → veldora_intel, veldora_listener, veldora_categorizer"
echo "  TGR-Canvas   → shion_canvas_auto, shion_brain, shion_email_briefing, shion_telegram"
echo "  TGR-Efreet   → efreet_fal (video generation pipeline)"
echo "  TGR-Raphael  → raphael_v1, raphael_v3, raphael_classifier (memory/embeddings)"
echo "  TGR-Social   → social_publishing daemon"
echo "  TGR-Ranga    → ranga_v3, daemon_health_monitor, orchestrator_verifier"
echo "  TGR-Task     → task_orchestrator, task_dispatch_bridge, sage_orchestrator"
echo ""
echo "DISPATCHED TGRs (Claude Code agents - always delegate, never do in-session what an agent can do):"
echo "  Academic Sentinel     → assignments, deadlines, Canvas submissions"
echo "  Architecture Auditor  → daemon fleet health, script audits"
echo "  Credential Watchdog   → API keys, billing, token expiry"
echo "  Release Pipeline Mgr  → ADITL/SoH release checklists"
echo "  Infrastructure Resolver → DNS, sites, deployment issues"
echo "  OpenAI Codex Bridge   → all OpenAI/build/generate/draft tasks"
echo "  Social Pipeline Unlocker → platform OAuth tokens"
echo "  Daemon Health Verifier  → output verification (not just 'running')"
echo "  Token Optimizer       → API cost analysis"
echo "  Data Consolidation    → D1 accuracy + aggregation"
echo "  Session Continuity    → SESSION_STATE writes (DIABLO only)"
echo "  UI Design Studio      → all web/visual redesigns"
echo ""
echo "DELEGATION RULE: On every boot, route ALL eligible tasks to TGR agents FIRST."
echo "In-session GS handles: architecture decisions, blocker analysis, Steve-facing comms."
echo "Agents handle: execution, file writes, API calls, submissions, research."
echo ""
echo "--- MANDATORY SKILL BOOT ---"
echo "INVOKE THESE SKILLS NOW (Skill tool, all in same message):"
echo "  1. Skill(skill-router) - auto-dispatch, maps all tasks to domain skills"
echo "  2. Skill(delegation)   - agent routing + parallelization matrix"
echo "  3. Skill(design)       - logo/CIP/banner/social/icon pipeline"
echo "  4. Skill(video-sight)  - video analysis via frame extraction + Claude vision"
echo "skill-router fires FIRST. It determines which other skills load per task."
echo ""
echo "--- ASSET DISCOVERY (every boot) ---"
echo "SCAN for new inputs Steve has dropped into the ecosystem:"
echo "  1. iCloud Downloads: ~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/"
echo "  2. Photos Library: ~/Pictures/Photos Library.photoslibrary/originals/ (last 72h)"
echo "  3. Desktop screenshots: ~/Desktop/Screenshot*.png (last 48h)"
echo "  4. Downloads folder: ~/Downloads/ (last 48h, exclude .tmp)"
echo "  5. Unacknowledged GS deliverables: check SESSION_STATE for items Steve hasn't seen"
echo "  6. Unused skills/tools: audit ~/.claude/skills/ for installed but never-invoked skills"
echo "Report NEW items found. Ingest anything that looks like content, reference, or instruction."
echo "This is NOT optional. Steve saves things expecting GS to notice them."
echo ""
echo "--- EXECUTE PROTOCOL ---"
echo "Read SESSION_STATE above. Verify against LIVE VERIFICATION."
echo "If state is stale, update SESSION_STATE.md immediately."
echo "Fire Codex Bridge agent for any CODEX DISPATCH items above (background)."
echo "Fire domain-specific TGR agents for any squad-relevant open tasks."
echo "Then execute highest-priority in-session work. No questions. No summaries."
echo ""
echo "Reversible work ships without permission."
echo "Only pause for: irreversible (send/publish/delete/pay) or genuine ambiguity."
echo "Authority: gs_execute_on_boot_no_ask + gs_digital_twin."
