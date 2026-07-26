#!/usr/bin/env python3
"""Migrate codex tables from sage-prism (D1) to sage-codex (D2) via wrangler CLI."""

import subprocess
import json
import sys
import os

TABLES_TO_MIGRATE = [
    "codex_academic",
    "codex_brand",
    "codex_code",
    "codex_conversations",
    "codex_convos",
    "codex_messages",
    "codex_music",
    "codex_personal",
    "gluttony_codex_index",
    "the_codex",
    "the_codex_browsing",
    "the_codex_conversations",
    "the_codex_conversations_native",
    "the_codex_emails",
    # "the_codex_embeddings",  # 291K rows - skip, needs batched migration
    "the_codex_sessions",
    "the_codex_transcripts",
]

BATCH_SIZE = 50  # rows per INSERT batch
PAGE_SIZE = 500  # rows per SELECT page


def run_wrangler(db, sql):
    """Execute SQL via wrangler and return parsed JSON results."""
    cmd = ["wrangler", "d1", "execute", db, "--remote", f"--command={sql}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    # Find the JSON array in the output
    try:
        start = output.index("[")
        # Find the matching end bracket
        depth = 0
        for i, c in enumerate(output[start:], start):
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    data = json.loads(output[start : i + 1])
                    return data[0].get("results", [])
    except (ValueError, json.JSONDecodeError, IndexError) as e:
        print(f"  ERROR parsing wrangler output: {e}")
        print(f"  stdout: {result.stdout[:500]}")
        print(f"  stderr: {result.stderr[:500]}")
        return None


def escape_sql_value(val):
    """Escape a value for SQL insertion."""
    if val is None:
        return "NULL"
    if isinstance(val, (int, float)):
        return str(val)
    # String: escape single quotes
    s = str(val).replace("'", "''")
    return f"'{s}'"


def migrate_table(table):
    """Export all rows from sage-prism and insert into sage-codex."""
    print(f"\n{'='*60}")
    print(f"Migrating: {table}")
    print(f"{'='*60}")

    # Get count
    rows = run_wrangler("sage-prism", f"SELECT COUNT(*) as cnt FROM {table}")
    if not rows:
        print(f"  Could not get count for {table}")
        return 0
    total = rows[0]["cnt"]
    print(f"  Source rows: {total}")

    if total == 0:
        print("  Nothing to migrate.")
        return 0

    migrated = 0
    offset = 0

    while offset < total:
        # Fetch a page
        # ORDER BY rowid: without a stable order, LIMIT/OFFSET can skip or repeat rows
        # across pages (silent data loss — the exact failure the repair scripts exist to fix).
        fetch_sql = f"SELECT * FROM {table} ORDER BY rowid LIMIT {PAGE_SIZE} OFFSET {offset}"
        page_rows = run_wrangler("sage-prism", fetch_sql)

        if not page_rows:
            print(f"  ERROR: No data returned at offset {offset}")
            break

        if len(page_rows) == 0:
            break

        # Get column names from first row
        columns = list(page_rows[0].keys())
        col_list = ", ".join(columns)

        # Batch insert
        for batch_start in range(0, len(page_rows), BATCH_SIZE):
            batch = page_rows[batch_start : batch_start + BATCH_SIZE]
            values_list = []
            for row in batch:
                vals = ", ".join(escape_sql_value(row.get(c)) for c in columns)
                values_list.append(f"({vals})")

            insert_sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES {', '.join(values_list)}"

            # Write SQL to a temp file to avoid shell escaping issues
            sql_file = f"/tmp/d2_migrate_{table}.sql"
            with open(sql_file, "w") as f:
                f.write(insert_sql)

            cmd = [
                "wrangler", "d1", "execute", "sage-codex", "--remote",
                f"--file={sql_file}"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                # Check if it's a size issue - try smaller batches
                if "too large" in result.stderr.lower() or "payload" in result.stderr.lower():
                    print(f"  Batch too large at offset {offset}+{batch_start}, trying row-by-row...")
                    for row in batch:
                        vals = ", ".join(escape_sql_value(row.get(c)) for c in columns)
                        single_sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({vals})"
                        with open(sql_file, "w") as f:
                            f.write(single_sql)
                        r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                        if r2.returncode == 0:
                            migrated += 1
                        else:
                            print(f"  WARN: Failed single insert: {r2.stderr[:200]}")
                    continue
                else:
                    print(f"  ERROR at offset {offset}+{batch_start}: {result.stderr[:300]}")
                    # Try to continue
                    continue

            migrated += len(batch)

        offset += len(page_rows)
        print(f"  Progress: {min(migrated, total)}/{total} rows")

    # Verify
    verify = run_wrangler("sage-codex", f"SELECT COUNT(*) as cnt FROM {table}")
    dest_count = verify[0]["cnt"] if verify else "ERR"
    print(f"  Final count in sage-codex: {dest_count}")

    # Clean up temp file
    try:
        os.remove(f"/tmp/d2_migrate_{table}.sql")
    except:
        pass

    return dest_count


def main():
    results = {}

    for table in TABLES_TO_MIGRATE:
        dest_count = migrate_table(table)
        results[table] = dest_count

    # Add embeddings as schema-only
    results["the_codex_embeddings"] = "schema_only"

    print("\n\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    for table, count in results.items():
        print(f"  {table}: {count}")

    return results


if __name__ == "__main__":
    main()
