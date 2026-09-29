"""core/charts.py — free-hand F-WP4: the chart series (F-7) and the "what the
rule would have done" markers (F-8).

The pure halves (`request`, `clamp`, `gapped_runs`, `series`, `rule`,
`markers`) are driven with literals; `draw` and `redraw` against a real
record.db, watchlist and suggestion store, like test_tool_record_timeline.py —
everything underneath is stdlib SQLite, so stubbing it would test the stub.

What is held: the four series shapes, window clipping, a 23- and a 25-hour
day drawn as such, over-long / too-short / before-the-record windows CLAMPED
with `window_clamped` (field run 2), `unknown`/`unavailable` as gaps that are
never counted (field run 2's 466 "on" hours), the caps; markers for each
supported shape under Home Assistant's semantics, and every unsupported
shape refused whole with a note naming it; a golden case over field run 2's
kitchen config; `redraw` and `run_analyst.py --redraw`.
"""
import calendar
from datetime import date
import importlib.util
import os
import time

import pytest

from core import activity
from core import charts
from core import record
from core import suggestions
from core import watchlist
from core import window


MIN = 60
HOUR = 3600
DAY = 86400
# Monday 2026-09-21 00:00 UTC — every pure test below runs under TZ=UTC.
T0 = calendar.timegm((2026, 9, 21, 0, 0, 0))

MOTION, LIGHT, FAN = "binary_sensor.motion", "light.kitchen", "fan.bath"

# Field run 2's idea #15, as the model submitted it (docs/TODO.md).
KITCHEN_MOTION, KITCHEN_LIGHT = "binary_sensor.test_motion_sensor", "light.test_kitchen_light"
KITCHEN_CONFIG = {
    "alias": "Kitchen light off after 30 min without motion",
    "mode": "single",
    "trigger": [{"entity_id": KITCHEN_MOTION, "platform": "state", "to": "off",
                 "for": "00:30:00"}],
    "condition": [{"condition": "state", "entity_id": KITCHEN_LIGHT, "state": "on"}],
    "action": [{"service": "light.turn_off", "target": {"entity_id": KITCHEN_LIGHT}}],
}


