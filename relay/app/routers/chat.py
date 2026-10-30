"""聊天转发：SSE 逐块透传到 8000。

**这里最容易踩的坑是 SSE 被缓冲。**
如果网关先把上游响应读完再转发给前端，用户会看到"卡十几秒，然后文字一次性全出现"——
流式就废了。必须**收到一块就立刻转一块**。

用 `aiter_raw()` 而不是 `aiter_bytes()`：前者不解码，直接把原始字节搬过去，
避免任何一层做行合并导致半行被截断。

**第二个坑是重试的边界。**
上游连不上时补一次重试很常见，但流式请求不能随便重试：已经向前端吐出内容之后再重试，
用户会看到重复的段落。所以判定标准不是"有没有出错"，而是"前端收到字节没有"。
做法是把上游的**第一个数据块预读出来**——预读发生在向客户端发送任何字节之前，
因此它之前的失败都能安全重试，它之后的失败一律不重试。
"""
import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.middleware import TRACE_HEADER, get_trace_id
from app.upstream import get_client

router = APIRouter()
logger = logging.getLogger(__name__)


class _UpstreamRejected(Exception):
    """上游返回 4xx/5xx。重试无意义：请求本身有问题，或上游实现有缺陷。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class _UpstreamNeverSpoke(Exception):
    """上游回了 200 却一个字节没写就断开——等价于"还没开始说话"，可以重试。"""


def _err(status: int, code: str, message: str):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "status": status,
                "trace_id": get_trace_id(),
            }
        },
    )


def _classify(exc: Exception) -> tuple[int, str, str]:
    """把"首块之前的失败"翻译成对用户有意义的错误。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return 504, "chat_timeout", "连接聊天服务超时"
    if isinstance(exc, httpx.ConnectError):
        return 502, "chat_unreachable", "聊天服务（8000）未启动或无法连接"
    if isinstance(exc, httpx.ReadError):
        return 502, "chat_interrupted", "聊天服务在开始输出之前断开"
    if isinstance(exc, httpx.RemoteProtocolError):
        return 502, "chat_protocol_error", "聊天服务返回了不完整的响应"
    return 502, "chat_no_output", "聊天服务未返回任何数据"


async def _open_stream(
    client: httpx.AsyncClient, body: bytes, timeout: httpx.Timeout
) -> tuple[httpx.Response, AsyncIterator[bytes], bytes]:
    """建立上游流式连接，并把第一个数据块预读出来。

    预读是重试安全性的唯一依据：它之后才向客户端写第一个字节。
    """
    upstream_req = client.build_request(
        "POST",
        f"{settings.chat_base_url}/api/chat/stream",
        content=body,
        headers={"Content-Type": "application/json", TRACE_HEADER: get_trace_id()},
        timeout=timeout,
    )
    upstream = await client.send(upstream_req, stream=True)

    if upstream.status_code >= 400:
        detail = (await upstream.aread()).decode("utf-8", "ignore")[:300]
        await upstream.aclose()
        raise _UpstreamRejected(upstream.status_code, detail or "聊天服务返回错误")

    # 迭代器只能建一次：aiter_raw() 每次调用都会重新包装底层流，
    # 第二次包装会抛 StreamConsumed。预读与后续转发必须共用同一个迭代器。
    raw_iter = upstream.aiter_raw()
    try:
        first = await raw_iter.__anext__()
    except StopAsyncIteration:
        await upstream.aclose()
        raise _UpstreamNeverSpoke("聊天服务未返回任何数据") from None

    if not first:
        # 空块不携带任何 SSE 事件，等价于上游还没开始说话
        await upstream.aclose()
        raise _UpstreamNeverSpoke("聊天服务返回了空数据块") from None

    return upstream, raw_iter, first


@router.post("/api/chat/stream")
async def chat_stream(request: Request):
    body = await request.body()
    client = get_client()

    # 聊天是远程流式，可能说很久：
    # 只设连接超时（连不上要快速失败），不设总超时；read 设的是"两块之间的间隔"上限。
    timeout = httpx.Timeout(
        connect=settings.chat_connect_timeout,
        read=settings.chat_read_timeout,
        write=settings.chat_connect_timeout,
        pool=None,
    )

    upstream: httpx.Response | None = None
    raw_iter: AsyncIterator[bytes] | None = None
    first: bytes = b""
    attempts = max(1, settings.chat_retry_times + 1)

    for attempt in range(attempts):
        try:
            upstream, raw_iter, first = await _open_stream(client, body, timeout)
            break
        except _UpstreamRejected as e:
            # 4xx/5xx：请求本身或上游实现的问题，重试只会让用户白等一次
            return _err(e.status, "chat_upstream_error", e.detail)
        except (
            _UpstreamNeverSpoke,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ) as e:
            # 首块未达 → 前端还没收到任何字节 → 可以安全重试
            if attempt + 1 >= attempts:
                return _err(*_classify(e))
            logger.warning(
                "首块未达（%s），%.2fs 后重试 %d/%d trace=%s",
                type(e).__name__,
                settings.chat_retry_backoff,
                attempt + 1,
                attempts - 1,
                get_trace_id(),
            )
            await asyncio.sleep(settings.chat_retry_backoff)
        except httpx.TimeoutException:
            # 读超时意味着上游已连上却卡住，重试只会让用户再等一个完整超时周期
            return _err(504, "chat_timeout", "聊天服务响应超时")
        except httpx.HTTPError as e:
            return _err(502, "chat_error", f"聊天服务通信失败：{str(e)[:150]}")

    if upstream is None or raw_iter is None:  # 循环要么 break 要么 return，这里只是让类型收敛
        return _err(502, "chat_error", "聊天服务不可用")

    async def relay_stream():
        try:
            # 首块已经在 _open_stream 里读出来了，先补发
            yield first
            async for chunk in raw_iter:
                yield chunk
        except httpx.ReadError:
            # 首块之后断开：前端会看到流结束但没有 done 事件，属于可接受的降级。
            # 这里**不能**重试——前端已经渲染了部分内容，重发会出现重复段落。
            pass
        finally:
            # 客户端断开时也会走到这里 → 上游连接被释放，不会挂着空跑
            await upstream.aclose()

    return StreamingResponse(
        relay_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 之类的缓冲（本地用不到，但接了反代就靠它）
            "X-Accel-Buffering": "no",
            TRACE_HEADER: get_trace_id(),
        },
    )


@router.get("/api/balance")
async def balance():
    """余额查询：转发到聊天服务（Key 只在 8000 那边的 .env 里，前端永远拿不到）。"""
    client = get_client()
    try:
        r = await client.get(
            f"{settings.chat_base_url}/api/balance",
            headers={TRACE_HEADER: get_trace_id()},
            timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=None),
        )
    except httpx.ConnectError:
        return _err(502, "chat_unreachable", "聊天服务（8000）未启动或无法连接")
    except httpx.TimeoutException:
        return _err(504, "balance_timeout", "查询余额超时")
    except httpx.HTTPError as e:
        return _err(502, "balance_error", f"余额查询失败：{str(e)[:150]}")

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        return _err(502, "balance_bad_response", "余额接口返回的不是合法 JSON")

    # 下游已经是 {ok, ...} 形状，原样返回，保持第一阶段契约不变
    return JSONResponse(status_code=r.status_code, content=payload)
