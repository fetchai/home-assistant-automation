"""core/models.py is the model layer: which model a tier resolves to, and the
strong-tier daily cap that must fail closed rather than fail open. These tests
are pure stdlib — no network, no real ChatOpenAI construction (conftest stubs
langchain_openai to raise on that).
"""
import json

import pytest

from core import models, settings


@pytest.fixture(autouse=True)
def fresh_budget(tmp_path, monkeypatch):
    """Points the budget file at a temp path and clears the module singleton,
    the same isolation test_watchlist.py uses for its own JSON store."""
    monkeypatch.setattr(models, "BUDGET_PATH", str(tmp_path / "model_budget.json"))
    monkeypatch.setattr(models, "_state", None)
    monkeypatch.setattr(settings, "ASI1_MODEL", "asi1-mini")
    monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "")
    monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 20)
    yield
    models._state = None


class TestModelFor:
    def test_cheap_is_always_asi1_model(self):
        assert models.model_for(models.CHEAP) == "asi1-mini"

    def test_strong_falls_back_to_cheap_when_unset(self):
        assert models.model_for(models.STRONG) == "asi1-mini"

    def test_strong_uses_the_configured_model_when_set(self, monkeypatch):
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        assert models.model_for(models.STRONG) == "asi1-ultra"


class TestTakeStrongCall:
    def test_returns_the_model_name_and_is_free_of_cost_when_strong_equals_cheap(self):
        # Unset strong tier == cheap tier: today's behaviour, nothing to ration.
        import os

        assert models.take_strong_call() == "asi1-mini"
        assert not os.path.exists(models.BUDGET_PATH)

    def test_consumes_one_unit_per_call_until_the_cap(self, monkeypatch):
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 2)

        assert models.take_strong_call() == "asi1-ultra"
        assert models.take_strong_call() == "asi1-ultra"
        assert models.take_strong_call() is None

    def test_refusal_does_not_fall_back_to_the_cheap_model(self, monkeypatch):
        """The caller decides what over-budget means — take_strong_call must
        not quietly hand back the cheap model in its place."""
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 0)

        assert models.take_strong_call() is None

    def test_date_rollover_resets_the_count(self, monkeypatch):
        import datetime as dt

        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 1)

        day1 = dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.timezone.utc)
        day2 = dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.timezone.utc)

        assert models.take_strong_call(now=day1) == "asi1-ultra"
        assert models.take_strong_call(now=day1) is None
        assert models.take_strong_call(now=day2) == "asi1-ultra"

    def test_persists_across_a_module_reload(self, monkeypatch):
        """A restart must not refill the cap — the count is a file, not a
        process-local variable."""
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 1)

        assert models.take_strong_call() == "asi1-ultra"
        # Simulate a fresh process: drop the cached state, keep the file.
        models._state = None
        assert models.take_strong_call() is None

    def test_corrupt_file_fails_closed_and_is_rewritten(self, monkeypatch):
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 5)

        with open(models.BUDGET_PATH, "w", encoding="utf-8") as f:
            f.write("not json")

        assert models.take_strong_call() is None

        with open(models.BUDGET_PATH, encoding="utf-8") as f:
            written = json.load(f)
        assert written["used"] == 5

    def test_missing_file_starts_at_zero_used_not_failed_closed(self, monkeypatch):
        """No file yet is a fresh install, not corruption — it must not be
        treated as an already-spent cap."""
        monkeypatch.setattr(settings, "ASI1_MODEL_STRONG", "asi1-ultra")
        monkeypatch.setattr(settings, "ASI1_STRONG_DAILY_CAP", 1)

        assert models.take_strong_call() == "asi1-ultra"


class TestChatModel:
    """conftest stubs ChatOpenAI to raise on construction, so the constructor
    is monkeypatched here and its arguments asserted instead."""

    def test_builds_with_the_given_model_and_asi1_base_url(self, monkeypatch):
        captured = {}

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        import langchain_openai
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)

        models.chat_model("asi1-mini")

        assert captured["model"] == "asi1-mini"
        assert captured["base_url"] == "https://api.asi1.ai/v1"
        assert captured["temperature"] == 0.1

    def _captured(self, monkeypatch, **kwargs):
        captured = {}

        class _FakeChatOpenAI:
            def __init__(self, **kw):
                captured.update(kw)

        import langchain_openai
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
        models.chat_model("asi1-mini", **kwargs)
        return captured

    def test_a_timeout_is_passed_through(self, monkeypatch):
        """2.18.2's rule applied to the model: the explorer's calls are bounded."""
        assert self._captured(monkeypatch, timeout=90)["timeout"] == 90

    def test_without_one_the_client_keeps_its_default(self, monkeypatch):
        """Chat passes none — rest_chat's wait_for already cuts its calls."""
        assert "timeout" not in self._captured(monkeypatch)

    def test_retries_are_pinned_either_way(self, monkeypatch):
        assert self._captured(monkeypatch)["max_retries"] == models.MODEL_MAX_RETRIES == 2
        assert self._captured(monkeypatch, timeout=5)["max_retries"] == models.MODEL_MAX_RETRIES
