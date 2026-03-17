# Expert Panel Review — v1.1 Comprehensive (2026-03-15)

**Scope**: Architecture, code quality, security, performance, efficiency, clean code, code smells
**Blast radius**: `host-level` — daemon controls shutdown signaling for a headless server
**Panel**: System Architect, Security Analyst, SRE, QA Engineer, Kaizen Master, Researcher (Battery Physics)
**Codebase**: ~2,300 LoC across 12 modules, 208 tests, Python 3.11+

---

## Panel Composition & Mandates

| Role | Mandate | Signature Question |
|------|---------|-------------------|
| System Architect | Long-term coherence, dependencies, tech debt | "How does this fit the whole system?" |
| Security Analyst | Attack surfaces, privilege, compliance | "How can this be exploited?" |
| SRE | Observability, SLOs, failure domains, resilience | "How will we know when this breaks?" |
| QA Engineer | Validation, edge cases, failure modes | "What if this fails halfway through?" |
| Kaizen Master | Over-engineering, YAGNI, incremental delivery | "Do we need this now?" |
| Researcher (Battery Physics) | Physics correctness, formula accuracy | "Is the math right?" |

---

## Individual Expert Analysis

### System Architect

**Assessment:** Clean, well-decomposed architecture. 12 modules with single responsibility each. Frozen dataclasses for config, state machine for event classification, atomic writes — solid engineering fundamentals. The daemon-as-data-source principle (no shutdown calls) follows NUT's design contract correctly.

**Risks:**
- `monitor.py` at 839 lines is the largest module and `run()` is a 70-line method mixing polling, error handling, sag measurement, discharge tracking, metrics computation, and virtual UPS writing — God Method smell
- `_write_health_endpoint()` is not atomic, while `model.json` and `ups-virtual.dev` both use atomic write patterns — inconsistency
- Double `_safe_save()` in `_update_battery_health()` — lines 404 and 420, model saved after `add_soh_history_entry()` and again after `set_replacement_due()`, second save could batch both
- `BatteryModel.data` is a raw dict, all getters do `.get()` with defaults — schema mismatch after manual edit silently falls back to defaults instead of failing loudly

**Recommendation:** Extract `run()` loop body into a `_poll_once()` method. Unify health endpoint writes with atomic pattern. Collapse double saves.

**Open question:** Has `health.json` corruption been observed in production during power events?

---

### Security Analyst

**Assessment:** Attack surface is minimal — daemon reads from localhost NUT socket and writes to tmpfs/config dir. Symlink guard on `/dev/shm/ups-virtual.dev` is good. No network-facing API. Solid threat model for a single-purpose daemon.

**Risks:**
- `_write_health_endpoint()` has no symlink check. `virtual_ups.py:49` guards against symlink attacks, but `monitor.py:214` writes `health.json` with plain `open()`. If an attacker places a symlink at `health.json` -> `/etc/passwd`, the daemon overwrites it every 10 seconds. Low likelihood (requires local access), but the pattern is inconsistent.
- `model.json` permissions not explicitly set. `atomic_write_json()` creates temp files with default umask. `virtual_ups.py` sets `fchmod(0o644)` explicitly — `model.py` should match for consistency.
- NUT client trusts all response data. `_parse_var_line()` parses `float()` from NUT responses without bounds checking. A compromised NUT daemon could send `battery.voltage: 99999.9` — daemon would propagate through EMA, SoC, and runtime. Physics formulas produce nonsensical results but don't crash.
- `daemon_version` hardcoded as string `"1.1"`, not read from `pyproject.toml`. Will drift on version bump.

**Recommendation:** Add symlink guard to `_write_health_endpoint()`. Set explicit file permissions on `model.json`. Consider bounds validation for NUT voltage/load values.

**Open question:** Is `~/.config/ups-battery-monitor/` writable only by `j2h4u`?

---

### SRE

**Assessment:** Good observability fundamentals — journald with structured fields, health.json endpoint, rate-limited error logging. Systemd watchdog integration (WatchdogSec=120) with sd_notify is production-grade. Error handling in polling loop is well-thought-out (burst control, sag state reset on errors).

