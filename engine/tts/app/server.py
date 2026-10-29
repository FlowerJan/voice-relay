"""TTS 服务入口（模型层 · 说）· 端口 8002。

整段合成：收到全文才合成一次，不做"边生成边播"。
理由见设计文档 5.4——边播要按句切分 + 逐段合成 + 队列 + 时序 + 中断，
复杂度换来的只是首字快几秒，而真正的瓶颈是"模型少说废话"。

引擎用 edge-tts（微软 Edge 的在线合成）：免费、中文音质好、无需 Key。
代价是**依赖外网**，断网时这个服务只能报错，不影响聊天和语音输入。
"""
import logging

import edge_tts
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.clean_text import to_speech_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("tts.api")

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

app = FastAPI(title="语音文字智能体 · TTS 服务（模型层）", version="2.0")


class TTSRequest(BaseModel):
    text: str = Field(..., description="要朗读的文本，可以是 Markdown，服务端会清洗")
    voice: str | None = Field(None, description="音色，默认 zh-CN-XiaoxiaoNeural")
    rate: str | None = Field(None, description="语速，如 '+10%' / '-10%'")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "layer": "model",
        "service": "tts",
        "port": 8002,
        "engine": "edge-tts",
        "default_voice": DEFAULT_VOICE,
    }


@app.get("/")
async def root():
    return {"layer": "model", "service": "tts", "port": 8002, "docs": "/docs"}


@app.post("/tts/synthesize")
async def synthesize(req: TTSRequest):
    """文字 → mp3 字节流。"""
    speech_text = to_speech_text(req.text)
    if not speech_text:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "empty_text",
                               "message": "清洗后没有可朗读的内容（可能整段都是代码）",
                               "status": 400}},
        )

    voice = req.voice or DEFAULT_VOICE
    kwargs = {}
    if req.rate:
        kwargs["rate"] = req.rate

    try:
        communicate = edge_tts.Communicate(speech_text, voice, **kwargs)
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
    except Exception as e:  # noqa: BLE001
        logger.exception("合成失败")
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "tts_failed",
                               "message": f"合成失败（edge-tts 依赖外网）：{type(e).__name__}: {str(e)[:200]}",
                               "status": 502}},
        )

    if not audio:
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "tts_empty", "message": "合成结果为空", "status": 502}},
        )

    logger.info("合成完成 | 原文 %d 字 → 朗读稿 %d 字 | %d 字节 | %s",
                len(req.text), len(speech_text), len(audio), voice)

    return Response(
        content=bytes(audio),
        media_type="audio/mpeg",
        headers={
            "X-Speech-Chars": str(len(speech_text)),
            "X-TTS-Voice": voice,
            "Cache-Control": "no-store",
        },
    )
