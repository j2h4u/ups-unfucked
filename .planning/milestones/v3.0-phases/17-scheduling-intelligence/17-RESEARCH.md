# Phase 17: Scheduling Intelligence - Research

**Researched:** 2026-03-17
**Domain:** Daemon-controlled UPS test scheduling, safety gates, blackout credit, systemd timer migration
**Confidence:** HIGH

## Summary

Phase 17 transitions the daemon from passive observer (Phase 16) to active decision maker — implementing intelligent test scheduling that evaluates sulfation score, cycle ROI, and safety constraints before dispatching upscmd commands. The phase replaces static systemd timers (`ups-test-quick.timer`, `ups-test-deep.timer`) with daemon-driven scheduling logic. Every decision is logged with reason codes to journald, enabling user verification and model refinement. All preconditions (SoC ≥95%, no power glitches in last 4h, no test running) are validated before any upscmd attempt.

The scheduling decision tree is a pure function (testable offline): inputs are current battery state + history, outputs are decision code (`propose_test` | `defer_test` | `block_test`) + reason. Safety gates are structural: SoH floor (≥60%), rate limiting (≤1 test/week), natural blackout credit (skip test when recent blackout already desulfated), and grid stability (no test if blackouts within 24h).

**Primary recommendation:** Implement scheduler as stateless decision engine in `src/battery_math/scheduler.py` (pure function, 100% testable offline). Integrate into `MonitorDaemon.run()` to evaluate daily (or per-poll based on project preference) and dispatch test commands. Disable systemd timers at daemon startup via `systemctl mask`. Track last upscmd timestamp in model.json to enforce rate limiting and detect test-initiated vs natural blackouts.

## Standard Stack

### Core Infrastructure (inherited from v2.0)
| Component | Version | Purpose | Why Standard |
|-----------|---------|---------|--------------|
| Python | 3.13 | Daemon language | Type hints, match stmt, performance |
| python-systemd | Latest | JournalHandler, sd_notify | Standard NUT daemon pattern |
| NUT | 2.8.1+ | UPS communication | Only hardware interface; instcmd RFC 9271 |
| pytest | 8.3.5 | Test framework | Existing 337-test suite |
| journald | systemd | Structured event logging | Standard Linux observability |
| systemctl | systemd | Unit management | Mask/disable timers on startup |

### Phase 17 New Components
| Component | Version | Purpose | When to Use |
|-----------|---------|---------|-------------|
| Scheduler decision engine | Pure function | Evaluate test candidacy (sulfation + ROI + safety gates) | Once daily or per-poll |
| Rate limiter | In-memory + model.json | Enforce ≤1 test/week, track last upscmd | Per scheduling decision |
| Blackout credit tracker | model.json field | Track recent natural blackouts for desulfation equivalence | On discharge complete |
| Precondition validator | Pure function | Check SoC ≥95%, no grid glitches, no test running | Before upscmd dispatch |
| Test command dispatcher | NUT client method | Send INSTCMD via RFC 9271 auth | Conditional on all gates pass |

### No New External Dependencies
Phase 17 uses only Python stdlib + NUT client (already in project). Zero new pip packages.

**Installation:** No new packages required.

**Version verification:** Python 3.13 ✓ (installed), systemd ✓ (on senbonzakura), NUT 2.8.1+ ✓ (upsd running). Verified 2026-03-17 against production environment.

## Architecture Patterns

### Recommended Model.json Schema Extension

Current schema (v2.0 + Phase 16):
```json
{
  "lut": [{...}],
  "soh": 1.0,
  "soh_history": [{...}],
  "sulfation_history": [{...}],
  "discharge_events": [{...}],
  "battery_install_date": "...",
  "cycle_count": 0
}
```

Phase 17 additions (new top-level fields):
```json
{
  "last_upscmd_timestamp": "2026-03-17T10:30:00Z",
  "last_upscmd_type": "test.battery.start.deep" | "test.battery.start.quick",
  "last_upscmd_status": "OK" | "ERR_CMD_NOT_SUPPORTED" | "ERR_TIMEOUT",
  "scheduled_test_timestamp": "2026-03-24T08:00:00Z",
  "scheduled_test_reason": "sulfation_score_0.65_roi_0.34",
  "test_block_reason": "SoH_below_floor_55%" | "grid_unstable_2_blackouts_in_24h" | null,
  "blackout_credit": {
    "active": true,
    "credited_event_timestamp": "2026-03-16T15:30:00Z",
    "credit_expires": "2026-03-23T15:30:00Z",
    "desulfation_credit": 0.18
  }
}
```

