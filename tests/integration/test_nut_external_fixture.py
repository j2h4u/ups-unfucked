"""External NUT 2.8.x fixture with no production sockets or service control."""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

WAIT_SECONDS = 8.0
COMMAND_TIMEOUT_SECONDS = 1.0
NUT_USERNAME = "fixture-monitor"
NUT_PASSWORD = "fixture-password"
_UNSHARE_MONITOR_CODE = (
    "import ctypes, os, sys\n"
    "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
    "if libc.mount(b'tmpfs', b'/run', b'tmpfs', 0, b'size=1m') != 0:\n"
    "    raise OSError(ctypes.get_errno(), 'mount private /run')\n"
    "os.mkdir('/run/nut')\n"
    "if libc.mount(sys.argv[1].encode(), b'/run/nut', None, 4096, None) != 0:\n"
    "    raise OSError(ctypes.get_errno(), 'bind private NUT pid path')\n"
    "os.execv(sys.argv[2], [sys.argv[2], *sys.argv[3:]])\n"
)


@dataclass(frozen=True, slots=True)
class NutBinaries:
    driver: Path
    server: Path
    monitor: Path
    client: Path


@dataclass
class NutSandbox:
    root: Path
    binaries: NutBinaries
    port: int
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    named_processes: dict[str, subprocess.Popen[str]] = field(default_factory=dict)
    log_handles: list[object] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.conf = self.root / "conf"
        self.state = self.root / "state"
        self.pid = self.root / "pid"
        self.logs = self.root / "logs"
        self.data_file = self.root / "fixture.dev"
        self.shutdown_marker = self.root / "shutdown.marker"
        self.powerdown_flag = self.root / "powerdown.flag"
        self.root.mkdir(mode=0o700)
        for path in (self.conf, self.state, self.pid, self.logs):
            path.mkdir(mode=0o700)
        self._write_shutdown_command()

    @property
    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "NUT_CONFPATH": str(self.conf),
                "NUT_STATEPATH": str(self.state),
                "NUT_ALTPIDPATH": str(self.pid),
            }
        )
        return environment

    def write_data(self, status: str) -> None:
        self.data_file.write_text(
            "\n".join(
                (
                    "battery.charge: 100",
                    "battery.runtime: 600",
                    "battery.runtime.low: 120",
                    "device.mfr: fixture",
                    "device.model: external-nut-fixture",
                    f"ups.status: {status}",
                    "ups.mfr: fixture",
                    "ups.model: external-nut-fixture",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.data_file.chmod(0o600)

    def write_config(self) -> None:
        self._write(
            "ups.conf",
            f"[fixture]\ndriver = dummy-ups\nport = {self.data_file}\ndesc = external fixture\n",
        )
        self._write(
            "upsd.conf",
            f"STATEPATH {self.state}\nLISTEN 127.0.0.1 {self.port}\nMAXAGE 2\n",
        )
        self._write(
            "upsd.users",
            f"[{NUT_USERNAME}]\npassword = {NUT_PASSWORD}\nupsmon primary\n",
        )
        self._write(
            "upsmon.conf",
            "\n".join(
                (
                    f"MONITOR fixture@127.0.0.1:{self.port} 1 {NUT_USERNAME} {NUT_PASSWORD} primary",
                    "MINSUPPLIES 1",
                    "POLLFREQ 1",
                    "POLLFREQALERT 1",
                    "DEADTIME 3",
                    "FINALDELAY 0",
                    "HOSTSYNC 2",
                    f"RUN_AS_USER {self._current_user()}",
                    f"SHUTDOWNCMD {self.root / 'shutdown.sh'}",
                    f"POWERDOWNFLAG {self.powerdown_flag}",
                    "NOTIFYFLAG ONLINE IGNORE",
                    "NOTIFYFLAG ONBATT IGNORE",
                    "NOTIFYFLAG LOWBATT IGNORE",
                    "NOTIFYFLAG FSD IGNORE",
                    "NOTIFYFLAG COMMOK IGNORE",
                    "NOTIFYFLAG COMMBAD IGNORE",
                    "NOTIFYFLAG NOCOMM IGNORE",
                )
            )
            + "\n",
        )
        for path in (self.conf / "ups.conf", self.conf / "upsd.conf", self.conf / "upsmon.conf"):
            path.chmod(0o600)
        (self.conf / "upsd.users").chmod(0o600)

    def start_driver(self) -> None:
        self._spawn("driver", [self.binaries.driver, "-a", "fixture", "-F"])

    def start_server(self) -> None:
        self._spawn("server", [self.binaries.server, "-F"])

    def start_monitor(self) -> None:
        unshare = shutil.which("unshare")
        if unshare is None:
            raise AssertionError("unshare is required to isolate upsmon's compiled PID path")
        self._spawn(
            "monitor",
            [
                unshare,
                "--user",
                "--map-root-user",
                "--mount",
                "--propagation",
                "private",
                "--fork",
                sys.executable,
                "-c",
                _UNSHARE_MONITOR_CODE,
                str(self.pid),
                self.binaries.monitor,
                "-F",
                "-p",
            ],
        )

    def stop_driver(self) -> None:
        process = self.named_processes.get("driver")
        if process is not None:
            self._stop_process(process)

    def query(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binaries.client, f"fixture@127.0.0.1:{self.port}"],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

    def status(self) -> str | None:
        result = self.query()
        for line in result.stdout.splitlines():
            if line.startswith("ups.status:"):
                return line.split(":", 1)[1].strip()
        return None

    def wait_for(self, predicate: Callable[[], bool], description: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {description}\n{self.logs_text()}")

    def logs_text(self) -> str:
        chunks = []
        for path in sorted(self.logs.glob("*.log")):
            chunks.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8')}")
        return "\n".join(chunks)

    def close(self) -> None:
        for process in reversed(self.processes):
            self._stop_process(process)
        for handle in self.log_handles:
            handle.close()  # type: ignore[union-attr]
        self.log_handles.clear()

    def _write_shutdown_command(self) -> None:
        script = self.root / "shutdown.sh"
        script.write_text(
            f"#!/bin/sh\nprintf 'shutdown-called\\n' >> {self.shutdown_marker}\n",
            encoding="utf-8",
        )
        script.chmod(0o700)

    def _write(self, name: str, contents: str) -> None:
        (self.conf / name).write_text(contents, encoding="utf-8")

    def _spawn(self, name: str, command: list[Path | str]) -> None:
        log = (self.logs / f"{name}.log").open("w", encoding="utf-8")
        self.log_handles.append(log)
        process = subprocess.Popen(
            [str(argument) for argument in command],
            env=self.environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self.processes.append(process)
        self.named_processes[name] = process

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)

    @staticmethod
    def _current_user() -> str:
        return os.environ.get("USER") or str(os.getuid())


def _discover_binaries() -> NutBinaries | None:
    candidates = {
        "driver": ("/usr/lib/nut/dummy-ups", "/lib/nut/dummy-ups"),
        "server": ("/usr/lib/nut/upsd", "/lib/nut/upsd"),
        "monitor": ("/usr/lib/nut/upsmon", "/lib/nut/upsmon"),
        "client": ("/usr/bin/upsc", "/bin/upsc"),
    }
    found: dict[str, Path] = {}
    for name, paths in candidates.items():
        executable = shutil.which(paths[0].split("/")[-1])
        possible = [executable, *paths]
        selected = next(
            (
                Path(path)
                for path in possible
                if path and Path(path).is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
        if selected is None:
            return None
        found[name] = selected
    return NutBinaries(**found)


def _version(binary: Path) -> tuple[int, int, int] | None:
    result = subprocess.run(
        [binary, "-V"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    text = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def nut_sandbox(tmp_path: Path) -> Iterator[NutSandbox]:
    binaries = _discover_binaries()
    if binaries is None:
        pytest.skip("NUT external fixture binaries are unavailable")
    versions = {
        name: _version(binary)
        for name, binary in (
            ("driver", binaries.driver),
            ("server", binaries.server),
            ("monitor", binaries.monitor),
            ("client", binaries.client),
        )
    }
    unsupported = {
        name: version
        for name, version in versions.items()
        if version is None or version < (2, 8, 0)
    }
    if unsupported:
        pytest.skip(f"external fixture requires NUT >= 2.8.0; observed versions: {versions}")
    sandbox = NutSandbox(tmp_path / "nut-fixture", binaries, _free_port())
    try:
        yield sandbox
    finally:
        sandbox.close()


def test_dev_ol_then_file_removed_and_driver_dead_is_not_fresh_ol(
    nut_sandbox: NutSandbox,
) -> None:
    nut_sandbox.write_data("OL")
    nut_sandbox.write_config()
    nut_sandbox.start_driver()
    nut_sandbox.start_server()
    nut_sandbox.wait_for(lambda: nut_sandbox.status() == "OL", "initial OL")

    nut_sandbox.data_file.unlink()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.status() == "OL",
        "dummy-once to retain OL after source file unlink",
    )
    nut_sandbox.stop_driver()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.query().returncode != 0,
        "upsd to stop serving the dead dummy driver",
    )
    assert nut_sandbox.status() is None

    nut_sandbox.write_data("OL")
    nut_sandbox.start_driver()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.status() == "OL",
        "dummy driver to return after a fresh source file",
    )


def test_cold_start_without_dev_file_does_not_publish_ol(
    nut_sandbox: NutSandbox,
) -> None:
    nut_sandbox.write_config()
    nut_sandbox.start_driver()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.processes[0].poll() is not None,
        "dummy driver to reject missing cold-start file",
    )
    nut_sandbox.start_server()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.query().returncode != 0,
        "upsd to expose unavailable cold-start driver",
    )
    assert nut_sandbox.status() is None


def test_explicit_ob_lb_reaches_upsmon_and_only_temp_shutdown_marker(
    nut_sandbox: NutSandbox,
) -> None:
    nut_sandbox.write_data("OB LB")
    nut_sandbox.write_config()
    nut_sandbox.start_driver()
    nut_sandbox.start_server()
    nut_sandbox.wait_for(lambda: nut_sandbox.status() == "OB LB", "explicit OB LB")
    nut_sandbox.start_monitor()
    nut_sandbox.wait_for(
        lambda: nut_sandbox.shutdown_marker.exists(),
        "harmless upsmon shutdown command",
    )
    assert nut_sandbox.shutdown_marker.read_text(encoding="utf-8") == "shutdown-called\n"
    assert nut_sandbox.powerdown_flag.exists()
    assert nut_sandbox.powerdown_flag.read_text(encoding="utf-8") == "upsmon-shutdown-file"
    assert (nut_sandbox.pid / "upsmon.pid").is_file()
