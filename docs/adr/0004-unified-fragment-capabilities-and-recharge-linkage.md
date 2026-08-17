# ADR 0004: Unified Fragment Capabilities and Recharge Linkage

**Status:** Accepted, 2026-08-17

## Context

The product remembers each loss of mains and the following recharge so that one user can
understand what happened, what the battery behavior supports, and whether a trend is becoming
dangerous. ADR 0003 made the event JSONL durable and kept safety publication independent from
storage. It also made two narrower choices that no longer describe the product boundary:

- only a `POWER_RESTORED`-terminated event could enter the scientific path; and
- the only product learning surface was the load-sag/IR (`ir_k`) path.

Those choices make a valid prefix of a deep event, a safe-shutdown boundary, a load step, and an
ordinary recharge either disappear from assessment or appear to be one event-wide yes/no result.
They also make arbitrary starting charge look like a global rejection even though it is useful
context for some questions. The UPS still has no independently measured battery current or
temperature. A broader product boundary must therefore mean more honest, question-specific
assessment, not a claim of capacity, State of Health, returned energy, Peukert exponent, or a
complete voltage-to-charge curve.

The next runtime is a fresh v3 authority. It must preserve raw observations and explicit loss
receipts, remain unattended, and make no safety decision wait for assessment or recharge
processing. Optional NUT fields need a reviewed, state-scoped capability baseline rather than
being silently promoted from a fixture or from daemon self-observation.

## Decision

### Supersession boundary

This ADR supersedes only ADR 0003's event-wide `POWER_RESTORED`-only science boundary and its
IR-only product boundary. It does not supersede ADR 0003's safety or storage decisions:

- safety publication remains the first responsibility of every one-second physical poll and is
  independent of persistence, assessment, reporting, and recharge work;
- immutable, append-only JSONL is the raw scientific authority; indexes, reports, and model
  candidates are rebuildable projections, and journald is not scientific input;
- `ModelOwner` is the sole writer for a scientific model change, through a typed `ModelChange`;
  model predictions never become evidence for updating that same model; and
- no SQLite or second runtime persistence model is introduced.

ADR 0003 remains the historical record of the prior candidate wherever it is not contradicted by
this decision. Release-A/v2 evidence remains separately labelled forensic history. It is not
silently reclassified as v3 science.

### Product-first domain language

The domain uses the following terms and does not treat them as claims about a complete
electrochemical cycle:

- `PowerInterruption` (also called a `Blackout`) is the physical loss-of-mains aggregate. It
  starts at the earliest independently observed boundary and ends at restoration, a safe-shutdown
  handoff, or an explicit censored/lost boundary.
- `RechargeEpisode` is the online recovery interval following a `PowerInterruption`. It ends on
  proven charge stabilization, a new interruption, battery-epoch reset, service stop, or an
  explicit gap/capture boundary.
- `CycleWindow` is a read-only linkage between one interruption and its optional recharge
  episode. It is not a physical `BatteryCycle` aggregate and never asserts that a complete
  electrochemical cycle was observed.

Capture preserves canonical raw sample hashes, raw NUT tokens and bounded forensic fields. It
uses small typed fragments rather than a generic evidence or capability framework:

- `DischargeSlice` describes a contiguous, independently assessable on-battery trajectory;
- `LoadStepObservation` describes a stable before/transition/after load and voltage movement;
- `EndpointAnchor` records boundary provenance, such as transfer to battery, raw firmware `LB`,
  restoration, boot, service stop, charge stabilization, modeled safe shutdown, gap, or
  corruption; and
- `RechargeSlice` describes a versioned, linked recharge interval, including its uniform
  backbone and separately tagged transition enrichment.

`EndpointAnchor` is evidence about an observed or published boundary, not a global verdict on the
event. In particular, safe shutdown is a censored safety boundary: it proves when the host was
handed to shutdown protection, not that the battery reached zero. Raw firmware `LB` is retained
as a diagnostic firmware marker and does not directly set virtual `LB`, FSD, or a model update.

An arbitrary starting charge is context. It can change uncertainty, endpoint interpretation, and
which consumer can admit a fragment; it does not globally discard the interruption. A deep
interruption may provide a longer trustworthy prefix while still being censored at shutdown.

### Consumer-specific assessment and capability admission

Each scientific or product consumer owns a typed assessment and its own refusal reasons. Examples
are `LoadSagAssessment`, `CurveAssessment`, `FirmwareLbAssessment`, and
`RechargeBehaviorAssessment`. There is no event-wide scientific yes/no gate and no generic
Capability bus. A fragment may be useful to one consumer and refused by another without changing
the raw record or erasing the other assessment.

Capability means that a named consumer has the required raw input, provenance, state scope,
coverage, quantisation, independence, and policy revision. It is not a claim that the UPS exposes
an absolute physical quantity. Missing optional fields disable only dependent assessments;
unregistered fields remain preserved raw-only.

The Slice-0 read-only capability baseline producer is the authority for optional fields used by
v3 policy freezes. It has these constraints:

1. `scripts/record-telemetry-capability-baseline` connects only to the configured physical NUT
   data socket and issues no UPS or test command.