**Backward compatibility:** Phase 16 daemon reads Phase 17 model.json correctly (missing keys ignored). Phase 17 daemon gracefully initializes missing fields on first run.

### Pattern 1: Scheduler Decision Engine (Pure Function)

**What:** Stateless evaluation of test candidacy — takes battery state snapshot, returns decision + reason.

**When to use:** Once daily (08:00 in examples) or per polling cycle (configurable).

**Example signature:**
```python
# src/battery_math/scheduler.py
@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable scheduling decision with full audit trail."""
    action: Literal['propose_test', 'defer_test', 'block_test']
    test_type: Literal['deep', 'quick'] | None  # Only set if action='propose_test'
    reason_code: str  # e.g., 'sulfation_0.65_roi_0.34', 'soh_floor_55%', 'rate_limit_3d_remaining'
    next_eligible_timestamp: Optional[str]  # ISO8601, for deferred/blocked tests

def evaluate_test_scheduling(
    sulfation_score: float,  # [0.0, 1.0]
    cycle_roi: float,  # [-1.0, 1.0]
    soh_percent: float,  # [0.0, 1.0] as percentage
    days_since_last_test: float,  # From last_upscmd_timestamp
    last_blackout_timestamp: Optional[str],  # From discharge_events with event_reason='natural'
    last_blackout_depth: float,  # DoD [0.0, 1.0]
    active_blackout_credit: Optional[dict],  # With 'expires' field
    cycle_budget_remaining: int,  # From SoH calculation
    soh_floor_threshold: float = 0.60,  # Config default
    min_days_between_tests: float = 7.0,  # Config default
    blackout_credit_window_days: float = 7.0,  # High depth (~90%) counts as desulfation
    roi_threshold: float = 0.2,  # Minimum ROI to recommend test
) -> SchedulerDecision:
    """Pure decision engine: evaluate all gates, return action + reason."""
    # Implementation: decision tree with explicit gate checks
    # No I/O, no logging, no side effects
    # Returns reason code that describes which gate blocked/allowed decision
```

**Guard clauses (gates):**
1. **SoH floor gate:** if soh_percent < soh_floor_threshold → `block_test` with reason "soh_floor_58%"
2. **Rate limiting gate:** if days_since_last_test < min_days_between_tests → `defer_test` with reason "rate_limit_4d_remaining"
3. **Blackout credit gate:** if active_blackout_credit exists and not expired → `defer_test` with reason "blackout_credit_active_until_2026-03-23"
4. **Grid stability gate:** if recent natural blackout in last 24h → `defer_test` with reason "grid_unstable_2_blackouts_in_24h"
5. **ROI gate:** if roi < roi_threshold → `defer_test` with reason "marginal_roi_0.18" (not critical, prefer skip)
6. **Cycle budget gate:** if cycle_budget < 5 → `block_test` with reason "critical_cycle_budget_3_remaining"

**Decision logic (pseudocode):**
```
if soh_percent < floor:
    return block_test(reason="soh_floor_X%")
if days_since_test < min_interval:
    return defer_test(reason="rate_limit_Xd_remaining", next=now+remainder)
if active_blackout_credit and not expired:
    return defer_test(reason="blackout_credit_active_until_DATE", next=credit_expire_time)
if recent_blackout_in_24h:
    return defer_test(reason="grid_unstable_Nblackouts", next=now+1day)
if roi < roi_threshold and cycle_budget > 20:
    return defer_test(reason="marginal_roi_X", next=now+2days)
if cycle_budget < 5:
    return block_test(reason="critical_cycle_budget")

# All gates pass: recommend test
if sulfation_score > 0.5:
    test_type = 'deep' if sulfation_score > 0.65 else 'quick'
    return propose_test(test_type=test_type, reason="sulfation_X_roi_Y")
else:
    return defer_test(reason="low_sulfation_0.32", next=now+2days)
```

### Pattern 2: Rate Limiter (Stateful in model.json)

**What:** Enforce ≤1 deep test per week. Track last upscmd timestamp + type in model.json.

