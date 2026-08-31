from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from hashlib import md5
from logging import Logger
from time import localtime, strftime
from typing import Any, Literal, Mapping

import hashlib
import hmac
import html
import json
import re
import time
import urllib.parse

from aiohttp import ClientResponse, ClientSession


_TRIGGER_PATTERN = re.compile(
    r"(?:\b(?:av|cv)\d+\b|\bBV[A-Za-z0-9]{10}\b|"
    r"b23\.tv|bili(?:22|23|33|2233)\.cn|(?:[a-z0-9-]+\.)?bilibili\.com|"
    r"QQ小程序(?:&amp;#93;|&#93;|\])哔哩哔哩)",
    re.IGNORECASE,
)
_MINI_PROGRAM_TITLE_PATTERN = re.compile(
    r"\[\s*(?:QQ\s*)?小程序\s*\]\s*哔哩哔哩\s*[:：]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_SHORT_URL_PATTERN = re.compile(
    r"(?:https?://)?(?P<host>b23\.tv|bili(?:22|23|33|2233)\.cn)/(?P<path>[A-Za-z0-9]+)",
    re.IGNORECASE,
)
_MIXIN_KEY_ENC_TABLE = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DYNAMIC_FEATURES = (
    "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,"
    "onlyfansAssetsV2,ugcDelete,onlyfansQaCard,commentsNewVersion"
)
_OPUS_FEATURES = (
    "onlyfansVote,onlyfansAssetsV2,decorationCard,htmlNewStyle,ugcDelete,editable,"
    "opusPrivateVisible,tribeeEdit,avatarAutoTheme,avatarTypeOpus"
)


class BilibiliError(RuntimeError):
    """可安全展示给用户的 Bilibili 请求或解析错误。"""


@dataclass(frozen=True)
class ParseOptions:
    cover_image_size: str = ""
    display_all_images: bool = False
    display_image_types: frozenset[str] = frozenset()
    image_size: str = ""
    include_description: bool = True

    def displays_images_for(self, content_type: str) -> bool:
        return self.display_all_images or content_type in self.display_image_types


@dataclass(frozen=True)
class ContentPart:
    kind: Literal["text", "image"]
    content: str


@dataclass(frozen=True)
class ParsedContent:
    canonical_url: str
    parts: tuple[ContentPart, ...]

    @property
    def plain_text(self) -> str:
        return "".join(
            part.content for part in self.parts if part.kind == "text"
        ).strip()


@dataclass(frozen=True)
class ExtractedReference:
    api_url: str
    page: int | None = None
    time_location: int | None = None


def matches_bilibili_content(text: str) -> bool:
    return (
        _TRIGGER_PATTERN.search(text) is not None
        or _MINI_PROGRAM_TITLE_PATTERN.search(text) is not None
    )


def _structured_payload_text(value: Any, depth: int = 0) -> str:
    """展开结构化消息及其中嵌套的 JSON 字符串。"""
    if depth > 4:
        return ""

    parts: list[str] = []
    nested_values: list[Any] = []
    if isinstance(value, dict):
        parts.append(json.dumps(value, ensure_ascii=False))
        nested_values.extend(value.values())
    elif isinstance(value, list):
        parts.append(json.dumps(value, ensure_ascii=False))
        nested_values.extend(value)
    elif isinstance(value, str):
        normalized = html.unescape(value).replace("\\/", "/").strip()
        if not normalized:
            return ""
        parts.append(normalized)
        if normalized[:1] in {"{", "["}:
            try:
                nested_values.append(json.loads(normalized))
            except json.JSONDecodeError:
                pass
    else:
        return ""

    for nested_value in nested_values:
        nested_text = _structured_payload_text(nested_value, depth + 1)
        if nested_text:
            parts.append(nested_text)
    return "\n".join(dict.fromkeys(parts))


def extract_message_text(
    plain_text: str,
    raw_message: list[dict[str, Any]],
    include_structured: bool,
    platform_card_payloads: list[dict[str, Any]] | None = None,
) -> str:
    """提取待解析文本，并始终保留可识别的 Bilibili 结构化卡片。"""
    parts = [plain_text] if plain_text else []
    for segment in raw_message:
        segment_type = str(segment.get("type") or "").strip().lower()
        if segment_type == "forward":
            return ""
        if segment_type == "text":
            text = str(segment.get("data") or "").strip()
            if text:
                parts.append(text)
            continue
        if segment_type not in {"dict", "json"}:
            continue

        structured_text = _structured_payload_text(segment.get("data"))
        if structured_text and (
            include_structured or matches_bilibili_content(structured_text)
        ):
            parts.append(structured_text)

    card_payload_text = _structured_payload_text(platform_card_payloads or [])
    if card_payload_text and (
        include_structured or matches_bilibili_content(card_payload_text)
    ):
        parts.append(card_payload_text)
    return "\n".join(dict.fromkeys(parts))


