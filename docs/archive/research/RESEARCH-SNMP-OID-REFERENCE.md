# Enterprise UPS SNMP OID Reference (For Future Expansion)

If you ever upgrade to enterprise UPS or add SNMP monitoring, these are the OID values to track.

---

## APC Smart-UPS (PowerNet MIB v4.5.7)

| Metric | OID | Type | Notes |
|--------|-----|------|-------|
| **Battery Capacity (%)** | .1.3.6.1.4.1.318.1.1.1.2.2.1.0 | Integer | 0–100 |
| **Battery Replace Indicator** | .1.3.6.1.4.1.318.1.1.1.2.2.4.0 | String | "noBatteryNeedsReplacing" / "batteryNeedsReplacing" |
| **Battery Voltage (V)** | .1.3.6.1.2.1.33.1.2.51.3 / 1.3.6.1.4.1.318.1.1.1.2.2.51.3 | Integer | Divide by 10 for V (e.g., 244 = 24.4V) |
| **Battery Temperature (°C)** | .1.3.6.1.4.1.318.1.1.1.2.2.2.0 | Integer | Celsius |
| **Estimated Minutes Remaining** | .1.3.6.1.2.1.33.1.2.3.0 | Integer | Firmware estimate |
| **Battery Install Date** | Not exposed via standard SNMP | — | Accessible via SmartSlot API XML only |
| **Li-ion Cell Max Voltage** | .1.3.6.1.4.1.318.1.1.1.2.2.cell.max | Integer | Smart-UPS 3000+ with Li-ion only |
| **Li-ion Cell Min Voltage** | .1.3.6.1.4.1.318.1.1.1.2.2.cell.min | Integer | Smart-UPS 3000+ with Li-ion only |
| **Li-ion Cell Max Temperature** | .1.3.6.1.4.1.318.1.1.1.2.2.temp.max | Integer | Smart-UPS 3000+ with Li-ion only |

---

## Eaton Powerware (XUPS-MIB)

| Metric | OID | Type | Notes |
|--------|-----|------|-------|
| **Battery Voltage (V DC)** | .1.3.6.1.4.1.534.1.2.2 | Integer | Divide by 10 for V |
| **Battery Capacity (%)** | .1.3.6.1.4.1.534.1.2.4 | Integer | 0–100 |
| **Estimated Runtime (minutes)** | .1.3.6.1.2.1.33.1.2.3 | Integer | Firmware estimate |
| **Seconds on Battery** | .1.3.6.1.2.1.33.1.2.2 | Integer | Cumulative discharge time |
| **Battery Status** | 1.3.6.1.2.1.33.1.2.1 | Integer | 1=normal, 2=low, 3=depleted |
| **OL→OB Transfer Count** | .1.3.6.1.4.1.534.X.Y.Z | Integer | Custom OID per Network-M2 card (not documented publicly) |

---

## CyberPower (Via SNMP with RMCARD400 or newer USB models)

### Standard UPS-MIB (RFC 3621)
| Metric | OID | Type | Notes |
|--------|-----|------|-------|
| **Battery Voltage** | .1.3.6.1.2.1.33.1.2.5 | Integer | Divide by 10 for V |
| **Battery Capacity (%)** | .1.3.6.1.2.1.33.1.2.4 | Integer | 0–100 (unreliable) |
| **Estimated Runtime (sec)** | .1.3.6.1.2.1.33.1.2.3 | Integer | In seconds; divide by 60 for minutes |

### CyberPower-Specific (NUT PR #1982, v2.8.1+)
| Metric | Variable | Type | Notes |
|--------|----------|------|-------|
| **Battery Install Date** | `battery.date` | String | ISO 8601 format; e.g., "2021-06-15" |
| **UPS Temperature (°C)** | `ups.temperature` | Float | Internal UPS temp sensor |
| **UPS Serial** | `ups.id` | String | Device identifier |
| **Last Test Date** | `ups.test.date` | String | ISO 8601; e.g., "2026-03-12" |
| **Last Test Result** | `ups.test.result` | String | "ok" / "failed" / "failed_voltage" |
| **Transfer Reason** | `input.transfer.reason` | String | "input_voltage_out_of_range" / "input_frequency_out_of_range" / "automatic_test" |

---

## Vertiv/Emerson Liebert (LIEBERT-GP-FLEXIBLE-MIB)