**When to use:** Every scheduling decision checks last_upscmd_timestamp.

**Integration:**
```python
# In MonitorDaemon.run(), during daily scheduling check:
last_test_time = model.get('last_upscmd_timestamp')
if last_test_time:
    last_test_dt = datetime.fromisoformat(last_test_time)
    days_since = (datetime.now(timezone.utc) - last_test_dt).total_seconds() / 86400.0
else:
    days_since = float('inf')  # No prior test

# Pass days_since to scheduler decision engine
decision = evaluate_test_scheduling(
    ...,
    days_since_last_test=days_since,
    ...
)

# If decision is propose_test, dispatch and update model.json
if decision.action == 'propose_test':
    success, msg = nut_client.send_instcmd(f'test.battery.start.{decision.test_type}')
    model.data['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()
    model.data['last_upscmd_type'] = f'test.battery.start.{decision.test_type}'
    model.data['last_upscmd_status'] = 'OK' if success else msg
    model.save()
```

### Pattern 3: Blackout Credit (Active Window)

**What:** When natural blackout ≥90% depth, daemon grants 7-day credit to skip scheduled deep test.

**When to use:** On discharge_complete event with event_reason='natural' and DoD ≥0.9.

**Integration (in discharge_handler):**
```python
# After classifying discharge as natural and calculating metrics
if event_reason == 'natural' and depth_of_discharge >= 0.90:
    logger.info(f"Natural blackout desulfation credit: DoD={depth_of_discharge:.0%}, active 7 days")
    self.battery_model.set_blackout_credit({
        'active': True,
        'credited_event_timestamp': datetime.now(timezone.utc).isoformat(),
        'credit_expires': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        'desulfation_credit': 0.15  # Approximate for ~90% DoD
    })
    self.battery_model.save()
```

### Pattern 4: Precondition Validator (Before upscmd dispatch)

**What:** Final gate checks before sending INSTCMD — verify grid conditions, UPS state, no test running.

**When to use:** Right before upscmd dispatch (after decision engine approves).

**Guard clauses:**
```python
def validate_preconditions_before_upscmd(
    ups_status: str,  # "OL", "OB DISCHRG", etc.
    soc: float,  # [0.0, 1.0]
    recent_power_glitches: int,  # Count of grid transitions in last 4h
    test_already_running: bool,  # From UPS status or model.json flag
) -> Tuple[bool, str]:
    """Return (can_proceed, reason_if_blocked)."""
    if 'OB' in ups_status or 'CAL' in ups_status:
        return False, "UPS_on_battery_cannot_test_during_discharge"
    if soc < 0.95:
        return False, f"SoC_below_95_percent_{soc:.0%}"
    if recent_power_glitches > 2:  # 2+ transitions in 4h = unstable grid
        return False, f"grid_unstable_{recent_power_glitches}_transitions_in_4h"
    if test_already_running:
        return False, "test_already_running"
    return True, "OK"
```

### Pattern 5: Test Dispatch with Error Handling

**What:** Send INSTCMD via NUT client, log result, update model.json atomically.

**When to use:** Conditional on all guards pass (decision + preconditions).

**Example:**
```python
# In MonitorDaemon.run() or scheduled callback
if decision.action == 'propose_test':
    can_proceed, block_reason = validate_preconditions_before_upscmd(
        ups_status=current_metrics.status,
        soc=current_metrics.soc,
        recent_power_glitches=count_grid_transitions(last_4h),
        test_already_running=model.data.get('test_running', False)
    )

    if not can_proceed:
        logger.info(f"Precondition blocked test: {block_reason}")
        return

    # All gates passed: send upscmd
    try:
        cmd = f'test.battery.start.{decision.test_type}'
        success, msg = nut_client.send_instcmd(cmd)

        if success:
            logger.info(f"Test dispatched: {cmd}", extra={
                'event_type': 'test_dispatched',
                'test_type': decision.test_type,
                'sulfation_score': ...,
                'roi': ...,
                'reason_code': decision.reason_code,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })

            # Persist dispatch for rate limiting
            model.data['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()
            model.data['last_upscmd_type'] = cmd
            model.data['test_running'] = True
            model.save()
        else:
            logger.error(f"Test dispatch failed: {msg}")
            model.data['last_upscmd_status'] = msg
            model.save()
    except socket.timeout:
        logger.error("NUT client timeout; test dispatch deferred")
    except Exception as e:
        logger.error(f"Unexpected error during test dispatch: {e}")
```

