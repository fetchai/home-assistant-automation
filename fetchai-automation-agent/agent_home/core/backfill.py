"""Warming the behavioural record from Home Assistant's recorder.

Without this the record starts empty at every cold start, and every hour the
add-on is switched off is a permanent hole — while Home Assistant is sitting
on up to ten days of exactly the data the agent wants. §6.3 of the design
treats that history as a bonus rather than a requirement, and so does this
module: a gap that cannot be filled is smaller coverage, never an error.

One code path covers three moments, because they are all the same question —
what happened between the last thing this entity recorded and now:

  * an entity is newly selected  →  as far back as the recorder still holds
  * the add-on starts after downtime  →  the gap it was away for
  * the websocket drops and reconnects  →  usually seconds

The conversion from a history series to record rows is pure and applies the
same core.observer rules the live loop does. That last part is load-bearing:
the recorder holds every reading, so without the numeric threshold rule a
couple of power sensors would fill the log on the first day.

Two things make this harder than "ask for the window and store the answer",
and both come from the same place — an integration that reports a state every
second or two:

  * the ANSWER cannot be held. Ten days of one such entity is 864,000 entries;
    twenty of them is gigabytes of Python objects, and the add-on is killed
    before a row is stored. So history is streamed and each chunk is converted
    and written on its own, and nothing beyond the chunk in hand exists.

  * the QUESTION cannot be asked all at once. A request must be sized to how
    often the entity really reports — measured, not assumed — and even sized,
    ten days of a one-second sensor is dozens of requests. Each such entity
    gets a request budget, spends it on the newest end where the questions
    are, and the stretch behind it is filled from Home Assistant's own
    five-minute and hourly aggregates instead.

That last fallback is deliberately the last one. An aggregate cannot carry a
crossing at the second it happened, and that is what record.scan_relations
pairs with what followed it. Coarse data at the old end is worth having;
coarse data instead of fine data is not.
"""
from datetime import datetime, timezone
import logging
import os

from core import observer
from core import record
from tools.tool_homeassistant import statistics as ha_statistics
from tools.tool_homeassistant.history import (
    BATCH_SIZE, CHUNK_SECONDS, chunk_for, is_batchable, probe_density, stream_history,
)

logger = logging.getLogger(__name__)

# How far back to look for an entity that has never been recorded. Home
# Assistant's recorder keeps ten days by default; asking for more is harmless
# (the answer is simply shorter) but there is no point asking for much more.
MAX_BACKFILL_DAYS = int(os.environ.get("RECORD_BACKFILL_DAYS", "10"))

# How many history requests one run may spend on entities that need a request
# of their own. An entity reporting every second costs roughly one request per
# four hours, so ten days of it is around sixty — affordable for one, not for
# twenty at once, and a cold start must not spend half an hour hammering the
# recorder of a house that is trying to be a house.
DENSE_REQUEST_BUDGET = int(os.environ.get("RECORD_BACKFILL_REQUESTS", "240"))

# ...but never fewer than this each, however many of them there are. Below a
# handful of requests the fine-grained window is too short to be worth the
# round trips, and the entity is better served entirely from aggregates.
MIN_REQUESTS_PER_ENTITY = 6

# Entities whose record has already been checked for history OLDER than it
# holds, this process. Resuming from the newest row is right for the ordinary
# case — the gap is always at the recent end — but it can only ever fill
# forwards, and the recorder may hold history from before the agent was
# watching at all. Looking backwards once per entity catches that; looking
# every time would turn a reconnect loop into a ten-day refetch.
_reached_back = set()


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def _rows_from(pairs, last=None, before=None) -> list:
    """The shared conversion: (ts, state) pairs into rows the record stores.

    `last` is the newest {state, ts} already recorded, so an overlapping window
    resumes rather than repeats. `before` bounds the other end, taking only
    pairs strictly older than it — which is how a gap BEFORE the record's first
    row is converted, since resuming from `last` would discard exactly those.
    Returns (ts, state, prev_state, prev_duration) tuples, oldest first — the
    shape record.record_many takes.

    Pure: no I/O, no clock. The rules applied are core.observer's, and they are
    applied here rather than in each source's own converter so that a stretch
    warmed from raw history, a stretch warmed from aggregates and a stretch
    captured live all obey the same ones.
    """
    rows = []
    for ts, state in sorted(pairs, key=lambda p: p[0]):
        # Pairs at or before the resume point are what the overlap is made of;
        # the UNIQUE index would reject them anyway, but dropping them here
        # keeps prev_duration measured against the right row.
        if last and ts <= int(last.get("ts") or 0):
            continue
        if before is not None and ts >= int(before):
            continue
        if not observer.should_record(state, ts, last):
            continue
        rows.append(observer.transition_row(
            {"entity_id": None, "state": state, "prev_state": None, "ts": ts}, last
        ))
        last = {"state": state, "ts": ts}
    return rows


