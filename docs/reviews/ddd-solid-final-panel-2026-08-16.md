# Final DDD/SOLID Expert Panel — 2026-08-16

> Historical snapshot, superseded on 2026-08-17. This GO applies only to the earlier
> 639-test candidate described below; it is not approval of the subsequently changed tree.
> The current pre-commit verdict and gate receipt must be taken from the newer panel artifact.

## Scope and evidence

The panel reviewed the exact dirty `feat/natural-blackout-learning` repository candidate before
deployment. Kaizen led the decision; independent DDD/SOLID and SRE/scientific-integrity reviewers
read production code before tests. Business priority was safety, scientific correctness,
reliability, maintainability, then simplicity. No reviewer changed code or touched the live UPS,
NUT services, state, or deployment.

Final deterministic receipt: `just check` passes with 639 tests, CRAP maximum `29.89 <= 30`, Ruff
default complexity rules, zero mandatory complexity suppressions, module `<=800`, class `<=500`,
Pyright, Vulture, Import Linter 6/6, and Tach normal/exact. Coverage is informational only.

## Final verdict

**GO for repository RC.** This is not deployment approval. Live systemd/NUT behavior and the
bounded physical UPS acceptance remain unverified Cluster 12 work.

- DDD: 8.3/10.
- SOLID: 7.9/10.
- Critical, High, Medium blockers: none remaining.

## Material findings found and closed during review

- Historical partial, operational-only, or capture-damaged events are rejected before projection
  into a future IR-learning cohort.
- Event capture has a 64 MiB aggregate ceiling and retains a 2 MiB terminal reserve; missing
  committed segments fail closed, while an exact pre-create reservation remains recoverable.
- Event files are size-preflighted before full reads.
- Index rebuild merge is bounded and resumable, uses cumulative integrity verification, seals the
  verified output read-only, promotes in O(1), and removes the recovery cursor last.
- Seal replay no longer scans the full growing index.
- The monitor explicitly wants the virtual NUT driver, while the driver remains bound and ordered
  after the monitor for restart recovery.
- Safety publication failure marks fatal stop intent before cleanup. Publication and stale-output
  invalidation share a finite main-thread deadline. Nested one-shot and periodic timer restoration
  does not extend an outer deadline.

## Architecture assessment

The inward dependency direction is real: composition to adapters and application, application to
domain, and domain to battery math. Domain lifecycle and learning policies are executable runtime
authorities rather than documentation-only abstractions. Consumer-owned ports limit application
authority, `ModelOwner` remains the sole scientific writer, and JSONL evidence stays independent
from journald and model-derived projections.

The former large persistence and orchestration modules were split by responsibility into event
stream, capacity, catalog, index, merge, codec, replay, reporting, and persistence collaborators.
This removed concentration and unbounded work rather than merely hiding line count. The remaining
graph is still substantial because crash-safe storage is substantial, but its boundaries and
budgets are explicit and executable.

## Accepted Low and next Kaizen step

`JsonlEventCapacity` still reaches private methods on the adapter-internal work-registry
collaborator. The coupling is bounded, remains inside one adapter family, creates no outward or
circular dependency, and is not a release blocker. After live UAT, the next smallest improvement is
to replace that callback with a narrow internal registry capability protocol if it makes the code
clearer without adding a framework.

The preserved earlier review remains unchanged as historical provenance and may contain NO-GO
findings that this document closes. Deployment, physical outage proof, post-UAT adjudication, and
temporary backup cleanup remain open by design.

## Premium-review remediation addendum — 2026-08-17

The first premium Claude Opus pass returned NO-GO and its output is preserved under
`docs/reviews/cross-ai-ddd-final-2026-08-16/20260816T183248Z/`. The panel re-opened the repository
verdict and confirmed the material findings rather than treating the earlier GO as final.

- The telemetry-loss grace is now derived from transport timing and the reserve between the
  configured shutdown threshold and the two-minute hard floor; current defaults yield 30 seconds.
  Cold start retries without inventing `LB`, while publication-integrity failures remain fatal.
- The virtual NUT instance is a repository-owned exact unit wanted by the monitor and is removed
  from `nut-driver.target`; this eliminates the verified host ordering cycle without changing the
  physical UPS driver chain.
- Fatal monitor restarts are limited to three attempts in five minutes. Ordinary telemetry loss is
  retried inside the running process.
- The deadline handler is completion-aware, so an alarm after a successful atomic publication
  cannot turn that success into stale invalidation.
- `shutdown_minutes` must now be strictly greater than the canonical two-minute hard safety floor,
  so an operator cannot accidentally collapse the telemetry-loss grace back to one second.
- The ADR, glossary, scenarios, and runbook now describe the managed exact-instance lifecycle and
  the safety-visible consequence of a maintenance stop without claiming deployment.

After these changes the three reviewers again returned **GO for repository RC**, with no remaining
Critical, High, or Medium blocker. The 30-second simultaneous-blackout-plus-NUT-loss tradeoff and
the installed merged systemd topology remain explicit live-UAT questions.

The next premium exact-tree pass also returned GO but identified one operator-facing Low: the
example configuration and loader still silently accepted keys from the deleted diagnostic
scheduler. That compatibility carve-out and its dead `config.toml`/README surface are now removed;
old keys produce the ordinary unknown-key warning. The other Low was reproducible Python bytecode
and test coverage residue, which is not product state and is scheduled for cleanup before
deployment.

## Final premium acceptance — 2026-08-17

Claude Opus reviewed the post-cleanup exact tree and returned **GO — repository RC accepted**. The
receipt is preserved at
`docs/reviews/cross-ai-ddd-final-accepted-2026-08-17/20260816T193941Z/claude-opus-5.md`.
It independently reproduced `just check`: 639 tests, CRAP maximum `29.89`, and every architecture,
complexity, typing, dead-code, and source-span gate green. It also verified that removal of the
obsolete diagnostic scheduler configuration did not remove the live bounded reporting/index
maintenance scheduler.

Remaining Low dispositions:

- The adapter-internal private registry callback is deliberately accepted for this RC and scheduled
  as the first post-UAT Kaizen candidate if a narrow capability protocol is simpler in practice.
- The expired outer timer edge on a failing nested publication deadline is deliberately accepted:
  production has no nested caller, and changing timer semantics before live UAT adds more risk than
  it removes.
- The pinned Pyright version notice is accepted for this RC and belongs in a separate dependency
  maintenance change.
- The word `scheduling` in the struck-through, explicitly retracted v3.0 history is retained as
  provenance, not as current configuration or capability.
- Ignored `.coverage`, `.pytest_cache`, and `__pycache__` artifacts are reproducible test residue,
  not release inputs. Automated removal was refused by the execution safety policy before any
  target changed; they remain scheduled for recoverable cleanup before deployment.

No live service, systemd topology, UPS command, state migration, or deployment was performed by the
panel or Cross-AI reviews. Cluster 12 remains the separate deployment and physical acceptance gate.
