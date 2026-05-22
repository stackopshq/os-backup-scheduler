"""Tests for the pure helpers in openstack-verify.py."""

from __future__ import annotations

from types import SimpleNamespace


def _res(name, status, size):
    """Minimal stand-in for an OpenStack volume/snapshot resource."""
    return SimpleNamespace(name=name, status=status, size=size)


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


def test_count_temp_resources_excludes_deleting(verify_module):
    # A volume deleted earlier in the same verify run lingers in `deleting`; it
    # must not be counted as a surviving orphan (regression: #15).
    volumes = [
        _res("temp_vol_x", "available", 250),
        _res("temp_vol_y", "deleting", 250),
    ]
    assert verify_module._count_temp_resources(volumes, []) == (1, 250)


def test_count_temp_resources_ignores_non_temp(verify_module):
    volumes = [_res("autoBackup_isa_vol", "available", 999)]
    assert verify_module._count_temp_resources(volumes, []) == (0, 0)


def test_count_temp_resources_counts_snapshots(verify_module):
    snapshots = [_res("temp_snap_z", "available", 40)]
    assert verify_module._count_temp_resources([], snapshots) == (1, 40)


def test_count_temp_resources_keeps_error_deleting(verify_module):
    # error_deleting is a genuine stuck orphan — it must still be counted.
    volumes = [_res("temp_vol_e", "error_deleting", 100)]
    assert verify_module._count_temp_resources(volumes, []) == (1, 100)


def test_count_temp_resources_sums_volumes_and_snapshots(verify_module):
    volumes = [_res("temp_vol_a", "available", 250), _res("temp_vol_b", "creating", 250)]
    snapshots = [_res("temp_snap_c", "available", 40)]
    assert verify_module._count_temp_resources(volumes, snapshots) == (3, 540)


def test_count_temp_resources_handles_none_size(verify_module):
    # A missing size attribute must not blow up the count.
    volumes = [_res("temp_vol_n", "available", None)]
    assert verify_module._count_temp_resources(volumes, []) == (1, 0)
