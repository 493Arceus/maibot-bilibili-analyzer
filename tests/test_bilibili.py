from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from bilibili import (  # noqa: E402
    BilibiliClient,
    BilibiliError,
    ExtractedReference,
    ParsedContent,
    ParseOptions,
    extract_message_text,
    matches_bilibili_content,
)


def _mini_program_payload(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "app": "com.tencent.miniapp_01",
        "desc": "哔哩哔哩",
        "prompt": "[QQ小程序]哔哩哔哩",
        "meta": {"detail_1": {"appid": "1109937557", **detail}},
    }


def _mini_program_segment(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "dict",
        "data": {
            "type": "json",
            "data": {
                "data": __import__("json").dumps(
                    _mini_program_payload(detail), ensure_ascii=False
                )
            },
        },
    }


class _FakeClient(BilibiliClient):
    def __init__(self, redirected_url: str | None = None) -> None:
        self.redirected_url = redirected_url
        self.reference: ExtractedReference | None = None
        self.searched_title = ""

    async def _follow_short_url(self, text: str) -> str:
        assert "b23.tv" in text
        assert self.redirected_url is not None
        return self.redirected_url

    async def search_video(self, title: str) -> str | None:
        self.searched_title = title
        return "https://www.bilibili.com/video/BV1ab411c7mD"

    async def _video_detail(
        self, reference: ExtractedReference, options: ParseOptions
    ) -> ParsedContent:
        del options
        self.reference = reference
        return ParsedContent("https://www.bilibili.com/video/av1", ())


class _FakeImageContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        assert size == 64 * 1024
        for chunk in self._chunks:
            yield chunk


class _FakeImageResponse:
    def __init__(self, chunks: list[bytes], content_length: int | None = None) -> None:
        self.content = _FakeImageContent(chunks)
        self.content_length = content_length
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.exited = True

    def raise_for_status(self) -> None:
        return None


class _FakeImageSession:
    def __init__(self, response: _FakeImageResponse) -> None:
        self._response = response
        self.requested_url = ""

    def get(self, url: str) -> _FakeImageResponse:
        self.requested_url = url
        return self._response


def test_extracts_nested_qq_mini_program_when_structured_messages_are_disabled() -> None:
    raw_message = [
        _mini_program_segment(
            {
                "desc": "迁移后的小程序解析测试",
                "qqdocurl": "https://b23.tv/AbCd123",
                "title": "哔哩哔哩",
            }
        )
    ]

    text = extract_message_text("[json]", raw_message, include_structured=False)

    assert matches_bilibili_content(text)
    assert "https://b23.tv/AbCd123" in text
    assert '"desc": "迁移后的小程序解析测试"' in text


def test_parses_qq_mini_program_short_url() -> None:
    text = extract_message_text(
        "[json]",
        [_mini_program_segment({"qqdocurl": "https://b23.tv/AbCd123"})],
        include_structured=False,
    )
    client = _FakeClient("https://www.bilibili.com/video/BV1ab411c7mD")

    result = asyncio.run(client.parse(text, ParseOptions()))

    assert result is not None
    assert client.reference is not None
    assert client.reference.api_url.endswith("?bvid=BV1ab411c7mD")


def test_extracts_exact_url_from_platform_card_payloads() -> None:
    platform_card_payloads = [
        {
            "type": "miniapp_card",
            "app": "com.tencent.miniapp_01",
            "payload": _mini_program_payload(
                {
                    "desc": "医学规培生下夜班",
                    "qqdocurl": "https://b23.tv/Exact123",
                    "title": "哔哩哔哩",
                }
            ),
        }
    ]

    text = extract_message_text(
        "[小程序] 哔哩哔哩：医学规培生下夜班",
        [{"type": "text", "data": "[小程序] 哔哩哔哩：医学规培生下夜班"}],
        include_structured=False,
        platform_card_payloads=platform_card_payloads,
    )

    assert "https://b23.tv/Exact123" in text
    client = _FakeClient("https://www.bilibili.com/video/BV1ab411c7mD")
    result = asyncio.run(client.parse(text, ParseOptions()))
    assert result is not None
    assert client.searched_title == ""
    assert client.reference is not None
    assert client.reference.api_url.endswith("?bvid=BV1ab411c7mD")


def test_does_not_guess_video_from_mini_program_title() -> None:
    text = "[小程序] 哔哩哔哩：医学规培生下夜班"

    assert matches_bilibili_content(text)

    client = _FakeClient()
    result = asyncio.run(client.parse(text, ParseOptions()))

    assert result is None
    assert client.searched_title == ""
    assert client.reference is None


def test_does_not_guess_video_from_json_description() -> None:
    text = extract_message_text(
        "[json]",
        [_mini_program_segment({"desc": "同名视频", "title": "哔哩哔哩"})],
        include_structured=False,
    )
    client = _FakeClient()

    result = asyncio.run(client.parse(text, ParseOptions()))

    assert result is None
    assert client.searched_title == ""


def test_matches_mini_program_title_variants() -> None:
    assert matches_bilibili_content("[QQ小程序]哔哩哔哩:标题")
    assert matches_bilibili_content("[ QQ 小程序 ] 哔哩哔哩 ： 标题")


def test_does_not_match_unrelated_mini_program() -> None:
    assert not matches_bilibili_content("[小程序] 微信：医学规培生下夜班")
    assert not matches_bilibili_content("[小程序] 哔哩哔哩")


def test_download_image_reads_every_response_chunk() -> None:
    chunks = [b"first-image-chunk", b"second-image-chunk", b"last-image-chunk"]
    response = _FakeImageResponse(chunks)
    session = _FakeImageSession(response)
    client = BilibiliClient(session, None)

    result = asyncio.run(client.download_image("//i0.hdslb.com/test.jpg"))

    assert result == b"".join(chunks)
    assert session.requested_url == "https://i0.hdslb.com/test.jpg"
    assert response.exited


def test_download_image_rejects_oversized_stream() -> None:
    response = _FakeImageResponse([b"a" * (6 * 1024 * 1024), b"b" * (5 * 1024 * 1024)])
    client = BilibiliClient(_FakeImageSession(response), None)

    try:
        asyncio.run(client.download_image("https://i0.hdslb.com/test.jpg"))
    except BilibiliError as exc:
        assert str(exc) == "图片超过 10 MiB，已跳过"
    else:
        raise AssertionError("超限图片未被拒绝")

    assert response.exited
