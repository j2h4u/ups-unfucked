# Release B: тенёвая оценка модели по естественным блэкаутам

**Статус:** implementation-ready план.
**Ветка:** `feat/natural-blackout-learning`.
**Предусловие:** Release A уже развёрнут в `capture_only`; durable capture и terminal
`recorded_only` работают.
**Граница работы по этому документу:** только код, тесты, документация и изолированные fixture/smoke
пути. Боевые service/unit/NUT/state не изменяются до отдельного release-runbook шага.

Этот документ детализирует только Release B канонического плана
`docs/plans/natural-blackout-learning-implementation.md`. Прецедентность решений явная:

1. последние зафиксированные unattended business invariants этого Release-B задания управляют
   реализацией;
2. независимое evidence означает независимость сенсорного/физического наблюдения от выхода самой
   модели, а не ручное подтверждение;
3. `capacity_ah_measured=None` означает отсутствие авторитетного измерения, а не future approval
   gate;
4. Release B никогда не обновляет модель; будущие автоматические обновления вверх или вниз остаются
   deferred до отдельно доказанной автоматической safety policy и никогда не превращаются в queue;
5. остальные непротиворечащие положения канонического плана сохраняются.

Root отдельно исправляет конфликтующую future-automation формулировку канонического документа. Все
пункты ниже обязательны; «опциональных», approval queue и ручного разбора штатных событий нет.

## 1. Бизнес-цели и требования

Release B должен доказать полный полезный вертикальный путь:

```text
естественный блэкаут
  -> durable start/sample/end
  -> автоматическая оценка качества и независимости evidence
  -> сравнение наблюдавшегося участка с моделью, замороженной в start
  -> автоматическое терминальное решение с причинами
  -> durable applied marker
  -> понятный health / battery-health / MOTD отчёт
```

| ID | Обязательное требование |
|---|---|
| `RB-01` | Каждое новое current-epoch событие с durable `end` автоматически получает разрешённую теневую проверку либо `recorded_only`/`rejected` с причинами; исключения — immutable history-only records `RB-26` и видимый retryable infrastructure `capture_failed`, ни одно из которых не ждёт ручного domain approval. |
| `RB-02` | Durable evidence предшествует оценке; сбой оценки не уничтожает и не переписывает raw evidence. |
| `RB-03` | Independent evidence означает независимость физического/сенсорного наблюдения от выхода проверяемой модели, а не подтверждение оператором; `capacity_ah_measured=None` не создаёт approval gate. |
| `RB-04` | Partial discharge проверяет модель только на наблюдавшемся участке и не доказывает full runtime, абсолютную capacity или SoH. |
| `RB-05` | Запрещены generic runtime correction, общий множитель времени и model-derived truth feedback в LUT/Peukert/IR/capacity/SoH. |
| `RB-06` | Learning/evaluation и safety остаются независимыми; LB/shutdown policy, runtime formula и physical/virtual NUT topology не ослабляются и не задерживаются. |
| `RB-07` | Release B не создаёт рабочих `CapacityCandidate`, cohorts, trends или proposals и не меняет scientific model; future automatic model updates, включая upward, требуют отдельно доказанной automatic safety policy в C/D, не очереди одобрения. |
| `RB-08` | В `start` до последующих model changes записывается immutable compact `BatteryModelSnapshot`, persisted scientific fingerprint и revision алгоритма. |
| `RB-09` | `ChargeReadinessTracker` автоматически обновляется на каждом успешном OL poll; его pre-OB snapshot фиксируется в `start` до reset. |
| `RB-10` | Immutable `EvidenceAssessment` и `LearningDecision` содержат отдельные решения и bounded reason codes по каждому реально существующему полю. |
| `RB-11` | Pure forward evaluator использует только start snapshot и фактически наблюдавшиеся same-boot сегменты; выдаёт `delivered_ah_proxy`, voltage и slope residuals. |
| `RB-12` | Duration, coverage и gaps происходят только из durable envelope `boot_id + monotonic_ns`; wall timestamp в sample payload не участвует в интеграции. |
| `RB-13` | `capture_only` является hard gate до legacy scientific handler и любой model save/setter/RLS/upscmd ветки; fingerprint остаётся вторичной сигнализацией. |
| `RB-14` | Collector, sag tracker и evaluator не импортируют mutable `BatteryModel`; прямое изменение `.state` вне владельца модели невозможно. Границы остаются лёгкими, без DDD framework. |
| `RB-15` | Каждый новый current-epoch корректно закрытый event получает `recorded_only` либо `rejected`; immutable history-only records не получают новый marker, а `pending_replay` означает только автоматически повторяемый infrastructure failure, не ожидание решения человека. |
| `RB-16` | Journal остаётся schema v1 с типами `start/sample/end/applied`, сохраняет legacy epoch/marker семантику и durable исторические строки. |
| `RB-17` | Unattended capacity policy не допускает скрытого упора активного journal в 64 MiB; размер, headroom, rollover и capture-unavailable видны в health. |
| `RB-18` | Health JSON остаётся bounded; `battery-health` и MOTD показывают plain-language результат, качество, gap, residual, решение и состояние журнала. |
| `RB-19` | Policy shutdown (`natural_policy_endpoint`) отличим от обычного SIGTERM/service stop; виртуальный LB не становится независимым terminal evidence. |
| `RB-20` | Raw-poll golden fixtures покрывают clock jumps, reboot gaps, CAL, short/noisy/10m partial/long events, policy endpoint, NUT/journal failures и live-model drift после start. |
| `RB-21` | Изолированный real-path E2E использует настоящие serializer/journal/replay/assessment/evaluator/reporting и только временные model/journal/output пути. |
| `RB-22` | Полный test/release gate выполняется один раз на RC, затем 24-hour canary; scientific fingerprint, runtime/LB и UPS command audit не меняются. |
| `RB-23` | Rollback сохраняет raw evidence и не требует reverse migration; все принадлежащие задаче временные ресурсы инвентаризуются и удаляются. |
| `RB-24` | README, user docs, glossary и internal context описывают фактический shadow-only путь, а не обещают обучение от partial blackout. |
| `RB-25` | Пользовательский итог прямо отвечает: что произошло, достаточно ли данных, где модель разошлась, что изменилось/не изменилось и почему. |
| `RB-26` | Исторические записи без epoch или старые `applied` без `disposition` остаются raw history-only evidence: без переписывания, backfill и переоценки. |
| `RB-27` | Операторский ввод допустим только для физически ненаблюдаемого внешнего факта (например, замена батареи), никогда для штатной оценки blackout event. |

## 2. Подтверждённое текущее состояние

Ниже — причины, почему Release B нельзя реализовать одной функцией в существующем handler.

- Канонический plan определяет лёгкую доменную границу, запрещает capture/safety менять модель и
  требует pure assessment/evaluation (`natural-blackout-learning-implementation.md:199-261`), а
  также snapshot именно в `start` с внутренним бюджетом 48 KiB
  (`natural-blackout-learning-implementation.md:601-607`).
- Текущий `CompletedDischarge` содержит один boolean `model_processing_eligible`, но не assessment,
  per-field decisions, snapshot или residual (`src/discharge_types.py:14-35`).
- Collector жёстко пишет natural event как `model_processing_eligible=False` и `operational`
  (`src/discharge_collector.py:281-302`, `src/discharge_collector.py:546-582`), одновременно импортируя
  и сохраняя mutable `BatteryModel` (`src/discharge_collector.py:21,51-74`).
- Start сохраняет predicted runtime, но не параметры, которыми он рассчитан
  (`src/discharge_collector.py:283-302`). После изменения live model такой event нельзя честно
  переиграть.
- Sample payload хранит wall `timestamp` (`src/discharge_collector.py:345-360`), а journal уже имеет
  правильный durable `boot_id` и `monotonic_ns` envelope (`src/discharge_journal.py:88-112`). Однако
  `observed_duration()` и monitor projection сейчас суммируют payload timestamps
  (`src/discharge_journal.py:249-256`, `src/monitor.py:625-636`).
- Journal уже фиксирует schema 1, закрытый набор record types и 64 MiB limit
  (`src/discharge_journal.py:26-35`), но `JournalHealth` не сообщает bytes/headroom
  (`src/discharge_journal.py:147-155`). При превышении limit replay только становится corrupt
  (`src/discharge_journal.py:355-374`).
- `mark_applied` уже даёт idempotent terminal marker, но payload ограничен hash/disposition
  (`src/discharge_journal.py:329-353`). Его нужно расширить данными решения, не меняя record type или
  schema version.
- `capture_only` включается после startup replay (`src/monitor.py:143-158`) и лишь сигнализирует о
  случившемся изменении (`src/monitor.py:396-412`). До этого replay вызывает scientific handler
  (`src/monitor.py:414-470`), а live completion вызывает его без hard gate
  (`src/monitor.py:750-794`).
- Handler допускает только `controlled_capacity_test` (`src/discharge_handler.py:320-335`), но после
  допуска напрямую меняет SoH/capacity/Peukert/state и вызывает `save()`
  (`src/discharge_handler.py:140-239`). Этот legacy путь нельзя использовать для shadow evaluation.
- `BatteryModel.state` публичен (`src/model.py:244-283`); direct state mutation существует в handler,
  MOTD и scheduler. Владелец модели уже имеет подходящие read-only scientific snapshot/fingerprint
  основы (`src/model.py:675-710`) и sanctioned `reset_baseline()` (`src/model.py:712-773`).
- Sag tracker уже observation-only, но всё ещё импортирует mutable model ради двух констант
  (`src/sag_tracker.py:15-17`, `src/sag_tracker.py:58-75`, `src/sag_tracker.py:176-183`).
- Health сейчас показывает journal health, pending replay и fingerprints
  (`src/monitor_config.py:293-334`, `src/monitor_config.py:369-403`), а CLI/MOTD — лишь общую journal
  строку (`scripts/battery-health.py:147-186`, `src/motd_status.py:101-115`). Event quality и residual
  пользователю не видны.
- Safety формирует `shutdown_imminent` отдельно от learning (`src/monitor.py:712-748`) и обновляет
  virtual/health outputs каждый poll (`src/monitor.py:1190-1215`). Этот порядок является release
  invariant и не меняется.

## 3. Scope

### В Release B

1. Полный vertical slice от raw poll до plain-language shadow result.
2. Immutable domain values, frozen start snapshot, charge readiness и исправленная monotonic
   provenance.
3. Pure evidence assessment и pure forward evaluation наблюдавшегося участка.
4. Hard `capture_only` gate и минимальная sole-writer граница модели.
5. Terminal decision в существующем `applied` marker.
6. Bounded health/CLI/MOTD observability и автоматический journal segment rollover.
7. Raw-poll golden replay, fault injection, real-path isolated smoke, RC gate и canary runbook.
8. Documentation truth fixes.

### Не входит; строго deferred

- **Release C:** создание `CapacityCandidate`, candidate projection, cohorts, comparability/decline
  trend и dedup этих сущностей.
- **Release D:** `CapacityUpdateProposal`, independent load-to-current calibration, изменение
  `capacity_ah_measured`, derived SoH, pre-change model snapshot и model commit.
- Обучение LUT, Peukert или IR; within-OB IR experiment; automatic deep/quick tests; battery
  replacement inference; generic runtime correction; ML/Bayesian framework.
- Изменение runtime formula, shutdown threshold, LB policy, NUT topology/unit или physical UPS.
- Backward binary compatibility и reverse migration. Текущий код обязан читать сохранённые schema-v1
  records; durable evidence не преобразуется и не удаляется (`RB-26`).
