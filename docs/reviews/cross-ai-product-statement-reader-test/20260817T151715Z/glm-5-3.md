# Cross-AI Result: glm-5-3

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:57295 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test/20260817T151715Z/inputs/product.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Fresh-reader test of PRODUCT.md only. Without relying on repository history, answer: (1) Why does the user hire this product? (2) What are J1-J5? (3) Does every blackout have to change the model? (4) What useful information comes from deep shutdown events and recharge? (5) What exact battery claims are forbidden with current sensors? (6) When and where does degradation warning appear? (7) What history is exact and from when? (8) What happens when storage or analysis fails? (9) How should a future implementation plan use this document? Then identify any Critical/High/Medium ambiguity, contradiction, missing decision rule, or technical anchoring that would make an implementation agent optimize the wrong outcome. Do not inspect repository code and do not edit files. Return GO if the document alone supports correct answers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:57295 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test/20260817T151715Z/inputs/product.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Fresh-reader test of PRODUCT.md only. Without relying on repository history, answer: (1) Why does the user hire this product? (2) What are J1-J5? (3) Does every blackout have to change the model? (4) What useful information comes from deep shutdown events and recharge? (5) What exact battery claims are forbidden with current sensors? (6) When and where does degradation warning appear? (7) What history is exact and from when? (8) What happens when storage or analysis fails? (9) How should a future implementation plan use this document? Then identify any Critical/High/Medium ambiguity, contradiction, missing decision rule, or technical anchoring that would make an implementation agent optimize the wrong outcome. Do not inspect repository code and do not edit files. Return GO if the document alone supports correct answers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Fresh-reader test: PRODUCT.md

Note: the canonical file is `PRODUCT.md` (the lowercase `product.md` path failed; a snapshot copy exists under `docs/reviews/.../inputs/`). Content of `PRODUCT.md` matches the text under review. Per instructions, no repository code was inspected.

## Findings (ordered by severity)

**Critical:** none.
**High:** none.

**Medium**

- **M1 — Battery-replacement evidence rule missing** (`PRODUCT.md:71`). Replacement "may require an operator," but no rule says what must happen to evidence integrity (cohort split, battery identity, model invalidation). An implementation could mix old-battery samples into new-battery J4 trends and still claim compliance, since "comparable" is delegated to policy. Biggest wrong-outcome risk in the doc.
- **M2 — Episode vs loss-receipt classification rule absent** (`PRODUCT.md:32-34`). The dichotomy is promised, but no rule (or explicit delegation) decides which side an event lands on — e.g., an interruption observed only via boot-identity gap after host death. Yet success measure `:111` demands *exact* event counts and loss counts, making this boundary acceptance-relevant, not implementation trivia.
- **M3 — Internal tension in J4's threshold** (`PRODUCT.md:56-58`). The doc states "at least six" then says the exact scientific threshold "belongs to the versioned domain policy, not this product document." Readable as "six is the product floor," but an agent may hard-code six or treat it as non-binding. Needs one clarifying clause.
- **M4 — Dangling reference: "the currently supported one"** (`PRODUCT.md:43-44`). The one writable parameter is never named; from this document alone the promise is unverifiable and an agent adding a new writable knob cannot know it violates the contract.
- **M5 — "one-second" anchored only in a success measure** (`PRODUCT.md:120`). J1/promises say "every physical poll" (`:77`) with no cadence; the 1 s figure appears once as a gate. Unclear whether it's a product promise or current implementation value; a legitimate cadence change would silently fail the measure.

**Low**

- **L1** — J3's "preregistered, practically meaningful margin" (`:85-86`) and "named prediction target" (`:114`) don't say where preregistration/naming lives; J4 delegates explicitly, J3 doesn't.
- **L2** — "day, month or year" (`:29`) implies ≥1-year queryability but no retention rule or success measure; aggressive pruning would silently weaken J2.
- **L3** — "an explicitly marked UAT" (`:71`) is undefined from the document alone.

**Security/privacy:** nothing to flag — local-first, no cloud, no personal data; sensor/evidence scope is explicitly bounded.

## Answers (from the document alone)

1. **Why hired:** turns every observed mains interruption + recharge into durable knowledge about *this particular* UPS battery, answering: what happened to power / how safely the server responded; how much runtime is likely now; is behavior persistently worsening (`:9-17`).
2. **J1–J5:** J1 conservative runtime/low-battery publication ahead of storage/analysis for safe shutdown (`:21-24`); J2 every interruption + recharge recorded as one queryable episode or explicit loss receipt (`:26-34`); J3 automatic use of trustworthy independent observations, every event → evidence or precise refusal, never self-learning from own predictions (`:36-44`); J4 distinguish stable vs persistent possible decline, reported as insufficient / stable / possible worsening (`:46-63`); J5 ordinary failures self-recover or record loss; routine operator work is zero (`:65-73`).
3. **No.** "The product does not promise a model change after every event" (`:42-44`); promotion additionally requires beating the frozen model by a preregistered margin and never making shutdown later (`:85-86`).
4. **Deep events:** valuable long observations, but host shutdown is a *censored safety boundary*, not proof of empty battery (`:81-82`, `:102`). **Recharge:** linked to the preceding interruption and used where it adds independent information (`:83-84`); faster recharge alone ≠ lower capacity (`:101`).
5. **Forbidden claims** (`:99-104`): exact amp-hours, returned energy, coulombic efficiency, absolute SoH; unique Peukert exponent or complete voltage-to-charge curve from partial data; faster-recharge→lower-capacity; shutdown=empty; model update merely because a blackout was long; intentional deep discharge as routine learning. Non-identifiable quantities → bounded trend or explicit refusal, never a plausible-looking number (`:106-107`).
6. **Warning channel:** `scripts/battery-health.py` plus the same result on the health/MOTD path (`:52-54`); triggers only when versioned policy has ≥6 comparable natural v3 samples per signal and the preregistered rule agrees on worsening (`:56-58`); may honestly stay "insufficient" for months post-cutover (`:61-63`).
7. **Exact history:** exact query history begins at the v3 cutover; Release-A/v2 evidence stays separately labelled forensic history, never mixed into exact counts (`:33-34`).
8. **On storage/analysis failure:** safety publication continues, the evidence path recovers or records its loss automatically, no routine operator needed (`:67-73`, `:119-120`); scientific failure never weakens safety (`:88`).
9. **Plan usage:** map slices to J1–J5, state deferred jobs explicitly, never silently weaken the contract (`:4-5`, `:145-146`); resolve conflicts via the decision priority (`:124-131`); a green technical gate ≠ product completion.

## Verdict

**GO** — the document alone yields correct answers to all nine questions, with clear anti-goal guardrails (censoring, no self-learning, no exact-capacity claims) and an explicit decision priority. The Medium findings are clarification gaps (replacement handling, episode/loss boundary, the "six" ownership, two dangling references, the 1 s anchor) that should be fixed in a future doc revision but do not block an implementation agent that maps work to J1–J5 and defers thresholds to versioned policy.
