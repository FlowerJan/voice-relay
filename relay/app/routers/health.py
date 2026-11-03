"""健康检查：一次看到所有下游状态。

协议层的价值之一：前端只要打一个地址，就知道"谁活着、谁挂了"，
不用自己分别去探 8000 / 8001 / 8002。

**这里有两个探针，语义不同，别混用。**

    /api/health     聚合：relay + 三个下游。给人看的（监控、逐级验证）。
    /api/identity   浅探针：只回 `{"service": "relay"}`，不碰任何下游。
                    给程序探活的：dev.py 判断"占着 9000 的是不是本服务"。

为什么必须分开。聚合探针的耗时取决于**最慢的下游**，而 dev.py 是用它来回答
"这个端口上是 relay 吗"的——身份问题不该依赖下游状态。原来的写法里两侧超时
都是 5.0 秒，于是只要有一个下游"端口通但不应答"（ASR 正在推理长录音就是），
聚合必然 ≥5.0s，客户端必然先在 5.0s 放弃，结论变成"认不出本服务，可能是别的
程序占了这个端口"——把"自己在忙"误诊成"外人占位"，排查方向整个带偏。

**这两个端点不能互换**：把探活改回 /api/health，问题原样复发；而它只是变慢、
不会报错，所以任何测试都不会红。`scripts/check.py` 的 `probe` 那一项就是为此
存在的——它直接读 dev.py 的 SERVICES 表。
"""
import asyncio

from fastapi import APIRouter

from app.config import settings
from app.middleware import get_trace_id
from app.upstream import get_client

router = APIRouter()


async def _probe(name: str, url: str) -> dict:
    client = get_client()
    try:
        r = await client.get(url, timeout=5.0)
        body = None
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.text[:200]
        return {
            "downstream": name,
            "url": url,
            "status": "ok" if r.status_code < 400 else "error",
            "http_status": r.status_code,
            "detail": body,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "downstream": name,
            "url": url,
            "status": "unreachable",
            "detail": f"{type(e).__name__}: {str(e)[:150]}",
        }


@router.get("/api/identity")
async def identity():
    """浅探针：不聚合、不查余额、不碰下游。

    返回体和 /api/health 里的 `service` 字段保持一致（同一个值、同一种拼法），
    这样 dev.py 里"认得出 relay"的判据不用分两套。
    """
    return {"service": "relay"}


@router.get("/api/health")
async def health():
    results = await asyncio.gather(
        _probe("chat", f"{settings.chat_base_url}/api/health"),
        _probe("asr", f"{settings.asr_base_url}/health"),
        _probe("tts", f"{settings.tts_base_url}/health"),
    )
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "layer": "protocol",
        "service": "relay",
        "status": "ok" if ok_count == len(results) else "degraded",
        "upstream_ok": f"{ok_count}/{len(results)}",
        "trace_id": get_trace_id(),
        "downstreams": list(results),
    }
