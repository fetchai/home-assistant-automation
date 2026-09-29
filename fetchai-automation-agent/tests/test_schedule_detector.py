"""The schedule detector's rules, against synthetic transition lists.

Same shape and same reason as tests/test_standby_detector.py: everything the
detector decides is decided by pure functions taking an ordered list of rows,
and the evidence floor is what makes that the only workable harness — a
candidate here needs eight occurrences of a weekday, which is eight weeks, and
nothing in this repo can plant that much history. As list literals it is a
loop.

The DST classes pin Europe/London rather than UTC, because a zone with no
clock change cannot exercise the two cases decision 3 is written about.
"""
from datetime import date, datetime, timedelta, timezone
import time

import pytest

from core import activity
from core import record
from core import suggestions
from core import watchlist
from core import window
from core.detectors import power
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant import registry


DAY = 86400
HEARTBEAT = 900  # observer.NUMERIC_MAX_GAP_SECONDS — the record's own floor

# Noon on a fixed day, so every cell below is arithmetic rather than a function
# of when the suite happens to run. 2026-06-30 is a Tuesday.
NOW = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
WINDOW_START = NOW - schedule.LOOKBACK_DAYS * DAY

SENSOR = "sensor.tv_power"
SWITCH = "switch.tv"

IDLE_WATTS = "12.0"
IN_USE = "120.0"

# The household the proposal opens with: out 07:00-17:00 on weekdays, up and
# about from six, in front of the television until eleven, and at home all
# weekend. What the detector should find in it is the daytime stretch, not the
# night — the night is standby.py's window and is only six hours here.
OFFICE_OUT = (7, 17)
WEEKDAY_USE = ((6, 7), (17, 23))
WEEKEND_USE = (8, 23)


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone.

    Cells are placed by LOCAL wall clock — deliberately, because a household's
    day is measured in one — so without this the tests would drift with the
    developer's. time.mktime's notion of local time comes from the C library,
    which tzset() changes and monkeypatch does not restore.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --------------------------------------------------------------------------
# building a series the record would plausibly have stored
# --------------------------------------------------------------------------
def _local(ts):
    return time.localtime(int(ts))


def _day_of(ts):
    return datetime.fromtimestamp(int(ts)).date()


def _office(ts) -> str:
    """The proposal's household, as a state callable."""
    hour = _local(ts).tm_hour
    if _day_of(ts).weekday() < 5:
        for low, high in WEEKDAY_USE:
            if low <= hour < high:
                return IN_USE
        return IDLE_WATTS
    return IN_USE if WEEKEND_USE[0] <= hour < WEEKEND_USE[1] else IDLE_WATTS


def _series(state=_office, source="live", start=WINDOW_START, end=NOW,
            step=HEARTBEAT, skip=None):
    """A heartbeat row every `step` seconds, the way NUMERIC_MAX_GAP_SECONDS
    leaves one on a value that never moves.

    `state` and `source` may be plain values or callables of the timestamp;
    `skip` is a predicate for rows to leave out, which is how a gap is made.
    """
    def value(fn, ts):
        return fn(ts) if callable(fn) else fn

    return [
        {"ts": ts, "state": str(value(state, ts)), "prev_state": None,
         "prev_duration": step, "source": value(source, ts)}
        for ts in range(int(start), int(end), step)
        if not (skip and skip(ts))
    ]


def _candidate(rows, switch=SWITCH, **kwargs):
    return schedule.find_candidate(SENSOR, rows, switch, now=NOW, **kwargs)


def _grid(rows, idle_below=13.1, start=WINDOW_START, end=NOW):
    return schedule.weekly_grid(schedule.intervals(rows, start, end, idle_below))


def _during(hours, weekdays=range(7)):
    """A predicate for a stretch of the local day on certain weekdays."""
    low, high = hours
    weekdays = set(weekdays)

    def inside(ts):
        return (_day_of(ts).weekday() in weekdays
                and low <= _local(ts).tm_hour < high)
    return inside


# --------------------------------------------------------------------------
class TestStageB:
    """`intervals` — the record condensed to stretches of one state, and the
    third state that is neither idle nor active."""

    def test_the_intervals_tile_the_window_end_to_end(self):
        """What lets a cell's verdict be read off the intervals it touches
        without also checking that they covered it."""
        series = schedule.intervals(_series(), WINDOW_START, NOW, 13.1)

        assert series[0].start == WINDOW_START
        assert series[-1].end == NOW
        assert all(a.end == b.start for a, b in zip(series, series[1:]))

    def test_a_flat_low_reading_is_idle_and_carries_its_watts(self):
        series = schedule.intervals(_series(state=IDLE_WATTS), WINDOW_START,
                                    WINDOW_START + 4 * 3600, 13.1)

        assert {interval.state for interval in series[:-1]} == {schedule.IDLE}
        assert series[0].watts == 12.0

    def test_a_reading_at_the_device_s_own_floor_is_in_use(self):
        series = schedule.intervals(_series(state=IN_USE), WINDOW_START,
                                    WINDOW_START + 4 * 3600, 13.1)

        assert series[0].state == schedule.ACTIVE

    def test_a_two_hour_gap_is_missing_coverage_not_a_flat_reading(self):
        """The trap this module exists around. Two hours of silence looks
        exactly like two hours of a value that never moved — and reading it
        that way would report a rock-solid idle run over a stretch where the
        agent was not watching at all."""
        hole_from = WINDOW_START + 3600
        hole_to = hole_from + 2 * 3600
        series = schedule.intervals(
            _series(state=IDLE_WATTS, end=WINDOW_START + 6 * 3600,
                    skip=lambda ts: hole_from <= ts < hole_to),
            WINDOW_START, WINDOW_START + 6 * 3600, 13.1)

        spanning = [i for i in series if i.start < hole_to and i.end > hole_from]
        assert [i.state for i in spanning] == [schedule.REASON_NO_COVERAGE]
        assert spanning[0].watts is None

    def test_a_gap_the_heartbeat_explains_is_not_a_gap(self):
        """One missed heartbeat is a restart, not a blind spot — otherwise
        every add-on update would disqualify half a day of cells."""
        series = schedule.intervals(
            _series(state=IDLE_WATTS, end=WINDOW_START + 6 * 3600,
                    step=schedule.COVERAGE_MAX_GAP_SECONDS),
            WINDOW_START, WINDOW_START + 6 * 3600, 13.1)

        assert {interval.state for interval in series[:-1]} == {schedule.IDLE}

    def test_a_statistics_row_is_not_evidence_of_anything_being_idle(self):
        """Bucket means cannot show that a value stayed under a floor — four
        minutes of use inside a five-minute bucket leaves the same mean — and
        "idle throughout" is the whole claim."""
        series = schedule.intervals(
            _series(state=IDLE_WATTS, source="statistics",
                    end=WINDOW_START + 4 * 3600),
            WINDOW_START, WINDOW_START + 4 * 3600, 13.1)

        assert series[0].state == schedule.REASON_STATISTICS
        assert series[0].watts is None

    def test_unavailable_is_not_zero_watts(self):
        series = schedule.intervals(
            _series(state="unavailable", end=WINDOW_START + 4 * 3600),
            WINDOW_START, WINDOW_START + 4 * 3600, 13.1)

        assert series[0].state == schedule.REASON_UNAVAILABLE

    def test_silence_after_an_unavailable_row_is_the_entity_not_the_agent(self):
        """Both silences look identical in the record — no rows — and they are
        different facts. observer.should_record writes 'unavailable' once and
        suppresses the repeats, so an entity offline for three hours leaves
        exactly one row; calling that "the agent was not looking" would put a
        falsehood in a blob a human reads."""
        gone_at = WINDOW_START + 3600
        rows = ([r for r in _series(state=IDLE_WATTS, end=gone_at)]
                + [{"ts": gone_at, "state": "unavailable", "prev_state": IDLE_WATTS,
                    "prev_duration": 900, "source": "live"}]
                + _series(state=IDLE_WATTS, start=gone_at + 5 * 3600,
                          end=gone_at + 6 * 3600))

        series = schedule.intervals(rows, WINDOW_START, gone_at + 6 * 3600, 13.1)

        assert [i for i in series if i.start == gone_at][0].state == \
            schedule.REASON_UNAVAILABLE

    def test_the_head_and_the_tail_have_nothing_to_be_read_against(self):
        """Between two rows the SECOND row is proof the agent was still
        watching across the gap. Before the first and after the last there is
        no such row, so there is nothing the silence can be read against."""
        series = schedule.intervals(
            _series(state=IDLE_WATTS, start=WINDOW_START + 2 * 3600,
                    end=WINDOW_START + 4 * 3600),
            WINDOW_START, WINDOW_START + 6 * 3600, 13.1)

        assert series[0].state == schedule.REASON_NO_COVERAGE
        assert series[0].start == WINDOW_START
        assert series[-1].state == schedule.REASON_NO_COVERAGE
        assert series[-1].end == WINDOW_START + 6 * 3600

    def test_a_window_with_no_rows_at_all_is_one_unobserved_stretch(self):
        series = schedule.intervals([], WINDOW_START, NOW, 13.1)

        assert series == [schedule.Interval(WINDOW_START, NOW,
                                            schedule.REASON_NO_COVERAGE, None)]


