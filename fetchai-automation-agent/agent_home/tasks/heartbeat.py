"""Event-loop liveness: a 60 s interval stamps a heartbeat, a daemon thread
watches it, and GET /health answers only while the loop runs.

Why (docs/TODO.md, 2026-09-22): a synchronous ledger query inside uagents'
registration held the event loop for ~21 h and nothing said so — the
observation thread kept recording, the log simply went quiet. Any future stall,
of any cause, has to be visible in minutes. The watcher is a thread, not an
interval, because an interval is exactly what a stalled loop stops running.

/health is what config.yaml's `watchdog:` points the Supervisor at, by way of
proxy.py — the agent listens on 127.0.0.1 only (core/uagent.py), and the proxy
forwards /health with an 8 s timeout. It is answered by uagents' REST server, on
this same loop, so a stall times the request out and the proxy answers 503 (the
Supervisor waits 10 s, polls every 120 s and restarts on its second miss). It deliberately does not fail on a stale heartbeat while the loop
does answer: a loop that serves requests is not one a restart would fix.
"""
import logging
import threading
import time

from uagents import Context

from core.protocols import HealthResponse
from core.uagent import agent_1

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60

# Three missed beats. Long enough that one slow interval (a handler awaiting
# HA) never reads as a stall, short enough that a real one is in the log within
# minutes of starting.
STALL_THRESHOLD_SECONDS = 180

CHECK_EVERY_SECONDS = 15

_last_beat = time.monotonic()
_watch_thread = None


def beat(now=None):
    global _last_beat
    _last_beat = time.monotonic() if now is None else now


def heartbeat_age(now=None):
    return (time.monotonic() if now is None else now) - _last_beat


class StallWatch:
    """PURE: one log line when the heartbeat goes stale, one when it resumes.

    `check` returns (level, message) or None; the thread does the logging, so
    this is testable without threads or clocks.
    """

    def __init__(self, threshold=STALL_THRESHOLD_SECONDS):
        self.threshold = threshold
        self._stalled_beat = None  # the beat the stall was measured from

    def check(self, now, last_beat):
        if self._stalled_beat is None:
            if now - last_beat > self.threshold:
                self._stalled_beat = last_beat
                return (logging.WARNING,
                        f"LOOP: stalled for {int(now - last_beat)} s — no interval"
                        f" or REST handler has run since")
            return None
        if last_beat != self._stalled_beat:
            gap = int(last_beat - self._stalled_beat)
            self._stalled_beat = None
            return (logging.INFO, f"LOOP: recovered — the event loop was stalled for ~{gap} s")
        return None


def _watch(watch, sleep=time.sleep, clock=time.monotonic):
    while True:
        sleep(CHECK_EVERY_SECONDS)
        line = watch.check(clock(), _last_beat)
        if line:
            logger.log(*line)


@agent_1.on_event("startup")
async def start_stall_watch(ctx: Context):
    """Starts the watcher thread once; from startup, not import, so importing
    this module (tests) starts nothing."""
    global _watch_thread
    beat()
    if _watch_thread is None or not _watch_thread.is_alive():
        _watch_thread = threading.Thread(
            target=_watch, args=(StallWatch(),), name="loop-stall-watch", daemon=True)
        _watch_thread.start()


@agent_1.on_interval(period=HEARTBEAT_INTERVAL_SECONDS)
async def heartbeat(ctx: Context):
    beat()


@agent_1.on_rest_get("/health", HealthResponse)
async def handle_health(ctx: Context) -> HealthResponse:
    return HealthResponse(status="ok", heartbeat_age_s=round(heartbeat_age(), 1))
