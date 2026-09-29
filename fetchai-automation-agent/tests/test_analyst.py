"""core/analyst.py's pure half and its run-date file — proposal §5B, WP-D1.

Four rules the run stands on. A local time the model wrote either becomes an
epoch or is None (`window.local_epoch`, which a chart request's times go
through) — never a neighbouring instant. The key is the config's signature (D-8), so it
survives rewording and key order but not a different target. The run is due
once per local day after ANALYST_HOUR (D-10), and the file that remembers the
last run fails CLOSED: a corrupt file is a run already made today, never a
free second one.
"""
from datetime import datetime, timezone
import importlib
import json
import logging
import os
import time

import pytest

from core import analyst, auditor, window


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Local wall time is the subject here; pinned as test_review_pass.py pins
    it, and restored afterwards because monkeypatch does not tzset()."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def run_file(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "RUN_PATH", str(tmp_path / "analyst_run.json"))


def _ts(*fields):
    return int(datetime(*fields, tzinfo=timezone.utc).timestamp())


NOW = _ts(2026, 6, 30, 12, 0)


class TestALocalTimeAsTheModelWritesIt:
    """`window.local_epoch`, which core/charts.py reads a chart request's
    start and end through (the analyst's own `to_epoch` wrapper went with the
    audited run's claims, free-hand F-9)."""

    def test_a_local_iso_minute_is_that_wall_time(self):
        assert window.local_epoch("2026-06-01T07:30", NOW) == _ts(2026, 6, 1, 7, 30)

    def test_seconds_are_optional_and_read_when_given(self):
        assert window.local_epoch("2026-06-01T07:30:15", NOW) == _ts(2026, 6, 1, 7, 30, 15)

    def test_the_space_the_read_tools_print_is_read_too(self):
        """tool_record._local_iso prints "YYYY-MM-DD HH:MM:SS"; the model copies
        what it was shown (§5B Deviations)."""
        assert window.local_epoch("2026-06-01 07:30:15", NOW) == _ts(2026, 6, 1, 7, 30, 15)

    def test_now_is_the_runs_now(self):
        assert window.local_epoch("now", NOW) == NOW

    @pytest.mark.parametrize("junk", [
        "yesterday", "", "Now", " now", "2026-06-01", "07:30", "2026-06-01T7:30",
        "2026-06-01T07:30Z", "2026-06-01T07:30+02:00", "2026-02-30T07:30",
        "2026-06-01T24:00", "2026-06-01T07:60", "2026-06-01T07:30:60",
        NOW, 1.5, True, None, ["2026-06-01T07:30"], {"at": "now"},
    ])
    def test_anything_else_is_none(self, junk):
        assert window.local_epoch(junk, NOW) is None

    def test_a_wall_time_that_never_happened_is_none(self, monkeypatch):
        """02:30 on the spring-forward morning in Central Europe. mktime would
        normalise it to a neighbouring instant without complaint; the claim
        would then be recounted over an hour nobody asked about."""
        if not os.path.exists("/usr/share/zoneinfo/Europe/Bratislava"):
            pytest.skip("no tz database")
        monkeypatch.setenv("TZ", "Europe/Bratislava")
        time.tzset()

        assert window.local_epoch("2026-03-29T02:30", NOW) is None
        assert window.local_epoch("2026-03-29T03:30", NOW) is not None


CONFIG = {
    "alias": "Hall light on with motion",
    "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hall"}}],
}


