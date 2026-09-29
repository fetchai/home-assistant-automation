"""Analyst rows in the suggestion store — proposal §5, WP-B1.

A second source of suggestion shares the table, the key discipline and the
state machine with the detectors, and differs only in what proves `validated`.
Three properties carry the weight here. A store created before the columns
existed is migrated in place and its rows read as what they always were —
detector rows, replayed. An analyst row cannot be given a label its own
evidence does not back: it reaches `validated` only through `mark_shown`,
with a chart. And the entries to `validated` cannot be crossed, because a
replay on an analyst row or a chart on a detector row is exactly that
unbacked label by another route. (`mark_audited`, the retired audited run's
entry, went with it — free-hand F-9, 2.23.0.)
"""
import sqlite3

import pytest

from core import activity, suggestions


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    suggestions.close()
    monkeypatch.setattr(activity, "DB_PATH", str(tmp_path / "activity.db"))
    yield
    suggestions.close()


KEY = "analyst:3f9a1c"
CONFIG = {"alias": "Hall light off at midnight",
          "triggers": [{"trigger": "time", "at": "00:00:00"}],
          "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.hall"}}]}


def _analyst(key=KEY, **kwargs):
    defaults = {"entities": ["light.hall"], "title": "Turn the hall light off at midnight",
                "rationale": "It is left on most nights.", "model": "asi1-mini"}
    return suggestions.upsert_analyst_candidate(key, **{**defaults, **kwargs})


# The table exactly as 2.15.0 created it — the store a live install upgrades.
_PRE_B = (
    "CREATE TABLE suggestions (id INTEGER PRIMARY KEY, key TEXT NOT NULL,"
    " kind TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,"
    " state TEXT NOT NULL DEFAULT 'candidate', entities TEXT, evidence TEXT,"
    " estimate TEXT, replay TEXT, config TEXT, change_id INTEGER, baseline TEXT,"
    " verify_after INTEGER, verdict TEXT, dismissed_until INTEGER, retired_reason TEXT)"
)


class TestMigration:
    def test_a_pre_b_store_gains_the_columns_and_its_rows_read_as_detector_rows(self):
        conn = sqlite3.connect(activity.DB_PATH)
        conn.execute(_PRE_B)
        conn.execute("INSERT INTO suggestions (id, key, kind, created_at, updated_at, state,"
                     " replay) VALUES (1, 'standby:sensor.tv_power', 'standby', 1, 1,"
                     " 'validated', '{\"firings\": 21}')")
        conn.commit()
        conn.close()

        row = suggestions.suggestion(1)

        assert row["source"] == "detector"
        assert row["evidence_level"] == "replayed"
        assert row["model"] is None and row["title"] is None
        assert row["rationale"] is None and row["claims"] is None
        assert row["replay"] == {"firings": 21}

    def test_migration_runs_once_and_leaves_a_new_store_alone(self):
        suggestions.upsert_candidate("standby:sensor.tv_power", "standby")
        suggestions.close()

        suggestion_id = suggestions.upsert_candidate("standby:sensor.fridge", "standby")

        conn = sqlite3.connect(activity.DB_PATH)
        names = [r[1] for r in conn.execute("PRAGMA table_info(suggestions)")]
        conn.close()
        assert names.count("source") == 1 and names.count("claims") == 1
        assert suggestions.suggestion(suggestion_id)["source"] == "detector"

    def test_a_new_detector_row_defaults_to_detector_and_replayed(self):
        row = suggestions.suggestion(suggestions.upsert_candidate("standby:sensor.tv_power",
                                                                  "standby"))

        assert (row["source"], row["evidence_level"], row["model"]) == \
            ("detector", "replayed", None)


class TestUpsertAnalystCandidate:
    def test_it_writes_an_untested_analyst_row_at_candidate(self):
        row = suggestions.suggestion(_analyst())

        assert row["state"] == "candidate"
        assert (row["source"], row["kind"], row["evidence_level"]) == \
            ("analyst", "analyst", "untested")
        assert row["model"] == "asi1-mini"
        assert row["title"] == "Turn the hall light off at midnight"
        assert row["entities"] == ["light.hall"]

    @pytest.mark.parametrize("key", ["", "standby:sensor.tv", "analyst:", "analyst"])
    def test_a_key_that_is_not_analyst_digest_is_refused(self, key):
        assert suggestions.upsert_analyst_candidate(key, title="x") is None

    def test_a_row_with_no_title_is_refused(self):
        assert _analyst(title="   ") is None

    def test_the_title_is_one_line_of_at_most_eighty_characters(self):
        row = suggestions.suggestion(_analyst(title="Turn\nthe hall " + "x" * 200))

        assert "\n" not in row["title"]
        assert len(row["title"]) == suggestions.TITLE_MAX_CHARS
        assert row["title"].endswith("…")

    def test_the_same_words_twice_are_one_suggestion_and_keep_what_it_has(self):
        first = _analyst()
        _show(first)

        assert _analyst() == first
        row = suggestions.suggestion(first)
        assert (row["state"], row["evidence_level"]) == ("validated", "shown")

    def test_new_words_drop_a_validated_row_back_and_clear_its_evidence(self):
        first = _analyst()
        _show(first)

        assert _analyst(rationale="Something else.") == first
        row = suggestions.suggestion(first)
        assert (row["state"], row["evidence_level"]) == ("candidate", "untested")
        assert row["config"] is None and row["chart"] is None and row["finding"] is None

    def test_a_proposed_row_is_not_rewritten(self):
        first = _analyst()
        _show(first)
        suggestions.mark_proposed(first, change_id=5)

        _analyst(title="Something else entirely")

        row = suggestions.suggestion(first)
        assert row["state"] == "proposed"
        assert row["title"] == "Turn the hall light off at midnight"

    def test_a_dismissal_holds_the_same_way_it_does_for_a_detector_row(self):
        first = _analyst()
        _show(first)
        suggestions.mark_proposed(first, change_id=5)
        suggestions.mark_dismissed(first, cooldown_days=30)

        _analyst(rationale="Asking again.")

        assert suggestions.suggestion(first)["state"] == "dismissed"


