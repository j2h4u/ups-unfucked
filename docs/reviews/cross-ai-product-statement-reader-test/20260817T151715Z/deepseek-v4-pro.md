# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:57295 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test/20260817T151715Z/inputs/product.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh-reader test of PRODUCT.md only. Without relying on repository history, answer: (1) Why does the user hire this product? (2) What are J1-J5? (3) Does every blackout have to change the model? (4) What useful information comes from deep shutdown events and recharge? (5) What exact battery claims are forbidden with current sensors? (6) When and where does degradation warning appear? (7) What history is exact and from when? (8) What happens when storage or analysis fails? (9) How should a future implementation plan use this document? Then identify any Critical/High/Medium ambiguity, contradiction, missing decision rule, or technical anchoring that would make an implementation agent optimize the wrong outcome. Do not inspect repository code and do not edit files. Return GO if the document alone supports correct answers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:57295 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/reviews/cross-ai-product-statement-reader-test/20260817T151715Z/inputs/product.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh-reader test of PRODUCT.md only. Without relying on repository history, answer: (1) Why does the user hire this product? (2) What are J1-J5? (3) Does every blackout have to change the model? (4) What useful information comes from deep shutdown events and recharge? (5) What exact battery claims are forbidden with current sensors? (6) When and where does degradation warning appear? (7) What history is exact and from when? (8) What happens when storage or analysis fails? (9) How should a future implementation plan use this document? Then identify any Critical/High/Medium ambiguity, contradiction, missing decision rule, or technical anchoring that would make an implementation agent optimize the wrong outcome. Do not inspect repository code and do not edit files. Return GO if the document alone supports correct answers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Fresh-reader test answers (PRODUCT.md alone)

1. **Why hire it?** To convert every observed mains interruption + following recharge into durable knowledge about this specific battery, answering three questions: what happened to power and how safely the server responded; how much runtime is likely available now; is the battery persistently getting worse.
2. **J1–J5:** J1 publish conservative runtime/low-battery state per physical poll without waiting for storage/analysis; J2 auto-record every interruption + following recharge as one queryable physical episode or explicit loss receipt (exact history from v3 cutover; older Release-A/v2 stays separately-labelled forensic history); J3 auto-improve prediction from trustworthy observations, every event becomes useful evidence or a precise refusal, never learn from own predictions; J4 distinguish stable vs possible worsening via `scripts/battery-health.py` + MOTD path, requiring ≥6 comparable natural v3 samples for the signal and preregistered trend/uncertainty agreement on worsening; J5 unattended recovery — ordinary failures never stop safety, evidence path recovers or records loss; only deployment/UAT/battery replacement/unrecoverable hardware may need an operator.
3. **Must every blackout change the model?** No — explicitly no model change after every event is promised; even a new writable parameter beyond the currently supported one is not promised.
4. **Deep shutdown + recharge value:** Deep events are valuable long observations, but host shutdown is a censored boundary, not proof of zero battery; recharge is linked to the preceding interruption and used where it adds independent information; partial starting charge is context, not a discard reason.
5. **Forbidden exact claims (current sensors):** amp-hours, returned energy, coulombic efficiency, absolute SoH; a unique Peukert exponent or complete voltage-to-charge curve from partial observations; "faster recharge ⇒ lower capacity"; "host shutdown ⇒ empty battery". Correct output is a bounded diagnostic trend or explicit refusal.
6. **Degradation warning when/where:** When the versioned policy holds ≥6 comparable natural v3 samples for that signal and its preregistered trend/uncertainty rule agrees on worsening — delivered through `scripts/battery-health.py`, same bounded result on the health/MOTD path.
7. **Exact history:** v3 cutover onward is exact; Release-A/v2 evidence is separately labelled forensic history, never silently mixed into exact counts.
8. **Storage/analysis failure:** Safety continues (J1 first priority); event becomes a queryable loss receipt; the path recovers automatically; no routine operator needed.
9. **How plans use it:** Every plan must map slices to J1–J5 and state intentionally deferred jobs; ADRs/plans must name the product job they serve; a green technical gate is not completion while a mapped job or success measure is unmet; conflicts resolve by the Decision Priority ladder.

**Verdict: GO** — the document alone supports correct answers to all nine questions, with the findings below.

# Findings (ordered by severity)

**High — "comparable" and "natural" are undefined (J4).** The entire health-warning gate hinges on "six comparable natural v3 samples," but the authority defines neither term. An implementing agent could define comparability arbitrarily (any two events, any load, any depth) and either manufacture a "possible worsening" verdict or never legitimately reach one. This is the J4 acceptance predicate left without a decision rule.

**High — "v3 cutover" boundary is presupposed but never specified.** J2 makes exact-vs-forensic history hinge on a cutover whose location/date/detection mechanism is absent from this document. A fresh-reader agent cannot determine which events fall on which side, risking exactly the silent mixing J2 forbids. The doc must at least state where the boundary is recorded and that mixing is a hard error, not just a labelling convention.

**Medium — one-second cadence appears only in Success Measures.** J1 says "every physical poll" but never fixes a poll interval; the 1s safety-publication requirement surfaces once, buried in a success measure. An agent optimizing J1 alone could pick any cadence.

**Medium — model-promotion criteria under-specified.** "Independent evidence," "preregistered, practically meaningful margin," and the "no-later-shutdown safety oracle" are named but never defined or bound to a preregistration mechanism. An agent could treat same-cohort holdout as independent or pick a trivially small margin.

**Medium — battery replacement does not fence health cohorts.** J5 says replacement may need an operator, but nothing states that replacement must reset/fence J4 cohorts; the "cannot poison later valid cohorts" success measure covers mixed/invalid evidence, not two different physical batteries' samples.

**Low — time-relative wording in a "stable authority."** "the currently supported one" writable parameter binds the product contract to today's implementation state; "read-only capability registration" and "boot identity" are implementation concepts. Fine as examples, hazardous as normative clauses.

**Low — delivery-path anchoring.** `scripts/battery-health.py` is a specific path in the authority; if the script moves, the product doc must change.

**Low — "diagnostic only" and "bounded" undefined.** The five-way outcome and health output both rely on "diagnostic only"/"bounded," which no section defines; readers must guess the boundary between refusal, diagnostic trend, and warning.

No Critical findings: the document is internally consistent on safety-first priority, evidence immutability, honest limitations, and refusals, and its decision-priority ladder is sufficient to resolve conflicts between jobs.