# --------------------------------------------------------------------------
class TestStageC:
    """`weekly_grid` — 7 x 48, and what makes a cell count."""

    def test_a_cell_counts_once_per_occurrence_and_is_idle_when_it_was(self):
        cells = _grid(_series()).cells

        # 07:30-08:00 on a Monday: idle every time it was seen, and seen about
        # as often as a ninety-day window offers a Monday.
        assert cells[(window.MONDAY, 15)]["idle"] == cells[(window.MONDAY, 15)]["observed"]
        assert cells[(window.MONDAY, 15)]["observed"] >= 12
        # ...and 19:30-20:00 on the same Monday never was.
        assert cells[(window.MONDAY, 39)]["idle"] == 0
        assert cells[(window.MONDAY, 39)]["observed"] >= 12

    def test_the_grid_is_always_the_full_336_cells(self):
        """Including the ones this record never reached, whose answer is zero
        observations rather than a KeyError."""
        cells = _grid(_series(start=NOW - 3 * DAY)).cells

        assert len(cells) == 7 * schedule.SLOTS_PER_DAY
        assert any(cell["observed"] == 0 for cell in cells.values())

    def test_a_cell_partly_unobserved_counts_as_neither_idle_nor_observed(self):
        """Decision 3: the whole-occurrence rule, asked about half an hour
        instead of about a night. A partly-covered cell is a shorter
        observation dressed up as a full one."""
        hole = _during((9, 11), weekdays=[window.MONDAY])
        cells = _grid(_series(skip=hole)).cells

        # 09:30-10:00, squarely inside the missing two hours.
        assert cells[(window.MONDAY, 19)] == {"idle": 0, "observed": 0}
        # ...and so is 08:30-09:00, which a reader would not have expected:
        # the silence begins at the LAST ROW, 08:45, not at the hour the hole
        # was punched in. That is the point — nothing says the agent kept
        # watching for the quarter hour after a row it then stopped writing.
        assert cells[(window.MONDAY, 17)] == {"idle": 0, "observed": 0}
        # The cell that ends before that last row is untouched, and so is the
        # one that begins where the rows resume.
        assert cells[(window.MONDAY, 16)]["observed"] >= 12
        assert cells[(window.MONDAY, 22)]["observed"] >= 12

    def test_a_statistics_row_disqualifies_the_cells_it_lands_in(self):
        stats = _during((12, 13), weekdays=[window.WEDNESDAY])
        cells = _grid(_series(source=lambda ts: "statistics" if stats(ts) else "live")).cells

        assert cells[(window.WEDNESDAY, 24)] == {"idle": 0, "observed": 0}
        assert cells[(window.WEDNESDAY, 26)]["observed"] >= 12

    def test_unavailable_disqualifies_the_cells_it_lands_in(self):
        gone = _during((12, 13), weekdays=[window.WEDNESDAY])
        cells = _grid(_series(
            state=lambda ts: "unavailable" if gone(ts) else _office(ts))).cells

        assert cells[(window.WEDNESDAY, 24)] == {"idle": 0, "observed": 0}
        assert cells[(window.WEDNESDAY, 26)]["observed"] >= 12

    def test_a_cell_is_placed_by_the_local_weekday_it_starts_on(self):
        """23:30-00:00 spans midnight the way a night does, and belongs to the
        evening it began in — core/window.py's convention, not a second one."""
        cells = _grid(_series()).cells
        last = schedule.SLOTS_PER_DAY - 1

        assert window.spans_midnight(schedule._CELL_SPECS[last])
        assert cells[(window.SATURDAY, last)]["observed"] >= 12


