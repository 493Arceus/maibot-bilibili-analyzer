from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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
ANALYSIS_FIELDS = {
    "analysis_blacklist",
    "analysis_cover_images_size",
    "analysis_desc_blacklist",
    "analysis_display_image",
    "analysis_display_image_list",
    "analysis_enable_search",
    "analysis_group_blacklist",
    "analysis_group_whitelist",
    "analysis_images_size",
    "analysis_reanalysis_time",
    "analysis_request_timeout",
    "analysis_trust_env",
    "analysis_use_on_message",
    "analysis_whitelist",
}
LEGACY_NON_DEFAULTS = {
    "analysis_images_size": "640w_80q",
    "analysis_cover_images_size": "960w_85q",
    "analysis_whitelist": ["user-allow"],
    "analysis_group_whitelist": ["group-allow"],
    "analysis_blacklist": ["user-deny"],
    "analysis_group_blacklist": ["group-deny"],
    "analysis_desc_blacklist": ["group-no-desc"],
    "analysis_reanalysis_time": 37,
    "analysis_display_image": True,
    "analysis_display_image_list": ["video", "dynamic"],
    "analysis_enable_search": True,
    "analysis_use_on_message": True,
    "analysis_trust_env": True,
    "analysis_request_timeout": 33.5,
}


def _passthrough_decorator(*args: object, **kwargs: object):
    del args, kwargs

    def decorator(func):
        return func

    return decorator


def _merge_missing(target: dict[str, object], defaults: Mapping[str, object]) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, Mapping):
            _merge_missing(target[key], value)


def _runner_values_are_compatible(target: object, source: object) -> bool:
    if target is None:
        return True
    if isinstance(target, bool):
        return isinstance(source, bool)
    if isinstance(target, str):
        return isinstance(source, str)
    if isinstance(target, list):
        return isinstance(source, list)
    if isinstance(target, int):
        return isinstance(source, int) and not isinstance(source, bool)
    if isinstance(target, float):
        return isinstance(source, (int, float)) and not isinstance(source, bool)
    return isinstance(source, type(target))


def _runner_rebuild(
    defaults: Mapping[str, object], current: Mapping[str, object]
) -> dict[str, object]:
    rebuilt = deepcopy(dict(defaults))
    for key, value in current.items():
        if key not in rebuilt or key == "config_version":
            continue
        target = rebuilt[key]
        if isinstance(target, dict) and isinstance(value, Mapping):
            rebuilt[key] = _runner_rebuild(target, value)
        elif _runner_values_are_compatible(target, value):
            rebuilt[key] = deepcopy(value)
    return rebuilt


def _load_plugin_module():
    sdk_module = ModuleType("maibot_sdk")

    class MaiBotPlugin:
        config_model = BaseModel

        @classmethod
        def build_default_config(cls) -> dict[str, object]:
            return cls.config_model().model_dump(mode="python")

        @classmethod
        def build_config_schema(cls, **kwargs: object) -> dict[str, object]:
            del kwargs
            sections = {}
            for name, field in cls.config_model.model_fields.items():
                annotation = field.annotation
                if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                    sections[name] = {
                        "name": name,
                        "fields": {
                            field_name: {} for field_name in annotation.model_fields
                        },
                    }
            return {"sections": sections}

        def __init__(self) -> None:
            self.config = self.config_model()
            self.ctx = SimpleNamespace(logger=Mock())

        def normalize_plugin_config(
            self, config_data: Mapping[str, object] | None
        ) -> tuple[dict[str, object], bool]:
            raw_config = deepcopy(dict(config_data or {}))
            merged_config = deepcopy(type(self).build_default_config())
            _merge_missing(raw_config, merged_config)
            normalized = self.config_model.model_validate(raw_config).model_dump(
                mode="python"
            )
            return normalized, normalized != config_data

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


def _config_with_ttl(ttl: int):
    return plugin_module.BilibiliPluginConfig(
        analysis=plugin_module.AnalysisSectionConfig(analysis_reanalysis_time=ttl)
    )


def test_release_manifest_preserves_config_schema_version() -> None:
    manifest = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == "1.0.7"
    assert manifest["host_application"] == {
        "min_version": "1.2.0",
        "max_version": "1.3.99",
    }
    assert manifest["i18n"] == {"default_locale": "zh-CN"}
    assert plugin_module.PluginSectionConfig().config_version == "1.0.6"


