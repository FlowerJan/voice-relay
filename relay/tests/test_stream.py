"""SSE 透传：流式不能被缓冲，重试不能越过"首块"这条线。

这是整个项目中判定条件最微妙的一处，所以单独一个文件。

**核心主张**：能不能重试，不取决于"有没有出错"，而取决于"前端收到字节没有"。
做法是把上游的第一个数据块预读出来——预读发生在向客户端写第一个字节之前，
因此它之前的所有失败都能安全重试，它之后的失败一律不重试（重发 = 重复段落）。

这个设计有两类可能的破坏，测试要分别盯住：
1. 预读把流缓冲了 → 用户看到"卡几秒，然后一次性全出现"；
2. 首块之后还重试 → 用户看到同一段话出现两遍。
"""
import httpx
import pytest

from app.config import settings
from app.routers.chat import chat_stream

from conftest import RecordedStream, drain, error_of, make_request

pytestmark = pytest.mark.anyio


async def test_first_chunk_is_forwarded_not_swallowed(upstream):
    """预读出来的首块必须补发出去，否则每次回答都少开头一段。"""
    upstream.streamed([b"data: \xe4\xbd\xa0\xe5\xa5\xbd\n\n", b"data: end\n\n"])
    response = await chat_stream(await make_request())

    chunks = await drain(response)
    assert chunks == [b"data: \xe4\xbd\xa0\xe5\xa5\xbd\n\n", b"data: end\n\n"]


async def test_stream_is_not_buffered_into_one_blob(upstream):
    """三块进、三块出。合并成一块虽然内容一样，但流式的意义就没了。"""
    upstream.streamed([b"a", b"b", b"c"])
    response = await chat_stream(await make_request())

    assert await drain(response) == [b"a", b"b", b"c"]
    assert isinstance(response.body_iterator, object)


async def test_upstream_closed_after_stream_ends(upstream):
    """正常结束也要关上游：不关就是一条挂着不动的长连接。"""
    stream = upstream.streamed([b"a", b"b"])
    response = await chat_stream(await make_request())
    await drain(response)

    assert stream.closed, "流结束后上游连接必须释放"


async def test_failure_after_first_chunk_keeps_content_and_does_not_retry(upstream):
    """首块之后断开：把已收到的内容交出去，但绝不重试。

    这是本文件最重要的一条。如果重试了，用户会看到同一段开头出现两次；
    如果直接报错，用户会丢掉已经看到的内容。
    """
    stream = upstream.streamed([b"data: part1\n\n", b"data: part2\n\n"], fail_after=1)
    response = await chat_stream(await make_request())

    chunks = await drain(response)
    assert b"".join(chunks) == b"data: part1\n\n", "已收到的内容不能丢"
    assert upstream.count == 1, "首块已到达，绝不能重试"
    assert stream.closed


async def test_failure_before_first_chunk_is_retried(upstream):
    """首块之前断开：此时前端一个字节都没收到，重试是安全的，也确实该试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadError("首块之前就断了")
        return httpx.Response(200, stream=RecordedStream([b"data: retried\n\n"]))

    upstream.set(handler)
    response = await chat_stream(await make_request())

    assert b"".join(await drain(response)) == b"data: retried\n\n"
    assert upstream.count == 2


async def test_retry_gives_up_and_reports_interrupted(upstream):
    """重试用尽仍失败：要说"在开始输出之前断开"，而不是笼统的 500。"""
    upstream.raise_(httpx.ReadError("断了"))
    response = await chat_stream(await make_request())

    assert response.status_code == 502
    assert error_of(response)["code"] == "chat_interrupted"
    assert upstream.count == 2, "默认重试 1 次 = 总共 2 次尝试"


async def test_retry_can_be_disabled_by_config(upstream, monkeypatch):
    """把次数设为 0 就等于关闭重试——这是文档里承诺的开关，必须真的有效。"""
    monkeypatch.setattr(settings, "chat_retry_times", 0)
    upstream.raise_(httpx.ConnectError("连不上"))
    response = await chat_stream(await make_request())

    assert response.status_code == 502
    assert upstream.count == 1


async def test_empty_first_chunk_counts_as_not_started(upstream):
    """空块（b""）不携带任何 SSE 事件，等价于上游还没开口 → 可以重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        chunks = [b""] if calls["n"] == 1 else [b"data: ok\n\n"]
        return httpx.Response(200, stream=RecordedStream(chunks))

    upstream.set(handler)
    response = await chat_stream(await make_request())

    assert b"".join(await drain(response)) == b"data: ok\n\n"
    assert upstream.count == 2


async def test_client_disconnect_releases_upstream(upstream):
    """前端关页面时，上游连接必须跟着释放。

    否则用户点了"停止"或者直接关标签，上游还在那儿生成——既烧钱又占连接池。
    这里手动中断响应生成器，模拟 ASGI 层收到 http.disconnect 后的行为。
    """
    stream = upstream.streamed([b"a", b"b", b"c"])
    response = await chat_stream(await make_request())

    iterator = response.body_iterator
    assert await iterator.__anext__() == b"a"
    await iterator.aclose()  # 客户端走了

    assert stream.closed, "客户端断开后上游连接没有被释放"


async def test_sse_headers_tell_proxies_not_to_buffer(upstream):
    """接了反代（nginx）时，靠这两个头阻止它攒够一整块才转发。"""
    upstream.streamed([b"a"])
    response = await chat_stream(await make_request())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    await response.body_iterator.aclose()
