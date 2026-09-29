"""Message and data model definitions used by the agent's REST API and chat
protocol."""
from uagents import Model


#REST CLI protocol message
class Request(Model):
    """REST chat request: a user message plus its session id and optional model."""
    text: str
    session_id: str
    model: str = ""

class Response(Model):
    """REST chat response: the agent's reply text."""
    text: str


class SessionRequest(Model):
    """Identifies one panel chat session for lifecycle endpoints."""
    session_id: str


# Pairing endpoints (rest/rest_pairing.py). All three answer with the same
# status shape so the panel can refresh from any of them.
class PairingRequest(Model):
    """Pairing request body. `address` is only read by the unpair endpoint."""
    address: str = ""

class PeersRequest(Model):
    """Body of every /rest/peers/* call. `action` (approve, deny, block) and
    `level` are read by decide; `address` by decide, remove and the ask pair;
    `name`, `purpose` and `confirm` by /rest/peers/ask/add, `confirm` by
    /rest/peers/ask/confirm."""
    address: str = ""
    action: str = ""
    level: str = "read"
    name: str = ""
    purpose: str = ""
    confirm: bool = True

class PeersResponse(Model):
    """Every agent the household knows (core/peers.py `list_peers`), the levels
    one may be granted, the agents' proposals still waiting for a person, and
    the outcome of the call just made — `error` is empty when it did what was
    asked. A proposal is answered through /rest/approve with its change_id."""
    peers: list[dict] = []
    levels: list[str] = []
    proposals: list[dict] = []
    error: str = ""

class PairingStatusResponse(Model):
    """Current bindings plus the outstanding pairing code, if any.
    `code` is empty when no code is live — the panel shows nothing then.
    `agent_address` is this agent's own uAgent address, which the user needs in
    order to address it from ASI1."""
    agent_address: str = ""
    paired: list[dict] = []
    code: str = ""
    code_expires_at: int = 0


# Observation endpoints (rest/rest_observation.py) — which entities the agent
# watches, and what it has recorded about them. One request model across all
# four, the way PairingRequest serves the pairing endpoints: each field is read
# only by the endpoints it makes sense for.
class ObservationRequest(Model):
    """`devices`/`entities` are the selection being saved; `entity_id`, `limit`
    and `days` are read by the per-entity detail endpoint."""
    devices: list[str] = []
    entities: list[str] = []
    entity_id: str = ""
    limit: int = 100
    days: int = 30

class ObservationRegistryResponse(Model):
    """The device/entity tree the picker renders, and what is ticked in it.
    `error` is non-empty when Home Assistant could not be reached, in which
    case the tree is empty and the panel says so rather than showing nothing
    as if the home had no devices."""
    areas: list[dict] = []
    selection: dict = {}
    watched_count: int = 0
    hidden_count: int = 0
    error: str = ""

class ObservationRecordResponse(Model):
    """What has actually been gathered: one row per watched entity, the
    whole-record totals, the strongest observed patterns, and the health of
    the link that fills it."""
    status: dict = {}
    entities: list[dict] = []
    totals: dict = {}
    relations: list[dict] = []
    error: str = ""

class ObservationEntityResponse(Model):
    """One entity's recent transitions, its on-read profile, and the patterns
    it triggers."""
    entity_id: str = ""
    name: str = ""
    recent: list[dict] = []
    profile: dict = {}
    relations: list[dict] = []
    error: str = ""


# Activity endpoints (rest/rest_activity.py) — what the agent has done, and
# what it still has to say. Same one-request-model-per-area shape as the
# observation endpoints above.
class ActivityRequest(Model):
    """`limit` bounds both listings; `change_id` is read only by the rollback
    endpoint, which undoes one recorded change."""
    limit: int = 50
    change_id: int = 0

class ActivityResponse(Model):
    """The change journal, the outbox, and the totals the tab header and its
    badge are drawn from.

    `error` carries a refused or failed rollback back to the panel, alongside a
    listing that already reflects it — the row saying the undo failed is in
    `changes` by the time the message is read.
    """
    changes: list[dict] = []
    messages: list[dict] = []
    totals: dict = {}
    error: str = ""


# Suggestion endpoints (rest/rest_suggestions.py) — what the agent has raised
# by itself and is waiting on. There is deliberately no approve endpoint here:
# the panel answers a suggestion by posting its change_id to /rest/approve, the
# same one the chat path reaches, because a second way in would be a second
# place for "everything lands on gate.apply" to stop being true. The verdict
# endpoint is not that: it answers a change that was approved a week ago and
# writes nothing through the gate — see rest_suggestions.py.
class SuggestionsRequest(Model):
    """`limit` bounds the listing; `suggestion_id` and `keep` are read only by
    the verdict endpoint, which answers one measured result; `suggestion_id`,
    `answer` ("yes" / "no") and `reason` only by the feedback endpoint, which
    RECORDS a shown row's answer (free-hand F-11) and approves nothing."""
    limit: int = 10
    suggestion_id: int = 0
    keep: bool = False
    answer: str = ""
    reason: str = ""

class SuggestionsResponse(Model):
    """The open proposals and the measured results, as §9's artefacts, already
    rendered.

    `digest` is artefact 1 — one line and one number per suggestion. `open` is
    one entry per suggestion carrying artefacts 2 and 3 plus the journal's own
    YAML for the diff, so the panel lays the sentences out rather than deciding
    what they may claim; core/artefacts.py decides that once, for both surfaces.
    `verified` is artefact 4 the same way: a week's measurement and the choice
    §7.2 offers whatever it came to.

    An `open` entry for an explorer idea (`evidence_level` "shown") also
    carries its stored `chart`, `sent` and `feedback` as they are, and
    artefacts.shown_detail's sentences for them; the panel draws the chart.

    `error` carries a refused or failed rollback back from the verdict
    endpoint, the way ActivityResponse's does — beside a listing that already
    reflects it — and a refused answer from the feedback endpoint.
    """
    digest: list[dict] = []
    open: list[dict] = []
    verified: list[dict] = []
    totals: dict = {}
    error: str = ""
    outcome: str = ""


# Liveness (tasks/heartbeat.py). GET /health is what the Supervisor's watchdog
# polls, through proxy.py (8098 -> 127.0.0.1:8099): it can only answer while the
# event loop runs, which is the whole check — a stalled loop times the request
# out and the add-on restarts.
class HealthResponse(Model):
    """`heartbeat_age_s` is seconds since the 60 s interval last ran — for a
    person reading it; the Supervisor looks only at the status code."""
    status: str = "ok"
    heartbeat_age_s: float = 0.0