| Metric | Variable | Type | Notes |
|--------|----------|------|-------|
| **Battery Temperature (°C)** | `VertivUPSTempIn` | Integer | Celsius |
| **Battery Time Remaining (min)** | `VertivUPSBatTimeRemain` | Integer | Minutes; thresholds: Warning 60, Critical 30 |
| **Battery Status** | `VertivUPSBatStatus` | String | "Normal", "Low", "Depleted" |
| **Charge Status** | `VertivUPSChargeStatus` | String | "Remained fully charged" / charging state |
| **Output Voltage (V)** | `VertivUPSOutputV` | Integer | Thresholds: Warning 130, Critical 108 |
| **Output Frequency (Hz)** | `VertivUPSOutputFreq` | Float | Should be 50 or 60 Hz |

---

## Schneider Electric / APC EcoStruxure IT

**Note:** EcoStruxure IT uses REST API, not SNMP OIDs directly.

### Available Sensors (via XML API, not SNMP)
| Metric | API Field | Notes |
|--------|-----------|-------|
| **Battery Age** | `battery.age.days` | Requires EcoStruxure Agent |
| **Battery Replacement Status** | `battery.replacement.status` | "ok" / "warning" / "critical" |
| **Cumulative Runtime** | `battery.runtime.cumulative.minutes` | If tracked |

**Limitations:** Community users (2025-03-05) requesting better API documentation; not all metrics are published.

---

## NUT Variable Naming Convention (RFC 9271)

Standard format: `domain.purpose.qualifier`

Examples:
- `battery.charge` — charge level (%)
- `battery.voltage.nominal` — rated voltage (V)
- `battery.runtime.low` — low-battery threshold (seconds)
- `ups.status` — UPS operational status (text string)
- `input.voltage` — incoming AC voltage (V)
- `output.voltage` — inverter output voltage (V)

**Key:** All variables are namespace-scoped. Check vendor MIB for custom extensions (e.g., CyberPower's `input.transfer.reason`).

---

## How to Query SNMP (Linux Example)

```bash
# Install tools
sudo apt-get install snmp snmp-mibs-downloader

# Query single OID (APC example: battery capacity)
snmpget -v2c -c public <ups-ip> 1.3.6.1.4.1.318.1.1.1.2.2.1.0

# Walk entire battery subtree (APC)
snmpwalk -v2c -c public <ups-ip> 1.3.6.1.4.1.318.1.1.1.2.2

# Walk entire UPS subtree (standard MIB)
snmpwalk -v2c -c public <ups-ip> 1.3.6.1.2.1.33

# Load custom MIB file
snmpget -v2c -c public -M +/path/to/mibs <ups-ip> UPS-MIB::upsIdentifier.0
```

---

## SNMP Community String / Authentication

**Default Community Strings (often unchanged):**
- APC: `public` (read-only)
- Eaton: `public` (read-only)
- CyberPower: `public` (read-only)
- Vertiv: Varies; check web interface

**SNMPv3 (secure, recommended for production):**
- Username + password + encryption
- Example: `snmpget -v3 -u username -A password -X privpassword <ups-ip> OID`

**Note:** Most budget UPS have no authentication; they expose all OIDs to anyone with network access.

---

## Testing SNMP Connectivity

```bash
# Check if SNMP is responding
snmpget -v2c -c public <ups-ip> sysUpTime.0

# If successful, shows: DISMAN-EXPRESSION-MIB::sysUpTime.0 = Timeticks: (12345) ...
# If fails, check: firewall, SNMP daemon enabled, community string
```

---

## Conversion Notes for SNMP Values

| Field | SNMP Type | Conversion |
|-------|-----------|-----------|
| Voltage | Integer | Divide by 10 (e.g., 244 = 24.4V) |
| Temperature | Integer | Direct (°C) |
| Capacity (%) | Integer | Direct (0–100) |
| Runtime | Integer | Seconds; divide by 60 for minutes |
| Frequency | Integer | Divide by 10 (e.g., 600 = 60Hz) |

---

## Future: Adding SNMP to CyberPower UT850EG

**Option 1: RMCARD400**
- Remote management card (~$500)
- Adds: SNMPv1/v2c/v3, Telnet, SSH, HTTP/HTTPS
- Requires: PS/2 or mini-USB connector on UPS
- Downside: Expensive for budget UPS

**Option 2: NUT + SNMP Exporter**
- NUT via USB (your current setup)
- Export NUT variables to Prometheus via `nut_exporter`
- No additional hardware needed
- Pros: Cheap, scriptable, works with existing NUT daemon

**Option 3: Keep Custom Estimation**
- Stick with Python + LUT model (cheapest)
- Focus on accuracy via calibration, not hardware upgrades
- Can always add SNMP later if budget allows

---

**Reference Generated:** 2026-03-14 | Updated for NUT v2.8.4+ and current manufacturer products
