"""聊天服务启动器：双击即可起，不用记 uvicorn 参数。

端口 8000 是这一层的固定身份，看端口就知道该进哪个目录看日志。
开发时可开 reload（这里只加载 openai 客户端，重载成本接近零）。
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info",
    )