# --------------------------------------------------------------------------
class TestTheClockChanges:
    """Decision 3's two visible days. A zone with no clock change cannot
    exercise either, so these pin one that has both."""

    SPRING_FORWARD = date(2026, 3, 29)   # 01:00 -> 02:00; 01:00-01:59 never was
    FALL_BACK = date(2026, 10, 25)       # 02:00 -> 01:00; 01:00-01:59 twice

    @pytest.fixture(autouse=True)
    def london(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/London")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def _verdicts(self, rows, day, start, end):
        grid = schedule.weekly_grid(schedule.intervals(rows, start, end, 13.1))
        return dict(grid.dates[day.weekday()])[day]

    def test_a_half_hour_the_clock_skipped_is_not_an_observation(self):
        """Not "unobserved" — there is nothing to have observed. Any run
        containing one is simply not an occurrence on that date, which is the
        refusal window.windows already makes about a night an hour of which
        the clock ate."""
        start = int(time.mktime((2026, 3, 28, 0, 0, 0, 0, 0, -1)))
        end = int(time.mktime((2026, 3, 31, 0, 0, 0, 0, 0, -1)))

        verdicts = self._verdicts(_series(state=IDLE_WATTS, start=start, end=end),
                                  self.SPRING_FORWARD, start, end)

        # 00:30-01:00 loses its END, 01:00-01:30 and 01:30-02:00 their start.
        assert verdicts[1] is None
        assert verdicts[2] is None
        assert verdicts[3] is None
        # The half-hours either side of the skip happened and were idle.
        assert verdicts[0] == schedule.IDLE
        assert verdicts[4] == schedule.IDLE

    def test_the_hour_the_clock_repeats_lands_in_one_ninety_minute_cell(self):
        bounds = window.bounds(schedule._CELL_SPECS[3], self.FALL_BACK)

        assert bounds[1] - bounds[0] == 90 * 60

    @pytest.mark.parametrize("before", [(2026, 1, 15), (2026, 7, 15)],
                             ids=["after a winter-time call", "after a summer-time call"])
    def test_whatever_mktime_was_asked_before(self, before):
        """glibc resolves tm_isdst -1 in the repeated hour from the offset of
        the previous mktime call in the process, so the same cell measured 30
        or 90 minutes depending on which test ran first (CI, 2026-09-26).
        Priming each way here makes that visible on glibc; musl, which always
        picks the earlier fold, passes either way."""
        time.mktime(before + (12, 0, 0, 0, 0, -1))

        bounds = window.bounds(schedule._CELL_SPECS[3], self.FALL_BACK)

        assert bounds[1] - bounds[0] == 90 * 60

    def test_and_that_cell_has_to_be_idle_across_all_ninety_minutes(self):
        """One occurrence, measured strictly — the direction that costs the
        claim rather than flattering it."""
        start = int(time.mktime((2026, 10, 24, 0, 0, 0, 0, 0, -1)))
        end = int(time.mktime((2026, 10, 27, 0, 0, 0, 0, 0, -1)))
        cell_start, cell_end = window.bounds(schedule._CELL_SPECS[3], self.FALL_BACK)
        # Busy for twenty minutes of the SECOND pass through 01:00-02:00,
        # which a cell ending at the first pass's 02:00 would never have seen.
        busy_from = cell_start + 70 * 60

        rows = _series(
            state=lambda ts: IN_USE if busy_from <= ts < busy_from + 20 * 60
            else IDLE_WATTS,
            start=start, end=end)

        verdicts = self._verdicts(rows, self.FALL_BACK, start, end)

        assert busy_from < cell_end
        assert verdicts[3] == schedule.ACTIVE
        assert verdicts[2] == schedule.IDLE


# --------------------------------------------------------------------------
class TestStageD1:
    """`schedules` — maximal all-idle runs, and what is and is not merged."""

    def test_the_weekday_office_pattern_is_what_it_finds(self):
        """The proposal's opening example, end to end: a household out from
        seven to five on weekdays, and the ten-hour stretch the night detector
        cannot see at all."""
        best = schedule.schedules(_grid(_series()))[0]

        assert window.label(best["window"]) == "07:00-17:00 local Mon-Fri"
        assert best["window"].days == window.WEEKDAYS
        assert window.hours(best["window"]) == 10

    def test_the_night_stretch_is_found_too_and_ranks_below_it(self):
        """Two true windows on one device. The daytime one is longer and
        happens nearly as often, which is the whole argument of §1.1."""
        labels = [window.label(run["window"]) for run in schedule.schedules(_grid(_series()))]

        assert labels[0] == "07:00-17:00 local Mon-Fri"
        assert "00:00-06:00 local Mon-Fri" in labels
        assert "00:00-08:00 local Sat, Sun" in labels

    def test_a_run_does_not_wrap_midnight(self):
        """Decision 5. A device idle 22:00-06:00 every day is described as two
        true windows rather than one, because the grid is keyed by weekday and
        a wrapping run's cells live under two of them."""
        rows = _series(state=lambda ts: IN_USE if 6 <= _local(ts).tm_hour < 22
                       else IDLE_WATTS)

        labels = [window.label(run["window"]) for run in schedule.schedules(_grid(rows))]

        assert "00:00-06:00 local" in labels
        assert "22:00-00:00 local" in labels
        assert not any(label.startswith("22:00-06:00") for label in labels)

    def test_a_run_too_short_to_be_worth_switching_is_not_proposed(self):
        """One idle hour in the middle of the day is the gap BETWEEN two uses,
        not a part of the household's day — and a relay has a finite number of
        cycles."""
        rows = _series(state=lambda ts: IDLE_WATTS if 12 <= _local(ts).tm_hour < 13
                       else IN_USE)
        grid = _grid(rows)

        # The cells really do qualify; it is the LENGTH that refuses them.
        assert grid.cells[(window.MONDAY, 24)]["idle"] >= schedule.MIN_OCCURRENCES
        assert grid.cells[(window.MONDAY, 24)]["observed"] == \
            grid.cells[(window.MONDAY, 24)]["idle"]
        assert schedule.schedules(grid) == []

    def test_a_run_covering_the_whole_day_is_refused_rather_than_built(self):
        """A workshop tool plug used only at weekends is idle round the clock
        from Monday to Friday. `window.spec("00:00", "00:00")` RAISES — a
        window with no ends is one nothing can be measured across — so before
        MAX_RUN_SLOTS this walked straight into a ValueError out of the daily
        pass, taking every other suggestion, verification and rollback due that
        morning with it. What that device actually wants is a window spanning
        several DATES, which the no-wrap rule already puts outside this
        module."""
        def state(ts):
            if _day_of(ts).weekday() >= 5 and 9 <= _local(ts).tm_hour < 18:
                return "150.0"
            return IDLE_WATTS

        rows = _series(state=state)
        grid = _grid(rows)

        # Monday really is idle in all 48 cells — it is the length rule, and
        # only the length rule, that refuses it.
        assert all(grid.cells[(window.MONDAY, slot)]["idle"] >= schedule.MIN_OCCURRENCES
                   and grid.cells[(window.MONDAY, slot)]["idle"]
                   == grid.cells[(window.MONDAY, slot)]["observed"]
                   for slot in range(schedule.SLOTS_PER_DAY))
        assert schedule._runs_for(grid.cells, window.MONDAY) == []

        # Mon-Fri contribute nothing at all; what survives is the weekend's own
        # idle stretches, which are ordinary windows and stay proposable.
        assert all(window.MONDAY not in run["window"].days
                   for run in schedule.schedules(grid))

    def test_a_run_one_cell_short_of_the_whole_day_is_still_proposed(self):
        """The other side of that line: MAX_RUN_SLOTS refuses a window
        core/window.py cannot describe, and nothing wider than that."""
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        for slot in range(schedule.SLOTS_PER_DAY - 1):
            cells[(window.MONDAY, slot)] = {"idle": 20, "observed": 20}

        runs = schedule._runs_for(cells, window.MONDAY)

        assert runs == [(0, schedule.SLOTS_PER_DAY - 2)]
        assert window.label(schedule._slot_spec(*runs[0], window.WEEKDAYS)) == \
            "00:00-23:30 local Mon-Fri"

    def test_a_cell_seen_too_few_times_breaks_the_run_it_sits_in(self):
        """§3 stage D1's minimum observation count per cell: a cell seen twice
        is not evidence and must not read the same as one seen thirteen
        times."""
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        for slot in range(10, 30):
            cells[(window.MONDAY, slot)] = {"idle": 20, "observed": 20}
        cells[(window.MONDAY, 20)] = {"idle": schedule.MIN_OCCURRENCES - 1,
                                      "observed": schedule.MIN_OCCURRENCES - 1}

        assert schedule._runs_for(cells, window.MONDAY) == [(10, 19), (21, 29)]

    def test_a_weekday_whose_run_differs_is_not_merged(self):
        """Decision 5's third half, and the deliberate under-reach: Friday's
        idle stretch ends an hour early, and the intersection — a true claim
        about both — is NOT what comes back. It is not maximal on either
        weekday, and choosing among intersections is the judgement stage D2
        exists to make."""
        def state(ts):
            if _day_of(ts).weekday() == window.FRIDAY and 16 <= _local(ts).tm_hour < 17:
                return IN_USE
            return _office(ts)

        runs = schedule.schedules(_grid(_series(state=state)))
        labels = [window.label(run["window"]) for run in runs]

        assert labels[0] == "07:00-17:00 local Mon-Thu"
        assert "07:00-16:00 local Fri" in labels
        # Not intersected down to something both agree on.
        assert not any(label.startswith("07:00-16:00 local Mon") for label in labels)

    def test_runs_are_ranked_by_what_they_are_worth_without_the_watts(self):
        runs = schedule.schedules(_grid(_series()))
        worth = [run["device_hours_per_month"] for run in runs]

        assert worth == sorted(worth, reverse=True)
        # Ten hours, about twenty-one times a month.
        assert round(runs[0]["device_hours_per_month"]) == 214


# --------------------------------------------------------------------------
class TestBackedWindows:
    """`backed_windows` — the same grid read WITHOUT the identical-runs
    restriction, so that the intersections `schedules()` documents itself as
    unable to name are on the table and ranked by the quantity `schedules()`
    already ranks by.

    Uncalled, like `grid_text` and `window_is_backed` before it. What these
    hold is the two claims it is worth having: it never loses on the shape D1
    is already optimal on, and on the ragged shapes it closes the gap the
    bench measured — at the same numbers proposal §7.5's table records.

    No longer uncalled: since 2.14.15 `find_candidate` picks from it with
    `couple_weekend=True`, on proposal §7.7's reading. What that changes about
    a candidate is held in TestTheShippedPicker; these stay at the grid.

    Every case is a literal Grid whose cells are COUNTED from its own per-date
    rows, so the two halves cannot disagree. A grid whose cells said one thing
    and whose dates said another would be testing the fixture.
    """

    # What a ninety-day lookback offers on one weekday: thirteen, five clear
    # of MIN_OCCURRENCES, so nothing below fails the floor by accident.
    WEEKS = 13

    MONDAY_1_JUNE = date(2026, 6, 1)

    def _from_dates(self, rows_by_weekday):
        """A Grid built from its per-date rows, cells counted off them."""
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        for weekday, rows in rows_by_weekday.items():
            for _day, verdicts in rows:
                for slot, verdict in enumerate(verdicts):
                    if verdict is None:
                        continue
                    cells[(weekday, slot)]["observed"] += 1
                    if verdict == schedule.IDLE:
                        cells[(weekday, slot)]["idle"] += 1
        return schedule.Grid(cells, rows_by_weekday)

    def _rows(self, weekday, verdicts, weeks=None):
        """`weeks` occurrences of one weekday, every one of them the same.

        The date only has to fall on `weekday` — nothing downstream reads the
        calendar — so they are counted forward from a known Monday."""
        return [(self.MONDAY_1_JUNE + timedelta(days=weekday + 7 * week), list(verdicts))
                for week in range(weeks or self.WEEKS)]

    def _idle_across(self, span):
        return [schedule.IDLE if span[0] <= slot <= span[1] else schedule.ACTIVE
                for slot in range(schedule.SLOTS_PER_DAY)]

    def _grid(self, spans, weeks=None):
        """A Grid from {weekday: (first slot, last slot)} — one idle stretch a
        weekday, in use either side of it, and the weekdays left out of the
        dict never observed at all."""
        return self._from_dates({weekday: self._rows(weekday, self._idle_across(span), weeks)
                                 for weekday, span in spans.items()})

    def _best(self, runs):
        return (window.label(runs[0]["window"]),
                round(runs[0]["device_hours_per_month"]))

    def test_where_d1_is_already_optimal_it_is_a_draw(self):
        """The shape every planted fixture has and the one §7.5 says the five
        real devices could not distinguish a good proposer from a lucky one
        on: identical runs on every weekday, which `schedules()` merges into
        exactly the window the intersection is. The no-regression case."""
        grid = self._grid({weekday: (16, 33) for weekday in range(window.MONDAY,
                                                                 window.FRIDAY + 1)})

        assert self._best(schedule.backed_windows(grid)) == \
            self._best(schedule.schedules(grid))
        assert self._best(schedule.backed_windows(grid)) == \
            ("08:00-17:00 local Mon-Fri", 193)

    def test_the_commuter_s_ragged_edges_are_reached(self):
        """Row 1 of §7.5's bench table, at full size: every weekday's edges
        half an hour apart, so `schedules()` can merge only Monday with
        Thursday. The five-day intersection is worth nearly twice its
        two-day window, and no run scan will ever name it."""
        grid = self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32),
                           3: (15, 33), 4: (14, 30)})

        reached = schedule.backed_windows(grid)
        d1 = schedule.schedules(grid)

        assert self._best(reached) == ("08:00-15:30 local Mon-Fri", 161)
        assert self._best(d1) == ("07:30-17:00 local Mon, Thu", 81)
        assert reached[0]["device_hours_per_month"] > d1[0]["device_hours_per_month"]

    def test_the_near_miss_weekday_is_kept_rather_than_dropped(self):
        """Row 2: four identical weekdays and a fifth half an hour short. D1
        drops Friday entirely rather than shorten the other four; an hour off
        four days buys a fifth day and is worth more."""
        grid = self._grid({0: (16, 33), 1: (16, 33), 2: (16, 33),
                           3: (16, 33), 4: (16, 31)})

        reached = schedule.backed_windows(grid)
        d1 = schedule.schedules(grid)

        assert self._best(reached) == ("08:00-16:00 local Mon-Fri", 171)
        assert self._best(d1) == ("08:00-17:00 local Mon-Thu", 154)
        assert reached[0]["device_hours_per_month"] > d1[0]["device_hours_per_month"]

    def test_every_window_it_returns_is_one_the_grid_backs(self):
        """The invariant that makes the list safe to rank from at all. A
        wider candidate set is only worth having if none of the extra members
        is a window the cells cannot evidence — `window_is_backed` is the
        grid's own answer to that, and it is asked of every row rather than
        of the winner."""
        grid = self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32),
                           3: (15, 33), 4: (14, 30), 5: (20, 40)})

        assert schedule.backed_windows(grid)
        assert all(schedule.window_is_backed(grid, run["window"]) is None
                   for run in schedule.backed_windows(grid))

    def test_a_weekday_the_window_itself_was_rarely_whole_on_is_dropped(self):
        """MIN_OCCURRENCES applied per weekday to the WINDOW, not only to the
        cells — `schedules()`' own second floor, for its own reason.

        Friday's cells all clear the floor and its run is the full stretch,
        but no single Friday was ever observed across the whole of it: the
        first eight weeks watched the morning half and the last eight the
        afternoon half. That is the Grid tuple's own example of why whole
        occurrences cannot be derived from cell counts, and Friday must be
        absent from the window rather than carried by it."""
        half = 8
        morning = [schedule.IDLE if 16 <= slot <= 24 else schedule.ACTIVE
                   for slot in range(schedule.SLOTS_PER_DAY)]
        afternoon = [schedule.IDLE if 25 <= slot <= 33 else schedule.ACTIVE
                     for slot in range(schedule.SLOTS_PER_DAY)]
        # A None is a slot that was not observed on that date at all, which is
        # what `_count_run` refuses an occurrence on.
        for slot in range(25, 34):
            morning[slot] = None
        for slot in range(16, 25):
            afternoon[slot] = None

        rows = {weekday: self._rows(weekday, self._idle_across((16, 33)))
                for weekday in range(window.MONDAY, window.FRIDAY)}
        rows[window.FRIDAY] = (self._rows(window.FRIDAY, morning, half)
                               + self._rows(window.FRIDAY, afternoon, half))
        grid = self._from_dates(rows)

        # Friday's cells really do qualify, and its run really is the whole
        # stretch — it is the per-occurrence floor, and only that, which
        # drops it.
        assert schedule._runs_for(grid.cells, window.FRIDAY) == [(16, 33)]

        best = schedule.backed_windows(grid)[0]

        assert window.label(best["window"]) == "08:00-17:00 local Mon-Thu"
        assert window.FRIDAY not in best["window"].days
        assert all(day.weekday() != window.FRIDAY for day in best["dates"])

    def test_a_record_that_reached_nothing_proposes_nothing(self):
        """336 cells present and every one of them unobserved — `weekly_grid`'s
        own answer for a record it could read nothing out of. Under the
        threshold a candidate is discarded, not shown with a weak number."""
        assert schedule.backed_windows(self._from_dates({})) == []

    def test_the_order_is_the_same_on_every_call(self):
        """Same tie-break and same reason as `schedules()`: two windows worth
        the same amount come back in the same order on every run of the pass,
        so a daily pass over an unchanged record asks the same question."""
        grid = self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32),
                           3: (15, 33), 4: (14, 30), 5: (16, 33), 6: (16, 33)})

        first = schedule.backed_windows(grid)
        again = schedule.backed_windows(grid)

        assert len(first) > 1
        assert [(run["slots"], sorted(run["window"].days)) for run in first] == \
            [(run["slots"], sorted(run["window"].days)) for run in again]

    # ---- the weekend coupling, `couple_weekend` ----------------------------

    def _both(self, grid):
        """The best window without the prior and with it."""
        return (self._best(schedule.backed_windows(grid)),
                self._best(schedule.backed_windows(grid, couple_weekend=True)))

    def _listing(self, runs):
        return [(run["slots"], sorted(run["window"].days),
                 round(run["device_hours_per_month"], 6)) for run in runs]

    def test_the_slack_predicate_on_its_own(self):
        """Zero slack is the only slack admitted, on either edge."""
        assert schedule._bounds_the_window(16, 32, (16, 32))
        assert not schedule._bounds_the_window(16, 32, (16, 40))
        assert not schedule._bounds_the_window(18, 33, (17, 33))
        assert not schedule._bounds_the_window(18, 33, (16, 35))

    def test_the_idle_saturday_is_declined(self):
        """sensor.spare_room_desk_power_draw's real shape and §3 stage D2's
        own example: without the prior Saturday rides along at 219, with it
        the weekday window keeps its hours and loses the day it never
        described."""
        grid = self._grid({**{weekday: (16, 32) for weekday in range(window.MONDAY,
                                                                     window.FRIDAY + 1)},
                           window.SATURDAY: (16, 40)})

        loose, coupled = self._both(grid)

        assert loose == ("08:00-16:30 local Mon-Sat", 219)
        assert coupled == ("08:00-16:30 local Mon-Fri", 182)

    def test_a_weekend_day_the_edges_describe_is_kept(self):
        """The case that makes it a slack test rather than "weekends are
        different". Thursday, Saturday and Sunday are idle 00:00-02:00 and
        03:00-18:00; the other days all the way from midnight. Saturday and
        Sunday's covering run stops exactly at 03:00 and 18:00, so they are
        bounded by the window and stay in it — the early stretch is a
        different run and carries no slack against it."""
        verdicts_full = self._idle_across((0, 35))
        verdicts_split = [schedule.IDLE if slot <= 3 or 6 <= slot <= 35 else schedule.ACTIVE
                          for slot in range(schedule.SLOTS_PER_DAY)]
        rows = {}
        for weekday in range(7):
            split = weekday in (window.THURSDAY, window.SATURDAY, window.SUNDAY)
            rows[weekday] = self._rows(weekday, verdicts_split if split else verdicts_full)
        grid = self._from_dates(rows)
        assert schedule._runs_for(grid.cells, window.SATURDAY) == [(0, 3), (6, 35)]

        best = schedule.backed_windows(grid, couple_weekend=True)[0]

        assert window.label(best["window"]) == "03:00-18:00 local"
        assert best["window"].days == window.EVERY_DAY
        assert self._both(grid)[0] == self._both(grid)[1]

    def test_identical_runs_every_day_are_untouched(self):
        grid = self._grid({weekday: (16, 33) for weekday in range(7)})

        loose = schedule.backed_windows(grid)
        coupled = schedule.backed_windows(grid, couple_weekend=True)

        assert coupled[0]["window"].days == window.EVERY_DAY
        assert self._listing(coupled)[0] == self._listing(loose)[0]

    def test_a_weekend_wider_on_both_ends_is_declined(self):
        """Row 5 of §7.5's bench table: the weekend stretch reaches an hour
        past the weekday window on each side, so neither weekend day is
        described by its edges."""
        grid = self._grid({**{weekday: (18, 33) for weekday in range(window.MONDAY,
                                                                     window.FRIDAY + 1)},
                           window.SATURDAY: (16, 35), window.SUNDAY: (16, 35)})

        loose, coupled = self._both(grid)

        assert loose == ("09:00-17:00 local", 240)
        assert coupled == ("09:00-17:00 local Mon-Fri", 171)

    def test_working_days_carry_slack_freely(self):
        """sensor.landing_media_hub_power_draw's near miss: Mon-Thu carry two
        cells of slack against the Mon-Fri window, and it is still the right
        answer. The rule must not touch a window with no weekend day in it."""
        grid = self._grid({0: (16, 33), 1: (16, 33), 2: (16, 33),
                           3: (16, 33), 4: (16, 31)})

        assert self._listing(schedule.backed_windows(grid, couple_weekend=True)) == \
            self._listing(schedule.backed_windows(grid))
        assert self._both(grid)[1] == ("08:00-16:00 local Mon-Fri", 171)

    def test_a_weekend_only_device_is_untouched(self):
        """A Saturday-and-Sunday window has no weekday boundary to be
        inconsistent with; Sunday's longer stretch is not slack against
        anything."""
        grid = self._grid({window.SATURDAY: (16, 33), window.SUNDAY: (12, 40)})

        assert schedule.backed_windows(grid)
        assert self._listing(schedule.backed_windows(grid, couple_weekend=True)) == \
            self._listing(schedule.backed_windows(grid))

    def test_off_by_default_changes_nothing(self):
        """Every WP-A grid, read with the argument omitted and passed False,
        comes back row for row the same — the prior is opt-in until the
        harness has read the reach and the prior apart."""
        grids = [
            self._grid({weekday: (16, 33) for weekday in range(5)}),
            self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32), 3: (15, 33), 4: (14, 30)}),
            self._grid({0: (16, 33), 1: (16, 33), 2: (16, 33), 3: (16, 33), 4: (16, 31)}),
            self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32),
                        3: (15, 33), 4: (14, 30), 5: (20, 40)}),
            self._grid({0: (15, 33), 1: (14, 34), 2: (16, 32),
                        3: (15, 33), 4: (14, 30), 5: (16, 33), 6: (16, 33)}),
        ]
        for grid in grids:
            assert self._listing(schedule.backed_windows(grid, couple_weekend=False)) == \
                self._listing(schedule.backed_windows(grid))


