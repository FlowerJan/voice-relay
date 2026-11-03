"""调 DeepSeek 的全部逻辑：拼历史 → 加 system → 调模型 → 转 SSE 事件流。

SSE 事件四种（每条都是 dict，由 router 序列化为 `data: {json}`）：
reasoning 思维链增量 / delta 正文增量 / done 结束 / error 出错。
类型常量在 `app/events.py`，契约原文在 `contracts/chat-events.json`——
这里不再重复写一遍字面量，免得三处各说各话。

错误映射全部收在 `describe_error` 里。

**判定顺序是这个函数的全部难点**（见 test_error_map.py）：openai 的异常层次是
`APITimeoutError → APIConnectionError → APIError`，`BadRequestError` /
`AuthenticationError` / `RateLimitError` 则都走 `APIStatusError → APIError`。
只要 `APIError` 排在前面，后面每个分支都会变成死代码——曾经就是这样，
断网和超时都被报成"API 密钥无效或余额不足"，排查方向直接跑偏。
"""
import logging
from datetime import date
from typing import AsyncIterator, Dict, List

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from app.config import SYSTEM_PROMPT, settings
from app.events import DELTA, DONE, ERROR, REASONING
from app.services.doc_fence import render as render_documents

logger = logging.getLogger(__name__)

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


def _content_chars(content) -> int:
    """一条消息有多少字（只数文本，图片不折算）。

    唯一用途是告诉用户"这次大概丢了多大的上下文"。**条数在这个对话里几乎不携带
    信息**：一条三万字的文档被整条砍掉也只算 1 条，用户看到"已省略 1 条"会以为
    丢的是一句寒暄，实际丢的是整份材料。数量要和真实损失对齐，否则这句提示
    比不提示更误导。
    """
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content or []:
        if isinstance(part, dict):
            ptype, text = part.get("type"), part.get("text") or ""
        else:
            ptype = getattr(part, "type", None)
            text = getattr(part, "text", None) or ""
        if ptype == "text":
            total += len(text)
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

    **带附件的消息例外**：只传文件不打字是常态（"这份合同有什么风险"常常懒得
    打字），内容为空但有 docs 时必须发出去。否则用户看着文件 chip 在界面上，
    模型那边却什么都没收到——这种"我明明传了"最难解释。
    """
    if m.role == "system":
        return False
    if getattr(m, "docs", None):
        return True
    content = m.content
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def _render_content(content, docs):
    """把附件围栏拼进这条消息的 content。没有附件时原样返回。

    content 可能是纯文本，也可能是多模态 parts 数组。带图 + 带文档是可以同时
    出现的（"看这张截图，再对照这份 PDF"），所以两种情况都要处理。
    """
    if not docs:
        return content
    if isinstance(content, str):
        return render_documents(content, docs)

    # 多模态：围栏并进第一个 text part。没有 text part 就自己造一个放最前。
    # 其余 part 顺序一律不动——上游对"先文后图"还是"先图后文"的解读并不相同。
    parts = [dict(part) if isinstance(part, dict) else part for part in (content or [])]
    for index, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "text":
            parts[index] = {**part, "text": render_documents(str(part.get("text") or ""), docs)}
            return parts
    return [{"type": "text", "text": render_documents("", docs)}, *parts]


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

    **附件围栏在裁剪之前拼**。文档正文是真的要发出去的 token，晚一步估就会
    低估一整份 PDF 的成本，预算形同虚设。前端的 docBodyBudget 只管"哪些附件
    带全文"，挡不住直连 8000 的客户端一次塞三万字。
    """
    usable: List = [m for m in history if _usable(m)]

    prepared: List = []
    for m in usable:
        docs = getattr(m, "docs", None)
        prepared.append(
            m if not docs else m.model_copy(update={"content": _render_content(m.content, docs)})
        )

    kept = _fit_budget(prepared[-settings.history_limit:], settings.history_token_budget)

    # 被丢掉的是 prepared 的头部：条数限制取的是尾部，token 预算也只会继续丢前面的。
    # 字符数按 prepared（围栏已展开）算——文档正文是真的没发出去，要算进损失里；
    # 按 usable 算的话，被丢掉的附件只能算成"一行文件名"。
    drop_n = len(prepared) - len(kept)
    dropped_chars = sum(_content_chars(m.content) for m in prepared[:drop_n])

    messages: List[Dict] = [{"role": "system", "content": system_content()}]
    for m in kept:
        messages.append({"role": m.role, "content": m.content})

    return messages, {
        "history_used": len(kept),
        "history_dropped": drop_n,
        "history_dropped_chars": dropped_chars,
    }


def build_messages(history: List) -> List[Dict]:
    """只关心消息、不关心统计时的入口（测试与既有调用方沿用）。"""
    return build_messages_with_stats(history)[0]


