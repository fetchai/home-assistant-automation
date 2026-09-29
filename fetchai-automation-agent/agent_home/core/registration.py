"""The agent's Almanac registration policy: uagents' own two steps, with the
ledger step bounded so it can never stop the event loop.

Why this exists (docs/TODO.md, 2026-09-22): uagents re-registers every hour
from a task on the agent's event loop. `DefaultRegistrationPolicy` runs two
steps. The Almanac API step is aiohttp with a timeout — harmless. The ledger
step, `LedgerBasedRegistrationPolicy.register`, is an `async def` whose body
calls cosmpy's SYNCHRONOUS gRPC queries (`AlmanacContract.is_registered` ->
`query_contract` -> grpc `_blocking`, no deadline). One unanswered testnet
query at 13:42 on 2026-09-21 held the loop thread for ~21 h: no interval, no
REST handler, no chat ran until the add-on was restarted. uagents 0.25.5 is
unchanged. Even constructing that policy queries the ledger (the registration
fee), which is why it is built lazily, on the ledger thread, here.

So the ledger step runs on a dedicated daemon thread with its own event loop,
and the agent's loop only awaits a Future for at most
LEDGER_REGISTRATION_TIMEOUT_SECONDS. A thread blocked in C inside gRPC cannot
be killed, only abandoned — so while one is still alive no second one is
started, and a permanently hung node leaks at most one thread.

Everything else keeps DefaultRegistrationPolicy's semantics: the API step
first, its failure logged and swallowed; no contract -> the ledger step is
skipped with uagents' own message; InsufficientFundsError swallowed (uagents'
policy already asked the testnet faucet); any other ledger error logged and
re-raised, so `Agent._schedule_registration` retries in a minute as before.

uagents is imported lazily, inside the two factories: the test suite stubs
uagents without its `registration` / `network` submodules, and the policy
itself needs neither — it is handed whatever the factories build.
"""
import asyncio
import logging
import threading

try:
    from uagents.network import InsufficientFundsError
except ImportError:  # the test stubs have no uagents.network
    class InsufficientFundsError(Exception):
        """Stand-in with the real class's role; see the module docstring."""

logger = logging.getLogger(__name__)

# How long the agent's loop waits for the ledger thread. A healthy
# `is_registered` + expiry/endpoint/protocol check is a few seconds; an actual
# registration waits for the transaction to be included, which uagents polls
# for with asyncio.sleep — on the ledger thread's own loop, so that costs the
# agent nothing. Two minutes is generous for both and short against the hour.
LEDGER_REGISTRATION_TIMEOUT_SECONDS = 120


def api_policy_for(agent):
    """The Almanac API step exactly as DefaultRegistrationPolicy builds it."""
    from uagents.registration import AlmanacApiRegistrationPolicy
    return AlmanacApiRegistrationPolicy(almanac_api=agent._almanac_api_url)


def ledger_policy_for(agent):
    """The ledger step exactly as DefaultRegistrationPolicy builds it, or None
    when uagents found no usable Almanac contract (its `almanac_contract is
    None` branch). Runs on the ledger thread: the constructor queries the fee."""
    if agent._almanac_contract is None:
        return None
    from uagents.registration import LedgerBasedRegistrationPolicy
    return LedgerBasedRegistrationPolicy(
        agent._ledger, agent._wallet, agent._almanac_contract,
        agent._network == "testnet",
    )


class BoundedRegistrationPolicy:
    """An `AgentRegistrationPolicy` (uagents only ever calls `register`)."""

    def __init__(self, api_policy_factory, ledger_policy_factory):
        self._api_policy_factory = api_policy_factory
        self._ledger_policy_factory = ledger_policy_factory
        self._api_policy = None
        # Built on the ledger thread, then reused by the next one — never two
        # at once, because a new thread starts only after the last one died.
        self._ledger_policy = None
        self._ledger_thread = None

    async def register(self, agent_identifier, identity, protocols, endpoints,
                       metadata=None):
        args = (agent_identifier, identity, protocols, endpoints, metadata)
        try:
            if self._api_policy is None:
                self._api_policy = self._api_policy_factory()
            await self._api_policy.register(*args)
        except Exception as e:
            logger.warning(f"Failed to register on Almanac API: {e.__class__.__name__}")
            logger.debug(e)

        await self._register_on_ledger(args)

    async def _register_on_ledger(self, args):
        if self._ledger_thread is not None and self._ledger_thread.is_alive():
            logger.warning(
                "REGISTRATION: the previous ledger step is still waiting on the ledger"
                " — skipped, no second thread started"
            )
            return

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._ledger_thread = threading.Thread(
            target=self._run_ledger_step, args=(loop, future, args),
            name="almanac-ledger-registration", daemon=True,
        )
        self._ledger_thread.start()
        try:
            # shield: on timeout wait_for cancels what it awaits; the Future is
            # abandoned either way, and _deliver checks done() before setting.
            outcome, error = await asyncio.wait_for(
                asyncio.shield(future), LEDGER_REGISTRATION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                f"REGISTRATION: ledger step did not answer in "
                f"{LEDGER_REGISTRATION_TIMEOUT_SECONDS}s — skipped"
            )
            return

        if outcome == "no_ledger":
            logger.info("No Ledger available. Skipping registration on Almanac contract.")
        elif isinstance(error, InsufficientFundsError):
            pass
        elif error is not None:
            logger.error(f"Failed to register on Almanac contract: {error.__class__.__name__}")
            logger.debug(error)
            raise error

    def _run_ledger_step(self, loop, future, args):
        """The ledger thread's body. Everything that can touch the ledger —
        building the policy included — happens here, never on `loop`."""
        try:
            if self._ledger_policy is None:
                self._ledger_policy = self._ledger_policy_factory()
            if self._ledger_policy is None:
                result = ("no_ledger", None)
            else:
                asyncio.run(self._ledger_policy.register(*args))
                result = ("done", None)
        except Exception as e:
            result = ("error", e)
        except BaseException as e:
            # Re-raised on the agent's loop, a CancelledError would read as the
            # registration task being cancelled and end it for good.
            result = ("error", RuntimeError(f"ledger step aborted: {e!r}"))
        try:
            loop.call_soon_threadsafe(_deliver, future, result)
        except RuntimeError:
            pass  # the agent's loop is closed — shutdown; nobody is waiting


def _deliver(future, result):
    if not future.done():
        future.set_result(result)