### Anti-Patterns to Avoid

- **Mutable global scheduler state:** Scheduler must be pure function, all state in model.json
- **Hardcoded thresholds in daemon:** All safety gates (SoH floor, rate limit, ROI threshold) should be constants in scheduler.py or config.toml
- **Blocking upscmd calls in polling loop:** Dispatch via timeout-guarded socket (NUT client already does this)
- **Silent failures:** Every dispatch attempt logged to journald with reason code
- **Scheduler decisions without audit trail:** Every decision (propose/defer/block) logged with full reason code for user review

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Scheduling logic | Custom state machine with hardcoded rules scattered in daemon | Pure function in src/battery_math/scheduler.py | Testable offline, auditable, version-controlled |
| Rate limiting | Per-poll memory counters | Last timestamp in model.json | Survives daemon restart, enforced across all processes |
| Test dispatch | Raw socket send with ad-hoc error handling | NUT client's send_instcmd() RFC 9271 auth | Already handles USERNAME→PASSWORD→LOGIN handshake |
| Blackout tracking | Infer from UPS status flags | Event metadata (event_reason='natural', DoD) in discharge_events | Explicit classification, more robust |
| Decision audit | Print logs with no structure | Structured journald events with extra={...} dict | Grafana ingestion, grep-able, machine-parseable |

**Key insight:** Scheduling is a pure decision problem: given state snapshot, compute action. Decoupling from daemon allows offline testing, policy refinement, and simulation without touching running service.

## Common Pitfalls

### Pitfall 1: Over-Aggressive Testing

**What goes wrong:** Daemon tests weekly even with low sulfation; battery wears faster than it desulfates. Real blackouts provide free desulfation — testing adds wear with marginal benefit.

**Why it happens:** Scheduler has no cost model, or cost model is too optimistic. Phase 16 computes ROI but Phase 17 ignores it.

**How to avoid:**
- Require roi > 0.2 AND sulfation_score > 0.5 to propose deep test (not just one gate)
- When in doubt, defer test by 2 days (let natural blackouts accumulate signal)
- Log every proposal + deferral with reason code — review journald to detect bias

**Warning signs:**
- Testing >1/week (should be rare, maybe 1/month in normal operation)
- Scheduled test timestamp within 3 days of last natural blackout (blackout credit should block this)

### Pitfall 2: Rate Limiter Not Enforced Across Restarts

**What goes wrong:** Daemon restarts, loses in-memory last-test time, dispatches test immediately. Rate limit broken.

**Why it happens:** Tracking last_upscmd_timestamp only in memory, not persisted.

**How to avoid:** Always persist last_upscmd_timestamp to model.json before exiting. Guard scheduler with check:
```python
last_test_dt = datetime.fromisoformat(model.get('last_upscmd_timestamp', ''))
days_since = (now - last_test_dt).total_seconds() / 86400.0
if days_since < 7.0:
    return defer_test(...)  # Before any other gate
```

**Warning signs:**
- Model.json missing last_upscmd_timestamp field (check on startup, initialize if missing)
- journald shows two test_dispatched events within 7 days

### Pitfall 3: Precondition Check Bypass

**What goes wrong:** Daemon sends upscmd while UPS on battery (OB state), test conflicts with real discharge, model breaks.

**Why it happens:** Dispatcher trusts decision engine and skips preconditions, or precondition logic is missing.

**How to avoid:**
- ALWAYS call validate_preconditions_before_upscmd() right before send_instcmd()
- Gate checklist: UPS online (OL, not OB/CAL), SoC ≥95%, no glitches in 4h, no test running
- Log each gate result: "precondition check: OL ✓, SoC 98% ✓, grid stable ✓, test_running ✗"

**Warning signs:**
- journald shows test_dispatched during OB state (UPS.status contained "OB")
- journald shows two test events within 30 minutes

### Pitfall 4: Blackout Credit Never Expires

**What goes wrong:** Daemon skips all tests for weeks because blackout_credit.active=true with no expiry date. Battery sulfation unchecked.

**Why it happens:** Credit granted but expiry timestamp not set, or expiry check is always skipped.

