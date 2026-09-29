"""Automation config normalisation, the offline half of preflight validation,
and the integration-level writes.

_preflight_automation is what stops an invalid config ever reaching the user as
an approval — it loops back to the model instead. Every rejection path it has is
pure, and one test here proves that: none of them may touch Home Assistant.

The write functions are what core/rollback.py calls, and what the LLM tools now
call too. The tests that matter for them are the ones proving those are the same
thing: a tool that stopped going through the function would let the rollback
path and the agent path drift apart, which is exactly what extracting them was
meant to prevent.
"""
import json

import pytest

from tools.tool_homeassistant import automations
from tools.tool_homeassistant.automations import (
    _normalize_automation_cfg,
    _preflight_automation,
)


VALID = {"alias": "Test", "trigger": [{"platform": "sun", "event": "sunset"}],
         "action": [{"service": "light.turn_on"}]}


class TestNormalizeAutomationCfg:
    @pytest.mark.parametrize("plural,singular", [
        ("triggers", "trigger"), ("conditions", "condition"), ("actions", "action"),
    ])
    def test_plural_keys_are_renamed(self, plural, singular):
        out = _normalize_automation_cfg({plural: [1]})
        assert out == {singular: [1]}

    def test_all_three_at_once(self):
        out = _normalize_automation_cfg({"triggers": [1], "conditions": [2], "actions": [3]})
        assert out == {"trigger": [1], "condition": [2], "action": [3]}

    def test_the_singular_key_wins_and_the_plural_survives(self):
        """The guard is `plural in cfg and singular not in cfg`, so when both are
        present nothing is renamed and nothing is dropped."""
        out = _normalize_automation_cfg({"triggers": [1], "trigger": [2]})
        assert out == {"triggers": [1], "trigger": [2]}

    def test_it_mutates_the_callers_dict(self):
        """Three call sites rely on this — they pass a dict and then keep using
        their own reference to it."""
        cfg = {"triggers": [1]}
        out = _normalize_automation_cfg(cfg)
        assert out is cfg
        assert cfg == {"trigger": [1]}

    @pytest.mark.parametrize("cfg", [{}, {"alias": "x"}])
    def test_unrelated_configs_are_untouched(self, cfg):
        assert _normalize_automation_cfg(dict(cfg)) == cfg


class TestPreflightRejections:
    @pytest.mark.parametrize("raw", ["", "not json", "{", "{'single': 'quotes'}"])
    def test_unparseable_json(self, raw):
        assert _preflight_automation(raw).startswith("Invalid JSON in config_json:")

    @pytest.mark.parametrize("raw", ["[]", '"a string"', "null", "42", "true"])
    def test_json_that_is_not_an_object(self, raw):
        assert _preflight_automation(raw) == "config_json must be a JSON object"

    @pytest.mark.parametrize("raw", [
        '{"trigger":[],"action":[]}',                 # no alias
        '{"alias":"a","action":[]}',                  # no trigger
        '{"alias":"a","trigger":[]}',                 # no action
        '{}',
    ])
    def test_missing_required_keys(self, raw):
        assert _preflight_automation(raw) == "config must include alias, trigger, and action"

    @pytest.mark.parametrize("raw", [
        '{"alias":"a","trigger":{},"action":[]}',
        '{"alias":"a","trigger":[],"action":"light.turn_on"}',
    ])
    def test_trigger_and_action_must_be_arrays(self, raw):
        assert _preflight_automation(raw) == "trigger and action must be arrays"

    def test_plural_keys_satisfy_the_requirement(self, monkeypatch):
        """Normalisation runs before validation, so the plural spelling the LLM
        sometimes emits is accepted rather than rejected as missing."""
        monkeypatch.setattr(automations, "_validate_by_test_create", lambda cfg: (True, "1"))
        assert _preflight_automation('{"alias":"a","triggers":[],"actions":[]}') is None

    def test_invalid_configs_never_touch_home_assistant(self, monkeypatch):
        """The rejection paths must all return before the test-create. If any of
        them reached HA, an invalid config would create a real automation."""
        def _boom(*args, **kwargs):
            raise AssertionError("preflight reached Home Assistant with an invalid config")

        monkeypatch.setattr(automations, "_ha_post", _boom)
        monkeypatch.setattr(automations, "_ha_delete", _boom)
        monkeypatch.setattr(automations, "_validate_by_test_create", _boom)

        for raw in ["", "not json", "[]", "null", "{}", '{"alias":"a","trigger":{},"action":[]}']:
            assert _preflight_automation(raw) is not None


