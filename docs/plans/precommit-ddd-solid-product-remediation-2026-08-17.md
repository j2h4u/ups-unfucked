# Pre-commit DDD/SOLID product remediation ledger

Status: **historical architecture/remediation receipt; product RC reopened.** The recorded 873-test and
deterministic quality-gate results remain valid for that exact tree, but they do not complete the broader
business goal of automatically using trustworthy fragments from every blackout and linked recharge.
Implementation and deployment are NO-GO pending
`docs/plans/unified-blackout-recharge-evidence-learning-plan.md`.

This is the execution ledger for the exact post-panel candidate. Earlier plans remain design inputs, but
their old test counts and unchecked decomposition are not current completion authority.

## Historical narrow-scope business acceptance

The checked items below describe the deliberately narrow IR-only Release-B scope. They are not current
acceptance authority for the reopened product scope.

- [x] Safety publication remains ahead of capture, assessment, reporting, and learning.
- [x] Every writable blackout is automatically captured and reaches an explainable outcome without an
  operator or agent deciding whether to use its data.
- [x] Self-tests, calibration, gaps, corruption, rebooted provenance, and model-derived values cannot
  authorize scientific learning.
- [x] Partial outages remain censored evidence: no automatic capacity, SoH, Peukert, total-runtime, or LUT
  claim is made.
- [x] The only automatic scientific model change is a bounded, independently evidenced downward change to
  load-sag compensation, through the sole `ModelOwner` writer.
- [x] Per-event hash-linked JSONL is authoritative; the index and reports are rebuildable projections.
- [x] Normal capture, assessment, rejection, learning, and reporting are unattended 24/7 paths.

## Architecture and quality gates

- [x] Domain policy, application orchestration, adapters, and composition are separate production layers.
- [x] Import Linter enforces six inward dependency contracts.
- [x] Tach passes in normal and exact mode against the current module graph.
- [x] The architecture guard rejects legacy model imports, alternate model writers, UPS command paths, and
  decline-policy relocation into application code.
- [x] Ruff format/lint and its configured complexity rules pass without mandatory-rule suppressions.
- [x] Every production module is at most 800 physical lines and every production class span at most 500.
- [x] CRAP is the test-quality gate: every measured production function is at most 30. Coverage percentage is
  informational and has no independent pass threshold.
- [x] Pyright and Vulture pass.

## Panel findings closed

- [x] Charge readiness is frozen from the last proven online sample at first battery operation, then reset.
- [x] A blackout seen while storage is unavailable retains its original first sample and later writes an
  explicit gap/rejected outcome instead of inventing a clean suffix.
- [x] START failure, END rejection, observation overflow, gap overflow, processing overlap, and a rapid second
  blackout preserve event boundaries or fail visibly.
- [x] A physical battery-state poll with missing voltage/load is published conservatively first, retained as
  unavailable capture evidence, and later converges to an explicit gapped/rejected JSONL outcome.
- [x] The bounded prestart FIFO coalesces repeated battery polls into physical episodes; overflow is surfaced
  immediately and later written as an aggregate receipt with count and first/last boundary provenance.
- [x] Graceful shutdown waits boundedly for lifecycle capacity and retries the final retained event before
  closing the writer.
- [x] Infrastructure rejection outcomes are restart-reconstructable and operator-visible.
- [x] The report outbox handles partial writes, `EINTR`, torn tails, and retry after `ENOSPC`/`EIO` without
  duplicating or silently acknowledging a report.
- [x] Report-outbox append, tail repair, pending reads, acknowledgement, and cursor mutation share one
  adapter-owned serialization boundary, including during a partial append.
- [x] A failed START transferred into the bounded aggregate overflow receipt releases its exact writer scope;
  retained physical boundaries continue to block maintenance until recovered.
- [x] Capture failure callbacks use typed aggregate methods and cannot acquire or mutate
  `BlackoutCapture` private lifecycle state from another module.
- [x] The redundant capture failure pass-through module is deleted; a repository-wide AST guard keeps
  private `BlackoutCapture` state inside its aggregate module.
