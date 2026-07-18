#!/usr/bin/env python3
"""
codex_to_chromadb.py — Embed ALL Codex data into ChromaDB
==========================================================
Takes every conversation, message, transcript, email, and browsing record
from sage_codex.db and sage_prism.db, chunks them, embeds via OpenAI
text-embedding-3-small, and stores in ChromaDB for semantic search.

This is the ACTUAL embedding pipeline. Previous attempts either:
  - Wrote to wrong location (embedding column instead of embeddings table)
  - Were never executed (raphael_embeddings.py, codex_semantic_embeddings.py)
  - Targeted D1 instead of ChromaDB

This script fixes all of that by going straight to ChromaDB.

Usage:
  python3 codex_to_chromadb.py                    # embed everything
  python3 codex_to_chromadb.py --dry-run           # show what would be embedded
  python3 codex_to_chromadb.py --source codex      # only sage_codex.db
  python3 codex_to_chromadb.py --source prism      # only sage_prism.db
  python3 codex_to_chromadb.py --batch-size 20     # OpenAI batch size
"""

import os
import sys
import json
import time
import sqlite3
import hashlib
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOME = Path.home()
CODEX_DB = HOME / "workspace" / "data" / "sqlite" / "sage_codex.db"
PRISM_DB = HOME / "workspace" / "data" / "sqlite" / "sage_prism.db"
CHROMADB_PATH = HOME / "workspace" / "data" / "gs_memory_chromadb"
LOG_FILE = HOME / "Library" / "Stylez" / "logs" / "codex_to_chromadb.log"

OPENAI_MODEL = "text-embedding-3-small"
OPENAI_DIMS = 1536
CHUNK_SIZE = 1500  # chars (~375 tokens)
CHUNK_OVERLAP = 200  # chars overlap between chunks
BATCH_SIZE = 20  # texts per OpenAI API call
COLLECTION_NAME = "gs_codex"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# OpenAI Embedding
# ---------------------------------------------------------------------------

