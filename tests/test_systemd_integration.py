"""
Systemd service configuration validation tests (OPS-01, OPS-03, OPS-04).

These tests verify the systemd service file (systemd/ups-battery-monitor.service)
meets Phase 5 requirements without requiring root or systemctl.

Tests parse and validate the service file directly against systemd.service(5)
specifications and project OPS requirements.
"""

import re
from pathlib import Path
from configparser import ConfigParser
import io
import pytest


# Service file path (relative to repo root)
SERVICE_FILE_PATH = Path(__file__).parent.parent / "systemd" / "ups-battery-monitor.service"


def parse_service_file(path):
    """
    Parse systemd .service file as INI (ConfigParser).

    Systemd service files use INI-like syntax ([Section] and Key=Value).
    ConfigParser requires allow_no_value=True for lines without =.
    Returns dict: section -> dict of key-value pairs.
    """
    config = {}
    current_section = None

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith(';') or line.startswith('#'):
                continue

            # Section header
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1]
                config[current_section] = {}
                continue

            # Key=Value pair
            if '=' in line and current_section:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # Handle multiple values (e.g., "After=target1 target2")
                if key in config[current_section]:
                    # Append to existing value (for multi-value directives)
                    config[current_section][key] += ' ' + value
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
    assert "UPS" in unit["Description"] or "Battery" in unit["Description"], \
        "Description should mention UPS or Battery"

    # After must include sysinit.target (ensures /run tmpfs is available)
    assert "After" in unit, "After directive missing"
    assert "sysinit.target" in unit["After"], \
        "sysinit.target not in After (tmpfs /run dependency missing)"

    # After must include nut-server.service
    assert "nut-server.service" in unit["After"], \
        "nut-server.service not in After (NUT dependency missing)"

    # Wants for network-online.target
    assert "Wants" in unit, "Wants directive missing"
    assert "network-online.target" in unit["Wants"], \
        "network-online.target not in Wants"

    # ConditionPathExists for soft NUT check
    assert "ConditionPathExists" in unit, "ConditionPathExists missing"
    assert "/run/nut/" in unit["ConditionPathExists"], \
        "ConditionPathExists should check /run/nut/"


# ============================================================================
# Test 3: [Service] section restart configuration
# ============================================================================

def test_service_file_service_section_restart_config():
    """
    Test: [Service] section has proper restart and throttling directives.

    OPS-01: Restart=on-failure, RestartSec=10, StartLimitBurst=3,
            StartLimitIntervalSec=60
    """
    config = parse_service_file(SERVICE_FILE_PATH)

    assert "Service" in config, "[Service] section missing"
    service = config["Service"]

    # Restart must be on-failure (respects exit 0, auto-restarts on non-zero)
    assert "Restart" in service, "Restart directive missing"
    assert service["Restart"] == "on-failure", \
        f"Restart should be 'on-failure', got '{service['Restart']}'"

    # RestartSec must be 10 seconds
    assert "RestartSec" in service, "RestartSec missing"
    assert service["RestartSec"] == "10", \
        f"RestartSec should be 10, got '{service['RestartSec']}'"

    # StartLimitBurst = 3 (max 3 restarts before giving up)
    assert "StartLimitBurst" in service, "StartLimitBurst missing"
    assert service["StartLimitBurst"] == "3", \
        f"StartLimitBurst should be 3, got '{service['StartLimitBurst']}'"

    # StartLimitIntervalSec = 60 (within 60-second window)
    assert "StartLimitIntervalSec" in service, "StartLimitIntervalSec missing"
    assert service["StartLimitIntervalSec"] == "60", \
        f"StartLimitIntervalSec should be 60, got '{service['StartLimitIntervalSec']}'"


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

    # User must be j2h4u (not root)
    assert "User" in service, "User directive missing"
    assert service["User"] == "j2h4u", \
        f"User should be 'j2h4u', got '{service['User']}'"
    assert service["User"] != "root", "User should not be root"

    # Group must match
    assert "Group" in service, "Group directive missing"
    assert service["Group"] == "j2h4u", \
        f"Group should be 'j2h4u', got '{service['Group']}'"


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
    assert service["StandardOutput"] == "null", \
        f"StandardOutput should be 'null', got '{service['StandardOutput']}'"

    # StandardError must be journal
    assert "StandardError" in service, "StandardError missing"
    assert service["StandardError"] == "journal", \
        f"StandardError should be 'journal', got '{service['StandardError']}'"

    # SyslogIdentifier for searchability
    assert "SyslogIdentifier" in service, "SyslogIdentifier missing"
    assert service["SyslogIdentifier"] == "ups-battery-monitor", \
        f"SyslogIdentifier should be 'ups-battery-monitor', got '{service['SyslogIdentifier']}'"


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
    assert "multi-user.target" in install["WantedBy"], \
        f"WantedBy should include 'multi-user.target', got '{install['WantedBy']}'"
    assert install["WantedBy"] != "emergency.target", \
        "WantedBy should not be emergency.target"


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
    assert exec_start.startswith("/"), \
        f"ExecStart must use absolute path, got '{exec_start}'"

    # Should use /usr/bin/python3 (standard location)
    assert "/usr/bin/python3" in exec_start, \
        f"ExecStart should use /usr/bin/python3, got '{exec_start}'"

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

    # Must be absolute path
    assert work_dir.startswith("/"), \
        f"WorkingDirectory must be absolute, got '{work_dir}'"

    # Should be repo root (contains 'ups-battery-monitor')
    assert "ups-battery-monitor" in work_dir, \
        f"WorkingDirectory should reference repo, got '{work_dir}'"


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
    assert "PYTHONPATH" in env_var, \
        f"Environment should set PYTHONPATH, got '{env_var}'"

    # Must reference repo directory
    assert "ups-battery-monitor" in env_var, \
        f"PYTHONPATH should reference repo, got '{env_var}'"
