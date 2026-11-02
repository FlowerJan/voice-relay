"""live 测试用的上游：一个**真的** FastAPI 应用，由 uvicorn 真跑在端口上。

**为什么需要它。** 其余 55 项测试都用 `httpx.MockTransport` 把上游换掉。打桩能证明
"决策逻辑是对的"——超时值传对了、错误码映射对了；但不能证明"接上真实 uvicorn 也
这样"。差别恰恰都在被替换掉的那一层：

- uvicorn 有自己的 HTTP/1.1 分块编码，桩上是直接给字节流
- 上游中途崩溃时，真实 uvicorn 是"发完响应头就断连"，不是"抛一个 ReadError"
- 客户端断开时，真实 uvicorn 用取消任务来传播，不是简单关个流

**控制方式：行为队列。** 测试先 `POST /_live/plan {"modes": ["empty", "ok"]}`，
之后每次 `/api/chat/stream` 消费一个；队列剩最后一个就一直沿用。这样"第一次断、
第二次成功"这种场景不需要重启上游。请求体不参与控制——relay 是原样转发的，
上游看什么不重要，重要的是它按什么方式回。

**状态回读。** `GET /_live/state` 给出"被调了几次、每次带了什么 trace、有几个被
取消"。这是测试唯一能知道上游侧发生了什么的方式（跨进程，看不到对方的变量）。
"""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="live 上游（测试用）")

# 行为队列：每个 chat 请求消费一个，只剩最后一个时一直复用
_plan: list[str] = ["ok"]
_hits: list[dict] = []
_cancelled = 0


@app.post("/_live/plan")
async def set_plan(request: Request):
    """排定接下来几次请求的行为，并清空计数。"""
    global _plan, _cancelled
    payload = await request.json()
    _plan = [str(m) for m in payload["modes"]]
    _hits.clear()
    _cancelled = 0
    return {"plan": _plan}


@app.get("/_live/state")
async def state():
    return {"hits": _hits, "cancelled": _cancelled}


def _sse(text: str) -> bytes:
    """一个完整的 SSE 帧。内容本身不重要，但用真形状便于断言拼接结果。"""
    payload = {"type": "delta", "text": text}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _next_mode() -> str:
    if len(_plan) > 1:
        return _plan.pop(0)
    return _plan[0]


@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    mode = _next_mode()
    _hits.append({"mode": mode, "trace": request.headers.get("x-trace-id")})

    if mode == "ok":
        async def gen():
            for index in range(3):
                yield _sse(f"第{index + 1}块")
                # 间隔要明显大于零，才能验证"逐块到达"而不是"一次全到"
                await asyncio.sleep(0.2)

        return StreamingResponse(gen(), media_type="text/event-stream")

    if mode == "empty":
        # 200 + 零字节 + 正常结束。等价于上游"接了请求但什么都没说"，
        # 真实的成因是上游内部提前 return 或者被下游适配层吃掉。
        async def empty():
            return
            yield  # 让它成为异步生成器

        return StreamingResponse(empty(), media_type="text/event-stream")

    if mode == "die_before_first":
        # 响应头已发（Starlette 先发 start 再迭代 body），随后异常 →
        # 真实 uvicorn 会直接断开 TCP，客户端看到的是不完整的 chunked 响应。
        async def die():
            raise RuntimeError("上游在首块之前就崩了")
            yield  # 让它成为异步生成器

        return StreamingResponse(die(), media_type="text/event-stream")

    if mode == "die_after_first":
        # 已经吐了一块再崩：客户端手里有半截内容，这正是"不能重试"的原因。
        async def die_after():
            yield _sse("第一块")
            await asyncio.sleep(0.05)
            raise RuntimeError("上游吐了一块之后断开")

        return StreamingResponse(die_after(), media_type="text/event-stream")

    if mode == "slow":
        # 一直不说话。用来触发 relay 的读超时（测试把它调成 1.5 秒）。
        async def slow():
            await asyncio.sleep(30)
            yield _sse("迟到的块")

        return StreamingResponse(slow(), media_type="text/event-stream")

    if mode == "cancel_probe":
        # 无限心跳。客户端断开后 uvicorn 会取消这个任务，finally 记一次。
        # 这是"SSE 断开时上游连接是否释放"唯一可观测的证据。
        async def heartbeat():
            global _cancelled
            try:
                while True:
                    yield _sse("心跳")
                    await asyncio.sleep(0.1)
            finally:
                _cancelled += 1

        return StreamingResponse(heartbeat(), media_type="text/event-stream")

    return StreamingResponse(
        iter(()), media_type="text/event-stream", status_code=500
    )