def _tz(monkeypatch, name):
    monkeypatch.setenv("TZ", name)
    time.tzset()


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    _tz(monkeypatch, "UTC")
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def house(tmp_path, monkeypatch):
    record.close()
    suggestions.close()
    monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setattr(watchlist, "STORE_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setattr(watchlist, "_state", None)
    monkeypatch.setattr(watchlist, "_resolved_set", frozenset())
    yield
    record.close()
    suggestions.close()
    watchlist._state = None
    watchlist._resolved_set = frozenset()


def _watch(*entities):
    watchlist.set_selection([], list(entities), {})


def _rows(*pairs):
    return [{"ts": ts, "state": state} for ts, state in pairs]


def _local(y, mo, d, h=0, mi=0):
    return window.instant(date(y, mo, d), h, mi)


def _cfg(trigger, action=None, condition=None, **top):
    cfg = {"alias": "t", "trigger": trigger if isinstance(trigger, list) else [trigger],
           "action": action or [{"service": "light.turn_on", "target": {"entity_id": LIGHT}}]}
    if condition is not None:
        cfg["condition"] = condition
    cfg.update(top)
    return cfg


def _hhmm(markers):
    return [(m["day"], m["minute"], m["action"], m["entity"]) for m in markers]


# ---------------------------------------------------------------------------
# request: the shape of what the model asked for
# ---------------------------------------------------------------------------

class TestRequest:
    def _req(self, **spec):
        base = {"type": "timeline", "entities": [LIGHT], "start": "2026-09-21 00:00:00",
                "end": "now"}
        return charts.request({**base, **spec}, T0 + 5 * DAY)

    def test_a_good_spec_is_normalised_with_now_frozen(self):
        spec, why = self._req()
        assert why is None
        assert spec == {"type": "timeline", "entities": [LIGHT], "state": None,
                        "start": T0, "end": T0 + 5 * DAY}

    def test_an_already_normalised_spec_is_read_as_is(self):
        spec, why = charts.request({"type": "timeline", "entities": [LIGHT],
                                    "start": T0, "end": T0 + 3 * DAY}, 0)
        assert why is None and (spec["start"], spec["end"]) == (T0, T0 + 3 * DAY)

    @pytest.mark.parametrize("spec, why", [
        ({"type": "pie"}, "not a chart type"),
        ({"entities": LIGHT}, "not a list"),
        ({"entities": [LIGHT, LIGHT]}, "named twice"),
        ({"entities": [MOTION, LIGHT, FAN, "light.four"]}, "draws 1-3 entities"),
        ({"type": "daily_count", "state": None}, "needs the state"),
        ({"type": "heatmap", "state": "  "}, "needs the state"),
        ({"start": "last week"}, "local 'YYYY-MM-DD HH:MM:SS'"),
        ({"start": True}, "local 'YYYY-MM-DD HH:MM:SS'"),
        ({"start": "2026-09-30 00:00:00"}, "ends before it starts"),
    ])
    def test_what_cannot_be_drawn_says_why_and_keeps_the_known_keys(self, spec, why):
        known, error = self._req(**spec, colour="red")
        assert why in error
        assert set(known) <= {"type", "entities", "state", "start", "end"}


# ---------------------------------------------------------------------------
# clamp: windows are clamped, not refused (field run 2)
# ---------------------------------------------------------------------------

class TestClamp:
    NOW = T0 + 100 * DAY

    def _spec(self, kind, days, end=None, entities=(MOTION, LIGHT)):
        end = self.NOW if end is None else end
        n = charts.CHART_RULES[kind][1]
        return {"type": kind, "entities": list(entities)[:n], "state": "on",
                "start": end - int(days * DAY), "end": end}

    @pytest.mark.parametrize("kind", sorted(charts.CHART_RULES))
    def test_each_type_at_its_bounds_is_drawn_as_asked(self, kind):
        _, _, _, min_days, max_days = charts.CHART_RULES[kind]
        for days in (min_days, max_days):
            spec = self._spec(kind, days)
            drawn, clamped, why = charts.clamp(spec, self.NOW, 0)
            assert why is None and clamped is None and drawn == spec

    @pytest.mark.parametrize("hours", [-1, 1])
    def test_an_hour_either_side_is_a_dst_day_not_a_clamp(self, hours):
        spec = self._spec("timeline", 21 + hours / 24)
        assert charts.clamp(spec, self.NOW, 0)[1] is None
        spec = self._spec("timeline", 3 - hours / 24)
        assert charts.clamp(spec, self.NOW, 0)[1] is None

    def test_field_run_2s_overlay_of_7_6_days_is_drawn_over_the_last_7(self):
        spec = self._spec("overlay", 7.6)
        drawn, clamped, why = charts.clamp(spec, self.NOW, 0)
        assert why is None
        assert (drawn["start"], drawn["end"]) == (self.NOW - 7 * DAY, self.NOW)
        assert clamped["asked"] == {"start": spec["start"], "end": spec["end"]}
        assert clamped["drawn"] == {"start": self.NOW - 7 * DAY, "end": self.NOW}
        assert "at most 7 days" in clamped["why"][0]
        # Printed on the card as it stands; the field run's card read "a overlay".
        assert clamped["why"][0] == "an overlay covers at most 7 days"

    def test_a_window_too_short_is_widened_back_from_its_end(self):
        spec = self._spec("daily_count", 2, end=self.NOW - DAY)
        drawn, clamped, _ = charts.clamp(spec, self.NOW, 0)
        assert (drawn["start"], drawn["end"]) == (self.NOW - 8 * DAY, self.NOW - DAY)
        assert "at least 7 days" in clamped["why"][0]

    def test_a_window_before_the_record_starts_where_the_record_does(self):
        first = self.NOW - 5 * DAY
        drawn, clamped, _ = charts.clamp(self._spec("timeline", 14), self.NOW, first)
        assert drawn["start"] == first
        assert clamped["drawn"]["start"] == first
        assert "does not reach back" in clamped["why"][-1]

    def test_a_window_ending_in_the_future_ends_now(self):
        spec = self._spec("timeline", 5, end=self.NOW + 2 * DAY)
        drawn, clamped, _ = charts.clamp(spec, self.NOW, 0)
        assert drawn["end"] == self.NOW and "future" in clamped["why"][0]

    def test_only_an_empty_window_is_refused(self):
        drawn, clamped, why = charts.clamp(self._spec("timeline", 5), self.NOW, self.NOW)
        assert drawn is None and "nothing in that window" in why


# ---------------------------------------------------------------------------
# series from literal rows
# ---------------------------------------------------------------------------

class TestTimelineSeries:
    def test_runs_are_clipped_to_the_window_and_cut_per_local_day(self):
        rows = _rows((T0 + 20 * HOUR, "on"), (T0 + DAY + 2 * HOUR, "off"))
        start, end = T0 + 12 * HOUR, T0 + 2 * DAY + 6 * HOUR
        runs = charts.gapped_runs(rows, start, end, state_before="off")
        s = charts.series("timeline", {LIGHT: runs}, start, end)
        assert [d["date"] for d in s["days"]] == ["2026-09-21", "2026-09-22", "2026-09-23"]
        first, second, third = s["days"]
        assert first["span"] == [720, 1440] and first["minutes"] == 1440
        assert first["entities"][LIGHT] == [[720, 1200, "off"], [1200, 1440, "on"]]
        assert second["entities"][LIGHT] == [[0, 120, "on"], [120, 1440, "off"]]
        assert third["span"] == [0, 360] and third["entities"][LIGHT] == [[0, 360, "off"]]
        assert s["truncated"] is False

    def test_a_short_blip_is_not_rounded_away(self):
        rows = _rows((T0 + HOUR, "on"), (T0 + HOUR + 30, "off"))
        runs = charts.gapped_runs(rows, T0, T0 + DAY, "off")
        (day,) = charts.series("timeline", {MOTION: runs}, T0, T0 + DAY)["days"]
        assert [60, 60.5, "on"] in day["entities"][MOTION]

    def test_overlay_draws_both_entities_on_each_day(self):
        runs = {MOTION: charts.gapped_runs(_rows((T0 + HOUR, "on")), T0, T0 + DAY, "off"),
                LIGHT: charts.gapped_runs(_rows((T0 + HOUR + 30, "on")), T0, T0 + DAY, "off")}
        (day,) = charts.series("overlay", runs, T0, T0 + DAY)["days"]
        assert day["entities"][MOTION] == [[0, 60, "off"], [60, 1440, "on"]]
        assert day["entities"][LIGHT] == [[0, 60.5, "off"], [60.5, 1440, "on"]]

    def test_a_23_hour_day_draws_as_23_hours(self, monkeypatch):
        _tz(monkeypatch, "Europe/Bratislava")
        # 2026-03-29: 02:00 CET -> 03:00 CEST. On 01:00 -> off 04:00 is two
        # real hours, so the run ends at elapsed minute 180, not 240.
        start, end = _local(2026, 3, 28), _local(2026, 3, 31)
        rows = _rows((_local(2026, 3, 29, 1), "on"), (_local(2026, 3, 29, 4), "off"))
        s = charts.series("timeline", {LIGHT: charts.gapped_runs(rows, start, end, "off")},
                          start, end)
        day = s["days"][1]
        assert day["date"] == "2026-03-29" and day["minutes"] == 1380
        assert day["entities"][LIGHT] == [[0, 60, "off"], [60, 180, "on"], [180, 1380, "off"]]
        assert [d["minutes"] for d in s["days"]] == [1440, 1380, 1440]

    def test_a_25_hour_day_draws_as_25_hours(self, monkeypatch):
        _tz(monkeypatch, "Europe/Bratislava")
        # 2026-10-25: 03:00 CEST -> 02:00 CET. 01:00 -> 04:00 is four hours.
        start, end = _local(2026, 10, 24), _local(2026, 10, 27)
        rows = _rows((_local(2026, 10, 25, 1), "on"), (_local(2026, 10, 25, 4), "off"))
        s = charts.series("timeline", {LIGHT: charts.gapped_runs(rows, start, end, "off")},
                          start, end)
        day = s["days"][1]
        assert day["minutes"] == 1500
        assert day["entities"][LIGHT] == [[0, 60, "off"], [60, 300, "on"], [300, 1500, "off"]]


class TestGaps:
    """Field run 2: the scenario light sat `on`, then HA restarted it into
    unknown/unavailable for days. Those stretches are "no data", never "on"."""

    ROWS = _rows((T0 + HOUR, "on"), (T0 + 2 * HOUR, "unavailable"),
                 (T0 + 3 * HOUR, "unknown"), (T0 + 5 * HOUR, "on"), (T0 + 6 * HOUR, "off"))

    def test_unknown_and_unavailable_are_one_gap_never_merged_into_on(self):
        runs = charts.gapped_runs(self.ROWS, T0, T0 + DAY, "off")
        assert runs == [(T0, T0 + HOUR, "off"), (T0 + HOUR, T0 + 2 * HOUR, "on"),
                        (T0 + 2 * HOUR, T0 + 5 * HOUR, None),
                        (T0 + 5 * HOUR, T0 + 6 * HOUR, "on"), (T0 + 6 * HOUR, T0 + DAY, "off")]

    def test_a_window_opening_in_a_gap_opens_with_a_gap(self):
        runs = charts.gapped_runs(_rows((T0 + HOUR, "on")), T0, T0 + DAY, "unavailable")
        assert runs[0] == (T0, T0 + HOUR, None)

    def test_before_the_first_row_is_no_data(self):
        runs = charts.gapped_runs(_rows((T0 + HOUR, "on")), T0, T0 + DAY)
        assert runs == [(T0, T0 + HOUR, None), (T0 + HOUR, T0 + DAY, "on")]

    def test_no_rows_at_all_is_one_gap(self):
        assert charts.gapped_runs([], T0, T0 + DAY) == [(T0, T0 + DAY, None)]

    def test_the_daily_count_does_not_count_a_return_from_a_gap(self):
        runs = charts.gapped_runs(self.ROWS, T0, T0 + 7 * DAY, "off")
        s = charts.series("daily_count", {LIGHT: runs}, T0, T0 + 7 * DAY, "on")
        assert s["days"][0] == {"date": "2026-09-21", "count": 1, "no_data_minutes": 180}
        assert [d["count"] for d in s["days"][1:]] == [0] * 6

    def test_the_heatmap_does_not_count_a_gap_either(self):
        runs = charts.gapped_runs(self.ROWS, T0, T0 + 14 * DAY, "off")
        s = charts.series("heatmap", {LIGHT: runs}, T0, T0 + 14 * DAY, "unavailable")
        assert s["changes"] == 0


class TestCountSeries:
    def test_heatmap_counts_changes_into_the_state_by_local_weekday_and_hour(self):
        pairs = []
        for week in range(2):
            monday = T0 + week * 7 * DAY
            pairs += [(monday + 7 * HOUR, "on"), (monday + 8 * HOUR, "off"),
                      (monday + 5 * DAY + 19 * HOUR, "on"), (monday + 5 * DAY + 20 * HOUR, "off")]
        runs = charts.gapped_runs(_rows(*pairs), T0, T0 + 14 * DAY, "off")
        s = charts.series("heatmap", {LIGHT: runs}, T0, T0 + 14 * DAY, "on")
        assert s["counts"][0][7] == 2 and s["counts"][5][19] == 2
        assert s["changes"] == 4
        assert s["days_per_weekday"] == [2] * 7
        assert len(s["counts"]) == 7 and all(len(row) == 24 for row in s["counts"])

    def test_heatmap_hours_are_local(self, monkeypatch):
        _tz(monkeypatch, "Europe/Bratislava")
        start = _local(2026, 9, 7)
        runs = charts.gapped_runs(_rows((_local(2026, 9, 8, 7, 15), "on")),
                                  start, start + 14 * DAY, "off")
        s = charts.series("heatmap", {LIGHT: runs}, start, start + 14 * DAY, "on")
        assert s["counts"][1][7] == 1        # Tuesday 07:xx local, 05:xx UTC

    def test_daily_count_per_local_day(self):
        pairs = [(T0 + d * DAY + 7 * HOUR, "on") for d in range(7)]
        pairs += [(T0 + d * DAY + 9 * HOUR, "off") for d in range(7)]
        runs = charts.gapped_runs(_rows(*sorted(pairs)), T0, T0 + 7 * DAY, "off")
        s = charts.series("daily_count", {LIGHT: runs}, T0, T0 + 7 * DAY, "on")
        assert [d["count"] for d in s["days"]] == [1] * 7
        assert all(d["no_data_minutes"] == 0 for d in s["days"])


class TestCaps:
    def _busy(self, days):
        pairs = []
        for d in range(days):
            for h in range(24):
                pairs.append((T0 + d * DAY + h * HOUR, "on" if h % 2 else "off"))
        return charts.gapped_runs(_rows(*pairs), T0, T0 + days * DAY)

    def test_past_the_segment_cap_the_oldest_days_go_and_it_says_so(self, monkeypatch):
        monkeypatch.setattr(charts, "SERIES_MAX_SEGMENTS", 100)
        s = charts.series("timeline", {LIGHT: self._busy(10)}, T0, T0 + 10 * DAY)
        assert s["truncated"] is True and s["days_dropped"] == 6
        assert s["days"][-1]["date"] == "2026-09-30", "the most recent days are kept"
        assert sum(len(d["entities"][LIGHT]) for d in s["days"]) <= 100

    def test_past_the_byte_cap_the_oldest_days_go(self, monkeypatch):
        monkeypatch.setattr(charts, "CHART_MAX_BYTES", 2000)
        s = charts.series("timeline", {LIGHT: self._busy(10)}, T0, T0 + 10 * DAY)
        assert s["truncated"] is True and charts._size(s) <= 2000

    def test_markers_past_their_cap_keep_the_most_recent(self, monkeypatch):
        monkeypatch.setattr(charts, "MARKERS_MAX", 3)
        cfg = _cfg({"platform": "time", "at": "12:00:00"})
        runs = {LIGHT: [(T0 - DAY, T0 + 10 * DAY, "off")]}
        marks, note = charts.markers(cfg, runs, (T0, T0 + 10 * DAY))
        assert [m["day"] for m in marks] == ["2026-09-28", "2026-09-29", "2026-09-30"]
        assert "most recent 3 of 10" in note


# ---------------------------------------------------------------------------
# markers: the supported subset, under Home Assistant's semantics
# ---------------------------------------------------------------------------

def _motion(*pairs, before="off", end=T0 + DAY):
    """RAW runs for the motion sensor from (ts, state) pairs."""
    from core import timeline
    return timeline.runs(_rows(*pairs), T0 - DAY, end, before)


LIGHT_OFF = [(T0 - DAY, T0 + 2 * DAY, "off")]
LIGHT_ON = [(T0 - DAY, T0 + 2 * DAY, "on")]
WINDOW = (T0, T0 + DAY)


class TestStateTrigger:
    def test_it_fires_at_the_transition(self):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "on"})
        runs = {MOTION: _motion((T0 + 7 * HOUR, "on"), (T0 + 7 * HOUR + 5 * MIN, "off")),
                LIGHT: LIGHT_OFF}
        marks, note = charts.markers(cfg, runs, WINDOW, charted=[MOTION, LIGHT])
        assert _hhmm(marks) == [("2026-09-21", 420, "turn_on", LIGHT)] and note is None

    def test_newer_trigger_key_is_read_like_platform(self):
        cfg = _cfg({"trigger": "state", "entity_id": MOTION, "to": "on"})
        runs = {MOTION: _motion((T0 + 7 * HOUR, "on")), LIGHT: LIGHT_OFF}
        assert len(charts.markers(cfg, runs, WINDOW)[0]) == 1

    def test_from_and_to_filter_the_transition(self):
        runs = {MOTION: _motion((T0 + HOUR, "on"), (T0 + 2 * HOUR, "off"),
                                (T0 + 3 * HOUR, "unavailable"), (T0 + 4 * HOUR, "off")),
                LIGHT: LIGHT_ON}
        to_off = _cfg({"platform": "state", "entity_id": MOTION, "to": "off"})
        assert [m["minute"] for m in charts.markers(to_off, runs, WINDOW)[0]] == [120, 240]
        on_to_off = _cfg({"platform": "state", "entity_id": MOTION, "from": "on", "to": "off"})
        assert [m["minute"] for m in charts.markers(on_to_off, runs, WINDOW)[0]] == [120]
        from_list = _cfg({"platform": "state", "entity_id": MOTION,
                          "from": ["on", "unavailable"]})
        assert [m["minute"] for m in charts.markers(from_list, runs, WINDOW)[0]] == [120, 240]

    def test_a_list_of_entities_fires_on_each(self):
        cfg = _cfg({"platform": "state", "entity_id": [MOTION, FAN], "to": "on"})
        runs = {MOTION: _motion((T0 + HOUR, "on")), FAN: _motion((T0 + 2 * HOUR, "on")),
                LIGHT: LIGHT_OFF}
        assert [m["minute"] for m in charts.markers(cfg, runs, WINDOW, [LIGHT])[0]] == [60, 120]

    @pytest.mark.parametrize("hold", ["00:30:00", "00:30", {"minutes": 30}, 1800,
                                      {"seconds": 1800}])
    def test_for_fires_only_after_the_state_held(self, hold):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "off", "for": hold})
        # off at 07:05, held to 08:00 -> fires 07:35. off at 08:10, back on at
        # 08:20 -> interrupted, no fire.
        runs = {MOTION: _motion((T0 + 7 * HOUR, "on"), (T0 + 7 * HOUR + 5 * MIN, "off"),
                                (T0 + 8 * HOUR, "on"), (T0 + 8 * HOUR + 10 * MIN, "off"),
                                (T0 + 8 * HOUR + 20 * MIN, "on"), before="on"),
                LIGHT: LIGHT_ON}
        marks, _ = charts.markers(cfg, runs, WINDOW)
        assert [m["minute"] for m in marks] == [455]

    def test_a_for_begun_before_the_window_fires_inside_it(self):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "off", "for": "00:30:00"})
        runs = {MOTION: _motion((T0 - 10 * MIN, "off"), before="on"), LIGHT: LIGHT_ON}
        assert [m["minute"] for m in charts.markers(cfg, runs, WINDOW)[0]] == [20]

    def test_a_for_the_window_ends_inside_has_not_been_seen_to_hold(self):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "off", "for": "00:30:00"})
        runs = {MOTION: _motion((T0 + DAY - 10 * MIN, "off"), before="on"), LIGHT: LIGHT_ON}
        assert charts.markers(cfg, runs, WINDOW)[0] == []

    def test_a_change_to_another_state_during_for_interrupts_it(self):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "off", "for": "00:30:00"})
        runs = {MOTION: _motion((T0 + HOUR, "off"), (T0 + HOUR + 10 * MIN, "unavailable"),
                                before="on"),
                LIGHT: LIGHT_ON}
        assert charts.markers(cfg, runs, WINDOW)[0] == []

    def test_the_first_run_is_not_a_transition(self):
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "on"})
        runs = {MOTION: [(T0, T0 + DAY, "on")], LIGHT: LIGHT_OFF}
        assert charts.markers(cfg, runs, WINDOW)[0] == []

    def test_two_triggers_in_the_same_second_draw_once(self):
        cfg = _cfg([{"platform": "state", "entity_id": MOTION, "to": "on"},
                    {"platform": "state", "entity_id": FAN, "to": "on"}])
        runs = {MOTION: _motion((T0 + HOUR, "on")), FAN: _motion((T0 + HOUR, "on")),
                LIGHT: LIGHT_OFF}
        assert len(charts.markers(cfg, runs, WINDOW, [LIGHT])[0]) == 1


