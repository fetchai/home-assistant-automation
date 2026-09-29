"""core/auditor.py after free-hand F-9: the house checks an analyst idea
passes before it is stored — every entity and service it names exists, no
sensitive domain (C-4, C-5), and it copies no existing automation (C-6). The
claim recount, its judge and its labels were retired with the audited run
(2.23.0); `tests/test_analyst_explorer.py` covers the run that calls these.
"""
import pytest

from core import auditor


STATES = {"light.hall": {}, "binary_sensor.motion": {}}
SERVICES = [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}}]

VALID_CFG = {
    "alias": "Hall light on motion",
    "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
    "action": [{"service": "light.turn_on", "target": {"entity_id": "light.hall"}}],
}


class TestCheckConfig:
    def test_a_valid_config_passes(self):
        assert auditor.check_config(VALID_CFG, STATES, SERVICES) is None

    @pytest.mark.parametrize("missing", ["alias", "trigger", "action"])
    def test_a_missing_required_key_is_refused(self, missing):
        cfg = {k: v for k, v in VALID_CFG.items() if k != missing}
        assert auditor.check_config(cfg, STATES, SERVICES)

    @pytest.mark.parametrize("key", ["trigger", "action"])
    def test_trigger_and_action_must_be_arrays(self, key):
        cfg = {**VALID_CFG, key: "not a list"}
        assert auditor.check_config(cfg, STATES, SERVICES)

    def test_a_missing_entity_is_refused(self):
        cfg = {**VALID_CFG, "action": [{"service": "light.turn_on",
                                        "target": {"entity_id": "light.nowhere"}}]}
        err = auditor.check_config(cfg, STATES, SERVICES)
        assert err and "light.nowhere" in err

    def test_an_unknown_service_is_refused(self):
        cfg = {**VALID_CFG, "action": [{"service": "light.explode"}]}
        err = auditor.check_config(cfg, STATES, SERVICES)
        assert err and "light.explode" in err

    @pytest.mark.parametrize("service", ["lock.unlock", "notify.mobile_app",
                                         "homeassistant.restart", "cover.open_cover"])
    def test_a_sensitive_service_is_refused(self, service):
        domain = service.split(".")[0]
        services = SERVICES + [{"domain": domain, "services": {service.split(".")[1]: {}}}]
        cfg = {**VALID_CFG, "action": [{"service": service}]}
        err = auditor.check_config(cfg, states=STATES, services=services)
        assert err and service in err


# ---------------------------------------------------------------------------
# C-6: duplicates.
# ---------------------------------------------------------------------------

class TestSignatureAndDuplicate:
    def test_the_same_trigger_and_action_shape_gives_the_same_signature(self):
        other = {**VALID_CFG, "alias": "A different name"}
        assert auditor.signature(VALID_CFG) == auditor.signature(other)

    def test_a_different_target_entity_changes_the_signature(self):
        other = {**VALID_CFG, "action": [{"service": "light.turn_on",
                                          "target": {"entity_id": "light.kitchen"}}]}
        assert auditor.signature(VALID_CFG) != auditor.signature(other)

    def test_duplicate_of_finds_the_matching_automations_alias(self):
        existing = {**VALID_CFG, "alias": "Existing one"}
        assert auditor.duplicate_of(VALID_CFG, [existing]) == "Existing one"

    def test_duplicate_of_is_none_when_nothing_matches(self):
        assert auditor.duplicate_of(VALID_CFG, []) is None
        assert auditor.duplicate_of(VALID_CFG, [{"alias": "Unrelated", "trigger": [],
                                                 "action": []}]) is None


# ---------------------------------------------------------------------------
# WP-C2: audit(), the I/O half.
# ---------------------------------------------------------------------------
