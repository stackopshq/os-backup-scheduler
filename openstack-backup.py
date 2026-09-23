#!/usr/bin/env python3
"""
OpenStack Automatic Backup Script

Automated backup solution for OpenStack instances and volumes with
configurable retention policy. Built on stackops-cloud: the OpenStack
inventory and the snapshot -> temporary volume -> backup sequence live in
the shared library; this script keeps the policy (which resources, what
names, how long) and the reporting.

Volume backups run concurrently (BACKUP_CONCURRENCY) on one authenticated
session.

Repository: https://git.stackops.ch/stackops/os-backup-scheduler
License: Apache-2.0
"""

import asyncio
import datetime
import logging
import os
import sys
import threading
import time

from stackops_cloud.errors import CloudError, ResourceBusyError
from stackops_cloud.provider import Credentials, Resource, ResourceKind
from stackops_cloud.providers.openstack import (
    OpenStackBackups,
    OpenStackInventory,
    OpenStackSession,
)

############################################################################
#  Configuration
############################################################################

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", 14))
USE_SNAPSHOT_METHOD = os.environ.get("USE_SNAPSHOT_METHOD", "true").lower() == "true"
WAIT_FOR_BACKUP = os.environ.get("WAIT_FOR_BACKUP", "false").lower() == "true"
RESOURCE_TIMEOUT = int(os.environ.get("RESOURCE_TIMEOUT", 3600))  # snapshots & temp volumes
BACKUP_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", 86400))  # actual backup (compress + Swift upload)
BACKUP_CONCURRENCY = int(os.environ.get("BACKUP_CONCURRENCY", 5))
REGION_NAME = os.environ.get("OS_REGION_NAME", "unknown")
SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null")
ZABBIX_SERVER = os.environ.get("ZABBIX_SERVER", "")
ZABBIX_HOST = os.environ.get("ZABBIX_HOST", "")


############################################################################
#  Thread-safe stats
############################################################################


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.instances_backed_up = 0
        self.volumes_backed_up = 0
        self.instance_backups_deleted = 0
        self.volume_backups_deleted = 0
        self.snapshots_created = 0
        self.snapshots_cleaned = 0
        self.temp_volumes_created = 0
        self.temp_volumes_cleaned = 0
        self.errors = 0
        # Detailed lists for summary report
        self.backed_instances: list = []  # (instance_name, backup_name)
        self.backed_volumes: list = []  # (volume_name, backup_name, method)
        self.errored_resources: list = []  # (name, error_msg)
        self.deleted_instance_backups_list: list = []  # image_name
        self.deleted_volume_backups_list: list = []  # backup_name

    def inc(self, field: str, amount: int = 1):
        with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    def append(self, field: str, value):
        with self._lock:
            getattr(self, field).append(value)


stats = Stats()


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


def credentials_from_env() -> Credentials:
    """Build the library credential from the OS_* contract.

    Password auth is the fleet's current contract. Application credentials
    (OS_APPLICATION_CREDENTIAL_ID + _SECRET) are accepted too, since that is
    the direction Arkeva takes: scoped, revocable, with an expiry.
    """
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
    """One authenticated scope: inventory, backups, and which services exist."""

    def __init__(self, session: OpenStackSession, creds: Credentials, services: tuple[str, ...]):
        self.creds = creds
        self.inventory = OpenStackInventory(session)
        self.backups = OpenStackBackups(
            session,
            resource_timeout=RESOURCE_TIMEOUT,
            backup_timeout=BACKUP_TIMEOUT,
        )
        self.services = services

    @property
    def has_block_storage(self) -> bool:
        return "block-storage" in self.services

    async def list(self, kind: ResourceKind) -> list[Resource]:
        return [r async for r in self.inventory.resources(self.creds, kinds=[kind])]


async def connect(session: OpenStackSession) -> Cloud:
    creds = credentials_from_env()
    print("Verifying OpenStack connectivity...")
    health = await OpenStackInventory(session).health(creds)
    if not health.ok:
        print(f"Error: Failed to authenticate with OpenStack: {health.detail}")
        sys.exit(1)
    print("Authentication successful.")
    return Cloud(session, creds, health.checked)


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")


############################################################################
#  Instance backups
############################################################################


