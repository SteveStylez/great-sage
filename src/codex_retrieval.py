#!/usr/bin/env python3
"""
codex_retrieval.py — Retrieval layer for Steve's 693 OpenAI conversations in D1.

Usage (CLI):
    python3 codex_retrieval.py "what did I say about OBS streaming"
    python3 codex_retrieval.py "my music production setup" --top 5
    python3 codex_retrieval.py "UCSD coursework" --domain academic --top 3

Usage (as module):
    from codex_retrieval import query_codex
    context_block = query_codex("what did I say about OBS streaming", top_k=3)
    # inject context_block into a Claude prompt

NOTE — two independent retrieval paths, not one pipeline:
    This module does keyword/LIKE-based retrieval straight against D1's
    the_codex_conversations table (title pre-filter + token scoring, no vectors).
    codex_to_chromadb.py is a SEPARATE pipeline: it embeds the same corpus (plus
    transcripts/emails/browsing/etc.) into a local ChromaDB collection via OpenAI
    embeddings for semantic/vector search. The two do NOT talk to each other —
    this file never queries the Chroma collection codex_to_chromadb.py builds.
    They are alternative retrieval strategies (fast/free keyword match here vs.
    semantic recall there) kept separate rather than wired into a single hybrid
    retriever, so pick the one that matches the query: exact-term recall → this
    module; conceptual/semantic recall → query ChromaDB's "gs_codex" collection
    directly (see codex_to_chromadb.py's COLLECTION_NAME).
"""

import os
import json
import re
import sys
import math
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

# ── Bridge config ──────────────────────────────────────────────────────────────
BRIDGE_URL = os.getenv("GS_BRIDGE_URL", "https://your-bridge.example.workers.dev/query")
BRIDGE_HEADERS = {
    "Content-Type": "application/json",
    "X-GS-Key": os.environ.get("GS_BRIDGE_KEY", ""),
    "User-Agent": "great-sage",
}

# ── Scoring config ─────────────────────────────────────────────────────────────
RECENCY_HALF_LIFE_DAYS = 365   # conversations decay to 50% weight after 1 year
RECENCY_WEIGHT = 0.25          # max recency contribution to final score
KEYWORD_WEIGHT = 0.75          # keyword match contribution to final score
SNIPPET_CHARS = 600            # chars extracted per matching message turn
MAX_TURNS_PER_CONV = 3         # max message turns surfaced per conversation

# ── Stop words (excluded from keyword matching) ────────────────────────────────
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "is", "was", "are", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "i", "me", "my", "we", "you", "he", "she", "it", "they",
    "that", "this", "what", "which", "who", "how", "when", "where", "why",
    "about", "said", "can", "also", "just", "so", "if", "then", "there",
    "their", "them", "its", "our", "your", "his", "her", "not", "no",
}


# ── Bridge query ───────────────────────────────────────────────────────────────

