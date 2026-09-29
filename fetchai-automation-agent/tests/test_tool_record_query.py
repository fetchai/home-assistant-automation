"""The free-hand analyst's read-only SQL tool (proposal F-1, F-WP1).

Run against a real record.db built through core/record.py, like
test_tool_record.py: the safety here IS SQLite's authorizer, read-only open
and progress handler, so stubbing any of it would test nothing. Every refusal
test also counts the events afterwards — "answered an error" is not the claim,
"changed nothing" is.
"""
from datetime import datetime
import json
import sqlite3

import pytest

from core import record
from core import watchlist
from tools import tool_record_query as trq


HOUR = 3600


@pytest.fixture(autouse=True)
def fresh_record(tmp_path, monkeypatch):
    """A private record and watchlist per test, watching the entities the
    fixtures below write — the views show watched entities only (2.21.3)."""
    record.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    _watch("light.lamp", "sensor.busy")
    yield
    record.close()


def _watch(*entity_ids):
    watchlist.set_selection([], list(entity_ids), {})


@pytest.fixture
def now():
    return int(datetime.now().timestamp())


@pytest.fixture
def lamp(now):
    record.record_transition("light.lamp", now - 2 * HOUR, "off")
    record.record_transition("light.lamp", now - HOUR, "on", "off", HOUR)
    record.record_transition("light.lamp", now - 600, "off", "on", HOUR - 600)
    return now


def _query(sql, **kwargs):
    return json.loads(trq.tool_record_query()._run(sql=sql, **kwargs))


def _event_count():
    return record._connect().execute("SELECT count(*) FROM events").fetchone()[0]


class TestAPlainSelect:
    def test_ev_returns_columns_and_rows(self, lamp):
        result = _query("SELECT * FROM ev ORDER BY ts")

        assert result["columns"] == ["entity_id", "ts", "local_time", "state", "prev_state",
                                     "prev_duration", "source"]
        assert result["row_count"] == 3
        assert result["truncated"] is False
        assert result["rows"][1][:2] == ["light.lamp", lamp - HOUR]
        assert result["rows"][1][3:6] == ["on", "off", HOUR]

    def test_local_time_is_local(self, lamp):
        row = _query("SELECT ts, local_time FROM ev ORDER BY ts LIMIT 1")["rows"][0]
        assert row[1] == datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S")

    def test_rel_joins_entity_ids(self, lamp):
        conn = record._connect()
        conn.execute("INSERT INTO relations VALUES (1, 1, 4, 0, 10)")
        conn.commit()
        result = _query("SELECT entity_a, entity_b, count FROM rel")
        assert result["rows"] == [["light.lamp", "light.lamp", 4]]

    @pytest.mark.parametrize("sql", [trq.EXAMPLE_ONS_PER_DAY, trq.EXAMPLE_ON_WHILE_OFF,
                                     trq.EXAMPLE_DAY_HOUR, trq.EXAMPLE_FIRST_ON_PER_DAY])
    def test_every_worked_example_in_the_description_is_allowed(self, lamp, sql):
        """An example the tool refuses teaches the model the tool is broken."""
        assert "error" not in _query(sql)
        assert sql in trq.tool_record_query().description


