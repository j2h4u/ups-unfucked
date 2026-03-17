# Module 9: Orchestration (`src/monitor.py`)

**Date:** 2026-03-17
**Panel:** System Architect, QA Engineer, Kaizen Master

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F47 | Call ordering correct: _handle_event_transition → _compute_metrics ensures fresh params on OB→OL | OK | No change |
| F48 | previous_event_type lags one poll — correct for transition detection | OK | No change |
| F49 | 60s cooldown handles rapid flicker: buffer accumulates, clears after 60s confirmed OL | OK | No change |
| F50 | cycle_count counts OL→OB transitions (inc. flicker), not discharge events. Matches spec. Wear proxy = cumulative_on_battery_sec. | Low | Document distinction |
| F51 | Sag measurement abandoned properly on OB→OL mid-measurement | OK | No change |
| F52 | Exception in _update_battery_health → skips watchdog → daemon restart. Acceptable. | Info | No change |

**Design notes:**
- Pipeline is well-ordered: EMA → classify → sag → discharge → event transition → metrics → export
- Cooldown design correctly treats OB→OL→OB within 60s as single discharge event
- cycle_count = transitions (for enterprise "transfer count"), cumulative_on_battery_sec = actual wear metric

## Fix Status

- [x] **F47** (OK): No action needed
- [x] **F48** (OK): No action needed
- [x] **F49** (OK): No action needed
- [ ] **F50** (Low): Open — document distinction
- [x] **F51** (OK): No action needed
- [x] **F52** (Info): No action needed — acceptable behavior
