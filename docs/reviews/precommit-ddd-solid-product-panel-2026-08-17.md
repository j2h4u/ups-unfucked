# Pre-commit product and DDD/SOLID panel — 2026-08-17

> **Historical receipt, superseded for product completeness.** This panel proved the exact narrow IR-only
> architecture candidate, not the broader requirement to use trustworthy fragments from short, partial,
> deep/safe-shutdown and linked recharge telemetry. The current product authority is
> `docs/plans/unified-blackout-recharge-evidence-learning-plan.md`; repository RC and deployment are NO-GO
> until that plan's applicable gates pass.

## Verdict

**Historical verdict:** GO for review of that narrow candidate. It is not a current repository-RC or
deployment GO.

The Kaizen lead, an independent scientific/SRE adversary, and an independent DDD/SOLID/DRY reviewer
inspected production code before relying on tests. The panel iterated until no Critical, High, or code-level
Medium finding remained. This receipt supersedes the historical 639-test panel artifact.

## Business result

- Safety publication remains the first responsibility of each successful poll.
- Natural-blackout data is automatically captured, assessed, used, or rejected without operator approval.
- CAL/self-test, gaps, corrupt data, rebooted provenance, and model-derived values cannot authorize science.
- Partial outages remain censored; the product does not claim measured capacity, SoH, Peukert, total runtime,
  or a learned LUT from them.
- Only independently evidenced, bounded downward load-sag compensation can change automatically, through the
  sole model owner and safety oracle.
- Decline reporting recomputes three bounded signals from current sealed raw evidence and never diagnoses
  capacity or SoH.

## Architecture result

- DDD layering is substantive: domain owns policy, application orchestrates ports, adapters own NUT/storage
  and model persistence, and `monitor.py` is the composition root.
- SOLID dependency direction is enforced by Import Linter and Tach exact mode.
- DRY authorities are singular for lifecycle transitions, readiness, safety floor, evidence identity, IR
  bounds, decline origin/selected LB, prestart recovery, and model mutation.
- Per-event hash-linked JSONL is evidence; registry, index, reports, and health are bounded projections.
- Production modules and classes satisfy hard physical-size limits without complexity suppressions.

## Adversarial findings closed during this panel

- First-OB readiness previously reset before capture and is now frozen at the last proven OL sample.
- Capture-unavailable and START-failure paths no longer invent a clean mid-blackout suffix.
- Rejected END/GAP, queue overflow, processing overlap, rapid consecutive outages, and graceful stop no longer
  silently merge or drop event boundaries.
- Invalid physical battery-state polls with missing voltage/load now preserve safety-first publication and
  later converge to a durable gapped/rejected event instead of disappearing as a poll error.
- The bounded prestart FIFO groups repeated polls by physical episode and produces an operator-visible,
  durable aggregate receipt if its finite boundary is exceeded while storage remains unavailable.
- Infrastructure rejections are reconstructable after restart and visible to the operator.
- The report outbox now survives short writes, `EINTR`, torn tails, and retryable `ENOSPC`/`EIO` failures
  without duplicate acknowledgement.
- The outbox's append/repair/read/ack/cursor lifecycle is serialized across capture-writer and reporting
  threads; a deterministic paused-partial-write test proves the reader cannot truncate in-flight bytes.
- A failed START represented by the aggregate overflow receipt releases its exact reserved writer scope,
  while retained scopes still keep maintenance behind capture recovery.
- Capture recovery is an enforced aggregate boundary: the external callback adapter can call only typed
  public recovery methods and an AST guard rejects private state access.
- The redundant callback adapter was subsequently deleted; the guard now checks every production module,
  and prestart recovery resumes from its durable sequence without duplicate gaps or a stranded END scope.
- Report delivery now acknowledges only the exact FIFO head, closes bounded readers deterministically, and
  uses explicit Index/Outbox/Filesystem facade methods rather than sibling private-state reach-through.
- Aggregate-overflow reconciliation clears its receipt only when the exact durable GAP is present; a
  terminalized event without that GAP leaves one pending receipt for the next retained event.
