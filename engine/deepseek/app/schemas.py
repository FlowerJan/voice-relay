"""请求 / 响应模型（Pydantic v2）。

前端传完整历史，后端只取 role + content。system 字段即使传来也会被忽略，
由 config.py 统一插入。

content 有两种形态：
- `str`：纯文本（绝大多数轮次）
- `list[dict]`：多模态 parts，形如
  `[{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]`
  带图的消息必须是后者。用 `list[dict]` 而不是精确建模，是为了不在这一层
  重复上游的 schema —— 上游已经改成三种 part、加一种就得改一次；
  这里只做"形状对得上就放行"，真实校验交给 DeepSeek（400 会被明确翻译）。
"""
from typing import Literal, Union

from pydantic import BaseModel, Field

ContentPart = dict


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[str, list[ContentPart]]


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., description="前端持有的完整对话历史")
