"""The state of the agent's link to Home Assistant's event stream.

An empty Record tab has two very different causes: nothing has happened in the
house yet, or the agent has not been listening. Those look identical unless the
link reports on itself, and the second one is a bug the user should be able to
see rather than a quiet absence of data. So the listener keeps its state here
and the REST layer reads it.

This module also holds the entity-name cache. The record itself stores entity
ids and nothing else — names belong to Home Assistant's registry and change
under it — but the panel needs to show "Hall light" rather than
light.hall_ceiling on every poll. Registry fetches drop names in here on the
way past, so a Record tab refresh costs no Home Assistant call at all.

The thread that holds the websocket lives here too; tasks/observation.py is
only the uAgent registration that starts it. Keeping the loop in core means it
imports no uagents, so the parts of it that make decisions can be exercised by
the test suite even though the socket itself cannot.
"""
from datetime import datetime, timezone
import logging
import threading

from core import backfill
from core import observer
from core import record
from core import watchlist
from tools.tool_homeassistant._client import _ha_ws_close, _ha_ws_connect

logger = logging.getLogger(__name__)

# Nothing is watched on a fresh install, and an idle socket subscribed to
# nothing is still a socket. The loop waits instead, and picks up a selection
# saved in the panel within this many seconds.
IDLE_POLL_SECONDS = 5

# How long recv() waits before the loop checks that the connection is still
# alive. A quiet house is normal; a dead socket that looks quiet is not. This
# is applied AFTER connecting — the handshake keeps a short timeout, so an
# unreachable Home Assistant delays the retry by seconds rather than minutes.
RECV_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 10

# Reconnect backoff. Home Assistant restarting, the add-on outliving a Core
# update, a network blip — all of them end here, and all of them recover.
BACKOFF_START_SECONDS = 1
BACKOFF_MAX_SECONDS = 60

# Written by the observation thread, read by the REST handlers. Plain dict
# assignment under the GIL is atomic, and nothing here needs a consistent
# multi-field snapshot badly enough to be worth a lock.
_state = {
    "running": False,        # the thread is alive
    "connected": False,      # ...and currently holds an authenticated socket
    "connected_since": None,
    "live_since": None,      # when the subscription started carrying events
    "last_event_at": None,   # last state_changed seen, watched or not
    "recorded": 0,           # transitions stored this process
    "seen": 0,               # events considered this process
    "backfilled_to": None,   # how far back the recorder was able to warm us
    "last_error": "",
}

_names = {}


def _now():
    return int(datetime.now(tz=timezone.utc).timestamp())


def status() -> dict:
    """A snapshot for the panel."""
    return dict(_state)


def note_running(running: bool):
    _state["running"] = bool(running)


def note_connected():
    """The socket is up and subscribed."""
    _state["connected"] = True
    _state["connected_since"] = _now()
    _state["live_since"] = _state["live_since"] or _now()
    _state["last_error"] = ""


def note_disconnected(error: str = ""):
    """The socket is gone. `live_since` deliberately survives: it marks when
    this process began covering events, and a blip does not restart that."""
    _state["connected"] = False
    _state["connected_since"] = None
    if error:
        _state["last_error"] = str(error)[:300]


def note_event(recorded: bool):
    _state["seen"] += 1
    _state["last_event_at"] = _now()
    if recorded:
        _state["recorded"] += 1


def note_backfilled(since):
    """How far back the record was warmed from Home Assistant's recorder.

    Keeps the earliest, since a later backfill of a smaller gap does not undo
    the reach of an earlier one."""
    if since is None:
        return
    current = _state["backfilled_to"]
    _state["backfilled_to"] = since if current is None else min(current, int(since))


def remember_names(names: dict):
    """Caches entity_id -> friendly name from a registry fetch."""
    for entity_id, name in (names or {}).items():
        if entity_id and name:
            _names[entity_id] = name


def name_for(entity_id: str) -> str:
    """The cached friendly name, or the entity_id itself. Never a blank: the
    panel has to render something, and the id is the honest fallback."""
    return _names.get(entity_id) or entity_id


def coverage() -> list:
    """What the record holds per entity, as both the panel and the LLM need it.

    Watched entities with nothing recorded yet are included with a zero count
    rather than omitted: "selected, waiting" and "not selected" are different
    states, and both readers exist to tell them apart. An entity can also hold
    history and no longer be watched — the record is kept when a selection is
    withdrawn — so `watched` is carried per row and the watched ones sort first.

    Lives here rather than in the REST handler because the record tool needs the
    same merge, and a tool may not import a handler module.
    """
    rows = {r["entity_id"]: r for r in record.summary()}
    watched = set(watchlist.watched_entities())
    for entity_id in watched:
        rows.setdefault(entity_id, {
            "entity_id": entity_id, "events": 0, "first_seen": None,
            "last_seen": None, "last_state": "", "backfilled": 0,
        })

    out = [
        {**row, "name": name_for(entity_id), "watched": entity_id in watched}
        for entity_id, row in rows.items()
    ]
    out.sort(key=lambda r: (not r["watched"], -(r["last_seen"] or 0)))
    return out


# --------------------------------------------------------------------------
# The listener.
# --------------------------------------------------------------------------
_thread = None
_thread_lock = threading.Lock()
_stop = threading.Event()

# entity_id -> {state, ts} of what was last RECORDED, which is what the
# should_record rules are judged against. Seeded from the record itself, so a
# restart does not store a duplicate of the last thing it already knows.
_last_recorded = {}