**Risks:**
- `_write_health_endpoint()` runs every poll (10s) but does plain filesystem writes. During rapid blackout recovery (OB->OL->OB in seconds), creates unnecessary I/O. If `model_dir` is on SSD (`~/.config/`), this is 8,640 writes/day for health status.
- No metric for NUT poll latency distribution. `poll_latency_ms` is logged but not exported to virtual UPS or health.json. A degrading NUT connection (e.g., USB driver issue) would only be visible in journal grep.
- `sd_notify('WATCHDOG=1')` is inside the try block. If `_write_health_endpoint()` fails (e.g., disk full), watchdog still gets notified. Daemon appears healthy when it can't write. Should move watchdog notification to after all critical writes succeed.
- No startup metric. How long does init take? If NUT unreachable at startup, `_check_nut_connectivity()` logs a warning but there's no metric for "time to first successful poll."

**Recommendation:** Move health.json to `/dev/shm/` alongside virtual UPS (eliminates SSD writes). Export poll latency to health endpoint. Move watchdog notification to after writes.

**Open question:** Is there a Grafana dashboard consuming `health.json`? If not, who reads it?

---

### QA Engineer

**Assessment:** 208 tests with strong coverage of edge cases (malformed JSON, socket timeouts, NTP jumps). Conftest fixtures are well-organized. Test-to-code ratio (~60% of codebase) is healthy.

**Risks:**
- **Critical logic bug**: `_handle_event_transition()` updates `previous_event_type` only inside the reporting gate (line 790). If daemon is in OL state and `poll_count % 6 != 0`, transitions are classified (`_classify_event` runs every poll) but `_handle_event_transition()` is skipped. On the next gated poll, `previous_event_type` is stale. An OL->OB->OL fast cycle (<60s) could miss the OB->OL transition entirely, skipping SoH calculation and LUT update.
- `DischargeBuffer.voltages` default is `None`, not `[]`. The `__post_init__` fix avoids the mutable default pitfall, but `None` as the field default with a post-init override is a code smell. `field(default_factory=list)` from `dataclasses` is the idiomatic pattern.
- `soc_from_voltage()` does linear scan for exact match (lines 41-42), then binary search for bracket. Linear scan iterates all LUT entries before the O(log n) binary search. With 200+ measured entries, this is O(n) per call. The exact match tolerance (+/-0.01V) rarely hits, making scan mostly wasted work.
- `calibration_write()` sorts entire LUT on every call (line 342). During a 3-hour discharge, called up to 1000 times, each sorting full LUT. `bisect.insort` would be O(log n) per insert instead of O(n log n).

**Recommendation:** F13 is a logic bug — `_handle_event_transition()` and `previous_event_type` update should run every poll, not just on the reporting gate. This is the highest-priority finding.

**Open question:** Has a fast OL->OB->OL cycle (<60s) been tested in production?

---

### Kaizen Master

**Assessment:** Project shows excellent restraint — 3 user-facing config options, hardcoded internals, no premature abstraction. The "daemon is a data source, not a decision maker" principle prevents scope creep. Physics-first calculations avoid empirical tuning nightmares.

**Risks:**
- Backward compatibility properties in `EMAFilter` (lines 120-133): `ema_voltage`, `ema_load`, `samples_since_init` — who consumes these? If only internal code, remove them. Dead compatibility shims accumulate.
- `alerter.py` module-level logger (line 9) is unused. Function signatures take `logger` as a parameter. The module-level `logger = logging.getLogger("ups-battery-monitor")` is never referenced.
- Docstring verbosity. `virtual_ups.py`'s `write_virtual_ups_dev()` has a 20-line docstring for a 30-line function. Code is self-documenting — docstring mostly restates what code does. Same pattern in several other functions (`_write_health_endpoint`, `Config`, `_safe_save`).
- `MODEL_DIR` and `MODEL_PATH` module-level constants (lines 107-108) are defined after `_load_config()` but before `MonitorDaemon`. Not used by `MonitorDaemon` (it uses `config.model_dir`). Dead code.

**Recommendation:** Remove backward compat properties if unused, unused logger, dead module-level constants. Trim docstrings to what adds value beyond the code.

**Open question:** Are the backward compatibility properties used by any external tool (battery-health.py, MOTD script)?

---

### Researcher (Battery Physics)

**Assessment:** Physics implementation is sound. Peukert's Law correctly applied, trapezoidal integration for SoH is appropriate for 10s sampling, time-weighted LUT averaging models electrode degradation correctly. IR compensation formula is standard. The 90-day half-life is a reasonable default for VRLA aging.

