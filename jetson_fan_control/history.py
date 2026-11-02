"""
Temperature history, kept in a small SQLite database.

Every reading the controller takes is folded into a running summary of the
current minute (sample count, sum, min, max and how many samples had the fan
on). When the minute ends, that summary is added to two tables:

- minute: one row per minute, pruned after `minute_retention_days`;
- hour: one row per hour, kept forever (under 9,000 rows a year).

Daily and monthly figures are derived from the hour table at report time
(see report.py), so nothing else needs storing. Storing sums rather than
averages lets rows be merged exactly, e.g. when the service restarts part
way through a minute.

Recording is best effort: a database error is logged and never stops the
fan control loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TABLES = ("minute", "hour")
_BUCKET_SECONDS = {"minute": 60, "hour": 3600}

# Stored in the database's `user_version` header field. Bump it whenever the
# tables change shape: a database with a different version is moved aside
# (never deleted) and a fresh one started, so an updated service never
# misreads or corrupts old data. An update that keeps the format keeps the
# history.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    start INTEGER PRIMARY KEY,  -- UTC epoch seconds at the start of the period
    samples INTEGER NOT NULL,
    temp_sum REAL NOT NULL,
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    fan_on_samples INTEGER NOT NULL
)
"""

_UPSERT = """
INSERT INTO {table} (start, samples, temp_sum, temp_min, temp_max, fan_on_samples)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(start) DO UPDATE SET
    samples = samples + excluded.samples,
    temp_sum = temp_sum + excluded.temp_sum,
    temp_min = min(temp_min, excluded.temp_min),
    temp_max = max(temp_max, excluded.temp_max),
    fan_on_samples = fan_on_samples + excluded.fan_on_samples
"""


class _Bucket:
    """Running summary of the readings within one minute."""

    def __init__(self, start: int):
        self.start = start
        self.samples = 0
        self.temp_sum = 0.0
        self.temp_min = float("inf")
        self.temp_max = float("-inf")
        self.fan_on_samples = 0

    def add(self, temp_c: float, fan_on: bool) -> None:
        self.samples += 1
        self.temp_sum += temp_c
        self.temp_min = min(self.temp_min, temp_c)
        self.temp_max = max(self.temp_max, temp_c)
        self.fan_on_samples += int(fan_on)


class History:
    def __init__(self, db_path: str, minute_retention_days: float):
        self._db_path = Path(db_path)
        self._minute_retention_sec = minute_retention_days * 86400
        self._conn: Optional[sqlite3.Connection] = None
        self._bucket: Optional[_Bucket] = None

    def open(self) -> None:
        """Open (creating if needed) the database. Raises on failure."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        if self._db_path.exists():
            version = schema_version(self._db_path)
            if version != SCHEMA_VERSION:
                self._set_aside(version)
        self._conn = sqlite3.connect(str(self._db_path), timeout=10)
        with self._conn:
            for table in TABLES:
                self._conn.execute(_SCHEMA.format(table=table))
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("Recording temperature history to %s", self._db_path)

    def _set_aside(self, version: int) -> None:
        """Rename an old-format database out of the way, keeping its data."""
        backup = self._db_path.with_name(f"{self._db_path.name}.v{version}")
        if backup.exists():
            backup = backup.with_name(f"{backup.name}.{int(time.time())}")
        self._db_path.rename(backup)
        logger.warning(
            "Temperature history %s is format version %d, this version uses %d: "
            "kept it as %s and started a new one",
            self._db_path, version, SCHEMA_VERSION, backup,
        )

    def record(self, temp_c: float, fan_on: bool, now: Optional[float] = None) -> None:
        """Add one reading; writes the previous minute once a new one starts."""
        if self._conn is None:
            return
        now = time.time() if now is None else now
        minute_start = int(now // 60 * 60)
        if self._bucket is not None and self._bucket.start != minute_start:
            self.flush()
        if self._bucket is None:
            self._bucket = _Bucket(minute_start)
        self._bucket.add(temp_c, fan_on)

    def flush(self) -> None:
        """Write the current minute's summary, if any readings are pending."""
        bucket = self._bucket
        if self._conn is None or bucket is None:
            return
        try:
            with self._conn:  # one transaction: both tables or neither
                for table in TABLES:
                    size = _BUCKET_SECONDS[table]
                    self._conn.execute(
                        _UPSERT.format(table=table),
                        (
                            bucket.start // size * size,
                            bucket.samples,
                            bucket.temp_sum,
                            bucket.temp_min,
                            bucket.temp_max,
                            bucket.fan_on_samples,
                        ),
                    )
                self._conn.execute(
                    "DELETE FROM minute WHERE start < ?",
                    (bucket.start - self._minute_retention_sec,),
                )
        except sqlite3.Error as exc:
            logger.warning("Could not write temperature history: %s", exc)
        # Dropped on failure too, so one bad write cannot grow without bound.
        self._bucket = None

    def close(self) -> None:
        self.flush()
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def schema_version(db_path: Path) -> int:
    """Format version of an existing database (0 if it was never stamped)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def open_history(db_path: str, minute_retention_days: float) -> Optional[History]:
    """Open the history database, or return None if disabled or unavailable."""
    if not db_path:
        logger.info("Temperature history disabled (FAN_CONTROL_HISTORY_DB is empty)")
        return None
    history = History(db_path, minute_retention_days)
    try:
        history.open()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Temperature history disabled: cannot open %s: %s", db_path, exc)
        return None
    return history
