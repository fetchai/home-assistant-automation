"""The disclosure boundary — what may leave the house.

`core/watchlist.py` decides what the agent may WRITE DOWN. This decides what it
may SAY. They are the same kind of thing and they are deliberately shaped the
same way: one small module, one rule, checkable by reading it.

Everything a tool returns is fed to ASI-1, which is a hosted model. So the set
of tool answers IS the set of things that leave the house — there is no other
egress. Until this module existed that set was whatever each tool happened to
build, which is not a boundary, it is a habit.

The rule
--------

A tool answer may carry **names, states, times, counts, aggregates and
automation configs**. It may not carry a **credential**, a **network address**,
a **geographic coordinate**, or an **unbounded structure**.

Names are the explicit exception and the reason the product works: "Martin's
office lamp" has to arrive at the model intact, so a field holding a name
(NAME_FIELDS) is never inspected for content. Everything else is, including
entity states — a state that is literally an IP address is network layout, and
that is the one thing the design says stays local regardless of who asked.

Where it is enforced
--------------------

At the only place data leaves: every tool returns through `emit()`, which is
`json.dumps` with the rule applied. `tests/test_disclosure.py` asserts that no
module under `tools/` calls `json.dumps` directly, so a new tool cannot quietly
route around this — the invariant is one grep, not a convention.

`scan()` is the same walk without the rewriting: it reports what a payload
would have leaked. That is what lets a test drive a tool against a fixture full
of secrets and assert nothing came through, rather than asserting on the
particular shape of the answer.

What each tool actually emits
-----------------------------

The audit this module was written from, so the table is somewhere a reader can
check it against the code rather than against the design doc:

| Tool                                | Emits                                              |
|-------------------------------------|----------------------------------------------------|
| Datetime get tool                   | local clock, date, weekday, timezone name          |
| List Home Assistant entities        | entity_id, friendly_name, state, domain, area      |
| Get Home Assistant entity details   | the same, plus last_changed/last_updated and the   |
|                                     | entity's attributes — FILTERED here, and the only  |
|                                     | attribute blob that leaves at all                  |
| List Home Assistant areas           | area_id, name, aliases, entity counts, domains     |
| List entities in HA area            | entity_id, friendly_name, domain, state, area      |
| List Home Assistant devices         | device_id, name, manufacturer, model, area,        |
|                                     | entity_ids                                         |
| List Home Assistant services        | HA's own capability catalogue — service names and  |
|                                     | parameter schemas. Vendor metadata, no home data   |
| Call Home Assistant service         | the call, and the {entity_id, state} it changed    |
| Get HA entity history               | up to 200 timestamped transitions from HA's        |
|                                     | recorder, plus the window's summary. Attributes    |
|                                     | are never fetched (`no_attributes` on the wire)    |
| List HA automations                 | automation_id, entity_id, alias, state             |
| Get HA automation                   | one automation's stored config, in full            |
| Create/Update/Delete HA automation  | ids, alias, warnings                               |
| Get behavioural record overview     | watch counts, record totals, listener health,      |
|                                     | per-entity id/name/state/first-seen/last-seen —    |
|                                     | watched entities only, totals over those (2.21.4)  |
| Get entity behaviour profile        | aggregates, plus up to 200 transitions from the    |
|                                     | record — for a watched entity only                 |
| Get observed patterns               | entity pairs, their names, counts, first/last seen |
|                                     | — both entities watched                            |
| Query the behavioural record        | analyst only: rows of `ev` / `rel`, watched        |
|                                     | entities only, ≤ 500 rows and 32 KB per answer     |
| Get entity timeline                 | analyst only: runs of ≤ 5 watched entities over    |
|                                     | ≤ 21 days, ≤ 300 runs each                         |
| Get pending messages                | the agent's own queued titles and bodies           |

Two of those were the holes worth naming. `Get Home Assistant entity details`
returned Home Assistant's whole state object verbatim, and an attributes dict
is exactly where a home's secrets live: `latitude`/`longitude` on a person or
device_tracker, `entity_picture` carrying a signed camera token, `ip_address`
and `mac` on anything network-aware, `access_token` on a camera. And the
listener's `last_error` in the record overview is a raw exception string, which
for a request failure carries the URL it was made against.

Automation configs
------------------

They leave in full because that is what the model edits, and because a config
the model cannot read back exactly is a config it will corrupt on the next
update. The one thing withheld from them is a credential — a `webhook_id` is a
bearer token by another name. That would break the read-modify-write round trip
on its own, so `automations.update_automation` puts withheld values back from
the config that is actually stored before it writes; see `restore()`.
"""
import json
import re

# --------------------------------------------------------------------------
# Bounds. "Unbounded" is a disclosure problem in its own right: a media_player
# carrying base64 artwork or a weather entity's forecast is not a secret, but
# it is an arbitrary amount of the house arriving somewhere else.
# --------------------------------------------------------------------------
MAX_KEYS = 100
MAX_ITEMS = 500
MAX_CHARS = 2000
MAX_DEPTH = 12

