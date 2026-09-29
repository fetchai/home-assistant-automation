"""Reading Home Assistant's long-term statistics.

The aggregates are core.backfill's last resort — what fills the stretch an
entity reporting every second is too large to read in full. That makes this
module's failure mode quiet by nature: when it comes back empty the record is
simply shorter, and nothing anywhere reports a problem. So the coverage here is
mostly about the shapes Home Assistant answers with, including the two it has
used for the same field across versions, because a shape misread here looks
exactly like a recorder that held nothing.
"""
import pytest

from tools.tool_homeassistant import statistics


T09 = 1787562000


@pytest.fixture
def calls(monkeypatch):
    """Captures the websocket commands sent, answering with nothing."""
    seen = []

    def _call(payload, **kwargs):
        seen.append(payload)
        return True, {}

    monkeypatch.setattr(statistics, "_ha_ws_call", _call)
    return seen


def _answering(monkeypatch, payload, ok=True):
    monkeypatch.setattr(statistics, "_ha_ws_call", lambda p, **kw: (ok, payload))


class TestWhichEntitiesHaveAMean:
    def test_it_keeps_only_the_entities_that_were_asked_about(self, monkeypatch):
        _answering(monkeypatch, [
            {"statistic_id": "sensor.power", "has_mean": True},
            {"statistic_id": "sensor.unrelated", "has_mean": True},
        ])

        assert statistics.mean_statistic_ids(["sensor.power"]) == {"sensor.power"}

    def test_an_entity_with_no_mean_is_left_out(self, monkeypatch):
        # A total-increasing energy meter has a sum and no mean; there is
        # nothing here to read as a state.
        _answering(monkeypatch, [
            {"statistic_id": "sensor.energy", "has_mean": False, "has_sum": True},
        ])

        assert statistics.mean_statistic_ids(["sensor.energy"]) == set()

    def test_the_newer_mean_type_field_is_understood_too(self, monkeypatch):
        # Home Assistant replaced has_mean (bool) with mean_type (0 none,
        # 1 arithmetic, 2 circular). Cores in the wild send either.
        _answering(monkeypatch, [
            {"statistic_id": "sensor.power", "mean_type": 1},
            {"statistic_id": "sensor.wind", "mean_type": 2},
            {"statistic_id": "sensor.energy", "mean_type": 0},
        ])

        assert statistics.mean_statistic_ids(
            ["sensor.power", "sensor.wind", "sensor.energy"]
        ) == {"sensor.power", "sensor.wind"}

    def test_an_id_carrying_neither_field_is_taken_as_having_no_mean(self, monkeypatch):
        # The conservative reading: the entity keeps the raw-history path,
        # which is the better source anyway.
        _answering(monkeypatch, [{"statistic_id": "sensor.power"}])

        assert statistics.mean_statistic_ids(["sensor.power"]) == set()

    def test_a_failed_call_leaves_every_entity_on_the_history_path(self, monkeypatch):
        _answering(monkeypatch, "connection refused", ok=False)

        assert statistics.mean_statistic_ids(["sensor.power"]) == set()

    def test_junk_in_the_listing_is_stepped_over(self, monkeypatch):
        _answering(monkeypatch, [None, "nope", {}, {"statistic_id": "sensor.power", "has_mean": True}])

        assert statistics.mean_statistic_ids(["sensor.power"]) == {"sensor.power"}

    def test_asking_about_nothing_does_not_call_home_assistant(self, monkeypatch):
        def _never(*args, **kwargs):
            raise AssertionError("must not ask HA about an empty entity list")

        monkeypatch.setattr(statistics, "_ha_ws_call", _never)

        assert statistics.mean_statistic_ids([]) == set()
        assert statistics.mean_statistic_ids(None) == set()