def test_schema_exposes_plugin_and_analysis_sections() -> None:
    schema = plugin_module.BilibiliAnalyzerPlugin.build_config_schema()

    assert set(schema["sections"]) == {"plugin", "analysis"}
    assert schema["sections"]["analysis"]["name"] == "analysis"
    assert set(schema["sections"]["analysis"]["fields"]) == ANALYSIS_FIELDS


def test_new_install_transports_defaults_then_normalizes_to_nested_config() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    defaults = plugin.build_default_config()

    assert "analysis" not in defaults
    assert {key: defaults[key] for key in ANALYSIS_FIELDS} == (
        plugin_module.AnalysisSectionConfig().model_dump(mode="python")
    )

    runner_config = _runner_rebuild(defaults, {})
    normalized, changed = plugin.normalize_plugin_config(runner_config)

    assert changed is True
    assert normalized == plugin_module.BilibiliPluginConfig().model_dump(mode="python")
    assert ANALYSIS_FIELDS.isdisjoint(normalized)


def test_all_legacy_root_fields_survive_runner_version_rebuild_and_migrate() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    legacy_config = {
        "plugin": {"enabled": False, "config_version": "1.0.5"},
        **deepcopy(LEGACY_NON_DEFAULTS),
    }
    runner_rebuilt = _runner_rebuild(plugin.build_default_config(), legacy_config)

    assert {key: runner_rebuilt[key] for key in ANALYSIS_FIELDS} == LEGACY_NON_DEFAULTS
    assert runner_rebuilt["plugin"] == {
        "enabled": False,
        "config_version": "1.0.6",
    }

    normalized, changed = plugin.normalize_plugin_config(runner_rebuilt)

    assert changed is True
    assert normalized["plugin"] == {"enabled": False, "config_version": "1.0.6"}
    assert normalized["analysis"] == LEGACY_NON_DEFAULTS
    assert ANALYSIS_FIELDS.isdisjoint(normalized)


def test_same_version_nested_config_is_preserved() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    current = plugin_module.BilibiliPluginConfig(
        plugin=plugin_module.PluginSectionConfig(enabled=False),
        analysis=plugin_module.AnalysisSectionConfig(**LEGACY_NON_DEFAULTS),
    ).model_dump(mode="python")

    runner_config = _runner_rebuild(current, {})
    normalized, changed = plugin.normalize_plugin_config(runner_config)

    assert changed is False
    assert normalized == current
    assert ANALYSIS_FIELDS.isdisjoint(normalized)


def test_version_rebuild_only_preserves_fields_in_default_skeleton() -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    mixed_old_config = {
        "plugin": {"enabled": True, "config_version": "1.0.5"},
        "analysis": {"analysis_display_image": False},
        "analysis_display_image": True,
    }

    runner_rebuilt = _runner_rebuild(plugin.build_default_config(), mixed_old_config)
    normalized, _ = plugin.normalize_plugin_config(runner_rebuilt)

    assert "analysis" not in runner_rebuilt
    assert normalized["analysis"]["analysis_display_image"] is True
    assert ANALYSIS_FIELDS.isdisjoint(normalized)


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


def test_duplicate_cache_honors_ttl(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    plugin.config = _config_with_ttl(10)
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(plugin_module, "time", SimpleNamespace(monotonic=clock))

    assert asyncio.run(plugin._is_duplicate("stream", "url")) is False
    clock.return_value = 109.9
    assert asyncio.run(plugin._is_duplicate("stream", "url")) is True
    clock.return_value = 110.0
    assert asyncio.run(plugin._is_duplicate("stream", "url")) is False


def test_zero_ttl_only_suppresses_the_immediately_previous_url(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    plugin.config = _config_with_ttl(0)
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=1.0))
    )

    async def check_sequence() -> list[bool]:
        return [
            await plugin._is_duplicate("stream", "A"),
            await plugin._is_duplicate("stream", "A"),
            await plugin._is_duplicate("stream", "B"),
            await plugin._is_duplicate("stream", "A"),
        ]

    assert asyncio.run(check_sequence()) == [False, True, False, False]
    assert plugin._seen_urls == {"stream": {"A": 1.0}}