# The marker left behind. One prefix for all of them, because `restore()` and
# `has_withheld()` need to recognise the result without knowing the reason.
WITHHELD = "<withheld"


def _withheld(reason: str) -> str:
    return f"{WITHHELD}: {reason}>"


# --------------------------------------------------------------------------
# The rules.
# --------------------------------------------------------------------------

# Fields whose value is a name or an identifier the user chose, and which are
# therefore never inspected. This list is the privacy claim's cost, stated
# openly: an IP address typed into a friendly name reaches the model.
NAME_FIELDS = frozenset({
    "alias", "aliases", "area", "area_id", "area_query", "areas",
    "automation_id", "available_areas", "available_domains", "called",
    "device_id", "domain", "domains", "entities", "entity_id", "entity_ids",
    "floor_id", "friendly_name", "kind", "manufacturer", "matched_areas",
    "model", "name", "names", "service", "source", "then", "then_name",
    "timezone", "trigger", "trigger_name",
})

# Key markers that withhold the value they label, grouped by what they are.
# Matched on whole words, as a contiguous run — so `wan_ip_address` matches
# ("ip",) and `ghost_mode` does not match ("host",).
_DENIED_KEYS = (
    ("credential", (
        ("token",), ("password",), ("passwd",), ("passphrase",), ("secret",),
        ("api", "key"), ("apikey",), ("private", "key"), ("encryption", "key"),
        ("credential",), ("credentials",), ("authorization",), ("cookie",),
        ("session", "id"), ("webhook", "id"), ("pin",), ("entity", "picture"),
    )),
    ("network address", (
        ("ip",), ("ipv4",), ("ipv6",), ("mac",), ("ssid",), ("bssid",),
        ("hostname",), ("host",), ("url",), ("uri",), ("port",),
        ("serial",), ("imei",), ("iccid",),
    )),
    ("location", (
        ("latitude",), ("longitude",), ("gps",),
    )),
)

_WORDS = re.compile(r"[^a-z0-9]+")

