#!/usr/bin/env python3
"""
OpenStack Backup Verification Script

Verifies backup completion, detects stuck/failed backups, and cleans up
temporary resources left by async backup mode. Single authenticated session
through stackops-cloud.

Repository: https://git.stackops.ch/stackops/os-backup-scheduler
License: Apache-2.0
"""

import asyncio
import datetime
import logging
import os
import sys
import time
from types import SimpleNamespace

from stackops_cloud.errors import CloudError
from stackops_cloud.provider import Credentials, Resource, ResourceKind
from stackops_cloud.providers.openstack import (
    OpenStackBackups,
    OpenStackInventory,
    OpenStackSession,
)

############################################################################
#  Configuration
############################################################################

REGION_NAME = os.environ.get("OS_REGION_NAME", "unknown")
SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null")
OUTPUT_FILE = os.environ.get("GITHUB_OUTPUT", "/dev/null")
ZABBIX_SERVER = os.environ.get("ZABBIX_SERVER", "")
ZABBIX_HOST = os.environ.get("ZABBIX_HOST", "")


############################################################################
#  Helpers
############################################################################


def summary(*lines: str):
    """Append lines to GitHub Step Summary."""
    try:
        with open(SUMMARY_FILE, "a") as f:
            for line in lines:
                f.write(line + "\n")
    except (PermissionError, OSError):
        pass


def set_output(key: str, value):
    try:
        with open(OUTPUT_FILE, "a") as f:
            f.write(f"{key}={value}\n")
    except (PermissionError, OSError):
        pass


def credentials_from_env() -> Credentials:
    app_cred = os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
    if app_cred:
        required = ["OS_AUTH_URL", "OS_APPLICATION_CREDENTIAL_ID", "OS_APPLICATION_CREDENTIAL_SECRET"]
    else:
        required = ["OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Error: Missing required environment variables: {' '.join(missing)}")
        print("Required: OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME")
        print("Optional: OS_USER_DOMAIN_NAME, OS_PROJECT_DOMAIN_NAME, OS_REGION_NAME, OS_IDENTITY_API_VERSION")
        sys.exit(1)

    secrets = {"auth_url": os.environ["OS_AUTH_URL"]}
    if app_cred:
        secrets["application_credential_id"] = app_cred
        secrets["application_credential_secret"] = os.environ["OS_APPLICATION_CREDENTIAL_SECRET"]
    else:
        secrets.update(
            username=os.environ["OS_USERNAME"],
            password=os.environ["OS_PASSWORD"],
            project_name=os.environ["OS_PROJECT_NAME"],
            user_domain_name=os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
            project_domain_name=os.environ.get("OS_PROJECT_DOMAIN_NAME", "default"),
        )
    return Credentials(
        provider_slug="openstack",
        region_id=os.environ.get("OS_REGION_NAME") or "",
        project_id="",
        secrets=secrets,
    )


class Cloud:
    def __init__(self, session: OpenStackSession, creds: Credentials, services: tuple[str, ...]):
        self.creds = creds
        self.inventory = OpenStackInventory(session)
        self.backups = OpenStackBackups(session)
        self.services = services

    @property
    def has_block_storage(self) -> bool:
        return "block-storage" in self.services

    async def list(self, kind: ResourceKind) -> list:
        """Resources of one kind as report views (see ``_view``); ``None``
        when the service is absent from the region, matching the original
        script's EndpointNotFound handling."""
        if kind in (ResourceKind.VOLUME, ResourceKind.BACKUP, ResourceKind.SNAPSHOT) and not self.has_block_storage:
            return None
        return [_view(r) async for r in self.inventory.resources(self.creds, kinds=[kind])]


def _view(resource: Resource) -> SimpleNamespace:
    """The attribute shape the report functions were written against.

    Keeping the report code verbatim is the point: it is what operators read
    every morning, and the tests exercise it through these attributes.
    """
    return SimpleNamespace(
        id=resource.id,
        name=resource.name,
        status=resource.raw_status,
        created_at=resource.created_at.isoformat() if resource.created_at else "",
        size=resource.size_gb,
        metadata=resource.tags,
        volume_id=resource.attributes.get("volume_id"),
        visibility=resource.attributes.get("visibility"),
        resource=resource,
    )


async def connect(session: OpenStackSession) -> Cloud:
    creds = credentials_from_env()
    print("Verifying OpenStack connectivity...")
    health = await OpenStackInventory(session).health(creds)
    if not health.ok:
        print(f"Error: Failed to authenticate with OpenStack: {health.detail}")
        sys.exit(1)
    print("Authentication successful.")
    return Cloud(session, creds, health.checked)


