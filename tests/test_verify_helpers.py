"""Tests for the pure helpers in openstack-verify.py."""
from __future__ import annotations


def test_parse_date_iso_with_microseconds(verify_module):
    # Real timestamps from OpenStack APIs include microseconds and the Z suffix.
    assert verify_module._parse_date("2026-05-14T12:34:56.789Z") == "2026-05-14"


def test_parse_date_iso_without_microseconds(verify_module):
    assert verify_module._parse_date("2026-05-14T12:34:56") == "2026-05-14"


def test_parse_date_empty_returns_empty(verify_module):
    assert verify_module._parse_date("") == ""


def test_parse_date_none_returns_empty(verify_module):
    # The function should not blow up on a None coming from a missing attribute.
    assert verify_module._parse_date(None) == ""