# Value shapes. These run over everything that is not a name field, including
# entity states.
_VALUE_RULES = (
    ("credential", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*")),
    ("credential", re.compile(r"(?i)\bbearer\s+\S+")),
    ("credential", re.compile(
        r"(?i)\b(?:access[_-]?token|api[_-]?key|apikey|token|password|passwd"
        r"|secret|authorization)\b\s*[=:]\s*[^\s,;&\"']+")),
    ("credential", re.compile(r"(?i)\b[0-9a-f]{32,}\b")),
    ("network address", re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.I)),
    ("network address", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    # IPv6 and MAC in one: four or more hex groups joined by ONE separator,
    # the same one throughout — hence the backreference. A clock ("10:30:00")
    # and a date ("2026-08-27") have two separators rather than three, and the
    # mixed form is what the suggestion surfaces emit constantly: a night
    # window reads "01:00-05:00", four groups with `:`, `-`, `:`, which this
    # rule used to withhold — replacing design §9's whole evidence sentence
    # with a marker. No address mixes its separators, so requiring one kind
    # throughout costs nothing and keeps every time range this agent renders.
    ("network address",
     re.compile(r"(?i)\b[0-9a-f]{1,4}([:\-])[0-9a-f]{1,4}(?:\1[0-9a-f]{1,4}){2,6}\b")),
)


def _tokens(key) -> tuple:
    return tuple(t for t in _WORDS.split(str(key).lower()) if t)


def _denied_key(key) -> str:
    """The reason this key's value is withheld, or "" if it is allowed."""
    words = _tokens(key)
    for reason, markers in _DENIED_KEYS:
        for marker in markers:
            n = len(marker)
            if any(words[i:i + n] == marker for i in range(len(words) - n + 1)):
                return reason
    return ""


def _scalar(value):
    return not isinstance(value, (dict, list, tuple))


def _flat(value):
    """True if this value carries content rather than shape.

    A denied key withholds a scalar or a list of scalars — `{"ip": "10.0.0.4"}`
    is a secret. It does NOT withhold a structure: `{"url": {"selector": ...}}`
    in Home Assistant's service catalogue is a parameter named url, not a url,
    and dropping it would cost the model a capability for nothing. The
    recursion reaches whatever is inside it anyway, under its own keys.
    """
    if _scalar(value):
        return True
    return isinstance(value, list) and all(_scalar(v) for v in value)


def _string(value, path, out):
    """One string, checked against the value rules and the length bound.

    A value that is already a marker is left alone and not reported. That is
    what makes the walk idempotent — `scan(emit(x))` is empty for every x —
    which is the property the tests actually lean on: they run a tool, scan its
    answer, and expect nothing, without having to know which fields the tool
    had to withhold.
    """
    if WITHHELD in value:
        return value
    for reason, pattern in _VALUE_RULES:
        if pattern.search(value):
            out.append(f"{path or '<root>'}: {reason}")
            return _withheld(reason)
    if len(value) > MAX_CHARS:
        out.append(f"{path or '<root>'}: {len(value)} characters")
        return value[:MAX_CHARS] + _withheld(f"{len(value) - MAX_CHARS} more characters")
    return value


def _walk(value, path, out, depth=0, exempt=False):
    """Applies the rule to one value, collecting what it had to withhold.

    One walk does both jobs — `emit` keeps the rewritten value, `scan` keeps the
    findings — so the two can never disagree about where the boundary is.

    `exempt` is carried down exactly one level: a scalar under a name field, or
    a scalar inside a list under one. A dict under a name field is a structure
    with keys of its own and is checked normally.
    """
    if depth > MAX_DEPTH:
        out.append(f"{path or '<root>'}: nested deeper than {MAX_DEPTH}")
        return _withheld("nested structure")

    if isinstance(value, dict):
        result = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= MAX_KEYS:
                out.append(f"{path or '<root>'}: {len(value)} keys")
                result[_withheld(f"{len(value) - MAX_KEYS} more keys")] = None
                break
            # A key can carry content too — `seconds_per_state` is keyed by the
            # state itself — so it goes through the same check as a value.
            safe_key = _string(str(key), f"{path}.<key>" if path else "<key>", out)
            reason = _denied_key(key)
            here = f"{path}.{key}" if path else str(key)
            if reason and _flat(item):
                if not has_withheld(item):
                    out.append(f"{here}: {reason}")
                result[safe_key] = _withheld(reason)
            else:
                result[safe_key] = _walk(item, here, out, depth + 1,
                                         exempt=str(key) in NAME_FIELDS)
        return result

    if isinstance(value, (list, tuple)):
        items = list(value)
        result = [_walk(v, f"{path}[{i}]", out, depth + 1, exempt)
                  for i, v in enumerate(items[:MAX_ITEMS])]
        if len(items) > MAX_ITEMS:
            out.append(f"{path or '<root>'}: {len(items)} items")
            result.append(_withheld(f"{len(items) - MAX_ITEMS} more items"))
        return result

    if isinstance(value, str):
        return value if exempt else _string(value, path, out)

    return value


# --------------------------------------------------------------------------
# The boundary itself.
# --------------------------------------------------------------------------

def emit(payload) -> str:
    """A tool answer, as JSON, with the rule applied.

    Every tool returns through this instead of json.dumps. That is the whole
    enforcement: one call at the one place where data leaves.
    """
    return json.dumps(_walk(payload, "", []))


def text(value: str) -> str:
    """The same rule for an answer that is prose rather than JSON — the clock
    tool, and any error string built from an exception."""
    return _walk(str(value), "", [])


def scan(payload) -> list:
    """What this payload would disclose, as a list of "path: reason".

    Empty means it is inside the boundary. This is what tests hold: drive a
    tool against a fixture full of secrets, scan the answer, and expect nothing
    — which stays true however the answer's shape changes.
    """
    out = []
    _walk(payload, "", out)
    return out


def has_withheld(value) -> bool:
    """True if anything in this structure came back through the boundary."""
    if isinstance(value, str):
        return WITHHELD in value
    if isinstance(value, dict):
        return any(has_withheld(k) or has_withheld(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_withheld(v) for v in value)
    return False


def restore(value, previous):
    """Puts withheld values back from `previous`, as (ok, value-or-error).

    The read-modify-write problem, and the only honest answer to it. The model
    reads an automation config through the boundary, edits it, and writes the
    whole thing back; a `webhook_id` it never saw would be written back as the
    marker, which is not a redaction, it is data loss. So the write path pairs
    the incoming config with the one Home Assistant actually holds and restores
    what was withheld at the same position.

    A marker with nothing behind it fails rather than being written: a config
    that cannot be reconstructed is a config that must not be saved.
    """
    if isinstance(value, str) and WITHHELD in value:
        if previous is None or not isinstance(previous, (str, int, float, bool)):
            return False, "a withheld value could not be restored from the stored config"
        return True, previous

    if isinstance(value, dict):
        prev = previous if isinstance(previous, dict) else {}
        out = {}
        for key, item in value.items():
            ok, restored = restore(item, prev.get(key))
            if not ok:
                return False, f"{restored} (at '{key}')"
            out[key] = restored
        return True, out

    if isinstance(value, (list, tuple)):
        prev = previous if isinstance(previous, list) else []
        out = []
        for i, item in enumerate(value):
            ok, restored = restore(item, prev[i] if i < len(prev) else None)
            if not ok:
                return False, f"{restored} (at index {i})"
            out.append(restored)
        return True, out

    return True, value