- Любая operator approval queue. `BaselineReset` остаётся отдельной командой для внешнего физического
  факта замены батареи (`RB-27`).

## 4. Рассмотренные варианты

| Вариант | Плюсы | Неприемлемый риск / решение |
|---|---|---|
| 1. Добавить natural boolean и residual расчёт в `DischargeHandler` | Мало новых файлов | Закрепляет handler, который одновременно считает, мутирует `.state`, RLS и сохраняет модель. Boolean снова смешивает history/evaluation/update, а `capture_only` остаётся post-factum alarm. Отклонено. |
| 2. Полный DDD/event-sourcing migration (`repositories`, factories, services, event bus) | Формально строгие слои | Слишком широк для одного use case, создаёт декоративные abstractions и миграционный риск в safety daemon. Отклонено каноническим правилом лёгкой доменной модели. |
| 3. **Выбран: лёгкий vertical domain slice в текущей структуре** | Два pure модуля, frozen dataclasses, явный composition root и порты только на I/O границах; один event проходит capture→assessment→evaluation→decision→report | Требует честно приватизировать model state и переподключить текущих callers, зато не создаёт framework и не цементирует handler. Это минимальный путь, который даёт compile/static и runtime proof `RB-13/RB-14`. |

Отдельный второй shadow daemon запрещён: он нарушает sole-writer/path isolation и не проверяет тот же
production lifecycle. Shadow означает «нет model mutation», а не второй процесс.

## 5. Целевой data flow и safety split

```text
NUT read-only poll
  -> UpsObservation(wall_time, monotonic_ns, boot_id, raw NUT, EMA values)
       OL -> ChargeReadinessTracker.update()
       first OB -> freeze ChargeReadinessSnapshot + BatteryModelSnapshot
                  -> journal start(schema=1, evaluation_revision, compact snapshot)
       OB -> journal sample(schema=1)
       end/signal -> journal end(schema=1, monotonic-derived lifecycle facts)
                         -> replay immutable EventProjection
                              -> assess_evidence(event)
                                   -> EvidenceAssessment + LearningDecision
                                        -> capture_only hard gate
                                             -> pure evaluate_observed_segments(...)
                                                  -> terminal applied marker
                                                       -> bounded health projection
                                                            -> battery-health / MOTD

Current persisted BatteryModel read-only view + current raw observation
  -> existing runtime/Safety Policy -> virtual UPS/LB -> upsmon
```

Evaluation output никогда не возвращается в safety branch. Safety не ждёт journal replay,
assessment, evaluator или health writer. Journal/evaluation failure может ухудшить capture health, но
не подавляет physical status, virtual LB или watchdog (`RB-06`).

## 6. Обязательные API и type contracts

Контракты ниже задают смысл и поля; конкретный Python syntax может отличаться только без изменения
семантики и test oracles.

### 6.1 Immutable values (`src/discharge_types.py`)

```python
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]       # canonical codes, max 8, each <= 64 UTF-8 bytes
    additional_reason_count: int  # overflow только этого Decision

@dataclass(frozen=True)
class LearningDecision:
    record_history: Decision
    record_model_residuals: Decision
    record_terminal_candidate: Decision
    propose_capacity_update: Decision

@dataclass(frozen=True)
class LutPointSnapshot:
    voltage: float
    soc: float
    source: str

@dataclass(frozen=True)
class BatteryModelSnapshot:
    snapshot_schema_revision: int     # 1
    battery_epoch_id: str
    persisted_model_hash: str
    scientific_fingerprint: str
    rated_capacity_ah: float
    nominal_voltage: float
    nominal_power_watts: float
    soh: float
    peukert_exponent: float
    ir_k_volts_per_percent: float
    reference_load_percent: float
    lut: tuple[LutPointSnapshot, ...]

EvidenceClass = Literal[
    "operational_partial",
    "operational_gapped",
    "natural_policy_endpoint",
    "natural_terminal_candidate",
    "controlled_capacity_test",
    "cal_or_self_test",
    "history_only",
]

TerminationKind = Literal[
    "power_restored",
    "policy_shutdown",
    "service_stop_signal",
    "controlled_test_complete",
    "history_only",
]

PipelineReason = Literal[
    "capture_failed",
    "evidence_gap",
    "journal_write_failed",
    "journal_sync_ambiguous",
    "conflicting_record",
    "retry_sidecar_invalid",
    "historical_rebuild_pending",
    "rebuild_spool_write_failed",
    "disk_growth_warning",
    "disk_growth_critical",
]

@dataclass(frozen=True)
class ChargeReadinessSnapshot:
    boot_id: str
    continuous_ol_seconds: float
    sample_count: int
    min_voltage: float | None
    max_voltage: float | None
    trailing_30m_voltage_span: float | None
    max_gap_seconds: float
    ready_for_terminal_evidence: bool
    reasons: tuple[str, ...]          # ordered readiness codes, max 8, each <= 64 bytes
    additional_reason_count: int

@dataclass(frozen=True)
class VoltageSummary:
    first_three_median_v: float | None
    last_three_median_v: float | None
    endpoint_movement_v: float | None
    minimum_v: float | None
    maximum_v: float | None

@dataclass(frozen=True)
class LoadSummary:
    mean_percent: float | None
    minimum_percent: float | None
    maximum_percent: float | None
    range_percentage_points: float | None
    population_stddev_percentage_points: float | None

@dataclass(frozen=True)
class EvidenceAssessment:
    event_id: str
    evidence_class: EvidenceClass
    observed_duration_seconds: float
    sample_count: int
    expected_sample_count: int
    coverage_ratio: float
    max_gap_seconds: float
    operational_gapped: bool
    termination_kind: TerminationKind
    voltage: VoltageSummary
    load: LoadSummary
    quality_reasons: tuple[str, ...]  # DecisionReason only, max 8, each <=64 bytes
    quality_additional_reason_count: int
    decision: LearningDecision

@dataclass(frozen=True)
class ModelResiduals:
    evaluation_algorithm_revision: str
    evaluated_sample_count: int
    delivered_ah_proxy: float
    start_voltage_residual_v: float
    end_voltage_residual_v: float
    mean_voltage_residual_v: float
    rmse_voltage_residual_v: float
    observed_slope_v_per_hour: float
    predicted_slope_v_per_hour: float
    slope_residual_v_per_hour: float

@dataclass(frozen=True)
class TerminalOutcome:
    disposition: Literal["recorded_only", "rejected"]
    assessment: EvidenceAssessment
    residuals: ModelResiduals | None
    marker_time_persisted_model_hash: str

@dataclass(frozen=True)
class FrozenJournalAppend:
    record_type: Literal["start", "sample", "end", "applied"]
    event_id: str
    seq: int                         # contiguous per-event sequence, start == 0
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    serialized_record: bytes
    sha256: str

@dataclass(frozen=True)
class RetryableProcessingFailure:
    stage: Literal["start", "sample", "end", "applied"]
    frozen_record: FrozenJournalAppend
    reason: PipelineReason
    attempt_count: int
    next_retry_monotonic_ns: int

ProcessingResult = TerminalOutcome | RetryableProcessingFailure
```

`BatteryModelSnapshot.persisted_model_hash` — hash persisted-модели в момент исходного `start`.
`TerminalOutcome.marker_time_persisted_model_hash` — отдельно заново прочитанный persisted hash в
момент записи terminal marker. Эти значения не подменяют друг друга и не сравниваются после события.

`TerminalOutcome` и `RetryableProcessingFailure` взаимно исключаются: domain result никогда не
содержит retry flag, а infrastructure retry никогда не masquerade-ит как terminal domain decision.
Health проецирует оба варианта через отдельные `last_terminal_outcome` и
`last_retryable_processing_failure` fixed-shape поля.

`EvidenceClass` выводится total-функцией в следующем порядке, первое совпадение побеждает:

1. unsupported/legacy/missing-or-mismatched epoch либо legacy marker → `history_only`;
2. classifier/raw status содержит CAL/self-test → `cal_or_self_test`;
3. reboot, non-increasing monotonic или acquisition gap `>25.000s` → `operational_gapped`;
4. sanctioned controlled test → `controlled_capacity_test`;
5. natural blackout + sticky policy endpoint → `natural_policy_endpoint`;
6. natural blackout + independently observed physical `LB` before end →
   `natural_terminal_candidate` (в B всё равно candidate denied/deferred);
7. любой оставшийся новый structurally valid event → `operational_partial`.

`termination_kind` не принимает свободный текст. Mapping из durable lifecycle исчерпывающий:

| Durable end fact | `TerminationKind` |
|---|---|
| raw transition OB→OL | `power_restored` |
| event-scoped sticky policy + shutdown signal | `policy_shutdown` |
| shutdown signal без sticky policy | `service_stop_signal` |
| sanctioned controlled-test completion | `controlled_test_complete` |
| immutable legacy/history-only projection | `history_only` |

Неизвестное значение lifecycle является `lifecycle_invalid`/`rejected`, а не новым enum value.

В Release B решения `record_terminal_candidate` и `propose_capacity_update` всегда `allowed=False`
с catalog-причинами `physical_terminal_deferred_release_c` и
`capacity_update_deferred_release_d`. Они существуют для честного пользовательского
ответа, но не создают candidate/proposal objects или persistence (`RB-07`).

### 6.2 Model owner (`src/model.py`)

- Внутренние dict/physics переименовать в `_state`/`_physics`; внешние writable `.state` и `.physics`
  удалить. Read-only tooling
  получает deep immutable/copy projection через `read_state()`; mutation копии не меняет model.
- `snapshot_for_evaluation() -> BatteryModelSnapshot` — единственный API, которым composition root
  получает compact scientific inputs. Он синхронно снимает значения и hash до `start`, ничего не
  сохраняет.
- Collector/sag/evaluator и shared closed-event processor получают готовые scalars/dataclasses, не
  model object, save callback или mutation protocol. Release-B composition передаёт им **zero
  scientific mutation capability**.
- Публичный `save()` становится приватным `_save_owned_state()` и вызывается только самим
  `BatteryModel` из sanctioned owner methods. Инициализация отсутствующего state получает отдельный
  owner method; read/health/report paths не сохраняют файл.
- `set_peukert_exponent`, `set_rls_state`, `add_soh_history_entry`, `append_capacity_estimate` и другие
  legacy scientific setters без production callers удалить либо сделать private owner-only; legacy
  handler не получает доступ к ним. Допустимые mutations в B исчерпываются owner methods
  `initialize_if_absent()`, `reset_baseline()` и operational `record_upscmd_result()`; последний не
  включён в capture-only composition, потому что command capability отсутствует.
- Будущий `apply_capacity_update()` не создаётся в B. Его API появится только вместе с отдельно
  доказанной automatic safety policy; `capacity_ah_measured=None` не блокирует ничего в ожидании
  человека.
- `scripts/battery-health.py` продолжает читать JSON как data; `motd_status` и scheduler используют
  read-only APIs, не `.state`.

Static и bounded call-graph guards проверяют весь `src/` graph: вне `src/model.py` нет обращения к
`_state`/`_physics`, присваивания или mutation `.state`/`.physics`, вызова public/private save,
scientific setter/add/append method, RLS `update`/state mutation либо передачи такого callable в
capture/evidence/evaluation. Dynamic
bomb doubles подтверждают ту же недостижимость для startup replay, live completion и retry paths.

Это не новый repository/service framework: один существующий model owner, один snapshot method и
read-only projection (`RB-14`).

### 6.3 Charge readiness (`src/discharge_collector.py`)