def _seed(entity_id: str):
    """The last recorded state for an entity, read from the record once."""
    if entity_id not in _last_recorded:
        _last_recorded[entity_id] = record.last_state(entity_id)
    return _last_recorded[entity_id]


def handle_message(message) -> bool:
    """Decides what one websocket message means for the record, and stores it
    if it means anything. Returns True if a row was written.

    This is the whole of the loop's judgement, separated from the socket that
    delivers it: the connection cannot be tested here, but everything it leads
    to can.
    """
    entry = observer.parse_state_changed(message)
    if not entry:
        return False

    entity_id = entry["entity_id"]
    # The gate. An entity nobody selected is not watched, not recorded, and
    # not remembered — the event is dropped here and goes no further.
    if not watchlist.is_watched(entity_id):
        return False

    last = _seed(entity_id)
    ts = entry["ts"] or _now()
    recorded = False
    if observer.should_record(entry["state"], ts, last):
        row = observer.transition_row({**entry, "ts": ts}, last)
        if record.record_transition(entity_id, row[0], row[1], row[2], row[3], source="live"):
            _last_recorded[entity_id] = {"state": entry["state"], "ts": ts}
            recorded = True

    # Counted whether or not it was stored: "seen 4000, recorded 12" is the
    # line that shows the filtering is working rather than the link being dead.
    note_event(recorded)
    return recorded


def _backfill_gap(entity_ids, until):
    """Fills what was missed while the agent was not listening.

    Runs on its own thread: a cold start can be ten days across every watched
    entity, and the events arriving on the socket meanwhile must not queue up
    behind it."""
    def _run():
        try:
            result = backfill.backfill(entity_ids, until=until)
            note_backfilled(result.get("since"))
        except Exception as e:
            # The record is authoritative; the recorder is a bonus. A bonus
            # that fails to arrive must not disturb live capture.
            logger.warning(f"OBSERVATION: backfill after connecting failed: {e}")

    threading.Thread(target=_run, name="record-backfill", daemon=True).start()


def _subscribe(ws) -> bool:
    """Asks for every state_changed event and confirms Home Assistant agreed."""
    import json
    ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("type") == "result":
            return bool(msg.get("success"))


def _consume(ws):
    """Reads events until the socket fails or the loop is asked to stop."""
    import json
    import websocket as ws_lib

    while not _stop.is_set():
        try:
            raw = ws.recv()
        except ws_lib.WebSocketTimeoutException:
            # A quiet house, or a connection that has died without saying so.
            # The ping tells the two apart; if it fails we fall out and
            # reconnect.
            ws.ping()
            continue
        if not raw:
            raise ConnectionError("Home Assistant closed the websocket")
        try:
            handle_message(json.loads(raw))
        except Exception as e:
            # One malformed event must not end the subscription.
            logger.warning(f"OBSERVATION: could not handle an event: {e}")


def _loop():
    """Connect, subscribe, backfill the gap, consume — and recover from
    anything that interrupts that."""
    backoff = BACKOFF_START_SECONDS
    while not _stop.is_set():
        watched = watchlist.watched_entities()
        if not watched:
            # Nothing selected: no socket at all, which is what a fresh install
            # should cost. Saving a selection in the panel starts this within
            # IDLE_POLL_SECONDS without anything having to signal it.
            _stop.wait(IDLE_POLL_SECONDS)
            continue

        ok, ws = _ha_ws_connect(timeout=CONNECT_TIMEOUT_SECONDS)
        if not ok:
            note_disconnected(ws)
            logger.warning(f"OBSERVATION: cannot reach Home Assistant ({ws}) — retrying in {backoff}s")
            _stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
            continue

        try:
            if not _subscribe(ws):
                raise ConnectionError("Home Assistant refused the state_changed subscription")
            # The subscription is confirmed under the short timeout; only the
            # long idle wait that follows needs the generous one.
            ws.settimeout(RECV_TIMEOUT_SECONDS)
            note_connected()
            backoff = BACKOFF_START_SECONDS
            logger.info(f"OBSERVATION: watching {len(watched)} entity(ies) live")

            # Subscribe first, THEN backfill up to the moment the subscription
            # started. The other order loses everything that happens in
            # between; this order can only overlap, and the record's UNIQUE
            # index makes an overlap free.
            _backfill_gap(watched, _state["live_since"] or _now())

            _consume(ws)
        except Exception as e:
            note_disconnected(str(e))
            logger.warning(f"OBSERVATION: connection lost ({e}) — retrying in {backoff}s")
            _stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        finally:
            _ha_ws_close(ws)
            note_disconnected()

    note_running(False)


def start():
    """Starts the listener once, if it is not already running."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="record-observer", daemon=True)
        note_running(True)
        _thread.start()
        logger.info("OBSERVATION: listener started")
        return _thread


def stop():
    """Asks the listener to finish. The add-on normally just exits, so this is
    for tests and for an orderly shutdown if one is ever added."""
    _stop.set()


def reset():
    """Clears everything. Tests only — this state is per-process by nature."""
    _last_recorded.clear()
    _state.update({
        "running": False, "connected": False, "connected_since": None,
        "live_since": None, "last_event_at": None, "recorded": 0, "seen": 0,
        "backfilled_to": None, "last_error": "",
    })
    _names.clear()
