"""错误映射：断网、超时、限流不能都被说成"密钥无效"。

这个文件盯的是一个**静默**的 bug。`except` 链按书写顺序匹配，而 openai 的异常
层次很深：

    APITimeoutError → APIConnectionError → APIError
    BadRequestError / AuthenticationError / RateLimitError → APIStatusError → APIError

所以只要 `APIError` 排在前面，后面每个分支都是死代码。代码照样跑，测试照样绿，
只有用户断网时看到"API 密钥无效或余额不足"才会发现——然后去翻密钥，翻半天。

因此这里测的不是"文案好不好听"，而是**每个异常有没有走到自己那一支**，
以及每一条都附带了一句**真能照着做**的建议。
"""
import asyncio
import inspect

import pytest
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from app.events import ERROR
from app.schemas import ChatMessage
from app.services import chat_service
from app.services.chat_service import explain_error

try:
    # openai 3.x 把内部 HTTP 客户端换成了 httpx2；旧版本仍是 httpx。
    # 只为了构造异常需要 Request/Response，用哪套都行。
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover
    import httpx

REQ = httpx.Request("POST", "https://api.deepseek.com/chat/completions")


def _status_error(cls, status: int, message: str):
    """构造带状态码的 openai 异常。"""
    response = httpx.Response(status, request=REQ)
    return cls(message, response=response, body={"error": {"message": message}})


# 每条：(异常实例, message 里必须出现的词)。这些词互不重叠，能判断走到了哪一支。
CASES = [
    (_status_error(BadRequestError, 400, "image format not supported"), "请求被拒绝"),
    (_status_error(AuthenticationError, 401, "Invalid API key"), "密钥无效"),
    (_status_error(PermissionDeniedError, 403, "no access"), "没有权限"),
    (_status_error(RateLimitError, 429, "rate limited"), "限流"),
    (_status_error(InternalServerError, 500, "upstream boom"), "上游服务故障"),
    (APITimeoutError(request=REQ), "超时"),
    (APIConnectionError(request=REQ), "网络连接失败"),
    (APIError("something odd", request=REQ, body=None), "调用失败"),
    (ValueError("裸异常"), "裸异常"),
]

#: 单独测 402：余额不足没有专属异常类，混在 APIStatusError 里，靠状态码分流。
INSUFFICIENT_QUOTA = (_status_error(APIStatusError, 402, "Insufficient Balance"), "余额不足")


def test_each_error_gets_its_own_message():
    """十条真实失败路径，十条不同的 message。

    以前这里只有一条——"API 密钥无效或余额不足"——覆盖了除 400 外的全部情况。
    """
    for exc, keyword in CASES + [INSUFFICIENT_QUOTA]:
        text = explain_error(exc)["message"]
        assert keyword in text, f"{type(exc).__name__} 应该提到「{keyword}」，实际是：{text}"


def test_timeout_and_connection_are_not_reported_as_key_problem():
    """曾经的真实故障：拔网线，界面说"API 密钥无效或余额不足"。"""
    for exc in (APITimeoutError(request=REQ), APIConnectionError(request=REQ)):
        text = explain_error(exc)["message"]
        assert "密钥" not in text and "余额" not in text, f"{type(exc).__name__} 被误报成密钥问题：{text}"


def test_messages_are_distinct():
    """任意两条路径的 message 不能相同——相同就意味着有一支被遮蔽了。"""
    texts = [explain_error(exc)["message"] for exc, _ in CASES]
    assert len(set(texts)) == len(texts), f"有分支被遮蔽，只得到 {len(set(texts))} 种文案：{texts}"


def test_every_error_comes_with_an_actionable_hint():
    """每个分支都必须给建议，而且不能只是把 message 说第二遍。

    空 hint 会让界面渲染出一条空行；复述 message 则等于没写——
    用户读完仍然不知道"现在该按哪里"。
    """
    for exc, _ in CASES + [INSUFFICIENT_QUOTA]:
        info = explain_error(exc)
        hint = info["hint"]
        assert hint.strip(), f"{type(exc).__name__} 没有 hint"
        assert hint != info["message"], f"{type(exc).__name__} 的 hint 只是复述 message"
        assert len(hint) >= 8, f"{type(exc).__name__} 的 hint 太短，不构成一个动作：{hint}"


def test_hint_avoids_the_empty_gesture():
    """"请联系管理员""在这台机器上不成立：没有管理员这个人。"""
    excs = [exc for exc, _ in CASES + [INSUFFICIENT_QUOTA]]
    offenders = [
        explain_error(e)["hint"]
        for e in excs
        if "联系" in explain_error(e)["hint"] or "管理员" in explain_error(e)["hint"]
    ]
    assert not offenders, f"这些建议没有可执行的动作：{offenders}"


def test_api_error_branch_is_placed_last():
    """结构性守卫：`APIError` 必须排在所有具体异常之后。

    不测文案，测源码里 isinstance 的先后。因为遮蔽的后果是静默的，
    而"加一个新分支时插错位置"是这类 bug 唯一的复现方式。
    """
    src = inspect.getsource(explain_error)
    order = [
        "BadRequestError",
        "AuthenticationError",
        "PermissionDeniedError",
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "APIStatusError",
        "APIError",
    ]
    positions = [src.index(f"isinstance(exc, {name})") for name in order]
    assert positions == sorted(positions), (
        "explain_error 里的判定顺序不对：基类必须排在子类之后，"
        f"否则后面的分支是死代码。当前顺序：{order}"
    )


def test_bad_request_keeps_upstream_detail():
    """400 的原始文案必须带出来：它通常直接写着哪个字段不对，是唯一能自助修的线索。"""
    exc = _status_error(BadRequestError, 400, "invalid image_url: unsupported scheme")
    assert "invalid image_url" in explain_error(exc)["message"]


def test_long_detail_is_truncated():
    """上游可能回一整页 HTML，不能原样塞进界面。"""
    exc = APIStatusError("x" * 5000, response=httpx.Response(502, request=REQ), body=None)
    assert len(explain_error(exc)["message"]) < 220


def test_stream_chat_actually_uses_the_mapping():
    """映射写对了但 stream_chat 没接上，等于没改。这条把两头钉在一起。"""

    class Boom:
        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        async def create(self, **kwargs):
            raise APITimeoutError(request=REQ)

    original = chat_service._client
    chat_service._client = Boom()
    try:
        events = asyncio.run(_drain())
    finally:
        chat_service._client = original

    assert events and events[-1]["type"] == ERROR
    assert "超时" in events[-1]["message"]
    assert "密钥" not in events[-1]["message"]
    assert events[-1]["hint"], "error 事件必须带 hint，否则前端只有红字没有下一步"


async def _drain():
    return [
        event
        async for event in chat_service.stream_chat([ChatMessage(role="user", content="hi")])
    ]