class TestTheRequestItBuilds:
    def test_it_asks_for_the_mean_over_the_window(self, calls):
        statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)

        assert calls[0]["type"] == "recorder/statistics_during_period"
        assert calls[0]["statistic_ids"] == ["sensor.power"]
        assert calls[0]["types"] == ["mean"]
        assert calls[0]["period"] == "5minute"

    def test_the_window_is_expressed_as_iso_timestamps(self, calls):
        statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)

        assert calls[0]["start_time"] == "2026-08-24T09:00:00+00:00"
        assert calls[0]["end_time"] == "2026-08-24T10:00:00+00:00"

    def test_the_period_is_the_callers_to_choose(self, calls):
        statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600, "hour")

        assert calls[0]["period"] == "hour"

    def test_the_listing_request_carries_no_filter_argument(self, monkeypatch):
        # statistic_type has moved between Home Assistant versions, and an
        # argument the schema does not know fails the whole call. The answer is
        # small, so it is filtered here instead.
        seen = []
        monkeypatch.setattr(statistics, "_ha_ws_call",
                            lambda p, **kw: (seen.append(p), (True, []))[1])

        statistics.mean_statistic_ids(["sensor.power"])

        assert set(seen[0]) == {"type"}

    def test_many_entities_are_split_into_batches(self, calls):
        entities = [f"sensor.s{i}" for i in range(statistics.BATCH_SIZE + 3)]

        statistics.fetch_statistics(entities, T09, T09 + 3600)

        # A bucket answer is dense whether the entity is busy or idle, so the
        # batch is much smaller than the history one.
        assert len(calls) == 2

    def test_a_window_that_is_not_a_window_asks_nothing(self, calls):
        assert statistics.fetch_statistics(["sensor.power"], T09, T09) == {}
        assert statistics.fetch_statistics(["sensor.power"], T09, T09 - 100) == {}
        assert statistics.fetch_statistics([], T09, T09 + 3600) == {}
        assert calls == []


class TestTheResponseItReads:
    def test_buckets_come_back_as_ts_and_mean(self, monkeypatch):
        _answering(monkeypatch, {"sensor.power": [
            {"start": T09 * 1000, "end": (T09 + 300) * 1000, "mean": 120.5},
        ]})

        result = statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)

        assert result["sensor.power"] == [{"ts": T09, "mean": 120.5}]

    def test_epoch_milliseconds_and_iso_strings_both_parse(self, monkeypatch):
        # Current cores answer in milliseconds; older ones send an ISO string.
        _answering(monkeypatch, {"sensor.power": [
            {"start": "2026-08-24T09:00:00+00:00", "mean": 1.0},
            {"start": (T09 + 300) * 1000, "mean": 2.0},
        ]})

        result = statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)

        assert [b["ts"] for b in result["sensor.power"]] == [T09, T09 + 300]

    def test_plain_epoch_seconds_are_not_mistaken_for_milliseconds(self, monkeypatch):
        _answering(monkeypatch, {"sensor.power": [{"start": T09, "mean": 1.0}]})

        assert statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)[
            "sensor.power"][0]["ts"] == T09

    def test_buckets_come_back_oldest_first(self, monkeypatch):
        _answering(monkeypatch, {"sensor.power": [
            {"start": (T09 + 600) * 1000, "mean": 3.0},
            {"start": T09 * 1000, "mean": 1.0},
            {"start": (T09 + 300) * 1000, "mean": 2.0},
        ]})

        result = statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)

        assert [b["mean"] for b in result["sensor.power"]] == [1.0, 2.0, 3.0]

    def test_a_bucket_with_no_mean_is_dropped(self, monkeypatch):
        # A period the recorder has a sum for and no mean carries nothing this
        # module can read as a state.
        _answering(monkeypatch, {"sensor.power": [
            {"start": T09 * 1000, "sum": 12.0},
            {"start": (T09 + 300) * 1000, "mean": 2.0},
        ]})

        assert len(statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)["sensor.power"]) == 1

    def test_an_entity_with_no_buckets_is_simply_absent(self, monkeypatch):
        _answering(monkeypatch, {"sensor.power": []})

        assert statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600) == {}

    def test_a_failed_request_is_skipped_rather_than_raised(self, monkeypatch):
        _answering(monkeypatch, "unknown command", ok=False)

        # Best-effort, exactly like history: a stretch that cannot be filled
        # leaves the record shorter.
        assert statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600) == {}

    def test_a_response_that_is_not_a_mapping_is_ignored(self, monkeypatch):
        _answering(monkeypatch, ["unexpected"])

        assert statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600) == {}

    def test_junk_among_the_buckets_is_stepped_over(self, monkeypatch):
        _answering(monkeypatch, {"sensor.power": [
            None, "nope", {}, {"start": None, "mean": 1.0},
            {"start": T09 * 1000, "mean": 1.0},
        ]})

        assert statistics.fetch_statistics(["sensor.power"], T09, T09 + 3600)[
            "sensor.power"] == [{"ts": T09, "mean": 1.0}]
