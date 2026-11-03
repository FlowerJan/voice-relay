"""两条"同时只能有一个"的边界：网关的在飞请求上限、ASR 的单并发槽。

这两个东西容易被当成同一件事，其实不是：

- **在飞请求上限**是系统维度的兜底：挡"很多个正常请求同时进来"；
- **ASR 槽**是链路维度的：8001 是 CPU 单进程，两个请求进来只会排队，
  而排队期间内存和连接都占着，所以第二个直接拒绝。

它们的共同点是都**不排队**——排队看起来比拒绝友好，实际是"系统看着正常、
内存在慢慢攒"，最后一次性死。所以两条都要钉住"超了就是 503 + 统一错误体"。
"""
import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.concurrency import ConcurrencyLimitMiddleware
from app.routers import asr as asr_router

from conftest import error_of

pytestmark = pytest.mark.anyio


async def test_over_limit_is_503_with_the_standard_envelope():
    """超上限的响应必须是统一错误体，不是 uvicorn 那种纯文本 503。

    纯文本 503 到了前端只能落到"服务返回了非预期的响应"，用户拿不到 code、
    也拿不到 hint——而这恰好是最需要说清楚的时候（"等一等"，不是"重试"）。
    """
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def slow(request):
        entered.set()
        await gate.wait()
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/slow", slow)])
    app.add_middleware(ConcurrencyLimitMiddleware, limit=1)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        first = asyncio.create_task(client.get("/slow"))
        await entered.wait()          # 保证第一条真的进了 handler，占住那一个名额
        try:
            # 必须给超时。负向验证时吃过一次亏：把上限判断去掉之后，第二个请求
            # 不是"变红"而是**挂死**——两个 handler 都在等 gate，而 gate 在第二个
            # 请求返回之后才 set。一个会挂死的测试比没有测试更糟：它让 /check
            # 卡在那儿，而不是告诉你坏了。
            second = await asyncio.wait_for(client.get("/slow"), timeout=1.0)
        except asyncio.TimeoutError:
            raise AssertionError("超上限的请求被放行排队了，而不是立刻拒绝")
        finally:
            gate.set()                # 无论如何都放第一条走
        assert (await first).status_code == 200

    assert second.status_code == 503
    error = error_of(second)
    assert error["code"] == "relay_overloaded"
    # hint 必须说"等"，不能说"重试"——超载时重试只会让情况更糟。
    assert "等" in error["hint"], error["hint"]


async def test_asr_second_request_is_rejected_instead_of_queued(upstream, monkeypatch):
    """槽被占时，第二段录音立刻拿到 503，而不是排在 8001 门口。

    ASR 那条链路是"排队反而更危险"：等的人占着请求体和连接，队列还能无限长。

    这里换掉模块级 `_slot` 而不是给它手工 acquire：负向验证时槽判断一旦失效，
    被测代码会自己拿到并释放那个槽，测试的 finally 再 release 一次，信号量就被
    多还了一格——污染的是**后面所有** ASR 测试。用局部信号量，多还也还给了垃圾。
    """
    slot = asyncio.Semaphore(1)
    await slot.acquire()
    monkeypatch.setattr(asr_router, "_slot", slot)

    request = _make_multipart_request()
    # 没有这层超时，槽判断失效时这里不是变红而是**一直等**（谁也拿不到槽，
    # 谁也不会先返回），表现成 /check 卡住。一个会挂死的测试比没有测试更糟。
    try:
        response = await asyncio.wait_for(asr_router.asr(request), timeout=1.0)
    except asyncio.TimeoutError:
        raise AssertionError("第二段录音被放进 8001 排队了，而不是立刻 503")

    assert response.status_code == 503
    error = error_of(response)
    assert error["code"] == "asr_busy"
    assert upstream.count == 0, "被拒的请求不该送到 8001"


async def test_asr_slot_is_released_after_a_failure(upstream, monkeypatch):
    """上游连不上也要把槽还回去 —— 否则一次失败会让 ASR 永久 503。"""
    slot = asyncio.Semaphore(1)
    monkeypatch.setattr(asr_router, "_slot", slot)
    upstream.raise_(httpx.ConnectError("refused"))

    request = _make_multipart_request()
    response = await asr_router.asr(request)

    assert response.status_code == 502
    assert not asr_router._slot.locked(), "请求失败后槽没释放，下一次录音会被永久拒绝"


def _make_multipart_request():
    """造一个最小的 multipart 请求，绕开真实文件上传。"""
    from starlette.requests import Request

    boundary = "----relaytest"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
        "RIFFxxxx\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/asr",
        "raw_path": b"/api/asr",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 9000),
    }
    return Request(scope, receive)