- Tracker получает каждый успешный, полностью валидный OL observation до event transition.
- Same-boot elapsed использует только monotonic clock. Хранится bounded deque последних 30 минут для
  voltage span; вся 12-hour история не сохраняется.
- Reset: первый OB (после того как snapshot скопирован), CAL, boot change/process start либо
  acquisition gap строго `>25.000s`. Gap `<=25.000s` сохраняет достижимую continuous-OL серию;
  elapsed суммируется только по same-boot monotonic OL edges.
- Ready только если continuous OL `>= 12h`, raw voltage всё окно 13.0–14.5 V, trailing 30m span
  `<= 0.30V`, max gap `<=25s`; это constants с golden fixture, не config knobs.
- Restart внутри окна автоматически даёт `readiness_process_restart`; человек это не подтверждает.
  Readiness reasons используют только следующий порядок и namespace:
  `readiness_process_restart`, `readiness_boot_changed`, `readiness_cal_observed`,
  `readiness_ol_gap_above_25s`, `readiness_ol_duration_below_12h`,
  `readiness_voltage_unavailable`, `readiness_voltage_out_of_13_14_5v`,
  `readiness_trailing_span_above_0_30v`. При ready tuple пуст; иначе первые 8 codes и per-snapshot
  `additional_reason_count`. Running 12-hour min/max/count занимают O(1), deque остаётся только 30m.

### 6.4 Journal schema-v1 payloads (`src/discharge_journal.py`)

Envelope и record types не меняются. Расширяются только bounded payloads.

Writer API меняется явно, wire schema — нет. Collector один раз создаёт UUID event ID до первой
persistence attempt и передаёт immutable `ObservationEnvelope(boot_id, wall_time_utc, monotonic_ns)`
для каждой физической observation. Контракты:

```python
start_event(start, *, event_id: str, envelope: ObservationEnvelope) -> EventCursor
append_sample(cursor, sample, *, envelope: ObservationEnvelope) -> EventCursor
close_event(cursor, end, *, envelope: ObservationEnvelope) -> FrozenJournalAppend
append_frozen(record: FrozenJournalAppend) -> EventCursor
```

`event_id` обязан быть canonical UUID, а `seq` — contiguous per-event (`start=0`). Production clocks
читаются только при создании `ObservationEnvelope`; journal не заменяет переданные boot/time значения
своими clock calls. `append_frozen` принадлежит sole writer, принимает только уже провалидированный
canonical envelope и используется retry/promotion; он не доступен collector/evaluator как общий
arbitrary-write capability. Поэтому retry и spool promotion могут сохранить исходные ID/times/bytes
без изменения schema-v1 record shape.

`start.payload` обязан содержать:

- текущий `battery_epoch_id`;
- `evaluation_algorithm_revision="shadow-forward-v1"`;
- `model_snapshot` и `model_snapshot_encoded_bytes`;
- `charge_readiness_snapshot` с последнего OL poll;
- существующие raw NUT/classification fields.

Перед append serializer проверяет snapshot budget **48 KiB** и полный encoded start line **<64 KiB**.
Если snapshot не помещается, start всё равно записывает raw history с
`model_snapshot=null`, `model_snapshot_status="budget_exceeded"`; assessment автоматически запрещает
residuals с `model_snapshot_budget_exceeded`. Нельзя молча усечь LUT или взять live model позже.

`sample.payload.timestamp` сохраняется как legacy wall observation metadata. Для duration, coverage,
max gap и интеграла используются только record envelope `monotonic_ns` внутри одинакового `boot_id`.
Единственное определение duration одинаково для no-gap и gapped events:

1. Построить ordered **OB observation points** `[start, samples...]`; `end` является lifecycle fact,
   но не observation point. Разбить точки на same-boot spans;
   reboot marker, boot change или non-increasing monotonic value
   ставит `operational_gapped=true`.
2. Для каждой соседней пары в одном boot с `0 < delta <=25.000s` принять observed edge; больший delta
   фиксируется как gap и не принимается. `observed_duration_seconds = sum(accepted_delta_ns)/1e9` для
   любого event. Поэтому no-gap telescopes в `last_ob_sample-start`, а gapped event использует тот же
   смысл. Неизвестный terminal tail `last_ob_sample→end` не входит ни в duration, ни в physics.
3. Интеграл/evaluator используют ровно те же accepted edges; reboot/gap time не прибавляется.
4. Для каждого maximal contiguous span OB observation points
   `expected = 1 + floor((last_ob_point-first_ob_point) / 10s)`; coverage =
   `min(1, durable_ob_samples / sum(expected))`.
   `durable_ob_samples` включает валидную OB observation из `start` как первую точку и каждую
   durable `sample` с raw `OB`; `end` никогда не считается точкой. Если transition handler имеет
   более свежую последнюю OB observation, он сначала append-ит её как обычный `sample`, затем `end`.
   Эти же точки образуют `n`, voltage/load arrays и evaluator input — отдельного подсчёта для gate нет.
5. `max_gap_seconds` — максимум положительных соседних same-boot OB-observation deltas до разбиения.
   End tail отдельно показывается как lifecycle metadata и не влияет на gate. Gap
   `>25s` запрещает
   residuals и классифицирует observation как `operational_gapped`.

`applied.payload` расширяется до:

```text
model_hash, disposition,
processing_revision="release-b-v1",
evidence_assessment (bounded summary),
learning_decision (четыре bounded decisions),
model_residuals (bounded scalar summary | null)
```

Повторная запись идентичного payload idempotent; несовпадающий marker fail-closed. Для retry frozen
serialized applied envelope является authority. Replay-oracle сравнивает canonical
`processing_revision + evidence_assessment + learning_decision + model_residuals`; envelope clocks,
attempt metadata и wire-key `model_hash` (доменное имя
`marker_time_persisted_model_hash`) исключены из equality, потому что это факты
момента первой marker-попытки. Они никогда не перечитываются и не пересериализуются при retry.
Schema-v1 wire key не переименовывается: новый serializer продолжает писать существующий
`applied.payload.model_hash`; parser проецирует его в доменное поле
`marker_time_persisted_model_hash`. Уже durable Release-A marker с `model_hash` и без disposition
остаётся history-only по существующему precedence, а idempotency никогда не сравнивает разные имена.
Legacy `applied`
без `disposition` остаётся terminal history-only и не переоценивается. Missing/other epoch также не
переоценивается (`RB-16/RB-26`).

### 6.5 Automatic journal capacity policy

Текущий hard stop 64 MiB без visibility несовместим с unattended capture. Release B поэтому обязан
добавить segment rollover, не retention deletion:

- до создания exact Release-B RC отдельно собирается и проверяется обязательный segment-aware
  Release-A rollback artifact из exact tag `release-a-20260815` (§9–10);
- active path остаётся `discharge-events-v1.jsonl`;
- до append, который превысит 64 MiB или 100,000 records, writer под уже удерживаемым process lock
  делает `fdatasync`, атомарно переименовывает active файл в
  `discharge-events-v1.segment-NNNNNN.jsonl`, sync parent и создаёт новый mode-0600 active файл;
- rollover разрешён и внутри открытого event; seq/event ID продолжаются в новом segment;
- replay читает segments по numeric generation и active как один логический schema-v1 stream, поэтому
  segment может начинаться с `sample` и заканчивать event из предыдущего segment;
- crash после rename и до create восстанавливается сканированием поколений и созданием active файла;
  строки не переписываются и не удаляются;
- перед каждым rollover проверяется disk reserve не меньше `2 * 64 MiB`. При недостатке места или
  ошибке rollover safety продолжает работать, `capture_available=false`, ошибка bounded, append
  автоматически повторяется на следующих polls; доменного approval нет;
- health показывает active bytes, 64-MiB limit, percent/headroom, segment count, total bytes,
  last rollover/error и `capture_available`;
- health также показывает filesystem free bytes, average sealed-segment growth/day за bounded
  последние 8 rollovers и `disk_growth_alarm=ok|warning|critical`: warning при free `<1 GiB` либо
  headroom `<8 * 64 MiB`, critical при free `<256 MiB` либо headroom `<2 * 64 MiB`. Transition
  логируется сразу, затем не чаще раза в час, а CLI/MOTD объясняют риск остановки capture;
- sealed segments никогда автоматически не удаляются. Storage cleanup/retention не входит в B и не
  может уничтожать evidence.

Полный logical replay всех segments разрешён только как **порционная** startup/isolation recovery
работа; rollover и ambiguous-write reconciliation читают только затронутый tail/delta. Обычный
1-second poll не перечитывает историю. Journal владеет
mode-0600 atomic `discharge-events-v1.projection-index`, содержащим schema/revision, последний
проверенный `(generation, byte_offset, event_id, per_event_seq)`, derived counters, open event cursor,
последний closed-unapplied cursor/end hash и bounded last terminal summary. `seq` нигде не глобальный:
каждый event начинается с `0` и продолжается `1..n`. Append обновляет journal bytes, затем index и
directory sync под тем же lock;
индекс является только пересоздаваемой проекцией, не evidence. При missing/hash/offset mismatch
startup не выполняет синхронный full replay до первого poll. Обязательный порядок:

1. read-only model validation, fingerprint и hard `capture_only` gate;
2. первый physical NUT poll, safety/LB computation и atomic virtual-UPS publication;
3. только после публикации — sidecar reconciliation и incremental historical rebuild под lock;
4. каждый rebuild slice читает не более `1 MiB` и `10,000 records` и прекращается также при
   `50 ms` monotonic wall budget; следующий safety poll/pre-existing watchdog имеет приоритет;
5. между polls slices продолжаются с durable rebuild cursor. `journal_projection_ready=false`,
   progress bytes/records/segments и bounded error видны до atomic index commit;
6. пока глобальный append cursor неизвестен, каждый новый physical transition и последующие точки
   автоматически fsync-ятся в отдельный mode-0600 append-only
   `discharge-events-v1.rebuild-spool.jsonl`. Spool имеет собственный schema/revision и локальный seq,
   но сохраняет заранее созданный UUID event ID, original boot/wall/monotonic envelopes, raw payload,
   start-time model/readiness snapshots и полный start/sample/end lifecycle. Он не является model
   input сам по себе и не требует глобального journal seq;
7. после atomic index commit spool автоматически replay-ится в порядке spool-local ordering:
   spool-local ordering number отбрасывается и никогда не попадает в main envelope; canonical main
   records сохраняют original event ID/times/payloads и получают contiguous **per-event** seq
   `start=0`, затем `1..n`;
   start/sample/end и затем обычный assessment/applied marker становятся durable в основном journal.
   Только после byte/hash/lifecycle verification promotion помечается durable `promoted` record в
   spool; повторный restart идемпотентен. OL до завершения rebuild закрывает event durable в spool, а
   не удаляет pending start. Поэтому blackout во время recovery получает обычное terminal решение,
   а не теряется как одна health-ошибка;
8. если OB ещё продолжается в момент готовности index, promotion пишет durable start/samples в main
   journal, под тем же sole-writer lock переносит ownership открытого event ID/cursor в основной
   collector и только затем пишет spool marker `promoted_open`. Следующие samples/end идут прямо в
   main journal. Restart в любой точке сверяет event ID и hashes: уже перенесённые records не
   дублируются, а незавершённый transfer повторяется идемпотентно;
9. закрытый spool event до promotion виден как
   `last_pipeline_status=historical_rebuild_pending`,
   `rebuild_spool_closed_awaiting_promotion_count=1`, `pending_replay=false`; это ожидание
   infrastructure promotion, не domain approval и не applied-stage retry;
