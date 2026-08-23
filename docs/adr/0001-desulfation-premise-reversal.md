# ADR 0001: Do not treat discharge as desulfation

**Status:** Accepted, 2026-06-03; implementation updated 2026-08-23

## Context

An earlier version treated scheduled deep discharge as active battery desulfation. That premise is
wrong for this lead-acid UPS: discharge forms more lead sulfate and adds cycle wear; reversal occurs
during charging. The CyberPower charger controls charging internally, and NUT exposes no usable
charge-voltage or equalization control to this daemon.

## Decision

- Do not implement sulfation scores, cycle-ROI estimates, therapeutic cycling, or autonomous deep
  discharge.
- The only automatic UPS command is a guarded quick self-test. It may run when the UPS is online at
  100% and no blackout, calibration, or self-test has occurred for 14 days.
- Treat a quick self-test as operational evidence only. It does not establish capacity, state of
  health, or future runtime.
- Charge-side battery maintenance remains outside the product boundary.

## Consequences

The service stays honest and small: it observes blackouts and recharge, may perform a low-wear
operational check, and never claims to repair the battery. Supporting a real charge-side maintenance
strategy would require different hardware with an exposed, trustworthy control surface.