class TestTimeTrigger:
    def test_daily_at_the_local_time(self):
        cfg = _cfg({"platform": "time", "at": "19:00"})
        marks, _ = charts.markers(cfg, {LIGHT: LIGHT_OFF}, (T0, T0 + 2 * DAY))
        assert _hhmm(marks) == [("2026-09-21", 1140, "turn_on", LIGHT),
                                ("2026-09-22", 1140, "turn_on", LIGHT)]

    def test_a_list_of_times(self):
        cfg = _cfg({"platform": "time", "at": ["07:00:00", "19:00:00"]})
        assert [m["minute"] for m in charts.markers(cfg, {LIGHT: LIGHT_OFF}, WINDOW)[0]] == \
            [420, 1140]

    def test_a_skipped_time_does_not_fire_that_day(self, monkeypatch):
        _tz(monkeypatch, "Europe/Bratislava")
        start, end = _local(2026, 3, 28), _local(2026, 3, 31)
        cfg = _cfg({"platform": "time", "at": "02:30:00"})
        marks, _ = charts.markers(cfg, {LIGHT: [(start - DAY, end, "off")]}, (start, end))
        assert [(m["day"], m["minute"]) for m in marks] == [("2026-03-28", 150),
                                                             ("2026-03-30", 150)]

    def test_an_ambiguous_time_fires_once_in_the_first_fold(self, monkeypatch):
        _tz(monkeypatch, "Europe/Bratislava")
        start, end = _local(2026, 10, 24), _local(2026, 10, 27)
        cfg = _cfg({"platform": "time", "at": "02:30:00"})
        marks, _ = charts.markers(cfg, {LIGHT: [(start - DAY, end, "off")]}, (start, end))
        # 02:30 CEST on the 25th is 150 elapsed minutes; the CET one would be 210.
        assert [(m["day"], m["minute"]) for m in marks] == [
            ("2026-10-24", 150), ("2026-10-25", 150), ("2026-10-26", 150)]