# --------------------------------------------------------------------------
def _idle_slots(spans):
    """A state callable idle across {weekday: (first slot, last slot)} and in
    use everywhere else — the half-hour twin of `_office`, for shapes whose
    edges do not fall on the hour. A weekday left out is in use all day."""
    def state(ts):
        local = _local(ts)
        span = spans.get(_day_of(ts).weekday())
        slot = (local.tm_hour * 60 + local.tm_min) // schedule.SLOT_MINUTES
        return IDLE_WATTS if span and span[0] <= slot <= span[1] else IN_USE
    return state


class TestTheShippedPicker:
    """2.14.15: `find_candidate` picks from `backed_windows(grid,
    couple_weekend=True)` rather than `schedules(grid)` — D1+ in proposal
    §7.7, where the model proposer was preferred over both deterministic ones 0
    times out of 8 and D-2 was answered no on that reading.

    These go through `find_candidate` from rows, not a literal Grid, because
    the claim is about what a household is SHOWN: that the reach and the
    weekend prior both arrive at the candidate, that the midnight skip still
    stands between the grid and the builder, and that `schedules()` — the
    reference the new picker was measured against — reads the same record
    exactly as it did."""

    RAGGED = {0: (15, 33), 1: (14, 34), 2: (16, 32), 3: (15, 33), 4: (14, 30)}

    def test_the_ragged_weekday_edges_are_what_a_household_is_shown(self):
        """§7.5's row 1 and `study_workstation`'s shape in §7.7. Every weekday
        is idle 08:00-15:30 and no two share both edges except Monday and
        Thursday, so `schedules()` could only ever show 07:30-17:00 on those
        two days. The five-day intersection is a true claim about every one of
        them and worth nearly twice as much, and it is now the candidate."""
        candidate = _candidate(_series(state=_idle_slots(self.RAGGED)))

        assert candidate is not None
        evidence = candidate["evidence"]
        assert evidence["schedule_window"] == "08:00-15:30 local Mon-Fri"
        assert evidence["schedule"] == {"start": "08:00", "end": "15:30",
                                        "days": [0, 1, 2, 3, 4]}

    def test_the_reference_reads_the_same_record_as_it_always_did(self):
        """The same rows through `schedules()`: still 07:30-17:00 on Monday
        and Thursday. The picker moved; the reference did not."""
        grid = _grid(_series(state=_idle_slots(self.RAGGED)))

        best = schedule.schedules(grid)[0]

        assert window.label(best["window"]) == "07:30-17:00 local Mon, Thu"

    def test_a_record_whose_only_windows_end_at_midnight_is_still_refused(self, caplog):
        """The midnight skip reads `backed_windows`' output now and must still
        hold. Idle 18:00-00:00 every day is a window the grid backs and one
        `builder._no_midnight` refuses, and with nothing else on the list the
        answer is None for that reason — not for an empty grid."""
        rows = _series(state=_idle_slots({weekday: (36, 47) for weekday in range(7)}))

        found = schedule.backed_windows(_grid(rows), couple_weekend=True)
        assert found
        assert all(window.spans_midnight(run["window"]) for run in found)

        with caplog.at_level("DEBUG", logger=schedule.logger.name):
            assert _candidate(rows) is None
        assert "ends at local midnight" in caplog.text

    def test_the_weekend_coupling_reaches_the_candidate(self):
        """`spare_room_desk`'s shape: idle 08:00-16:30 Mon-Fri, 08:00-20:30
        Saturday, in use all Sunday. Without the prior Saturday rides along
        because it scores higher; the picker passes `couple_weekend=True`, so
        the household is asked about the weekday window alone."""
        spans = {**{weekday: (16, 32) for weekday in range(window.MONDAY, window.FRIDAY + 1)},
                 window.SATURDAY: (16, 40)}
        rows = _series(state=_idle_slots(spans))

        loose = schedule.backed_windows(_grid(rows))[0]
        assert window.label(loose["window"]) == "08:00-16:30 local Mon-Sat"

        candidate = _candidate(rows)

        assert candidate is not None
        assert candidate["evidence"]["schedule_window"] == "08:00-16:30 local Mon-Fri"


