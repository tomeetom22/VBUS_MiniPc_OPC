#!/usr/bin/env python3
"""Plot PM time series from the CSV files produced by opc_n3_logger.py.

Examples:
    python plot_opc_data.py
    python plot_opc_data.py --input data --output plots/pm.png --show
    python plot_opc_data.py --sensor opc_n3_2
    python plot_opc_data.py --no-hawthorne
    python plot_opc_data.py --start 2026-10-27
"""

from __future__ import annotations

import argparse
import csv
import math
import ssl
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo


# One panel is made for each requested regulatory PM metric.  Sensors are
# overlaid within each panel so their readings can be compared directly.
PM_COLUMNS = (("PM1", "pm1_ug_m3"), ("PM2.5", "pm2_5_ug_m3"), ("PM10", "pm10_ug_m3"))
HAWTHORNE_LABEL = "Hawthorne (Utah DAQ)"
HAWTHORNE_URL = "https://air.utah.gov/csvFeed.php?id=slc"
DEFAULT_TALL_TOWER_INPUT = Path("/Users/tomw/Documents/VBUS/LEMSv3/Data/FleetBlock_Data/TallTower/AQ_TT")
TALL_TOWER_LABEL_PREFIX = "Tall Tower — "
LOCAL_TIMEZONE = ZoneInfo("America/Denver")
# Hawthorne/Salt Lake City; used only for the daylight-background calculation.
LOCAL_LATITUDE, LOCAL_LONGITUDE = 40.7608, -111.8910


def read_measurements(input_dir: Path, requested_sensor: str | None = None, label_prefix: str = "") -> dict[str, list[tuple[datetime, dict[str, float]]]]:
    measurements: dict[str, list[tuple[datetime, dict[str, float]]]] = defaultdict(list)
    for path in sorted(input_dir.glob("opc_n3_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                sensor = row.get("sensor_id", "")
                if row.get("record_status") != "ok" or not sensor or (requested_sensor and sensor != requested_sensor):
                    continue
                try:
                    timestamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
                    pm = {label: float(row[column]) for label, column in PM_COLUMNS if row.get(column) not in (None, "")}
                except (KeyError, ValueError):
                    continue
                if pm:
                    measurements[f"{label_prefix}{sensor}"].append((timestamp, pm))
    return measurements


def read_hawthorne_measurements(url: str = HAWTHORNE_URL) -> list[tuple[datetime, dict[str, float]]]:
    """Read Utah DAQ's hourly Hawthorne PM2.5/PM10 measurements in UTC."""
    # certifi supplies a current CA bundle when a locally installed Python has
    # no usable system certificate store (a common macOS Python.org setup).
    import certifi
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(url, timeout=15, context=ssl_context) as response:
        text = response.read().decode("utf-8-sig")
    rows = csv.DictReader(text.splitlines(), delimiter="\t")
    measurements = []
    mountain_time = ZoneInfo("America/Denver")
    for row in rows:
        try:
            # Utah DAQ timestamps are Mountain local time. The feed has used
            # month/day/year historically; accept day/month/year defensively.
            raw_timestamp = row["dtstamp"].strip()
            try:
                timestamp = datetime.strptime(raw_timestamp, "%m/%d/%Y %H:%M:%S")
            except ValueError:
                timestamp = datetime.strptime(raw_timestamp, "%d/%m/%Y %H:%M:%S")
            pm = {
                label: float(row[column])
                for label, column in (("PM2.5", "pm25"), ("PM10", "pm10"))
                if row.get(column, "").strip() not in ("", "-9999")
            }
        except (KeyError, ValueError):
            continue
        if pm:
            measurements.append((timestamp.replace(tzinfo=mountain_time).astimezone(ZoneInfo("UTC")), pm))
    if not measurements:
        raise ValueError("No Hawthorne PM2.5 or PM10 measurements were available from Utah DAQ.")
    return measurements


def measurement_time_range(measurements: dict[str, list[tuple[datetime, dict[str, float]]]]) -> tuple[datetime, datetime]:
    timestamps = [timestamp for rows in measurements.values() for timestamp, _ in rows]
    if not timestamps:
        raise ValueError("No successful PM measurements found.")
    return min(timestamps), max(timestamps)


def parse_utc_start(value: str) -> datetime:
    """Parse an ISO-8601 date/time, treating a date or naive time as UTC."""
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "start must be an ISO date/time, for example 2026-10-27 or 2026-10-27T12:00:00Z"
        ) from exc
    return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)