def _parse_date(ts: str) -> str:
    """Return YYYY-MM-DD from an ISO timestamp string."""
    return (ts or "")[:10]


############################################################################
#  Instance backup verification
############################################################################


def check_instance_backups(all_images: list, today: str) -> dict:
    print("-" * 40)
    print("Checking instance backups!")

    counts = dict(active=0, stuck=0, error=0, stuck_old=0)
    rows_today = []
    rows_old_stuck = []

    for image in all_images:
        name = image.name or ""
        if not name.startswith("autoBackup"):
            continue

        status = image.status or ""
        created_at = getattr(image, "created_at", "") or ""
        is_today = _parse_date(created_at) == today

        if is_today:
            if status == "active":
                counts["active"] += 1
                rows_today.append(f"| {name} | ✅ {status} |")
            elif status in ("queued", "saving"):
                counts["stuck"] += 1
                print(f"⚠️  STUCK: {name} - Status: {status}")
                rows_today.append(f"| {name} | ⚠️ {status} (stuck) |")
            else:
                counts["error"] += 1
                print(f"❌ ERROR: {name} - Status: {status} - Created: {created_at}")
                rows_today.append(f"| {name} | ❌ {status} |")
        else:
            if status in ("queued", "saving"):
                counts["stuck_old"] += 1
                print(f"🔴 OLD BACKUP: {name} - Status: {status} - Created: {created_at}")
                rows_old_stuck.append(f"| {name} | 🔴 {status} | {created_at[:10]} |")

    icon = "❌" if counts["error"] else ("⚠️" if counts["stuck"] else "✅")
    summary(
        f"### {icon} Instance Backups - {counts['active']} ✅ active · {counts['stuck']} ⚠️ stuck · {counts['error']} ❌ error",
        "",
    )
    if rows_today:
        summary("| Backup | Status |", "|--------|--------|", *rows_today)
    else:
        summary("_No instance backups found for today._")
    if rows_old_stuck:
        summary(
            "",
            "**🔴 Old backups still stuck:**",
            "",
            "| Backup | Status | Created |",
            "|--------|--------|---------|",
            *rows_old_stuck,
        )
    summary("")

    return counts


############################################################################
#  Volume backup verification
############################################################################


def check_volume_backups(all_backups: list, today: str) -> dict:
    print("-" * 40)
    print("Checking volume backups!")

    summary("### Volume Backups", "")

    counts = dict(available=0, stuck=0, error=0, stuck_old=0)

    if all_backups is None:
        summary("### ℹ️ Volume Backups - service not available in this region", "")
        return counts
    if not all_backups:
        summary("### ℹ️ Volume Backups - no backups found", "")
        return counts

    rows_today = []
    rows_old_stuck = []

    for backup in all_backups:
        name = backup.name or ""
        if not name.startswith("autoBackup"):
            continue

        status = backup.status or ""
        created_at = getattr(backup, "created_at", "") or ""
        is_today = _parse_date(created_at) == today

        if is_today:
            if status == "available":
                counts["available"] += 1
                rows_today.append(f"| {name} | ✅ {status} |")
            elif status in ("creating", "backing-up"):
                counts["stuck"] += 1
                print(f"⚠️  STUCK: {name} - Status: {status}")
                rows_today.append(f"| {name} | ⚠️ {status} (stuck) |")
            else:
                counts["error"] += 1
                print(f"❌ ERROR: {name} - Status: {status} - Created: {created_at}")
                rows_today.append(f"| {name} | ❌ {status} |")
        else:
            if status in ("creating", "backing-up"):
                counts["stuck_old"] += 1
                print(f"🔴 OLD BACKUP: {name} - Status: {status} - Created: {created_at}")
                rows_old_stuck.append(f"| {name} | 🔴 {status} | {created_at[:10]} |")

    icon = "❌" if counts["error"] else ("⚠️" if counts["stuck"] else "✅")
    summary(
        f"### {icon} Volume Backups - {counts['available']} ✅ available · {counts['stuck']} ⚠️ stuck · {counts['error']} ❌ error",
        "",
    )
    if rows_today:
        summary("| Backup | Status |", "|--------|--------|", *rows_today)
    else:
        summary("_No volume backups found for today._")
    if rows_old_stuck:
        summary(
            "",
            "**🔴 Old backups still stuck:**",
            "",
            "| Backup | Status | Created |",
            "|--------|--------|---------|",
            *rows_old_stuck,
        )
    summary("")

    return counts


