"""协议层启动器（端口 9000）。

**不开 reload。** Windows 上 uvicorn 的 reload 会派生子进程（spawn_main），
杀掉父进程后子进程仍然占着端口、跑着旧代码，表现为"改了代码不生效、还查不出原因"。
这一层要改的代码很少，手动重启更可控。
"""
import uvicorn

from app import trace

if __name__ == "__main__":
    # 日志格式要带 trace 字段：dev.py 把五个进程的输出汇到一个终端，
    # 没有这个字段就只能靠时间戳猜哪几行属于同一次请求。
    trace.configure()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=9000,
        reload=False,
        log_level="info",
        # 关掉 uvicorn 自带的 access log：它用自己的 formatter，不带 trace 字段。
        # 每个请求的"方法 路径 状态"由 TraceIdMiddleware 记，那一行有 trace。
        access_log=False,
    )