def get_openai_key():
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    env_file = HOME / ".env.codex"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def embed_batch(texts):
    """Embed a batch of texts via OpenAI API. Returns list of vectors."""
    key = get_openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not found in env or ~/.env.codex")

    body = json.dumps({
        "model": OPENAI_MODEL,
        "input": texts,
        "dimensions": OPENAI_DIMS,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    return [item["embedding"] for item in data["data"]]


# ---------------------------------------------------------------------------
# Text Chunking
# ---------------------------------------------------------------------------

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks."""
    if not text or not text.strip():
        return []
    text = text.strip()
    if len(text) <= size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap
        if start >= len(text):
            break

    return chunks


# ---------------------------------------------------------------------------
# Data Extraction
# ---------------------------------------------------------------------------

def extract_codex_data(db_path, tables_config):
    """Extract text records from a SQLite database."""
    if not db_path.exists():
        log(f"Database not found: {db_path}", "WARN")
        return []

    records = []
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row

    for table_name, config in tables_config.items():
        try:
            text_col = config["text_col"]
            id_col = config.get("id_col", "rowid")
            source_type = config.get("source_type", table_name)
            extra_meta = config.get("extra_meta", {})

            rows = conn.execute(
                f"SELECT {id_col}, {text_col} FROM {table_name} "
                f"WHERE {text_col} IS NOT NULL AND {text_col} != ''"
            ).fetchall()

            for row in rows:
                text = row[text_col] if text_col in row.keys() else str(row[1])
                record_id = str(row[0])

                if not text or len(text.strip()) < 20:
                    continue

                records.append({
                    "id": f"{source_type}_{record_id}",
                    "text": text.strip(),
                    "source_db": db_path.name,
                    "source_table": table_name,
                    "source_type": source_type,
                    "record_id": record_id,
                    **extra_meta,
                })

        except Exception as e:
            log(f"Error reading {table_name} from {db_path.name}: {e}", "WARN")

    conn.close()
    return records


def get_all_records(sources):
    """Extract records from all specified databases."""
    all_records = []

    if "codex" in sources:
        codex_tables = {
            "the_codex_conversations": {
                "text_col": "messages",
                "id_col": "conversation_id",
                "source_type": "codex_conversation",
            },
            "the_codex_transcripts": {
                "text_col": "text",
                "id_col": "id",
                "source_type": "codex_transcript",
            },
            "the_codex_browsing": {
                "text_col": "title",
                "id_col": "id",
                "source_type": "codex_browsing",
            },
            "the_codex_emails": {
                "text_col": "body",
                "id_col": "rowid",
                "source_type": "codex_email",
            },
            "codex_messages": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_message",
            },
            "codex_academic": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_academic",
            },
            "codex_brand": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_brand",
            },
            "codex_code": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_code",
            },
            "codex_music": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_music",
            },
            "codex_personal": {
                "text_col": "content",
                "id_col": "rowid",
                "source_type": "codex_personal",
            },
        }
        records = extract_codex_data(CODEX_DB, codex_tables)
        all_records.extend(records)
        log(f"Extracted {len(records)} records from sage_codex.db")

    if "prism" in sources:
        prism_tables = {
            "improvements": {
                "text_col": "entry",
                "id_col": "id",
                "source_type": "prism_improvement",
            },
            "daemon_events": {
                "text_col": "event",
                "id_col": "id",
                "source_type": "prism_daemon_event",
            },
        }
        records = extract_codex_data(PRISM_DB, prism_tables)
        all_records.extend(records)
        log(f"Extracted {len(records)} records from sage_prism.db")

    return all_records


# ---------------------------------------------------------------------------
# ChromaDB Indexing
# ---------------------------------------------------------------------------

def index_to_chromadb(records, dry_run=False, batch_size=BATCH_SIZE):
    """Chunk, embed, and index all records into ChromaDB."""
    import chromadb

    client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={
            "description": "Full Codex knowledge base — conversations, transcripts, browsing, emails, messages",
            "model": OPENAI_MODEL,
            "dims": str(OPENAI_DIMS),
        },
    )

    existing_count = collection.count()
    log(f"ChromaDB collection '{COLLECTION_NAME}': {existing_count} existing documents")

    # Get existing IDs to skip
    existing_ids = set()
    if existing_count > 0:
        # Fetch in batches
        for offset in range(0, existing_count, 1000):
            batch = collection.get(limit=1000, offset=offset)
            existing_ids.update(batch["ids"])
    log(f"Found {len(existing_ids)} already-indexed chunks")

    # Prepare all chunks
    all_chunks = []
    for record in records:
        chunks = chunk_text(record["text"])
        for i, chunk in enumerate(chunks):
            chunk_id = f"{record['id']}_chunk{i}_{hashlib.md5(chunk[:100].encode()).hexdigest()[:8]}"
            if chunk_id in existing_ids:
                continue
            all_chunks.append({
                "id": chunk_id,
                "text": chunk,
                "metadata": {
                    "source_db": record["source_db"],
                    "source_table": record["source_table"],
                    "source_type": record["source_type"],
                    "record_id": record["record_id"],
                    "chunk_idx": str(i),
                },
            })

    log(f"Total new chunks to embed: {len(all_chunks)}")

    if dry_run:
        total_chars = sum(len(c["text"]) for c in all_chunks)
        est_tokens = total_chars // 4
        est_cost = (est_tokens / 1_000_000) * 0.02
        log(f"[DRY RUN] Would embed {len(all_chunks)} chunks")
        log(f"[DRY RUN] Total chars: {total_chars:,} | Est tokens: {est_tokens:,}")
        log(f"[DRY RUN] Est cost: ${est_cost:.4f} (text-embedding-3-small @ $0.02/1M tokens)")
        return 0

    if not all_chunks:
        log("Nothing new to embed")
        return 0

    # Process in batches
    total_embedded = 0
    t0 = time.time()

    for batch_start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[batch_start:batch_start + batch_size]
        texts = [c["text"] for c in batch]

        try:
            vectors = embed_batch(texts)
        except Exception as e:
            log(f"Embedding error at batch {batch_start}: {e}", "ERROR")
            time.sleep(5)
            try:
                vectors = embed_batch(texts)
            except Exception as e2:
                log(f"Retry failed: {e2}", "ERROR")
                continue

        # Add to ChromaDB
        collection.add(
            ids=[c["id"] for c in batch],
            documents=texts,
            embeddings=vectors,
            metadatas=[c["metadata"] for c in batch],
        )

        total_embedded += len(batch)

        if (batch_start // batch_size) % 5 == 0:
            elapsed = time.time() - t0
            rate = total_embedded / max(elapsed, 1)
            remaining = (len(all_chunks) - total_embedded) / max(rate, 0.1)
            log(f"Progress: {total_embedded}/{len(all_chunks)} chunks "
                f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)")

        # Rate limit: OpenAI allows 3000 RPM for embeddings
        time.sleep(0.1)

    elapsed = time.time() - t0
    final_count = collection.count()
    log(f"COMPLETE: Embedded {total_embedded} chunks in {elapsed:.1f}s")
    log(f"ChromaDB '{COLLECTION_NAME}' now has {final_count} total documents")
    return total_embedded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Embed Codex data into ChromaDB")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without embedding")
    parser.add_argument("--source", choices=["codex", "prism", "all"], default="all",
                        help="Which database to embed (default: all)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Texts per OpenAI API call (default: {BATCH_SIZE})")

    args = parser.parse_args()

    sources = ["codex", "prism"] if args.source == "all" else [args.source]

    log(f"Starting Codex->ChromaDB embedding | sources={sources} | dry_run={args.dry_run}")

    records = get_all_records(sources)
    if not records:
        log("No records found to embed")
        return

    log(f"Total records to process: {len(records)}")

    indexed = index_to_chromadb(records, dry_run=args.dry_run, batch_size=args.batch_size)
    log(f"Indexing complete: {indexed} new chunks added to ChromaDB")


if __name__ == "__main__":
    main()
