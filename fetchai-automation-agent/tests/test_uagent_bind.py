"""core/uagent.py — the agent's REST server listens on 127.0.0.1 (2.24.2).

uagents 0.25.1 has no option for the address; the module constant
`uagents.asgi.HOST` is rebound before the Agent exists. The real-deps test is
the one that matters on an upgrade: if a uagents release stops reading `HOST`
in `ASGIServer.serve`, the rebind silently does nothing and every endpoint is
back on 0.0.0.0."""
import inspect

import pytest

import conftest
import uagents.asgi
from core import uagent

REAL_DEPS = not conftest.STUBBED


def test_the_rest_server_is_bound_to_loopback():
    assert uagent.AGENT_HOST == "127.0.0.1"
    assert uagents.asgi.HOST == "127.0.0.1"


@pytest.mark.skipif(not REAL_DEPS, reason="needs the real uagents package")
def test_uagents_still_takes_the_address_from_that_constant():
    source = inspect.getsource(uagents.asgi.ASGIServer.serve)

    assert "host=HOST" in source