# --------------------------------------------------------------------------
class TestTheEvidenceFloor:
    """§7.1: under the threshold a candidate is discarded, not shown with a
    weak number."""

    def test_the_office_pattern_becomes_a_candidate(self):
        candidate = _candidate(_series())

        assert candidate is not None
        assert candidate["key"] == f"schedule:{SENSOR}"
        assert candidate["kind"] == "schedule"
        assert candidate["entities"] == [SENSOR, SWITCH]

    def test_a_device_with_only_one_level_is_never_asked_about(self):
        """Decision 2, and the failure with the worst consequence. A router, a
        NAS or an aquarium pump is idle-shaped every hour of every day — and
        cutting its power is how a home loses its network. You cannot call
        15 W idle without having seen what in use looks like on that device."""
        rows = _series(state="15.0")

        assert schedule.device_levels(rows) is None
        assert _candidate(rows) is None

    def test_a_second_level_only_just_above_the_first_is_not_a_second_level(self):
        """A DVR idling at 15 W and recording at 25 W is exactly the device §6
        says must keep its power."""
        rows = _series(state=lambda ts: "20.0" if 7 <= _local(ts).tm_hour < 17
                       else "12.0")

        assert _candidate(rows) is None

    def test_a_device_already_switched_off_is_not_a_standing_draw(self):
        """A plug the household already cuts 09:00-18:00 on a timer. Read as
        idle, the run ran straight across the dark hours and joined them to the
        real ones — `00:00-19:00 local` — and `measure`'s per-occurrence
        minimum then reported the OFF level as the draw: a nineteen-hour window
        worth 0.17 kWh a month. The off hours break the run instead, and what
        is left is the stretch the device really was idle across."""
        def state(ts):
            hour = _local(ts).tm_hour
            if 9 <= hour < 18:
                return "0.3"
            return IN_USE if 19 <= hour < 23 else IDLE_WATTS

        rows = _series(state=state)
        grid = _grid(rows)

        assert grid.cells[(window.MONDAY, 24)]["observed"] >= 12
        assert grid.cells[(window.MONDAY, 24)]["idle"] == 0

        evidence = _candidate(rows)["evidence"]

        assert evidence["schedule_window"] == "00:00-09:00 local"
        assert evidence["watts"] == 12.0

    def test_a_sensor_reporting_export_as_a_negative_number_saves_nothing(self):
        """The same failure with the sign flipped, and it used to reach the
        card as `kwh_per_month: -114.0`. A saving cannot be negative."""
        def state(ts):
            hour = _local(ts).tm_hour
            if 9 <= hour < 18:
                return "-200.0"
            return IN_USE if 19 <= hour < 23 else IDLE_WATTS

        evidence = _candidate(_series(state=state))["evidence"]

        assert evidence["schedule_window"] == "00:00-09:00 local"
        assert evidence["kwh_per_month"] > 0

    def test_a_device_that_is_only_ever_off_or_in_use_has_no_idle_window(self):
        """Nothing to cut: it is dark when it is not being used, which is what
        a household already doing the right thing looks like."""
        rows = _series(state=lambda ts: IN_USE if 9 <= _local(ts).tm_hour < 18
                       else "0.3")

        assert _candidate(rows) is None

    def test_a_sensor_with_no_controllable_switch_produces_no_candidate(self):
        assert _candidate(_series(), switch=None) is None

    def test_a_record_that_cannot_reach_eight_weeks_is_a_normal_no(self):
        """Decision 4's caveat made concrete: record.enforce_cap() halves the
        retention window repeatedly on a home watching fast-reporting sensors,
        so a ninety-day read comes back with §2's "forty-five days or fewer".
        Six occurrences of a weekday is under the floor everywhere in the grid,
        and that home gets no schedule suggestion at all — which is §7.1's
        answer rather than a gap."""
        rows = _series(start=NOW - 45 * DAY)

        assert _candidate(rows) is None

    def test_fifty_days_still_reaches_the_floor_on_one_weekday(self):
        """The other side of the same line, and worth pinning because it is
        where the floor actually bites: fifty days holds eight Tuesdays but
        only seven of everything else, so what survives is a Tuesday-only
        claim about the night hours — thin, but eight observations thin rather
        than four, and honest about being one weekday."""
        evidence = _candidate(_series(start=NOW - 50 * DAY))["evidence"]

        assert evidence["schedule_window"] == "00:00-06:00 local Tue"
        assert evidence["occurrences"] == schedule.MIN_OCCURRENCES

    def test_a_gap_is_not_read_as_the_run_it_would_have_completed(self):
        """The trap, at candidate scale. Two hours missing from every Monday
        must remove Monday from the claim — never fill it in as idleness."""
        hole = _during((9, 11), weekdays=[window.MONDAY])

        evidence = _candidate(_series(skip=hole))["evidence"]

        assert evidence["schedule_window"] == "07:00-17:00 local Tue-Fri"

    def test_a_week_of_statistics_rows_takes_its_weekday_out_of_the_claim(self):
        stats = _during((12, 13), weekdays=[window.WEDNESDAY])
        rows = _series(source=lambda ts: "statistics" if stats(ts) else "live")

        evidence = _candidate(rows)["evidence"]

        assert evidence["schedule_window"] == "07:00-17:00 local Mon, Tue, Thu, Fri"


# --------------------------------------------------------------------------
class TestWhatTheEvidenceSays:
    """§9: every number travels with the observation behind it, and the
    exclusions are part of the observation."""

    def test_the_numbers_are_the_ones_the_proposal_predicted(self):
        """§1.1's arithmetic: 12 W across ten hours, about twenty-one weekdays
        a month — roughly twice what the night window can claim."""
        evidence = _candidate(_series())["evidence"]

        assert evidence["watts"] == 12.0
        assert evidence["watts_range"] == [12.0, 12.0]
        assert evidence["hours_per_occurrence"] == 10
        assert evidence["occurrences_per_month"] == 21.4
        assert evidence["kwh_per_month"] == 2.57

    def test_it_speaks_in_occurrences_rather_than_in_nights(self):
        """Decision 6. No blob of this kind is on disk anywhere, so the
        vocabulary is free to be honest — and "nights" would be a lie about a
        window that happens at eleven on a Tuesday."""
        evidence = _candidate(_series())["evidence"]

        assert set(evidence) >= {"occurrences", "occurrences_observed",
                                 "occurrences_examined", "occurrences_per_month",
                                 "hours_per_occurrence", "schedule_window"}
        assert not any(key.startswith("night") for key in evidence)
        assert "hours_per_day" not in evidence

    def test_the_fields_a_reader_has_to_find_by_name_are_declared(self):
        """review.evidence_fingerprint reads entity, switch and watts — common
        to both shapes — plus how often and the window, which are not; and
        core/artefacts.py reads the counts, because §9 prints no number without
        the observation behind it. Both go through EVIDENCE_FIELDS. Asserted
        against the blob as well as against the declaration, so that renaming
        one without moving the other fails a test rather than a home."""
        evidence = _candidate(_series())["evidence"]

        assert evidence["occurrences_per_month"] is not None
        assert evidence["schedule_window"] == "07:00-17:00 local Mon-Fri"
        assert schedule.EVIDENCE_FIELDS == {
            "qualified": "occurrences",
            "observed": "occurrences_observed",
            "examined": "occurrences_examined",
            "per_month": "occurrences_per_month",
            "hours": "hours_per_occurrence",
            "window": "schedule_window",
        }
        assert set(evidence) >= set(schedule.EVIDENCE_FIELDS.values())

    def test_and_the_one_coverage_figure_every_shape_states_alike(self):
        """`covered_days` is standby.py's name for it and is stated here too,
        because §9 renders one sentence from it for either shape. The
        `coverage` sub-blob keeps the pair decision 4 is about and never
        disagrees with it — one expression, stated twice."""
        evidence = _candidate(_series())["evidence"]

        assert evidence["covered_days"] == evidence["coverage"]["days_observed"]
        assert evidence["covered_days"] <= 91

    def test_the_window_is_carried_as_a_value_as_well_as_a_sentence(self):
        """One of them is prose and one of them is what the automation gets
        built from. The prose is rendered by core/window.py and by nothing
        here."""
        evidence = _candidate(_series())["evidence"]

        assert evidence["schedule"] == {"start": "07:00", "end": "17:00",
                                        "days": [0, 1, 2, 3, 4]}
        rebuilt = window.spec(evidence["schedule"]["start"],
                              evidence["schedule"]["end"],
                              evidence["schedule"]["days"])
        assert window.label(rebuilt) == evidence["schedule_window"]

    def test_the_excluded_occurrences_are_counted_by_reason(self):
        """A week with the entity offline. The occurrences it cost are named,
        because "idle on fifty-eight of sixty-three" and "idle on fifty-eight
        of a hundred" are not the same claim."""
        offline = {date(2026, 5, 11) + timedelta(days=n) for n in range(7)}
        rows = _series(state=lambda ts: "unavailable" if _day_of(ts) in offline
                       else _office(ts))

        evidence = _candidate(rows)["evidence"]

        assert evidence["occurrences"] == 58
        assert evidence["occurrences_examined"] == 63
        assert evidence["excluded"] == {schedule.REASON_UNAVAILABLE: 5}
        # ...and the monthly figure is projected over what was seen, never
        # over what the calendar offers.
        assert evidence["occurrences_per_month"] == 19.7

    def test_the_coverage_is_measured_rather_than_assumed(self):
        """Decision 4. The nominal window and what was actually there are two
        separate fields on purpose: a blob printing only the first would be
        claiming ninety days of coverage on a home whose record holds sixty."""
        rows = _series(start=NOW - 60 * DAY)

        evidence = _candidate(rows)["evidence"]

        assert evidence["window"]["days"] == 90
        assert evidence["window"]["from"] == "2026-04-01"
        assert evidence["coverage"]["days_requested"] == 90
        assert evidence["coverage"]["days_observed"] <= 61
        assert evidence["coverage"]["measured_from"] == "2026-05-01"
        assert evidence["coverage"]["cells_total"] == 336

    def test_the_thresholds_that_produced_the_numbers_travel_with_them(self):
        evidence = _candidate(_series())["evidence"]

        # This DEVICE's floor, not a global constant — the asymmetry with
        # standby.py that decision 2 names.
        assert evidence["thresholds"]["idle_below_w"] == evidence["active_floor"]
        assert evidence["thresholds"]["min_occurrences"] == schedule.MIN_OCCURRENCES
        assert evidence["thresholds"]["min_hours"] == 2.0
        assert evidence["thresholds"]["slot_minutes"] == 30

    def test_the_device_s_own_two_levels_are_part_of_the_evidence(self):
        evidence = _candidate(_series())["evidence"]

        assert evidence["idle_watts"] == 12.0
        assert evidence["active_floor"] == 13.1

    def test_a_kilowatt_scale_sensor_is_read_in_watts(self):
        rows = _series(state=lambda ts: "0.120" if _office(ts) == IN_USE else "0.012")

        evidence = _candidate(rows, scale=1000.0)["evidence"]

        assert evidence["watts"] == 12.0
        assert evidence["kwh_per_month"] == 2.57

    def test_history_outside_the_judged_window_is_not_coverage_of_it(self):
        """A caller reading a year and asking for ninety days to be judged: the
        reported coverage must not contradict the window printed beside it."""
        evidence = _candidate(_series(start=WINDOW_START - 200 * DAY))["evidence"]

        assert evidence["window"]["days"] == 90
        assert evidence["coverage"]["days_observed"] <= 91
        assert evidence["coverage"]["measured_from"] == "2026-04-01"


