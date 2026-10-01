"""Shared fixtures for the reminder plugin contract tests."""

import os
import sys
from pathlib import Path

import pytest

from _loader import PLUGIN_DIR, load_plugin_module


KIRA_ROOT = PLUGIN_DIR.parents[2]
if (KIRA_ROOT / "core").is_dir() and str(KIRA_ROOT) not in sys.path:
    sys.path.insert(0, str(KIRA_ROOT))
if Path.cwd() == PLUGIN_DIR:
    # KiraAI resolves its logging and data paths relative to the process cwd.
    os.chdir(KIRA_ROOT)


@pytest.fixture(scope="session")
def reminder_main():
    return load_plugin_module("main")


def attach_delivery(plugin, reminder_main, tmp_path: Path):
    plugin._delivery_storage = reminder_main.ReminderStorage(tmp_path / "delivery_state.json")
    plugin._delivery = reminder_main.DeliveryTracker(plugin._delivery_storage, plugin._storage)
    return plugin
