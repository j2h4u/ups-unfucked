# Cross-AI Result: glm-5-3

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:52395 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/claude-opus-5.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Standard re-review of the complete uncommitted Release A implementation after premium H-1 partial-NUT false-OL fix, exact deployment-runbook redesign, full release gates, and removal of production-dead legacy APIs. Inspect current repository source and tests, not only attached reports. Return GO only if no actionable Critical, High, or Medium correctness, UPS shutdown-safety, data-integrity, architecture, or deployment findings remain. Verify the one-time 13-to-17-key conversion and rollback checkout procedure are executable and do not run strict code on old state; verify capture-only natural blackout journaling, exactly-once dispositions, watchdog/READY, partial reply preservation, 1-second poll/10-second durable samples, epoch isolation, and scheduler command blocking. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:52395 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/claude-opus-5.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-post-gates/20260814T221111Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Standard re-review of the complete uncommitted Release A implementation after premium H-1 partial-NUT false-OL fix, exact deployment-runbook redesign, full release gates, and removal of production-dead legacy APIs. Inspect current repository source and tests, not only attached reports. Return GO only if no actionable Critical, High, or Medium correctness, UPS shutdown-safety, data-integrity, architecture, or deployment findings remain. Verify the one-time 13-to-17-key conversion and rollback checkout procedure are executable and do not run strict code on old state; verify capture-only natural blackout journaling, exactly-once dispositions, watchdog/READY, partial reply preservation, 1-second poll/10-second durable samples, epoch isolation, and scheduler command blocking. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Release A re-review — вердикт: **GO**

Статическая проверка (bash ограничен `git status/diff/log/show`; полный test/lint/type gate остаётся операторским pre-flight). Проверял исходники и тесты напрямую, а не только отчёты.

## Премиум H-1 (false OL при частичном ответе NUT) — ЗАКРЫТ, двумя слоями

1. **Ранний гейт в daemon**: `_physical_reply_is_valid` (src/monitor.py:874-906) требует непустой `ups.status` + конечные voltage ∈ [8,15] и load ∈ [0,100]; неполный ответ → `STATUS=degraded` и return до любой записи выходов (monitor.py:1110-1120). EMA, классификатор, collector и scheduler не вызываются.
2. **Гейт в exporter**: `_has_usable_status` (src/virtual_ups_exporter.py:44-62) отказывается писать без реального статуса; фабрикатный fallback `.get("ups.status", "OL")` удалён из кода полностью. `_build_virtual_metrics` дублирует отказ (exporter:206-207).
3. **Регрессия настоящая**, не мок: `test_partial_status_reply_preserves_virtual_ups_file_and_heartbeats` (tests/test_monitor_integration.py:1033-1067) кладёт `OB DISCHRG LB` в реальный `.dev`, не мокает `write_virtual_ups`, и ассертит байт-идентичность файла после тика + один heartbeat + отсутствие READY.
4. Дополнительно проверил тонкий сценарий «stale override OL × raw OB без input.voltage»: passthrough-гарда `_should_passthrough_ob_status` (exporter:258-268) публикует сырой OB консервативно — оба направления уклона безопасны.

## Премиум M-1 (runbook) — ЗАКРЫТ

`docs/RELEASE-A-DEPLOYMENT.md` теперь исполним: проработанное jq-преобразование 13→17 ключей без `//`-подмен; preflight (свежий health, `journal_healthy`, `active_event_id==null`, физический OL через `upsc`); всё готовится до stop, окно < 30 c; rollback-функции ставят старый unit и делают `daemon-reload` **до** возврата 13-полевого файла и валидируют `len(d)==13` заранее — строгий loader никогда не видит старое состояние. Контракт закреплён тестом `test_release_a_deployment_doc.py:47-101` (порядок шагов, отсутствие запрещённых инструментов), а `:104-125` **реально исполняет** документированное преобразование на приватной копии retained-бэкапа и доказывает принятие результата строгим `BatteryModel`.

## Остальные пункты задания — перепроверены

| Пункт | Доказательство |
|---|---|
| Capture-only journaling | collector пишет только `model_processing_eligible: False` (5 мест); `_validate_application_input` требует `controlled_capacity_test` (discharge_handler.py:326-328), которого никто не производит |
| Exactly-once dispositions | `mark_applied` идемпотентен при том же hash+disposition, конфликт — ошибка, open-event отклонён (discharge_journal.py:330-353); исторические маркеры без disposition не переписываются |
| Watchdog/READY | heartbeat безусловно в `finally` (monitor.py:1084-1089); таблица READY/STATUS (:1203-1215) + регрессия на всю последовательность (test_monitor_integration.py:961-1031) |
| 1s poll / 10s durable / 60s report | monitor_config.py:46,47,54 |
| Epoch isolation | чужая/отсутствующая эпоха → терминальный `closed_epoch_mismatch`/`history_only` (discharge_collector.py:416-455); replay-фильтр (monitor.py:421-427); тесты проекции/счётчиков |
| Scheduler blocking | `SchedulerModeError` на единственной границе (scheduler_manager.py:111-118); proposal-режим только логирует `test_proposal_only` без model-save (:541-552) |
| Strict schema / BaselineReset / lock / unit | model.py:382-404+; monitor.py:816-861, 290-323; `TimeoutStartSec=0`, `WatchdogSec=120` |
| Legacy API removal | `calibration_write` и `handle_discharge_complete` удалены из src |

## Премиум L-1/L-2 — исправлены

`tmp_path_retention_*` перенесены в `pytest.ini` (который выигрывает precedence); `pytest-timeout` теперь в `[dependency-groups] dev` (pyproject.toml).

## Остаточные Low (не блокирующие)

1. Инлайн-валидация loader в runbook (`RELEASE-A-DEPLOYMENT.md:122`, `python3 -c 'from src.model import ...'`) требует cwd = корень репо; блок не делает `cd "$repo"`. Отказ безопасен (abort до любых изменений), но может ложно прервать валидный деплой.
2. Мёртвые делегаты `_auto_calibrate_peukert`/`_log_discharge_prediction` (monitor.py:804-811) — отложены на коммит 18 плана.
3. `render_motd` по-прежнему пропускает `ModelLoadError` (motd_status.py:83) — прежний L-4 без изменений.
4. Полный replay журнала раз в тик — известная ограниченная стоимость, принята.

**Critical/High/Medium отсутствуют — GO.** Перед 24-часовым окном приёмки обязательны: полный test/lint/type gate (не запускался в этой сессии) и однократная deploy-конвертация по runbook.