class TestRefusals:
    @pytest.mark.parametrize("sql, named", [
        ("INSERT INTO events (entity, ts, state) VALUES (1, 1, 'on')", "INSERT"),
        ("UPDATE events SET state = 'x'", "UPDATE"),
        ("DELETE FROM events", "DELETE"),
        ("CREATE TABLE t (x)", "schema"),
        ("DROP TABLE events", "schema"),
        ("ATTACH DATABASE ':memory:' AS other", "ATTACH"),
        ("PRAGMA journal_mode", "PRAGMA"),
        ("SELECT * FROM pragma_table_info('events')", "pragma_table_info"),
        ("SELECT load_extension('/tmp/x.so')", "load_extension"),
        ("SELECT randomblob(8)", "randomblob"),
        ("SELECT * FROM sqlite_master", "sqlite_master"),
        ("SELECT * FROM sqlite_temp_master", "sqlite_temp_master"),
        ("SELECT * FROM profiles", "profiles"),
        ("SELECT * FROM events", "query the views ev and rel"),
        ("SELECT * FROM temp.watched", "watched"),
        ("SELECT * FROM json_each('[1]')", "json_each"),
        ("CREATE TEMP VIEW v AS SELECT 1", "schema"),
        ("DROP VIEW ev", "schema"),
        ("BEGIN IMMEDIATE", "transaction"),
    ])
    def test_it_answers_an_error_naming_the_refusal_and_changes_nothing(self, lamp, sql, named):
        before = _event_count()
        result = _query(sql)
        assert set(result) == {"error"}
        assert named.lower() in result["error"].lower()
        assert _event_count() == before

    @pytest.mark.parametrize("sql", [
        "SELECT 1; DELETE FROM events",
        "SELECT 1; SELECT 2",
        "",
        "   ",
        "SELECT 'unterminated",
    ])
    def test_anything_but_exactly_one_statement_is_refused_before_running(self, lamp, sql):
        before = _event_count()
        assert "one complete SELECT" in _query(sql)["error"]
        assert _event_count() == before

    def test_a_trailing_semicolon_or_comment_is_still_one_statement(self, lamp):
        assert _query("SELECT count(*) FROM ev;")["rows"] == [[3]]
        assert _query("SELECT count(*) FROM ev -- all of them")["rows"] == [[3]]

    def test_a_semicolon_inside_a_string_is_not_a_second_statement(self, lamp):
        assert _query("SELECT 'a;b'")["rows"] == [["a;b"]]

    def test_a_syntax_error_is_reported_not_raised(self, lamp):
        assert "the query failed" in _query("SELEC 1")["error"]


class TestTheWorkedOverlapQuery:
    def test_hours_b_was_on_while_a_was_off(self):
        """A is off [0h, 2h) and [5h, 8h); B is on [1h, 6h). Overlap: 1h + 1h."""
        t0 = 1_700_000_000
        a, b = "binary_sensor.office_occupancy", "switch.office_fan"
        _watch(a, b)
        record.record_transition(a, t0, "off")
        record.record_transition(a, t0 + 2 * HOUR, "on", "off", 2 * HOUR)
        record.record_transition(a, t0 + 5 * HOUR, "off", "on", 3 * HOUR)
        record.record_transition(a, t0 + 8 * HOUR, "on", "off", 3 * HOUR)
        record.record_transition(b, t0 + HOUR, "on")
        record.record_transition(b, t0 + 6 * HOUR, "off", "on", 5 * HOUR)

        assert _query(trq.EXAMPLE_ON_WHILE_OFF)["rows"] == [[2.0]]

    def test_a_stretch_ended_by_a_gap_is_not_counted(self):
        """Field run 2: B sat "on" for two days and then went `unknown` (a
        restart with the device gone) while A was "off" throughout. Nobody
        left B on, so the overlap is 0 hours, not 48 — and the real 1-hour
        overlap that closed by a change still counts."""
        t0 = 1_700_000_000
        a, b = "binary_sensor.office_occupancy", "switch.office_fan"
        _watch(a, b)
        record.record_transition(a, t0, "off")
        record.record_transition(a, t0 + 60 * HOUR, "on", "off", 60 * HOUR)
        record.record_transition(b, t0, "on")
        record.record_transition(b, t0 + HOUR, "off", "on", HOUR)
        record.record_transition(b, t0 + 2 * HOUR, "on", "off", HOUR)
        record.record_transition(b, t0 + 50 * HOUR, "unknown", "on", 48 * HOUR)
        record.record_transition(b, t0 + 51 * HOUR, "unavailable", "unknown", HOUR)

        assert _query(trq.EXAMPLE_ON_WHILE_OFF)["rows"] == [[1.0]]


class TestTheTimeout:
    @pytest.mark.parametrize("sql", [
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c",
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000)"
        " SELECT count(*) FROM c a, c b",
    ])
    def test_a_runaway_query_is_stopped(self, lamp, monkeypatch, sql):
        monkeypatch.setattr(trq, "QUERY_TIMEOUT_SECONDS", 0.2)
        result = _query(sql)
        assert result == {"error": "the query took longer than 0.2 s and was stopped"}

    def test_the_default_message_says_two_seconds(self):
        assert trq.QUERY_TIMEOUT_SECONDS == 2

    def test_a_string_that_would_eat_memory_is_refused(self, lamp):
        sql = ("WITH RECURSIVE c(s, n) AS (SELECT 'xx', 0 UNION ALL"
               " SELECT s || s, n + 1 FROM c WHERE n < 40) SELECT max(length(s)) FROM c")
        assert "error" in _query(sql)