class TestPreflightAcceptance:
    def test_a_valid_config_returns_none_and_gets_defaults(self, monkeypatch):
        captured = {}

        def _capture(cfg):
            captured.update(cfg)
            return True, "1700000000000"

        monkeypatch.setattr(automations, "_validate_by_test_create", _capture)
        assert _preflight_automation(
            '{"alias":"Test","trigger":[{"platform":"sun"}],"action":[{"service":"light.turn_on"}]}'
        ) is None
        assert captured["mode"] == "single"
        assert captured["description"] == ""
        assert captured["condition"] == []

    def test_an_explicit_mode_is_not_overwritten(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            automations, "_validate_by_test_create",
            lambda cfg: (captured.update(cfg), (True, "1"))[1],
        )
        _preflight_automation(
            '{"alias":"a","trigger":[],"action":[],"mode":"restart"}'
        )
        assert captured["mode"] == "restart"

    def test_a_rejection_from_home_assistant_is_reported_back_to_the_model(self, monkeypatch):
        monkeypatch.setattr(
            automations, "_validate_by_test_create", lambda cfg: (False, "bad trigger")
        )
        out = _preflight_automation('{"alias":"a","trigger":[],"action":[]}')
        assert out.startswith("HA rejected this config:")
        assert out.endswith("Fix the config and retry.")
        assert "bad trigger" in out