def _detail(exc: BaseException, limit: int = 160) -> str:
    """取上游给的最有信息量的那句话，截断。"""
    text = getattr(exc, "message", None) or str(exc)
    return text[:limit]


def explain_error(exc: BaseException) -> Dict[str, str]:
    """把任意异常翻成 `{message, hint}`。

    message 说「哪里坏了」，hint 说「现在能做什么」。分两个字段而不是合成一句，
    理由和 relay 侧一致：日志要前者，用户要后者；合起来 message 会变长，
    而且终端里 grep 一个错误码时会被后半句干扰。

    顺序从最具体到最抽象，**不能调换**。`APITimeoutError` 必须早于
    `APIConnectionError`（前者是后者子类），所有 `APIStatusError` 的子类必须早于
    `APIError`（后者几乎是所有 openai 异常的祖先）。写反了不会报错，只会让
    分支变成死代码——这类 bug 只有"断网时用户看到密钥无效"才会暴露。
    """
    if isinstance(exc, BadRequestError):
        # 400 是"请求本身不合法"：图片格式不对、体积超限、part 结构写错。
        # 上游原文要带出来，它通常直接写着哪个字段不对。
        return {
            "message": f"请求被拒绝：{_detail(exc)}",
            "hint": "多半是图片格式或体积超限：换 JPG/PNG、减少张数，或把问题拆短",
        }
    if isinstance(exc, AuthenticationError):
        return {
            "message": "API 密钥无效或已失效",
            "hint": "打开 engine/deepseek/.env 换成有效的 DEEPSEEK_API_KEY，然后重启 8000",
        }
    if isinstance(exc, PermissionDeniedError):
        return {
            "message": f"没有权限调用该模型：{_detail(exc)}",
            "hint": "换一个有该模型权限的 key，或把 DEEPSEEK_MODEL 改成 key 能用的模型",
        }
    if isinstance(exc, RateLimitError):
        return {
            "message": "请求太频繁，被上游限流",
            "hint": "等十几秒再发一次；频繁触发就把问题拆短，减少单次 token 量",
        }
    if isinstance(exc, APITimeoutError):
        return {
            "message": "请求超时",
            "hint": "再发一次；连续超时就检查本机到 api.deepseek.com 的网络",
        }
    if isinstance(exc, APIConnectionError):
        # 本机有 HTTP_PROXY 时，连不上 api.deepseek.com 多半是代理劫持了请求。
        return {
            "message": "网络连接失败，没能连上模型服务",
            "hint": "确认能访问 api.deepseek.com；本机的 HTTP_PROXY 可能拦截了请求",
        }
    if isinstance(exc, InternalServerError):
        return {
            "message": "上游服务故障（5xx）",
            "hint": "等一两分钟再试；上游故障期间重试也没用",
        }
    if isinstance(exc, APIStatusError):
        if exc.status_code == 402:
            return {
                "message": "账户余额不足",
                "hint": "到 platform.deepseek.com 充值后再试",
            }
        return {
            "message": f"上游返回 {exc.status_code}：{_detail(exc)}",
            "hint": "按上面的原文处理；不确定就先重试一次",
        }
    if isinstance(exc, APIError):
        return {
            "message": f"调用失败：{_detail(exc)}",
            "hint": "重试一次；仍失败就看 8000 窗口的最后几行日志",
        }
    return {
        "message": str(exc)[:200] or "未知错误",
        "hint": "重试一次；仍失败就看 8000 窗口的最后几行日志",
    }


async def stream_chat(history: List) -> AsyncIterator[Dict]:
    """异步生成 SSE 事件字典。异常全部吞掉并转为 error 事件，不抛给上层。"""
    client = get_client()
    messages, stats = build_messages_with_stats(history)
    finish_reason = "stop"
    stream = None
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
            "history_dropped_chars": stats["history_dropped_chars"],
        }
    except Exception as e:  # noqa: BLE001
        # 全交给 explain_error，不再在这里排 except 链：
        # 顺序写错的代价是静默的（分支变死代码），放一个函数里才测得住。
        logger.warning("模型调用失败（%s）：%s", type(e).__name__, e)
        info = explain_error(e)
        # 逐字段写死，不用 **info：契约测试靠 AST 扫 yield 的键集合，
        # 展开运算符会让它无法静态校验（那正是它要防的后门）。
        yield {"type": ERROR, "message": info["message"], "hint": info["hint"]}
    finally:
        # 用户点「停止」时，上层 aclose() 这个生成器，GeneratorExit 直接穿到
        # finally（它继承 BaseException，上面的 except Exception 接不住）。
        # SDK 只在**把流读完**时自动关连接；中途退出的话要等 GC 才释放，
        # 而在这之前上游仍在按 token 生成并计费。
        if stream is not None:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001  关不掉不该盖掉真正的错误
                logger.debug("关闭上游流失败", exc_info=True)
