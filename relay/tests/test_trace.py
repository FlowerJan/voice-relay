"""链路 ID：一次请求跨五个进程，靠它把三个服务的日志串起来。

两个方向都必须成立，少一个就断链：

- **向下去**：前端传来的（或协议层生成的）trace 必须出现在发给上游的请求头里。
  不透传的话，上游日志里的 trace 和协议层对不上，等于各打各的。
- **向上回**：响应头和错误体里必须是同一个值。用户报错时截图里那个 trace
  如果在后端查不到，这个机制就是摆设。
"""
import asyncio

import httpx
import pytest

from conftest import RecordedStream

pytestmark = pytest.mark.anyio

TRACE_HEADER = "X-Trace-Id"


async def test_trace_is_generated_when_client_sends_none(api, upstream):
    upstream.streamed([b"data: x\n\n"])
    response = await api.post("/api/chat/stream", json={})

    trace = response.headers.get(TRACE_HEADER)
    assert trace, "响应头必须带 trace，否则前端报错时无从追起"
    assert len(trace) == 16, f"约定是 16 位十六进制，实际 {trace!r}"
    assert all(char in "0123456789abcdef" for char in trace)


async def test_client_supplied_trace_is_reused(api, upstream):
    """前端/上游已经给了一个 trace 时不能另起一个——那会把一次调用拆成两条链。"""
    upstream.streamed([b"data: x\n\n"])
    given = "0123456789abcdef"
    response = await api.post("/api/chat/stream", json={}, headers={TRACE_HEADER: given})

    assert response.headers[TRACE_HEADER] == given


async def test_upstream_receives_the_same_trace(api, upstream):
    """透传方向：上游请求头里的 trace 必须与响应头一致。"""
    upstream.streamed([b"data: x\n\n"])
    given = "aaaabbbbccccdddd"
    response = await api.post("/api/chat/stream", json={}, headers={TRACE_HEADER: given})

    assert upstream.header(TRACE_HEADER) == given
    assert response.headers[TRACE_HEADER] == given


async def test_error_body_carries_the_same_trace(api, upstream):
    """错误体里的 trace 和响应头必须一致——排查时通常只拿到其中一个。"""
    upstream.raise_(httpx.ConnectError("连不上"))
    response = await api.post("/api/chat/stream", json={})

    assert response.status_code == 502
    assert response.json()["error"]["trace_id"] == response.headers[TRACE_HEADER]


async def test_each_request_gets_a_fresh_trace(api, upstream):
    """连续两次请求不能复用同一个 trace（ContextVar 没重置就会这样）。"""
    upstream.set(lambda request: httpx.Response(200, stream=RecordedStream([b"data: x\n\n"])))

    first = await api.post("/api/chat/stream", json={})
    second = await api.post("/api/chat/stream", json={})

    assert first.headers[TRACE_HEADER] != second.headers[TRACE_HEADER]


async def test_concurrent_requests_keep_their_own_trace(api, upstream):
    """并发时 trace 不能串：ContextVar 的意义就在这里。"""
    upstream.set(lambda request: httpx.Response(200, stream=RecordedStream([b"data: x\n\n"])))
    sent = ["1111111111111111", "2222222222222222"]

    async def ask(trace: str) -> tuple[str, str]:
        response = await api.post("/api/chat/stream", json={}, headers={TRACE_HEADER: trace})
        return trace, response.headers[TRACE_HEADER]

    results = await asyncio.gather(*(ask(trace) for trace in sent))
    for wanted, got in results:
        assert got == wanted, f"并发请求的 trace 串了：期望 {wanted}，拿到 {got}"


async def test_sse_response_is_not_buffered_by_the_gateway(api, upstream):
    """流式响应不能被中间件攒成一整块：三块进，三块出。

    这条顺带验证了 TraceIdMiddleware 没有因为"读一遍响应"而破坏流式——
    BaseHTTPMiddleware 历史上最容易在这里出问题。
    """
    upstream.streamed([b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"])
    response = await api.post("/api/chat/stream", json={})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "data: one\n\ndata: two\n\ndata: three\n\n"


async def test_asr_success_body_echoes_trace(api, upstream):
    """ASR 成功时把 trace 塞进响应体：这条链路上前端拿到的是 JSON，不是响应头。"""
    upstream.reply(httpx.Response(200, json={"text": "你好", "duration": 3.2}))
    response = await api.post(
        "/api/asr",
        files={"audio": ("a.wav", b"RIFF0000", "audio/wav")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "你好"
    assert payload["trace_id"] == response.headers[TRACE_HEADER]
