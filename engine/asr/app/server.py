"""ASR 服务入口（模型层 · 听）·  端口 8001。

启动时加载模型（约 1.5~2 秒，1.8GB 常驻内存），紧接着空跑一次预热再置就绪——
预热是"把首句的一次性开销提前付掉"，细节见 model_holder._warmup。退出时释放。
**这个进程绝不开 reload** —— 改一行代码就要重新加载 1.8GB。

健康检查路径是 `/health`，**不带 /api 前缀**：ASR 是独立服务，
它的内部命名不受前端契约约束（网关负责映射成对外的 /api/asr）。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import model_holder, trace
from app.routers import transcribe

# 日志格式带 trace 字段：relay 传来的 X-Trace-Id 会出现在每一行里。
trace.configure()


@asynccontextmanager
async def lifespan(app: FastAPI):
    model_holder.load()
    yield
    model_holder.unload()


app = FastAPI(
    title="语音文字智能体 · ASR 服务（模型层）",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:9000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 最后加 = 最外层（Starlette 里后加的包在外面）：连 CORS 预检的响应也带 trace_id。
app.add_middleware(trace.TraceMiddleware)

app.include_router(transcribe.router)


@app.get("/health")
async def health():
    st = model_holder.status()
    return {
        "status": "ok" if st["loaded"] else "loading_or_failed",
        "layer": "model",
        "service": "asr",
        "port": 8001,
        **st,
    }


@app.get("/")
async def root():
    return {"layer": "model", "service": "asr", "port": 8001, "docs": "/docs"}
