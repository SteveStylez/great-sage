#!/usr/bin/env python3
"""
gs_agent_bus.py — Great Sage Agent Communication Bus
=====================================================
The protocol layer that lets GS daemons discover each other,
send structured messages, and coordinate tasks without being
hardwired to one another.

Architecture:
  - Agent Registry (D1: gs_agent_registry) — who exists, what they can do
  - Message Bus    (D1: gs_agent_messages) — structured envelopes between agents
  - Pub/Sub        (local SQLite fallback for offline/fast path)

Any daemon imports this module and gets:
  bus = AgentBus("shion")
  bus.register(capabilities=["academic","canvas","email"])
  bus.send(to="efreet", msg_type="request", capability="video_gen", payload={...})
  messages = bus.poll()   # returns messages addressed to this agent

Designed for the GS fleet. Three-model code audit gate for Architect proposals.
Author: Great Sage / Steve Whiting
"""

import os
import json
import uuid
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOME = Path.home()
LOG_DIR = HOME / "Library" / "Stylez" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_DIR / "gs_agent_bus.log"),
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
)
log = logging.getLogger("gs_agent_bus")

BRIDGE_URL = os.getenv(
    "GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query"
)
BRIDGE_KEY = os.getenv("GS_BRIDGE_KEY", "")

# Local SQLite fallback — used when bridge is unreachable
LOCAL_BUS_DB = HOME / "workspace" / "code" / "gs_agent_bus.db"

# Message TTL — messages older than this are expired on next poll
MSG_TTL_HOURS = 24

# Heartbeat interval agents should use when calling register()
HEARTBEAT_INTERVAL = 300  # seconds


# ---------------------------------------------------------------------------
# Bridge helper
# ---------------------------------------------------------------------------