############################################################################
#  Source volume health check
############################################################################


def check_source_volumes(all_volumes: list) -> int:
    print("-" * 40)
    print("Checking source volumes!")

    summary("### Source Volumes Status", "")

    stuck = 0

    if all_volumes is None:
        summary("### ℹ️ Source Volumes - service not available in this region", "")
        return 0

    tagged = [v for v in all_volumes if (v.metadata or {}).get("autoBackup") == "true"]

    if not tagged:
        summary("### ✅ Source Volumes - no tagged volumes found", "")
        return 0

    rows = []
    for vol in tagged:
        name = vol.name or vol.id[:8]
        status = vol.status or ""
        if status in ("creating", "backing-up", "deleting", "restoring-backup"):
            stuck += 1
            print(f"⚠️  STUCK SOURCE VOLUME: {name} - Status: {status}")
            rows.append(f"| {name} | ⚠️ {status} |")
        else:
            rows.append(f"| {name} | ✅ {status} |")

    icon = "⚠️" if stuck else "✅"
    summary(
        f"### {icon} Source Volumes - {len(tagged)} tagged, {stuck} stuck",
        "",
        "| Volume | Status |",
        "|--------|--------|",
        *rows,
        "",
    )

    return stuck


############################################################################
#  Temporary resource cleanup
############################################################################


def _count_temp_resources(volumes, snapshots) -> tuple[int, int]:
    """Return (count, total_gb) of temp_* resources that are genuine orphans.

    Resources in the ``deleting`` state are excluded: ``delete_volume()`` and
    ``delete_snapshot()`` are asynchronous, so resources deleted earlier in the
    same verify run linger in ``deleting`` and would otherwise be miscounted as
    survivors, inflating the verify.temp_gb / verify.temp_count Zabbix metrics
    (os-backup-scheduler#15). ``error_deleting`` is kept: it is a real orphan.
    """
    count = 0
    gb = 0
    for res in (*volumes, *snapshots):
        if not (res.name or "").startswith("temp_"):
            continue
        if (res.status or "") == "deleting":
            continue
        count += 1
        gb += int(res.size or 0)
    return count, gb


async def cleanup_temp_resources(cloud: Cloud, all_volumes: list, all_backups: list) -> dict:
    print("-" * 40)
    print("Cleaning up temporary resources!")

    counts = dict(volumes=0, snapshots=0, errors=0)
    rows = []

    # Temp volumes (temp_vol_*)
    print("Checking for temporary volumes to cleanup...")
    for vol in all_volumes or []:
        name = vol.name or ""
        if not name.startswith("temp_vol_"):
            continue

        status = vol.status or ""
        if status == "available":
            if all_backups is None:
                print(f"Skipping temp volume (cannot verify backup status): {name} ({vol.id})")
                rows.append(f"| {name} | 💾 Volume | ⏳ Skipped (backup service unavailable) |")
                continue
            backup_in_progress = any(
                b.volume_id == vol.id and b.status in ("creating", "backing-up") for b in all_backups
            )
            if backup_in_progress:
                print(f"Skipping temp volume (backup still in progress): {name} ({vol.id})")
                rows.append(f"| {name} | 💾 Volume | ⏳ Backup still in progress |")
                continue
            print(f"Cleaning up temporary volume: {name} ({vol.id})")
            try:
                await cloud.backups.delete(cloud.creds, vol.resource)
                counts["volumes"] += 1
                rows.append(f"| {name} | 💾 Volume | 🗑️ Deleted |")
            except Exception as e:
                counts["errors"] += 1
                print(f"Warning: Failed to delete temp volume {name}: {e}")
                rows.append(f"| {name} | 💾 Volume | ❌ Delete failed |")
        elif status in ("in-use", "creating"):
            print(f"Skipping temporary volume (still in use): {name} - Status: {status}")
            rows.append(f"| {name} | 💾 Volume | ⏳ {status} |")

    # Temp snapshots (temp_snap_*)
    print("Checking for temporary snapshots to cleanup...")
    all_snapshots = await cloud.list(ResourceKind.SNAPSHOT)

    for snap in all_snapshots or []:
        name = snap.name or ""
        if not name.startswith("temp_snap_"):
            continue

        status = snap.status or ""
        if status == "available":
            print(f"Cleaning up temporary snapshot: {name} ({snap.id})")
            try:
                await cloud.backups.delete(cloud.creds, snap.resource)
                counts["snapshots"] += 1
                rows.append(f"| {name} | 📸 Snapshot | 🗑️ Deleted |")
            except Exception as e:
                counts["errors"] += 1
                print(f"Warning: Failed to delete temp snapshot {name}: {e}")
                rows.append(f"| {name} | 📸 Snapshot | ❌ Delete failed |")
        elif status in ("creating", "deleting"):
            print(f"Skipping temporary snapshot (busy): {name} - Status: {status}")
            rows.append(f"| {name} | 📸 Snapshot | ⏳ {status} |")

    total_cleaned = counts["volumes"] + counts["snapshots"]
    summary(f"### 🧹 Temporary Resources Cleanup - {total_cleaned} deleted", "")
    if rows:
        summary("| Resource | Type | Action |", "|----------|------|--------|", *rows)
    else:
        summary("_No temporary resources found._")
    if counts["errors"]:
        summary("", f"> ⚠️ {counts['errors']} cleanup error(s)")

    # Count temp_* resources that survived the cleanup (still consuming storage).
    # These are typically backups still in progress or genuinely stuck states.
    # Resources mid-deletion are excluded: see _count_temp_resources. Feeds the
    # verify.temp_count / verify.temp_gb Zabbix items.
    remaining_count = 0
    remaining_gb = 0
    try:
        remaining_count, remaining_gb = _count_temp_resources(
            await cloud.list(ResourceKind.VOLUME) or [],
            await cloud.list(ResourceKind.SNAPSHOT) or [],
        )
    except Exception as e:
        print(f"Warning: failed to count remaining temp resources: {e}")
    counts["remaining_count"] = remaining_count
    counts["remaining_gb"] = remaining_gb

    summary(f"> Remaining temp_* resources after cleanup: **{remaining_count}** items, **{remaining_gb} GB**")
    summary("")

    return counts


