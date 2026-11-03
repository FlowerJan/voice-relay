"""链路 ID 的下游一侧：读 relay 传来的 `X-Trace-Id`，写进每一条日志。

**为什么需要这个文件。** relay 从第一天就在发 `X-Trace-Id`，但下游没有人读它。
于是"trace 串起四跳"只是一句说法：ID 生成在 relay、随响应回到前端，中间这几跳
的日志里一个字都没有。出事时拿着 trace_id 去 grep，只有 relay 那一份对得上——
而 relay 恰恰是最不需要排查的那一跳（它是纯转发）。

**为什么三个服务各存一份，不抽公共包。** 这几个进程能各自独立启动、独立装依赖，
是这套架构最要紧的性质（ASR 那 1.8GB 的 venv 不该被别的服务的依赖改动牵连）。
一份七十行的日志配置复制三次，换来"任何一个目录单独删掉都不影响其他"，这笔买卖值。
"""
import logging
import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

TRACE_HEADER = "X-Trace-Id"

# 本服务自己生成的形态：`uuid4().hex[:16]`。沿用上游时只认这一种。
TRACE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

#: 当前请求的链路 ID。"-" 表示这条日志不属于任何请求（启动、加载模型、后台任务）。
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(trace)s | %(name)s | %(message)s"

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
    超长、大小写变体。
    """
    return raw if is_acceptable_trace_id(raw) else uuid.uuid4().hex[:16]


class TraceFormatter(logging.Formatter):
    """给 `%(trace)s` 兜底。

    用 Formatter 而不是给 handler 挂 Filter：filter 一旦漏挂某个 handler，
    `%(trace)s` 就会抛 KeyError，那条日志会被 logging 整个吞掉。表现为
    "这行日志没打出来"——比格式难看严重得多。Formatter 是每条记录的必经之路，
    在这里兜底等于不可能漏。
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace"):
            record.trace = trace_id_var.get()
        return super().format(record)


def configure(level: int = logging.INFO) -> None:
    """装上带 trace 的日志格式。各服务入口处调用一次。"""
    handler = logging.StreamHandler()
    handler.setFormatter(TraceFormatter(LOG_FORMAT))
    root = logging.getLogger()
    # 换掉而不是追加：basicConfig 之后再挂一个 handler 会让每行日志出现两次。
    root.handlers[:] = [handler]
    root.setLevel(level)


class TraceMiddleware(BaseHTTPMiddleware):
    """沿用上游传来的 ID；没传就自己生成一个。

    自己生成是有用的：直连 8000/8001 的 curl 也是一次真实请求，它同样需要一个
    可追的 ID，而且这个 ID 会出现在响应头里——调试时把它贴进日志搜索即可。

    这里 reset 不会伤到流式响应：body 虽然在 `dispatch` 返回之后才产出，但
    Starlette 创建响应任务时已经复制了上下文（ASGITransport 与真实 uvicorn
    都实测过）。写 reset 是为了让上下文不在请求结束后残留。
    """

    async def dispatch(self, request, call_next):
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
        finally:
            trace_id_var.reset(token)
        response.headers[TRACE_HEADER] = trace_id
        return response
