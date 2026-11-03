"""下游这一侧：ID 要出现在日志行里，而不是只出现在响应头里。

relay 侧测的是"ID 传过来了"，这里测"传过来之后有没有被用上"。
没有这一段，四跳可观测只有说法——出事时 grep 得到的是四个互不相干的窗口。

**这里不用 httpx。** 这个 venv 只装了 openai + fastapi，没有 httpx（它只是
fastapi TestClient 的可选依赖）。直接走 ASGI 接口反而更贴底层：测试想知道的是
"body 产出期间上下文里有没有 ID"，而不是 HTTP 客户端的解析细节。
"""
import logging

import pytest

from app import trace

pytestmark = pytest.mark.anyio


async def call_asgi(app, headers: dict[str, str]) -> tuple[dict[str, str], bytes]:
    """不经过任何 HTTP 客户端，直接喂 ASGI 三元组。返回 (响应头, 响应体)。"""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/s",
        "raw_path": b"/s",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }
    inbox = [{"type": "http.request", "body": b"", "more_body": False}]
    sent: list[dict] = []

    async def receive() -> dict:
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


def test_formatter_injects_the_current_trace():
    """格式串里的 %(trace)s 必须真的被填上。"""
    trace.trace_id_var.set("abc123")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)

    line = trace.TraceFormatter(trace.LOG_FORMAT).format(record)

    assert "abc123" in line
    assert "hello" in line


def test_configure_installs_a_handler_that_understands_trace():
    """配了格式但没装 handler，等于什么都没配（日志会落到 lastResort 的裸格式）。"""
    trace.configure()

    root = logging.getLogger()
    assert root.handlers, "没有 handler，日志只会走 lastResort"
    assert isinstance(root.handlers[0].formatter, trace.TraceFormatter)


async def test_stream_generator_sees_the_request_trace():
    """流式生成器里打的日志也要带 ID。

    这是本服务最重要的一条：模型报错、流中断，日志全是在生成器里打的。
    中间件若在读完请求头后就 reset，这些行会变成 "-"，等于白接。
    """
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("trace.probe")
    logger.addHandler(Collect())
    logger.setLevel(logging.INFO)
    logger.propagate = False

    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    async def endpoint(request):
        async def body():
            logger.info("流内日志 trace=%s", trace.get_trace_id())
            yield b"ok"

        return StreamingResponse(body())

    app = Starlette(routes=[Route("/s", endpoint)])
    app.add_middleware(trace.TraceMiddleware)

    # 用合法形态的 ID（16 位十六进制）：中间件只沿用"自己会生成的那种"，
    # 随便一串字符会被重新生成。这条测的是"流内上下文有没有丢"，
    # 那段校验规则由 relay 侧单独钉住，不混在同一个断言里。
    given = "abc1234567890def"
    headers, body = await call_asgi(app, {trace.TRACE_HEADER: given})

    assert body == b"ok"
    assert lines == [f"流内日志 trace={given}"], lines
    assert headers[trace.TRACE_HEADER.lower()] == given