class TestTheCaps:
    @pytest.fixture
    def many(self):
        """700 rows of ~200 characters each: past both caps."""
        conn = record._connect()
        conn.execute("INSERT INTO entities (entity_id) VALUES ('sensor.busy')")
        conn.executemany(
            "INSERT INTO events (entity, ts, state) VALUES"
            " ((SELECT id FROM entities WHERE entity_id = 'sensor.busy'), ?, ?)",
            [(1_700_000_000 + i, "x" * 200) for i in range(700)])
        conn.commit()

    def test_max_rows_sets_truncated(self, many):
        result = _query("SELECT ts FROM ev", max_rows=10)
        assert result["row_count"] == 10
        assert result["truncated"] is True
        assert "max_rows=10" in result["note"]

    def test_max_rows_is_clamped_to_the_ceiling(self, many):
        result = _query("SELECT ts FROM ev", max_rows=10_000)
        assert result["row_count"] == trq.QUERY_MAX_ROWS
        assert result["truncated"] is True

    def test_exactly_max_rows_is_not_truncated(self, many):
        result = _query("SELECT ts FROM ev LIMIT 10", max_rows=10)
        assert result["row_count"] == 10
        assert result["truncated"] is False

    def test_the_byte_cap_sets_truncated(self, many):
        answer = trq.tool_record_query()._run(sql="SELECT entity_id, ts, state FROM ev",
                                              max_rows=500)
        result = json.loads(answer)
        assert len(answer) <= trq.QUERY_MAX_BYTES
        assert 0 < result["row_count"] < 500
        assert result["truncated"] is True
        assert "KB" in result["note"]


class TestReadingAlongsideAWriter:
    def test_a_read_succeeds_while_another_connection_holds_a_write_transaction(self, lamp):
        writer = sqlite3.connect(record.DB_PATH, isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO events (entity, ts, state) VALUES (1, 1, 'uncommitted')")
            result = _query("SELECT count(*) FROM ev")
            assert result["rows"] == [[3]]
        finally:
            writer.execute("ROLLBACK")
            writer.close()


class TestTheConnectionIsAlwaysClosed:
    @pytest.fixture
    def opened(self, monkeypatch):
        real = sqlite3.connect
        seen = []

        def spy(target, *args, **kwargs):
            conn = real(target, *args, **kwargs)
            if str(target).endswith("?mode=ro"):
                seen.append(conn)
            return conn

        monkeypatch.setattr(sqlite3, "connect", spy)
        return seen

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM ev",                  # success
        "DELETE FROM events",                # refused by the authorizer
        "SELECT randomblob(1)",              # refused function
        "SELEC 1",                           # syntax error
    ])
    def test_on_every_path(self, lamp, opened, sql):
        _query(sql)
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_on_the_timeout_path(self, lamp, opened, monkeypatch):
        monkeypatch.setattr(trq, "QUERY_TIMEOUT_SECONDS", 0.1)
        _query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c)"
               " SELECT count(*) FROM c")
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_a_missing_record_is_an_error_not_a_traceback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "nowhere" / "record.db"))
        assert "could not be opened" in _query("SELECT 1")["error"]


