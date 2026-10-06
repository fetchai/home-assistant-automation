"""Test bootstrap: makes agent_home importable without its heavy dependencies.

The add-on's Python only runs meaningfully inside a Home Assistant add-on
container — every module reaches `uagents`, and the runtime reaches langchain on
top of that. Development happens on a box that has neither. So before any
agent_home module is imported, this file installs small stand-ins for the
third-party packages into sys.modules.

That is honest here only because this suite tests PURE logic and WIRING — the
approval-gate predicates, the tool registry, and the REST handlers that call
them — never real langchain or uagents behaviour. The uagents.Agent stub
records what gets registered instead of opening a network connection, which is
enough for handle_post/handle_approve to be imported and called directly.
Anything that would actually reach the network (the LLM client, HTTP,
websockets, Agentverse registration) is stubbed to RAISE, so a test that
strays past pure logic and wiring fails loudly instead of quietly passing.

Stubs are installed only where the real package is genuinely absent. On a machine
or CI image with requirements.txt installed, nothing is stubbed and these same
tests run against the real libraries — which is what keeps the stubs from
silently drifting into fiction. tests/test_stubs_sanity.py enforces the rest.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import types

import pytest


# --------------------------------------------------------------------------
# 1. Environment isolation — before anything can read it.
#
# core/pairing.py computes STORE_PATH from settings.DATA_DIR at IMPORT time, and
# the default is /data: the live, mounted add-on volume. Without this override a
# test that redeems a pairing code would write into the real home's bindings.
# --------------------------------------------------------------------------
os.environ["DATA_DIR"] = os.path.join(tempfile.gettempdir(), "ceelabs-agent-tests")

# Blanked so a developer with these exported in their shell cannot get a real
# request past the token check in tools/tool_homeassistant/_client.py.
for _leaky in ("SUPERVISOR_TOKEN", "ASI1_API_KEY", "AGENTVERSE_API_KEY"):
    os.environ[_leaky] = ""


# --------------------------------------------------------------------------
# 2. Stub installation.
# --------------------------------------------------------------------------
class NetworkAccessInTests(BaseException):
    """Raised when a test reaches the network.

    Subclasses BaseException, NOT Exception, on purpose: _client._request wraps
    every call in `except Exception` and would otherwise turn an accidental HTTP
    call into a benign (False, "HA request failed: ...") tuple — and the test
    would pass. BaseException sails through that handler while pytest still
    reports it as an ordinary failure.
    """


#: Top-level package names this run replaced with a stub. Empty when the real
#: dependencies are installed. tests/test_stubs_sanity.py asserts on it.
STUBBED = set()


def _absent(name: str) -> bool:
    """True if `name` cannot be imported for real in this interpreter.

    Only ever called with top-level names: find_spec("langchain.tools") would
    import langchain as a side effect, which is the thing we are avoiding.
    """
    if name in sys.modules:
        return False
    try:
        return importlib.util.find_spec(name) is None
    except Exception:
        return True


def _stub(name: str):
    """Registers an empty stub module under `name` and returns it."""
    module = types.ModuleType(name)
    module.__ceelabs_stub__ = True
    sys.modules[name] = module
    STUBBED.add(name.split(".")[0])
    return module


def _install_uagents():
    uagents = _stub("uagents")

    class Context:
        """Only ever used as a type annotation, or passed as literal None —
        neither handle_post nor handle_approve in rest_chat.py touch it."""

    class Model:
        """Stand-in for the pydantic model uagents exposes; core/protocols.py
        subclasses it with plain annotated attributes."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Agent:
        """Stand-in for uagents.Agent.

        core/uagent.py builds a real Agent at module scope, which is what made
        rest_chat.py, rest_pairing.py, tasks/scheduled_tasks.py and
        core/uagent.py itself unimportable in a plain test. This stub records
        what gets registered instead of opening a real network connection, so
        those modules import cleanly and their handlers (handle_post,
        handle_approve, ...) become directly callable functions — which is
        what actually lets the approval gate's WIRING be tested, not only the
        pure predicates it is supposed to call.

        handlers/chat_handlers.py additionally needs
        uagents_core.contrib.protocols.chat, which stays unstubbed on purpose
        — that module is deliberately still out of scope. main.py stays out of
        scope too: it calls agent_1.run(), which this stub makes raise.
        """

        def __init__(self, *, name="", seed="", port=0, **kwargs):
            self.name = name
            self._seed = seed
            self._port = port
            # A stable, obviously-fake identity — no real cryptography, and
            # deterministic so a test can assert on it without hardcoding a value.
            self.address = "agent1qteststub" + format(abs(hash(seed)) % 10**8, "08d")
            self._rest_routes = {}
            self._event_handlers = {}
            self._interval_handlers = []
            # The real Agent's own attribute name, so the wiring test that
            # reads it holds against both (core/registration.py).
            self._registration_policy = kwargs.get("registration_policy")
            # Public on the real Agent too, keyed by protocol digest: what
            # core.uagent registers with Agentverse at startup.
            self.protocols = {}
            # readme_path and other real-Agent-only kwargs land in **kwargs and
            # are dropped — a CI run against real uagents found the real Agent
            # exposes no public attribute for it either, so a stub attribute
            # here would only test the stub's own bookkeeping. See
            # test_the_readme_path_resolves_regardless_of_cwd, which checks
            # core.uagent's own module-level path instead.

        def on_rest_post(self, path, request_model, response_model):
            def decorator(fn):
                self._rest_routes[path] = fn
                return fn
            return decorator

        def on_rest_get(self, path, response_model):
            # tasks/heartbeat.py's /health — the only GET route. Same dict as
            # the POSTs: paths never collide, and the tests look up by path.
            def decorator(fn):
                self._rest_routes[path] = fn
                return fn
            return decorator

        def on_event(self, name):
            def decorator(fn):
                self._event_handlers.setdefault(name, []).append(fn)
                return fn
            return decorator

        def on_interval(self, period):
            def decorator(fn):
                self._interval_handlers.append(fn)
                return fn
            return decorator

        def include(self, protocol, publish_manifest=False):
            pass

        def run(self):
            raise NetworkAccessInTests(
                "agent_1.run() starts a real server — must not run in tests"
            )

    class Protocol:
        """Stand-in for uagents.Protocol; only handlers.chat_handlers touches
        this, and that module is out of scope — kept here anyway so a future
        test extending scope to it has somewhere to start from."""

        def __init__(self, *, spec=None, name=None, version=None):
            self.spec = spec
            self.name = name
            self.version = version
            self._message_handlers = {}

        def on_message(self, model, replies=None, **kwargs):
            def decorator(fn):
                self._message_handlers[model] = fn
                return fn
            return decorator

    uagents.Context = Context
    uagents.Model = Model
    uagents.Agent = Agent
    uagents.Protocol = Protocol

    # core/uagent.py rebinds uagents.asgi.HOST before the Agent is built (the
    # REST server's listen address). The real module-level default, so a test
    # sees the override replace exactly what uagents 0.25.1 ships.
    asgi = _stub("uagents.asgi")
    asgi.HOST = "0.0.0.0"
    uagents.asgi = asgi


