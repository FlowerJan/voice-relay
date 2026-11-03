"""文档抽取转发：multipart 透传到 8000 的 /api/extract。

和 ASR 转发是同一个套路（见 asr.py），差别只有超时：
抽取是本地 CPU 解析，一个 200 页 PDF 可能要十几秒，但不像 ASR 那样慢到两分钟，
所以超时给 90 秒，比聊天短、比普通接口长。

**为什么不让协议层自己做抽取**
pymupdf 自带 onnxruntime，装上就是几十 MB 的依赖。协议层要一直保持
"只有 fastapi + httpx" 这种随时能重建的轻量状态——它是唯一对外的一层。
"""
import httpx
from fastapi import APIRouter, Request

from app.body import declared_too_large, limit_text
from app.config import settings
from app.errors import error_response as _err
from app.middleware import TRACE_HEADER, get_trace_id
from app.upstream import get_client

router = APIRouter()


@router.post("/api/extract")
async def extract(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return _err(400, "bad_request", "需要 multipart/form-data 上传文件")

    # 这条链路也是边收边转。上限比 20MB 文件的限额多留 4MB：
    # multipart 的头、boundary、字段名都算在 body 里，卡死 20MB 会把
    # 刚好 20MB 的合法文件一起拒掉。
    if declared_too_large(request, settings.extract_max_body):
        return _err(
            413,
            "extract_body_too_large",
            f"请求体超过 {limit_text(settings.extract_max_body)} 上限（文件本身限 20MB）",
        )

    client = get_client()
    headers = {"Content-Type": content_type, TRACE_HEADER: get_trace_id()}

    upstream_req = client.build_request(
        "POST",
        f"{settings.chat_base_url}/api/extract",
        content=request.stream(),  # 流式转发：20MB 文件不在网关这层攒整段
        headers=headers,
        timeout=httpx.Timeout(
            connect=10.0,
            read=settings.extract_timeout,
            write=settings.extract_timeout,
            pool=None,
        ),
    )

    try:
        upstream = await client.send(upstream_req)
    except httpx.ConnectError:
        return _err(502, "chat_unreachable", "聊天服务（8000）未启动或无法连接")
    except httpx.ReadTimeout:
        return _err(504, "extract_timeout", f"文件解析超时（>{settings.extract_timeout:.0f} 秒）")
    except httpx.HTTPError as e:
        return _err(502, "extract_error", f"文件解析服务通信失败：{str(e)[:150]}")

    body = await upstream.aread()
    await upstream.aclose()

    try:
        payload = upstream.json()
    except Exception:  # noqa: BLE001
        return _err(502, "extract_bad_response", body.decode("utf-8", "ignore")[:300] or "解析服务返回了非 JSON")

    if upstream.status_code >= 400:
        # 下游是 {ok:false, message}，这里统一翻成前端的错误形状一次，
        # 免得前端要同时处理两种错误体
        msg = payload.get("message") if isinstance(payload, dict) else None
        return _err(upstream.status_code, "extract_failed", msg or "文件解析失败")

    return payload
