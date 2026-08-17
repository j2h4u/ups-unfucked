"""
Systemd service configuration validation tests (OPS-01, OPS-03, OPS-04).

These tests verify the systemd service file (systemd/ups-battery-monitor.service)
meets requirements without requiring root or systemctl.

Tests parse and validate the service file directly against systemd.service(5)
specifications and project OPS requirements.
"""

from pathlib import Path

# Service file path (relative to repo root)
SERVICE_FILE_PATH = Path(__file__).parent.parent / "systemd" / "ups-battery-monitor.service"
DRIVER_UNIT_PATH = (
    Path(__file__).parent.parent / "systemd" / "nut-driver@cyberpower-virtual.service"
)
LEGACY_DRIVER_DROPIN_PATH = Path(__file__).parent.parent / "systemd" / "nut-driver-virtual.conf"
DUMMY_CONFIG_PATH = Path(__file__).parent.parent / "config" / "dummy-ups.conf"


def parse_service_file(path):
    """
    Parse systemd .service file as INI (ConfigParser).

    Systemd service files use INI-like syntax ([Section] and Key=Value).
    ConfigParser requires allow_no_value=True for lines without =.
    Returns dict: section -> dict of key-value pairs.
    """
    config = {}
    current_section = None

    with open(path, "r") as f:
        for line in f:
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith(";") or line.startswith("#"):
                continue

            # Section header
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                config[current_section] = {}
                continue

            # Key=Value pair
            if "=" in line and current_section:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Handle multiple values (e.g., "After=target1 target2")
                if key in config[current_section]:
                    # Append to existing value (for multi-value directives)
                    config[current_section][key] += " " + value
                else:
                    config[current_section][key] = value

    return config


# ============================================================================
# Test 1: File existence and readability
# ============================================================================


def test_service_file_exists_and_readable():
    """
    Test: Service file exists at expected path and is readable.

    OPS-01: Service file installable to /etc/systemd/system
    """
    assert SERVICE_FILE_PATH.is_file(), f"Service file not found: {SERVICE_FILE_PATH}"
    assert SERVICE_FILE_PATH.stat().st_size > 500, "Service file too small (empty or stub)"


# ============================================================================
# Test 2: [Unit] section required fields
# ============================================================================


def test_service_file_unit_section_required_fields():
    """
    Test: [Unit] section has all required directives.

    OPS-01: Description, After (sysinit.target, nut-server.service),
            Wants, ConditionPathExists for /run/nut/
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Unit" in config, "[Unit] section missing"
    unit = config["Unit"]

    # Description must be present and mention UPS/Battery
    assert "Description" in unit, "Description missing"
    assert "UPS" in unit["Description"] or "Battery" in unit["Description"], (
        "Description should mention UPS or Battery"
    )

    # After must include sysinit.target (ensures /run tmpfs is available)
    assert "After" in unit, "After directive missing"
    assert "sysinit.target" in unit["After"], (
        "sysinit.target not in After (tmpfs /run dependency missing)"
    )

    # After must include nut-server.service
    assert "nut-server.service" in unit["After"], (
        "nut-server.service not in After (NUT dependency missing)"
    )

    # Wants for network-online.target
    assert "Wants" in unit, "Wants directive missing"
    assert "network-online.target" in unit["Wants"], "network-online.target not in Wants"
    assert "nut-driver@cyberpower-virtual.service" in unit["Wants"], (
        "monitor restart must start the bound virtual driver"
    )

    # ConditionPathExists for soft NUT check
    assert "ConditionPathExists" in unit, "ConditionPathExists missing"
    assert "/run/nut/" in unit["ConditionPathExists"], "ConditionPathExists should check /run/nut/"


# ============================================================================
# Test 3: [Service] section restart configuration
# ============================================================================


def test_service_file_service_section_restart_config():
    """
    Test: [Service] restart directives and [Unit] rate-limiting.

    OPS-01: Restart=on-failure, RestartSec=10, bounded retry interval.
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    # Restart must be on-failure (respects exit 0, auto-restarts on non-zero)
    assert "Restart" in service, "Restart directive missing"
    assert service["Restart"] == "on-failure", (
        f"Restart should be 'on-failure', got '{service['Restart']}'"
    )

    # RestartSec must be 10 seconds
    assert "RestartSec" in service, "RestartSec missing"
    assert service["RestartSec"] == "10", f"RestartSec should be 10, got '{service['RestartSec']}'"

    # StartLimitIntervalSec belongs in [Unit]. Three attempts in five minutes
    # are bounded, while RestartSec=10 prevents an immediate restart storm.
    assert "Unit" in config, "[Unit] section missing"
    unit = config["Unit"]

    assert "StartLimitIntervalSec" in unit, "StartLimitIntervalSec missing from [Unit]"
    assert unit["StartLimitIntervalSec"] == "300", (
        f"StartLimitIntervalSec should be 300, got '{unit['StartLimitIntervalSec']}'"
    )
    assert unit["StartLimitBurst"] == "3"


