"""健康检查：一次看到所有下游状态。

协议层的价值之一：前端只要打一个地址，就知道"谁活着、谁挂了"，
不用自己分别去探 8000 / 8001 / 8002。
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
