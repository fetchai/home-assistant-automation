"""What a window IS: a stretch of the household's clock, and the arithmetic
over it.

The suggestion pipeline reasons about a period of the day. `core/detectors/
standby.py` asks whether a device's draw was flat across 01:00-05:00,
`core/replay.py` places that period on every date in the record, `core/
builder.py` writes its two ends into an automation and `core/verify.py`
measures the same hours again a week later. Until now "a night" was a pair of
integer hours living in the detector, and each of those modules reached in for
them — so the period was not a thing anyone could pass, only a constant
everybody knew.

This module is that thing. A spec is data: two wall-clock times and the
weekdays it happens on. Everything a caller needs to do with one — place it on
a date, enumerate it over a range, print it, ask how long it is — is here, and
nowhere else. Nothing in it knows what standby draw is, and it imports nothing
from core/ at all, which is what makes it testable against a list literal and
what stops a second detector having to inherit the first one's vocabulary.

BUILT AHEAD OF ITS CALLER, DELIBERATELY
---------------------------------------
Two things here have no caller today. A window may name its weekdays
(`WEEKDAYS`), and a window may cross midnight (`22:00-06:00`). The detector
that needs the first is the next work package; nothing is planned that needs
the second.

They are here anyway because the cost of adding them later is not the code, it
is re-opening every module this seam was extracted to close. A spec that could
only describe a same-day, every-day window would put `day + 1` and a weekday
filter back into `bounds`, `windows` and `label` — the three functions whose
whole purpose is that nobody downstream writes calendar arithmetic again. The
generality is in the SHAPE of the spec rather than in speculative features:
there is no recurrence language here, no exceptions, no per-date overrides, and
a window this cannot describe should be refused rather than approximated.

ONE ANSWER ABOUT THE CLOCK, AND IT IS THE STRICT ONE
-----------------------------------------------------
`instant` is the only place in the pipeline that turns a wall-clock time into a
unix second, and it answers None when the time did not happen. There were two
implementations of this before the seam and they disagreed: the detector called
mktime and took whatever came back, so on a spring-forward date where 01:00
never existed it silently measured a neighbouring instant and called the result
a four-hour night; replay round-tripped the answer through localtime and could
therefore tell that the time did not exist, which is what let it report "there
was no trigger" rather than "the guard failed".

The strict one is authoritative, for two reasons. It is the one that carries
more information — a lenient answer can be built from a strict one by ignoring
the None, and a strict answer cannot be recovered from a lenient one at all —
and it is the rule `windows` already applies in the other dimension. A window
half inside the range is refused there because a partly-covered occurrence is
evidence for something else; an occurrence an hour of which did not happen is
the same refusal, made about the clock instead of about the range. The cost is
that a home loses one occurrence a year, on the date the clocks go forward, out
of a window that needs ten of thirty — which is the direction this pipeline
fails in everywhere else.
"""
from collections import namedtuple
from datetime import date, datetime, timedelta
import re
import time

# Weekdays as date.weekday() numbers them: Monday is 0. Named rather than
# written as literals at call sites, because 0 meaning Monday is exactly the
# kind of convention that gets read as Sunday once.
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)
EVERY_DAY = frozenset(range(7))
WEEKDAYS = frozenset({MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY})
WEEKENDS = frozenset({SATURDAY, SUNDAY})

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# How many days in a row have to be named before they are printed as a range.
# "Mon-Fri" is how a household says it; "Sat-Sun" is not, and neither is
# "Mon-Tue". Below this they are listed.
_RUN_MIN = 3


# The spec itself: two wall-clock times and the days it happens on. A tuple
# because a window is a value — two specs describing the same hours are equal,
# it can be a dict key, and it prints readably in a log line, none of which is
# true of a dict. `days` holds the weekday numbers the window STARTS on: an
# occurrence that crosses midnight belongs to the evening it began in, which is
# how a person names the night of a Monday.
Window = namedtuple("Window", "start_hour start_minute end_hour end_minute days")


def spec(start: str, end: str, days=EVERY_DAY) -> Window:
    """A window from two "HH:MM" times, validated at the point it is written.

    Constructed from the strings it prints as, so that a constant declared at
    the top of a detector reads as the sentence a user will be shown. Every
    refusal here raises rather than returning None: these are module-level
    declarations, and a window nobody can describe is a bug to be found at
    import rather than a None to be handled at midnight.
    """
    start_hour, start_minute = _hhmm(start)
    end_hour, end_minute = _hhmm(end)
    if (start_hour, start_minute) == (end_hour, end_minute):
        # Not "a whole day": a window with no ends is one nothing can be
        # measured across, and guessing which of the two readings was meant is
        # how a detector comes to claim twenty-four hours of anything.
        raise ValueError(f"a window from {start} to {end} has no span")
    days = frozenset(int(day) for day in days)
    if not days or not days <= EVERY_DAY:
        raise ValueError(f"{sorted(days)} is not a set of weekdays")
    return Window(start_hour, start_minute, end_hour, end_minute, days)


