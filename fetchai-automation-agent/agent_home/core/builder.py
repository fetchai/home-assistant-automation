"""Candidate to automation: the config a user is actually asked about.

Design §7 stage 5 is "propose", and between a detector's candidate and the
approval gate there is a step nobody else does — turning a measurement into a
rule. `core/detectors/standby.py` says *this device drew 12 W across four hours
on twenty-one of the last thirty nights*. That is a fact about a house. What
the user has to answer in one word (§9) is a change to it, and this module is
where the fact becomes the change.

Deterministic, and for the same reason the detector is. §7.1 draws the line —
"the model proposes candidates; it does not get to grade them" — and a builder
that asked an LLM to write the YAML would put the model back inside the one
step that has to be reproducible: the config is what replay (WP5) is graded
against and what the estimate beside it describes, so a config that could come
out differently on two runs would make the number beside it unfalsifiable. No
model, no prompt, no import of core/agent_runtime.py.

It also writes nothing to a home. `build` returns a config; `gate.propose`
raises it and `gate.apply` writes it. The one exception is preflight's own
test-create, which is discussed below.


THE NIGHT SHAPE, AND WHY IT IS THIS ONE
---------------------------------------
Off at the start of the detector's night window, on again at its end, with a
condition that the device is genuinely idle at the moment the rule fires:

    01:00  ->  if the power sensor reads below the standby ceiling  ->  turn off
    05:00  ->  turn on, unconditionally

Three independent arguments land on it.

*It is what the evidence supports.* The detector measures a plateau across its
own window — `standby.NIGHT`, whose two ends are the triggers below and whose
length is what the kWh figure was multiplied by. A rule that cut power outside
that window would save an amount nobody measured, and the figure printed beside
it in the digest would then belong to a different automation. §7.2's whole
instruction is to constrain the claim rather than inflate it, and this is that
rule applied to the config rather than to the prose.

*It is what replay can honestly validate.* A time trigger needs no series at
all — it fires at 01:00 whatever the record says — and the condition is one
instant per night, which is exactly what a jitter-filtered transition log
answers well. A `numeric_state` trigger with a `for:` would be the shape the
record is least faithful to (NUMERIC_MIN_INTERVAL_SECONDS is a 60-second floor,
and NUMERIC_MAX_GAP_SECONDS a fifteen-minute heartbeat — see WP4's docstring),
so the rule easiest to write would be the rule validated worst.

*It is what the user can live with.* Cutting power to a television and leaving
it cut means a remote that does nothing, and the thing the household would
normally reach for is the thing that stopped working. Turning it back on at
05:00 makes the change invisible except on the bill. That is also why the
readback below states the dead hours in plain words: a user who learns about
them at 02:00 has learned them from the wrong source.

The hours and the ceiling are imported from the detector, never restated — the
hours as core/window.py's spec rather than as the numbers inside it, so that a
detector proposing some other window is a config built from a different spec
and not a second copy of this shape. If the two could ever disagree about when
the window is, the estimate would stop describing the automation it is printed
beside.

The two ends are the ONLY thing this reads off the spec, and two windows this
shape cannot express are refused in `_standby_shape` rather than approximated
there. One naming its weekdays would need a `condition: time` with a `weekday:`
list, and a config that quietly ignored the days it was given would be graded
over occurrences it would never have fired on — that window has a shape of its
own now, and the next section is it. One crossing midnight would run correctly
in Home Assistant and then be discarded by `replay.plan_for`, which bounds the
span by subtracting one clock time from the other — a refusal that names the
config's trigger order rather than the window that could not be written.


ONE AUTOMATION, NOT TWO
-----------------------
Both triggers live in one config, branching on the trigger id. A pair of
automations would be two journal rows the user answered with one word, which
§9 forbids ("never a single yes covering several changes"), and rolling back
would then be two operations where core/rollback.py recorded one — the second
of which nothing would ever run. The branch is `choose` on `trigger.id`
rather than a top-level condition, because a top-level `numeric_state` would
gate the 05:00 turn-on as well and the device would never come back.


THE ID, AND THE PREFLIGHT THAT DOES NOT COLLIDE WITH IT
-------------------------------------------------------
`automation_id_for` derives the id from the suggestion key by hash, so the same
candidate cannot mint two. Home Assistant's own editor uses milliseconds since
the epoch, which is the opposite property: it is different every time it is
asked, and a review pass that built the same candidate twice would leave two
automations doing the same thing in one house.

The obvious worry is that preflight test-creates and then deletes, and a
preflight running against the real id would delete the automation it was
checking. It does not: `_validate_by_test_create` mints a fresh
`new_automation_id()` for the temporary write and never sees the id computed
here. The stable id and the preflight do not touch.

What they do NOT do is reach the write. `tool_ha_create_automation` mints its
own id at execution time and `gate._created_inverse` reads the id back out of
the tool result — so a change proposed through the create tool is written at
Home Assistant's id, not at this one, and today the stable id is an identity
this module can compute rather than one the home ends up carrying. That is
recorded here rather than worked around, because working around it would mean
either proposing an update against an automation that does not exist yet (which
`gate._inverse_for` would journal as `one_way` — a change advertised as
irreversible that is nothing of the kind) or teaching the create tool a new
argument, which is a change to what the model is shown. Neither belongs in the
builder.


TWO PREFLIGHTS, AND NEITHER IS REDUNDANT
----------------------------------------
`gate.propose` preflights too. This one is still wanted, because they run at
different moments and answer for different things.

This one answers "is what I just built a config at all". Its caller (WP7) does
more with the result than propose it: WP5's replay grades it and
`suggestions.mark_validated` stores it on the suggestion row, and a stored
config Home Assistant would reject is a suggestion the user can accept and the
agent can never apply — a promise recorded in the audit trail that nothing can
keep. Catching it here means the candidate simply is not validated.

The gate's answers "is this still a config now, at the moment I am asking
someone about it". A suggestion may be validated on Monday and proposed on
Friday; in between an entity can disappear and a Home Assistant upgrade can
tighten a schema. `gate.apply` preflights a third time for the same reason,
and that is not redundant either.

The cost of doing it here is that `build` is not pure — it makes a round trip
to Home Assistant. So the judgement is in `build_config`, which is pure and
takes a candidate literal, and `build` is the thin layer that validates it.
Same split as core/observer.py against core/observation.py, for the same
reason: the rules have to be exercisable without a home.


TWO SHAPES, DISPATCHED ON KIND
------------------------------
core/detectors/schedule.py asks a different question. Not "was the draw flat
across 01:00-05:00" but "which stretch of the WEEK is this device idle across",
and its answer is a window that names its weekdays — 07:00-17:00 Mon-Fri, the
pattern docs/PROPOSAL-SCHEDULE-DETECTOR.md opens with. That is precisely the
window the section above refuses, having said the per-weekday shape belonged to
the work package that introduced the window. This is that shape.

The dispatch is on the candidate's `kind` and on nothing else. Not on the
evidence: a third detector arriving without a shape of its own has to fail
loudly here, and a builder that sniffed a blob for a field it recognised would
instead hand it one of these two and write a rule about a measurement it never
read.

WHERE THE WEEKDAYS GO, AND WHY THERE IS ONE PLACE FOR THEM
The restriction is a `condition: time` carrying a `weekday:` list, and it goes
in the CUTTING branch, beside the trigger-id condition and the idle guard. Not
at the top level, for the reason the night shape's empty `condition: []`
already gives one step further on: a top-level condition gates the restoring
branch too, and a rule that can leave a device off from Friday evening until
Monday morning is not a rule whose saving anybody may compute. core/replay.py
states the same thing as a refusal — `plan_for` will not grade a config whose
restoring branch carries a guard — so this is not a preference between two
placements that both work. It is the only one.

Restricting the cut alone is COMPLETE rather than a simplification, and the
guarantee comes from the detector rather than from an assumption made here:
schedule.py's runs do not wrap midnight (its decision 5, argued at `_runs_for`
and enforced by a loop that stops at slot 47), so both ends of every window it
proposes fall on the same weekday. There is no occurrence whose cut is
restricted and whose restore is not. What the unconditional turn-on leaves
behind is the night shape's safety net, unchanged: a device switched off by
hand on a Saturday is switched back on the next morning either way, and a rule
that only ever restores power is the one kind of daily noise this pipeline is
willing to write.

THE GUARD IS THIS DEVICE'S OWN FLOOR, NOT THE GLOBAL CEILING
The night shape writes `standby.STANDBY_MAX_WATTS`. This one writes the
`active_floor` the schedule candidate's own evidence carries — the threshold
every one of its 336 cells was actually judged against. The asymmetry is
deliberate, and it is the proposal's own: moving standby onto a per-device
number would change automations users have already approved, so §5 files it
there as an optional follow-on. No schedule automation exists in any house, so
here the per-device number is free, and it is the number the evidence printed
beside it was measured with. A guard at 30 W under a card claiming the device
is idle below 8 W would be two different claims about one rule, and the user is
asked to answer both in one word.

The raw-versus-watts discipline is unchanged, and it is the thing that is
easiest to get backwards: `below` is compared against the sensor's RAW reading
and is therefore divided by the scale, while every sentence a person reads
quotes WATTS. `_readback` explains what getting that the wrong way round costs.

THE WINDOW IS REBUILT FROM THE EVIDENCE, NEVER FROM A CONSTANT
There is no `schedule.SCHEDULE` to import the way `standby.NIGHT` is imported:
this window is a finding about one device, not a declaration about the house.
So the spec is rebuilt from `evidence["schedule"]` — `start`, `end` and the
weekday numbers, which schedule.py writes as a VALUE beside the prose label
precisely so that nothing downstream has to parse the sentence. That is the
readback's rule applied to the config: a proposal raised last week has to
describe the automation it would actually write, and a window taken from
anywhere but the stored blob is a different window. A blob whose `schedule` is
missing or malformed, or which describes a window core/window.py refuses, is a
refusal in a plain sentence rather than an approximation — same as every other
refusal here.
"""
import hashlib
import json
import logging
import math

