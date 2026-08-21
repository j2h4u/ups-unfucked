import importlib.util
from pathlib import Path

import pytest

from src.adapters.minimal_event_file import encode, sample


def _script():
    path = Path(__file__).parents[2] / "scripts" / "blackout-history.py"
    spec = importlib.util.spec_from_file_location("blackout_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(at: str, status: str, battery_pct: float) -> dict[str, object]:
    return sample(at, 13.3, battery_pct, 600.0, 20.0, 0.0 if "OB" in status else 230.0, 230.0, status)


def _write(root: Path, rows: list[dict[str, object]]) -> Path:
    path = root / "events" / "telemetry.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"".join(encode(row) for row in rows))
    return path


def test_year_counts_natural_restored_and_unfinished_but_excludes_cal(tmp_path: Path) -> None:
    module = _script()
    path = _write(
        tmp_path,
        [
            _row("2026-01-01T00:00:00Z", "OB DISCHRG", 40.0),
            _row("2026-01-01T00:00:01Z", "OL CHRG", 80.0),
            _row("2026-01-02T00:00:00Z", "OB CAL", 40.0),
            _row("2026-01-02T00:00:01Z", "OL", 100.0),
            _row("2026-01-03T00:00:00Z", "OB DISCHRG", 40.0),
        ],
    )

    start, end = module._period(module._parser().parse_args(["--year", "2026"]))

    assert module._episodes(path, start, end) == (
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"),
        ("2026-01-03T00:00:00Z", None),
    )


def test_day_is_half_open_at_next_midnight(tmp_path: Path) -> None:
    module = _script()
    path = _write(
        tmp_path,
        [
            _row("2026-01-01T23:59:59Z", "OB DISCHRG", 40.0),
            _row("2026-01-02T00:00:00Z", "OL", 100.0),
            _row("2026-01-02T00:00:00Z", "OB DISCHRG", 40.0),
        ],
    )
    start, end = module._period(module._parser().parse_args(["--day", "2026-01-01"]))

    assert (end - start).total_seconds() == 86400
    assert module._episodes(path, start, end) == (("2026-01-01T23:59:59Z", "2026-01-02T00:00:00Z"),)


def test_malformed_row_fails_with_concise_error(tmp_path: Path, capsys) -> None:
    module = _script()
    path = tmp_path / "events" / "telemetry.jsonl"
    path.parent.mkdir()
    path.write_text("not-json\n")

    with pytest.raises(SystemExit, match="2"):
        module.main(["--state-dir", str(tmp_path), "--day", "2026-01-01"])

    assert "blackout-history:" in capsys.readouterr().err
