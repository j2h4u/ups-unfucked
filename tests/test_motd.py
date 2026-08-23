"""Regression tests for the login-shell UPS status line."""

import os
import subprocess
from pathlib import Path

MOTD = Path(__file__).parents[1] / "scripts" / "motd" / "51-ups-health.sh"


def test_motd_prints_live_ups_summary(tmp_path: Path) -> None:
    upsc = tmp_path / "upsc"
    upsc.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' 'battery.charge: 100' 'battery.health: 91' "
        "'battery.runtime: 2848' 'ups.load: 15' 'ups.status: OL'\n"
    )
    upsc.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "UPS_NUT_ADDRESS": "fixture@localhost",
    }

    result = subprocess.run(
        ["bash", str(MOTD)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert "UPS: Online" in result.stdout
    assert "charge 100%" in result.stdout
    assert "runtime 47m" in result.stdout
    assert "load 15%" in result.stdout
    assert "health " in result.stdout
    assert "91%" in result.stdout