def average_measurements(measurements: dict[str, list[tuple[datetime, dict[str, float]]]], minutes: int) -> dict[str, list[tuple[datetime, dict[str, float]]]]:
    """Return per-sensor, per-metric means in UTC-aligned time bins."""
    if minutes < 1:
        raise ValueError("averaging time must be at least one minute")
    interval_seconds = minutes * 60
    averaged: dict[str, list[tuple[datetime, dict[str, float]]]] = {}
    for sensor, rows in measurements.items():
        buckets: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for timestamp, pm in rows:
            bin_start = int(timestamp.astimezone(timezone.utc).timestamp() // interval_seconds * interval_seconds)
            for label, value in pm.items():
                if math.isfinite(value):
                    buckets[bin_start][label].append(value)
        averaged[sensor] = [
            (
                datetime.fromtimestamp(bin_start + interval_seconds / 2, tz=timezone.utc),
                {label: sum(values) / len(values) for label, values in values_by_label.items() if values},
            )
            for bin_start, values_by_label in sorted(buckets.items())
        ]
    return averaged


def solar_event_utc(local_date: date, sunrise: bool) -> datetime | None:
    """Calculate local sunrise or sunset using the standard NOAA approximation."""
    day_number = local_date.timetuple().tm_yday
    longitude_hour = LOCAL_LONGITUDE / 15
    approximate_time = day_number + ((6 if sunrise else 18) - longitude_hour) / 24
    mean_anomaly = 0.9856 * approximate_time - 3.289
    true_longitude = (mean_anomaly + 1.916 * math.sin(math.radians(mean_anomaly))
                      + 0.020 * math.sin(math.radians(2 * mean_anomaly)) + 282.634) % 360
    right_ascension = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_longitude)))) % 360
    right_ascension += (math.floor(true_longitude / 90) - math.floor(right_ascension / 90)) * 90
    right_ascension /= 15
    sin_declination = 0.39782 * math.sin(math.radians(true_longitude))
    cos_declination = math.cos(math.asin(sin_declination))
    cos_hour_angle = ((math.cos(math.radians(90.833)) - sin_declination * math.sin(math.radians(LOCAL_LATITUDE)))
                      / (cos_declination * math.cos(math.radians(LOCAL_LATITUDE))))
    if not -1 <= cos_hour_angle <= 1:
        return None
    hour_angle = math.degrees(math.acos(cos_hour_angle))
    if sunrise:
        hour_angle = 360 - hour_angle
    universal_time = ((hour_angle / 15 + right_ascension - 0.06571 * approximate_time - 6.622) - longitude_hour) % 24
    event = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=universal_time)
    # Sunset may fall after midnight UTC. Shift to the requested local date.
    while event.astimezone(LOCAL_TIMEZONE).date() < local_date:
        event += timedelta(days=1)
    while event.astimezone(LOCAL_TIMEZONE).date() > local_date:
        event -= timedelta(days=1)
    return event


