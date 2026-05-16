"""Unit tests for the ZABBIX_SERVER → Sender helper in both scripts."""

from __future__ import annotations

import types

import pytest

# Each parametrize entry: (input_string, expected_clusters_repr)
CASES = [
    pytest.param(
        "host.example",
        [[["host.example", 10051]]],
        id="single-host-no-port",
    ),
    pytest.param(
        "host.example:10051",
        [[["host.example", 10051]]],
        id="single-host-with-port",
    ),
    pytest.param(
        "host.example:9999",
        [[["host.example", 9999]]],
        id="single-host-non-default-port",
    ),
    pytest.param(
        "10.9.0.15,10.8.0.15",
        [[["10.9.0.15", 10051], ["10.8.0.15", 10051]]],
        id="two-hosts-comma-no-port",
    ),
    pytest.param(
        "10.9.0.15:10051,10.8.0.15:10051",
        [[["10.9.0.15", 10051], ["10.8.0.15", 10051]]],
        id="two-hosts-with-port",
    ),
    pytest.param(
        "  10.9.0.15  ,  10.8.0.15  ",
        [[["10.9.0.15", 10051], ["10.8.0.15", 10051]]],
        id="whitespace-tolerant",
    ),
    pytest.param(
        "10.9.0.15,,",
        [[["10.9.0.15", 10051]]],
        id="trailing-and-empty-entries-dropped",
    ),
]


def _clusters(module: types.ModuleType, spec: str) -> list:
    # zabbix_utils returns a list of Cluster objects, each holding Node objects
    # with .address/.port. Normalise to plain Python so the comparison is stable
    # across zabbix_utils versions.
    raw = module._make_zabbix_sender(spec).clusters
    return [[[n.address, n.port] for n in cluster.nodes] for cluster in raw]


@pytest.mark.parametrize(("spec", "expected"), CASES)
def test_make_zabbix_sender_backup(backup_module, spec, expected):
    assert _clusters(backup_module, spec) == expected


@pytest.mark.parametrize(("spec", "expected"), CASES)
def test_make_zabbix_sender_verify(verify_module, spec, expected):
    assert _clusters(verify_module, spec) == expected
