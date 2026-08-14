# Controlled Capacity-Test Protocol

**Status:** Written/supervised procedure only. This document is not an automation interface.
**Approval:** The operator must explicitly approve the specific hardware test immediately before
execution. A scheduler suggestion, a quick self-test, or a natural blackout is not approval.

## Safety boundary

This project MUST NOT execute or recommend an automatic hardware deep test. Do not add a timer,
daemon action, installer action, or test fixture that sends a real deep-test command. If any
precondition, endpoint observer, abort path, or recharge step below is unavailable, do not run the
test.

## Preconditions

1. Durable discharge capture is deployed and its journal is healthy. Verify the active process,
   model directory, journal mode (`0700` directory and `0600` journal), health endpoint, and a
   recent virtual-UPS read.
2. UPS is online, fully charged, and has completed the manufacturer-appropriate post-charge rest.
   Record the charge value, start time, ambient temperature, and rest interval.
3. Establish the intended load. Record both the load percentage and an independently measured or
   otherwise documented battery/load current. `ups.load` alone is not a coulomb measurement.
4. Record ambient/battery temperature and the limitation if no UPS temperature sensor exists.
5. Confirm the configured safe-shutdown threshold and the host's ability to shut down without
   losing the independent endpoint observation.
6. Enumerate the actual device commands without sending one:

   ```bash
   upscmd -l cyberpower
   ```

   Confirm that the intended start command is present and that `test.battery.stop` is present and
   has documented, tested behaviour for this exact UPS/driver. Do not assume another CyberPower
   model or NUT driver has the same command set.
7. Define an abort owner, an abort deadline, and a communication path. The abort must be possible
   without relying on the monitor's process, Grafana, DNS, or WAN.
8. Define the endpoint before starting: voltage/current/time threshold, UPS low-battery threshold,
   or another manufacturer-supported endpoint. A host shutdown is not evidence of what happened
   after the host stopped observing.

## Virtual rehearsal

Before any real hardware command, rehearse the complete sequence against a virtual/fake NUT
source: start classification, sample capture, endpoint crossing, abort request, signal/restart,
journal replay, low-battery publication, and post-event recharge bookkeeping. Verify that a
persistence error does not suppress LB or the `upsmon` shutdown path. The rehearsal must not issue
any command to the physical UPS.

## Supervised execution record

If and only if the operator gives explicit approval after the checks above, record:

- approval time and operator/independent observer;
- UPS identity, driver version, NUT version, command list, and selected command;
- full-charge/rest evidence, load/current, temperature, start voltage, and start time;
- endpoint and abort thresholds, observer location, and communications path;
- every command outcome and every observed sample gap;
- the exact stop/abort action and its result;
- host state and whether the endpoint remained independently observed through shutdown.

Do not place credentials in the record. Do not pass tokens or secrets on command lines.

## Abort and post-test recovery

Abort on the pre-defined voltage/current/temperature limit, unexpected load change, telemetry
loss, communication loss, UPS alarm, endpoint ambiguity, or any threat to host safety. Use the
verified `test.battery.stop` path only when the pre-check established that it works for this
device; otherwise follow the vendor's documented safe action and stop the protocol.

After the endpoint/abort:

1. Confirm the UPS is online and charging.
2. Maintain the required recharge period and post-recharge rest before interpreting results.
3. Verify the host, NUT, monitor, journal, and health endpoint independently.
4. Preserve the raw journal, observer notes, NUT output, timestamps, and checksums.
5. Classify the event. A gap, unknown endpoint, missing current/temperature context, or incomplete
   observation makes it operational evidence only.

## Evidence gate

Only a complete observation from a known online baseline through a defined endpoint, with full
charge/rest, documented load/current, temperature context, abort/recovery record, and independent
endpoint observation may be classified `controlled_capacity_test`. Even then, capacity/SoH claims
must stay within the protocol's measured limits; Peukert requires sufficient load/rate evidence.
No partial or recovered event may be imported as authoritative capacity, SoH, Peukert, or LUT
evidence without a separately reviewed approval.
