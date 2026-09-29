"""tasks/heartbeat.py — a stalled event loop has to be in the log within
minutes (docs/TODO.md, 2026-09-22: a ~21 h stall left no trace but silence)."""
import asyncio
import logging

import pytest

import conftest
from tasks import heartbeat
from tasks.heartbeat import StallWatch

REAL_DEPS = not conftest.STUBBED


class TestStallWatch:
    def test_nothing_is_said_inside_the_threshold(self):
        watch = StallWatch(threshold=180)
        assert watch.check(now=180, last_beat=0) is None

    def test_a_stall_is_one_warning_then_silence(self):
        watch = StallWatch(threshold=180)

        level, message = watch.check(now=181, last_beat=0)

        assert level == logging.WARNING
        assert message.startswith("LOOP: stalled for 181 s")
        assert watch.check(now=600, last_beat=0) is None
        assert watch.check(now=6000, last_beat=0) is None

    def test_recovery_is_one_line_with_the_gap_then_silence(self):
        watch = StallWatch(threshold=180)
        watch.check(now=200, last_beat=0)

        level, message = watch.check(now=905, last_beat=900)

        assert level == logging.INFO
        assert message == "LOOP: recovered — the event loop was stalled for ~900 s"
        assert watch.check(now=960, last_beat=960) is None

    def test_a_second_stall_after_recovery_is_reported_again(self):
        watch = StallWatch(threshold=180)
        watch.check(now=200, last_beat=0)
        watch.check(now=905, last_beat=900)

        level, _ = watch.check(now=1100, last_beat=900)

        assert level == logging.WARNING


def test_the_watch_thread_logs_what_the_watch_says(monkeypatch, caplog):
    monkeypatch.setattr(heartbeat, "_last_beat", 0.0)
    clock = iter([100.0, 181.0, 190.0])

    class _Done(Exception):
        pass

    def sleep(_):
        if not remaining:
            raise _Done
        remaining.pop()

    remaining = [1, 1, 1]
    with caplog.at_level(logging.INFO, logger="tasks.heartbeat"):
        with pytest.raises(_Done):
            heartbeat._watch(StallWatch(threshold=180), sleep=sleep, clock=lambda: next(clock))

    assert caplog.text.count("LOOP: stalled for 181 s") == 1


def test_beat_resets_the_age():
    heartbeat.beat(now=1000.0)
    assert heartbeat.heartbeat_age(now=1042.5) == 42.5


def test_health_answers_with_the_heartbeat_age():
    heartbeat.beat()
    response = asyncio.run(heartbeat.handle_health(None))
    assert response.status == "ok"
    assert 0 <= response.heartbeat_age_s < 5


@pytest.mark.skipif(
    REAL_DEPS,
    reason="_rest_routes/_event_handlers/_interval_handlers are bookkeeping conftest's "
           "Agent stub adds for introspection — the real Agent has no such attributes",
)
def test_the_heartbeat_is_wired_on_the_agent():
    from core.uagent import agent_1

    assert agent_1._rest_routes["/health"] is heartbeat.handle_health
    assert heartbeat.heartbeat in agent_1._interval_handlers
    assert heartbeat.start_stall_watch in agent_1._event_handlers["startup"]
