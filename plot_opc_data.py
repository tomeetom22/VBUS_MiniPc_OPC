#!/usr/bin/env python3
"""Plot PM time series from the CSV files produced by opc_n3_logger.py.

Examples:
    python plot_opc_data.py
    python plot_opc_data.py --input data --output plots/pm.png --show
    python plot_opc_data.py --sensor opc_n3_2
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path


PM_COLUMNS = (("PM1", "pm1_ug_m3"), ("PM2.5", "pm2_5_ug_m3"), ("PM4.25", "pm4_25_ug_m3"), ("PM10", "pm10_ug_m3"))


def read_measurements(input_dir: Path, requested_sensor: str | None = None) -> dict[str, list[tuple[datetime, dict[str, float]]]]:
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
                    measurements[sensor].append((timestamp, pm))
    return measurements


def plot(measurements: dict[str, list[tuple[datetime, dict[str, float]]]], output: Path, show: bool) -> None:
    if not measurements:
        raise ValueError("No successful PM measurements found.")
    import matplotlib
    if not show:
        # File creation must also work on a Mini PC without a desktop session.
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    figure, axes = plt.subplots(len(measurements), 1, figsize=(13, 4 * len(measurements)), sharex=True, squeeze=False)
    for axis, (sensor, rows) in zip(axes[:, 0], sorted(measurements.items())):
        timestamps = [timestamp for timestamp, _ in rows]
        for label, _column in PM_COLUMNS:
            values = [pm.get(label, float("nan")) for _, pm in rows]
            if any(value == value for value in values):  # at least one non-NaN value
                axis.plot(timestamps, values, label=label, linewidth=0.8)
        axis.set_title(sensor)
        axis.set_ylabel("PM (µg/m³)")
        axis.grid(True, alpha=0.3)
        axis.legend(ncol=4)
    axes[-1, 0].set_xlabel("Timestamp (UTC)")
    axes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d\n%H:%M", tz=measurements[next(iter(measurements))][0][0].tzinfo))
    figure.suptitle("OPC-N3 particulate-matter concentration")
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160, bbox_inches="tight")
    print(f"Saved {output}")
    if show:
        plt.show()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data"), help="Directory containing opc_n3_YYYY-MM-DD.csv files.")
    parser.add_argument("--output", type=Path, default=Path("plots/opc_pm.png"), help="PNG file to create.")
    parser.add_argument("--sensor", help="Plot only this sensor_id.")
    parser.add_argument("--show", action="store_true", help="Also open the figure window after saving.")
    args = parser.parse_args()
    try:
        plot(read_measurements(args.input, args.sensor), args.output, args.show)
    except (OSError, ValueError, ModuleNotFoundError) as exc:
        print(f"Error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
