# Module 1: Signal Processing (`src/ema_filter.py`)

**Date:** 2026-03-17
**Panel:** Researcher (DSP), System Architect, QA Engineer, Kaizen Master

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F1 | Adaptive alpha amplifies ADC quantization oscillation ~3x (0.06V band at 13.5V) | Low | Document only — <1% SoC impact |
| F2 | `abs(ema) < 1e-6` guard: load near zero → alpha=1.0, no smoothing | Low | Document only — server load 14-20%, never near zero |
| F3 | IR compensation applied during OB discharge — linear model invalid for electrochemical discharge | Medium | **Defer to Module 2** — need to measure SoC impact |
| F4 | First sample seeds EMA directly — stale NUT reading could bias EMA for full 120s window | Medium | Verify NUT behavior on reconnect |
| F5 | sensitivity=0.05 well-calibrated: 0.1V/13.5V=0.74% << 5% threshold | OK | Rationale documented |
| F6 | Time-based stabilization correct | OK | Fixed this session |

**Design notes:**
- Adaptive alpha is correct and well-calibrated for the ADC/voltage range
- During OB discharge, adaptive alpha correctly fast-tracks monotonically dropping voltage (this is intended, not a bug)
- NUT usbhid-ups pollinterval=2s — daemon's 10s poll always gets fresh data
- sensitivity=0.05 hardcoded is fine (YAGNI to make configurable)