def _text(content: str) -> ContentPart:
    return ContentPart("text", content)


def _image(content: str) -> ContentPart:
    return ContentPart("image", content)


def _handle_num(value: int) -> str:
    return f"{value / 10000:.2f}万" if value > 10000 else str(value)


def _resize_image(source: str, options: ParseOptions, *, is_cover: bool = False) -> str:
    source = _normalize_image_url(source)
    size = options.cover_image_size if is_cover else options.image_size
    if not size:
        return source
    suffix = source.rsplit(".", 1)[-1].split("?", 1)[0] if "." in source else "jpg"
    return f"{source}@{size}.{suffix}"


def _normalize_image_url(source: str) -> str:
    source = str(source or "").strip()
    if source.startswith("//"):
        return f"https:{source}"
    return source


def _extract_reference(text: str) -> ExtractedReference:
    page_match = re.search(r"(?:[?&]|&amp;)p=(\d+)", text, re.IGNORECASE)
    time_match = re.search(r"(?:[?&]|&amp;)t=(\d+)", text, re.IGNORECASE)
    page = int(page_match.group(1)) if page_match else None
    time_location = int(time_match.group(1)) if time_match else None

    bvid = re.search(r"\bBV[A-Za-z0-9]{10}\b", text, re.IGNORECASE)
    aid = re.search(r"\bav(\d+)\b", text, re.IGNORECASE)
    epid = re.search(r"\bep(\d+)\b", text, re.IGNORECASE)
    ssid = re.search(r"\bss(\d+)\b", text, re.IGNORECASE)
    mdid = re.search(r"\bmd(\d+)\b", text, re.IGNORECASE)
    room_id = re.search(
        r"live\.bilibili\.com/(?:blanc/|h5/)?(\d+)", text, re.IGNORECASE
    )
    cvid = re.search(
        r"(?:/read/(?:cv|mobile|native)(?:/|\?id=)?|\bcv)(\d+)\b", text, re.IGNORECASE
    )
    dynamic_type_two = re.search(
        r"(?:t|m)\.bilibili\.com/(\d+)\?(?:.*?)(?:&|&amp;)type=2",
        text,
        re.IGNORECASE,
    )
    dynamic_id = re.search(
        r"(?:(?:t|m)\.bilibili\.com/(?:opus/)?|www\.bilibili\.com/opus/)(\d+)",
        text,
        re.IGNORECASE,
    )

    if bvid:
        return ExtractedReference(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid.group(0)}",
            page,
            time_location,
        )
    if aid:
        return ExtractedReference(
            f"https://api.bilibili.com/x/web-interface/view?aid={aid.group(1)}",
            page,
            time_location,
        )
    if epid:
        return ExtractedReference(
            f"https://api.bilibili.com/pgc/view/web/season?ep_id={epid.group(1)}",
            time_location=time_location,
        )
    if ssid:
        return ExtractedReference(
            f"https://api.bilibili.com/pgc/view/web/season?season_id={ssid.group(1)}",
            time_location=time_location,
        )
    if mdid:
        return ExtractedReference(
            f"https://api.bilibili.com/pgc/review/user?media_id={mdid.group(1)}",
            time_location=time_location,
        )
    if room_id:
        return ExtractedReference(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
            f"?room_ids={room_id.group(1)}&req_biz=web_room_componet"
        )
    if cvid:
        article_id = cvid.group(1)
        return ExtractedReference(
            f"https://api.bilibili.com/x/article/viewinfo?id={article_id}&mobi_app=pc&from=web",
            page=int(article_id),
        )
    if dynamic_type_two:
        return ExtractedReference(
            f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?rid={dynamic_type_two.group(1)}&type=2"
        )
    if dynamic_id:
        return ExtractedReference(
            f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id.group(1)}"
        )
    return ExtractedReference("")


