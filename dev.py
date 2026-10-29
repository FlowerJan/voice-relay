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

  * 端口已在监听 → 不报错，直接跳过。你可以用它起全部，再单独去某个目录
    重起某个服务调试，两者不打架。
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
    proc: subprocess.Popen | None = None


# 顺序即依赖顺序：三个模型层 → 协议层 → 前端。
# 网关不主动探活，先起 9000 的话它不报错，但一收请求就是 502。
SERVICES: list[Service] = [
    Service("chat",  "engine/deepseek", ["run.py"],        8000, 30, "想 · deepseek-flash（远程）"),
    Service("asr",   "engine/asr",      ["run.py"],        8001, 90, "听 · Qwen3-ASR-0.6B（本地 CPU，读 1.8GB）"),
    Service("tts",   "engine/tts",      ["run.py"],        8002, 30, "说 · edge-tts（远程合成）"),
    Service("relay", "relay",           ["run.py"],        9000, 30, "协议层 · 唯一对外入口"),
    Service("view",  "view",            ["run", "dev"],    5173, 60, "前端 · Vite"),
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


def launch(svc: Service) -> None:
    if port_open(svc.port):
        say(f":{svc.port} 已在监听，跳过（视为已在外运行）", svc.name)
        return

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


def stop_all() -> None:
    live = [s for s in SERVICES if s.proc and s.proc.poll() is None]
    if not live:
        return
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
        state = "占用中" if port_open(svc.port) else "空闲"
        mark = "OK  " if ok else "缺失"
        say(f"{mark} :{svc.port:<5} {svc.path:<16} 端口{state}   {'' if ok else '缺 ' + need}", svc.name)
        bad += 0 if ok else 1
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
        stop_all()
        say("已全部停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