def history_to_transitions(series, last=None, before=None) -> list:
    """Converts one entity's recorder history into rows the record stores.

    `series` is the recorder's entries for that entity.
    """
    pairs = []
    for entry in (series or []):
        if not isinstance(entry, dict):
            continue
        ts = observer.parse_timestamp(entry.get("last_changed") or entry.get("last_updated"))
        state = entry.get("state")
        if ts is None or state is None:
            continue
        pairs.append((ts, state))
    return _rows_from(pairs, last, before)


def _mean_as_state(mean):
    """A statistics bucket's mean as the record's TEXT state.

    Rounded, because the recorder's mean carries float noise no live reading
    ever would — "21.400000000000002" is read by a person in the panel as well
    as by should_record, and it is the same measurement either way.
    """
    try:
        value = round(float(mean), 3)
    except (TypeError, ValueError):
        return None
    return str(int(value)) if value == int(value) else str(value)


def statistics_to_transitions(buckets, last=None, before=None) -> list:
    """Converts one entity's statistics buckets into rows the record stores.

    A bucket's mean is read as the entity's state at the bucket's START, which
    is the only placement that does not invent a timestamp. The same
    core.observer rules then thin it further — a five-minute bucket is finer
    than the record's own fifteen-minute heartbeat for a numeric entity, so
    most of a flat stretch collapses exactly as a live one would.
    """
    pairs = []
    for bucket in (buckets or []):
        if not isinstance(bucket, dict):
            continue
        ts = bucket.get("ts")
        state = _mean_as_state(bucket.get("mean"))
        if ts is None or state is None:
            continue
        pairs.append((int(ts), state))
    return _rows_from(pairs, last, before)


def _needs_backward(entity_id: str, floor: int) -> bool:
    """True if this entity's record starts later than the recorder might reach,
    and that has not been checked yet this process."""
    if entity_id in _reached_back:
        return False
    first = record.first_ts(entity_id)
    return first is not None and first > floor


def _store(entity_id, entries, chains, convert, source) -> int:
    """Converts one chunk for one entity and stores it, returning rows added.

    Two chains are carried per entity because there are two gaps and they
    resume from different places: forward from the newest row the record
    already holds, and backward from nothing into the stretch older than its
    oldest. Chunks arrive in time order, so both only ever advance — which is
    what lets this run per chunk at all instead of over a whole series.
    """
    rows = []
    if chains["first"] is not None:
        backward = convert(entries, chains["backward"], before=chains["first"])
        if backward:
            chains["backward"] = {"state": backward[-1][1], "ts": backward[-1][0]}
            rows.extend(backward)
    forward = convert(entries, chains["forward"])
    if forward:
        chains["forward"] = {"state": forward[-1][1], "ts": forward[-1][0]}
        rows.extend(forward)
    return record.record_many(entity_id, rows, source=source) if rows else 0


def _chains_for(entity_id: str) -> dict:
    """Where this entity's two conversions resume from, read once.

    `first` in particular has to be captured before anything is written: it is
    the boundary the backward fill stops at, and storing rows would move it.
    """
    return {
        "forward": record.last_state(entity_id),
        "backward": None,
        "first": record.first_ts(entity_id),
    }


def _absorb_history(entity_ids, since, until, *, batch_size, chunk_seconds, added, oldest):
    """Streams raw history for these entities into the record.

    Nothing beyond the chunk in hand is held. `added` and `oldest` are filled
    in place — how many rows each entity gained, and the oldest entry the
    recorder actually produced for it, which is where the aggregates behind it
    have to start.
    """
    chains = {}
    for entity_id, entries in stream_history(entity_ids, since, until,
                                             batch_size=batch_size,
                                             chunk_seconds=chunk_seconds):
        if entity_id not in chains:
            chains[entity_id] = _chains_for(entity_id)
        if entity_id not in oldest:
            # Chunks arrive oldest first, so the first one to carry anything for
            # an entity carries its oldest entry. Worth saying rather than
            # re-scanning every chunk: for the entities this matters to, that is
            # a second timestamp parse of several hundred thousand entries.
            stamps = [observer.parse_timestamp(e.get("last_changed") or e.get("last_updated"))
                      for e in entries if isinstance(e, dict)]
            stamps = [t for t in stamps if t is not None]
            if stamps:
                oldest[entity_id] = min(stamps)
        stored = _store(entity_id, entries, chains[entity_id], history_to_transitions, "history")
        if stored:
            added[entity_id] = added.get(entity_id, 0) + stored


