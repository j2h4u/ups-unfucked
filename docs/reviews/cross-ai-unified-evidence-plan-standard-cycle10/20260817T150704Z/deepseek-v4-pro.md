# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:59623 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/unified-blackout-recharge-evidence-learning-plan.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Exact standard delta-convergence. Review the current plan against the two attached cycle-9 findings and verify every Critical/High/Medium is closed. Key edits: one explicit DerivedTailBudget total inequality with bounded descriptors; discharge_gap wire repeats UAT origin/intent; CapabilityIdentityPolicy defines automatic re-enablement; segment-ref producer is named; quiescence hashes include paths/sizes; missing initial baseline aborts; recharge-to-blackout cross-reference is a registry transaction; NUT invalid replies coalesce across subreasons with exact size domains; anchor roles map every kind to capture or terminal region. Find any remaining Critical/High/Medium. Return GO only if the plan is internally implementation-ready. Do not re-audit unrelated repository code and do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:59623 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/unified-blackout-recharge-evidence-learning-plan.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-unified-evidence-plan-standard-cycle10/20260817T150704Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Exact standard delta-convergence. Review the current plan against the two attached cycle-9 findings and verify every Critical/High/Medium is closed. Key edits: one explicit DerivedTailBudget total inequality with bounded descriptors; discharge_gap wire repeats UAT origin/intent; CapabilityIdentityPolicy defines automatic re-enablement; segment-ref producer is named; quiescence hashes include paths/sizes; missing initial baseline aborts; recharge-to-blackout cross-reference is a registry transaction; NUT invalid replies coalesce across subreasons with exact size domains; anchor roles map every kind to capture or terminal region. Find any remaining Critical/High/Medium. Return GO only if the plan is internally implementation-ready. Do not re-audit unrelated repository code and do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Delta-convergence review — unified blackout/recharge evidence plan vs cycle-9 findings (deepseek-v4-pro, glm-5-3)

**Verdict: GO at standard level.** No Critical/High/Medium remains. All nine claimed edits are present in the live plan (`docs/plans/unified-blackout-recharge-evidence-learning-plan.md`), and each closes the finding it targets. Premium Cross-AI review (plan §11) remains outstanding and is not satisfied by this verdict.

## Findings

None at Critical/High/Medium.

### Low (residual observations, not blocking)

**L1 — `CapabilityIdentityPolicy` re-enablement is conservative but state-window dependent.** plan:541–545. Re-enablement requires the new 60-reply signature to "exactly match its prior registered signature in every observed state". If the auto-collected 60-reply window happens to be OL-only while the registered signature included OB-scoped fields, the match fails and previously approved capabilities stay raw-only until review — weakening the "routine package upgrade needs no operator" claim (plan:546–547) in a reachable, safe direction. Recommend either fixing the match to "every state observed in both windows, with absence of a previously observed state tolerated as field-level unavailability for a bounded number of consecutive candidate baselines", or accepting the conservative reading explicitly. No safety impact: the failure mode is over-restriction, never over-admission.

**L2 — editorial.** plan:335 glues the aggregate-loss UTC-attribution sentence onto the `continued_by` bullet mid-line. Split into its own bullet when next editing.

## Closure verification

| Finding | Plan evidence | Status |
|---|---|---|
| deepseek M1 — UAT stamp missing on `discharge_gap` wire | plan:330–332 "The durable `discharge_gap` … repeats the exact immutable `observation_origin`/`uat_intent_id`" | Closed |
| deepseek M2 / glm L3 — re-enablement rules undefined | plan:541–545 `CapabilityIdentityPolicy` freeze: identity fields, status-scoped signatures, single re-enablement rule (already-reviewed field + exact signature match), else raw-only until review | Closed (L1 above) |
| deepseek M3 / glm M1 — `DerivedTailBudget` admits 3 MiB reading | plan:191–198 single inequality `derived_total + terminal_link_receipt_outcome_total <= 2 MiB`; derived_total ≤ 128×8 KiB = 1 MiB incl. descriptor batches; ≤256 descriptors × ≤256 bytes bounded; "never an additional 2 MiB"; construction guards + max-size fixtures | Closed |
| deepseek L1 — segment-ref producer unnamed | plan:204–207 refs created only by torn/corrupt/damage quarantine recovery; byte rollover adds none | Closed |
| deepseek L2 — handshake lacks hashed paths | plan:375–377 "exact relative paths, byte sizes and hashes of every v2 registry/receipt/outbox input" | Closed |
| deepseek L3 — missing initial baseline unspecified | plan:530–533 missing/corrupt/mismatched first-install baseline aborts activation; never silently seeds from auto-collection | Closed |
| deepseek L4 — `superseded_by_blackout` dangling ID | plan:271–274 registry-reserved cross-episode transaction: reserve ID → durable new START → recharge END with reserved ID → activate; failure leaves recharge open/censored, no dangling ID, restart replays reserved IDs | Closed |
| deepseek L5 / glm L2 — invalid-reply domains and cross-subreason coalescing | plan:340–348 coalesced "regardless of alternating oversize/malformed subreason"; ≤16 KiB accepted; (16,64] KiB → `codec_oversize`; >64 KiB/incomplete/malformed → `telemetry_reply_lost`; both feed one receipt; 16/20 KiB freeze deferred to Slice-0 live sizes | Closed |
| glm L1 — anchor-kind → region mapping incomplete | plan:184–190 six kinds fixed (`transfer_to_battery`, `raw_firmware_lb` intermediate; `modeled_safe_shutdown`, `power_restored`, `service_stop`, `charge_stabilized` terminal); `boot_boundary`/`gap`/`corruption` deterministic via their explicit `anchor_role` | Closed |

## Limitations

- Bash was restricted to `git diff/status/log/show`; verification used read/grep of the live plan only, which is sufficient for this plan-level delta review.
- Did not re-audit repository code per the delta-convergence scope; the repository-state note from cycle-9 (dirty tree, Slice-0 baseline commit required) remains applicable and is itself the first Slice-0 action.
