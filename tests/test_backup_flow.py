"""End-to-end judge: the backup run against a mocked OpenStack.

This is the test that decides whether the port onto stackops-cloud lost
behaviour. One cloud, every branch the script has: a tagged boot-from-image
instance, a tagged boot-from-volume one, a busy one, an untagged one; an
attached unnamed volume, a detached named one, an untagged one; images and
backups on both sides of the retention date. Every counter the summary
prints is asserted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

KEYSTONE = "https://keystone.example"
NOVA = "https://nova.example"
CINDER = "https://cinder.example/v3/p-1"
GLANCE = "https://glance.example"

ENV = {
    "OS_AUTH_URL": KEYSTONE,
    "OS_USERNAME": "backup",
    "OS_PASSWORD": "pw",
    "OS_PROJECT_NAME": "PCP-TEST",
    "OS_REGION_NAME": "dc1",
}


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _endpoint(service_type: str, url: str) -> dict:
    return {"type": service_type, "endpoints": [{"interface": "public", "region_id": "dc1", "url": url}]}


def _token(services=("identity", "compute", "block-storage", "image")) -> dict:
    urls = {"identity": KEYSTONE, "compute": NOVA, "block-storage": CINDER, "image": GLANCE}
    return {
        "token": {
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "project": {"id": "p-1"},
            "user": {"id": "u-1"},
            "roles": [],
            "catalog": [_endpoint(s, urls[s]) for s in services],
        }
    }


def _body(call) -> dict:
    return json.loads(call.request.content)


@pytest.fixture
def cloud(monkeypatch, backup_module):
    """Env, fresh stats, and a fully mocked cloud."""
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(backup_module, "USE_SNAPSHOT_METHOD", True)
    monkeypatch.setattr(backup_module, "WAIT_FOR_BACKUP", False)
    monkeypatch.setattr(backup_module, "RETENTION_DAYS", 14)
    monkeypatch.setattr(backup_module, "stats", backup_module.Stats())
    return backup_module


def _mock_cloud(services=("identity", "compute", "block-storage", "image")) -> dict[str, respx.Route]:
    routes: dict[str, respx.Route] = {}
    respx.post(f"{KEYSTONE}/v3/auth/tokens").mock(
        return_value=httpx.Response(201, json=_token(services), headers={"X-Subject-Token": "t"})
    )
    respx.get(f"{NOVA}/servers", params={"limit": "1"}).mock(return_value=httpx.Response(200, json={"servers": []}))
    respx.get(f"{NOVA}/servers/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "s-img",
                        "name": "web",
                        "status": "ACTIVE",
                        "image": {"id": "i-base"},
                        "metadata": {"autoBackup": "true"},
                        "OS-EXT-STS:task_state": None,
                    },
                    {
                        "id": "s-bfv",
                        "name": "db",
                        "status": "ACTIVE",
                        "image": "",
                        "metadata": {"autoBackup": "true"},
                        "OS-EXT-STS:task_state": None,
                    },
                    {
                        "id": "s-busy",
                        "name": "busy",
                        "status": "ACTIVE",
                        "image": {"id": "i-base"},
                        "metadata": {"autoBackup": "true"},
                        "OS-EXT-STS:task_state": "rebooting",
                    },
                    {
                        "id": "s-plain",
                        "name": "plain",
                        "status": "ACTIVE",
                        "image": {"id": "i-base"},
                        "metadata": {},
                        "OS-EXT-STS:task_state": None,
                    },
                ]
            },
        )
    )
    routes["server_backup"] = respx.post(f"{NOVA}/servers/s-img/action").mock(return_value=httpx.Response(202))
    routes["busy_backup"] = respx.post(f"{NOVA}/servers/s-busy/action").mock(return_value=httpx.Response(202))
    routes["bfv_backup"] = respx.post(f"{NOVA}/servers/s-bfv/action").mock(return_value=httpx.Response(202))

    respx.get(f"{CINDER}/volumes/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "volumes": [
                    {
                        "id": "v-attached",
                        "name": "",
                        "status": "in-use",
                        "size": 50,
                        "bootable": "true",
                        "metadata": {"autoBackup": "true"},
                        "attachments": [{"server_id": "s-img"}],
                    },
                    {
                        "id": "v-detached",
                        "name": "data",
                        "status": "available",
                        "size": 20,
                        "bootable": "false",
                        "metadata": {"autoBackup": "true"},
                        "attachments": [],
                    },
                    {
                        "id": "v-plain",
                        "name": "scratch",
                        "status": "available",
                        "size": 5,
                        "bootable": "false",
                        "metadata": {},
                        "attachments": [],
                    },
                ]
            },
        )
    )
    routes["snapshot"] = respx.post(f"{CINDER}/snapshots").mock(
        return_value=httpx.Response(202, json={"snapshot": {"id": "snap-1", "status": "creating"}})
    )
    respx.get(f"{CINDER}/snapshots/snap-1").mock(
        return_value=httpx.Response(200, json={"snapshot": {"id": "snap-1", "status": "available"}})
    )
    routes["temp_volume"] = respx.post(f"{CINDER}/volumes").mock(
        return_value=httpx.Response(202, json={"volume": {"id": "tv-1", "status": "creating"}})
    )
    respx.get(f"{CINDER}/volumes/tv-1").mock(
        return_value=httpx.Response(200, json={"volume": {"id": "tv-1", "status": "available"}})
    )

    def _create_backup(request: httpx.Request) -> httpx.Response:
        volume_id = json.loads(request.content)["backup"]["volume_id"]
        return httpx.Response(202, json={"backup": {"id": f"b-for-{volume_id}", "status": "creating"}})

    routes["backup"] = respx.post(f"{CINDER}/backups").mock(side_effect=_create_backup)

    respx.get(f"{GLANCE}/v2/images").mock(
        return_value=httpx.Response(
            200,
            json={
                "images": [
                    {
                        "id": "i-old",
                        "name": "autoBackup_old_web",
                        "status": "active",
                        "visibility": "private",
                        "created_at": _iso(30),
                    },
                    {
                        "id": "i-new",
                        "name": "autoBackup_new_web",
                        "status": "active",
                        "visibility": "private",
                        "created_at": _iso(1),
                    },
                    {
                        "id": "i-pub",
                        "name": "autoBackup_public",
                        "status": "active",
                        "visibility": "public",
                        "created_at": _iso(30),
                    },
                    {
                        "id": "i-base",
                        "name": "rocky-10",
                        "status": "active",
                        "visibility": "private",
                        "created_at": _iso(400),
                    },
                ]
            },
        )
    )
    routes["delete_old_image"] = respx.delete(f"{GLANCE}/v2/images/i-old").mock(return_value=httpx.Response(204))
    routes["delete_pub_image"] = respx.delete(f"{GLANCE}/v2/images/i-pub").mock(return_value=httpx.Response(204))

    respx.get(f"{CINDER}/backups/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "backups": [
                    {"id": "b-old", "name": "autoBackup_old_data", "status": "available", "created_at": _iso(30)},
                    {"id": "b-new", "name": "autoBackup_new_data", "status": "available", "created_at": _iso(1)},
                    {"id": "b-manual", "name": "manual-keep", "status": "available", "created_at": _iso(300)},
                ]
            },
        )
    )
    routes["delete_old_backup"] = respx.delete(f"{CINDER}/backups/b-old").mock(return_value=httpx.Response(204))
    return routes


@respx.mock
def test_full_run_matches_the_original_behaviour(cloud):
    routes = _mock_cloud()
    asyncio.run(cloud.run())
    stats = cloud.stats

    # Instances: only the tagged, boot-from-image, idle one.
    assert routes["server_backup"].call_count == 1
    assert not routes["bfv_backup"].called, "boot-from-volume instances are skipped"
    assert not routes["busy_backup"].called, "a busy task_state skips the instance"
    body = _body(routes["server_backup"].calls[0])["createBackup"]
    assert body["backup_type"] == "daily" and body["rotation"] == 14
    assert body["name"].startswith("autoBackup_") and body["name"].endswith("_web")
    assert stats.instances_backed_up == 1

    # Volumes: attached one through the snapshot path, detached one directly.
    assert routes["snapshot"].call_count == 1
    assert _body(routes["snapshot"].calls[0])["snapshot"]["force"] is True
    assert routes["temp_volume"].call_count == 1
    backup_bodies = [_body(c)["backup"] for c in routes["backup"].calls]
    by_volume = {b["volume_id"]: b for b in backup_bodies}
    assert set(by_volume) == {"tv-1", "v-detached"}, "the live attached volume is never backed up directly"
    assert "force" not in by_volume["tv-1"]
    assert "force" not in by_volume["v-detached"]
    assert stats.volumes_backed_up == 2
    assert stats.snapshots_created == 1 and stats.temp_volumes_created == 1

    # Naming: an unnamed attached volume is labelled after its instance.
    methods = {name: method for name, _, method in stats.backed_volumes}
    assert methods == {"web_vol": "snapshot", "data": "direct"}
    temp_names = _body(routes["snapshot"].calls[0])["snapshot"]["name"]
    assert temp_names.startswith("temp_snap_") and temp_names.endswith("_web_vol")

    # Async mode: temporaries are left for the verification run.
    deletes = [c.request.url.path for c in respx.calls if c.request.method == "DELETE"]
    assert "/v3/p-1/volumes/tv-1" not in deletes
    assert "/v3/p-1/snapshots/snap-1" not in deletes

    # Retention: older than 14 days, autoBackup-prefixed, private images only.
    assert routes["delete_old_image"].call_count == 1
    assert not routes["delete_pub_image"].called, "public images are never touched"
    assert routes["delete_old_backup"].call_count == 1
    assert stats.instance_backups_deleted == 1
    assert stats.volume_backups_deleted == 1
    assert stats.deleted_volume_backups_list == ["autoBackup_old_data"]

    assert stats.errors == 0
    assert stats.errored_resources == []


@respx.mock
def test_region_without_cinder_skips_volume_steps(cloud):
    _mock_cloud(services=("identity", "compute", "image"))
    asyncio.run(cloud.run())
    cinder_calls = [c for c in respx.calls if "cinder" in str(c.request.url)]
    assert cinder_calls == []
    assert cloud.stats.instances_backed_up == 1
    assert cloud.stats.volumes_backed_up == 0
    assert cloud.stats.errors == 0


@respx.mock
def test_busy_volume_is_an_error_not_a_crash(cloud):
    routes = _mock_cloud()
    respx.get(f"{CINDER}/volumes/detail").mock(
        return_value=httpx.Response(
            200,
            json={
                "volumes": [
                    {
                        "id": "v-busy",
                        "name": "busy",
                        "status": "backing-up",
                        "size": 1,
                        "bootable": "false",
                        "metadata": {"autoBackup": "true"},
                        "attachments": [],
                    }
                ]
            },
        )
    )
    asyncio.run(cloud.run())
    assert not routes["backup"].called
    assert cloud.stats.errors == 1
    assert cloud.stats.errored_resources == [("busy", "backup failed (method: force)")]


@respx.mock
def test_snapshot_failure_cleans_up_and_counts_one_error(cloud):
    routes = _mock_cloud()
    routes["temp_volume"].mock(return_value=httpx.Response(500, json={"message": "no space"}))
    snap_delete = respx.delete(f"{CINDER}/snapshots/snap-1").mock(return_value=httpx.Response(204))
    asyncio.run(cloud.run())
    assert snap_delete.called, "the snapshot from step 1 is removed when step 3 fails"
    assert cloud.stats.errors == 1
    assert cloud.stats.volumes_backed_up == 1, "the detached volume still succeeds"
