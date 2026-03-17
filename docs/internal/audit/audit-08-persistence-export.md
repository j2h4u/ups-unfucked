# Module 8: Persistence & Export (`src/model.py` + `src/virtual_ups.py`)

**Date:** 2026-03-17
**Panel:** Sys Admin (filesystem), QA Engineer, Kaizen Master

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F41 | **BLACKOUT_TEST suppresses LB unconditionally** — deep test can drain battery to hardware cutoff without graceful shutdown | **High** | Add hard LB floor: runtime < 2 min → LB regardless of event type |
| F42 | Virtual UPS write logs at INFO every 10s — 8640 lines/day of zero-info spam | Medium | Change to DEBUG |
| F43 | fdatasync on ext4 data=ordered: safe, dir fsync not needed | OK | No change |
| F44 | fdatasync on tmpfs: no-op, negligible overhead, correct defensive coding | OK | No change |
| F45 | Prune limits adequate: 30 SoH ≈ 15-30 months, 200 LUT ≈ 7 weeks | OK | No change |
| F46 | model.json 3.8KB — tiny, indent=2 fine | OK | No change |
