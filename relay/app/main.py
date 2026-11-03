"""协议层入口：唯一对外端口 9000。

职责（只有这四件）：
1. 统一入口     —— 前端只认 9000，代码里只有一个 baseURL
2. 统一错误格式 —— 下游错误在这里收口成一种形状
3. 分服务超时   —— ASR 和聊天分别配置
4. 链路 ID      —— 贯穿三个服务的日志

不含业务：这里没有 prompt、没有模型参数、没有 ASR 逻辑。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.concurrency import ConcurrencyLimitMiddleware
from app.middleware import ErrorEnvelopeMiddleware, TraceIdMiddleware
from app.routers import asr, chat, extract, health, tts
from app.upstream import close_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_client()


app = FastAPI(
    title="语音文字智能体 · 协议层（relay）",
    version="2.0",
    description="前端唯一入口。下游：8000 聊天 / 8001 ASR / 8002 TTS",
    lifespan=lifespan,
)

app.add_middleware(
    ErrorEnvelopeMiddleware,
)
# 顺序有讲究：add_middleware 是"后加的在外层"，所以请求的实际路径是
# CORS → TraceId → 并发上限 → ErrorEnvelope → 路由。
# 并发上限放在 TraceId 里面，是为了让那条 503 也带上 trace_id（它自己就是
# 一次可追的请求）；放在 ErrorEnvelope 外面，是因为它自己就是正常返回，
# 不需要异常收口。
app.add_middleware(ConcurrencyLimitMiddleware, limit=32)
app.add_middleware(
    TraceIdMiddleware,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(extract.router)
app.include_router(asr.router)
app.include_router(tts.router)


@app.get("/")
async def root():
    return {
        "layer": "protocol",
        "service": "relay",
        "port": 9000,
        "downstream": {
            "chat": "http://127.0.0.1:8000",
            "asr": "http://127.0.0.1:8001",
            "tts": "http://127.0.0.1:8002",
        },
        "docs": "/docs",
    }
