"""一条命令跑完全部检查，末尾给 PASS/FAIL 汇总。

**为什么需要它。** 这套工程有六个可独立运行的检查，分散在四个目录、两种语言、
四个互不相干的虚拟环境里。没有 CI 的时候，"这些测试都是绿的"只能靠记忆，
而记忆是最不可靠的证词。跑一条命令、看到 6/6，才是一个可以被别人验证的说法。

**为什么不用一条 pytest 扫全仓库。** 四个服务各自独立 venv 是这套架构的设计决定
（互不污染：装了 pymupdf 的引擎不会把 openai 的版本传给协议层）。
代价就摆在下面这张表里——必须显式列出每个解释器的路径，不能靠一个全局解释器。
这个代价是值得的，但它确实存在，所以写在这里而不是藏着。

**用法：**

    python scripts/check.py            # 全部（不含端到端）
    python scripts/check.py --live     # 再跑一组真起 uvicorn 的端到端用例
    python scripts/check.py --only relay,view
    python scripts/check.py -v         # 成功项也打印输出

退出码：全绿 0，有失败 1。可以直接挂在 git hook 或 CI 上。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 四个独立解释器 —— 隔离的代价，见文件头。
PY_RELAY = ROOT / "relay" / "venv" / "Scripts" / "python.exe"
PY_CHAT = ROOT / "engine" / "deepseek" / "venv" / "Scripts" / "python.exe"
PY_ASR = ROOT / "engine" / "asr" / "venv" / "Scripts" / "python.exe"
NPM = shutil.which("npm") or "npm"


@dataclass
class Check:
    key: str
    label: str
    command: str
    cwd: Path
    # 从输出里提取"跑了多少项"，用于汇总。提取不到就留空，不影响判定。
    count_pattern: str = r"(\d+)\s*(?:项通过|passed)"


CHECKS: list[Check] = [
    Check(
        "relay",
        "协议层：事件契约 + 分路超时 + 错误收口 + 流式",
        f'"{PY_RELAY}" -m pytest -q',
        ROOT / "relay",
    ),
    Check(
        "prompt",
        "聊天引擎：系统提示词与历史预算",
        f'"{PY_CHAT}" -m pytest -q',
        ROOT / "engine" / "deepseek",
    ),
    Check(
        "extract",
        "文档抽取：PDF / Word / Excel / 编码 / 扫描件",
        f'"{PY_CHAT}" _extract_test.py',
        ROOT / "engine" / "deepseek",
    ),
    Check(
        "session",
        "前端：会话持久化 / 搜索 / 消息 ID / 附件注入",
        f'"{NPM}" run test:session',
        ROOT / "view",
    ),
    Check(
        "tts",
        "前端：播报切段",
        f'"{NPM}" run test:tts',
        ROOT / "view",
    ),
    Check(
        "types",
        "前端：类型检查（tsc --noEmit）",
        f'"{NPM}" run typecheck',
        ROOT / "view",
        count_pattern=r"(^$)",  # typecheck 不打项数，这里不提取
    ),
]

# 端到端用例单独一项、默认不跑。理由不是"跑不了"：它要起两个 uvicorn 进程、
# 等端口、真等一次读超时，十几秒起步，而上面六套加起来只要 6 秒。把它塞进
# 默认列表的后果是"改一行等二十秒"，人就会开始跳过检查。
# 但它必须存在、且能被一条命令叫出来，否则等于没有。
LIVE_CHECK = Check(
    "live",
    "协议层：端到端（真起 uvicorn 与上游，7 项）",
    f'"{PY_RELAY}" -m pytest -q -m live',
    ROOT / "relay",
)


def run(check: Check, verbose: bool) -> tuple[bool, str, int]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 否则中文输出在管道里会变成乱码
    started = time.time()
    try:
        completed = subprocess.run(  # noqa: S602 —— 命令全部硬编码，无外部输入
            check.command,
            cwd=str(check.cwd),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "超时（>600s）", 600

    elapsed = int(time.time() - started)
    output = (completed.stdout or "") + (completed.stderr or "")

    count = ""
    if check.count_pattern != r"(^$)":
        matches = re.findall(check.count_pattern, output)
        if matches:
            count = matches[-1]

    ok = completed.returncode == 0
    if not ok or verbose:
        print()
        print(f"───── {check.label} 输出 ─────")
        tail = output.strip().splitlines()
        print("\n".join(tail[-30:] if not ok else tail))
        print("─────────────────────────────")
    return ok, count, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="跑完所有检查并汇总")
    parser.add_argument(
        "--only", default="",
        help="只跑指定项，逗号分隔（relay,prompt,extract,session,tts,types,live）")
    parser.add_argument(
        "--live", action="store_true",
        help="额外跑端到端用例（真起 uvicorn，约 10 秒）")
    parser.add_argument("-v", "--verbose", action="store_true", help="成功项也打印输出")
    args = parser.parse_args()

    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    selected = [c for c in CHECKS if not wanted or c.key in wanted]
    if "live" in wanted or (args.live and not wanted):
        selected.append(LIVE_CHECK)
    if not selected:
        print(f"没有匹配的检查项：{args.only}", file=sys.stderr)
        return 2

    print("═══ 全量检查 ═══")
    print(f"目录  {ROOT}")
    print(f"Python {sys.version.split()[0]}　检查项 {len(selected)}")
    print()

    results = []
    total_seconds = 0
    for index, check in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {check.label} ... ", end="", flush=True)
        ok, count, elapsed = run(check, args.verbose)
        total_seconds += elapsed
        results.append((check, ok, count, elapsed))
        status = "PASS" if ok else "FAIL"
        count_text = f"{count} 项" if count else "—"
        print(f"{status}  {count_text:>6}  {elapsed}s")

    failed = [item for item in results if not item[1]]
    numbers = [int(item[2]) for item in results if item[2]]
    total_items = sum(numbers) if numbers else 0

    print()
    print("─── 汇总 ───")
    passed = len(results) - len(failed)
    print(f"{passed}/{len(results)} 套通过，共 {total_items} 项，耗时 {total_seconds}s")
    if failed:
        for check, _, _, _ in failed:
            print(f"  失败：{check.key}　{check.label}")
    else:
        print("全部通过。")
        # README 里那个"168 项"应该是这个数，不是手写的。
        print(f"\n可以写进文档的项数：{total_items}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
