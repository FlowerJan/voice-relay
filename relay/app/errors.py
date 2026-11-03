"""错误应答的唯一出口：一个形状，加一句「现在能做什么」。

**为什么要有 hint。** 原来的错误体只有 code / message：message 说的是"哪里坏了"，
例如「ASR 识别超时（>120 秒）」——用户读完仍不知道下一步。是重录？换格式？还是等？
于是这类消息在界面上等价于"出错了"，用户唯一的动作是再点一次，错了再点一次。

hint 回答的是另一个问题：**我现在能做什么**。它必须能被直接执行，所以只写
用户在自己电脑上真的能做的动作（重启某个端口、分段录、换格式、点重试），
不写"请联系管理员""检查网络配置"这种等于没说的话。

**为什么不把两者合成一句。** 合成之后 message 必然变长，而 message 还会被日志、
终端、trace 页面消费；那里要的是「什么坏了」，不是操作指引。两种读者，两个字段。

**hint 缺失时的行为。** 未知错误码不编造建议——编出来的建议比没有更糟，
用户会照着做，然后发现没用。这时给一句通用的"重试并带上 trace_id"，它任何
情况下都成立，也不会误导。
"""
from typing import Final

from fastapi.responses import JSONResponse

from app.middleware import get_trace_id

#: 未知错误码的兜底建议。任何情况下都成立，不会误导，但也帮不上具体忙。
GENERIC_HINT: Final[str] = "重试一次；仍失败请把下面的 trace_id 一起报出来"

#: 精确到错误码的建议。每条都必须是用户能立刻做的动作。
_HINTS: Final[dict[str, str]] = {
    # ---- 聊天（含余额） ----
    "chat_unreachable": "确认聊天服务在 8000 端口跑着；dev.py 会把它一起拉起来",
    "chat_timeout": "稍等几秒再发一次；连续超时就去看 8000 窗口的最后几行日志",
    "chat_interrupted": "直接点这条回答上的「重试」重新发一次",
    "chat_protocol_error": "重启 8000 再试；反复出现说明下游实现有问题，不是网络抖动",
    "chat_no_output": "点「重试」；仍是空说明 8000 收下了请求却没在正常回话",
    "chat_upstream_error": "多为模型侧拒绝（密钥/余额/请求过长），按上面的原文处理；请求过长就把这条提问拆短",
    "chat_error": "确认 9000 和 8000 都在运行，然后重试",
    "balance_timeout": "稍后刷新页面再看；余额查不到不影响继续对话",
    "balance_error": "刷新页面重试；余额只是展示，不影响继续对话",
    "balance_bad_response": "刷新页面重试；仍失败说明余额接口返回了非预期内容",
    # ---- ASR ----
    "asr_unreachable": "确认 ASR 服务在 8001 端口跑着",
    "asr_busy": "ASR 正在识别上一段录音：等它出结果再录一次（本地单进程，一次只处理一段）",
    "asr_timeout": "这段录音太长或机器正忙：分段录，或等几秒再录一次",
    "asr_error": "重新录一次；反复失败就重启 8001",
    "asr_bad_response": "重新录一次；仍失败说明 8001 没在正常响应",
    "asr_upstream_error": "换一段更短的录音再试；仍失败说明这段音频解析不了",
    # ---- 文档抽取 ----
    "extract_timeout": "文件太大或页数太多：拆成几个小文件分别上传",
    "extract_failed": "扫描件先做 OCR，老 .doc 另存为 .docx，超大文件拆开再传",
    "extract_error": "重新上传一次；反复失败就重启 8000",
    "extract_bad_response": "重新上传一次；仍失败说明解析服务没在正常响应",
    # ---- TTS ----
    "tts_unreachable": "确认 TTS 服务在 8002 端口跑着",
    "tts_timeout": "这段回答太长：先只选一段文字再点播报",
    "tts_error": "再点一次播报；反复失败就重启 8002",
    "tts_upstream_error": "再点一次播报；仍失败说明这段文本合成不了，可先去掉特殊符号",
    # ---- 请求体超限：四条链路各自有上限，建议必须分开写 ----
    # 合成一句通用的"请求太大了"，用户不知道是图片多了还是文件大了，也就没法动手。
    "chat_body_too_large": "删掉几张图片再发，或把长文档拆开分几轮问",
    "tts_body_too_large": "要朗读的文字太长：先只选一段再点播报",
    "asr_body_too_large": "这段录音太长：分几段录，单段控制在 60 秒内",
    "extract_body_too_large": "文件超过 20MB：拆成几个小文件，或压缩后再传",
    # ---- 请求本身 ----
    "bad_request": "刷新页面后重试；用页面上的按钮上传，不要手工构造请求",
    "internal_error": "重试一次；仍失败就把 trace_id 和触发的操作一起报出来",
    # 网关自己的并发上限。这条只有"同时发太多"时才出现，正常单人使用见不到。
    "relay_overloaded": "同时进行的请求太多了：等手上这几条结束再继续，不用重试",
}

