---
created: "2026-03-16T22:24:47.432Z"
title: Reduce model.json save frequency to event-driven only
area: general
files:
  - src/monitor.py
  - src/model.py
---

## Problem

Daemon loads model.json into memory at startup and overwrites it on every `_safe_save()` call — which happens on every voltage sag, every calibration point batch, and every discharge event. This means:

1. **External edits are silently lost** — manually fixing model.json while daemon is running is useless; daemon overwrites with its in-memory (possibly corrupted) state on next save. Real incident: reset SoH/Peukert in model.json, daemon immediately re-corrupted the file.
2. **Unnecessary SSD writes** — `_safe_save` with fdatasync on every sag measurement is excessive for a 10-second poll loop.
3. **No safe window for external tools** — MOTD scripts, battery-health.py, or operator cannot modify model.json without stop/edit/start dance.

Current `_safe_save` call sites in monitor.py:
- `_record_voltage_sag()` — every OL→OB transition sag measurement
- `_auto_calibrate_peukert()` — removed redundant save in RLS refactor, but parent `_update_battery_health` still saves
- `_handle_event_transition()` — after `_update_battery_health` + cliff region update
- `_handle_discharge_complete()` — via `battery_model.add_capacity_estimate()` which calls `save()` internally
- `_reset_battery_baseline()` — on battery replacement
- `_signal_handler()` — on SIGTERM/SIGINT (this one is correct)
- `calibration_batch_flush()` — every REPORTING_INTERVAL during discharge

## Solution

**"Memory is source of truth" model — save only on real events:**

1. **Keep saves for:** OB→OL discharge complete (`_update_battery_health`), battery replacement (`_reset_battery_baseline`), graceful shutdown (`_signal_handler`), capacity convergence events.
2. **Remove saves from:** `_record_voltage_sag` (sag data is ephemeral until discharge completes), individual calibration flushes during discharge (batch at end).
3. **Document the model** in AGENTS.md or project docs: "model.json is written by daemon on discharge events and shutdown. Between events, file can be safely edited. Daemon restart picks up changes."
4. **Consider**: `model.json` reload-on-SIGHUP for live config updates without restart.