# --------------------------------------------------------------------------
class TestTheKeyIsStable:
    """The daily review re-derives the same key from the same waste every
    morning, so the same suggestion is never shown twice."""

    def test_the_same_waste_produces_the_same_key_on_a_later_run(self):
        first = schedule.find_candidate(SENSOR, _series(), SWITCH, now=NOW)
        second = schedule.find_candidate(
            SENSOR, _series(start=WINDOW_START + DAY, end=NOW + DAY),
            SWITCH, now=NOW + DAY)

        assert first is not None and second is not None
        assert first["key"] == second["key"] == f"schedule:{SENSOR}"
        assert first["evidence"] != second["evidence"]

    def test_a_window_that_shifts_by_half_an_hour_is_still_the_same_key(self):
        """Which is why the label is not in the key: a run that moves is the
        same waste, and a key that moved with it would propose it twice."""
        def state(ts):
            hour = _local(ts).tm_hour
            if _day_of(ts).weekday() < 5:
                return IN_USE if (6 <= hour < 8 or 17 <= hour < 23) else IDLE_WATTS
            return IN_USE if 8 <= hour < 23 else IDLE_WATTS

        shifted = _candidate(_series(state=state))

        assert shifted["evidence"]["schedule_window"] == "08:00-17:00 local Mon-Fri"
        assert shifted["key"] == f"schedule:{SENSOR}"

    def test_the_same_input_twice_produces_the_same_candidate(self):
        rows = _series()

        assert _candidate(rows) == _candidate(list(rows))


# --------------------------------------------------------------------------
class TestTheSharedAnswers:
    """Decision 0: power_scale and controllable_switch moved BELOW the
    detectors rather than being imported from one of them, and standby.py
    re-exports them so nothing that named it has to move."""

    def test_standby_still_names_the_answers_it_used_to_own(self):
        assert standby.power_scale is power.power_scale
        assert standby.controllable_switch is power.controllable_switch
        assert standby.CONTROLLABLE_DOMAINS is power.CONTROLLABLE_DOMAINS
        assert standby.POWER_UNITS is power.POWER_UNITS

    def test_the_schedule_detector_does_not_import_the_standby_one(self):
        """A detector depending on another detector is backwards from how the
        two are meant to relate — they are siblings reading the same record,
        not layers."""
        source = open(schedule.__file__, encoding="utf-8").read()

        assert "import standby" not in source
        assert "detectors.standby" not in source

    def test_the_switch_on_the_same_device_is_the_one(self):
        by_device = {"dev1": [SENSOR, SWITCH, "sensor.tv_voltage"]}

        assert power.controllable_switch(SENSOR, "dev1", by_device) == SWITCH

    def test_two_switches_on_one_device_is_a_guess_and_is_refused(self):
        by_device = {"dev1": [SENSOR, "switch.socket_1", "switch.socket_2"]}

        assert power.controllable_switch(SENSOR, "dev1", by_device) is None

    def test_a_watt_sensor_reads_one_to_one_and_an_energy_meter_is_not_one(self):
        assert power.power_scale(
            {"attributes": {"unit_of_measurement": "W", "device_class": "power"}}) == 1.0
        assert power.power_scale(
            {"attributes": {"unit_of_measurement": "kWh", "device_class": "energy"}}) is None

    def test_unavailable_readings_are_left_out_and_statistics_rows_are_not(self):
        """The two rules that make `readings` more than a comprehension: an
        entity that was gone was not drawing zero, and a bucket mean can
        understate a peak but never invent one."""
        rows = [{"ts": 1, "state": "12.0", "source": "live"},
                {"ts": 2, "state": "unavailable", "source": "live"},
                {"ts": 3, "state": "120.0", "source": "statistics"}]

        assert power.readings(rows) == [12.0, 120.0]


# --------------------------------------------------------------------------
class TestTheThinLayer:
    """scan() — the I/O half. Everything it decides is decided above; what is
    checked here is that it joins the registry to the record correctly and
    hands the review pass what that pass needs, without recording anything."""

    STEP = 1800  # COVERAGE_MAX_GAP_SECONDS — the coarsest a cell still reads

    @pytest.fixture(autouse=True)
    def stores(self, tmp_path, monkeypatch):
        record.close()
        suggestions.close()
        monkeypatch.setattr(record, "DB_PATH", str(tmp_path / "record.db"))
        monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
        yield
        record.close()
        suggestions.close()

    @pytest.fixture
    def home(self, monkeypatch):
        snapshot = {
            "areas": [],
            "devices": [{"id": "dev1", "name": "TV plug"}],
            "entities": [{"entity_id": SENSOR, "device_id": "dev1"},
                         {"entity_id": SWITCH, "device_id": "dev1"}],
            "states": {
                SENSOR: {"state": IDLE_WATTS, "attributes": {
                    "unit_of_measurement": "W", "device_class": "power"}},
                # The switch's own name — the sensor's would name the meter
                # rather than the thing being switched off.
                SWITCH: {"state": "on", "attributes": {"friendly_name": "TV socket"}},
            },
        }
        monkeypatch.setattr(watchlist, "watched_entities", lambda: [SENSOR, SWITCH])
        monkeypatch.setattr(registry, "fetch_registry", lambda: (True, snapshot))
        return snapshot

    def _plant(self, entity_id=SENSOR):
        for ts in range(WINDOW_START, NOW, self.STEP):
            value = _office(ts)
            record.record_transition(entity_id, ts, value, value, self.STEP)

    def test_a_watched_office_pattern_becomes_a_candidate(self, home):
        self._plant()

        found = schedule.scan(now=NOW)

        assert [c["key"] for c in found] == [f"schedule:{SENSOR}"]
        assert found[0]["entities"] == [SENSOR, SWITCH]
        assert found[0]["evidence"]["schedule_window"] == "07:00-17:00 local Mon-Fri"

    def test_the_series_it_judged_travels_with_the_candidate(self, home):
        """The grading has to run over the same series the measurement came
        from, so scan hands them over rather than making its caller read the
        record a second time over a window that has moved."""
        self._plant()

        found = schedule.scan(now=NOW)

        assert found[0]["rows"] == record.between(SENSOR, WINDOW_START, NOW)
        assert found[0]["scale"] == 1.0
        assert found[0]["name"] == "TV socket"

    def test_it_records_nothing_itself(self, home):
        self._plant()

        assert schedule.scan(now=NOW)

        assert suggestions.open_for(f"schedule:{SENSOR}") is None

    def test_an_unwatched_home_is_scanned_for_nothing(self, home, monkeypatch):
        self._plant()
        monkeypatch.setattr(watchlist, "watched_entities", list)

        assert schedule.scan(now=NOW) == []

    def test_a_registry_it_cannot_read_is_skipped_rather_than_guessed(self, home, monkeypatch):
        self._plant()
        monkeypatch.setattr(registry, "fetch_registry", lambda: (False, "HA is down"))

        assert schedule.scan(now=NOW) == []

    def test_a_sensor_with_no_switch_is_never_even_read(self, home, monkeypatch):
        """No switch means no candidate, so there is nothing the record could
        say that would change the answer — and it is not asked."""
        self._plant()
        home["entities"] = [{"entity_id": SENSOR, "device_id": "dev1"}]
        reads = []
        monkeypatch.setattr(record, "between", lambda *a, **kw: reads.append(a) or [])

        assert schedule.scan(now=NOW) == []
        assert reads == []


# --------------------------------------------------------------------------
class TestGridText:
    """`grid_text` — WP1: the grid rendered as text a model can read. Lands
    with no caller (see the module's "BUILT AHEAD OF ITS CALLER" section),
    so every test here drives it from a literal Grid, no fixtures."""

    def _full_grid(self, overrides=None):
        """A fully-populated 336-cell grid, all cells zeroed unless
        overridden. `grid_text` indexes every (weekday, slot) unconditionally
        — the way `weekly_grid` itself always produces all 336 — so a
        partial dict would KeyError rather than exercise the renderer."""
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        cells.update(overrides or {})
        return schedule.Grid(cells, {})

    def _row(self, text, weekday):
        """The 48-character body of one weekday's row, prefix stripped."""
        prefix = schedule._WEEKDAY_ABBR[weekday] + " "
        line = next(line for line in text.splitlines() if line.startswith(prefix))
        return line[len(prefix):]

    def test_the_three_states_render_differently(self):
        grid = self._full_grid({
            (window.MONDAY, 0): {"idle": 9, "observed": 9},   # idle every time
            (window.MONDAY, 1): {"idle": 3, "observed": 9},   # observed, sometimes active
            (window.MONDAY, 2): {"idle": 0, "observed": 0},   # never observed
        })

        row = self._row(schedule.grid_text(grid), window.MONDAY)

        idle_mark, mixed_mark, unobserved_mark = row[0], row[1], row[2]
        assert len({idle_mark, mixed_mark, unobserved_mark}) == 3
        assert unobserved_mark == "?"
        assert idle_mark.isdigit()
        assert mixed_mark.isalpha()

    def test_the_observation_count_travels_with_the_cell(self):
        """A cell seen twice must not read the same as one seen many times —
        the proposal's own line about stage C."""
        grid = self._full_grid({
            (window.MONDAY, 0): {"idle": 2, "observed": 2},
            (window.MONDAY, 1): {"idle": 5, "observed": 5},
            (window.MONDAY, 2): {"idle": 13, "observed": 13},
        })

        row = self._row(schedule.grid_text(grid), window.MONDAY)

        assert row[0] == "2"
        assert row[1] == "5"
        # thirteen is capped rather than carried in full (GRID_TEXT_COUNT_CAP)
        assert row[2] == str(schedule.GRID_TEXT_COUNT_CAP)
        assert row[0] != row[2]

    def test_the_count_also_travels_with_a_mixed_cell(self):
        grid = self._full_grid({
            (window.MONDAY, 0): {"idle": 1, "observed": 4},
            (window.MONDAY, 1): {"idle": 1, "observed": 9},
        })

        row = self._row(schedule.grid_text(grid), window.MONDAY)

        assert row[0] == schedule._MIXED_MARKS[4 - 1]
        assert row[1] == schedule._MIXED_MARKS[schedule.GRID_TEXT_COUNT_CAP - 1]
        assert row[0] != row[1]

    def test_an_all_unobserved_grid_is_all_question_marks(self):
        text = schedule.grid_text(self._full_grid())

        for weekday in range(7):
            row = self._row(text, weekday)
            assert row == "?" * schedule.SLOTS_PER_DAY

    def test_a_full_grid_stays_under_the_size_bound(self):
        """§3 claimed 1-2 KB for a rendering with no counts at all; verify it
        still holds now that a count travels with every cell."""
        overrides = {}
        for weekday in range(7):
            for slot in range(schedule.SLOTS_PER_DAY):
                overrides[(weekday, slot)] = {"idle": 7, "observed": 9}
        grid = self._full_grid(overrides)

        text = schedule.grid_text(grid)

        assert len(text.encode("ascii")) < 2048

    def test_ascii_only(self):
        overrides = {}
        for weekday in range(7):
            for slot in range(schedule.SLOTS_PER_DAY):
                overrides[(weekday, slot)] = {"idle": slot % 9, "observed": (slot % 9) or 1}
        grid = self._full_grid(overrides)

        text = schedule.grid_text(grid)

        text.encode("ascii")  # raises UnicodeEncodeError on anything else

    def test_a_grid_that_is_not_fully_populated_reads_as_unobserved(self):
        """`weekly_grid([])` returns `Grid({}, {})` — the 336-cell promise is
        the usual case, not a guarantee the type itself enforces — so
        `grid_text` reads a missing cell the way `_runs_for` already does,
        rather than raising a KeyError on a Grid it did not build itself."""
        grid = schedule.Grid({}, {})

        text = schedule.grid_text(grid)

        for weekday in range(7):
            assert self._row(text, weekday) == "?" * schedule.SLOTS_PER_DAY

    def test_the_proposals_own_office_pattern_is_visible(self):
        """07:00-17:00 Mon-Fri, idle-on-every-observation, well clear of
        MIN_OCCURRENCES — the pattern the proposal opens with."""
        first_slot, last_slot = 14, 33  # 07:00 .. 17:00, per _slot_clock
        overrides = {}
        for weekday in range(window.MONDAY, window.FRIDAY + 1):
            for slot in range(first_slot, last_slot + 1):
                overrides[(weekday, slot)] = {"idle": 10, "observed": 10}
        grid = self._full_grid(overrides)

        text = schedule.grid_text(grid)

        for weekday in range(window.MONDAY, window.FRIDAY + 1):
            row = self._row(text, weekday)
            assert row[:first_slot] == "?" * first_slot
            assert row[first_slot:last_slot + 1] == str(schedule.GRID_TEXT_COUNT_CAP) * (last_slot - first_slot + 1)
            assert row[last_slot + 1:] == "?" * (schedule.SLOTS_PER_DAY - last_slot - 1)
        for weekday in (window.SATURDAY, window.SUNDAY):
            row = self._row(text, weekday)
            assert row == "?" * schedule.SLOTS_PER_DAY

    def test_the_header_explains_the_encoding(self):
        text = schedule.grid_text(self._full_grid())

        assert "never observed" in text
        assert "idle on every observation" in text


