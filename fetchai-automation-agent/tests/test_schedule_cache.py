"""D-4's cache — read, write, and the malformed-file cases a home's own
history can produce. core/schedule_cache.py is pure stdlib, so this runs
against the real module whether or not the heavy dependencies are installed.

Everything here drives the store directly with literal fingerprints, name
digests and windows; tests/test_schedule_detector.py::TestGridFingerprint owns
the fingerprint's own correctness and tests/test_scheduler_llm.py owns the name
digest's.
"""
import json

import pytest

from core import schedule_cache
from core import settings
from core import window


FP_A = "i" * 336
FP_B = "a" * 336
# Two name digests, in `scheduler_llm.name_print`'s shape. Literal rather than
# computed, the same way the fingerprints above are: what a digest is OF is
# tests/test_scheduler_llm.py's question, and this file only has to know that
# two different ones are two different questions.
NAME_A = "0123456789abcdef"
NAME_B = "fedcba9876543210"
PROMPT_V1 = 1
PROMPT_V2 = 2

OFFICE = window.spec("09:00", "17:00", window.WEEKDAYS)
EVENING = window.spec("18:00", "22:00", window.EVERY_DAY)

# Captured before any test's fixture monkeypatches STORE_PATH for isolation,
# so this is the path the module actually computed from settings.DATA_DIR at
# import time — what TestDataDirIsolation below is checking.
_ORIGINAL_STORE_PATH = schedule_cache.STORE_PATH


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    """Points the store at a temp file and clears the module-level cache, the
    same isolation tests/test_watchlist.py applies to its own store."""
    monkeypatch.setattr(schedule_cache, "STORE_PATH", str(tmp_path / "schedule_cache.json"))
    monkeypatch.setattr(schedule_cache, "_state", None)
    yield
    schedule_cache._state = None


class TestLookupAndStore:
    def test_an_entity_never_stored_is_a_miss(self):
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_a_stored_window_round_trips(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        result = schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1)

        assert result == OFFICE

    def test_a_cached_refusal_is_returned_as_none_not_as_a_miss(self):
        """The whole reason MISS is a distinct sentinel: `None` here means
        'the model was asked and declined', and a caller must not re-ask on
        it the way it would on an actual miss."""
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, None)

        result = schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1)

        assert result is None
        assert result is not schedule_cache.MISS

    def test_a_changed_fingerprint_is_a_miss(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        assert schedule_cache.lookup("switch.tv", FP_B, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_a_changed_name_is_a_miss(self):
        """The name is a second INPUT to the prompt and it moves the answer —
        the same grid named "Lounge TV Power" and "Server rack Power" came
        back with seven days and with five, reproducibly. An entry keyed on
        the grid alone would hand a renamed device the answer to its old
        name, and the fingerprint never moves for a rename."""
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_B, PROMPT_V1) is schedule_cache.MISS
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE

    def test_a_changed_prompt_version_invalidates_an_otherwise_matching_entry(self):
        """D-4's non-negotiable: the cached answer is only an answer to the
        prompt that produced it. A revised PROMPT_VERSION must not serve a
        stale answer just because the grid's shape did not move."""
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V2) is schedule_cache.MISS

    def test_storing_again_replaces_the_previous_answer(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)
        schedule_cache.store("switch.tv", FP_B, NAME_A, PROMPT_V1, EVENING)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS
        assert schedule_cache.lookup("switch.tv", FP_B, NAME_A, PROMPT_V1) == EVENING

    def test_entities_are_independent(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)
        schedule_cache.store("switch.kettle", FP_A, NAME_A, PROMPT_V1, None)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE
        assert schedule_cache.lookup("switch.kettle", FP_A, NAME_A, PROMPT_V1) is None

    def test_a_window_naming_specific_weekdays_round_trips_exactly(self):
        weekend = window.spec("10:00", "13:00", [window.SATURDAY, window.SUNDAY])
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, weekend)

        result = schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1)

        assert result.days == weekend.days


class TestPersistsAcrossReloads:
    def test_a_fresh_module_read_of_the_file_still_answers(self):
        """`_state` is only a cache of the file, not the source of truth —
        clearing it and reading again must reproduce the same answer, the
        way a process restart would."""
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)
        schedule_cache._state = None

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE


