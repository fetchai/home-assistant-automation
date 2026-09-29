"""What a power sensor is, what can be switched off at the other end of it, and
what its readings are in watts.

Three answers every detector reasoning about power needs before it can judge a
single row, and none of them trivial: a sensor's unit is not always where you
expect it, a device with two switches is a guess nobody should make, and an
'unavailable' reading is not zero.

They lived in core/detectors/standby.py until the schedule detector needed the
identical three. Importing standby.py from another detector would make one
detector depend on another, which is backwards from how the two are meant to
relate — they are siblings reading the same record, not layers.
core/detectors/levels.py already set the precedent for exactly this situation:
the shared question moved into its own module BELOW the detectors and
standby.py became a caller of it. This is the same move. standby.py re-exports
these four names, so nothing that has always said `standby.power_scale` or
`standby.CONTROLLABLE_DOMAINS` has to move, and the reasoning below is carried
over unchanged rather than restated.

Nothing here knows what a night, a window or a plateau is, and it imports
core/observer.py and nothing else from core/ — which is what keeps it below
every detector rather than beside one.
"""
from core import observer

# The only control that removes a standing draw is cutting the device's supply,
# which in Home Assistant is the `switch` domain — a smart plug or a relay.
# Deliberately narrow: turning off a `light` or a `media_player` changes what
# the device is doing rather than whether it is fed, and the draw these
# detectors measure is what remains after the device is already "off".
CONTROLLABLE_DOMAINS = frozenset({"switch"})

# Units these detectors understand, and what they are in watts. Anything else is
# not read at all rather than assumed — a sensor in kWh is energy, not power,
# and treating its reading as a rate would invent a number.
POWER_UNITS = {"W": 1.0, "kW": 1000.0}


def power_scale(state: dict, registry_row: dict = None):
    """Watts per reported unit for a power sensor, or None if this is not one.

    device_class is the reliable signal where an integration sets it; a great
    many do not, so a plain 'W' sensor with no class is accepted too. A sensor
    declaring some OTHER class is not — an energy meter reports 'kWh' and a
    reading of 3 there is three kilowatt-hours, not three of anything per hour.

    Read from the registry row as well as the live state, and the registry
    first, because the live state is not there when it is most needed: Home
    Assistant drops a sensor's unit and capability attributes while it is
    unavailable, so a plug that happens to be offline at the moment the review
    runs would otherwise stop looking like a power sensor at all — and be
    skipped before its thirty days of recorded evidence were even read. A
    device that is reliably offline at that hour would never be detected.

    What is NOT done is guessing the unit from the device class alone. A power
    sensor may report watts or kilowatts, the difference is a factor of a
    thousand, and a saving overstated by that much is worse than a saving not
    found.
    """
    attributes = (state or {}).get("attributes") or {}
    row = registry_row or {}
    unit = row.get("unit_of_measurement") or attributes.get("unit_of_measurement")
    device_class = (row.get("device_class") or row.get("original_device_class")
                    or attributes.get("device_class"))
    if unit not in POWER_UNITS:
        return None
    if device_class not in ("power", None, ""):
        return None
    return POWER_UNITS[unit]


def controllable_switch(entity_id: str, device_id, entities_by_device) -> str:
    """The one switch on the same device as this sensor, or None.

    ONE. A device offering several switches is a multi-outlet plug or a
    multi-relay module, and which of them carries the measured load is not
    something the registry says — so nothing is paired rather than a guess
    being made that could cut power to the wrong socket. That is the same
    reasoning a detector applies when it discards an unpaired sensor: an
    unactionable observation is not a suggestion, and a wrongly-actionable one
    is worse than neither.
    """
    if not entity_id or not device_id:
        return None
    siblings = (entities_by_device or {}).get(device_id) or []
    found = [
        sibling for sibling in siblings
        if sibling != entity_id and str(sibling).split(".")[0] in CONTROLLABLE_DOMAINS
    ]
    return found[0] if len(found) == 1 else None


def readings(rows, scale: float = 1.0) -> list:
    """Every numeric reading in these rows, in watts, in the order recorded.

    The read-the-record half of asking core/detectors/levels.py what a device's
    two levels are. Both detectors ask that question, both have to read the
    rows the same way to ask it, and the two rules below are the whole of why
    that reading is not a list comprehension. What differs between the callers
    is the FLOOR each applies BEFORE asking — standby.py derives one from its
    own night count, the schedule detector lets levels.MIN_MODE_READINGS be the
    only floor there is — and that difference stays with each caller rather
    than coming here.
    """
    values = []
    for row in rows or []:
        value = observer.as_float(row.get("state"))
        if value is None:
            # 'unavailable' and 'unknown' are recorded deliberately and are not
            # zero watts. Counting them low would invent an idle level for a
            # device that was simply gone — and, worse, drag the floor down
            # until the device's own jitter cleared it.
            continue
        # A statistics row IS counted, unlike everywhere a detector asks whether
        # a value ever dropped below a floor. That rule is about a FLOOR — a
        # bucket mean cannot show that a value never went under one — and a
        # level is not a floor claim. A mean can only understate a peak, never
        # invent one, so an aggregate sitting above the idle level really was
        # above it.
        values.append(value * scale)
    return values
