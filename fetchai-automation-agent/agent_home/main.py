"""Entry point for the Home Assistant automation uAgent.

This module wires the agent together and starts it. The actual behavior lives
in focused modules that register their handlers on `agent_1` / `chat_proto` at
import time:
  - core.agent_runtime    : tools, session memory, the LLM agent loop, approval helpers
  - handlers.chat_handlers: chat protocol (message/ack) handlers
  - handlers.peer_handlers: the agent-to-agent protocol — other uAgents, approved in the panel
  - rest.rest_chat        : /rest/post, /rest/approve
  - rest.rest_pairing     : /rest/pairing/* — which identities may talk to this home
  - rest.rest_observation : /rest/observation/* — what the agent watches and what it recorded
  - rest.rest_activity    : /rest/activity/* — what it did, what it still has to say, and undoing it
  - rest.rest_suggestions : /rest/suggestions — what it has raised by itself and is waiting on
  - tasks.scheduled_tasks : on_interval periodic tasks
  - tasks.observation     : the state-change listener, watchlist resolution, retention
  - tasks.delivery        : drains the outbox, closes approvals and rollbacks a restart orphaned
  - tasks.review          : the daily review pass — waste found, replayed, and put to the user
  - tasks.analyst         : the daily analyst run — the model's ideas, audited, stored for review
  - tasks.heartbeat       : event-loop liveness — the stall log line and GET /health (the watchdog)
"""
import logging
logging.basicConfig(level=logging.INFO)

import socket
socket.setdefaulttimeout(10)  # seconds

from core import settings
from core.uagent import agent_1

# Import handler modules — importing them registers their decorated handlers
# (REST endpoints, intervals, message handlers) on agent_1 / chat_proto.
from rest import rest_chat            # noqa: F401  /rest/post, /rest/approve
from rest import rest_pairing         # noqa: F401  /rest/pairing/status|code|unpair
from rest import rest_observation     # noqa: F401  /rest/observation/registry|selection|record|entity
from rest import rest_activity        # noqa: F401  /rest/activity|/rest/activity/seen
from rest import rest_suggestions     # noqa: F401  /rest/suggestions|/rest/suggestions/verdict
from rest import rest_peers           # noqa: F401  /rest/peers/list|decide|remove
from tasks import scheduled_tasks     # noqa: F401  on_interval tasks
from tasks import observation         # noqa: F401  listener thread + record upkeep
from tasks import delivery            # noqa: F401  outbox drain + restart cleanup
from tasks import review              # noqa: F401  the daily suggestion review pass
from tasks import analyst             # noqa: F401  the daily analyst run, before the review
from tasks import heartbeat           # noqa: F401  loop liveness: stall log + GET /health
from tasks import peer_outcomes       # noqa: F401  tells other agents how their proposals ended
from handlers import chat_handlers    # noqa: F401  chat protocol handlers
from handlers.chat_handlers import chat_proto
from handlers import peer_handlers    # noqa: F401  ConnectRequest / Query from other agents
from core.peer_protocol import peer_proto

print("Running agent version:" + settings.VERSION)

if settings.CLIENTS[settings.IDX]["CHATSUPPORT"]:
    agent_1.include(chat_proto, publish_manifest=True)

# Always, unlike chat: another agent reaches this home only through this
# protocol, and only once a person approves it (core/peers.py).
agent_1.include(peer_proto, publish_manifest=True)

# run main uAgent
agent_1.run()
