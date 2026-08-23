# ADR 0002: Durable Discharge Journal and Evidence-Gated Replay

**Status:** Superseded, 2026-08-23. The current implementation keeps raw evidence in
`events/telemetry.jsonl` and derived aggregates in `events/history.jsonl`; this ADR is historical.

## Context

The monitor previously held the active discharge buffer only in RAM. On 2026-08-14 the UPS
experienced a real blackout and the host performed its normal low-battery shutdown. The signal
path saved `model.json`, but the in-memory voltage/time buffer was lost before power returned.
The remaining model and LUT data could not reconstruct the raw event or the time after shutdown.

An earlier 2026-03-14 decision treated timestamp deduplication/checkpointing as sufficient
restart protection. Timestamp deduplication only prevents duplicate exported samples; it does
not persist an accepted sample across process death and it cannot recover a buffer that was never
written. That decision is obsolete and is explicitly reversed here.

## Decision

Use a small local append-only JSONL journal at
`~/.config/ups-battery-monitor/discharge-events-v1.jsonl` as the durable source of raw operational
discharge evidence.

- Write `start`, `sample`, `end`, and `applied` records with a stable event ID and monotonic
  sequence.
- Synchronise every accepted on-battery record. Ignore only a torn final line; middle corruption
  degrades journal health and blocks scientific replay.
- Refuse symlink/non-regular journal paths. The directory is `0700`; the journal is `0600`.
- Replay idempotently at boot. Continue the same event if the UPS is still on battery, otherwise
  close at the last confirmed sample and represent the reboot interval as unknown.
- Keep lifecycle (`open`, `closed_*`) separate from evidence class
  (`operational_partial`, `operational_gapped`, `operational_complete_to_safety_threshold`,
  `controlled_quick_test`, `controlled_capacity_test`). Closure is not scientific completeness.
- Apply authoritative capacity, SoH, and Peukert state only after the explicit evidence gate and
  one atomic model commit. Partial/reboot-gapped observations remain operational evidence.
- Keep persistence fail-visible but fail-open for the safety path: a journal error must not block
  the virtual low-battery signal or NUT/upsmon shutdown.

## Consequences

The next synced sample survives SIGTERM, crash, reboot, and torn-tail recovery without requiring
Grafana, WAN, Alloy, or a new privilege. The journal is intentionally simple and retained
indefinitely because discharge volume is negligible. Grafana remains a secondary forensic source,
not runtime storage.

Older code does not project the new journal-derived counters during rollback. Rollback therefore
preserves the journal and recovery artifacts, and re-upgrade replays them; operators must not
delete or manually merge the journal. This is a reversible operational downgrade, not a migration
that rewrites evidence.

## Rejected alternatives

- **RAM-only/timestamp dedup:** this is the reversed 2026-03-14 decision; it loses accepted data
  on process death.
- **Full-buffer checkpoint rewrite:** possible, but needlessly rewrites a growing trace and makes
  audit history and directory durability easier to get wrong.
- **Grafana as primary storage:** unavailable during network/cloud/credential failures and subject
  to scrape cadence and Prometheus lookback semantics.
- **Automatic hardware deep test:** outside this durability change and unsafe without a written,
  supervised capacity-test protocol and explicit user approval.
