from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import importlib.util
import json
import sys

from pydantic import BaseModel, Field


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "bilibili_analyzer_test_package"


def _passthrough_decorator(*args: object, **kwargs: object):
    del args, kwargs

    def decorator(func):
        return func

    return decorator


def _load_plugin_module():
    sdk_module = ModuleType("maibot_sdk")

    class MaiBotPlugin:
        config_model = BaseModel

        @classmethod
        def build_config_schema(cls, **kwargs: object) -> dict[str, object]:
            del kwargs
            general_fields = {
                name: {} for name in cls.config_model.model_fields if name != "plugin"
            }
            return {
                "sections": {
                    "plugin": {"name": "plugin", "fields": {}},
                    "general": {"name": "general", "fields": general_fields},
                }
            }

        def __init__(self) -> None:
            self.config = self.config_model()
            self.ctx = SimpleNamespace(logger=Mock())

    sdk_module.CONFIG_RELOAD_SCOPE_SELF = "self"
    sdk_module.Command = _passthrough_decorator
    sdk_module.Field = Field
    sdk_module.HookHandler = _passthrough_decorator
    sdk_module.MaiBotPlugin = MaiBotPlugin
    sdk_module.PluginConfigBase = BaseModel

    sdk_types_module = ModuleType("maibot_sdk.types")
    sdk_types_module.ErrorPolicy = SimpleNamespace(LOG="log")
    sdk_types_module.HookMode = SimpleNamespace(OBSERVE="observe")
    sdk_types_module.HookOrder = SimpleNamespace(NORMAL="normal")

    sys.modules["maibot_sdk"] = sdk_module
    sys.modules["maibot_sdk.types"] = sdk_types_module

    package_module = ModuleType(PACKAGE_NAME)
    package_module.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PACKAGE_NAME] = package_module

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.plugin", PLUGIN_ROOT / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_module = _load_plugin_module()


def test_manifest_and_default_config_versions_match() -> None:
    manifest = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "1.0.5"
    assert plugin_module.PluginSectionConfig().config_version == manifest["version"]


def test_general_schema_fields_map_to_config_root() -> None:
    schema = plugin_module.BilibiliAnalyzerPlugin.build_config_schema()
    general_section = schema["sections"]["general"]

    assert general_section["name"] == "."
    assert "analysis_display_image" in general_section["fields"]
    assert "analysis_enable_search" in general_section["fields"]


def test_message_context_reads_platform_card_payloads() -> None:
    card_payload = {
        "type": "miniapp_card",
        "payload": {
            "app": "com.tencent.miniapp_01",
            "meta": {
                "detail_1": {
                    "title": "哔哩哔哩",
                    "desc": "同名视频",
                    "qqdocurl": "https://b23.tv/Exact123",
                }
            },
        },
    }
    context = plugin_module.MessageContext.from_message(
        {
            "processed_plain_text": "[小程序] 哔哩哔哩：同名视频",
            "raw_message": [{"type": "text", "data": "[小程序] 哔哩哔哩：同名视频"}],
            "message_info": {
                "additional_config": {"platform_card_payloads": [card_payload]}
            },
        }
    )

    assert context.platform_card_payloads == [card_payload]
    assert "https://b23.tv/Exact123" in context.content_text(False)


def test_self_config_update_rebuilds_client_and_clears_cache() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    old_client = SimpleNamespace(close=AsyncMock())
    new_client = object()
    plugin._client = old_client
    plugin._seen_urls = {"stream": {"https://b23.tv/example": 1.0}}
    plugin.config = plugin_module.BilibiliPluginConfig(analysis_request_timeout=30.0)
    plugin._create_client = Mock(return_value=new_client)

    asyncio.run(plugin.on_config_update("self", {"analysis_request_timeout": 30.0}, ""))

    assert plugin._client is new_client
    plugin._create_client.assert_called_once_with()
    old_client.close.assert_awaited_once_with()
    assert plugin._seen_urls == {}
    plugin.ctx.logger.info.assert_called_once_with(
        "Bilibili 解析配置已热更新: config_version=%s，HTTP 客户端已重建，重复解析缓存已清空",
        "1.0.5",
    )


def test_other_scope_config_update_does_nothing() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    old_client = SimpleNamespace(close=AsyncMock())
    plugin._client = old_client
    plugin._seen_urls = {"stream": {"https://b23.tv/example": 1.0}}
    plugin._create_client = Mock()

    asyncio.run(plugin.on_config_update("dependency", {}, "1.0.2"))

    assert plugin._client is old_client
    plugin._create_client.assert_not_called()
    old_client.close.assert_not_awaited()
    assert plugin._seen_urls == {"stream": {"https://b23.tv/example": 1.0}}
    plugin.ctx.logger.info.assert_not_called()