class BilibiliClient:
    """通过 Bilibili Web API 获取并格式化内容信息。"""

    def __init__(self, session: ClientSession, logger: Logger) -> None:
        self._session = session
        self._logger = logger

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()

    async def parse(self, text: str, options: ParseOptions) -> ParsedContent | None:
        normalized_text = text
        if _SHORT_URL_PATTERN.search(normalized_text):
            normalized_text = await self._follow_short_url(normalized_text)

        reference = _extract_reference(normalized_text)
        if not reference.api_url:
            return None

        if "/x/web-interface/view?" in reference.api_url:
            return await self._video_detail(reference, options)
        if "/pgc/" in reference.api_url:
            return await self._bangumi_detail(reference, options)
        if "/xlive/" in reference.api_url:
            return await self._live_detail(reference, options)
        if "/x/article/" in reference.api_url:
            return await self._article_detail(reference, options)
        if "/x/polymer/web-dynamic/" in reference.api_url:
            return await self._dynamic_detail(reference, options)
        return None

    async def search_video(self, title: str) -> str | None:
        visitor_cookies = await self._get_visitor_cookies()
        self._session.cookie_jar.update_cookies(visitor_cookies)
        await self._expect_success(await self._session.get("https://www.bilibili.com/"))
        query = await self._signed_query({"keyword": title})
        ticket = await self._get_ticket()
        self._session.cookie_jar.update_cookies({"bili_ticket": ticket})
        payload = await self._get_json(
            f"https://api.bilibili.com/x/web-interface/wbi/search/all/v2?{query}"
        )
        if payload.get("code") == -412:
            raise BilibiliError("搜索请求被 Bilibili 风控，请稍后重试")
        if payload.get("code") != 0:
            raise BilibiliError(
                str(payload.get("message") or "Bilibili 搜索接口返回错误")
            )

        data = payload.get("data")
        if isinstance(data, dict) and data.get("v_voucher"):
            raise BilibiliError("搜索请求需要额外验证，请稍后重试")
        result_groups = data.get("result", []) if isinstance(data, dict) else []
        for result_group in result_groups if isinstance(result_groups, list) else []:
            if (
                not isinstance(result_group, dict)
                or result_group.get("result_type") != "video"
            ):
                continue
            videos = result_group.get("data")
            if isinstance(videos, list) and videos and isinstance(videos[0], dict):
                url = videos[0].get("arcurl")
                return str(url) if url else None
        return None

    async def download_image(self, url: str) -> bytes:
        if not url:
            raise BilibiliError("图片地址为空")
        async with self._session.get(_normalize_image_url(url)) as response:
            response.raise_for_status()
            if response.content_length is not None and response.content_length > _MAX_IMAGE_BYTES:
                raise BilibiliError("图片超过 10 MiB，已跳过")

            chunks: list[bytes] = []
            total_size = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total_size += len(chunk)
                if total_size > _MAX_IMAGE_BYTES:
                    raise BilibiliError("图片超过 10 MiB，已跳过")
                chunks.append(chunk)
        return b"".join(chunks)

    async def _follow_short_url(self, text: str) -> str:
        match = _SHORT_URL_PATTERN.search(text.replace("\\", ""))
        if match is None:
            return text
        short_url = f"https://{match.group('host')}/{match.group('path')}"
        async with self._session.get(short_url, allow_redirects=True) as response:
            response.raise_for_status()
            return str(response.url)

    async def _video_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        payload = await self._get_json(reference.api_url)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("视频已删除、不可见、审核中或权限不足")

        canonical_url = f"https://www.bilibili.com/video/av{data['aid']}"
        title_lines = [f"标题：{data['title']}"]
        pages = data.get("pages") if isinstance(data.get("pages"), list) else []
        if reference.page is not None and 1 <= reference.page <= len(pages):
            canonical_url += f"?p={reference.page}"
            page_info = pages[reference.page - 1]
            part_title = (
                str(page_info.get("part") or "") if isinstance(page_info, dict) else ""
            )
            if part_title and part_title != data["title"]:
                title_lines.append(f"小标题：{part_title}")
        if reference.time_location is not None:
            canonical_url += (
                "&" if "?" in canonical_url else "?"
            ) + f"t={reference.time_location}"

        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        published = strftime(
            "%Y-%m-%d %H:%M:%S", localtime(int(data.get("pubdate") or 0))
        )
        text_parts = [
            f"{canonical_url}\n",
            "\n".join(title_lines) + "\n",
            f"类型：{data.get('tname', '未知')} | UP：{owner.get('name', '未知')} | 日期：{published}\n",
            (
                f"播放：{_handle_num(int(stat.get('view') or 0))} | "
                f"弹幕：{_handle_num(int(stat.get('danmaku') or 0))} | "
                f"收藏：{_handle_num(int(stat.get('favorite') or 0))}\n"
                f"点赞：{_handle_num(int(stat.get('like') or 0))} | "
                f"硬币：{_handle_num(int(stat.get('coin') or 0))} | "
                f"评论：{_handle_num(int(stat.get('reply') or 0))}\n"
            ),
        ]
        if options.include_description:
            description_lines = [
                line for line in str(data.get("desc") or "").splitlines() if line
            ]
            description = "\n".join(description_lines[:3])
            if len(description_lines) > 3:
                description += "……"
            if description:
                text_parts.append(f"简介：{description}")

        parts: list[ContentPart] = []
        if options.displays_images_for("video"):
            parts.append(_image(_resize_image(str(data.get("pic") or ""), options)))
        parts.extend(_text(part) for part in text_parts)
        return ParsedContent(canonical_url, tuple(parts))

    async def _bangumi_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        api_url = reference.api_url
        is_media = "media_id=" in api_url
        if is_media:
            media_payload = await self._get_json(api_url)
            result = media_payload.get("result")
            media = result.get("media") if isinstance(result, dict) else None
            season_id = media.get("season_id") if isinstance(media, dict) else None
            if not season_id:
                raise BilibiliError("未找到番剧季度信息")
            api_url = (
                f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}"
            )

        payload = await self._get_json(api_url)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BilibiliError("番剧不存在或暂不可见")

        long_title = ""
        if is_media:
            canonical_url = (
                f"https://www.bilibili.com/bangumi/media/md{result['media_id']}"
            )
        elif "season_id=" in api_url:
            canonical_url = (
                f"https://www.bilibili.com/bangumi/play/ss{result['season_id']}"
            )
        else:
            episode_match = re.search(r"ep_id=(\d+)", api_url)
            if episode_match is None:
                raise BilibiliError("番剧集数信息无效")
            episode_id = episode_match.group(1)
            canonical_url = f"https://www.bilibili.com/bangumi/play/ep{episode_id}"
            episodes = (
                result.get("episodes")
                if isinstance(result.get("episodes"), list)
                else []
            )
            for episode in episodes:
                if (
                    isinstance(episode, dict)
                    and str(episode.get("ep_id")) == episode_id
                ):
                    long_title = str(episode.get("long_title") or "")
                    break
        if reference.time_location is not None:
            canonical_url += f"?t={reference.time_location}"

        new_episode = (
            result.get("new_ep") if isinstance(result.get("new_ep"), dict) else {}
        )
        styles = "、".join(str(style) for style in result.get("styles", []) if style)
        text_parts = [f"{canonical_url}\n", f"番剧：{result.get('title', '未知')}\n"]
        if long_title:
            text_parts.append(f"标题：{long_title}\n")
        if new_episode.get("desc"):
            text_parts.append(f"{new_episode['desc']}\n")
        if styles:
            text_parts.append(f"类型：{styles}\n")
        if options.include_description and result.get("evaluate"):
            text_parts.append(f"简介：{result['evaluate']}")

        parts: list[ContentPart] = []
        if options.displays_images_for("bangumi"):
            parts.append(
                _image(
                    _resize_image(
                        str(result.get("cover") or ""), options, is_cover=True
                    )
                )
            )
        parts.extend(_text(part) for part in text_parts)
        return ParsedContent(canonical_url, tuple(parts))

    async def _live_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        payload = await self._get_json(reference.api_url)
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise BilibiliError(str(payload.get("message") or "直播间不存在或暂不可见"))
        data = payload["data"]
        by_room_ids = (
            data.get("by_room_ids") if isinstance(data.get("by_room_ids"), dict) else {}
        )
        room_info = next(
            (item for item in by_room_ids.values() if isinstance(item, dict)), {}
        )
        room_id = room_info.get("room_id")
        if not room_id:
            raise BilibiliError("直播间信息不完整")
        canonical_url = f"https://live.bilibili.com/{room_id}"

        live_status = int(room_info.get("live_status") or 0)
        if room_info.get("lock_status"):
            title = "[已封禁]直播间当前处于封禁状态"
        elif live_status == 1:
            title = f"[直播中]标题：{room_info.get('title', '')}"
        elif live_status == 2:
            title = f"[轮播中]标题：{room_info.get('title', '')}"
        else:
            title = f"[未开播]标题：{room_info.get('title', '')}"

        text_parts = [
            f"{canonical_url}\n",
            f"{title}\n",
            (
                f"主播：{room_info.get('uname', '未知')}  当前分区："
                f"{room_info.get('parent_area_name', '未知')}-{room_info.get('area_name', '未知')}\n"
            ),
            f"当前人气：{_handle_num(int(room_info.get('online') or 0))}\n",
        ]
        if room_info.get("tags"):
            text_parts.append(f"标签：{room_info['tags']}\n")
        if live_status:
            text_parts.append(
                "独立播放器：https://www.bilibili.com/blackboard/live/live-activity-player.html"
                f"?enterTheRoom=0&cid={room_id}"
            )

        parts: list[ContentPart] = []
        if options.displays_images_for("live") and room_info.get("cover"):
            parts.append(
                _image(_resize_image(str(room_info["cover"]), options, is_cover=True))
            )
        parts.extend(_text(part) for part in text_parts)
        return ParsedContent(canonical_url, tuple(parts))

    async def _article_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        payload = await self._get_json(reference.api_url)
        data = payload.get("data")
        if not isinstance(data, dict) or reference.page is None:
            raise BilibiliError("专栏不存在或暂不可见")
        canonical_url = f"https://www.bilibili.com/read/cv{reference.page}"
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        text_parts = [
            f"标题：{data.get('title', '未知')}\n",
            f"作者：{data.get('author_name', '未知')} (https://space.bilibili.com/{data.get('mid', '')})\n",
            (
                f"阅读数：{_handle_num(int(stats.get('view') or 0))} "
                f"收藏数：{_handle_num(int(stats.get('favorite') or 0))} "
                f"硬币数：{_handle_num(int(stats.get('coin') or 0))}\n"
                f"分享数：{_handle_num(int(stats.get('share') or 0))} "
                f"点赞数：{_handle_num(int(stats.get('like') or 0))} "
                f"不喜欢数：{_handle_num(int(stats.get('dislike') or 0))}\n"
            ),
            canonical_url,
        ]
        parts: list[ContentPart] = []
        if options.displays_images_for("article"):
            image_urls = (
                data.get("origin_image_urls")
                if isinstance(data.get("origin_image_urls"), list)
                else []
            )
            parts.extend(
                _image(_resize_image(str(url), options)) for url in image_urls if url
            )
        parts.extend(_text(part) for part in text_parts)
        return ParsedContent(canonical_url, tuple(parts))

    async def _dynamic_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        visitor_cookies = await self._get_visitor_cookies()
        parsed_url = urllib.parse.urlsplit(reference.api_url)
        request_params = dict(urllib.parse.parse_qsl(parsed_url.query))
        dynamic_id = str(request_params.get("id") or request_params.get("rid") or "")
        if not dynamic_id:
            raise BilibiliError("动态 ID 无效")

        opus_content = await self._opus_detail(dynamic_id, options, visitor_cookies)
        if opus_content is not None:
            return opus_content

        request_params.update(
            {
                "features": _DYNAMIC_FEATURES,
                "gaia_source": "main_web",
                "platform": "web",
                "timezone_offset": "-480",
                "web_location": "333.1368",
            }
        )
        signed_query = await self._signed_query(request_params)
        request_url = urllib.parse.urlunsplit(parsed_url._replace(query=signed_query))
        payload = await self._get_json(request_url, cookies=visitor_cookies)
        data = payload.get("data")
        item = data.get("item") if isinstance(data, dict) else None
        if payload.get("code") != 0 or not isinstance(item, dict):
            raise BilibiliError(str(payload.get("message") or "动态不存在或暂不可见"))
        dynamic_id = str(item.get("id_str") or "")
        if not dynamic_id:
            raise BilibiliError("动态信息不完整")
        canonical_url = f"https://t.bilibili.com/{dynamic_id}"

        modules = item.get("modules") if isinstance(item.get("modules"), dict) else {}
        module_dynamic = (
            modules.get("module_dynamic")
            if isinstance(modules.get("module_dynamic"), dict)
            else {}
        )
        description = (
            module_dynamic.get("desc")
            if isinstance(module_dynamic.get("desc"), dict)
            else {}
        )
        content = str(description.get("text") or "").replace("\r", "\n")
        while "\n\n" in content:
            content = content.replace("\n\n", "\n")

        parts: list[ContentPart] = []
        if content:
            parts.append(_text(content + "\n"))
        module_type = str(item.get("type") or "")
        major = module_dynamic.get("major")
        if isinstance(major, dict) and module_type == "DYNAMIC_TYPE_DRAW":
            draw = major.get("draw") if isinstance(major.get("draw"), dict) else {}
            items = draw.get("items") if isinstance(draw.get("items"), list) else []
            if options.displays_images_for("dynamic"):
                parts.extend(
                    _image(_resize_image(str(draw_item.get("src") or ""), options))
                    for draw_item in items
                    if isinstance(draw_item, dict) and draw_item.get("src")
                )
            elif items:
                parts.append(_text(f"PS：动态中包含{len(items)}张图片\n"))
        elif isinstance(major, dict) and module_type == "DYNAMIC_TYPE_AV":
            archive = (
                major.get("archive") if isinstance(major.get("archive"), dict) else {}
            )
            if options.displays_images_for("dynamic") and archive.get("cover"):
                parts.append(_image(_resize_image(str(archive["cover"]), options)))
            jump_url = str(archive.get("jump_url") or "")
            if jump_url.startswith("//"):
                jump_url = f"https:{jump_url}"
            parts.append(
                _text(f"转发视频：{jump_url}\n简介：{archive.get('desc', '')}\n")
            )
        elif module_type == "DYNAMIC_TYPE_FORWARD":
            original = item.get("orig") if isinstance(item.get("orig"), dict) else {}
            if original.get("id_str"):
                parts.append(
                    _text(f"转发动态：https://t.bilibili.com/{original['id_str']}\n")
                )

        additional = (
            module_dynamic.get("additional")
            if isinstance(module_dynamic.get("additional"), dict)
            else {}
        )
        if additional.get("type") == "ADDITIONAL_TYPE_GOODS":
            goods = (
                additional.get("goods")
                if isinstance(additional.get("goods"), dict)
                else {}
            )
            goods_items = (
                goods.get("items") if isinstance(goods.get("items"), list) else []
            )
            for goods_item in goods_items:
                if isinstance(goods_item, dict):
                    parts.append(
                        _text(
                            f"{goods_item.get('name', '')}（{goods_item.get('price', '')}）\n"
                        )
                    )
        parts.append(_text(f"动态链接：{canonical_url}"))
        return ParsedContent(canonical_url, tuple(parts))

    async def _opus_detail(
        self,
        dynamic_id: str,
        options: ParseOptions,
        visitor_cookies: Mapping[str, str],
    ) -> ParsedContent | None:
        query = urllib.parse.urlencode({"id": dynamic_id, "features": _OPUS_FEATURES})
        payload = await self._get_json(
            f"https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail?{query}",
            cookies=visitor_cookies,
        )
        if payload.get("code") != 0:
            if payload.get("code") == -352:
                return None
            raise BilibiliError(
                str(payload.get("message") or "图文动态不存在或暂不可见")
            )

        data = payload.get("data")
        item = data.get("item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return None

        canonical_url = (
            f"https://www.bilibili.com/opus/{item.get('id_str') or dynamic_id}"
        )
        basic = item.get("basic") if isinstance(item.get("basic"), dict) else {}
        modules = item.get("modules") if isinstance(item.get("modules"), list) else []
        title = str(basic.get("title") or "").strip()
        author_name = ""
        author_mid = ""
        content_lines: list[str] = []
        image_urls: list[str] = []

        for module in modules:
            if not isinstance(module, dict):
                continue
            module_title = module.get("module_title")
            if not title and isinstance(module_title, dict):
                title = str(module_title.get("text") or "").strip()

            module_author = module.get("module_author")
            if isinstance(module_author, dict):
                author_name = str(module_author.get("name") or author_name)
                author_mid = str(module_author.get("mid") or author_mid)

            module_content = module.get("module_content")
            if not isinstance(module_content, dict):
                continue
            paragraphs = module_content.get("paragraphs")
            if not isinstance(paragraphs, list):
                continue
            for paragraph in paragraphs:
                if not isinstance(paragraph, dict):
                    continue
                text_data = paragraph.get("text")
                nodes = text_data.get("nodes") if isinstance(text_data, dict) else []
                paragraph_text: list[str] = []
                for node in nodes if isinstance(nodes, list) else []:
                    if not isinstance(node, dict):
                        continue
                    word = node.get("word")
                    rich = node.get("rich")
                    formula = node.get("formula")
                    if isinstance(word, dict) and word.get("words"):
                        paragraph_text.append(str(word["words"]))
                    elif isinstance(rich, dict):
                        paragraph_text.append(
                            str(rich.get("text") or rich.get("orig_text") or "")
                        )
                    elif isinstance(formula, dict):
                        paragraph_text.append(
                            str(
                                formula.get("latex_content")
                                or formula.get("formula_content")
                                or ""
                            )
                        )
                if paragraph_text:
                    content_lines.append("".join(paragraph_text))

                picture_data = paragraph.get("pic")
                pictures = (
                    picture_data.get("pics") if isinstance(picture_data, dict) else []
                )
                for picture in pictures if isinstance(pictures, list) else []:
                    if isinstance(picture, dict) and picture.get("url"):
                        image_urls.append(str(picture["url"]))

        parts: list[ContentPart] = []
        if title:
            parts.append(_text(f"标题：{title}\n"))
        if author_name:
            author_url = (
                f" (https://space.bilibili.com/{author_mid})" if author_mid else ""
            )
            parts.append(_text(f"作者：{author_name}{author_url}\n"))
        if content_lines:
            parts.append(_text("\n".join(content_lines) + "\n"))
        if options.displays_images_for("dynamic"):
            parts.extend(_image(_resize_image(url, options)) for url in image_urls)
        elif image_urls:
            parts.append(_text(f"PS：动态中包含{len(image_urls)}张图片\n"))
        parts.append(_text(f"动态链接：{canonical_url}"))
        return ParsedContent(canonical_url, tuple(parts))

    async def _get_json(
        self, url: str, *, cookies: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        async with self._session.get(url, cookies=cookies) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise BilibiliError("Bilibili 接口返回了无法识别的数据")
        return payload

    async def _get_visitor_cookies(self) -> dict[str, str]:
        payload = await self._get_json("https://api.bilibili.com/x/frontend/finger/spi")
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise BilibiliError("无法获取 Bilibili 访客标识")
        buvid3 = str(data.get("b_3") or "")
        buvid4 = str(data.get("b_4") or "")
        if not buvid3:
            raise BilibiliError("Bilibili 访客标识为空")
        cookies = {"buvid3": buvid3}
        if buvid4:
            cookies["buvid4"] = buvid4
        return cookies

    @staticmethod
    async def _expect_success(response: ClientResponse) -> None:
        async with response:
            response.raise_for_status()

    async def _signed_query(self, params: Mapping[str, Any]) -> str:
        nav_payload = await self._get_json(
            "https://api.bilibili.com/x/web-interface/nav"
        )
        data = nav_payload.get("data")
        wbi_images = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi_images, dict):
            raise BilibiliError("无法获取 Bilibili WBI 签名密钥")
        image_key = (
            str(wbi_images.get("img_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
        )
        sub_key = (
            str(wbi_images.get("sub_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
        )
        if not image_key or not sub_key:
            raise BilibiliError("Bilibili WBI 签名密钥为空")

        mixin_key = reduce(
            lambda current, index: current + (image_key + sub_key)[index],
            _MIXIN_KEY_ENC_TABLE,
            "",
        )[:32]
        signed_params = dict(params)
        signed_params["wts"] = round(time.time())
        normalized_params = {
            key: "".join(char for char in str(value) if char not in "!'()*")
            for key, value in sorted(signed_params.items())
        }
        query = urllib.parse.urlencode(normalized_params)
        normalized_params["w_rid"] = md5((query + mixin_key).encode()).hexdigest()
        return urllib.parse.urlencode(normalized_params)

    async def _get_ticket(self) -> str:
        timestamp = int(time.time())
        signature = hmac.new(
            b"XgwSnGZ1p",
            f"ts{timestamp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        params = {
            "key_id": "ec02",
            "hexsign": signature,
            "context[ts]": str(timestamp),
            "csrf": "",
        }
        async with self._session.post(
            "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
            params=params,
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        data = payload.get("data") if isinstance(payload, dict) else None
        ticket = data.get("ticket") if isinstance(data, dict) else None
        if not ticket:
            raise BilibiliError("无法获取 Bilibili 搜索凭证")
        return str(ticket)
