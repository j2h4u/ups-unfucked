# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:41543 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/product.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh-reader delta test. Using only current PRODUCT.md and the two prior reader findings, verify that every Critical/High/Medium is closed: comparable/natural definition, immutable v3 cutover boundary, one-second promise, scientific preregistration/holdout/margin, battery replacement epoch fencing, episode-vs-loss rule, six-sample product floor, removal of current-parameter promise, year-scale retention, UAT definition, stable report capability rather than path, and diagnostic/bounded definitions. Re-answer the nine reader questions and flag only remaining Critical/High/Medium ambiguity that could drive the wrong product. Do not inspect code or edit files. Return GO only if PRODUCT.md stands alone.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:41543 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/product.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test-cycle2/20260817T152040Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh-reader delta test. Using only current PRODUCT.md and the two prior reader findings, verify that every Critical/High/Medium is closed: comparable/natural definition, immutable v3 cutover boundary, one-second promise, scientific preregistration/holdout/margin, battery replacement epoch fencing, episode-vs-loss rule, six-sample product floor, removal of current-parameter promise, year-scale retention, UAT definition, stable report capability rather than path, and diagnostic/bounded definitions. Re-answer the nine reader questions and flag only remaining Critical/High/Medium ambiguity that could drive the wrong product. Do not inspect code or edit files. Return GO only if PRODUCT.md stands alone.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Delta verification: prior Critical/High/Medium findings vs current PRODUCT.md

PRODUCT.md is untracked (`?? PRODUCT.md`), so there is no git delta to diff; closure is verified against the current file content (179 lines) directly.

## Prior findings — closure status

| Prior finding (reviewer) | Severity | Status | Evidence in current PRODUCT.md |
|---|---|---|---|
| "comparable"/"natural" undefined (deepseek H1) | High | **Closed** | Lines 65–67 define a comparable natural sample: same-battery-epoch fragment, raw input/status proving natural mains loss vs CAL/self-test, per-signal versioned load/voltage/coverage/gap/independence rules |
| v3 cutover boundary unspecified (deepseek H2) | High | **Closed** | Lines 39–41: recorded by immutable deployment receipt + v3 state epoch; missing/contradictory identity fails the query explicitly |
| One-second cadence only in success measures (deepseek M, glm M5) | Medium | **Closed** | Lines 23–26: stated in J1 itself as a product safety promise, change requires explicit product decision + new end-to-end safety evidence |
| Model-promotion criteria under-specified (deepseek M, glm L1) | Medium | **Closed** | New "Scientific decision authority" (lines 161–168): versioned policy preregisters target, inputs, nuisance assumptions, independent cohort, uncertainty method, margin, update bounds, no-later-shutdown oracle; holdout from different physical episodes + disjoint hashes; margin must exceed sensor quantisation/noise floor |
| Battery replacement doesn't fence J4 cohorts (deepseek M, glm M1) | Medium | **Closed** | Lines 89–92: new battery epoch before evidence admitted, fences all old cohorts and consumed-evidence budgets, requires explicit fresh baseline; old evidence can never enter new-battery learning/degradation cohorts |
| Episode-vs-loss classification rule absent (glm M2) | Medium | **Closed** | Lines 34–37: durable accepted event START → episode; boundary without obtainable START → individual or explicitly counted aggregate loss receipt under versioned capture policy; neither path may silently disappear |
| Six-sample floor ownership tension (glm M3) | Medium | **Closed** | Lines 68–70: six is the product minimum; policy may require more or reject invalid samples, may never use fewer; clean six-sample fixture must classify, not remain unclassified |
| Dangling "currently supported one" writable parameter (deepseek L, glm M4) | Medium | **Closed** | Lines 50–51: no promise of a model change per event *or any particular number of writable parameters* — the anchor is removed |
| Year-scale retention rule (glm L2) | Low | **Closed** | Lines 41–42: v3 raw evidence has no automatic time-based pruning |
| UAT undefined (glm L3) | Low | **Closed** | Lines 88–89: operator-created, durable, time-bounded intent recorded before induced mains loss; evidence remains operational-only |
| Report path anchoring (deepseek L) | Low | **Closed** | Lines 60–62: capability promised, not path — "currently exposed by … a future interface may replace those paths but may not remove the capability" |
| "diagnostic"/"bounded" undefined (deepseek L) | Low | **Closed** | Lines 128–131: diagnostic = read-only, cannot authorize a model write; bounded = inputs, uncertainty, retention identity and claim limited by named versioned policy; distinguished from refusal |

