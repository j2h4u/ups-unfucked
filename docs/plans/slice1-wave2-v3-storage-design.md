# Slice 1 Wave 2 v3 blackout storage design

**Status:** implementation-ready design; the approved direction is a fresh v3 authority with no v2
reader, migration, mutation, import, or reverse projection. This document authorizes implementation design,
not deployment.

**Authority:** `PRODUCT.md`, ADR 0004, and
`docs/plans/unified-blackout-recharge-evidence-learning-plan.md`. Wave 1 commit `687bfc6` supplies the
domain records, application ports and values, schema-3 canonical envelope, physical codecs, terminal/tail
codecs, and byte budgets reused here.

## 1. Outcome and scope

Wave 2 makes a durably accepted v3 blackout START survive process or host death and makes a sealed blackout
queryable without treating a projection as scientific evidence. It implements the private filesystem side of
the Wave 1 ports:

- `BlackoutCaptureStorePort`;
- `BlackoutEvidencePort`;
- `BlackoutTailStorePort`;
- `BlackoutHistoryPort`; and
- `BlackoutModelCapturePort`.

The result serves these product jobs:

- **J1 — Protect:** all storage calls remain behind safety publication; storage never owns shutdown.
- **J2 — Remember:** an accepted START has an immutable, streamable v3 terminal locator and history summary,
  or a typed failure remains in bounded recovery state.
- **J3 — Improve:** Wave 3 can consume verified raw records and append the existing typed assessment tail;
  summaries never substitute for evidence.
- **J4 — Warn:** the durable typed summary hashes needed by later cohort/report work become queryable.
- **J5 — Run unattended:** every durable transition is restart-convergent and recovery work is bounded.

### 1.1 In scope

1. Owner-only v3 paths, file validation, exact-write/readback, sync, atomic replace, and fault hooks.
2. A private bounded work registry with preparing, capturing, processing, tail, rollover-reservation, and
   sealing transaction state.
3. Registry-first START, exact compare-and-append, terminal close, damage continuation, and size rollover.
4. Bounded streaming of physical evidence; no whole-event materialization.
5. Typed derived-tail persistence in the already-frozen codec order and immutable event sealing.
6. Immutable terminal locators, append-only terminal catalog, resumable order-independent history rebuild,
   and bounded history pages.
7. A ModelOwner-owned atomic `(snapshot, persisted hash)` capture seam.
8. Targeted unit/integration/fault tests and architecture declarations.

### 1.2 Explicitly out of scope

- Recharge storage belongs to Slice 4 and is not prebuilt here.
- Wave 3 owns fragment profiling, consumer assessment, learning decisions, model commit orchestration,
  terminal-outcome construction, report rendering, health/MOTD presentation, and report retry. Wave 2 only
  persists and verifies the typed batch Wave 3 supplies.
- No v2 or Release-A path is opened, decoded, copied, imported, reprojected, or modified.
- No SQLite, third-party dependency, generic repository, generic event bus, or application-visible JSON map.
- No live service/config change, cutover handshake, deployment, UAT, or Cross-AI action is part of Wave 2.

## 2. Context and constraints

### 2.1 Existing contracts retained

- `SCHEMA_VERSION = 3`; a root wire record has `seq=0` and `prev_record_sha256=null`.
- Each wire chain starts at `seq=0`/`prev=null`, is contiguous, and is at most `3197`; wire sequence is
  independent from logical domain counters, which remain unsigned 64-bit values. One aggregate has a
  physical-capture chain and a separate terminal/derived chain, as already demonstrated by the Wave 1 tail
  codec fixtures that root `endpoint_anchor` at sequence zero.
- A complete raw NUT token map is at most 16 KiB; a physical JSONL line is at most 20 KiB.
- A raw evidence page is at most 1,024 records and 4 MiB. A recovery page is at most 32 work items.
- Capture accounting stops at 62 MiB and retains a 2 MiB full-tail reserve. An aggregate never exceeds
  64 MiB. A logical aggregate has at most 64 private physical segment references.
- The existing tail budget remains 128 derived records, 8 KiB per derived record, 256 descriptors of at most
  256 canonical bytes, and 2 MiB for the complete derived/terminal tail. In the `prove_tail_budget` contract,
  `derived_records` contains profiles, three assessment summaries, decision, and optional receipt;
  `terminal_records` contains all terminal anchors, `blackout_end`, and `terminal_outcome`. Outcome is never
  counted inside the 128-derived-record ceiling.
- Physical files are scientific authority. Registry, catalog, locators, offset tables, indexes, summaries,
  reports, cursors, and health values are either transaction metadata or rebuildable projections.

### 2.2 Safety and compatibility

- The monitor publishes the virtual UPS before submitting storage work. Wave 2 adds no poll-thread filesystem
  call and does not change the one-second cadence, sticky shutdown, unknown handling, or runtime floor.
- The fresh root is `STATE_ROOT/events-v3/blackouts/`. `STATE_ROOT/events/`,
  `STATE_ROOT/discharge-events-v1.jsonl`, and every other v2/Release-A path are outside the v3 path resolver.
- No compatibility fallback is permitted. A missing/corrupt v3 structure fails v3 evidence availability; it
  never searches an older root.
- Python 3.13 standard library only. File ownership is the service uid; no new privilege or capability is
  required.

### 2.3 Performance bounds

- Capture operations encode and append one bounded record. They do not scan an event, catalog, or index.
- Recovery returns at most 32 items; the global pending-processing cap is eight.
- Evidence consumers hold at most one 4 MiB page plus bounded decoder state.
- An index tick completes at most 32 catalog entries, reads/writes at most 4 MiB, and runs for at most 50 ms.
- A single event larger than the tick budget advances by a durable 512 KiB byte cursor; it is not rejected.
- Index/catalog/history size has no 4 MiB global ceiling and no automatic time pruning.

## 3. Approaches considered

Scores are 1 (poor) through 5 (strong). The weighted result is out of 5.

| Criterion | Weight | A: clean private v3 family | B: subclass/parameterize v2 | C: one file per record | D: SQLite |
|---|---:|---:|---:|---:|---:|
| Fail-closed security and path isolation | 20 | 5 | 2 | 4 | 4 |
| Proof that v2 is never read or mutated | 20 | 5 | 1 | 5 | 5 |
| Crash durability and exact replay | 20 | 5 | 4 | 3 | 5 |
| Wave 1 contract/codec alignment | 15 | 5 | 3 | 3 | 2 |
| Bounded streaming and rebuild | 10 | 5 | 4 | 2 | 4 |
| Testability/fault injection | 10 | 5 | 3 | 4 | 3 |
| Kaizen simplicity/dependency cost | 5 | 4 | 4 | 2 | 1 |
| **Weighted result** | **100** | **4.95** | **2.55** | **3.50** | **3.85** |

### 3.1 Approach A — clean private v3 adapter family (selected)

Create v3-only path, durability, registry, catalog, locator, capture, evidence, tail, history, and model-capture
collaborators. Reuse the Wave 1 typed values and codecs, and reuse v2 *techniques* only where they are still
correct: registry-first creation, exact append intent, owner-only modes, bounded cursors, external merge, and
atomic promotion.

Benefits are mechanical v2 isolation, typed least-authority ports, bounded recovery, and direct fault tests.
The cost is new adapter code, but the code is cohesive and contains no migration archaeology.

### 3.2 Approach B — subclass or parameterize v2 storage (rejected)

The existing durability code has valuable patterns, but its public classes encode v2 filenames, schemas,
record grammar, lifecycle, report outbox, and projection assumptions. Reusing those classes makes an
accidental v2 open/import hard to disprove and couples v3 changes to legacy behavior. This violates the
approved fresh-authority boundary.

### 3.3 Approach C — one immutable file per record (rejected)

This makes append idempotency easy, but a long event creates thousands of directory entries, makes directory
fsync the one-second hot path, complicates ordered streaming and rollover, and broadens cleanup/recovery. It
does not improve product evidence over bounded append-only segments.

### 3.4 Approach D — SQLite/WAL (rejected)

SQLite offers transactions and indexes, but it contradicts the authoritative JSONL decision, adds a
dependency and a second scientific representation, and obscures immutable raw receipts. No current business
requirement justifies it.

### 3.5 Project-specific rejection rules

Any implementation is rejected if it opens a v2 path, requires a whole-event/index scan for a normal append,
places raw serial/tokens in logs or human projections, follows symlinks, widens owner-only modes, accepts a
second writer, treats an index as evidence, silently truncates corruption, or lets storage delay safety
publication.

## 4. Selected architecture

```text
ModelOwner (owns process lock fd + thread transaction gate)
   |-- atomic blackout capture adapter -> FrozenModelCapture
   `-- injects writer lease into v3 mutators

BlackoutCaptureStorePort -> registry -> physical JSONL segment(s) + offset tables
                                      -> processing registry
BlackoutEvidencePort     <- active registry or immutable terminal locator
BlackoutTailStorePort    -> terminal-chain staging -> immutable terminal chain -> seal
                                                     |
                                                     v
                           immutable terminal locator -> terminal catalog
                                                               |
                                                               v