**Risks:**
- SoH calculation uses `reference_soh * degradation_ratio` (soh_calculator.py:89). Multiplicatively compounds: if `reference_soh=0.9` and `degradation_ratio=0.95`, new SoH=0.855. Over many short blackouts (100-200/year), this can cause SoH to drift downward faster than reality because each short discharge measures only a small portion of the curve. The area ratio from a 2-minute discharge is not representative of full-capacity health.
- `_auto_calibrate_peukert()` uses EMA load as "average load during discharge" (line 484). EMA load reflects the smoothed current load, not the average over the entire discharge. For short blackouts, EMA load could still be converging from the OL->OB transition.
- `ir_compensate()` formula is linear (V_norm = V + k*(L - L_base)). Real IR drop is non-linear at high loads (>50%) due to concentration polarization. At 15-20% load, linear is fine, but if load ever increases significantly, SoC predictions will be off.

**Recommendation:** Require a minimum discharge duration (e.g., 5 minutes) before updating SoH. This filters out noise from micro-blackouts that don't reveal meaningful capacity information.

**Open question:** What's the shortest blackout that has triggered a SoH update in production?

---

## Panel Conflicts & Resolutions

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| F13: Transition handling in reporting gate | QA: Logic bug, fix immediately | Architect: By design (reduces computation during OL) | **QA wins.** Missing OB->OL transitions means lost calibration data — the daemon's core value prop. Move transition handling out of the gate. |
| F9: health.json on SSD | SRE: Move to /dev/shm | Kaizen: It's 200 bytes, SSD wear is negligible | **SRE wins.** 8,640 writes/day * 365 = 3.15M writes/year. On Kingston SSD with ~600 TBW, negligible in bytes, but write amplification (4KB minimum block) and journal commits add up. Moving to tmpfs is trivial and eliminates the concern. |
| F21: SoH drift from short blackouts | Researcher: LUT-freshness gate (binary) | Statistician: Duration-weighted Bayesian blending (soft) | **Statistician wins (stakeholder-approved).** Binary freshness gate is too coarse — either updates or doesn't. Duration-weighted Bayesian blending is superior: `new_soh = prior*(1-w) + measured*w` where `w = min(duration / (0.30 * T_expected), 1.0)`. 10s test → w=0.01 (negligible), 30min → w=1.0 (full update). Electrochemist confirmed physics model is sound; statistician fixed the estimator. LUT calibration points always recorded regardless. |

---

## Priority Ladder (used for conflict resolution)

1. **Safety** — No data loss, no unrecoverable states, no security regression
2. **Correctness** — It must actually work
3. **Security** — Least privilege, defense in depth
4. **Reliability** — Proven over novel
5. **Simplicity** — Fewer moving parts wins
6. **Cost/Effort** — After the above are satisfied
7. **Elegance** — Nice but never decisive

---

## Complete Findings Table