def _hhmm(value):
    """"HH:MM" as (hour, minute), or a refusal."""
    parts = str(value).split(":")
    if len(parts) != 2:
        raise ValueError(f"a window time is written HH:MM, not {value!r}")
    try:
        hour, minute = (int(part) for part in parts)
    except (TypeError, ValueError):
        raise ValueError(f"a window time is written HH:MM, not {value!r}")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{value!r} is not a time of day")
    return hour, minute


# --------------------------------------------------------------------------
# what a spec says about itself
# --------------------------------------------------------------------------
def spans_midnight(window: Window) -> bool:
    """Whether the window ends on the day after it starts."""
    return (window.end_hour, window.end_minute) <= (window.start_hour, window.start_minute)


def minutes(window: Window) -> int:
    """The window's nominal length in minutes — what the CLOCK says it is.

    Nominal, and the distinction matters twice a year: on the dates the clocks
    move, the elapsed seconds between the two ends are an hour more or less
    than this. Callers measuring a saving want the clock's answer, because that
    is what the automation's two triggers describe; callers bounding an elapsed
    span want the smaller of the two, which is `bounds`' business rather than
    this one's.
    """
    span = ((window.end_hour * 60 + window.end_minute)
            - (window.start_hour * 60 + window.start_minute))
    return span + 24 * 60 if span <= 0 else span


def hours(window: Window):
    """The nominal length in hours: an int where that is whole, else a float.

    The int is not tidiness. This figure is written into the evidence blob as
    `hours_per_day` and those blobs are on disk on live installs, where a
    four-hour night has always been the integer 4; a window of eight and a half
    hours is 8.5 and could never have been anything else.
    """
    span = minutes(window)
    return span // 60 if span % 60 == 0 else span / 60.0


def clock(hour: int, minute: int) -> str:
    """A wall-clock time as a person reads it."""
    return f"{int(hour):02d}:{int(minute):02d}"


def starts_at(window: Window) -> str:
    return clock(window.start_hour, window.start_minute)


def ends_at(window: Window) -> str:
    return clock(window.end_hour, window.end_minute)


def label(window: Window) -> str:
    """The window as §9 prints it, in one place.

    Written into every evidence blob and every baseline, and compared against
    by core/verify.py to decide whether two measurements taken a week apart
    were taken over the same hours. Two implementations of one label would make
    that comparison fail on the punctuation — which is also why the every-day
    form carries no weekday phrase at all rather than a redundant "every day":
    homes are mid-verification against "01:00-05:00 local" written last week,
    and a label that gained a word would report a real saving as unmeasurable.
    """
    days = "" if window.days == EVERY_DAY else f" {days_phrase(window.days)}"
    return f"{starts_at(window)}-{ends_at(window)} local{days}"


def days_phrase(days) -> str:
    """The named days, as a household would say them.

    Public because `label` is not the only sentence that has to name them:
    core/builder.py's readback for a per-weekday rule says which days the
    device will be unreachable on, and it has the weekday numbers rather than
    a spec to render. Two implementations of "Mon-Fri" would be free to
    disagree in a place a user reads both.

    Runs are read from Monday forwards and never wrapped around the end of the
    week: "Sun, Mon" is two days a person names separately, and rendering it as
    a range would put the week's ends in an order nobody writes.
    """
    runs = []
    for day in sorted(days):
        if runs and runs[-1][-1] == day - 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    out = []
    for run in runs:
        if len(run) >= _RUN_MIN:
            out.append(f"{_DAY_NAMES[run[0]]}-{_DAY_NAMES[run[-1]]}")
        else:
            out.extend(_DAY_NAMES[day] for day in run)
    return ", ".join(out)


# --------------------------------------------------------------------------
# placing one on the calendar
# --------------------------------------------------------------------------
def _local_date(ts):
    """The local calendar date an instant falls on."""
    return datetime.fromtimestamp(int(ts)).date()


