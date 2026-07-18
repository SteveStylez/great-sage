#!/usr/bin/env python3
"""Execute /tmp/missing_by_key.sql against sage-codex in chunks. On chunk
failure, retry that chunk's statements one-by-one so a single malformed row
doesn't drop the whole batch. INSERT OR IGNORE = idempotent. Does NOT drop."""
import subprocess, os

SRC = "/tmp/missing_by_key.sql"
PREFIX = "INSERT OR IGNORE INTO the_codex_embeddings"
STMTS_PER_CHUNK = 100

lines = [l for l in open(SRC).read().split("\n") if l.strip()]
# each statement is one line (builder writes one per line)
stmts = [l for l in lines if l.startswith(PREFIX)]
print(f"parsed {len(stmts)} statements", flush=True)

os.makedirs("/tmp/kchunks", exist_ok=True)
def run_file(text):
    fp = "/tmp/kchunks/x.sql"; open(fp,"w").write(text)
    r = subprocess.run(["wrangler","d1","execute","sage-codex","--remote","--file",fp],
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stderr

ok = fail = 0
for i in range(0, len(stmts), STMTS_PER_CHUNK):
    chunk = stmts[i:i+STMTS_PER_CHUNK]
    rc, err = run_file("\n".join(chunk))
    if rc == 0:
        ok += len(chunk)
    else:
        # fallback: per-row
        for s in chunk:
            rc2, err2 = run_file(s)
            if rc2 == 0: ok += 1
            else:
                fail += 1
                print(f"  ROW FAIL: {s[:90]} :: {err2[-90:]}", flush=True)
    if (i // STMTS_PER_CHUNK) % 10 == 0:
        print(f"  progress: {ok} ok / {fail} failed / {len(stmts)} total", flush=True)
print(f"DONE: {ok} ok, {fail} failed", flush=True)
