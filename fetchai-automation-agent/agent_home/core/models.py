"""The model layer (design §5): the one place a chat model is constructed.

Design §15.4 chooses a model by what the STEP is, not by guessing how hard a
question is — chat and the analyst's first pass stay on the cheap tier;
authoring a config or re-checking a destructive call from chat (Phase F)
wants the strong tier. This module is where that choice becomes a name and,
for the strong tier, a rationed one: `model_for` resolves a tier to a model
name, `take_strong_call` additionally spends one unit of today's cap and
refuses once it is gone.

Unset `ASI1_MODEL_STRONG` collapses the strong tier onto the cheap one —
today's behaviour, and the default until §6's "which ASI1 model" is answered.
When the two tiers name the same model there is nothing to ration, so
`take_strong_call` returns it for free rather than spending budget the cap
was never meant to track.

The budget is a **cost cap, not a preference**: it must fail closed. A
restart must not refill it, so the count is a file in `DATA_DIR`, not process
state, and a file that cannot be read is treated as today's cap already
spent rather than as an empty one — the opposite mistake would let a full
disk or a corrupt write buy unlimited strong-tier calls. The lock exists
because the analyst (Phase D) runs off the event loop while chat runs on it;
both may reach this file the same day.

`take_strong_call` deliberately does not fall back to the cheap model by
itself when the cap is spent. Design §15.4 requires the fallback to be
*stated*, not silent — the analyst's shortlist waits for tomorrow, an
approval is rendered "not reviewed by the strong model" (Phase F) — and only
the caller knows which of those applies to its own step.

`chat_model` builds the `ChatOpenAI` instance every caller in this add-on
should use; `agent_runtime._create_llm_with_tools` is that caller for chat.
`core/scheduler_llm.py` builds its own and is the one named, retiring
exception (§2 of docs/PROPOSAL-MODEL-ANALYST.md) — it is not touched here.
"""
from datetime import datetime
import json
import logging
import os
import threading

from core import settings

logger = logging.getLogger(__name__)

CHEAP = "cheap"
STRONG = "strong"

BUDGET_PATH = os.path.join(settings.DATA_DIR, "model_budget.json")

# Attempts after the first that one model call may make. What the pinned
# langchain-openai 1.1 does today (it leaves max_retries None and the openai
# client retries twice), written down: with a per-attempt timeout the worst case of a
# call is (1 + MODEL_MAX_RETRIES) × timeout, and that bound should not move
# because a library changed its default.
MODEL_MAX_RETRIES = 2

# Loaded once and written through on every change, guarded by _lock because
# the analyst thread and the event loop can both spend budget the same day.
_lock = threading.Lock()
_state = None


def _today(now=None) -> str:
    return (now or datetime.now().astimezone()).date().isoformat()


def _cap() -> int:
    return settings.ASI1_STRONG_DAILY_CAP


def _write_locked(state: dict) -> None:
    tmp = f"{BUDGET_PATH}.tmp"
    try:
        os.makedirs(os.path.dirname(BUDGET_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, BUDGET_PATH)
    except OSError as e:
        logger.error(f"MODEL BUDGET: failed to persist {BUDGET_PATH}: {e}")


def _load_locked() -> dict:
    """Returns the cached budget state, reading it from disk on first use.

    A missing file is a fresh install and starts at zero used. A file that
    exists but cannot be parsed fails closed: today reads as fully spent, and
    that verdict is written back so a crash loop cannot keep re-rolling the
    same free cap on every restart."""
    global _state
    if _state is not None:
        return _state
    try:
        with open(BUDGET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("date"), str) \
                or not isinstance(data.get("used"), int):
            raise ValueError("unexpected structure")
        _state = data
    except FileNotFoundError:
        _state = {"date": _today(), "used": 0}
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning(
            f"MODEL BUDGET: could not read {BUDGET_PATH} ({e}) — "
            f"failing closed, today's strong-tier cap treated as spent"
        )
        _state = {"date": _today(), "used": _cap()}
        _write_locked(_state)
    return _state


def model_for(tier: str) -> str:
    """The model name a tier resolves to today. STRONG falls back to the
    cheap tier's model when no strong model is configured."""
    if tier == STRONG:
        return settings.ASI1_MODEL_STRONG or settings.ASI1_MODEL
    return settings.ASI1_MODEL


def take_strong_call(now=None):
    """Spends one unit of today's strong-tier budget and returns the strong
    model's name, or None once today's cap is spent.

    Free when the strong tier is the same model as the cheap one — there is
    no extra cost to ration in that case, so nothing is consumed."""
    strong = model_for(STRONG)
    if strong == model_for(CHEAP):
        return strong

    today = _today(now)
    with _lock:
        global _state
        state = _load_locked()
        if state["date"] != today:
            state = {"date": today, "used": 0}
        if state["used"] >= _cap():
            _state = state
            _write_locked(state)
            return None
        state["used"] += 1
        _state = state
        _write_locked(state)
        return strong


def chat_model(model_name: str, temperature: float = 0.1, tier: str = None, timeout=None):
    """Builds the ASI1 `ChatOpenAI` instance. The only place this add-on
    talks to `langchain_openai`, other than the retiring `scheduler_llm.py`.

    `timeout` is seconds per HTTP attempt. The analyst's explorer run passes
    one, because it calls `invoke` from an executor thread and nothing else
    would ever end a call ASI1 never answers — 2.18.2's rule (nothing with
    network I/O unbounded) applied to the model. Chat passes none: its calls
    are `ainvoke` on the loop, already cut by `asyncio.wait_for` in
    rest/rest_chat.py, so its client keeps the library's default. Retries are
    pinned to MODEL_MAX_RETRIES either way — what the library does today,
    stated here so a dependency bump cannot multiply the worst case."""
    from langchain_openai import ChatOpenAI

    logger.info(f"MODEL: {model_name} ({tier or 'session'})")
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return ChatOpenAI(
        model=model_name,
        temperature=temperature,
        api_key=settings.ASI1_API_KEY,
        base_url="https://api.asi1.ai/v1",
        max_retries=MODEL_MAX_RETRIES,
        **kwargs,
    )
