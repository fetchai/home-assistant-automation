The Fetch.ai Automation Agent is a smart autonomous digital agent for Home Assistant. It talks to the user
in natural language, controls their devices, and turns what the user describes into real Home Assistant
automations — proposing, creating, modifying and deleting them, with the user's confirmation.

# **Pairing is required**

This agent is bound to one household. It will not read or change anything until the owner of that home has
paired your address from the add-on panel inside their Home Assistant.

If you are the owner: open the **Fetch.ai Automation Agent** panel in Home Assistant, press **Pair a
device**, and send the six-digit code it shows to this agent as a chat message. The code is valid for 10
minutes and can be used once. Several household members can pair, and any of them can approve changes.

Until an address is paired the agent replies with those instructions and nothing else.

# **Core Capabilities**

### *Home Assistant discovery*

The agent can inspect the user's Home Assistant instance (read-only) to find out what is actually there:
- Listing the areas (rooms) of the home and the entities that belong to each of them.
- Listing entities and their current states, filtered by room and by kind of device — e.g. "You have 4
  lights in the living room."
- Getting the details and attributes of a single entity.
- Listing devices and the services (actions) available for a domain — e.g. what `light.*` can do.
- Listing existing automations and reading the full config of any of them.

### *Room-aware resolution*

When the user says "the kitchen light", the agent resolves the room through the Home Assistant area
registry and uses the entity directly when the room contains only one matching device. It asks the user to
choose only when there really are several candidates, and then lists them by name.

### *Direct control*

The agent can act on the home immediately by calling Home Assistant services — turning entities on, off or
toggling them, setting brightness, temperature, volume and any other service parameter. Everyday domains
run straight away; actions that are not trivially undone (locks, alarm, covers, notifications, Home
Assistant itself) are confirmed by the user first.

### *Automation proposals*

Given a goal rather than a rule ("I'd like the house to look occupied when we're away"), the agent proposes
concrete automations in plain language — trigger, conditions, actions — and only builds the one the user picks.

### *Automation management*

The agent creates, updates and deletes Home Assistant automations through the Core API:
- Every config is validated against Home Assistant before the user is asked to confirm it.
- Every create/update/delete is confirmed by the user first — in the panel with a YAML diff, in chat with a
  YES/NO reply.
- Updates replace the whole automation, so the agent always reads the current config first.

Requests for something to happen right now are executed as a service call; automations are reserved for
what should happen on a schedule or in reaction to an event.

# **Sample questions**
- What is your version?
- What rooms do I have?
- What lights do I have?
- Turn off the kitchen light.
- Dim the living room lamps to 30%.
- What services can I use with my thermostat?
- Turn on the porch light at sunset and off at 23:00.
- Every weekday at 6:30 open the bedroom blinds, but only if somebody is home.
- List my automations.
- What does the "Morning routine" automation do?
- Change the morning routine to start at 7:00 instead of 6:30.
- Delete the automation that turns off the heating.