- [x] Prestart recovery reconciles its durable tail and appends only missing gap/overflow/end records,
  including after END became durable before its acknowledgement; exact writer scope and FIFO receipt are
  completed once.
- [x] Report acknowledgement reads only the exact FIFO head, closes its file descriptor deterministically,
  and survives stale/out-of-order/duplicate acknowledgements and restart without cursor movement.
- [x] Report adapters use explicit facade methods instead of reaching through `JsonlIndex._report_outbox` or
  calling private `JsonlFilesystem` operations from the outbox.
- [x] An aggregate-overflow receipt is cleared only after its exact durable GAP is proven; an END/outcome
  reconciliation without that GAP keeps the receipt pending and appends it exactly once to the next retained
  event.
- [x] The durable-overflow proof projects the terminal event and matches GAP type, system provenance,
  blackout identity, canonical sequence, reason, count, and all first/last boot and monotonic boundaries;
  unrelated record counts and every mismatched field fail closed.
- [x] Overflow delivery is a two-slot transaction: the writer owns one immutable in-flight snapshot while
  later physical boundaries form a separate exact residual receipt; acknowledgement clears only the proven
  snapshot, queue rejection merges the exact prefix/suffix, and stale callbacks are no-ops.
- [x] Overflow recovery has a total tagged outcome (`not_attempted`, `attempted_unproven`,
  `proven_durable`): terminal reconciliation and partial failure release every attempted-but-unproven
  snapshot before removing its carrier; only an exact proof acknowledges it.
- [x] `service_stop` recognizes the same in-flight retained blackout and never double-submits it; a different
  pending blackout remains flushable. No-durable exits release their reservation, and ACK/release ownership
  is guarded by an explicit bounded delivery token rather than receipt equality or object identity.
- [x] The sole domain safety policy treats `UNKNOWN` conservatively, all expected capture bookkeeping faults
  use the typed post-publication boundary, the safety oracle states the actual monotonicity guarantee and
  sampled-grid role, and JSONL open/fstat/fchmod failures close every acquired fd exactly once.
- [x] `JsonlFilesystem.atomic_replace` and `sync_storage_directory` are the actual public implementations,
  not compatibility wrappers; all JSONL callers use those boundaries.
- [x] The aggregate privacy guard follows holder attributes such as `self._capture` and rejects cross-module
  access to private `BlackoutCapture` state with a red fixture.
- [x] Current decline reports recompute raw evidence per metric instead of trusting frozen terminal classes.
- [x] One CAL/self-test event does not poison six later natural events.
- [x] Firmware reserve uses the domain-selected origin-to-first-LB metric prefix while provenance from event
  start through that selected LB must be natural, same-boot, and gap-free.
- [x] Early pre-origin LB, pre-origin CAL, high-input self-test, pre-LB reboot, and pre-selected-LB gap are all
  rejected; post-LB damage is ignored only for the firmware-prefix metric.
- [x] Load-sag and long-partial decline metrics use their own current-policy evidence windows and independent
  latest-six cohorts.
- [x] Current safety floor, evidence-set identity, and IR bounds each have one policy authority.
- [x] Upward load-sag evidence is durably reported as possible degradation and never changes the model.
- [x] README, internal context, package metadata, ADR, glossary, scenarios, and runbook describe the current
  DDD/JSONL/IR-only product rather than deleted legacy behavior.

## Physical durability boundary

- [x] Graceful stop and recoverable storage failure are covered by durable retry/gap behavior.
- [x] The ADR and scenarios state the unavoidable boundary: a hard process/host kill before the first writer
  command becomes durable while storage is unwritable can lose the RAM-only sample. The service does not add
  a second synchronous journal or fabricate evidence to conceal that fact.
- [x] An uninterruptible kernel filesystem operation may temporarily delay evidence capture; safety
  publication remains isolated and live, and recoverable storage failures converge automatically afterward.

## Exact candidate receipt

- [x] `just check` passes on 2026-08-17 with 873 tests.
- [x] Ruff, suppression scan, source spans, Import Linter 6/6, Tach normal/exact, Pyright, Vulture, and CRAP
  `<=30` all pass.