10. spool append failure использует `rebuild_spool_write_failed`, оставляет safety активной и явно
   ставит `capture_available=false`; successful spool append восстанавливает latch. Spool участвует
   в тех же free-space alarms; proven-promoted spool generation является пересоздаваемой копией и
   может быть атомарно очищена только после проверки основного journal, незавершённые bytes никогда
   автоматически не удаляются.

Эти constants внутренние и не config knobs. First physical poll и virtual publication должны
завершиться не позднее `2 * polling_interval` при отвечающем fake NUT независимо от 0/10/100 sealed
segments. В steady state `_journal_projection_for_poll()` проверяет
только stat/identity и читает O(delta) bytes от сохранённого offset; virtual UPS/LB публикуется до
необязательного health-delta refresh и никогда не ждёт полного historical replay. Fixture с 0, 10 и
100 sealed segments требует одинаковое число прочитанных steady-state bytes/calls при пустом delta.
Если append journal уже durable, а atomic запись projection-index завершилась ошибкой, старый index
остаётся валидной нижней границей: следующий poll читает только bounded delta от старого offset,
атомарно пересобирает index под lock и возвращается к обычному O(delta) пути. Ошибка index никогда
не запускает полный replay в safety poll и не задерживает virtual UPS/LB.

Ни один public write/resume API не вызывает `replay()` внутри. Release B заменяет текущие контракты:

- `resume_event(event_id, projection_cursor)` валидирует open-event cursor из projection-index и
  затронутый active-tail delta; отсутствие/несовпадение переводит работу в incremental rebuild/retry
  после safety publication, но не запускает full replay inline;
- `mark_applied(event_projection, frozen_applied)` получает уже проверенный closed-unapplied cursor,
  end hash и последний per-event seq из projection/index; под lock проверяет только delta от
  сохранённого offset и append-ит следующий per-event seq;
- live close сначала публикует physical/virtual safety result, затем pure assessment и marker work
  выполняются в том же bounded post-safety budget. Historical ended-unapplied queue обрабатывается
  порциями между polls;
- missing/stale projection никогда не разрешает запись «по догадке»: marker/resume остаются retryable
  до bounded reconciliation. Full logical replay допустим только incremental recovery path §6.5.

Fixtures на 0/10/100 sealed segments обязаны отдельно измерить `resume_event`, close-event assessment
и `mark_applied`: число historical bytes/calls не зависит от segment count, virtual UPS/LB уже
опубликован, а post-safety work соблюдает 1-MiB/10k-record/50-ms slice ceiling.

Runtime feature flag для rollover отсутствует: после выполненного rollback-artifact precondition
rollover входит в exact Release-B candidate всегда. Canary запускает тот же package/commit/hash,
который прошёл RC gates; после gates код, config и enablement не меняются.

Middle corruption и unknown schema не могут навсегда остановить новые observations. При replay
writer под process `LOCK_EX`:

1. находит `corruption_offset` и последний `validated_prefix_boundary` сразу после полной валидной
   newline-terminated record; фиксирует original path, byte size, full-file SHA-256 и prefix SHA-256;
2. не truncates и не переписывает ни одного его byte;
3. атомарно переименовывает весь segment в
   `discharge-events-v1.isolated-NNNNNN-<sha256-prefix>.jsonl`, sync parent;
4. append+sync-ит immutable entry в mode-0600
   `discharge-events-v1.isolation-manifest.jsonl`: original/isolation basename, full hash/size,
   corruption offset, validated prefix boundary/hash, reason и generation. Existing manifest entries
   immutable; повторный restart с тем же full hash idempotent и не создаёт второй entry;
5. logical replay читает isolated file только до manifest prefix boundary. Он включает все события с
   валидным durable `end`, полностью находящиеся в trusted prefix (включая event, начавшийся в
   предыдущем segment). Ended-but-unapplied событие снова проходит обычный snapshot-only
   `process_closed_event()` и automatic terminal-marker retry. Исключаются только event, открытый или
   crossing на boundary, и весь untrusted suffix. Counters/last disposition строятся из этой
   projection;
6. crossing event получает pipeline status/reasons `capture_failed`/`evidence_gap`, никогда не обучает и не получает
   synthetic start/end/applied;
7. writer создаёт/продолжает чистый active generation и автоматически возобновляет capture на следующем
   валидном poll.

Isolation transaction восстанавливается идемпотентно после каждого crash window. При startup writer
под lock сначала валидирует manifest как append-only journal: torn final manifest suffix truncates к
последней полной newline entry с `ftruncate+fdatasync+directory sync`; middle corruption самого
manifest изолирует manifest bytes и rebuild-ит его из неизменяемых isolated filenames/full hashes.
Затем scan находит orphan isolated file после rename-before-manifest, повторно вычисляет full/prefix
hash и corruption boundary, дописывает missing manifest entry, sync-ит её и лишь затем создаёт active.
Crash после manifest sync, но до active create просто создаёт missing active; повторный restart с тем
же full hash ничего не дублирует.

Health хранит bounded count, last isolated path basename/hash/reason и `capture_available` после
успешного нового append. Isolated bytes сохраняются как raw forensic evidence; ручное domain approval
не требуется. Это отличается от torn final suffix: только доказанный torn tail по-прежнему можно
durably truncate по существующему правилу.

Restart tests обязаны доказать: manifest torn-tail/middle-corruption recovery; rename-before-manifest и
manifest-before-active crash windows; manifest idempotency; те же complete/ended-but-unapplied prefix
events/counters до и после restart; crossing event не учитывается; новый event получает новый ID/seq
chain; full isolated hash и prefix hash неизменны. Так active operation остаётся bounded и не требует
routine operator rotation.
Disk exhaustion всё равно является infrastructure incident, а не поводом автоматически удалить
доказательства: warning/critical alarm не запускает retention, compression или deletion. Запуск
оригинального Release A на пустом active файле при спрятанных segments запрещён.

### 6.6 Pure assessment/evaluation

```python
def assess_evidence(event: EventProjection) -> EvidenceAssessment: ...

def evaluate_observed_segments(
    *, snapshot: BatteryModelSnapshot, event: EventProjection,
) -> ModelResiduals: ...
```

Обе функции не читают файлы, clock, config или current `BatteryModel` и не пишут journal/model.

Assessment использует durable samples, соответствующие raw OB observations. Voltage берётся из EMA.
Load берётся из EMA, а только для первого OB sample до прогрева EMA — из сохранённого finite raw
`ups.load`; collector больше не подставляет sentinel `0.0`. Если ни EMA, ни raw load недоступны либо
реальный load равен нулю, событие остаётся валидной историей, но residual автоматически запрещён.
При `n >= 3` summaries считаются точно так:

```text
first_three_median_v = median(V[0:3])
last_three_median_v = median(V[n-3:n])
endpoint_movement_v = first_three_median_v - last_three_median_v
minimum_v = min(V); maximum_v = max(V)

mean_load = sum(L) / n
load_range_pp = max(L) - min(L)
load_population_stddev_pp = sqrt(sum((x - mean_load)^2 for x in L) / n)
```

Формулы total для малых выборок и никогда не бросают исключение:

- `n=0`: все поля `VoltageSummary` и `LoadSummary` равны `None`; assessment получает
  `sample_count_below_31`, затем остальные применимые причины, не evaluator exception;
- `n=1`: voltage first/last/min/max = единственное значение, movement `0.0`; load
  mean/min/max = значение, range/stddev `0.0`;
- `n=2`: voltage first = `V[0]`, last = `V[1]`, movement = `V[0]-V[1]`, min/max обычные; load
  summary использует те же population formulas с делителем `n=2`;
- `n>=3`: используются first/last three medians и общие formulas выше.

Structural/non-finite validation выполняется до numeric summary; invalid series получает `None` в
невычислимых полях и ordered reject code, а не NaN/Infinity.

Frozen inclusive gates для `record_model_residuals=yes`:

| Поле | Pass |
|---|---|
| natural provenance | classifier `BLACKOUT_REAL`; каждый используемый raw status содержит `OB`, ни один не содержит `CAL` |
| duration | `observed_duration_seconds >= 300.000` |
| sample count | `n >= 31` и lengths voltage/load/records равны |
| coverage | `coverage_ratio >= 0.900000` |
| max gap | `max_gap_seconds <= 25.000` и нет reboot/unknown span |
| voltage validity | каждое `V` finite и `8.000 <= V <= 16.000` V |
| voltage movement | `endpoint_movement_v >= 0.200` V |
| load validity | каждое `L` доступно, finite и `0.000 < L <= 100.000` percent |
| load range | `load_range_pp <= 5.000` percentage points |
| load noise | `load_population_stddev_pp <= 2.000` percentage points |
| frozen inputs | snapshot присутствует, уложился в budget и revision равна `shadow-forward-v1` |

`0.200 V` — два шага текущего 0.1-V NUT representation, поэтому один quantization step не выдаётся
за signal. Load range `5 pp` и population sigma `2 pp` допускают несколько шагов текущего 1-pp load
representation, но консервативно отсеивают изменение рабочей нагрузки. 300 s/31 points оставляют
минимум 30 десятисекундных intervals; отдельный 10-minute fixture доказывает применимость без
подгонки порогов. Эти constants живут в `discharge_evidence.py`, не в config.

Полный `DecisionReason` catalog и его **единственный порядок**:

| # | Code | Класс/эффект |
|---:|---|---|
| 1 | `journal_schema_unsupported` | isolate, history-only, не оценивать |
| 2 | `lifecycle_invalid` | `rejected` |
| 3 | `payload_malformed` | `rejected` |
| 4 | `epoch_missing_history_only` | history-only, не переоценивать |
| 5 | `epoch_mismatch_history_only` | history-only, не переоценивать |
| 6 | `legacy_applied_without_disposition` | history-only terminal, не переоценивать |
| 7 | `series_length_mismatch` | `rejected` |
| 8 | `voltage_non_finite` | `rejected` |
| 9 | `voltage_out_of_8_16v_range` | `rejected` |
| 10 | `load_non_finite` | `rejected` |
| 11 | `load_out_of_0_100pct_range` | отрицательный либо `>100%`: `rejected` |
| 12 | `raw_status_not_ob` | `rejected` |
| 13 | `monotonic_not_strict_same_boot` | `rejected` |
| 14 | `reboot_gap_present` | `recorded_only`, residual denied |
| 15 | `sample_gap_above_25s` | `recorded_only`, residual denied |
| 16 | `cal_present` | `recorded_only`, residual denied |
| 17 | `not_natural_blackout` | `recorded_only`, residual denied |
| 18 | `duration_below_300s` | `recorded_only`, residual denied |
| 19 | `sample_count_below_31` | `recorded_only`, residual denied |
| 20 | `coverage_below_0_90` | `recorded_only`, residual denied |
| 21 | `load_unavailable_or_zero` | `recorded_only`, residual denied |
| 22 | `voltage_movement_below_0_20v` | `recorded_only`, residual denied |
| 23 | `load_range_above_5pp` | `recorded_only`, residual denied |
| 24 | `load_stddev_above_2pp` | `recorded_only`, residual denied |
| 25 | `model_snapshot_missing` | `recorded_only`, residual denied |
| 26 | `model_snapshot_budget_exceeded` | `recorded_only`, residual denied |
| 27 | `evaluation_revision_unsupported` | `recorded_only`, residual denied |
| 28 | `model_evaluation_failed` | deterministic evaluator failure: `recorded_only`, residual null/denied, retry false |
| 29 | `power_restored_not_terminal` | terminal candidate denied; residual не блокирует |
| 30 | `virtual_policy_endpoint_model_derived` | terminal candidate denied; residual denied |
| 31 | `physical_terminal_deferred_release_c` | terminal candidate denied in B; residual не блокирует |
| 32 | `capacity_update_deferred_release_d` | capacity proposal denied in B |
| 33 | `shadow_evaluation_accepted` | residual allowed |
| 34 | `history_recorded` | history allowed |