class TestConditions:
    def test_a_state_condition_reads_the_state_at_fire_time(self):
        cfg = _cfg({"platform": "time", "at": ["06:00", "12:00"]},
                   action=[{"service": "light.turn_off", "target": {"entity_id": LIGHT}}],
                   condition=[{"condition": "state", "entity_id": LIGHT, "state": "on"}])
        light = [(T0 - DAY, T0 + 10 * HOUR, "on"), (T0 + 10 * HOUR, T0 + DAY, "off")]
        assert [m["minute"] for m in charts.markers(cfg, {LIGHT: light}, WINDOW)[0]] == [360]

    def test_a_state_condition_with_a_list_of_states(self):
        cfg = _cfg({"platform": "time", "at": "12:00"},
                   condition=[{"condition": "state", "entity_id": LIGHT,
                               "state": ["on", "off"]}])
        assert len(charts.markers(cfg, {LIGHT: LIGHT_OFF}, WINDOW)[0]) == 1

    @pytest.mark.parametrize("at, fires", [("23:00", True), ("01:00", True),
                                           ("12:00", False), ("06:00", False),
                                           ("22:00", True)])
    def test_a_time_window_crossing_midnight(self, at, fires):
        cfg = _cfg({"platform": "time", "at": at},
                   condition=[{"condition": "time", "after": "22:00:00", "before": "06:00"}])
        assert bool(charts.markers(cfg, {LIGHT: LIGHT_OFF}, WINDOW)[0]) == fires

    @pytest.mark.parametrize("after, before, fires", [("08:00", "18:00", True),
                                                      ("13:00", None, False),
                                                      (None, "12:30", True)])
    def test_a_same_day_time_window(self, after, before, fires):
        condition = {"condition": "time"}
        if after:
            condition["after"] = after
        if before:
            condition["before"] = before
        cfg = _cfg({"platform": "time", "at": "12:00"}, condition=[condition])
        assert bool(charts.markers(cfg, {LIGHT: LIGHT_OFF}, WINDOW)[0]) == fires

    def test_the_weekday_is_the_day_of_the_fire_not_of_the_window(self):
        # Sat 2026-09-26. An overnight window with weekday sat: Saturday 23:00
        # and Saturday 01:00 pass; Sunday 01:00 does not (HA checks today).
        saturday = T0 + 5 * DAY
        cfg = _cfg({"platform": "time", "at": ["01:00", "23:00"]},
                   condition=[{"condition": "time", "after": "22:00", "before": "06:00",
                               "weekday": ["sat"]}])
        marks, _ = charts.markers(cfg, {LIGHT: [(T0, T0 + 9 * DAY, "off")]},
                                  (saturday, saturday + 2 * DAY))
        assert [(m["day"], m["minute"]) for m in marks] == [("2026-09-26", 60),
                                                             ("2026-09-26", 1380)]

    def test_and_of_conditions(self):
        cfg = _cfg({"platform": "time", "at": ["07:00", "20:00"]},
                   condition=[{"condition": "and", "conditions": [
                       {"condition": "time", "after": "06:00", "before": "23:00"},
                       {"condition": "state", "entity_id": MOTION, "state": "on"}]}])
        motion = [(T0 - DAY, T0 + 19 * HOUR, "off"), (T0 + 19 * HOUR, T0 + DAY, "on")]
        marks, _ = charts.markers(cfg, {LIGHT: LIGHT_OFF, MOTION: motion}, WINDOW, [LIGHT])
        assert [m["minute"] for m in marks] == [1200]

    def test_a_condition_the_record_cannot_judge_draws_nothing_and_says_so(self):
        cfg = _cfg({"platform": "time", "at": "12:00"},
                   condition=[{"condition": "state", "entity_id": MOTION, "state": "on"}])
        marks, note = charts.markers(cfg, {LIGHT: LIGHT_OFF, MOTION: []}, WINDOW, [LIGHT])
        assert marks == [] and "1 time(s)" in note and "could not be judged" in note


