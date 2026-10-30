#!/usr/bin/env python
"""一键启动器 —— 一个终端、一条命令，起全部五个服务。

设计取舍（为什么不合并进程）：

  * 五个服务各自有独立的 venv 和完全不同的重启成本。ASR 一重载就要重新读
    1.8GB 模型，聊天层只看一个 openai 客户端、重载近乎免费。合并成一个进程，
    等于用"最慢的那个"绑架所有人的开发节奏。
  * 合并还会丢故障隔离：ASR 内存炸了会把聊天一起带走，这在演示时很难看。

  所以这里只做**编排**，不做合并：进程还是五个、端口还是五个，但对你来说
  只有一条命令、一个地址。端口从此是内部细节，不需要你逐个 `cd` 进去敲。

另外两个刻意的设计：

  * 端口已在监听 → 先请求 /health 认身份，认得出来才跳过（这样你可以用它起全部，
    再单独去某个目录重起某个服务调试，两者不打架）；认不出来说明端口被**别的
    程序**占着，直接报出占用者和处置办法后退出。
    只看"端口有人听"是不够的——本项目就吃过这个亏：一个无关的 python 进程先绑了
    8000，这里静默跳过、以为"已经在跑"，结果前端一路 404，排查了半天。
  * Ctrl+C 一次收全部。Windows 下 uvicorn 的 reload 模式会派生子进程，
    必须 taskkill /T 才能连带子进程一起杀掉，否则端口被僵尸进程占住、
    表现为"改了代码不生效"——这个坑本项目已经踩过一次。

用法：

    E:/python/python.exe dev.py            # 起全部，就绪后自动开浏览器，然后守着
    E:/python/python.exe dev.py --no-open  # 同上，但不自动开浏览器
    E:/python/python.exe dev.py --check    # 只体检，不启动

注意：要用**系统 Python** 跑这个脚本（它本身不依赖任何第三方包），
不要用某个服务的 venv，否则 venv_py() 的相对路径判断会错位。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
APP_URL = "http://localhost:5173"

RESET = "\033[0m"
C = {
    "chat": "\033[36m",
    "asr": "\033[35m",
    "tts": "\033[33m",
    "relay": "\033[32m",
    "view": "\033[34m",
    "sys": "\033[37m",
}

_lock = threading.Lock()


def say(msg: str, tag: str = "sys") -> None:
    with _lock:
        sys.stdout.write(f"{C.get(tag, '')}[{tag:>5}]{RESET} {msg}\n")
        sys.stdout.flush()


@dataclass
class Service:
    name: str
    path: str
    argv: list[str]
    port: int
    wait: float          # 最多等它多少秒就绪
    desc: str
    health: str          # 探活路径，用来确认"占着端口的到底是谁"
    identity: str        # 该路径返回体里必须出现的身份标识
    proc: subprocess.Popen | None = None


# 顺序即依赖顺序：三个模型层 → 协议层 → 前端。
# 网关不主动探活，先起 9000 的话它不报错，但一收请求就是 502。
#
# identity 一律取"对方自己吐出来的、别人极不可能恰好也吐的那一串"：
# 四个后端服务认 service 字段，前端认 Vite 注入的 /@vite/client。
SERVICES: list[Service] = [
    Service("chat",  "engine/deepseek", ["run.py"],        8000, 30, "想 · deepseek-flash（远程）",
            "/api/health", '"service":"chat"'),
    Service("asr",   "engine/asr",      ["run.py"],        8001, 90, "听 · Qwen3-ASR-0.6B（本地 CPU，读 1.8GB）",
            "/health", '"service":"asr"'),
    Service("tts",   "engine/tts",      ["run.py"],        8002, 30, "说 · edge-tts（远程合成）",
            "/health", '"service":"tts"'),
    Service("relay", "relay",           ["run.py"],        9000, 30, "协议层 · 唯一对外入口",
            "/api/health", '"service":"relay"'),
    Service("view",  "view",            ["run", "dev"],    5173, 60, "前端 · Vite",
            "/", "@vite/client"),
]


def venv_py(cwd: Path) -> Path:
    p = cwd / ("venv/Scripts/python.exe" if IS_WIN else "venv/bin/python")
    if not p.exists():
        raise SystemExit(f"找不到 {p}\n  这个服务还没装环境，按 README「首次安装」里对应的那一步做一遍。")
    return p


def port_open(port: int, timeout: float = 0.25) -> bool:
    """127.0.0.1 和 ::1 都试一遍 —— Vite 只监听 IPv6，只测 v4 会误判为没起来。"""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _squeeze(s: str) -> str:
    """去掉全部空白再比。FastAPI 吐的是紧凑 JSON，换个实现就可能带空格。"""
    return "".join(s.split())


def fetch_head(port: int, path: str, timeout: float = 5.0) -> str | None:
    """取响应体开头一段用于认身份。连不上、超时、非 2xx 一律返回 None。

    超时给到 5s 是量出来的：relay 的 /api/health 要串并三个下游，某个下游刚挂
    时会等它自己的超时，实测 2.2s。给 1.5s 就会把"自己人反应慢"误判成"外人占位"。
    """
    try:
        with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=timeout) as r:
            return r.read(8192).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_self(svc: Service) -> bool:
    """端口上跑的到底是不是这个服务。返回体里认得出 identity 才算。

    探不到就再给一次机会：首轮可能正好赶上对方在做慢启动或慢响应，
    这里的目标是抓"根本不是它"，不是抓"它此刻有点慢"。
    """
    for attempt in range(2):
        body = fetch_head(svc.port, svc.health)
        if body is not None and _squeeze(svc.identity) in _squeeze(body):
            return True
        if attempt == 0:
            time.sleep(0.4)
    return False


def _win_proc_names() -> dict[str, str]:
    """PID → 进程名。查不到就返回空表，不影响报错信息的主体。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table: dict[str, str] = {}
    for line in out.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) >= 2 and cols[1].isdigit():
            table[cols[1]] = cols[0]
    return table


