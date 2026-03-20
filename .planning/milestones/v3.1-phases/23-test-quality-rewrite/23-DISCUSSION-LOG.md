# Phase 23: Test Quality Rewrite - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 23-test-quality-rewrite
**Areas discussed:** Rewrite scope & strategy, Mock replacement approach, Test splitting granularity, DI pattern for virtual_ups
**Mode:** --auto (all decisions auto-selected from recommended defaults)

---

## Rewrite Scope & Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Incremental fix | Fix specific anti-patterns per TEST-01–09, preserve working tests | ✓ |
| Wholesale rewrite | Rewrite entire test files from scratch | |
| Hybrid | Rewrite worst files, incrementally fix others | |

**User's choice:** [auto] Incremental fix (recommended default)
**Notes:** 555 tests currently passing. Incremental approach ensures no batch breakage and maps cleanly to 9 discrete requirements.

---

## Mock Replacement Approach

| Option | Description | Selected |
|--------|-------------|----------|
| I/O boundary mocks only | Keep mocks for NUT/disk/systemd, replace sequence assertions with state checks | ✓ |
| Full mock elimination | Remove all mocks, use real objects everywhere | |
| Minimal changes | Only fix the specific assertions called out in requirements | |

**User's choice:** [auto] I/O boundary mocks only (recommended default)
**Notes:** Phases 19-21 extracted SagTracker, SchedulerManager, DischargeCollector specifically to enable real-collaborator testing. Using them fulfills TEST-05 naturally.

---

## Test Splitting Granularity

| Option | Description | Selected |
|--------|-------------|----------|
| Split multi-behavior only | Split tests verifying unrelated behaviors, keep related assertions together | ✓ |
| Aggressive split | One assertion per test function | |
| No splitting | Keep test structure, only fix assertion types | |

**User's choice:** [auto] Split multi-behavior only (recommended default)
**Notes:** One-assertion-per-test is excessive for this codebase. Related assertions (e.g., buffer state before and after transition) belong together.

---

## DI Pattern for Virtual UPS

| Option | Description | Selected |
|--------|-------------|----------|
| Optional output_path parameter | Add parameter to write_virtual_ups_dev(), default to production path | ✓ |
| Config-based injection | Add path to Config dataclass | |
| Environment variable | Read path from env var in tests | |

**User's choice:** [auto] Optional output_path parameter (recommended default)
**Notes:** Simplest DI approach. Config-based would couple virtual UPS path to daemon config. Env var would be fragile.

---

## Claude's Discretion

- Exact test function names after splits
- Whether to consolidate shared setup into new fixtures or keep inline
- Order of test functions within files
- Whether to add helper functions for common assertion patterns

## Deferred Ideas

None — discussion stayed within phase scope
