# Expert Panel Review — 2026-03-14

Comprehensive review: architecture, code quality, security, performance, efficiency, clean code.

## Scope

- **What:** UPS battery monitor daemon (Python, systemd service, single home server)
- **Blast radius:** `host-level` — bug can cause incorrect shutdown (data loss) or missed shutdown (hardware damage)
- **Codebase:** ~1,856 LoC across 11 modules, 181 tests

## Operating Context

- **UPS:** CyberPower UT850EG (425W), VRLA lead-acid battery, connected via USB
- **Server:** Headless Debian 13 (no monitor, no keyboard), accessible only via SSH. Unclean shutdown = potential data loss
- **Power grid:** Unstable. **Blackouts several times per week** — mostly short (1-2 minutes), occasionally several hours
- **Battery stress:** Frequent charge/discharge cycles accelerate degradation. Battery will degrade measurably within months, not years
- **Key requirements:**
  - Accurate runtime prediction during each blackout ("how many minutes until forced shutdown?")
  - Predictive battery replacement ("order a new battery in N months") — with 100-200+ discharge events per year, statistical models have enough data
  - Correct LB flag delivery to upsmon for timely shutdown
- **Architecture role:** Daemon is a **data source** in the NUT stack — reads from real UPS, computes better metrics (SoC, runtime, SoH), publishes via virtual UPS. Shutdown decision belongs to upsmon, not to this daemon.

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

- [x] ~~run() 200+ lines, 5 nesting levels~~ — refactored to 48 lines, 8 methods extracted.
- [ ] `_update_battery_health()` 80 lines — acceptable, each section is sequential (SoH → prediction → alerting). Would not benefit from splitting.
- [ ] `_auto_calibrate_peukert()` 55 lines — guards are necessary for physics edge cases. Debug logs added.

#### 4.2 Feature Envy

- [x] ~~Calibration writes~~ — extracted to `_write_calibration_points()`.
- [ ] `soh_calculator.calculate_soh_from_discharge()` takes many args — acceptable, pure function with clear signature.

#### 4.3 Boolean Flag Proliferation

- [ ] Deferred. `sag_collected`, `fast_poll_active`, `collecting` serve different lifecycles. State enum would add abstraction without reducing bugs. Current code is clear enough after method extraction.

#### 4.4 Inconsistent Error Handling

- [x] ~~Disk full~~ — all model.save() wrapped in try/except OSError.
- [x] ~~Error log flooding~~ — rate-limited.
- [ ] Three error patterns exist (raise/return None/return {}) — acceptable. Each matches its caller's expectations. Documented in code.

#### 4.5 Magic Numbers

- [x] ~~All magic numbers~~ — "why" comments added: 1000, 5, 100, 0.5, 6, 1e-6.

#### 4.6 Test Coverage Gaps

- [ ] Deferred. Current 181 tests cover critical paths. Missing integration tests for error scenarios (NUT unreachable, disk full, virtual UPS write failure) are P2 items.

#### 4.7 Naming Issues

- [x] ~~ema_ring_buffer.py~~ — renamed to `ema_filter.py`.
- [x] ~~last_soc/last_time_rem~~ — renamed to `_last_logged_soc`/`_last_logged_time_rem`.
- [x] ~~"L1 fix" comments~~ — removed during run() refactor.

#### 4.8 Dead Code

- [x] ~~soh_calculator.py unused import~~ — already clean (report was stale).

#### 4.9 Comments Quality

- [x] ~~All "what not why" comments~~ — rewritten with "why" explanations:
  - fsync read-only FD in model.py
  - epsilon guard in ema_filter.py
  - 100V threshold in event_classifier.py
  - 0.5 SoC fallback in soc_predictor.py

#### 4.10 QA Open Questions

- [x] Q1: fsync on read-only FD — correct for ext4. The write FD from NamedTemporaryFile is closed; reopen read-only ensures metadata flush. Comment added.
- [x] Q2: `_recv_until()` 2s timeout — adequate for localhost NUT. LIST VAR response is ~200 bytes, fits in single recv(). Wall-clock deadline is a safety net, not normal path.
- [x] Q3: Cliff region interpolation only on TEST — by design. TEST is a controlled full discharge; REAL blackout may be partial (power restored early), producing incomplete cliff data.
- [x] Q4: SoC fallback 0.5 — correct choice. 0.0 would trigger false LB flag (premature shutdown). 1.0 would suppress LB when battery is actually low. 0.5 is conservative middle. Comment added.

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

- [x] ~~11 env vars~~ — eliminated entirely. Replaced with:
  - TOML config file (`config.toml`) with 3 user-facing settings: ups_name, shutdown_minutes, soh_alert
  - Physics params (IR_K, IR_L_BASE) live only in model.json
  - Everything else hardcoded as implementation details

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

#### 5.4 Startup Health Checks