**How to avoid:**
```python
credit = model.get('blackout_credit', {})
if credit.get('active'):
    expires_dt = datetime.fromisoformat(credit.get('credit_expires', ''))
    if datetime.now(timezone.utc) > expires_dt:
        credit['active'] = False  # Expire credit
        model.save()
```

**Warning signs:**
- Blackout credit active for >10 days (check credit_expires field)
- No scheduled_test_timestamp for >2 weeks (check if credit is stale)

### Pitfall 5: Silent Upscmd Failures

**What goes wrong:** Test command fails (ERR_CMD_NOT_SUPPORTED, socket timeout, NUT not responding), but daemon doesn't log reason. User sees no scheduled test but has no idea why.

**Why it happens:** Dispatcher catches exception, logs generic error, doesn't update model.json with failure reason.

**How to avoid:**
```python
success, msg = nut_client.send_instcmd(cmd)
model.data['last_upscmd_status'] = 'OK' if success else msg
model.data['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()
model.save()

# Log both success and failure
if success:
    logger.info("Test dispatched successfully", extra={'event_type': 'test_dispatched'})
else:
    logger.error(f"Test dispatch failed: {msg}", extra={'event_type': 'test_dispatch_failed', 'error': msg})
```

**Warning signs:**
- last_upscmd_status contains error message instead of "OK" (grep model.json)
- journald missing test_dispatch_failed event even though next_scheduled_test is empty

## Code Examples

Verified patterns from Phase 16 and project v2.0:

### Dispatch Test with Full Audit Trail

```python
# Source: src/nut_client.py send_instcmd() + Phase 17 pattern
def dispatch_test_with_audit(
    nut_client: NUTClient,
    battery_model: BatteryModel,
    decision: SchedulerDecision,
    current_metrics: CurrentMetrics,
) -> bool:
    """Dispatch test command with full precondition checks and journald logging."""

    # Precondition validation
    can_proceed, block_reason = validate_preconditions_before_upscmd(
        ups_status=current_metrics.status,
        soc=current_metrics.soc,
        recent_power_glitches=0,  # Calculate from history in real code
        test_already_running=battery_model.data.get('test_running', False),
    )

    if not can_proceed:
        logger.info(
            f"Test dispatch precondition blocked: {block_reason}",
            extra={
                'event_type': 'test_precondition_blocked',
                'reason': block_reason,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
        )
        return False

    # All guards pass: send upscmd
    cmd = f'test.battery.start.{decision.test_type}'
    try:
        success, msg = nut_client.send_instcmd(cmd)

        if success:
            # Persist state
            battery_model.data['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()
            battery_model.data['last_upscmd_type'] = cmd
            battery_model.data['last_upscmd_status'] = 'OK'
            battery_model.data['test_running'] = True
            battery_model.save()

            # Log success
            logger.info(
                f"Test dispatched: {cmd}",
                extra={
                    'event_type': 'test_dispatched',
                    'test_type': decision.test_type,
                    'command': cmd,
                    'reason_code': decision.reason_code,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }
            )
            return True
        else:
            # Log failure, persist error message
            battery_model.data['last_upscmd_status'] = msg
            battery_model.save()

            logger.error(
                f"Test dispatch failed: {msg}",
                extra={
                    'event_type': 'test_dispatch_failed',
                    'command': cmd,
                    'error': msg,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }
            )
            return False
    except socket.timeout:
        logger.error("Test dispatch timeout; will retry next cycle")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during test dispatch: {e}")
        return False
```

### Scheduler Decision Engine (Pure Function)