from core import window
from core.detectors import power
from core.detectors import schedule
from core.detectors import standby
from tools.tool_homeassistant.automations import _preflight_automation

logger = logging.getLogger(__name__)

# Prefixed so that an automation this agent wrote is recognisable as one in
# Home Assistant's own storage, where every other id is a millisecond
# timestamp. Sixteen hex characters of SHA-256 over the suggestion key: long
# enough that two keys will not collide, short enough to read in a URL, and a
# pure function of the key so the identity survives a restart, a re-scan and a
# reinstall without being stored anywhere.
ID_PREFIX = "fetchai_"
ID_DIGEST_CHARS = 16

# The trigger ids the action branches on. Named rather than positional because
# `choose` reads them back by name, and a reader of the YAML should not have to
# count triggers to know which branch is which.
TRIGGER_OFF = "night_start"
TRIGGER_ON = "night_end"

# The alias this module builds, minus the device's name. Named because
# `_parts_of` reads the name back out of a stored alias, and a suffix written
# out twice is a suffix the two copies can disagree about.
ALIAS_SUFFIX = " off overnight"

# ...and the schedule shape's own three. Separate constants rather than the
# three above reworded, and that is not tidiness: "night_start" and "off
# overnight" are wrong English for a rule that cuts power at 07:00 on a
# Tuesday, and the three above are IN PEOPLE'S automations.yaml — `_parts_of`
# reads those ids back out of a stored config and matches that suffix
# literally, so a config written last month would stop being recognisable as
# one of this module's the moment either moved by a character.
#
# The suffix says "on a schedule" rather than anything about the day: a run
# `schedules` finds is most often the daytime one the proposal is about, but a
# two-hour evening window is the same shape, and an alias that guessed would be
# the one piece of this text somebody reads six months later in Home Assistant.
SCHEDULE_TRIGGER_OFF = "schedule_start"
SCHEDULE_TRIGGER_ON = "schedule_end"
SCHEDULE_ALIAS_SUFFIX = " off on a schedule"

