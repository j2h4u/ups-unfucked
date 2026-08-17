# Slice 0 live capability receipt

**Status:** the read-only physical-UPS capability baseline is recorded and verified. The
implementation is committed at `5ac571fad9819f1b26981e2d1a110c25da301ff9`. Final conservative
adjudication is **GO for Critical/High/Medium** findings. This is not deployed; v3 activation
remains **NO-GO** until its composition root enforces the baseline preflight and Slices 1-4 are
implemented and reviewed.

## Implementation and verification

- Final exact-tree `just check`: `933 passed in 66.39s`; CRAP maximum `29.67`, with zero functions
  at or above `30`.
- Ruff and complexity, source-span, six kept Import-Linter contracts, exact Tach, Pyright, and
  Vulture gates: green.
- `git diff --check`: green.
- Read-only live verification: green.

The standard CrossAI convergence output is
`docs/reviews/cross-ai-slice0-capability-standard-convergence/20260817T180712Z`. GLM returned GO.
DeepSeek's M1 claim about `StateSignature.field` and Vulture was disproved by the exact gate; the
unused helper was then removed. All actionable Lows are closed. The intentional lock-file and
strict quote behavior remain retained. The final conservative adjudication is GO for Critical,
High, and Medium findings. No premium review is claimed.

## Live collection

- Date: `2026-08-17` (`Asia/Almaty`).
- Configured physical NUT endpoint: `localhost:3493/cyberpower`.
- UPS state during the accepted window: `OL` only.
- Ordinary complete `LIST VAR` replies: `60` at one-second cadence.
- Raw keys observed: `48`.
- UPS commands or tests issued: none.
- Running v2 service changed or restarted: no.

The first collection attempt safely refused before artifact publication because this UPS exposes
no `ups.firmware` or `device.firmware` field. The pre-release identity contract was corrected to
record firmware presence/value explicitly, with `null` meaning unavailable. It never substitutes
`driver.version.data` or another driver value for UPS firmware. Targeted regressions prove that a
later firmware appearance or disappearance is an identity mismatch.

## Artifact verification

- Path: `~/.config/ups-battery-monitor/telemetry-capability-baseline-v1.json`.
- Artifact SHA-256: `2ac619188ed4cb28cff38a68df9b432f71ebee90dd2b32a3404acee53ae59da0`.
- Size: `101822` bytes.
- Owner UID: `1000`; file mode: `0600`; parent mode: `0700`.
- Producer script mode: `0755`; adjacent temporary files: zero.
- Canonical JSON and required final newline: verified.
- Configured endpoint and one fresh ordinary-reply identity match: verified.
- Explicit UPS firmware availability: unavailable (`null`).

The string semantic signature uses bounded SHA-256 vocabulary fingerprints; numeric values remain
excluded.

Serial and raw telemetry values are deliberately absent from this repository receipt. The private
live artifact is derived configuration, not scientific evidence, and is not committed.

## Remaining activation boundary

This receipt completes the one-time live input needed by the future v3 capability manifest. It
does not authorize v3 deployment by itself, and no deployment was performed for this receipt.
Before v3 activation, the new composition root must load this artifact, validate
owner/schema/endpoint/current identity, seed only reviewed typed capabilities, and fail closed for
typed capability activation while leaving safety and raw capture available. Deployed v2 remains
unchanged.
