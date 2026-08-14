# ups-unfucked — Current Context

This document provides essential context for expert reviewers, panels, and new contributors. Updated 2026-08-14 after the durable-discharge incident and journal design.

## What This Is

A Python daemon that transforms a budget CyberPower UT850EG ($30 UPS) into an enterprise-grade battery monitoring system. It sits between the real UPS and NUT, replacing firmware's inaccurate metrics with physics-based calculations, and exposing enterprise-equivalent telemetry (SoH, cycle count, replacement prediction, internal resistance) that the hardware doesn't natively provide.

## Operating Environment

- **UPS**: CyberPower UT850EG, 425W, 12V VRLA (lead-acid), 7.2Ah, connected via USB
- **Server**: Headless Debian 13 (no monitor/keyboard, SSH only). Unclean shutdown = data loss risk.
- **Power grid**: Unstable. **Blackouts several times per week** — mostly 1-2 minutes, occasionally hours.
- **Battery stress**: Frequent charge/discharge cycles. Battery degrades measurably within months, not years. 100-200+ discharge events per year.
- **Load**: ~15-20% of 425W (stable, server workload)
- **Temperature**: No sensor. Indoor 18-25°C, ±3% SoC uncertainty from temperature is accepted.

## Architecture

```
Real UPS (usbhid-ups) → NUT upsd (:3493)
    ↓ TCP (LIST VAR, single connection per poll)
ups-battery-monitor daemon (1s physical poll; 10s durable samples; 60s human report)
    ↓ MetricEMA (per-metric adaptive EMA) → IR compensation → SoC (voltage LUT) → Runtime (Peukert)
    ↓ Event classifier (ONLINE / BLACKOUT_REAL / BLACKOUT_TEST)
    ↓ Event journal (raw evidence) → lifecycle/evidence classification → gated model updates
    ↓ SoH tracking → Capacity estimation → Replacement prediction → R_internal measurement
    ↓ Enterprise counters (cycle count, cumulative on-battery time, install date)
    ↓ Per-poll writes during OB state (no 60s lag on LB flag)
    ↓
/run/ups-battery-monitor/ups-virtual.dev (atomic tmpfs write, fdatasync)
    ↓
NUT dummy-ups → upsd → upsmon (shutdown) / Grafana (dashboards) / MOTD
    ↑
health.json (last_poll, SoC, online, capacity metrics — for external monitoring)
```

**Key principles**:
- Daemon is a **data source**, not a decision maker. Shutdown logic belongs to upsmon. Daemon publishes corrected metrics and LB flag through the virtual UPS.
- **The journal is source of truth for raw operational discharge evidence.** It lives at
  `~/.config/ups-battery-monitor/discharge-events-v1.jsonl`, uses one synced JSON record per
  accepted observation, and is replayed at boot. `model.json` remains the authoritative derived
  battery state, but partial or reboot-gapped events never update its absolute capacity, SoH, or
  Peukert fields. To edit model.json while daemon is running: `systemctl stop
  ups-battery-monitor`, edit, `systemctl start ups-battery-monitor`.

## What The Daemon Computes (vs firmware)

| Metric | Firmware | Daemon | Method |
|--------|----------|--------|--------|
| Charge % | Coulomb counter (drifts, ±50% error) | Voltage→SoC lookup table | LUT with linear interpolation, IR-compensated |
| Runtime | ~22 min reported, actual ~47 min | Peukert model (±10%) | Physics-based, load-dependent, SoH-adjusted |
| SoH | Not available | Evidence-gated capacity-based degradation tracking | measured_Ah / rated_Ah only for eligible controlled evidence |
| Measured capacity | Not available | Controlled load/current plus voltage evidence | Trapezoidal integration, CoV-based convergence (≥3 eligible samples) |
| Replacement due date | Not available | Linear regression on SoH history | Persisted in model.json, exported to virtual UPS |
| Internal resistance | Not available | Voltage sag measurement (dV/dI) | On every OL→OB transition |
| Cycle count | Not available | OL→OB transition counter | Persisted in model.json |
| Cumulative runtime | Not available | Sum of discharge durations | Persisted in model.json |
| Battery age | Not available | Install date tracking | Set on first startup |

## Self-Calibration

The daemon learns conservatively from retained evidence:
- **Every accepted on-battery sample** is durably journaled with raw values and model inputs; partial/reboot-gapped events remain operational evidence only
- **Eligible observations** may write measured voltage→SoC points to the LUT, subject to their evidence gate
- **LUT dedup**: entries within ±0.01V are deduplicated, keeping most recent per voltage band
- **Cliff region** (10.5-11.0V): observations can be retained there, but an authoritative
  capacity claim requires a complete supervised endpoint observation
- **Peukert exponent**: calibrated only from evidence that can support a complete load/rate comparison; partial/reboot-gapped events cannot produce an authoritative fit
- **IR compensation coefficient**: auto-calibrated from voltage sag measurements via RLS
- **No automatic hardware deep test**: any capacity test follows the written supervised protocol and explicit approval gate

## Key Technical Decisions

1. **Adaptive EMA** (not fixed alpha): Dynamic alpha scales with input deviation — instant reaction to power events, smooth filtering of sensor noise. Inspired by DynamicAdaptiveFilterV2 (Arduino).

2. **LIST VAR single connection**: One TCP connection per poll instead of 6. Wall-clock deadline + 64KB buffer cap prevent hangs.

