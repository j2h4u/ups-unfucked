# Product statement

This document is the stable product authority for UPS Battery Monitor. It explains why the product exists and
what outcomes it must deliver. Roadmaps, plans, ADRs and implementation details may change; they must state
which product job they serve and may not silently weaken this contract.

## Why this product exists

The product turns every observed mains interruption and the following recharge into durable knowledge about
this particular UPS battery. It helps one user answer three practical questions:

1. What happened to power and how safely did the server respond?
2. How much useful runtime is likely available now?
3. Is the battery's behavior persistently getting worse?

It must answer as accurately as the UPS's real sensors permit, improve automatically when independent evidence
supports an improvement, and say clearly when the available evidence cannot support a conclusion.

## Jobs to be done

### J1 — Protect the server during a blackout

When mains power fails, I want the service to publish a conservative remaining-runtime and low-battery state
once per second without waiting for storage or analysis, so the existing shutdown system can stop the server
safely. The one-second cadence is a product safety promise; changing it requires an explicit product decision
and new end-to-end safety evidence.

### J2 — Remember every power event

When any interruption occurs — brief, partial, long, restarted, or ending in safe shutdown — I want it and the
following recharge recorded automatically, so I can ask what happened over a day, month or year without
Grafana, cloud archives or manual reconstruction.

An independently observed event must become either one queryable physical episode or an explicit queryable
loss receipt. A durable accepted event START creates an episode; an observed boundary that cannot obtain such
a START is represented by an individual or explicitly counted aggregate loss receipt under the versioned
capture policy. Neither path may silently disappear.

Exact query history begins at the v3 cutover recorded by the immutable deployment receipt and v3 state epoch.
Older Release-A/v2 evidence remains separately labelled forensic history. Missing or contradictory cutover
identity makes a query fail explicitly; histories are never silently mixed. V3 raw evidence has no automatic
time-based pruning, so year-scale queries remain available while the underlying state is retained.

### J3 — Improve the prediction from real experience

When trustworthy observations add independent information about this battery, I want the service to use them
automatically, so its remaining-runtime prediction becomes more accurate whenever the evidence honestly
permits a bounded improvement.

Every event must be processed automatically into either useful evidence or a precise refusal. The product does
not promise a model change after every event or any particular number of writable parameters. It promises
never to manufacture learning from its own predictions.

### J4 — Warn before battery behavior becomes dangerous

When enough comparable discharge or recharge evidence accumulates, I want the service to distinguish stable
behavior from a persistent possible decline, so I can investigate or replace the battery before the change
causes an unexpected outage.

The warning is delivered through the product's battery-health report, currently exposed by
`scripts/battery-health.py` and the normal health/MOTD path. A future interface may replace those paths but may
not remove the capability. For each supported signal it reports one of:
`insufficient comparable evidence`, `stable within observed evidence`, or `possible worsening`.

For this product, a comparable natural sample is a same-battery-epoch fragment whose raw input/status proves a
natural loss of mains rather than CAL/self-test, and which satisfies that signal's versioned load, voltage,
coverage, gap and independence rules. "Persistent" requires at least six such samples for that specific
signal and agreement of its preregistered trend/uncertainty rule on the worsening direction. Six is the product
minimum; a versioned scientific policy may require more or reject otherwise invalid samples, but may never use
fewer. The clean six-sample reference fixture must produce stable or possible-worsening rather than remain
unclassified. A warning is evidence for investigation, not a claim of exact remaining capacity or a unique
diagnosis.

Because v3 cohorts deliberately start empty, this job may remain `insufficient comparable evidence` for
months after cutover. That honest dormancy is preferable to importing incompatible history or inventing a
conclusion.

### J5 — Run without babysitting

When capture, storage, assessment or reporting encounters an ordinary failure, I want safety to continue and
the evidence path to recover or record its loss automatically, so routine operation does not depend on an
operator, another agent, or a rare manually induced blackout.

One-time deployment, an explicitly marked UAT, battery replacement and genuinely unrecoverable hardware or
disk failure may require an operator. Ordinary learning, refusal, restart recovery, projection rebuild and
capability re-observation may not.

