# Layer 1 — pure-logic tests

Run:

```bash
cd /addons/CEELABS-Fetchai-AutomationAgent/fetchai-automation-agent
/addons/CEELABS-Fetchai-AutomationAgent/venv/bin/python -m pytest
```

`pytest.ini` already sets `addopts = -q` — don't add your own `-q` on the command line.
pytest's quiet flag is cumulative, so `-q -q` drops the final `"N passed"` summary line
entirely (the tests still ran and still passed; you just can't see it).

One-time setup: `python3 -m venv /addons/CEELABS-Fetchai-AutomationAgent/venv` then
`venv/bin/pip install -r fetchai-automation-agent/requirements-dev.txt`.
(A bare `pip3 install` refuses — this host's Python is externally managed.)

These tests need no Home Assistant, no ASI1 key, no network and no container.
`conftest.py` stubs the heavy third-party imports; read its docstring first, and
run `tests/test_stubs_sanity.py` after touching it.

## What this layer covers

The **safety boundary** — `_is_destructive`, `is_sensitive_service`,
`parse_approval_decision`, `approval_expired` — and the **consistency traps**
CLAUDE.md warns about: tool names are duplicated across the `tools` registry,
`DESTRUCTIVE_TOOLS`, `SERVICE_CALL_TOOL`, `_format_approval_message` and the prose
of `settings.system_message`, with nothing linking them. `test_tool_registry.py`
is that link, and it is the highest-value file here.

The **disclosure boundary** — `core/disclosure.py`, what a tool answer may carry out of
the house. `test_disclosure.py` drives every tool in the registry against a Home Assistant
whose every answer is poisoned with coordinates, camera tokens, MAC addresses and URLs, and
asserts on `disclosure.scan()` rather than on each answer's shape, so it keeps holding when
a payload is rewritten. It also asserts that no module under `tools/` calls `json.dumps`,
which is what stops a new tool routing around the boundary.

Plus the pure parsers and transforms: `core/text.py`, `format_approval_for_chat`,
`_normalize_automation_cfg`, the offline half of `_preflight_automation`,
`resolve_area` / `entities_in_area` and the `unambiguous` contract.

## What it deliberately does NOT cover — don't over-read the green

1. **Pydantic semantics.** In production `BaseTool` is a pydantic `BaseModel`; here
   it is a plain class. A tool that raises at import under real pydantic passes
   here. The biggest blind spot, since the registry test's premise is that all 18
   tools instantiate.
2. **LangChain API drift.** The stub satisfies any import, so a dependency bump
   that moves `langchain.tools` stays green while the container fails to start.
3. **The LLM wire** — what descriptions and `args_schema` the model actually
   receives, the ASI1 system-message-first constraint, `_NO_TOOL_NUDGE`, `_run_agent`.
4. **Anything touching Home Assistant** — `_client.py`'s HTTP/websocket handling,
   real registry payloads, every tool's `_run`. This suite proves those are never
   *called*, not that they work.
5. **The approval gate's wiring.** The decision functions are covered; the code
   that calls them is not. `rest_chat.handle_post` parking `pending_approval`,
   `chat_handlers.send_message` routing YES/NO, and the pairing gate all live in
   modules that build the uAgent at import, so they are unreachable from here. A
   regression where `handle_post` simply stopped consulting `_is_destructive`
   would ship green. This is the obvious next layer.
6. **Python version skew.** These run on the dev box's Python; production is
   `python:3.12-slim`.

Risks 1, 2 and 6 disappear wherever the real dependencies are installed: conftest
stubs nothing there and these same files run unchanged. That is the intended CI
job — `pip install -r requirements.txt -r requirements-dev.txt && pytest` on
`python:3.12-slim`.