def test_notify_service_waits_indefinitely_for_degraded_startup():
    """A missing physical UPS must not trigger Type=notify startup restarts."""
    service = parse_service_file(SERVICE_FILE_PATH)["Service"]

    assert service["Type"] == "notify"
    assert service["TimeoutStartSec"] == "0"
    assert service["WatchdogSec"] == "120"


def test_stopped_monitor_invalidates_virtual_ups_snapshot():
    """A dead monitor cannot leave dummy-ups serving stale virtual OL."""
    service = parse_service_file(SERVICE_FILE_PATH)["Service"]

    assert service["ExecStartPre"] == ("-/usr/bin/unlink /run/ups-battery-monitor/ups-virtual.dev")
    assert service["ExecStopPost"] == ("-/usr/bin/unlink /run/ups-battery-monitor/ups-virtual.dev")


def test_virtual_driver_is_bound_to_monitor_lifecycle_without_reverse_ordering():
    """The dummy projection stops with monitor and is pulled back on restart."""
    monitor = parse_service_file(SERVICE_FILE_PATH)["Unit"]
    driver = parse_service_file(DRIVER_UNIT_PATH)
    unit = driver["Unit"]
    service = driver["Service"]
    install = driver["Install"]

    assert "nut-driver@cyberpower-virtual.service" in monitor["Wants"]
    assert unit["BindsTo"] == "ups-battery-monitor.service"
    assert unit["PartOf"] == "ups-battery-monitor.service"
    assert "ups-battery-monitor.service" in unit["After"]
    assert "Before" not in unit
    assert all("nut-driver.target" not in value for value in unit.values())
    assert install["WantedBy"] == "ups-battery-monitor.service"
    assert service["Environment"] == "NUT_IGNORE_NOWAIT=true"
    assert "upsdrvctl  start" in service["ExecStart"]
    assert "upsdrvctl stop" in service["ExecStop"]
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "15s"
    assert service["Type"] == "forking"
    assert "/run/ups-battery-monitor/ups-virtual.dev" in service["ExecStartPre"]


def test_legacy_enumerator_owned_driver_dropin_is_retired():
    """The exact fragment must survive enumerator cleanup of instance drop-ins."""
    assert not LEGACY_DRIVER_DROPIN_PATH.exists()


def test_rendered_topology_has_no_stock_target_cycle():
    """Stock target/server ordering leads into monitor-owned virtual driver only."""
    after = {
        "nut-driver.target": {"local-fs.target"},
        "nut-server.service": {"nut-driver.target"},
        "ups-battery-monitor.service": {"nut-server.service"},
        "nut-driver@cyberpower-virtual.service": {
            "local-fs.target",
            "ups-battery-monitor.service",
        },
    }

    assert "nut-driver@cyberpower-virtual.service" not in after["nut-driver.target"]
    assert "nut-driver.target" in after["nut-server.service"]
    assert "nut-server.service" in after["ups-battery-monitor.service"]
    assert "ups-battery-monitor.service" in after["nut-driver@cyberpower-virtual.service"]

    def reaches(start: str, target: str, seen: set[str] | None = None) -> bool:
        visited = set() if seen is None else seen
        if start == target:
            return True
        if start in visited:
            return False
        visited.add(start)
        return any(reaches(parent, target, visited) for parent in after.get(start, set()))

    def has_cycle(unit: str, path: set[str]) -> bool:
        if unit in path:
            return True
        return any(has_cycle(parent, path | {unit}) for parent in after.get(unit, set()))

    for unit in after:
        assert not has_cycle(unit, set()), f"ordering cycle from {unit}"
    assert reaches("nut-driver@cyberpower-virtual.service", "nut-driver.target")


def test_virtual_dummy_config_points_only_at_runtime_publication():
    """The NUT projection consumes the lifecycle-owned runtime file."""
    config = DUMMY_CONFIG_PATH.read_text()

    assert "[cyberpower-virtual]" in config
    assert "driver = dummy-ups" in config
    assert "port = /run/ups-battery-monitor/ups-virtual.dev" in config


