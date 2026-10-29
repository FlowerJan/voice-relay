"""聊天服务（模型层 · DeepSeek 适配器）入口。

职责边界：这个进程只做一件事——把内部契约翻译成 DeepSeek 的 OpenAI 兼容协议。
它不知道 ASR 存在，也不负责路由分发（那是协议层 relay 的事）。

启动方式二选一：
- `venv\\Scripts\\python.exe run.py`（推荐，端口写死在这一层）
- `venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000`
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import chat, extract
from app.services.chat_service import close_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时准备，退出时释放连接池。"""
    yield
    await close_client()


app = FastAPI(
    title="语音文字智能体 · 聊天服务（模型层）",
    version="2.0",
    lifespan=lifespan,
)

# CORS：前端最终只连 9000，但允许 5173 直连便于排查
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:9000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(extract.router)


@app.get("/")
async def root():
    return {
        "layer": "model",
        "service": "deepseek",
        "port": 8000,
        "model": settings.deepseek_model,
        "docs": "/docs",
    }
