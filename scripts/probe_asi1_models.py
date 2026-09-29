#!/usr/bin/env python3
"""Which model names ASI-1 answers to, asked of ASI-1 itself.

The strong tier (`asi1_model_strong`) has been unset since it existed because
nobody has verified a name: the analyst proposal's §7 lists a catalogue from
documentation, not from the API. Explorer field run 1 (2.19.0) then ran on
asi1-mini because of it. This settles the name before field run 2.

Two ways to ask, in order:

1. GET `/v1/models`, the OpenAI-compatible list. If it answers, its ids are
   printed and that is the answer — unless `--probe` is given, which also
   runs step 2 over the candidates and every listed id (a listed id is not
   proof it serves chat completions).
2. If the list is refused, absent or unreadable, one POST to
   `/v1/chat/completions` per candidate name with `max_tokens: 1`: the name
   answers, or the error says why not (unknown model, not on this plan, …).
   One token each — a handful of calls, cents at most.

The key comes from `ASI1_API_KEY`, which `with_addon_key.py` supplies inside
the add-on's container. It is never printed: it goes only into the
Authorization header, and every error body printed has it replaced first, in
case the server echoes the request.

Stdlib only, like its neighbours. Writes nothing.

    python3 with_addon_key.py probe_asi1_models.py
    python3 with_addon_key.py probe_asi1_models.py --probe      # list AND chat-probe
    python3 with_addon_key.py probe_asi1_models.py --model asi1-pro
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# core/models.py's base_url — the one the agent itself talks to.
BASE_URL = "https://api.asi1.ai/v1"

# Names seen in ASI-1's documentation and announcements, none verified.
CANDIDATES = ("asi1-mini", "asi1", "asi1-extended", "asi1-fast", "asi1-ultra", "asi1-graph")

TIMEOUT_SECONDS = 30

# How much of an error body is printed. Enough for the server's reason.
ERROR_BODY_CHARS = 300


def _redact(text, key) -> str:
    text = str(text)
    return text.replace(key, "<key>") if key else text


def _error(exc, key) -> str:
    """What went wrong, as one printable line with the key taken out."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        body = " ".join(body.split())[:ERROR_BODY_CHARS]
        return _redact(f"HTTP {exc.code} {exc.reason}" + (f": {body}" if body else ""), key)
    return _redact(f"{type(exc).__name__}: {exc}", key)


def _request(path, key, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    return urllib.request.Request(
        BASE_URL + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})


def list_models(key, urlopen):
    """→ (ids, "") from `/models`, or (None, why)."""
    try:
        with urlopen(_request("/models", key), timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return None, _error(exc, key)
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return None, "the answer has no 'data' list"
    ids = [row.get("id") for row in rows if isinstance(row, dict) and row.get("id")]
    if not ids:
        return None, "the list is empty"
    return ids, ""


def probe(name, key, urlopen):
    """→ (True, the model the answer names) or (False, why), from one
    1-token chat completion."""
    payload = {"model": name, "max_tokens": 1, "temperature": 0,
               "messages": [{"role": "user", "content": "ping"}]}
    try:
        with urlopen(_request("/chat/completions", key, payload),
                     timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, _error(exc, key)
    answered = body.get("model") if isinstance(body, dict) else None
    return True, f"answered as {answered!r}" if answered else "answered"


def main(argv=None, urlopen=None, out=None) -> int:
    urlopen = urlopen or urllib.request.urlopen
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Which model names ASI-1 answers to.")
    parser.add_argument("--probe", action="store_true",
                        help="chat-probe the candidates even when /models answers")
    parser.add_argument("--model", action="append", default=[],
                        help="another name to chat-probe (repeatable)")
    args = parser.parse_args(argv)

    key = os.environ.get("ASI1_API_KEY", "")
    if not key:
        print("ASI1_API_KEY is not set — run this through with_addon_key.py", file=out)
        return 2

    ids, why = list_models(key, urlopen)
    if ids is not None:
        print(f"GET {BASE_URL}/models — {len(ids)} model(s):", file=out)
        for model_id in ids:
            print(f"  {_redact(model_id, key)}", file=out)
        if not args.probe and not args.model:
            return 0
    else:
        print(f"GET {BASE_URL}/models — no list ({why}); probing each name instead", file=out)

    names = list(dict.fromkeys(list(CANDIDATES) + (ids or []) + args.model))
    answering = []
    print(f"POST {BASE_URL}/chat/completions, max_tokens 1, per name:", file=out)
    for name in names:
        ok, detail = probe(name, key, urlopen)
        print(f"  {'OK  ' if ok else 'FAIL'} {_redact(name, key)} — {detail}", file=out)
        if ok:
            answering.append(name)
    print(f"answering: {', '.join(answering) if answering else 'none'}", file=out)
    return 0 if answering else 1


if __name__ == "__main__":
    sys.exit(main())
