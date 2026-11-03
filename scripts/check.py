"""一条命令跑完全部检查，末尾给 PASS/FAIL 汇总。

**为什么需要它。** 这套工程有八个可独立运行的检查，分散在四个目录、两种语言、
四个互不相干的虚拟环境里。没有 CI 的时候，"这些测试都是绿的"只能靠记忆，
而记忆是最不可靠的证词。跑一条命令、看到 8/8，才是一个可以被别人验证的说法。

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

**写新测试时的第一原则：它必须能给出可用信号。**
一个检查的价值不在"它存在"，而在"它红了就是真有事、它绿了就是真没事"。三种失效
方式都会让这一点落空，而它们的**症状完全不同、危险程度却一样**：

1. **永远绿**——断言对象被别的东西顶掉了。本项目撞过两次：一次是把
   `trace.configure()` 注释掉后，注释里的字符串还在源码里，检查照样 PASS；一次是
   `app/tests/` 里一份测试代码里也写了 `trace.configure()`，把真正漏掉的那处顶掉。
   解法是先剥掉注释与文档串（`_code_only`），再显式排除测试文件（`_is_runtime_code`）。
2. **永远绿（更隐蔽）**——检查器认"某种写法"，而代码换了个同样正确的写法。第一次
   运行 trace 装配检查时报了三条假 FAIL，就是因为它只认 `run.py` 里的 `configure()`，
   而三个引擎写在模块顶部。**误报会喊，假绿不会**：宁可从宽（扫目录）也不要从窄。
3. **永远不返回**——不是红也不是绿。本项目也撞过：负向验证去掉并发上限后，两个
   handler 都在等一个永远不会被 set 的 `gate`，`check.py` 直接卡住。表现成"环境
   有问题"，而不是"测试失败"。**所以每个可能阻塞的测试都要有超时**
   （`asyncio.wait_for` / `timeout=`），让它在秒级明确失败。

一句话：**一个会挂死的测试，和一个永远不会红的检查，是同一类问题**——都没有给出
可用的信号。写完一条测试，先故意改坏被测代码走一遍，看它是"红"还是"没反应"。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
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
    # 有的检查不需要起进程（例如"扫源码确认装配接上了"），给一个函数就地跑。
    fn: Callable[[], tuple[bool, str]] | None = None


# 四跳 trace 的"装配"检查：代码对 ≠ 接上了。
#
# 四份 trace.py 是有意复制的（各自可独立删除），所以"测一份"只证明代码本身对；
# 它在每一跳能不能真的生效，取决于三件互不相干的事：入口挂了中间件、启动器装了
# Formatter、trace 模块里有兜底类。任何一件漏了，那一跳的日志就永远没有 trace，
# 而**现有测试一条都不会红**——请求成功、探活成功、只是搜不到。
#
# 这就是"缺少负向覆盖"的典型形态：功能失效不产生任何失败信号。
#
# 检查扫描的是 `app/` **整个目录**，不是几个固定文件。原因是"放哪儿"是自由的：
# relay 在 run.py 里调 configure()，三个引擎在 app/main.py、app/server.py 的模块
# 顶部调——两者等价（都早于第一个请求），写死其中一个位置就会误报，这正是它第一
# 次运行时报的那三条 FAIL。
#
# 为什么不扫整个服务目录（含 tests/）：**方向反了会制造假绿。** 测试里同样会
# `app.add_middleware(trace.TraceMiddleware)` 造一个被测应用，扫进去就等于
# "main.py 忘了挂中间件"这件事被测试文件顶掉了——检查永远 PASS，比误报糟得多。
# `app/` 是运行时代码的唯一位置，run.py 绝不会 import tests/。
#
# 边界还得再往里收一格：`app/` 里也可能出现 `tests/` 或 `test_*.py`（同一个人、
# 同一类失误，只是放深了一层）。所以 `_read_tree` 显式跳过 tests 目录、test_*.py、
# conftest.py 和 __pycache__。这是**已实测的漏洞**：在 `engine/asr/app/tests/`
# 塞一行 `trace.configure()`、再把真配置注释掉，过滤前检查照样 PASS。
TRACE_JOINS = [
    ("relay :9000", "relay/run.py", "relay/app",
     "TraceIdMiddleware", "relay/app/trace.py"),
    ("chat  :8000", "engine/deepseek/run.py", "engine/deepseek/app",
     "trace.TraceMiddleware", "engine/deepseek/app/trace.py"),
    ("asr   :8001", "engine/asr/run.py", "engine/asr/app",
     "trace.TraceMiddleware", "engine/asr/app/trace.py"),
    ("tts   :8002", "engine/tts/run.py", "engine/tts/app",
     "trace.TraceMiddleware", "engine/tts/app/trace.py"),
]


def _read(rel: str) -> str | None:
    path = ROOT / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


#: 扫描运行时代码时要跳过的目录名与文件名前缀。理由是**假绿**而不是洁癖：
#: 测试代码里同样会写 `trace.configure()`，扫进去就能顶掉真正漏掉的那一处。
_SKIP_DIRS = {"__pycache__", "tests", "test", "venv", ".venv", "node_modules"}
_SKIP_FILE_PREFIXES = ("test_", "conftest")


def _is_runtime_code(path: Path) -> bool:
    """这个 .py 是不是运行时代码（而不是测试 / 缓存 / 依赖）。"""
    if any(part in _SKIP_DIRS for part in path.parts):
        return False
    return not path.name.startswith(_SKIP_FILE_PREFIXES)


def _read_tree(rel_dir: str) -> str:
    """把一个包目录下的运行时代码拼成一段（递归、已去注释与文档串）。

    只收 `.py`：`__pycache__` 里全是 `.pyc`，靠后缀天然被挡住；但目录本身仍显式
    跳过，免得以后有人往那里放 `.py`。
    """
    root = ROOT / rel_dir
    if not root.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if not _is_runtime_code(path.relative_to(root)):
            continue
        try:
            parts.append(_code_only(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return "\n".join(parts)


def _code_only(src: str) -> str:
    """去掉文档字符串与整行注释，只留可执行代码。

    **不这么做，这个检查就是个摆设。** 负向验证时正好撞到：把
    `trace.configure()` 注释掉，检查依旧 PASS——因为注释里的那串字符还在。
    一个永远不会红的检查比没有检查更糟：它让你以为有人在看着。
    """
    no_doc = re.sub(r'"""(?:.|\n)*?"""', "", src)
    return "\n".join(
        line for line in no_doc.splitlines() if not line.lstrip().startswith("#")
    )


def check_trace_assembly() -> tuple[bool, str]:
    """三处装配缺一不可：中间件、Formatter、configure。

    扫描范围是 `app/` 整个包 + 启动器（见 TRACE_JOINS 上面的说明）。这样"把
    configure() 挪到另一个模块"不会误报，"在测试里挂一次中间件"也不会造成假绿。
    """
    problems: list[str] = []
    table: list[str] = []

    for name, runner, pkg, middleware, trace_mod in TRACE_JOINS:
        runner_src = _code_only(_read(runner) or "")
        pkg_src = _read_tree(pkg)
        trace_src = _code_only(_read(trace_mod) or "")
        joined_src = f"{runner_src}\n{pkg_src}"
        bad: list[str] = []

        if not pkg_src:
            bad.append(f"缺目录 {pkg}")
        elif middleware not in pkg_src:
            bad.append(f"{pkg}/** 里没挂 {middleware} → 这一跳不读 X-Trace-Id")

        if not runner_src:
            bad.append(f"缺启动器 {runner}")
        elif "trace.configure(" not in joined_src:
            bad.append(
                f"{runner} 与 {pkg}/** 都没调 trace.configure() → 日志格式不带 %(trace)s")

        if not trace_src:
            bad.append(f"缺 {trace_mod}")
        elif "class TraceFormatter" not in trace_src:
            bad.append(f"{trace_mod} 没有 TraceFormatter → 漏 filter 时 KeyError 吞日志")

        table.append(f"{'OK  ' if not bad else '缺失'} {name}")
        problems.extend(f"{name}: {item}" for item in bad)

    detail = "\n".join(table)
    if problems:
        detail += "\n\n" + "\n".join(f"  - {item}" for item in problems)
    return not problems, detail


def _load_dev():
    """把 dev.py 当模块跑一遍，只取它的 SERVICES 表。

    不用正则扫源码：那张表怎么排版、字段顺序怎么变，这里都不用跟着改。
    dev.py 顶层只有常量、dataclass 和函数定义（主流程在 `__main__` 里），
    所以导入它没有副作用，也不依赖任何第三方包。
    """
    spec = importlib.util.spec_from_file_location("_dev_launcher_for_check", ROOT / "dev.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再 exec：dataclass 在装饰阶段会回头查
    # `sys.modules[cls.__module__].__dict__`，不登记就是 AttributeError。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def check_probe_identity() -> tuple[bool, str]:
    """身份探针必须是**浅**的，而且 dev.py 与 relay 指的是同一个端点。

    守的是一条"不会报错、只会变慢"的契约，所以**没有任何行为测试能红**：
    把 relay 的 identity_path 改回 `/api/health`，全系统照常工作，只是某个下游
    在推理长录音时，dev.py 会打印"认不出本服务，可能是别的程序占了这个端口"——
    把"自己在忙"误诊成"外人占位"，排查方向整个带偏。这种回归只能静态挡。

    两件事一起查，因为它们必须同时成立才有意义：浅端点在 relay 上存在，
    且 dev.py 的 relay 条目真的指向它。
    """
    problems: list[str] = []
    table: list[str] = []

    try:
        dev = _load_dev()
    except Exception as exc:  # noqa: BLE001 —— 导入失败本身就是结论
        return False, f"dev.py 无法加载：{type(exc).__name__}: {exc}"

    services = list(getattr(dev, "SERVICES", []))
    if not services:
        return False, "dev.py 里没有 SERVICES 表"

    for svc in services:
        bad = []
        if not getattr(svc, "identity_path", ""):
            bad.append("没填 identity_path")
        if not getattr(svc, "identity", ""):
            bad.append("没填 identity")
        table.append(f"{'OK  ' if not bad else '缺失'} {svc.name:<5} {svc.identity_path}")
        problems.extend(f"{svc.name}: {item}" for item in bad)

    relay = next((s for s in services if s.name == "relay"), None)
    if relay is None:
        problems.append("dev.py 的 SERVICES 里没有 relay")
    elif relay.identity_path != "/api/identity":
        problems.append(
            f"relay 的探活路径是 {relay.identity_path!r}，应为 '/api/identity'。"
            " /api/health 会聚合三个下游，拿一个会等的端点回答“你是谁”，"
            "下游忙时必然误判成“端口被别的程序占了”")

    health_src = _code_only(_read("relay/app/routers/health.py") or "")
    if not health_src:
        problems.append("读不到 relay/app/routers/health.py")
    elif '@router.get("/api/identity")' not in health_src:
        problems.append("relay 没有 /api/identity 路由（dev.py 会指向一个 404）")

    detail = "\n".join(table)
    if problems:
        detail += "\n\n" + "\n".join(f"  - {item}" for item in problems)
    return not problems, detail


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
    Check(
        "trace",
        "四跳 trace：装配真的接上了（中间件 + Formatter + configure）",
        "",
        ROOT,
        fn=check_trace_assembly,
    ),
    Check(
        "probe",
        "探活口径：身份探针是浅端点，且 dev.py 与 relay 一致",
        "",
        ROOT,
        fn=check_probe_identity,
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
    # 就地检查：不起进程，也不打项数。
    if check.fn is not None:
        started = time.time()
        ok, detail = check.fn()
        if not ok or verbose:
            print()
            print(f"───── {check.label} 输出 ─────")
            print(detail)
            print("─────────────────────────────")
        return ok, "", int(time.time() - started)

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
        help="只跑指定项，逗号分隔（relay,prompt,extract,session,tts,types,trace,probe,live）")
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