def instant(day, hour: int, minute: int = 0, second: int = 0):
    """The unix second a wall-clock time falls on, or None if it never happened.

    THE one answer about the clock — see the module docstring for why it is the
    strict one and what that costs.

    The C library resolves the clock change rather than this module knowing any
    zone's rules. What mktime will NOT do is tell you the time did not exist: on
    a skipped hour it normalises to a neighbouring instant and returns it without
    complaint. So each answer is round-tripped through localtime, and a resolved
    instant the local clock disagrees with is the clock change rather than the
    time that was asked for.

    A time the clock shows TWICE (the repeated hour when the clocks go back) is
    the EARLIER of its two instants, and it is asked for by name: mktime is
    called with tm_isdst 0 and 1 and the earlier survivor kept. tm_isdst -1
    would leave the choice to the C library, and glibc — the add-on image's,
    python:3.12-slim — makes it from the offset of whichever mktime call came
    before in the process, so one wall time gave two answers depending on what
    ran first (CI, 2026-09-26: a fall-back cell measured as 30 minutes after a
    winter-time call, 90 after a summer one). musl always picks the earlier, so
    the dev host never saw it. The earlier is also what every caller needs:
    a cell that starts in the repeated hour covers both passes through it, and
    Home Assistant's time trigger fires in the first fold (see `first_instant`).

    The DATE is compared as well as the time, and not for symmetry: a zone can
    skip a whole calendar day (Samoa did, in December 2011), and normalising
    that lands on the same wall-clock time one day later — the one shape a
    time-only comparison would wave through.
    """
    found = []
    for isdst in (0, 1):
        try:
            ts = int(time.mktime(
                (day.year, day.month, day.day, hour, minute, second, 0, 0, isdst)))
        except (OverflowError, ValueError):
            continue
        local = time.localtime(ts)
        if ((local.tm_year, local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min,
             local.tm_sec) == (day.year, day.month, day.day, hour, minute, second)):
            found.append(ts)
    return min(found) if found else None


def bounds(window: Window, day):
    """(start, end) unix seconds of the occurrence beginning on one local date,
    or None when the clock skipped over either end.

    The date is the one the window STARTS on. A window that crosses midnight
    ends on the day after, which is what makes "the night of the 14th" one
    occurrence rather than two halves of different dates.
    """
    start = instant(day, window.start_hour, window.start_minute)
    end_day = day + timedelta(days=1) if spans_midnight(window) else day
    end = instant(end_day, window.end_hour, window.end_minute)
    if start is None or end is None:
        return None
    return start, end


def windows(window: Window, start, end) -> list:
    """Every occurrence falling WHOLE inside [start, end), oldest first, as
    (local date it began on, start, end).

    Whole, because a partly-covered occurrence is a shorter observation dressed
    up as a full one: a claim about four hours is not weakened by having two of
    them, it becomes a claim about something else. The same rule is what
    `instant` applies to an occurrence the clock ate an hour of.

    Dates the spec does not name are skipped rather than returned empty, so a
    caller counting what it examined counts occurrences and never days.
    """
    start, end = int(start), int(end)
    out = []
    day = _local_date(start)
    last = _local_date(end)
    while day <= last:
        if day.weekday() in window.days:
            occurrence = bounds(window, day)
            if occurrence and occurrence[0] >= start and occurrence[1] <= end:
                out.append((day, occurrence[0], occurrence[1]))
        day += timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# reading a time a model wrote, and the edges of a local day
# --------------------------------------------------------------------------
# D-4's format. The separator is `T` as ISO writes it, or a space as
# `tool_record._local_iso` prints it: the model copies what it was shown.
_LOCAL_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")


def local_epoch(value, now):
    """A local time as a model wrote it -> unix seconds, or None.

    `"now"` is `now`. A local `YYYY-MM-DD[T ]HH:MM[:SS]` is that wall time in
    the household's zone, through `instant` — so a time the clock skipped is
    None, never a neighbouring instant. Anything else is None. Here rather
    than in core/analyst.py because core/charts.py reads the same strings (a
    chart request's start and end) and must not import the analyst."""
    if not isinstance(value, str):
        return None
    if value == "now":
        return int(now)
    match = _LOCAL_TIME.fullmatch(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g) if g else 0 for g in match.groups())
    try:
        on = date(year, month, day)
    except ValueError:
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    return instant(on, hour, minute, second)


def first_instant(day, hour: int, minute: int = 0, second: int = 0):
    """The EARLIEST unix second the local clock reads this time on this date,
    or None if it never does.

    What core/charts.py asks for by this name, because it depends on the
    answer: Home Assistant's time trigger fires once on the day the clocks go
    back, in the FIRST fold (`dt_util.find_next_time_expression_time` returns
    `result.replace(fold=0)` when the next match is ambiguous). A skipped time
    is None — HA does not fire it that day, it waits for the next one.
    `instant` has given the earlier fold since 2.24.1 (it used to leave the
    choice to the C library); this name stays so the reason is said where the
    chart relies on it."""
    return instant(day, hour, minute, second)


def day_start(day):
    """The first unix second of a local date.

    Not `instant(day, 0, 0)`: a zone that moves its clocks AT midnight has
    no 00:00 on that date, and its day begins at 01:00. So this searches for
    the first second whose local date is `day` — a bisection over the 36
    hours before local noon, exact in any zone. A date the zone skipped
    entirely (Samoa, 30 December 2011) answers the next date's start, so the
    skipped day has length zero, which is what it had."""
    lo = int(time.mktime((day.year, day.month, day.day, 12, 0, 0, 0, 0, -1))) - 36 * 3600
    hi = lo + 48 * 3600
    while lo < hi:
        mid = (lo + hi) // 2
        if _local_date(mid) >= day:
            hi = mid
        else:
            lo = mid + 1
    return lo
