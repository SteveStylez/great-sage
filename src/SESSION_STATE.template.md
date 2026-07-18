# SESSION STATE

<!--
  This is the warm handoff a session writes at close (the DIABLO ritual) and the
  next session reads at boot (the ARISE ritual). It is the difference between a
  new session resuming work and a new session starting cold.

  All content below is FICTIONAL placeholder text to show the structure.
  Replace every field with your own state. Keep it short: this file is read in
  full on every boot, so it should hold the working set, not the archive.
-->

Last updated: 2026-01-01T00:00:00Z (by DIABLO close ritual)
Session id: example-0000

## Identity anchor

Operator: Example Operator. System: Example Autonomous System.
One line of who-am-I so a cold session does not have to reconstruct it.

## Done this session

- Shipped the example widget and verified it renders in the staging build.
- Closed out three queued render jobs; all outputs landed in the exports folder.
- Answered the open question about the example schema: use per-record keys, not row ids.

## In flight (resume these first)

- Example migration is half done: batches 1 through 4 loaded, batches 5 through 9 still pending. Re-run the backfill script to finish.
- Draft of the example report is written but not proofread. File is on disk, needs one editing pass before it goes out.
- Waiting on an external job to finish before the next step can start.

## Owner-only actions (cannot be done unattended)

- Sign in to the example dashboard and confirm the pending payout (behind the operator's login).
- Approve or reject the example proposal the architect flagged for review.
- Decide whether to publish the example post; drafted and ready, needs a human yes.

## Next priorities (in order)

1. Finish the example migration backfill.
2. Proofread and send the example report.
3. Start the example feature once the external job clears.

## Open blockers

- External service credential expires soon; renew before the next scheduled run.
- One daemon has produced no output for two cycles; check its log before trusting it.

## Notes for the next session

- The example queue counts in this file may be stale. Verify against live state at boot before acting on them.
- If this file is missing entirely, that is a cold boot: reconstruct state from the live database instead.
