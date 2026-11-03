"""协议层行为测试的公共设施。

两条原则：

1. **不打真实服务。** 上游用 `httpx.MockTransport` 打桩，跑测试不需要把五个端口
   全起着。协议层的价值本来就是"转发与收口的决策"，这些决策不依赖真上游。

2. **测行为，不测实现。** 断言的是"发出去的超时是多少""用户拿到什么错误码"，
   而不是"某个私有函数被调用了几次"。内部重命名函数时，这些测试不该跟着改。

一个必须记住的坑（踩过一次）：**不要用 `httpx.Response(200, content=...)` 造流式响应。**
这样造出来的响应在 httpx 里是"已加载"状态（`is_stream_consumed = True`），
`aiter_raw()` 必然抛 `StreamConsumed`。那是测试构造错了，不是被测代码错了——
真实的 uvicorn 流式响应不会这样。必须传一个真正的 `AsyncByteStream`。
"""
from collections.abc import AsyncIterator, Callable

import json

import httpx
import pytest
from starlette.requests import Request

from app import upstream as upstream_module
from app.main import app


class RecordedStream(httpx.AsyncByteStream):
    """上游字节流：可分段吐、可中途断、可断言最终有没有被关闭。"""

    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self.chunks = list(chunks)
        # fail_after=N 表示"吐到第 N 块之前抛 ReadError"，用来模拟首块后/首块前的断开
        self.fail_after = fail_after
        self.closed = False
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if self.fail_after is not None and index >= self.fail_after:
                raise httpx.ReadError("模拟上游中途断开")
            self.sent += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Upstream:
    """可编程的上游：设定它怎么回，检查 relay 实际发出去什么。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.streams: list[RecordedStream] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    # ---------- 设定上游行为 ----------

    def reply(self, response: httpx.Response) -> None:
        self.handler = lambda request: response

    def set(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler

    def raise_(self, exc: Exception) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        self.handler = handler

    def streamed(
        self,
        chunks: list[bytes],
        fail_after: int | None = None,
        status: int = 200,
    ) -> RecordedStream:
        """回一个真正的流式响应，返回字节流本体以便断言是否被关闭。"""
        stream = RecordedStream(chunks, fail_after)
        self.streams.append(stream)
        response = httpx.Response(
            status,
            stream=stream,
            headers={"content-type": "text/event-stream"},
        )
        self.reply(response)
        return stream

    # ---------- 检查 relay 发出去的东西 ----------

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "relay 没有向上游发出任何请求"
        return self.requests[-1]

    @property
    def timeout(self) -> dict:
        """httpx 把实际生效的超时写进 request.extensions["timeout"]（单位秒）。

        这是唯一能证明"路由真的用了这个超时值"的地方——直接读 settings 只能
        证明配置存在，证明不了它被用上。
        """
        return self.last.extensions.get("timeout", {})

    def header(self, name: str) -> str | None:
        return self.last.headers.get(name)


@pytest.fixture
def upstream(monkeypatch) -> Upstream:
    """把 relay 的共享 HTTP 客户端换成打桩的，并记录每一次上游请求。"""
    state = Upstream()

    def dispatch(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if state.handler is None:
            raise AssertionError("测试没有设定上游该怎么回")
        return state.handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch), trust_env=False)
    monkeypatch.setattr(upstream_module, "_client", client)
    return state


@pytest.fixture
def api() -> httpx.AsyncClient:
    """打 relay 这个 ASGI 应用本身的客户端——走完整中间件栈，但不出网。

    用于验证"响应头""错误体形状"这类只有经过中间件才会出现的东西。
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://relay.test",
        trust_env=False,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    """重试退避归零。测试要验的是"有没有试"，不是"等了多久"。

    默认 0.5 秒 × 二十几个用例就是十几秒的纯等待，没必要。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chat_retry_backoff", 0.0)


async def make_request(
    body: bytes = b"{}",
    *,
    path: str = "/api/chat/stream",
    headers: dict[str, str] | None = None,
) -> Request:
    """构造一个可以直接喂给路由函数的 Starlette Request。

    有些行为（客户端断开时上游连接是否被释放）必须能中途叫停，
    走 ASGI 传输层做不到精确控制，只能直接操作响应生成器。
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    given = {key.lower() for key in (headers or {})}
    if "content-type" not in given:
        # 默认 JSON；上传类接口会自己传 multipart，不能同时塞两个 content-type，
        # 否则 request.headers.get 拿到的是先出现的那个。
        raw_headers.append((b"content-type", b"application/json"))
    for key, value in (headers or {}).items():
        raw_headers.append((key.lower().encode(), value.encode()))

    delivered = False

    async def receive() -> dict:
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
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 9000),
    }
    return Request(scope, receive)


async def drain(response) -> list[bytes]:
    """把流式响应的 body 迭代器读完，返回所有块。"""
    chunks: list[bytes] = []
    iterator = response.body_iterator
    async for chunk in iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
    return chunks


def error_of(response) -> dict:
    """从统一错误体里取出 error 对象，顺带断言形状没跑偏。

    三种入参都要吃：路由直接返回的 JSONResponse（有 .body 但没有 .json()）、
    httpx.Response（有 .json()）、以及 FastAPI 序列化的普通 dict。
    """
    if isinstance(response, dict):
        payload = response
    elif hasattr(response, "json"):
        payload = response.json()
    else:
        payload = json.loads(response.body)
    assert set(payload) == {"error"}, f"错误体顶层应该只有一个 error 键，实际：{payload}"
    error = payload["error"]
    assert set(error) == {"code", "message", "hint", "status", "trace_id"}, f"错误体字段不对：{error}"
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    # hint 是这个形状里唯一"给用户看的动作"：空串等于没写，前端会渲染成一条空行。
    assert isinstance(error["hint"], str) and error["hint"].strip(), f"缺可执行建议：{error}"
    assert isinstance(error["status"], int)
    assert isinstance(error["trace_id"], str) and error["trace_id"]
    return error
