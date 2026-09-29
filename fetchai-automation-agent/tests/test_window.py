"""What a window is, and the arithmetic over one.

core/window.py is the seam the suggestion pipeline's period of the day was
extracted into, so everything here is a list literal and a fixed date: the
module holds no store, no clock it was not handed and nothing from core/. What
it DOES hold is the pipeline's only answer about the household's clock, which
is why half of this file is about dates on which the clock moved — the cases no
scenario harness can plant and every home meets twice a year.

Three shapes are exercised throughout, because the seam exists so that all
three can be described by the same code: the night the standby detector
measures today, the weekday office window the next detector needs, and a window
that crosses midnight. The third has no caller at all yet; it is tested for the
reason the module docstring gives for building it — retrofitting it later means
re-opening every module the seam was extracted to close.
"""
from datetime import date, datetime, timedelta, timezone
import time

import pytest

from core import window


DAY = 86400

NIGHT = window.spec("01:00", "05:00")
OFFICE = window.spec("08:30", "17:00", days=window.WEEKDAYS)
LATE = window.spec("22:00", "06:00")

# A Monday, so that the weekday arithmetic below is readable rather than
# looked up.
MONDAY = date(2026, 6, 1)


def _utc(year, month, day, hour=0):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Pins the process timezone, exactly as the detector's tests do.

    Every answer here is LOCAL, so without this the file would drift with the
    developer's clock. time.mktime's notion of local time comes from the C
    library, which tzset() changes and monkeypatch does not restore.
    """
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _in_zone(monkeypatch, name):
    """Runs the rest of a test in a real zone with a real clock change."""
    monkeypatch.setenv("TZ", name)
    time.tzset()


# --------------------------------------------------------------------------
class TestTheSpec:
    """A window is data, and it is validated where it is declared."""

    def test_the_three_shapes_the_seam_exists_to_describe(self):
        assert (NIGHT.start_hour, NIGHT.start_minute) == (1, 0)
        assert (NIGHT.end_hour, NIGHT.end_minute) == (5, 0)
        assert NIGHT.days == window.EVERY_DAY

        assert (OFFICE.start_hour, OFFICE.start_minute) == (8, 30)
        assert OFFICE.days == frozenset({0, 1, 2, 3, 4})

        assert window.spans_midnight(LATE)
        assert not window.spans_midnight(NIGHT)
        assert not window.spans_midnight(OFFICE)

    def test_two_specs_of_the_same_window_are_the_same_value(self):
        """A tuple rather than a dict: the pipeline passes these around and
        compares them, and a window is a value rather than a record."""
        assert window.spec("01:00", "05:00") == NIGHT
        assert window.spec("01:00", "05:00", days=window.WEEKDAYS) != NIGHT

    def test_a_window_with_no_span_is_refused_rather_than_read_as_a_whole_day(self):
        with pytest.raises(ValueError):
            window.spec("01:00", "01:00")

    def test_a_time_that_is_not_a_time_is_refused_at_the_declaration(self):
        for bad in ("1am", "25:00", "01:60", "01", "01:00:00", ""):
            with pytest.raises(ValueError):
                window.spec(bad, "05:00")

    def test_a_window_happening_on_no_day_is_refused(self):
        with pytest.raises(ValueError):
            window.spec("01:00", "05:00", days=[])
        with pytest.raises(ValueError):
            window.spec("01:00", "05:00", days={7})


class TestHowLongItIs:
    def test_the_night_is_four_hours_and_says_so_as_an_integer(self):
        """`hours_per_day` is on disk in evidence blobs written before the
        seam existed, where four hours has always been the integer 4."""
        assert window.hours(NIGHT) == 4
        assert isinstance(window.hours(NIGHT), int)

    def test_a_window_of_half_hours_is_a_float(self):
        assert window.hours(OFFICE) == 8.5
        assert window.minutes(OFFICE) == 510

    def test_a_window_crossing_midnight_is_measured_forwards_over_it(self):
        assert window.hours(LATE) == 8
        assert window.minutes(LATE) == 8 * 60


class TestTheLabel:
    def test_the_night_reads_exactly_as_it_has_always_read(self):
        """The one string in this module that may not move by a character.

        verify._like_periods compares the `night_window` a baseline stored last
        week against the one measured today and reports the verification as not
        comparable when they differ. Homes are mid-verification against this
        literal right now, so it is asserted as a literal — a round trip
        through the renderer would agree with whatever the renderer became.
        """
        assert window.label(NIGHT) == "01:00-05:00 local"

    def test_a_window_naming_its_days_says_which(self):
        assert window.label(OFFICE) == "08:30-17:00 local Mon-Fri"

    def test_a_window_crossing_midnight_prints_both_ends_as_written(self):
        assert window.label(LATE) == "22:00-06:00 local"

    def test_a_run_is_a_range_only_where_a_household_would_say_one(self):
        def days(*numbers):
            return window.label(window.spec("01:00", "05:00", days=set(numbers)))

        assert days(5, 6) == "01:00-05:00 local Sat, Sun"
        assert days(0, 2, 4) == "01:00-05:00 local Mon, Wed, Fri"
        assert days(0, 1, 2) == "01:00-05:00 local Mon-Wed"
        assert days(0, 1, 2, 3, 4, 6) == "01:00-05:00 local Mon-Fri, Sun"

    def test_the_week_is_read_from_monday_and_never_wrapped(self):
        """"Sun, Mon" is two days a person names separately; rendering it as a
        range would put the week's ends in an order nobody writes."""
        assert window.label(window.spec("01:00", "05:00", days={6, 0})) == \
               "01:00-05:00 local Mon, Sun"