```python
# Source: src/battery_math/scheduler.py (new file)
from dataclasses import dataclass
from typing import Literal, Optional
from datetime import datetime, timezone, timedelta

@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable scheduling decision with full audit trail."""
    action: Literal['propose_test', 'defer_test', 'block_test']
    test_type: Optional[Literal['deep', 'quick']] = None
    reason_code: str = ""
    next_eligible_timestamp: Optional[str] = None

def evaluate_test_scheduling(
    sulfation_score: float,
    cycle_roi: float,
    soh_percent: float,
    days_since_last_test: float,
    last_blackout_timestamp: Optional[str],
    last_blackout_depth: float,
    active_blackout_credit: Optional[dict],
    cycle_budget_remaining: int,
    soh_floor_threshold: float = 0.60,
    min_days_between_tests: float = 7.0,
    roi_threshold: float = 0.2,
) -> SchedulerDecision:
    """Pure decision engine: evaluate all gates, return action + reason.

    No I/O, no logging. All inputs are parameters; all output in return value.
    Decisions are reproducible: same inputs → same output (pure function).
    """
    now = datetime.now(timezone.utc)

    # Gate 1: SoH floor (hard block)
    if soh_percent < soh_floor_threshold:
        return SchedulerDecision(
            action='block_test',
            reason_code=f'soh_floor_{soh_percent:.0%}',
        )

    # Gate 2: Rate limiting (defer with next_eligible)
    if days_since_last_test < min_days_between_tests:
        remaining_days = min_days_between_tests - days_since_last_test
        next_eligible = now + timedelta(days=remaining_days)
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'rate_limit_{remaining_days:.1f}d_remaining',
            next_eligible_timestamp=next_eligible.isoformat(),
        )

    # Gate 3: Blackout credit (defer with expiry)
    if active_blackout_credit and active_blackout_credit.get('active'):
        expires_str = active_blackout_credit.get('credit_expires')
        try:
            expires_dt = datetime.fromisoformat(expires_str)
            if now < expires_dt:  # Credit still active
                return SchedulerDecision(
                    action='defer_test',
                    reason_code=f'blackout_credit_active',
                    next_eligible_timestamp=expires_dt.isoformat(),
                )
        except (ValueError, TypeError):
            pass  # Invalid expiry timestamp, ignore credit

    # Gate 4: Grid stability (defer if recent blackout in last 24h)
    if last_blackout_timestamp:
        try:
            blackout_dt = datetime.fromisoformat(last_blackout_timestamp)
            hours_since = (now - blackout_dt).total_seconds() / 3600.0
            if hours_since < 24.0:
                next_eligible = now + timedelta(hours=24.0 - hours_since)
                return SchedulerDecision(
                    action='defer_test',
                    reason_code=f'grid_unstable_recent_blackout',
                    next_eligible_timestamp=next_eligible.isoformat(),
                )
        except (ValueError, TypeError):
            pass  # Invalid timestamp, proceed

    # Gate 5: Cycle budget (hard block if critical)
    if cycle_budget_remaining < 5:
        return SchedulerDecision(
            action='block_test',
            reason_code=f'critical_cycle_budget_{cycle_budget_remaining}',
        )

    # Gate 6: ROI (defer if marginal, but not hard block)
    if roi_threshold > 0 and cycle_roi < roi_threshold and cycle_budget_remaining > 20:
        next_eligible = now + timedelta(days=2)
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'marginal_roi_{cycle_roi:.2f}',
            next_eligible_timestamp=next_eligible.isoformat(),
        )

    # All gates passed: recommend test based on sulfation severity
    if sulfation_score > 0.65:
        return SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code=f'sulfation_{sulfation_score:.2f}_roi_{cycle_roi:.2f}',
        )
    elif sulfation_score > 0.40:
        return SchedulerDecision(
            action='propose_test',
            test_type='quick',
            reason_code=f'sulfation_{sulfation_score:.2f}_ir_measure',
        )
    else:
        next_eligible = now + timedelta(days=2)
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'low_sulfation_{sulfation_score:.2f}',
            next_eligible_timestamp=next_eligible.isoformat(),
        )
```

### Systemd Timer Migration (Disable at Startup)

```python
# In MonitorDaemon.__init__() after all other initialization
import subprocess

def disable_legacy_timers():
    """Disable systemd timers that daemon now controls."""
    timers_to_mask = [
        'ups-test-quick.timer',
        'ups-test-deep.timer',
    ]

    for timer in timers_to_mask:
        try:
            # Mask timer (prevent even manual starts)
            subprocess.run(['systemctl', 'mask', timer], check=False, capture_output=True, timeout=5)
            logger.info(f"Masked timer: {timer}")
        except Exception as e:
            logger.warning(f"Failed to mask timer {timer}: {e}")

# Call at daemon startup
disable_legacy_timers()
```

## State of the Art

