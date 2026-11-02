import sqlite3
import time
from pathlib import Path

import pytest

from jetson_fan_control import history as history_mod
from jetson_fan_control import report
from jetson_fan_control.history import History, open_history

# 2026-01-01 00:00:00 UTC
T0 = 1767225600


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Report periods are local time; pin it to UTC so labels are predictable."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "state" / "history.db")


@pytest.fixture
def history(db_path):
    history = History(db_path, minute_retention_days=30)
    history.open()
    yield history
    history.close()


def rows(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY start").fetchall()


def test_minute_is_written_once_the_next_one_starts(history, db_path):
    history.record(40.0, False, now=T0 + 1)
    history.record(44.0, True, now=T0 + 31)
    assert rows(db_path, "minute") == []

    history.record(50.0, True, now=T0 + 61)
    # start, samples, temp_sum, temp_min, temp_max, fan_on_samples
    assert rows(db_path, "minute") == [(T0, 2, 84.0, 40.0, 44.0, 1)]
    assert rows(db_path, "hour") == [(T0, 2, 84.0, 40.0, 44.0, 1)]


def test_hour_accumulates_its_minutes(history, db_path):
    history.record(40.0, False, now=T0)
    history.record(50.0, True, now=T0 + 60)
    history.record(30.0, False, now=T0 + 3600)
    history.flush()

    assert rows(db_path, "minute") == [
        (T0, 1, 40.0, 40.0, 40.0, 0),
        (T0 + 60, 1, 50.0, 50.0, 50.0, 1),
        (T0 + 3600, 1, 30.0, 30.0, 30.0, 0),
    ]
    assert rows(db_path, "hour") == [
        (T0, 2, 90.0, 40.0, 50.0, 1),
        (T0 + 3600, 1, 30.0, 30.0, 30.0, 0),
    ]


def test_restart_within_a_minute_merges_into_the_same_row(db_path):
    for temp_c in (40.0, 42.0):
        history = History(db_path, minute_retention_days=30)
        history.open()
        history.record(temp_c, False, now=T0 + 10)
        history.close()

    assert rows(db_path, "minute") == [(T0, 2, 82.0, 40.0, 42.0, 0)]


def test_old_minutes_are_pruned_but_hours_kept(db_path):
    history = History(db_path, minute_retention_days=1)
    history.open()
    history.record(40.0, False, now=T0)
    history.record(41.0, False, now=T0 + 86400 + 60)
    history.close()

    assert [row[0] for row in rows(db_path, "minute")] == [T0 + 86400 + 60]
    assert [row[0] for row in rows(db_path, "hour")] == [T0, T0 + 86400]


def test_reopening_keeps_existing_history(db_path, tmp_path):
    history = History(db_path, minute_retention_days=30)
    history.open()
    history.record(40.0, False, now=T0)
    history.close()

    history = History(db_path, minute_retention_days=30)
    history.open()
    history.close()

    assert rows(db_path, "hour") == [(T0, 1, 40.0, 40.0, 40.0, 0)]
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == ["history.db"]


def test_new_database_is_stamped_with_schema_version(history, db_path):
    assert history_mod.schema_version(Path(db_path)) == history_mod.SCHEMA_VERSION


def test_other_format_version_is_set_aside_not_deleted(db_path, tmp_path):
    Path(db_path).parent.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE old (x)")
        conn.execute("INSERT INTO old VALUES (42)")
        conn.execute("PRAGMA user_version = 99")

    history = History(db_path, minute_retention_days=30)
    history.open()
    history.record(40.0, False, now=T0)
    history.close()

    backup = tmp_path / "state" / "history.db.v99"
    assert sorted(p.name for p in backup.parent.iterdir()) == ["history.db", "history.db.v99"]
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT x FROM old").fetchall() == [(42,)]
    assert rows(db_path, "hour") == [(T0, 1, 40.0, 40.0, 40.0, 0)]


def test_cli_refuses_other_format_version(db_path, capsys):
    Path(db_path).parent.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 99")
    assert report.main(["--db", db_path]) == 1
    assert "format version 99" in capsys.readouterr().err


def test_open_history_can_be_disabled():
    assert open_history("", minute_retention_days=30) is None


def test_open_history_failure_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert open_history(str(blocker / "history.db"), minute_retention_days=30) is None


@pytest.fixture
def two_months(history, db_path):
    """A reading at 00:00 and 12:00 every day of Jan and Feb 2026."""
    for day in range(31 + 28):
        for hour, temp_c, fan_on in ((0, 30.0, False), (12, 40.0, True)):
            history.record(temp_c, fan_on, now=T0 + day * 86400 + hour * 3600)
    history.flush()
    return db_path


@pytest.mark.parametrize(
    "period, first, last",
    [
        ("hour", "2026-02-28 00:00", "2026-02-28 12:00"),
        ("day", "2026-02-27", "2026-02-28"),
        ("month", "2026-01", "2026-02"),
    ],
)
def test_summarise_most_recent_periods_oldest_first(two_months, period, first, last):
    with sqlite3.connect(two_months) as conn:
        summary = report.summarise(conn, period, limit=2)
    assert [row.period for row in summary] == [first, last]


def test_summarise_day_figures(two_months):
    with sqlite3.connect(two_months) as conn:
        (day,) = report.summarise(conn, "day", limit=1)
    assert (day.temp_min, day.temp_avg, day.temp_max) == (30.0, 35.0, 40.0)
    assert day.fan_on_pct == 50.0
    assert day.samples == 2


def test_summarise_minutes(history, db_path):
    history.record(40.0, True, now=T0 + 5)
    history.record(42.0, False, now=T0 + 65)
    history.flush()
    with sqlite3.connect(db_path) as conn:
        summary = report.summarise(conn, "minute", limit=10)
    assert [row.period for row in summary] == ["2026-01-01 00:00", "2026-01-01 00:01"]


def test_cli_prints_table(two_months, capsys):
    assert report.main(["month", "--db", two_months]) == 0
    out = capsys.readouterr().out
    assert "2026-01" in out and "2026-02" in out
    assert "50%" in out


def test_cli_prints_csv(two_months, capsys):
    assert report.main(["month", "--csv", "--db", two_months]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "month,temp_min,temp_avg,temp_max,fan_on_pct,samples"
    assert lines[1] == "2026-01,30.00,35.00,40.00,50.0,62"


def test_cli_reports_missing_database(tmp_path, capsys):
    assert report.main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "Cannot read temperature history" in capsys.readouterr().err