class TestAnalystKey:
    def test_it_is_prefixed_and_sixteen_hex_digits(self):
        key = analyst.analyst_key(CONFIG)

        assert key.startswith("analyst:")
        assert len(key) == len("analyst:") + 16
        int(key.split(":", 1)[1], 16)

    def test_stable_under_key_order_in_the_config(self):
        reordered = {
            "action": [{"target": {"entity_id": "light.hall"}, "service": "light.turn_on"}],
            "trigger": [{"to": "on", "entity_id": "binary_sensor.hall_motion",
                         "platform": "state"}],
            "alias": "Hall light on with motion",
        }

        assert analyst.analyst_key(reordered) == analyst.analyst_key(CONFIG)

    def test_and_under_the_plural_keys_audit_normalises(self):
        plural = {"alias": CONFIG["alias"], "triggers": CONFIG["trigger"],
                  "actions": CONFIG["action"]}

        assert analyst.analyst_key(plural) == analyst.analyst_key(CONFIG)
        assert "triggers" in plural, "the caller's config must not be normalised in place"

    def test_new_words_are_the_same_suggestion(self):
        """Alias and condition are not in the signature: rewording reaches
        upsert_analyst_candidate's "new words are a new claim" path instead."""
        reworded = {**CONFIG, "alias": "Light the hall when someone walks in",
                    "condition": [{"condition": "sun", "after": "sunset"}]}

        assert analyst.analyst_key(reworded) == analyst.analyst_key(CONFIG)

    def test_a_different_target_is_a_different_key(self):
        other = {**CONFIG, "action": [{"service": "light.turn_on",
                                       "target": {"entity_id": "light.landing"}}]}

        assert analyst.analyst_key(other) != analyst.analyst_key(CONFIG)

    def test_it_does_not_depend_on_the_order_a_set_iterates_in(self, monkeypatch):
        """signature() is frozensets, whose order follows PYTHONHASHSEED — a
        restart. The same pairs met in either order must hash alike."""
        pairs = [("state", "binary_sensor.b"), ("state", "binary_sensor.a"), ("time", None)]
        actions = [("light.turn_on", "light.hall"), ("light.turn_on", None)]

        monkeypatch.setattr(auditor, "signature", lambda cfg: (list(pairs), list(actions)))
        forward = analyst.analyst_key(CONFIG)
        monkeypatch.setattr(auditor, "signature",
                            lambda cfg: (list(reversed(pairs)), list(reversed(actions))))

        assert analyst.analyst_key(CONFIG) == forward

    def test_a_config_that_is_not_an_object_has_no_key(self):
        assert analyst.analyst_key(None) is None
        assert analyst.analyst_key("alias: x") is None


class TestDue:
    def test_not_before_the_hour(self):
        assert analyst.ANALYST_HOUR == 7
        assert analyst.due(_ts(2026, 6, 30, 6, 59), None) is False
        assert analyst.due(_ts(2026, 6, 30, 7, 0), None) is True

    def test_it_runs_before_the_review_so_the_review_ranks_what_it_stored(self):
        from core import review
        assert analyst.ANALYST_HOUR < review.REVIEW_HOUR

    def test_once_a_local_day_across_the_day_boundary(self):
        assert analyst.due(_ts(2026, 6, 30, 23, 59), "2026-06-30") is False
        assert analyst.due(_ts(2026, 7, 1, 0, 30), "2026-06-30") is False
        assert analyst.due(_ts(2026, 7, 1, 7, 0), "2026-06-30") is True
        assert analyst.due(_ts(2026, 7, 1, 7, 0), "2026-07-01") is False


class TestTheRunFile:
    def test_a_fresh_install_has_never_run(self):
        assert analyst.last_run_date(NOW) is None
        assert analyst.due(NOW, analyst.last_run_date(NOW)) is True

    def test_a_stamp_round_trips(self):
        assert analyst.stamp_run(NOW) is True

        assert analyst.last_run_date(NOW) == "2026-06-30"
        with open(analyst.RUN_PATH, encoding="utf-8") as f:
            assert json.load(f) == {"date": "2026-06-30"}

    def test_a_restart_cannot_rerun_it(self, monkeypatch):
        """The whole reason it is a file: the review's process-memory cadence
        would let a noon restart run the analyst — and send the house out — a
        second time."""
        path = analyst.RUN_PATH
        analyst.stamp_run(NOW)

        importlib.reload(analyst)
        monkeypatch.setattr(analyst, "RUN_PATH", path)

        assert analyst.due(NOW + 3600, analyst.last_run_date(NOW + 3600)) is False

    @pytest.mark.parametrize("content", [
        "{not json", "", '["2026-06-29"]', '{"date": 20260629}', '{"date": "last tuesday"}',
        '{"when": "2026-06-29"}',
    ])
    def test_an_unreadable_file_fails_closed(self, content, caplog):
        with open(analyst.RUN_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        with caplog.at_level(logging.WARNING):
            last = analyst.last_run_date(NOW)

        assert last == "2026-06-30"
        assert analyst.due(NOW, last) is False
        assert "failing closed" in caplog.text
        with open(analyst.RUN_PATH, encoding="utf-8") as f:
            assert json.load(f) == {"date": "2026-06-30"}, \
                "overwritten with today, so a crash loop cannot re-roll it"

    def test_tomorrow_after_a_corrupt_file_runs_again(self):
        with open(analyst.RUN_PATH, "w", encoding="utf-8") as f:
            f.write("{not json")
        analyst.last_run_date(NOW)

        tomorrow = NOW + 86400
        assert analyst.due(tomorrow, analyst.last_run_date(tomorrow)) is True

    def test_a_stamp_that_cannot_be_written_says_so(self, tmp_path, monkeypatch):
        """The caller does not start a run a restart would repeat."""
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("")
        monkeypatch.setattr(analyst, "RUN_PATH", str(blocker / "analyst_run.json"))

        assert analyst.stamp_run(NOW) is False