- [x] ~~model.json validation~~ — `_validate_model()` checks LUT, anchor, SoH range, capacity.
- [x] ~~NUT reachable~~ — `_check_nut_connectivity()` at startup, logs warning if unreachable. Intentionally doesn't fail — daemon should retry (NUT may start later).
- [ ] /dev/shm writable — deferred (low risk, systemd ReadWritePaths guarantees access).

#### 5.5 Discharge Buffer Persistence

- [ ] Deferred. `calibration_last_written_index` in RAM only. Mitigated by timestamp-based dedup — restart during calibration re-processes points but duplicates are skipped.

#### 5.6 Modularity Assessment

- [x] Resolved: keep modules separate. 181 tests across 15 files justify the modularity cost.

#### 5.7 What Kaizen Would Still Cut

- [x] ~~11 env vars~~ — replaced with 3-setting TOML config.
- [ ] Virtual UPS fsync on tmpfs — cargo cult (tmpfs is RAM-backed, fsync is no-op). Harmless, costs nothing. Not worth changing.
- [x] ~~Alerter abstraction~~ — verified justified. MOTD script (`51-ups-health.sh`) reads from upsc + model.json. Alerter provides structured journald warnings for operators/Grafana Loki. Different audience.

#### 5.8 What Kaizen Would Add

- [x] ~~Config file~~ — TOML config implemented.
- [x] ~~Startup health checks~~ — `_validate_model()` implemented.
- [x] ~~Fallback shutdown~~ — **rejected** (violates NUT architecture, see 5.3).
- [ ] Discharge buffer checkpointing — deferred (see 5.5).

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

## Recommended Action Plan — Status

### P0 — Safety & Resilience — COMPLETE

- [x] Systemd hardening (ProtectSystem, NoNewPrivileges, UMask=0077, etc.)
- [x] model.save() in signal handler
- [x] Verify upsmon reliability (NUT on localhost, nut-monitor enabled)
- [x] Rename ema_ring_buffer.py -> ema_filter.py
- [x] Symlink guard in virtual_ups.py
- [x] Disk full handling (all save() wrapped in try/except OSError)

### P1 — Configuration & Code Quality — COMPLETE

- [x] Replace env vars with TOML config (3 user-facing settings)
- [x] Model validation at startup (_validate_model)
- [x] Extract 8 methods from run() (208 → 48 lines)
- [x] Rate-limit error logs (first 10 + summary every 60s)
- [x] "Why" comments on all magic numbers
- [x] Fix naming (last_soc → _last_logged_soc, ema_ring_buffer → ema_filter)
- [x] Install script security (sed -i.bak, getent instead of eval)
- [x] Calibration write dedup by timestamp (not voltage)
- [x] Debug logs on all silent early returns

### P2 — Observability & Edge Cases — MOSTLY COMPLETE

- [x] Structured logging: nut_latency (ms) and discharge_buf depth in status log
- [x] Systemd watchdog: Type=notify, WatchdogSec=120, sd_notify on each poll
- [ ] Discharge buffer checkpointing in calibration mode — deferred (timestamp dedup mitigates)
- [x] Clock jump guard: soh_calculator rejects non-monotonic timestamps
- [x] Fast poll recovery: reset fast_poll_active on exception
- [ ] Test coverage: integration tests for error scenarios — deferred

### P3 — Deferred (revisit after 3 months of data)

- [ ] Peukert auto-calibration hardening — require 3+ consistent >10% errors
- [ ] Kaizen's 3-month verification — compare predicted vs actual runtime on 50+ events
- [x] ~~Boolean flags → state enum~~ — SagState enum (IDLE → MEASURING → COMPLETE) replaces 3 boolean flags

### Open Items — RESOLVED

- [x] NUT bound to localhost only — verified (`127.0.0.1:3493`)
- [x] /dev/shm mounted nosuid,nodev — verified
- [x] UMask set to 0077 in service file
- [x] MOTD uses upsc + model.json, not journald — alerter is justified (serves operators/Loki)
- [x] Calibration mode — removed as separate mode. LUT auto-calibrates from every discharge. `--calibration-mode` flag deleted.

### Beyond Action Plan (additional work from this session)

- [x] NUT client: LIST VAR single-connection (6 TCP → 1)
- [x] Adaptive EMA (DynamicAdaptiveFilterV2-inspired, fast reaction to power events)
- [x] Time-weighted cliff interpolation (CURVE_RELEVANCE_HALF_LIFE_DAYS=90, reviewed by statistician + battery chemist)
- [x] Auto-calibration on every discharge (removed calibration_mode gate)
- [x] TOML config (11 env vars → 3 user-facing settings)
- [x] Magic numbers → named constants (6 constants extracted)
- [x] Logging deduplication (3 handlers → 1 JournalHandler, explicit identifier)
- [x] model.json written at first startup (tools can always read it)
- [x] battery-health.py script (human-friendly health report with UPS identity)
- [x] User scenarios documentation (3 JTBD: health report, deep test, config)
- [x] nut_exporter switched to virtual UPS (`?ups=cyberpower-virtual`)
- [x] Old MOTD duplicate removed
- [x] GSD todos: battery replacement scenario, install.sh system integration gaps
