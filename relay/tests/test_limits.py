"""请求体上限：先看头，再数流，两道都得有。

同一件事的两个变种，缺任何一道都会漏：

- **老实但巨大**：`Content-Length: 2GB`。必须在读之前就拒——内存正是在"读完"
  那一刻没的，读完再判断等于"先被打死，再报告很疼"。
- **撒谎或不说**：没有这个头（分块传输），或者声明得比实际小。只能在边读边数，
  一过线立刻停。这条同时也是"内存峰值不超过一个 chunk"的证明。

这两条都不是功能测试：正常用法永远碰不到上限。它们测的是"被误暴露之后还能不能活"。
"""
import pytest
from starlette.requests import Request

from app.config import settings
from app.errors import GENERIC_HINT, hint_for
from app.routers.asr import asr
from app.routers.chat import chat_stream
from app.routers.extract import extract
from app.routers.tts import tts

from conftest import error_of, make_request

pytestmark = pytest.mark.anyio


def _chunked_request(
    chunks: list[bytes],
    headers: dict[str, str] | None = None,
    touched: list[int] | None = None,
) -> Request:
    """按 `chunks` 分块吐 body 的 Request。

    `touched` 用来证明"根本没读"：只要路由碰了 `request.stream()`，
    这个列表就会被追加，于是"读之前就拒了"变成可断言的事实，而不是一句承诺。
    """
    raw = [(b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode(), value.encode()))

    cursor = {"i": 0}

    async def receive() -> dict:
        if touched is not None:
            touched.append(1)
        if cursor["i"] >= len(chunks):
            return {"type": "http.disconnect"}
        index = cursor["i"]
        cursor["i"] += 1
        return {
            "type": "http.request",
            "body": chunks[index],
            "more_body": cursor["i"] < len(chunks),
        }

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat/stream",
        "raw_path": b"/api/chat/stream",
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 9000),
    }
    return Request(scope, receive)


async def test_declared_size_is_rejected_before_reading(upstream, monkeypatch):
    """头里已经说超了，就一个字节都不该读。"""
    monkeypatch.setattr(settings, "chat_max_body", 1000)
    touched: list[int] = []
    request = _chunked_request(
        [b"x" * 5000], {"content-length": "5000"}, touched=touched
    )

    response = await chat_stream(request)

    assert response.status_code == 413
    assert error_of(response)["code"] == "chat_body_too_large"
    assert not touched, "Content-Length 已经说明超限，却还是把 body 读进内存了"
    assert upstream.count == 0, "超限的请求不该被转发给 8000"


async def test_oversized_body_is_caught_while_streaming(upstream, monkeypatch):
    """没有 Content-Length 也拦得住：单块不超、累计超，才算真的在"边读边数"。"""
    monkeypatch.setattr(settings, "chat_max_body", 1000)
    request = _chunked_request([b"x" * 600, b"y" * 600])

    response = await chat_stream(request)

    assert response.status_code == 413
    assert error_of(response)["code"] == "chat_body_too_large"
    assert upstream.count == 0


async def test_normal_body_goes_through(upstream):
    """上限不能误伤正常请求——这是"加了限制之后老功能还坏不坏"的那一半。"""
    upstream.streamed([b'data: {"type":"delta","content":"hi"}\n\n'])

    response = await chat_stream(await make_request(b'{"messages":[]}'))

    assert response.status_code == 200
    assert upstream.count == 1


async def test_tts_has_its_own_limit_and_its_own_hint(upstream, monkeypatch):
    """TTS 只收要朗读的文字，上限比 chat 小得多，建议也不该共用一句。"""
    monkeypatch.setattr(settings, "tts_max_body", 100)

    response = await tts(await make_request(b"x" * 200, path="/api/tts"))

    assert response.status_code == 413
    error = error_of(response)
    assert error["code"] == "tts_body_too_large"
    assert "播报" in error["hint"], f"TTS 的建议不该是给图片用的：{error['hint']}"
    assert upstream.count == 0


async def test_streamed_links_reject_on_declared_size(upstream, monkeypatch):
    """ASR / 抽取是边收边转，不在协议层攒内存，所以只做声明长度预检。

    但"预检"也必须在转发之前：否则等于让下游先收一个 2GB 音频才开始算。
    """
    monkeypatch.setattr(settings, "asr_max_body", 1000)
    monkeypatch.setattr(settings, "extract_max_body", 1000)
    multipart = {"content-type": "multipart/form-data; boundary=b", "content-length": "5000"}

    audio = await asr(await make_request(b"x" * 10, path="/api/asr", headers=multipart))
    doc = await extract(await make_request(b"x" * 10, path="/api/extract", headers=multipart))

    assert error_of(audio)["code"] == "asr_body_too_large"
    assert error_of(doc)["code"] == "extract_body_too_large"
    assert upstream.count == 0, "超限请求被转发给下游了"


def test_limits_are_sane_and_every_one_has_a_real_hint():
    """常量本身也是要钉的：上限拍错比没有上限更难发现。

    - 4MB 是真实请求的量级上限（6 张 1400px 图片的 base64 约 2.2MB），
      低于它会把正常对话挡掉，高出 64MB 等于没设。
    - 抽取的 body 上限必须**大于** 20MB 文件限额：body 里还有 multipart 的
      头和 boundary，卡死 20MB 会把刚好 20MB 的合法文件一起拒掉。
    """
    assert 4 << 20 <= settings.chat_max_body <= 64 << 20, settings.chat_max_body
    assert settings.tts_max_body < settings.chat_max_body
    assert settings.extract_max_body > 20 << 20, "上限卡在文件限额上会误伤合法文件"

    for code in (
        "chat_body_too_large",
        "tts_body_too_large",
        "asr_body_too_large",
        "extract_body_too_large",
    ):
        assert hint_for(code) != GENERIC_HINT, f"{code} 只落到了通用兜底，等于没给建议"