class TestTheWritesThemselves:
    """No tool, no model, no registry — an id and a dict."""

    @pytest.fixture
    def ha(self, monkeypatch):
        """Captures the HTTP the write layer makes, and accepts it all."""
        calls = []

        def _post(path, body=None):
            calls.append(("POST", path, body))
            return True, {}

        def _delete(path):
            calls.append(("DELETE", path, None))
            return True, {}

        monkeypatch.setattr(automations, "_ha_post", _post)
        monkeypatch.setattr(automations, "_ha_delete", _delete)
        return calls

    def test_a_write_posts_the_config_and_reloads(self, ha):
        ok, detail = automations.create_automation("1748", dict(VALID))

        assert (ok, detail) == (True, "")
        assert ha[0][:2] == ("POST", "/config/automation/config/1748")
        assert ha[1][:2] == ("POST", "/services/automation/reload")

    def test_restoring_and_updating_are_the_same_call(self, ha):
        """HA's config endpoint does not distinguish them. Two names, because
        the journal distinguishes them; one body, because the wire does not."""
        automations.create_automation("1748", dict(VALID))
        automations.update_automation("1748", dict(VALID))

        assert ha[0] == ha[2]

    def test_defaults_are_filled_in(self, ha):
        automations.update_automation("1748", {"alias": "a", "trigger": [], "action": []})

        assert ha[0][2]["mode"] == "single"
        assert ha[0][2]["condition"] == []

    def test_plural_keys_are_accepted(self, ha):
        """Same normalisation the tools always did — it moved down with them."""
        automations.create_automation("1748", {"alias": "a", "triggers": [1], "actions": [2]})

        assert ha[0][2]["trigger"] == [1]

    def test_a_rejected_write_is_a_failure_not_a_warning(self, monkeypatch):
        monkeypatch.setattr(automations, "_ha_post", lambda path, body=None: (False, "HTTP 400"))

        assert automations.create_automation("1748", dict(VALID)) == (False, "HTTP 400")

    def test_a_write_that_could_not_reload_succeeds_and_says_so(self, monkeypatch):
        """Neither a clean success nor something to roll back: the config IS
        written, it is just not live yet."""
        def _post(path, body=None):
            return (False, "HA is busy") if "reload" in path else (True, {})

        monkeypatch.setattr(automations, "_ha_post", _post)

        ok, detail = automations.update_automation("1748", dict(VALID))

        assert ok is True
        assert "reload failed" in detail

    def test_delete_does_not_reload(self, ha):
        """Home Assistant reloads on its own for a delete, and always did — the
        extraction must not have quietly added a second call."""
        assert automations.delete_automation("1748") == (True, "")
        assert ha == [("DELETE", "/config/automation/config/1748", None)]

    @pytest.mark.parametrize("write", ["create_automation", "update_automation",
                                       "delete_automation"])
    def test_no_id_means_no_request(self, write, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("wrote to Home Assistant with no automation id")

        monkeypatch.setattr(automations, "_ha_post", _boom)
        monkeypatch.setattr(automations, "_ha_delete", _boom)

        ok, detail = getattr(automations, write)(*[""] + ([{}] if "delete" not in write else []))
        assert (ok, detail) == (False, "automation_id is required")


class TestFindDependentAutomations:
    """Walks each automation's config looking for one entity_id — used only by
    tasks/observation.py, when a watched entity has actually disappeared, never
    on the resolve_watchlist tick itself."""

    def _states(self, ids):
        return [
            {"entity_id": f"automation.{i}", "state": "on",
             "attributes": {"id": i, "friendly_name": f"Automation {i}"}}
            for i in ids
        ]

    def test_an_entity_referenced_by_two_automations_names_both(self, monkeypatch):
        monkeypatch.setattr(automations, "_ha_get",
                            lambda path: (True, self._states(["1", "2", "3"])))
        configs = {
            "1": {"alias": "A", "trigger": [{"platform": "state", "entity_id": "light.hall"}],
                  "action": []},
            "2": {"alias": "B", "condition": [{"condition": "state",
                  "entity_id": "sensor.other"}], "action": []},
            "3": {"alias": "C", "action": [{"service": "light.turn_on",
                  "target": {"entity_id": ["light.hall", "light.kitchen"]}}]},
        }
        monkeypatch.setattr(automations, "read_automation",
                            lambda aid: (True, configs[aid]))

        ok, names = automations.find_dependent_automations("light.hall")

        assert ok is True
        assert sorted(names) == ["A", "C"]

    def test_an_entity_referenced_by_none_names_none(self, monkeypatch):
        monkeypatch.setattr(automations, "_ha_get",
                            lambda path: (True, self._states(["1"])))
        monkeypatch.setattr(
            automations, "read_automation",
            lambda aid: (True, {"alias": "A", "trigger": [{"entity_id": "sensor.other"}],
                                "action": []}),
        )

        ok, names = automations.find_dependent_automations("light.hall")

        assert (ok, names) == (True, [])

    def test_an_entity_nested_inside_a_choose_branch_is_found(self, monkeypatch):
        monkeypatch.setattr(automations, "_ha_get",
                            lambda path: (True, self._states(["1"])))
        monkeypatch.setattr(automations, "read_automation", lambda aid: (True, {
            "alias": "Nested",
            "action": [{"choose": [{"conditions": [{"condition": "state",
                        "entity_id": "light.hall"}], "sequence": []}]}],
        }))

        ok, names = automations.find_dependent_automations("light.hall")

        assert (ok, names) == (True, ["Nested"])

    def test_the_listing_itself_failing_is_reported_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(automations, "_ha_get", lambda path: (False, "connection refused"))

        ok, detail = automations.find_dependent_automations("light.hall")

        assert (ok, detail) == (False, "connection refused")

    def test_one_automations_config_failing_to_load_fails_the_whole_lookup(self, monkeypatch):
        """A caller that cannot tell "found none" from "checked none" must be
        told which one it got — under-reporting would be exactly the kind of
        guess this feature exists to stop."""
        monkeypatch.setattr(automations, "_ha_get",
                            lambda path: (True, self._states(["1"])))
        monkeypatch.setattr(automations, "read_automation", lambda aid: (False, "HTTP 404"))

        ok, detail = automations.find_dependent_automations("light.hall")

        assert (ok, detail) == (False, "HTTP 404")


class TestTheToolsGoThroughThem:
    """The tools are wrappers now. If one grew its own HTTP call back, the
    rollback path and the agent path would be writing automations two different
    ways — which is the drift the extraction exists to prevent."""

    @pytest.fixture
    def no_http(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("a tool reached Home Assistant without the write layer")

        monkeypatch.setattr(automations, "_ha_post", _boom)
        monkeypatch.setattr(automations, "_ha_delete", _boom)

    def test_create(self, monkeypatch, no_http):
        seen = {}
        monkeypatch.setattr(automations, "create_automation",
                            lambda aid, cfg: (seen.update(id=aid, cfg=cfg), (True, ""))[1])

        out = json.loads(automations.tool_ha_create_automation()._run(json.dumps(VALID)))

        assert out["created"] is True
        assert out["automation_id"] == seen["id"]
        assert seen["cfg"]["alias"] == "Test"

    def test_update(self, monkeypatch, no_http):
        seen = {}
        monkeypatch.setattr(automations, "update_automation",
                            lambda aid, cfg: (seen.update(id=aid), (True, ""))[1])

        out = json.loads(automations.tool_ha_update_automation()._run(
            "automation.1748", json.dumps(VALID)))

        assert out["updated"] is True
        # The entity_id prefix is stripped before the write layer sees it.
        assert seen["id"] == "1748"

    def test_delete(self, monkeypatch, no_http):
        seen = {}
        monkeypatch.setattr(automations, "delete_automation",
                            lambda aid: (seen.update(id=aid), (True, ""))[1])

        out = json.loads(automations.tool_ha_delete_automation()._run("automation.1748"))

        assert out["deleted"] is True
        assert seen["id"] == "1748"

    def test_a_reload_warning_reaches_the_model(self, monkeypatch, no_http):
        """It has to say so, or the model reports a change the home has not
        picked up yet as done."""
        monkeypatch.setattr(automations, "create_automation",
                            lambda aid, cfg: (True, "reload failed — HA is busy"))

        out = json.loads(automations.tool_ha_create_automation()._run(json.dumps(VALID)))

        assert out["created"] is True
        assert "reload failed" in out["warning"]

    def test_a_failed_write_is_an_error_the_model_can_see(self, monkeypatch, no_http):
        monkeypatch.setattr(automations, "update_automation",
                            lambda aid, cfg: (False, "HTTP 404"))

        out = json.loads(automations.tool_ha_update_automation()._run("1748", json.dumps(VALID)))

        assert out["error"] == "Update failed: HTTP 404"
