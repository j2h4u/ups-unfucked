# ups-unfucked — Current Context

This document provides essential context for expert reviewers, panels, and new contributors. Updated 2026-03-14 (post v1.1).

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
    ↓ SoH tracking → Replacement prediction → R_internal measurement
    ↓ Enterprise counters (cycle count, cumulative on-battery time, install date)
    ↓ Per-poll writes during OB state (no 60s lag on LB flag)
    ↓
/dev/shm/ups-virtual.dev (atomic tmpfs write, fdatasync)
    ↓
NUT dummy-ups → upsd → upsmon (shutdown) / Grafana (dashboards) / MOTD
    ↑
health.json (last_poll, SoC, online, version — for external monitoring)
```

**Key principle**: Daemon is a **data source**, not a decision maker. Shutdown logic belongs to upsmon. Daemon publishes corrected metrics and LB flag through the virtual UPS.

## What The Daemon Computes (vs firmware)

| Metric | Firmware | Daemon | Method |
|--------|----------|--------|--------|
| Charge % | Coulomb counter (drifts, ±50% error) | Voltage→SoC lookup table | LUT with linear interpolation, IR-compensated |
| Runtime | ~22 min reported, actual ~47 min | Peukert model (±10%) | Physics-based, load-dependent, SoH-adjusted |
| SoH | Not available | Discharge curve area analysis | Trapezoidal integration, compared to reference |
| Replacement due date | Not available | Linear regression on SoH history | Persisted in model.json, exported to virtual UPS |
| Internal resistance | Not available | Voltage sag measurement (dV/dI) | On every OL→OB transition |
| Cycle count | Not available | OL→OB transition counter | Persisted in model.json |
| Cumulative runtime | Not available | Sum of discharge durations | Persisted in model.json |
| Battery age | Not available | Install date tracking | Set on first startup |

## Self-Calibration

The daemon learns the battery automatically:
- **Every blackout** (even 1-2 min): writes measured voltage→SoC points to LUT, gradually replacing the standard VRLA curve
- **Time-weighted averaging**: recent measurements dominate (CURVE_RELEVANCE_HALF_LIFE_DAYS=90), tracking battery aging
- **Cliff region** (10.5-11.0V): interpolated when ≥2 measured points exist there (requires deep discharge)
- **Peukert exponent**: auto-calibrated when predicted vs actual runtime diverges >10%
- **No special mode needed**: all calibration is continuous and automatic

## Key Technical Decisions

1. **Adaptive EMA** (not fixed alpha): Dynamic alpha scales with input deviation — instant reaction to power events, smooth filtering of sensor noise. Inspired by DynamicAdaptiveFilterV2 (Arduino).

2. **LIST VAR single connection**: One TCP connection per poll instead of 6. Wall-clock deadline + 64KB buffer cap prevent hangs.

3. **TOML config** (not env vars): Only 3 user-facing settings (ups_name, shutdown_minutes, soh_alert). Everything else is hardcoded or in model.json.

4. **Systemd integration**: Type=notify with WatchdogSec=120, JournalHandler for logging, ProtectSystem=strict hardening.

5. **LUT point sources**: `standard` (datasheet), `measured` (real discharge), `interpolated` (cliff region fill), `anchor` (10.5V physical limit).

6. **Fallback shutdown rejected**: Daemon does not call `systemctl poweroff`. That's upsmon's job. Separation of concerns per NUT architecture.

## Codebase

- **~6,600 LoC** across 12 modules, **208 tests**
- Key files: `src/monitor.py` (daemon, Config/CurrentMetrics frozen dataclasses, _safe_save helper), `src/nut_client.py` (LIST VAR, single TCP connection), `src/ema_filter.py` (MetricEMA generic class + adaptive EMA + IR compensation), `src/model.py` (battery model persistence, history/LUT pruning), `src/soh_calculator.py` (SoH + time-weighted cliff interpolation)
- Config: `config.toml` (3 settings), `model.json` (battery state, auto-calibrated, pruned)
- Scripts: `scripts/battery-health.py` (health report), `scripts/install.sh` (product installer)

## Known Limitations

- **No temperature sensor**: ±3% SoC uncertainty from temperature. $2 NTC thermistor is highest-ROI hardware improvement.
- **CyberPower doesn't expose temperature**: No `battery.temperature` or `ups.temperature` via NUT.
- **Cliff region accuracy**: Requires deep discharge to measure 10.5-11.0V range. Short blackouts only calibrate upper curve.
- **Peukert scalar**: Exponent is load-independent. Works with consistent ~15-20% load. Would need rework for variable loads.

## Documentation

- `README.md` — Product overview, architecture, quick start
- `docs/USER-SCENARIOS.md` — Health report, deep test, battery replacement (planned), config
- `docs/EXPERT-PANEL-REVIEW-2026-03-14.md` — First expert review (architect, security, SRE, QA, Kaizen, statistician, battery chemist)
- `docs/EXPERT-PANEL-REVIEW-2026-03-15.md` — Second expert review (5-panel). **All 21 findings fixed in v1.1** (2 P0 + 7 P1 + 7 P2 + 5 P3)
- `docs/EXPERT-PANEL-REVIEW-2026-03-14-post-v1.1.md` — Third expert review (post v1.1). **All 20 findings fixed** (0 P0 + 5 P1 + 9 P2 + 6 P3)
- `docs/GLOSSARY.md` — Term definitions

## Open Work

- **GSD todos**: Battery replacement scenario (docs + implementation), install.sh system integration gaps (product vs site-specific split)
- **v2 candidates**: Automatic IR coefficient estimation (CAL2-01), Peukert exponent refinement (CAL2-02), Grafana metrics export (MON-01)
- **Research docs**: Enterprise UPS metrics analysis (RESEARCH-*.md) — identified 7 of 9 enterprise metrics as estimable
