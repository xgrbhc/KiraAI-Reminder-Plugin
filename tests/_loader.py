"""Load plugin modules under an isolated package name for tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin_module(name: str, package_name: str = "reminder_plugin_contract_tests"):
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[package_name] = package

    qualified_name = f"{package_name}.{name}"
    module = sys.modules.get(qualified_name)
    if module is not None:
        return module

    spec = importlib.util.spec_from_file_location(qualified_name, PLUGIN_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module