#: 链路级建议：错误码以 `asr_` 开头就能命中；下游自造的码（bad_audio）连前缀
#: 都对不上时，由调用方显式传 link 指定退到哪一条。
_LINK_HINTS: Final[dict[str, str]] = {
    "chat": "点「重试」重新发一次；仍失败就看 8000 窗口的日志",
    "balance": "刷新页面重试；余额只是展示，不影响继续对话",
    "asr": "重新录一次；仍失败就换更短的录音或重启 8001",
    "extract": "重新上传一次；不行就换个格式（PDF / DOCX / TXT）再传",
    "tts": "再点一次播报；仍失败就重启 8002",
}


def hint_for(code: str, link: str | None = None) -> str:
    """错误码 → 可执行建议。未知码返回通用兜底，绝不返回空串。

    `link` 是"这条错误从哪条链路来的"。下游会自造错误码（bad_audio、no_speech），
    映射表不可能预先收录，但"换一段录音再试"这类建议跟具体码无关、照样成立，
    所以允许调用方指定退到链路建议，而不是一律落到那句什么都没说的通用兜底。
    """
    exact = _HINTS.get(code)
    if exact:
        return exact
    for name, text in _LINK_HINTS.items():
        if code.startswith(name + "_"):
            return text
    if link:
        return _LINK_HINTS.get(link, GENERIC_HINT)
    return GENERIC_HINT


def merge_upstream_error(
    payload: dict,
    status: int,
    link: str,
    trace_id: str | None = None,
) -> dict:
    """把下游的错误体补成统一形状。

    **"透传"不能是原样转发。** 下游各有各的写法，实测 TTS 的错误体只有
    `{"error": {"code", "message"}}`，没有 status；一旦原样转发，前端按统一形状
    解析就会拿到 undefined，气泡里少一行、或者整个解析失败。所以这里逐字段补齐，
    只补缺的，不覆盖下游已有的（它比我更清楚错在哪）。

    `link` 用于两件事：给 hint 指明该落到哪条链路的建议，以及 code 缺失时兜一个
    `<link>_upstream_error`，而不是把空串送出去。
    """
    error = payload["error"]
    error.setdefault("code", f"{link}_upstream_error")
    error.setdefault("message", "下游服务返回错误")
    error.setdefault("status", status)
    error.setdefault("trace_id", trace_id or get_trace_id())
    # hint 依赖 code，必须放在 code 补齐之后。
    error.setdefault("hint", hint_for(str(error.get("code") or ""), link=link))
    return payload


def error_response(
    status: int,
    code: str,
    message: str,
    hint: str | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """构造统一错误体。

    `hint` 只在调用点知道得比映射表多时才显式传；其余情况由 code 推导，
    这样新增错误码的人只要在 `_HINTS` 里补一行，不必记得每个返回点都带上建议。

    `trace_id` 同理：默认从 ContextVar 取。只有中间件需要显式传——它在最外层，
    而 TraceIdMiddleware 的 finally 已经把 ContextVar 重置过了，直接取会拿到 "-"，
    于是用户报上来的 500 反而追不到日志。
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "hint": hint if hint is not None else hint_for(code),
                "status": status,
                "trace_id": trace_id or get_trace_id(),
            }
        },
    )
