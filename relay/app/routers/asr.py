"""ASR 转发：multipart **流式**透传到 8001。

两个要点：
1. **不把音频读进内存**。用 `request.stream()` 直接作为上游请求体，
   音频边收边转，60 秒录音也不会让协议层内存飙起来。
2. **重设 Content-Type 的边界**。multipart 的 boundary 在原始头里，
   原样带过去下游才解得开；但 Content-Length 必须去掉（改成分块传输）。

外部路径 `/api/asr` → 内部路径 `/asr/transcribe`：网关负责这一次名字映射，
这样模型层的内部命名可以自由调整，前端契约不受影响。
"""
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


@router.post("/api/asr")
async def asr(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return _err(400, "bad_request", "需要 multipart/form-data 上传音频")

    client = get_client()
    trace_id = get_trace_id()

    # 只保留必要的头：Content-Type 带 boundary 是必须的，Content-Length 不能带
    headers = {"Content-Type": content_type, TRACE_HEADER: trace_id}
    if request.headers.get("content-disposition"):
        headers["Content-Disposition"] = request.headers["content-disposition"]

    upstream_req = client.build_request(
        "POST",
        f"{settings.asr_base_url}/asr/transcribe",
        content=request.stream(),  # 流式转发，不在内存里攒整段音频
        headers=headers,
        # ASR 是本地 CPU 推理，慢是正常的：总超时给足（默认 120 秒）
        timeout=httpx.Timeout(
            connect=10.0,
            read=settings.asr_timeout,
            write=settings.asr_timeout,
            pool=None,
        ),
    )

    try:
        upstream = await client.send(upstream_req)
    except httpx.ConnectError:
        return _err(502, "asr_unreachable", "ASR 服务（8001）未启动或无法连接")
    except httpx.ReadTimeout:
        return _err(504, "asr_timeout", f"ASR 识别超时（>{settings.asr_timeout:.0f} 秒）")
    except httpx.HTTPError as e:
        return _err(502, "asr_error", f"ASR 服务通信失败：{str(e)[:150]}")

    body = await upstream.aread()
    await upstream.aclose()

    if upstream.status_code >= 400:
        # 下游已经是统一格式就直接透传，否则包一层。
        # 两种情况都补上 trace_id：同一个错误，从哪一跳看都应该能串起来。
        try:
            payload = upstream.json()
        except Exception:  # noqa: BLE001
            payload = {"error": {"code": "asr_upstream_error",
                                 "message": body.decode("utf-8", "ignore")[:300],
                                 "status": upstream.status_code}}
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            payload["error"].setdefault("trace_id", trace_id)
        return JSONResponse(status_code=upstream.status_code, content=payload)

    try:
        payload = upstream.json()
    except Exception:  # noqa: BLE001
        return _err(502, "asr_bad_response", "ASR 返回的不是合法 JSON")

    payload["trace_id"] = trace_id
    return payload


@router.get("/api/asr/health")
async def asr_health():
    client = get_client()
    try:
        r = await client.get(f"{settings.asr_base_url}/health", timeout=5.0)
        return {"downstream": "asr", "status": "ok" if r.status_code < 400 else "error",
                "detail": r.json()}
    except Exception as e:  # noqa: BLE001
        return {"downstream": "asr", "status": "unreachable", "detail": str(e)[:200]}