BlackoutHistoryPort      <- active sorted index generation <- bounded rebuild
```

The adapter has one logical aggregate `segment_id`, retained in every Wave 1 domain value and v3 envelope.
It has two named wire chains: `physical` contains START, samples, gaps, and intermediate anchors;
`terminal` contains terminal anchors, END, profiles, summaries, decision, optional receipt, and outcome.
Damage recovery may split only the physical chain's bytes across private storage segments. Those private
storage IDs never cross an application port. Therefore every `StoredPhysicalRecord.ref` remains the same
`BlackoutRef`, while the locator preserves up to 64 physical-segment receipts plus one terminal-chain receipt.

## 5. Exact filesystem contract

`STATE_ROOT` is the already-existing model directory. Components accept it together with the injected
ModelOwner writer lease; they do not infer it from environment variables. V3 storage validates the existing
directory against the lease and may create only descendants beginning at `events-v3/`. It never creates
`STATE_ROOT`, its parent, or `monitor.lock`.

```text
STATE_ROOT/                                      0700, existing
  monitor.lock                                  0600, ModelOwner-owned
  events-v3/                                    0700
    blackouts/                                  0700
      work-registry-v1.json                     0600
      terminal-catalog-v1.jsonl                 0600
      terminal-catalog-head-v1.json             0600
      terminal-catalog-append-intent-v1.json    0600, present only in flight
      segments/                                 0700
        blk-<utc>-<blackout>-p<ordinal>-<storage>.jsonl
        blk-<utc>-<blackout>-p<ordinal>-<storage>.offsets
        damaged-<blackout>-<logical>-p<ordinal>-<storage>-<file_sha256>.jsonl    0400
        damaged-<blackout>-<logical>-p<ordinal>-<storage>-<file_sha256>.offsets 0400
      terminal-chains/                          0700
        <blackout>.jsonl                         0400 after seal
      transactions/                             0700
        tail-<blackout>.jsonl                    0600, terminal chain before seal
      terminal-locators/                        0700
        <blackout>.json                         0400
      history/                                  0700
        index-v1.jsonl                           0600
        index-head-v1.json                       0600
        rebuild-state-v1.json                    0600, present only during rebuild
        event-scan-v1.json                       0600, present only for a large event
        runs/                                    0700
        merge-v1.jsonl                           0600, present only during rebuild
```

### 5.1 Grammar

- Adapter-generated `blackout_id`, logical `segment_id`, and private `storage_id` are lowercase UUID4 hex:
  `[0-9a-f]{32}` with version nibble `4` and RFC-4122 variant. Supplied IDs are validated to this grammar at
  the storage edge even though the reusable domain values accept bounded text.
- `<utc>` is the START UTC rendered as `YYYYMMDDTHHMMSSffffffZ`.
- `<ordinal>` is six decimal digits `000000..000063`. Ordinal 63 (physical reference 64) is admitted only for
  the terminal damage-continuation receipt inside the same logical blackout. Size rollover starts a new
  aggregate at ordinal `000000`; it never consumes another physical reference in the old aggregate.
- The complete active filename regex is
  `\Ablk-\d{8}T\d{12}Z-[0-9a-f]{32}-p\d{6}-[0-9a-f]{32}\.jsonl\Z`.
- The complete damaged JSONL filename regex is
  `\Adamaged-[0-9a-f]{32}-[0-9a-f]{32}-p\d{6}-[0-9a-f]{32}-[0-9a-f]{64}\.jsonl\Z`;
  fields are respectively blackout ID, logical segment ID, ordinal, private storage ID, and the immutable
  damaged JSONL SHA-256. Its paired offset filename replaces only `.jsonl` with `.offsets`. Including all
  three identities makes equal damaged bytes in different aggregates/segments collision-free. The longer
  damaged offset basename is 187 ASCII bytes, below the closed 255-byte component bound.
- Persisted path tokens are typed resolver capabilities, never filesystem paths. An active segment token has
  exact grammar
  `v3seg1:<blackout_id>:<logical_segment_id>:<utc>:p<ordinal>:<storage_id>:jsonl`; its offset token is the
  identical fields with final component `offsets`. A damaged token has exact grammar
  `v3dam1:<blackout_id>:<logical_segment_id>:p<ordinal>:<storage_id>:<file_sha256>:jsonl|offsets`.
  Parsing validates every field, requires the two active tokens to differ only in their final component, and
  requires their blackout/logical/storage/ordinal fields to equal the containing registry variant and segment
  receipt before the resolver derives a basename. A token from another aggregate or segment is a typed
  `V3PathBindingConflict`, even if its rendered basename exists.
- Only the resolver creates paths. IDs and cursor strings are never concatenated without grammar validation;
  `..`, separators, control characters, absolute paths, symlinks, devices, sockets, and hard links with
  `st_nlink != 1` fail closed.

### 5.2 Creation and sealing modes

- Directories are created `0700` and rejected if group/other bits are present.
- Mutable files are created with `O_NOFOLLOW|O_CLOEXEC`, exclusive create where applicable, and mode `0600`.
- A sealed segment, offset table, or locator is `fchmod(0400)`, `fsync`ed, and followed by parent directory
  `fsync`. Existing modes are never silently widened or repaired.
- Temporary names are generated only under the exact owning directory as `.<basename>.tmp-<uuid4hex>`.
  Cleanup removes only a verified regular file with that exact prefix and same uid; cleanup failure is noted
  without masking the primary error.

## 6. Private persisted schemas

All JSON is strict UTF-8 canonical JSON (`sort_keys=True`, compact separators, ASCII escapes, no NaN), ends
with one newline, and rejects duplicate keys or unknown fields.

### 6.1 Work registry

Top-level schema `v3-blackout-work-registry-v1` has exactly:

```json
{"capture":null,"pending":[],"schema":"v3-blackout-work-registry-v1"}
```

There is at most one `capture` value and at most eight `pending` entries. A START is refused before reservation
when eight entries are already pending, so every accepted START has a processing slot. The registry is at
most 256 KiB.

The private variants are frozen dataclasses with the following exact fields; the JSON field names are the
snake-case names shown here and no inheritance discriminator is serialized beyond `tag`:

```python
@dataclass(frozen=True, slots=True)
class PreparingCaptureState:
    tag: Literal["preparing"]
    blackout_id: str
    logical_segment_id: str
    storage_id: str
    path_token: V3SegmentPathToken
    offset_token: V3OffsetPathToken
    start_line_utf8: str
    start_sha256: str
    start_length: int
    started_utc: str
    frozen_policy_revision: str

