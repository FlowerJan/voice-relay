"""ASR 冒烟测试：CPU 加载 + 真实识别。

这一步是本项目最大的技术风险：Qwen3-ASR 官方所有示例都用 cuda:0，
Windows + CPU 组合官方从未验证过。所以单独留一个能重复跑的最小验证。
"""
import os
import time

import torch

WAV = "samples/asr_zh.wav"
WEIGHTS = "weights/Qwen3-ASR-0.6B"
# 与 app/model_holder.py 保持一致：封顶 4，小机器用满。改一处就要改两处，
# 否则冒烟测试和服务跑的不是同一套线程设置，"测过了"就没有意义。
THREADS = int(os.getenv("ASR_THREADS", str(min(4, os.cpu_count() or 4))))

print(f"[1] 环境 | torch={torch.__version__} | cuda={torch.cuda.is_available()} | 线程={THREADS}")
torch.set_num_threads(THREADS)

print(f"[2] 加载模型 {WEIGHTS} ...")
t0 = time.time()
from qwen_asr import Qwen3ASRModel

model = Qwen3ASRModel.from_pretrained(
    WEIGHTS,
    dtype=torch.float32,   # CPU 必须 float32
    device_map="cpu",
    max_inference_batch_size=1,
    max_new_tokens=512,
)
load_s = time.time() - t0
n_params = sum(p.numel() for p in model.model.parameters()) / 1e9
print(f"    加载完成 | {load_s:.2f}s | 参数量 {n_params:.3f}B")

print(f"[3] 识别 {WAV}（跑两遍：第一遍含预热，第二遍看稳定态）")
import soundfile as sf

info = sf.info(WAV)
dur = info.frames / info.samplerate

for i in (1, 2):
    t0 = time.time()
    results = model.transcribe(audio=WAV, language=None)
    infer_s = time.time() - t0
    r = results[0]
    tag = "含预热" if i == 1 else "稳定态"
    print(f"    第{i}遍（{tag}）| 推理 {infer_s:.2f}s | 速度比 {dur / infer_s:.2f}x 实时 | 语种 {r.language}")
    if i == 1:
        print(f"    文字 = {r.text}")

print()
print("[4] 结论：稳定态推理速度决定 60 秒录音要等多久")
