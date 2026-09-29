"""Tests for the test harness itself — they keep conftest's stubs honest.

Every other file in this suite is only worth reading if these pass, because a
stub that silently masks the wrong thing turns the whole suite green for free.
Run this file first after touching conftest.py.
"""
import importlib
import os
import pathlib
import sys

import pytest

import conftest


REAL_DEPS = not conftest.STUBBED


def test_stub_set_is_all_or_nothing():
    """Either every third-party package is stubbed, or none of them is.

    A half-installed requirements.txt gives an environment that matches neither
    development nor the container, and failures in it mean nothing.
    """
    names = {name for name, _ in conftest.STUB_CANDIDATES}
    assert conftest.STUBBED in (set(), names), (
        f"partially stubbed environment: {sorted(conftest.STUBBED)} stubbed, "
        f"{sorted(names - conftest.STUBBED)} real. Install all of requirements.txt or none of it."
    )


def test_stubs_carry_their_marker_and_real_modules_do_not():
    for name, _ in conftest.STUB_CANDIDATES:
        module = sys.modules.get(name)
        if name in conftest.STUBBED:
            assert getattr(module, "__ceelabs_stub__", False) is True
        elif module is not None:
            assert not getattr(module, "__ceelabs_stub__", False)


def test_stubs_never_shadow_first_party_or_stdlib():
    """The worst possible typo: stubbing the app's own `tools` package would
    replace all thirteen tools with nothing, and every other test would still pass."""
    forbidden = {"core", "tools", "handlers", "rest", "tasks",
                 "yaml", "json", "re", "typing", "datetime", "os", "sys"}
    assert conftest.STUBBED & forbidden == set()


def test_yaml_is_real():
    """agent_runtime imports yaml for the approval diff; it must not be a stub."""
    import yaml

    assert not getattr(yaml, "__ceelabs_stub__", False)
    assert yaml.safe_load("a: 1") == {"a": 1}


def test_agent_modules_are_loaded_from_this_repo():
    """Catches a sys.path pointing at a stale copy of the add-on."""
    from core import agent_runtime
    from tools.tool_homeassistant import actions

    for module in (agent_runtime, actions):
        assert pathlib.Path(module.__file__).resolve().is_relative_to(conftest.AGENT_HOME)


@pytest.mark.parametrize("module_name", [
    "core.uagent", "rest.rest_chat", "rest.rest_pairing", "tasks.scheduled_tasks",
])
def test_the_agent_stub_makes_the_rest_wiring_importable(module_name):
    """The boundary this layer covers, stated positively.

    conftest's Agent stub records what gets registered instead of opening a
    real connection, so these four modules — which is where handle_post and
    handle_approve actually live — import cleanly and are directly callable.
    tests/test_rest_chat_wiring.py is what that unlocks.

    Deliberately no sys.modules.pop before importing: a CI run against real
    uagents found that forcing a FRESH reconstruction of Agent(...) relies on
    asyncio's deprecated auto-loop-creation, which stops working once anything
    in the process has called asyncio.run() (test_rest_chat_wiring.py does,
    repeatedly) — a real uagents fragility under repeated construction that is
    irrelevant to production, which constructs Agent exactly once, before
    touching asyncio at all. A plain import proves the same thing without that
    risk: if the stub (or, under real deps, the library itself) were broken,
    test_rest_chat_wiring.py's own top-level `from rest import rest_chat`
    would already fail loudly at collection, well before this test runs.
    """
    importlib.import_module(module_name)


def test_the_readme_path_resolves_regardless_of_cwd():
    """Pins a real bug a CI run against actual uagents caught, invisible under
    the stub (which never opens readme_path at all): Agent(readme_path=...) is
    resolved relative to the process's cwd by the real library. run.sh happens
    to cd into /app first, which is the only reason the plain "Readme.md"
    string ever worked; a test process run from anywhere else missed the file
    outright. core/uagent.py now derives an absolute path from __file__.

    Checked on the module-level helper, not on agent_1 itself: a second CI run
    showed the real uagents.Agent has no public readme_path attribute either
    (another stub-only introspection field, same lesson as _rest_routes below)
    — it presumably reads and discards the path during __init__. That's fine:
    a full regression back to the bare "Readme.md" string breaks EVERY test
    that imports core.uagent, loudly, at collection — exactly what the first
    CI run caught. This test only needs to prove the computation itself still
    resolves to a real, absolute path.
    """
    from core.uagent import _README_PATH

    assert os.path.isabs(_README_PATH)
    assert os.path.isfile(_README_PATH)


@pytest.mark.skipif(REAL_DEPS, reason="with real uagents_core installed these DO import")
@pytest.mark.parametrize("module_name", ["handlers.chat_handlers", "main"])
def test_still_out_of_scope(module_name):
    """Pins the REMAINING boundary.

    handlers/chat_handlers.py additionally needs
    uagents_core.contrib.protocols.chat, which conftest deliberately does not
    stub, so it — and main.py, which imports it — stay unimportable. If someone
    later stubs that module too, this test fails and forces the decision to
    extend scope to be deliberate rather than incidental.
    """
    sys.modules.pop(module_name, None)
    with pytest.raises(ImportError):
        importlib.import_module(module_name)


def test_network_stub_survives_the_blanket_except(monkeypatch):
    """The reason NetworkAccessInTests subclasses BaseException.

    _client._request wraps its call in `except Exception`. A stub raising a plain
    Exception would be swallowed into (False, "HA request failed: ...") and the
    calling test would pass while having tried to reach the real home.
    """
    if REAL_DEPS:
        pytest.skip("requests is real here; nothing is stubbed to raise")

    from core import settings
    from tools.tool_homeassistant._client import _ha_get

    # Without a token _request returns early and never reaches requests at all,
    # so this test would otherwise pass for entirely the wrong reason.
    monkeypatch.setattr(settings, "SUPERVISOR_TOKEN", "test-token")

    with pytest.raises(conftest.NetworkAccessInTests):
        _ha_get("/states")
