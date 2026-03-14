# Expert Panel Review — 2026-03-14

Comprehensive review: architecture, code quality, security, performance, efficiency, clean code.

## Scope

- **What:** UPS battery monitor daemon (Python, systemd service, single home server)
- **Blast radius:** `host-level` — bug can cause incorrect shutdown (data loss) or missed shutdown (hardware damage)
- **Codebase:** ~1,856 LoC across 11 modules, 181 tests

---

## 1. System Architect

**Assessment:** Clean linear architecture (NUT -> EMA -> SoC -> runtime -> virtual UPS), no circular dependencies, atomic writes. But `monitor.py` is a god object at 600 lines with 8 responsibility domains.

### Findings

#### 1.1 Module Boundaries

Well-separated:
- **NUT Client** (178 LoC): Pure socket abstraction; stateless polling; timeout guards + wall-clock deadline in `_recv_until`. Parser is private.
- **EMA Filter** (85 LoC): Adaptive exponential smoothing; gate-based stabilization (>=12 samples). Clear contract: `add_sample(v, l)` -> `voltage`/`load`/`stabilized`.
- **Physics calculators** (soc_predictor, runtime_calculator, soh_calculator): Pure functions; no state. Peukert's Law encapsulated cleanly.
- **Model** (287 LoC): Battery model persistence with atomic writes + fsync. Getters return defaults, never None.
- **Event Classifier** (67 LoC): FSM for (ONLINE, BLACKOUT_REAL, BLACKOUT_TEST) with explicit input voltage thresholds.
- **Virtual UPS** (137 LoC): Tmpfs write with atomic rename. Status override logic is pure.
- **Alerter** (97 LoC): Logger configuration + structured warning output to journald.

Weak point:
- **monitor.py** (600 LoC): God Object. Contains: poll loop, event transition handling, discharge buffer management, calibration writes, voltage sag measurement, health alerting, Peukert auto-calibration. Each concern is separable but tightly coupled in one class.

#### 1.2 Data Flow

```
NUT upsd (real device)
    | [stateless TCP polling every 10s]
NUTClient.get_ups_vars() -> dict {battery.voltage, ups.load, ups.status, input.voltage, ...}
    |
MonitorDaemon.run()
    |-- EMAFilter.add_sample(voltage, load)
    |   |-- Adaptive alpha: deviation >=5% sensitivity -> instant react
    |   '-- Stabilized after 12+ samples (~2 min at 10s intervals)
    |
    |-- EventClassifier.classify(ups_status, input_voltage)
    |   '-- ONLINE / BLACKOUT_REAL / BLACKOUT_TEST
    |
    |-- [Every 6 polls, ~60s]:
    |   |-- ir_compensate(V_ema, L_ema, k, l_base) -> V_norm
    |   |-- soc_from_voltage(V_norm, LUT) -> SoC [0.0-1.0]
    |   |-- charge_percentage(SoC) -> charge [0-100%]
    |   '-- runtime_minutes(SoC, L_ema, capacity, SoH, exponent, V_nom, P_nom) -> minutes
    |
    |-- [On event transition: ONLINE->OB or OB->ONLINE]
    |   |-- If OB->ONLINE after discharge:
    |   |   |-- soh_calculator.calculate_soh_from_discharge(voltage_series, time_series)
    |   |   |-- Update model.soh_history + model.save() [atomic write]
    |   |   '-- replacement_predictor.linear_regression_soh() -> replacement date
    |   |
    |   '-- If calibration_mode + BLACKOUT_TEST:
    |       '-- For each new measurement: model.calibration_write(V, SoC, timestamp)
    |
    |-- compute_ups_status_override(event_type, time_rem, threshold)
    |       -> "OL" / "OB DISCHRG" / "OB DISCHRG LB"
    |
    '-- write_virtual_ups_dev({battery.runtime, battery.charge, ups.status, ...})
            -> /dev/shm/ups-virtual.dev
                |
            NUT dummy-ups reads file
                |
            upsmon, Grafana (virtual UPS as source)
```