class TestTheStateMachineIsTheSame:
    def test_an_analyst_row_walks_the_same_lifecycle_to_kept(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        assert suggestions.mark_proposed(suggestion_id, change_id=9)
        assert suggestions.mark_applied(suggestion_id, baseline={"outcome": "not_measured"},
                                        verify_after=1000)
        assert suggestions.record_verdict(suggestion_id, {"outcome": "not_measured"})
        assert suggestions.record_verdict(suggestion_id, {"outcome": "not_measured"},
                                          state="kept")

        assert suggestions.suggestion(suggestion_id)["state"] == "kept"

    def test_an_analyst_row_cannot_skip_a_stage(self):
        suggestion_id = _analyst()

        assert not suggestions.mark_proposed(suggestion_id, change_id=9)
        assert not suggestions.mark_applied(suggestion_id, verify_after=1000)

    def test_an_analyst_row_is_listed_with_the_others(self):
        detector_id = suggestions.upsert_candidate("standby:sensor.tv_power", "standby")
        analyst_id = _analyst()

        assert {r["id"] for r in suggestions.active()} == {detector_id, analyst_id}
        assert suggestions.stats()["candidate"] == 2


# --------------------------------------------------------------------------
# mark_shown — the analyst's entry to `validated` (free-hand F-9, F-10, F-WP3)
# --------------------------------------------------------------------------
CHART = {"spec": {"type": "timeline", "entities": ["light.hall"], "state": None,
                  "start": 1_700_000_000, "end": 1_701_000_000},
         "series": None, "markers": None, "error": None}
SENT = [{"tool": "Query the behavioural record", "query": {"sql": "SELECT 1"},
         "answer_excerpt": "{}", "rows": 1, "truncated": False}]


def _show(suggestion_id, **kwargs):
    defaults = {"config": CONFIG, "finding": "The hall light is left on after midnight.",
                "rule_text": "At midnight, turn the hall light off.", "chart": CHART,
                "sent": SENT}
    return suggestions.mark_shown(suggestion_id, **{**defaults, **kwargs})


class TestMarkShown:
    def test_a_pre_f_store_gains_the_explorer_columns(self):
        conn = sqlite3.connect(activity.DB_PATH)
        conn.execute(_PRE_B)
        conn.commit()
        conn.close()

        suggestions.upsert_candidate("standby:sensor.tv_power", "standby")

        conn = sqlite3.connect(activity.DB_PATH)
        names = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
        conn.close()
        assert {"finding", "rule_text", "chart", "sent"} <= names

    def test_it_moves_candidate_to_validated_as_shown_with_everything_stored(self):
        suggestion_id = _analyst()

        assert _show(suggestion_id)

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "validated"
        assert row["evidence_level"] == suggestions.EVIDENCE_SHOWN == "shown"
        assert row["config"] == CONFIG
        assert row["finding"] == "The hall light is left on after midnight."
        assert row["rule_text"] == "At midnight, turn the hall light off."
        assert row["chart"] == CHART and row["sent"] == SENT
        assert row["claims"] is None and row["replay"] is None and row["estimate"] is None

    def test_shown_is_not_a_rung_of_the_three_labels(self):
        assert suggestions.EVIDENCE_SHOWN not in suggestions.EVIDENCE_LEVELS

    @pytest.mark.parametrize("chart", [None, {}, {"series": None}, "timeline"])
    def test_a_row_without_a_chart_request_is_refused(self, chart):
        suggestion_id = _analyst()
        assert not _show(suggestion_id, chart=chart)
        assert suggestions.suggestion(suggestion_id)["state"] == "candidate"

    def test_a_chart_the_record_could_not_draw_is_still_a_chart_request(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id, chart={"spec": {"type": "pie"}, "series": None,
                                           "markers": None, "error": "not a chart type"})

    @pytest.mark.parametrize("field", ["finding", "rule_text"])
    def test_the_agents_words_are_required(self, field):
        assert not _show(_analyst(), **{field: "  "})

    def test_no_config_is_refused(self):
        assert not _show(_analyst(), config={})

    def test_nothing_sent_stores_null(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id, sent=[])
        assert suggestions.suggestion(suggestion_id)["sent"] is None

    def test_a_detector_row_cannot_be_shown(self):
        suggestion_id = suggestions.upsert_candidate("standby:sensor.tv_power", "standby")
        assert not _show(suggestion_id)

    def test_a_validated_row_cannot_be_shown_twice(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        assert not _show(suggestion_id)

    def test_new_words_clear_what_was_shown(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)

        assert _analyst(rationale="Something else entirely.") == suggestion_id

        row = suggestions.suggestion(suggestion_id)
        assert row["state"] == "candidate"
        assert row["evidence_level"] == "untested"
        assert (row["finding"], row["rule_text"], row["chart"], row["sent"]) == \
            (None, None, None, None)


# set_feedback — the household's answer to a shown row (free-hand F-11, F-WP5)
# --------------------------------------------------------------------------
class TestSetFeedback:
    def test_a_pre_f_store_gains_the_feedback_column(self):
        conn = sqlite3.connect(activity.DB_PATH)
        conn.execute(_PRE_B)
        conn.commit()
        conn.close()

        suggestions.upsert_candidate("standby:sensor.tv_power", "standby")

        conn = sqlite3.connect(activity.DB_PATH)
        names = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
        conn.close()
        assert "feedback" in names

    def test_it_stores_answer_reason_and_when(self, monkeypatch):
        monkeypatch.setattr(suggestions, "_now", lambda: 1_790_000_000)
        suggestion_id = _analyst()
        assert _show(suggestion_id)

        stored = suggestions.set_feedback(suggestion_id, "No", "  the dog\nsleeps there ")

        assert stored == {"answer": "no", "reason": "the dog sleeps there", "at": 1_790_000_000,
                          "change_id": None}
        assert suggestions.suggestion(suggestion_id)["feedback"] == stored

    def test_it_moves_nothing(self):
        """It records an answer; the change is decided by /rest/approve. Not
        a transition, and `updated_at` — "proposed today" — is untouched."""
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        before = suggestions.suggestion(suggestion_id)

        assert suggestions.set_feedback(suggestion_id, "yes")

        after = suggestions.suggestion(suggestion_id)
        assert (after["state"], after["updated_at"]) == (before["state"], before["updated_at"])
        assert {k: v for k, v in after.items() if k != "feedback"} == \
            {k: v for k, v in before.items() if k != "feedback"}

    def test_it_keeps_the_change_it_answered(self):
        """F-WP6: a row raised again is proposed through a new change (the
        reword reset clears change_id first); the answer names its own, so
        the prompt block checks it against the journal row it answered."""
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        assert suggestions.mark_proposed(suggestion_id, change_id=41)
        assert suggestions.set_feedback(suggestion_id, "no")["change_id"] == 41

    def test_the_answered_ideas_are_read_without_their_charts(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        assert suggestions.answered_ideas() == []          # nothing answered, no change yet
        suggestions.set_feedback(suggestion_id, "no", "not now")
        (row,) = suggestions.answered_ideas()
        assert set(row) == {"id", "title", "feedback", "change_id"}
        assert row["feedback"]["reason"] == "not now"

    def test_a_second_answer_replaces_the_first(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        suggestions.set_feedback(suggestion_id, "yes")
        suggestions.set_feedback(suggestion_id, "no", "changed my mind")
        assert suggestions.suggestion(suggestion_id)["feedback"]["answer"] == "no"

    def test_a_long_reason_is_cut(self):
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        stored = suggestions.set_feedback(suggestion_id, "no", "x" * 2000)
        assert len(stored["reason"]) == suggestions.FEEDBACK_REASON_MAX_CHARS

    @pytest.mark.parametrize("answer", ["", "maybe", "rolled_back", None])
    def test_only_yes_or_no(self, answer):
        """`rolled_back` is F-WP6's, written by the rollback path — never
        something the panel may claim."""
        suggestion_id = _analyst()
        assert _show(suggestion_id)
        assert suggestions.set_feedback(suggestion_id, answer) is None
        assert suggestions.suggestion(suggestion_id)["feedback"] is None

    def test_only_a_shown_row_takes_one(self):
        detector = suggestions.upsert_candidate("standby:sensor.tv_power", "standby")
        unshown = _analyst(key="analyst:other")  # still at candidate, never shown

        assert suggestions.set_feedback(detector, "no") is None
        assert suggestions.set_feedback(unshown, "no") is None
        assert suggestions.set_feedback(999, "no") is None