# The two shapes as `_parts_of` recognises them coming back IN: the pair of
# trigger ids it matches on and the alias suffix it strips. A table rather than
# two branches of if, because which pair of ids is present is the only thing
# that distinguishes the shapes in a stored config and a reader should be able
# to see that in one place.
#
# The name each row carries is the shape's identity, and it is what `_parts_of`
# hands back: WHICH pair of ids matched is the only honest answer to "what
# shape is this", and re-deriving it downstream from some other field is how a
# config comes to be rendered by the wrong renderer.
NIGHT_SHAPE = "night"
SCHEDULE_SHAPE = "schedule"
_SHAPES = (
    (NIGHT_SHAPE, TRIGGER_OFF, TRIGGER_ON, ALIAS_SUFFIX),
    (SCHEDULE_SHAPE, SCHEDULE_TRIGGER_OFF, SCHEDULE_TRIGGER_ON, SCHEDULE_ALIAS_SUFFIX),
)

# Home Assistant's spelling of the weekdays, in date.weekday() order so the
# index IS the weekday number core/window.py uses. Here rather than in
# core/window.py for `_time_at`'s reason: the seam renders a window for a
# person, and this renders one part of it for a schema to parse.
_HA_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_NUMBERS = {name: number for number, name in enumerate(_HA_WEEKDAYS)}


