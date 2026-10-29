"""协议层中间件：链路 ID + 统一错误格式。

这两个东西是协议层"不是纯转发"的核心证据：
- 没有链路 ID，一次请求跨 3 跳，出问题要看三个窗口且串不起来
- 没有统一错误格式，前端要为每个下游写一套错误处理
"""
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# 当前请求的链路 ID。三个服务的日志都打这个值，一次请求的日志就能串起来。
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

TRACE_HEADER = "X-Trace-Id"


def get_trace_id() -> str:
    return trace_id_var.get()


def _error_body(code: str, message: str, status_code: int) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "status": status_code,
            "trace_id": get_trace_id(),
        }
    }


class TraceIdMiddleware(BaseHTTPMiddleware):
    """给每个请求生成（或沿用）一个链路 ID，并回写到响应头。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex[:16]
        token = trace_id_var.set(trace_id)
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(token)
        response.headers[TRACE_HEADER] = trace_id
        return response


class ErrorEnvelopeMiddleware(BaseHTTPMiddleware):
    """把任何未捕获异常收口成统一错误格式。

    统一格式（前端只认这一种）：
        {"error": {"code", "message", "status", "trace_id"}}
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            code = type(exc).__name__
            message = str(exc)[:300] or code
            return JSONResponse(
                status_code=500,
                content=_error_body(code, message, 500),
            )