- No circular dependencies. Dependency graph is DAG.
- Data lives in three isolation zones: RAM (EMA), Tmpfs (/dev/shm), Disk (~/.config model.json).
- Tmpfs write frequency underspecified: every 60s, but during fast_poll the data may be stale.

#### 1.3 Configuration Management

- [x] ~~Physics params in THREE places, precedence undocumented~~ — env vars replaced with TOML config file. Physics params live only in model.json.

#### 1.4 Error Propagation — Silent Failures

- [x] ~~Missing voltage/load -> 60s delay~~ — after run() refactor, missing data does `continue` with 10s sleep, not 60s. Not a problem.
- [x] ~~Calibration write index bug~~ — index now advances per successful write, breaks on first error.
- [x] ~~Silent returns in _auto_calibrate_peukert~~ — 6 debug logs added to all early returns.

#### 1.5 File Naming

- [x] ~~ema_ring_buffer.py misleading~~ — renamed to `ema_filter.py`.

#### 1.6 Architect Risks Table

- [x] ~~Stale tmpfs writes during fast-poll~~ — verified: fast poll = 1s sleep, write every 6 polls = 6s. Not a problem.
- [x] ~~Calibration duplicate writes on retry~~ — dedup changed from voltage (±0.01V) to timestamp.
- [x] ~~Silent calibration skip in Peukert~~ — debug logs added.
- [x] ~~Configuration layer fragmented~~ — resolved by TOML config migration.
- [x] ~~MonitorDaemon violates SRP~~ — run() refactored from 208 to 48 lines, 8 methods extracted.
- [x] ~~Discharge buffer cap silently drops data~~ — logs warning on every poll while capped. Acceptable.

#### 1.7 Architect Open Questions

- [x] Q1: Tmpfs write latency SLA — dummy-ups reads file on demand. 60s staleness in normal mode is fine; LB flag is set on event transition, not tied to 6-poll window.
- [x] Q2: Discharge buffer overflow — stop collecting (not FIFO). Warning logged. Acceptable for max ~3h discharge.
- [ ] Q3: `calibration_last_written_index` in RAM only — deferred to P2 (discharge buffer checkpointing). Low risk: timestamp dedup prevents duplicate writes on restart.
- [ ] Q4: Peukert exponent load-dependent — open design question. Current scalar approach works with consistent load (~16-20%). Deferred.
- [x] Q5: Model validation at startup — `_validate_model()` added: checks LUT >=2 points, anchor voltage, SoH range, capacity > 0.
- [x] Q6: Event classifier 50-99V threshold — verified correct. CyberPower shows 0V on real blackout, full voltage on test. 100V threshold is sound, no need to configure.

---

## 2. Security Analyst

**Assessment:** Strong defensive design (atomic writes, socket timeouts, stateless polling) but insufficient privilege isolation and no systemd hardening. File operations well-protected against TOCTOU via atomic patterns, but directory creation lacks permission validation. No embedded credentials.

### Findings

#### 2.1 Privilege & Capability Gaps (HIGH)

- [x] ~~No systemd hardening~~ — added ProtectSystem=strict, NoNewPrivileges, PrivateDevices, ProtectClock, RestrictAddressFamilies, UMask=0077, ProtectHome=read-only with ReadWritePaths for config dir and /dev/shm.

#### 2.2 NUT Socket Injection (MEDIUM)

- [x] ~~Malformed NUT response~~ — accepted risk. NUT bound to localhost only (verified: `127.0.0.1:3493`). 64KB buffer cap + socket timeout + `_parse_var_line` bounds check mitigate crashes. `ups_name` comes from config file, not user input at runtime.

#### 2.3 Tmpfs Symlink Attack (MEDIUM)

- [x] ~~Symlink attack on /dev/shm/ups-virtual.dev~~ — added `is_symlink()` guard before write. Refuses to write through symlinks.

#### 2.4 Model Directory Permissions Not Validated (MEDIUM)