class TestPlacingOneOnADate:
    def test_a_same_day_window_is_the_two_instants_on_that_date(self):
        assert window.bounds(NIGHT, MONDAY) == (_utc(2026, 6, 1, 1), _utc(2026, 6, 1, 5))

    def test_a_window_crossing_midnight_ends_on_the_day_after_it_began(self):
        """The date is the one it STARTS on, which is what makes "the night of
        the 1st" one occurrence rather than two halves of two dates."""
        assert window.bounds(LATE, MONDAY) == (_utc(2026, 6, 1, 22), _utc(2026, 6, 2, 6))

    def test_the_minutes_are_placed_as_well_as_the_hours(self):
        start, end = window.bounds(OFFICE, MONDAY)

        assert start == _utc(2026, 6, 1, 8) + 1800
        assert end == _utc(2026, 6, 1, 17)

    def test_bounds_are_placed_on_a_date_the_spec_does_not_name(self):
        """`bounds` answers about the date it was handed. Which dates a window
        happens on is `windows`' filter, and asking the two questions in one
        function is how a caller comes to place an occurrence it then cannot
        find in the enumeration."""
        saturday = date(2026, 6, 6)

        assert window.bounds(OFFICE, saturday) is not None


class TestEnumeratingThemOverARange:
    def test_every_occurrence_whole_inside_the_range_oldest_first(self):
        found = window.windows(NIGHT, _utc(2026, 6, 1), _utc(2026, 6, 4))

        assert [day for day, _s, _e in found] == [date(2026, 6, 1), date(2026, 6, 2),
                                                  date(2026, 6, 3)]
        assert found[0] == (date(2026, 6, 1), _utc(2026, 6, 1, 1), _utc(2026, 6, 1, 5))

    def test_a_partly_covered_occurrence_is_evidence_for_something_else(self):
        """The rule standby.night_windows established and this inherited: half
        a window is not weaker evidence for the same claim."""
        found = window.windows(NIGHT, _utc(2026, 6, 1, 3), _utc(2026, 6, 3, 3))

        assert [day for day, _s, _e in found] == [date(2026, 6, 2)]

    def test_an_occurrence_ending_exactly_at_the_end_of_the_range_is_whole(self):
        found = window.windows(NIGHT, _utc(2026, 6, 1), _utc(2026, 6, 1, 5))

        assert len(found) == 1

    def test_dates_the_window_does_not_name_are_skipped_not_returned_empty(self):
        """A caller counting what it examined counts occurrences, never days —
        five weekdays out of a calendar week."""
        found = window.windows(OFFICE, _utc(2026, 6, 1), _utc(2026, 6, 8))

        assert [day.weekday() for day, _s, _e in found] == [0, 1, 2, 3, 4]

    def test_a_window_crossing_midnight_is_counted_on_the_day_it_began(self):
        found = window.windows(LATE, _utc(2026, 6, 1), _utc(2026, 6, 4))

        assert [day for day, _s, _e in found] == [date(2026, 6, 1), date(2026, 6, 2)]
        assert found[-1][2] == _utc(2026, 6, 3, 6)

    def test_a_range_holding_no_whole_occurrence_is_empty_rather_than_partial(self):
        assert window.windows(NIGHT, _utc(2026, 6, 1, 6), _utc(2026, 6, 1, 23)) == []