def shade_local_nights(axes: object, start: datetime, end: datetime) -> None:
    """Shade local nighttime on every plot axis between start and end UTC."""
    local_day = start.astimezone(LOCAL_TIMEZONE).date()
    last_day = end.astimezone(LOCAL_TIMEZONE).date()
    while local_day <= last_day:
        midnight = datetime.combine(local_day, datetime.min.time(), tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        next_midnight = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        sunrise, sunset = solar_event_utc(local_day, True), solar_event_utc(local_day, False)
        night_ranges = [(midnight, sunrise), (sunset, next_midnight)]
        for night_start, night_end in night_ranges:
            if night_start is not None and night_end is not None and night_start < end and night_end > start:
                for axis in axes:
                    axis.axvspan(max(start, night_start), min(end, night_end), color="#34495e", alpha=0.10, zorder=0)
        local_day += timedelta(days=1)


def plot(measurements: dict[str, list[tuple[datetime, dict[str, float]]]], output: Path, show: bool) -> None:
    if not measurements:
        raise ValueError("No successful PM measurements found.")
    import matplotlib
    if not show:
        # File creation must also work on a Mini PC without a desktop session.
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    # Hawthorne data is filtered to this same interval before plotting, but use
    # the combined local-OPC interval explicitly so it can never expand the
    # horizontal range.
    opc_measurements = {sensor: rows for sensor, rows in measurements.items() if sensor != HAWTHORNE_LABEL}
    start, end = measurement_time_range(opc_measurements)
    figure, axes = plt.subplots(len(PM_COLUMNS), 1, figsize=(13, 10), sharex=True, squeeze=False)
    shade_local_nights(axes[:, 0], start, end)
    for axis, (label, _column) in zip(axes[:, 0], PM_COLUMNS):
        for sensor, rows in sorted(measurements.items()):
            timestamps = [timestamp for timestamp, _ in rows]
            values = [pm.get(label, float("nan")) for _, pm in rows]
            if any(value == value for value in values):  # at least one non-NaN value
                style = {"linestyle": "--", "linewidth": 1.2} if sensor == HAWTHORNE_LABEL else {"linewidth": 0.8}
                axis.plot(timestamps, values, label=sensor, **style)
        axis.set_title(label)
        axis.set_ylabel(f"{label} (µg/m³)")
        axis.grid(True, alpha=0.3)
        axis.legend(title="Sensor")
        if start != end:
            axis.set_xlim(start, end)
    axes[-1, 0].set_xlabel("Timestamp (UTC)")
    axes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d\n%H:%M", tz=start.tzinfo))
    figure.suptitle("OPC-N3 particulate-matter concentration")
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, bbox_inches="tight")
    print(f"Saved {output}")
    if show:
        plt.show()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default="/Users/tomw/Documents/VBUS/LEMSv3/Data/FleetBlock_Data/PopUp/AQ", help="Directory containing opc_n3_YYYY-MM-DD.csv files.")
    parser.add_argument("--tall-tower-input", type=Path, default=DEFAULT_TALL_TOWER_INPUT, metavar="DIR", help="Tall Tower OPC CSV directory to overlay (default: %(default)s).")
    parser.add_argument("--no-tall-tower", action="store_true", help="Do not overlay Tall Tower OPC data.")
    parser.add_argument("--output", type=Path, default=Path("plots/opc_pm.png"), help="PNG file to create.")
    parser.add_argument("--sensor", metavar="SENSOR_ID", help="Plot only this sensor_id (for example, --sensor opc_n3_1).")
    parser.add_argument("--start", type=parse_utc_start, metavar="ISO_DATE", help="Plot data at or after this UTC date/time (for example, 2026-10-27).")
    parser.add_argument("--no-hawthorne", action="store_true", help="Do not overlay hourly PM2.5 and PM10 from Utah DAQ's Hawthorne site.")
    parser.add_argument("--average-minutes", type=int, default=30, metavar="MINUTES", help="Time-bin mean for each series (default: 30).")
    parser.add_argument("--show", action="store_true", help="Also open the figure window after saving.")
    args = parser.parse_args()
    try:
        measurements = read_measurements(args.input, args.sensor)
        if not args.no_tall_tower:
            # Keep site labels distinct even though both loggers use the same
            # opc_n3_1/opc_n3_2 sensor IDs.  Do not clip the Tower archive to
            # the PopUp archive: a requested later range may exist only at the
            # Tower site.
            measurements.update(read_measurements(args.tall_tower_input, args.sensor, TALL_TOWER_LABEL_PREFIX))
        if args.start:
            measurements = {
                sensor: [(timestamp, pm) for timestamp, pm in rows if timestamp >= args.start]
                for sensor, rows in measurements.items()
            }
            measurements = {sensor: rows for sensor, rows in measurements.items() if rows}
        start, end = measurement_time_range(measurements)
        if not args.no_hawthorne:
            try:
                hawthorne = [(timestamp, pm) for timestamp, pm in read_hawthorne_measurements() if start <= timestamp <= end]
                if hawthorne:
                    measurements[HAWTHORNE_LABEL] = hawthorne
                else:
                    print("Warning: no Hawthorne measurements overlap the primary input time range.")
            except (OSError, ValueError) as exc:
                # A network or feed outage should not prevent plotting local
                # OPC data; make the missing comparison explicit instead.
                print(f"Warning: could not load Hawthorne comparison data: {exc}")
        plot(average_measurements(measurements, args.average_minutes), args.output, args.show)
    except (OSError, ValueError, ModuleNotFoundError) as exc:
        print(f"Error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