## Remaining findings

**Medium**

- **M1 — UAT evidence can satisfy J4's literal sample test; cohort exclusion is only implied** (`PRODUCT.md:65-67` vs `:88-89`). A UAT blackout is a *real* mains loss, so raw input/status proves "natural loss of mains rather than CAL/self-test" — the mark is what must exclude it, and the only exclusion hook is "remains operational-only." Unlike battery replacement (line 91: "can never enter new-battery learning or degradation cohorts"), UAT exclusion never names cohorts. An implementer could admit induced UAT discharges into the six-sample trend and produce a false `possible worsening` warning, violating the "mixed or invalid evidence cannot poison later valid cohorts" success measure. Fix: mirror the replacement wording for UAT evidence.

**Low**

- **L1 — "conservative remaining-runtime" has direction but no floor** (`:23`). The safety margin magnitude is left entirely to the frozen model plus live acceptance scenarios; acceptable as delegation, but the most safety-critical word in the doc is the least defined.
- **L2 — recharge-to-interruption pairing rule unstated** (`:102`) when several interruptions share one recharge; delegated to the capture policy, fine but unnamed.
- **L3 — "trustworthy observations"** (`:46`) is qualified only indirectly via the scientific-policy preregistration (`:163-168`); delegation is defensible.
- **L4 — "immutable deployment receipt" and "v3 state epoch"** (`:39`) are named but not described; the fail-explicit behavior is specified, so the gap is cosmetic.

**Security/privacy:** nothing new — local-first, no PII, no cloud, marks and receipts local. **Operational:** capability anchoring is now correct.

## The nine reader questions (current doc)

1. **Why hired:** turns every observed interruption + following recharge into durable knowledge about *this* battery, answering the three questions (`:7-17`).
2. **J1–J5:** J1 1 Hz conservative runtime/low-battery publication without waiting for storage (`:21-26`); J2 every interruption + recharge as one queryable episode or explicit loss receipt, exact history from v3 cutover (`:28-42`); J3 automatic use of trustworthy independent observations, every event → evidence or precise refusal, never self-learning (`:44-52`); J4 stable-vs-worsening with ≥6 comparable natural v3 samples and preregistered rule agreement, via health report/MOTD capability (`:54-76`); J5 unattended recovery; only deployment/UAT/replacement/unrecoverable hardware may need an operator (`:78-92`).
3. **Must every blackout change the model?** No — explicitly not promised, nor any particular number of writable parameters (`:50-51`).
4. **Deep shutdown + recharge value:** deep events are valuable long observations but host shutdown is a censored boundary, not proof of zero (`:99-101`); recharge is linked to the preceding interruption and used where it adds independent information (`:102-103`); partial starting charge is context, not a discard reason (`:98`).
5. **Forbidden claims:** exact amp-hours, returned energy, coulombic efficiency, absolute SoH, unique Peukert exponent, complete voltage-to-charge curve from partial data, faster-recharge⇒lower-capacity, shutdown⇒empty, model update merely for a long blackout, intentional deep discharge as routine learning (`:116-123`); non-identifiable ⇒ bounded diagnostic trend or explicit refusal (`:125-126`).
6. **Warning when/where:** when ≥6 comparable natural samples exist for that signal and the preregistered trend/uncertainty rule agrees on worsening; via the battery-health report capability (currently `scripts/battery-health.py` + MOTD path) (`:60-72`); may honestly stay `insufficient` for months (`:74-76`).
7. **Exact history:** from the v3 cutover recorded by the deployment receipt + state epoch; Release-A/v2 stays separately labelled forensic history; fail-explicit on identity mismatch; no time-based pruning (`:39-42`).
8. **Storage/analysis failure:** safety publication continues, evidence path recovers or records its loss automatically, no routine operator (`:80-92`, `:143-144`).
9. **Plan usage:** map every slice to J1–J5, state deferred jobs, never silently weaken the contract, resolve conflicts by the decision-priority ladder, green gate ≠ product completion (`:3-5`, `:146-155`, `:178-179`).

## Verdict

**GO** — all twelve prior Critical/High/Medium items are closed, and PRODUCT.md alone supports correct answers to all nine questions. The single remaining Medium (M1, UAT cohort exclusion) should be tightened in the next revision but does not flip any of the nine answers or the priority ladder.