- That proof now reads the typed event projection and matches the GAP kind, provenance, blackout identity,
  canonical sequence, reason, count, and every first/last boot and monotonic boundary; record count alone is
  never treated as evidence.
- Overflow delivery now reserves one immutable snapshot and collects boundaries arriving during the async
  write into a separate residual receipt. Only the exact proven snapshot is acknowledged; rejected enqueue
  merges the exact prefix and suffix, and stale acknowledgements cannot clear either slot.
- Recovery results are a total tagged outcome. Terminal reconciliation or partial failure without durable
  proof returns the exact in-flight snapshot to pending before its FIFO carrier is removed; a proven snapshot
  alone is acknowledged.
- Shutdown cannot submit a second recovery for the same retained blackout already in flight; no-durable exits
  return the reservation immediately, and a bounded opaque delivery token makes stale ACK/release ownership
  explicit even when receipt values are equal.
- The domain safety authority itself maps `UNKNOWN` conservatively; expected capture bookkeeping errors cross
  a typed post-publication boundary; the safety oracle distinguishes its pointwise monotonicity guarantee from
  its sampled regression net; JSONL file primitives close every acquired descriptor on all validation errors.
- Filesystem durability operations are real public adapter boundaries rather than wrappers over private
  twins, and the aggregate privacy guard follows holder attributes such as `self._capture`.
- Decline selection is per metric and current-policy; CAL does not poison later natural cohorts.
- Firmware reserve uses the domain-selected origin-to-first-LB prefix but requires natural, same-boot,
  gap-free provenance from EventStart through that selected LB.
- Early LB, pre-origin CAL, high-input self-test, pre-LB reboot, and a gap before the selected LB are rejected;
  post-LB damage is ignored only for the firmware prefix metric.
- Upward load-sag evidence is durably explained as possible degradation and never committed to the model.
- Runtime/package documentation no longer advertises automatic SoH or obsolete legacy behavior.
- Invalid `UNKNOWN`/`COMMFAULT` outage candidates with unusable battery/load telemetry now preserve their
  physical boundary and converge to a durable gapped/rejected event without becoming scientific evidence.
- Atomic virtual-UPS publication proves the exact temporary inode after exclusive creation and restores the
  calling thread's signal mask on every bounded-cleanup path.
- A closed domain terminal policy permits science only after exact physical `power_restored`; service stop,
  restart closure, damaged, missing, and unknown terminal facts remain retained but censored with an explicit
  `event_not_naturally_completed` reason.
- Graceful stop cannot submit END ahead of a retained loss GAP; a rejected GAP retry blocks the END, and the
  successful retry preserves exact GAP-then-END order without duplication.
- Temporary-file ownership is inspected independently through descriptor and path identities, so either
  single inspection failure still permits exact cleanup while two unavailable identities fail closed.

## Exact deterministic receipt

- `just check`: passed on the final reviewed candidate.
- Tests: **873 passed** on Python 3.14.6.
- CRAP: every measured production function `<=30`; no independent coverage-percentage gate.
- Ruff format/lint and configured complexity rules: passed.
- Mandatory complexity suppression scan: passed with zero suppressions.
- Source spans: every production module `<=800` lines and class `<=500` lines.
- Import Linter: 6/6 contracts kept.
- Tach: normal and exact modes passed.
- Pyright: 0 errors; Vulture passed.
- `git diff --check`: passed.

## Remaining Low and deployment-only work

- The JSONL capacity policy uses private calls within its own storage adapter family. A proposed public API
  would violate the tested facade contract, so the panel accepts this bounded internal coupling for the RC;
  reconsider only after live UAT if a real maintenance problem appears.
- `CaptureQueueHealth` now lives in the neutral application value module rather than the concrete writer.
- README Quick Start now states that it is fresh-install-only and directs upgrades to transform-first cutover.
- A hard process/host kill before any first writer command becomes durable while storage is unwritable can
  lose the RAM-only sample. This physical boundary is explicit; graceful stop is durably retried.
