"""End-to-end judge for the verification run against a mocked OpenStack.

Covers the decisions that cost storage when wrong: a temporary volume whose
backup is still running must survive, one whose backup finished must go, and
the remaining-temporaries count must exclude what is mid-deletion.
"""

from __future__ import annotations

import asyncio
import datetime

import httpx
import pytest
import respx
from test_backup_flow import CINDER, ENV, GLANCE, KEYSTONE, NOVA, _iso, _token


@pytest.fixture
def cloud(monkeypatch, verify_module):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return verify_module


def _today() -> str:
    return datetime.date.today().isoformat()


def _now_iso() -> str:
    """A timestamp the script counts as today.

    The script compares the local calendar date with the first ten characters
    of an ISO timestamp, as the original did with openstacksdk. Around
    midnight the two disagree, so the test builds the timestamp from the same
    local date rather than from UTC now.
    """
    return f"{_today()}T12:00:00+00:00"


@respx.mock
def test_verify_run_cleans_only_finished_temporaries(cloud):
    respx.post(f"{KEYSTONE}/v3/auth/tokens").mock(
        return_value=httpx.Response(201, json=_token(), headers={"X-Subject-Token": "t"})
    )
    respx.get(f"{NOVA}/servers", params={"limit": "1"}).mock(return_value=httpx.Response(200, json={"servers": []}))
    respx.get(f"{NOVA}/servers/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "s-1",
                        "name": "web",
                        "status": "ACTIVE",
                        "image": {"id": "i"},
                        "metadata": {"autoBackup": "true"},
                    }
                ]
            },
        )
    )
    respx.get(f"{GLANCE}/v2/images").mock(
        return_value=httpx.Response(
            200,
            json={
                "images": [
                    {
                        "id": "i-today",
                        "name": "autoBackup_x_web",
                        "status": "active",
                        "visibility": "private",
                        "created_at": _now_iso(),
                    },
                    {
                        "id": "i-stuck",
                        "name": "autoBackup_old_web",
                        "status": "saving",
                        "visibility": "private",
                        "created_at": _iso(3),
                    },
                ]
            },
        )
    )
    volumes = {
        "volumes": [
            {
                "id": "v-src",
                "name": "data",
                "status": "in-use",
                "size": 50,
                "bootable": "false",
                "metadata": {"autoBackup": "true"},
                "attachments": [],
            },
            {
                "id": "tv-done",
                "name": "temp_vol_x_data",
                "status": "available",
                "size": 50,
                "bootable": "false",
                "metadata": {},
                "attachments": [],
            },
            {
                "id": "tv-busy",
                "name": "temp_vol_y_data",
                "status": "available",
                "size": 50,
                "bootable": "false",
                "metadata": {},
                "attachments": [],
            },
            {
                "id": "tv-going",
                "name": "temp_vol_z_data",
                "status": "deleting",
                "size": 50,
                "bootable": "false",
                "metadata": {},
                "attachments": [],
            },
        ]
    }
    respx.get(f"{CINDER}/volumes/detail").mock(return_value=httpx.Response(200, json=volumes))
    respx.get(f"{CINDER}/backups/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "backups": [
                    {
                        "id": "b-1",
                        "name": "autoBackup_x_data",
                        "status": "available",
                        "volume_id": "tv-done",
                        "created_at": _now_iso(),
                    },
                    {
                        "id": "b-2",
                        "name": "autoBackup_y_data",
                        "status": "creating",
                        "volume_id": "tv-busy",
                        "created_at": _now_iso(),
                    },
                ]
            },
        )
    )
    respx.get(f"{CINDER}/snapshots/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "snapshots": [
                    {"id": "snap-done", "name": "temp_snap_x_data", "status": "available", "size": 50},
                    {"id": "snap-busy", "name": "temp_snap_y_data", "status": "creating", "size": 50},
                ]
            },
        )
    )
    delete_done = respx.delete(f"{CINDER}/volumes/tv-done").mock(return_value=httpx.Response(204))
    delete_busy = respx.delete(f"{CINDER}/volumes/tv-busy").mock(return_value=httpx.Response(204))
    delete_snap = respx.delete(f"{CINDER}/snapshots/snap-done").mock(return_value=httpx.Response(204))

    results = asyncio.run(cloud.run(_today()))

    assert delete_done.called, "a temp volume whose backup is available is removed"
    assert not delete_busy.called, "a temp volume whose backup is still creating survives"
    assert delete_snap.called
    assert results["img"] == {"active": 1, "stuck": 0, "error": 0, "stuck_old": 1}
    assert results["vol"]["available"] == 1 and results["vol"]["stuck"] == 1
    assert results["stuck_source"] == 0
    assert results["temp"]["volumes"] == 1 and results["temp"]["snapshots"] == 1
    # Remaining: tv-done, tv-busy, snap-done, snap-busy re-listed (the mock is
    # static), minus tv-going which is mid-deletion. 4 items, 200 GB.
    assert results["temp"]["remaining_count"] == 4
    assert results["temp"]["remaining_gb"] == 200
    assert results["has_tagged_resources"] is True