def occupant(port: int) -> str:
    """谁占着这个端口 —— 纯给人看的，查不到也要能读。"""
    if not IS_WIN:
        return f"未知（非 Windows，用 lsof -i :{port} 查）"
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, errors="replace", timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "未知（netstat 调不起来）"
    pids = {
        parts[4]
        for parts in (line.split() for line in out.splitlines())
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}")
    }
    if not pids:
        return "未知（netstat 里没有 LISTENING 记录，可能只绑了 IPv6）"
    names = _win_proc_names()
    return "、".join(f"{names.get(p, '?')}(PID {p})" for p in sorted(pids))


def launch(svc: Service) -> None:
    if port_open(svc.port):
        if is_self(svc):
            say(f":{svc.port} 已是本服务在跑，跳过", svc.name)
            return
        # 端口有人听 ≠ 是我们的服务。放它过去只会得到"前端白屏 404"这种远因报错，
        # 所以这里就当场停下，把"谁占的、怎么办"直接说清楚。
        raise SystemExit(
            f"\n:{svc.port} 上认不出本服务（探活 {svc.health} 里没有 {svc.identity}）。\n"
            f"  占用者：{occupant(svc.port)}\n"
            f"  可能是别的程序占了这个端口；也可能它就是本服务的旧进程——\n"
            f"  旧进程要杀掉重启才会加载新代码。关掉它再跑 dev.py 即可。\n"
        )

    cwd = ROOT / svc.path
    if not cwd.is_dir():
        raise SystemExit(f"目录不存在：{cwd}")

    if svc.name == "view":
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("找不到 npm，前端起不来")
        argv = [npm, *svc.argv]
    else:
        argv = [str(venv_py(cwd)), *svc.argv]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"   # 子进程按 utf-8 写管道，父进程才解得开中文
    env["PYTHONUNBUFFERED"] = "1"       # 否则日志要攒满缓冲区才吐出来

    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    svc.proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=flags,
    )
    threading.Thread(target=_pump, args=(svc,), daemon=True).start()
    say(f"启动中… {svc.desc}", svc.name)