class TestActions:
    @pytest.mark.parametrize("state, action", [("on", "turn_off"), ("off", "turn_on"),
                                               ("unknown", "turn_on")])
    def test_a_toggle_resolves_from_the_targets_state(self, state, action):
        cfg = _cfg({"platform": "time", "at": "12:00"},
                   action=[{"service": "light.toggle", "target": {"entity_id": LIGHT}}])
        marks, _ = charts.markers(cfg, {LIGHT: [(T0, T0 + DAY, state)]}, WINDOW)
        assert [m["action"] for m in marks] == [action]

    def test_an_unavailable_target_is_skipped_as_ha_skips_it(self):
        cfg = _cfg({"platform": "time", "at": "12:00"})
        assert charts.markers(cfg, {LIGHT: [(T0, T0 + DAY, "unavailable")]}, WINDOW)[0] == []

    def test_a_marker_per_charted_target_and_the_action_spelling(self):
        cfg = _cfg({"platform": "time", "at": "12:00"},
                   action=[{"action": "switch.turn_off",
                            "target": {"entity_id": ["switch.a", "switch.b"]}},
                           {"service": "fan.turn_on", "target": {"entity_id": FAN},
                            "data": {"percentage": 50}}])
        runs = {"switch.a": LIGHT_ON, "switch.b": LIGHT_ON, FAN: LIGHT_OFF}
        marks, _ = charts.markers(cfg, runs, WINDOW, charted=["switch.a", FAN])
        assert [(m["action"], m["entity"]) for m in marks] == [("turn_off", "switch.a"),
                                                               ("turn_on", FAN)]

    def test_a_rule_acting_on_nothing_charted_draws_no_markers_and_says_so(self):
        cfg = _cfg({"platform": "time", "at": "12:00"})
        marks, note = charts.markers(cfg, {MOTION: LIGHT_OFF}, WINDOW, charted=[MOTION])
        assert marks == [] and "acts on light.kitchen, which this chart does not draw" in note


