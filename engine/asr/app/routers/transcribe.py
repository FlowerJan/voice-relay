"""ASR 接口：POST /asr/transcribe（内部路径，网关映射成对外的 /api/asr）。

只干一件事：**音频进，文字出**。
它不知道 DeepSeek 存在，也不需要知道——语音输入是"更快的打字"，不是第二个聊天入口。
"""
import asyncio
import logging

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from app import audio, model_holder

logger = logging.getLogger("asr.api")
router = APIRouter()

MIN_SECONDS = 0.5      # 短于此直接丢弃（用户误触）
MAX_SECONDS = 120.0    # 硬上限，超过直接拒（前端 60 秒自动停，这里留余量）


def _err(status: int, code: str, message: str):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "status": status}},
    )


@router.post("/asr/transcribe")
async def transcribe(file: UploadFile = File(...)):
    if not model_holder.is_loaded():
        return _err(503, "model_not_loaded", "模型尚未加载完成，请稍后重试")

    raw = await file.read()
    if not raw:
        return _err(400, "empty_audio", "音频为空")

    suffix = "." + (file.filename or "audio.webm").rsplit(".", 1)[-1]
    tmp_src = tmp_wav = None
    try:
        # ffmpeg 转码是阻塞的，丢线程池
        tmp_src, tmp_wav = await asyncio.to_thread(audio.transcode_bytes, raw, suffix)

        dur = audio.duration_seconds(tmp_wav)
        if dur < MIN_SECONDS:
            return _err(400, "audio_too_short", f"音频太短（{dur:.2f} 秒），请说完整一句")
        if dur > MAX_SECONDS:
            return _err(400, "audio_too_long", f"音频太长（{dur:.1f} 秒），上限 {MAX_SECONDS:.0f} 秒")

        # 模型推理是阻塞的（CPU 自回归逐字生成），必须丢线程池，否则卡死整个事件循环
        result = await asyncio.to_thread(model_holder.transcribe, str(tmp_wav))

        if not result["text"]:
            return _err(422, "empty_text", "没识别出文字，可能是静音或噪声")

        logger.info(
            "识别完成 | 时长 %.2fs | 推理 %.2fs | 语种 %s | %s",
            dur, result["infer_seconds"], result["language"], result["text"][:40],
        )
        return {
            "ok": True,
            "text": result["text"],
            "language": result["language"],
            "duration": round(dur, 2),
            "infer_seconds": result["infer_seconds"],
        }
    except audio.AudioError as e:
        return _err(422, "audio_error", str(e)[:300])
    except Exception as e:  # noqa: BLE001
        logger.exception("识别失败")
        return _err(500, "asr_failed", f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        if tmp_src:
            audio.cleanup(tmp_src.parent)
