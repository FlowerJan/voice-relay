"""协议层启动器（端口 9000）。

**不开 reload。** Windows 上 uvicorn 的 reload 会派生子进程（spawn_main），
杀掉父进程后子进程仍然占着端口、跑着旧代码，表现为"改了代码不生效、还查不出原因"。
这一层要改的代码很少，手动重启更可控。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=9000,
        reload=False,
        log_level="info",
    )
