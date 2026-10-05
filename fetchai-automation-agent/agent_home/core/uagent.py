"""uAgent instance creation and startup wiring: registers the agent with
Agentverse's mailbox service and seeds the LLM system message with the
agent's own identity (name, version).

Almanac registration — is the on-chain (ledger) record needed? Decided
2026-09-22, after its synchronous gRPC query froze the loop for ~21 h
(docs/TODO.md; the fix is core/registration.py). Kept, because it could not
be shown to be unneeded:
  - Every uagents resolver asks the Almanac API first; `AlmanacApiResolver.
    resolve` falls back to the contract only when the API has no record for
    the address. With `mailbox=True` our only endpoint is Agentverse's
    mailbox, and the API step (bounded aiohttp) registers exactly that
    endpoint every hour — so in the normal path the ledger record is a
    redundant copy.
  - But it is live: the log shows "Almanac contract registration is up to
    date!", i.e. the record exists on testnet. It is the fallback for any
    sender whose API lookup fails (API outage, our API record lapsing), and
    how ASI1 / Agentverse route to a mailbox agent internally is not
    something this code can see. Dropping it could make the agent silently
    unreachable in exactly the case the fallback exists for.
So the ledger step stays, bounded on its own thread; disabling it remains
the one-line alternative (`ledger_policy_factory=lambda: None`) if Fetch.ai
ever documents that mailbox agents do not need it.

The REST server listens on 127.0.0.1, not uagents' 0.0.0.0 (2.24.2). Every
`on_rest_*` endpoint here is unauthenticated — uagents restricts only its own
reserved endpoints — and on 0.0.0.0 any other add-on, the host and SSH reached
/rest/pairing/code, /rest/approve and /rest/post on 172.30.33.1:8099 with no
login (checked 2026-09-29, Fetch.ai's question). Nothing outside this container
needs the port: the mailbox is an outbound connection, proxy.py (same
container) forwards to 127.0.0.1:8099, and the Supervisor's watchdog now asks
proxy.py for /health. uagents 0.25.1 has no option for the address —
`asgi.HOST` is a module constant `ASGIServer.serve()` reads when it starts —
so it is rebound before the Agent exists; tests/test_uagent_bind.py fails if a
uagents release stops reading it there. A per-endpoint `local_only` in uagents
(proposed to Fetch.ai) would replace this."""
import os

import uagents.asgi
from uagents import Agent, Context
from uagents_core.utils.registration import (
    register_chat_agent,
    RegistrationRequestCredentials,
)
from core import settings
from core import pairing
from core import registration

# uagents.Agent opens readme_path relative to the process's CURRENT WORKING
# DIRECTORY, not this module's location. In the container that happens to
# work because run.sh cds into /app (agent_home/'s contents) before starting
# the process — but anything invoked from elsewhere (a test importing this
# module, a future entry point) would silently miss the file. Resolved from
# __file__ instead, so it holds regardless of cwd.
_README_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Readme.md")
)

#: Where the REST server listens. Loopback only — see the module docstring.
AGENT_HOST = "127.0.0.1"
uagents.asgi.HOST = AGENT_HOST

# AGENT init
agent_1 = Agent(
    name=settings.CLIENTS[settings.IDX]["NAME"],
    seed=settings.CLIENTS[settings.IDX]["SEED"],
    port=settings.CLIENTS[settings.IDX]["PORT"],
    mailbox=True,
    publish_agent_details=True,
    readme_path=_README_PATH,
    network="testnet",
    # Not uagents' DefaultRegistrationPolicy: its hourly ledger step calls
    # cosmpy's synchronous gRPC query with no deadline on the event loop, and
    # one unanswered query on 2026-09-21 froze the whole agent for ~21 h
    # (docs/TODO.md). Same two steps, the ledger one on its own thread under a
    # timeout. Passed here, not via update_registration_policy(), because the
    # default policy's constructor already queries the ledger. The lambdas
    # read agent_1 only when registration first runs, after it exists.
    registration_policy=registration.BoundedRegistrationPolicy(
        api_policy_factory=lambda: registration.api_policy_for(agent_1),
        ledger_policy_factory=lambda: registration.ledger_policy_for(agent_1),
    ),
)


# STARTUP handler for uAgent
@agent_1.on_event("startup")
async def process_startup(ctx: Context):
    """Logs the agent's startup info, appends its identity to the LLM
    system message, and registers it with Agentverse's mailbox if configured."""
    settings.agentCtx = ctx
    ctx.logger.info("Agent "+agent_1.name+" ready on port "+agent_1._port.__str__())

    # The address is what a user has to address in ASI1, and the panel shows it
    # for that reason — but it is also the first thing to check when someone
    # reports the agent ignoring them, so put it in the log too, next to the
    # count of identities currently allowed to talk to this home.
    paired_count = len(pairing.list_paired())
    ctx.logger.info(f"Agent address: {agent_1.address}")
    ctx.logger.info(
        f"PAIRING: {paired_count} identity(ies) paired with this home"
        + ("" if paired_count else " — pair one from the add-on panel to use the agent from ASI1")
    )
    settings.system_message = settings.system_message + ". <agent_details> Your name is: "+ agent_1.name \
    + ". Your version is: "+settings.VERSION+" .</agent_details>"

    if settings.AGENTVERSE_API_KEY and settings.CLIENTS[settings.IDX]["SEED"]:
        try:
            ctx.logger.info("Registering agent with Agentverse mailbox...")
            endpoint = f"http://localhost:{settings.CLIENTS[settings.IDX]['PORT']}"
            register_chat_agent(
                settings.CLIENTS[settings.IDX]["NAME"],
                endpoint,
                active=True,
                credentials=RegistrationRequestCredentials(
                    agentverse_api_key=settings.AGENTVERSE_API_KEY,
                    agent_seed_phrase=settings.CLIENTS[settings.IDX]["SEED"],
                ),
                description="Fetchai automation agent for Home Assistant — creates and manages HA automations from natural language.",
            )
            ctx.logger.info("Agentverse mailbox registration successful")
        except Exception as e:
            ctx.logger.warning(f"Agentverse registration failed: {e}")
    else:
        ctx.logger.warning("Skipping Agentverse registration: agentverse_api_key not configured")