class TestRunsText:
    """`runs_text` — the same grid read off in clock times, and what
    core/scheduler_llm.py actually sends. The reason it exists beside
    `grid_text` is measured rather than assumed and is in the section comment
    above the function; what these tests hold is the claim the swap rests on
    — the stretches listed are exactly the ones a legal window can sit
    inside, and a day with nothing has its two reasons kept apart.
    """

    def _full_grid(self, overrides=None):
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        cells.update(overrides or {})
        return schedule.Grid(cells, {})

    def _idle(self, cells, weekday, first_slot, last_slot, observed=13):
        for slot in range(first_slot, last_slot + 1):
            cells[(weekday, slot)] = {"idle": observed, "observed": observed}
        return cells

    def _line(self, text, weekday):
        prefix = f"  {schedule._WEEKDAY_ABBR[weekday]}  "
        return next(line[len(prefix):] for line in text.splitlines()
                    if line.startswith(prefix))

    def test_a_stretch_is_listed_as_clock_times(self):
        """The whole point of the rendering: 07:30-17:00 is READ, not counted
        to. Slot 15 to slot 33 inclusive is that window, and the end is the
        moment the stretch stops rather than the start of its last cell."""
        grid = self._full_grid(self._idle({}, window.MONDAY, 15, 33))

        assert self._line(schedule.runs_text(grid), window.MONDAY) == "07:30-17:00"

    def test_a_stretch_running_to_midnight_ends_at_24_00(self):
        """`_slot_clock` wraps slot 48 to 00:00, which is right for building a
        spec and reads as a window running backwards in a list."""
        grid = self._full_grid(self._idle({}, window.MONDAY, 40, 47))

        assert self._line(schedule.runs_text(grid), window.MONDAY) == "20:00-24:00"

    def test_two_stretches_on_one_day_are_both_listed(self):
        cells = self._idle({}, window.MONDAY, 2, 9)
        grid = self._full_grid(self._idle(cells, window.MONDAY, 20, 30))

        assert self._line(schedule.runs_text(grid), window.MONDAY) == "01:00-05:00, 10:00-15:30"

    def test_a_day_in_use_and_a_day_never_watched_read_differently(self):
        """Not the same fact, and the difference is the one this detector is
        most exposed to: a stretch nobody watched is the absence of evidence,
        and a model told only "nothing here" cannot tell them apart."""
        cells = {(window.MONDAY, slot): {"idle": 0, "observed": 13}
                 for slot in range(schedule.SLOTS_PER_DAY)}
        text = schedule.runs_text(self._full_grid(cells))

        assert "in use" in self._line(text, window.MONDAY)
        assert "nothing watched" in self._line(text, window.SUNDAY)

    def test_a_thin_stretch_is_not_listed(self):
        """MIN_OCCURRENCES, in the rendering: a stretch nobody watched often
        enough cannot be inside a window `window_is_backed` accepts, so
        listing it could only invite an answer the validator then refuses."""
        thin = schedule.MIN_OCCURRENCES - 1
        grid = self._full_grid(self._idle({}, window.MONDAY, 15, 33, observed=thin))

        assert "in use" in self._line(schedule.runs_text(grid), window.MONDAY)

    def test_a_stretch_under_the_minimum_length_is_not_listed(self):
        """What bounds the size of this text by FORMAT rather than by
        household — the objection GRID_TEXT_COUNT_CAP's comment raises against
        a run table, answered."""
        grid = self._full_grid(
            self._idle({}, window.MONDAY, 10, 10 + schedule.MIN_RUN_SLOTS - 2))

        assert "in use" in self._line(schedule.runs_text(grid), window.MONDAY)

    def test_every_listed_stretch_is_one_a_window_can_sit_inside(self):
        """The claim the swap from `grid_text` rests on, asserted rather than
        argued: a window covering a listed stretch whole is one
        `window_is_backed` accepts, on every day it is listed for."""
        cells = self._idle({}, window.MONDAY, 15, 33)
        grid = self._full_grid(self._idle(cells, window.TUESDAY, 14, 34))

        for weekday, (first_slot, last_slot) in ((window.MONDAY, (15, 33)),
                                                 (window.TUESDAY, (14, 34))):
            spec = schedule._slot_spec(first_slot, last_slot, {weekday})
            assert schedule.window_is_backed(grid, spec) is None

    def test_the_header_states_the_floor_the_stretches_cleared(self):
        text = schedule.runs_text(self._full_grid())

        assert str(schedule.MIN_OCCURRENCES) in text
        assert "idle on EVERY observation" in text


