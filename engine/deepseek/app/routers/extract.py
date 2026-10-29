"""文档抽取接口：POST /api/extract（multipart 上传单个文件）。

返回纯文本，前端把它作为附件挂在消息上再发给模型。

**为什么用 multipart 而不是 base64 塞进 JSON**
base64 会让体积膨胀 33%，而且 JSON 解析要把整个字符串读进内存才能开始干活。
multipart 能边收边写，这里是同步解析、必须收完才能抽，但至少不用付 base64 的税。
"""
import asyncio

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from app.services import extract_service
from app.services.extract_service import ExtractError

router = APIRouter()

# 单文件上限。与前端的上限保持一致：前端先拦一次是给用户即时反馈，
# 后端这一层是真正的防线（绕过前端直接打接口时仍然有效）。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "message": message})


@router.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return _fail(413, f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB，请先拆分或压缩")
    if not data:
        return _fail(400, "文件是空的")

    try:
        # 解析是纯 CPU 活（PDF 排版还原尤其慢），丢线程池，别把事件循环堵住——
        # 堵住的后果不是这一个文件慢，而是同一时刻的聊天流式也会卡住。
        result = await asyncio.to_thread(
            extract_service.extract, file.filename or "", data
        )
    except ExtractError as e:
        return _fail(400, str(e))
    except Exception as e:  # noqa: BLE001
        # 兜底：解析库的异常类型太多（zip 损坏、XML 结构怪、字体缺失），
        # 统一收口成一句人话，不把堆栈丢给前端
        return _fail(400, f"解析失败：{type(e).__name__}: {str(e)[:150]}")

    return result.as_dict()