def _install_uagents_core():
    """core/uagent.py imports uagents_core.utils.registration and
    uagents_core.config unconditionally at module scope, even though
    register_agent is only ever called from inside process_startup — an event
    handler this suite invokes only with the function replaced
    (tests/test_agentverse_profile.py). Stubbed just enough to satisfy the
    import; register_agent itself still raises if anything ever does call it,
    same defence-in-depth as the requests stub.

    uagents_core.contrib.protocols.chat (handlers/chat_handlers.py's dependency)
    is deliberately NOT stubbed here — that module stays out of scope.
    """
    root = _stub("uagents_core")
    utils = _stub("uagents_core.utils")
    registration = _stub("uagents_core.utils.registration")

    config = _stub("uagents_core.config")

    class RegistrationRequestCredentials:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentverseRegistrationRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AgentverseConfig:
        pass

    def register_agent(*args, **kwargs):
        raise NetworkAccessInTests(
            "register_agent talks to Agentverse — must not run in tests"
        )

    registration.RegistrationRequestCredentials = RegistrationRequestCredentials
    registration.AgentverseRegistrationRequest = AgentverseRegistrationRequest
    registration.register_agent = register_agent
    config.AgentverseConfig = AgentverseConfig
    utils.registration = registration
    root.utils = utils
    root.config = config


def _install_langchain():
    langchain = _stub("langchain")
    tools = _stub("langchain.tools")

    class BaseTool:
        """Plain stand-in for langchain's pydantic-backed BaseTool.

        Every tool in this codebase declares its identity as ordinary annotated
        class attributes with literal defaults (`name: str = "..."`), so under a
        plain base class they still expose .name/.description and the registry in
        core/agent_runtime.py instantiates cleanly.
        """

        name = ""
        description = ""
        args_schema = None

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    tools.BaseTool = BaseTool
    langchain.tools = tools


def _install_langchain_core():
    _stub("langchain_core")
    messages = _stub("langchain_core.messages")

    class _Message:
        def __init__(self, content="", **kwargs):
            self.content = content
            self.__dict__.update(kwargs)

        def __repr__(self):
            return f"{type(self).__name__}({self.content!r})"

    for cls_name in ("HumanMessage", "SystemMessage", "AIMessage", "ToolMessage"):
        setattr(messages, cls_name, type(cls_name, (_Message,), {}))
    sys.modules["langchain_core"].messages = messages


def _install_langchain_openai():
    module = _stub("langchain_openai")

    class ChatOpenAI:
        def __init__(self, *args, **kwargs):
            raise NetworkAccessInTests(
                "ChatOpenAI is stubbed out — this layer must not reach the LLM"
            )

    module.ChatOpenAI = ChatOpenAI


