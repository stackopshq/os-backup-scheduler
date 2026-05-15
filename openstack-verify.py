#!/usr/bin/env python3
"""
OpenStack Backup Verification Script

Verifies backup completion, detects stuck/failed backups, and cleans up
temporary resources left by async backup mode. Single authenticated session
via openstacksdk — no per-command subprocess overhead.

Repository: https://github.com/net-architect-cloud/os-backup-scheduler
License: Apache-2.0
"""

import datetime
import os
import sys
import time

import openstack
import openstack.exceptions

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
        f"### {icon} Instance Backups — {counts['active']} ✅ active · {counts['stuck']} ⚠️ stuck · {counts['error']} ❌ error",
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
        summary("### ℹ️ Volume Backups — service not available in this region", "")
        return counts
    if not all_backups:
        summary("### ℹ️ Volume Backups — no backups found", "")
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
        f"### {icon} Volume Backups — {counts['available']} ✅ available · {counts['stuck']} ⚠️ stuck · {counts['error']} ❌ error",
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
        summary("### ℹ️ Source Volumes — service not available in this region", "")
        return 0

    tagged = [v for v in all_volumes if (v.metadata or {}).get("autoBackup") == "true"]

    if not tagged:
        summary("### ✅ Source Volumes — no tagged volumes found", "")
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
        f"### {icon} Source Volumes — {len(tagged)} tagged, {stuck} stuck",
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


def cleanup_temp_resources(conn, all_volumes: list, all_backups: list) -> dict:
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
                conn.block_storage.delete_volume(vol.id, ignore_missing=True)
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
    try:
        all_snapshots = list(conn.block_storage.snapshots(details=True))
    except openstack.exceptions.EndpointNotFound:
        all_snapshots = None

    for snap in all_snapshots or []:
        name = snap.name or ""
        if not name.startswith("temp_snap_"):
            continue

        status = snap.status or ""
        if status == "available":
            print(f"Cleaning up temporary snapshot: {name} ({snap.id})")
            try:
                conn.block_storage.delete_snapshot(snap.id, ignore_missing=True)
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
    summary(f"### 🧹 Temporary Resources Cleanup — {total_cleaned} deleted", "")
    if rows:
        summary("| Resource | Type | Action |", "|----------|------|--------|", *rows)
    else:
        summary("_No temporary resources found._")
    if counts["errors"]:
        summary("", f"> ⚠️ {counts['errors']} cleanup error(s)")

    # Count temp_* resources that survived the cleanup (still consuming storage).
    # These are typically backups still in progress, stuck states, or delete failures.
    # Used to feed the verify.temp_count / verify.temp_gb Zabbix items.
    remaining_count = 0
    remaining_gb = 0
    try:
        for vol in conn.block_storage.volumes(details=True):
            if (vol.name or "").startswith("temp_"):
                remaining_count += 1
                remaining_gb += int(vol.size or 0)
        for snap in conn.block_storage.snapshots(details=True):
            if (snap.name or "").startswith("temp_"):
                remaining_count += 1
                remaining_gb += int(snap.size or 0)
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


def send_zabbix_metrics(total_success: int, total_stuck: int, total_error: int, temp_count: int = 0, temp_gb: int = 0):
    if not ZABBIX_SERVER or not ZABBIX_HOST:
        return

    host = f"{ZABBIX_HOST}-{REGION_NAME}"
    try:
        from zabbix_utils import ItemValue, Sender

        sender = Sender(server=ZABBIX_SERVER)
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


def main():
    today = datetime.date.today().isoformat()

    now_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    summary(f"## Verification Report — {REGION_NAME} — {now_str}", "")

    conn = get_connection()

    # Fetch shared resource lists once — passed to functions to avoid duplicate API calls.
    # None means the service endpoint is unavailable; [] means available but empty.
    all_images = list(conn.image.images(visibility="private"))

    try:
        all_volumes = list(conn.block_storage.volumes(details=True))
    except openstack.exceptions.EndpointNotFound:
        all_volumes = None

    try:
        all_backups = list(conn.block_storage.backups(details=True))
    except openstack.exceptions.EndpointNotFound:
        all_backups = None

    # Count tagged resources to know if backups are expected
    tagged_instances = [
        s for s in conn.compute.servers(details=True) if (s.metadata or {}).get("autoBackup") == "true" and s.image
    ]
    tagged_volumes = [v for v in (all_volumes or []) if (v.metadata or {}).get("autoBackup") == "true"]
    has_tagged_resources = bool(tagged_instances or tagged_volumes)

    img = check_instance_backups(all_images, today)
    vol = check_volume_backups(all_backups, today)
    stuck_source = check_source_volumes(all_volumes)
    temp = cleanup_temp_resources(conn, all_volumes, all_backups)

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
