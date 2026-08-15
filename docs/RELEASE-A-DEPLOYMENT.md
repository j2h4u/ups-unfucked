# Release A: одноразовая установка и конвертация состояния

Этот документ описывает одну операцию перехода на Release A. Резервная копия
уже создана внешним оператором и должна оставаться доступной до окончательной
приёмки. Конвертация выполняется один раз вне постоянного кода приложения.

На этом хосте рабочий `systemd` unit указывает прямо на checkout Release A, а
запущенный до операции процесс ещё использует старый код и 13-полевой
`model.json`. Поэтому после остановки нельзя просто запустить существующий
unit: строгий текущий загрузчик отвергнет старое состояние. До короткого окна
оператор заранее готовит отдельный detached checkout старого кода и отдельный
rollback unit. Оба сохраняются до полной приёмки.

Полный `scripts/install.sh` в критическом окне не запускается: он останавливает
монитор и перезапускает NUT. Конфигурация NUT и drop-in уже установлены и в этой
операции не меняются. Здесь меняются только состояние и unit монитора.
В штатном unit сохраняется `TimeoutStartSec=0`: startup timeout systemd отключён;
это не разрешает запускать строгий код на старом состоянии.

## Подготовка и проверки при работающем старом процессе

Выполняйте блоки последовательно в одной fish-сессии. Все пути ниже явные;
заменяйте их только после проверки. Подготовка выполняется до остановки, поэтому
её длительность не входит в целевое окно остановка--запуск менее 30 секунд.
Целевое окно stop-to-start — under 30 seconds. Отдельный writer журнала в этих шагах не создаётся: свежий health-файл уже
является источником проверки состояния журнала, и второй writer нам не нужен.
Операция разрешена только из чистого checkout с финальным Release A commit:
перед запуском root должен создать tag `release-a-20260815`. Блок ниже требует
HEAD ровно этого commit, отсутствия tracked diff и отсутствия untracked файлов
в `src`, `systemd`, `tests`, `scripts`; review outputs можно хранить вне этих
runtime-путей. При любом отличии операция прерывается.

