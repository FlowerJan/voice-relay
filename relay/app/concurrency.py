"""在飞请求数上限：超了立刻 503，不排队。

**为什么需要这一层。** 这里的每一条链路都是"单请求维度"的检查（body 上限、
分路超时、错误收口）——它们保证"一个坏请求不会拖垮进程"，但不保证"一百个
正常请求不会"。两者不是一回事：16MB 的上限挡住了一个 2GB 的 body，挡不住
几十个 16MB 同时进来。

**为什么不用 uvicorn 的 `limit_concurrency`。** 核过源码
（`uvicorn/protocols/http/h11_impl.py:228`），它的判据是：

    len(self.connections) >= limit or len(self.tasks) >= limit

`self.connections` 是**打开的连接数**，包含 keep-alive 里空闲的那些。浏览器
一个页面就开六条，开几个标签页就上两位数，再叠一条 SSE（长连接常驻，占一个
task），32 这个数会在"完全正常的单人使用"下被顶到——保护变成误伤，而且还是
一个纯文本 503，前端连错误码都拿不到。

这里数的是**正在处理中的请求数**（进 handler 到响应结束），这才是这个项目里
"并发"的意思：ASR 推理、文档抽取这些真在吃 CPU 的事同时跑了几件。

**为什么是拒绝而不是排队。** 排队看起来更"友好"，但排队的代价是请求体和连接
一起占着，队列还能无限长——系统看着正常，实际在慢慢攒内存，最后一次性死。
503 至少是**看得见的失败**：用户立刻知道"现在忙"，而不是转圈等到超时。
"""
import logging

from app.errors import error_response

logger = logging.getLogger(__name__)


class ConcurrencyLimitMiddleware:
    """纯 ASGI 中间件，不是 BaseHTTPMiddleware。

    故意不用 BaseHTTPMiddleware：它会把响应包成 task、流式 body 要过一层队列，
    而这里只需要在 `__call__` 前后各记一次数。原始 ASGI 最短、最不容易出事。

    计数用 int 而不是 `asyncio.Semaphore`：信号量的价值是"排队等待"，而这里
    明确不排队。`inflight >= limit` 的判断与自增之间没有 await，事件循环是
    单线程的，所以不需要锁。
    """

    def __init__(self, app, limit: int = 32) -> None:
        self.app = app
        self.limit = limit
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan / websocket 不计数：它们不是"一次请求"。
            return await self.app(scope, receive, send)

        if self.inflight >= self.limit:
            logger.warning(
                "并发已达上限 %d，拒绝 %s %s",
                self.limit, scope.get("method", "?"), scope.get("path", "?"),
            )
            response = error_response(
                503,
                "relay_overloaded",
                f"网关同时处理的请求已达上限（{self.limit}）",
            )
            return await response(scope, receive, send)

        self.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            # 必须在 finally：客户端中途断开时 ASGI 应用会收到取消，
            # 漏减一次这个计数就永久少一格，最后全站 503。
            self.inflight -= 1
