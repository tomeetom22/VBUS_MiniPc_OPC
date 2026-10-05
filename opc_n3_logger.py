#!/usr/bin/env python3
"""Log one or more Alphasense OPC-N3s through USB--SPI adapters."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# The N3 produces 24 size bins. Keep a fixed CSV schema even when a poll fails.
BIN_COLUMNS = [f"bin_{n:02d}_count" for n in range(24)]
# Map the names returned by py-opc-ng to stable, CSV-friendly column names.
DIAGNOSTIC_FIELDS = {
    "Bin1 MToF": "bin1_mtof", "Bin3 MToF": "bin3_mtof", "Bin5 MToF": "bin5_mtof", "Bin7 MToF": "bin7_mtof",
    "Sampling Period": "sampling_period_s", "SFR": "sample_flow_rate", "Temperature": "temperature_c",
    "Relative humidity": "relative_humidity_percent", "PM1": "pm1_ug_m3", "PM2.5": "pm2_5_ug_m3",
    "PM4.25": "pm4_25_ug_m3", "PM10": "pm10_ug_m3", "#RejectGlitch": "reject_glitch_count",
    "#RejectLongTOF": "reject_long_tof_count", "#RejectRatio": "reject_ratio_count",
    "#RejectOutOfRange": "reject_out_of_range_count", "Fan rev count": "fan_revolution_count",
    "Laser status": "laser_status", "Checksum": "checksum",
}
# One row per sensor per polling cycle; failed polls are retained for QA/auditability.
CSV_COLUMNS = ["timestamp_utc", "sensor_id", "port", "record_status", "consecutive_failures", "recovery_count", "error"] + BIN_COLUMNS + list(DIAGNOSTIC_FIELDS.values())


@dataclass(frozen=True)
class SensorConfig:
    # sensor_id distinguishes rows when two or more OPCs share one CSV file.
    sensor_id: str
    # Windows uses COMx.
    port: str
    # Optional, permanent identity of the USB-ISS bridge (eight-byte hex serial).
    # If supplied, the logger finds its current COM port automatically.
    adapter_serial: str | None = None
    spi_speed_hz: int = 500_000
    warmup_seconds: float = 3.0
    serial_read_timeout_seconds: float = 1.0
    serial_write_timeout_seconds: float = 2.0


class OPCN3Logger:
    def __init__(self, config: SensorConfig, restart_after_failures: int, device_factory: Callable[[SensorConfig], Any], sleep: Callable[[float], None] = time.sleep) -> None:
        self.config, self.restart_after_failures = config, restart_after_failures
        self._device_factory, self._sleep, self.device = device_factory, sleep, None
        self.active_port = config.port
        # Failure state is kept independently for each configured sensor.
        self.consecutive_failures = self.last_poll_consecutive_failures = self.recovery_count = 0

    def connect(self) -> None:
        # Construct a fresh USB--SPI connection, then enable N3 laser and fan.
        self.device = self._device_factory(self.config)
        # A serial-matched bridge can have a different COM number than config.port.
        self.active_port = getattr(self.device, "_logger_port", self.config.port)
        self.device.on()
        # Let the fan settle before requesting a measurement.
        self._sleep(self.config.warmup_seconds)

    def close(self) -> None:
        if self.device is not None:
            try:
                self.device.off()
            except Exception:
                logging.exception("Could not turn off %s", self.config.sensor_id)
        # A later poll will create a fresh connection instead of reusing this one.
        self.device = None

    def _recover(self) -> None:
        # Software recovery: fan/laser off, short pause, new connection, fan/laser on.
        # This is triggered only after restart_after_failures failed polls.
        logging.warning("Recovering %s after %d failed polls", self.config.sensor_id, self.consecutive_failures)
        self.close()
        self._sleep(1)
        self.connect()
        # Count only completed recovery sequences. The caller resets the poll
        # window even if connect() above raises an exception.
        self.recovery_count += 1

    def poll(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            if self.device is None:
                self.connect()
            # histogram() requests all bins, PM values, and N3 diagnostics in one response.
            reading = self.device.histogram()
            if not isinstance(reading, dict) or not reading:
                raise RuntimeError("empty or malformed histogram response")
            # Any good reading clears the consecutive-failure counter.
            self.consecutive_failures = self.last_poll_consecutive_failures = 0
            return reading, None
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_poll_consecutive_failures = self.consecutive_failures
            error = f"{type(exc).__name__}: {exc}"
            logging.exception("Poll failed for %s", self.config.sensor_id)
            # Default threshold is N=3; set restart_after_failures in the JSON config.
            if self.consecutive_failures >= self.restart_after_failures:
                try:
                    self._recover()
                except Exception as recovery_exc:
                    error += f"; recovery failed: {type(recovery_exc).__name__}: {recovery_exc}"
                    logging.exception("Recovery failed for %s", self.config.sensor_id)
                finally:
                    # Avoid recovery thrashing: a failed recovery also starts a
                    # fresh window, so another recovery needs N more bad polls.
                    # last_poll_consecutive_failures remains N for this CSV row.
                    self.consecutive_failures = 0
            return None, error


def create_opcn3(config: SensorConfig) -> Any:
    try:
        from usbiss.spi import SPI
        import opcng
    except ImportError as exc:
        raise RuntimeError("Install dependencies: python -m pip install -r requirements.txt") from exc
    # pyusbiss converts COM-port traffic to the SPI Mode 1 N3 interface.
    port = resolve_adapter_port(config)
    spi = SPI(port)
    # pyusbiss defaults its read timeout to one second but leaves writes
    # unbounded.  A USB/COM fault on Windows can otherwise block forever
    # before OPCN3Logger can count the failed poll and recover the N3.
    serial_port = spi._usbiss.serial
    serial_port.timeout = config.serial_read_timeout_seconds
    serial_port.write_timeout = config.serial_write_timeout_seconds
    # Alphasense OPC-N3: SPI Mode 1, 500 kHz by default, MSB first.
    spi.mode, spi.max_speed_hz, spi.lsbfirst = 1, config.spi_speed_hz, False
    device = opcng.OPCN3(spi)
    # Preserve the actual port for the CSV when automatic COM-port resolution is used.
    device._logger_port = port
    return device


def adapter_serial_text(raw_serial: Any) -> str:
    """Return the USB-ISS eight-byte serial in the stable config-file format."""
    if isinstance(raw_serial, (bytes, bytearray)):
        return bytes(raw_serial).hex().upper()
    return str(raw_serial).strip().upper()


def read_adapter_serial(port: str) -> str:
    """Open one USB-ISS bridge briefly and query its hardware serial number."""
    from usbiss.usbiss import USBISS

    bridge = USBISS(port)
    try:
        return adapter_serial_text(bridge.iss_sn)
    finally:
        bridge.close()


def available_serial_ports() -> list[str]:
    """Return currently available COM/serial ports without assuming their names."""
    from serial.tools import list_ports

    return [item.device for item in list_ports.comports()]


def resolve_adapter_port(config: SensorConfig) -> str:
    """Use configured port normally; use the physical bridge serial when supplied."""
    if not config.adapter_serial:
        return config.port
    expected = config.adapter_serial.strip().upper()
    # Check the prior COM number first, then scan other currently available ports.
    candidates = [config.port] + [port for port in available_serial_ports() if port != config.port]
    for port in candidates:
        try:
            if read_adapter_serial(port) == expected:
                if port != config.port:
                    logging.warning("%s moved from %s to %s", config.sensor_id, config.port, port)
                return port
        except Exception:
            # Non-USB-ISS ports and unplugged ports are not this configured adapter.
            continue
    raise RuntimeError(f"Could not find USB-ISS adapter serial {expected} for {config.sensor_id}")


def list_adapters() -> int:
    """Print physical USB-ISS serials so they can be tied to sensor locations."""
    found = 0
    for port in available_serial_ports():
        try:
            print(f"{port}  adapter_serial={read_adapter_serial(port)}")
            found += 1
        except Exception:
            continue
    if not found:
        print("No USB-ISS adapters found. Check power, USB cable, and COM drivers.")
        return 2
    return 0


def utc_timestamp() -> str:
    # UTC avoids daylight-saving ambiguity and is retained verbatim in every CSV row.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_value(value: Any) -> Any:
    # Empty cells are easier for CSV/HPC tools to handle than NaN or Infinity strings.
    return "" if isinstance(value, float) and not math.isfinite(value) else value


def normalize_reading(sensor: OPCN3Logger, reading: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    # Pre-fill every column so failed rows have the same schema as successful rows.
    row = {column: "" for column in CSV_COLUMNS}
    row.update(timestamp_utc=utc_timestamp(), sensor_id=sensor.config.sensor_id, port=sensor.active_port,
               record_status="ok" if reading is not None else "poll_failed",
               consecutive_failures=sensor.last_poll_consecutive_failures, recovery_count=sensor.recovery_count, error=error or "")
    if reading:
        # The N3 histogram uses "Bin 0" through "Bin 23" as its source names.
        for i, column in enumerate(BIN_COLUMNS): row[column] = safe_value(reading.get(f"Bin {i}"))
        for source, column in DIAGNOSTIC_FIELDS.items(): row[column] = safe_value(reading.get(source))
    return row


def append_rows(output_dir: Path, rows: Iterable[dict[str, Any]]) -> Path | None:
    rows = list(rows)
    if not rows: return None
    output_dir.mkdir(parents=True, exist_ok=True)
    # Rotate output daily, while letting all configured sensors share the same file.
    path = output_dir / f"opc_n3_{rows[0]['timestamp_utc'][:10]}.csv"
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        # Write the header only for a new/empty daily file.
        if handle.tell() == 0: writer.writeheader()
        writer.writerows(rows)
    return path


def console_summary(row: dict[str, Any]) -> str:
    """A concise live display; the CSV retains the full histogram and diagnostics."""
    prefix = f"{row['timestamp_utc']}  {row['sensor_id']}"
    if row["record_status"] != "ok":
        return f"{prefix}  poll failed ({row['error']})"
    # Keep the terminal concise; the CSV still contains bins and diagnostics.
    pm_fields = [("PM1", "pm1_ug_m3"), ("PM2.5", "pm2_5_ug_m3"), ("PM4.25", "pm4_25_ug_m3"), ("PM10", "pm10_ug_m3")]
    values = [f"{label}={float(row[column]):.2f} µg/m³" for label, column in pm_fields if row[column] not in ("", None)]
    return f"{prefix}  " + "  ".join(values)


def load_config(path: Path) -> tuple[list[SensorConfig], float, int, Path]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sensors = [SensorConfig(**item) for item in data["sensors"]]
    if not sensors or len({sensor.sensor_id for sensor in sensors}) != len(sensors):
        raise ValueError("config needs sensors with unique sensor_id values")
    # N=3 by default: after three back-to-back bad polls, attempt N3 recovery.
    interval, failures = float(data.get("poll_interval_seconds", 5)), int(data.get("restart_after_failures", 3))
    if interval < 2.5 or failures < 1: raise ValueError("poll interval must be at least 2.5; failures at least 1")
    return sensors, interval, failures, Path(data.get("output_dir", "data"))


def run(sensors: list[OPCN3Logger], interval: float, output_dir: Path, once: bool = False) -> None:
    stopping = False
    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
    # Ctrl+C/SIGTERM requests a clean stop: finish this cycle and turn peripherals off.
    signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            started = time.monotonic()
            # Poll sensors serially. Each becomes an independent row in the daily CSV.
            rows = [normalize_reading(sensor, *sensor.poll()) for sensor in sensors]
            append_rows(output_dir, rows)
            for row in rows:
                print(console_summary(row), flush=True)
            if once: break
            # Maintain the requested cadence after accounting for communication time.
            time.sleep(max(0, interval - (time.monotonic() - started)))
    finally:
        # Best-effort N3 shutdown on normal exit or Ctrl+C.
        for sensor in sensors: sensor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("opc_n3_config.json"))
    parser.add_argument("--list-adapters", action="store_true", help="List USB-ISS COM ports and permanent adapter serials, then exit.")
    parser.add_argument("--once", action="store_true"); parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.list_adapters:
        try:
            return list_adapters()
        except ImportError as exc:
            logging.error("Install dependencies: python -m pip install -r requirements.txt")
            return 2
    try:
        configs, interval, failures, output_dir = load_config(args.config)
        run([OPCN3Logger(item, failures, create_opcn3) for item in configs], interval, output_dir, args.once)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        logging.error("%s", exc); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