3. **TOML config** (not env vars): Only 4 user-facing settings (ups_name, shutdown_minutes, soh_alert, capacity_ah). Everything else is hardcoded or in model.json.

4. **Systemd integration**: Type=notify with WatchdogSec=120, JournalHandler for logging, ProtectSystem=strict hardening.

5. **LUT point sources**: `standard` (datasheet), `measured` (real discharge), `interpolated` (cliff region fill), `anchor` (10.5V physical limit).

6. **Fallback shutdown rejected**: Daemon does not call `systemctl poweroff`. That's upsmon's job. Separation of concerns per NUT architecture.

7. **Evidence-gated capacity-based SoH** (not area-under-curve): measured_capacity/rated_capacity
   is authoritative only for a controlled-capacity event. Partial/recovered observations support
   practical runtime trends and do not masquerade as capacity.

8. **Durable discharge journal**: local JSONL is append-only, `0600`, and synced per accepted
   sample. Journal failure is visible but fail-open for LB/shutdown. Boot replay is idempotent,
   preserves event IDs, and records unknown reboot gaps instead of inventing runtime.

9. **Rollback preserves evidence**: older code may show frozen journal-derived counters; never
   delete or manually merge the journal, and let a re-upgrade replay it.

## Codebase

- **~12,500 LoC** across 14 modules, **337 tests** (336 pass + 1 xfail)
- **Module structure** (F58 decomposition):
  - `src/monitor.py` (791L) — pipeline orchestrator: poll, EMA, classify, sag, discharge, metrics, export
  - `src/monitor_config.py` (262L) — Config dataclass, constants, health endpoint, logger
  - `src/discharge_handler.py` (413L) — DischargeHandler class: SoH, capacity, Peukert, alerts
  - `src/battery_math/` — pure kernel functions: RLS, Peukert calibration, SoH calculation
  - `src/model.py` — BatteryModel persistence, LUT management, atomic JSON writes
  - `src/ema_filter.py` — adaptive EMA with per-metric instances
  - `src/capacity_estimator.py` — coulomb counting + convergence tracking
  - `src/soc_predictor.py` — voltage→SoC LUT lookup
  - `src/runtime_calculator.py` — Peukert runtime prediction
  - `src/event_classifier.py` — NUT status flag-based state machine
  - `src/soh_calculator.py` — capacity-based SoH orchestrator
  - `src/replacement_predictor.py` — linear regression on SoH history
- Config: `config.toml` (4 settings), `model.json` (battery state, auto-calibrated, pruned)
- Scripts: `scripts/battery-health.py` (health report), `scripts/install.sh` (product installer)

## Known Limitations

Documented inline in code as "Known limitations (audit 2026-03-17)" blocks. Key ones:
- **No temperature sensor**: ±3% SoC uncertainty from temperature. $2 NTC thermistor is highest-ROI hardware improvement.
- **CyberPower doesn't expose temperature**: No `battery.temperature` or `ups.temperature` via NUT.
- **Cliff region accuracy**: Requires a complete, supervised observation to claim a 10.5-11.0V capacity result. Short blackouts only provide upper-curve operational evidence.
- **Peukert scalar**: Exponent is load-independent. Works with consistent ~15-20% load. Would need rework for variable loads.
- **Nominal voltage in current calculation**: ~4% systematic bias in coulomb counting (F14/F27). Consistent direction, doesn't affect convergence.
- **IR compensation during discharge**: Linear model approximate during OB. ≤0.06V error at typical loads (F3/F8).

## Documentation

- `README.md` — Product overview, architecture, quick start, roadmap
- `docs/USER-SCENARIOS.md` — Health report, interrupted shutdown/recovery, supervised capacity test, battery replacement, config
- `docs/GLOSSARY.md` — Term definitions for all domain concepts
- `docs/CONTROLLED-CAPACITY-TEST-PROTOCOL.md` — written/supervised protocol; no automatic hardware deep test
- `docs/adr/0002-durable-discharge-journal.md` — decision reversing obsolete timestamp-dedup/checkpoint assumptions
- `docs/archive/` — Completed work: 10 module audits, 7 expert panels, research docs, incident report

## v3.0 — Active Battery Care (Anti-Sulfation) — RETRACTED in v3.2

> **Superseded — see [ADR 0001](../adr/0001-desulfation-premise-reversal.md)**
> The sulfation model, cycle ROI metric, and desulfation-by-discharge premise were all
> removed in v3.2. The scheduler was reframed to diagnostic-only capacity verification
> (IEEE-1188 annual cadence). The bullets below describe what was built in v3.0; they
> are no longer the current design.

~~The daemon currently watches the battery degrade and reports on it. v3.0 makes it fight back:~~
- ~~**Sulfation model**: temperature-dependent crystal growth rate, desulfation from deep discharges~~
- ~~**Smart scheduling**: replace fixed monthly deep test timer with daemon-driven decisions based on days since last deep discharge, sulfation score, SoH trend, and natural blackout frequency~~
- ~~**Cycle ROI metric**: net benefit per discharge (sulfation reversal vs cycle wear)~~
- ~~**Integration with existing systemd timers**: daemon overrides or skips scheduled deep tests based on battery state~~

**Current design:** Any hardware capacity test is considered only through the written supervised
protocol, after full charge/rest, endpoint and abort checks, virtual rehearsal, and explicit user
approval. No daemon path executes or recommends an automatic hardware deep test. Scheduler and
health context may remain visible, but they are not permission to issue a real NUT test command.
