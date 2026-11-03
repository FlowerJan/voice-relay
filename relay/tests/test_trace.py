"""链路 ID：一次请求跨五个进程，靠它把三个服务的日志串起来。

两个方向都必须成立，少一个就断链：

- **向下去**：前端传来的（或协议层生成的）trace 必须出现在发给上游的请求头里。
  不透传的话，上游日志里的 trace 和协议层对不上，等于各打各的。
- **向上回**：响应头和错误体里必须是同一个值。用户报错时截图里那个 trace
  如果在后端查不到，这个机制就是摆设。
"""
import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.middleware import TraceIdMiddleware, get_trace_id
from app.trace import LOG_FORMAT, TraceFormatter

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


# ---------------- 第二层：ID 落到日志里，而不只是落到响应头 —----------------
#
# 上面全部通过，也只能说明"ID 在传递"。可观测性缺的不是传，是**落地**：
# 日志格式里没有这个字段时，拿 trace_id 去 grep 一处也对不上，
# 前面这些断言全部白做。


def test_formatter_fills_trace_for_records_without_it():
    """没有 trace 属性的记录也不能炸。

    logging 的行为是：格式串里的字段取不到就抛 KeyError，然后**整条日志消失**。
    所以兜底必须在 Formatter 里，而不是给 handler 挂 filter——filter 漏挂一个
    handler 就是"这批日志凭空没了"，比格式难看难查得多。
    """
    formatter = TraceFormatter(LOG_FORMAT)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)

    assert "hello" in formatter.format(record)


async def test_trace_is_still_set_while_streaming():
    """流式 body 在中间件返回之后才产出，ID 必须还在。

    这条守的是**结果**，不是某种实现：实测在 `finally` 里 reset 也不会丢
    （Starlette 创建响应任务时复制了上下文，ASGITransport 与真实 uvicorn 都验过）。
    留着它是因为——哪天有人把中间件换成纯 ASGI 实现、或改掉响应包装方式，
    "流内日志拿不到 ID"会静默发生，而那时 trace_id 就只剩响应头这一个用途了。
    """

    async def endpoint(request):
        async def body():
            yield get_trace_id().encode()

        return StreamingResponse(body(), media_type="text/plain")

    app = Starlette(routes=[Route("/s", endpoint)])
    app.add_middleware(TraceIdMiddleware)

    # 用合法形态的 ID：中间件只沿用"自己会生成的那种"（16 位十六进制），
    # 随便一串字符会被换掉——那条规则在 test_resilience 里单独钉住，
    # 这条测的是"流内上下文有没有丢"，两件事不该混在一个断言里。
    given = "abc1234567890def"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        response = await client.get("/s", headers={TRACE_HEADER: given})

    assert response.text == given, f"流内看到的 trace 是 {response.text!r}"
    assert response.headers[TRACE_HEADER] == given


async def test_request_line_carries_the_trace(api, upstream):
    """relay 自己的请求行必须带 trace，而且要在 reset **之前**打。

    踩过的点：这行如果放在 finally 之后，上下文变量已经还原成 "-"，日志照样
    一行不少、看着一切正常，但拿 trace 去 grep 一条都搜不到。所以断言的是
    **格式化后的整行**里包含这个 ID，而不是"有没有日志"。
    """
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(TraceFormatter(LOG_FORMAT).format(record))

    handler = Collect()
    target = logging.getLogger("app.middleware")
    target.addHandler(handler)
    # pytest 里 root 的默认级别是 WARNING，INFO 会被直接丢掉；生产环境由
    # trace.configure() 把 root 设成 INFO，所以这里补上同样的前提。
    target.setLevel(logging.INFO)
    try:
        upstream.streamed([b"data: x\n\n"])
        response = await api.post(
            "/api/chat/stream", json={}, headers={TRACE_HEADER: "feedfacecafebeef"}
        )
    finally:
        target.removeHandler(handler)
        target.setLevel(logging.NOTSET)

    trace = response.headers[TRACE_HEADER]
    assert any(
        trace in line and "/api/chat/stream" in line for line in lines
    ), f"请求行里没有 trace：{lines}"


def test_every_service_reads_the_header_and_logs_it():
    """三个下游都要真接上，否则"四跳可观测"只是说法。

    这里是静态检查，但不是为了省事：把三个服务真起一遍要几十秒，而这条约束
    失败的方式只有一种——"某个服务忘了接"。静态检查恰好完整覆盖它。
    """
    root = Path(__file__).resolve().parents[2]

    for service, entry in (
        ("engine/asr", "app/server.py"),
        ("engine/tts", "app/server.py"),
        ("engine/deepseek", "app/main.py"),
    ):
        assert (root / service / "app" / "trace.py").is_file(), (
            f"{service} 没有 trace.py：它读不到 X-Trace-Id"
        )
        source = (root / service / entry).read_text(encoding="utf-8")
        assert "trace.configure()" in source, f"{service} 没有装带 trace 的日志格式"
        assert "TraceMiddleware" in source, f"{service} 没装中间件，收到的 ID 无处可去"

    # 格式串里必须有 %(trace)s：配了格式却漏字段，等于什么都没做。
    for path in sorted(root.glob("engine/*/app/trace.py")):
        assert "%(trace)s" in path.read_text(encoding="utf-8"), path
    assert "%(trace)s" in (root / "relay" / "app" / "trace.py").read_text(encoding="utf-8")
