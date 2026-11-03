"""TTS 转发：文字 → 音频，透传到 8002。

返回的是音频字节流（audio/mpeg），前端拿到后直接交给 <audio> 播放。
整段合成，不做逐句流水线——见设计文档 5.4。
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.body import BodyTooLarge, read_body
from app.config import settings
from app.errors import error_response as _err
from app.errors import merge_upstream_error
from app.middleware import TRACE_HEADER, get_trace_id
from app.upstream import get_client

router = APIRouter()


@router.post("/api/tts")
async def tts(request: Request):
    try:
        body = await read_body(request, settings.tts_max_body)
    except BodyTooLarge as e:
        return _err(413, "tts_body_too_large", e.message)

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
        # 下游 TTS 的失败也是统一错误体。以前这里直接取 `upstream.text[:300]`，
        # 用户就在气泡里看到一坨 `{"error": {"code": "tts_failed", ...}}` 原文，
        # 而且第 3 项给四条链路都配了 hint，只有这条路被整体绕过。
        raw = upstream.text[:300]
        try:
            payload = upstream.json()
        except Exception:  # noqa: BLE001  不是 JSON（HTML 错误页之类）
            payload = None
        await upstream.aclose()

        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            # 下游的 code 是它自己造的（tts_failed…），精确表和前缀都认不出来，
            # 所以显式退到 TTS 这条链路的建议；下游哪天自带 hint 就不覆盖。
            payload = merge_upstream_error(payload, upstream.status_code, "tts", trace_id)
            return JSONResponse(status_code=upstream.status_code, content=payload)

        return _err(upstream.status_code, "tts_upstream_error", raw or "TTS 合成失败")

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