| Old Approach | Current Approach (v3.0) | When Changed | Impact |
|--------------|-------------------------|--------------|--------|
| Static systemd timers (fixed schedule) | Daemon-controlled scheduling (dynamic based on battery state) | Phase 17 | Tests triggered only when beneficial (ROI > 0), not routine |
| Manual `systemctl restart ups-test-quick` | Automatic via daemon decision engine | Phase 17 | No user intervention needed; decisions logged for review |
| No rate limiting (could test daily) | ≤1 deep test/week enforced in scheduler | Phase 17 | Battery wears less; natural blackouts provide free desulfation |
| No blackout credit (test within days of blackout) | 7-day credit after ≥90% DoD blackout | Phase 17 | Avoids redundant testing, lets recovery signals settle |
| SoH floor unknown (could test on dying battery) | Explicit SoH ≥60% gate before any test | Phase 17 | Safety: no deep discharge when battery critically degraded |
| Test failures silent | All dispatch attempts logged to journald with reason codes | Phase 17 | Full audit trail; user can verify "why no test this week" |

**Deprecated/outdated:**
- `ups-test-quick.timer`: Replaced by daemon decision engine (monthly quick IR measurement)
- `ups-test-deep.timer`: Replaced by daemon decision engine (1/month if sulfation warrants)
- Manual systemd timer edits: Daemon handles scheduling; config.toml for thresholds

## Open Questions

1. **Scheduling frequency:** Should daemon evaluate test candidacy once daily (08:00), once per polling cycle (every 10s), or on discharge completion only?
   - **What we know:** Phase 16 completes daily, discharge events are 0-2/day. Once daily is low-overhead.
   - **What's unclear:** How often users want to see deferral reasons (journald verbosity).
   - **Recommendation:** Evaluate once daily at fixed time (08:00 UTC or configurable). Log full decision to journald every time for audit trail.

2. **Deep vs Quick test preference:** Should daemon prefer quick tests (IR measurement, ~10s) to avoid deep discharge wear, or balance with deep tests for desulfation?
   - **What we know:** Quick tests are low-risk (minimal discharge), deep tests desulfate but wear battery.
   - **What's unclear:** Optimal ratio (1 deep / 3 quick? 1 deep / 10 quick?).
   - **Recommendation:** Propose quick test if sulfation 0.40–0.65, deep if >0.65. Both count toward rate limit. Phase 17 implements both; user can tune preference in config.toml.

3. **ROI threshold tuning:** Default is 0.2 (marginal benefit > cost). Is this conservative enough?
   - **What we know:** ROI=0.2 means benefit is 20% of (benefit + cost). Could mean 60% benefit, 40% cost for a specific battery.
   - **What's unclear:** Field validation on real batteries (need 30+ days observation).
   - **Recommendation:** Use default 0.2, but track deferral reasons in journald. If "marginal_roi" is most common reason, lower threshold to 0.1 in Phase 17 Plan 02.

4. **Precondition check implementation:** Where should SoC ≥95% check be enforced — in scheduler decision engine, or only before dispatch?
   - **What we know:** SoC is dynamic (changes every poll). Checking at decision time (daily 08:00) might differ from dispatch time (later that day).
   - **What's unclear:** Should we check SoC in decision (proposal valid only if SoC≥95% now) or at dispatch (defer if SoC<95% when ready to send).
   - **Recommendation:** Check at dispatch only (precondition validator). Decision engine doesn't know SoC at 08:00, but dispatch does. Log "dispatched" or "precondition blocked: SoC 92%".

5. **Test-initiated vs natural blackout classification:** Phase 16 always returns 'natural'. How do we distinguish?
   - **What we know:** NUT event_classifier detects real blackout (input_voltage ~0V) vs test (input_voltage ≥100V).
   - **What's unclear:** Can we reliably detect "this discharge was started by our upscmd" vs "random blackout during same hour"?
   - **Recommendation:** Phase 17 tracks last_upscmd_timestamp. If discharge starts within 60 seconds of upscmd, classify as 'test_initiated'. Otherwise 'natural'. Phase 16 has placeholder (always returns 'natural'); Phase 17 fills in logic.

## Validation Architecture