```fish
set repo /home/j2h4u/repos/j2h4u/ups-battery-monitor
set state_dir ~/.config/ups-battery-monitor
set model "$state_dir/model.json"
set health_path /run/ups-battery-monitor/ups-health.json
set backup_model /home/j2h4u/.config/ups-battery-monitor.backup-20260815-3rb0H4/model.json
set unit_path /etc/systemd/system/ups-battery-monitor.service
set rollback_checkout /home/j2h4u/.cache/ups-battery-monitor-release-a-previous

# Root creates this annotated/lightweight tag only after the feature release is
# committed. Replace nothing here: the tag is the required release assertion.
set release_a_commit (git -C "$repo" rev-parse 'refs/tags/release-a-20260815^{commit}'); or begin
    echo "CRITICAL: required release-a-20260815 tag is missing"; exit 1
end
test (git -C "$repo" rev-parse HEAD) = "$release_a_commit"; or begin
    echo "CRITICAL: checkout HEAD is not the asserted Release A commit"; exit 1
end
git -C "$repo" diff --quiet "$release_a_commit" --; or begin
    echo "CRITICAL: tracked checkout content differs from the asserted release"; exit 1
end
set untracked_runtime (git -C "$repo" status --porcelain=v1 --untracked-files=all -- src systemd tests scripts)
test (count $untracked_runtime) -eq 0; or begin
    echo "CRITICAL: runtime source/unit/tests/scripts contain untracked files"; exit 1
end

function release_a_preflight
    test -s "$health_path"; or return 1
    set now (date +%s)
    set last_poll (jq -er '.last_poll_unix' "$health_path"); or return 1
    string match -rq '^[0-9]+$' -- "$last_poll"; or return 1
    set age (math "$now - $last_poll")
    test "$age" -ge 0; and test "$age" -lt 10; or return 1
    jq -e '.journal_healthy == true and .active_event_id == null' "$health_path" >/dev/null; or return 1
    set physical_status (upsc cyberpower@localhost ups.status 2>/dev/null); or return 1
    string match -rq '(^|[[:space:]])OL([[:space:]]|$)' -- "$physical_status"; or return 1
end

test -d "$repo"; or exit 1
test -d "$state_dir"; or exit 1
test -f "$model"; and not test -L "$model"; or exit 1
test -f "$backup_model"; and not test -L "$backup_model"; or exit 1
set state_real (realpath -e "$state_dir"); or exit 1
set model_real (realpath -e "$model"); or exit 1
set backup_real (realpath -e "$backup_model"); or exit 1
test (dirname -- "$model_real") = "$state_real"; or exit 1
release_a_preflight; or exit 1

set legacy_keys (string join ',' battery_install_date cumulative_on_battery_sec cycle_count discharge_events last_upscmd_status last_upscmd_timestamp last_upscmd_type lut new_battery_detected physics r_internal_history soh soh_history)
python3 -c 'import json, sys; expected=set(sys.argv[1].split(",")); data=json.load(open(sys.argv[2])); assert set(data) == expected' "$legacy_keys" "$model"; or exit 1
python3 -c 'import json, sys; expected=set(sys.argv[1].split(",")); data=json.load(open(sys.argv[2])); assert set(data) == expected' "$legacy_keys" "$backup_model"; or exit 1
set source_sha_before (sha256sum "$model" | cut -d ' ' -f 1)
set backup_sha (sha256sum "$backup_model" | cut -d ' ' -f 1)
string match -rq '^[0-9a-f]{64}$' -- "$source_sha_before"; or exit 1
string match -rq '^[0-9a-f]{64}$' -- "$backup_sha"; or exit 1
echo "source sha256: $source_sha_before"
echo "retained backup sha256: $backup_sha"

set rollback_parent /home/j2h4u/.cache
test -d "$rollback_parent"; and not test -L "$rollback_parent"; or exit 1
not test -e "$rollback_checkout"; or exit 1
set rollback_commit (git -C "$repo" rev-parse c6c5980); or exit 1
git worktree add --detach "$rollback_checkout" c6c5980; or exit 1
test (git -C "$rollback_checkout" rev-parse HEAD) = "$rollback_commit"; or exit 1

set candidate (mktemp -p "$state_dir" model.json.release-a.XXXXXX); or exit 1
set new_unit_candidate (mktemp -p "$state_dir" ups-battery-monitor.new-unit.XXXXXX); or exit 1
set rollback_unit_candidate (mktemp -p "$state_dir" ups-battery-monitor.rollback-unit.XXXXXX); or exit 1
for path in "$candidate" "$new_unit_candidate" "$rollback_unit_candidate"
    test -n "$path"; and test -f "$path"; and not test -L "$path"; or exit 1
    set path_parent (realpath -e (dirname -- "$path")); or exit 1
    test "$path_parent" = "$state_real"; or exit 1
    chmod 0600 "$path"; or exit 1
end

set epoch_id (python3 -c 'import uuid; print(uuid.uuid4())'); or exit 1
string match -rq '^[0-9a-f-]{36}$' -- "$epoch_id"; or exit 1
jq --arg battery_epoch_id "$epoch_id" '{
  soh: .soh,
  soh_history: .soh_history,
  capacity_estimates: [],
  capacity_ah_measured: null,
  physics: .physics,
  lut: .lut,
  r_internal_history: .r_internal_history,
  battery_install_date: .battery_install_date,
  battery_epoch_id: $battery_epoch_id,
  cycle_count: .cycle_count,
  cumulative_on_battery_sec: .cumulative_on_battery_sec,
  new_battery_detected: .new_battery_detected,
  new_battery_detected_timestamp: null,
  discharge_events: .discharge_events,
  last_upscmd_timestamp: .last_upscmd_timestamp,
  last_upscmd_type: .last_upscmd_type,
  last_upscmd_status: .last_upscmd_status
}' "$model" > "$candidate"; or exit 1
test -s "$candidate"; or exit 1
test (stat -c %a "$candidate") = 600; or exit 1

set run_user (id -un); or exit 1
set install_home (getent passwd "$run_user" | cut -d: -f6); or exit 1
test -n "$run_user"; and test -n "$install_home"; and not test "$install_home" = /; or exit 1
sed -e "s|@RUN_USER@|$run_user|g" -e "s|@INSTALL_DIR@|$repo|g" -e "s|@INSTALL_HOME@|$install_home|g" "$repo/systemd/ups-battery-monitor.service" > "$new_unit_candidate"; or exit 1
sed -e "s|@RUN_USER@|$run_user|g" -e "s|@INSTALL_DIR@|$rollback_checkout|g" -e "s|@INSTALL_HOME@|$install_home|g" "$rollback_checkout/systemd/ups-battery-monitor.service" > "$rollback_unit_candidate"; or exit 1
test -s "$new_unit_candidate"; and test -s "$rollback_unit_candidate"; or exit 1
test (stat -c %a "$new_unit_candidate") = 600; and test (stat -c %a "$rollback_unit_candidate") = 600; or exit 1
for path in "$new_unit_candidate" "$rollback_unit_candidate"
    not string match -q '*@RUN_USER@*' (cat "$path"); or exit 1
    not string match -q '*@INSTALL_DIR@*' (cat "$path"); or exit 1
    not string match -q '*@INSTALL_HOME@*' (cat "$path"); or exit 1
    string match -q '*ExecStart=/usr/bin/python3 -m src.monitor*' (cat "$path"); or exit 1
    string match -q '*WorkingDirectory=*' (cat "$path"); or exit 1
end

cd "$repo"; or exit 1
python3 -c 'import sys; from pathlib import Path; from src.model import BatteryModel, KNOWN_STATE_KEYS; p=Path(sys.argv[1]); BatteryModel(p); assert set(__import__("json").loads(p.read_text())) == KNOWN_STATE_KEYS' "$candidate"; or exit 1
release_a_preflight; or exit 1
set source_sha_pre_stop (sha256sum "$model" | cut -d ' ' -f 1)
test "$source_sha_pre_stop" = "$source_sha_before"; or exit 1
```

