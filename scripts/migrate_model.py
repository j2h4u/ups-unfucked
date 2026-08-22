#!/usr/bin/env python3
"""One-shot migration from the retired 18-key model to the strict predictor."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters import model_state_persistence as files
from src.adapters import model_state_schema as target
from src.adapters.battery_history import HISTORY_FILENAME
from src.application.safety import SafetyInputs, calculate_safety
from src.battery_math.constants import NOMINAL_POWER_WATTS, NOMINAL_VOLTAGE, RATED_CAPACITY_AH
from src.battery_math.lut import LutPoint
from src.domain.values import BlackoutKind, FrozenModelSnapshot

SOURCE_KEYS = frozenset(
    {
        "soh",
        "soh_history",
        "capacity_estimates",
        "capacity_ah_measured",
        "physics",
        "lut",
        "r_internal_history",
        "battery_install_date",
        "battery_epoch_id",
        "cycle_count",
        "cumulative_on_battery_sec",
        "new_battery_detected",
        "new_battery_detected_timestamp",
        "discharge_events",
        "last_upscmd_timestamp",
        "last_upscmd_type",
        "last_upscmd_status",
        "ir_learning_policy",
    }
)
GRID_LOADS = (0.0, 20.0, 100.0)
GRID_SHUTDOWNS = (1, 5, 60)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="publish the migration")
    return parser


def _state_dir(argument: Path | None) -> Path:
    if argument is not None:
        return argument
    user = os.environ.get("SUDO_USER")
    return (
        (Path(pwd.getpwnam(user).pw_dir) if user else Path.home())
        / ".config"
        / "ups-battery-monitor"
    )


def _source(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"model path is not a regular file: {path}")
    try:
        value = json.loads(files.read_model_file(path, error_type=RuntimeError))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid source JSON: {path}") from exc
    if not isinstance(value, dict) or set(value) != SOURCE_KEYS:
        raise RuntimeError("source model does not have the exact 18-key shape")
    physics = value["physics"]
    if not isinstance(physics, dict) or set(physics) != {"peukert_exponent", "ir_compensation"}:
        raise RuntimeError("source physics shape is not supported")
    ir = physics["ir_compensation"]
    if not isinstance(ir, dict) or set(ir) != {"k_volts_per_percent", "reference_load_percent"}:
        raise RuntimeError("source IR shape is not supported")
    if ir["reference_load_percent"] != 0.0:
        raise RuntimeError("source IR reference load must be zero")
    lut = value["lut"]
    if not isinstance(lut, list) or len(lut) < 2:
        raise RuntimeError("source LUT is not supported")
    for row in lut:
        if not isinstance(row, dict) or not {"v", "soc", "source"}.issubset(row):
            raise RuntimeError("source LUT entry is not supported")
    return value


def _target_state(source: dict[str, Any]) -> dict[str, Any]:
    physics, ir = source["physics"], source["physics"]["ir_compensation"]
    migrated = {
        "soh": source["soh"],
        "physics": {
            "peukert_exponent": physics["peukert_exponent"],
            "ir_compensation": {"k_volts_per_percent": ir["k_volts_per_percent"]},
        },
        "lut": [{"v": row["v"], "soc": row["soc"]} for row in source["lut"]],
    }
    target.validate_target_state(migrated, source="migrated model")
    return migrated


def _timestamp(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        moment = datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _history_records(source: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    if source["battery_install_date"] is not None:
        records.append(
            {"kind": "battery_installed", "at": _timestamp(source["battery_install_date"])}
        )
    seen: set[str] = set()
    for row in source["r_internal_history"]:
        record = {
            "kind": "ir_observation",
            "at": _timestamp(row["date"]),
            "r_ohm": row["r_ohm"],
            "v_before": row["v_before"],
            "v_sag": row["v_sag"],
            "load_pct": row["load_percent"],
            "event": row["event"],
        }
        identity = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if identity not in seen:
            seen.add(identity)
            records.append(record)
    records.extend(
        {
            "kind": "lut_observation",
            "at": datetime.fromtimestamp(float(row["timestamp"]), timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "v": row["v"],
            "soc": row["soc"],
        }
        for row in source["lut"]
        if row["source"] == "measured"
    )
    return records


def _snapshot(state: dict[str, Any]) -> FrozenModelSnapshot:
    physics, ir = state["physics"], state["physics"]["ir_compensation"]
    return FrozenModelSnapshot(
        rated_capacity_ah=RATED_CAPACITY_AH,
        nominal_voltage_v=NOMINAL_VOLTAGE,
        nominal_power_watts=NOMINAL_POWER_WATTS,
        soh=float(state["soh"]),
        peukert_exponent=float(physics["peukert_exponent"]),
        ir_k_v_per_pp=float(ir["k_volts_per_percent"]),
        ir_reference_load_percent=0.0,
        lut=tuple(LutPoint(float(row["v"]), float(row["soc"])) for row in state["lut"]),
    )


def prove_prediction_equivalence(source: dict[str, Any], migrated: dict[str, Any]) -> int:
    old, new = _snapshot(source), _snapshot(migrated)
    voltages = {float(row["v"]) for row in source["lut"]}
    voltages.update({0.0, 12.0, 13.7})
    for voltage in tuple(voltages):
        voltages.update({voltage - 0.011, voltage - 0.009, voltage + 0.009, voltage + 0.011})
    cases = 0
    for voltage in sorted(voltages):
        for load in GRID_LOADS:
            for kind in BlackoutKind:
                for shutdown in GRID_SHUTDOWNS:
                    inputs = SafetyInputs(voltage, load, kind, shutdown)
                    if calculate_safety(inputs=inputs, snapshot=old) != calculate_safety(
                        inputs=inputs, snapshot=new
                    ):
                        raise RuntimeError(f"prediction equivalence failed at case {cases}")
                    cases += 1
    return cases


def _append_history(path: Path, original: bytes, appended: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_size != len(original):
            raise RuntimeError("history changed during migration")
        os.write(fd, appended)
        os.fsync(fd)
    except BaseException:
        os.ftruncate(fd, len(original))
        os.fsync(fd)
        raise
    finally:
        os.close(fd)


def _restore_history(path: Path, raw: bytes, existed: bool) -> None:
    if not existed:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.restore.tmp")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(state_dir: Path, *, apply: bool) -> str:
    model_path = state_dir / "model.json"
    history_path = state_dir / "events" / HISTORY_FILENAME
    source = _source(model_path)
    migrated = _target_state(source)
    if history_path.is_symlink() or history_path.exists() and not history_path.is_file():
        raise RuntimeError(f"history path is not a regular file: {history_path}")
    history_existed = history_path.exists()
    old_history = history_path.read_bytes() if history_existed else b""
    records = _history_records(source)
    appended = b"".join(
        (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        for row in records
    )
    staged_history = old_history + appended
    if any(not isinstance(json.loads(line), dict) for line in staged_history.splitlines()):
        raise RuntimeError("staged history contains a non-object record")
    cases = prove_prediction_equivalence(source, migrated)
    if apply:
        lock = files.acquire_writer_lock(state_dir / "monitor.lock")
        try:
            if records:
                _append_history(history_path, old_history, appended)
            try:
                files.atomic_write_model(model_path, target.canonical_json(migrated))
            except BaseException:
                if records:
                    _restore_history(history_path, old_history, history_existed)
                raise
        finally:
            files.release_writer_lock(lock)
    return (
        f"{'applied' if apply else 'dry-run'}: lut_points={len(migrated['lut'])} "
        f"history_records={len(records)} "
        f"equivalence_cases={cases} target_keys=soh,physics,lut"
    )


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
        print(migrate(_state_dir(arguments.state_dir), apply=arguments.apply))
    except (RuntimeError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"migrate-model: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