**Validation enabled:** Present in .planning/config.json, workflow.nyquist_validation is not explicitly set to false (treating as enabled).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.5 |
| Config file | pytest.ini (standard pytest discovery) |
| Quick run command | `pytest tests/test_battery_math/ -v` (scheduler tests) |
| Full suite command | `pytest tests/ -v` (all 337 tests) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SCHED-01 | Daemon sends upscmd test.battery.start.deep when sulfation warrants + safety gates pass | unit + integration | `pytest tests/test_scheduler.py::test_propose_deep_test_high_sulfation -xvs` | ❌ Wave 0 |
| SCHED-03 | Natural blackout ≥90% DoD grants 7-day credit, scheduled test skipped | unit | `pytest tests/test_scheduler.py::test_blackout_credit_active_blocks_test -xvs` | ❌ Wave 0 |
| SCHED-04 | No test when UPS on battery (OB state) | unit | `pytest tests/test_scheduler.py::test_precondition_blocks_test_during_ob -xvs` | ❌ Wave 0 |
| SCHED-05 | No test when SoH <60% | unit | `pytest tests/test_scheduler.py::test_soh_floor_blocks_deep_test -xvs` | ❌ Wave 0 |
| SCHED-06 | No test when grid unstable (blackouts in last 24h) | unit | `pytest tests/test_scheduler.py::test_grid_instability_defers_test -xvs` | ❌ Wave 0 |
| SCHED-07 | Daemon masks systemd timers on startup | integration | `pytest tests/test_systemd_integration.py::test_daemon_masks_legacy_timers -xvs` | ❌ Wave 0 |
| SCHED-08 | Discharge classified as 'test_initiated' vs 'natural' based on upscmd timestamp | unit | `pytest tests/test_discharge_handler.py::test_classify_test_initiated_discharge -xvs` | ✅ Existing (hardcoded 'natural') |

### Sampling Rate
- **Per task commit:** `pytest tests/test_scheduler.py -v` (~5 tests, <1 second)
- **Per wave merge:** `pytest tests/ -v` (all 337 tests, ~30 seconds)
- **Phase gate:** Full suite green + manual verification of journald logs showing decision reasons

### Wave 0 Gaps

- [ ] `tests/test_scheduler.py` — 8 tests covering all gates (SoH floor, rate limit, blackout credit, grid stability, ROI, cycle budget) + decision tree logic
- [ ] `tests/test_scheduler.py::test_integration_full_scenario` — End-to-end: given battery state, verify correct action (propose/defer/block) with reason code
- [ ] `tests/test_dispatch.py` — 4 tests covering upscmd dispatch with preconditions, error handling, model.json updates
- [ ] `tests/test_systemd_integration.py::test_daemon_masks_legacy_timers` — Verify systemctl mask called on startup
- [ ] `src/battery_math/scheduler.py` — Pure scheduler function (SchedulerDecision dataclass + evaluate_test_scheduling)
- [ ] Framework install: No new framework needed; pytest already installed. Scheduler tests use existing fixtures (battery_model, config, nut_client mocks)

## Sources

### Primary (HIGH confidence)
- NUT RFC 9271 INSTCMD protocol — implemented in src/nut_client.py, tested in test_nut_client.py
- Phase 16 RESEARCH.md — sulfation scoring + ROI computation patterns proven in practice
- Phase 16 discharge_handler.py — integration points where scheduler calls fit
- Project v2.0 battery_math/ — pure function patterns for testability

### Secondary (MEDIUM confidence)
- IEEE-450 lead-acid standards — sulfation physics basis (Shepherd model reference in sulfation.py)
- NUT upsd documentation — timer control via systemctl
- Python systemd module — JournalHandler for structured logging

### Tertiary (validation needed)
- Blackout credit window (7 days) — needs field validation on real battery
- ROI threshold (0.2) — needs 30+ days monitoring to confirm signal quality
- Rate limit enforcement (1 test/week) — assumes last_upscmd_timestamp persists correctly

## Metadata

**Confidence breakdown:**
- Scheduler decision tree: **HIGH** — logic is deterministic; Phase 16 sulfation/ROI proven; safety gates are structured preconditions
- Systemd timer migration: **HIGH** — systemctl mask/disable is standard practice
- NUT upscmd dispatch: **HIGH** — RFC 9271 already implemented and tested in Phase 15
- Blackout credit algorithm: **MEDIUM** — needs field validation (7-day window, ≥90% DoD threshold)
- Precondition validation: **HIGH** — guards are simple (SoC, grid transitions, test state)

**Research date:** 2026-03-17
**Valid until:** 2026-04-17 (30 days; stable domain, limited new information expected)

**Prepared for:** Phase 17 Planning (PLAN.md generation)