`model.json` после jq имеет ровно полную текущую 17-полевую схему: четыре
отсутствующих в старой копии поля получают `[]`, `null`, новый UUID и `null`.
Ни один обязательный существующий научный ключ не подменяется через `//`.
Проверка `BatteryModel` относится только к кандидату, не к старой резервной
копии. Кандидаты являются обычными файлами mode 0600 в том же state directory;
до остановки не изменяются `/etc` и не выполняется `daemon-reload`.

## Критическое окно: замена и восстановление защиты

Следующий блок выполняется сразу после подготовки. Функции восстановления не
запускают строгий текущий код на старом 13-полевом состоянии. При любой ошибке
до замены старый checkout включается через rollback unit. После замены либо
завершается запуск нового unit на 17-полевом состоянии, либо сначала
восстанавливается сохранённая 13-полевая копия и только затем включается старый
checkout. Если автоматическое восстановление не удалось, оператор получает
явное `CRITICAL`, и сервис нельзя считать защищённым.

При остановке systemd удаляет `RuntimeDirectory`: virtual UPS и shutdown
protection недоступны, а защита degraded только в целевом stop-to-start окне
`<30s`. Поэтому непосредственная проверка OL перед остановкой и заранее
подготовленный rollback обязательны.

```fish
function stop_and_require_inactive
    sudo systemctl stop --no-block ups-battery-monitor; or return 1
    for second in (seq 1 10)
        set active_state (systemctl show -p ActiveState --value ups-battery-monitor); or return 1
        if test "$active_state" = inactive; or test "$active_state" = failed
            return 0
        end
        sleep 1
    end
    echo "CRITICAL: ActiveState did not become exactly inactive or failed"
    return 1
end

function bounded_start
    set budget_sec $argv[1]
    sudo systemctl start --no-block ups-battery-monitor; or return 1
    for second in (seq 1 $budget_sec)
        set active_state (systemctl show -p ActiveState --value ups-battery-monitor); or return 1
        if test "$active_state" = active
            return 0
        end
        if test "$active_state" = failed
            echo "CRITICAL: service entered failed state during bounded start"
            return 1
        end
        sleep 1
    end
    echo "CRITICAL: bounded start timed out with ActiveState=$active_state"
    return 1
end

function recover_before_replace
    echo "Release A aborted before model replacement: $argv[1]"
    stop_and_require_inactive; or begin
        echo "CRITICAL: protection is not restored; old service did not stop"; return 1
    end
    if test -n "$candidate"; and test -e "$candidate"
        rm -- "$candidate"; or true
    end
    sudo install -o root -g root -m 0644 "$rollback_unit_candidate" "$unit_path"; or echo "CRITICAL: rollback unit install failed"
    sudo systemctl daemon-reload; or echo "CRITICAL: rollback daemon-reload failed"
    bounded_start 30; or echo "CRITICAL: protection is not restored"
end

function restore_after_replace
    echo "Release A failed after model replacement: $argv[1]"
    stop_and_require_inactive; or begin
        echo "CRITICAL: protection is not restored; strict service did not stop"; return 1
    end
    set restore_candidate (mktemp -p "$state_dir" model.json.rollback.XXXXXX); or begin
        echo "CRITICAL: cannot stage retained backup"; return 1
    end
    chmod 0600 "$restore_candidate"; or begin
        echo "CRITICAL: cannot protect rollback candidate"; return 1
    end
    cp -- "$backup_model" "$restore_candidate"; or begin
        echo "CRITICAL: cannot copy retained backup"; return 1
    end
    python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); assert len(d) == 13' "$restore_candidate"; or begin
        echo "CRITICAL: retained backup is not the expected old 13-key state"; return 1
    end
    sudo install -o root -g root -m 0644 "$rollback_unit_candidate" "$unit_path"; or begin
        echo "CRITICAL: rollback unit install failed"; return 1
    end
    sudo systemctl daemon-reload; or begin
        echo "CRITICAL: rollback daemon-reload failed"; return 1
    end
    mv -- "$restore_candidate" "$model"; or begin
        echo "CRITICAL: retained state restore failed"; return 1
    end
    bounded_start 30; or begin
        echo "CRITICAL: protection is not restored"; return 1
    end
end

release_a_preflight; or begin; recover_before_replace "preflight changed"; exit 1; end
stop_and_require_inactive; or begin; recover_before_replace "service stop failed"; exit 1; end
set source_sha_after_stop (sha256sum "$model" | cut -d ' ' -f 1)
test "$source_sha_after_stop" = "$source_sha_before"; or begin; recover_before_replace "source model changed during preflight"; exit 1; end
mv -- "$candidate" "$model"; or begin; recover_before_replace "atomic model replacement failed"; exit 1; end
set candidate ""
sudo install -o root -g root -m 0644 "$new_unit_candidate" "$unit_path"; or begin; restore_after_replace "new unit install failed"; exit 1; end
sudo systemctl daemon-reload; or begin; restore_after_replace "daemon-reload failed"; exit 1; end
bounded_start 10; or begin; restore_after_replace "new service failed to start within 10 seconds"; exit 1; end
```

