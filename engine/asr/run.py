"""ASR 服务启动器（端口 8001）。

**绝对不开 reload**：这个进程加载了 1.8GB 模型，重载一次就要重新加载。
开发时改代码就手动重启，不接受"省事"换来的两秒等待 × 每次热更。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.server:app",
        host="127.0.0.1",
        port=8001,
        reload=False,
        log_level="info",
    )