def automation_id_for(key: str) -> str:
    """The stable Home Assistant automation id for a suggestion key."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return f"{ID_PREFIX}{digest[:ID_DIGEST_CHARS]}"


def _time_at(hour: int, minute: int = 0) -> str:
    """A wall-clock time as Home Assistant's `time` trigger spells it.

    Home Assistant's spelling, which is why the formatting is here and not in
    core/window.py: the seam renders a window for a person to read, and this
    renders one end of it for a schema to parse.
    """
    return f"{int(hour):02d}:{int(minute):02d}:00"


def _label(entity_id: str, name: str = "") -> str:
    """What to call the device in text a person reads.

    A friendly name if the caller has one — it comes from Home Assistant, where
    the household already named the thing. Failing that the entity id is turned
    into words rather than printed raw: "switch.living_room_tv" reads as
    "Living room tv" in an alias, and an alias is the one place this text is
    seen months later by someone who never saw the suggestion.
    """
    if name and str(name).strip():
        return str(name).strip()
    tail = str(entity_id).split(".", 1)[-1]
    return tail.replace("_", " ").strip().capitalize() or str(entity_id)


def _endpoints(candidate: dict):
    """(power sensor, switch) out of a candidate, or (None, reason).

    Read from the evidence first and the entity list second, because the
    evidence names the two roles and the list only orders them — and an
    ordering is the kind of thing a second detector could get backwards.

    The switch's domain is checked rather than assumed. `switch.turn_off` on a
    `light.` entity is not a service that exists, and a config built on one
    would fail preflight with a message about Home Assistant rather than about
    the pairing that was wrong.
    """
    evidence = candidate.get("evidence") or {}
    # Indexed, never compacted: the list is two roles in a fixed order, and
    # dropping a missing one would slide the switch into the sensor's slot and
    # build a rule that watched the thing it was about to switch off.
    entities = candidate.get("entities") or []
    sensor = evidence.get("entity") or (entities[0] if len(entities) > 0 else None)
    switch = evidence.get("switch") or (entities[1] if len(entities) > 1 else None)
    if not sensor:
        return None, "the candidate names no power sensor"
    if not switch:
        # WP4 discards these before they become candidates; this is the same
        # rule stated where the config would otherwise be built, so a second
        # detector cannot arrive without it.
        return None, f"{sensor} has no controllable switch, so there is nothing to switch off"
    domain = str(switch).split(".", 1)[0]
    # core/detectors/power.py's, not standby.py's — the same frozenset either
    # way, but this module now serves two detectors and asking the night one
    # what a switch is would make the schedule shape depend on it for nothing.
    if domain not in power.CONTROLLABLE_DOMAINS:
        return None, (f"{switch} is not something whose supply can be cut "
                      f"(expected one of: {', '.join(sorted(power.CONTROLLABLE_DOMAINS))})")
    return (sensor, switch), None


def _readback(label: str, cut_at: str, restore_at: str, ceiling_w, evidence: dict) -> str:
    """§9 artefact 3, in two sentences: what will be written, plainly.

    The second sentence is the one that has to be there. Everything else about
    this change is invisible to the household, and the part that is not — a
    device that does not answer between one and five in the morning — is the
    part somebody will otherwise meet at 02:00 with no idea why.

    One truthful description of one config, and every value in it is handed in
    rather than read off this module's constants. `_standby_shape` passes what
    it is about to write; `readback` below passes what a stored config actually
    says. Reading the constants here instead would make this sentence describe
    the rule the agent WOULD build today, which for a proposal raised a week
    ago is not the rule the user is being asked about.

    The ceiling is quoted in WATTS, deliberately, and is not the number in the
    config. `below` is compared against the sensor's raw reading, so on a
    sensor reporting kilowatts it is 0.03 — and "less than 0.03 W" is a figure
    wrong by a thousand in the one artefact the approval rests on. Prose is
    read by a person and watts is the unit a person has, so the two numbers
    state the same threshold in different units rather than disagreeing
    about it.
    """
    watts = evidence.get("watts")
    kwh = evidence.get("kwh_per_month")
    saving = ""
    if watts is not None and kwh is not None:
        saving = (f" It drew about {watts} W every night without being used, "
                  f"which is roughly {kwh} kWh a month.")
    return (
        f"At {cut_at} every night the agent will switch "
        f"{label} off at the socket, but only if it is drawing less than "
        f"{ceiling_w} W at that "
        f"moment — so it is left alone if it is still in use. It is switched back on at "
        f"{restore_at}, and in between it is genuinely off: it will "
        f"not respond to its remote, an app, or Home Assistant until then.{saving}"
    )


def _span_phrase(cut_at: str, restore_at: str) -> str:
    """"for those 10 hours " — or "in between " when the two ends do not
    describe a window core/window.py will measure.

    The trailing space travels with the phrase for `_description`'s reason: a
    clause that guards its value but not the words around it is how a
    half-finished sentence reaches the one artefact the approval rests on.
    """
    try:
        span = window.hours(window.spec(cut_at, restore_at))
    except (TypeError, ValueError):
        return "in between "
    return f"for those {span} hours "


def _schedule_readback(label: str, cut_at: str, restore_at: str, days,
                       floor_w, evidence: dict) -> str:
    """§9 artefact 3 for a rule that runs on named weekdays.

    `_readback`'s two sentences with the days named, and with the second one
    pushed harder rather than softened. The proposal's §6 is explicit about
    why: "the daytime window is the riskier one, because a wrong cut at 11:00
    on a Tuesday meets somebody who is at home, where a wrong cut at 03:00
    usually does not". The night shape's dead hours are hours nobody is awake
    for. These are not, and §6's other half is that this pipeline cannot tell
    idle from unwanted at all — a DVR, a printer, a console mid-download are
    all idle and all needed — so the sentence saying which hours the device
    will not work in is the whole of what the user is being asked to check.

    The threshold is quoted in WATTS, with the same danger `_readback`
    describes: the config's `below` is the sensor's RAW unit, and on a kW
    sensor the two numbers differ by a factor of a thousand. `floor_w` may be
    None for a stored config whose evidence no longer carries the level it was
    measured against; the clause then states the guard without a number rather
    than dropping the guard, because "it is left alone if it is still in use"
    is the part being agreed to and the number is not what makes it true.
    """
    guard = (f"drawing less than {floor_w} W" if floor_w is not None
             else "drawing little enough to be idle")
    # "every day" rather than seven names: a seven-day rule is one a household
    # says no days about, and window.days_phrase is not asked to render the set
    # `window.label` deliberately prints nothing for.
    when = (window.days_phrase(days)
            if days and set(days) != set(window.EVERY_DAY) else "every day")
    watts = evidence.get("watts")
    kwh = evidence.get("kwh_per_month")
    saving = ""
    if watts is not None and kwh is not None:
        saving = (f" It drew about {watts} W across that stretch without being used, "
                  f"which is roughly {kwh} kWh a month.")
    return (
        f"At {cut_at} on {when} the agent will switch {label} off at the socket, "
        f"but only if it is {guard} at that moment — so it is left alone if it is "
        f"still in use. It is switched back on at {restore_at}, and "
        f"{_span_phrase(cut_at, restore_at)}it is genuinely off: it will not respond "
        f"to its remote, an app, or Home Assistant until then. Those are hours "
        f"somebody may well be at home, so if the device turns out to be wanted "
        f"during them this is the part of the change the household will meet.{saving}"
    )


def _floor_from(evidence):
    """This device's own in-use floor, out of a schedule candidate's evidence,
    or None.

    `active_floor` first because it is the detector's field, travelling whole
    out of `schedule.device_levels`; then `thresholds.idle_below_w`, the same
    number recorded where a reader goes looking for the rules rather than for
    the finding. There is deliberately no constant behind the two:
    `standby.STANDBY_MAX_WATTS` is a claim about every device in the house, and
    falling back to it would write a guard the evidence printed beside it never
    measured anything against.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    for value in (evidence.get("active_floor"),
                  (evidence.get("thresholds") or {}).get("idle_below_w")):
        try:
            floor = float(value)
        except (TypeError, ValueError):
            continue
        # isfinite and positive, for the reason `scale` is checked that way in
        # `build_config`: `below: NaN` serialises, preflights and then makes
        # every comparison false — an approved rule that never switches
        # anything off.
        if math.isfinite(floor) and floor > 0:
            return floor
    return None


