"""端到端（live）：真起两个 uvicorn，真走 TCP。

**默认不跑，也不是"跑不了所以不跑"。** 它要起进程、等端口、真等超时，十几秒起步，
而 55 项桩测试只要 0.2 秒。把它挂进每次 `scripts/check.py`，结果是"改一行等半分钟"，
人就会开始跳过检查——那才是最坏的结局。所以它是显式的：

    cd relay && venv/Scripts/python.exe -m pytest -m live -v

**它补的到底是什么。** 桩测试里，上游是 `MockTransport` + 一个假的字节流对象；
这里上游是一个真的 FastAPI 进程，由 uvicorn 真的监听端口。两者差在：

- 上游崩在首块之前时，真实 uvicorn 是"响应头已发、然后直接断 TCP"，
  客户端拿到的是不完整的 chunked 响应，而不是一个 ReadError 对象
- 客户端断开时，真实 uvicorn 靠取消任务传播，而不是关一个流对象
- SSE 会不会被中转层缓冲，只有真走一次 HTTP/1.1 分块编码才看得出来

**这里也测不到什么**（别把话说满）：不跨机器、不接真实模型层、不测 TLS 和反代。
它证明的是"同一台机器上，relay 与一个真实 uvicorn 上游对接时的行为"，仅此而已。

---

**一个必须记住的坑（第一次写这组用例时踩的）：不要把子进程日志接到 `PIPE`。**

上游抛异常时，uvicorn 会先 `logger.error(exc_info=...)` 把整个 traceback 写进 stderr，
然后才 `transport.close()`。Windows 的匿名管道缓冲区只有 4KB 左右，没人读就会**写满**，
于是上游进程阻塞在写日志这一步——而它阻塞的正是**整个事件循环**。现象极具误导性：

- 连接永远关不掉 → relay 等到读超时 → 报 504，看起来像"重试没生效"
- 之后所有请求（包括 `/_live/state`）全部超时，看起来像"上游挂了"
- 上游进程还活着，`poll()` 返回 `None`，日志里也看不到任何报错

这不是被测代码的缺陷，是脚手架的缺陷。所以日志一律写临时文件，不接管道。
（同类错误本项目已经犯过两次：`httpx.Response(200, content=...)` 的 `StreamConsumed`、
用 1×1 图判定"模型不支持视觉"。共同点都是**把工具的局限当成了被测对象的问题**。）
"""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

# anyio 用来跑 async 用例（与其余四个测试文件一致），live 用来做默认跳过
pytestmark = [pytest.mark.anyio, pytest.mark.live]

RELAY_DIR = Path(__file__).resolve().parent.parent

# 给 relay 的 chat 读超时。正常用例在 0.7 秒内结束，不会误触；
# slow 用例靠它把 30 秒的沉默变成 504，顺便证明"生效的配置是这个值"。
READ_TIMEOUT = 1.5


# ---------------------------------------------------------------- 子进程