Только этот блок содержит остановку. Его целевой stop-to-monitor-start интервал — менее
30 секунд; обычный запуск ограничен 10 секундами, rollback — 30 секундами,
поскольку старый unit имеет `TimeoutStartSec=30`. Все UUID, jq, проверки,
worktree и unit-файлы подготовлены заранее. Ни один запуск не блокирует
оператора дольше указанного бюджета.
Существующая NUT-служба не перезапускается.

## Приёмка и rollback

После старта проверить свежий health-файл, оба виртуальных выхода и физический
UPS. Остановка монитора удаляет его `RuntimeDirectory`, поэтому уже работающий
`dummy-ups` может потерять `.dev` и завершиться. Его штатный systemd unit имеет
`RestartSec=15s`: не считать первый `Driver not connected` отказом Release A,
а ограниченно ждать переподключения до 30 секунд.

```fish
test -s /run/ups-battery-monitor/ups-health.json; and jq -e '.startup_degraded == false and .model_update_mode == "capture_only" and .automatic_dispatch == false' /run/ups-battery-monitor/ups-health.json
upsc cyberpower@localhost ups.status
test -s /run/ups-battery-monitor/ups-virtual.dev
set virtual_ready false
for second in (seq 1 30)
    if upsc cyberpower-virtual@localhost ups.status 2>/dev/null | string match -rq '(^|[[:space:]])OL([[:space:]]|$)'
        set virtual_ready true
        break
    end
    sleep 1
end
test "$virtual_ready" = true
```

