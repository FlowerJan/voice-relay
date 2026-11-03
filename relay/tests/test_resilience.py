"""失败路径上的两件事：连接要还回去，500 要留下痕迹。

这些都是"测试全绿但仍然错"的地方——因为正常路径根本走不到：

- 首块读取失败时漏掉 `aclose()`，连接要等 GC。重试几次就漏几条，
  在"上游一直拒连"的场景下会越积越多。
- 未捕获异常既不打日志、又把 `str(exc)` 原样回给前端。前者让排查无从下手，
  后者把内部路径送到浏览器里。

这里不做端到端重试的完整链路（那在 test_retry.py / test_live.py），
只钉住"有没有闭"和"有没有记"。
"""
import logging
import re

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from app.config import settings
from app.middleware import ErrorEnvelopeMiddleware, TraceIdMiddleware
from app.routers.chat import chat_stream
from app.routers.tts import tts

from conftest import RecordedStream, error_of, make_request

pytestmark = pytest.mark.anyio


async def test_first_chunk_read_error_closes_upstream(upstream, monkeypatch):
    """首块就断：必须关掉上游连接，而且只试一次。"""
    monkeypatch.setattr(settings, "chat_retry_times", 0)
    # chunks 必须非空：fail_after 是在"吐第 N 块之前"触发，
    # 传空列表的话循环根本进不去，只会停成 StopAsyncIteration。
    stream = upstream.streamed([b"data: {}\n\n"], fail_after=0)

    response = await chat_stream(await make_request(b'{"messages":[]}'))

    assert response.status_code == 502
    assert error_of(response)["code"] == "chat_interrupted"
    assert stream.closed, "首块读取失败后上游连接没关，重试会不断叠加漏掉的连接"


async def test_retried_attempts_do_not_leak_connections(upstream, monkeypatch):
    """重试两次 = 开过两条连接，两条都要还回去。

    这条比上一条更接近真实故障：上游 8000 刚崩，relay 连开两条，都读到 ReadError。
    每次重试都要一条**新**流——复用同一个 Response 会得到 StreamConsumed，
    那是测试构造错了，不是被测代码的问题。
    """
    monkeypatch.setattr(settings, "chat_retry_times", 1)
    streams: list[RecordedStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = RecordedStream([b"data: {}\n\n"], fail_after=0)
        streams.append(stream)
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    upstream.set(handler)

    response = await chat_stream(await make_request(b'{"messages":[]}'))

    assert response.status_code == 502
    assert len(streams) == 2, f"应该重试一次（共两条连接），实际 {len(streams)}"
    assert all(s.closed for s in streams), f"有连接没关：{[s.closed for s in streams]}"


async def test_tts_upstream_error_body_is_parsed_not_dumped(upstream):
    """下游 TTS 的错误体是 JSON，不能整段塞进 message 给用户看。"""
    upstream.reply(
        httpx.Response(400, json={"error": {"code": "tts_failed", "message": "文本为空"}})
    )

    response = await tts(await make_request(b'{"text":""}', path="/api/tts"))
    error = error_of(response)

    assert response.status_code == 400
    assert error["code"] == "tts_failed", "下游的错误码应当透传，不该被改写成 tts_upstream_error"
    assert error["message"] == "文本为空"
    assert "{" not in error["message"], "用户看到的是 JSON 原文，说明没有解析"
    assert error["hint"].strip(), "这条路径曾经整体绕过 hint"


async def test_tts_non_json_error_still_wrapped(upstream):
    """下游回 HTML 错误页（比如反代 502）时，仍要包成统一形状。"""
    upstream.reply(httpx.Response(502, text="<html>Bad Gateway</html>"))

    error = error_of(await tts(await make_request(b"{}", path="/api/tts")))

    assert error["code"] == "tts_upstream_error"
    assert "Bad Gateway" in error["message"], "原始文本要带上，否则排查时只剩一个 502"


@pytest.fixture
def boom_app():
    async def boom(request):
        raise RuntimeError("读不到配置 /secret/path.py")

    app = Starlette(routes=[Route("/boom", boom)])
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(TraceIdMiddleware)
    return app


async def test_unhandled_error_is_logged(boom_app, caplog):
    """500 必须在服务端留下 traceback ——否则线上只有用户屏幕上那一句话。"""
    caplog.set_level(logging.ERROR, logger="app.middleware")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boom_app), base_url="http://t"
    ) as client:
        await client.get("/boom")

    records = [r for r in caplog.records if r.name == "app.middleware"]
    assert records, "未捕获异常没有打日志"
    text = "\n".join(r.getMessage() for r in records)
    assert "/boom" in text, f"日志里没有出错路径：{text}"
    assert records[0].exc_info, "错误日志必须带 traceback，否则等于没记"


