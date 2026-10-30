"""FastAPI 入口：装配路由 + CORS（仅放行前端 5173）。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import chat

app = FastAPI(title="语音文字智能体 · 聊天服务")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # 不用 *，最小暴露面
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)


@app.get("/")
async def root():
    return {"msg": "语音文字智能体后端运行中", "docs": "/docs"}