def _parts_of(config):
    """(shape, label, cut_at, restore_at, weekdays) read out of a config this
    module wrote, or None if it is not one.

    Both shapes, matched on the pair of trigger ids in `_SHAPES` — which is the
    only thing that tells them apart on the way back in, and why those ids may
    never be reworded. The match is RETURNED rather than left here, because
    `readback` has to pick a renderer and the trigger ids are the only field
    that answers: deciding from the weekday list instead would render a night
    rule somebody hand-edited a `condition: time` into with the schedule's
    prose, and a schedule rule whose restriction was edited away with the
    night's global ceiling — a threshold the config does not contain, in the
    one artefact the approval rests on. `weekdays` is reported beside it as
    what the config actually says, and is None for a cut nothing restricts.

    Only the fields the readback quotes, and only from the config itself. The
    switch is not needed: the alias already names the device in the words the
    household will see in Home Assistant, which is the same name the readback
    should use.
    """
    if not isinstance(config, dict):
        return None
    times = {}
    for trigger in config.get("trigger") or []:
        if isinstance(trigger, dict) and trigger.get("id"):
            times[trigger["id"]] = str(trigger.get("at") or "")[:5]
    for shape, off_id, on_id, suffix in _SHAPES:
        if not (times.get(off_id) and times.get(on_id)):
            continue
        alias = str(config.get("alias") or "")
        label = alias[:-len(suffix)] if alias.endswith(suffix) else alias
        return (shape, label or "the device", times[off_id], times[on_id],
                _weekdays_of(config))
    return None


def label_of(config) -> str:
    """The device's name out of a stored alias, with whichever suffix this
    module appended taken off again — or "" when there is no alias at all.

    Public, and here rather than in core/artefacts.py, because the suffix is
    this module's: `_config` writes it and `_SHAPES` is the one place both of
    them are named. A renderer that stripped the night's suffix by hand read a
    schedule automation's alias whole and put "Office printer off on a
    schedule" on the card as the device's name.

    Falls back to the alias as it stands, which is what a config somebody
    hand-edited has. `_parts_of` is the stricter reader and refuses anything
    this module did not write; this one is only after the name, and an alias
    nobody recognises still names the device better than an entity id does.

    The suffix is matched against the alias BEFORE it is stripped of
    whitespace, because every suffix in `_SHAPES` opens with a space: an alias
    that is exactly one of them is a device with no name, and trimming first
    would leave it not matching and hand back "off overnight" as what to call
    the thing.
    """
    alias = str((config or {}).get("alias") or "")
    for _shape, _off_id, _on_id, suffix in _SHAPES:
        if alias.endswith(suffix):
            return alias[:-len(suffix)].strip()
    return alias.strip()


def _weekdays_of(config):
    """The weekday numbers a stored config restricts its cut to, or None.

    Read out of the choose branches rather than off a remembered position, and
    without caring which branch carries it: only the cutting one ever does, so
    a scan cannot pick the wrong branch. A day name Home Assistant does not
    spell is dropped rather than guessed at, and a restriction naming nothing
    this module recognises comes back None — a readback that says the rule runs
    every day is wrong in a way the user can see in the diff beside it, where
    one naming a day the config does not is not.
    """
    actions = config.get("action") or []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    for branch in first.get("choose") or []:
        if not isinstance(branch, dict):
            continue
        for condition in branch.get("conditions") or []:
            if isinstance(condition, dict) and condition.get("condition") == "time":
                days = {_WEEKDAY_NUMBERS[name]
                        for name in condition.get("weekday") or []
                        if name in _WEEKDAY_NUMBERS}
                return sorted(days) or None
    return None


def readback(config, evidence, name: str = "", ceiling_w=None) -> str:
    """§9 artefact 3 for a config that is already stored: "" if it is not a
    config this module wrote.

    WP8's surfaces show the readback beside a proposal that may have been
    raised days ago, and the suggestion row holds the config rather than the
    sentence. Rebuilding it from the CONFIG — its trigger times, its alias —
    rather than from the detector's constants is what keeps the sentence a
    description of the automation that will actually be written.

    Either shape: `_parts_of` says which, by the pair of trigger ids in the
    config and by nothing else, and the two sentences are rendered by the
    function written for that shape rather than by one that reads both
    vocabularies.

    The threshold is the one value that cannot be read off the config as it
    stands: `below` is in the sensor's raw units and the scale that converted
    it is not stored there. So it is taken in order of how well each source
    knows THIS config — `ceiling_w` from the caller (core/replay.py graded the
    config and recorded `guard.below_w`, the same threshold in watts), then the
    thresholds recorded beside the measurement, and only then the live
    constant. The last is a genuine fallback rather than a preference: a
    constant that moved between the proposal and the answer would state a
    threshold the config does not contain — and for a schedule config there is
    no such constant at all, which is `_floor_from`'s whole point. A schedule
    readback whose evidence has lost the level simply does not quote one.
    """
    parts = _parts_of(config)
    if not parts:
        return ""
    shape, label, cut_at, restore_at, days = parts
    evidence = evidence if isinstance(evidence, dict) else {}
    label = str(name).strip() or label
    if shape == NIGHT_SHAPE:
        ceiling = ceiling_w
        if ceiling is None:
            ceiling = (evidence.get("thresholds") or {}).get("ceiling_w")
        if ceiling is None:
            ceiling = standby.STANDBY_MAX_WATTS
        return _readback(label, cut_at, restore_at, ceiling, evidence)
    floor = ceiling_w if ceiling_w is not None else _floor_from(evidence)
    return _schedule_readback(label, cut_at, restore_at, days, floor, evidence)


