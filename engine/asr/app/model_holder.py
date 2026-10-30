"""模型生命周期：加载一次，常驻内存，随进程退出释放。

**为什么必须常驻：** 加载 1.8GB 权重在 CPU 上要 1.5~2 秒，每次请求重载等于每次都多等两秒。
模型是"进程级资源"，不是"请求级资源"，这才是它必须独立成进程的根本原因。

**线程数是这个服务最关键的一行配置，但它的作用常被误解。** 早期版本在这里
写的是"不设线程 6.33 秒、设对线程 2.28 秒，差 2.8 倍"——`bench/asr_bench.py`
重测后这个说法不成立：耗时的大头是进程内**第一次**推理（预热，3.3~3.9 秒），
线程数只能解释其中 7~17% 的差异。

但 7~17% 也不是噪声。这台机器是 4 个 P 核 + 8 个 E 核的大小核架构（16 逻辑
核来自 P 核超线程），线程数一旦超过 P 核数，任务就会被派到 E 核上，再往上还
会让两个线程抢同一个核心的执行单元。实测 4 线程 2.44 秒、12 线程 2.75 秒、
16 线程 2.84 秒——**4 恰好是 P 核数，也是最慢档的一半**。
"""
import logging
import os
import time
from pathlib import Path

import torch

logger = logging.getLogger("asr.model")

BASE_DIR = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = BASE_DIR / "weights" / "Qwen3-ASR-0.6B"

# 线程数：封顶 4，核数不足的小机器用满。
#
# 为什么不用 os.cpu_count()：它返回逻辑核数，会把 E 核和超线程一起算进去。
# 0.6B 的模型在 CPU 上是**内存带宽受限**的，多出来的线程抢同一份带宽，只会
# 更慢（本机 16 线程比 4 线程慢 14%）。
#
# 为什么封顶而不是写死：2 核机器上开 4 个线程只会互相拖，min() 让它用满。
#
# 这台机器只是其中一个样本，别的架构（纯大核、更多核）最优值可能不同，
# 所以保留 ASR_THREADS 覆盖——而不是把某台机器的最优值当成普适结论。
THREADS = int(os.getenv("ASR_THREADS", str(min(4, os.cpu_count() or 4))))

_state: dict = {
    "model": None,
    "loaded": False,
    "load_seconds": 0.0,
    "device": "cpu",
    "threads": THREADS,
    "load_error": None,
}


def is_loaded() -> bool:
    return _state["loaded"]


def status() -> dict:
    return {
        "loaded": _state["loaded"],
        "device": _state["device"],
        "threads": _state["threads"],
        "load_seconds": round(_state["load_seconds"], 2),
        "weights_dir": str(WEIGHTS_DIR),
        "weights_exist": WEIGHTS_DIR.exists(),
        "error": _state["load_error"],
    }


def load() -> None:
    """加载模型到 CPU。失败不抛，记在 status 里，让 /health 能看出问题。"""
    if _state["loaded"]:
        return
    if not WEIGHTS_DIR.exists():
        _state["load_error"] = f"模型目录不存在：{WEIGHTS_DIR}"
        logger.error(_state["load_error"])
        return

    # 关键：先把线程数钉死，再加载。加载和推理都吃这个设置。
    torch.set_num_threads(THREADS)
    try:
        # 只能设置一次，且必须在任何并行工作之前；已有并行任务时会抛异常，忽略即可
        torch.set_num_interop_threads(max(1, THREADS // 2))
    except RuntimeError:
        pass

    from qwen_asr import Qwen3ASRModel

    started = time.time()
    try:
        # CPU 上必须 float32：bfloat16 在 CPU 上很多算子没有优化实现，反而更慢甚至报错
        model = Qwen3ASRModel.from_pretrained(
            str(WEIGHTS_DIR),
            dtype=torch.float32,
            device_map="cpu",
            max_inference_batch_size=1,
            # 60 秒中文语音约 240 字，留一倍余量
            max_new_tokens=512,
        )
    except Exception as e:  # noqa: BLE001
        _state["load_error"] = f"{type(e).__name__}: {str(e)[:500]}"
        logger.exception("模型加载失败")
        return

    _state["model"] = model
    _state["loaded"] = True
    _state["load_seconds"] = time.time() - started
    logger.info(
        "模型加载完成 | 耗时 %.2fs | 设备 %s | 线程 %d",
        _state["load_seconds"], _state["device"], THREADS,
    )


def transcribe(wav_path: str) -> dict:
    """同步识别（阻塞）。调用方负责丢到线程池，别占事件循环。"""
    model = _state["model"]
    if model is None:
        raise RuntimeError(_state["load_error"] or "模型尚未加载")

    started = time.time()
    results = model.transcribe(audio=wav_path, language=None)  # None = 自动判语种
    elapsed = time.time() - started

    r = results[0]
    text = (getattr(r, "text", "") or "").strip()
    language = getattr(r, "language", None)
    return {
        "text": text,
        "language": language,
        "infer_seconds": round(elapsed, 2),
    }


def unload() -> None:
    _state["model"] = None
    _state["loaded"] = False