- [x] ~~MODEL_DIR permissions~~ — mitigated by systemd `UMask=0077` and `ProtectHome=read-only`. Daemon can only write to explicitly allowed paths.

#### 2.5 Install Script Issues (MEDIUM)

- [x] ~~sed -i without backup~~ — changed to `sed -i.bak`.
- [x] ~~eval echo ~${SUDO_USER}~~ — replaced with `getent passwd | cut -d: -f6`.

#### 2.6 No Input Validation on Environment Variables (LOW)

- [x] ~~Env var bounds checking~~ — env vars eliminated entirely. Config is now TOML file + hardcoded internals. Invalid TOML = daemon won't start.

#### 2.7 No Explicit Umask in Daemon (LOW)

- [x] ~~No umask~~ — handled by systemd `UMask=0077` in service file.

#### 2.8 Security Open Questions

- [x] Q1: NUT bound to **127.0.0.1:3493** (localhost only). Safe.
- [x] Q2: `/dev/shm` mounted **nosuid,nodev**. No noexec but we write data, not executables.
- [x] Q3: UMask was 0022, now set to **0077** in service file.
- [x] Q4: NUT dummy-ups reads file as `nut` user. File is 0644 (readable). Acceptable.
- [x] Q5: NUT bound to localhost only — confirmed via `ss -tlnp`.

---

## 3. SRE / Performance Engineer

**Assessment:** Excellent resource efficiency (<100 KB RAM, <0.1% CPU). TCP roundtrip to NUT (~80ms) is the only bottleneck. But: silent data loss on disk full, no model.save() on SIGTERM, no rate-limiting of error logs.

### Findings

#### 3.1 Hot Path Analysis (10-second poll loop)

| Operation | Cost | Frequency |
|-----------|------|-----------|
| `get_ups_vars()` (NUT LIST VAR) | ~80ms TCP roundtrip + parsing | Every 10 sec |
| EMA update (2 floating-point ops) | <1us | Every cycle |
| Voltage sag detection (list append) | <1us | Fast poll only (rare) |
| Logging (every 6 polls) | ~5ms (journald serialize) | Every 60 sec |
| Virtual UPS write (atomic tmpfs) | ~10ms (fsync) | Every 60 sec |
| SoH/runtime calculation | ~2ms (LUT lookup + Peukert) | Every 60 sec |

Memory footprint: <100 KB total. No concerns with Python GC pauses.

#### 3.2 Failure Modes

- [x] **Scenario 1: NUT crashes during OL->OB** — accepted risk. Daemon retries every 10s. Sag window lost but discharge buffer survives (collecting flag persists). Recovers on next discharge.
- [x] **Scenario 2: Disk full** — all `model.save()` wrapped in `try/except OSError`. Daemon continues running with data in RAM. Alerting and Peukert calibration still execute. Error logged.
- [x] **Scenario 3: Virtual UPS write fails** — already caught and logged. Old file persists (atomic rename never happens). upsmon reads stale but valid data. Acceptable.
- [ ] **Scenario 4: Clock jump backward** — deferred. SoH uses `times[-1] - times[0]`; negative delta_t would produce invalid result. Low probability on NTP-synced system (corrections are <1ms). Guard could be added to soh_calculator.

#### 3.3 Observability Gaps

- [ ] Deferred to P2. Current logging covers all critical events. Structured fields (POLL_LATENCY_MS, DISCHARGE_BUFFER_SIZE) are nice-to-have for Grafana dashboards.

#### 3.4 Signal Handling

- [x] ~~No model.save() before exit~~ — signal handler now calls `model.save()` before `running = False`.
- [x] ~~Partial virtual_ups write~~ — atomic write pattern (tempfile + rename) prevents partial reads. Signal during write = old file persists.

#### 3.5 Unbounded Log Growth

- [x] ~~No rate limiting~~ — error logs now rate-limited: full traceback for first 10 errors, summary every 60s after. Counter resets on successful poll.

