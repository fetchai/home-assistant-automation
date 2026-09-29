"""The suggestion lifecycle store, against real SQLite.

core/suggestions.py is stdlib sqlite3 on top of core/activity.py's connection,
so nothing here is stubbed and nothing needs to be — these are the actual
queries the review pass, the replay engine and the verification schedule will
issue.

Two properties carry the weight. A review that runs twice must produce one
suggestion, because the same key is re-derived from the same waste every day
and a home that is told about it twice stops reading. And a state must never be
reached out of order, because `applied` is the claim that something was written
into someone's home and `verified` the claim that it was measured — both are
evidence, and evidence that code can set by accident is not evidence.
"""
import pytest

from core import activity, suggestions


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A private database per test. The store keeps a thread-local connection —
    activity.py's, since this is a third table in activity.db and not a fourth
    file — so pointing DB_PATH somewhere new is not enough on its own."""
    suggestions.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    suggestions.close()


KEY = "standby:sensor.tv_power"
DAY = 86400


def _candidate(key=KEY, **kwargs):
    defaults = {"kind": "standby", "entities": ["sensor.tv_power"],
                "evidence": {"nights": 21, "watts": 14.2}}
    return suggestions.upsert_candidate(key, **{**defaults, **kwargs})


def _validated(**kwargs):
    suggestion_id = _candidate(**kwargs)
    suggestions.mark_validated(suggestion_id, replay={"firings": 21},
                               estimate={"kwh_year": 124}, config={"alias": "TV standby"})
    return suggestion_id


def _applied(verify_after=1000, **kwargs):
    suggestion_id = _validated(**kwargs)
    suggestions.mark_proposed(suggestion_id, change_id=77)
    suggestions.mark_applied(suggestion_id, baseline={"watts": 14.2},
                             verify_after=verify_after)
    return suggestion_id


class TestItLivesInActivityDb:
    def test_the_suggestion_and_the_message_announcing_it_share_one_file(self):
        """Not a preference about filenames. Proposing writes a suggestion row
        AND queues the outbox row that announces it, and two files cannot be
        one transaction."""
        suggestion_id = _candidate()
        message_id = activity.queue("digest", "One suggestion", "…")

        assert suggestions.suggestion(suggestion_id)["key"] == KEY
        assert activity.messages()[0]["id"] == message_id

    def test_it_does_not_disturb_the_journal_it_borrows(self):
        _candidate()

        change_id = activity.record_proposal(tool="Create Home Assistant automation",
                                             args={}, session_id="session_abc")

        assert activity.change(change_id)["decision"] == "proposed"


class TestDedupeByKey:
    def test_a_review_running_twice_produces_one_suggestion(self):
        """The single most important behaviour here. The daily pass re-derives
        the same key from the same waste, every day, forever."""
        first = _candidate()
        second = _candidate()

        assert second == first
        assert len(suggestions.active()) == 1

    def test_two_different_wastes_are_two_suggestions(self):
        first = _candidate()
        second = _candidate(key="standby:sensor.printer_power")

        assert second != first
        assert len(suggestions.active()) == 2

    def test_open_for_finds_the_live_row_for_a_key(self):
        suggestion_id = _candidate()

        assert suggestions.open_for(KEY)["id"] == suggestion_id

    def test_a_repeat_does_not_disturb_a_validated_replay(self):
        """Same evidence, same conclusion. Clearing the replay every morning
        would mean the suggestion was revalidated daily and never proposed."""
        suggestion_id = _validated()

        _candidate()

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "validated"
        assert row["replay"] == {"firings": 21}

    def test_evidence_that_moved_sends_it_back_to_be_revalidated(self):
        """A replay validated against figures that have changed is the weak
        number §7.1 refuses to show."""
        suggestion_id = _validated()

        _candidate(evidence={"nights": 22, "watts": 9.1})

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "candidate"
        assert row["replay"] is None
        assert row["evidence"]["watts"] == 9.1

    def test_the_evidence_under_a_proposal_is_never_rewritten(self):
        """It travels with the suggestion (§9) — the user is reading it."""
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)

        _candidate(evidence={"nights": 40, "watts": 1.0})

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "proposed"
        assert row["evidence"]["nights"] == 21

    def test_a_retired_key_does_not_block_the_same_waste_being_found_again(self):
        """The waste came back, or the entity did. The old row keeps its key as
        part of the audit trail; it does not get a veto over the future."""
        old = _candidate()
        suggestions.retire(old, "the entity disappeared")

        new = _candidate()

        assert new != old
        assert suggestions.suggestion(old)["state"] == "retired"

    def test_the_database_enforces_it_not_just_the_function(self):
        """Dedupe is a property of the data, so that it survives a caller that
        goes around upsert_candidate()."""
        import sqlite3

        _candidate()
        conn = activity._connect()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO suggestions (key, kind, created_at, updated_at, state)"
                " VALUES (?, 'standby', 1, 1, 'candidate')", (KEY,))

    def test_a_candidate_without_a_key_is_refused_rather_than_stored(self):
        assert suggestions.upsert_candidate("", "standby") is None


class TestTheStagesInOrder:
    def test_a_candidate_starts_as_one(self):
        row = suggestions.suggestion(_candidate())

        assert row["state"] == "candidate"
        assert (row["created_at"], row["updated_at"]) > (0, 0)

    def test_validation_stores_the_replay_the_estimate_and_the_config(self):
        """One fact, not three: this config, replayed, gave this estimate."""
        suggestion_id = _candidate()

        assert suggestions.mark_validated(suggestion_id, replay={"firings": 21},
                                          estimate={"kwh_year": 124},
                                          config={"alias": "TV standby"}) is True

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "validated"
        assert row["estimate"] == {"kwh_year": 124}
        assert row["config"] == {"alias": "TV standby"}

    def test_proposing_points_at_the_change_the_approval_lives_on(self):
        """One recorded approval per write — the gate's — not a second copy
        here that could disagree with it."""
        suggestion_id = _validated()

        suggestions.mark_proposed(suggestion_id, change_id=77)

        assert suggestions.suggestion(suggestion_id)["change_id"] == 77

    def test_the_pointer_reads_back_from_the_change(self):
        """How an approval finds its suggestion again: the gate hands back a
        change_id and nothing else, and on the day it is answered that is the
        only name the suggestion has."""
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)

        assert suggestions.for_change(77)["id"] == suggestion_id
        # Most of the journal is conversation, and no suggestion is behind it.
        assert suggestions.for_change(78) is None
        assert suggestions.for_change(None) is None

    def test_applying_captures_the_before(self):
        """From this moment the record is being changed by the automation that
        was just written, so a baseline computed later reads the after."""
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)

        suggestions.mark_applied(suggestion_id, baseline={"watts": 14.2},
                                 verify_after=9_999)

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "applied"
        assert row["baseline"] == {"watts": 14.2}
        assert row["verify_after"] == 9_999

    def test_a_verdict_can_be_reported_before_it_is_answered(self):
        """§7.2 makes the report and the decision two moments: the agent states
        a number, and always offers the rollback whatever it says."""
        suggestion_id = _applied()

        suggestions.record_verdict(suggestion_id, {"measured_kwh": 96}, "verified")

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "verified"
        assert row["verdict"] == {"measured_kwh": 96}

    def test_the_user_keeps_it_or_rolls_it_back(self):
        kept = _applied()
        rolled = _applied(key="standby:sensor.printer_power")
        suggestions.record_verdict(kept, {"measured_kwh": 96}, "verified")
        suggestions.record_verdict(rolled, {"measured_kwh": 0}, "verified")

        assert suggestions.record_verdict(kept, {"answer": "keep"}, "kept") is True
        assert suggestions.record_verdict(rolled, {"answer": "undo"}, "rolled_back") is True

        assert suggestions.suggestion(kept)["state"] == "kept"
        assert suggestions.suggestion(rolled)["state"] == "rolled_back"

    def test_retirement_is_reachable_from_anywhere_and_says_why(self):
        """§11's "referenced entity disappears", at whatever stage it had
        reached."""
        for suggestion_id in (_candidate(), _validated(key="k2"), _applied(key="k3")):
            assert suggestions.retire(suggestion_id, "sensor.tv_power is gone") is True
            row = suggestions.suggestion(suggestion_id)
            assert row["state"] == "retired"
            assert "gone" in row["retired_reason"]


class TestIllegalTransitions:
    def test_a_candidate_cannot_be_applied_without_being_validated(self):
        """`applied` is the claim that something was written into a home."""
        suggestion_id = _candidate()

        assert suggestions.mark_applied(suggestion_id, baseline={}, verify_after=1) is False
        assert suggestions.suggestion(suggestion_id)["state"] == "candidate"

    def test_a_candidate_cannot_be_proposed_unreplayed(self):
        """A candidate below the evidence threshold is discarded, not shown
        with a weak number (§7.1)."""
        suggestion_id = _candidate()

        assert suggestions.mark_proposed(suggestion_id, change_id=77) is False

    def test_a_verdict_cannot_be_recorded_for_something_never_applied(self):
        suggestion_id = _validated()

        assert suggestions.record_verdict(suggestion_id, {"measured_kwh": 96}) is False

    def test_a_finished_suggestion_cannot_be_reproposed(self):
        """The user has seen the measured result and answered. Proposing the
        same change again would be arguing with them."""
        suggestion_id = _applied()
        suggestions.record_verdict(suggestion_id, {"measured_kwh": 96}, "verified")
        suggestions.record_verdict(suggestion_id, {"answer": "keep"}, "kept")

        assert suggestions.mark_proposed(suggestion_id, change_id=99) is False
        assert suggestions.suggestion(suggestion_id)["state"] == "kept"

    def test_a_retired_suggestion_stays_retired(self):
        suggestion_id = _candidate()
        suggestions.retire(suggestion_id, "gone")

        assert suggestions.mark_validated(suggestion_id, replay={}, estimate={}) is False

    def test_a_refused_transition_writes_nothing_at_all(self):
        """Not "refused, but the columns went in anyway"."""
        suggestion_id = _candidate()

        suggestions.mark_applied(suggestion_id, baseline={"watts": 1}, verify_after=5)

        row = suggestions.suggestion(suggestion_id)
        assert row["baseline"] is None
        assert row["verify_after"] is None

    def test_an_unknown_verdict_state_is_refused(self):
        suggestion_id = _applied()

        assert suggestions.record_verdict(suggestion_id, {}, "state = 'kept'") is False
        assert suggestions.suggestion(suggestion_id)["state"] == "applied"

    def test_a_transition_on_nothing_is_not_an_error(self):
        """These are called with whatever the review pass carried. A missing id
        must not take the pass down with it."""
        assert suggestions.mark_validated(None) is False
        assert suggestions.retire(0, "x") is False
        assert suggestions.mark_proposed(9999, change_id=1) is False
        assert suggestions.suggestion(None) is None
        assert suggestions.open_for("") is None


class TestCooldown:
    def test_a_dismissal_hides_it_for_the_cooldown(self):
        """The waste is still there and the review finds it again tomorrow. A
        dismissal with no expiry would either silence it forever or reduce to
        asking again in the morning."""
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)

        suggestions.mark_dismissed(suggestion_id, cooldown_days=30)

        assert suggestions.active() == []
        assert suggestions.suggestion(suggestion_id)["state"] == "dismissed"

    def test_it_comes_back_once_the_cooldown_expires(self):
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)
        suggestions.mark_dismissed(suggestion_id, cooldown_days=30)

        later = suggestions.suggestion(suggestion_id)["dismissed_until"] + 1

        assert [r["id"] for r in suggestions.active(now=later)] == [suggestion_id]

    def test_the_boundary_instant_counts_as_expired(self):
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)
        suggestions.mark_dismissed(suggestion_id, cooldown_days=7)
        due = suggestions.suggestion(suggestion_id)["dismissed_until"]

        assert suggestions.active(now=due - 1) == []
        assert len(suggestions.active(now=due)) == 1

    def test_a_review_during_the_cooldown_changes_nothing(self):
        """Not "not now" answered by asking again the same evening."""
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)
        suggestions.mark_dismissed(suggestion_id, cooldown_days=30)

        assert _candidate(evidence={"nights": 30, "watts": 14.2}) == suggestion_id
        assert suggestions.suggestion(suggestion_id)["state"] == "dismissed"
        assert suggestions.active() == []

    def test_a_review_after_the_cooldown_reopens_it_as_a_fresh_candidate(self):
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)
        suggestions.mark_dismissed(suggestion_id, cooldown_days=0)

        assert _candidate() == suggestion_id

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "candidate"
        assert row["dismissed_until"] is None
        assert row["change_id"] is None
        assert row["replay"] is None

    def test_a_retired_suggestion_is_not_active_however_long_you_wait(self):
        """`retire` is where "never" lives; a cooldown is not."""
        suggestion_id = _candidate()
        suggestions.retire(suggestion_id, "the entity disappeared")

        assert suggestions.active(now=suggestions._now() + 3650 * DAY) == []


class TestDueForVerification:
    def test_it_finds_what_the_week_is_up_on(self):
        due = _applied(verify_after=1_000)
        later = _applied(key="k2", verify_after=5_000)

        assert [r["id"] for r in suggestions.due_for_verification(now=1_000)] == [due]
        assert [r["id"] for r in suggestions.due_for_verification(now=5_000)] == [due, later]

    def test_the_instant_itself_is_due(self):
        """verify_after is when the week is up, not when it starts to be up."""
        suggestion_id = _applied(verify_after=1_000)

        assert suggestions.due_for_verification(now=999) == []
        assert len(suggestions.due_for_verification(now=1_000)) == 1

    def test_oldest_first(self):
        later = _applied(key="k2", verify_after=5_000)
        due = _applied(key="k1", verify_after=1_000)

        assert [r["id"] for r in suggestions.due_for_verification(now=9_000)] == [due, later]

    def test_only_applied_suggestions_are_due(self):
        """One that was never written into the home has nothing to measure."""
        proposed = _validated()
        suggestions.mark_proposed(proposed, change_id=77)

        assert suggestions.due_for_verification(now=9_999_999) == []

    def test_a_verified_suggestion_stops_being_due(self):
        suggestion_id = _applied(verify_after=1_000)
        suggestions.record_verdict(suggestion_id, {"measured_kwh": 96}, "verified")

        assert suggestions.due_for_verification(now=9_000) == []

    def test_an_applied_suggestion_with_no_window_is_not_due(self):
        suggestion_id = _validated()
        suggestions.mark_proposed(suggestion_id, change_id=77)
        suggestions.mark_applied(suggestion_id, baseline={"watts": 1})

        assert suggestions.due_for_verification(now=9_999_999) == []


class TestDeferringAVerification:
    """core/verify.py decision 1: a week that could not be read may well be
    readable next week, so the attempt is recorded and the window moved — and
    the suggestion does not claim to have been measured in the meantime."""

    def test_it_moves_the_window_without_moving_the_state(self):
        suggestion_id = _applied(verify_after=1_000)

        assert suggestions.defer_verification(
            suggestion_id, 8_000, {"outcome": "unmeasured", "attempts": 1}) is True

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "applied"
        assert row["verify_after"] == 8_000
        assert row["verdict"]["attempts"] == 1

    def test_the_suggestion_stops_being_due_until_then(self):
        suggestion_id = _applied(verify_after=1_000)
        suggestions.defer_verification(suggestion_id, 8_000, {"attempts": 1})

        assert suggestions.due_for_verification(now=7_999) == []
        assert len(suggestions.due_for_verification(now=8_000)) == 1

    def test_the_count_survives_a_restart_because_it_is_on_the_row(self):
        """A counter kept in a process would be reset by every restart, and the
        report that eventually says "this could not be measured" has to be able
        to say how many times it tried."""
        suggestion_id = _applied(verify_after=1_000)
        suggestions.defer_verification(suggestion_id, 8_000, {"attempts": 1})
        suggestions.close()

        assert suggestions.suggestion(suggestion_id)["verdict"]["attempts"] == 1

    def test_only_an_applied_suggestion_can_be_deferred(self):
        """`verified` is the claim that a measurement was taken. Nothing here
        may reach it, and nothing here may move a row that already has."""
        verified = _applied(verify_after=1_000)
        suggestions.record_verdict(verified, {"outcome": "saved"}, "verified")

        assert suggestions.defer_verification(verified, 8_000, {"attempts": 2}) is False
        assert suggestions.suggestion(verified)["state"] == "verified"

    def test_and_a_suggestion_that_is_not_there_is_refused(self):
        assert suggestions.defer_verification(9_999, 8_000, {}) is False
        assert suggestions.defer_verification(None, 8_000, {}) is False


class TestJsonColumns:
    def test_the_columns_come_back_as_data_not_strings(self):
        suggestion_id = _applied()
        suggestions.record_verdict(suggestion_id, {"measured_kwh": 96, "range": [80, 110]})

        row = suggestions.suggestion(suggestion_id)
        assert row["entities"] == ["sensor.tv_power"]
        assert row["evidence"]["nights"] == 21
        assert row["verdict"]["range"] == [80, 110]

    def test_nothing_to_say_is_stored_as_nothing(self):
        """An empty dict and "not measured" must not read the same later."""
        suggestion_id = _candidate(entities=None, evidence={})

        row = suggestions.suggestion(suggestion_id)
        assert row["entities"] is None
        assert row["evidence"] is None


class TestItSurvivesARestart:
    def test_the_lifecycle_is_on_disk_and_not_in_the_process(self, tmp_path, monkeypatch):
        """A restart between two stages loses the process, not the suggestion —
        which is the whole reason none of this is held in the model's head."""
        suggestion_id = _applied(verify_after=1_000)

        suggestions.close()
        row = suggestions.suggestion(suggestion_id)

        assert row["state"] == "applied"
        assert row["baseline"] == {"watts": 14.2}
        assert [r["id"] for r in suggestions.due_for_verification(now=1_000)] == [suggestion_id]

    def test_dedupe_holds_across_a_restart(self):
        """The review that runs tomorrow is a different process from the one
        that ran today. That is the case dedupe exists for."""
        first = _candidate()

        suggestions.close()

        assert _candidate() == first
        assert len(suggestions.active()) == 1


class TestStats:
    def test_it_counts_one_per_state(self):
        _candidate()
        _validated(key="k2")
        applied = _applied(key="k3", verify_after=1)

        stats = suggestions.stats()

        assert stats["suggestions"] == 3
        assert stats["candidate"] == 1
        assert stats["validated"] == 1
        assert stats["applied"] == 1
        assert stats["due"] == 1
        assert applied

    def test_an_empty_store_reports_zeroes_rather_than_failing(self):
        assert suggestions.stats()["suggestions"] == 0
        assert suggestions.stats()["retired"] == 0
