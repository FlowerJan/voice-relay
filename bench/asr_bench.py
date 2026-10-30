"""ASR 基准：线程数与耗时的关系，一条命令给出可复现的表格。

**为什么主指标用"相对"，不用绝对秒数。**
绝对耗时绑死在这台机器上：换个 CPU、别人的笔记本上多开两个应用，数字全变，
读者只能选择相信你。相比之下"4 线程之后加线程没有收益"这种**形状**
是可以跨机器成立的，也才是真正值得写进文档的结论。
所以表格里同时给绝对值和实时倍率，但结论落在形状上。

**这个脚本存在的原因，是一次自我纠错。**
项目早期一版文档写过"钉线程后 6.33s → 2.28s，快 2.8 倍"。用这个脚本实测后：
4~16 线程的稳定态都在 2.5~2.8s，差异在 11% 以内；而进程内**第一次**推理
比稳定态慢 1.4s 左右。也就是说，原数字里能找到的机制只有"预热"，
而预热最多解释 1.5 倍，解释不了 2.8 倍——它很可能是三段不同条件下的测量拼在一起
（含预热的首次 + 更长的音频 + 另一个 torch 版本）。
所以文档改成了现在这版：**只写这次能复现的东西**。
一个站不住的数字，比没有数字更伤——面试官自己跑一遍就露馅了。

**跑法（必须用 ASR 那套 venv，里面才有 qwen_asr 和 torch）：**

    engine/asr/venv/Scripts/python.exe bench/asr_bench.py

    # 指定档位与轮次
    engine/asr/venv/Scripts/python.exe bench/asr_bench.py --threads 2,4,8 --rounds 5
"""
from __future__ import annotations

import argparse
import os
import platform
import socket
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASR_DIR = ROOT / "engine" / "asr"
DEFAULT_WAV = "samples/asr_zh.wav"
WEIGHTS = ASR_DIR / "weights" / "Qwen3-ASR-0.6B"

# 线程数差异小于这个比例就当作噪声：小模型在 CPU 上受内存带宽限制，
# 多给核心也喂不满，"差 5%" 并不代表"多两线程有用"。
NOISE_THRESHOLD = 0.15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASR 线程数与耗时基准")
    parser.add_argument("--wav", default=DEFAULT_WAV, help="音频路径（相对 engine/asr）")
    parser.add_argument("--rounds", type=int, default=3, help="每档跑几遍取中位数")
    parser.add_argument(
        "--threads",
        default="",
        help="逗号分隔的线程数；留空则自动取「4 / torch 默认 / 逻辑核数」三档",
    )
    return parser.parse_args()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def cpu_name() -> str:
    return (
        os.environ.get("PROCESSOR_IDENTIFIER")
        or platform.processor()
        or platform.machine()
        or "unknown"
    )


