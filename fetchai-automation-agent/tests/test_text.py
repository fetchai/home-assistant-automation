"""Chat-text parsers in core.text.

Both of these decide something security-relevant from free text, and both follow
the same rule: match the WHOLE normalised message, never a substring. A parser
that matches loosely turns an incidental six-digit number into a pairing attempt.
"""
import pytest

from core.text import parse_pairing_code, strip_leading_mentions


class TestStripLeadingMentions:
    @pytest.mark.parametrize("raw,expected", [
        ("@agent1qxyz hello", "hello"),
        ("@a @b hello", "hello"),
        ("   @agent1qxyz hello", "hello"),
        ("@agent1qxyz    hello there", "hello there"),
    ])
    def test_leading_mentions_are_removed(self, raw, expected):
        assert strip_leading_mentions(raw) == expected

    @pytest.mark.parametrize("raw", [
        "hello @agent1qxyz",          # anchored to the start
        "email me at a@b.com",
        "@onlyamention",              # no trailing whitespace, so not a prefix
        "plain text",
    ])
    def test_other_text_is_untouched(self, raw):
        assert strip_leading_mentions(raw) == raw

    @pytest.mark.parametrize("falsy,expected", [("", ""), (None, "")])
    def test_falsy_input_yields_empty_string(self, falsy, expected):
        """parse_pairing_code relies on this to stay None-safe."""
        assert strip_leading_mentions(falsy) == expected


class TestParsePairingCode:
    @pytest.mark.parametrize("raw", [
        "123456",
        "123 456",                 # as the panel displays it
        "pair 123456",
        "pair: 123456",
        "PAIR 123 456",
        "  123456  ",
        "123\t456",
        "@agent1qxyz 123 456",     # as ASI1 delivers it
        "@a @b pair 123456",
    ])
    def test_accepted_forms_all_yield_the_digits(self, raw):
        assert parse_pairing_code(raw) == "123456"

    @pytest.mark.parametrize("raw", [
        "",
        None,
        "pair",
        "12345",                   # too short
        "1234567",                 # too long
        "123-456",
        "123456.",                 # no punctuation stripping here, unlike the approval parser
        "the code is 123456",
        "call me on 555123",       # the whole-message rule: not a pairing attempt
        "my pin is 123 456",
    ])
    def test_rejected_forms_return_none(self, raw):
        assert parse_pairing_code(raw) is None
