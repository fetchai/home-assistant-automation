"""The delivery layer: which channel a message goes down, and what happens when
it cannot.

The producers are not allowed to know a channel exists (design §4), so
everything about routing is decided here — which makes this the file where the
one agent-to-user path in the whole product is either correct or silently
broken.

The Home Assistant call is monkeypatched at _ha_post, the same seam the rest of
the suite draws. What is being tested is the decision to call it, the shape of
the call, and above all what happens when it fails: §11 promises "outbox holds;
nothing is lost", which is only true if a failed push leaves the row exactly as
it found it.
"""
import pytest

from core import activity
from core import delivery
from core import settings


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    activity.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    monkeypatch.setattr(settings, "NOTIFY_SERVICE", "")
    yield
    activity.close()


@pytest.fixture
def sent(monkeypatch):
    """Captures the service calls the delivery layer makes, accepting them all."""
    calls = []

    def _post(path, body):
        calls.append((path, body))
        return True, {}

    monkeypatch.setattr(delivery, "_ha_post", _post)
    return calls


def _refusing(monkeypatch):
    monkeypatch.setattr(delivery, "_ha_post", lambda path, body: (False, "HA is restarting"))


def _urgent(title="A device disappeared", body="light.hall is gone."):
    return activity.queue("entity_gone", title, body, urgency="urgent")


class TestWhatGetsPushed:
    def test_an_urgent_message_is_pushed(self, sent):
        _urgent()

        assert delivery.push_urgent() == 1
        assert len(sent) == 1

    def test_an_ordinary_message_waits_to_be_collected(self, sent):
        """The push channel is the only way to reach someone who is not already
        talking to the agent. A digest sent this way teaches them to ignore it."""
        activity.queue("change_applied", "Done", "…")

        assert delivery.push_urgent() == 0
        assert sent == []

    def test_a_pushed_message_is_not_pushed_again(self, sent):
        _urgent()

        delivery.push_urgent()
        delivery.push_urgent()

        assert len(sent) == 1

    def test_a_backlog_is_not_delivered_all_at_once(self, sent, monkeypatch):
        monkeypatch.setattr(delivery, "PUSH_BATCH", 2)
        for _ in range(5):
            _urgent()

        assert delivery.push_urgent() == 2

    def test_an_empty_outbox_costs_no_call(self, sent):
        assert delivery.push_urgent() == 0
        assert sent == []


class TestWhereItGoes:
    def test_it_defaults_to_a_persistent_notification(self, sent):
        """No configuration, present on every install, visible in the sidebar."""
        _urgent()

        delivery.push_urgent()

        path, body = sent[0]
        assert path == "/services/persistent_notification/create"
        assert body["title"] == "A device disappeared"
        assert body["message"] == "light.hall is gone."
        # Addressable, so a later version can replace one rather than leaving a
        # pile of them behind.
        assert body["notification_id"].startswith("fetchai_agent_")

    def test_a_configured_service_takes_over(self, sent, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_SERVICE", "notify.mobile_app_pixel")
        _urgent()

        delivery.push_urgent()

        assert sent[0][0] == "/services/notify/mobile_app_pixel"

    def test_a_service_written_without_its_domain_still_works(self, sent, monkeypatch):
        """"notify.mobile_app_x" and "mobile_app_x" are the same thing to
        everyone except an API call."""
        monkeypatch.setattr(settings, "NOTIFY_SERVICE", "mobile_app_pixel")
        _urgent()

        delivery.push_urgent()

        assert sent[0][0] == "/services/notify/mobile_app_pixel"

    def test_a_notify_target_gets_no_notification_id(self, sent, monkeypatch):
        """It is a persistent_notification field; notify services reject extras."""
        monkeypatch.setattr(settings, "NOTIFY_SERVICE", "notify.mobile_app_pixel")
        _urgent()

        delivery.push_urgent()

        assert "notification_id" not in sent[0][1]


class TestWhenItCannotBeDelivered:
    def test_a_refused_push_leaves_the_message_pending(self, monkeypatch):
        """Home Assistant restarting, or a notify target that no longer exists,
        must delay a message rather than consume it."""
        _urgent()
        _refusing(monkeypatch)

        assert delivery.push_urgent() == 0
        assert len(activity.pending("push")) == 1

    def test_it_arrives_once_the_target_works_again(self, monkeypatch, sent):
        _urgent()
        _refusing(monkeypatch)
        delivery.push_urgent()

        monkeypatch.setattr(delivery, "_ha_post", lambda path, body: (True, {}))
        assert delivery.push_urgent() == 1

    def test_a_failure_stops_the_batch_rather_than_burning_through_it(self, monkeypatch):
        for _ in range(3):
            _urgent()
        _refusing(monkeypatch)

        delivery.push_urgent()

        assert len(activity.pending("push")) == 3


class TestTheChatChannel:
    def test_collecting_hands_over_everything_waiting(self):
        activity.queue("change_applied", "Done", "…")
        activity.queue("entity_gone", "Gone", "…", urgency="urgent")

        assert len(delivery.collect_for_chat()) == 2

    def test_what_was_collected_does_not_come_back(self):
        """A message counted as delivered but never shown is the one failure
        mode a pull channel has — so it is marked here, by the code that hands
        it over, not by the caller."""
        activity.queue("change_applied", "Done", "…")

        delivery.collect_for_chat()

        assert delivery.collect_for_chat() == []

    def test_collecting_in_chat_does_not_mark_it_seen_in_the_panel(self):
        activity.queue("change_applied", "Done", "…")

        delivery.collect_for_chat()

        assert len(activity.pending("panel")) == 1

    def test_an_empty_outbox_collects_nothing(self):
        assert delivery.collect_for_chat() == []
