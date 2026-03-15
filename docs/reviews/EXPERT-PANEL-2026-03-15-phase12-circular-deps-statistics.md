# Expert Panel: Phase 12 — Circular Dependencies & Statistical Rigor

**Date:** 2026-03-15
**Trigger:** Phase 12 "Deep Discharge Capacity Estimation" plan review
**Focus:** (1) Circular dependency SoH↔Capacity↔Peukert, (2) Statistical correctness of confidence metric
**Panel:** 3 experts — Electrochemist, Statistician, System Architect

---

## Scope

**What:** Validate Phase 12 capacity estimation design for circular dependencies (SoH↔Capacity) and statistical correctness (confidence metric).

**Blast radius:** `contained` — affects battery model accuracy, no system-level risk.

**Decision type:** Stress-testing an existing plan.

---

## Expert Analysis

### 🔬 Dr. Voronova — Electrochemist / Battery Domain

**Assessment:** Циклической зависимости SoH↔Capacity **нет в текущем коде**, но план 12 **создаёт предпосылки** для неё. Сейчас: `capacity_ah` приходит из `config.toml` (7.2) → записывается в `model.data['full_capacity_ah_ref']` → передаётся в `calculate_soh_from_discharge()` и `runtime_minutes()` как константа. SoH вычисляется из area-under-curve и никуда не возвращается обратно в capacity. **Но!** План 02 (строка 257) говорит: _"CAP-04: Daemon replaces rated capacity_ah with measured value when confidence exceeds threshold"_. Если измеренная capacity подменяет `full_capacity_ah_ref`, а `full_capacity_ah_ref` используется в `calculate_soh_from_discharge()` (строка 439 monitor.py) — вот ваш цикл.

**Конкретный путь цикла:**
```
measured_capacity → full_capacity_ah_ref → calculate_soh_from_discharge(capacity_ah=...)
    → soh_new → runtime_minutes(soh=soh_new, capacity_ah=full_capacity_ah_ref)
    → predicted_runtime → _auto_calibrate_peukert() → peukert_exponent
    → следующий вызов CapacityEstimator._estimate_from_voltage_curve()
    → ah_voltage (cross-check) → влияет на accept/reject estimate
```

**Рекомендация:** **Capacity estimation и SoH MUST использовать разные поля.** SoH продолжает использовать `full_capacity_ah_ref` (rated/config). CapacityEstimator хранит результаты в `capacity_estimates[]` — отдельный массив. Замена rated→measured — это **Phase 13 решение**, не Phase 12. Plan 02 должен убрать формулировку "replaces rated capacity_ah". Phase 12 только **измеряет и хранит**, не подменяет.

**Open question:** Когда (и если) measured capacity заменит rated — как пересчитать SoH history? Все старые SoH записи были вычислены с rated=7.2. Если capacity станет 6.8, все SoH задним числом неверны.

---

### 📊 Проф. Кац — Математическая статистика

**Assessment:** Формула `confidence = 1 - CoV` имеет **четыре проблемы**:

**(a) CoV с 2 выборками бессмысленна.** `statistics.stdev()` с n=2 даёт Bessel-corrected std (делит на n-1=1), что при двух измерениях [7.2, 7.5] даёт std=0.212, CoV=0.029, confidence=97%. Это **ложная уверенность** — две точки не могут дать 97% confidence. Population variance (деление на n) даёт std=0.15, CoV=0.020, confidence=98% — ещё хуже.

**Проблема в коде:** Research (строка 278-283) использует **population variance** (`sum/len`), но упоминает `statistics.stdev()` который делает **sample variance**. Они дадут разные результаты. При n=2 разница 41% (√2 фактор).

**(b) Монотонность НЕ гарантирована.** Пусть первые 2 измерения: [7.2, 7.3] → CoV=0.0096, confidence=99%. Третье измерение: 6.8 (шумное) → [7.2, 7.3, 6.8] → CoV=0.036, confidence=96.4%. Confidence **упала** с 99% до 96%. Планы утверждают "monotonically increases" — это **математически ложно** для CoV-based метрики.