def _bridge(sql: str, params: list = None) -> Optional[list]:
    """Execute SQL via sage-bridge. Returns rows or None on failure."""
    try:
        payload = {"sql": sql}
        if params:
            payload["params"] = [str(p) for p in params]
        r = requests.post(
            BRIDGE_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-GS-Key": BRIDGE_KEY,
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("results", [])
        log.warning("Bridge returned %s: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        log.warning("Bridge unreachable: %s", e)
        return None


# ---------------------------------------------------------------------------
# Local fallback DB
# ---------------------------------------------------------------------------

def _local_db() -> sqlite3.Connection:
    """Open (or create) the local fallback bus DB."""
    db = sqlite3.connect(str(LOCAL_BUS_DB))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_registry (
            agent_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            capabilities TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            last_heartbeat TEXT,
            meta TEXT DEFAULT '{}'
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL DEFAULT '*',
            msg_type TEXT NOT NULL,
            capability TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            reply TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            delivered_at TEXT,
            expires_at TEXT
        )
    """)
    db.commit()
    return db


# ---------------------------------------------------------------------------
# AgentBus class
# ---------------------------------------------------------------------------

class AgentBus:
    """
    The communication interface every GS daemon uses to join the network.

    Usage:
        bus = AgentBus("efreet")
        bus.register(capabilities=["video_gen", "media", "faceswap"])

        # Send a request to another agent
        thread = bus.send(
            to="shion",
            msg_type="request",
            capability="academic_lookup",
            payload={"query": "next deadline"}
        )

        # Poll for messages addressed to me
        for msg in bus.poll():
            print(msg)
            bus.reply(msg["id"], {"result": "done"})
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._use_bridge = bool(BRIDGE_KEY)

    # ------------------------------------------------------------------ #
    # Registration                                                          #
    # ------------------------------------------------------------------ #

    def register(self, capabilities: list = None, meta: dict = None) -> bool:
        """
        Register this agent in the registry and update heartbeat.
        Call once on daemon startup, then again every HEARTBEAT_INTERVAL seconds.
        """
        caps = json.dumps(capabilities or [])
        meta_str = json.dumps(meta or {})
        now = datetime.now(timezone.utc).isoformat()

        sql = """
            INSERT INTO gs_agent_registry (agent_id, name, capabilities, status, last_heartbeat, meta)
            VALUES (?, ?, ?, 'active', ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                capabilities=excluded.capabilities,
                status='active',
                last_heartbeat=excluded.last_heartbeat,
                meta=excluded.meta
        """
        params = [self.agent_id, self.agent_id, caps, now, meta_str]

        result = _bridge(sql, params)
        if result is not None:
            log.info("[%s] registered on bridge (caps=%s)", self.agent_id, caps)
            return True

        # Fallback to local
        try:
            db = _local_db()
            db.execute(sql.replace("gs_agent_registry", "agent_registry"), params)
            db.commit()
            db.close()
            log.info("[%s] registered locally (bridge down)", self.agent_id)
            return True
        except Exception as e:
            log.error("[%s] register failed: %s", self.agent_id, e)
            return False

    def heartbeat(self) -> bool:
        """Lightweight heartbeat — just updates last_heartbeat. Call in daemon loop."""
        now = datetime.now(timezone.utc).isoformat()
        sql = "UPDATE gs_agent_registry SET last_heartbeat=?, status='active' WHERE agent_id=?"
        result = _bridge(sql, [now, self.agent_id])
        if result is not None:
            return True
        try:
            db = _local_db()
            db.execute(
                "UPDATE agent_registry SET last_heartbeat=?, status='active' WHERE agent_id=?",
                [now, self.agent_id],
            )
            db.commit()
            db.close()
            return True
        except Exception as e:
            log.warning("[%s] heartbeat failed: %s", self.agent_id, e)
            return False

    # ------------------------------------------------------------------ #
    # Discovery                                                             #
    # ------------------------------------------------------------------ #

    def discover(self, capability: str = None) -> list:
        """
        Find active agents. Optionally filter by capability.
        Returns list of dicts: {agent_id, name, capabilities, last_heartbeat}
        """
        if capability:
            sql = """
                SELECT agent_id, name, capabilities, last_heartbeat
                FROM gs_agent_registry
                WHERE status='active'
                AND capabilities LIKE ?
            """
            params = [f"%{capability}%"]
        else:
            sql = """
                SELECT agent_id, name, capabilities, last_heartbeat
                FROM gs_agent_registry WHERE status='active'
            """
            params = []

        rows = _bridge(sql, params)
        if rows is not None:
            return rows

        try:
            db = _local_db()
            cur = db.execute(
                sql.replace("gs_agent_registry", "agent_registry"), params
            )
            result = [dict(r) for r in cur.fetchall()]
            db.close()
            return result
        except Exception as e:
            log.warning("[%s] discover failed: %s", self.agent_id, e)
            return []

    # ------------------------------------------------------------------ #
    # Messaging                                                             #
    # ------------------------------------------------------------------ #

    def send(
        self,
        to: str,
        msg_type: str,
        payload: dict = None,
        capability: str = None,
        thread_id: str = None,
        ttl_hours: int = MSG_TTL_HOURS,
    ) -> str:
        """
        Send a message to another agent (or broadcast to '*').
        Returns the thread_id so caller can poll for replies.
        """
        thread_id = thread_id or str(uuid.uuid4())
        payload_str = json.dumps(payload or {})
        now = datetime.now(timezone.utc).isoformat()
        expires = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        ).isoformat()

        sql = """
            INSERT INTO gs_agent_messages
            (thread_id, from_agent, to_agent, msg_type, capability,
             payload, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """
        params = [
            thread_id, self.agent_id, to, msg_type,
            capability or "", payload_str, now, expires,
        ]

        result = _bridge(sql, params)
        if result is not None:
            log.info(
                "[%s] → [%s] type=%s thread=%s",
                self.agent_id, to, msg_type, thread_id[:8],
            )
            return thread_id

        try:
            db = _local_db()
            db.execute(
                sql.replace("gs_agent_messages", "agent_messages"), params
            )
            db.commit()
            db.close()
            log.info(
                "[%s] → [%s] type=%s thread=%s (local)",
                self.agent_id, to, msg_type, thread_id[:8],
            )
            return thread_id
        except Exception as e:
            log.error("[%s] send failed: %s", self.agent_id, e)
            return thread_id

    def poll(self, limit: int = 20) -> list:
        """
        Fetch pending messages addressed to this agent (or broadcast '*').
        Marks them delivered automatically.
        """
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            SELECT id, thread_id, from_agent, to_agent, msg_type,
                   capability, payload, status, created_at
            FROM gs_agent_messages
            WHERE (to_agent=? OR to_agent='*')
              AND status='pending'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY id ASC
            LIMIT ?
        """
        params = [self.agent_id, now, str(limit)]

        rows = _bridge(sql, params)
        if rows is None:
            try:
                db = _local_db()
                cur = db.execute(
                    sql.replace("gs_agent_messages", "agent_messages"), params
                )
                rows = [dict(r) for r in cur.fetchall()]
                db.close()
            except Exception as e:
                log.warning("[%s] poll failed: %s", self.agent_id, e)
                return []

        if not rows:
            return []

        # Mark delivered
        ids = [str(r["id"]) for r in rows]
        id_list = ",".join(ids)
        mark_sql = f"""
            UPDATE gs_agent_messages
            SET status='delivered', delivered_at=?
            WHERE id IN ({id_list})
        """
        _bridge(mark_sql, [now])

        # Parse payloads
        result = []
        for r in rows:
            try:
                r["payload"] = json.loads(r.get("payload", "{}"))
            except Exception:
                pass
            result.append(r)

        log.info("[%s] polled %d messages", self.agent_id, len(result))
        return result

    def reply(self, msg_id: int, reply_payload: dict) -> bool:
        """Post a reply to a specific message."""
        sql = """
            UPDATE gs_agent_messages
            SET reply=?, status='replied'
            WHERE id=?
        """
        params = [json.dumps(reply_payload), str(msg_id)]
        result = _bridge(sql, params)
        return result is not None

    def get_thread(self, thread_id: str) -> list:
        """Fetch all messages in a conversation thread."""
        sql = """
            SELECT id, from_agent, to_agent, msg_type, capability,
                   payload, status, reply, created_at
            FROM gs_agent_messages
            WHERE thread_id=?
            ORDER BY id ASC
        """
        rows = _bridge(sql, [thread_id])
        if rows is None:
            return []
        for r in rows:
            try:
                r["payload"] = json.loads(r.get("payload", "{}"))
            except Exception:
                pass
        return rows

    # ------------------------------------------------------------------ #
    # Architect proposal submission                                         #
    # ------------------------------------------------------------------ #

    def propose_improvement(
        self,
        target_file: str,
        change_type: str,
        description: str,
        diff: str,
    ) -> str:
        """
        Submit a code improvement proposal for three-model audit.
        change_type: 'bugfix' | 'optimization' | 'feature' | 'refactor'
        Returns proposal_id.
        """
        proposal_id = str(uuid.uuid4())
        sql = """
            INSERT INTO gs_architect_proposals
            (proposal_id, proposed_by, target_file, change_type,
             description, diff, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending_audit')
        """
        params = [
            proposal_id, self.agent_id, target_file,
            change_type, description, diff,
        ]
        _bridge(sql, params)
        log.info(
            "[%s] proposal submitted: %s → %s",
            self.agent_id, proposal_id[:8], target_file,
        )
        return proposal_id


# ---------------------------------------------------------------------------
# Standalone CLI — test and inspect the bus
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="GS Agent Bus CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show all registered agents")
    send_p = sub.add_parser("send", help="Send a test message")
    send_p.add_argument("from_agent")
    send_p.add_argument("to_agent")
    send_p.add_argument("message")
    sub.add_parser("messages", help="Show recent messages")

    args = parser.parse_args()

    if args.cmd == "status":
        bus = AgentBus("cli")
        agents = bus.discover()
        if not agents:
            print("No agents registered yet.")
        else:
            print(f"{'AGENT':<20} {'CAPABILITIES':<40} {'LAST SEEN'}")
            print("-" * 80)
            for a in agents:
                caps = a.get("capabilities", "[]")
                hb = a.get("last_heartbeat", "never")[:19]
                print(f"{a['agent_id']:<20} {caps:<40} {hb}")

    elif args.cmd == "send":
        bus = AgentBus(args.from_agent)
        thread = bus.send(
            to=args.to_agent,
            msg_type="test",
            payload={"message": args.message},
        )
        print(f"Sent. Thread: {thread}")

    elif args.cmd == "messages":
        rows = _bridge(
            "SELECT id, from_agent, to_agent, msg_type, status, created_at "
            "FROM gs_agent_messages ORDER BY id DESC LIMIT 20",
        )
        if not rows:
            print("No messages.")
            return
        print(f"{'ID':<6} {'FROM':<15} {'TO':<15} {'TYPE':<15} {'STATUS':<12} {'WHEN'}")
        print("-" * 80)
        for r in rows:
            print(
                f"{r['id']:<6} {r['from_agent']:<15} {r['to_agent']:<15} "
                f"{r['msg_type']:<15} {r['status']:<12} {r['created_at'][:19]}"
            )
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