async def test_unhandled_error_does_not_leak_internals(boom_app, caplog):
    """返回给前端的是通用文案，内部细节只进日志。"""
    caplog.set_level(logging.ERROR, logger="app.middleware")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boom_app), base_url="http://t"
    ) as client:
        response = await client.get("/boom", headers={"X-Trace-Id": "trace-abc"})

    error = error_of(response)
    assert response.status_code == 500
    assert "/secret" not in response.text, "内部路径被回给了前端"
    assert "RuntimeError" not in response.text
    assert error["hint"].strip(), "500 也必须有可执行建议，否则形状和别的错误对不上"


async def test_unhandled_error_keeps_the_original_trace_id(boom_app):
    """500 的错误体里必须是**这次请求**的 trace_id，不能是 "-"。

    TraceIdMiddleware 在更内层，它的 finally 会把 ContextVar 重置；
    最外层再去 get_trace_id() 只能拿到默认值，于是用户报上来的 500 追不到日志。
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boom_app), base_url="http://t"
    ) as client:
        response = await client.get("/boom", headers={"X-Trace-Id": "0123456789abcdef"})

    assert error_of(response)["trace_id"] == "0123456789abcdef"
    assert response.headers["X-Trace-Id"] == "0123456789abcdef"


async def test_foreign_trace_header_is_not_copied_verbatim(boom_app):
    """外部塞进来的 trace 头不能原样进日志。

    这个值会写进**每一行**日志，而日志是 `|` 分隔给人读的：一个带分隔符的串
    能拼出一条看起来完全合法的日志行，把"grep 一个 ID 拉出全链路"这件事变成
    可以伪造的。所以只沿用"自己会生成的形态"，别的一律重新生成。

    注意代价：非法形态被换掉意味着**链路会断**（这一跳的 ID 和前几跳对不上）。
    这是有意的取舍——断链看得见，被污染的证据看不见。

    **"看得见"靠的是下面那条测试里的日志行。** 光换掉不记，链路会"静默分叉"：
    上游一段、下游一段，各自都合法，不对比发现不了；而拿原始 ID 的人只有一个，
    等于永远发现不了。

    另外注意 bad_ids 里全是 **ASCII**：HTTP 头只能是 ASCII，"中文 trace_id"
    这个攻击面在传输层就不存在（构造它会先在 httpx 编码阶段失败，不是产品缺陷）。
    真正要防的是 ASCII 范围内的分隔符伪造、超长、大小写变体。
    """
    bad_ids = [
        "trace-abc",                       # 非十六进制
        "0123456789abcde",                 # 15 位，短一位
        "0123456789abcdef0",               # 17 位，长一位
        "0123456789ABCDEF",                # 全大写：本项目从不生成这种形态
        "deadbeef | INFO    | app.middleware | forged",  # 分隔符伪造
        "a" * 4096,                        # 超长，会撑爆每一行日志
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boom_app), base_url="http://t"
    ) as client:
        for bad in bad_ids:
            response = await client.get("/boom", headers={"X-Trace-Id": bad})
            got = response.headers["X-Trace-Id"]
            assert got != bad, f"非法 trace 头被原样沿用了：{bad!r}"
            assert re.fullmatch(r"[0-9a-f]{16}", got), (
                f"重新生成的 ID 不是合法形态：{got!r}")


async def test_replaced_trace_id_is_logged_without_the_raw_value(boom_app, caplog):
    """换掉非法 trace 头时必须留一行日志，而且不能把原值写进去。

    这一行是"断链看得见"的全部依据。换掉 ID 之后，上游一段、下游一段，
    **各自都是合法的**——不对比就发现不了；而原始 ID 只在发它的那个人手里，
    所以"以后再对比"等于"永远发现不了"。留一行"发生了替换"，这件事才可查。

    同时钉住"原值不记录"：那个串本来就不该被信任，不能借日志再传播一次
    （它可能就是用来伪造日志行的）。只记长度，够区分"没传"和"传了个垃圾"。

    注：这行在 `trace_id_var.set()` **之后**打，所以配真实 Formatter 时它带的是
    **新的** trace_id，和这次请求其余日志落在一组里。caplog 只存原始记录、
    不跑 Formatter，所以这里断言不到那个字段——由代码注释和装配检查覆盖。
    """
    forged = "deadbeef | INFO    | app.middleware | forged"
    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=boom_app), base_url="http://t"
        ) as client:
            response = await client.get("/boom", headers={"X-Trace-Id": forged})

    hits = [r.getMessage() for r in caplog.records if "形态不合法" in r.getMessage()]
    assert hits, "trace 头被换掉了却没有任何日志：断链变成了静默分叉"
    assert all(forged not in line for line in hits), f"原值被写进日志了：{hits}"
    # 合法请求不该产生这行噪音（-vv 跑测试时能看到它出现在哪）。
    assert response.headers["X-Trace-Id"] != forged
