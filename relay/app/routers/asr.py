"""ASR 转发：multipart **流式**透传到 8001。

两个要点：
1. **不把音频读进内存**。用 `request.stream()` 直接作为上游请求体，
   音频边收边转，60 秒录音也不会让协议层内存飙起来。
2. **重设 Content-Type 的边界**。multipart 的 boundary 在原始头里，
   原样带过去下游才解得开；但 Content-Length 必须去掉（改成分块传输）。

外部路径 `/api/asr` → 内部路径 `/asr/transcribe`：网关负责这一次名字映射，
这样模型层的内部命名可以自由调整，前端契约不受影响。

**同时只放一个请求进 8001。** ASR 是本地 CPU 单进程推理，两个人同时说话不会
并行，只会排队——而排队期间请求体、连接、上游 socket 全都占着，队列还能无限
增长。所以第二个请求直接 503，而不是让它在那儿等：**看得见的忙，好过看不见
的等**。这和"分路超时/body 上限"不是一回事，那两条管的是单个请求。
"""
import asyncio

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.body import declared_too_large, limit_text
from app.config import settings
from app.errors import error_response as _err
from app.errors import merge_upstream_error
from app.middleware import TRACE_HEADER, get_trace_id
from app.upstream import get_client

router = APIRouter()

#: ASR 的并发槽。1 就是"一次一段"——判断在下面，不在这里等。
_slot = asyncio.Semaphore(1)


@router.post("/api/asr")
async def asr(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return _err(400, "bad_request", "需要 multipart/form-data 上传音频")

    # 这条链路是边收边转，不攒内存，所以只做"声明长度"预检：
    # 明显超限的直接拒，不浪费 8001 一次推理。
    if declared_too_large(request, settings.asr_max_body):
        return _err(
            413,
            "asr_body_too_large",
            f"请求体超过 {limit_text(settings.asr_max_body)} 上限",
        )

    # 这一条必须排在 8001 之前：已经有一段的推理在跑，再把这段送进去只是让
    # 两段都变慢、并多占一份内存。
    #
    # 用 locked() 判断、不用 `wait_for(acquire(), timeout=0)`：判断和下面的
    # `async with` 之间没有 await，事件循环单线程，两者之间不可能插进别的请求。
    if _slot.locked():
        return _err(503, "asr_busy", "ASR 正在识别另一段录音（同时只处理一段）")

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
        # 槽覆盖"发出去 + 读完响应"整段：推理在 8001 那边，只锁住 send 的话
        # 下一段照样能挤进去排队。
        async with _slot:
            upstream = await client.send(upstream_req)
            body = await upstream.aread()
            await upstream.aclose()
    except httpx.ConnectError:
        return _err(502, "asr_unreachable", "ASR 服务（8001）未启动或无法连接")
    except httpx.ReadTimeout:
        return _err(504, "asr_timeout", f"ASR 识别超时（>{settings.asr_timeout:.0f} 秒）")
    except httpx.HTTPError as e:
        return _err(502, "asr_error", f"ASR 服务通信失败：{str(e)[:150]}")

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
            # 下游的 code 对我们来说是未知码（bad_audio、no_speech…），精确表和前缀
            # 都可能认不出来，这时退到 ASR 这条链路的建议；下游自带 hint 就不覆盖。
            payload = merge_upstream_error(payload, upstream.status_code, "asr", trace_id)
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
