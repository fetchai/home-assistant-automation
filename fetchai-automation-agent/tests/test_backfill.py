"""Warming the record from Home Assistant's recorder.

Two halves, and both matter for different reasons. history_to_transitions is
pure and is where a backfilled stretch of the record is made consistent with a
live one — same rules, same durations. backfill() is the orchestration, tested
against real SQLite with the Home Assistant call replaced, because the property
that matters most about it is that running it twice is not the same as running
it once: the add-on restarting in a loop must not inflate the record.
"""
import pytest

from core import backfill
from core import record


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    record.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    # Which entities have already been checked for older history is per-process
    # state by design; without clearing it the suite becomes order-dependent.
    backfill._reached_back.clear()
    yield
    record.close()
    backfill._reached_back.clear()


def _entry(state, iso, entity_id=None):
    entry = {"state": state, "last_changed": iso}
    if entity_id:
        entry["entity_id"] = entity_id
    return entry


# 2026-08-24, an ordinary Monday.
T09 = 1787562000
T10 = T09 + 3600
T11 = T09 + 7200


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class TestHistoryToTransitions:
    def test_a_series_becomes_rows_oldest_first(self):
        rows = backfill.history_to_transitions([
            _entry("off", _iso(T09), "light.hall"),
            _entry("on", _iso(T10)),
            _entry("off", _iso(T11)),
        ])

        assert [r[0] for r in rows] == [T09, T10, T11]
        assert [r[1] for r in rows] == ["off", "on", "off"]

    def test_durations_are_measured_across_consecutive_entries(self):
        rows = backfill.history_to_transitions([
            _entry("off", _iso(T09), "light.hall"),
            _entry("on", _iso(T10)),
            _entry("off", _iso(T11)),
        ])

        # The first row has nothing before it to measure against.
        assert rows[0][3] is None
        assert rows[1][3] == 3600
        assert rows[2][3] == 3600
        # ...and each row names the state it replaced.
        assert rows[1][2] == "off"
        assert rows[2][2] == "on"

    def test_entries_arriving_out_of_order_are_sorted_first(self):
        rows = backfill.history_to_transitions([
            _entry("off", _iso(T11), "light.hall"),
            _entry("on", _iso(T10)),
        ])

        assert [r[0] for r in rows] == [T10, T11]

    def test_it_resumes_after_what_was_already_recorded(self):
        rows = backfill.history_to_transitions(
            [_entry("off", _iso(T09), "light.hall"),
             _entry("on", _iso(T10)),
             _entry("off", _iso(T11))],
            last={"state": "on", "ts": T10},
        )

        # Only the part of the window that is genuinely new, and its duration
        # measured against the recorded row rather than the discarded one.
        assert [r[0] for r in rows] == [T11]
        assert rows[0][2] == "on"
        assert rows[0][3] == 3600

    def test_the_same_rules_apply_as_to_live_events(self):
        rows = backfill.history_to_transitions([
            _entry("on", _iso(T09), "light.hall"),
            _entry("on", _iso(T10)),      # attribute-only churn
            _entry("off", _iso(T11)),
        ])

        assert [r[1] for r in rows] == ["on", "off"]

    def test_numeric_readings_are_thresholded_not_stored_wholesale(self):
        # The recorder holds every reading; this is what stops ten days of a
        # power sensor from becoming the whole record.
        series = [_entry(str(100 + i * 0.1), _iso(T09 + i * 60), "sensor.power") for i in range(30)]

        rows = backfill.history_to_transitions(series)

        assert len(rows) < 5

    def test_a_series_of_junk_yields_nothing_rather_than_raising(self):
        assert backfill.history_to_transitions(None) == []
        assert backfill.history_to_transitions([]) == []
        assert backfill.history_to_transitions([None, "nope", {}, {"state": "on"}]) == []

    def test_entries_with_an_unreadable_timestamp_are_skipped(self):
        rows = backfill.history_to_transitions([
            _entry("on", "not a timestamp", "light.hall"),
            _entry("off", _iso(T10)),
        ])

        assert [r[0] for r in rows] == [T10]

    def test_it_falls_back_to_last_updated_when_last_changed_is_absent(self):
        rows = backfill.history_to_transitions(
            [{"entity_id": "light.hall", "state": "on", "last_updated": _iso(T09)}]
        )
        assert rows[0][0] == T09


