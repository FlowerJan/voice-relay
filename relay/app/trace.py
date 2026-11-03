"""协议层自己的日志格式：每行都带 `trace_id`。

中间件生成了 ID、错误体里也一直带着它，但**日志本身**以前没有这个字段——
于是"按 trace_id grep 一遍"在 relay 这一层也做不到，只能靠时间戳去猜哪几行属于
同一次请求。可观测性缺的从来不是 ID 的产生，而是 ID 的落地。

本模块只负责格式，不负责生成：上下文变量属于 `middleware.py`（中间件在那里）。
"""
import logging

from app.middleware import trace_id_var

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(trace)s | %(name)s | %(message)s"


class TraceFormatter(logging.Formatter):
    """给 `%(trace)s` 兜底，理由见 engine/*/app/trace.py 里的同名类。

    简言之：filter 漏挂会让整条日志因 KeyError 消失，Formatter 不会被绕过。
    """

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace"):
            record.trace = trace_id_var.get()
        return super().format(record)


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(TraceFormatter(LOG_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