def main() -> int:
    args = parse_args()

    wav = Path(args.wav)
    if not wav.is_absolute():
        wav = ASR_DIR / wav
    if not wav.exists():
        print(f"找不到音频：{wav}", file=sys.stderr)
        return 2
    if not WEIGHTS.exists():
        print(f"找不到模型权重：{WEIGHTS}\n先跑 engine/asr/download_model.sh", file=sys.stderr)
        return 2

    try:
        import soundfile as sf
        import torch
        from qwen_asr import Qwen3ASRModel
    except ImportError as exc:
        print(
            f"导入失败：{exc}\n"
            "这个脚本必须用 ASR 的虚拟环境跑：\n"
            "  engine/asr/venv/Scripts/python.exe bench/asr_bench.py",
            file=sys.stderr,
        )
        return 2

    if port_in_use(8001):
        print("警告：8001 上已有 ASR 服务在跑，测出的耗时会偏高。建议先停掉。\n")

    info = sf.info(str(wav))
    duration = info.frames / info.samplerate

    default_threads = torch.get_num_threads()
    logical = os.cpu_count() or default_threads
    if args.threads.strip():
        wanted = sorted({int(x) for x in args.threads.split(",") if x.strip()})
    else:
        # 三档正好回答"代码里该设多少"：饱和点附近 / 不设时的默认 / 逻辑核数
        wanted = sorted({4, default_threads, logical})

    print(f"加载模型 {WEIGHTS.name} ...")
    load_started = time.time()
    model = Qwen3ASRModel.from_pretrained(
        str(WEIGHTS),
        dtype=torch.float32,
        device_map="cpu",
        max_inference_batch_size=1,
        max_new_tokens=512,
    )
    load_seconds = time.time() - load_started

    # 先做一次预热，不计入任何档位。
    # 进程内第一次推理要建 oneDNN 执行计划、启动线程池，往往比稳定态慢一半以上；
    # 不预热的话，谁排在第一个谁背这个锅，表格会变成"线程越少越慢"的假象。
    torch.set_num_threads(default_threads)
    print("预热（进程内第一次推理，不计入统计）...")
    started = time.time()
    warm = model.transcribe(audio=str(wav), language=None)
    warmup_seconds = time.time() - started
    text = (getattr(warm[0], "text", "") or "").strip()
    print(f"加载 {load_seconds:.2f}s | 预热 {warmup_seconds:.2f}s\n")

    rows = []
    for threads in wanted:
        torch.set_num_threads(threads)
        times: list[float] = []
        for _ in range(max(2, args.rounds)):
            started = time.time()
            result = model.transcribe(audio=str(wav), language=None)
            times.append(time.time() - started)
            text = (getattr(result[0], "text", "") or "").strip()
        median = statistics.median(times)
        rows.append(
            {
                "threads": threads,
                "median": median,
                "best": min(times),
                "worst": max(times),
                "rtf": duration / median,
            }
        )
        print(f"线程 {threads:>2}：中位 {median:.2f}s（{min(times):.2f}~{max(times):.2f}）")

    slowest = max(row["median"] for row in rows)
    for row in rows:
        row["speedup"] = slowest / row["median"]

    fastest = min(rows, key=lambda row: row["median"])
    spread = (slowest - fastest["median"]) / fastest["median"]
    stable_reference = statistics.median(row["median"] for row in rows)

    print()
    print("<!-- 由 bench/asr_bench.py 生成 -->")
    print()
    print("| 项目 | 值 |")
    print("|---|---|")
    print(f"| CPU | {cpu_name()} |")
    print(f"| 逻辑核 / torch 默认线程 | {logical} / {default_threads} |")
    print(f"| torch | {torch.__version__} |")
    print(f"| 音频 | `{wav.name}`，{duration:.2f}s |")
    print(f"| 模型加载 | {load_seconds:.2f}s（一次性，进程常驻后不再付） |")
    print(f"| 首次推理 | {warmup_seconds:.2f}s（一次性，含内核与线程池初始化） |")
    print()
    print("| 线程数 | 稳定态（中位数） | 波动范围 | 实时倍率 | 相对最慢档 |")
    print("|---|---|---|---|---|")
    for row in rows:
        print(
            f"| {row['threads']} | {row['median']:.2f}s "
            f"| {row['best']:.2f}~{row['worst']:.2f}s "
            f"| {row['rtf']:.2f}x | {row['speedup']:.2f}x |"
        )
    print()

    warmup_cost = warmup_seconds - stable_reference
    print(
        f"**预热**：首次推理 {warmup_seconds:.2f}s，稳定态约 {stable_reference:.2f}s，"
        f"多出 {warmup_cost:.2f}s。这部分与线程数无关，只影响重启后的第一句。"
    )
    if spread < NOISE_THRESHOLD:
        print(
            f"**线程饱和**：{fastest['threads']} 线程最快，与最慢档差 {spread * 100:.0f}%，"
            f"落在波动范围（每档 {min(r['worst'] - r['best'] for r in rows):.2f}s 起）以内。"
            "0.6B 的模型在 CPU 上受内存带宽限制，线程再多也喂不满。"
        )
    else:
        print(
            f"**线程仍有收益**：{fastest['threads']} 线程最快，"
            f"最慢档比它慢 {spread * 100:.0f}%。"
        )

    # 当前代码设的是 os.cpu_count()（逻辑核数）。如果它不是最快的，
    # 说明超线程在这里带来的是线程争用而不是吞吐——这一点值得直接说出来。
    logical_row = next((row for row in rows if row["threads"] == logical), None)
    if logical_row and fastest["threads"] != logical:
        over = (logical_row["median"] - fastest["median"]) / fastest["median"]
        print(
            f"**当前配置可能偏高**：`engine/asr` 现在按逻辑核数 {logical} 线程启动，"
            f"比 {fastest['threads']} 线程慢 {over * 100:.0f}%。"
            "小模型在 CPU 上受内存带宽限制，超出的线程只增加争用。"
            "要改的话设 `ASR_THREADS` 即可，不用动代码。"
        )
    print()
    print(f"识别结果：{text}")
    print()
    print(
        f"> 60 秒录音按最快档的倍率约需 {60 / fastest['rtf']:.0f} 秒。"
        "这是等待语音转文字的真实体验，也是 ASR 必须独立成进程的原因——"
        "它的延迟不能和聊天共用同一个事件循环。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
