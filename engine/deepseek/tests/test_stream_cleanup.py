"""用户点「停止」时，上游那条流必须被关掉。

SDK 只在**把流读完**时自动 `close()`。中途退出（前端 abort、用户切会话、
浏览器关掉）走的是生成器的 `aclose()`，此时 `async for` 被打断，SDK 不会接住——
连接要等 GC 才释放。在那之前，上游还在按 token 生成，账单照走。

这条路径在正常使用里天天发生（每次点停止），却因为"界面看起来没问题"而没人查。
"""
import asyncio
from types import SimpleNamespace

from app.events import DELTA, DONE, ERROR
from app.schemas import ChatMessage
from app.services import chat_service


class FakeStream:
    """可断言的假流：记下被读了几块，以及有没有被关闭。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.consumed = 0

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def close(self):
        self.closed = True


def _chunk(content=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class FakeClient:
    def __init__(self, stream):
        self.stream = stream

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        return self.stream


def _install(monkeypatch, stream):
    monkeypatch.setattr(chat_service, "_client", FakeClient(stream))


def test_stopping_midway_closes_upstream(monkeypatch):
    """读一块就停：close 必须被调用，且不该把剩下的块读完。"""
    stream = FakeStream([_chunk("你"), _chunk("好"), _chunk("世界")])
    _install(monkeypatch, stream)

    async def scenario():
        agen = chat_service.stream_chat([ChatMessage(role="user", content="hi")])
        first = await agen.__anext__()
        assert first["type"] == DELTA
        await agen.aclose()

    asyncio.run(scenario())

    assert stream.closed, "中途停止后上游流没关，连接要等 GC 才释放"
    assert stream.consumed < 3, f"已经停了还在往下读：读了 {stream.consumed} 块"


def test_normal_completion_also_closes(monkeypatch):
    """正常读完也要走同一条清理路径（SDK 自己会关，但重复 close 必须无害）。"""
    stream = FakeStream([_chunk("好", finish="stop")])
    _install(monkeypatch, stream)

    async def scenario():
        return [e async for e in chat_service.stream_chat([ChatMessage(role="user", content="hi")])]

    events = asyncio.run(scenario())

    assert [e["type"] for e in events] == [DELTA, DONE]
    assert stream.closed


def test_close_failure_does_not_hide_the_error(monkeypatch):
    """关闭本身失败时，用户该看到的是真正的错误，不是关闭时的异常。"""

    class BadClose(FakeStream):
        async def close(self):
            self.closed = True
            raise RuntimeError("close 炸了")

        def __aiter__(self):
            return self._iterate()

        async def _iterate(self):
            raise ValueError("上游突然断了")
            yield b""  # pragma: no cover  让它是生成器

    stream = BadClose([])
    _install(monkeypatch, stream)

    async def scenario():
        return [e async for e in chat_service.stream_chat([ChatMessage(role="user", content="hi")])]

    events = asyncio.run(scenario())

    assert events[-1]["type"] == ERROR
    assert "上游突然断了" in events[-1]["message"]
    assert stream.closed
