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

Physics params (IR_K, Peukert exponent) exist in THREE places:
1. `model.py` lines 125-131 (JSON defaults)
2. `monitor.py` lines 30-31 (env var fallbacks)
3. `BatteryModel.get_ir_k()` line 159 (nested defaults)

Order of precedence is undocumented. Risk: env var set but never consulted.

#### 1.4 Error Propagation — Silent Failures

- `monitor.py:386-389`: Missing voltage/load -> logged as warning, poll continues. If stabilized gate blocks for up to 60s, shutdown signal may be delayed.
- `monitor.py:450-461`: Calibration write exception -> logged, but index still incremented -> potential duplicate/gap writes.
- `monitor.py:298-301`: `_auto_calibrate_peukert()` returns silently if load=0 or times<2. No log. Caller unaware calibration was skipped.

#### 1.5 File Naming

`ema_ring_buffer.py` contains `EMAFilter` class and `ir_compensate()` function. Neither is a ring buffer. Misleading for search and onboarding.

**Recommendation:** Rename to `ema_filter.py` or `signal_filter.py`.

#### 1.6 Architect Risks Table

| Risk | Severity | Location | Mitigation |
|------|----------|----------|------------|
| Stale tmpfs writes during fast-poll | Medium | monitor.py:565 | Write every poll during fast-poll |
| Calibration duplicate writes on retry | Medium | monitor.py:459, model.py:247 | Use timestamp as dedup key |
| Silent calibration skip in Peukert autocalibrate | Low | monitor.py:310-312 | Add debug log at each early return |
| Configuration layer fragmented | Medium | monitor.py:30-31, model.py:125-131 | Document precedence |
| MonitorDaemon violates SRP | High | monitor.py:56-600 | Extract event handlers |
| Discharge buffer cap silently drops data | Medium | monitor.py:442-443 | Log periodically, consider hard timeout |

#### 1.7 Architect Open Questions

1. Tmpfs write latency SLA: dummy-ups reads every poll, metrics updated every 60s. Can stale data cause false LOW_BATTERY?
2. Discharge buffer overflow: at cap, should we FIFO evict or stop collecting?
3. `calibration_last_written_index` in RAM only — if daemon restarts during BLACKOUT_TEST, index resets.
4. Peukert exponent is scalar but load-dependent — how to combine multiple discharges at different loads?
5. Model initialization: no validation that LUT has >2 points before entering main loop.
6. Event classifier: >=100V = test, <100V = real. 50V-99V range? Configurable threshold?

---

## 2. Security Analyst

**Assessment:** Strong defensive design (atomic writes, socket timeouts, stateless polling) but insufficient privilege isolation and no systemd hardening. File operations well-protected against TOCTOU via atomic patterns, but directory creation lacks permission validation. No embedded credentials.

### Findings

#### 2.1 Privilege & Capability Gaps (HIGH)

- Daemon runs as `j2h4u:j2h4u` (ups-battery-monitor.service lines 10-11).
- No `ProtectSystem`, `ReadWritePaths`, `NoNewPrivileges` in systemd units.
- Should run with minimal privileges and explicitly deny what it doesn't need.

**Recommendation — add to both .service files:**
```ini
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/dev/shm/
NoNewPrivileges=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_UNIX
RestrictNamespaces=yes
SystemCallFilter=@system-service
UMask=0077
PrivateDevices=yes
ProtectKernelLogs=yes
ProtectClock=yes
```

#### 2.2 NUT Socket Injection (MEDIUM)

- `nut_client.py:51-70` (`_parse_var_line`) splits on whitespace and quotes.
- Malformed NUT response could cause crashes (mitigated by 64KB buffer cap).
- NUT is trusted (localhost:3493) but compromised NUT upsd could trigger issues.
- `ups_name` substituted into socket command without sanitization (nut_client.py:170).

#### 2.3 Tmpfs Symlink Attack (MEDIUM)

- `virtual_ups.py:48` — `mkdir(parents=True, exist_ok=True)` on `/dev/shm/`.
- `virtual_ups.py:79` — `tmp_path.replace(virtual_ups_path)` vulnerable to symlink attack.
- **Fix:** Validate that target is a regular file (not symlink) before writing, or use `O_CREAT | O_EXCL`.

#### 2.4 Model Directory Permissions Not Validated (MEDIUM)

- `monitor.py:77` — `MODEL_DIR.mkdir(parents=True, exist_ok=True)` without permission enforcement.
- If attacker pre-creates directory with world-writable permissions, daemon writes there without validation.
- **Fix:** Check ownership + permissions after mkdir.

#### 2.5 Install Script Issues (MEDIUM)

