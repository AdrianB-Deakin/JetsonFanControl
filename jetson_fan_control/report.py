"""
Summaries of the recorded temperature history, in the style of vnstat.

    jetson-fan-stats             # daily summary (last 30 days)
    jetson-fan-stats minute      # last 60 minutes
    jetson-fan-stats hour -n 48  # last 48 hours
    jetson-fan-stats month --csv

Periods are shown in local time. Minute rows only go back as far as the
service's retention setting; hour, day and month summaries cover the whole
history.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from typing import List, NamedTuple, Optional

from .config import Config
from .history import SCHEMA_VERSION

# period -> (source table, strftime format of the period label, default rows)
PERIODS = {
    "minute": ("minute", "%Y-%m-%d %H:%M", 60),
    "hour": ("hour", "%Y-%m-%d %H:00", 24),
    "day": ("hour", "%Y-%m-%d", 30),
    "month": ("hour", "%Y-%m", 12),
}

_QUERY = """
SELECT label, SUM(samples), SUM(temp_sum), MIN(temp_min), MAX(temp_max),
       SUM(fan_on_samples)
FROM (SELECT strftime(?, start, 'unixepoch', 'localtime') AS label, * FROM {table})
GROUP BY label
ORDER BY label DESC
LIMIT ?
"""


class Row(NamedTuple):
    period: str
    temp_min: float
    temp_avg: float
    temp_max: float
    fan_on_pct: float
    samples: int


def summarise(conn: sqlite3.Connection, period: str, limit: int) -> List[Row]:
    """Return up to `limit` of the most recent periods, oldest first."""
    table, label_format, _ = PERIODS[period]
    rows = [
        Row(
            period=label,
            temp_min=temp_min,
            temp_avg=temp_sum / samples,
            temp_max=temp_max,
            fan_on_pct=100.0 * fan_on_samples / samples,
            samples=samples,
        )
        for label, samples, temp_sum, temp_min, temp_max, fan_on_samples in conn.execute(
            _QUERY.format(table=table), (label_format, limit)
        )
    ]
    return rows[::-1]


def print_table(rows: List[Row], period: str) -> None:
    width = max([len(period)] + [len(row.period) for row in rows])
    header = f"{period:<{width}}  {'min':>6}  {'avg':>6}  {'max':>6}  {'fan on':>6}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.period:<{width}}  {row.temp_min:6.1f}  {row.temp_avg:6.1f}  "
            f"{row.temp_max:6.1f}  {row.fan_on_pct:5.0f}%"
        )
    print("(temperatures in deg C)")


def print_csv(rows: List[Row], period: str) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow([period, "temp_min", "temp_avg", "temp_max", "fan_on_pct", "samples"])
    for row in rows:
        writer.writerow(
            [
                row.period,
                f"{row.temp_min:.2f}",
                f"{row.temp_avg:.2f}",
                f"{row.temp_max:.2f}",
                f"{row.fan_on_pct:.1f}",
                row.samples,
            ]
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jetson-fan-stats",
        description="Show summaries of the fan controller's temperature history.",
    )
    parser.add_argument(
        "period", nargs="?", choices=list(PERIODS), default="day",
        help="summary period (default: day)",
    )
    parser.add_argument(
        "-n", "--limit", type=int,
        help="number of most recent periods to show "
             "(default: 60 minutes, 24 hours, 30 days, 12 months)",
    )
    parser.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    parser.add_argument(
        "--db", default=None,
        help="history database (default: FAN_CONTROL_HISTORY_DB or the service's)",
    )
    args = parser.parse_args(argv)

    db_path = args.db or Config.history_db
    if not db_path:
        parser.error("temperature history is disabled (FAN_CONTROL_HISTORY_DB is empty)")
    limit = args.limit if args.limit is not None else PERIODS[args.period][2]

    # Read-only, so it works without root and can never disturb the service.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            print(
                f"{db_path} is history format version {version}; this command "
                f"reads version {SCHEMA_VERSION}. Use the matching version of "
                f"jetson-fan-stats to read it.",
                file=sys.stderr,
            )
            return 1
        rows = summarise(conn, args.period, limit)
    except sqlite3.Error as exc:
        print(f"Cannot read temperature history at {db_path}: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No temperature history recorded yet.", file=sys.stderr)
        return 0
    (print_csv if args.csv else print_table)(rows, args.period)
    return 0


if __name__ == "__main__":
    sys.exit(main())
