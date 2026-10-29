"""聊天转发：SSE 逐块透传到 8000。

**这里最容易踩的坑是 SSE 被缓冲。**
如果网关先把上游响应读完再转发给前端，用户会看到"卡十几秒，然后文字一次性全出现"——
流式就废了。必须**收到一块就立刻转一块**。

用 `aiter_raw()` 而不是 `aiter_bytes()`：前者不解码，直接把原始字节搬过去，
避免任何一层做行合并导致半行被截断。
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.middleware import TRACE_HEADER, get_trace_id
from app.upstream import get_client

router = APIRouter()

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

    upstream_req = client.build_request(
        "POST",
        f"{settings.chat_base_url}/api/chat/stream",
        content=body,
        headers={"Content-Type": "application/json", TRACE_HEADER: get_trace_id()},
        timeout=timeout,
    )

    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        return _err(502, "chat_unreachable", "聊天服务（8000）未启动或无法连接")
    except httpx.TimeoutException:
        return _err(504, "chat_timeout", "连接聊天服务超时")
    except httpx.HTTPError as e:
        return _err(502, "chat_error", f"聊天服务通信失败：{str(e)[:150]}")

    if upstream.status_code >= 400:
        detail = (await upstream.aread()).decode("utf-8", "ignore")[:300]
        await upstream.aclose()
        return _err(upstream.status_code, "chat_upstream_error", detail or "聊天服务返回错误")

    async def relay_stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.ReadError:
            # 上游中途断开：前端会看到流结束但没有 done 事件，属于可接受的降级
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