def _description(label: str, evidence: dict) -> str:
    """The trace left inside Home Assistant itself.

    Everywhere else the reasoning lives in the add-on's journal and its
    suggestion row. Someone editing automations in Home Assistant six months
    from now is not looking at either, and an automation that appeared on its
    own with no explanation is the kind of thing people delete for being
    unexplained rather than for being wrong.
    """
    nights = evidence.get("nights")
    examined = evidence.get("nights_examined")
    watts = evidence.get("watts")
    # Named for the key it is read from, and not `window`, which is the seam
    # this module imports: the label stored on a proposal raised last week is
    # not necessarily what the live spec renders today, and a reader has to be
    # able to see which of the two this sentence quotes.
    night_window = evidence.get("night_window")
    # Each clause carries its own connecting words. Guarding the value but not
    # the preposition around it is how "measured continuously between  without
    # being used" gets written into automations.yaml — a half-finished sentence
    # in the one place this reasoning is visible from inside Home Assistant.
    drew = f" drawing about {watts} W" if watts is not None else ""
    between = f" between {night_window}" if night_window else ""
    seen = f" on {nights} of the last {examined} nights" if nights and examined else ""
    return (
        f"Written by the Fetch home agent, and approved by you. {label} was measured{drew}"
        f" continuously{between}{seen} without being used, so it is switched off at the "
        f"socket overnight and switched back on in the morning. Delete this automation to stop it."
    )


def _schedule_description(label: str, evidence: dict) -> str:
    """`_description` for a weekly schedule, in that detector's own words.

    Not one function reading either blob. schedule.py's decision 6 is that its
    evidence speaks in OCCURRENCES because "nights" would be a lie about a
    window that happens at 11:00 on a Tuesday, and a description that fell back
    from one set of names to the other is exactly where the two vocabularies
    would quietly become one. What this leaves inside Home Assistant is read by
    whoever finds the automation in six months, and it should say what was
    actually measured.

    Each clause carries its own connecting words, for `_description`'s reason:
    a guarded value inside an unguarded preposition is how "measured
    continuously across  without being used" reaches automations.yaml.
    """
    occurrences = evidence.get("occurrences")
    examined = evidence.get("occurrences_examined")
    watts = evidence.get("watts")
    # Named for the key it is read from, not for the seam: the label stored on
    # a proposal raised last week is not necessarily what the live spec renders
    # today, and a reader has to see which of the two this sentence quotes.
    schedule_window = evidence.get("schedule_window")
    drew = f" drawing about {watts} W" if watts is not None else ""
    across = f" across {schedule_window}" if schedule_window else ""
    seen = (f" on {occurrences} of the last {examined} occurrences"
            if occurrences and examined else "")
    return (
        f"Written by the Fetch home agent, and approved by you. {label} was measured{drew}"
        f" continuously{across}{seen} without being used, so it is switched off at the "
        f"socket for those hours and switched back on at the end of them. Delete this "
        f"automation to stop it."
    )


def _spec_from(evidence: dict):
    """(spec, None) | (None, refusal): the window a schedule candidate found,
    rebuilt from its own evidence.

    `evidence["schedule"]` holds `start`, `end` and the weekday numbers, which
    schedule.py writes as a value beside its prose label precisely so that this
    is a reconstruction rather than a parse of the sentence a person is shown.
    Nothing here reads a constant: the night shape can read `standby.NIGHT`
    because that window is a declaration about the house, and this one is a
    FINDING about one device — a proposal raised last week must still describe
    the automation it would actually write, which is the same rule `readback`
    follows for the config.

    What comes back is validated rather than trusted. A stored blob can be
    missing the field, can hold something that is not a mapping, and can
    describe a window `window.spec` refuses outright — all three are a refusal
    in a plain sentence, because an approximation here is a rule graded over
    occurrences it would never have fired on.

    `days` is required rather than defaulted, and that is the same rule rather
    than an extra one. Defaulting a missing one to every day is not a neutral
    reading of a truncated blob — it WIDENS the rule, cutting the supply on the
    two days the evidence never looked at, beside a kWh figure
    `schedule.occurrences_per_month` scaled by the weekdays the window did
    name. A window whose days are unknown is a window nobody measured.
    """
    blob = (evidence or {}).get("schedule")
    if not isinstance(blob, dict):
        return None, ("this candidate does not record the window it found, so there is "
                      "nothing to write a schedule from")
    if blob.get("days") is None:
        return None, ("the window this candidate records does not say which days it "
                      "happens on, and a rule cannot be widened to all of them")
    try:
        found = window.spec(blob.get("start"), blob.get("end"), blob["days"])
    except (TypeError, ValueError) as error:
        return None, (f"the window this candidate records cannot be written as a "
                      f"rule: {error}")
    return found, None


def _no_midnight(spec):
    """The refusal both shapes make about a window crossing midnight, written
    once.

    The schedule shape cannot produce one — `schedules` builds its runs from a
    loop that stops at local midnight (its decision 5) — and it is checked
    anyway, because this module takes a candidate rather than reads a detector
    and the cost of being wrong is not a bad config. `window.spec("00:00",
    "00:00")` RAISES, and a stored blob arriving from anywhere else would turn
    the daily review pass into a traceback that takes every other suggestion,
    verification and rollback due that morning with it.
    """
    if window.spans_midnight(spec):
        return ("this automation shape cuts and restores on one date, so it cannot "
                f"express {window.label(spec)}, which crosses midnight")
    return None