async def backup_instances(cloud: Cloud) -> dict[str, str]:
    """Back up tagged boot-from-image instances. Returns id -> name for all
    servers, which the volume step uses to label unnamed volumes."""
    print("-" * 40)
    print("Creating instance backups!")

    names: dict[str, str] = {}
    for server in await cloud.list(ResourceKind.INSTANCE):
        names[server.id] = server.name
        if server.tags.get("autoBackup") != "true":
            print(f"Skipping instance (no autoBackup metadata): {server.name} - {server.id}")
            continue

        # The inventory derives this from Nova's `image` field, which is empty
        # for boot-from-volume instances; image_id alone is unreliable.
        if server.attributes.get("boot_from_volume"):
            print(
                f"Skipping instance {server.name}: boot-from-volume "
                "(backup the volume directly with autoBackup metadata)"
            )
            continue

        task_state = server.attributes.get("task_state")
        if task_state not in (None, "None"):
            print(f"Skipping instance {server.name}: busy (task_state: {task_state})")
            continue

        backup_name = f"autoBackup_{_timestamp()}_{server.name}"
        print(f"Instance {server.name} is boot-from-image, creating server backup")
        try:
            await cloud.backups.backup_instance(
                cloud.creds, server, name=backup_name, rotation=RETENTION_DAYS, backup_type="daily"
            )
            stats.inc("instances_backed_up")
            stats.append("backed_instances", (server.name, backup_name))
        except Exception as e:
            print(f"Error: Failed to create backup for instance {server.name}: {e}")
            stats.inc("errors")
            stats.append("errored_resources", (server.name, str(e)))
    return names


############################################################################
#  Volume backups
############################################################################


def _volume_label(volume: Resource, server_names: dict[str, str]) -> str:
    """The script's naming fallback: name, else <attached-instance>_vol, else id prefix."""
    if volume.name:
        return volume.name
    attached = volume.attributes.get("attached_to") or []
    if attached:
        server_name = server_names.get(attached[0])
        if server_name:
            return f"{server_name}_vol"
    return volume.id[:8]


async def _volume_backup_task(cloud: Cloud, volume: Resource, server_names: dict[str, str]) -> bool:
    """Back up one volume. Runs under the concurrency semaphore."""
    volume_name = _volume_label(volume, server_names)
    print(f"Processing volume: {volume_name} - {volume.id} (status: {volume.raw_status})")

    backup_name = f"autoBackup_{_timestamp()}_{volume_name}"
    status = volume.raw_status
    if USE_SNAPSHOT_METHOD and status == "in-use":
        print(f"Using snapshot method for attached volume {volume_name}")
        method = "snapshot"
    elif status == "available":
        print(f"Creating direct backup for detached volume {volume_name}")
        method = "direct"
    else:
        print(f"Using force method for volume {volume_name} (status: {status})")
        method = "force"

    try:
        result = await cloud.backups.backup_volume(
            cloud.creds,
            volume,
            name=backup_name,
            prefer_snapshot=USE_SNAPSHOT_METHOD,
            wait=WAIT_FOR_BACKUP,
            temp_label=volume_name,
        )
    except ResourceBusyError:
        print(f"Error: Volume {volume_name} is in '{status}' state - cannot create backup")
        stats.append("errored_resources", (volume_name, f"backup failed (method: {method})"))
        return False
    except Exception as e:
        print(f"Error: Failed to backup volume {volume_name}: {e}")
        stats.append("errored_resources", (volume_name, f"backup failed (method: {method})"))
        return False

    method = result.method.value
    print(f"Volume backup initiated: {backup_name} ({result.backup_id})")
    if result.method.value == "snapshot":
        stats.inc("snapshots_created")
        stats.inc("temp_volumes_created")
        if WAIT_FOR_BACKUP:
            # The library waited and removed the temporaries before returning.
            stats.inc("temp_volumes_cleaned")
            stats.inc("snapshots_cleaned")
        else:
            print("  Async mode: cleanup deferred to verification workflow")
            print(f"    Temp snapshot: {result.temp_snapshot_id} ({result.temp_snapshot_name})")
            print(f"    Temp volume:   {result.temp_volume_id} ({result.temp_volume_name})")
    stats.append("backed_volumes", (volume_name, backup_name, method))
    return True