@dataclass(frozen=True, slots=True)
class CapturingState:
    tag: Literal["capturing"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor: BlackoutCaptureCursor | None
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    append_intent: V3AppendIntent | None
    last_append: V3LastAppend | None
    damage_continuation: V3DamageContinuation | None
    rollover: V3RolloverReservation | None

@dataclass(frozen=True, slots=True)
class ProcessingState:
    tag: Literal["processing"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor_after_end: BlackoutCaptureCursor
    terminal_root_sha256: str
    terminal_closing_anchor_sha256: str | None
    terminal_end_sha256: str
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    tail_build_intent: V3TailBuildIntent | None

@dataclass(frozen=True, slots=True)
class TailState:
    tag: Literal["tail"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor_after_outcome: BlackoutCaptureCursor
    terminal_root_sha256: str
    terminal_closing_anchor_sha256: str | None
    terminal_end_sha256: str
    terminal_outcome_sha256: str
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    tail_path_token: V3TerminalStagingToken
    tail_length: int
    tail_sha256: str
    tail_records: tuple[V3TailRecordReceipt, ...]
    seal_intent: V3SealIntent | None
```

`V3TailBuildIntent` has exactly `tail_path_token`, `expected_terminal_cursor`, `batch_sha256`,
`encoded_length`, and `encoded_sha256`. `V3SealIntent` has exactly `phase`
(`reserved|files_sealed|locator_durable|catalog_durable`), `locator_seq`, `catalog_offset`, nullable
`locator_sha256`, and nullable `catalog_line_sha256`. `V3WorkRegistry.capture` is
`PreparingCaptureState|CapturingState|None`; `pending` is an ordered tuple of
`ProcessingState|TailState`. These four unions are closed.

Each `storage_segments` item has exactly `ordinal`, `storage_id`, `path_token`, `offset_token`, `trusted_bytes`,
`first_seq`, `last_seq`, `last_record_sha256`, nullable `damaged_file_sha256`, and `terminal_only`.

`append_intent` has exactly `chain` (`physical|terminal`), `operation`, `expected_seq`,
`expected_previous_hash`, nullable `storage_ordinal`,
`file_offset`, `line_utf8`, `line_sha256`, `line_length`, and `expected_cursor_sha256`. It is persisted before
the file write. `last_append` repeats the operation, prior cursor hash, line hash, and resulting cursor. It
makes an immediate exact retry idempotent; any different bytes or older cursor are a typed conflict.

`damage_continuation` is distinct from rollover and has exactly `phase` (`reserved`, `old_renamed`,
`successor_created`, or `gap_durable`), unchanged `blackout_id`/`logical_segment_id`, old/new storage IDs and
ordinals, old active path tokens, exact damaged path tokens, trusted byte length/sequence/hash, damaged file
SHA-256, exact first continuation gap line/hash/length, and new physical cursor. It never contains
`continued_from`, `continued_by`, or a successor START.

`rollover` is a subtransaction preparing a new aggregate, not a second concurrently active capture. It
contains exact `phase` (`reserved`,
`successor_started`, or `carrier_ended`), `budget_kind` (`bytes|segment_refs`), old/new blackout IDs,
old/new logical segment IDs, shared physical episode ID, old/new path and storage IDs, exact encoded successor
START and carrier END with hashes/lengths, and `continuation_kind=size_rollover`. This is the only
representation of a preparing successor aggregate; it does not consume a pending slot and cannot become
capture-active until registry swap.

Every token nested in these variants is cross-bound by §5.1. Both cursors carry their closed chain kind.
`ProcessingState.terminal_cursor_after_end.previous_record_sha256` must equal mandatory
`terminal_end_sha256`; `terminal_closing_anchor_sha256` is null only for the codec-authorized anchorless
budget END, otherwise END must link it. `TailState` retains that same mandatory END hash and requires
`terminal_cursor_after_outcome.previous_record_sha256 == terminal_outcome_sha256`; its ordered receipts start
strictly after the END cursor and end at the outcome. Capturing cannot contain an END hash, processing cannot
contain an outcome hash, and tail cannot have a cursor at or before END.

Registry transition is exactly:

```text
preparing -> capturing -> processing -> tail -> sealed (registry entry removed)
```

### 6.2 Offset table

Each `.offsets` file is a private derived accelerator. Its exact eight-byte header literal is
`OFFSET_MAGIC_VERSION = b"UBMV3OF\x01"`: seven ASCII magic bytes `UBMV3OF` followed by binary format version
`0x01`. Any other byte or length fails closed. The header is followed by fixed 56-byte big-endian entries:

```text
seq:u32 | file_offset:u64 | line_length:u32 | record_sha256:32 bytes | record_kind:u8 |
reserved:7 zero bytes
```

`record_kind:u8` is permanently mapped as follows; `0x00` and `0x06..0xff` are invalid:

| Value | Wave 1 physical record type |
|---:|---|
| `0x01` | `blackout_start` |
| `0x02` | `discharge_sample` |
| `0x03` | `discharge_gap` |
| `0x04` | `endpoint_anchor` |
| `0x05` | `blackout_end` |

The production physical-segment table uses `0x01..0x04`; `blackout_end` lives in the independent terminal
chain and therefore has no production offset-table entry. Its `0x05` assignment is nevertheless frozen so
all Wave 1 physical types have one stable binary vocabulary and strict decoders cannot reinterpret it later.

The first physical segment starts at sequence zero with previous hash null. Later damage segments continue
the same logical sequence/hash scope. An offset entry becomes visible only after its JSONL line is durable.
A missing/damaged offset table is rebuildable by bounded scan; it never changes evidence meaning.

### 6.3 Terminal locator

Schema `v3-blackout-terminal-locator-v1` contains exactly:

- aggregate, physical-episode, battery-epoch, and logical-segment IDs;
- origin, nullable UAT intent, started/ended UTC, termination, sample/gap uint64 counts;
- ordered physical-segment receipts (maximum 64): ordinal, path token, offset token, file SHA-256, offset-table
  SHA-256, byte length, trusted byte length, first/last sequence, first/last record hashes, and damaged flag;
- one terminal-chain receipt: path token, file SHA-256, byte length, root/final sequence and hashes;
- `physical_chain_root_record_sha256` and `physical_chain_final_record_sha256`;
- `terminal_chain_root_record_sha256`, `blackout_end_record_sha256`,
  `terminal_outcome_record_sha256`, and `terminal_chain_final_record_sha256` (equal to the outcome hash);
- immutable aggregate hash: SHA-256 of canonical ordered physical-segment receipts plus terminal-chain,
  END, and outcome hashes;
- exact canonical `BlackoutSummary` projection and its SHA-256;
- reserved terminal catalog sequence, byte offset, catalog-line SHA-256; and
- locator SHA-256 computed over all preceding fields.

The locator is written once. An existing exact locator is idempotent; any difference is corruption/conflict.
Scientific readers verify segment and record hashes from it and never trust its summary as raw evidence.

### 6.4 Terminal catalog

Each `v3-blackout-terminal-catalog-entry-v1` line is at most 1 KiB and has exactly `schema`, `seq` (uint64),
`locator_token`, `locator_sha256`, `summary_sort_key`, `previous_entry_sha256`, and `entry_sha256`. Entry zero
has null previous hash; later entries are contiguous.

`terminal-catalog-head-v1.json` stores next sequence, byte size, and last entry hash. The append-intent file
stores the exact line and offset before append. Retry uses `pread` at that offset and the immutable locator's
catalog coordinates; it never scans the catalog. A full exact line advances the head, zero bytes retry, a
proper torn prefix is quarantined as projection damage and rebuild is unavailable, and different bytes are a
conflict.

### 6.5 History index

Each `v3-blackout-history-summary-v1` line is at most 8 KiB. It contains the exact `BlackoutSummary` fields,
locator hash, aggregate hash, terminal outcome hash, `sort_key`, previous index-line hash, and line hash.
`sort_key` is canonical `started_at_utc + "|" + blackout_id`; it is unique and order is ascending. Wall time
or catalog sequence may regress without invalidating a generation.

The head names an immutable generation UUID, byte length, line count, final hash, catalog snapshot offset/hash,
and index SHA-256. A promoted generation is a projection snapshot; newer catalog entries wait for the next
generation rather than changing the bytes underneath an active page cursor.

## 7. Component and API design

### 7.1 New adapter files

| File | Responsibility |
|---|---|
| `src/adapters/jsonl_v3_errors.py` | Closed storage error taxonomy and bounded OS-error rendering. |
| `src/adapters/jsonl_v3_storage_paths.py` | Exact root/filename grammar, uid/mode/type/link validation. |
| `src/adapters/jsonl_v3_filesystem.py` | Writer-lease validation, write-all, readback, sync, atomic replace, immutable seal, fault hooks. |
| `src/adapters/jsonl_v3_registry.py` | Strict private schema, global bounds, transitions, append/rollover/seal intents, recovery pages. |
| `src/adapters/jsonl_v3_segment_index.py` | Fixed offset entries and bounded rebuild/read by sequence. |
| `src/adapters/jsonl_v3_capture_store.py` | `BlackoutCaptureStorePort`; START, append, close, rollover, damage continuation. |
| `src/adapters/jsonl_v3_evidence_store.py` | `BlackoutEvidencePort`; verified 1,024-record/4 MiB pages across private segments. |
| `src/adapters/jsonl_v3_tail_store.py` | `BlackoutTailStorePort`; ordered encoding, staging, append, readback, sealing handoff. |
| `src/adapters/jsonl_v3_terminal_locator.py` | Immutable locator codec/store and segment/aggregate verification. |
| `src/adapters/jsonl_v3_terminal_catalog.py` | Hash-linked catalog, bounded batches, head/append-intent recovery. |
| `src/adapters/jsonl_v3_history_index.py` | `BlackoutHistoryPort`, bounded external runs/merge, generation cursors. |
| `src/adapters/jsonl_v3_model_capture.py` | Thin `BlackoutModelCapturePort` adapter delegating one atomic ModelOwner seam. |

### 7.2 Existing files changed by implementation

- `src/adapters/model_owner.py`: add the atomic blackout capture seam and writer-lease capability; retain sole
  lock ownership.
- `src/adapters/jsonl_v3_terminal_tail_codec.py`: encode/decode both exact anchor roles. Kinds
  `transfer_to_battery` and `raw_firmware_lb` require `anchor_role=intermediate`; all other existing kinds
  require `anchor_role=terminal`. Existing terminal golden bytes remain unchanged.
- `src/application/blackout_storage_values.py`: add the closed `BlackoutChainKind`, carry it in capture
  cursors/stored refs, validate pages one chain at a time, freeze `MAX_TAIL_ANCHORS=32`, and freeze the opaque
  history cursor limit; no JSON/path value is added.
- `src/application/active_capture_session.py`: retain the advancing physical cursor and nullable advancing
  terminal cursor independently; a terminal marker never overwrites the cursor needed by later samples.
- `src/application/blackout_ports.py`: clarify the `project` semantics below without broadening signatures.
- `tach.toml`: declare the new adapter dependencies and forbid v3 modules from v2 adapter imports.

No configuration file changes.

### 7.3 Minimal Cluster A APIs

Cluster A exposes only typed persistence primitives; capture lifecycle decisions remain in Cluster B. There
is exactly one mutable filesystem boundary and it is transaction-scoped:

```python
type V3ReadableFileToken = (
    V3RegistryToken | V3SegmentPathToken | V3OffsetPathToken
    | V3TerminalStagingToken | V3TerminalChainToken | V3TerminalLocatorToken
    | V3CatalogToken | V3CatalogHeadToken | V3CatalogIntentToken
    | V3HistoryFileToken | V3TemporaryFileToken
)
type V3MutableFileToken = (
    V3RegistryToken | V3SegmentPathToken | V3OffsetPathToken
    | V3TerminalStagingToken | V3CatalogToken | V3CatalogHeadToken
    | V3CatalogIntentToken | V3HistoryFileToken | V3TemporaryFileToken
)
type V3AppendableFileToken = (
    V3SegmentPathToken | V3TerminalStagingToken | V3CatalogToken
    | V3HistoryFileToken | V3TemporaryFileToken
)
type V3SealableFileToken = (
    V3SegmentPathToken | V3OffsetPathToken | V3TerminalStagingToken
    | V3TerminalLocatorToken | V3HistoryFileToken | V3TemporaryFileToken
)
type V3PromotedFileToken = (
    V3TerminalChainToken | V3TerminalLocatorToken | V3HistoryFileToken
    | V3SegmentPathToken | V3OffsetPathToken
)

class JsonlV3Filesystem:
    def __init__(
        self,
        state_root: Path,
        *,
        writer_lease: ModelOwnerWriterLease,
        fault_hook: Callable[[V3FaultPoint], None] | None,
        monotonic_clock_ns: Callable[[], int],
    ) -> None: ...

    def write_transaction(
        self,
    ) -> AbstractContextManager[V3WriteTransaction]: ...

@dataclass(frozen=True, slots=True)
class V3FileSnapshot:
    byte_length: int
    content_sha256: str

@dataclass(frozen=True, slots=True)
class V3AppendReceipt:
    previous_length: int
    appended_length: int
    resulting_length: int
    appended_sha256: str

class V3WriteTransaction:
    def read_bounded(
        self, token: V3ReadableFileToken, *, max_bytes: int
    ) -> tuple[bytes, V3FileSnapshot]: ...
    def replace_bounded(
        self,
        token: V3MutableFileToken,
        *,
        expected: V3FileSnapshot | None,
        contents: bytes,
        max_bytes: int,
    ) -> V3FileSnapshot: ...
    def append_and_sync(
        self,
        token: V3AppendableFileToken,
        *,
        expected_offset: int,
        contents: bytes,
        max_result_bytes: int,
    ) -> V3AppendReceipt: ...
    def seal(
        self,
        token: V3SealableFileToken,
        *,
        expected_length: int,
        max_bytes: int,
    ) -> V3FileSnapshot: ...
    def promote(
        self,
        source: V3TemporaryFileToken,
        target: V3PromotedFileToken,
        *,
        expected_source: V3FileSnapshot,
        require_target_absent: bool,
    ) -> V3FileSnapshot: ...
    def create_offset_index(
        self, token: V3OffsetPathToken
    ) -> SegmentIndexSnapshot: ...
    def snapshot_offset_index(
        self, token: V3OffsetPathToken
    ) -> SegmentIndexSnapshot: ...
    def append_offset_index(
        self,
        token: V3OffsetPathToken,
        *,
        expected: SegmentIndexSnapshot,
        entry: SegmentIndexEntry,
    ) -> SegmentIndexSnapshot: ...
    def get_offset_index(
        self, token: V3OffsetPathToken, *, sequence: int
    ) -> SegmentIndexEntry | None: ...
    def page_offset_index(
        self,
        token: V3OffsetPathToken,
        *,
        entry_ordinal: int,
        limit: int,
    ) -> SegmentIndexPage: ...
```

`V3WriteTransaction` is a final, slots-only coordination object constructed only by `write_transaction`; its
constructor is repository-internal. The threat model trusts conforming repository code and the service UID,
while treating persisted bytes, token values, unexpected filesystem objects, and same-UID hostile processes as
untrusted. Arbitrary same-process reflection, monkeypatching, direct `os`/`ctypes`, or root access are outside
this boundary. Production implementations receive the transaction only from `write_transaction`, call only
the declared methods, and expose no public fd, fileno, opener, generic path, or dynamic dispatch. It performs only typed-token, dirfd-relative
operations. All reads used to authorize mutation, replacements, seals, and promotions occur while the lease
is held and are bounded by their explicit limits. Context exit closes every owned fd and invalidates the
object; every later call raises `V3TransactionClosed`. Implementations may call only these declared methods,
never dynamic probing in conforming production callers. There is no default/nullable lease and
no filesystem method for acquiring a lock. `append_and_sync` hashes/readbacks only the supplied bounded
append and returns its receipt; it never computes a whole-file digest. `seal` is the only facade operation
that computes a complete mutable-file digest and refuses a file above the explicit Wave 1/Wave 2 bound passed
as `max_bytes`.

```python
@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    state: V3WorkRegistry
    byte_length: int
    canonical_sha256: str

class JsonlV3WorkRegistry:
    def __init__(
        self,
        filesystem: JsonlV3Filesystem,
    ) -> None: ...

    def open_or_create(self, transaction: V3WriteTransaction) -> RegistrySnapshot: ...
    def read(self, transaction: V3WriteTransaction) -> RegistrySnapshot: ...
    def compare_and_replace(
        self,
        transaction: V3WriteTransaction,
        *,
        expected: RegistrySnapshot,
        replacement: V3WorkRegistry,
    ) -> RegistrySnapshot: ...
```

The registry token is the closed singleton `V3RegistryToken.WORK_REGISTRY`; callers cannot supply a path.
The variants are the exact private frozen dataclasses in §6.1; no `dict[str, object]` crosses this API.
`open_or_create` creates only the exact empty schema. CAS uses `transaction.read_bounded(..., max_bytes=256
KiB)`, requires both expected byte length and canonical hash, and uses `replace_bounded` for atomic replace,
readback, and result receipt. All three methods reject a transaction from another filesystem or an invalidated context.
The registry class has no lifecycle convenience methods.

```python
@dataclass(frozen=True, slots=True)
class SegmentIndexEntry:
    sequence: int
    file_offset: int
    line_length: int
    record_sha256: str
    record_kind: V3PhysicalRecordKind

@dataclass(frozen=True, slots=True)
class SegmentIndexSnapshot:
    entry_count: int
    first_sequence: int | None
    last_sequence: int | None
    byte_length: int
    append_state_sha256: str

@dataclass(frozen=True, slots=True)
class SegmentIndexPage:
    entries: tuple[SegmentIndexEntry, ...]
    next_entry_ordinal: int | None
    complete: bool

class JsonlV3SegmentIndex:
    def __init__(
        self, filesystem: JsonlV3Filesystem, token: V3OffsetPathToken
    ) -> None: ...

    def create(self, transaction: V3WriteTransaction) -> SegmentIndexSnapshot: ...
    def snapshot(self, transaction: V3WriteTransaction) -> SegmentIndexSnapshot: ...
    def append(
        self,
        transaction: V3WriteTransaction,
        *,
        expected: SegmentIndexSnapshot,
        entry: SegmentIndexEntry,
    ) -> SegmentIndexSnapshot: ...
    def get(
        self, transaction: V3WriteTransaction, sequence: int
    ) -> SegmentIndexEntry | None: ...
    def page(
        self,
        transaction: V3WriteTransaction,
        *,
        entry_ordinal: int = 0,
        limit: int = 1_024,
    ) -> SegmentIndexPage: ...
```

The constructor accepts only a validated token whose final component is `offsets`; the resolver derives the
exact paired `.offsets` basename from §5.1, and no arbitrary `Path` overload exists. `create`, `snapshot`, and
`append` delegate to the correspondingly named typed transaction methods. Each operation resolves through
the verified `segments/` dirfd and opens at most one descriptor. In particular `append_offset_index` opens
one `O_RDWR|O_NOFOLLOW|O_CLOEXEC` descriptor and uses that same descriptor for `fstat`, header/tail `pread`,
expected-snapshot comparison, one 56-byte append, `fdatasync`, exact appended-entry readback, and the result
snapshot; it neither closes/reopens nor resolves the name again in the operation.

`append_state_sha256` is deliberately not a whole-file digest. It is the restart-reconstructible CAS token

```text
SHA256("UBMV3-IDX-CAS-v1\0" || header_8 || u64be(byte_length) ||
       (first_entry_56 || last_entry_56 if entry_count > 0 else empty))
```

`snapshot` obtains it with bounded reads of exactly the header and, when present, the first and final entries
after validating `(byte_length - 8) % 56 == 0`; normal append therefore never scans the index. Expected CAS
comparison covers every snapshot field: first/last sequence, length, count, and this token. Exclusive writer
ownership plus exact byte length and boundary-entry receipts prevents lost append updates. Middle accelerator
corruption is detected by bounded `page` verification against JSONL record receipts and causes typed rebuild;
the `.offsets` file is derived, never evidence authority. No head sidecar is added.

`append` additionally requires contiguous sequence, nonoverlapping increasing file offsets, a 1..20 KiB
line, and the closed kind mapping. `get` uses the first sequence plus fixed-width arithmetic. `page` returns
at most 1,024 entries. Bounded rebuild creates a temporary typed offset token, feeds verified records through
`append`, seals it, and promotes it through the same transaction; there is no extra public rebuild API.

These index methods are the mutable active/rebuild API. Cluster B's sealed evidence reader opens only
immutable `0400` offset files through its read-only typed resolver and exposes no mutation or writable fd.

These are the complete public APIs of the two Cluster A storage classes; codecs and validation helpers remain
module-private.

### 7.4 Public port behavior

Existing Wave 1 signatures remain authoritative.

`BlackoutCaptureStorePort.open(start)`:

- validates IDs, codec bytes, policy, origin, model hash, and backlog before any filesystem mutation;
- is idempotent only for the exact preparing/capturing START bytes;
- returns the `physical` chain cursor immediately after durable START.

`append_sample`, `append_gap`, and `append_anchor`:

- require exact active `BlackoutRef` and cursor;
- encode with the caller's sequence and previous hash;
- return the next cursor only after write, `fdatasync`, bounded readback, a physical offset receipt when
  applicable, and registry advance;
- return the stored result for an exact immediate retry; any changed record at the same cursor is
  `V3AppendConflict`.

The narrow Wave 2 contract correction adds `BlackoutChainKind(PHYSICAL, TERMINAL)` to
`BlackoutCaptureCursor` and `StoredRecordRef`. `ActiveCaptureSession` retains a typed pair: the required
physical cursor and a nullable terminal cursor. Samples, gaps, and intermediate anchors use and advance only
the physical cursor. The first terminal anchor compares against, but does not consume or advance, the current
physical cursor; it creates the terminal staging file at terminal `seq=0`/`prev=null` and populates the
terminal cursor. Later terminal anchors advance only that terminal cursor. Thus the application can continue
physical samples with its retained physical cursor after a modeled-safe-shutdown marker.

`close(ref,cursor,end)`:

- requires the immediately preceding durable record to be the END's exact terminal anchor, except
  `aggregate_budget_exhausted`, whose domain payload always has
  `terminal_anchor_record_hash=null`;
- normally requires a terminal cursor and appends END to that chain. For anchorless
  `aggregate_budget_exhausted`, an existing terminal cursor supplies the next sequence and previous-record hash;
  only when no terminal cursor exists does the physical cursor authorize a terminal-root END at
  `seq=0`/`prev=null`. The wire contract rejects `seq=0` with a previous hash and `seq>0` without one. It then
  moves the guaranteed registry slot to `processing` and never appends assessment data.

`recover(cursor,limit)`:

- limit is `1..32`; order is `(blackout_id, logical_segment_id)` and does not use wall time;
- emits at most one active capture plus the deterministic processing/tail entries;
- completes any persisted intent before exposing work and returns the existing typed Wave 1 page/cursor.

`BlackoutEvidencePort.page(ref,cursor,limit)`:

- limit is `1..1024`; result bytes are at most 4 MiB and include at least one legal record when data remains;
- traverses private storage segments while every returned record retains the same logical `BlackoutRef`;
- emits only the physical chain and is complete after the final physical record; terminal anchors and END are
  handled by the independent terminal-chain store;
- each page independently verifies contiguous sequence, previous hash, codec, scope, and locator snapshot;
- streams only `blackout_start`, `discharge_sample`, `discharge_gap`, and intermediate `endpoint_anchor` records.

`BlackoutTailStorePort.append_tail(ref,batch)`:

- requires `processing`, verifies the batch's repeated terminal anchor/END facts, encodes the authoritative
  order in section 8, proves actual bytes, and persists one exact staging transaction;
- returns `stage=TAIL` after the terminal outcome is durable.

`mark_processed(processing)`:

- verifies the exact tail, seals segment/offset bytes, writes/verifies locator and catalog, removes the
  registry entry, and returns `stage=SEALED`;
- exact retry reconstructs SEALED from locator/catalog coordinates. It never reruns scientific evaluation.

`BlackoutHistoryPort.project(ref)` is frozen as follows:

- `ref` must be sealed and identify its logical aggregate; active/processing input raises
  `V3ProjectionUnavailable`;
- verifies the immutable locator and submits its exact summary to the current/next rebuild generation;
- returns `BlackoutSummaryPage(summaries=(summary,), next_cursor=None, complete=True)` — exactly one summary,
  never an arbitrary page or physical-episode fold;
- an exact retry returns the same typed summary; a different summary for the ID is a conflict.

`page_summaries(cursor,limit)`:

- limit is `1..100`; null cursor starts at the promoted generation's first line;
- the opaque cursor is URL-safe base64 canonical JSON containing schema, generation ID, next byte offset,
  previous line hash, last sort key, and a SHA-256 checksum; encoded size is at most 512 bytes;
- it returns ascending `(started_at_utc, blackout_id)` order. `complete=true` means end of that immutable
  generation; new events appear only in a later null-cursor query.

### 7.5 Atomic model capture and writer ownership

`ModelOwner` adds:

```python
def capture_blackout_model(self) -> FrozenModelCapture: ...
def writer_lease(self) -> ModelOwnerWriterLease: ...
```

`capture_blackout_model` executes under the owner's existing re-entrant lock, reads the model file, verifies
its hash still equals `_persisted_hash`, and constructs `FrozenModelCapture(self._snapshot,
self._persisted_hash)` before releasing the lock. The adapter's `capture()` only calls this method; it may not
read/hash the file itself.

`ModelOwner.open_runtime` alone acquires `STATE_ROOT/monitor.lock` before model load and owns the fd until
`close`. `writer_lease()` returns a ModelOwner-created object capability bound to that exact open fd, the
cached `(st_dev, st_ino)` of `monitor.lock`, the cached `(st_dev, st_ino)` of the already-existing
`STATE_ROOT`, the service uid, and the owner's in-process `RLock`. Composition must inject this capability
into `JsonlV3Filesystem`; construction without it fails without performing `lstat`, opening a directory, or
touching layout.

```python
class ModelOwnerWriterLease:
    def hold(self) -> AbstractContextManager[ValidatedWriterLease]: ...
```

`hold()` is the first operation in `JsonlV3Filesystem.write_transaction`. Inside ModelOwner code it acquires
the owner's `RLock` and, before any root or path `lstat`, verifies that the owner is open, the exact cached lock
fd is still held, and `fstat(fd)` is the cached regular lock inode owned by the service uid. Only then does it
yield the opaque `ValidatedWriterLease` carrying the cached root identity. While that guard remains held,
`write_transaction` validates `lstat(STATE_ROOT)` against the cached private-directory identity, opens the
verified v3 dirfd chain, and yields `V3WriteTransaction`. Exit invalidates the transaction and closes its fds
before `hold()` releases the owner lock. `hold()` performs no `flock`: continued ownership of the exact fd is
the capability proof. `ValidatedWriterLease` has no public constructor; storage consumes it only as the
linear, root-bound proof returned by `hold()`. Same-process reflection or monkeypatching is outside this
boundary. Thus:

- storage never opens, creates, acquires, re-flocks, unlocks, or releases `monitor.lock`;
- storage never creates `STATE_ROOT` or its parent; it may create only verified v3 descendants;
- model commits and v3 durable transitions cannot interleave inside one process;
- a second process fails the existing nonblocking flock in `ModelOwner.open_runtime`; and
- read-only immutable evidence/history methods need no writer authority; any read of mutable registry or
  offset-index state uses `write_transaction` and finishes before the lease is released.

## 8. State and durability sequences

### 8.1 Open

1. Validate START and encode exact `seq=0`, `prev=null` bytes.
2. Under the writer lease, require no active capture and fewer than eight pending entries.
3. Atomically replace registry with `preparing` and fsync the blackouts directory.
4. Exclusive-create segment and offset files; no path may pre-exist.
5. Write-all START, `fdatasync` segment, read back exact bytes/hash.
6. Write/fdatasync offset header+entry, then fsync `segments/`.
7. Atomically replace registry with `capturing` and exact cursor.

Recovery before step 3 did nothing. From steps 3–6 it recreates or verifies only the frozen bytes in
`preparing`. Different bytes conflict. After step 7, retry returns the existing opened value.

### 8.2 Append

1. Validate ref/cursor and encode the exact line; check line, capture-byte, record-count, sequence-tail, and
   segment-reference reservations before mutation.
2. Persist `append_intent` with expected offset and bytes.
3. Write-all and `fdatasync` the JSONL fd.
4. `pread` exactly `line_length` at the intent offset and verify equality/hash.
5. Append/fdatasync its offset entry.
6. Replace registry with new cursor/counters, clear intent, retain `last_append`.

`write_all` retries `EINTR`, advances on positive short writes, and treats zero as failure without spinning.
After a crash, exact full bytes complete the receipt, zero bytes retry, and any prefix/other bytes enter damage
continuation. Normal append never scans the file.

For the first terminal anchor, the same intent protocol creates `transactions/tail-<blackout>.jsonl`, writes
the anchor at `seq=0`/`prev=null`, fdatasyncs/readbacks it, fsyncs `transactions/`, and advances the terminal
cursor. Later terminal anchors append with the terminal cursor. Terminal records do not receive physical
offset-table entries because the whole terminal chain is independently bounded to 2 MiB and verified as one
sealing unit.

### 8.3 Independent sequence and byte admission

The physical and terminal chains have independent sequence roots; no tail reservation is subtracted from the
physical chain. This is required by the already-frozen Wave 1 limits and is arithmetically safe:

```text
physical worst case used by DischargeFragmentPolicy:
  START 1 + physical samples 3170 + GAP 1 + intermediate ANCHOR 1
  = 3173 records, seq 0..3172 <= 3197

terminal policy ceiling:
  terminal anchors 32 + END 1 + derived-record ceiling 128 + outcome 1
  = 162 records, seq 0..161 <= 3197

reachable Wave 1 BlackoutTailBatch maximum:
  terminal anchors 32 + END 1 + profile records 96 + summaries 3 +
  learning decision 1 + optional receipt 1 + outcome 1
  = 135 records, seq 0..134 <= 3197
```

The 101 reachable derived records are `96 profiles + 3 summaries + 1 decision + 1 optional receipt`.
`terminal_outcome` is terminal, not derived. The 256-descriptor limit is a total across the at-most-96 profile
records; it does not add records. The 128-derived-record policy ceiling deliberately has 27 unused record
slots for this closed Wave 1 order and is still tested as a construction guard.

The policy's reserved END remains part of its conservative 62 MiB byte construction even though END is in
the terminal chain; that over-reservation is safe and is not removed. Actual physical-chain bytes are still
limited to 62 MiB. The complete terminal chain, including anchors, END, derived records, receipt, and outcome,
must pass the existing 2 MiB `prove_tail_budget` measurement, so the aggregate total remains at most 64 MiB.

Physical-chain sequence exhaustion outside the frozen maximum construction is a defensive capacity failure:
before accepting such a record, the writer performs the same new-aggregate size rollover with
`aggregate_budget_exhausted/budget_kind=bytes`. It never consumes terminal sequence space because none is
shared.

### 8.4 Terminal and intermediate anchors

- `transfer_to_battery` and `raw_firmware_lb` are intermediate-role physical-chain records and count against
  62 MiB.
- `modeled_safe_shutdown`, `power_restored`, `service_stop`, `boot_boundary`, `charge_stabilized`, `gap`, and
  `corruption` use terminal role, live in the terminal chain, and count against the 2 MiB reserve.
- A modeled-safe-shutdown anchor may remain a mid-event fact if shutdown is cancelled; its *storage role*
  remains terminal. Physical samples may continue in the independent physical chain. A later final boundary
  gets its own terminal-chain anchor immediately before END.
- `blackout_end.terminal_anchor_record_hash` always links the exact last closing anchor, not merely the first
  terminal marker. If OL follows more physical samples, a new `power_restored` anchor is appended to the
  terminal chain and END links it; the earlier modeled marker remains independently queryable. A
  `safe_shutdown_restarted` END may use the modeled anchor as its closing anchor only when no later physical
  sample exists and its source-sample hash equals the physical chain's durable final record. If later physical
  bytes exist or reconciliation contradicts that link, recovery appends the appropriate boot/gap/corruption
  closing anchor and cannot classify the END as `safe_shutdown_restarted`.
- At most 32 terminal anchors are retained. Exact duplicates coalesce by source-sample hash; exhausting the
  bound closes as explicit capture damage rather than silently omitting an anchor.

### 8.5 Close and tail order

The first terminal anchor roots `transactions/tail-<blackout>.jsonl` at sequence zero; an anchorless budget END
may instead be its root when no terminal anchor exists. With prior terminal records, that same anchorless budget
END is linked at the next sequence with the prior record hash. Close durably appends the final terminal anchor (if
not already present) then END and moves to `processing`. `append_tail` continues that same terminal chain and
writes exactly:

1. `fragment_profile` records in codec-produced ordinal order (1–96);
2. one `load_sag_assessment_summary`;
3. one `curve_assessment_summary`;
4. one `firmware_lb_assessment_summary`;
5. one `learning_decision`;
6. zero or one `ir_model_commit_receipt`; and
7. one `terminal_outcome`, always last.

`BlackoutTailBatch.anchors` repeats all already-durable terminal anchors in order as verification input and
does not append them twice. The receipt is present only for the existing accepted-commit disposition. Every
terminal-chain record is contiguous in sequence/hash order and scoped to the same blackout;
compact-summary `segment_id="summary"` remains the codec's existing namespace. `prove_tail_budget` receives
the actual derived records plus all terminal anchors, END, and outcome and must pass before append and after
readback.

### 8.6 Rollover

The two continuation mechanisms are disjoint:

| Mechanism | Aggregate identity | Wire continuation | Physical references |
|---|---|---|---|
| Damage continuation | same `blackout_id`, logical `segment_id`, and `physical_episode_id` | next physical `seq`, previous trusted record hash; no START or `continued_*` link | adds one private storage segment |
| Size/reference rollover | new `blackout_id` and new logical `segment_id`, same `physical_episode_id` | successor START at physical `seq=0`/`prev=null`; exact `continued_from`/`continued_by`, `continuation_kind=size_rollover` | successor begins at private ordinal 0; old aggregate gains no ordinary rollover segment |

Thus “continue after damaged bytes” never creates an aggregate, while “roll over a bounded aggregate” always
creates one. If damage exhausts physical references, reference 64 first records the final same-aggregate loss;
only then does the separate `budget_kind=segment_refs` aggregate rollover begin.

For a bytes, physical-sequence, or segment-reference aggregate rollover:

1. Persist old/new UUIDs, exact link, successor START, and carrier END in `capture.rollover.phase=reserved`.
2. Create and sync successor START under a new `blackout_id` and logical `segment_id`, same
   `physical_episode_id`, copied epoch/origin/intent, and `continued_from`/`size_rollover`; its physical chain
   and private storage ordinal both restart at zero.
3. Append and sync old carrier END with `continued_by`; no dangling link is allowed before successor START.
4. Atomically swap registry active capture to successor and clear rollover.

The successor is not capture-active before step 4. Recovery reuses exact IDs/bytes. If successor creation
fails before durable START, old stays active/censored. If START is durable but carrier END is not, history uses
the registry reservation to fold one open physical episode, never two completed events.

### 8.7 Damage continuation and 64 references

Torn tail, unexpected append bytes, or middle-chain corruption is never truncated into apparent cleanliness.
The current physical file is closed, hashed, renamed to its exact damaged name, made `0400`, and retained.
Registry stores its trusted prefix. A new private storage segment continues the same logical `blackout_id`,
logical `segment_id`, next sequence, and last trusted hash; the first new record is an explicit typed
gap/corruption fact.

References 1–63 allow capture continuation. If another recovery is required at 63, physical reference 64 is
reserved for the final physical gap/corruption receipt. The independent terminal chain then appends the
closing corruption anchor and END before rollover. A 65th physical reference is rejected before file
creation. Ordinary byte rollover never adds a storage reference to the old aggregate.

### 8.8 Seal

1. Verify terminal outcome and every linked typed record from the terminal staging chain and durable readback.
2. `fdatasync`, readback/hash, `fchmod(0400)`, and `fsync` every physical segment/offset file; fsync
   `segments/`. Apply the same operations to the terminal staging file, rename it to
   `terminal-chains/<blackout>.jsonl`, and fsync `terminal-chains/`.
3. Reserve catalog sequence/offset in the pending registry entry.
4. Write locator temporary, fdatasync, readback, rename, and fsync `terminal-locators/`.
5. Persist catalog append intent, append/fdatasync/readback catalog line, advance catalog head, clear intent.
6. Remove registry pending entry and fsync blackouts directory. The staging name no longer exists because its
   exact bytes were promoted to the immutable terminal-chain name; fsync `transactions/` after the rename.

Registry removal is the SEALED transition. A crash at any step resumes from exact hashes/coordinates. No
sealed scientific file is reopened writable.

## 9. History rebuild and large-event behavior

`rebuild_tick(max_files=32,max_bytes=4*MiB,max_wall_s=0.050)` snapshots terminal catalog byte offset and hash.
It never assumes catalog, close, or wall-time order.

1. Read catalog entries by byte cursor and verify the catalog hash chain.
2. Verify each immutable locator and its segment receipts. For an event larger than the remaining tick budget,
   persist `event-scan-v1.json` with locator hash, segment ordinal, byte offset, cumulative SHA-256 state
   receipt, and expected segment hash. Advance by at most 512 KiB per tick. The catalog entry is consumed only
   after all segment hashes and terminal records verify.
3. Accumulate at most 1,024 summaries/4 MiB, sort by `(started_at_utc, blackout_id)`, and atomically seal a run.
4. Merge at most 16 runs at once in 4 MiB chunks. Exact duplicate ID/summary collapses; differing bytes for
   one ID fail closed. Multi-pass merge handles any run count.
5. Verify output order, line chain, byte count, and SHA-256; atomically rename to `index-v1.jsonl`, fsync
   `history/`, then publish the new head.

Rebuild state records phase (`scan`, `merge`, `verify`, `prepared`), generation, catalog snapshot, run list,
input/output offsets and hashes, and last progress UTC. Wall time is observability only. Restart resumes exact
offsets; if any immutable input hash changes, the generation fails closed. The previously promoted generation
remains queryable during rebuild and after a failed rebuild.

## 10. Error model and recovery policy

New typed errors are:

- `V3StorageError` base;
- `V3WriterOwnershipError` for missing/wrong lease or second writer;
- `V3PathError` for path/type/link/uid/mode violations;
- `V3ValidationError` for schema, ID, cursor, or codec-boundary failures;
- `V3AppendConflict` for same identity/cursor with different bytes;
- `V3CapacityError` only when a construction invariant is violated before an explicit rollover path;
- `V3PersistenceError` for write/sync/rename/readback/ENOSPC/EIO failures;
- `V3CorruptionError` for durable bytes that violate a hash/schema/order invariant; and
- `V3ProjectionUnavailable` for an absent/stale/damaged derived generation.

Errors expose bounded stable reason codes to health/application layers. Raw NUT tokens, serials, complete
paths, and unbounded OS messages are not logged. Persistence errors latch evidence health but never alter the
safety result. Sealed corruption is never repaired in place; it makes that evidence/projection unavailable
and preserves bytes for diagnosis.

## 11. Security design

### 11.1 Threat model

| Asset | Actor/vector | Mitigation |
|---|---|---|
| Raw UPS evidence and serial fields | local user reads files | service uid ownership; directories 0700; files 0600/0400; no human projection |
| Evidence integrity | second daemon or concurrent model writer | one ModelOwner-owned flock plus shared in-process transaction gate |
| State-root boundary | symlink, traversal, device, hard-link substitution | closed grammar, `lstat`, `O_NOFOLLOW`, regular-file/uid/mode/link-count checks, dir-fd-relative operations |
| Scientific truth | index/locator substitution | event bytes remain authority; immutable segment/aggregate/outcome hashes reverified |
| Availability | oversized input, huge event/index, corrupt cursor | 16/20 KiB records, bounded pages/ticks/runs, no whole-file hot-path scan, fail-closed cursor validation |
| Provenance | retry changes origin/UAT/continuation | exact preparing/rollover bytes and immutable START/END stamps |
| Information disclosure | OS errors/logging | bounded reason codes; no token/serial/payload logging |

### 11.2 Security controls checklist

- [ ] All IDs, paths, enums, counters, sizes, cursors, and canonical JSON are validated at adapter boundaries.
- [ ] No path is accepted from a wire record or index; the resolver produces all paths.
- [ ] Owner-only uid/modes and non-symlink regular files/directories are verified on every open/recovery.
- [ ] One nonblocking writer lock is acquired before model load and remains ModelOwner-owned.
- [ ] No secret is added; raw forensic values remain only in owner-only event bytes.
- [ ] No SQL, shell command, network listener, authentication surface, or elevated capability is added.
- [ ] Resource ceilings exist for records, registry, pending work, segments, pages, tail, tick, run memory, and
  cursor size; aggregate/index total history is streamed rather than globally capped.
- [ ] Unknown/corrupt state fails closed and never falls back to v2.

No Critical or High security issue is accepted for merge.

## 12. Exact fault hooks and fault matrix

`V3FaultPoint` is a closed enum. Hooks are test-only injected callables and receive only these names:

```text
layout.after_events_v3_dirsync
open.after_registry_preparing
open.after_segment_create
open.after_start_write
open.after_start_fdatasync
open.after_start_readback
open.after_offset_fdatasync
open.after_segments_dirsync
open.after_registry_capturing
append.after_registry_intent
append.after_line_write
append.after_line_fdatasync
append.after_line_readback
append.after_offset_fdatasync
append.after_registry_advance
terminal.after_registry_intent
terminal.after_chain_create
terminal.after_anchor_write
terminal.after_anchor_fdatasync
terminal.after_anchor_readback
terminal.after_transactions_dirsync
terminal.after_registry_advance
damage.after_segment_rename
damage.after_segments_dirsync
damage.after_continuation_create
damage.after_continuation_fdatasync
damage.after_registry_advance
rollover.after_registry_reserve
rollover.after_successor_create
rollover.after_successor_fdatasync
rollover.after_successor_dirsync
rollover.after_carrier_end_fdatasync
rollover.after_registry_swap
close.after_anchor_fdatasync
close.after_end_fdatasync
close.after_registry_processing
tail.after_staging_fdatasync
tail.after_registry_intent
tail.after_batch_write
tail.after_batch_fdatasync
tail.after_batch_readback
tail.after_registry_outcome
seal.after_files_readback
seal.after_files_chmod_fsync
seal.after_terminal_rename
seal.after_terminal_dirsync
seal.after_locator_fdatasync
seal.after_locator_rename
seal.after_locator_dirsync
seal.after_catalog_intent
seal.after_catalog_write
seal.after_catalog_fdatasync
seal.after_catalog_head
seal.after_registry_remove
seal.after_transactions_dirsync
index.after_generation_cursor
index.after_event_scan_cursor
index.after_run_fdatasync
index.after_merge_chunk_fdatasync
index.after_prepared
index.after_promote_rename
index.after_history_dirsync
index.after_head_publish
```

Tests inject process-stop exceptions at every hook and reopen from disk. They also inject:

- positive short writes, zero writes, `EINTR`, `ENOSPC`, `EIO`, fdatasync/fsync/directory-fsync/rename/readback
  failures;
- torn final line, unexpected tail bytes, hash-valid middle semantic corruption, offset-table corruption;
- unsafe permissions, wrong uid where testable, symlink/nonregular/hard-link paths, and a second writer;
- duplicate exact delivery and differing-byte conflict at START, append, tail, locator, and catalog;
- every open/append/close/tail/seal/rollover/damage durable crash point;
- a physical event above 4 MiB, an event above one tick budget, a maximum 64 MiB aggregate, and an index above
  4 MiB/10,000 summaries;
- byte/sequence rollover, 63-to-64 terminal reference use, refused 65th reference, and unchanged hashes;
- modeled-safe-shutdown as terminal root followed by more physical samples and a distinct final closing
  anchor; restart accepts the modeled anchor only when its source hash is still the physical-chain final hash;
- multiple simultaneous pending entries, recovery pages over 32, unsorted/regressing wall times and catalog
  completion order, exact duplicate summaries, and conflicting duplicate IDs.

Every restart assertion proves either exact convergence or an explicit typed unavailable/loss state; no test
accepts silent omission.

## 13. Test plan

### 13.1 Unit tests

| Test file | Required coverage |
|---|---|
| `tests/adapters/test_jsonl_v3_storage_paths.py` | active/damaged grammar, damaged cross-aggregate/hash collision resistance, 187-byte bound, UUID4, traversal, modes, links |
| `tests/adapters/test_jsonl_v3_filesystem.py` | capability validation before root/path lstat, required lease, one typed write context, bounded methods, owned-fd closure/invalidation, no fd/reflection/name-probing escape, no storage flock/open of monitor.lock |
| `tests/adapters/test_jsonl_v3_registry.py` | exact four frozen variants, token/ID cross-binding, mandatory END/outcome cursor relations, one active/eight pending, CAS and 32-page recovery |
| `tests/adapters/test_jsonl_v3_segment_index.py` | exact `b"UBMV3OF\x01"` golden, complete u8 mapping, 56-byte entries, one O_RDWR descriptor per CAS, restart-rebuilt append-state token, no normal full scan, get/page/rebuild feed |
| `tests/application/test_blackout_storage_values.py` | independent chain cursors/refs, one-chain pages, physical-to-terminal cursor transition |
| `tests/application/test_active_capture_session_chains.py` | retained physical cursor plus nullable terminal cursor; interleaved modeled marker/sample/final close |
| `tests/adapters/test_jsonl_v3_capture_store.py` | open/append/close idempotency, conflict, uint64 counters, 16/20 KiB, root/chain |
| `tests/adapters/test_jsonl_v3_rollover.py` | bytes/sequence, exact links/IDs, old-open failure, successor-before-carrier, swap recovery |
| `tests/adapters/test_jsonl_v3_damage_recovery.py` | torn/middle corruption, trusted prefix, 63/64 refs, no 65th file |
| `tests/adapters/test_jsonl_v3_evidence_store.py` | 1,024/4 MiB pages across private files, cursors, codec/scope/hash rejection |
| `tests/adapters/test_jsonl_v3_tail_store.py` | independent seq-zero terminal root, reachable 101-derived/135-total maximum, 128-derived/162-total policy ceiling, 256 descriptors, 2 MiB proof |
| `tests/adapters/test_jsonl_v3_terminal_locator.py` | exact physical-root/final, terminal-root/END/outcome/final hashes, immutable retry/conflict, aggregate hash |
| `tests/adapters/test_jsonl_v3_terminal_catalog.py` | root/contiguous catalog, head/intent, no-scan retry, >4 MiB catalog |
| `tests/adapters/test_jsonl_v3_history_index.py` | unsorted input, external runs/merge, cursor generations, duplicate/conflict |
| `tests/adapters/test_jsonl_v3_large_event_rebuild.py` | >tick and 64 MiB event across bounded restartable ticks |
| `tests/adapters/test_jsonl_v3_model_capture.py` | atomic exact snapshot/hash, external model conflict, shared lease/second writer |
| `tests/adapters/test_jsonl_v3_fault_matrix.py` | every named hook and injected syscall/error class |

### 13.2 Integration and architecture tests

- `tests/application/test_blackout_v3_storage_integration.py`: typed ports from START through SEALED and
  exact `project`/page behavior; no application JSON/path leakage.
- `tests/application/test_blackout_v3_recovery_integration.py`: multiple work items, stage convergence,
  duplicate/conflict, tail retry, and order-independent recovery.
- Extend `tests/test_architecture_boundaries.py` to reject any import from the new v3 family to v2 JSONL
  modules and any domain/application import of concrete adapters.
- Extend `tests/test_quality_gates.py` only if a new source-span rule needs registration; thresholds are never
  loosened or suppressed.

### 13.3 Business acceptance tests

1. A durable START survives death after every open boundary and appears exactly once.
2. A >4 MiB event streams in bounded pages; no consumer materializes it.
3. A >tick-budget event eventually enters history over multiple restarts.
4. Exact duplicate append/tail/seal calls change no bytes; different bytes conflict.
5. Torn/corrupt capture retains immutable damaged bytes and explicit continuation/loss facts.
6. Byte/sequence and reference rollover preserve one physical episode, exact links, hashes, origin, and all
   accepted samples; no dangling `continued_by` appears.
7. Eight pending events recover; a ninth START is explicitly refused before acceptance rather than silently
   stranded.
8. Unsorted terminal order and backward wall time yield the same sorted unique history generation.
9. An index/catalog larger than 4 MiB pages normally and rebuilds without a global ceiling.
10. A storage/permission/second-writer failure leaves safety publication behavior unchanged in the existing
    safety integration fixture.

## 14. Implementation clusters for LUNA

Clusters are sequential at their integration boundaries but have non-overlapping file ownership. Every LUNA
prompt must state that other agents may be working in the tree, must preserve unrelated changes, and must not
edit another cluster's files.

### Cluster A — durability foundation

**Owns:** `jsonl_v3_errors.py`, `jsonl_v3_storage_paths.py`, `jsonl_v3_filesystem.py`,
`jsonl_v3_registry.py`, `jsonl_v3_segment_index.py`, the chain-kind/cursor changes in
`src/application/blackout_storage_values.py`, and their same-named tests plus
`tests/application/test_blackout_storage_values.py`.

**Completion:** exact four-variant schemas and token bindings, active/damaged path bounds, typed
independent-chain cursors, capability validation before path inspection, one invalidating transaction facade
with zero fd/reflection/flock escape, exact writes/intents, registry CAS, `b"UBMV3OF\x01"`, the full u8 kind
mapping, and single-descriptor segment-index CAS/get/page tests pass. The index golden proves the bounded
restart-reconstructible append-state token and that normal append performs no full scan. Tests distinguish
same-aggregate damage continuation from new-aggregate rollover. No capture/tail/history policy is implemented
here.

**Targeted gate:**

```bash
uv run pytest tests/adapters/test_jsonl_v3_storage_paths.py tests/adapters/test_jsonl_v3_filesystem.py tests/adapters/test_jsonl_v3_registry.py tests/adapters/test_jsonl_v3_segment_index.py tests/application/test_blackout_storage_values.py
uv run ruff format --check src/adapters/jsonl_v3_errors.py src/adapters/jsonl_v3_storage_paths.py src/adapters/jsonl_v3_filesystem.py src/adapters/jsonl_v3_registry.py src/adapters/jsonl_v3_segment_index.py src/application/blackout_storage_values.py tests/adapters/test_jsonl_v3_storage_paths.py tests/adapters/test_jsonl_v3_filesystem.py tests/adapters/test_jsonl_v3_registry.py tests/adapters/test_jsonl_v3_segment_index.py tests/application/test_blackout_storage_values.py
uv run ruff check src/adapters/jsonl_v3_errors.py src/adapters/jsonl_v3_storage_paths.py src/adapters/jsonl_v3_filesystem.py src/adapters/jsonl_v3_registry.py src/adapters/jsonl_v3_segment_index.py src/application/blackout_storage_values.py tests/adapters/test_jsonl_v3_storage_paths.py tests/adapters/test_jsonl_v3_filesystem.py tests/adapters/test_jsonl_v3_registry.py tests/adapters/test_jsonl_v3_segment_index.py tests/application/test_blackout_storage_values.py
```

### Cluster B — capture and evidence

**Owns:** `jsonl_v3_capture_store.py`, `jsonl_v3_evidence_store.py`, modification to
`jsonl_v3_terminal_tail_codec.py`, dual-cursor changes to `src/application/active_capture_session.py`, and
capture/rollover/damage/evidence tests plus `tests/application/test_active_capture_session_chains.py`.

**Completion:** exact port behavior, retained physical plus nullable terminal cursor, modeled-marker/sample/
closing-anchor interleave, independent roots, anchor roles, streaming, byte/sequence rollover, corruption
continuation, and 64-reference boundary pass without touching tail/catalog/history files.

**Targeted gate:**

```bash
uv run pytest tests/adapters/test_jsonl_v3_capture_store.py tests/adapters/test_jsonl_v3_rollover.py tests/adapters/test_jsonl_v3_damage_recovery.py tests/adapters/test_jsonl_v3_evidence_store.py tests/adapters/test_jsonl_v3_physical_codecs.py tests/adapters/test_jsonl_v3_terminal_tail_codec.py tests/application/test_blackout_storage_values.py tests/application/test_active_capture_session_chains.py
uv run ruff format --check src/adapters/jsonl_v3_capture_store.py src/adapters/jsonl_v3_evidence_store.py src/adapters/jsonl_v3_terminal_tail_codec.py src/application/active_capture_session.py tests/adapters/test_jsonl_v3_capture_store.py tests/adapters/test_jsonl_v3_rollover.py tests/adapters/test_jsonl_v3_damage_recovery.py tests/adapters/test_jsonl_v3_evidence_store.py tests/application/test_active_capture_session_chains.py
uv run ruff check src/adapters/jsonl_v3_capture_store.py src/adapters/jsonl_v3_evidence_store.py src/adapters/jsonl_v3_terminal_tail_codec.py src/application/active_capture_session.py tests/adapters/test_jsonl_v3_capture_store.py tests/adapters/test_jsonl_v3_rollover.py tests/adapters/test_jsonl_v3_damage_recovery.py tests/adapters/test_jsonl_v3_evidence_store.py tests/application/test_active_capture_session_chains.py
```

### Cluster C — tail, locator, catalog, and model capture

**Owns:** `jsonl_v3_tail_store.py`, `jsonl_v3_terminal_locator.py`,
`jsonl_v3_terminal_catalog.py`, `jsonl_v3_model_capture.py`, modification to `model_owner.py`, and their tests.

**Completion:** existing codecs persist in exact order, actual tail proof passes, SEALED is restart-convergent,
catalog append is no-scan idempotent, and snapshot/hash is atomic under shared ownership.

**Targeted gate:**

```bash
uv run pytest tests/adapters/test_jsonl_v3_tail_store.py tests/adapters/test_jsonl_v3_terminal_locator.py tests/adapters/test_jsonl_v3_terminal_catalog.py tests/adapters/test_jsonl_v3_model_capture.py tests/adapters/test_jsonl_v3_tail_budget.py tests/adapters/test_jsonl_v3_*codec.py tests/adapters/test_model_owner.py
uv run ruff format --check src/adapters/jsonl_v3_tail_store.py src/adapters/jsonl_v3_terminal_locator.py src/adapters/jsonl_v3_terminal_catalog.py src/adapters/jsonl_v3_model_capture.py src/adapters/model_owner.py tests/adapters/test_jsonl_v3_tail_store.py tests/adapters/test_jsonl_v3_terminal_locator.py tests/adapters/test_jsonl_v3_terminal_catalog.py tests/adapters/test_jsonl_v3_model_capture.py
uv run ruff check src/adapters/jsonl_v3_tail_store.py src/adapters/jsonl_v3_terminal_locator.py src/adapters/jsonl_v3_terminal_catalog.py src/adapters/jsonl_v3_model_capture.py src/adapters/model_owner.py tests/adapters/test_jsonl_v3_tail_store.py tests/adapters/test_jsonl_v3_terminal_locator.py tests/adapters/test_jsonl_v3_terminal_catalog.py tests/adapters/test_jsonl_v3_model_capture.py
```

### Cluster D — history and composition contracts

**Owns:** `jsonl_v3_history_index.py`, history/large-event tests,
`test_blackout_v3_storage_integration.py`, `test_blackout_v3_recovery_integration.py`, and the narrow
documentation/contract changes to `blackout_ports.py`, `tach.toml`, and architecture tests.

**Completion:** bounded order-independent rebuild, >tick event progress, generation paging, exact project
semantics, and import boundaries pass.

**Targeted gate:**

```bash
uv run pytest tests/adapters/test_jsonl_v3_history_index.py tests/adapters/test_jsonl_v3_large_event_rebuild.py tests/application/test_blackout_v3_storage_integration.py tests/application/test_blackout_v3_recovery_integration.py tests/application/test_blackout_ports.py tests/test_architecture_boundaries.py
uv run ruff format --check src/adapters/jsonl_v3_history_index.py src/application/blackout_ports.py tests/adapters/test_jsonl_v3_history_index.py tests/adapters/test_jsonl_v3_large_event_rebuild.py tests/application/test_blackout_v3_storage_integration.py tests/application/test_blackout_v3_recovery_integration.py
uv run ruff check src/adapters/jsonl_v3_history_index.py src/application/blackout_ports.py tests/adapters/test_jsonl_v3_history_index.py tests/adapters/test_jsonl_v3_large_event_rebuild.py tests/application/test_blackout_v3_storage_integration.py tests/application/test_blackout_v3_recovery_integration.py
uv run lint-imports
uv run tach check --exact
uv run pyright
```

### Wave 2 release-candidate gate

Only after all four clusters are code-complete:

```bash
just check
git diff --check
```

No suppression, threshold change, or weakened CRAP/structural/source-span/architecture rule is allowed to make
the RC green.

## 15. Rollback and cleanup

Wave 2 is not deployed independently. Code rollback is a normal revert of Wave 2 implementation commits.
Because the v3 root is fresh and v2 is untouched, rollback runs the old binary against its existing state;
the old binary never sees `events-v3`.

For development tests, each test owns a temporary state root and verifies no tail staging, append intent,
rebuild run, merge file, open fd, process lock, container, worktree, or test database remains. Runtime recovery
removes a staging file only after SEALED registry removal and exact hash verification. Raw/sealed/damaged
event bytes and immutable locators are never cleanup targets.

If a future cutover is rolled back after real v3 events exist, retain the complete owner-only `events-v3`
tree as forensic evidence. Do not import it into v2 and do not delete it as derived state.

## 16. Validation checklist

### Implementation

- [ ] Only the exact v3 root is reachable; production v3 code has no v2 adapter import or path literal.
- [ ] Registry schemas and all lifecycle transitions are strict, bounded, and restart-convergent.
- [ ] START is registry-first and durable before `open` returns.
- [ ] Append retry is exact and no normal append scans a whole file.
- [ ] Independent physical/terminal roots, per-chain sequence/hash/scope, uint64 counters, 16/20 KiB,
  62/64 MiB, 32 terminal anchors, 128 derived records, and 64 physical references are enforced before
  mutation.
- [ ] Damage is retained and explicit; sealing makes raw bytes immutable.
- [ ] Evidence and history paging obey exact page/cursor semantics.
- [ ] Rebuild is order-independent, resumable, and progresses on an event larger than one tick.
- [ ] Model snapshot/hash capture and writer-lock ownership are atomic and tested.
- [ ] Wave 2 does not render reports or mutate the model.
- [ ] All temporary resources are inventoried and cleaned by their owner.

### Security and quality

- [ ] Owner-only modes, no-follow/type/uid/link validation, least privilege, and second-writer refusal pass.
- [ ] Raw token/serial values do not enter logs, indexes, reports, health, or cursors.
- [ ] Every fault hook and syscall error in section 12 converges or fails explicitly.
- [ ] Ruff structural checks, CRAP <=30 per function, source-span limits, Pyright, Vulture, Import Linter, and
  Tach exact pass with no suppression or loosened threshold.
- [ ] `just check` and `git diff --check` pass on the exact Wave 2 RC tree.

## 17. Business-value traceability

| Product value | Durable mechanism | Acceptance proof |
|---|---|---|
| A blackout does not disappear after accepted START (J2/J5) | registry-first frozen START and exact recovery | crash at every open hook yields exactly one START |
| Safety never waits for evidence (J1) | storage stays behind publication and uses the existing bounded submission lane | held writer/storage failure keeps safety fixture fresh |
| Long/deep events remain usable (J2/J3) | 64 MiB immutable aggregate, 4 MiB evidence pages, 512 KiB rebuild cursor | >4 MiB and >tick fixtures finish with bounded state |
| Damage is honest, not silently repaired (J2/J3) | immutable damaged segment receipt plus explicit gap/corruption continuation | torn/middle-corrupt fixtures preserve hashes and refuse affected science |
| One physical outage is not inflated by technical rollover (J2) | same physical episode, exact two-sided links, successor-before-carrier transaction | byte/sequence/ref rollover projects one linked episode without dangling link |
| History works for day/month/year without Grafana (J2) | immutable locators plus unlimited streamed sorted generations | >10,000/>4 MiB unsorted history returns exact unique pages |
| Later science cannot learn from a summary (J3/J4) | evidence port reopens/verifies raw JSONL; index carries hashes only | architecture and mutation tests reject projection-as-evidence paths |
| Model evidence matches the actual persisted model (J1/J3) | one ModelOwner-locked snapshot+hash capture | concurrent/external model mutation is an explicit conflict |
| Reporting failure cannot strand evidence (J5) | SEALED/catalog completion is independent of Wave 3 report sinks | sealed event remains queryable with report layer absent/failing |

Wave 2 is complete only when every row has a deterministic passing test and the full RC gate is green. It is
not a live v3 deployment claim; the authoritative plan still requires the remaining slices, reviews,
cutover preflight, and user-run deployment/UAT.