############################################################################
#  Zabbix reporting
############################################################################


def _make_zabbix_sender(server_spec: str):
    """Build a `zabbix_utils.Sender` from the `ZABBIX_SERVER` env value.

    A comma-separated string (e.g. ``"proxy-a.example,proxy-b.example"`` or
    ``"10.9.0.15:10051,10.8.0.15:10051"``) is interpreted as a single
    failover cluster: ``zabbix_utils`` tries each entry in order and falls
    back to the next on failure. A bare host (or ``host:port``) keeps the
    historical single-target behaviour.
    """
    from zabbix_utils import Sender

    nodes = [p.strip() for p in server_spec.split(",") if p.strip()]
    if len(nodes) <= 1:
        # Single target. Split off ``:port`` if present so zabbix_utils 2.0.4
        # does not pass the port through to ``Node(*split(':'))`` (which only
        # takes 2 args).
        spec = nodes[0] if nodes else server_spec
        if ":" in spec and spec.rsplit(":", 1)[1].isdigit():
            host, port = spec.rsplit(":", 1)
            return Sender(server=host, port=int(port))
        return Sender(server=spec)
    return Sender(clusters=[nodes])


def send_zabbix_run_started():
    """Ship a single trapper item marking that the verify run has started.

    Mirror of openstack-backup.py's send_zabbix_run_started; see the comment
    there.
    """
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return
    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue

        _make_zabbix_sender(ZABBIX_SERVER).send([ItemValue(host, "verify.run_started_at", int(time.time()))])
        print(f"Zabbix run-started ping sent to {ZABBIX_SERVER} for host {host}")
    except Exception as e:
        print(f"Warning: Failed to send Zabbix run-started ping: {e}")


def send_zabbix_metrics(total_success: int, total_stuck: int, total_error: int, temp_count: int = 0, temp_gb: int = 0):
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return

    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue

        sender = _make_zabbix_sender(ZABBIX_SERVER)
        sender.send(
            [
                ItemValue(host, "verify.ok", total_success),
                ItemValue(host, "verify.stuck", total_stuck),
                ItemValue(host, "verify.errors", total_error),
                ItemValue(host, "verify.temp_count", temp_count),
                ItemValue(host, "verify.temp_gb", temp_gb),
                ItemValue(host, "verify.heartbeat", int(time.time())),
            ]
        )
        print(f"Zabbix metrics sent to {ZABBIX_SERVER} for host {host}")
    except Exception as e:
        print(f"Warning: Failed to send Zabbix metrics: {e}")


############################################################################
#  Entry point
############################################################################