class TestUnsupported:
    """Anything outside the subset: no markers at all, and a note naming the
    first part — never a partial drawing."""

    TIME = {"platform": "time", "at": "12:00"}

    @pytest.mark.parametrize("cfg, named", [
        (_cfg({"platform": "sun", "event": "sunset"}), "a sun trigger"),
        (_cfg({"platform": "device", "device_id": "x"}), "a device trigger"),
        (_cfg({"platform": "numeric_state", "entity_id": MOTION, "below": 3}),
         "a numeric_state trigger"),
        (_cfg({"platform": "state", "entity_id": MOTION}), "neither `to` nor `from`"),
        (_cfg({"platform": "state", "entity_id": MOTION, "from": "on", "for": 60}),
         "`for` and `from` but no `to`"),
        (_cfg({"platform": "state", "entity_id": MOTION, "to": "on", "attribute": "x"}),
         "`attribute` in a state trigger"),
        (_cfg({"platform": "state", "entity_id": MOTION, "to": "on", "for": "{{ 5 }}"}),
         "a template"),
        (_cfg({"platform": "state", "entity_id": MOTION, "to": None}), "`to`"),
        (_cfg({"platform": "time", "at": "input_datetime.wake"}), "an entity's time"),
        (_cfg({"platform": "time", "at": "7 pm"}), "not HH:MM"),
        (_cfg(TIME, condition=[{"condition": "numeric_state", "entity_id": MOTION,
                                "below": 1}]), "a numeric_state condition"),
        (_cfg(TIME, condition=[{"condition": "or", "conditions": []}]), "an or condition"),
        (_cfg(TIME, condition=[{"condition": "sun", "after": "sunset"}]), "a sun condition"),
        (_cfg(TIME, condition=["{{ is_state('light.kitchen', 'on') }}"]), "a template"),
        (_cfg(TIME, condition=[{"condition": "state", "entity_id": LIGHT, "state": "on",
                                "for": "00:05:00"}]), "`for` in a state condition"),
        (_cfg(TIME, condition=[{"condition": "time", "after": "input_datetime.x"}]),
         "an entity's time"),
        (_cfg(TIME, action=[{"delay": "00:05:00"}]), "a delay action"),
        (_cfg(TIME, action=[{"choose": []}]), "a choose action"),
        (_cfg(TIME, action=[{"service": "notify.mobile_app_phone"}]),
         "the service notify.mobile_app_phone"),
        (_cfg(TIME, action=[{"service": "light.turn_on", "target": {"area_id": "kitchen"}}]),
         "without target.entity_id"),
        (_cfg(TIME, action=[{"service": "light.turn_on", "entity_id": LIGHT}]),
         "`entity_id` in an action"),
        (_cfg(TIME, action=[{"service": "light.turn_on", "target": {"entity_id": LIGHT},
                             "data": {"brightness": "{{ 200 }}"}}]), "a template"),
        (_cfg(TIME, mode="queued"), "mode queued"),
        (_cfg(TIME, variables={"x": 1}), "`variables` in the automation"),
    ])
    def test_no_markers_and_a_note_naming_the_part(self, cfg, named):
        marks, note = charts.markers(cfg, {LIGHT: LIGHT_OFF, MOTION: LIGHT_OFF}, WINDOW)
        assert marks is None
        assert note.startswith(charts.CANNOT_DRAW_RULE)
        assert named in note

    def test_plural_keys_are_read_as_ha_reads_them(self):
        cfg = {"alias": "t", "triggers": [self.TIME],
               "actions": [{"service": "light.turn_on", "target": {"entity_id": LIGHT}}]}
        assert len(charts.markers(cfg, {LIGHT: LIGHT_OFF}, WINDOW)[0]) == 1


# ---------------------------------------------------------------------------
# draw: from the record, clamps and gaps included
# ---------------------------------------------------------------------------

