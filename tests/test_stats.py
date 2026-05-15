"""Tests for the thread-safe Stats accumulator in openstack-backup.py.

Volume backups run in parallel via ThreadPoolExecutor. Stats has a Lock for
that exact reason; this regression test guards against someone "simplifying"
the lock away in a future refactor.
"""

from __future__ import annotations

import concurrent.futures


def test_stats_inc_single_threaded(backup_module):
    stats = backup_module.Stats()
    stats.inc("instances_backed_up")
    stats.inc("instances_backed_up", amount=4)
    assert stats.instances_backed_up == 5


def test_stats_append(backup_module):
    stats = backup_module.Stats()
    stats.append("backed_instances", ("server-a", "backup-1"))
    stats.append("backed_instances", ("server-b", "backup-2"))
    assert stats.backed_instances == [("server-a", "backup-1"), ("server-b", "backup-2")]


def test_stats_inc_is_thread_safe(backup_module):
    # 10 threads × 1000 increments must equal 10_000. Without the lock this
    # would race under CPython's GIL release between read-modify-write.
    stats = backup_module.Stats()
    workers = 10
    iterations = 1000

    def hammer():
        for _ in range(iterations):
            stats.inc("volumes_backed_up")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(hammer) for _ in range(workers)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    assert stats.volumes_backed_up == workers * iterations