def _bridge_query(sql: str, params: Optional[list] = None) -> list[dict]:
    """Execute SQL against D1 via the sage-bridge Worker."""
    payload = {"sql": sql}
    if params:
        payload["params"] = params

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BRIDGE_URL, data=data, headers=BRIDGE_HEADERS, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("results", [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Bridge HTTP {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bridge connection error: {e.reason}")


# ── Query tokenisation ─────────────────────────────────────────────────────────

def _tokenise(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, remove stop words and short tokens.

    Was `[a-z]+`, which silently dropped every digit — a query like "RC-505" or
    "H100" tokenised to "rc" (or nothing useful), so numeric-suffixed product/model
    names could never match. `[a-z0-9]+` keeps digits attached to their token.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in STOP_WORDS]


# ── Recency score ──────────────────────────────────────────────────────────────

def _recency_score(created_at_str: str) -> float:
    """Exponential decay: score = e^(-lambda * age_days), lambda = ln(2)/half_life."""
    try:
        # Handle ISO 8601 with timezone offset
        dt_str = created_at_str[:19]  # strip microseconds / tz for parsing
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max((now - dt).total_seconds() / 86400, 0)
        lam = math.log(2) / RECENCY_HALF_LIFE_DAYS
        return math.exp(-lam * age_days)
    except Exception:
        return 0.0


# ── Keyword score ──────────────────────────────────────────────────────────────

def _keyword_score(title: str, messages_json: str, tokens: list[str]) -> tuple[float, list[str]]:
    """
    Score a conversation against query tokens.
    Returns (normalised_score 0-1, matched_snippets list).
    """
    if not tokens:
        return 0.0, []

    # Parse messages (may be large JSON; handle gracefully)
    try:
        messages = json.loads(messages_json) if messages_json else []
    except (json.JSONDecodeError, TypeError):
        messages = []

    title_lower = (title or "").lower()
    hits: dict[str, int] = {}      # token -> hit_count (title=2x weight, msg=1x)
    matching_snippets: list[str] = []

    # Score title (2x weight)
    for tok in tokens:
        count = title_lower.count(tok)
        if count:
            hits[tok] = hits.get(tok, 0) + count * 2

    # Score message turns, collect snippets from matching turns
    turns_added = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        content_lower = content.lower()

        turn_hits = 0
        for tok in tokens:
            count = content_lower.count(tok)
            if count:
                hits[tok] = hits.get(tok, 0) + count
                turn_hits += count

        if turn_hits > 0 and turns_added < MAX_TURNS_PER_CONV:
            label = "Steve" if role == "user" else "GPT"
            snippet = content[:SNIPPET_CHARS].strip()
            if len(content) > SNIPPET_CHARS:
                snippet += "..."
            matching_snippets.append(f"[{label}]: {snippet}")
            turns_added += 1

    # Normalise: what fraction of query tokens had any match, weighted by hit depth
    if not hits:
        return 0.0, []

    matched_token_ratio = len(hits) / len(tokens)
    total_hits = sum(hits.values())
    depth_bonus = min(total_hits / (len(tokens) * 3), 1.0)  # cap at 1
    score = (matched_token_ratio * 0.7) + (depth_bonus * 0.3)
    return min(score, 1.0), matching_snippets


# ── Candidate fetch ────────────────────────────────────────────────────────────

def _fetch_candidates(tokens: list[str], domain_filter: Optional[str], limit: int = 150) -> list[dict]:
    """
    Pull candidate rows from D1.
    Strategy: use SQLite LIKE for each token against title, then load messages for scoring.
    Falls back to recent-only fetch if no tokens.
    """
    if not tokens:
        # No keywords — return most recent conversations
        sql = "SELECT id, conversation_id, title, created_at, messages, word_count, domain FROM the_codex_conversations ORDER BY created_at DESC LIMIT ?"
        params_list = [str(limit)]
        if domain_filter:
            sql = "SELECT id, conversation_id, title, created_at, messages, word_count, domain FROM the_codex_conversations WHERE domain = ? ORDER BY created_at DESC LIMIT ?"
            params_list = [domain_filter, str(limit)]
        return _bridge_query(sql, params_list)

    # Build LIKE clauses for title search (fast pre-filter)
    # Use up to 5 most distinctive tokens to keep SQL reasonable
    search_tokens = tokens[:5]
    like_clauses = " OR ".join(["title LIKE ?" for _ in search_tokens])
    like_params = [f"%{t}%" for t in search_tokens]

    if domain_filter:
        sql = f"""
            SELECT id, conversation_id, title, created_at, messages, word_count, domain
            FROM the_codex_conversations
            WHERE domain = ? AND ({like_clauses})
            ORDER BY created_at DESC
            LIMIT ?
        """
        params_list = [domain_filter] + like_params + [str(limit)]
    else:
        sql = f"""
            SELECT id, conversation_id, title, created_at, messages, word_count, domain
            FROM the_codex_conversations
            WHERE {like_clauses}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params_list = like_params + [str(limit)]

    rows = _bridge_query(sql, params_list)

    # If title pre-filter returned few results, supplement with recent rows
    if len(rows) < 20:
        if domain_filter:
            supplement_sql = """
                SELECT id, conversation_id, title, created_at, messages, word_count, domain
                FROM the_codex_conversations
                WHERE domain = ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            supplement_params = [domain_filter, str(limit - len(rows))]
        else:
            supplement_sql = """
                SELECT id, conversation_id, title, created_at, messages, word_count, domain
                FROM the_codex_conversations
                ORDER BY created_at DESC
                LIMIT ?
            """
            supplement_params = [str(limit - len(rows))]

        existing_ids = {r["id"] for r in rows}
        for row in _bridge_query(supplement_sql, supplement_params):
            if row["id"] not in existing_ids:
                rows.append(row)
                existing_ids.add(row["id"])

    return rows


# ── Main retrieval function ────────────────────────────────────────────────────

def query_codex(
    query: str,
    top_k: int = 5,
    domain_filter: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """
    Search the_codex_conversations for conversations relevant to `query`.

    Args:
        query:         Natural language question or topic.
        top_k:         Number of results to return (3-5 recommended).
        domain_filter: Restrict to a domain ('personal','academic','code','business',
                       'music','creative','brand','chatgpt_delegation','other').
        verbose:       Print scoring details to stderr.

    Returns:
        A formatted context block (string) ready to inject into a Claude prompt.
    """
    tokens = _tokenise(query)

    if verbose:
        print(f"[codex_retrieval] tokens: {tokens}", file=sys.stderr)
        print(f"[codex_retrieval] domain_filter: {domain_filter}", file=sys.stderr)

    candidates = _fetch_candidates(tokens, domain_filter)

    if verbose:
        print(f"[codex_retrieval] candidates fetched: {len(candidates)}", file=sys.stderr)

    scored: list[tuple[float, dict, list[str]]] = []

    for row in candidates:
        kw_score, snippets = _keyword_score(
            row.get("title", ""),
            row.get("messages", ""),
            tokens,
        )
        rec_score = _recency_score(row.get("created_at", ""))
        final_score = (kw_score * KEYWORD_WEIGHT) + (rec_score * RECENCY_WEIGHT)

        if verbose:
            print(
                f"  id={row['id']:4d}  kw={kw_score:.3f}  rec={rec_score:.3f}  "
                f"final={final_score:.3f}  title={row.get('title','')[:50]}",
                file=sys.stderr,
            )

        if final_score > 0.01:  # discard near-zero matches
            scored.append((final_score, row, snippets))

    # Sort by score descending, take top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    if not top:
        return (
            "<!-- codex_retrieval: no relevant prior conversations found for this query -->"
        )

    # ── Format context block ───────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("<!-- CODEX CONTEXT: Prior conversations relevant to this query -->")
    lines.append(f"<!-- Query: {query} -->")
    lines.append("")

    for rank, (score, row, snippets) in enumerate(top, 1):
        title = row.get("title", "Untitled")
        domain = row.get("domain", "unknown")
        created = row.get("created_at", "")[:10]  # YYYY-MM-DD
        word_count = row.get("word_count", 0)
        conv_id = row.get("conversation_id", "")

        lines.append(f"### [{rank}] {title}")
        lines.append(
            f"Domain: {domain} | Date: {created} | Words: {word_count} | "
            f"Relevance: {score:.2f} | ID: {conv_id}"
        )
        lines.append("")

        if snippets:
            lines.append("Relevant excerpts:")
            for snippet in snippets:
                lines.append(f"  {snippet}")
        else:
            lines.append("(Title match only — no snippet extracted)")

        lines.append("")

    lines.append("<!-- END CODEX CONTEXT -->")
    return "\n".join(lines)


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Search Steve's 693 OpenAI conversations in D1 and return context.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 codex_retrieval.py "what did I say about OBS streaming"
  python3 codex_retrieval.py "music production setup" --top 5
  python3 codex_retrieval.py "UCSD coursework deadlines" --domain academic
  python3 codex_retrieval.py "brand identity RISE Holdings" --domain brand --top 3
  python3 codex_retrieval.py "any topic" --verbose

Available domains: personal, academic, code, business, music, creative,
                   brand, chatgpt_delegation, other
        """,
    )
    parser.add_argument("query", help="What to search for in prior conversations")
    parser.add_argument("--top", type=int, default=5, metavar="N", help="Number of results (default: 5)")
    parser.add_argument("--domain", type=str, default=None, help="Filter to a specific domain")
    parser.add_argument("--verbose", action="store_true", help="Print scoring details to stderr")
    args = parser.parse_args()

    top_k = max(1, min(args.top, 10))  # clamp 1-10

    try:
        result = query_codex(
            query=args.query,
            top_k=top_k,
            domain_filter=args.domain,
            verbose=args.verbose,
        )
        print(result)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