class TestOnlyWatchedEntitiesAreShown:
    """F-3's bound is "watched", and unwatching deletes nothing from the record
    (only retention and the size cap do), so the views filter on the
    watchlist at the call and the tables are not readable around them."""

    @pytest.fixture
    def unwatched_lamp(self, lamp):
        _watch("sensor.busy")
        record.record_transition("sensor.busy", lamp - 60, "x")
        return lamp

    def test_an_unwatched_entity_is_still_in_the_record(self, unwatched_lamp):
        """The premise: without the filter, the lamp's rows would be readable."""
        assert record.first_ts("light.lamp") is not None

    def test_ev_does_not_show_an_entity_after_it_is_unwatched(self, unwatched_lamp):
        result = _query("SELECT DISTINCT entity_id FROM ev")
        assert result["rows"] == [["sensor.busy"]]

    def test_rel_keeps_a_relation_only_when_both_entities_are_watched(self, unwatched_lamp):
        conn = record._connect()
        lamp_id, busy_id = (conn.execute("SELECT id FROM entities WHERE entity_id = ?",
                                         (e,)).fetchone()[0]
                            for e in ("light.lamp", "sensor.busy"))
        conn.executemany("INSERT INTO relations VALUES (?, ?, 4, 0, 10)",
                         [(lamp_id, busy_id), (busy_id, lamp_id), (busy_id, busy_id)])
        conn.commit()
        result = _query("SELECT entity_a, entity_b FROM rel")
        assert result["rows"] == [["sensor.busy", "sensor.busy"]]

    def test_rewatching_shows_it_again(self, unwatched_lamp):
        _watch("sensor.busy", "light.lamp")
        assert _query("SELECT count(*) FROM ev WHERE entity_id = 'light.lamp'")["rows"] == [[3]]

    def test_nothing_watched_shows_nothing(self, lamp):
        _watch()
        assert _query("SELECT count(*) FROM ev")["rows"] == [[0]]

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM events",
        "SELECT * FROM main.events",
        "SELECT entity_id FROM entities",
        "SELECT * FROM relations",
        "SELECT * FROM ev WHERE ts IN (SELECT ts FROM events)",
        "SELECT * FROM (SELECT * FROM events) AS ev",
    ])
    def test_a_direct_read_of_a_table_is_refused(self, unwatched_lamp, sql):
        result = _query(sql)
        assert "error" in result
        assert "query the views ev and rel" in result["error"]

    @pytest.mark.parametrize("sql", [
        # SQLite reports a CTE's name as the reader exactly as it reports a
        # view's, so a CTE that borrows `ev` or `rel` must not pass as one.
        "WITH ev AS (SELECT * FROM events) SELECT * FROM ev",
        "WITH ev AS MATERIALIZED (SELECT * FROM main.events) SELECT * FROM ev",
        "WITH rel AS (SELECT * FROM relations) SELECT * FROM rel",
        "WITH ev AS (SELECT entity_id FROM entities) SELECT * FROM ev, temp.ev",
    ])
    def test_a_cte_named_like_a_view_does_not_unlock_the_tables(self, unwatched_lamp, sql):
        result = _query(sql)
        assert "error" in result
        assert "light.lamp" not in json.dumps(result)

    def test_sqlite_names_the_view_as_the_reader_of_a_table(self, lamp):
        """What the authorizer relies on: a read made for a TEMP view arrives
        with that view's name as `source`, not None."""
        conn = sqlite3.connect(f"file:{record.DB_PATH}?mode=ro", uri=True)
        try:
            conn.execute("CREATE TEMP VIEW _inner AS SELECT entity FROM main.events")
            conn.execute("CREATE TEMP VIEW outer_v AS SELECT * FROM temp._inner")
            seen = set()

            def spy(action, arg1, _arg2, db_name, source):
                if action == sqlite3.SQLITE_READ:
                    seen.add((db_name, arg1, source))
                return sqlite3.SQLITE_OK
            conn.set_authorizer(spy)
            conn.execute("WITH c AS (SELECT * FROM outer_v) SELECT * FROM c").fetchall()
        finally:
            conn.close()
        assert ("main", "events", "_inner") in seen
        assert ("temp", "_inner", "outer_v") in seen

    @pytest.mark.parametrize("sql", [
        "WITH c AS (SELECT entity_id, ts FROM ev) SELECT count(*) FROM c",
        "SELECT count(*) FROM (SELECT entity_id FROM ev WHERE state = 'on')",
        "SELECT count(*) FROM ev WHERE ts IN (SELECT ts FROM ev WHERE state = 'off')",
        "SELECT count(*) FROM ev a JOIN ev b ON a.entity_id = b.entity_id AND b.ts > a.ts",
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3)"
        " SELECT count(*) FROM n JOIN ev ON 1",
    ])
    def test_ctes_subqueries_and_self_joins_over_ev_still_work(self, lamp, sql):
        result = _query(sql)
        assert "error" not in result
        assert result["rows"][0][0] > 0

    def test_the_inner_view_names_differ_on_every_call(self, lamp, monkeypatch):
        names = []
        real = trq._authorizer

        def spy(refused, inner):
            names.append(inner)
            return real(refused, inner)
        monkeypatch.setattr(trq, "_authorizer", spy)
        _query("SELECT 1")
        _query("SELECT 1")
        assert len(names) == 2 and not names[0] & names[1]
