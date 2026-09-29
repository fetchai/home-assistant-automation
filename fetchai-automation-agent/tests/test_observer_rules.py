"""The rules that turn Home Assistant's state_changed firehose into "one row
per meaningful transition".

The observation thread itself cannot be tested here — it holds a websocket.
Every decision it makes was put in core/observer.py precisely so it could be,
and this file is that coverage. A regression in should_record does not crash
anything; it quietly makes the record either useless (everything filtered out)
or enormous (nothing filtered out), and neither shows up anywhere else.
"""
from core import observer


def _state(value, last_changed="2026-08-24T09:00:00+00:00"):
    return {"state": value, "last_changed": last_changed, "attributes": {}}


def _event(entity_id="light.hall", new="on", old="off",
           last_changed="2026-08-24T09:00:00+00:00"):
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": last_changed,
            "data": {
                "entity_id": entity_id,
                "new_state": _state(new, last_changed) if new is not None else None,
                "old_state": _state(old) if old is not None else None,
            },
        },
    }


class TestParseStateChanged:
    def test_it_reads_the_entity_state_and_time_out_of_an_event(self):
        parsed = observer.parse_state_changed(_event())

        assert parsed["entity_id"] == "light.hall"
        assert parsed["state"] == "on"
        assert parsed["prev_state"] == "off"
        assert parsed["ts"] == 1787562000

    def test_it_accepts_the_bare_event_as_well_as_the_envelope(self):
        envelope = _event()
        assert observer.parse_state_changed(envelope["event"]) == \
               observer.parse_state_changed(envelope)

    def test_an_entity_being_removed_yields_nothing_to_record(self):
        # HA sends new_state: null when an entity is deleted from the registry.
        # There is no transition there, only an absence.
        assert observer.parse_state_changed(_event(new=None)) is None

    def test_an_entity_appearing_for_the_first_time_is_a_transition(self):
        parsed = observer.parse_state_changed(_event(old=None))

        assert parsed["state"] == "on"
        assert parsed["prev_state"] is None

    def test_other_event_types_are_ignored(self):
        event = _event()
        event["event"]["event_type"] = "call_service"
        assert observer.parse_state_changed(event) is None

    def test_malformed_messages_come_back_as_none_rather_than_raising(self):
        for message in (None, "", [], {}, {"event": None}, {"event": {"event_type": "state_changed"}}):
            assert observer.parse_state_changed(message) is None

    def test_it_falls_back_to_the_event_time_when_the_state_carries_none(self):
        event = _event()
        event["event"]["data"]["new_state"]["last_changed"] = None
        assert observer.parse_state_changed(event)["ts"] == 1787562000


class TestParseTimestamp:
    def test_it_reads_the_offset_and_zulu_forms_home_assistant_emits(self):
        assert observer.parse_timestamp("2026-08-24T09:00:00+00:00") == 1787562000
        assert observer.parse_timestamp("2026-08-24T09:00:00Z") == 1787562000
        assert observer.parse_timestamp("2026-08-24T09:00:00.123456+00:00") == 1787562000

    def test_a_local_offset_is_honoured_not_dropped(self):
        assert observer.parse_timestamp("2026-08-24T11:00:00+02:00") == 1787562000

    def test_nonsense_yields_none_so_the_caller_can_use_its_own_clock(self):
        for value in (None, "", "yesterday", 12345, {}):
            assert observer.parse_timestamp(value) is None


class TestShouldRecord:
    def test_the_first_thing_an_entity_ever_does_is_always_recorded(self):
        assert observer.should_record("on", 1000, None)

    def test_a_real_change_is_recorded(self):
        assert observer.should_record("on", 1000, {"state": "off", "ts": 900})

    def test_an_attribute_only_change_is_not(self):
        # A light reporting a new brightness at the same on/off state fires a
        # state_changed event. Storing it would fill the log with non-events.
        assert not observer.should_record("on", 1000, {"state": "on", "ts": 900})

    def test_flapping_within_the_debounce_window_is_collapsed(self):
        assert not observer.should_record("off", 901, {"state": "on", "ts": 900})

    def test_the_same_change_just_outside_the_window_is_kept(self):
        assert observer.should_record("off", 903, {"state": "on", "ts": 900})

    def test_an_empty_state_is_never_recorded(self):
        assert not observer.should_record("", 1000, None)
        assert not observer.should_record(None, 1000, None)

    def test_time_running_backwards_is_refused(self):
        # Two sources disagreeing about the clock would otherwise corrupt the
        # duration of the state being replaced.
        assert not observer.should_record("off", 800, {"state": "on", "ts": 900})

    def test_going_unavailable_is_a_transition_worth_keeping(self):
        assert observer.should_record("unavailable", 1000, {"state": "on", "ts": 900})

    def test_coming_back_from_unavailable_is_too(self):
        assert observer.should_record("21.5", 1000, {"state": "unavailable", "ts": 900})


