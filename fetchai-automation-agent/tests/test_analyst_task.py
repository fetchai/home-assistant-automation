"""tasks/analyst.py — the analyst's daily tick, proposal §5B, WP-D3 (D-10).

The body is driven with core/analyst.py's four entry points replaced, so what is
held is the ORDER the task calls them in: not due → nothing; due → the run file
is stamped before `run_explorer` starts; a stamp that could not be written → no run
(fails closed); a run that raises → logged and swallowed, never propagated into
the event loop. One test then uses the real run file, to hold that the stamp a
run writes is what makes the next tick not due — i.e. a restart cannot re-run.
"""
import asyncio
from datetime import datetime, timezone
import logging
import time

import pytest

import conftest
from core import analyst
from tasks import analyst as analyst_task

REAL_DEPS = not conftest.STUBBED


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def run_file(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "RUN_PATH", str(tmp_path / "analyst_run.json"))


NOW = int(datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc).timestamp())


class _Ctx:
    logger = logging.getLogger("test.analyst_task")


def _tick():
    asyncio.run(analyst_task.analyst_pass(_Ctx()))


@pytest.fixture
def calls(monkeypatch):
    """Records the order of the task's calls into core/analyst.py."""
    log = []
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(analyst, "last_run_date", lambda now=None: log.append("last") or None)
    monkeypatch.setattr(analyst, "stamp_run", lambda now=None: log.append("stamp") or True)
    monkeypatch.setattr(analyst, "run_explorer", lambda now=None: log.append("run") or {})
    return log


def test_not_due_does_nothing(calls, monkeypatch):
    monkeypatch.setattr(analyst, "due", lambda now, last: False)
    _tick()
    assert calls == ["last"]


def test_due_stamps_before_it_runs(calls, monkeypatch):
    monkeypatch.setattr(analyst, "due", lambda now, last: True)
    _tick()
    assert calls == ["last", "stamp", "run"]


def test_a_stamp_that_fails_means_no_run(calls, monkeypatch, caplog):
    monkeypatch.setattr(analyst, "due", lambda now, last: True)
    monkeypatch.setattr(analyst, "stamp_run", lambda now=None: calls.append("stamp") or False)
    with caplog.at_level(logging.ERROR):
        _tick()
    assert calls == ["last", "stamp"]
    assert "not running it" in caplog.text


def test_a_run_that_raises_is_logged_and_swallowed(calls, monkeypatch, caplog):
    monkeypatch.setattr(analyst, "due", lambda now, last: True)

    def boom(now=None):
        calls.append("run")
        raise RuntimeError("ASI1 unreachable")

    monkeypatch.setattr(analyst, "run_explorer", boom)
    with caplog.at_level(logging.ERROR):
        _tick()  # does not raise
    assert calls == ["last", "stamp", "run"]
    assert "ANALYST: the daily run failed: ASI1 unreachable" in caplog.text


def test_the_persisted_stamp_is_what_stops_a_second_run(monkeypatch):
    """Real due / last_run_date / stamp_run over the run file: two ticks the
    same morning — as across a restart, since nothing is held in process —
    run once."""
    runs = []
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(analyst, "run_explorer", lambda now=None: runs.append(now) or {})
    _tick()
    _tick()
    assert runs == [NOW]
    assert analyst.last_run_date(NOW) == "2026-06-30"


def test_before_the_hour_nothing_runs(monkeypatch):
    runs = []
    early = NOW - 2 * 3600  # 06:00, before ANALYST_HOUR
    monkeypatch.setattr(time, "time", lambda: early)
    monkeypatch.setattr(analyst, "run_explorer", lambda now=None: runs.append(now) or {})
    _tick()
    assert runs == []
    assert analyst.last_run_date(early) is None


def test_it_runs_before_the_review():
    """D-10: the morning's review ranks what the analyst stored."""
    from core import review
    assert analyst.ANALYST_HOUR < review.REVIEW_HOUR


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_interval_handlers is bookkeeping conftest's Agent stub adds for "
           "introspection — the real uagents.Agent exposes no such attribute.",
)
class TestTheAgentIsWiredToTheAnalyst:
    """Nothing else starts the analyst, so a registration that quietly stopped
    happening would leave every other test green and the analyst silent."""

    def test_the_daily_run_is_registered_as_an_interval(self):
        from core.uagent import agent_1
        assert analyst_task.analyst_pass in agent_1._interval_handlers

    def test_the_interval_is_finer_than_the_cadence_it_serves(self):
        assert analyst_task.ANALYST_INTERVAL_SECONDS < 3600