def _install_pydantic():
    module = _stub("pydantic")

    class BaseModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def Field(*args, **kwargs):
        # Return the declared default so a field reads as "" rather than as an
        # opaque sentinel, matching real pydantic closely enough for this layer.
        return kwargs.get("default")

    module.BaseModel = BaseModel
    module.Field = Field


def _install_requests():
    module = _stub("requests")

    def _refuse(*args, **kwargs):
        raise NetworkAccessInTests(
            "a test reached the network — pure-logic tests must not call Home Assistant"
        )

    module.get = module.post = module.delete = module.request = _refuse


def _install_websocket():
    # Imported lazily inside _client._ha_ws_calls, so not needed to satisfy an
    # import — stubbed anyway because that function also swallows Exception.
    module = _stub("websocket")

    def _refuse(*args, **kwargs):
        raise NetworkAccessInTests("a test reached the Home Assistant websocket API")

    module.create_connection = _refuse


#: Every third-party package agent_home reaches at import time, with its stub
#: installer. Exposed so the sanity tests can assert the set was replaced
#: all-or-nothing — a half-stubbed environment matches neither dev nor production.
STUB_CANDIDATES = (
    ("uagents", _install_uagents),
    ("uagents_core", _install_uagents_core),
    ("langchain", _install_langchain),
    ("langchain_core", _install_langchain_core),
    ("langchain_openai", _install_langchain_openai),
    ("pydantic", _install_pydantic),
    ("requests", _install_requests),
    ("websocket", _install_websocket),
)

for _name, _install in STUB_CANDIDATES:
    if _absent(_name):
        _install()


# --------------------------------------------------------------------------
# 3. Import root. Derived from __file__ so the suite runs from any cwd.
# --------------------------------------------------------------------------
AGENT_HOME = pathlib.Path(__file__).resolve().parents[1] / "agent_home"
if str(AGENT_HOME) not in sys.path:
    sys.path.insert(0, str(AGENT_HOME))


# --------------------------------------------------------------------------
# 4. Fixtures.
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_session_memory():
    """Empties the in-process session store around every test.

    session_memory_store is a module global, so without this the suite becomes
    order-dependent and can pass or fail depending on -k selection.
    """
    from core import agent_runtime

    agent_runtime.session_memory_store.clear()
    yield
    agent_runtime.session_memory_store.clear()


@pytest.fixture
def area_index():
    """A canned registry index of the shape build_area_index() returns.

    Deliberately includes the awkward rows: an area with an empty name, and an
    entity whose area_id is not in "areas" at all.
    """
    return {
        "areas": {
            "kitchen": {"area_id": "kitchen", "name": "Kitchen",
                        "aliases": ["cooking room"], "floor_id": "ground"},
            "living_room": {"area_id": "living_room", "name": "Living room",
                            "aliases": [], "floor_id": "ground"},
            "bedroom": {"area_id": "bedroom", "name": "Bedroom",
                        "aliases": [], "floor_id": "upstairs"},
            "nameless": {"area_id": "nameless", "name": "",
                         "aliases": [], "floor_id": None},
        },
        "entity_area": {
            "light.kitchen_ceiling": "kitchen",
            "sensor.kitchen_temperature": "kitchen",
            "light.living_room_lamp": "living_room",
            "light.living_room_strip": "living_room",
            "switch.bedroom_fan": "bedroom",
            "light.nameless_lamp": "nameless",
            "light.orphan": "area_that_does_not_exist",
        },
    }


@pytest.fixture
def ha_states():
    """A canned {entity_id: state} map of the shape _states_by_entity() returns.

    light.living_room_strip has no attributes key and light.orphan is absent
    entirely, so the `or {}` guards in entities_in_area get exercised.
    """
    return {
        "light.kitchen_ceiling": {
            "entity_id": "light.kitchen_ceiling", "state": "on",
            "attributes": {"friendly_name": "Kitchen ceiling"},
        },
        "sensor.kitchen_temperature": {
            "entity_id": "sensor.kitchen_temperature", "state": "21.5",
            "attributes": {"friendly_name": "Kitchen temperature"},
        },
        "light.living_room_lamp": {
            "entity_id": "light.living_room_lamp", "state": "off",
            "attributes": {"friendly_name": "Living room lamp"},
        },
        "light.living_room_strip": {
            "entity_id": "light.living_room_strip", "state": "off",
        },
        "switch.bedroom_fan": {
            "entity_id": "switch.bedroom_fan", "state": "off",
            "attributes": {"friendly_name": "Bedroom fan"},
        },
        "light.nameless_lamp": {
            "entity_id": "light.nameless_lamp", "state": "on",
            "attributes": {"friendly_name": "Nameless lamp"},
        },
    }