@dataclass
class Child:
    """一个跑起来的 uvicorn，日志落盘（**不接管道**，理由见模块注释）。"""

    name: str
    proc: subprocess.Popen
    log_path: Path
    log_file: object = field(repr=False, default=None)

    def logs(self, lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "(无日志)"
        tail = text.strip().splitlines()[-lines:]
        return "\n".join(tail) or "(暂无日志)"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.log_file is not None:
            self.log_file.close()


def _free_port() -> int:
    """让内核分配一个空闲端口。绑了立刻放，中间有微小竞争，可接受。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn(name: str, module: str, port: int, env_extra: dict[str, str]) -> Child:
    log_path = Path(tempfile.gettempdir()) / f"relay-live-{name}-{port}.log"
    handle = open(log_path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    proc = subprocess.Popen(
        [
            # 用当前解释器：测试本身就跑在 relay 的 venv 里，不必写死路径
            sys.executable, "-m", "uvicorn", module,
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "info",
        ],
        cwd=str(RELAY_DIR),
        env={**os.environ, **env_extra},
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return Child(name=name, proc=proc, log_path=log_path, log_file=handle)


def _wait_ready(child: Child, url: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.proc.poll() is not None:
            raise RuntimeError(
                f"{child.name} 提前退出（码 {child.proc.returncode}）：\n{child.logs()}")
        try:
            if httpx.get(url, timeout=1.0, trust_env=False).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{child.name} 在 {timeout} 秒内没就绪（{url}）：\n{child.logs()}")


# ---------------------------------------------------------------- 测试台

@dataclass
class LiveStack:
    relay_url: str
    upstream_url: str
    relay: Child
    upstream: Child

    def plan(self, *modes: str) -> None:
        """排定接下来几次请求的行为。"""
        response = httpx.post(
            f"{self.upstream_url}/_live/plan",
            json={"modes": list(modes)},
            timeout=5.0,
            trust_env=False,
        )
        response.raise_for_status()

    def state(self) -> dict:
        return httpx.get(
            f"{self.upstream_url}/_live/state", timeout=5.0, trust_env=False
        ).json()

    def hits(self) -> list[dict]:
        return self.state()["hits"]

    def diagnose(self) -> str:
        return f"\n--- relay 日志 ---\n{self.relay.logs()}\n--- 上游日志 ---\n{self.upstream.logs()}"


def _assert_wired(stack: LiveStack) -> None:
    """确认 relay 真的连到了 live 上游，而不是 relay/.env 里的 8000。

    这一步不多余：`relay/.env` 里写着 `CHAT_BASE_URL=http://127.0.0.1:8000`。
    如果环境变量没能盖过 dotenv（本项目踩过一次同类坑：断言取的不是生效配置），
    所有用例都会以 502 失败，而 502 的报错看不出"配置没生效"和"上游没起来"的区别。
    宁可在这里报一句人话。
    """
    stack.plan("ok")
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        response = client.post(f"{stack.relay_url}/api/chat/stream", json={})
    if response.status_code != 200 or "第1块" not in response.text:
        raise RuntimeError(
            "relay 没有接通 live 上游，怀疑环境变量没盖过 relay/.env：\n"
            f"  状态 {response.status_code}\n  响应 {response.text[:300]}"
            f"{stack.diagnose()}"
        )


@pytest.fixture(scope="session")
def live() -> LiveStack:
    upstream_port, relay_port = _free_port(), _free_port()
    upstream = _spawn("upstream", "tests.live_upstream:app", upstream_port, {})
    relay = _spawn(
        "relay",
        "app.main:app",
        relay_port,
        {
            "CHAT_BASE_URL": f"http://127.0.0.1:{upstream_port}",
            "CHAT_READ_TIMEOUT": str(READ_TIMEOUT),
            "CHAT_CONNECT_TIMEOUT": "2",
            "CHAT_RETRY_TIMES": "1",
            # 退避归零：验的是"有没有重试"，不是"等了多久"
            "CHAT_RETRY_BACKOFF": "0",
        },
    )
    stack = LiveStack(
        relay_url=f"http://127.0.0.1:{relay_port}",
        upstream_url=f"http://127.0.0.1:{upstream_port}",
        relay=relay,
        upstream=upstream,
    )
    try:
        _wait_ready(upstream, f"{stack.upstream_url}/_live/state")
        # 用 / 而不是 /api/health：health 会并行探三个下游，这里只想知道
        # "端口起来没有"，不该被 asr/tts 没起拖成 degraded
        _wait_ready(relay, f"{stack.relay_url}/")
        _assert_wired(stack)
        yield stack
    finally:
        relay.stop()
        upstream.stop()


# ---------------------------------------------------------------- 读流

async def _read_all(stack: LiveStack, timeout: float = 20.0) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream(
            "POST", f"{stack.relay_url}/api/chat/stream", json={}
        ) as response:
            body = (await response.aread()).decode("utf-8", "replace")
            return response.status_code, body


# ---------------------------------------------------------------- 用例

async def test_live_stream_arrives_incrementally(live):
    """SSE 是否真的逐块到达——桩测试里没有"传输层"，这一条只能真跑。"""
    live.plan("ok")
    stamps: list[float] = []
    chunks: list[bytes] = []

    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        async with client.stream(
            "POST", f"{live.relay_url}/api/chat/stream", json={}
        ) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_raw():
                stamps.append(time.monotonic())
                chunks.append(chunk)

    body = b"".join(chunks).decode("utf-8", "replace")
    assert "第1块" in body and "第3块" in body, f"内容不完整：{body!r}{live.diagnose()}"
    assert len(chunks) >= 3, (
        f"三个间隔 0.2 秒的 SSE 帧被合并成了 {len(chunks)} 块，说明中间有一层在攒")
    span = stamps[-1] - stamps[0]
    assert span >= 0.25, (
        f"首块与末块只差 {span:.3f} 秒，说明整段被缓冲完才一次性吐出")


async def test_live_retries_before_first_chunk(live):
    """第一次在首块之前断开，第二次成功：用户应该只看到一份完整回答。"""
    live.plan("die_before_first", "ok")
    status, body = await _read_all(live)

    assert status == 200, f"期望重试后成功，实际 {status}：{body}{live.diagnose()}"
    assert "第1块" in body and "第3块" in body
    hits = live.hits()
    assert len(hits) == 2, f"上游被打了 {len(hits)} 次，应该是 2 次"
    assert hits[0]["trace"] == hits[1]["trace"], (
        "重试用的是新的链路 ID——日志里会变成两个不相关的请求，串不起来")


async def test_live_empty_200_is_retried(live):
    """200 但零字节：等价于上游"接了但没说话"，同样可以安全重试。"""
    live.plan("empty", "ok")
    status, body = await _read_all(live)

    assert status == 200, f"{body}{live.diagnose()}"
    assert "第1块" in body
    assert len(live.hits()) == 2


async def test_live_does_not_retry_after_first_chunk(live):
    """首块之后断开：**绝不能**重试，否则用户会看到重复段落。"""
    live.plan("die_after_first", "ok")
    status, body = await _read_all(live)

    assert status == 200, (
        f"首块之后断开应降级成「部分内容 + 正常结束」，实际 {status}：{body}")
    assert "第一块" in body, "首块之后的内容丢了：已经吐出去的东西不该被吞掉"
    assert "第1块" not in body, "重试了：第二次的完整内容混了进来"
    assert len(live.hits()) == 1, (
        f"上游被打了 {len(live.hits())} 次，首块之后不该再试")


async def test_live_read_timeout_maps_to_504(live):
    """读超时映射成 504，而不是 500 或者一直挂着。

    顺带用行为验证"生效的读超时确实是 READ_TIMEOUT"——只读 settings 证明不了
    环境变量有没有盖过 .env。
    """
    live.plan("slow")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.post(f"{live.relay_url}/api/chat/stream", json={})
    elapsed = time.monotonic() - started

    assert response.status_code == 504, response.text
    error = response.json()["error"]
    assert error["code"] == "chat_timeout"
    assert error["status"] == 504
    assert error["trace_id"], "错误体里没有链路 ID，前端报障时无从对日志"
    assert elapsed < 10, (
        f"用了 {elapsed:.1f} 秒才超时，说明生效的读超时不是 {READ_TIMEOUT} 秒")


async def test_live_trace_id_reaches_upstream(live):
    """同一个链路 ID 要同时出现在响应头和上游收到的请求里。"""
    live.plan("ok")
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        async with client.stream(
            "POST", f"{live.relay_url}/api/chat/stream", json={}
        ) as response:
            trace_id = response.headers.get("X-Trace-Id")
            await response.aread()

    assert trace_id, "响应头里没有 X-Trace-Id"
    assert live.hits()[0]["trace"] == trace_id, (
        f"上游收到的是 {live.hits()[0]['trace']!r}，响应头是 {trace_id!r}——两跳对不上")


async def test_live_client_disconnect_releases_upstream(live):
    """客户端断开后，上游的生成器要被取消，不能挂在那边空转。

    这是"泄漏"类问题里最难从现象反推的一种：一切看起来都正常，只是上游多挂了
    一个永远跑下去的协程，直到某天连接数满了才暴露。
    """
    live.plan("cancel_probe")
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        async with client.stream(
            "POST", f"{live.relay_url}/api/chat/stream", json={}
        ) as response:
            assert response.status_code == 200
            async for _ in response.aiter_raw():
                break  # 读一块就走人，模拟用户关掉页面

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if live.state()["cancelled"] >= 1:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        "客户端断开 8 秒后，上游生成器仍未被取消——这是一处连接泄漏"
        f"{live.diagnose()}")