NOW = T0 + 10 * DAY


def _record(entity, *pairs):
    for ts, state in pairs:
        record.record_transition(entity, ts, state)


class TestDraw:
    def _spec(self, **over):
        spec = {"type": "timeline", "entities": [LIGHT], "start": "2026-09-27 00:00:00",
                "end": "now"}
        spec.update(over)
        return spec

    def test_a_timeline_is_drawn_from_the_record(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0, "off"), (T0 + 6 * DAY + 7 * HOUR, "on"),
                (T0 + 6 * DAY + 8 * HOUR, "off"))
        before = int(time.time())
        chart = charts.draw(self._spec(), None, NOW)
        assert chart["error"] is None and chart["window_clamped"] is None
        assert chart["spec"] == {"type": "timeline", "entities": [LIGHT], "state": None,
                                 "start": T0 + 6 * DAY, "end": NOW}
        first = chart["series"]["days"][0]
        assert first["entities"][LIGHT] == [[0, 420, "off"], [420, 480, "on"], [480, 1440, "off"]]
        assert chart["drawn_at"] >= before
        assert chart["markers"] is None and "no config" in chart["markers_note"]

    def test_a_row_exactly_at_the_window_start_opens_the_first_run(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0, "off"), (T0 + 6 * DAY, "on"))
        chart = charts.draw(self._spec(), None, NOW)
        assert chart["series"]["days"][0]["entities"][LIGHT][0] == [0, 1440, "on"]

    def test_before_the_record_it_is_clamped_to_first_ts(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0 + 2 * DAY, "off"), (T0 + 3 * DAY, "on"))
        chart = charts.draw(self._spec(start="2026-09-10 00:00:00"), None, NOW)
        assert chart["error"] is None
        assert chart["spec"]["start"] == T0 + 2 * DAY
        assert chart["window_clamped"]["drawn"]["start"] == T0 + 2 * DAY

    def test_too_long_is_clamped_to_the_most_recent_span(self, house):
        _watch(MOTION, LIGHT)
        _record(LIGHT, (T0 - 20 * DAY, "off"))
        _record(MOTION, (T0 - 20 * DAY, "off"))
        chart = charts.draw(self._spec(type="overlay", entities=[MOTION, LIGHT],
                                       start="2026-09-15 00:00:00"), None, NOW)
        assert chart["spec"]["start"] == NOW - 7 * DAY
        assert chart["window_clamped"]["asked"]["start"] == T0 - 6 * DAY

    def test_the_gap_is_drawn_as_no_data(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0, "on"), (T0 + 7 * DAY, "unavailable"), (T0 + 8 * DAY, "on"))
        chart = charts.draw(self._spec(), None, NOW)
        day = next(d for d in chart["series"]["days"] if d["date"] == "2026-09-28")
        assert day["entities"][LIGHT] == [[0, 1440, None]]

    def test_an_unwatched_entity_is_an_error(self, house):
        chart = charts.draw(self._spec(), None, NOW)
        assert "light.kitchen is not watched" in chart["error"]
        assert chart["series"] is None and chart["markers"] is None

    def test_an_entity_never_recorded_is_an_error(self, house):
        _watch(LIGHT)
        chart = charts.draw(self._spec(), None, NOW)
        assert "the record holds nothing for light.kitchen" in chart["error"]

    def test_a_record_starting_after_the_window_is_an_error(self, house):
        _watch(LIGHT)
        _record(LIGHT, (NOW + DAY, "on"))
        chart = charts.draw(self._spec(end="2026-09-30 00:00:00"), None, NOW + 2 * DAY)
        assert "nothing in that window" in chart["error"] and chart["series"] is None

    def test_a_window_with_no_rows_is_an_error(self, house, monkeypatch):
        # first_ts reaches back (a collapsed profile does that) but no live
        # row is in or before the window: there is nothing to draw.
        _watch(LIGHT)
        _record(LIGHT, (NOW + DAY, "on"))
        monkeypatch.setattr(record, "first_ts", lambda entity: T0 - 30 * DAY)
        chart = charts.draw(self._spec(), None, NOW)
        assert "no rows in that window" in chart["error"] and chart["series"] is None

    def test_markers_that_read_an_unwatched_entity_are_not_drawn(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0, "off"))
        cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "on"})
        chart = charts.draw(self._spec(), cfg, NOW)
        assert chart["markers"] is None
        assert "does not hold binary_sensor.motion, which it reads" in chart["markers_note"]

    def test_heatmap_and_daily_count_carry_no_markers(self, house):
        _watch(LIGHT)
        _record(LIGHT, (T0 - 20 * DAY, "off"), (T0 + HOUR, "on"))
        chart = charts.draw(self._spec(type="daily_count", state="on",
                                       start="2026-09-21 00:00:00"),
                            _cfg({"platform": "time", "at": "12:00"}), NOW)
        assert chart["error"] is None and chart["series"]["days"][0]["count"] == 1
        assert chart["markers"] is None and chart["markers_note"] is None

    def test_a_drawing_that_raises_is_an_error_not_an_exception(self, house, monkeypatch):
        _watch(LIGHT)
        _record(LIGHT, (T0, "off"))
        monkeypatch.setattr(charts, "series", lambda *a, **k: 1 / 0)
        chart = charts.draw(self._spec(), None, NOW)
        assert "could not be drawn" in chart["error"]


