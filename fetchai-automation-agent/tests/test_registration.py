"""core/registration.py — the Almanac registration policy whose ledger step can
no longer stop the event loop (docs/TODO.md, 2026-09-22: one unanswered gRPC
query froze the agent for ~21 h).

The fakes stand in for uagents' two policies. The one that matters blocks in a
plain synchronous wait — the same shape as cosmpy's `_blocking` gRPC call — so
if the policy ever ran it on the calling loop, the "loop stays responsive"
assertion would hang instead of passing.
"""
import asyncio
import logging
import threading
import time

import pytest

from core import registration
from core.registration import BoundedRegistrationPolicy, InsufficientFundsError

ARGS = ("test-agent://agent1qx", object(), ["proto"], ["endpoint"], None)


class _Api:
    def __init__(self, calls, error=None):
        self.calls, self.error = calls, error

    async def register(self, *args):
        self.calls.append(("api", threading.current_thread()))
        if self.error:
            raise self.error


class _Ledger:
    """`block` holds register() in a synchronous wait until the test releases it."""

    def __init__(self, calls, block=None, error=None):
        self.calls, self.block, self.error = calls, block, error

    async def register(self, *args):
        self.calls.append(("ledger", threading.current_thread(), args))
        if self.block is not None:
            self.block.wait()
        if self.error:
            raise self.error


def _policy(calls, ledger=None, api_error=None, factory_calls=None):
    def ledger_factory():
        if factory_calls is not None:
            factory_calls.append(threading.current_thread())
        return ledger
    return BoundedRegistrationPolicy(
        api_policy_factory=lambda: _Api(calls, api_error),
        ledger_policy_factory=ledger_factory,
    )


@pytest.fixture
def release():
    """Unblocks every hung fake ledger at teardown, so no test leaves a thread."""
    event = threading.Event()
    yield event
    event.set()


class TestAHungLedgerStep:
    def test_returns_within_the_timeout_and_the_loop_keeps_running(
            self, monkeypatch, caplog, release):
        monkeypatch.setattr(registration, "LEDGER_REGISTRATION_TIMEOUT_SECONDS", 0.3)
        calls = []
        policy = _policy(calls, _Ledger(calls, block=release))
        ticks = []

        async def other_work():
            for i in range(5):
                await asyncio.sleep(0.02)
                ticks.append(i)
            return "done"

        async def main():
            started = time.monotonic()
            _, other = await asyncio.gather(policy.register(*ARGS), other_work())
            return time.monotonic() - started, other

        with caplog.at_level(logging.WARNING, logger="core.registration"):
            elapsed, other = asyncio.run(main())

        assert elapsed < 2
        assert other == "done" and ticks == [0, 1, 2, 3, 4]
        assert [c[0] for c in calls] == ["api", "ledger"]
        assert calls[1][1] is not threading.current_thread()
        assert "REGISTRATION: ledger step did not answer in 0.3s — skipped" in caplog.text

    def test_a_second_call_while_the_first_thread_is_stuck_starts_no_thread(
            self, monkeypatch, caplog, release):
        monkeypatch.setattr(registration, "LEDGER_REGISTRATION_TIMEOUT_SECONDS", 0.1)
        calls, factory_calls = [], []
        policy = _policy(calls, _Ledger(calls, block=release), factory_calls=factory_calls)

        asyncio.run(policy.register(*ARGS))
        first = policy._ledger_thread
        assert first.is_alive()

        with caplog.at_level(logging.WARNING, logger="core.registration"):
            asyncio.run(policy.register(*ARGS))

        assert policy._ledger_thread is first
        assert [c[0] for c in calls] == ["api", "ledger", "api"]  # API step still ran
        assert len(factory_calls) == 1
        assert "previous ledger step is still waiting" in caplog.text

        release.set()
        first.join(timeout=2)
        assert not first.is_alive()

    def test_once_the_stuck_thread_ends_the_next_call_runs_the_ledger_again(
            self, monkeypatch, release):
        monkeypatch.setattr(registration, "LEDGER_REGISTRATION_TIMEOUT_SECONDS", 0.1)
        calls = []
        ledger = _Ledger(calls, block=release)
        policy = _policy(calls, ledger)
        asyncio.run(policy.register(*ARGS))
        release.set()
        policy._ledger_thread.join(timeout=2)

        asyncio.run(policy.register(*ARGS))

        assert [c[0] for c in calls] == ["api", "ledger", "api", "ledger"]


class TestALedgerStepThatAnswers:
    def test_is_awaited_off_the_loop_thread_with_the_same_arguments(self):
        calls, factory_calls = [], []
        policy = _policy(calls, _Ledger(calls), factory_calls=factory_calls)

        asyncio.run(policy.register(*ARGS))

        kind, thread, args = calls[1]
        assert kind == "ledger" and args == ARGS
        assert thread is not threading.current_thread()
        # Building uagents' ledger policy queries the ledger too — so it is
        # built on that thread as well, and only once.
        assert factory_calls == [thread]
        asyncio.run(policy.register(*ARGS))
        assert len(factory_calls) == 1

    def test_insufficient_funds_is_swallowed_as_uagents_does(self):
        calls = []
        policy = _policy(calls, _Ledger(calls, error=InsufficientFundsError()))

        asyncio.run(policy.register(*ARGS))  # no raise

    def test_any_other_error_is_logged_and_re_raised_for_the_retry(self, caplog):
        calls = []
        policy = _policy(calls, _Ledger(calls, error=ValueError("bad reply")))

        with caplog.at_level(logging.ERROR, logger="core.registration"):
            with pytest.raises(ValueError, match="bad reply"):
                asyncio.run(policy.register(*ARGS))
        assert "Failed to register on Almanac contract: ValueError" in caplog.text

    def test_no_contract_skips_the_ledger_with_uagents_message(self, caplog):
        calls = []
        policy = _policy(calls, ledger=None)

        with caplog.at_level(logging.INFO, logger="core.registration"):
            asyncio.run(policy.register(*ARGS))
        assert [c[0] for c in calls] == ["api"]
        assert "No Ledger available" in caplog.text

    def test_an_api_failure_is_logged_and_the_ledger_step_still_runs(self, caplog):
        calls = []
        policy = _policy(calls, _Ledger(calls), api_error=OSError("down"))

        with caplog.at_level(logging.WARNING, logger="core.registration"):
            asyncio.run(policy.register(*ARGS))
        assert [c[0] for c in calls] == ["api", "ledger"]
        assert "Failed to register on Almanac API: OSError" in caplog.text


def test_the_agent_is_built_with_this_policy():
    from core.uagent import agent_1

    assert isinstance(agent_1._registration_policy, BoundedRegistrationPolicy)
