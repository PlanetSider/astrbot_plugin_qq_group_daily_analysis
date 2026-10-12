"""自用插件安装身份、运行命名空间和共享宿主资源隔离回归。"""

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot_plugin_qq_group_daily_analysis.src.infrastructure.scheduler.auto_scheduler import (
    AutoScheduler,
)
from astrbot_plugin_qq_group_daily_analysis.src.infrastructure.webui.plugin_page_bridge import (
    PluginPageWebUIBridge,
)
from astrbot_plugin_qq_group_daily_analysis.src.shared.constants import (
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    PLUGIN_NAME,
)

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_NAME = "astrbot_plugin_qq_group_daily_analysis"
PRIVATE_NAME = "astrbot_plugin_qq_group_daily_analysis_planetsider"


def test_installation_identity_matches_registry_and_runtime():
    """安装元数据、源记录和运行身份一致，模板资源仍使用上游仓库。"""
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "astrbot-plugins.json").read_text(encoding="utf-8"))
    plugin = registry[f"PlanetSider/{PRIVATE_NAME}"]
    assert metadata["name"] == plugin["name"] == PLUGIN_NAME == PRIVATE_NAME
    assert metadata["name"] != UPSTREAM_NAME
    for key in (
        "author",
        "version",
        "display_name",
        "repo",
        "astrbot_version",
        "support_platforms",
    ):
        assert plugin[key] == metadata[key]
    assert f"PlanetSider/{UPSTREAM_NAME}" not in registry
    assert plugin["download_url"].endswith("/feature/serverchan-summary-routing.zip")
    assert (GITHUB_REPO_OWNER, GITHUB_REPO_NAME) == ("SXP-Simon", UPSTREAM_NAME)


def test_entrypoint_imports_under_independent_installation_name(monkeypatch):
    """模拟按新目录名加载入口，避免仅在旧仓库包名下通过测试。"""
    package = ModuleType(PRIVATE_NAME)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, PRIVATE_NAME, package)
    try:
        entrypoint = importlib.import_module(f"{PRIVATE_NAME}.main")
        assert entrypoint.GroupDailyAnalysis.__module__ == f"{PRIVATE_NAME}.main"
        assert entrypoint.PLUGIN_NAME == PRIVATE_NAME
        assert entrypoint.TaskLifecycleService.__module__.startswith(f"{PRIVATE_NAME}.")
        assert entrypoint.AutoScheduler.__module__.startswith(f"{PRIVATE_NAME}.")
    finally:
        for name in list(sys.modules):
            if name.startswith(f"{PRIVATE_NAME}."):
                del sys.modules[name]


def test_scheduling_reload_and_unload_preserve_upstream_jobs():
    """共享 AstrBot 调度器中重载和卸载自用版时保留上游任务。"""
    scheduler = AsyncIOScheduler()
    upstream_id = f"{UPSTREAM_NAME}_trigger_0"
    upstream_job = scheduler.add_job(Mock(), "cron", hour=23, minute=55, id=upstream_id)
    owner = object.__new__(AutoScheduler)
    owner.config_manager = SimpleNamespace(
        get_auto_analysis_time=Mock(return_value=["23:55"])
    )
    owner.scheduler_job_ids = []
    context = SimpleNamespace(cron_manager=SimpleNamespace(scheduler=scheduler))

    for _ in range(2):
        owner._schedule_report_time_jobs(scheduler)
        assert owner.scheduler_job_ids == [f"{PRIVATE_NAME}_trigger_0"]
        assert scheduler.get_job(upstream_id) is upstream_job
        assert scheduler.get_job(f"{PRIVATE_NAME}_trigger_0") is not None
        owner.unschedule_jobs(context)
        assert scheduler.get_job(upstream_id) is upstream_job
        assert scheduler.get_job(f"{PRIVATE_NAME}_trigger_0") is None
        assert owner.scheduler_job_ids == []


def test_web_api_registration_preserves_upstream_routes(monkeypatch):
    """共享路由表中注册自用版仅写入新前缀，不替换上游接口。"""
    from astrbot_plugin_qq_group_daily_analysis.src.infrastructure.logging.plugin_log_buffer import (
        global_log_buffer,
    )

    upstream_path = f"/{UPSTREAM_NAME}/tasks/active"
    upstream_handler = Mock()
    registered = {(upstream_path, "GET"): upstream_handler}

    def register(path, handler, methods, description):
        assert description
        for method in methods:
            registered[(path, method)] = handler

    context = SimpleNamespace(register_web_api=Mock(side_effect=register))
    monkeypatch.setattr(global_log_buffer, "register_listener", Mock())
    bridge = PluginPageWebUIBridge(context, MagicMock(), MagicMock())
    bridge.register_routes()
    assert registered[(upstream_path, "GET")] is upstream_handler
    assert (
        registered[(f"/{PRIVATE_NAME}/tasks/active", "GET")]
        == bridge.task_routes.api_get_active_tasks
    )
    assert (
        registered[(f"/{PRIVATE_NAME}/config", "GET")]
        == bridge.config_routes.api_get_config
    )
    assert (
        registered[(f"/{PRIVATE_NAME}/openapi.json", "GET")]
        == bridge.api_get_openapi_spec
    )
    assert len(context.register_web_api.call_args_list) > 20
    assert all(
        call.args[0].startswith(f"/{PRIVATE_NAME}/")
        for call in context.register_web_api.call_args_list
    )