Assessment проходит catalog сверху вниз, никогда не сортирует строки лексикографически и не зависит
от порядка dict/set. Для каждого `Decision.reasons` берутся первые 8 применимых codes в этом порядке;
остаток отражается в **его собственном** `Decision.additional_reason_count`, поэтому decisions с
разным набором причин не делят ambiguous global overflow. Precedence:

1. Codes 1/4/5/6 означают immutable history-only и запрещают новую assessment marker запись.
2. Любой code 2/3/7–13 даёт terminal `rejected` и residual denied.
3. При отсутствии reject любой code 14–28/30 даёт `recorded_only` с residual denied. Code 28
   добавляет processing boundary, если pure evaluator вернул exception: создаётся новый immutable
   `LearningDecision` с residual denied; retry не создаётся.
4. Только отсутствие residual blockers добавляет code 33 и разрешает residuals; итог всё равно
   `recorded_only`, потому что Release B модель не меняет.
5. `record_terminal_candidate` всегда denied. Для событий, дошедших до допустимой observed
   termination, он получает один из 29/30/31; для CAL, gapped, rejected и иных более ранних отказов
   он наследует применимые блокирующие codes из общего каталога в том же precedence-порядке.
   `propose_capacity_update` всегда denied code 32. `record_history` получает code 34 для нового
   валидного current-epoch event; если history запрещена, её Decision аналогично наследует
   соответствующие codes 1/4/5/6 вместо искусственного terminal code.

Весь machine-readable reason namespace исчерпывается тремя disjoint типами: ordered
`DecisionReason` 1–34 выше, ordered readiness catalog §6.3 и ordered `PipelineReason` в порядке его
`Literal` declaration §6.1. Строки `capture_failed` и `evidence_gap` существуют только как pipeline
status/reason, не подмешиваются в `Decision.reasons`. Неизвестный reason запрещён serializer-ом;
bounded human diagnostic хранится отдельно в `last_error` и не участвует в ordering/goldens.

Boundary fixtures обязательны для каждого числового gate:

| Gate | Ниже | Ровно | Выше |
|---|---:|---:|---:|
| duration min | `299.999` fail | `300.000` pass | `300.001` pass |
| sample count min | `30` fail | `31` pass | `32` pass |
| coverage min | `0.899999` fail | `0.900000` pass | `0.900001` pass |
| max gap max | `24.999` pass | `25.000` pass | `25.001` fail |
| endpoint movement min | `0.199` fail | `0.200` pass | `0.201` pass |
| load range max | `4.999` pass | `5.000` pass | `5.001` fail |
| load stddev max | `1.999` pass | `2.000` pass | `2.001` fail |
| voltage lower bound | `7.999` fail | `8.000` pass | `8.001` pass |
| voltage upper bound | `15.999` pass | `16.000` pass | `16.001` fail |
| load lower bound | `-0.001` rejected | `0.000` recorded-only | `0.001` pass |
| load upper bound | `99.999` pass | `100.000` pass | `100.001` fail |

Fixtures также переставляют одновременно применимые failures и требуют тот же ordered bounded reason
tuple и `additional_reason_count`.
В live pipeline physical observation validator уже отклоняет voltage `>15.0 V`, поэтому верхняя
граница assessment `16.0 V` является defence-in-depth для replay/imported evidence. Boundary fixture
вызывает pure assessor напрямую и отдельно доказывает, что live validator не пропускает `15.001 V`;
имплементер не должен ослаблять physical validator ради достижимости assessment fixture.

Forward evaluator реализует канонический алгоритм без нового коэффициента:

1. Initial SoC = existing `soc_from_voltage(ir_compensate(first V/load, frozen IR), frozen LUT)`.
2. Для каждого monotonic interval взять фактическую load, вычислить existing full-runtime formula из
   frozen rated capacity/SoH/Peukert и уменьшить predicted SoC только на `delta_t` этого interval.
3. Инвертировать frozen LUT линейно с теми же clamp rules; применить обратную frozen IR correction.
4. Для каждого interval вычислить ток-proxy
   `load/100 * nominal_power/nominal_voltage`, trapezoid-интегрировать A·s по monotonic seconds и
   разделить сумму на `3600`, получив `delivered_ah_proxy` в Ah.
5. Observed slope = `(median(last 3 V) - median(first 3 V)) / (median(last 3 monotonic times) -
   median(first 3 monotonic times)) * 3600`; predicted slope использует те же timestamps и predicted
   V. Вернуть scalar residual summary только для реально наблюдавшегося участка.
6. Знак всех signed residuals един: `observed - predicted`. Поэтому
   `start/end/mean_voltage_residual_v = observed_voltage_v - predicted_voltage_v`, а
   `slope_residual_v_per_hour = observed_slope_v_per_hour - predicted_slope_v_per_hour`.
   Отрицательное voltage residual в renderer означает «наблюдаемое напряжение ниже прогноза»;
   `rmse_voltage_residual_v = sqrt(mean((observed-predicted)^2))` всегда неотрицателен.

Нельзя вычислять «actual runtime», продолжать кривую после OL, использовать virtual LB как truth,
подставлять journal model-derived SoC как target либо сравнивать с live model (`RB-03/04/05/11`).

### 6.7 Hard gate, outcomes и endpoint classification

Startup order является частью hard gate: strict read-only model validation → baseline scientific
fingerprint → `capture_only=True` и удаление всех scientific/command capabilities из composition →
первый physical poll и virtual safety publication → bounded sidecar reconciliation/incremental
journal rebuild, параллельно с durable rebuild-spool capture до готовности cursor → automatic spool
promotion → единый `process_closed_event()`. Ни один replay record,
включая исторический eligible `controlled_capacity_test`, не достигает legacy handler/save до
включения gate. Fixture с таким closed event при startup ставит bomb на handler/save/RLS и требует
ноль вызовов.

Порядок live completion и replay одинаков:

1. Получить immutable projection и assessment.
2. Если mode не `capture_only`, Release-B binary fail-closed: неизвестный mode не открывает writer.
3. В `capture_only` legacy `DischargeHandler.apply_completed_discharge()` не вызывается вообще.
4. Разрешён только pure evaluator и journal `applied` write. Model `save`, scientific setters, RLS
   update и `NUTClient.send_instcmd` недостижимы; production scheduler не получает command capability.
5. Записать terminal `recorded_only` или `rejected` и bounded reasons.

Fingerprint check остаётся alarm против нарушения инварианта, но не считается gate.

`pending_replay=true` ставится только когда durable `end` есть, terminal `applied` ещё нет и повтор
может завершиться после transient infrastructure failure. Domain reject, missing snapshot, CAL, gap,
short/noisy event и deterministic evaluator error получают `TerminalOutcome`; они никогда не
создают retry state.

Автоматическое поведение persistence stages:

До **первой** попытки любого append journal создаёт полный immutable envelope ровно один раз:
`schema_version`, record type, event ID, seq, original boot ID, original wall time, original
`monotonic_ns` и payload; canonical serializer один раз создаёт `FrozenJournalAppend.serialized_record`.
Ни retry poll, ни restart/replay не получают новые clocks/boot ID и не пересериализуют payload.

Для start/end/applied и pending sample frozen bytes + SHA/stage/attempt сохраняются до append в
mode-0600 atomic retry sidecar `discharge-events-v1.pending-record`; directory sync предшествует
journal write. Sidecar хранит ровно один record (journal writer и так последовательный), поэтому
start/end retry переживает process restart/reboot. После подтверждённой durable записи sidecar
атомарно очищается и sync-ится; start envelope после reboot всё равно содержит original boot ID/time.
Если pending start прекращён из-за OL до durable start, sidecar атомарно удаляется+directory-sync до
обработки следующего события. На startup sidecar сначала сверяется с logical replay без предположения
об UPS status: exact durable record reconciles как success, invalid/conflicting sidecar изолируется;
решение для never-open start откладывается до первого валидного physical poll, а READY остаётся
withheld. При первом OL sidecar удаляется без append; при OB продолжается byte-identical retry.
Ни unknown status, ни stale sidecar не создают phantom open event.
Это правило удаления относится только к обычному append failure при уже готовом cursor. Пока
`journal_projection_ready=false`, start/sample/end принадлежат rebuild spool §6.5: OL durably закрывает
spool event, и он сохраняется до automatic promotion; обычный single-record sidecar не используется
как замена многоточечной recovery capture.

| Stage | Durable факт | Автоматическое поведение | Видимость |
|---|---|---|---|
| `start` append/sync fail | Event ещё не доказан durable | Сохранить один bounded in-memory pending start с заранее созданным event ID; retry того же start перед каждым следующим sample, пока raw status остаётся OB. Не принимать sample как durable до успеха. Если OL наступил раньше, прекратить retry исчезнувшего source event, не создавать synthetic journal event и удерживать bounded historical `capture_failed` health до следующего успешного durable start. | `last_pipeline_status=capture_failed`, `capture_failure_stage=start`, `raw_event_seen=true`, `durable_start=false`, `pending_replay=false`. |
| `sample` append/sync fail | Start durable, этот sample нет | Не продвигать cursor/last-durable time; кешировать ровно один последний sample и retry его до нового sample/end. После recovery продолжить; monotonic hole автоматически даст gap/recorded-only. | `capture_failed`, stage/sample, event ID, last durable seq; `pending_replay=false`. |
| `end` append/sync fail | Start/samples durable, event остаётся open | Сохранить exact end payload и retry каждый poll и startup до durable end. Evaluation не запускать. | `capture_failed`, stage/end, active event; `pending_replay=false`. |
| `applied` append/sync fail | Полный raw event durable | Retry только frozen applied envelope из sidecar. Replay может заново вычислить science projection как oracle; equality исключает envelope clocks/attempt и marker-time persisted hash. Frozen serialized bytes authoritative и используются без новой сериализации даже после sanctioned live-model change. | `capture_failed`, stage/applied; только здесь `pending_replay=true`. |

После ambiguous `write()`/`fdatasync()` failure writer **не пишет повторно вслепую**. Он закрывает и
заново открывает logical journal под `LOCK_EX`, затем сверяет frozen bytes:

1. exact byte-identical record с тем же event ID/seq уже присутствует — считать append успешным,
   обновить cursor и очистить sidecar;
2. на ожидаемом offset есть только torn final suffix — `ftruncate(valid_prefix_boundary)`,
   `fdatasync`, directory sync, затем retry тех же frozen serialized bytes;
3. event ID/seq отсутствует и prefix полностью валиден — append тех же frozen bytes;
4. тот же event ID/seq существует с другими bytes либо запись оказалась не в ожидаемой позиции —
   conflicting sequence: isolate segment через manifest §6.5, исключить crossing event и fail-closed;
   никогда не выбирать одну из двух версий и не учить;
5. retry sidecar checksum/shape invalid — isolate sidecar bytes, `capture_failed`, новый evidence не
   синтезировать.

После cases 1–3 и успешной проверки всего logical tail journal явно восстанавливает operational latch:
`_healthy=True`, `_last_error=None`, `capture_available=true`; это единственный recovery transition
после transient failure. Cases 4–5 используют isolation flow, после которого healthy становится true
только после создания нового active и первого успешного append. Так последующие start/sample не
блокируются навсегда прежним `_fail()`.

