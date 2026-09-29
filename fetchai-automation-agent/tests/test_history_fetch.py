"""Reading Home Assistant's recorder — the request that is built, and the
response that comes back.

This file exists because of a bug it would have caught. fetch_history was the
one piece of the backfill path with no coverage: test_backfill.py stubs it out
wholesale, so the URL it assembles was never looked at by anything except Home
Assistant, which answered "Invalid end_time" and left every window empty. The
cause was an ISO timestamp ending in "+00:00" going into a query string, where
"+" means a space. The timestamp in the PATH was fine, which is what made it
easy to miss.

So the first test here is about percent-encoding, and the rest cover the
shapes the recorder actually answers with, including the minimal_response form
where only the first entry of a series names its entity.
"""
import pytest

from tools.tool_homeassistant import history


T09 = 1787562000
DAY = 86400


@pytest.fixture
def requests(monkeypatch):
    """Captures the paths fetch_history asks for, answering with nothing."""
    seen = []

    def _get(path, **kwargs):
        seen.append(path)
        return True, []

    monkeypatch.setattr(history, "_ha_get", _get)
    return seen


def _answering(monkeypatch, payload):
    monkeypatch.setattr(history, "_ha_get", lambda path, **kwargs: (True, payload))


class TestTheRequestItBuilds:
    def test_no_raw_plus_reaches_the_query_string(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + 3600)

        # "+00:00" in a query value decodes to a space, and Home Assistant
        # answers "Invalid end_time". This is the regression.
        query = requests[0].split("?", 1)[1]
        assert "+" not in query
        assert "%2B" in query

    def test_the_window_is_expressed_as_encoded_timestamps(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + 3600)

        assert "2026-08-24T09%3A00%3A00%2B00%3A00" in requests[0]
        assert "end_time=2026-08-24T10%3A00%3A00%2B00%3A00" in requests[0]

    def test_it_asks_only_for_what_the_record_stores(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + 3600)

        # Attributes are never stored, and letting HA drop the churn server
        # side costs nothing to transfer.
        assert "minimal_response" in requests[0]
        assert "no_attributes" in requests[0]
        assert "significant_changes_only" in requests[0]

    def test_the_entities_are_named_in_the_filter(self, requests):
        history.fetch_history(["light.hall", "switch.fan"], T09, T09 + 3600)

        assert "filter_entity_id=light.hall%2Cswitch.fan" in requests[0]


class TestBatchingAndChunking:
    def test_a_long_window_is_split_into_days(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + 3 * DAY)

        # A power sensor over ten days is a very large answer; a day at a time
        # keeps any single response bounded.
        assert len(requests) == 3

    def test_the_last_chunk_stops_at_the_window_end(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + DAY + 3600)

        assert "end_time=2026-08-25T10%3A00%3A00%2B00%3A00" in requests[-1]

    def test_many_entities_are_split_into_batches(self, requests):
        entities = [f"light.l{i}" for i in range(history.BATCH_SIZE + 5)]

        history.fetch_history(entities, T09, T09 + 3600)

        assert len(requests) == 2

    def test_a_window_that_is_not_a_window_asks_nothing(self, requests):
        assert history.fetch_history(["light.hall"], T09, T09) == {}
        assert history.fetch_history(["light.hall"], T09, T09 - 100) == {}
        assert history.fetch_history([], T09, T09 + 3600) == {}
        assert requests == []