def _stub_history(monkeypatch, payload, calls=None):
    """Home Assistant's recorder, answering with `payload` keyed by entity.

    backfill consumes history as a STREAM — it must never hold a whole window
    in memory — so the stand-in is a generator of (entity_id, entries) rather
    than the dict the module used to build. `calls` captures the window each
    request covers, which is what most of the tests below are really about.

    probe_density is stubbed to know nothing, which reads as "every entity is
    ordinary": one shared, batched request, the shape these tests assert on.
    Density-driven routing has its own tests in TestDenseEntities.
    """
    def _fake(entity_ids, since, until, **kwargs):
        if calls is not None:
            calls.append((list(entity_ids), since, until))
        for entity_id in entity_ids:
            entries = payload.get(entity_id)
            if entries:
                yield entity_id, entries

    monkeypatch.setattr(backfill, "stream_history", _fake)
    monkeypatch.setattr(backfill, "probe_density", lambda entity_ids, until: {})


class TestBackfill:
    def _history(self, monkeypatch, payload, calls=None):
        _stub_history(monkeypatch, payload, calls)

    def test_it_stores_what_the_recorder_returns(self, monkeypatch):
        self._history(monkeypatch, {"light.hall": [
            _entry("off", _iso(T09), "light.hall"),
            _entry("on", _iso(T10)),
        ]})

        result = backfill.backfill(["light.hall"], until=T11)

        assert result["added"] == {"light.hall": 2}
        assert [r["ts"] for r in record.recent("light.hall")] == [T10, T09]

    def test_backfilled_rows_are_marked_as_such(self, monkeypatch):
        self._history(monkeypatch, {"light.hall": [_entry("on", _iso(T09), "light.hall")]})

        backfill.backfill(["light.hall"], until=T11)

        assert record.recent("light.hall")[0]["source"] == "history"

    def test_running_it_twice_adds_nothing_the_second_time(self, monkeypatch):
        payload = {"light.hall": [
            _entry("off", _iso(T09), "light.hall"),
            _entry("on", _iso(T10)),
        ]}
        self._history(monkeypatch, payload)

        first = backfill.backfill(["light.hall"], until=T11)
        second = backfill.backfill(["light.hall"], until=T11)

        # A restart loop must not inflate the record.
        assert first["added"] == {"light.hall": 2}
        assert second["added"] == {}
        assert len(record.recent("light.hall")) == 2

    def test_it_resumes_from_the_last_recorded_transition(self, monkeypatch):
        record.record_transition("light.hall", T09, "off")
        # Reaching backwards is a separate concern with its own tests below;
        # marking it done isolates the forward resume this test is about.
        backfill._reached_back.add("light.hall")
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11)

        # Not from the ten-day floor: the gap is what is missing, and asking
        # for more would be work the UNIQUE index then throws away.
        assert calls[0][1] == T09

    def test_an_entity_never_recorded_is_asked_for_from_the_floor(self, monkeypatch):
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11)

        assert calls[0][1] == T11 - backfill.MAX_BACKFILL_DAYS * 86400

    def test_an_entity_the_recorder_knows_nothing_about_is_not_an_error(self, monkeypatch):
        # Excluded from the recorder, or simply never seen. Best-effort means
        # this is a bonus not arriving, not a failure.
        self._history(monkeypatch, {})

        assert backfill.backfill(["light.hall"], until=T11)["added"] == {}

    def test_asking_about_nothing_does_not_call_home_assistant(self, monkeypatch):
        def _never(*args, **kwargs):
            raise AssertionError("must not ask HA about an empty entity list")

        monkeypatch.setattr(backfill, "stream_history", _never)
        monkeypatch.setattr(backfill, "probe_density", _never)

        assert backfill.backfill([], until=T11)["added"] == {}
        assert backfill.backfill(None, until=T11)["added"] == {}

    def test_an_up_to_date_entity_asks_for_nothing(self, monkeypatch):
        record.record_transition("light.hall", T11, "on")
        backfill._reached_back.add("light.hall")

        def _never(*args, **kwargs):
            raise AssertionError("nothing is missing, so nothing should be requested")

        monkeypatch.setattr(backfill, "stream_history", _never)
        monkeypatch.setattr(backfill, "probe_density", _never)

        assert backfill.backfill(["light.hall"], until=T11)["added"] == {}

    def test_several_entities_are_filled_in_one_pass(self, monkeypatch):
        self._history(monkeypatch, {
            "light.hall": [_entry("on", _iso(T09), "light.hall")],
            "switch.fan": [_entry("off", _iso(T10), "switch.fan")],
        })

        result = backfill.backfill(["light.hall", "switch.fan"], until=T11)

        assert result["added"] == {"light.hall": 1, "switch.fan": 1}

    def test_the_window_ends_where_the_caller_says(self, monkeypatch):
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T10)

        # The live subscription owns everything after this instant; asking the
        # recorder for it too would just duplicate the listener's work.
        assert calls[0][2] == T10