# --------------------------------------------------------------------------
class TestTheClock:
    """The seam's single answer about a wall-clock time, which is the strict
    one. There were two implementations of this before the seam and they
    disagreed; these are the cases they disagreed on."""

    def test_a_time_the_clock_skipped_over_never_happened(self, monkeypatch):
        # Europe/London, 29 March 2026: 01:00 GMT becomes 02:00 BST, so no
        # local clock in the country read 01:00 that morning.
        _in_zone(monkeypatch, "Europe/London")

        assert window.instant(date(2026, 3, 29), 1, 0) is None
        assert window.instant(date(2026, 3, 29), 2, 0) is not None
        assert window.instant(date(2026, 3, 28), 1, 0) is not None

    def test_the_occurrence_that_hour_belonged_to_is_refused_whole(self, monkeypatch):
        """The detector used to take whatever mktime normalised 01:00 to and
        measure a three-hour night as a four-hour one. A window an hour of
        which did not happen is refused, the same way a window half inside the
        range is."""
        _in_zone(monkeypatch, "Europe/London")

        assert window.bounds(NIGHT, date(2026, 3, 29)) is None

    def test_it_costs_one_occurrence_and_not_the_range_around_it(self, monkeypatch):
        _in_zone(monkeypatch, "Europe/London")
        start = int(datetime(2026, 3, 27, tzinfo=timezone.utc).timestamp())

        found = window.windows(NIGHT, start, start + 4 * DAY)

        assert [day for day, _s, _e in found] == [date(2026, 3, 27), date(2026, 3, 28),
                                                  date(2026, 3, 30)]

    def test_an_hour_the_clock_repeated_is_still_an_hour_that_happened(self, monkeypatch):
        """The autumn change is not the same case and must not be refused: the
        time is ambiguous rather than absent, and either instant is a real one.
        """
        _in_zone(monkeypatch, "Europe/London")

        resolved = window.instant(date(2026, 10, 25), 1, 30)

        assert resolved is not None
        assert time.localtime(resolved).tm_hour == 1
        assert window.bounds(NIGHT, date(2026, 10, 25)) is not None

    def test_a_calendar_date_that_did_not_exist_is_refused_too(self, monkeypatch):
        """Samoa skipped 30 December 2011 entirely. Normalising it lands on the
        same wall-clock time one day later, which is the one shape a check on
        the hours alone would wave through."""
        _in_zone(monkeypatch, "Pacific/Apia")

        assert window.instant(date(2011, 12, 30), 0, 0) is None
        assert window.instant(date(2011, 12, 29), 0, 0) is not None
        assert window.bounds(NIGHT, date(2011, 12, 30)) is None


# --------------------------------------------------------------------------
# F-WP4: a model's time, the first fold, and where a local day begins
# --------------------------------------------------------------------------
class TestLocalEpoch:
    def test_now_and_both_separators(self):
        assert window.local_epoch("now", 123) == 123
        assert window.local_epoch("2026-06-01 07:30:15", 0) == _utc(2026, 6, 1, 7) + 30 * 60 + 15
        assert window.local_epoch("2026-06-01T07:30", 0) == _utc(2026, 6, 1, 7) + 30 * 60

    @pytest.mark.parametrize("junk", [None, 5, "", "yesterday", "2026-02-30 00:00:00",
                                      "2026-06-01 24:00:00"])
    def test_anything_else_is_none(self, junk):
        assert window.local_epoch(junk, 0) is None


class TestFirstInstant:
    def test_an_ambiguous_time_is_the_earlier_one(self, monkeypatch):
        _in_zone(monkeypatch, "Europe/Bratislava")
        # 2026-10-25 02:30 happens at 00:30 UTC (CEST) and again at 01:30 (CET).
        assert window.first_instant(date(2026, 10, 25), 2, 30) == _utc(2026, 10, 25, 0) + 1800

    def test_a_skipped_time_is_none(self, monkeypatch):
        _in_zone(monkeypatch, "Europe/Bratislava")
        assert window.first_instant(date(2026, 3, 29), 2, 30) is None

    def test_an_ordinary_time_is_instant(self, monkeypatch):
        _in_zone(monkeypatch, "Europe/Bratislava")
        day = date(2026, 6, 1)
        assert window.first_instant(day, 7, 15) == window.instant(day, 7, 15)


class TestDayStart:
    def test_utc_midnight(self):
        assert window.day_start(date(2026, 6, 1)) == _utc(2026, 6, 1)

    def test_a_23_and_a_25_hour_day(self, monkeypatch):
        _in_zone(monkeypatch, "Europe/Bratislava")
        def length(d):
            return window.day_start(d + timedelta(days=1)) - window.day_start(d)
        assert length(date(2026, 3, 29)) == 23 * 3600
        assert length(date(2026, 10, 25)) == 25 * 3600
        assert length(date(2026, 6, 1)) == DAY

    def test_a_zone_that_moves_its_clocks_at_midnight_starts_the_day_at_one(self, monkeypatch):
        _in_zone(monkeypatch, "America/Santiago")
        # 2026-09-06 00:00 -04 never happens: the clock jumps to 01:00 -03.
        start = window.day_start(date(2026, 9, 6))
        assert time.localtime(start)[:5] == (2026, 9, 6, 1, 0)
        assert time.localtime(start - 1)[:3] == (2026, 9, 5)

    def test_a_skipped_date_has_length_zero(self, monkeypatch):
        _in_zone(monkeypatch, "Pacific/Apia")
        assert window.day_start(date(2011, 12, 30)) == window.day_start(date(2011, 12, 31))
