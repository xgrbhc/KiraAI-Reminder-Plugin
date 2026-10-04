"""Characterization tests for interfaces preserved by structural refactoring."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
from urllib.parse import urljoin
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient

from core.plugin.plugin_registry import PluginComponents, _plugin_components

from _loader import PLUGIN_DIR, load_plugin_module
from conftest import attach_delivery
from _events import batch, message, observe


TOOL_NAMES = {
    "list_pending_reminder_requests",
    "confirm_reminder_request",
    "list_message_sources",
    "set_reminder",
    "list_reminders",
    "delete_reminder",
    "confirm_delete_reminder",
    "mark_reminder_important",
    "unmark_reminder_important",
    "pause_reminder",
    "resume_reminder",
    "edit_reminder",
    "list_autonomous_intents",
    "create_autonomous_intent",
    "update_autonomous_intent",
    "close_autonomous_intent",
    "schedule_intent_followup",
    "list_delivery_issues",
    "review_delivery_issue",
}


def test_manifest_requires_core_with_provider_failure_hook():
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["core_version"] == ">=2.24.2"


def test_dashboard_assets_resolve_from_folder_page():
    web_dir = PLUGIN_DIR / "web"
    app = FastAPI()
    app.mount(
        "/page/plugin/reminder_plugin/dashboard",
        StaticFiles(directory=str(web_dir), html=True),
    )

    with TestClient(app) as client:
        page = client.get("/page/plugin/reminder_plugin/dashboard", follow_redirects=True)
        assert page.status_code == 200
        assert page.url.path.endswith("/dashboard/")
        assert '<link rel="stylesheet" href="./style.css">' in page.text
        assert '<script src="./app.js"></script>' in page.text
        assert "<style>" not in page.text
        assert "<script>" not in page.text

        style = client.get(urljoin(str(page.url), "./style.css"))
        script = client.get(urljoin(str(page.url), "./app.js"))
        assert style.status_code == 200
        assert script.status_code == 200
        assert "text/css" in style.headers["content-type"]
        assert "javascript" in script.headers["content-type"]
        assert ".glass-panel" in style.text
        assert "window.PluginPageContext.ready()" in script.text

        resources = re.findall(r'(?:src|href)="([^"]+)"', page.text)
        assert resources and all(resource.startswith("./") for resource in resources)
        for resource in resources:
            asset = client.get(urljoin(str(page.url), resource))
            assert asset.status_code == 200, resource
            assert asset.content, resource
            if resource.endswith(".css"):
                for font in re.findall(r'url\([\'"]?([^\)\'"\s]+)[\'"]?\)', asset.text):
                    font_response = client.get(urljoin(str(asset.url), font))
                    assert font_response.status_code == 200, font
                    assert font_response.content, font
                    # Windows MIME databases may serve fonts as binary streams.
                    mime = font_response.headers["content-type"]
                    assert "font" in mime or mime == "application/octet-stream", font


def test_registered_entry_points(reminder_main):
    components = _plugin_components["reminder_plugin"]
    assert set(components.tools) == TOOL_NAMES
    assert all(tool["parameters"]["type"] == "object" for tool in components.tools.values())
    assert len(components.hooks) == 7
    assert {hook.handler.__name__ for hook in components.hooks} == {
        "inject_usage_prompt",
        "enforce_autonomy_tool_policy",
        "handle_quick_command",
        "inject_delivery_issues",
        "observe_provider_failure",
        "acknowledge_delivery",
        "observe_reminder_confirmation",
    }
    assert [(page["route"], page["auth"]) for page in components.pages] == [
        ("/dashboard", True)
    ]
    assert {(route["method"], route["path"], route["auth"]) for route in components.api_routes} == {
        ("GET", "/sessions", True),
        ("GET", "/reminders/{session_id}", True),
        ("POST", "/reminders/confirm-delete", True),
        ("POST", "/reminders/{action}", True),
        ("GET", "/deliveries/{session_id}", True),
        ("POST", "/deliveries/{decision}", True),
    }
    assert components.tool_funcs["set_reminder"] is reminder_main.ReminderPlugin.set_reminder


def test_fresh_main_import_keeps_single_registration_set(reminder_main):
    package_name = "reminder_plugin_reload_contract_tests"
    original = _plugin_components["reminder_plugin"]
    try:
        for _ in range(2):
            _plugin_components["reminder_plugin"] = PluginComponents()
            sys.modules.pop(f"{package_name}.main", None)
            load_plugin_module("main", package_name=package_name)
            components = _plugin_components["reminder_plugin"]
            assert set(components.tools) == TOOL_NAMES
            assert len(components.hooks) == 7
            assert len(components.pages) == 1
            assert len(components.api_routes) == 6
    finally:
        _plugin_components["reminder_plugin"] = original
        for module_name in tuple(sys.modules):
            if module_name == package_name or module_name.startswith(f"{package_name}."):
                sys.modules.pop(module_name, None)


def test_config_compatibility_and_validation(reminder_main):
    config = reminder_main.ReminderConfig(
        admin_users="10001，10002\n10003",
        group_create_policy="invalid",
        action_policy="invalid",
        random_check_daily_count=99,
        daily_reflection_hour=-1,
    )
    assert config.admin_users == ["10001", "10002", "10003"]
    assert config.group_create_policy == "admin_only"
    assert config.action_policy == "admin_and_trusted_bot"
    assert config.random_check_daily_count == 3
    assert config.daily_reflection_hour == 0

    legacy = {"autonomy_mode": "off", "advanced_config": {"autonomy_mode": "observe"}}
    assert reminder_main.ReminderPlugin._flatten_config(legacy)["autonomy_mode"] == "observe"
    schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
    assert "advanced_config" in schema


def test_time_helper_contract(reminder_main):
    start = reminder_main.parse_time_string("2026-09-30 10:15")
    assert start == dt.datetime(2026, 9, 30, 10, 15)
    assert reminder_main.determine_random_count(random_count=2) == 2
    assert reminder_main.generate_multiple_random_times(start, start, 2) == []


def test_legacy_json_round_trip_in_temp_directory(reminder_main, tmp_path: Path):
    path = tmp_path / "reminders.json"
    legacy = {
        "qq:dm:10001": [
            {"job_id": "legacy-1", "content": "旧提醒", "time": "2026-10-01 08:30", "repeat": "none"}
        ]
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    storage = reminder_main.ReminderStorage(path)

    async def round_trip():
        assert await storage.load() == legacy
        async with storage.modify() as reminders:
            assert reminders == legacy

    asyncio.run(round_trip())
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


def test_delete_dispatch_from_tool_api_and_quick_command(reminder_main, tmp_path: Path):
    sid = "qq:dm:10001"

    async def run_entry(entry: str, allowed: bool):
        path = tmp_path / f"{entry}-{allowed}" / "reminders.json"
        storage = reminder_main.ReminderStorage(path)
        await storage.save({sid: [{"job_id": "job-1", "content": "test"}]})
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = storage
        plugin._scheduler = None
        plugin._pending = {}
        plugin.config = SimpleNamespace(admin_users=[])
        plugin._cleanup_tokens = lambda: None
        plugin._get_sid = lambda _event: sid
        plugin._check_permission = lambda *_args: allowed
        plugin._get_principal = lambda _event: reminder_main.PrincipalContext(
            reminder_main.PrincipalKind.WEB, "web-admin", trusted=True,
        )
        expected = "已删除: test" if allowed else "❌ 权限拒绝：您无权操作该任务 (创建人: 未知)"

        if entry == "tool":
            response = await plugin.delete_reminder(SimpleNamespace(), job_id="job-1")
            assert response == expected
        elif entry == "api":
            plugin._build_web_event = lambda _sid: SimpleNamespace()
            response = await plugin.api_action_reminders(
                "delete", {"session_id": sid, "job_id": "job-1"}
            )
            assert response == {"status": "ok" if allowed else "error", "msg": expected}
        else:
            sent = []

            async def send_message_chain(**kwargs):
                sent.append(kwargs)

            stopped = []
            event = SimpleNamespace(
                message=SimpleNamespace(chain=[reminder_main.Text("/rm 1")]),
                is_stopped=False,
                discard=lambda **kwargs: stopped.append(("discard", kwargs)),
                stop=lambda: stopped.append(("stop", {})),
            )
            plugin.ctx = SimpleNamespace(
                message_processor=SimpleNamespace(send_message_chain=send_message_chain)
            )
            plugin._is_group_event = lambda _event: False
            await plugin.handle_quick_command(event)
            assert sent[0]["session"] == sid
            assert sent[0]["chain"][0].text == expected
            assert stopped == [("discard", {"force": True}), ("stop", {})]

        remaining = [] if allowed else [{"job_id": "job-1", "content": "test"}]
        assert await storage.load() == {sid: remaining}

    for entry in ("tool", "api", "quick"):
        for allowed in (True, False):
            asyncio.run(run_entry(entry, allowed))


def test_important_delete_requires_actor_bound_confirmation(reminder_main, tmp_path: Path):
    sid = "qq:dm:10001"
    record = {
        "job_id": "job-1",
        "content": "important task",
        "important": True,
        "owner_type": "user",
        "owner_id": "10001",
        "creator_id": "10001",
    }

    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        await plugin._storage.save({sid: [record]})
        plugin._pending = {}
        plugin._scheduler = None
        plugin.config = SimpleNamespace(admin_users=[])
        plugin._get_sid = lambda _event: sid
        plugin._check_permission = lambda *_args: True
        plugin._is_admin_user = lambda _event: False
        plugin._identity = reminder_main.IdentityResolver("test-secret")
        owner_event = batch(message("10001", group=None), adapter="qq")
        other_event = batch(message("20002", group=None), adapter="qq")

        response = await plugin.delete_reminder(owner_event, job_id="job-1")
        assert "待确认请求" in response
        token = json.loads(await plugin.list_pending_reminder_requests(owner_event))[0]["request_id"]
        assert "确认未完成" in await plugin.confirm_delete_reminder(other_event, token)
        assert "确认未完成" in await plugin.confirm_delete_reminder(owner_event, token)
        assert await plugin._storage.load() == {sid: [record]}
        confirmed = await observe(plugin, message("10001", "确认删除", group=None), "qq")
        assert await plugin.confirm_delete_reminder(confirmed, token) == "已删除: important task"
        assert await plugin._storage.load() == {sid: []}

    asyncio.run(run())


def test_list_reminders_preserves_visible_fields(reminder_main, tmp_path: Path):
    sid = "qq:dm:10001"

    async def run():
        plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
        plugin._storage = reminder_main.ReminderStorage(tmp_path / "reminders.json")
        attach_delivery(plugin, reminder_main, tmp_path)
        plugin._pending = {}
        plugin._scheduler = None
        plugin.config = SimpleNamespace(admin_users=[])
        plugin._get_sid = lambda _event: sid
        plugin._is_admin_user = lambda _event: False
        plugin._get_principal = lambda _event: reminder_main.PrincipalContext(
            kind=reminder_main.PrincipalKind.USER,
            principal_id="10001",
            session_id=sid,
        )
        plugin._allowed_autonomy_sessions = lambda: []
        await plugin._storage.save({sid: [{
            "job_id": "job-1",
            "content": "test",
            "time": "2026-10-01 08:30",
            "repeat": "daily",
            "important": True,
            "paused": True,
            "creator_name": "Alice",
            "owner_type": "user",
            "owner_id": "10001",
        }]})
        response = await plugin.list_reminders(SimpleNamespace())
        assert response == (
            "📋 可访问待办列表：\n\n"
            "1. test ⭐重要 ⏸️已暂停\n"
            "   时间: 2026-10-01 08:30 [每天]\n"
            "   job_id: job-1\n"
            "   创建人: Alice"
        )

    asyncio.run(run())