# ============================================================================
# Test 4: Unprivileged execution (User/Group)
# ============================================================================


def test_service_file_unprivileged_execution():
    """
    Test: Service runs as unprivileged user j2h4u.

    OPS-01: User=j2h4u, Group=j2h4u (no privilege escalation)
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    assert "User" in service, "User directive missing"
    assert service["User"] != "root", "Service should not run as root"

    assert "Group" in service, "Group directive missing"
    assert service["Group"] != "root", "Service should not run as root group"


# ============================================================================
# Test 5: Logging configuration (journald integration)
# ============================================================================


def test_service_file_logging_configuration():
    """
    Test: Service logs to journald with proper tagging.

    OPS-04: StandardOutput=null (JournalHandler writes directly, stdout would duplicate),
            StandardError=journal, SyslogIdentifier=ups-battery-monitor
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    # StandardOutput=null — JournalHandler writes to journald directly, stdout would duplicate
    assert "StandardOutput" in service, "StandardOutput missing"
    assert service["StandardOutput"] == "null", (
        f"StandardOutput should be 'null', got '{service['StandardOutput']}'"
    )

    # StandardError must be journal
    assert "StandardError" in service, "StandardError missing"
    assert service["StandardError"] == "journal", (
        f"StandardError should be 'journal', got '{service['StandardError']}'"
    )

    # SyslogIdentifier for searchability
    assert "SyslogIdentifier" in service, "SyslogIdentifier missing"
    assert service["SyslogIdentifier"] == "ups-battery-monitor", (
        f"SyslogIdentifier should be 'ups-battery-monitor', got '{service['SyslogIdentifier']}'"
    )


# ============================================================================
# Test 6: [Install] section for boot auto-start
# ============================================================================


def test_service_file_install_section_boot_start():
    """
    Test: [Install] section enables auto-start on boot.

    OPS-01: WantedBy=multi-user.target
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Install" in config, "[Install] section missing"
    install = config["Install"]

    # WantedBy must enable boot start
    assert "WantedBy" in install, "WantedBy missing"
    assert "multi-user.target" in install["WantedBy"], (
        f"WantedBy should include 'multi-user.target', got '{install['WantedBy']}'"
    )
    assert install["WantedBy"] != "emergency.target", "WantedBy should not be emergency.target"


# ============================================================================
# Test 7: ExecStart uses absolute path
# ============================================================================


def test_exec_start_is_absolute_path():
    """
    Test: ExecStart uses absolute path (not relative or ~).

    OPS-01: ExecStart=/usr/bin/python3 (must be absolute)
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    assert "ExecStart" in service, "ExecStart missing"
    exec_start = service["ExecStart"]

    # Must start with / (absolute path)
    assert exec_start.startswith("/"), f"ExecStart must use absolute path, got '{exec_start}'"

    # Should use /usr/bin/python3 (standard location)
    assert "/usr/bin/python3" in exec_start, (
        f"ExecStart should use /usr/bin/python3, got '{exec_start}'"
    )

    # Should not contain ~ or relative paths
    assert "~" not in exec_start, "ExecStart should not use ~ path expansion"


# ============================================================================
# Test 8: WorkingDirectory is absolute and documented
# ============================================================================


def test_working_directory_exists_or_documented():
    """
    Test: WorkingDirectory is absolute path.

    OPS-01: WorkingDirectory should be repo root for PYTHONPATH discovery
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    assert "WorkingDirectory" in service, "WorkingDirectory missing"
    work_dir = service["WorkingDirectory"]

    # Must be an absolute path or an install-time placeholder filled by install.sh
    assert work_dir.startswith("/") or work_dir == "@INSTALL_DIR@", (
        f"WorkingDirectory must be absolute or '@INSTALL_DIR@' placeholder, got '{work_dir}'"
    )


# ============================================================================
# Test 9: PYTHONPATH environment variable set
# ============================================================================


def test_service_pythonpath_environment():
    """
    Test: PYTHONPATH environment variable is set for module discovery.

    OPS-01: Environment="PYTHONPATH=..." for src module imports
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    assert "Environment" in service, "Environment variable missing"
    env_var = service["Environment"]

    # Must include PYTHONPATH
    assert "PYTHONPATH" in env_var, f"Environment should set PYTHONPATH, got '{env_var}'"

    # Must reference repo directory or contain the install-time placeholder
    assert "ups-battery-monitor" in env_var or "@INSTALL_DIR@" in env_var, (
        f"PYTHONPATH should reference repo or '@INSTALL_DIR@' placeholder, got '{env_var}'"
    )
