# Module 7: Event Classification (`src/event_classifier.py`)

**Date:** 2026-03-17
**Panel:** Researcher (NUT/usbhid-ups), QA Engineer, Kaizen Master

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F36 | **"OB LB DISCHRG" not matched** — LB flag breaks exact match. If first OB poll is already LB, entire discharge missed (stays ONLINE). | **High** | Change to flag-based matching: "OB" in status → battery |
| F37 | "keep current state" for unknown status mitigates F36 in common case (mid-discharge LB transition keeps BLACKOUT_REAL) | Info | Mitigating factor, not a fix |
| F36a | **Unrecognized status → false OL in virtual UPS.** When classifier keeps previous state (ONLINE), `compute_ups_status_override` generates "OL". Original `ups.status` (e.g., "OB LB DISCHRG") is excluded from passthrough. upsmon sees "OL" during critical battery discharge. **Worse than missed event — active misinformation.** | **Critical** | Fallback: pass original `ups.status` when classifier returns unknown. Fix together with F36. |
| F38 | FSD, BYPASS, OFF not matched — falls to unknown → keeps state | Low | YAGNI — never observed on CyberPower UT850 |
| F39 | Brownout misclassification: non-issue for line-interactive UPS with AVR | OK | AVR prevents battery switch on brownout |
| F40 | 100V threshold well-chosen for CyberPower (0V blackout vs ~220V test) | OK | No change |

**Design notes:**
- NUT status is space-separated flags, not fixed enum. Exact string matching breaks on flag combinations.
- CyberPower UT850 documented statuses: OL, OL CHRG, OB DISCHRG, OB LB DISCHRG, CAL DISCHRG, FSD
- Fix: `"OB" in status or "CAL" in status → battery`, `"OL" in status → online`. Forward-compatible.
- 100V threshold works because CyberPower reports exactly 0V on blackout and ~220V on test — no ambiguous middle range in practice.

## Fix Status

- [x] **F36** (High): Fixed — flag-based matching: "OB" in status → battery discharge detected
- [x] **F36a** (Critical): Fixed — fallback passes through raw status when classifier returns unknown
- [x] **F37** (Info): No action needed — mitigating factor for F36
- [x] **F38** (Low): ✅ Documented in classify() — YAGNI for CyberPower UT850EG
- [x] **F39** (OK): No action needed — brownout misclassification non-issue for line-interactive UPS
- [x] **F40** (OK): No action needed — 100V threshold well-chosen
