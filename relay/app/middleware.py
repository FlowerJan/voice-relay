"""协议层中间件：链路 ID + 统一错误格式。

这两个东西是协议层"不是纯转发"的核心证据：
- 没有链路 ID，一次请求跨 3 跳，出问题要看三个窗口且串不起来
- 没有统一错误格式，前端要为每个下游写一套错误处理
"""
import logging
import re
import uuid
from contextvars import ContextVar

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# 当前请求的链路 ID。三个服务的日志都打这个值，一次请求的日志就能串起来。
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

TRACE_HEADER = "X-Trace-Id"

# 本服务自己生成的形态：`uuid4().hex[:16]`。沿用上游时只认这一种。
TRACE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

logger = logging.getLogger(__name__)


def get_trace_id() -> str:
    return trace_id_var.get()


def is_acceptable_trace_id(raw: str | None) -> bool:
    return raw is not None and TRACE_ID_RE.match(raw) is not None


def accept_trace_id(raw: str | None) -> str:
    """沿用上游的 ID，但只接受本服务自己会生成的那种形态。

    "沿用"的目的是把一次请求在四跳里串起来，不是照抄任意字符串：这个值会进
    **每一条**日志行，而日志是给人读、用 `|` 分隔的。一个带分隔符或超长的外部
    串能拼出一条看起来合法的日志行——那比"没有 ID"更难查，因为它看起来是对的。

    形态限死为 16 位小写十六进制，与 `uuid4().hex[:16]` 完全一致；不合形态就
    重新生成。宁可断链，也不把一个来源不明的串写进证据。

    **注意这不是 HTTP 头注入的防线。** 头值里的 CR/LF 在 h11 / llhttp 那一层就
    被拒了，根本到不了这里；真正要防的是 ASCII 范围内的东西——分隔符伪造、
    超长、大小写变体。所以测试里构造 "中文 trace_id" 会先在 httpx 编码阶段失败，
    那不是产品缺陷（详见 test_resilience 里那条注释）。
    """
    return raw if is_acceptable_trace_id(raw) else uuid.uuid4().hex[:16]


class TraceIdMiddleware(BaseHTTPMiddleware):
    """给每个请求生成（或沿用）一个链路 ID，并回写到响应头。

    这里 reset 是安全的：流式响应的 body 虽然在 `dispatch` 返回之后才产出，但
    Starlette 在创建响应任务时已经复制了上下文。ASGITransport 和真实 uvicorn
    下都实测过——流内日志拿到的仍是本次请求的 ID，不是默认的 "-"。
    """

    async def dispatch(self, request: Request, call_next):
        raw = request.headers.get(TRACE_HEADER)
        trace_id = accept_trace_id(raw)
        token = trace_id_var.set(trace_id)
        request.state.trace_id = trace_id
        if raw is not None and not is_acceptable_trace_id(raw):
            # 换掉 ID 等于让链路在**这一跳的接缝处**断开：上游一段、下游一段，
            # 各自都合法。不记这一行的话，"断链"只能靠同时持有原始 ID 和下游日志
            # 才能发现——而原始 ID 只在发它的那个人手里。
            #
            # 这行必须打在 set 之后：这样它自己带着**新的** trace_id，和这次请求
            # 的其余日志落在同一组里，grep 得出来。原值不记——它本来就不该被信任，
            # 也不该借日志再传播一次；只记长度，够区分"没传"和"传了个垃圾"。
            logger.warning(
                "X-Trace-Id 形态不合法（%d 字符），已重新生成；原值不记录", len(raw)
            )
        try:
            response = await call_next(request)
            response.headers[TRACE_HEADER] = trace_id
            # 每个请求留一行，而且必须在 finally **之前**打：reset 之后上下文变量
            # 已经变回 "-"，这一行就白打了。uvicorn 自己的 access log 用它自己的
            # formatter，不带 trace 字段，所以这一层自己记。
            logger.info(
                "%s %s -> %s", request.method, request.url.path, response.status_code
            )
            return response
        finally:
            trace_id_var.reset(token)


class ErrorEnvelopeMiddleware(BaseHTTPMiddleware):
    """把任何未捕获异常收口成统一错误格式。

    统一格式（前端只认这一种）：
        {"error": {"code", "message", "hint", "status", "trace_id"}}

    这里有三件事以前是缺的，缺任何一件都会让 500 变成"查不下去"：
    - **不打日志**：原始异常和栈彻底消失，只剩前端屏幕上那句话；
    - **没有 hint**：形状和其余所有错误不一致，前端按统一解析会拿不到建议；
    - **把 str(exc) 原样回给前端**：内部路径、主机名、SQL 片段都可能出现在浏览器里。
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001
            # trace_id 从 request.state 取，不能直接 get_trace_id()：
            # TraceIdMiddleware 在更内层，它的 finally 已经把 ContextVar 重置成 "-"，
            # 于是用户报上来的 500 反而追不到日志——那正是最需要 trace_id 的时刻。
            trace_id = getattr(request.state, "trace_id", "") or get_trace_id()
            logger.exception(
                "未捕获异常 %s %s trace_id=%s", request.method, request.url.path, trace_id
            )
            # 延迟导入：app.errors 反过来依赖本模块的 get_trace_id，顶层导入会成环。
            from app.errors import error_response

            return error_response(
                500,
                "internal_error",
                "服务内部错误，原因见服务端日志",
                trace_id=trace_id,
            )