2. It records exactly 60 consecutive complete ordinary replies, the UPS model/serial, explicit
   firmware presence and value when the device exposes one, and NUT driver identity/version. An
   absent UPS-firmware field is recorded as absent and must remain absent across the window; no
   driver version is relabelled as UPS firmware. Every returned key and original token remains
   available in the bounded raw envelope. Firmware-dependent assessments remain unavailable when
   that explicit identity value is absent; unrelated assessments are unaffected.
3. It records every observed `ups.status` and scopes an optional capability to the states in
   which it was observed. An OL-only run may register recharge-visible fields but cannot claim
   availability while on battery.
4. It atomically writes owner-only `telemetry-capability-baseline-v1.json`. It refuses concurrent
   runs, incomplete replies, identity changes, unsafe ownership/mode, and replacing an existing
   destination without an explicit no-clobber choice.
5. The artifact is derived configuration, not scientific evidence. Saved fixtures are regression
   inputs only and never seed the live manifest. A missing or unstable field is an explicit
   unavailable capability, not an invented value.

Capability signatures exclude dynamic numeric values and retain their parse/precision shape.
For string-valued fields they retain a bounded SHA-256 vocabulary fingerprint, not cleartext in
the signature, so a changed string meaning cannot silently auto-reenable a reviewed capability.

The v3 activation preflight requires that baseline to be present, owner-only, schema-valid, and
identity-matched to the physical UPS/NUT endpoint. Missing, corrupt, or mismatched input fails
closed by aborting activation and directing the operator to run the named read-only producer; it
does not silently seed capabilities from daemon auto-collection. After an already-running v3
instance sees a hardware/driver identity change, dependent typed capabilities become unavailable
while safety and raw capture continue. Read-only re-observation may re-enable an already reviewed
field only after its state-scoped 60-reply signature exactly matches the registered signature.
New fields and semantic/signature changes remain raw-only pending a reviewed policy revision.

### Evidence, linkage, and learning

Every individually recorded interruption that returns online starts at most one idempotent
`RechargeEpisode`, keyed by immutable interruption identity, battery epoch, and the restoration
observation. If only an aggregate/prestart loss is known, the recharge is recorded as an explicit
unlinked censored episode with its loss receipt; it is never silently omitted or assigned to a
made-up blackout. Recharge capture is persistence subsampling of the uninterrupted safety poll:
the dense transition window and event-triggered enrichment are versioned, while the uniform
backbone is the default exchangeable input for trend assessment.

Assessment may use any qualifying typed fragment, including a valid prefix before a gap or the
recharge linked through a censored interruption. Assessment may not turn a partial observation
into measured total runtime, capacity, SoH, returned energy, coulombic efficiency, or a uniquely
identified Peukert exponent. Faster apparent recharge alone is not evidence of lower capacity.
When those quantities are not identifiable, the product reports a bounded diagnostic or an exact
refusal.

The current automatic writable target remains `ir_k`, and only under the existing strict
independent-evidence and safety-oracle gates. This ADR broadens the product's assessable evidence,
not the set of writable model parameters. Any new runtime, capacity, LUT, Peukert, degradation,
or recharge-derived writable target requires a separate identifiability preregistration that
names its raw inputs, independent cohort, uncertainty, meaningful margin, bounds, and no-later-
shutdown oracle. Until that review and proof exist, the result is diagnostic/corroborative only.

### Storage and cutover

V3 writes fresh append-only JSONL authorities under the sole writer lock in the planned blackout
and recharge directories (`events-v3/blackouts/` and `events-v3/recharges/`). Each chain retains
raw records, explicit gaps, typed anchors, and terminal outcomes; projections can be rebuilt from
those chains. The v3 daemon has no v2 reader. Deployment may use a one-shot, transform-only
quiescence handshake for the pre-deployment state, but it never imports v2 raw events into v3
scientific cohorts. Cutover is atomic; there is no mixed v2/v3 reader period.

The safety path has priority over all of this. A storage, baseline, optional-field, assessment,
recharge, report, or model-candidate failure refuses or degrades that non-safety capability and
records the reason; it never delays the one-second safety publication or weakens shutdown
protection.

## Consequences

Ordinary short, partial, deep, restarted, safe-shutdown, and recharge observations can now be
remembered and assessed according to the question they can honestly answer. Valid prefixes and
diagnostic recharge trends remain useful without pretending to be complete battery tests. History
can link one interruption to one recharge while keeping technical segments, gaps, and aggregate
losses explicit.

The product and implementation must carry more typed provenance, policy revisions, and
consumer-specific outcomes. A missing capability or sensor can leave a consumer diagnostic or
refused for months; this is an honest limitation, not an operator task. `ir_k` remains the only
automatic model write until a separate scientific decision proves another target identifiable.

## Rejected alternatives

- **Keep `POWER_RESTORED` as the global science gate:** discards useful censored prefixes and
  makes an infrastructure terminal disposition decide unrelated scientific questions.
- **Replace the IR-only boundary with a generic capability bus:** hides consumer-specific
  assumptions, encourages broad admission, and creates a second architecture authority.
- **Treat safe shutdown as an empty-battery endpoint:** the safety model initiates shutdown before
  zero and the resulting boundary is censored.
- **Infer capacity or SoH from recharge speed or partial voltage:** the current sensors lack the
  independent current, temperature, and endpoint evidence required for that claim.
- **Use fixture or daemon self-observation to seed optional capabilities:** it can silently turn
  unreviewed or state-inappropriate fields into scientific authority; the read-only 60-reply,
  identity-matched baseline is the required gate.
