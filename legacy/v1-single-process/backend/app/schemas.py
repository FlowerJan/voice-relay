"""请求 / 响应模型（Pydantic v2）。

前端传完整历史，后端只取 role + content。system 字段即使传来也会被忽略，
由 config.py 统一插入。
"""
from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., description="前端持有的完整对话历史")