class TestTheResponseItReads:
    def test_a_series_is_keyed_by_its_entity(self, monkeypatch):
        _answering(monkeypatch, [[
            {"entity_id": "light.hall", "state": "on", "last_changed": "2026-08-24T09:00:00+00:00"},
            {"state": "off", "last_changed": "2026-08-24T09:30:00+00:00"},
        ]])

        result = history.fetch_history(["light.hall"], T09, T09 + 3600)

        # Under minimal_response only the first entry names the entity, and
        # the rest of the series belongs to it.
        assert list(result) == ["light.hall"]
        assert [e["state"] for e in result["light.hall"]] == ["on", "off"]

    def test_several_series_come_back_separately(self, monkeypatch):
        _answering(monkeypatch, [
            [{"entity_id": "light.hall", "state": "on", "last_changed": "2026-08-24T09:00:00+00:00"}],
            [{"entity_id": "switch.fan", "state": "off", "last_changed": "2026-08-24T09:00:00+00:00"}],
        ])

        result = history.fetch_history(["light.hall", "switch.fan"], T09, T09 + 3600)

        assert set(result) == {"light.hall", "switch.fan"}

    def test_chunks_of_one_entity_are_joined_end_to_end(self, monkeypatch):
        # Each day's request answers separately; the series is one entity's.
        def _get(path, **kwargs):
            return True, [[{"entity_id": "light.hall", "state": "on",
                            "last_changed": "2026-08-24T09:00:00+00:00"}]]

        monkeypatch.setattr(history, "_ha_get", _get)

        result = history.fetch_history(["light.hall"], T09, T09 + 2 * DAY)

        assert len(result["light.hall"]) == 2

    def test_an_entity_the_recorder_has_nothing_for_is_simply_absent(self, monkeypatch):
        _answering(monkeypatch, [])

        assert history.fetch_history(["light.hall"], T09, T09 + 3600) == {}

    def test_a_failed_window_is_skipped_rather_than_raised(self, monkeypatch):
        monkeypatch.setattr(history, "_ha_get", lambda path, **kwargs: (False, "HTTP 400: Invalid end_time"))

        # Best-effort: a gap that cannot be filled leaves the record smaller,
        # which is a bonus not arriving rather than a failure to handle.
        assert history.fetch_history(["light.hall"], T09, T09 + 3600) == {}

    def test_one_failed_window_does_not_lose_the_others(self, monkeypatch):
        answers = [
            (False, "HTTP 500"),
            (True, [[{"entity_id": "light.hall", "state": "on",
                      "last_changed": "2026-08-25T09:00:00+00:00"}]]),
        ]
        monkeypatch.setattr(history, "_ha_get", lambda path, **kwargs: answers.pop(0))

        result = history.fetch_history(["light.hall"], T09, T09 + 2 * DAY)

        assert len(result["light.hall"]) == 1

    def test_junk_in_the_response_is_stepped_over(self, monkeypatch):
        _answering(monkeypatch, [
            None, [], "nope",
            [{"state": "on", "last_changed": "2026-08-24T09:00:00+00:00"}],  # names no entity
            [{"entity_id": "light.hall", "state": "on", "last_changed": "2026-08-24T09:00:00+00:00"}],
        ])

        assert list(history.fetch_history(["light.hall"], T09, T09 + 3600)) == ["light.hall"]

    def test_a_response_that_is_not_a_list_is_ignored(self, monkeypatch):
        _answering(monkeypatch, {"message": "unexpected"})

        assert history.fetch_history(["light.hall"], T09, T09 + 3600) == {}


class TestSizingTheRequestToTheEntity:
    """Why any of this exists. The old shape was fixed — twenty entities, one
    day — and an integration reporting every second holds 86,400 entries a day
    on its own. Asking for twenty of those is around 120 MB, which Home
    Assistant cannot serialise inside any sane timeout; the request was retried
    on every reconnect, failed every time, and the entity was never observed at
    all. The request has to be sized to how often the entity really reports.
    """

    def test_an_ordinary_entity_keeps_the_full_day(self):
        # A few hundred changes a day — the shape everything assumed before.
        assert history.chunk_for(500 / 86400) == history.CHUNK_SECONDS

    def test_a_one_hertz_entity_gets_a_narrow_window(self):
        chunk = history.chunk_for(1.0)

        assert chunk == history.MAX_ENTRIES_PER_REQUEST
        assert chunk < history.CHUNK_SECONDS

    def test_the_window_never_narrows_past_the_floor(self):
        # Below an hour the request COUNT is the problem instead of the
        # response size, and an entity that dense belongs on the aggregates.
        assert history.chunk_for(1000.0) == history.MIN_CHUNK_SECONDS

    def test_an_unknown_rate_is_read_as_ordinary(self):
        assert history.chunk_for(0) == history.CHUNK_SECONDS
        assert history.chunk_for(None) == history.CHUNK_SECONDS

    def test_an_ordinary_entity_may_share_a_request(self):
        assert history.is_batchable(500 / 86400)
        assert history.is_batchable(0)
        assert history.is_batchable(None)

    def test_a_fast_reporting_entity_may_not(self):
        # One entity reporting a hundred times more often than the rest makes
        # the whole batch's response too large to answer, and nineteen
        # innocent entities lose their history because of it.
        assert not history.is_batchable(1.0)