class TestGoldenKitchen:
    """Field run 2's kitchen idea (#15) over one literal day of motion and
    light, through `draw`: motion `off` for 30 minutes while the light is on
    turns it off."""

    def test_the_markers_are_where_the_rule_would_have_acted(self, house):
        _watch(KITCHEN_MOTION, KITCHEN_LIGHT)
        day = T0 + 5 * DAY
        at = lambda h, m=0: day + h * HOUR + m * MIN    # noqa: E731
        _record(KITCHEN_MOTION, (T0, "off"),
                (at(7), "on"), (at(7, 5), "off"),          # held -> 07:35, light on
                (at(8), "on"), (at(8, 10), "off"),         # held -> 08:40, light on
                (at(12), "on"), (at(12, 2), "off"),        # held -> 12:32, light OFF
                (at(18), "on"), (at(18, 20), "off"),       # back on after 20 min
                (at(18, 40), "on"), (at(18, 45), "off"),   # held -> 19:15, light on
                (at(21), "on"), (at(21, 10), "off"))       # held -> 21:40, light unavailable
        _record(KITCHEN_LIGHT, (T0, "off"),
                (at(7), "on"), (at(9), "off"),
                (at(18), "on"), (at(21, 30), "unavailable"))
        spec = {"type": "overlay", "entities": [KITCHEN_MOTION, KITCHEN_LIGHT],
                "start": "2026-09-26 00:00:00", "end": "2026-09-27 00:00:00"}
        chart = charts.draw(spec, KITCHEN_CONFIG, NOW)

        assert chart["error"] is None and chart["markers_note"] is None
        assert _hhmm(chart["markers"]) == [
            ("2026-09-26", 455, "turn_off", KITCHEN_LIGHT),
            ("2026-09-26", 520, "turn_off", KITCHEN_LIGHT),
            ("2026-09-26", 1155, "turn_off", KITCHEN_LIGHT),
        ]
        (drawn,) = chart["series"]["days"]
        assert drawn["entities"][KITCHEN_LIGHT][-1] == [1290, 1440, None]


# ---------------------------------------------------------------------------
# redraw: an old row gets the chart F-WP4 draws
# ---------------------------------------------------------------------------

OLD_CHART = {"spec": {"type": "overlay", "entities": [MOTION, LIGHT],
                      "start": "2026-09-22 00:00:00", "end": "now", "state": None},
             "series": None, "markers": None, "error": "a overlay covers 1-7 days, not 7.6"}


def _shown_row(chart=OLD_CHART):
    cfg = _cfg({"platform": "state", "entity_id": MOTION, "to": "on"})
    sid = suggestions.upsert_analyst_candidate("analyst:x", [MOTION, LIGHT], "Title",
                                               "A finding.", "asi1")
    assert suggestions.mark_shown(sid, cfg, "A finding.", "A rule.", chart, [])
    return sid


class TestRedraw:
    def test_an_old_row_is_drawn_again_in_place(self, house, monkeypatch):
        _watch(MOTION, LIGHT)
        _record(MOTION, (T0, "off"), (T0 + 3 * DAY + HOUR, "on"))
        _record(LIGHT, (T0, "off"))
        monkeypatch.setattr(suggestions, "_now", lambda: T0 + 4 * DAY)
        sid = _shown_row()
        before = suggestions.suggestion(sid)

        chart = charts.redraw(sid)

        after = suggestions.suggestion(sid)
        assert after["chart"] == chart and chart["error"] is None
        # "now" is the row's created_at: the window the model looked at.
        assert chart["spec"]["start"] == T0 + DAY and chart["spec"]["end"] == T0 + 4 * DAY
        assert _hhmm(chart["markers"]) == [("2026-09-24", 60, "turn_on", LIGHT)]
        assert after["state"] == before["state"] == "validated"
        assert after["updated_at"] == before["updated_at"], "a redraw is not a proposal"

    def test_a_second_redraw_keeps_the_window_the_model_asked_for(self, house, monkeypatch):
        # Field test of #15: the first redraw stored the clamped window as
        # the spec, and a second drew that again — clamping nothing, so the
        # card lost "the agent asked for …".
        _watch(MOTION, LIGHT)
        _record(MOTION, (T0, "off"))
        _record(LIGHT, (T0, "on"))
        monkeypatch.setattr(suggestions, "_now", lambda: T0 + 9 * DAY)
        sid = _shown_row({**OLD_CHART, "spec": {**OLD_CHART["spec"],
                                                "start": T0 + HOUR, "end": T0 + 9 * DAY}})

        first = charts.redraw(sid)
        second = charts.redraw(sid)

        for chart in (first, second):
            assert chart["window_clamped"]["asked"] == {"start": T0 + HOUR, "end": T0 + 9 * DAY}
            assert chart["window_clamped"]["why"] == ["an overlay covers at most 7 days"]
            assert (chart["spec"]["start"], chart["spec"]["end"]) == (T0 + 2 * DAY, T0 + 9 * DAY)

    def test_a_row_that_is_not_an_explorer_row_is_left_alone(self, house):
        sid = suggestions.upsert_candidate("standby:x", "standby", ["switch.x"])
        assert charts.redraw(sid) is None
        assert charts.redraw(999) is None

    def test_the_store_refuses_a_chart_without_a_spec_and_a_retired_row(self, house):
        _watch(MOTION, LIGHT)
        sid = _shown_row()
        assert suggestions.set_chart(sid, {"series": []}) is False
        assert suggestions.retire(sid, "gone")
        assert suggestions.set_chart(sid, OLD_CHART) is False


class TestRunAnalystRedraw:
    def _script(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "run_analyst.py")
        spec = importlib.util.spec_from_file_location("run_analyst_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_redraw_is_wired_and_calls_no_model(self, monkeypatch, capsys):
        module = self._script()
        asked = []
        drawn = {"spec": {"type": "overlay"}, "series": {"days": [1, 2]}, "markers": [],
                 "markers_note": None, "error": None, "drawn_at": 0,
                 "window_clamped": {"asked": {}, "drawn": {}, "why": ["x"]}}
        monkeypatch.setattr(charts, "redraw", lambda sid: asked.append(sid) or drawn)
        assert module.main(["--redraw", "15"]) == 0
        assert asked == [15]
        out = capsys.readouterr().out
        assert "#15: redrawn" in out and "window clamped" in out and "markers: 0" in out

    def test_redraw_of_a_row_that_is_not_one_fails(self, monkeypatch):
        module = self._script()
        monkeypatch.setattr(charts, "redraw", lambda sid: None)
        assert module.main(["--redraw", "15"]) == 1