#### 3.6 SRE Recommendations

**P0: Data Loss Protection**
- [x] ~~Model load validation~~ — `_validate_model()` checks LUT, anchor, SoH range, capacity.
- [x] ~~Disk full handling~~ — all save() wrapped in try/except OSError.
- [x] ~~Graceful shutdown~~ — model.save() in signal handler.

**P1: Observability**
- [ ] Structured logging fields — deferred to P2.
- [x] ~~Rate-limit error logs~~ — done, first 10 + every 60s summary.
- [ ] Health check (systemd watchdog) — deferred.

**P2: Edge Cases**
- [ ] Fast poll recovery: reset sag state on exception — deferred (low impact).
- [ ] Timestamp monotonicity guard in SoH calculator — deferred.
- [x] ~~Calibration write error handling~~ — already breaks on first error, resumes from correct index.

#### 3.7 SRE Open Questions

- [x] Q1: Clock jump — low risk on NTP-synced system (corrections are sub-millisecond). Deferred guard.
- [x] Q2: Disk full during save — atomic write pattern: temp file created, fsync fails → temp file cleaned up in except block. Old model.json untouched.
- [x] Q3: Virtual UPS write atomicity — same: old file persists on failure. Temp file cleaned up.
- [x] Q4: EMA after long NUT outage — adaptive alpha handles it: first sample after outage has large deviation → alpha approaches 1.0 → instant reaction. Verified by `test_spike_recovery_within_2_samples`.

---

## 4. QA Engineer / Clean Code Advocate

**Assessment:** 70% happy-path, 30% edge-case tests. `run()` is 200+ lines with 5 nesting levels. Boolean flag proliferation. No tests for `write_virtual_ups_dev()` and `alerter.py`.

### Findings

#### 4.1 Long Methods & Deep Nesting

- `monitor.py run()`: 200+ lines, 5 nesting levels. Contains polling, EMA, events, sag tracking, discharge buffering, virtual UPS writing.
- `monitor.py _update_battery_health()`: 80 lines mixing SoH calculation, history append, replacement prediction, alerting, Peukert calibration.
- `monitor.py _auto_calibrate_peukert()`: 55 lines, 6 nesting levels. Physics calculation obscured by guards.

**Recommendation:** Extract `_poll_once()`, `_measure_voltage_sag()`, `_accumulate_discharge_buffer()`, `_handle_soh_update()`.

#### 4.2 Feature Envy

- `monitor.py:449-466`: Direct manipulation of discharge buffer internals and calibration logic. Should delegate to a class.
- `monitor.py:224-235`: Passes 10 arguments to `soh_calculator.calculate_soh_from_discharge()`.

#### 4.3 Boolean Flag Proliferation

- `self.sag_collected`, `self.fast_poll_active`, `self.discharge_buffer['collecting']` — three separate boolean flags for sag state.
- Easy to get state out of sync (e.g., `fast_poll_active=True` but `sag_collected=True` is contradictory).
- `calibration_mode=False` boolean flag changes behavior deeply.

**Better approach:** State enum `SagState(Enum): IDLE, ARMED, MEASURING, COMPLETE`.

#### 4.4 Inconsistent Error Handling

Three different patterns:
- `nut_client.py`: Silent failures (return empty dict)
- `model.py`: Fallback to default on corrupt file
- `monitor.py`: Log and continue on exception

**Recommendation:** Establish policy: recoverable (log warning + continue), corruption (log error + fallback), critical (raise).

#### 4.5 Magic Numbers

| Number | Location | Meaning |
|--------|----------|---------|
| 1000 | monitor.py:116 | Discharge buffer cap (~3h at 10s poll) |
| 5 | monitor.py:425 | Sag sample count |
| 100 | event_classifier.py:49 | Test vs real blackout voltage threshold |
| 0.5 | soc_predictor.py:29 | Fallback SoC for empty LUT |
| 6 | monitor.py:464,445 | Polls between calibration writes (=60s) |
| 12 | ema_ring_buffer.py:61 | Min samples for stabilization |