async def backup_volumes(cloud: Cloud, server_names: dict[str, str]):
    print("-" * 40)
    print("Creating volume backups!")

    if not cloud.has_block_storage:
        print("Volume service not available in this region, skipping.")
        return

    all_volumes = await cloud.list(ResourceKind.VOLUME)
    tagged = [v for v in all_volumes if v.tags.get("autoBackup") == "true"]
    if not tagged:
        print("No volumes with autoBackup=true found.")
        return

    print(f"Found {len(tagged)} volume(s) - running up to {BACKUP_CONCURRENCY} in parallel.")
    semaphore = asyncio.Semaphore(BACKUP_CONCURRENCY)

    async def guarded(vol: Resource) -> bool:
        async with semaphore:
            try:
                return await _volume_backup_task(cloud, vol, server_names)
            except Exception as e:
                print(f"Error: Unexpected error for volume {vol.name or vol.id[:8]}: {e}")
                return False

    for success in await asyncio.gather(*(guarded(v) for v in tagged)):
        stats.inc("volumes_backed_up" if success else "errors")


############################################################################
#  Retention cleanup
############################################################################


async def delete_old_instance_backups(cloud: Cloud, expire_time: datetime.datetime):
    print("-" * 40)
    print("Deleting old instance backups!")

    for image in await cloud.list(ResourceKind.IMAGE):
        if image.attributes.get("visibility") != "private":
            continue
        if not image.name.startswith("autoBackup"):
            continue
        if image.created_at is None:
            continue
        if image.created_at < expire_time:
            print(f"Deleting old instance backup: {image.name} ({image.id})")
            try:
                await cloud.backups.delete(cloud.creds, image)
                stats.inc("instance_backups_deleted")
                stats.append("deleted_instance_backups_list", image.name)
            except Exception as e:
                print(f"Error: Failed to delete instance backup {image.id}: {e}")
                stats.inc("errors")
                stats.append("errored_resources", (image.name, str(e)))
        else:
            print(f"Skipping instance backup: {image.name}")


async def delete_old_volume_backups(cloud: Cloud, expire_time: datetime.datetime):
    print("-" * 40)
    print("Deleting old volume backups!")

    if not cloud.has_block_storage:
        print("Volume backup service not available in this region, skipping.")
        return

    for backup in await cloud.list(ResourceKind.BACKUP):
        if not backup.name.startswith("autoBackup"):
            continue
        if backup.created_at is None:
            continue
        if backup.created_at < expire_time:
            print(f"Deleting old volume backup: {backup.name} ({backup.id})")
            try:
                await cloud.backups.delete(cloud.creds, backup)
                stats.inc("volume_backups_deleted")
                stats.append("deleted_volume_backups_list", backup.name)
            except Exception as e:
                print(f"Error: Failed to delete volume backup {backup.id}: {e}")
                stats.inc("errors")
                stats.append("errored_resources", (backup.name, str(e)))
        else:
            print(f"Skipping volume backup: {backup.name}")


############################################################################
#  Report
############################################################################


