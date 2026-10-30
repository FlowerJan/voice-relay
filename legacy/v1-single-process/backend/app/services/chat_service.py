"""调 DeepSeek 的全部逻辑：拼历史 → 加 system → 调模型 → 转 SSE 事件流。

SSE 事件四种（每条都是 dict，由 router 序列化为 `data: {json}`）：
- {"type": "reasoning", "content": ...}  思维链增量
- {"type": "delta", "content": ...}       正文增量
- {"type": "done", "reason": ...}        结束，reason=length 表示被 max_tokens 截断
- {"type": "error", "message": ...}       出错

错误处理映射（见设计文档 4.12）：
- 密钥无效 / 余额不足 → "API 密钥无效或余额不足"
- 请求超时           → "请求超时，请重试"
- 网络断开           → "网络连接失败"
- 其他               → 原始错误信息截断到 200 字
"""
from typing import AsyncIterator, Dict, List

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI

from app.config import SYSTEM_PROMPT, settings


def build_messages(history: List) -> List[Dict[str, str]]:
    """加 system prompt，并把历史截断到最近 history_limit 条（忽略客户端 system）。"""
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    recent = history[-settings.history_limit:]
    for m in recent:
        if m.role == "system":
            continue  # 客户端禁带 system，后端统一加
        if not m.content.strip():
            continue  # 跳过空消息（前端占位符残留）
        messages.append({"role": m.role, "content": m.content})
    return messages


async def stream_chat(history: List) -> AsyncIterator[Dict]:
    """异步生成 SSE 事件字典。异常全部吞掉并转为 error 事件，不抛给上层。"""
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )
    messages = build_messages(history)
    finish_reason = "stop"
    try:
        stream = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=messages,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            # deepseek-flash 默认 high 档思维链，reasoning_content 与 content 双字段
            if getattr(delta, "reasoning_content", None):
                yield {"type": "reasoning", "content": delta.reasoning_content}
            if delta.content:
                yield {"type": "delta", "content": delta.content}
        # reason=length 表示达到 max_tokens 上限，正文被截断，前端需提示用户
        yield {"type": "done", "reason": finish_reason}
    except APIError:
        # 含 401 密钥无效 / 402 余额不足等
        yield {"type": "error", "message": "API 密钥无效或余额不足"}
    except APITimeoutError:
        yield {"type": "error", "message": "请求超时，请重试"}
    except APIConnectionError:
        yield {"type": "error", "message": "网络连接失败"}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": str(e)[:200]}