- [x] The Kaizen-led product/scientific and DDD/SOLID/DRY panel reports no remaining Critical, High, or
  code-Medium issue in `docs/reviews/precommit-ddd-solid-product-panel-2026-08-17.md`.
- [x] The first premium Cross-AI run correctly returned NO-GO for an outbox race, failed-START scope leak,
  and capture-aggregate encapsulation gap; all three are fixed with deterministic regressions.
- [x] The second premium Cross-AI run returned GO with four Low findings; all four were removed and the
  expanded crash/FIFO/fd-close regressions pass on the successor candidate.
- [x] The third premium Cross-AI run returned NO-GO for one overflow-receipt durability mismatch and two
  encapsulation/guard Lows; all three are fixed with deterministic regressions in this 813-test successor.
- [x] The fourth premium Cross-AI run rejected the remaining record-count heuristic in overflow receipt
  reconciliation; it is replaced by exact typed projection proof with positive, unrelated-tail, and
  five-field mismatch regressions in this 820-test successor.
- [x] The fifth premium Cross-AI run found a concurrent receipt-update loss window; immutable in-flight and
  residual receipt slots now preserve exact new first/last provenance during asynchronous delivery, with an
  aggregate-level barrier regression in this 822-test successor.
- [x] The sixth premium Cross-AI run found an unproven terminal-exit stranding window; tagged outcomes now
  release unproven snapshots on both terminal reconciliation and partial failure before carrier removal,
  with exactly-once successor delivery in this 824-test candidate.
- [x] The seventh premium Cross-AI run found a `service_stop` double-submit High plus two ownership Lows;
  same-in-flight gating, symmetric no-durable release, and opaque delivery tokens close all three in this
  826-test candidate.
- [x] The eighth premium Cross-AI run found a defense-in-depth safety-policy Medium and three Lows; domain
  `UNKNOWN` fail-safe handling, typed capture errors, truthful oracle terminology, and complete fd cleanup
  close all four with deterministic regressions in this 843-test candidate.
- [x] The ninth premium Cross-AI run returned GO on Critical/High/Medium but found four actionable Lows;
  writer-lock/atomic-replace fd cleanup, the final typed poll boundary, signal-safe exact temporary cleanup,
  and a distinct operator alert for corrupt decline evidence close all four in this 850-test successor.
- [x] Premium Cross-AI run `20260817T070638Z` approves the 850-test candidate with zero Critical, High,
  Medium, or actionable Low finding. A later internal exact-tree panel then found one scientific Medium and
  two cleanup Lows, so this receipt remains historical rather than authority for the 856-test successor.
- [x] The successor retains invalid `UNKNOWN`/`COMMFAULT` outage boundaries as explicit gapped/rejected
  evidence, proves exact virtual-UPS temporary-file ownership even when descriptor inspection fails, and
  restores a worker thread's signal mask after bounded cleanup.
- [x] The final internal scientific/SRE pass found that `service_stop`/restart terminals could still enter
  science, a retained loss GAP could be bypassed by graceful stop, and a single failed ownership inspection
  could orphan a publication temporary. One domain terminal policy now censors every non-`power_restored`
  event with an explicit reason, stop flushes GAP before END, and independent fd/path identity inspection
  safely cleans either single-failure case. The exact successor passes all 873 tests.
- [x] The final exact-tree Kaizen/product, DDD/SOLID/DRY, and scientific/SRE panel unanimously returns GO:
  zero Critical, High, Medium, and actionable Low findings on the 873-test successor.
- [ ] Obtain a fresh premium Cross-AI verdict for the exact 873-test successor with zero Critical, High,
  Medium, or actionable Low finding.
- [ ] Create the repository RC commit only after the premium receipt is green.

## Deployment and live acceptance — deliberately pending

- [ ] Run the read-only preflight and preserve the external state backup.
- [ ] Perform the documented transform/cutover with rollback available.
- [ ] Verify physical and virtual OL plus healthy storage after startup.
- [ ] Optionally run the vendor short self-test to prove operational classification only.
- [ ] Run the bounded physical-outage UAT from the operations runbook.
- [ ] Re-run the expert panel against live receipts before removing the temporary state backup.
