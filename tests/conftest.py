"""Shared pytest fixtures.

The two production scripts use dashes in their filename (`openstack-backup.py`,
`openstack-verify.py`), which prevents a regular `import openstack_backup`.
We load them via importlib so the tests can exercise their public symbols.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name: str, path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def backup_module() -> types.ModuleType:
    return _load_module("os_backup", ROOT / "openstack-backup.py")


@pytest.fixture(scope="session")
def verify_module() -> types.ModuleType:
    return _load_module("os_verify", ROOT / "openstack-verify.py")