def test_global_cleanup_runs_at_the_sixty_second_boundary(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    plugin.config = _config_with_ttl(10)
    plugin._seen_urls = {
        "stale": {"old": 1.0},
        "mixed": {"old": 1.0, "fresh": 56.0},
    }
    plugin._last_cache_cleanup = 0.0
    clock = Mock(return_value=59.9)
    monkeypatch.setattr(plugin_module, "time", SimpleNamespace(monotonic=clock))

    assert asyncio.run(plugin._is_duplicate("active", "before-boundary")) is False
    assert plugin._seen_urls["stale"] == {"old": 1.0}
    clock.return_value = 60.0
    assert asyncio.run(plugin._is_duplicate("active", "at-boundary")) is False

    assert "stale" not in plugin._seen_urls
    assert plugin._seen_urls["mixed"] == {"fresh": 56.0}
    assert plugin._last_cache_cleanup == 60.0


def test_cache_enforces_stream_lru_limit(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    plugin.config = _config_with_ttl(3600)
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=1.0))
    )

    async def fill_cache() -> None:
        for index in range(1024):
            assert (
                await plugin._is_duplicate(f"stream-{index}", f"url-{index}") is False
            )
        assert await plugin._is_duplicate("stream-0", "url-0") is True
        assert await plugin._is_duplicate("stream-overflow", "url-overflow") is False

    asyncio.run(fill_cache())

    assert len(plugin._seen_urls) == 1024
    assert "stream-0" in plugin._seen_urls
    assert "stream-1" not in plugin._seen_urls
    assert "stream-overflow" in plugin._seen_urls


def test_cache_evicts_oldest_url_within_stream(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    plugin.config = _config_with_ttl(3600)
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=1.0))
    )

    async def fill_cache() -> None:
        for index in range(129):
            assert await plugin._is_duplicate("stream", f"url-{index}") is False

    asyncio.run(fill_cache())

    assert len(plugin._seen_urls["stream"]) == 128
    assert "url-0" not in plugin._seen_urls["stream"]
    assert "url-128" in plugin._seen_urls["stream"]


def test_self_config_update_uses_injected_nested_config_and_resets_state(
    monkeypatch,
) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    old_client = SimpleNamespace(close=AsyncMock())
    new_client = object()
    plugin._client = old_client
    plugin._seen_urls = {"stream": {"https://b23.tv/example": 1.0}}
    plugin._last_cache_cleanup = 1.0
    hot_update = {
        "plugin": {"enabled": True, "config_version": "1.0.6"},
        "analysis": {
            "analysis_request_timeout": 30.0,
            "analysis_trust_env": True,
        },
    }
    normalized, _ = plugin.normalize_plugin_config(hot_update)
    plugin.config = plugin_module.BilibiliPluginConfig.model_validate(normalized)
    observed_client_config = []

    def create_client():
        observed_client_config.append(
            (
                plugin.config.analysis.analysis_request_timeout,
                plugin.config.analysis.analysis_trust_env,
            )
        )
        return new_client

    plugin._create_client = Mock(side_effect=create_client)
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=77.0))
    )

    asyncio.run(plugin.on_config_update("self", hot_update, "1.0.6"))

    assert plugin._client is new_client
    plugin._create_client.assert_called_once_with()
    assert observed_client_config == [(30.0, True)]
    old_client.close.assert_awaited_once_with()
    assert plugin._seen_urls == {}
    assert plugin._last_cache_cleanup == 77.0
    plugin.ctx.logger.info.assert_called_once_with(
        "Bilibili 解析配置已热更新: config_version=%s，HTTP 客户端已重建，重复解析缓存已清空",
        "1.0.6",
    )


def test_self_config_update_ignores_old_client_close_error_and_resets_state(
    monkeypatch,
) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    old_client = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("旧客户端关闭失败"))
    )
    new_client = object()
    plugin._client = old_client
    plugin._seen_urls = {"stream": {"https://b23.tv/example": 1.0}}
    plugin._last_cache_cleanup = 1.0
    plugin._create_client = Mock(return_value=new_client)
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=99.0))
    )

    asyncio.run(plugin.on_config_update("self", {}, "1.0.6"))

    assert plugin._client is new_client
    plugin._create_client.assert_called_once_with()
    old_client.close.assert_awaited_once_with()
    assert plugin._seen_urls == {}
    assert plugin._last_cache_cleanup == 99.0


def test_unload_closes_client_and_resets_cache(monkeypatch) -> None:
    plugin = plugin_module.BilibiliAnalyzerPlugin()
    client = SimpleNamespace(close=AsyncMock())
    plugin._client = client
    plugin._seen_urls = {"stream": {"https://b23.tv/example": 1.0}}
    plugin._last_cache_cleanup = 1.0
    monkeypatch.setattr(
        plugin_module, "time", SimpleNamespace(monotonic=Mock(return_value=88.0))
    )

    asyncio.run(plugin.on_unload())

    assert plugin._client is None
    client.close.assert_awaited_once_with()
    assert plugin._seen_urls == {}
    assert plugin._last_cache_cleanup == 88.0


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
