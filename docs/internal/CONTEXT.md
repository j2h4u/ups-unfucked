# ups-unfucked — Current Context

This document provides essential context for expert reviewers, panels, and new contributors. Updated 2026-03-17 (post v2.0, all audit findings closed).

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
ups-battery-monitor daemon (10s poll interval)
    ↓ MetricEMA (per-metric adaptive EMA) → IR compensation → SoC (voltage LUT) → Runtime (Peukert)
    ↓ Event classifier (ONLINE / BLACKOUT_REAL / BLACKOUT_TEST)
    ↓ SoH tracking → Capacity estimation → Replacement prediction → R_internal measurement
    ↓ Enterprise counters (cycle count, cumulative on-battery time, install date)
    ↓ Per-poll writes during OB state (no 60s lag on LB flag)
    ↓
/dev/shm/ups-virtual.dev (atomic tmpfs write, fdatasync)
    ↓
NUT dummy-ups → upsd → upsmon (shutdown) / Grafana (dashboards) / MOTD
    ↑
health.json (last_poll, SoC, online, capacity metrics — for external monitoring)
```

**Key principles**:
- Daemon is a **data source**, not a decision maker. Shutdown logic belongs to upsmon. Daemon publishes corrected metrics and LB flag through the virtual UPS.
- **Memory is source of truth for model.json.** Daemon loads model.json at startup and holds state in memory. Writes to disk happen only on real events (discharge complete, battery replacement, capacity convergence, graceful shutdown) — not on every poll or sag. Between events, the file is not touched. To edit model.json while daemon is running: `systemctl stop ups-battery-monitor`, edit, `systemctl start ups-battery-monitor`.

## What The Daemon Computes (vs firmware)

| Metric | Firmware | Daemon | Method |
|--------|----------|--------|--------|
| Charge % | Coulomb counter (drifts, ±50% error) | Voltage→SoC lookup table | LUT with linear interpolation, IR-compensated |
| Runtime | ~22 min reported, actual ~47 min | Peukert model (±10%) | Physics-based, load-dependent, SoH-adjusted |
| SoH | Not available | Capacity-based degradation tracking | measured_Ah / rated_Ah, Bayesian blend weighted by ΔSoC |
| Measured capacity | Not available | Coulomb counting from deep discharges | Trapezoidal integration, CoV-based convergence (≥3 samples) |
| Replacement due date | Not available | Linear regression on SoH history | Persisted in model.json, exported to virtual UPS |
| Internal resistance | Not available | Voltage sag measurement (dV/dI) | On every OL→OB transition |
| Cycle count | Not available | OL→OB transition counter | Persisted in model.json |
| Cumulative runtime | Not available | Sum of discharge durations | Persisted in model.json |
| Battery age | Not available | Install date tracking | Set on first startup |

## Self-Calibration

The daemon learns the battery automatically:
- **Every blackout** (even 1-2 min): writes measured voltage→SoC points to LUT, gradually replacing the standard VRLA curve
- **LUT dedup**: entries within ±0.01V are deduplicated, keeping most recent per voltage band
- **Cliff region** (10.5-11.0V): interpolated when ≥2 measured points exist there (requires deep discharge)
- **Peukert exponent**: auto-calibrated via RLS (Recursive Least Squares) with forgetting factor λ=0.97. Clamped values (hitting [1.0, 1.4] bounds) are skipped to prevent convergence drift.
- **IR compensation coefficient**: auto-calibrated from voltage sag measurements via RLS
- **No special mode needed**: all calibration is continuous and automatic

## Key Technical Decisions

1. **Adaptive EMA** (not fixed alpha): Dynamic alpha scales with input deviation — instant reaction to power events, smooth filtering of sensor noise. Inspired by DynamicAdaptiveFilterV2 (Arduino).

2. **LIST VAR single connection**: One TCP connection per poll instead of 6. Wall-clock deadline + 64KB buffer cap prevent hangs.

3. **TOML config** (not env vars): Only 4 user-facing settings (ups_name, shutdown_minutes, soh_alert, capacity_ah). Everything else is hardcoded or in model.json.

4. **Systemd integration**: Type=notify with WatchdogSec=120, JournalHandler for logging, ProtectSystem=strict hardening.

5. **LUT point sources**: `standard` (datasheet), `measured` (real discharge), `interpolated` (cliff region fill), `anchor` (10.5V physical limit).

6. **Fallback shutdown rejected**: Daemon does not call `systemctl poweroff`. That's upsmon's job. Separation of concerns per NUT architecture.

7. **Capacity-based SoH** (not area-under-curve): v2.0 replaced the original voltage-area formula (which produced wrong SoH on partial discharges) with measured_capacity/rated_capacity. Coulomb counting + LUT ΔSoC extrapolation.

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
- **Cliff region accuracy**: Requires deep discharge to measure 10.5-11.0V range. Short blackouts only calibrate upper curve.
- **Peukert scalar**: Exponent is load-independent. Works with consistent ~15-20% load. Would need rework for variable loads.
- **Nominal voltage in current calculation**: ~4% systematic bias in coulomb counting (F14/F27). Consistent direction, doesn't affect convergence.
- **IR compensation during discharge**: Linear model approximate during OB. ≤0.06V error at typical loads (F3/F8).

## Documentation

- `README.md` — Product overview, architecture, quick start, roadmap
- `docs/USER-SCENARIOS.md` — Health report, deep test, battery replacement, config
- `docs/GLOSSARY.md` — Term definitions for all domain concepts
- `docs/archive/` — Completed work: 10 module audits, 7 expert panels, research docs, incident report

## Next: v3.0 — Active Battery Care (Anti-Sulfation)

The daemon currently watches the battery degrade and reports on it. v3.0 makes it fight back:
- **Sulfation model**: temperature-dependent crystal growth rate, desulfation from deep discharges
- **Smart scheduling**: replace fixed monthly deep test timer with daemon-driven decisions based on days since last deep discharge, sulfation score, SoH trend, and natural blackout frequency
- **Cycle ROI metric**: net benefit per discharge (sulfation reversal vs cycle wear)
- **Integration with existing systemd timers**: daemon overrides or skips scheduled deep tests based on battery state

Design captured in GSD todo: "Anti-sulfation deep discharge scheduling for battery longevity"