class TestCorruptFile:
    def test_a_missing_file_is_a_miss_not_an_error(self):
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_unparseable_json_yields_an_empty_cache_rather_than_raising(self):
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            f.write("{not json at all")

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_entities_not_being_a_dict_yields_an_empty_cache(self):
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entities": "not a dict"}, f)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_one_malformed_entry_is_dropped_but_the_rest_of_the_file_survives(self):
        """`window.spec` RAISES on a bad time — a hand-edited or truncated
        entry must not take the whole file's cache with it."""
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "entities": {
                    "switch.good": {
                        "fingerprint": FP_A,
                        "name_print": NAME_A,
                        "prompt_version": PROMPT_V1,
                        "decided_at": 0,
                        "window": {"start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]},
                    },
                    "switch.corrupt": {
                        "fingerprint": FP_A,
                        "name_print": NAME_A,
                        "prompt_version": PROMPT_V1,
                        "decided_at": 0,
                        "window": {"start": "25:99", "end": "17:00", "days": [0]},
                    },
                },
            }, f)

        assert schedule_cache.lookup("switch.good", FP_A, NAME_A, PROMPT_V1) == OFFICE
        assert schedule_cache.lookup("switch.corrupt", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_an_entry_written_before_the_name_joined_the_key_is_dropped(self):
        """Fail closed rather than defaulting: an entry with no `name_print`
        cannot say which name it was decided against, and guessing one would
        be the exact silent staleness the field exists to prevent."""
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "entities": {
                    "switch.tv": {
                        "fingerprint": FP_A,
                        "prompt_version": PROMPT_V1,
                        "decided_at": 0,
                        "window": {"start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]},
                    },
                },
            }, f)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_an_entry_missing_a_required_field_is_dropped(self):
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "entities": {"switch.tv": {"fingerprint": FP_A, "window": None}},
            }, f)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_an_entry_with_an_empty_days_list_is_dropped_not_raised(self):
        """`spec` refuses an empty weekday set; that refusal must land on
        this one entry, not escape `_load`."""
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "entities": {
                    "switch.tv": {
                        "fingerprint": FP_A,
                        "name_print": NAME_A,
                        "prompt_version": PROMPT_V1,
                        "decided_at": 0,
                        "window": {"start": "09:00", "end": "17:00", "days": []},
                    },
                },
            }, f)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_a_dropped_entry_does_not_corrupt_a_later_write(self):
        """Reading a corrupt entry and then storing a fresh one for the same
        entity must leave the store in a clean, readable state."""
        with open(schedule_cache.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "entities": {
                    "switch.tv": {
                        "fingerprint": FP_A,
                        "name_print": NAME_A,
                        "prompt_version": PROMPT_V1,
                        "decided_at": 0,
                        "window": {"start": "bad", "end": "17:00", "days": [0]},
                    },
                },
            }, f)

        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE


class TestPrune:
    def test_prune_drops_entities_not_in_keep(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)
        schedule_cache.store("switch.kettle", FP_A, NAME_A, PROMPT_V1, None)

        dropped = schedule_cache.prune(["switch.tv"])

        assert dropped == ["switch.kettle"]
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE
        assert schedule_cache.lookup("switch.kettle", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_prune_keeps_everything_named(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)
        schedule_cache.store("switch.kettle", FP_A, NAME_A, PROMPT_V1, None)

        dropped = schedule_cache.prune(["switch.tv", "switch.kettle"])

        assert dropped == []
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) == OFFICE
        assert schedule_cache.lookup("switch.kettle", FP_A, NAME_A, PROMPT_V1) is None

    def test_prune_with_an_empty_keep_set_drops_everything(self):
        schedule_cache.store("switch.tv", FP_A, NAME_A, PROMPT_V1, OFFICE)

        dropped = schedule_cache.prune([])

        assert dropped == ["switch.tv"]
        assert schedule_cache.lookup("switch.tv", FP_A, NAME_A, PROMPT_V1) is schedule_cache.MISS

    def test_prune_does_not_take_the_watchlist_as_an_argument(self):
        """D-4's own boundary: the caller already has the set of entities to
        keep, and this module must not reach for core/watchlist.py to get
        it. `prune` takes a plain iterable."""
        import inspect
        params = inspect.signature(schedule_cache.prune).parameters

        assert list(params) == ["keep"]


class TestDataDirIsolation:
    def test_the_store_path_is_built_from_settings_data_dir(self):
        """conftest.py points settings.DATA_DIR at a temp directory before
        any agent_home module is imported, so the module-level STORE_PATH
        captured above was already computed against that temp directory —
        this asserts schedule_cache used the setting rather than hardcoding
        `/data`, the live add-on volume."""
        assert _ORIGINAL_STORE_PATH.startswith(settings.DATA_DIR)
        assert not _ORIGINAL_STORE_PATH.startswith("/data")

    def test_a_test_run_never_writes_into_the_live_data_directory(self):
        """Belt and braces on top of the fixture's own monkeypatch: even the
        module's un-patched, import-time STORE_PATH must not point at /data,
        since a bug in the fixture would otherwise write through to it."""
        import os
        assert not os.path.exists("/data/schedule_cache.json")