async def run(today: str) -> dict:
    """Everything that talks to the cloud, in the original order so the
    summary reads the same. Returns the figures main() reports on."""
    async with OpenStackSession() as session:
        cloud = await connect(session)

        # Fetch shared resource lists once; passed to functions to avoid duplicate API calls.
        # None means the service endpoint is unavailable; [] means available but empty.
        all_images = [i for i in await cloud.list(ResourceKind.IMAGE) if i.visibility == "private"]
        all_volumes = await cloud.list(ResourceKind.VOLUME)
        all_backups = await cloud.list(ResourceKind.BACKUP)

        # Count tagged resources to know if backups are expected
        tagged_instances = [
            s
            for s in await cloud.list(ResourceKind.INSTANCE)
            if (s.metadata or {}).get("autoBackup") == "true" and not s.resource.attributes.get("boot_from_volume")
        ]
        tagged_volumes = [v for v in (all_volumes or []) if (v.metadata or {}).get("autoBackup") == "true"]
        has_tagged_resources = bool(tagged_instances or tagged_volumes)

        img = check_instance_backups(all_images, today)
        vol = check_volume_backups(all_backups, today)
        stuck_source = check_source_volumes(all_volumes)
        temp = await cleanup_temp_resources(cloud, all_volumes, all_backups)

    return {
        "img": img,
        "vol": vol,
        "stuck_source": stuck_source,
        "temp": temp,
        "has_tagged_resources": has_tagged_resources,
    }


def main():
    # Emit run-started ping first thing so a crash during auth or imports
    # still produces a recent run_started_at: paired with the missing
    # verify.heartbeat at the end, a Zabbix trigger can detect a stuck or
    # crashed run within ~2 h instead of the 25 h nodata floor.
    send_zabbix_run_started()

    today = datetime.date.today().isoformat()

    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    summary(f"## Verification Report - {REGION_NAME} - {now_str}", "")

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

    try:
        results = asyncio.run(run(today))
    except CloudError as e:
        print(f"Error: {e}")
        sys.exit(1)

    img = results["img"]
    vol = results["vol"]
    stuck_source = results["stuck_source"]
    temp = results["temp"]
    has_tagged_resources = results["has_tagged_resources"]

    # ---- console summary ----
    total_stuck = img["stuck"] + vol["stuck"] + stuck_source + img["stuck_old"] + vol["stuck_old"]
    total_error = img["error"] + vol["error"]
    total_success = img["active"] + vol["available"]

    print("-" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Active instance backups:  {img['active']}")
    print(f"Available volume backups: {vol['available']}")
    print(f"Stuck (today):            {img['stuck'] + vol['stuck']}")
    print(f"Stuck (old):              {img['stuck_old'] + vol['stuck_old']}")
    print(f"Stuck source volumes:     {stuck_source}")
    print(f"Errors:                   {total_error}")
    print(f"Temp volumes cleaned:     {temp['volumes']}")
    print(f"Temp snapshots cleaned:   {temp['snapshots']}")
    print("-" * 40)

    # ---- GitHub Actions outputs ----
    set_output("stuck_count", total_stuck)
    set_output("error_count", total_error)
    set_output("success_count", total_success)
    set_output("stuck_source_volumes", stuck_source)
    set_output("stuck_old_backups", img["stuck_old"] + vol["stuck_old"])

    send_zabbix_metrics(
        total_success,
        total_stuck,
        total_error,
        temp_count=temp.get("remaining_count", 0),
        temp_gb=temp.get("remaining_gb", 0),
    )

    summary("---", "")

    if total_error > 0:
        summary(
            f"❌ **Failed** · {total_error} backup(s) in error · {total_success} ok · {temp['volumes'] + temp['snapshots']} temp resources cleaned"
        )
        print(f"Finished with {total_error} error(s)!")
        sys.exit(1)
    elif total_stuck > 0:
        stuck_old_total = img["stuck_old"] + vol["stuck_old"]
        msg = f"⚠️ **Stuck** · {total_stuck} resource(s) require attention"
        if stuck_old_total > 0:
            msg += f" · {stuck_old_total} old backup(s) still processing"
        if stuck_source > 0:
            msg += f" · {stuck_source} source volume(s) unstable"
        summary(msg)
        print(f"Finished with {total_stuck} stuck resource(s)!")
        sys.exit(1)
    elif total_success == 0 and has_tagged_resources:
        summary("⚠️ **Warning** · No backups found for today despite tagged resources")
        print("Warning: No backups found for today despite tagged resources!")
        sys.exit(1)
    else:
        summary(
            f"✅ **Success** · {total_success} backup(s) verified · {temp['volumes'] + temp['snapshots']} temp resource(s) cleaned"
        )
        print("Finished successfully!")


if __name__ == "__main__":
    main()