def _config(label: str, suffix: str, sensor: str, switch: str,
            cut_at: str, restore_at: str, off_id: str, on_id: str,
            below, cut_conditions: list, description: str) -> dict:
    """The one automation both shapes are, with the parts that differ handed in.

    One assembler rather than two, because ONE AUTOMATION, NOT TWO above is an
    argument about the structure and not about the night: two triggers in one
    config branching on `trigger.id`, so that one journal row answers one word
    and core/rollback.py has one operation to reverse. A second copy of this
    dict is a second place for that to stop being true.

    `cut_conditions` is everything the cutting branch carries BEYOND the
    trigger-id match and the idle guard — empty for the night shape, and the
    weekday restriction for the schedule one. It goes here and nowhere else;
    see WHERE THE WEEKDAYS GO in the module docstring for why the top-level
    condition is not an option.
    """
    return {
        "alias": f"{label}{suffix}",
        "description": description,
        "trigger": [
            {"platform": "time", "at": cut_at, "id": off_id},
            {"platform": "time", "at": restore_at, "id": on_id},
        ],
        # Empty on purpose, in both shapes. The idle check belongs to the
        # cutting branch alone — here it would also have to be true at the
        # restore, and a device that is off reads 0 W, so the turn-on would
        # fire only when the turn-off had not. The weekday restriction is the
        # same argument with a different consequence: at the top level it would
        # gate the restore too, and a device cut on Friday would stay cut until
        # Monday. core/replay.py refuses both, from the other end.
        "condition": [],
        "action": [
            {
                "choose": [
                    {
                        "conditions": [
                            {"condition": "trigger", "id": off_id},
                            *cut_conditions,
                            # The whole point of the guard: the detector's
                            # claim is about a typical occurrence, and this is
                            # the one occurrence it is being acted on. A device
                            # still working when the rule fires is left alone.
                            {"condition": "numeric_state", "entity_id": sensor,
                             "below": below},
                        ],
                        "sequence": [
                            {"service": "switch.turn_off",
                             "target": {"entity_id": switch}},
                        ],
                    },
                    {
                        "conditions": [
                            {"condition": "trigger", "id": on_id},
                        ],
                        # Unconditional, in both shapes. Restoring power is
                        # what makes the change invisible, and a condition here
                        # is a way for the device to stay off all day — which
                        # is also what `replay.plan_for` refuses to grade.
                        "sequence": [
                            {"service": "switch.turn_on",
                             "target": {"entity_id": switch}},
                        ],
                    },
                ]
            }
        ],
        "mode": "single",
    }


def _standby_shape(sensor: str, switch: str, label: str, evidence: dict, scale: float):
    """The night shape: ((config, readback), None) or (None, refusal).

    The window is read off `standby.NIGHT` — the spec, never the numbers inside
    it — so that the rule written is the rule the estimate beside it describes
    however the detector declares its window.

    Two windows this shape cannot express are REFUSED rather than built. The
    refusal is the rule core/replay.py states for an unknown trigger platform —
    an operation the table does not know is refused, never improvised — applied
    one step earlier, and it is here rather than downstream because a refusal
    that arrives at the grader names the wrong thing.

    A window that does not happen every day: the triggers below fire on all
    seven, and a config that ignored the five it was given would be replayed
    over occurrences it would never have fired on and proposed with a saving
    measured over occurrences it would. That window now has a shape of its own
    — `_schedule_shape` — and this is still a refusal rather than a hand-off,
    because a candidate's KIND says which shape it gets and `standby.NIGHT`
    naming weekdays would mean the night detector had changed its mind about
    what it measures, not that this one is a schedule.

    And a window that crosses midnight, which `_no_midnight` argues.
    """
    spec = standby.NIGHT
    if spec.days != window.EVERY_DAY:
        return None, ("this automation shape fires every day, so it cannot express a "
                      f"window measured only on {window.label(spec)}")
    error = _no_midnight(spec)
    if error:
        return None, error

    cut_at = _time_at(spec.start_hour, spec.start_minute)
    restore_at = _time_at(spec.end_hour, spec.end_minute)
    # The ceiling in the units the sensor actually reports. Rounded, because a
    # float division renders as 0.030000000000000002 in the YAML the user
    # reads, and six places is far below anything a power sensor resolves.
    ceiling = round(standby.STANDBY_MAX_WATTS / scale, 6)

    config = _config(label, ALIAS_SUFFIX, sensor, switch, cut_at, restore_at,
                     TRIGGER_OFF, TRIGGER_ON, ceiling, [],
                     _description(label, evidence))
    return (config, _readback(label, cut_at[:5], restore_at[:5],
                              standby.STANDBY_MAX_WATTS, evidence)), None


def _schedule_shape(sensor: str, switch: str, label: str, evidence: dict, scale: float):
    """The weekly shape: ((config, readback), None) or (None, refusal).

    The window comes from the evidence (`_spec_from`) and the guard from the
    device's own in-use floor (`_floor_from`) — the two decisions the module
    docstring argues. Neither has a constant behind it, and that is the point:
    the night shape is a rule about the house that happens to be applied to a
    device, and this is a rule about one device that nothing else in the house
    shares.
    """
    spec, error = _spec_from(evidence)
    if error:
        return None, error
    error = _no_midnight(spec)
    if error:
        return None, error

    floor_w = _floor_from(evidence)
    if floor_w is None:
        # No global fallback, deliberately. Every cell this candidate rests on
        # was judged against this device's own floor, and a guard written from
        # `standby.STANDBY_MAX_WATTS` instead would be a threshold nothing in
        # the evidence beside it ever measured against — two claims about one
        # rule, answered with one word.
        return None, ("this candidate records no in-use level for the device, so there "
                      "is no threshold to tell idle from in use when the rule fires")

    cut_at = _time_at(spec.start_hour, spec.start_minute)
    restore_at = _time_at(spec.end_hour, spec.end_minute)
    # Rounded for the reason the night shape's ceiling is, and then CHECKED for
    # the reason the NaN guard in `build_config` exists. The night shape
    # divides a constant and cannot round it away; this one divides whatever
    # the evidence recorded, and a fraction of a watt on a sensor reporting
    # kilowatts comes out as `below: 0.0` — a condition that is never true,
    # which preflights cleanly and is approved by a user who then has an
    # automation that silently never switches anything off.
    below = round(floor_w / scale, 6)
    if below <= 0:
        return None, ("this device's in-use level is too small to write as a threshold "
                      f"in the units {sensor} reports")

    days = sorted(spec.days)
    # Written even when the window names all seven days. It is one line of YAML
    # either way, and it is what makes the config self-describing: `_parts_of`
    # reads the days back OUT of a stored config, and a shape that omitted them
    # when they happened to be complete would leave the readback unable to tell
    # a seven-day rule from one whose restriction had been edited away.
    weekdays = {"condition": "time", "weekday": [_HA_WEEKDAYS[day] for day in days]}

    config = _config(label, SCHEDULE_ALIAS_SUFFIX, sensor, switch, cut_at, restore_at,
                     SCHEDULE_TRIGGER_OFF, SCHEDULE_TRIGGER_ON, below, [weekdays],
                     _schedule_description(label, evidence))
    return (config, _schedule_readback(label, cut_at[:5], restore_at[:5], days,
                                       floor_w, evidence)), None