An explicitly marked UAT is an operator-created, durable, time-bounded intent recorded before induced mains
loss; its blackout and recharge evidence remains operational-only and can never enter automatic learning,
runtime-model validation, or degradation cohorts. Battery replacement creates a new battery
epoch before new evidence is admitted, fences all old cohorts and consumed-evidence budgets, and requires an
explicit fresh model baseline. Old evidence remains readable history but can never enter new-battery learning
or degradation cohorts.

## Product promises

- Safety publication remains the first responsibility of every physical poll.
- Every completed event gets a plain-language outcome: what was recorded, what was usable, what changed, what
  became diagnostic only, and why anything was refused.
- Partial starting charge is context, not an automatic reason to discard an event.
- Deep events are valuable long observations, but host shutdown is a censored safety boundary, not proof that
  the battery reached zero.
- Recharge behavior is linked to the preceding interruption and used automatically where it adds independent
  information.
- A candidate model change must beat the frozen deployed model on independent evidence by a preregistered,
  practically meaningful margin and must never make a shutdown decision later.
- Raw physical evidence is immutable; model outputs never become evidence for updating the same model.
- Routine operator work is zero, and optional scientific failure never weakens the safety path.

## Honest limitations

With the current UPS, the service observes status, battery voltage and its raw token/quantisation, UPS load
percentage, input voltage, time and boot identity. Optional vendor fields are usable only after read-only
capability registration. There is no battery-temperature sensor and currently no independently measured
battery current or returned energy.

Therefore the product does not promise:

- exact amp-hours, returned energy, coulombic efficiency or absolute state of health;
- a unique Peukert exponent or complete voltage-to-charge curve from partial observations;
- that faster recharge alone means lower capacity;
- that host shutdown means an empty battery;
- a model update merely because a blackout was long;
- intentional deep discharge as a routine learning mechanism.

When these quantities are not identifiable, the correct result is a bounded diagnostic trend or an explicit
refusal — not a plausible-looking number.

Here, *diagnostic* means a read-only observation that cannot authorize a model write, and *bounded* means its
inputs, uncertainty, retention identity and user-facing claim are limited by a named versioned policy. It is
distinct from refusal: a diagnostic may describe a supported trend, while a refusal explains why a requested
scientific conclusion was not identifiable.

## Success measures

- Deterministic and live acceptance scenarios produce exact v3 event counts, boundaries, shutdown facts and
  explicit loss counts.
- Every terminal episode automatically produces the five-way plain-language outcome described above.
- Independent holdout evaluation proves every promoted model change improves its named prediction target and
  preserves the no-later-shutdown safety oracle.
- At exactly the policy's minimum comparable cohort, health output deterministically distinguishes
  insufficient, stable and possibly worsening behavior; mixed or invalid evidence cannot poison later valid
  cohorts.
- Restart, temporary storage failure, missing optional fields, unusable evidence and reporting failure do not
  stop one-second safety publication or require routine intervention.

## Decision priority

When goals conflict, choose in this order:

1. Physical shutdown safety.
2. Immutable and scientifically honest evidence.
3. Unattended recovery.
4. Predictive value.
5. Useful history and diagnostics.
6. Implementation convenience.

DDD/SOLID/DRY, JSONL, policies, ports, file budgets and individual model parameters are implementation
constraints or current design choices, not the reason the product exists. Prefer a simpler design only when it
preserves every higher-priority outcome and its verification.

## Scientific decision authority

The versioned scientific-policy artifact preregisters each writable target, required raw inputs, nuisance
assumptions, independent cohort, uncertainty method, practically meaningful improvement margin, update bounds
and no-later-shutdown oracle before candidate evaluation. Holdout evidence must come from different physical
episodes and disjoint immutable evidence hashes; the candidate event or model-generated values cannot serve
as their own validation. The margin must exceed the registered sensor quantisation/noise floor. If any field
is absent, the candidate remains diagnostic or refused.

## Relationship to delivery documents

- `README.md` describes the currently deployed product behavior.
- `PRODUCT.md` is the product intent and acceptance authority.
- `docs/adr/` records durable architectural decisions and their rationale.
- `docs/plans/` describes how a particular change will satisfy named product jobs.
- `docs/OPERATIONS-RUNBOOK.md` describes operator procedures and live acceptance.

Every implementation plan must map its slices to J1–J5 and state any product job it intentionally defers. A
green technical gate is not product completion when a mapped job or success measure remains unmet.