- A kernel-blocked regular-file operation cannot be safely cancelled from Python. The panel accepts this
  physical Low: safety publication stays isolated, and the existing writer lane retries once the kernel and
  storage return.
- Premium Cross-AI review is the final repository-RC gate after this receipt.
- Premium Cross-AI run `20260817T040737Z` returned NO-GO and found the outbox race, START-scope leak, and
  capture-boundary gap above. The panel accepted those findings; this receipt describes their remediated
  successor, which requires a fresh premium verdict.
- Premium Cross-AI run `20260817T043159Z` returned GO with four Low cleanup findings. Those findings are now
  closed in this successor.
- Premium Cross-AI run `20260817T045851Z` returned NO-GO for an overflow-receipt durability mismatch plus
  filesystem-boundary and privacy-guard Lows. All three are closed in its successor.
- Premium Cross-AI run `20260817T051856Z` found that the successor still used record count as its durable GAP
  proof. The heuristic is gone; exact projection matching and seven adversarial regressions are green in this
  successor.
- Premium Cross-AI run `20260817T053543Z` found that a newer boundary could arrive while the proven snapshot
  was in flight and then be cleared with it. A bounded two-slot delivery protocol and deterministic async
  barrier regression close that window in its successor.
- Premium Cross-AI run `20260817T055449Z` found that terminal reconciliation without the GAP proof could
  strand the in-flight receipt after removing its carrier. Tagged recovery outcomes now release every
  attempted-but-unproven snapshot before carrier removal in its successor.
- Premium Cross-AI run `20260817T060728Z` found a shutdown double-submit High and two recovery-ownership Lows.
  Same-in-flight shutdown gating, symmetric no-durable release, and opaque delivery tokens close them in this
  successor.
- Premium Cross-AI run `20260817T062642Z` found a domain fail-safe Medium and three actionable Lows. Internal
  `UNKNOWN` conservatism, typed capture bookkeeping errors, truthful oracle receipts, and exact fd cleanup
  close all four in its successor.
- Premium Cross-AI run `20260817T064334Z` returned GO on Critical/High/Medium but identified four actionable
  Lows. The exact writer-lock and atomic-replace fd paths now preserve primary errors, the final expected poll
  invariant uses the typed post-publication boundary, atomic virtual-UPS cleanup defers the deadline only
  across exact-owned close/unlink work, and corrupt decline evidence produces a distinct operator alert.
  Their 850-test successor requires one final exact-tree premium verdict.
- Premium Cross-AI run `20260817T070638Z` independently reran the complete gate and returned an unqualified
  **GO** with zero Critical, High, Medium, or actionable Low. Its source-first receipt is
  `docs/reviews/cross-ai-precommit-final-2026-08-17/20260817T070638Z/claude-opus-5.md`.
- That verdict is bound to its 850-test input tree. A later internal panel found and closed one scientific
  Medium (invalid `UNKNOWN`/`COMMFAULT` outage evidence could disappear) and two cleanup Lows (temporary-file
  ownership on descriptor-inspection failure and worker signal-mask restoration). The resulting 856-test
  tree requires a fresh premium verdict before the repository RC commit.
- A subsequent exact-tree scientific/SRE pass returned NO-GO with one High, one Medium, and one actionable
  Low: non-natural terminal facts could enter science, graceful stop could bypass a retained GAP, and the
  complementary ownership-inspection failure could orphan a temporary. All three are closed in the 873-test
  successor described above; its final exact-tree panel and premium verdict supersede every earlier receipt.
- The final exact-tree Kaizen/product, DDD/SOLID/DRY, and scientific/SRE reviewers unanimously return
  **GO**, each with zero Critical, High, Medium, and actionable Low findings. The external premium rerun was
  attempted against this successor but Claude returned only a session-limit error; it is not counted as a
  verdict and must be repeated after the account reset.
- Transform/cutover, systemd/NUT verification, optional vendor self-test, bounded physical-outage UAT, and
  post-UAT backup cleanup remain separate live work.
