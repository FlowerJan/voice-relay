"""聊天接口：POST /api/chat/stream（SSE 流式）+ GET /api/health + GET /api/balance。"""
import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.schemas import ChatRequest
from app.services.balance_service import BalanceError, fetch_balance
from app.services.chat_service import stream_chat

router = APIRouter()
logger = logging.getLogger(__name__)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    # 每个请求留一行：没有它，成功路径在日志里就是一段空白，
    # 想确认"请求到底进没进这一层"只能靠猜。trace_id 由日志格式自动带上。
    logger.info("收到请求：历史 %d 条", len(req.messages))

    async def event_generator():
        async for event in stream_chat(req.messages):
            yield _sse(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/health")
async def health():
    # 带上 service/port：dev.py 靠它确认"占着 8000 的确实是自己"，
    # 少了这个字段，一个恰好返回 {"status":"ok"} 的别家程序会被误认。
    return {
        "status": "ok",
        "layer": "model",
        "service": "chat",
        "port": 8000,
        "model": settings.deepseek_model,
    }


@router.get("/api/balance")
async def balance():
    """查询 DeepSeek 账户余额。阻塞的 HTTP GET 丢到线程池，不占事件循环。"""
    try:
        data = await asyncio.to_thread(fetch_balance)
    except BalanceError as e:
        return JSONResponse(
            status_code=e.status_code, content={"ok": False, "message": e.message}
        )
    return {"ok": True, **data}
