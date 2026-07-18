#!/usr/bin/env python3
"""Build INSERT OR IGNORE statements for the D1 chunks genuinely absent from D2,
identified by content key (conversation_id, chunk_idx) — NOT id. Omit the id
column so D2's AUTOINCREMENT assigns fresh ids (the id mismatch is what broke
every prior attempt). Fetch D1 rows in conversation batches."""
import json, subprocess, collections

SRC, TBL = "sage-prism", "the_codex_embeddings"
# id intentionally excluded — D2 is AUTOINCREMENT + UNIQUE(conversation_id,chunk_idx)
COLS = ["conversation_id","chunk_idx","chunk_text","embedding_json","model","dims","created_at"]
OUT = "/tmp/missing_by_key.sql"

def wq(db, sql, retries=4):
    for _ in range(retries):
        r = subprocess.run(["wrangler","d1","execute",db,"--remote","--command",sql,"--json"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            try: return json.loads(r.stdout)[0].get("results", [])
            except: pass
    raise RuntimeError(f"{db}: {r.stderr[-160:]}")

def esc(v):
    return "NULL" if v is None else "'" + str(v).replace("'","''") + "'"

keys = set(json.load(open("/tmp/true_missing_keys.json")))
print(f"missing keys to migrate: {len(keys)}", flush=True)

# group by conversation_id so we can batch-fetch
by_conv = collections.defaultdict(set)
for k in keys:
    conv, ci = k.rsplit("#", 1)
    by_conv[conv].add(ci)
convs = sorted(by_conv)
print(f"across {len(convs)} conversations", flush=True)

written = 0
fout = open(OUT, "w")
BATCH = 60
for i in range(0, len(convs), BATCH):
    batch = convs[i:i+BATCH]
    inlist = ",".join(esc(c) for c in batch)
    rows = wq(SRC, f"SELECT {', '.join(COLS)} FROM {TBL} WHERE conversation_id IN ({inlist})")
    for row in rows:
        key = f"{row['conversation_id']}#{int(row['chunk_idx'])}"
        if key in keys:
            fout.write("INSERT OR IGNORE INTO the_codex_embeddings (" + ", ".join(COLS) + ") VALUES (" +
                       ", ".join(esc(row[c]) for c in COLS) + ");\n")
            written += 1
    if (i//BATCH) % 20 == 0:
        print(f"  convs {i}/{len(convs)}, written {written}", flush=True)
fout.close()
print(f"DONE: wrote {written} INSERT OR IGNORE statements to {OUT}", flush=True)