| # | Priority | Category | Expert | Finding | File:Line |
|---|----------|----------|--------|---------|-----------|
| F13 | **P0** | Correctness | QA | `_handle_event_transition()` inside reporting gate — misses fast OB->OL transitions, skips SoH/LUT update | monitor.py:786-792 |
| F1 | P1 | Architecture | Architect | `monitor.py:run()` is 70-line God Method mixing 6+ concerns | monitor.py:746-818 |
| F5 | P1 | Security | Security | `_write_health_endpoint()` no symlink guard, not atomic — inconsistent with virtual_ups.py pattern | monitor.py:191-216 |
| F9 | P1 | Performance | SRE | `health.json` writes to SSD every 10s (8,640 writes/day), should be on tmpfs | monitor.py:795-799 |
| F2 | P2 | Architecture | Architect | `_write_health_endpoint()` uses plain `open()`, not atomic — power loss = corrupted health.json | monitor.py:214 |
| F3 | P2 | Code smell | Architect | Double `_safe_save()` in `_update_battery_health()` — save at line 404 and again at 420 | monitor.py:404,420 |
| F4 | P2 | Architecture | Architect | `BatteryModel.data` is raw dict — silent fallback on schema mismatch via `.get()` defaults | model.py:60-380 |
| F6 | P2 | Security | Security | `model.json` no explicit file permissions (`fchmod`), relies on umask | model.py:37-57 |
| F10 | P2 | Observability | SRE | NUT poll latency logged but not exported to health.json or virtual UPS | monitor.py:767 |
| F11 | P2 | Reliability | SRE | `sd_notify('WATCHDOG=1')` before critical writes verified — daemon appears healthy when it can't write | monitor.py:801 |
| F14 | P2 | Code smell | QA | `DischargeBuffer` uses `None` default instead of `field(default_factory=list)` | monitor.py:148-159 |
| F15 | P2 | Performance | QA | `soc_from_voltage()` O(n) linear scan before O(log n) binary search — wasted on 200+ entries | soc_predictor.py:41-42 |
| F16 | P2 | Performance | QA | `calibration_write()` sorts entire LUT on every call — up to 1000 times during 3h discharge | model.py:342 |
| F17 | P2 | Code smell | Kaizen | Backward compat properties in `EMAFilter` (`ema_voltage`, `ema_load`, `samples_since_init`) — possibly dead | ema_filter.py:120-133 |
| F18 | P2 | Code smell | Kaizen | Unused module-level `logger` in alerter.py — functions take logger as parameter | alerter.py:9 |
| F21 | **P0** | Physics/Stats | Researcher + Statistician | **CRITICAL**: Current SoH estimator has −99.6% bias on short discharges (10s test → SoH=0.004). Fix: duration-weighted Bayesian blending. LUT calibration always runs; SoH gated by soft weight. See `STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md`. | soh_calculator.py:87-92 |
| F22 | P2 | Physics | Researcher | Peukert auto-calibration uses EMA load (not true discharge-average load) | monitor.py:484 |
| F7 | P3 | Security | Security | NUT response values not bounds-checked — compromised NUT could send extreme values | nut_client.py:67-70 |
| F8 | P3 | Code smell | Security | Hardcoded version string `"1.1"` — will drift on version bump | monitor.py:210 |
| F12 | P3 | Observability | SRE | No startup timing metric — "time to first successful poll" not tracked | monitor.py:312-321 |
| F19 | P3 | Code smell | Kaizen | Docstring verbosity — 20-line docstrings for 30-line functions, restating code | virtual_ups.py, monitor.py |
| F20 | P3 | Code smell | Kaizen | Dead `MODEL_DIR`/`MODEL_PATH` module-level constants — not used by MonitorDaemon | monitor.py:107-108 |
| F23 | P3 | Physics | Researcher | Linear IR compensation — fine at 15-20% load, inaccurate at >50% due to concentration polarization | ema_filter.py:136-145 |

**Totals: 2 P0, 3 P1, 12 P2, 6 P3 = 23 findings**

---

## Recommended Action Plan

### Phase 1 — Correctness (P0)

- [x] **F13**: Move `_handle_event_transition()` and `previous_event_type` update out of the reporting gate in `run()`. Only `_compute_metrics`, `_log_status`, and `_write_virtual_ups` should remain gated. Transition handling must run every poll to catch fast OL->OB->OL cycles.
- [x] **F13-note**: Daily 10-second battery test (cron) is a known OL->OB->OL fast cycle. EventClassifier already distinguishes BLACKOUT_TEST vs BLACKOUT_REAL by input voltage — both event types should trigger calibration data collection correctly after this fix.
- [x] **F13-test**: Add unit test for fast OL->OB->OL cycle (<60s) verifying SoH update fires
- [x] **F13-test**: Add unit test for daily battery test (BLACKOUT_TEST, ~10s) verifying calibration points are collected

### Phase 2 — Security & Safety (P1)

- [x] **F5**: Add symlink guard to `_write_health_endpoint()` (match `virtual_ups.py:49` pattern)
- [x] **F5**: Make `_write_health_endpoint()` atomic (tempfile + fdatasync + rename pattern)
- [x] **F9**: Move `health.json` to `/dev/shm/` alongside virtual UPS (eliminate SSD writes)
- [x] **F9**: Update any consumers of health.json to read from new location
- [x] **F1**: Extract `run()` loop body into `_poll_once()` method for readability and testability

### Phase 3 — Physics & Accuracy (P2)