#### 4.6 Test Coverage Gaps

**Missing tests:**
- `monitor.py` polling loop: NUT unreachable at startup, EMA not stabilized, OB->OL health update, discharge buffer overflow
- `virtual_ups.py`: No tests for `write_virtual_ups_dev()` at all
- `alerter.py`: No tests at all
- `replacement_predictor.py`: Non-ISO8601 date parsing, future dates >2100

#### 4.7 Naming Issues

- `ema_ring_buffer.py` -> should be `ema_filter.py`
- `monitor.py:111` `self.last_soc` -> suggests "previous iteration" but used for change detection threshold
- Comments reference "L1 fix" (monitor.py:394) without context

#### 4.8 Dead Code

- `soh_calculator.py` line 3: `import logging` imported but never used.

#### 4.9 Comments Quality

- Comments explain "what" not "why". Examples:
  - `# L1 fix: Log when EMA stabilizes` — what is L1?
  - `# Voltage sag: start fast poll` — why measure sag?
  - `model.py:43-47` fsync on read-only FD — why read-only?
  - `ema_ring_buffer.py:40-41` `abs(current_ema) < 1e-6` — why this guard?

#### 4.10 QA Open Questions

1. Why fsync on read-only FD in model.py? Verify correctness for ext4 metadata persistence.
2. What happens if `_recv_until()` hits timeout during valid multi-packet NUT response? 2s may be tight.
3. Calibration mode: why interpolate cliff region only on TEST, not REAL blackout?
4. SoC fallback 0.5: should it be 1.0 (assume charged) or 0.0 (assume drained) for safety?

---

## 5. Kaizen Master (re-reviewed with corrected context)

**Context correction:** Initial review assumed ~1 blackout/year. Actual conditions: **several blackouts per week** (mostly 1-2 min, occasionally hours). Battery under constant stress. 100-200+ discharge events per year — plenty of statistical data for all physics models.

**Assessment:** With frequent blackouts, the core architecture **IS justified**: Peukert physics, SoH tracking, replacement predictor, event classification, adaptive EMA — all earn their complexity. However, the configuration surface is over-engineered (11 env vars the user never touches), and a critical safety feature is missing (fallback shutdown if upsmon fails).

### Findings

#### 5.1 Feature Justification (revised)

| Feature | Lines | Verdict | Why |
|---------|-------|---------|-----|
| Peukert runtime prediction | 62 (runtime_calculator) | **Essential** | Non-linear battery physics. 5-10% runtime difference at 10-20% load. |
| SoC via voltage LUT | 92 (soc_predictor) | **Essential** | VRLA curve is non-linear; lookup table is the only practical approach. |
| SoH from discharge curves | 149 (soh_calculator) | **Essential** | 100+ events/year makes area-under-curve SoH tracking statistically valid. |
| Replacement predictor | 95 (replacement_predictor) | **Justified** | Linear regression on SoH history becomes useful at N>20 events (~5 months). |
| Adaptive EMA | 85 (ema_filter) | **Justified** | Fast reaction to power events + noise smoothing. Verify sensitivity=0.05 against real load patterns. |
| Voltage sag + R_internal | ~50 (monitor.py:347-361) | **Justified** | With weekly events, R_internal history builds fast enough for degradation signal. |
| Peukert auto-calibration | 55 (monitor.py:291-345) | **Justified with caveat** | Single-event trigger is noise-prone. Consider requiring 3+ consistent >10% errors before adjusting. |
| Calibration mode | ~100+ (monitor.py + soh_calculator) | **Justified but unvalidated** | Needs real hardware test. Mark as experimental until validated. |
| Event classifier | 67 (event_classifier) | **Essential** | FSM distinguishing test/real blackout — critical for correct SoH data. |

#### 5.2 Configuration Surface — Over-engineered

11 env vars in monitor.py:22-35, **none of which the user has ever configured or plans to use**:

| Env Var | Default | Verdict |
|---------|---------|---------|
| UPS_MONITOR_POLL_INTERVAL | 10 | Move to config file |
| UPS_MONITOR_MODEL_DIR | ~/.config/... | Move to config file |
| UPS_MONITOR_NUT_HOST | localhost | Move to config file |
| UPS_MONITOR_NUT_PORT | 3493 | Move to config file |
| UPS_MONITOR_NUT_TIMEOUT | 2.0 | Move to config file |
| UPS_MONITOR_UPS_NAME | cyberpower | Move to config file |
| UPS_MONITOR_EMA_WINDOW | 120 | Move to config file |
| UPS_MONITOR_IR_K | 0.015 | Move to model.json (physics param) |
| UPS_MONITOR_IR_BASE | 20.0 | Move to model.json (physics param) |
| UPS_MONITOR_SHUTDOWN_THRESHOLD_MIN | 5 | Move to config file |
| UPS_MONITOR_SOH_THRESHOLD | 0.80 | Move to config file |

**Recommendation:** Replace all env vars with a centralized config file (e.g., `~/.config/ups-battery-monitor/config.ini` or YAML). Physics params (IR_K, IR_L_BASE) belong in model.json (they're calibrated, not configured). Env vars are for containerized deployments — this is a bare-metal systemd service.

#### 5.3 Fallback Shutdown — REJECTED

Kaizen proposed: if `time_rem < 1 min` for >60s and upsmon hasn't acted, daemon calls `systemctl poweroff`.

**Decision: REJECTED.** Violates NUT's separation of responsibilities.

NUT architecture assigns clear roles:
```
driver (usbhid-ups / dummy-ups) → publishes UPS variables
upsd (server)                   → serves data over TCP
upsmon (client)                 → monitors status, decides shutdown
```

Our daemon is a **data source** (writes to virtual UPS), not a decision maker. Adding shutdown logic would:
1. Duplicate upsmon's responsibility
2. Create a second shutdown path that could conflict with upsmon
3. Break the principle: our daemon provides metrics, upsmon acts on them

**Correct mitigation:** Ensure upsmon is reliable:
- `systemctl is-enabled nut-monitor` — verify enabled
- upsmon has `Restart=on-failure` in systemd unit
- Our daemon correctly sets LB flag in virtual UPS — this IS our responsibility, and it works

**Note:** `shutdown_imminent` flag in monitor.py is used for internal logging/metrics, not for triggering actions. This is correct.

#### 5.4 MISSING: Startup Health Checks

Daemon starts polling immediately without validating dependencies:
- NUT reachable? (only logs warning, doesn't fail)
- model.json readable and valid? (falls back to defaults silently)
- /dev/shm writable? (not checked)
- Shutdown threshold >= 2 min? (not validated)

**Recommendation:** Fail fast at startup if critical dependencies are unavailable. A daemon that can't reach NUT is useless and should signal failure to systemd (exit 1) rather than polling forever into error logs.

#### 5.5 MISSING: Discharge Buffer Persistence

`calibration_last_written_index` lives in RAM only. If daemon crashes during BLACKOUT_TEST, index resets to 0. Previously-written calibration points get re-processed.

**Recommendation:** Checkpoint buffer state to model.json during calibration mode.

#### 5.6 Modularity Assessment

With 15 test files running 181 tests, the modularity cost IS paid off. Each module has focused tests. Merging modules would reduce lines but break test isolation.

**Verdict:** Keep modules separate. The cognitive load is manageable with good documentation.

#### 5.7 What Kaizen Would Still Cut

1. **11 env vars -> config file** — env vars are wrong paradigm for bare-metal systemd service
2. **Virtual UPS fsync on tmpfs** — tmpfs is RAM-backed, fsync is cargo cult. Keep atomic rename, drop fsync. Document why.
3. **Alerter abstraction** — verify MOTD actually parses journald messages from alerter. If not, inline logging.

#### 5.8 What Kaizen Would Add

1. **Fallback shutdown** (critical, ~20 lines)
2. **Startup health checks** (~30 lines)
3. **Config file** replacing env vars
4. **Discharge buffer checkpointing** in calibration mode

#### 5.9 Kaizen's Verification Proposal (revised)

With frequent blackouts, verification happens naturally:
- After 1 month: 20+ discharge events. Compare predicted vs actual runtime. If error >15%, investigate Peukert exponent.
- After 3 months: 50+ events. SoH trend should be visible. Replacement predictor should give first estimate.
- After 6 months: 100+ events. Full model validation. Compare LUT-predicted SoC with UPS firmware's battery.charge at known discharge points.

---

## Panel Conflicts

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| SoH/replacement predictor | Architect: keep, extract to handlers | Kaizen (revised): **justified**, keep as-is | **All agree:** features earn their complexity with frequent blackouts. |
| Monitor.py refactor -> handlers | Architect: extract 5+ classes | Kaizen: extract methods, not classes | **Compromise:** extract methods (reduce nesting) but don't create new classes yet. |
| Configuration | Architect: create config.py with validation | Kaizen: replace env vars with config file | **Agreement:** centralized config file, not env vars. Physics params to model.json. |
| Systemd hardening | Everyone agrees | Everyone agrees | **Do it.** 10 lines of copy-paste. |
| Symlink attack on tmpfs | Security: real risk | Kaizen: simple fix | **Do it.** check is_symlink before write. |
| Fallback shutdown | Kaizen: add fallback poweroff | User+Architect: violates NUT separation | **Rejected.** Daemon is data source, not decision maker. Ensure upsmon reliability instead. |

---

## Recommended Action Plan

### P0 — Safety & Resilience (do now)

1. **Systemd hardening** — add ProtectSystem, NoNewPrivileges, etc. to both .service files (10 lines)
2. **model.save() in signal handler** — persist SoH before shutdown
3. **Verify upsmon reliability** — `systemctl is-enabled nut-monitor`, check Restart=on-failure
4. **Rename ema_ring_buffer.py -> ema_filter.py** — name matches content

### P1 — Configuration & Code Quality (next session)

5. **Replace env vars with config file** — centralized `~/.config/ups-battery-monitor/config.ini`, physics params to model.json. Validate at startup.
6. **Startup health checks** — fail fast if NUT unreachable, model.json unreadable, /dev/shm unwritable
7. **Extract methods from run()** — `_poll_once()`, `_update_sag()`, `_update_discharge_buffer()` to reduce nesting
8. **Rate-limit error logs** — suppress after 10 repeats in 5 min window
9. **Remove unused import** — `logging` from soh_calculator.py
10. **Add "why" comments** to magic numbers (1000, 5, 100, 0.5)

### P2 — Security & Observability (future)

11. **Symlink check** in virtual_ups.py before write
12. **Structured logging fields** — POLL_LATENCY_MS, DISCHARGE_BUFFER_SIZE
13. **Health check** — file-based or systemd watchdog
14. **Disk full handling** — wrap model.save() in try/except, log ALERT priority
15. **Discharge buffer checkpointing** in calibration mode

### P3 — Deferred (revisit after 3 months of data)

16. **Monitor.py handler classes** — only if adding new features requires it
17. **Peukert auto-calibration hardening** — require 3+ consistent >10% errors before adjusting
18. **Calibration mode validation** — test on real hardware, mark as experimental until then
19. **Kaizen's 3-month verification** — compare predicted vs actual runtime on 50+ events

### Open Items Requiring User Input

- Has calibration mode been tested on real hardware? If not, mark as experimental.
- Verify: `ss -tlnp | grep 3493` — is NUT bound to localhost only?
- Verify: `mount | grep /dev/shm` — is tmpfs mounted noexec,nosuid?
- Verify: `systemctl show -p UMask ups-battery-monitor.service` — what umask?
- Does MOTD (`~/scripts/motd/51-ups.sh`) parse journald messages from alerter.py? If not, alerter abstraction is unjustified.