- `install.sh:190` — `sed -i` on `/etc/nut/upsmon.conf` without backup.
- `install.sh:201` — `eval echo ~${SUDO_USER}` — potential injection.
- No match count validation before/after sed.
- **Fix:** `sed -i.bak`, validate grep count, replace eval with direct path.

#### 2.6 No Input Validation on Environment Variables (LOW)

- `monitor.py:22-35` — `int()` cast without bounds.
- `POLL_INTERVAL=0` -> tight loop, `NUT_PORT=99999` -> invalid.
- **Fix:** Add min/max bounds check at startup.

#### 2.7 No Explicit Umask in Daemon (LOW)

- Daemon doesn't set `umask()` before creating files. Inherits parent process umask.
- **Fix:** `os.umask(0o077)` at startup.

#### 2.8 Security Open Questions

1. Can NUT socket be hijacked? Check `ls -l /run/nut/` permissions.
2. Is `/dev/shm` mounted `noexec,nosuid`? If not, tmpfs write could be exploited.
3. What umask does systemd enforce? `systemctl show -p UMask ups-battery-monitor.service`.
4. Does NUT dummy-ups verify file permissions before reading?
5. Is NUT bound to localhost only? `ss -tlnp | grep 3493`.

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

**Scenario 1: NUT crashes during OL->OB transition**
- Transition detected, next poll fails, sleeps 10 sec.
- `fast_poll_active` already False, voltage sag window lost.
- Discharge buffer may have stale state.
- SoH calculation uses incomplete data. Recovers on next discharge.
- Severity: Medium.

**Scenario 2: Disk full (model.json can't write)**
- `battery_model.save()` throws IOError.
- Exception propagates through `_handle_event_transition()`.
- Poll loop catches at line 570, logs, sleeps 10 sec.
- Discharge buffer data in RAM never persisted.
- On restart: all SoH history lost.
- Severity: HIGH (silent data loss).

**Scenario 3: Virtual UPS write fails (tmpfs full or permissions)**
- `write_virtual_ups_dev()` throws OSError.
- Caught, logged, polling continues.
- NUT dummy-ups reads stale data.
- upsmon doesn't receive LB flag, shutdown delayed.
- Severity: HIGH (shutdown signal loss).

**Scenario 4: Clock jump backward (NTP correction)**
- Discharge buffer timestamps become non-monotonic.
- SoH area calculation uses negative dt -> breaks.
- Severity: Medium (unhandled edge case).

#### 3.3 Observability Gaps

**Visible from logs:**
- Daemon startup config, EMA stabilization, event transitions, SoC/runtime (every 60s), voltage sag, SoH calculations, Peukert calibration, errors.

**NOT visible:**
- NUT poll latency (no timing data)
- Virtual UPS write latency
- EMA filter diagnostic (alpha value, deviation magnitude)
- Discharge buffer depth
- Model.json file size
- Rate of missed virtual UPS writes

#### 3.4 Signal Handling

- SIGTERM/SIGINT -> `self.running = False`.
- Allows current poll to complete (up to 10 sec latency).
- **Missing:** No `model.save()` before exit. Latest SoH not persisted.
- **Missing:** If in virtual_ups write at signal time, file may be partially written.

#### 3.5 Unbounded Log Growth

- NUT unreachable -> error log with `exc_info=True` every 10 seconds.
- No rate limiting. 30 min NUT outage = 180 entries x 10+ lines = 1800+ log lines.
- Risk: journal pressure on headless server.

#### 3.6 SRE Recommendations

**P0: Data Loss Protection**
1. Model load validation: Add checksum field to model.json. Alert on mismatch.
2. Disk full handling: Wrap all `.save()` in try/except. Log with ALERT priority.
3. Graceful shutdown: Call `model.save()` in signal handler before `running = False`.

**P1: Observability**
1. Add structured logging: `POLL_LATENCY_MS`, `DISCHARGE_BUFFER_SIZE`, `VIRTUAL_UPS_WRITE_STATUS`.
2. Rate-limit error logs: suppress after 10 occurrences in 5 min.
3. Health check: file-based or systemd watchdog.

**P2: Edge Cases**
1. Fast poll recovery: reset sag state on exception.
2. Timestamp monotonicity: assert sorted ascending in SoH calculator.
3. Calibration write: track exception count, skip after 3+ consecutive failures.

#### 3.7 SRE Open Questions

1. Clock jump behavior: test with `timedatectl set-time` during discharge. Does SoH break?
2. Disk full during model.save(): what's tmpfile cleanup behavior if fsync fails?
3. Virtual UPS write atomicity: if write fails after tmpfile created, does old file persist?
4. EMA after long NUT outage: does EMA immediately react or stay stale?

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