READY считается доказанным только после валидного свежего poll и успешной записи
обоих выходов. Если приёмка не проходит, не запускайте текущий строгий unit на
старой копии. Остановите текущий сервис, установите сохранённый rollback unit,
сделайте `daemon-reload`, атомарно положите старую 13-полевую копию из
сохранённой резервной директории и запустите старый checkout. Проверяйте, что
его commit остаётся `c6c5980`; это тот же порядок, что и
`restore_after_replace` выше. Резервная копия не подаётся текущему загрузчику.

Если требуется ручной rollback после приёмки, выполняйте его тем же pinned
checkout и unit-кандидатом до их очистки. Сначала убедитесь, что checkout и
кандидат существуют; если они уже удалены, восстановите именно checkout
`c6c5980` и заново отрендерьте rollback unit до остановки текущего сервиса:

```fish
test (git -C "$rollback_checkout" rev-parse HEAD) = "$rollback_commit"; or exit 1
test -f "$rollback_unit_candidate"; and not test -L "$rollback_unit_candidate"; or exit 1
release_a_preflight; or exit 1
stop_and_require_inactive; or begin
    echo "CRITICAL: current service did not become exactly inactive or failed; do not start rollback"; exit 1
end
sudo install -o root -g root -m 0644 "$rollback_unit_candidate" "$unit_path"; or exit 1
sudo systemctl daemon-reload; or exit 1
set rollback_candidate (mktemp -p "$state_dir" model.json.rollback.XXXXXX); or exit 1
chmod 0600 "$rollback_candidate"; or exit 1
cp -- "$backup_model" "$rollback_candidate"; or exit 1
python3 -c 'import json, sys; d=json.load(open(sys.argv[1])); assert len(d) == 13' "$rollback_candidate"; or exit 1
mv -- "$rollback_candidate" "$model"; or exit 1
bounded_start 30; or begin
    echo "CRITICAL: rollback service did not become active within 30 seconds"; exit 1
end
```

В этой последовательности rollback unit устанавливается и загружается до
возврата 13-полевого файла; текущий строгий loader никогда не видит backup.

Удалять rollback checkout, оба unit-кандидата и retained backup можно только
после полной окончательной приёмки. До этого rollback checkout и резервная
копия сохраняются. После приёмки удаляйте только явно перечисленные пути:

```fish
rm -- "$new_unit_candidate" "$rollback_unit_candidate"
if test -n "$candidate"; and test -e "$candidate"
    rm -- "$candidate"
end
git worktree remove "$rollback_checkout"
# retained backup удаляется отдельным подтверждённым действием после приёмки
```

В приложении нет преобразования состояния во время работы, совместимости со
старой схемой, запасного пути, самовосстановления, sentinel старой эпохи или
постоянного скрипта-конвертера. Исторические записи журнала без epoch остаются
исходными данными и не попадают в будущие когорты, ограниченные текущим epoch.
