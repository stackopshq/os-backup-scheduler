#!/usr/bin/env python3
"""
OpenStack Automatic Backup Script

Automated backup solution for OpenStack instances and volumes with
configurable retention policy. Volume backups run in parallel via
ThreadPoolExecutor — one OpenStack session, no per-command subprocess
overhead.

Repository: https://github.com/net-architect-cloud/os-backup-scheduler
License: Apache-2.0
"""

import datetime
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openstack
import openstack.exceptions

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


def get_connection() -> openstack.connection.Connection:
    required = ["OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD", "OS_PROJECT_NAME"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Error: Missing required environment variables: {' '.join(missing)}")
        print("Required: OS_AUTH_URL, OS_USERNAME, OS_PASSWORD, OS_PROJECT_NAME")
        print("Optional: OS_USER_DOMAIN_NAME, OS_PROJECT_DOMAIN_NAME, OS_REGION_NAME, OS_IDENTITY_API_VERSION")
        sys.exit(1)

    conn = openstack.connect(
        auth_url=os.environ["OS_AUTH_URL"],
        username=os.environ["OS_USERNAME"],
        password=os.environ["OS_PASSWORD"],
        project_name=os.environ["OS_PROJECT_NAME"],
        user_domain_name=os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
        project_domain_name=os.environ.get("OS_PROJECT_DOMAIN_NAME", "default"),
        identity_api_version=os.environ.get("OS_IDENTITY_API_VERSION", "3"),
        region_name=os.environ.get("OS_REGION_NAME"),
    )

    print("Verifying OpenStack connectivity...")
    try:
        conn.authorize()
    except openstack.exceptions.SDKException as e:
        print(f"Error: Failed to authenticate with OpenStack: {e}")
        sys.exit(1)
    print("Authentication successful.")
    return conn


def _wait(conn, resource, status="available", failures=None):
    """Wait for a volume or snapshot to reach a target status."""
    conn.block_storage.wait_for_status(
        resource,
        status=status,
        failures=failures or ["error", "error_deleting"],
        interval=10,
        wait=RESOURCE_TIMEOUT,
    )


def _wait_backup(conn, backup):
    """Wait for a backup to become available.

    Uses wait_for_backup() rather than the generic wait_for_status() because
    the block-storage proxy resolves Backup resources differently from Volume
    and Snapshot resources internally.
    """
    conn.block_storage.wait_for_backup(
        backup.id,
        status="available",
        failures=["error"],
        interval=30,
        wait=BACKUP_TIMEOUT,
    )


############################################################################
#  Instance backups
############################################################################


def backup_instances(conn: openstack.connection.Connection):
    print("-" * 40)
    print("Creating instance backups!")

    for server in conn.compute.servers(details=True):
        if (server.metadata or {}).get("autoBackup") != "true":
            print(f"Skipping instance (no autoBackup metadata): {server.name} - {server.id}")
            continue

        # BFV detection: server.image is None or {} for boot-from-volume instances;
        # image_id alone can be unreliable across API versions.
        if not server.image:
            print(
                f"Skipping instance {server.name}: boot-from-volume "
                "(backup the volume directly with autoBackup metadata)"
            )
            continue

        if server.task_state not in (None, "None"):
            print(f"Skipping instance {server.name}: busy (task_state: {server.task_state})")
            continue

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_name = f"autoBackup_{timestamp}_{server.name}"
        print(f"Instance {server.name} is boot-from-image, creating server backup")
        try:
            conn.compute.backup_server(server.id, backup_name, "daily", RETENTION_DAYS)
            stats.inc("instances_backed_up")
            stats.append("backed_instances", (server.name, backup_name))
        except Exception as e:
            print(f"Error: Failed to create backup for instance {server.name}: {e}")
            stats.inc("errors")
            stats.append("errored_resources", (server.name, str(e)))


############################################################################
#  Volume backups
############################################################################


def _cleanup_temp(conn, temp_volume=None, temp_snapshot=None):
    if temp_volume:
        try:
            conn.block_storage.delete_volume(temp_volume.id, ignore_missing=True)
            stats.inc("temp_volumes_cleaned")
        except Exception as e:
            print(f"Warning: Failed to delete temp volume {temp_volume.id}: {e}")
    if temp_snapshot:
        try:
            conn.block_storage.delete_snapshot(temp_snapshot.id, ignore_missing=True)
            stats.inc("snapshots_cleaned")
        except Exception as e:
            print(f"Warning: Failed to delete temp snapshot {temp_snapshot.id}: {e}")


def _backup_via_snapshot(conn, volume, volume_name: str, backup_name: str) -> bool:
    """Snapshot → temp volume → backup (avoids --force on attached volumes)."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    snapshot_name = f"temp_snap_{timestamp}_{volume_name}"
    temp_vol_name = f"temp_vol_{timestamp}_{volume_name}"
    temp_snapshot = None
    temp_volume = None

    try:
        print(f"  Step 1/5: Creating snapshot of {volume_name}...")
        temp_snapshot = conn.block_storage.create_snapshot(
            volume_id=volume.id,
            name=snapshot_name,
            is_forced=True,
        )
        stats.inc("snapshots_created")

        print("  Step 2/5: Waiting for snapshot...")
        _wait(conn, temp_snapshot)

        print("  Step 3/5: Creating temp volume from snapshot...")
        temp_volume = conn.block_storage.create_volume(
            name=temp_vol_name,
            snapshot_id=temp_snapshot.id,
        )
        stats.inc("temp_volumes_created")

        print("  Step 4/5: Waiting for temp volume...")
        _wait(conn, temp_volume)

        print("  Step 5/5: Creating backup from temp volume...")
        backup = conn.block_storage.create_backup(
            volume_id=temp_volume.id,
            name=backup_name,
        )
        print(f"  Backup initiated: {backup.id}")

        if WAIT_FOR_BACKUP:
            _wait_backup(conn, backup)
            _cleanup_temp(conn, temp_volume, temp_snapshot)
        else:
            print("  Async mode: cleanup deferred to verification workflow")
            print(f"    Temp snapshot: {temp_snapshot.id} ({snapshot_name})")
            print(f"    Temp volume:   {temp_volume.id} ({temp_vol_name})")

        return True

    except Exception as e:
        print(f"Error: Snapshot-based backup failed for {volume_name}: {e}")
        _cleanup_temp(conn, temp_volume, temp_snapshot)
        return False


def _backup_direct(conn, volume, volume_name: str, backup_name: str, force: bool = False) -> bool:
    try:
        backup = conn.block_storage.create_backup(
            volume_id=volume.id,
            name=backup_name,
            force=force,
        )
        print(f"Volume backup initiated: {backup_name} ({backup.id})")
        if WAIT_FOR_BACKUP:
            _wait_backup(conn, backup)
        return True
    except Exception as e:
        print(f"Error: Failed to backup volume {volume_name}: {e}")
        return False


def _volume_backup_task(conn, volume) -> bool:
    """Back up one volume. Runs in a thread-pool worker."""
    volume_name = volume.name
    if not volume_name:
        attachments = volume.attachments or []
        if attachments:
            try:
                volume_name = f"{conn.compute.get_server(attachments[0]['server_id']).name}_vol"
            except Exception:
                volume_name = volume.id[:8]
        else:
            volume_name = volume.id[:8]

    print(f"Processing volume: {volume_name} - {volume.id} (status: {volume.status})")

    if volume.status in ("backing-up", "creating", "deleting", "restoring-backup"):
        print(f"Error: Volume {volume_name} is in '{volume.status}' state — cannot create backup")
        return False

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"autoBackup_{timestamp}_{volume_name}"

    if USE_SNAPSHOT_METHOD and volume.status == "in-use":
        print(f"Using snapshot method for attached volume {volume_name}")
        method = "snapshot"
        success = _backup_via_snapshot(conn, volume, volume_name, backup_name)
    elif volume.status == "available":
        print(f"Creating direct backup for detached volume {volume_name}")
        method = "direct"
        success = _backup_direct(conn, volume, volume_name, backup_name)
    else:
        print(f"Using force method for volume {volume_name} (status: {volume.status})")
        method = "force"
        success = _backup_direct(conn, volume, volume_name, backup_name, force=True)

    if success:
        stats.append("backed_volumes", (volume_name, backup_name, method))
    else:
        stats.append("errored_resources", (volume_name, f"backup failed (method: {method})"))
    return success


def backup_volumes(conn: openstack.connection.Connection):
    print("-" * 40)
    print("Creating volume backups!")

    try:
        all_volumes = list(conn.block_storage.volumes(details=True))
        tagged = [v for v in all_volumes if (v.metadata or {}).get("autoBackup") == "true"]
    except openstack.exceptions.EndpointNotFound:
        print("Volume service not available in this region, skipping.")
        return

    if not tagged:
        print("No volumes with autoBackup=true found.")
        return

    print(f"Found {len(tagged)} volume(s) — running up to {BACKUP_CONCURRENCY} in parallel.")

    with ThreadPoolExecutor(max_workers=BACKUP_CONCURRENCY) as executor:
        futures = {executor.submit(_volume_backup_task, conn, vol): vol for vol in tagged}
        for future in as_completed(futures):
            vol = futures[future]
            try:
                success = future.result()
            except Exception as e:
                print(f"Error: Unexpected error for volume {vol.name or vol.id[:8]}: {e}")
                success = False
            stats.inc("volumes_backed_up" if success else "errors")


############################################################################
#  Retention cleanup
############################################################################


def _parse_ts(ts: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def delete_old_instance_backups(conn, expire_time: datetime.datetime):
    print("-" * 40)
    print("Deleting old instance backups!")

    for image in conn.image.images(visibility="private"):
        if not (image.name or "").startswith("autoBackup"):
            continue
        try:
            created_at = _parse_ts(image.created_at)
        except (AttributeError, ValueError):
            continue
        if created_at < expire_time:
            print(f"Deleting old instance backup: {image.name} ({image.id})")
            try:
                conn.image.delete_image(image.id, ignore_missing=True)
                stats.inc("instance_backups_deleted")
                stats.append("deleted_instance_backups_list", image.name)
            except Exception as e:
                print(f"Error: Failed to delete instance backup {image.id}: {e}")
                stats.inc("errors")
                stats.append("errored_resources", (image.name, str(e)))
        else:
            print(f"Skipping instance backup: {image.name}")


def delete_old_volume_backups(conn, expire_time: datetime.datetime):
    print("-" * 40)
    print("Deleting old volume backups!")

    try:
        backups = list(conn.block_storage.backups(details=True))
    except openstack.exceptions.EndpointNotFound:
        print("Volume backup service not available in this region, skipping.")
        return

    for backup in backups:
        if not (backup.name or "").startswith("autoBackup"):
            continue
        try:
            created_at = _parse_ts(backup.created_at)
        except (AttributeError, ValueError):
            continue
        if created_at < expire_time:
            print(f"Deleting old volume backup: {backup.name} ({backup.id})")
            try:
                conn.block_storage.delete_backup(backup.id, ignore_missing=True)
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
        f"## {icon} Backup Report — {REGION_NAME} — {date_str}",
        "",
        f"**Mode:** {'⏳ Async — temp resources will be cleaned up by the verification workflow' if not WAIT_FOR_BACKUP else '🔄 Sync — waited for each backup to complete'}",
        f"**Retention:** {RETENTION_DAYS}",
        "",
        "---",
        "",
    ]

    # Instance backups
    lines.append(f"### 🖥️ Instance Backups — {stats.instances_backed_up} backed up")
    lines.append("")
    if stats.backed_instances:
        lines += ["| Instance | Backup |", "|----------|--------|"]
        for name, bname in stats.backed_instances:
            lines.append(f"| {name} | {bname} |")
    else:
        lines.append("_No instance backups created._")
    lines.append("")

    # Volume backups
    lines.append(f"### 💾 Volume Backups — {stats.volumes_backed_up} backed up")
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
    lines.append(f"### 🗑️ Retention Cleanup — {total_deleted} deleted")
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
        lines.append(f"### ❌ Errors — {stats.errors}")
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


def send_zabbix_metrics(duration: int):
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return

    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue, Sender

        sender = Sender(server=ZABBIX_SERVER)
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


def main():
    start_time = time.monotonic()
    now = datetime.datetime.now(datetime.UTC)
    expire_time = now - datetime.timedelta(days=RETENTION_DAYS)

    conn = get_connection()

    backup_instances(conn)
    backup_volumes(conn)
    delete_old_instance_backups(conn, expire_time)
    delete_old_volume_backups(conn, expire_time)
    write_summary(now.strftime("%Y-%m-%d"))

    send_zabbix_metrics(int(time.monotonic() - start_time))

    if stats.errors:
        print(f"Finished with {stats.errors} error(s)!")
        sys.exit(1)
    print("Finished successfully!")


if __name__ == "__main__":
    main()
