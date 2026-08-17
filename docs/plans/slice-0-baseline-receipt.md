# Slice 0 baseline receipt

**Status:** baseline established; Slice 0 implementation is not complete; deployment remains **NO-GO**.

## Immutable identity

- Branch: `feat/natural-blackout-learning`
- Baseline commit: `259b991b8c04eaf711212ee19c063f932a2091c3`
- Parent commit: `77a84ca9775b1f110f1aa1b7f2de527dee5e5592`
- Commit time: `2026-08-17T20:45:08+05:00`
- Fixture manifest: `tests/fixtures/slice0/fixture-manifest.json`
- Fixture manifest SHA-256: `3ce94741d7948f78a2e5e5ebc3203b57b7b0bd664bf89c5ddebe9a074c525476`
- Fixture manifest Git blob: `ae690187ece554a30236ff18a037b7cecbc82b7e`
- Frozen entries: 34 files and 75 exact test IDs.

The manifest was committed with a null self-referential baseline SHA. This receipt resolves that deliberately
non-circular field to the baseline commit above. The manifest bytes named here are the exact bytes stored in
that commit.

## Verification receipt

The exact candidate tree was checked immediately before the baseline commit with `just check`:

- Ruff format and lint: passed;
- mandatory-complexity suppression scan: passed;
- production source-span budgets: passed;
- Import Linter: 6 contracts kept, 0 broken;
- Tach normal and `--exact`: passed;
- Pyright: 0 errors and 0 warnings;
- Vulture: passed;
- pytest with coverage as CRAP input: 873 passed in 64.44 seconds;
- CRAP: every measured function below or equal to 30; maximum observed value 29.67.

The manifest itself was then validated as JSON; all 34 file hashes and all 75 named test IDs resolved exactly.
`git diff --cached --check` also passed. The manifest is data-only and did not change the tested runtime.

## Scope and exclusions

The commit contains the complete intended DDD/v2 candidate: production code, tests, legacy removals,
architecture and quality gates, product authority, plans, operational documentation, and selected final review
verdicts. It contains no live `model.json`, event data, state database, socket, PID, lock, `.env`, private key,
or credential file. Test-only NUT credentials remain explicit fixtures.

Generated Cross-AI input copies, `opencode.json`, serve logs, caches, virtual environments, graph exports, and
historical generated review directories were intentionally excluded. They are not product authority and are
not part of the baseline tree.

## Review status

- Standard Cross-AI plan convergence is recorded in
  `docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/summary.md`.
- Product-statement reader convergence is recorded in
  `docs/reviews/cross-ai-product-statement-reader-test-cycle3/20260817T152430Z/summary.md`.
- The last premium attempt returned an Anthropic HTTP 429 session-limit error and produced no verdict. It is
  retained as a missing verdict, not counted as approval and not used to block standard reviewers or Slice 0
  implementation.

## Next allowed work

Slice 0 may now add ADR 0004, the read-only telemetry capability baseline producer, identity validation and v3
deployment preflight. Slices 1–4 remain undeployed and must ship atomically only after their implementation,
targeted checks, full release gate, independent panel review, available premium review and live UAT.
