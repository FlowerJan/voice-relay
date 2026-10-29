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

from openai import APIError, APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError

from app.config import SYSTEM_PROMPT, settings

# 共享客户端：每次请求新建 AsyncOpenAI 会反复建/拆连接池。
# 进程内共用一个，由 main.py 的 lifespan 在退出时释放。
_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
    return _client


async def close_client() -> None:
    """关闭共享客户端的连接池。lifespan 退出时调用。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def build_messages(history: List) -> List[Dict]:
    """加 system prompt，并把历史截断到最近 history_limit 条（忽略客户端 system）。

    content 可能是纯文本，也可能是多模态 parts 数组（带图的消息）。
    判断"空消息"要分开做：对字符串是 strip 后为空，对数组是长度为零。
    """
    messages: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    recent = history[-settings.history_limit:]
    for m in recent:
        if m.role == "system":
            continue  # 客户端禁带 system，后端统一加
        content = m.content
        if isinstance(content, str):
            if not content.strip():
                continue  # 跳过空消息（前端占位符残留）
        elif not content:
            continue
        messages.append({"role": m.role, "content": content})
    return messages


async def stream_chat(history: List) -> AsyncIterator[Dict]:
    """异步生成 SSE 事件字典。异常全部吞掉并转为 error 事件，不抛给上层。"""
    client = get_client()
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
    except BadRequestError as e:
        # 400 是"请求本身不合法"：图片格式不对、体积超限、part 结构写错。
        # 必须排在 APIError 之前——否则会被下面那条误报成"密钥无效"，
        # 排查时会往密钥/余额方向找，完全走错。
        yield {"type": "error", "message": f"请求被拒绝：{str(e)[:160]}"}
    except APIError:
        # 含 401 密钥无效 / 402 余额不足等
        yield {"type": "error", "message": "API 密钥无效或余额不足"}
    except APITimeoutError:
        yield {"type": "error", "message": "请求超时，请重试"}
    except APIConnectionError:
        yield {"type": "error", "message": "网络连接失败"}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": str(e)[:200]}
