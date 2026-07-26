# REMAINING — deliberately left after the code-review fix pass

Everything in the review's items 1-6 was fixed in place and verified (py_compile /
bash -n / import test per file — see the coordinator summary for the per-file list).
This file covers what was intentionally NOT done, and why.

## Item 7 — Architecture-level refactors (not attempted)

These are real observations but are refactors, not bugs, and touch every daemon
in the fleet. Doing them "if time allows, only if fully verifiable" was the
instruction; given the blast radius, they were left alone rather than risked on a
public portfolio repo without a full daemon-by-daemon regression pass:

1. **The bridge is reimplemented 5 ways** (`requests`, `urllib`, `curl` subprocess,
   `wrangler` CLI) across `task_dispatch_bridge.py`, `goblin_dispatch.py`,
   `gs_health_monitor.py`, `gs_agent_bus.py`, `d2_migrate.py`. Consolidating into a
   single `gs_bridge.py` with one retry/timeout policy is the right long-term move,
   but every one of those call sites has slightly different param-passing
   conventions (some send `db_id`, some don't; some use `?` placeholders, some
   inline SQL) that would need to be normalized first. Flagging for a dedicated pass.

2. **Config loads from 4 places** — `os.getenv` scattered per-file, a `.env` file
   under `~/Library/Stylez/`, a `~/.env.codex` file (codex_to_chromadb.py), and
   inline defaults. A single config loader is straightforward but changes the
   import-time behavior of every daemon that currently reads env vars directly;
   left alone to avoid a fleet-wide behavior change in this pass.

3. **Packaging via `sys.path` hacks + hardcoded `~/workspace` paths** — making this
   clone-and-run via a `pyproject.toml` with console entry points is real value for
   a portfolio repo, but it's a structural change (imports, working-directory
   assumptions, path construction throughout) that deserves its own PR and testing
   pass rather than being folded into a bug-fix pass.

## Smaller scope decisions made during the fix pass

- **`gs_agent_bus.py`'s `logging.basicConfig()` root-logger hijack** was fixed (now
  a module-scoped logger with its own `FileHandler`, `propagate=False`). The same
  `logging.basicConfig()` pattern also exists in `goblin_dispatch.py` and
  `gs_architect.py`. Those two are typically run standalone (`if __name__ ==
  "__main__"`) rather than imported as a library by other daemons, so the blast
  radius of the root-logger hijack is much smaller there. Left as-is since the
  finding specifically named `gs_agent_bus.py`; flagging here in case Steve wants
  the same treatment applied fleet-wide for consistency.

- **`codex_retrieval.py` / `codex_to_chromadb.py` semantic wiring**: per the
  instruction ("wire it up OR document as alternative paths if too invasive"),
  documented rather than wired. Making `codex_retrieval.py` actually query the
  Chroma collection would mean adding a `chromadb` runtime dependency to a module
  that's currently dependency-light (stdlib + `urllib`) and used synchronously in
  hot paths (CLI, prompt injection). That's a legitimate follow-up but is new
  functionality, not a fix to existing wiring — the two files were never connected
  in the first place, so there's no regression risk in leaving them documented as
  parallel retrieval strategies (see the module docstrings added to both files).

- **ChromaDB delete-before-reinsert (upsert)**: implemented as an actual code fix
  (not deferred) in `codex_to_chromadb.py`'s `index_to_chromadb()` — see the
  per-file changelog. Calling this out here only because it's a real behavior
  change to a data pipeline: it now issues a `collection.delete(where=...)` call
  for every record whose current chunk IDs aren't fully present in the index
  (i.e., new-or-changed records only — unchanged records are still skipped
  entirely, preserving the original incremental/low-cost design). This has not
  been run against the live ChromaDB collection in this pass (no chromadb runtime
  / no access to the real `gs_memory_chromadb` store from this environment) — only
  `py_compile` and a static import test were possible. Recommend a `--dry-run`
  pass against the real collection before the next scheduled re-embed to confirm
  the purge-then-reinsert path behaves as expected at scale.
