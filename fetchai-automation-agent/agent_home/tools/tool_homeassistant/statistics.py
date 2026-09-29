"""Reading Home Assistant's long-term statistics — the recorder's own aggregates.

Not a tool: nothing here is exposed to the model, the same way registry.py is
not. It exists for core.backfill, which has one problem raw history cannot
solve on its own.

Raw history is the record's preferred source and stays so. It carries the thing
the product is actually built on — the crossing, at the second it happened,
which core.record.scan_relations then pairs with whatever followed it inside
two minutes. An aggregate cannot do that: a five-minute bucket is coarser than
the whole relation window, so a stretch backfilled this way contributes nothing
to "what leads to what". A kettle drawing 3 kW for thirty seconds averages into
its bucket and is gone.

What aggregates are good for is the two places raw history cannot reach:

  * behind the request budget. core.backfill spends a bounded number of
    requests per entity on full-fidelity history, newest first, because an
    entity reporting every second costs a request per few hours. The window it
    could not afford is filled from buckets instead — coarse, but the coarse
    end is the old end.

  * behind the recorder's purge window. Short-term (five-minute) statistics are
    purged with the states, on whatever purge_keep_days the user set; the
    hourly ones are never purged at all. So a home on purge_keep_days: 3 can
    still be warmed with months of hourly shape, which is strictly more than
    the record could hold before.

Five minutes is the finest aggregate Home Assistant keeps — the recorder
compiles statistics_short_term on a five-minute boundary and statistics hourly,
and day/week/month are rollups of the latter computed on demand. There is
nothing between "five-minute mean" and "every reading".

Statistics are only reachable over the websocket API, like the registries.
"""
from datetime import datetime, timezone
import logging

from core import observer
from ._client import _ha_ws_call

logger = logging.getLogger(__name__)

# How many entities one statistics request asks about. Far smaller than the
# history batch, because a bucket answer is dense rather than sparse: ten days
# of five-minute buckets is 2,880 rows PER ENTITY whether the entity is busy or
# idle, where a history request for a quiet entity is nearly empty.
BATCH_SIZE = 10

# Statistics queries hit the same database history does, and are no faster for
# being smaller to transfer.
TIMEOUT_SECONDS = 60


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _has_mean(item) -> bool:
    """Whether a statistic id carries a mean.

    Home Assistant renamed this: `has_mean`, a bool, became `mean_type` — 0
    none, 1 arithmetic, 2 circular — and cores in the wild still send either.
    An id carrying neither field is read as having no mean, which is the
    conservative answer: the entity then keeps the raw-history path, which is
    the better source anyway.
    """
    if item.get("has_mean"):
        return True
    mean_type = item.get("mean_type")
    return isinstance(mean_type, int) and mean_type > 0


def mean_statistic_ids(entity_ids) -> set:
    """Which of these entities the recorder keeps a mean for.

    Only numeric entities whose integration declares a state_class have
    statistics at all, so this is a real filter and not a formality — a text
    sensor or a device_tracker that reports every second has no aggregate to
    fall back on, and core.backfill has to handle that case rather than assume
    this one.

    An unanswerable request comes back as the empty set, not an error: every
    entity then takes the history path, which is what happened before
    statistics existed here.
    """
    wanted = {e for e in (entity_ids or []) if e}
    if not wanted:
        return set()

    # Unfiltered on purpose. The command takes a statistic_type argument, but
    # it has moved between Home Assistant versions and an argument the schema
    # does not know fails the whole call; the answer is one small row per
    # statistic id in the house, so filtering here costs nothing.
    ok, data = _ha_ws_call({"type": "recorder/list_statistic_ids"}, timeout=TIMEOUT_SECONDS)
    if not ok or not isinstance(data, list):
        logger.warning(
            f"STATISTICS: could not list statistic ids ({data}) — every entity keeps the history path"
        )
        return set()

    return {
        item["statistic_id"] for item in data
        if isinstance(item, dict) and item.get("statistic_id") in wanted and _has_mean(item)
    }


def _bucket_ts(value):
    """A bucket's start as a unix second.

    Home Assistant answers with epoch MILLISECONDS on current cores and an ISO
    string on older ones. Milliseconds are far larger than any plausible unix
    second, which is how the two are told apart without a version check.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e11 else int(value)
    return observer.parse_timestamp(value)


def fetch_statistics(entity_ids, since: int, until: int, period: str = "5minute") -> dict:
    """Returns {entity_id: [{ts, mean}, ...]} for the window, oldest first.

    Only the mean is asked for. min and max are there for the asking, but the
    record stores one state per timestamp, and placing a bucket's extreme
    anywhere inside its bucket means inventing a timestamp — which corrupts
    both prev_duration and the hour-of-day histogram that profiles are built
    from. A mean at the bucket's start is the one honest reading of an
    aggregate.

    Best-effort in the same way history is: an unanswerable request leaves that
    stretch of the record empty rather than raising.
    """
    out = {}
    entity_ids = [e for e in (entity_ids or []) if e]
    if not entity_ids or int(until) <= int(since):
        return out

    for i in range(0, len(entity_ids), BATCH_SIZE):
        batch = entity_ids[i:i + BATCH_SIZE]
        ok, data = _ha_ws_call({
            "type": "recorder/statistics_during_period",
            "start_time": _iso(since),
            "end_time": _iso(until),
            "statistic_ids": batch,
            "period": period,
            "types": ["mean"],
        }, timeout=TIMEOUT_SECONDS)
        if not ok:
            logger.warning(
                f"STATISTICS: {period} request failed ({data}) — leaving that window empty"
            )
            continue
        if not isinstance(data, dict):
            logger.warning("STATISTICS: unexpected response shape — leaving that window empty")
            continue

        for entity_id, buckets in data.items():
            if not isinstance(buckets, list):
                continue
            rows = []
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                ts = _bucket_ts(bucket.get("start"))
                mean = bucket.get("mean")
                if ts is None or mean is None:
                    continue
                rows.append({"ts": ts, "mean": mean})
            if rows:
                rows.sort(key=lambda r: r["ts"])
                out.setdefault(entity_id, []).extend(rows)

    return out