class TestReachingBackwards:
    """Resuming from the newest row can only ever fill forwards. History that
    exists for a period the agent was not watching sits at the other end of the
    record, and nothing would ever ask for it.

    This is not only a test-data concern: it is what §6.3 means by warming the
    record as far back as the recorder reaches.
    """

    def _history(self, monkeypatch, payload, calls=None):
        _stub_history(monkeypatch, payload, calls)

    def test_a_record_that_starts_late_is_asked_for_from_the_floor(self, monkeypatch):
        # The agent has today's events and nothing older — exactly the state
        # after watching an entity for an afternoon.
        record.record_transition("light.hall", T11, "on")
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11 + 60)

        assert calls[0][1] == T11 + 60 - backfill.MAX_BACKFILL_DAYS * 86400

    def test_the_older_history_actually_lands(self, monkeypatch):
        record.record_transition("light.hall", T11, "on")
        old = T11 - 5 * 86400
        self._history(monkeypatch, {"light.hall": [
            _entry("off", _iso(old), "light.hall"),
            _entry("on", _iso(old + 3600)),
        ]})

        backfill.backfill(["light.hall"], until=T11 + 60)

        # Resuming forwards would have discarded both of these for being older
        # than the newest recorded row.
        assert [r["ts"] for r in record.recent("light.hall")] == [T11, old + 3600, old]

    def test_the_backward_chunk_chains_from_nothing(self, monkeypatch):
        record.record_transition("light.hall", T11, "on")
        old = T11 - 5 * 86400
        self._history(monkeypatch, {"light.hall": [
            _entry("off", _iso(old), "light.hall"),
            _entry("on", _iso(old + 3600)),
        ]})

        backfill.backfill(["light.hall"], until=T11 + 60)

        rows = {r["ts"]: r for r in record.recent("light.hall")}
        assert rows[old]["prev_duration"] is None       # nothing precedes it
        assert rows[old + 3600]["prev_duration"] == 3600
        assert rows[old + 3600]["prev_state"] == "off"

    def test_it_is_asked_only_once_per_process(self, monkeypatch):
        record.record_transition("light.hall", T11, "on")
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11 + 60)
        backfill.backfill(["light.hall"], until=T11 + 120)

        # Otherwise a reconnect loop turns into a ten-day refetch every time.
        assert calls[1][1] == T11
        assert calls[0][1] < calls[1][1]

    def test_a_record_already_reaching_the_floor_asks_for_nothing_extra(self, monkeypatch):
        floor = T11 + 60 - backfill.MAX_BACKFILL_DAYS * 86400
        record.record_transition("light.hall", floor - 3600, "off")
        record.record_transition("light.hall", T11, "on")
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11 + 60)

        assert calls[0][1] == T11

    def test_an_entity_with_no_record_at_all_still_starts_at_the_floor(self, monkeypatch):
        calls = []
        self._history(monkeypatch, {}, calls)

        backfill.backfill(["light.hall"], until=T11)

        assert calls[0][1] == T11 - backfill.MAX_BACKFILL_DAYS * 86400

    def test_a_record_whose_earliest_rows_have_been_pruned_still_counts_as_reaching_the_floor(
        self, monkeypatch
    ):
        """record.first_ts() now widens with the entity's collapsed profile —
        so an entity whose oldest LIVE row looks recent, because everything
        before it was already pruned away, must not be mistaken for one that
        has never reached backwards at all. Getting this wrong would turn
        every prune cycle into an unnecessary re-backfill of history the
        record already has, just collapsed rather than raw."""
        floor = T11 + 60 - backfill.MAX_BACKFILL_DAYS * 86400
        record.record_transition("light.hall", floor - 30 * 86400, "off")
        record.record_transition("light.hall", T11, "on")
        # prune() reads real wall-clock time by default; pin it so this test's
        # outcome does not depend on when it happens to run. backfill.backfill
        # takes `until` explicitly and never consults this clock, so leaving
        # it patched for the rest of the test changes nothing else.
        monkeypatch.setattr(record, "_now", lambda: T11 + 60)
        record.prune(retention_days=backfill.MAX_BACKFILL_DAYS // 2)  # collapses the old row
        assert record.first_ts("light.hall") < floor  # the floor is still covered, via profiles

        calls = []
        self._history(monkeypatch, {}, calls)
        backfill.backfill(["light.hall"], until=T11 + 60)

        # Resumes forward from the live row, same as the already-reaching-the-
        # floor case above — no unnecessary reach backward.
        assert calls[0][1] == T11


def _stub_statistics(monkeypatch, with_mean=(), buckets=None, calls=None):
    """Home Assistant's aggregates, answering with `buckets` keyed by entity.

    `buckets` is {period: {entity_id: [{ts, mean}, ...]}} — the two periods
    matter separately, because the hourly pass exists only to reach past a
    purge window the five-minute one could not.
    """
    buckets = buckets or {}

    def _ids(entity_ids):
        return {e for e in entity_ids if e in with_mean}

    def _fetch(entity_ids, since, until, period="5minute"):
        if calls is not None:
            calls.append((period, list(entity_ids), since, until))
        return {e: list(rows) for e, rows in buckets.get(period, {}).items()
                if e in set(entity_ids)}

    monkeypatch.setattr(backfill.ha_statistics, "mean_statistic_ids", _ids)
    monkeypatch.setattr(backfill.ha_statistics, "fetch_statistics", _fetch)


class TestStatisticsToTransitions:
    """The aggregates converted the same way history is. A bucket's mean is
    read as the entity's state at the bucket's START — the only placement that
    does not invent a timestamp, and inventing one would corrupt both
    prev_duration and the hour-of-day histogram profiles are built from."""

    def test_a_bucket_becomes_a_row_at_its_start(self):
        rows = backfill.statistics_to_transitions([{"ts": T09, "mean": 100.0}])

        assert rows == [(T09, "100", None, None)]

    def test_float_noise_is_rounded_away(self):
        # The recorder's mean carries noise no live reading ever would, and a
        # person reads this column in the panel.
        rows = backfill.statistics_to_transitions([{"ts": T09, "mean": 21.400000000000002}])

        assert rows[0][1] == "21.4"

    def test_the_observer_rules_apply_to_buckets_too(self):
        # A flat stretch collapses exactly as a live one would: five minutes is
        # finer than the record's own heartbeat for a numeric entity.
        rows = backfill.statistics_to_transitions([
            {"ts": T09, "mean": 100.0},
            {"ts": T09 + 300, "mean": 100.1},
            {"ts": T09 + 600, "mean": 100.2},
        ])

        assert len(rows) == 1

    def test_a_step_between_buckets_is_kept(self):
        rows = backfill.statistics_to_transitions([
            {"ts": T09, "mean": 100.0},
            {"ts": T09 + 300, "mean": 3000.0},
        ])

        assert [r[1] for r in rows] == ["100", "3000"]

    def test_it_resumes_and_stops_where_it_is_told(self):
        rows = backfill.statistics_to_transitions(
            [{"ts": T09, "mean": 1.0}, {"ts": T10, "mean": 500.0}, {"ts": T11, "mean": 900.0}],
            last={"state": "1", "ts": T09}, before=T11,
        )

        assert [r[0] for r in rows] == [T10]

    def test_junk_is_stepped_over(self):
        assert backfill.statistics_to_transitions(None) == []
        assert backfill.statistics_to_transitions([]) == []
        assert backfill.statistics_to_transitions(
            [None, "nope", {}, {"ts": None, "mean": 1.0}, {"ts": T09, "mean": None}]) == []


class TestEntitiesThatReportTooOften:
    """The case this whole path exists for: an integration pushing a state
    every second or two. Ten days of it is 864,000 entries, and the old fixed
    request shape asked for twenty such entities at once — a request Home
    Assistant could not answer, retried on every reconnect, so the entity was
    never observed at all.
    """

    def _dense(self, monkeypatch, rate=1.0, payload=None, calls=None):
        _stub_history(monkeypatch, payload or {}, calls)
        monkeypatch.setattr(backfill, "probe_density",
                            lambda entity_ids, until: {e: rate for e in entity_ids})

    def test_it_is_fetched_alone_and_in_narrow_windows(self, monkeypatch):
        calls = []
        self._dense(monkeypatch, calls=calls)
        _stub_statistics(monkeypatch)

        backfill.backfill(["sensor.power", "light.hall"], until=T11)

        # One request per entity, not one shared request: a firehose sharing a
        # batch takes nineteen innocent entities down with it.
        assert [c[0] for c in calls] == [["sensor.power"], ["light.hall"]]

    def test_the_raw_window_is_bounded_by_the_request_budget(self, monkeypatch):
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 2)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 2)
        calls = []
        self._dense(monkeypatch, calls=calls)
        _stub_statistics(monkeypatch)

        backfill.backfill(["sensor.power"], until=T11)

        # Two requests' worth of the newest end, not the whole ten days — and
        # the newest end is where the questions are.
        assert calls[0][1] == T11 - 2 * backfill.chunk_for(1.0)
        assert calls[0][2] == T11

    def test_an_ordinary_entity_still_shares_one_request(self, monkeypatch):
        calls = []
        _stub_history(monkeypatch, {}, calls)
        monkeypatch.setattr(backfill, "probe_density", lambda entity_ids, until: {})

        backfill.backfill(["light.hall", "switch.fan"], until=T11)

        assert calls[0][0] == ["light.hall", "switch.fan"]

    def test_the_stretch_the_budget_could_not_reach_is_filled_from_aggregates(self, monkeypatch):
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        self._dense(monkeypatch)
        old = T11 - 5 * 86400
        _stub_statistics(monkeypatch, with_mean={"sensor.power"}, buckets={
            "5minute": {"sensor.power": [{"ts": old, "mean": 100.0},
                                         {"ts": old + 300, "mean": 3000.0}]},
        })

        result = backfill.backfill(["sensor.power"], until=T11)

        assert result["added"] == {"sensor.power": 2}
        assert [r["ts"] for r in record.recent("sensor.power")] == [old + 300, old]

    def test_those_rows_are_marked_as_coming_from_the_aggregates(self, monkeypatch):
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        self._dense(monkeypatch)
        _stub_statistics(monkeypatch, with_mean={"sensor.power"}, buckets={
            "5minute": {"sensor.power": [{"ts": T11 - 5 * 86400, "mean": 100.0}]},
        })

        backfill.backfill(["sensor.power"], until=T11)

        # 'history' and 'statistics' are counted together by the panel and kept
        # apart by the row: one carries crossings, the other means.
        assert record.recent("sensor.power")[0]["source"] == "statistics"

    def test_the_aggregates_are_asked_for_only_behind_what_history_reached(self, monkeypatch):
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        reached = T11 - 4 * 3600
        self._dense(monkeypatch, payload={"sensor.power": [
            _entry("100", _iso(reached), "sensor.power"),
        ]})
        calls = []
        _stub_statistics(monkeypatch, with_mean={"sensor.power"}, calls=calls)

        backfill.backfill(["sensor.power"], until=T11)

        # Bounded by the oldest entry history actually produced, not by what
        # the budget planned to fetch.
        assert calls[0][3] == reached

    def test_an_entity_with_no_aggregates_keeps_what_history_managed(self, monkeypatch):
        # A text sensor or a device_tracker reporting every second has no
        # state_class and so no statistics to fall back on. The recent end is
        # all there is, and that is not an error.
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        self._dense(monkeypatch, payload={"sensor.chatty": [
            _entry("here", _iso(T10), "sensor.chatty"),
        ]})
        _stub_statistics(monkeypatch, with_mean=set())

        result = backfill.backfill(["sensor.chatty"], until=T11)

        assert result["added"] == {"sensor.chatty": 1}

    def test_the_hourly_pass_covers_only_what_five_minutes_could_not(self, monkeypatch):
        """Short-term statistics are purged with the states, on the user's own
        retention setting; the hourly ones are never purged. So a home on
        purge_keep_days: 3 is still warmed with the shape behind that — and a
        home that keeps ten days never makes the second request."""
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        self._dense(monkeypatch)
        short_reach = T11 - 3 * 86400
        calls = []
        _stub_statistics(monkeypatch, with_mean={"sensor.power"}, calls=calls, buckets={
            "5minute": {"sensor.power": [{"ts": short_reach, "mean": 100.0}]},
        })

        backfill.backfill(["sensor.power"], until=T11)

        assert [c[0] for c in calls] == ["5minute", "hour"]
        # The hourly window stops where the five-minute buckets began, so the
        # two never overlap and interleave.
        assert calls[1][3] == short_reach

    def test_a_full_five_minute_answer_asks_for_no_hourly_pass(self, monkeypatch):
        monkeypatch.setattr(backfill, "DENSE_REQUEST_BUDGET", 1)
        monkeypatch.setattr(backfill, "MIN_REQUESTS_PER_ENTITY", 1)
        self._dense(monkeypatch)
        floor = T11 - backfill.MAX_BACKFILL_DAYS * 86400
        calls = []
        _stub_statistics(monkeypatch, with_mean={"sensor.power"}, calls=calls, buckets={
            "5minute": {"sensor.power": [{"ts": floor, "mean": 100.0}]},
        })

        backfill.backfill(["sensor.power"], until=T11)

        assert [c[0] for c in calls] == ["5minute"]

    def test_an_ordinary_entity_never_reaches_for_the_aggregates(self, monkeypatch):
        # They are the last resort, not a second source. An aggregate cannot
        # carry a crossing at the second it happened, which is what
        # record.scan_relations pairs with whatever followed it.
        _stub_history(monkeypatch, {"light.hall": [_entry("on", _iso(T09), "light.hall")]})

        def _never(*args, **kwargs):
            raise AssertionError("raw history reached the floor — nothing to fall back to")

        monkeypatch.setattr(backfill.ha_statistics, "mean_statistic_ids", _never)

        assert backfill.backfill(["light.hall"], until=T11)["added"] == {"light.hall": 1}