class TestStreaming:
    """core.backfill must never hold a window in memory: ten days of twenty
    one-second entities is some seventeen million entries, and the add-on is
    killed before a single row reaches the record."""

    def test_each_chunk_is_yielded_as_it_arrives(self, monkeypatch):
        def _get(path, **kwargs):
            return True, [[{"entity_id": "light.hall", "state": "on",
                            "last_changed": "2026-08-24T09:00:00+00:00"}]]

        monkeypatch.setattr(history, "_ha_get", _get)

        chunks = list(history.stream_history(["light.hall"], T09, T09 + 3 * DAY))

        assert len(chunks) == 3
        assert all(entity_id == "light.hall" for entity_id, _ in chunks)

    def test_nothing_is_requested_before_the_first_chunk_is_asked_for(self, requests):
        # A generator that did its work eagerly would defeat the point.
        history.stream_history(["light.hall"], T09, T09 + 3 * DAY)

        assert requests == []

    def test_the_recorder_gets_a_timeout_of_its_own(self, monkeypatch):
        seen = {}

        def _get(path, timeout=None):
            seen["timeout"] = timeout
            return True, []

        monkeypatch.setattr(history, "_ha_get", _get)
        history.fetch_history(["light.hall"], T09, T09 + 3600)

        # The client default is sized for a state read; a database query behind
        # even a well-sized window takes longer than that on a Pi.
        assert seen["timeout"] == history.TIMEOUT_SECONDS

    def test_newest_first_walks_the_window_backwards(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + 3 * DAY, newest_first=True)

        assert "2026-08-26T09" in requests[0]
        assert "2026-08-24T09" in requests[-1]

    def test_the_walk_stops_once_the_entry_cap_is_reached(self, monkeypatch):
        made = []

        def _get(path, **kwargs):
            made.append(path)
            return True, [[{"entity_id": "light.hall", "state": "on",
                            "last_changed": "2026-08-24T09:00:00+00:00"}] * 40]

        monkeypatch.setattr(history, "_ha_get", _get)

        list(history.stream_history(["light.hall"], T09, T09 + 10 * DAY, max_entries=100))

        # Three chunks of forty pass the cap; the remaining seven days are
        # never asked for.
        assert len(made) == 3

    def test_a_chunk_size_may_be_given(self, requests):
        history.fetch_history(["light.hall"], T09, T09 + DAY, chunk_seconds=3600)

        assert len(requests) == 24

    def test_a_batch_size_may_be_given(self, requests):
        history.fetch_history(["light.hall", "switch.fan"], T09, T09 + 3600, batch_size=1)

        assert len(requests) == 2


class TestProbingDensity:
    """How often an entity really reports, measured rather than guessed. A
    guess is wrong in both directions: a calm entity pushed onto a narrower
    path pays round trips it never needed, and a firehose left on the default
    path hangs the backfill for good."""

    def test_it_reports_entries_per_second(self, monkeypatch):
        entries = [{"entity_id": "sensor.power", "state": "1",
                    "last_changed": "2026-08-24T09:00:00+00:00"}] * history.PROBE_SECONDS
        monkeypatch.setattr(history, "_ha_get", lambda path, **kwargs: (True, [entries]))

        assert history.probe_density(["sensor.power"], T09)["sensor.power"] == 1.0

    def test_it_looks_at_the_window_just_before_the_instant_given(self, requests):
        history.probe_density(["sensor.power"], T09)

        assert "end_time=2026-08-24T09%3A00%3A00%2B00%3A00" in requests[0]
        assert f"period/{'2026-08-24T08%3A45%3A00%2B00%3A00'}" in requests[0]

    def test_it_probes_with_the_request_the_real_fetch_makes(self, requests):
        # Otherwise the number would not mean what that fetch has to carry.
        history.probe_density(["sensor.power"], T09)

        assert "minimal_response" in requests[0]
        assert "significant_changes_only" in requests[0]

    def test_an_entity_the_recorder_says_nothing_about_is_absent_not_zero(self, requests):
        # "Quiet" and "unknown" are different claims, and the caller reads an
        # absence as ordinary.
        assert history.probe_density(["sensor.power"], T09) == {}

    def test_one_probe_covers_the_whole_batch(self, requests):
        history.probe_density([f"sensor.s{i}" for i in range(history.BATCH_SIZE)], T09)

        assert len(requests) == 1