class TestNumericSensorsAreThresholdCrossings:
    """The recorder holds every reading; the record must not. Without this,
    a couple of power sensors alone would dominate the whole log."""

    def test_a_reading_barely_moving_is_not_recorded(self):
        assert not observer.should_record("100.4", 1000, {"state": "100.0", "ts": 900})

    def test_a_reading_crossing_the_relative_threshold_is(self):
        assert observer.should_record("106.0", 1000, {"state": "100.0", "ts": 900})

    def test_small_values_use_the_absolute_floor_instead(self):
        # 5% of 0.2 is noise; without the floor a value hovering near zero
        # would cross the relative threshold on every jitter.
        assert not observer.should_record("0.21", 1000, {"state": "0.2", "ts": 900})
        assert observer.should_record("0.8", 1000, {"state": "0.2", "ts": 900})

    def test_a_slow_drift_is_still_sampled_periodically(self):
        # A freezer warming half a degree an hour never crosses the threshold,
        # and is exactly the anomaly the product is meant to catch.
        assert observer.should_record("100.1", 1000 + observer.NUMERIC_MAX_GAP_SECONDS,
                                      {"state": "100.0", "ts": 1000})

    def test_a_change_of_sign_is_a_crossing(self):
        assert observer.should_record("-5.0", 1000, {"state": "5.0", "ts": 900})

    def test_a_numeric_looking_state_next_to_a_word_is_not_treated_as_numeric(self):
        # "21.5" following "unavailable" is a plain transition, not a delta.
        assert observer.should_record("21.5", 1000, {"state": "unknown", "ts": 900})


class TestTheJitterFloor:
    """What stops an integration that reports every second from filling the
    disk. Without it there is no upper bound on the record at all: a power
    reading crosses the 5% threshold on noise alone, which is 86,400 rows a day
    per entity rather than the ~96 the heartbeat budgets for.

    The floor applies to jitter only. A real step keeps its own timestamp,
    because record.scan_relations pairs a change with whatever followed it
    inside two minutes — a crossing shifted even a minute late stops being
    evidence of what it caused, which would cost exactly the fine-grained
    automation proposals the record exists to make.
    """

    def test_jitter_inside_the_floor_is_not_recorded(self):
        # 100 -> 110 clears the 5% threshold, and a second later it is still
        # the sensor describing itself rather than the home doing anything.
        assert not observer.should_record("110.0", 1001, {"state": "100.0", "ts": 1000})

    def test_the_same_jitter_outside_the_floor_is(self):
        assert observer.should_record(
            "110.0", 1000 + observer.NUMERIC_MIN_INTERVAL_SECONDS, {"state": "100.0", "ts": 1000}
        )

    def test_a_real_step_does_not_wait(self):
        # A kettle switching on, one second after the last recorded reading.
        assert observer.should_record("3000.0", 1001, {"state": "100.0", "ts": 1000})

    def test_a_real_step_downwards_does_not_wait_either(self):
        assert observer.should_record("100.0", 1001, {"state": "3000.0", "ts": 1000})

    def test_a_step_is_recorded_at_the_second_it_happened(self):
        # The whole point of exempting steps: the row lands on the crossing,
        # not on the next slot the floor would have allowed.
        row = observer.transition_row(
            {"entity_id": "sensor.power", "state": "3000.0", "prev_state": "100.0", "ts": 1001},
            {"state": "100.0", "ts": 1000},
        )
        assert row[0] == 1001

    def test_a_value_near_zero_does_not_step_on_noise(self):
        # 25% of a value sitting at zero is zero, so the relative test alone
        # would call every flicker a step and the floor would never apply.
        assert not observer.should_record("0.05", 1001, {"state": "0.0", "ts": 1000})

    def test_the_heartbeat_still_wins_over_everything(self):
        assert observer.should_record("100.0", 1000 + observer.NUMERIC_MAX_GAP_SECONDS,
                                      {"state": "100.0", "ts": 1000})

    def test_the_floor_bounds_a_one_hertz_sensor(self):
        """The property the number is actually for: a reading every second,
        jittering by more than 5% every time, cannot write more than one row
        per floor interval unless it genuinely steps."""
        last = {"state": "100.0", "ts": 0}
        recorded = 0
        for second in range(1, 3601):
            # Alternating 100/106 clears the 5% threshold on every reading and
            # never approaches the 25% step.
            state = "106.0" if second % 2 else "100.0"
            if observer.should_record(state, second, last):
                recorded += 1
                last = {"state": state, "ts": second}
        assert recorded <= 3600 // observer.NUMERIC_MIN_INTERVAL_SECONDS + 1

    def test_non_numeric_entities_are_untouched_by_it(self):
        # A door is not a measurement, and its debounce is MIN_INTERVAL_SECONDS
        # — two seconds, not the jitter floor.
        assert observer.should_record("open", 1003, {"state": "closed", "ts": 1000})


class TestTransitionRow:
    def test_it_measures_how_long_the_previous_state_lasted(self):
        row = observer.transition_row(
            {"entity_id": "light.hall", "state": "off", "prev_state": "on", "ts": 1500},
            {"state": "on", "ts": 1000},
        )
        assert row == (1500, "off", "on", 500)

    def test_the_first_row_for_an_entity_has_no_duration_to_report(self):
        row = observer.transition_row(
            {"entity_id": "light.hall", "state": "on", "prev_state": None, "ts": 1000}, None
        )
        assert row == (1000, "on", None, None)

    def test_the_previous_state_comes_from_the_record_not_the_event(self):
        # The event's old_state may describe a change that was itself filtered
        # out, which would make the stored chain disagree with itself.
        row = observer.transition_row(
            {"entity_id": "sensor.x", "state": "30", "prev_state": "25", "ts": 2000},
            {"state": "10", "ts": 1000},
        )
        assert row == (2000, "30", "10", 1000)
