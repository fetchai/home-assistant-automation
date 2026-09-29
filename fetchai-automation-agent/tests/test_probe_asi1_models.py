"""scripts/probe_asi1_models.py — which model names ASI-1 answers to (2.19.1).

The script is not part of the add-on and is never run here: `urlopen` is
replaced by a fake that answers from literals and records what it was asked.
What is held: a `/models` list that answers is printed and nothing else is
asked; one that is refused falls back to one 1-token chat completion per
candidate name; the key goes only into the Authorization header and never into
the output, even when the server echoes it back in an error.
"""
import importlib.util
import io
import json
import os
import urllib.error

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "probe_asi1_models.py")
_spec = importlib.util.spec_from_file_location("probe_asi1_models", _PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

KEY = "sk-secret-0123456789"


class _Answer:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "Refused", {}, io.BytesIO(body.encode("utf-8")))


class _FakeUrlopen:
    """`/models` answers `models` (a body, or an exception to raise); each
    chat completion answers for the names in `serves`, and refuses the rest
    with an error body that echoes the key."""

    def __init__(self, models, serves=()):
        self.models = models
        self.serves = set(serves)
        self.asked = []

    def __call__(self, request, timeout=None):
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        url = request.full_url
        if url.endswith("/models"):
            self.asked.append(("GET", "/models"))
            if isinstance(self.models, Exception):
                raise self.models
            return _Answer(self.models)
        payload = json.loads(request.data.decode("utf-8"))
        self.asked.append(("POST", payload["model"]))
        assert payload["max_tokens"] == 1
        if payload["model"] in self.serves:
            return _Answer({"model": payload["model"], "choices": []})
        raise _http_error(url, 404, f'{{"error": "unknown model", "key": "{KEY}"}}')


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("ASI1_API_KEY", KEY)


def _run(fake, *argv):
    out = io.StringIO()
    code = probe.main(list(argv), urlopen=fake, out=out)
    return code, out.getvalue()


def test_a_models_list_is_printed_and_nothing_else_is_asked():
    fake = _FakeUrlopen({"data": [{"id": "asi1-mini"}, {"id": "asi1-extended"}]})
    code, out = _run(fake)
    assert code == 0
    assert "asi1-mini" in out and "asi1-extended" in out
    assert fake.asked == [("GET", "/models")]


def test_a_refused_list_falls_back_to_one_probe_per_candidate():
    fake = _FakeUrlopen(_http_error("x", 404, "not found"), serves={"asi1-mini", "asi1-fast"})
    code, out = _run(fake)
    assert code == 0
    assert "no list (HTTP 404 Refused: not found)" in out
    assert [name for verb, name in fake.asked if verb == "POST"] == list(probe.CANDIDATES)
    assert "OK   asi1-mini" in out and "OK   asi1-fast" in out
    assert "FAIL asi1-ultra" in out and "unknown model" in out
    assert out.strip().endswith("answering: asi1-mini, asi1-fast")


def test_an_empty_or_unreadable_list_falls_back_too():
    fake = _FakeUrlopen({"object": "list", "data": []}, serves={"asi1-mini"})
    code, out = _run(fake)
    assert "the list is empty" in out and ("POST", "asi1-mini") in fake.asked


def test_probe_also_asks_every_listed_id():
    fake = _FakeUrlopen({"data": [{"id": "asi1-mini"}, {"id": "asi1-pro"}]}, serves={"asi1-pro"})
    code, out = _run(fake, "--probe")
    posted = [name for verb, name in fake.asked if verb == "POST"]
    assert posted == list(probe.CANDIDATES) + ["asi1-pro"]
    assert "OK   asi1-pro" in out


def test_nothing_answering_is_a_failure_exit():
    fake = _FakeUrlopen(OSError("network is unreachable"))
    code, out = _run(fake)
    assert code == 1 and "answering: none" in out


@pytest.mark.parametrize("models", [OSError(f"refused for {KEY}"),
                                    {"data": [{"id": "asi1-mini"}]}])
def test_the_key_never_appears_in_the_output(models):
    fake = _FakeUrlopen(models, serves=())
    code, out = _run(fake, "--probe")
    assert KEY not in out
    assert "<key>" in out


def test_no_key_asks_nothing(monkeypatch):
    monkeypatch.delenv("ASI1_API_KEY")
    fake = _FakeUrlopen({"data": []})
    code, out = _run(fake)
    assert code == 2 and fake.asked == []