class TestWindowIsBacked:
    """`window_is_backed` — WP2's first three checks (proposal §7.2 decision
    3), living here because they are questions about the GRID. Pure, so every
    case below is a literal grid and a literal window with no client and no
    model anywhere near it; `tests/test_scheduler_llm.py` owns the other
    three and the call.

    §7.1 is what these are for. Replay only catches a window the record
    PROVES was in use, so a window laid across half-hours nobody watched
    passes grading cleanly — the question asked here is not "did this go
    wrong" but "was this ever watched"."""

    FIRST_SLOT, LAST_SLOT = 18, 33  # the cells 09:00-17:00 covers, inclusive
    WELL_OBSERVED = schedule.MIN_OCCURRENCES + 2

    def _grid(self, overrides=None):
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        cells.update(overrides or {})
        return schedule.Grid(cells, {})

    def _plus(self, grid, overrides):
        """A grid with some cells replaced. Spelt out rather than
        `dict(grid.cells, **overrides)`, which cannot take tuple keys."""
        return schedule.Grid({**grid.cells, **overrides}, grid.dates)

    def _office_grid(self, days=range(window.MONDAY, window.FRIDAY + 1)):
        """A grid idle across 09:00-17:00 on `days` and unobserved everywhere
        else — the shape every refusal below is one edit away from.

        Not `_office`, which this file already uses at module level for
        something else entirely: a power reading for a timestamp."""
        seen = {"idle": self.WELL_OBSERVED, "observed": self.WELL_OBSERVED}
        return self._grid({(weekday, slot): dict(seen)
                           for weekday in days
                           for slot in range(self.FIRST_SLOT, self.LAST_SLOT + 1)})

    def test_a_window_the_grid_backs_has_no_reason(self):
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        assert schedule.window_is_backed(self._office_grid(), spec) is None

    def test_a_window_inside_a_backed_one_is_backed_too(self):
        spec = window.spec("10:00", "16:00", window.WEEKDAYS)

        assert schedule.window_is_backed(self._office_grid(), spec) is None

    def test_one_cell_the_device_was_ever_active_in_kills_it(self):
        grid = self._plus(self._office_grid(),
                          {(window.THURSDAY, 24): {"idle": 7, "observed": 10}})
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        reason = schedule.window_is_backed(grid, spec)

        assert "Thu 12:00" in reason
        assert "active on 3 of its 10" in reason

    def test_a_cell_under_the_occurrence_floor_kills_it(self):
        thin = schedule.MIN_OCCURRENCES - 1
        grid = self._plus(self._office_grid(),
                          {(window.MONDAY, 30): {"idle": thin, "observed": thin}})
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        reason = schedule.window_is_backed(grid, spec)

        assert "Mon 15:00" in reason
        assert f"under the {schedule.MIN_OCCURRENCES}" in reason

    def test_a_never_observed_cell_inside_the_window_kills_it(self):
        """The failure this module's own docstring names — an unobserved
        stretch reading as flatness. A '?' cell is not evidence of idleness,
        and it is the one thing replay cannot contradict."""
        grid = self._plus(self._office_grid(),
                          {(window.TUESDAY, 22): {"idle": 0, "observed": 0}})
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        reason = schedule.window_is_backed(grid, spec)

        assert "Tue 11:00" in reason
        assert "observed 0 times" in reason

    def test_a_weekday_the_record_has_nothing_about_is_named_as_such(self):
        """§3 stage D2's counter-example: the weekday pattern extended to a
        Saturday that was never watched. Reported a DAY at a time rather than
        as one thin cell, because that is what it is."""
        spec = window.spec("09:00", "17:00", sorted(window.WEEKDAYS) + [window.SATURDAY])

        assert schedule.window_is_backed(self._office_grid(), spec) == "Sat never observed at all"

    def test_a_day_observed_only_outside_the_window_is_not_never_observed(self):
        """Saturday watched at 20:00 but not at noon fails on the CELL, not on
        the day — two different things, and the reason has to say which."""
        grid = self._plus(self._office_grid(),
                          {(window.SATURDAY, 40): {"idle": self.WELL_OBSERVED,
                                                   "observed": self.WELL_OBSERVED}})
        spec = window.spec("09:00", "17:00", sorted(window.WEEKDAYS) + [window.SATURDAY])

        reason = schedule.window_is_backed(grid, spec)

        assert "never observed at all" not in reason
        assert "Sat 09:00" in reason

    def test_a_day_the_window_does_not_name_is_not_looked_at(self):
        """Sunday is a wreck and irrelevant: the window does not claim it."""
        grid = self._plus(self._office_grid(),
                          {(window.SUNDAY, slot): {"idle": 0, "observed": 3}
                           for slot in range(schedule.SLOTS_PER_DAY)})
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        assert schedule.window_is_backed(grid, spec) is None

    def test_a_window_running_through_midnight_cannot_be_indexed(self):
        """The grid is cut at local midnight, so such a window's cells sit
        under two weekday keys. A floor beneath decision 3's own check 4,
        which core/scheduler_llm.py applies first."""
        spec = window.spec("22:00", "06:00", [window.MONDAY])

        reason = schedule.window_is_backed(self._office_grid(days=range(7)), spec)

        assert reason == "runs through local midnight, which the grid is cut at"
        # the fault, not the window: the caller prints the label itself
        assert window.label(spec) not in reason

    def test_a_misaligned_window_is_checked_against_the_cells_it_touches(self):
        """Fail-closed: the end is taken to the cell it lands PAST, so the
        extra cell can only add a refusal. 09:00-17:15 reaches into the 17:00
        cell, which this grid never observed."""
        spec = window.spec("09:00", "17:15", window.WEEKDAYS)

        reason = schedule.window_is_backed(self._office_grid(), spec)

        assert "Mon 17:00" in reason

    def test_a_misaligned_start_reaches_back_into_its_own_cell(self):
        spec = window.spec("08:45", "17:00", window.WEEKDAYS)

        reason = schedule.window_is_backed(self._office_grid(), spec)

        assert "Mon 08:30" in reason

    def test_a_grid_that_is_not_fully_populated_reads_as_unobserved(self):
        """`weekly_grid([])` returns `Grid({}, {})` — nothing here may
        KeyError on a grid it did not build itself."""
        spec = window.spec("09:00", "17:00", window.WEEKDAYS)

        reason = schedule.window_is_backed(schedule.Grid({}, {}), spec)

        assert "never observed at all" in reason

    def test_it_refuses_rather_than_repairing(self):
        """There is deliberately no return path handing back a trimmed
        window, a clamped one or the subset of days that would have passed —
        the answer is a reason or nothing."""
        spec = window.spec("09:00", "17:00", sorted(window.WEEKDAYS) + [window.SATURDAY])

        reason = schedule.window_is_backed(self._office_grid(), spec)

        assert isinstance(reason, str)

    def test_what_d1_finds_in_a_grid_is_backed_by_it(self):
        """The two halves agree. A run `_runs_for` picks out of a grid is a
        window `window_is_backed` accepts against the same grid — which
        matters because WP4 falls back to D1's pick whenever the model is
        refused, and a fallback this validator would also refuse would leave
        the detector with nothing on a grid it can read.

        Driven off `_runs_for` rather than `schedules`, which counts whole
        occurrences out of `Grid.dates` and so cannot be asked about a grid
        assembled cell by cell."""
        grid = self._office_grid()
        found = 0

        for weekday in range(7):
            for first_slot, last_slot in schedule._runs_for(grid.cells, weekday):
                spec = schedule._slot_spec(first_slot, last_slot, [weekday])
                assert schedule.window_is_backed(grid, spec) is None
                found += 1

        assert found == len(window.WEEKDAYS)


class TestGridFingerprint:
    """`grid_fingerprint` — D-4/WP3's key into `core/schedule_cache.py`. The
    package only pays for itself if two grids describing the same shape
    fingerprint identically despite the counts moving underneath them every
    single day (§7.1's decision-1 trap), and if two grids that would answer
    `window_is_backed` differently never fingerprint alike. Both are tested
    directly rather than left as an argument."""

    WELL_OBSERVED = schedule.MIN_OCCURRENCES + 2

    def _grid(self, overrides=None):
        cells = {(weekday, slot): {"idle": 0, "observed": 0}
                 for weekday in range(7) for slot in range(schedule.SLOTS_PER_DAY)}
        cells.update(overrides or {})
        return schedule.Grid(cells, {})

    def test_it_covers_all_336_cells(self):
        grid = self._grid()

        assert len(schedule.grid_fingerprint(grid)) == 7 * schedule.SLOTS_PER_DAY

    def test_an_unpopulated_grid_reads_as_entirely_unusable(self):
        """`weekly_grid([])`'s `Grid({}, {})` — nothing here may KeyError on
        a grid it did not build itself, the same guarantee `grid_text` and
        `window_is_backed` already make."""
        fingerprint = schedule.grid_fingerprint(schedule.Grid({}, {}))

        assert set(fingerprint) == {"u"}

    def test_never_observed_is_unusable(self):
        grid = self._grid({(window.MONDAY, 10): {"idle": 0, "observed": 0}})

        fingerprint = schedule.grid_fingerprint(grid)

        assert fingerprint[10] == "u"

    def test_idle_on_every_observation_is_backed_idle(self):
        seen = {"idle": self.WELL_OBSERVED, "observed": self.WELL_OBSERVED}
        grid = self._grid({(window.MONDAY, 10): seen})

        fingerprint = schedule.grid_fingerprint(grid)

        assert fingerprint[10] == "i"

    def test_active_at_least_once_is_backed_active(self):
        seen = {"idle": self.WELL_OBSERVED - 1, "observed": self.WELL_OBSERVED}
        grid = self._grid({(window.MONDAY, 10): seen})

        fingerprint = schedule.grid_fingerprint(grid)

        assert fingerprint[10] == "a"

    def test_a_count_shifted_across_the_min_occurrences_boundary_differs(self):
        """The trap this package exists to avoid, made concrete: one more
        observation is enough to flip a cell from unusable to backed, and the
        fingerprint MUST notice — otherwise a genuinely thin grid would be
        cached as though it had cleared the floor."""
        thin = self._grid({(window.MONDAY, 10):
                           {"idle": schedule.MIN_OCCURRENCES - 1,
                            "observed": schedule.MIN_OCCURRENCES - 1}})
        cleared = self._grid({(window.MONDAY, 10):
                              {"idle": schedule.MIN_OCCURRENCES,
                               "observed": schedule.MIN_OCCURRENCES}})

        assert schedule.grid_fingerprint(thin) != schedule.grid_fingerprint(cleared)

    def test_counts_shifted_on_the_same_side_of_the_boundary_are_identical(self):
        """The whole point of D-4: the window slides one day every morning
        and nearly every cell's `observed` count moves by one, but the
        SHAPE — which cells are unusable, idle-backed or active-backed — does
        not, so the fingerprint must not either."""
        yesterday = self._grid({(window.MONDAY, 10):
                                {"idle": self.WELL_OBSERVED, "observed": self.WELL_OBSERVED},
                                (window.TUESDAY, 20):
                                {"idle": self.WELL_OBSERVED - 1, "observed": self.WELL_OBSERVED}})
        today = self._grid({(window.MONDAY, 10):
                            {"idle": self.WELL_OBSERVED + 1, "observed": self.WELL_OBSERVED + 1},
                            (window.TUESDAY, 20):
                            {"idle": self.WELL_OBSERVED, "observed": self.WELL_OBSERVED + 1}})

        assert schedule.grid_fingerprint(yesterday) == schedule.grid_fingerprint(today)

    def test_equal_fingerprints_answer_window_is_backed_identically(self):
        """The invariant the docstring claims: two grids sharing a
        fingerprint must agree, cell for cell, on every `window_is_backed`
        verdict — checked here across every run `_runs_for` finds plus one
        window that is refused, on two grids built to differ only in counts
        that stay on the same side of MIN_OCCURRENCES."""
        first_slot, last_slot = 18, 33  # 09:00-17:00
        seen_a = {"idle": self.WELL_OBSERVED, "observed": self.WELL_OBSERVED}
        seen_b = {"idle": self.WELL_OBSERVED + 3, "observed": self.WELL_OBSERVED + 3}
        office_a = self._grid({(weekday, slot): dict(seen_a)
                               for weekday in window.WEEKDAYS
                               for slot in range(first_slot, last_slot + 1)})
        office_b = self._grid({(weekday, slot): dict(seen_b)
                               for weekday in window.WEEKDAYS
                               for slot in range(first_slot, last_slot + 1)})
        assert schedule.grid_fingerprint(office_a) == schedule.grid_fingerprint(office_b)

        candidates = [
            window.spec("09:00", "17:00", window.WEEKDAYS),
            window.spec("10:00", "16:00", window.WEEKDAYS),
            window.spec("09:00", "17:00", sorted(window.WEEKDAYS) + [window.SATURDAY]),
            window.spec("08:00", "18:00", window.WEEKDAYS),
        ]
        for spec in candidates:
            assert (schedule.window_is_backed(office_a, spec)
                    is None) == (schedule.window_is_backed(office_b, spec) is None)

    def test_it_is_not_hashed(self):
        """A readable string, not a digest — `review.evidence_fingerprint`'s
        own reason for staying a tuple rather than a hash applies here too."""
        grid = self._grid()

        fingerprint = schedule.grid_fingerprint(grid)

        assert isinstance(fingerprint, str)
        assert set(fingerprint) <= {"u", "i", "a"}