Retry cadence: первая повторная попытка на следующем poll, затем bounded exponential backoff
`min(60s, polling_interval * 2**min(attempt_count, 6))`, измеренный monotonic clock; успешная запись
сбрасывает failure. Никакого max-attempt terminal reject и никакого approval queue нет. Safety loop,
watchdog и virtual LB продолжаются на каждом poll независимо.

Обязательные fault fixtures для каждого record type: failure до write, partial write, write success +
`fdatasync` failure, restart до reconciliation, restart после exact durable record, torn-tail repair и
conflicting same-event/seq. Start и end отдельно проходят retry через несколько polls и новый boot с
byte-identical original envelope.

Middle corruption/unknown schema автоматически изолируются под lock по §6.5 и дают видимый
`capture_failed` с preserved hash/path, но не `pending_replay`: повреждённые bytes никогда не
оцениваются, а новый active capture продолжается автоматически.

Policy endpoint sticky принадлежит ровно текущему event ID. Он устанавливается, когда safety policy
впервые сформировала virtual LB/shutdown context во время этого OB, и сбрасывается после durable `end`
этого события либо при остановке процесса. Если затем приходит SIGTERM во время того же OB:

- sticky policy context -> lifecycle `closed_policy_shutdown`, evidence class
  `natural_policy_endpoint`, termination `policy_shutdown`; это проверка policy, не capacity truth;
- SIGTERM без sticky policy -> `closed_shutdown_requested`, termination `service_stop_signal`,
  evidence остаётся partial/gapped по фактам;
- raw physical `LB` сохраняется отдельно. В B он может влиять на evidence class/reasons, но candidate
  не создаётся до C;
- signal handler только ставит stop reason; journal close происходит в normal finally path. Safety
  output не ждёт assessment.

## 7. Exact file inventory

Реализация по этому плану создаёт/изменяет только перечисленные файлы. Если обнаружится дополнительный
production caller mutable state или изменённого API, сначала этот inventory и traceability должны быть
обновлены; молчаливое расширение scope запрещено.

### Создать

- `src/discharge_evidence.py` — pure assessment и reason-code constants.
- `src/model_evaluation.py` — pure delivered-Ah proxy, LUT inversion и residual evaluator.
- `tests/fixtures/release_b_raw_polls.json` — named raw-poll timelines.
- `tests/fixtures/release_b_expected.json` — независимые golden outcomes/scalars.
- `tests/fixtures/release_a_segment_reader.patch` — deterministic minimal backport, применяемый только
  к exact `release-a-20260815` в temporary worktree.
- `scripts/build_release_a_segment_rollback.sh` — fail-fast воспроизводимая сборка bundle/hash из tag
  и committed patch без сети и без production paths; он же предоставляет hermetic probe entrypoint
  для exact active interpreter/locked Release-B dependency environment.
- `tests/test_discharge_evidence.py`.
- `tests/test_model_evaluation.py`.
- `tests/test_release_b_replay.py`.
- `tests/test_release_b_guards.py`.
- `tests/test_release_b_e2e.py`.
- `tests/test_release_a_rollback_compat.py` — differential harness для exact tag
  `release-a-20260815` и обязательного segment-aware rollback artifact.
- `docs/RELEASE-B-DEPLOYMENT.md` — preflight, RC gate, canary, acceptance и rollback.

### Изменить: production

- `src/discharge_types.py` — frozen contracts.
- `src/model.py` — private state/physics, read-only projection, immutable evaluation snapshot.
- `src/discharge_collector.py` — `UpsObservation`, readiness, start snapshot, endpoint provenance;
  удалить imports/references на mutable model и handler.
- `src/sag_tracker.py` — принимать nominal voltage/power scalars; удалить `BatteryModel` import.
- `src/discharge_journal.py` — monotonic projection, enriched applied payload, segment rollover,
  projection index, recovery latch и size health; schema/version/record types неизменны.
- `src/discharge_recovery_spool.py` — bounded startup-rebuild capture и идемпотентное automatic
  promotion в schema-v1 journal; не импортирует evaluator или mutable model.
- `src/discharge_handler.py` — удалить operational routing и direct `.state` mutation; legacy
  scientific application не является Release-B composition path и не получает новый natural use
  case.
- `src/monitor.py` — composition root, hard gate, shared live/replay processor, policy-vs-signal
  context и bounded health projection.
- `src/monitor_config.py` — frozen health fields/serialization limits; без experimental knobs.
- `src/virtual_ups_exporter.py` — перенести bounded Release-B projection в health без влияния на LB.
- `src/scheduler_manager.py` — read-only model APIs; в capture-only composition нет command/save
  capability.
- `src/motd_status.py` — read-only model API и plain-language shadow fields.
- `scripts/battery-health.py` — plain-language event/evidence/residual/journal capacity report.
- `.github/workflows/ci.yml` — checkout/fetch обязан предоставлять exact release tag для обязательного
  rollback differential; отсутствие tag/patch является fail, не skip.

### Изменить: существующие tests

- `tests/conftest.py`
- `tests/test_battery_health_report.py`
- `tests/test_config.py`
- `tests/test_discharge_application.py`
- `tests/test_discharge_collector.py`
- `tests/test_discharge_event_logging.py`
- `tests/test_discharge_handler.py`
- `tests/test_discharge_journal.py`
- `tests/test_dispatch.py`
- `tests/test_health_endpoint_v16.py`
- `tests/test_model.py`
- `tests/test_monitor.py`
- `tests/test_monitor_integration.py`
- `tests/test_motd.py`
- `tests/test_motd_status.py`
- `tests/test_release_a_observability.py`
- `tests/test_sag_tracker.py`
- `tests/test_scheduler_manager.py`

### Изменить: truth docs

- `README.md`
- `docs/GLOSSARY.md`
- `docs/USER-SCENARIOS.md`
- `docs/internal/CONTEXT.md`
- `docs/CONTROLLED-CAPACITY-TEST-PROTOCOL.md`
- `docs/plans/natural-blackout-learning-implementation.md` — root-owned amendment конфликтующей
  future-automation формулировки; Release-B implementation commit включает согласованную версию, но
  исполнитель этого плана не создаёт approval gate.

### Явно не менять

`config.toml`, systemd units, NUT config, UPS commands, model schema/fields, runtime calculator,
SoC/safety formulas и production state/journal content.

## 8. Implementation clusters и targeted checks

Каждый cluster должен быть code-complete и завершаться указанными targeted checks. Полный suite не
запускается между мелкими правками.

### Cluster 1 — contracts, model boundary, clock provenance (`RB-08/09/12/14/19`)

1. Добавить frozen types и canonical JSON serializers/validators.
2. Приватизировать `_state` и `_physics`, перевести production readers на read-only APIs, добавить
   `snapshot_for_evaluation()`.
3. Удалить `BatteryModel` из collector/sag imports и constructor signatures.
4. Создать readiness tracker; snapshot брать на первом OB до reset.
5. Перевести duration/coverage/gaps на единую accepted-envelope-edge формулу и реализовать bounded
   readiness catalogs/thresholds.
6. Добавить per-event sticky policy endpoint и signal termination kind без изменения safety policy;
   durable end обязан сбрасывать sticky до следующего event.

Targeted checks:

```bash
pytest -q tests/test_model.py tests/test_discharge_collector.py tests/test_sag_tracker.py
pytest -q tests/test_discharge_journal.py tests/test_monitor_integration.py -k 'duration or gap or shutdown or policy'
```

Oracles: wall jump не меняет duration; no-gap duration равна start→end, а gapped duration той же
формулой суммирует только accepted start/sample/end edges; reboot interval не интегрируется;
`readiness_reachable_12h_with_1s_jitter` true, gap `25.000s` сохраняет серию, `25.001s` reset-ит её,
readiness после restart false с ordered bounded reason; mutation read-only projection не меняет hash;
collector/sag import graph не содержит
`src.model.BatteryModel`; составной fixture policy endpoint → durable OL end → новый OB → plain SIGTERM
не наследует sticky предыдущего события.

### Cluster 2 — journal result contract and unattended capacity (`RB-02/15/16/17/26`)

1. Расширить `mark_applied` bounded typed payload и строгую idempotency.
2. Замораживать full envelope/serialized bytes до append, durable retry sidecar и ambiguous-write
   reconciliation; start/end обязаны переживать polls/reboot без новых clocks.
3. Реализовать logical multi-segment replay и atomic rollover под sole-writer lock.
4. Реализовать immutable isolation manifest, complete/ended-unapplied prefix projection, все crash
   windows manifest/isolation и автоматическое продолжение capture.
5. Разделить `TerminalOutcome` и `RetryableProcessingFailure`; реализовать точные start/sample/end/
   applied retry semantics и `capture_failed` health.
6. Добавить atomic projection index, safety-first startup и bounded incremental rebuild; historical
   replay не предшествует первому physical poll/virtual publication и не достижим из safety path;
   до готовности cursor raw lifecycle durable пишется в rebuild spool и автоматически promotes.
7. Добавить bytes/headroom/segments/isolation/capture availability в `JournalHealth`; после
   reconciliation явно восстанавливать healthy latch.
8. Очистить abandoned/stale pending-start sidecar по physical OL/startup matrix без phantom event.
9. Сохранить legacy epoch/applied behavior без conversion.

Targeted checks:

```bash
pytest -q tests/test_discharge_journal.py tests/test_release_b_replay.py
pytest -q tests/test_monitor_integration.py -k 'replay or marker or journal or writer'
```

Oracles: rollover до/после rename survives restart; event через два segments проецируется один раз;
sealed/isolated bytes и SHA неизменны; old marker history-only; middle corruption автоматически
изолируется и следующий event captures без обучения повреждённого; каждый failure stage выдаёт
заданный union/health; transient fail восстанавливает journal healthy и следующий event; 0/10/100
sealed segments дают O(delta), не O(total), per-poll reads; abandoned start не создаёт open event;
startup с 100 segments публикует первый physical/virtual result до любого rebuild slice и в пределах
двух poll intervals, каждый slice соблюдает 1-MiB/10k-record/50-ms bounds; ENOSPC/permission не
останавливает safety; disk alarm меняет ok→warning→critical без удаления bytes; second writer fail-fast.
Отдельный oracle начинает OB после первого safety output, держит его до нескольких rebuild slices,
возвращает OL до готовности index, перезапускает процесс в каждом promotion crash window и требует
durable full lifecycle в spool, ровно один promoted main event и обычный terminal decision.
Вариант того же oracle оставляет UPS в OB до готовности index и требует atomic open-event handoff:
следующие samples/end появляются ровно один раз в main journal после `promoted_open`. Пока закрытый
spool event ждёт promotion, health показывает count=1/status=`historical_rebuild_pending`, но
`pending_replay=false`.

### Cluster 3 — automatic assessment, pure forward evaluation, hard gate (`RB-01/03/04/05/07/10/11/13/15`)

1. Реализовать pure assessment и evaluator.
2. Реализовать total `n=0/1/2` summaries, per-Decision overflow и ordered
   `model_evaluation_failed` terminal handling.
3. Создать единый `process_closed_event()` для live/replay; никакой отдельной replay science ветки.
4. На startup установить baseline fingerprint и hard `capture_only` до replay; поставить тот же gate до
   handler/model/scheduler effects.
5. Записывать terminal decision/residual summary в applied marker.
6. Удалить natural route и все scientific mutation capabilities из legacy handler; public save и
   legacy setters удалить/приватизировать, разрешённые mutations оставить только owner methods;
   не создавать candidate/proposal.

Targeted checks:

```bash
pytest -q tests/test_discharge_evidence.py tests/test_model_evaluation.py
pytest -q tests/test_release_b_guards.py tests/test_discharge_application.py tests/test_discharge_handler.py
pytest -q tests/test_monitor_integration.py -k 'operational or replay or capture_only'
```

Guard test подставляет bomb doubles во все public/private save, scientific setter/add/append,
owner mutation, RLS update/state mutation и `send_instcmd` и доказывает ноль вызовов для startup,
live/replay/retry каждого Release-B fixture. AST + bounded call-graph test запрещает вне
`src/model.py` direct/private state/physics access, любой save/setter/add/append/RLS mutation edge, передачу
mutation callable и imports mutable model в collector/sag/evaluator/closed-event processor.
Contract goldens дополнительно доказывают total mapping каждой lifecycle/provenance комбинации в
bounded `EvidenceClass`/`TerminationKind`, exhaustive rejection unknown enum/reason и знак residual
`observed-predicted` для положительного, нулевого и отрицательного случая. Applied-retry golden меняет
live model после первой ambiguous marker write: science projection совпадает, marker-time fields не
входят в oracle equality, а retry bytes остаются исходными.

### Cluster 4 — bounded reporting and documentation truth (`RB-18/24/25`)

1. Health отдаёт fixed-shape last-event summary; никаких raw arrays и event ID labels.
   Golden schema отдельно закрепляет
   `last_pipeline_status=historical_rebuild_pending`,
   `rebuild_spool_closed_awaiting_promotion_count` как bounded `0|1` и
   `pending_replay=false` для закрытого spool event; никакой другой stage не переиспользует этот
   status.
2. Ограничить event ID 128 bytes, reason lists 8 элементов, reason 64 bytes, error 512 bytes; весь
   health JSON acceptance limit `<=32 KiB`.
3. `battery-health` и MOTD показывают duration/load/coverage/max gap, residual, disposition, model
   unchanged reason, journal headroom и capture availability.
4. Исправить docs: partial events теперь shadow-check, не measured capacity/SoH; controlled capacity
   application не объявлять активным Release-B path; operator approval для event отсутствует.

Targeted checks:

```bash
pytest -q tests/test_health_endpoint_v16.py tests/test_release_a_observability.py
pytest -q tests/test_battery_health_report.py tests/test_motd.py tests/test_motd_status.py
pytest -q tests/test_release_b_replay.py -k reporting
```

### Cluster 5 — golden replay and isolated real-path smoke (`RB-20/21`)

Cluster 5 также владеет `.github/workflows/ci.yml`: до targeted rollback test checkout обязан получить
full history/tags через `fetch-depth: 0`, а Python 3.13/3.14 jobs используют уже созданное из RC
`uv.lock` environment для offline hermetic harness §9. Изменение workflow входит в тот же code-complete
cluster и проверяется до замораживания RC, не откладывается на deploy.

Fixture rows содержат deterministic wall time, monotonic_ns, boot ID и raw NUT fields; expected file
хранится отдельно от inputs. Обязательные named cases:

1. `short_natural`;
2. `partial_10m_quality`;
3. `partial_10m_wall_clock_forward_jump`;
4. `partial_10m_wall_clock_backward_jump`;
5. `reboot_gap_same_event`;
6. `missing_poll_gap_over_25s`;
7. `cal_self_test`;
8. `noisy_voltage`;
9. `unstable_load`;
10. `long_power_restored_not_runtime`;
11. `raw_physical_lb_long_event_no_candidate_b`;
12. `virtual_policy_endpoint_then_sigterm`;
13. `plain_sigterm_during_ob`;
14. `nut_timeout_and_partial_reply`;
15. `journal_start_sample_end_applied_failures`;
16. `rollover_mid_event_and_crash_windows`;
17. `live_model_changes_after_start`;
18. `legacy_missing_epoch_and_legacy_applied_marker`;
19. `second_writer`;
20. `private_path_isolation`;
21. `assessment_threshold_boundaries` — все just-below/equal/just-above значения таблицы §6.6;
22. `reason_precedence_and_truncation` — переставленные simultaneous failures дают одинаковые
    первые 8 codes и `additional_reason_count`;
23. `middle_corruption_auto_isolation` — exact bytes/hash сохранены, следующий event captured;
24. `start_sample_end_applied_retry_matrix` — disjoint union и health semantics для каждой стадии;
25. `zero_one_two_sample_summaries` — exact `None`/fallback fields и ordered reasons;
26. `per_decision_reason_overflow` — разные decisions имеют независимые overflow counts;
27. `write_success_fdatasync_failure_all_records` — reconcile exact/torn/conflict для каждого type;
28. `start_end_retry_across_poll_and_reboot` — original envelope bytes/hash/clocks неизменны;
29. `isolated_prefix_restart_projection` — complete terminal prefix events/counters сохранены,
    crossing event excluded, manifest/restart idempotent.
30. `isolated_prefix_ended_unapplied` — trusted durable end повторно проходит normal marker processing;
31. `isolation_manifest_crash_matrix` — rename-before-manifest, torn manifest tail,
    manifest-before-active и middle-corrupt manifest recovery;
32. `projection_index_constant_poll_cost` — 0/10/100 sealed segments читают один delta;
33. `transient_failure_health_recovery` — reconcile возвращает healthy и следующий event captured;
34. `abandoned_start_sidecar_ol_restart` — stale start очищен, phantom open отсутствует;
35. `startup_eligible_controlled_event_hard_gate` — replay не вызывает handler/save/RLS;
36. `policy_sticky_is_event_scoped` — предыдущее policy event не переклассифицирует новый SIGTERM;
37. `first_ob_ema_load_uninitialized` — finite raw load используется вместо sentinel; missing/zero
    load автоматически recorded-only.
38. `projection_index_write_failure_self_heals` — durable journal append при старом index; следующий
    poll читает только bounded delta, не задерживает virtual UPS/LB и атомарно чинит index.
39. `decision_reason_inheritance` — CAL/gapped/rejected/history-only решения получают применимые
    catalog codes в общем precedence-порядке без выдуманного terminal outcome.
40. `startup_incremental_rebuild_100_segments` — первый physical poll и virtual safety output
    предшествуют historical reads; каждый slice и first-poll latency соблюдают §6.5 bounds.
41. `reason_namespace_exhaustive` — все emitted Decision/readiness/pipeline reasons принадлежат одному
    из трёх ordered catalogs; unknown serialization fails.
42. `evidence_class_termination_mapping` — каждая строка mapping §6.1 и unknown lifecycle oracle.
43. `residual_sign_observed_minus_predicted` — exact positive/zero/negative voltage/slope и RMSE.
44. `applied_retry_after_live_model_change` — frozen terminal envelope authoritative, marker-time
    fields исключены из science equality, retry bytes/hash неизменны.
45. `unified_duration_edges_gapped_and_complete` — start/first и last/end tails включены, gap/reboot
    edges исключены, один и тот же алгоритм используется gate/report/evaluator.
46. `charge_readiness_reachable_boundaries` — 12h и gap `25.000/25.001s`, restart/CAL/boot reasons,
    max-8 serialization.
47. `disk_growth_alarm_no_deletion` — exact free-space thresholds, rate-limit log и byte-identical
    segments до/после warning/critical.
48. `release_a_rollback_hermetic_matrix` — original/patched Release A запускаются offline тем же
    matrix interpreter на Python 3.13/3.14, network/package-manager/user-site access fail-fast.
49. `startup_blackout_during_rebuild_spooled_and_promoted` — OB/start/samples/OL durable переживают
    restart до готовности historical index; promotion идемпотентно создаёт ровно один main event и
    terminal marker, original event ID/times/payload hashes сохранены.
50. `startup_open_blackout_handoff_after_rebuild` — незакрытый spool event атомарно продолжает тот же
    event ID в main journal; promotion crash windows не дублируют start/samples и последующий OL
    создаёт ровно один end/applied.
51. `coverage_start_point_exactness` — start считается первой OB точкой, end не считается, а
    transition-time last OB сначала durable записывается как sample; `n`, coverage и evaluator
    используют один и тот же набор.

Golden для `live_model_changes_after_start` сначала пишет start, затем меняет отдельный temporary live
model и требует identical assessment/decision/residual science projection. Отдельный
`applied_retry_after_live_model_change` замораживает bytes первой marker-попытки до model change и
требует byte-for-byte identical retry; marker-time hash не переопределяется. Golden для 10-minute
partial проверяет каждый simulator step: initial SoC, predicted SoC, inverted LUT voltage, reverse IR,
proxy integral, voltage/slope residuals.

Real-path E2E создаёт один temporary root и явно передаёт:

- temporary current-schema `model.json`;
- настоящие active/segment journal paths;
- private `.dev`, health и MOTD paths;
- fake read-only NUT source;
- production classifier, collector, journal, replay, assessor, evaluator, exporter и serializer.

Запрещены `/run/ups-battery-monitor`, `/etc/nut`, default home path, physical NUT, `upscmd`, sudo,
systemctl и второй daemon. Smoke прогоняет raw OL→OB→10m samples→OL, повторный process startup/replay
и CLI/MOTD rendering, затем доказывает unchanged scientific hash/runtime/LB outputs.

Targeted check:

```bash
pytest -q tests/test_release_b_e2e.py tests/test_release_b_replay.py
```

## 9. Full RC release gate и 24-hour canary

Последовательность линейная; runtime rollover flag и post-gate enable step отсутствуют:

1. Committed `tests/fixtures/release_a_segment_reader.patch` и
   `scripts/build_release_a_segment_rollback.sh` детерминированно создают во временном worktree из
   exact tag `release-a-20260815` (`047bab6`) минимальный `release-a-20260815-segment-aware`
   commit/bundle. Он меняет только read-only segment discovery/replay/isolation compatibility, без
   Release-B assessment/evaluator и без сети. Script принимает только explicit tag/patch/output/tmp
   paths, проверяет tag commit и clean apply, не читает production paths и всегда удаляет свой
   временный worktree через trap после сохранения результата.
2. `tests/test_release_a_rollback_compat.py` реально запускает исходный tag на pre-rollover,
   post-rollover, cross-segment-open-event, isolation-manifest и rename/create crash fixtures и
   фиксирует actual behavior. Те же fixtures на segment-aware artifact обязаны восстановить logical
   projection/counters, сохранить каждый evidence byte, продолжить capture и дать те же Release-A
   runtime/LB outputs. Исходный tag не является допустимым post-rollover rollback.
3. Hermetic execution contract: CI сначала создаёт обычное Release-B test environment из committed
   `uv.lock`; после этого rollback phase работает offline. Harness запускает original и patched trees
   exact `sys.executable` каждой CI matrix job (Python 3.13 и 3.14) через `-I` bootstrap, который явно
   добавляет только temporary worktree и уже установленный locked Release-B site-packages, ставит
   `PYTHONNOUSERSITE=1`, очищает proxy/config/Python path variables и до import подменяет socket/network
   и package-manager subprocess entrypoints на fail-fast bombs. В worktree запрещены `uv sync`, `pip`,
   download и cache-dependent install; import/dependency/version failure является test failure.
4. `.github/workflows/ci.yml` принадлежит Cluster 5: checkout получает full tag history строго через
   `fetch-depth: 0` до `just check`, и обе matrix jobs исполняют hermetic rollback test. Это не
   release-time ручная подготовка. `tests/test_release_a_rollback_compat.py` всегда применяет committed
   patch в pytest tmp worktree и
   **fail**, а не skip, если tag, patch, apply или differential отсутствуют. Поэтому обычный
   `just check` воспроизводит rollback gate локально и в CI без заранее созданного `/var/backups`
   artifact.
