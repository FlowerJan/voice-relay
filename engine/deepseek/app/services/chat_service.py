"""调 DeepSeek 的全部逻辑：拼历史 → 加 system → 调模型 → 转 SSE 事件流。

SSE 事件四种（每条都是 dict，由 router 序列化为 `data: {json}`）：
reasoning 思维链增量 / delta 正文增量 / done 结束 / error 出错。
类型常量在 `app/events.py`，契约原文在 `contracts/chat-events.json`——
这里不再重复写一遍字面量，免得三处各说各话。

错误处理映射（见设计文档 4.12）：
- 密钥无效 / 余额不足 → "API 密钥无效或余额不足"
- 请求超时           → "请求超时，请重试"
- 网络断开           → "网络连接失败"
- 其他               → 原始错误信息截断到 200 字
"""
from datetime import date
from typing import AsyncIterator, Dict, List

from openai import APIError, APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError

from app.config import SYSTEM_PROMPT, settings
from app.events import DELTA, DONE, ERROR, REASONING

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


# 图片的 token 成本取决于分辨率；前端已把长边压到 1400，这里按 1500 估算并留余量。
# 宁可高估——低估会让预算失效，请求真的超出上下文。
_IMAGE_TOKEN_ESTIMATE = 1500


def _text_tokens(text: str) -> int:
    """粗略估算 token：中日韩字符约 1 字 1 token，其余约 4 字符 1 token。

    只用于预算分配，不追求精确，但**宁可高估**：低估的后果是请求超出上下文。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return cjk + (len(text) - cjk) // 4 + 1


def _content_tokens(content) -> int:
    """一条消息的估算成本。content 可能是字符串，也可能是多模态 parts 数组。"""
    if isinstance(content, str):
        return _text_tokens(content)
    total = 0
    for part in content or []:
        if isinstance(part, dict):
            ptype, text = part.get("type"), part.get("text") or ""
        else:
            ptype = getattr(part, "type", None)
            text = getattr(part, "text", None) or ""
        if ptype == "text":
            total += _text_tokens(text)
        elif ptype == "image_url":
            total += _IMAGE_TOKEN_ESTIMATE
    return total


def _fit_budget(messages: List, budget: int) -> List:
    """从最新往回装，装不下就丢更早的；至少保留最后一条。

    最后一条即使超预算也必须留下——那是用户刚刚发的东西，丢掉等于"你发的我没收到"，
    比超出预算更糟。它本身的上限由前端的 docBodyBudget 负责。
    """
    picked: List = []
    used = 0
    for m in reversed(messages):
        cost = _content_tokens(m.content)
        if picked and used + cost > budget:
            break
        picked.append(m)
        used += cost
    picked.reverse()
    return picked


def _today_note() -> str:
    """把「今天几号」作为运行时信息注入，而不是写死在系统提示词里。

    两个方向都试过，原来的写法都不好：
    - 把日期写进 SYSTEM_PROMPT —— 第二天就错了，而且改它要动代码；
    - 在提示词里声明「你不知道今天的日期」—— 白白砍掉一个能力，
      「这周末是哪两天」「这个月还剩几天」这类问题本来答得了。

    正解是把会变的信息放在运行时注入：提示词管规则，注入管事实。
    """
    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    return f"\n今天是 {today.isoformat()}（星期{weekday}）。这个日期由系统提供。"


def system_content() -> str:
    """system 消息的完整内容：固定规则 + 运行时日期。"""
    return SYSTEM_PROMPT + _today_note()


def _usable(m) -> bool:
    """这条消息值不值得发给模型。

    system 不算：客户端禁带 system，后端统一加（否则任何人都能改人设和边界）。
    空内容也不算：前端等首字节时会留一个空的 assistant 占位，重试时也可能
    残留空消息，喂给模型只会让它困惑「你想让我回什么」。
    """
    if m.role == "system":
        return False
    content = m.content
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def build_messages_with_stats(history: List) -> tuple[List[Dict], Dict[str, int]]:
    """加 system、按**双重预算**裁剪历史，并把"裁掉了多少条"一起回报。

    两道限制缺一不可：条数挡住零碎消息堆积，token 挡住少数几条超长消息撑爆窗口。
    content 可能是纯文本，也可能是多模态 parts 数组（带图的消息）。

    **为什么要把裁剪条数回报出去**：这两道预算都会丢消息，而丢消息属于
    「用户能感知、却看不到原因」的故障——表现为"它怎么忘了我刚说的"，
    从外部完全无法反推是预算裁掉了。裁剪本身是产品决策，静默裁剪不是，
    所以数字要一路带到 done 事件里，由前端明说。

    先剔无效消息再取最近 N 条（顺序不能反）：反过来会让几条空占位
    白占掉同样多的名额，等于凭空少看几轮真历史。
    """
    usable: List = [m for m in history if _usable(m)]
    kept = _fit_budget(usable[-settings.history_limit:], settings.history_token_budget)

    messages: List[Dict] = [{"role": "system", "content": system_content()}]
    for m in kept:
        messages.append({"role": m.role, "content": m.content})

    return messages, {"history_used": len(kept), "history_dropped": len(usable) - len(kept)}


def build_messages(history: List) -> List[Dict]:
    """只关心消息、不关心统计时的入口（测试与既有调用方沿用）。"""
    return build_messages_with_stats(history)[0]


async def stream_chat(history: List) -> AsyncIterator[Dict]:
    """异步生成 SSE 事件字典。异常全部吞掉并转为 error 事件，不抛给上层。"""
    client = get_client()
    messages, stats = build_messages_with_stats(history)
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
                yield {"type": REASONING, "content": delta.reasoning_content}
            if delta.content:
                yield {"type": DELTA, "content": delta.content}
        # reason=length 表示达到 max_tokens 上限，正文被截断，前端需提示用户。
        # history_* 是本轮真实发给模型的历史条数：裁剪若悄悄发生，用户只会感到
        # "它忘了我刚说的"，所以这个数字必须跟着 done 一起送出去。
        yield {
            "type": DONE,
            "reason": finish_reason,
            "history_used": stats["history_used"],
            "history_dropped": stats["history_dropped"],
        }
    except BadRequestError as e:
        # 400 是"请求本身不合法"：图片格式不对、体积超限、part 结构写错。
        # 必须排在 APIError 之前——否则会被下面那条误报成"密钥无效"，
        # 排查时会往密钥/余额方向找，完全走错。
        yield {"type": ERROR, "message": f"请求被拒绝：{str(e)[:160]}"}
    except APIError:
        # 含 401 密钥无效 / 402 余额不足等
        yield {"type": ERROR, "message": "API 密钥无效或余额不足"}
    except APITimeoutError:
        yield {"type": ERROR, "message": "请求超时，请重试"}
    except APIConnectionError:
        yield {"type": ERROR, "message": "网络连接失败"}
    except Exception as e:  # noqa: BLE001
        yield {"type": ERROR, "message": str(e)[:200]}