def write_summary(date_str: str):
    icon = "❌" if stats.errors else "✅"
    status = "Failed" if stats.errors else "Success"

    print("-" * 40)
    print("SUMMARY")
    print("-" * 40)
    print(f"Instances backed up:       {stats.instances_backed_up}")
    print(f"Volumes backed up:         {stats.volumes_backed_up}")
    print(f"Instance backups deleted:  {stats.instance_backups_deleted}")
    print(f"Volume backups deleted:    {stats.volume_backups_deleted}")
    if USE_SNAPSHOT_METHOD:
        print(f"Snapshots created:         {stats.snapshots_created}")
        print(f"Temp volumes created:      {stats.temp_volumes_created}")
    print(f"Errors:                    {stats.errors}")
    print("-" * 40)

    lines = [
        f"## {icon} Backup Report - {REGION_NAME} - {date_str}",
        "",
        f"**Mode:** {'⏳ Async - temp resources will be cleaned up by the verification workflow' if not WAIT_FOR_BACKUP else '🔄 Sync - waited for each backup to complete'}",
        f"**Retention:** {RETENTION_DAYS}",
        "",
        "---",
        "",
    ]

    # Instance backups
    lines.append(f"### 🖥️ Instance Backups - {stats.instances_backed_up} backed up")
    lines.append("")
    if stats.backed_instances:
        lines += ["| Instance | Backup |", "|----------|--------|"]
        for name, bname in stats.backed_instances:
            lines.append(f"| {name} | {bname} |")
    else:
        lines.append("_No instance backups created._")
    lines.append("")

    # Volume backups
    lines.append(f"### 💾 Volume Backups - {stats.volumes_backed_up} backed up")
    lines.append("")
    if stats.backed_volumes:
        lines += ["| Volume | Backup | Method |", "|--------|--------|--------|"]
        method_labels = {"snapshot": "📸 Snapshot", "direct": "➡️ Direct", "force": "⚡ Force"}
        for vname, bname, method in stats.backed_volumes:
            lines.append(f"| {vname} | {bname} | {method_labels.get(method, method)} |")
        if not WAIT_FOR_BACKUP and stats.snapshots_created > 0:
            lines.append("")
            lines.append(
                f"> ⏳ **{stats.snapshots_created} snapshot(s)** and **{stats.temp_volumes_created} temp volume(s)** are pending cleanup by the verification workflow."
            )
    else:
        lines.append("_No volume backups created._")
    lines.append("")

    # Retention cleanup
    total_deleted = stats.instance_backups_deleted + stats.volume_backups_deleted
    lines.append(f"### 🗑️ Retention Cleanup - {total_deleted} deleted")
    lines.append("")
    if stats.deleted_instance_backups_list or stats.deleted_volume_backups_list:
        lines += ["| Backup | Type |", "|--------|------|"]
        for name in stats.deleted_instance_backups_list:
            lines.append(f"| {name} | 🖥️ Instance |")
        for name in stats.deleted_volume_backups_list:
            lines.append(f"| {name} | 💾 Volume |")
    else:
        lines.append("_No backups deleted._")
    lines.append("")

    # Errors
    if stats.errored_resources:
        lines.append(f"### ❌ Errors - {stats.errors}")
        lines.append("")
        lines += ["| Resource | Error |", "|----------|-------|"]
        for name, msg in stats.errored_resources:
            lines.append(f"| {name} | {msg} |")
        lines.append("")

    lines += [
        "---",
        "",
        f"{icon} **{status}** · {stats.instances_backed_up} instance(s) · {stats.volumes_backed_up} volume(s) · {stats.errors} error(s)",
    ]

    summary(*lines)


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
    """Ship a single trapper item marking that the backup run has started.

    Called from main() before authenticating against OpenStack so a crash
    during auth or imports still produces a recent run_started_at without a
    matching heartbeat, which a Zabbix trigger can detect within ~2 h.
    """
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return
    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue

        _make_zabbix_sender(ZABBIX_SERVER).send([ItemValue(host, "backup.run_started_at", int(time.time()))])
        print(f"Zabbix run-started ping sent to {ZABBIX_SERVER} for host {host}")
    except Exception as e:
        print(f"Warning: Failed to send Zabbix run-started ping: {e}")


def send_zabbix_metrics(duration: int):
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return

    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue

        sender = _make_zabbix_sender(ZABBIX_SERVER)
        sender.send(
            [
                ItemValue(host, "backup.instances.ok", stats.instances_backed_up),
                ItemValue(host, "backup.volumes.ok", stats.volumes_backed_up),
                ItemValue(host, "backup.errors", stats.errors),
                ItemValue(host, "backup.duration", duration),
                ItemValue(host, "backup.heartbeat", int(time.time())),
            ]
        )
        print(f"Zabbix metrics sent to {ZABBIX_SERVER} for host {host}")
    except Exception as e:
        print(f"Warning: Failed to send Zabbix metrics: {e}")


############################################################################
#  Entry point
############################################################################


async def run() -> None:
    now = datetime.datetime.now(datetime.UTC)
    expire_time = now - datetime.timedelta(days=RETENTION_DAYS)

    async with OpenStackSession() as session:
        cloud = await connect(session)
        server_names = await backup_instances(cloud)
        await backup_volumes(cloud, server_names)
        await delete_old_instance_backups(cloud, expire_time)
        await delete_old_volume_backups(cloud, expire_time)

    write_summary(now.strftime("%Y-%m-%d"))


def main():
    start_time = time.monotonic()
    # Emit run-started ping first thing so a crash during auth or imports
    # still produces a recent run_started_at: paired with the missing
    # backup.heartbeat at the end, a Zabbix trigger can detect a stuck or
    # crashed run within ~2 h instead of waiting for the 25 h nodata floor.
    send_zabbix_run_started()

    # The library narrates the snapshot sequence ("Step 1/5 ...") through
    # logging; surface it on stdout next to this script's own prints.
    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        asyncio.run(run())
    except CloudError as e:
        print(f"Error: {e}")
        stats.inc("errors")

    send_zabbix_metrics(int(time.monotonic() - start_time))

    if stats.errors:
        print(f"Finished with {stats.errors} error(s)!")
        sys.exit(1)
    print("Finished successfully!")


if __name__ == "__main__":
    main()
