"""请求 / 响应模型（Pydantic v2）。

前端传完整历史，后端只取 role + content + docs。system 字段即使传来也会被忽略，
由 config.py 统一插入。

content 有两种形态：
- `str`：纯文本（绝大多数轮次）
- `list[dict]`：多模态 parts，形如
  `[{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]`
  带图的消息必须是后者。用 `list[dict]` 而不是精确建模，是为了不在这一层
  重复上游的 schema —— 上游已经改成三种 part、加一种就得改一次；
  这里只做"形状对得上就放行"，真实校验交给 DeepSeek（400 会被明确翻译）。

**docs 是结构化字段，不是已经拼好的文本。** 曾经由前端把正文拼进 content：
`<document name="...">正文</document>`。那样做的问题不在样式，在于**转义只能
在客户端做**——换个客户端直连 8000，文档里的 `</document>` 就能伪造出
"文档结束、下面是新指令"。现在正文原样传上来，由 `doc_fence` 拼标签并转义，
所有客户端自动获得同一套保护。
"""
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

ContentPart = dict


class DocAttachment(BaseModel):
    """一个附件的正文。

    `body` 为空表示"这次不给全文"（对话太长、超出附件预算），服务端会换成
    一句省略说明——不要在前端拼这句话，否则它就成了可以被伪造的正文。
    """

    name: str = "未命名文件"
    #: 展示给模型的元信息，形如「PDF，3 页，内容已截断」。由前端拼，因为页数、
    #: 是否扫描件这些事实只有抽取服务知道，engine 没必要再认识一遍。
    meta: str = ""
    body: str = ""


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[str, list[ContentPart]]
    docs: Optional[list[DocAttachment]] = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., description="前端持有的完整对话历史")