**(c) Monte Carlo тест с ±5% шумом.** При I=35A ±5% (std=1.75A), за 3 измерения средний CoV будет ~0.03-0.05. CoV < 0.10 в 95% случаев — реалистично, но только если все 3 discharge одинаковой глубины и длительности. В реальности разные blackout'ы имеют разный load profile, что увеличивает разброс. Тест пройдёт, но не валидирует реальный сценарий.

**(d) `confidence = 1 - CoV` не имеет статистического смысла.** Это не доверительный интервал, не posterior probability, не p-value. Это произвольная трансформация. Само по себе не проблема (это UI metric), но документация не должна называть её "confidence" в статистическом смысле.

**Рекомендация (минимальные правки):**
1. **n < 3 → confidence = 0.** Не вычислять CoV с 1-2 выборками. Просто `if len < 3: return 0.0`
2. **Убрать "monotonically increases" из планов.** Заменить на: "generally increases with sample count; may fluctuate due to measurement noise"
3. **Решить population vs sample variance.** Для CoV с малыми n: **population variance** (деление на n) — менее волатильная, предпочтительнее для UI-метрики
4. **Переименовать.** `confidence` → `convergence_score` или `stability_metric`. Не путать с confidence interval.

---

### 🏗️ Архитектор — System Design

**Assessment:** Планы концептуально правильны, но два structural issue:

1. **CapacityEstimator хранит state в памяти (`self.measurements`)**, но между рестартами демона теряется. Plan 02 добавляет `model.add_capacity_estimate()` для persistence, но **CapacityEstimator.__init__() не загружает историю из model.json**. После рестарта `has_converged()` вернёт False (measurements пуст), confidence=0. Решение: в Plan 02 при инициализации MonitorDaemon, загрузить `model.get_capacity_estimates()` в `estimator.measurements`.

2. **`_auto_calibrate_peukert()` уже существует** в monitor.py (строка 507). VAL-02 говорит "Peukert fixed at 1.2, no auto-refinement", но код **уже делает auto-calibration!** Phase 12 не решает этот конфликт. Если Peukert меняется на лету, а CapacityEstimator использует hardcoded 1.2, то voltage cross-check будет некорректным (разный Peukert для coulomb и voltage).

**Рекомендация:** Plan 01 должен читать `model.get_peukert_exponent()` вместо hardcoded 1.2. Или Phase 12 явно отключает `_auto_calibrate_peukert()` (что ломает Phase 1 контракт).

---

## Panel Conflicts

| Topic | Позиция | Разрешение |
|-------|---------|------------|
| Peukert hardcoded 1.2 vs auto-calibration | Электрохимик: фиксировать. Архитектор: использовать из model. | **Читать из model.** Код уже калибрует Peukert; игнорировать это = внутреннее противоречие. CapacityEstimator должен использовать `model.get_peukert_exponent()`, не 1.2. |
| Confidence formula | Статистик: переименовать. Электрохимик: пользователь не различает. | **Оставить "confidence" для UX**, но в коде назвать поле `convergence_score`. Документировать что это не confidence interval. |
| Capacity replacing rated | Электрохимик: Phase 13. Архитектор: уточнить в планах. | **Phase 12 только хранит.** Убрать "replaces rated" из Plan 02. `full_capacity_ah_ref` остаётся неизменным. |

## Итоговые правки в планы

1. **Plan 01, Task 1:** `_compute_confidence()` → return 0.0 if `len(measurements) < 3` (не < 2). Убрать claim "monotonically increases". Использовать population std (деление на n, не n-1)
2. **Plan 01:** CapacityEstimator.__init__ принимает `peukert_exponent` из model, не hardcoded 1.2
3. **Plan 02, Task 2:** Загружать historical measurements из model.json при init. Убрать "replaces rated capacity_ah"
4. **Plan 02:** CapacityEstimator инициализируется с `model.get_peukert_exponent()`, не 1.2
5. **Plan 03, must_haves:** Убрать "monotonically increases" → "generally increases"
6. **Все планы:** `full_capacity_ah_ref` НЕ изменяется в Phase 12. Measured capacity живёт только в `capacity_estimates[]`

---

**Status:** All recommendations applied to Phase 12 plans (12-01-PLAN.md, 12-02-PLAN.md, 12-03-PLAN.md) and STATE.md on 2026-03-15.
