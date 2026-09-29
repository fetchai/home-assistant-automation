"""The sensitive-service predicate — one half of the approval gate.

is_sensitive_service decides whether calling a service pauses for confirmation.
Everything here describes current behaviour deliberately, including the rough
edges, so that changing any of it is a conscious act rather than a side effect.
"""
import pytest

from tools.tool_homeassistant.actions import (
    SENSITIVE_SERVICE_DOMAINS,
    is_sensitive_service,
)


def test_sensitive_domains_are_exactly_these():
    """A canary on the safety boundary. CLAUDE.md: 'Do not widen or narrow this
    without saying so explicitly.' If this fails, that is the conversation."""
    assert SENSITIVE_SERVICE_DOMAINS == {
        "alarm_control_panel",
        "cover",
        "hassio",
        "homeassistant",
        "lock",
        "notify",
        "shell_command",
        "valve",
    }


@pytest.mark.parametrize("service", [
    "lock.unlock",
    "alarm_control_panel.alarm_disarm",
    "cover.open_cover",
    "valve.open_valve",
    "notify.mobile_app_phone",
    "hassio.addon_restart",
    "homeassistant.restart",
    "shell_command.backup",
])
def test_sensitive_services_need_approval(service):
    assert is_sensitive_service(service) is True


@pytest.mark.parametrize("service", [
    "light.turn_on",
    "switch.toggle",
    "climate.set_temperature",
    "media_player.media_pause",
    "fan.turn_off",
    "automation.reload",
])
def test_everyday_services_run_immediately(service):
    """By design — asking twice for 'turn off the kitchen light' is worse than
    the mistake it would prevent."""
    assert is_sensitive_service(service) is False


@pytest.mark.parametrize("service", ["", None, ".turn_on", "notadomain.foo"])
def test_malformed_services_are_not_sensitive(service):
    assert is_sensitive_service(service) is False


def test_a_bare_domain_is_still_matched():
    """No dot at all: partition yields the whole string as the domain."""
    assert is_sensitive_service("lock") is True


def test_matching_is_case_sensitive():
    """Documented, not endorsed. HA service names are lower case in practice, and
    the tool resolves them from a listing rather than from user prose."""
    assert is_sensitive_service("LOCK.unlock") is False


def test_only_the_first_dot_separates_the_domain():
    assert is_sensitive_service("lock.unlock.extra") is True