# kind -> the shape that answers for it. A table rather than a chain of ifs
# because the refusal below is the important branch: a detector with no entry
# here has to fail loudly, and a lookup that comes back None says so without
# anyone having remembered to write the else.
_SHAPE_FOR = {
    standby.KIND: _standby_shape,
    schedule.KIND: _schedule_shape,
}


def build_config(candidate: dict, name: str = "", scale: float = 1.0):
    """The judgement, pure: a candidate in, (result, None) or (None, error) out.

    `result` is {"key", "kind", "automation_id", "config", "config_json",
    "readback"} — everything the caller needs to replay it, store it on the
    suggestion row and hand it to the gate, and nothing about a conversation.

    The shape is chosen by the candidate's `kind` and by nothing else, out of
    `_SHAPE_FOR`. Everything up to that point is common to both — the endpoints,
    the label, the key, the scale — and everything that is a claim about the
    window or the threshold belongs to the shape.

    `config_json` is built here rather than left to the caller so that the
    string preflighted below is byte-for-byte the string proposed. A caller
    re-serialising the dict itself could validate one config and ask about
    another.

    `scale` is watts per reported unit — `power.power_scale`'s answer for this
    sensor, and the caller has it. It matters because the condition compares
    the sensor's RAW reading against the threshold: a sensor reporting
    kilowatts needs `below: 0.03`, and `below: 30` on one would be a condition
    that is always true and a device switched off mid-film. The default is 1.0
    because watts is what almost every power sensor reports, and it is the only
    default that is honest for a caller that did not think about it.
    """
    if not isinstance(candidate, dict) or not candidate:
        return None, "no candidate to build from"
    kind = candidate.get("kind")
    shape = _SHAPE_FOR.get(kind)
    if shape is None:
        # One shape per detector, dispatched on kind rather than sniffed out of
        # the evidence: a third detector arriving without one must fail here,
        # not silently get one of these two applied to whatever it found.
        return None, f"no automation shape is defined for a {kind!r} candidate"
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 0.0
    # isfinite as well as positive, because NaN compares False against every
    # operator and would sail through a bare `<= 0`. What it produces is worse
    # than a refusal: `below: NaN` serialises into the config, json.loads
    # accepts it so preflight passes it on, and every numeric comparison
    # against NaN is false — an automation the user approved that silently
    # never switches anything off.
    if not math.isfinite(scale) or scale <= 0:
        return None, "the sensor's unit scale is unknown, so no threshold can be written"

    endpoints, error = _endpoints(candidate)
    if error:
        return None, error
    sensor, switch = endpoints

    evidence = candidate.get("evidence") or {}
    # The kind is part of the fallback key as well as of the dispatch, which is
    # what keeps one device's two suggestions apart: `schedule:sensor.tv_power`
    # and `standby:sensor.tv_power` hash to different automation ids, so a
    # television with both a night plateau and a weekday one carries two rules
    # rather than one overwriting the other.
    key = candidate.get("key") or f"{kind}:{sensor}"
    label = _label(switch, name)

    built, error = shape(sensor, switch, label, evidence, scale)
    if error:
        return None, error
    config, readback_text = built

    return {
        "key": key,
        "kind": kind,
        "automation_id": automation_id_for(key),
        "config": config,
        "config_json": json.dumps(config),
        "readback": readback_text,
    }, None


def build(candidate: dict, name: str = "", scale: float = 1.0) -> dict:
    """`build_config`, validated against the home. Returns
    {"ok": True, ...the result...} or {"ok": False, "error"}.

    The test-create is the same `_preflight_automation` a model-raised config
    goes through — imported rather than reimplemented, for the reason
    core/rollback.py gives about inverses: a second implementation of "is this
    valid" is a second answer that can differ from the one the gate will give
    at approval time. It is also the only Home Assistant traffic this module
    makes, and it writes nothing that survives the call.

    A config that will not validate is not a suggestion. The error is returned
    to the caller rather than logged and swallowed, because the caller is the
    review pass and its correct response is to not validate the candidate at
    all — not to propose something it has been told is broken.
    """
    result, error = build_config(candidate, name=name, scale=scale)
    if error:
        return {"ok": False, "error": error}

    preflight_error = _preflight_automation(result["config_json"])
    if preflight_error:
        logger.info(f"BUILDER: {result['key']} did not preflight — "
                    f"{str(preflight_error)[:200]}")
        return {"ok": False, "error": preflight_error}

    return {"ok": True, **result}
