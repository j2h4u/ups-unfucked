"""Focused tests for the Slice-0 capability CLI composition root."""

from pathlib import Path
from typing import Any

import pytest

from src import telemetry_capability_cli as cli
from src.adapters.telemetry_capability_baseline import ARTIFACT_FILENAME, NUTEndpoint
from src.monitor_config import Config, ConfigError


def _config(*, ups_name: str = "cyberpower") -> Config:
    return Config(ups_name=ups_name, model_dir=Path("/configured/model"))


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_record_path_composes_physical_client_and_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client_instances: list[FakeClient] = []
    record_calls: list[tuple[object, Path, NUTEndpoint]] = []

    def make_client(**kwargs: Any) -> FakeClient:
        client = FakeClient(**kwargs)
        client_instances.append(client)
        return client

    def record(client: object, destination: Path, *, endpoint: NUTEndpoint) -> None:
        record_calls.append((client, destination, endpoint))

    monkeypatch.setattr(cli, "load_config", lambda: _config())
    monkeypatch.setattr(cli, "NUTClient", make_client)
    monkeypatch.setattr(cli, "record_baseline", record)

    output = f"/tmp/{ARTIFACT_FILENAME}"
    assert cli.main(["--output", output]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"recorded {output}\n"
    assert captured.err == ""
    assert len(client_instances) == 1
    assert client_instances[0].kwargs == {
        "host": "localhost",
        "port": 3493,
        "timeout": 2.0,
        "ups_name": "cyberpower",
    }
    assert len(record_calls) == 1
    client, destination, endpoint = record_calls[0]
    assert client is client_instances[0]
    assert destination == Path(output)
    assert endpoint.host == "localhost"
    assert endpoint.port == 3493
    assert endpoint.ups_name == "cyberpower"


def test_verify_path_passes_configured_endpoint_to_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client_instances: list[FakeClient] = []
    verify_calls: list[tuple[Path, object, dict[str, object]]] = []

    def make_client(**kwargs: Any) -> FakeClient:
        client = FakeClient(**kwargs)
        client_instances.append(client)
        return client

    def verify(path: Path, client: object, **kwargs: object) -> None:
        verify_calls.append((path, client, kwargs))

    monkeypatch.setattr(cli, "load_config", lambda: _config())
    monkeypatch.setattr(cli, "NUTClient", make_client)
    monkeypatch.setattr(cli, "verify_baseline", verify)

    output = f"/tmp/{ARTIFACT_FILENAME}"
    assert cli.main(["--verify", "--output", output]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"verified {output}\n"
    assert captured.err == ""
    assert len(verify_calls) == 1
    path, client, endpoint = verify_calls[0]
    assert path == Path(output)
    assert client is client_instances[0]
    assert endpoint == {"host": "localhost", "port": 3493, "ups_name": "cyberpower"}


def test_real_adapter_rejects_noncanonical_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _config())
    monkeypatch.setattr(cli, "NUTClient", FakeClient)

    noncanonical = tmp_path / "telemetry-baseline.json"
    assert cli.main(["--output", str(noncanonical)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"telemetry baseline refused: destination must be named {ARTIFACT_FILENAME}\n"
    )


def test_output_help_names_canonical_filename(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    captured = capsys.readouterr()
    assert "filename must be telemetry-" in captured.out
    assert "capability-baseline-v1.json" in captured.out


def test_virtual_ups_is_refused_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def fail_client(**_kwargs: Any) -> FakeClient:
        raise AssertionError("virtual UPS must be refused before client construction")

    monkeypatch.setattr(cli, "load_config", lambda: _config(ups_name="cyberpower-virtual"))
    monkeypatch.setattr(cli, "NUTClient", fail_client)

    assert cli.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "telemetry baseline refused: configured UPS is virtual; baseline requires the physical UPS\n"
    )


def test_config_error_returns_refusal_exit_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: (_ for _ in ()).throw(ConfigError("broken")))

    assert cli.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "telemetry baseline refused: broken\n"