- [x] **F21**: **Replace multiplicative SoH with duration-weighted Bayesian blending** (statistician's recommendation, stakeholder-approved). The current `new_soh = reference_soh * degradation_ratio` has catastrophic −99.6% bias on short discharges (10s test → SoH collapses to 0.004). Fix:
  ```python
  discharge_weight = min(discharge_duration / (0.30 * T_expected_sec), 1.0)
  measured_soh = reference_soh * degradation_ratio
  new_soh = reference_soh * (1 - discharge_weight) + measured_soh * discharge_weight
  ```
  This gives weight ~0.01 for 10s tests (barely changes SoH), ~0.15 for 2-min blackouts, ~1.0 for 30-min+ discharges. Equivalent to Bayesian posterior with exponential prior. **Replaces the earlier LUT-freshness gate proposal** — soft weighting is superior to binary gating.
- [x] **F21-note**: Electrochemist confirms multiplicative model is chemically correct for VRLA sulfation/corrosion, but the ratio estimator is broken for partial discharges. Duration weighting solves the sampling bias without changing the underlying physics model.
- [x] **F21-calibration**: LUT calibration points (voltage->SoC mappings) should ALWAYS be recorded from any discharge regardless of duration — they improve the voltage curve. Duration weighting applies ONLY to SoH calculation.
- [x] **F21-micro-gate**: Add `if discharge_weight < 0.001: return reference_soh` guard for micro-discharges (<~3 seconds) that carry zero signal.
- [x] **F21-test**: 10-second test barely changes SoH (assert soh > 0.94 for reference_soh=0.95)
- [x] **F21-test**: 30-minute discharge strongly updates SoH (assert soh < reference_soh and soh > 0.80)
- [x] **F21-test**: SoH change scales smoothly with duration (delta_10s < delta_100s < delta_1000s)
- [x] **F21-docs**: Reference `docs/STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md` and `IMPLEMENTATION-GUIDE-SOH-FIX.md` (created by statistician) for full mathematical justification and ready-to-use code
- [x] **F22**: Compute true average load during discharge (accumulate load samples in `DischargeBuffer`, divide by count) instead of using EMA snapshot
- [x] **F3**: Collapse double `_safe_save()` in `_update_battery_health()` — single save at end of method after all mutations complete

### Phase 4 — Performance (P2)

- [x] **F15**: Remove linear scan for exact match in `soc_from_voltage()` — let binary search handle all cases, including near-matches within tolerance
- [x] **F16**: Replace `list.sort()` in `calibration_write()` with `bisect.insort()` for O(log n) per insert instead of O(n log n)

### Phase 5 — Observability (P2)

- [x] **F10**: Export NUT poll latency to health.json (add `poll_latency_ms` field)
- [x] **F11**: Move `sd_notify('WATCHDOG=1')` to after all critical writes (virtual UPS + health endpoint) succeed
- [x] **F6**: Add explicit `fchmod(0o644)` in `atomic_write_json()` (match virtual_ups.py pattern)

### Phase 6 — Code Smell Cleanup (P2)

- [x] **F14**: Replace `DischargeBuffer` `None` defaults with `field(default_factory=list)`
- [x] **F17**: Check if `ema_voltage`, `ema_load`, `samples_since_init` backward compat properties are used by battery-health.py or MOTD scripts; remove if not
- [x] **F18**: Remove unused module-level `logger` in `alerter.py` (line 9)
- [x] **F4**: Consider adding schema validation on `BatteryModel.load()` — at minimum, log warning if expected keys are missing (don't silently fall back)

### Phase 7 — Low Priority (P3, do when convenient)

- [x] **F7**: Add bounds validation for NUT voltage (8.0-15.0V) and load (0-100%) in `_update_ema()` — log warning and skip sample if out of range
- [x] **F8**: Read version from `importlib.metadata.version('ups-battery-monitor')` instead of hardcoding `"1.1"`
- [x] **F12**: Log startup timing: record `time.monotonic()` at init start and first successful poll, log delta
- [ ] **F19**: Trim verbose docstrings that restate code — keep only non-obvious context (physics rationale, failure modes, external contract) — **Skipped: low ROI, noisy diffs**
- [x] **F20**: Remove dead `MODEL_DIR` and `MODEL_PATH` module-level constants (lines 107-108)
- [x] **F23**: Document that IR compensation is linear and valid only at <50% load — add comment in `ir_compensate()` noting the limitation

---

## F21 Specialist Sub-Panel (Statistician + Electrochemist)

Convened to evaluate the proposed LUT-freshness gate for SoH estimation.

### Electrochemist's Key Findings

- Multiplicative SoH model (`new = prev * ratio`) is **electrochemically correct** for VRLA sulfation/corrosion
- VRLA discharge curve has 3 regions: flat (0-20% depth), cliff (20-80%), floor (80-100%). Short discharges only sample region 1 — **not representative** of full-capacity health.
- 90-day half-life matches electrode microstructure change timescale (30-40 cycles at our rate)
- Suggested considering 60-day half-life for faster adaptation
- Proposed depth categorization (10s_test / short / medium / deep) with per-category weights
- Confirmed: no better approach available without hardware (coulomb counting needs current sensor, OCV needs 30min rest, EIS needs LCR meter)

### Statistician's Key Findings

- **CRITICAL**: Current estimator has −99.6% downward bias on short discharges. 10s test → `ratio ≈ 0.004` → `SoH = 0.95 * 0.004 = 0.004`. Catastrophic.
- This is not a tuning problem — it's a fundamental estimator design flaw
- Proposed **duration-weighted Bayesian blending** as fix (Option C in their analysis):
  - `weight = min(duration / (0.30 * T_expected), 1.0)`
  - `new_soh = reference_soh * (1 - weight) + measured_soh * weight`
- Evaluated 4 alternatives: hard cutoff (crude), weighted exponent (still multiplicative), Bayesian blend (recommended), direct capacity measurement (v2.0)
- Assessed our freshness gate as "principle correct, binary gating too conservative"
- **Stakeholder chose: Bayesian blend (statistician's recommendation)**

### Specialist Deliverables

| Document | Location | Content |
|----------|----------|---------|
| Full statistical analysis | `docs/STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md` | 9-section mathematical analysis, bias decomposition, variance, alternatives |
| Executive summary | `docs/SOH-ESTIMATOR-EXECUTIVE-SUMMARY.md` | Condensed findings for engineers |
| Implementation guide | `IMPLEMENTATION-GUIDE-SOH-FIX.md` | Ready-to-use code, 3 test cases, deployment checklist, rollback plan |
| Final report | `STATISTICAL-REVIEW-FINAL-REPORT.md` | Decision matrix, risk assessment, action items |
| Quick reference | `docs/QUICK-REFERENCE-SOH-STATISTICS.md` | TL;DR + code snippets |
| Document index | `docs/INDEX-STATISTICAL-ANALYSIS.md` | Navigation guide |

---

## Open Questions from Panel

These should be investigated/answered before or during implementation:

- [ ] Has `health.json` corruption been observed in production during power events? (Architect) — **Unanswered, unknown**
- [x] Is `~/.config/ups-battery-monitor/` writable only by `j2h4u`? (Security) — **Yes. Single-user server, out of scope for threat modeling. F6 deprioritized.**
- [x] Is there a Grafana dashboard consuming `health.json`? If not, who reads it? (SRE) — **No known consumers. Grafana Alloy reads from elsewhere. Possibly MOTD script — needs code check. Safe to move to /dev/shm/.**
- [x] Has a fast OL->OB->OL cycle (<60s) been tested in production? (QA) — **Rare (~1/month), but daily 10-second battery test via cron also triggers OL->OB->OL. F13 is real: every morning the test cycle could miss the OB->OL transition and skip calibration.**
- [x] Are the backward compatibility properties (`ema_voltage`, `ema_load`) used by battery-health.py or MOTD scripts? (Kaizen) — **Both used but fully under our control. No external API contract. Safe to remove compat properties and update consumers.**
- [x] What's the shortest blackout that has triggered a SoH update in production? (Researcher) — **Most common blackouts are 1.5-3 minutes. Daily 10-second battery test also triggers. F21 confirmed: 10-second test and 1.5-min blackouts should NOT update SoH. 5-minute minimum threshold is correct.**

---

## Positive Observations (what the panel praised)

The panel unanimously noted several strengths worth preserving:

1. **Separation of concerns**: Daemon is a data source, not a decision maker. Shutdown logic belongs to upsmon. This is architecturally sound and follows NUT's design contract.
2. **Physics-first design**: All formulas (Peukert, Trapezoidal Rule, Linear Regression) derived from first principles — no empirical tuning constants that would drift.
3. **Atomic write pattern**: fdatasync + rename for crash safety on model.json and virtual UPS. This is production-grade.
4. **Adaptive EMA**: Dynamic alpha provides fast reaction to power events while smoothing sensor noise — elegant solution.
5. **Self-calibration loop**: LUT improves with every blackout, Peukert exponent adjusts from actual data, cliff region interpolates from measured points. The system gets better over time without manual intervention.
6. **Configuration restraint**: Only 3 user-facing settings. Everything else is hardcoded or auto-calibrated. This prevents misconfiguration.
7. **Batch calibration writes**: 60x SSD wear reduction by buffering calibration points and flushing periodically.
8. **Error handling in polling loop**: Rate-limited logging (first 10 full, then summary), sag state reset on errors, no crash on NUT disconnection.
9. **History pruning**: Automatic pruning of SoH, LUT, and R_internal histories prevents unbounded growth.
10. **Test coverage**: 208 tests (~60% of codebase) covering edge cases like malformed JSON, socket timeouts, NTP clock jumps, and floating-point precision.
