"""Low-level HTTP and websocket client helpers for the Home Assistant
Supervisor and Core APIs, shared by all tool_homeassistant tools."""
import json

import requests

from core import settings

SUPERVISOR_BASE_URL = "http://supervisor"


# What almost every call in this file is: a state read, a service call, an
# automation reload. Reading the recorder is the exception — Home Assistant has
# to query the database and serialise the answer, which on a Pi with MariaDB is
# seconds even for a well-sized window — so those callers pass their own.
DEFAULT_TIMEOUT_SECONDS = 10


def _request(method: str, base_url: str, path: str, *, token_missing_msg: str,
             request_failed_prefix: str, non_json_prefix: str,
             body=None, tolerate_empty_body: bool = False,
             timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """Sends a GET/POST/DELETE request with the supervisor bearer token and
    returns (True, parsed_json) on success or (False, error_message) otherwise."""
    token = settings.SUPERVISOR_TOKEN
    if not token:
        return False, token_missing_msg
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        if method == "GET":
            r = requests.get(f"{base_url}{path}", headers=headers, timeout=timeout)
        elif method == "DELETE":
            r = requests.delete(f"{base_url}{path}", headers=headers, timeout=timeout)
        elif method == "POST":
            r = requests.post(f"{base_url}{path}", headers=headers, json=body if body is not None else {}, timeout=timeout)
    except Exception as e:
        return False, f"{request_failed_prefix}: {e}"
    if 200 <= r.status_code < 300:
        if tolerate_empty_body:
            try:
                return True, r.json() if r.text else {}
            except Exception:
                return True, {}
        try:
            return True, r.json()
        except Exception as e:
            return False, f"{non_json_prefix}: {e}"
    return False, f"HTTP {r.status_code}: {r.text}"


def _supervisor_get(path: str):
    """GETs a path on the Supervisor API."""
    return _request(
        "GET", SUPERVISOR_BASE_URL, path,
        token_missing_msg="Supervisor token not available",
        request_failed_prefix="Supervisor request failed",
        non_json_prefix="Supervisor returned non-JSON",
    )


def _ha_get(path: str, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """GETs a path on the Home Assistant Core API."""
    return _request(
        "GET", settings.HA_API_URL, path,
        token_missing_msg="Home Assistant API not available — addon must run inside HA with homeassistant_api: true",
        request_failed_prefix="HA request failed",
        non_json_prefix="HA returned non-JSON",
        timeout=timeout,
    )


def _ha_delete(path: str):
    """DELETEs a path on the Home Assistant Core API."""
    return _request(
        "DELETE", settings.HA_API_URL, path,
        token_missing_msg="Home Assistant API not available — addon must run inside HA with homeassistant_api: true",
        request_failed_prefix="HA request failed",
        non_json_prefix="HA returned non-JSON",
        tolerate_empty_body=True,
    )


def _ha_post(path: str, body):
    """POSTs a JSON body to a path on the Home Assistant Core API."""
    return _request(
        "POST", settings.HA_API_URL, path,
        token_missing_msg="Home Assistant API not available — addon must run inside HA with homeassistant_api: true",
        request_failed_prefix="HA request failed",
        non_json_prefix="HA returned non-JSON",
        body=body,
        tolerate_empty_body=True,
    )


def _ha_ws_connect(timeout: int = 10):
    """Opens an authenticated websocket to Home Assistant Core and returns
    (True, socket) or (False, error_message).

    The handshake — wait for auth_required, send the token, check for auth_ok —
    is the same whether the caller wants to run a couple of registry queries and
    hang up, or hold the connection open for hours subscribed to events. It
    lives here so there is one copy of it: the caller is responsible for
    closing what it gets back.
    """
    import websocket as ws_lib
    token = settings.SUPERVISOR_TOKEN
    if not token:
        return False, "Supervisor token not available"
    ws = None
    try:
        ws = ws_lib.create_connection(settings.HA_WS_URL, timeout=timeout)
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            _ha_ws_close(ws)
            return False, f"HA websocket auth failed: {auth_result}"
        return True, ws
    except Exception as e:
        _ha_ws_close(ws)
        return False, f"HA websocket request failed: {e}"


def _ha_ws_close(ws):
    """Closes a websocket without letting the close itself raise."""
    if ws is None:
        return
    try:
        ws.close()
    except Exception:
        pass


def _ha_ws_calls(payloads: list, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """Runs several commands over one authenticated HA websocket connection
    and returns (True, [result, ...]) in payload order, or (False, error).

    The area/device/entity registries are only reachable over the websocket
    API — HA Core's REST API does not expose them — so registry-backed tools
    (areas, entity filtering) go through here. So do the recorder's long-term
    statistics, which are the reason for the timeout parameter: a registry
    answers instantly, a statistics query does not.
    """
    if not settings.SUPERVISOR_TOKEN:
        return False, "Supervisor token not available"
    if not payloads:
        return True, []

    ok, ws = _ha_ws_connect(timeout=timeout)
    if not ok:
        return False, ws
    try:
        for msg_id, payload in enumerate(payloads, start=1):
            ws.send(json.dumps({"id": msg_id, **payload}))

        results = {}
        # Commands are answered by id, not necessarily in the order they were
        # sent, and other message types may arrive in between.
        while len(results) < len(payloads):
            msg = json.loads(ws.recv())
            if msg.get("type") != "result":
                continue
            if not msg.get("success", False):
                return False, f"HA websocket command failed: {msg.get('error', msg)}"
            results[msg.get("id")] = msg.get("result")
        return True, [results.get(i) for i in range(1, len(payloads) + 1)]
    except Exception as e:
        return False, f"HA websocket request failed: {e}"
    finally:
        _ha_ws_close(ws)


def _ha_ws_call(payload: dict, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """Runs a single command over the HA websocket API."""
    ok, data = _ha_ws_calls([payload], timeout=timeout)
    if not ok:
        return False, data
    return True, data[0]