def _pump(svc: Service) -> None:
    assert svc.proc is not None and svc.proc.stdout is not None
    for line in svc.proc.stdout:
        line = line.rstrip()
        if line:
            say(line, svc.name)


def wait_ready(svc: Service) -> None:
    t0 = time.time()
    while time.time() - t0 < svc.wait:
        if svc.proc and svc.proc.poll() is not None:
            raise SystemExit(
                f"{svc.name} 启动即退出（退出码 {svc.proc.returncode}），往上看它的日志"
            )
        if port_open(svc.port):
            say(f"就绪 :{svc.port}  {time.time() - t0:.1f}s", svc.name)
            return
        time.sleep(0.3)
    raise SystemExit(f"{svc.name} 等了 {svc.wait:.0f}s 还没监听 :{svc.port}，往上看它的日志")


def stop_all() -> bool:
    """收掉本次起的进程。一个都没起时什么都不说 —— 免得给"启动失败"的场景
    再补一句莫名其妙的"已全部停止"。返回是否真停过东西。"""
    live = [s for s in SERVICES if s.proc and s.proc.poll() is None]
    if not live:
        return False
    say(f"停止 {len(live)} 个进程…")
    for svc in live:
        assert svc.proc is not None
        if IS_WIN:
            # /T 连带子进程：reload 模式派生的 spawn_main 不杀掉就会占着端口
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(svc.proc.pid)],
                capture_output=True,
            )
        else:
            svc.proc.terminate()
    for svc in live:
        try:
            svc.proc.wait(timeout=5)   # type: ignore[union-attr]
        except subprocess.TimeoutExpired:
            svc.proc.kill()            # type: ignore[union-attr]
    return True


def do_check() -> int:
    say(f"项目根 {ROOT}")
    bad = 0
    for svc in SERVICES:
        cwd = ROOT / svc.path
        if svc.name == "view":
            ok = (cwd / "node_modules").is_dir()
            need = "node_modules"
        else:
            py = cwd / ("venv/Scripts/python.exe" if IS_WIN else "venv/bin/python")
            ok = py.exists()
            need = str(py.relative_to(ROOT)) if cwd.is_dir() else str(py)
        state, taken = "空闲", False
        if port_open(svc.port):
            if is_self(svc):
                state = "本服务"
            else:
                state, taken = "被他人占", True
        mark = "OK  " if ok else "缺失"
        say(f"{mark} :{svc.port:<5} {svc.path:<16} 端口 {state:<4}  {'' if ok else '缺 ' + need}", svc.name)
        if taken:
            say(f"      ↑ 认不出本服务，dev.py 会拒绝启动；占用者：{occupant(svc.port)}")
        bad += 0 if ok else 1
        bad += 1 if taken else 0
    say("体检通过，可以 dev.py 启动" if not bad else f"有 {bad} 个服务环境不全", "sys")
    return 0 if not bad else 1


def main() -> int:
    if IS_WIN:
        os.system("")   # 让 cmd 认 ANSI 颜色；失败也无所谓，纯装饰

    if "--check" in sys.argv:
        return do_check()

    say(f"项目根 {ROOT}")
    try:
        for svc in SERVICES:
            launch(svc)
        for svc in SERVICES:
            if svc.proc is not None:
                wait_ready(svc)
        say("")
        say(f"全部就绪 →  {APP_URL}    （注意是 localhost，不是 127.0.0.1）")
        if "--no-open" in sys.argv:
            say("（--no-open：不自动开浏览器）")
        else:
            # 就绪后才开，避免浏览器先到、页面白屏报"无法连接"
            try:
                webbrowser.open(APP_URL)
                say("已唤出默认浏览器")
            except Exception as e:      # 没有默认浏览器 / 无桌面环境时不该让服务跟着退出
                say(f"自动开浏览器失败（不影响使用，请手动打开 {APP_URL}）：{e}")
        say("停止 → Ctrl+C")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("")
        say("收到 Ctrl+C")
    finally:
        if stop_all():
            say("已全部停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
