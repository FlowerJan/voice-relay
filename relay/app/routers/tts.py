"""TTS 转发：文字 → 音频，透传到 8002。

返回的是音频字节流（audio/mpeg），前端拿到后直接交给 <audio> 播放。
整段合成，不做逐句流水线——见设计文档 5.4。
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

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


@router.post("/api/tts")
async def tts(request: Request):
    body = await request.body()
    client = get_client()
    trace_id = get_trace_id()

    try:
        upstream = await client.post(
            f"{settings.tts_base_url}/tts/synthesize",
            content=body,
            headers={"Content-Type": "application/json", TRACE_HEADER: trace_id},
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=None),
        )
    except httpx.ConnectError:
        return _err(502, "tts_unreachable", "TTS 服务（8002）未启动或无法连接")
    except httpx.TimeoutException:
        return _err(504, "tts_timeout", "TTS 合成超时")
    except httpx.HTTPError as e:
        return _err(502, "tts_error", f"TTS 服务通信失败：{str(e)[:150]}")

    if upstream.status_code >= 400:
        detail = upstream.text[:300]
        await upstream.aclose()
        return _err(upstream.status_code, "tts_upstream_error", detail or "TTS 合成失败")

    audio = upstream.content
    media = upstream.headers.get("content-type", "audio/mpeg")
    await upstream.aclose()

    return Response(
        content=audio,
        media_type=media,
        headers={
            "Cache-Control": "no-store",
            TRACE_HEADER: trace_id,
        },
    )
