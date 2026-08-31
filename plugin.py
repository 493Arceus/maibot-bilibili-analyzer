from __future__ import annotations

from typing import Any, Literal

import asyncio
import base64
import time

from aiohttp import ClientSession, ClientTimeout
from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .bilibili import (
    BilibiliClient,
    BilibiliError,
    ParseOptions,
    ParsedContent,
    extract_message_text,
    matches_bilibili_content,
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用 Bilibili 解析")
    config_version: str = Field(default="1.0.5", description="配置版本")


class BilibiliPluginConfig(PluginConfigBase):
    """Bilibili 解析插件配置。"""

    __ui_label__ = "Bilibili 解析"
    __ui_icon__ = "video"

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    analysis_images_size: str = Field(
        default="", description="普通图片缩放参数，例如 480w_90q"
    )
    analysis_cover_images_size: str = Field(
        default="", description="封面图片缩放参数，例如 480w_90q"
    )
    analysis_whitelist: list[str] = Field(
        default_factory=list, description="允许使用插件的用户 ID 白名单"
    )
    analysis_group_whitelist: list[str] = Field(
        default_factory=list, description="允许使用插件的群 ID 白名单"
    )
    analysis_blacklist: list[str] = Field(
        default_factory=list, description="禁止使用插件的用户 ID 黑名单"
    )
    analysis_group_blacklist: list[str] = Field(
        default_factory=list, description="禁止使用插件的群 ID 黑名单"
    )
    analysis_desc_blacklist: list[str] = Field(
        default_factory=list, description="不显示简介的群 ID 列表"
    )
    analysis_reanalysis_time: int = Field(
        default=0, ge=0, description="同一聊天流重复解析的抑制秒数，0 表示只记住上一条"
    )
    analysis_display_image: bool = Field(
        default=False, description="是否显示全部支持类型的图片"
    )
    analysis_display_image_list: list[
        Literal["video", "bangumi", "live", "article", "dynamic"]
    ] = Field(
        default_factory=list,
        description="仅为指定内容类型显示图片",
    )
    analysis_enable_search: bool = Field(
        default=False, description="是否启用“搜视频 关键词”命令"
    )
    analysis_use_on_message: bool = Field(
        default=False,
        description="是否检查全部结构化消息内容；Bilibili 小程序卡片始终检查",
    )
    analysis_trust_env: bool = Field(
        default=False, description="是否继承系统代理环境变量"
    )
    analysis_request_timeout: float = Field(
        default=20.0, ge=3.0, le=60.0, description="Bilibili 请求超时秒数"
    )


class MessageContext:
    """从 Host 序列化消息中提取解析所需字段。"""

    __slots__ = (
        "group_id",
        "message_id",
        "plain_text",
        "platform_card_payloads",
        "raw_message",
        "stream_id",
        "user_id",
    )

    def __init__(
        self,
        *,
        group_id: str = "",
        message_id: str = "",
        plain_text: str = "",
        platform_card_payloads: list[dict[str, Any]] | None = None,
        raw_message: list[dict[str, Any]] | None = None,
        stream_id: str = "",
        user_id: str = "",
    ) -> None:
        self.group_id = group_id
        self.message_id = message_id
        self.plain_text = plain_text
        self.platform_card_payloads = platform_card_payloads or []
        self.raw_message = raw_message or []
        self.stream_id = stream_id
        self.user_id = user_id

    @classmethod
    def from_message(cls, message: Any, **kwargs: Any) -> MessageContext:
        payload = message if isinstance(message, dict) else {}
        message_info = (
            payload.get("message_info")
            if isinstance(payload.get("message_info"), dict)
            else {}
        )
        user_info = (
            message_info.get("user_info")
            if isinstance(message_info.get("user_info"), dict)
            else {}
        )
        group_info = (
            message_info.get("group_info")
            if isinstance(message_info.get("group_info"), dict)
            else {}
        )
        additional_config = (
            message_info.get("additional_config")
            if isinstance(message_info.get("additional_config"), dict)
            else {}
        )
        platform_card_payloads = (
            additional_config.get("platform_card_payloads")
            if isinstance(additional_config.get("platform_card_payloads"), list)
            else []
        )
        raw_message = (
            payload.get("raw_message")
            if isinstance(payload.get("raw_message"), list)
            else []
        )
        return cls(
            group_id=str(group_info.get("group_id") or kwargs.get("group_id") or ""),
            message_id=str(payload.get("message_id") or kwargs.get("message_id") or ""),
            plain_text=str(
                payload.get("processed_plain_text")
                or payload.get("plain_text")
                or kwargs.get("plain_text")
                or kwargs.get("text")
                or ""
            ),
            platform_card_payloads=[
                item for item in platform_card_payloads if isinstance(item, dict)
            ],
            raw_message=[item for item in raw_message if isinstance(item, dict)],
            stream_id=str(
                payload.get("session_id")
                or payload.get("stream_id")
                or kwargs.get("stream_id")
                or kwargs.get("session_id")
                or ""
            ),
            user_id=str(user_info.get("user_id") or kwargs.get("user_id") or ""),
        )

    def content_text(self, include_structured: bool) -> str:
        return extract_message_text(
            self.plain_text,
            self.raw_message,
            include_structured,
            self.platform_card_payloads,
        )


class BilibiliAnalyzerPlugin(MaiBotPlugin):
    """自动解析聊天消息中的 Bilibili 内容。"""

    config_model = BilibiliPluginConfig

    @classmethod
    def build_config_schema(
        cls,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, Any]:
        schema = super().build_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        sections = schema.get("sections")
        general_section = sections.get("general") if isinstance(sections, dict) else None
        if isinstance(general_section, dict):
            # SDK 的 general 是展示分区，而这些字段实际存储在 TOML 根级；
            # Dashboard 会过滤点路径中的空片段，因此 "." 明确映射到配置根级。
            general_section["name"] = "."
        return schema

    def __init__(self) -> None:
        super().__init__()
        self._client: BilibiliClient | None = None
        self._dedup_lock = asyncio.Lock()
        self._seen_urls: dict[str, dict[str, float]] = {}

    async def on_load(self) -> None:
        self._client = self._create_client()
        self.ctx.logger.info("Bilibili 解析插件已加载")

    async def on_unload(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()
        self._seen_urls.clear()

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del config_data, version
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return

        old_client = self._client
        self._client = self._create_client()
        if old_client is not None:
            await old_client.close()
        self._seen_urls.clear()
        self.ctx.logger.info(
            "Bilibili 解析配置已热更新: config_version=%s，HTTP 客户端已重建，重复解析缓存已清空",
            self.config.plugin.config_version,
        )

    @HookHandler(
        "chat.receive.after_process",
        name="bilibili_auto_analysis",
        description="自动解析消息中的 Bilibili 链接和内容编号",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=30000,
        error_policy=ErrorPolicy.LOG,
    )
    async def handle_bilibili_message(
        self, message: Any = None, **kwargs: Any
    ) -> dict[str, str]:
        context = MessageContext.from_message(message, **kwargs)
        text = context.content_text(self.config.analysis_use_on_message).strip()
        if not text or text.startswith("搜视频") or not matches_bilibili_content(text):
            return {"action": "continue"}
        if not context.stream_id or not self._is_allowed(context):
            return {"action": "continue"}

        await self._analyze_and_send(context, text)
        return {"action": "continue"}

    @Command(
        "bilibili_search",
        description="搜索 Bilibili 视频并解析第一个结果",
        pattern=r"^搜视频(?:\s+(?P<query>.+))?$",
    )
    async def handle_bilibili_search(
        self, stream_id: str = "", **kwargs: Any
    ) -> tuple[bool, str, int]:
        message = kwargs.pop("message", None)
        context = MessageContext.from_message(message, stream_id=stream_id, **kwargs)
        if not self.config.analysis_enable_search or not self._is_allowed(context):
            return False, "", 0

        matched_groups = kwargs.get("matched_groups")
        query = (
            str(matched_groups.get("query") or "").strip()
            if isinstance(matched_groups, dict)
            else ""
        )
        if not query:
            await self.ctx.send.text(
                "用法：搜视频 <关键词>", context.stream_id or stream_id
            )
            return False, "缺少搜索关键词", 2

        client = self._require_client()
        try:
            video_url = await client.search_video(query)
            if not video_url:
                await self.ctx.send.text(
                    "没有找到相关的 Bilibili 视频。", context.stream_id or stream_id
                )
                return False, "未找到视频", 2
            await self._analyze_and_send(context, video_url)
            return True, "已发送 Bilibili 搜索结果", 2
        except BilibiliError as exc:
            await self.ctx.send.text(
                f"Bilibili 搜索失败：{exc}", context.stream_id or stream_id
            )
            return False, str(exc), 2
        except Exception:
            self.ctx.logger.exception("Bilibili 搜索出现未预期错误")
            await self.ctx.send.text(
                "Bilibili 搜索失败，请稍后重试。", context.stream_id or stream_id
            )
            return False, "搜索出现未预期错误", 2

    def _create_client(self) -> BilibiliClient:
        timeout = ClientTimeout(
            total=self.config.analysis_request_timeout,
            connect=min(8.0, self.config.analysis_request_timeout),
        )
        session = ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
            },
            timeout=timeout,
            trust_env=self.config.analysis_trust_env,
        )
        return BilibiliClient(session, self.ctx.logger)

    def _require_client(self) -> BilibiliClient:
        if self._client is None:
            raise RuntimeError("Bilibili 客户端尚未初始化")
        return self._client

    def _parse_options(self, group_id: str) -> ParseOptions:
        return ParseOptions(
            cover_image_size=self.config.analysis_cover_images_size.strip(),
            display_all_images=self.config.analysis_display_image,
            display_image_types=frozenset(self.config.analysis_display_image_list),
            image_size=self.config.analysis_images_size.strip(),
            include_description=group_id
            not in {str(item) for item in self.config.analysis_desc_blacklist},
        )

    def _is_allowed(self, context: MessageContext) -> bool:
        user_whitelist = {str(item) for item in self.config.analysis_whitelist}
        group_whitelist = {str(item) for item in self.config.analysis_group_whitelist}
        if context.user_id in user_whitelist or (
            context.group_id and context.group_id in group_whitelist
        ):
            return True
        if user_whitelist or group_whitelist:
            return False

        user_blacklist = {str(item) for item in self.config.analysis_blacklist}
        group_blacklist = {str(item) for item in self.config.analysis_group_blacklist}
        return context.user_id not in user_blacklist and (
            not context.group_id or context.group_id not in group_blacklist
        )

    async def _analyze_and_send(self, context: MessageContext, text: str) -> None:
        client = self._require_client()
        try:
            content = await client.parse(text, self._parse_options(context.group_id))
            if content is None:
                return
            if await self._is_duplicate(context.stream_id, content.canonical_url):
                return
            await self._send_content(content, context.stream_id)
        except BilibiliError as exc:
            self.ctx.logger.warning("Bilibili 解析失败: %s", exc)
            await self.ctx.send.text(f"Bilibili 解析失败：{exc}", context.stream_id)
        except Exception:
            self.ctx.logger.exception("Bilibili 解析出现未预期错误")
            await self.ctx.send.text(
                "Bilibili 解析失败，请稍后重试。", context.stream_id
            )

    async def _is_duplicate(self, stream_id: str, canonical_url: str) -> bool:
        now = time.monotonic()
        ttl = self.config.analysis_reanalysis_time
        async with self._dedup_lock:
            stream_cache = self._seen_urls.setdefault(stream_id, {})
            if ttl <= 0:
                duplicate = canonical_url in stream_cache
                self._seen_urls[stream_id] = {canonical_url: now}
                return duplicate

            expired_urls = [
                url for url, seen_at in stream_cache.items() if now - seen_at >= ttl
            ]
            for url in expired_urls:
                del stream_cache[url]
            if canonical_url in stream_cache:
                return True
            stream_cache[canonical_url] = now
            return False

    async def _send_content(self, content: ParsedContent, stream_id: str) -> None:
        client = self._require_client()
        image_parts = [
            part for part in content.parts if part.kind == "image" and part.content
        ]
        image_results = await asyncio.gather(
            *(client.download_image(part.content) for part in image_parts),
            return_exceptions=True,
        )
        image_payloads: dict[str, str] = {}
        for part, result in zip(image_parts, image_results, strict=True):
            if isinstance(result, bytes):
                image_payloads[part.content] = base64.b64encode(result).decode("ascii")
            elif isinstance(result, Exception):
                self.ctx.logger.warning(
                    "Bilibili 图片下载失败: url=%s error=%s", part.content, result
                )

        segments: list[dict[str, str]] = []
        for part in content.parts:
            if part.kind == "text" and part.content:
                segments.append({"type": "text", "content": part.content})
            elif part.kind == "image" and part.content in image_payloads:
                segments.append(
                    {"type": "image", "content": image_payloads[part.content]}
                )

        text = content.plain_text
        if image_payloads:
            sent = await self.ctx.send.hybrid(segments, stream_id)
            if sent:
                return
            self.ctx.logger.warning("Bilibili 图文消息发送失败，降级为纯文本")
        if text:
            await self.ctx.send.text(text, stream_id)


def create_plugin() -> BilibiliAnalyzerPlugin:
    return BilibiliAnalyzerPlugin()