5. Для RC сохранить построенный тем же script проверенный bundle mode 0600 как
   `/var/backups/ups-battery-monitor/release-a-20260815-segment-aware.bundle` и SHA-256 как соседний
   `.sha256`; проверить offline `git bundle verify` и checkout commit. Если exact path недоступен,
   release блокируется — альтернативное расположение не выбирается молча.
6. Только после pass шагов 1–5 зафиксировать exact Release-B RC commit. Rollover/isolation уже входят
   в candidate без flag; commit больше не изменяется.
7. На этом exact RC выполнить:

   ```bash
   just check
   git diff --check
   ```

   Затем выполнить Release-A differential harness, все raw/golden boundary/reason/retry/corruption
   fixtures, isolated real-path E2E, runtime/LB differential и static/dynamic zero-mutation/command
   guards. Сохранить hashes inputs/expected, before/after scientific fingerprint, E2E transcript,
   path inventory и actual renderer examples.
8. Package строится из того же RC commit; package SHA-256 и commit фиксируются в deployment
   inventory. Canary развёртывает именно этот package. После `just check`/differential/E2E gates нет
   code/config/flag/enablement mutations.

Canary начинается только по `docs/RELEASE-B-DEPLOYMENT.md` после read-only preflight:

- physical и virtual UPS доступны и OL;
- нет active event и `shutdown_imminent`;
- journal healthy, capture available, headroom достаточен, pending replay false;
- process writer lock свободен для штатного единственного daemon;
- зафиксированы model/journal/unit/NUT checksums, boot ID, scientific fingerprint и command audit;
- segment-aware Release-A rollback bundle/hash проверены, а его commit доступен локально без сети;
- автоматический UPS dispatch отсутствует; реальный blackout/test не инициируется.

В 24 часа наблюдаются обычный daemon и scheduler hour плюс один заранее предусмотренный monitor-only
restart только если Release-A runbook считает его безопасным. Pass:

- нет UPS commands, FSD, неожиданного LB, reboot, NUT restart или второго writer;
- scientific fingerprint неизменен;
- runtime/LB policy и physical→virtual safety behavior неизменны;
- journal/capture healthy, rollover (если случился) целостен, size/headroom видимы;
- любое естественно случившееся событие само получило terminal decision; отсутствие blackout не
  считается proof real-NUT evaluation и не требует его искусственно вызывать;
- health/CLI/MOTD остаются bounded и понятны.

Любое изменение scientific fingerprint, более поздний LB, вызов UPS command, journal degradation,
stale safety output, restart loop или второй writer — немедленный rollback trigger.

## 10. Rollback и cleanup

Rollback:

1. Сохранить failing code version, health, logs, model/journal/segments и checksums; ничего не
   обрезать и не удалять.
2. Подтвердить physical UPS OL.
3. Проверить SHA-256 и commit обязательного `release-a-20260815-segment-aware` artifact, уже прошедшего
   RC fixtures; обычный tag `release-a-20260815` после первого rollover использовать запрещено.
4. Развернуть именно segment-aware Release-A artifact и перезапустить только monitor service по
   Release-A runbook; NUT не перезапускать без отдельного доказательства. Он читает все sealed/active
   segments как один логический journal и сохраняет open cross-segment event.
5. **Запрещено** прятать/переименовывать segments и запускать исходный Release A на пустом active
   journal: это теряет projection/counters/open-event continuity, даже если bytes лежат рядом.
6. Проверить свежие physical/virtual status, journal projection/health и исходный scientific
   fingerprint. Model rollback
   не нужен: Release B не имеет model write path.

Temporary cleanup является частью каждого test/smoke завершения:

- записать exact temporary root, files, permissions и sizes;
- проверить, что path находится внутри pytest/tmp root и не symlink;
- закрыть descriptors/processes;
- удалить только созданный этим run root и проверить отсутствие leftovers;
- не удалять shared caches, production model/journal, unrelated branches/worktrees/containers;
- если failure требует сохранить fixture root, runbook сообщает exact path и size, иначе retained
  temporary resources = none.

## 11. Documentation truth changes

- `README.md`: заменить обещание «model learns from accepted observations» на точный split:
  natural partial даёт durable shadow residuals и terminal reason, scientific model unchanged;
  authoritative capacity/SoH deferred до independent evidence.
- `docs/GLOSSARY.md`: определить `EvidenceAssessment`, `BatteryModelSnapshot`, `ModelResiduals`,
  `recorded_only`, `rejected`, `pending_replay` и `natural_policy_endpoint`; запретить «actual runtime»
  для power-restored partial.
- `docs/USER-SCENARIOS.md`: показать unattended automatic result, journal capacity/capture unavailable,
  policy-vs-SIGTERM и отсутствие event approval workflow.
- `docs/internal/CONTEXT.md`: обновить architecture data flow и sole-writer invariant; отделить
  shadow evaluation от model application и safety.
- `docs/CONTROLLED-CAPACITY-TEST-PROTOCOL.md`: явно пометить протокол как отдельно санкционируемую
  операторскую процедуру, не production producer и не approval step штатного Release-B learning.
- `docs/plans/natural-blackout-learning-implementation.md`: root-owned truth fix удаляет
  `capacity_ah_measured=None` как manual/future approval gate и описывает future upward/downward
  updates только через отдельно доказанную automatic safety policy, не queue.
- `docs/RELEASE-B-DEPLOYMENT.md`: только verified preflight/RC/canary/rollback команды; green config or
  tests не выдавать за live deployment или real blackout proof.

## 12. Traceability и acceptance oracles

| IDs | Реализация | Доказательство | Наблюдаемый результат |
|---|---|---|---|
| `RB-01/02/15` | Clusters 2–3, disjoint union, frozen record retry sidecar/reconciliation | all-record ambiguous-write и poll/reboot fixtures | Каждый record retry byte-identical; terminal/capture_failed различимы; без approval. |
| `RB-03/04/05/11` | Pure evaluator и frozen snapshot | stepwise golden, live-model-change replay | Только observed segment/proxy/residual; никакого full-runtime/capacity/SoH claim или correction factor. |
| `RB-06/13` | Parallel safety path, gate armed before replay, safety-first poll/publication и bounded incremental index rebuild | bomb doubles, runtime/LB differential, 0/10/100-segment poll-cost + 100-segment cold-start fixtures | Zero scientific/UPS effect; historical rebuild следует после первого safety output и bounded между polls; LB timing unchanged. |
| `RB-07` | Denied per-field decisions | AST/type and golden projection | Нет candidate/cohort/proposal type/persistence; explicit deferred reasons. |
| `RB-08/09/10` | Frozen dataclasses/start payload/readiness, total summaries, exhaustive typed reason catalogs, total evidence/termination mapping | snapshot/readiness reachability, n=0/1/2, enum/reason exhaustion, below/equal/above, overflow/reason-order fixtures | Summaries/assessment total, immutable и детерминированы; readiness достижима и bounded; evaluator failure terminal. |
| `RB-12` | Единая accepted-envelope-edge monotonic projection | wall jumps/reboot/gap/tail fixtures | Gate/report/evaluator используют одну duration; start/first и last/end tails учтены, wall/reboot/gap time не интегрирован. |
| `RB-14` | Private `_state`/`_physics`/save/setters, scalar/snapshot injection, zero-capability composition | import/AST/call-graph/bomb guards | Capture/evaluation graph не имеет ни одного scientific mutation edge. |
| `RB-16/17/26` | Schema-v1 segments, projection index, rebuild spool, immutable isolation manifest/prefix projection и no-deletion disk-growth alarm | restart/counters/idempotency/corruption/promotion-crash/health-recovery/alarm + hermetic pre-RC Release-A differential on 3.13/3.14 | Ended prefix facts retained/reprocessed, recovery-time blackout promoted once, crossing/suffix excluded, transient latch recovers, evidence preserved, capture continues; disk pressure visible without deletion. |
| `RB-18/25` | Fixed health projection, CLI/MOTD | size and renderer goldens | Пользователь видит quality/residual/decision/reason без чтения JSONL. |
| `RB-19` | Event-scoped sticky policy context + stop reason | policy endpoint/plain SIGTERM/composite-next-event fixtures | Policy endpoint не назван physical capacity; sticky не протекает в следующий event. |
| `RB-20/21` | Raw fixtures и real-path E2E | Cluster 5 | Deterministic replay, path isolation, all real serializers and files exercised. |
| `RB-22/23` | Linear artifact-before-RC sequence, unchanged candidate/package/canary | bundle verify + exact commit/package hashes + release checklist | No circular flag enable; 24h unchanged science/safety/commands; rollback preserves evidence. |
| `RB-24/27` | Truth docs | doc assertions/search | Нет manual approval queue; operator нужен только для внешнего физического факта. |

## 13. Обязательный пользовательский acceptance example

На fixture `partial_10m_quality` `battery-health` и эквивалентные MOTD fields должны выражать ровно
следующий смысл простым русским языком (числа являются golden expectation fixture, не production
порогами):

```text
Естественное отключение: 10 мин, средняя нагрузка 21%, покрытие 98%, максимальный разрыв 20 с.
Проверка модели: к концу наблюдавшегося участка напряжение на 0,18 В ниже прогноза;
скорость падения отличается на 0,04 В/ч.
Решение: recorded_only.
Модель не изменена: сеть вернулась до независимого физического конца, поэтому событие проверяет
только наблюдавшийся участок и не доказывает полный runtime, ёмкость или SoH.
Безопасное выключение: правила и порог не изменены.
```

Acceptance fail, если отчёт говорит «actual runtime», «measured capacity», предлагает ручное
одобрение события, скрывает gap/journal-unavailable состояние или не объясняет, почему модель
осталась неизменной.

## 14. Closure premium review cycle 1

| Finding | Обязательное закрытие в этом плане |
|---|---|
| `H1` | §6.5/6.7/Cluster 2: first physical poll и virtual safety publication предшествуют rebuild; rebuild имеет 1-MiB/10k-record/50-ms slice bounds, а blackout во время rebuild durable сохраняется в spool и автоматически promotes; 100-segment cold-start и recovery-blackout oracles обязательны. |
| `M1` | §6.1/6.6: exhaustive disjoint ordered Decision/readiness/pipeline reason namespaces; `model_evaluation_unavailable` удалён, capture/gap являются typed pipeline reasons. |
| `M2` | §6.1: total ordered `EvidenceClass` derivation и bounded exhaustive `TerminationKind` mapping; unknown lifecycle rejected. |
| `M3` | §6.6: все signed residuals определены как observed minus predicted; RMSE unsigned; sign goldens обязательны. |
| `M4` | §6.4/6.7: frozen applied bytes authoritative; marker-time/envelope fields исключены из science replay equality; live-model-change retry fixture. |
| `M5` | §6.4: одна accepted-envelope-edge duration для complete/gapped, gate/report/evaluator; tails/gap/reboot oracle. |
| `M6` | §6.3: reset gap `>25.000s`, достижимые 12h readiness условия и exact bounded reason catalog/boundaries. |
| `M7` | §7–9/Cluster 5: CI owns full tag checkout; original/patched Release A run offline under exact 3.13/3.14 matrix interpreter and locked RC dependencies with network/install bombs. |
| `L1` | `RB-01/RB-15/RB-26`: terminal-marker promise явно исключает immutable history-only и отдельно показывает retryable capture failure. |
| `L2` | §6.1/6.3: readiness reason tuple ограничен 8×64 bytes, имеет exact order и per-snapshot overflow count. |
| `L3` | §6.5/Clusters 2/4: exact warning/critical disk-growth alarms и renderer visibility; retention/deletion evidence запрещены. |