def _absorb_statistics(windows, added):
    """Fills each entity's remaining stretch from Home Assistant's aggregates.

    `windows` is {entity_id: (since, until)} — what raw history could not
    reach, per entity. Only entities the recorder keeps a mean for can be
    served at all; the rest keep whatever the history path managed, which for a
    fast-reporting text sensor is the recent end and nothing before it.

    Five-minute buckets first, then hourly for anything they did not cover.
    Short-term statistics are purged with the states on the user's own
    retention setting while the hourly ones are never purged, so the second
    request is what reaches past a short purge window — and it is asked for
    only where the first one came up short, per entity, so a home that keeps
    ten days never makes it.
    """
    if not windows:
        return
    with_mean = ha_statistics.mean_statistic_ids(list(windows))
    if not with_mean:
        return

    for period in ("5minute", "hour"):
        pending = {e: w for e, w in windows.items() if e in with_mean and w[0] < w[1]}
        if not pending:
            return
        # One window per request batch, so the batching stays worthwhile; each
        # entity's own bounds are applied when its answer is converted.
        span_since = min(w[0] for w in pending.values())
        span_until = max(w[1] for w in pending.values())
        buckets = ha_statistics.fetch_statistics(list(pending), span_since, span_until, period)

        for entity_id, (start, end) in pending.items():
            rows = [b for b in buckets.get(entity_id, ()) if start <= b["ts"] < end]
            if not rows:
                continue
            stored = _store(entity_id, rows, _chains_for(entity_id),
                            statistics_to_transitions, "statistics")
            if stored:
                added[entity_id] = added.get(entity_id, 0) + stored
            # Whatever the five-minute buckets reached becomes the new ceiling
            # for the hourly pass, so the two never overlap and interleave.
            windows[entity_id] = (start, min(end, min(b["ts"] for b in rows)))


def backfill(entity_ids, until: int = None) -> dict:
    """Fills each entity's gap from the recorder. Returns a per-entity count of
    rows added, plus the window covered.

    `until` is normally the instant the live subscription started: everything
    after that point is the listener's to record, and asking the recorder for
    it as well would only duplicate work the UNIQUE index then throws away.
    """
    until = _now() if until is None else int(until)
    entity_ids = [e for e in (entity_ids or []) if e]
    if not entity_ids:
        return {"added": {}, "since": until, "until": until}

    floor = until - MAX_BACKFILL_DAYS * 86400
    # Each entity resumes from its own last row; a single window covering all
    # of them is what the recorder is actually asked for, since one request
    # serves a batch.
    resume = {e: (record.last_ts(e) or floor) for e in entity_ids}
    since = min(resume.values())

    # ...but an entity whose record starts later than the recorder reaches has
    # a gap at the OTHER end, which resuming forwards can never close. That
    # happens whenever history exists for a period the agent was not watching:
    # normally only before the first selection, when resume is the floor
    # anyway, and also whenever the recorder gains older data afterwards.
    reaching_back = [e for e in entity_ids if _needs_backward(e, floor)]
    _reached_back.update(entity_ids)
    if reaching_back:
        logger.info(
            f"BACKFILL: {len(reaching_back)} entity(ies) hold no history as far back as the "
            f"recorder may reach — asking from the floor"
        )
        since = floor
        for e in reaching_back:
            resume[e] = floor

    if since >= until:
        return {"added": {}, "since": until, "until": until}

    # How often each of these actually reports, measured against the recorder
    # itself. Everything below follows from this one number.
    rate = probe_density(entity_ids, until)
    batched = [e for e in entity_ids if is_batchable(rate.get(e, 0.0))]
    shared = set(batched)
    alone = [e for e in entity_ids if e not in shared]

    logger.info(
        f"BACKFILL: asking Home Assistant for {len(entity_ids)} entity(ies) "
        f"between {datetime.fromtimestamp(since, tz=timezone.utc):%Y-%m-%d %H:%M} and now"
        + (f" — {len(alone)} of them report too often to share a request" if alone else "")
    )

    added = {}
    oldest = {}
    if batched:
        _absorb_history(batched, since, until, batch_size=BATCH_SIZE,
                        chunk_seconds=CHUNK_SECONDS, added=added, oldest=oldest)

    # Each entity that needs its own requests gets an equal share of the run's
    # budget, spent on the newest end — the end questions are asked about, and
    # the only end where a crossing is still worth its exact second.
    budget = max(MIN_REQUESTS_PER_ENTITY, DENSE_REQUEST_BUDGET // len(alone)) if alone else 0
    coarse = {}
    for entity_id in alone:
        chunk = chunk_for(rate.get(entity_id, 0.0))
        # This entity's own resume point, not the shared one: sharing it would
        # have every reconnect re-fetch a window this entity already holds.
        start = max(resume[entity_id], until - budget * chunk)
        _absorb_history([entity_id], start, until, batch_size=1, chunk_seconds=chunk,
                        added=added, oldest=oldest)
        reached = oldest.get(entity_id, start)
        if reached - resume[entity_id] >= 3600:
            coarse[entity_id] = (resume[entity_id], reached)
            logger.info(
                f"BACKFILL: {entity_id} reports too often to read in full — read it back to "
                f"{datetime.fromtimestamp(reached, tz=timezone.utc):%Y-%m-%d %H:%M} and will "
                f"fill what is behind that from Home Assistant's aggregates"
            )

    _absorb_statistics(coarse, added)

    total = sum(added.values())
    if total:
        logger.info(f"BACKFILL: recovered {total} transition(s) across {len(added)} entity(ies)")
    else:
        logger.info("BACKFILL: nothing to recover — the recorder held no history for this window")
    return {"added": added, "since": since, "until": until}
