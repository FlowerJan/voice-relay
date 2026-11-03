"""错误收口：无论上游怎么错，用户只看到一种形状。

    {"error": {"code", "message", "hint", "status", "trace_id"}}

这条约束是协议层第二个存在理由。没有它，前端要为每条链路写一套错误处理，
而且"上游返回了 500 的 HTML"这种最常见的情况会直接把解析器搞崩。

这里覆盖的不只是"错了变什么码"，还有三个更细的决定：
- 上游已是统一格式 → 透传，只补 trace_id 与 hint，不重新包装（否则 message 被吞掉）
- 上游不是 JSON → 包一层，把原始文本截断后带上（否则排查时只剩 "500"）
- 每个码都要配一句**能照着做**的建议（hint）：message 说"哪里坏了"，
  hint 说"现在做什么"。只有前者的话，界面上的错误和"出错了"三个字没有区别。
"""
import httpx
import pytest

from app.config import settings
from app.errors import GENERIC_HINT, hint_for
from app.routers.asr import asr
from app.routers.chat import chat_stream
from app.routers.extract import extract
from app.routers.tts import tts

from conftest import error_of, make_request

pytestmark = pytest.mark.anyio

MULTIPART = {"content-type": "multipart/form-data; boundary=abc"}
JSON = {"content-type": "application/json"}


# ---------------- 聊天 ----------------


async def test_chat_unreachable_is_502_not_500(upstream):
    """上游没启动是我们这一侧的可预期故障，不是"服务器内部错误"。"""
    upstream.raise_(httpx.ConnectError("连不上"))
    response = await chat_stream(await make_request())

    assert response.status_code == 502
    error = error_of(response)
    assert error["code"] == "chat_unreachable"
    assert error["status"] == 502
    assert "8000" in error["message"]


async def test_chat_connect_timeout_is_504(upstream):
    """连接超时是"等不到"，与"连不上"要分开——用户能据此判断是没开还是卡住。"""
    upstream.raise_(httpx.ConnectTimeout("连接超时"))
    response = await chat_stream(await make_request())

    assert response.status_code == 504
    assert error_of(response)["code"] == "chat_timeout"


async def test_chat_read_timeout_is_504_and_does_not_retry(upstream):
    """读超时不重试：上游已经连上却卡住，重试等于让用户再等一整个读超时周期。"""
    upstream.raise_(httpx.ReadTimeout("读超时"))
    response = await chat_stream(await make_request())

    assert response.status_code == 504
    assert error_of(response)["code"] == "chat_timeout"
    assert upstream.count == 1, "读超时不该重试"


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_chat_upstream_status_is_passed_through(upstream, status):
    """上游的 4xx/5xx 原样透出状态码：把 429 改成 500 会让用户以为服务坏了。

    同时必须带上上游原文——"rate limit exceeded" 和 "context length exceeded"
    都是 400，不带原文就只能靠猜。
    """
    upstream.reply(httpx.Response(status, text="upstream says: rate limit exceeded"))
    response = await chat_stream(await make_request())

    assert response.status_code == status
    error = error_of(response)
    assert error["code"] == "chat_upstream_error"
    assert "rate limit exceeded" in error["message"]
    assert upstream.count == 1, "4xx/5xx 是请求本身的问题，重试没有意义"


async def test_chat_upstream_status_truncates_long_detail(upstream):
    """上游可能回一整页 HTML，错误消息不能原样塞给前端。"""
    upstream.reply(httpx.Response(500, text="x" * 5000))
    response = await chat_stream(await make_request())

    assert len(error_of(response)["message"]) <= 300


async def test_chat_empty_stream_is_reported_as_no_output(upstream):
    """上游 200 却一个字节没写：重试一次，仍为空则明确说"没返回数据"。

    这比让前端收到一个空的 SSE 流好——后者表现为"转圈圈然后啥也没有"。
    """
    # 每次请求都要造一个新的 Response：httpx 的响应是一次性的，
    # 同一个对象被消费后 is_stream_consumed=True，第二次读会抛 StreamConsumed。
    upstream.set(lambda request: httpx.Response(200, stream=_empty_stream()))
    response = await chat_stream(await make_request())

    assert response.status_code == 502
    assert error_of(response)["code"] == "chat_no_output"
    assert upstream.count == 2, "首块未达属于可安全重试的范围"


def _empty_stream() -> httpx.AsyncByteStream:
    """一个立刻结束、不产出任何块的流。"""
    from conftest import RecordedStream

    return RecordedStream([])


# ---------------- ASR ----------------


async def test_asr_rejects_non_multipart_without_calling_upstream(upstream):
    """参数错要在协议层就挡掉，不要转给一个必然失败的下游。"""
    response = await asr(await make_request(b"{}", path="/api/asr", headers=JSON))

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "bad_request"
    assert upstream.count == 0, "参数不合法时不该向上游发起请求"


async def test_asr_unreachable_is_502(upstream):
    upstream.raise_(httpx.ConnectError("连不上"))
    response = await asr(await make_request(b"x", path="/api/asr", headers=MULTIPART))

    assert response.status_code == 502
    error = error_of(response)
    assert error["code"] == "asr_unreachable"
    assert "8001" in error["message"]


async def test_asr_timeout_message_states_the_limit(upstream):
    """超时消息要写清多少秒——用户据此判断是不是该换更短的录音。"""
    upstream.raise_(httpx.ReadTimeout("超时"))
    response = await asr(await make_request(b"x", path="/api/asr", headers=MULTIPART))

    assert response.status_code == 504
    error = error_of(response)
    assert error["code"] == "asr_timeout"
    assert str(int(settings.asr_timeout)) in error["message"]


async def test_asr_passes_through_envelope_and_fills_trace(upstream):
    """下游已经是统一格式 → 原样透传，只补 trace_id，不重新包一层。

    重新包装会把下游精心写的 message 丢掉，只剩一个笼统的"ASR 服务出错"。
    """
    upstream.reply(
        httpx.Response(
            422,
            json={"error": {"code": "bad_audio", "message": "音频格式不支持", "status": 422}},
        )
    )
    response = await asr(await make_request(b"x", path="/api/asr", headers=MULTIPART))

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "bad_audio", "下游的错误码不该被改写"
    assert error["message"] == "音频格式不支持"
    assert error["trace_id"], "透传时必须补上 trace_id，否则跨跳排查断链"
    # 下游只说了"格式不支持"，没说"那我该怎么办"——建议由我们这一层补。
    assert "录" in error["hint"] or "换" in error["hint"]


async def test_asr_wraps_plain_text_error(upstream):
    """下游回了 HTML / 纯文本错误页：包一层，但把原文带上。"""
    upstream.reply(httpx.Response(502, text="<html>Bad Gateway</html>"))
    response = await asr(await make_request(b"x", path="/api/asr", headers=MULTIPART))

    assert response.status_code == 502
    error = error_of(response)
    assert error["code"] == "asr_upstream_error"
    assert "Bad Gateway" in error["message"]


async def test_asr_success_but_invalid_json_is_explicit(upstream):
    """200 但不是 JSON —— 常见于下游被换成了静态服务器。必须说清，不能假装成功。"""
    upstream.reply(httpx.Response(200, text="<!doctype html>"))
    response = await asr(await make_request(b"x", path="/api/asr", headers=MULTIPART))

    assert response.status_code == 502
    assert error_of(response)["code"] == "asr_bad_response"


# ---------------- 抽取 ----------------


async def test_extract_rejects_non_multipart(upstream):
    response = await extract(await make_request(b"{}", path="/api/extract", headers=JSON))

    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"
    assert upstream.count == 0


async def test_extract_reports_downstream_message(upstream):
    """抽取失败的原因很具体（扫描件无文字层、文件加密、页数超限），必须带出来。"""
    upstream.reply(
        httpx.Response(400, json={"ok": False, "message": "这份 PDF 没有文字层，疑似扫描件"})
    )
    response = await extract(await make_request(b"x", path="/api/extract", headers=MULTIPART))

    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "extract_failed"
    assert "扫描件" in error["message"]


async def test_extract_timeout_mentions_the_limit(upstream):
    upstream.raise_(httpx.ReadTimeout("超时"))
    response = await extract(await make_request(b"x", path="/api/extract", headers=MULTIPART))

    assert response.status_code == 504
    error = error_of(response)
    assert error["code"] == "extract_timeout"
    assert str(int(settings.extract_timeout)) in error["message"]


async def test_extract_non_json_body(upstream):
    upstream.reply(httpx.Response(200, text="not json"))
    response = await extract(await make_request(b"x", path="/api/extract", headers=MULTIPART))

    assert response.status_code == 502
    assert error_of(response)["code"] == "extract_bad_response"


# ---------------- TTS ----------------


async def test_tts_unreachable_is_502(upstream):
    upstream.raise_(httpx.ConnectError("连不上"))
    response = await tts(await make_request(b"{}", path="/api/tts", headers=JSON))

    assert response.status_code == 502
    error = error_of(response)
    assert error["code"] == "tts_unreachable"
    assert "8002" in error["message"]


async def test_tts_timeout_is_504(upstream):
    upstream.raise_(httpx.ReadTimeout("超时"))
    response = await tts(await make_request(b"{}", path="/api/tts", headers=JSON))

    assert response.status_code == 504
    assert error_of(response)["code"] == "tts_timeout"


async def test_tts_upstream_error_is_wrapped(upstream):
    upstream.reply(httpx.Response(500, text="voice not found"))
    response = await tts(await make_request(b"{}", path="/api/tts", headers=JSON))

    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "tts_upstream_error"
    assert "voice not found" in error["message"]


# ---------------- 形状一致性 ----------------


async def test_every_failure_has_the_same_shape(upstream):
    """四条链路各取一个失败样例，比对错误体形状完全一致。

    前端只写一套错误渲染，靠的就是这个不变量。
    """
    cases = [
        (chat_stream, "/api/chat/stream", JSON),
        (asr, "/api/asr", MULTIPART),
        (extract, "/api/extract", MULTIPART),
        (tts, "/api/tts", JSON),
    ]
    shapes = []
    for router, path, headers in cases:
        upstream.raise_(httpx.ConnectError("连不上"))
        response = await router(await make_request(b"x", path=path, headers=headers))
        error = error_of(response)
        shapes.append(set(error))
        assert error["status"] == response.status_code, "错误体里的 status 必须与 HTTP 状态码一致"

    assert all(shape == shapes[0] for shape in shapes), f"错误体形状不一致：{shapes}"


# ---------------- 建议（hint） ----------------


async def test_every_failure_path_has_a_tailored_hint(upstream):
    """每条真实失败路径都要有专属建议，不能落到通用兜底。

    通用兜底（"重试一次，仍失败带上 trace_id"）是留给未知码的：它对任何错误都
    成立，也正因为对任何错误都成立，对具体故障几乎没有帮助。连不上、超时、
    下游拒绝这类每天都在发生的故障，如果也只能拿到这一句，说明映射表漏了行。
    """
    cases = [
        ("chat", chat_stream, "/api/chat/stream", JSON, httpx.ConnectError("x")),
        ("chat", chat_stream, "/api/chat/stream", JSON, httpx.ConnectTimeout("x")),
        ("chat", chat_stream, "/api/chat/stream", JSON, httpx.ReadTimeout("x")),
        ("asr", asr, "/api/asr", MULTIPART, httpx.ConnectError("x")),
        ("asr", asr, "/api/asr", MULTIPART, httpx.ReadTimeout("x")),
        ("extract", extract, "/api/extract", MULTIPART, httpx.ConnectError("x")),
        ("extract", extract, "/api/extract", MULTIPART, httpx.ReadTimeout("x")),
        ("tts", tts, "/api/tts", JSON, httpx.ConnectError("x")),
        ("tts", tts, "/api/tts", JSON, httpx.ReadTimeout("x")),
    ]
    seen: list[tuple[str, str, str, str]] = []
    for link, router, path, headers, exc in cases:
        upstream.raise_(exc)
        error = error_of(await router(await make_request(b"x", path=path, headers=headers)))
        seen.append((link, error["code"], error["message"], error["hint"]))

    weak = [item[1] for item in seen if item[3] == GENERIC_HINT]
    assert not weak, f"这些失败没有专属建议，只落到了通用兜底：{weak}"

    repeated = [item[1] for item in seen if item[3] == item[2]]
    assert not repeated, "建议不能直接复述 message：那只是把问题说了两遍"


async def test_upstream_rejection_also_gets_a_hint(upstream):
    """4xx/5xx 透传路径容易被漏掉：错误码换了，映射表却没跟上。"""
    upstream.reply(httpx.Response(429, text="rate limit exceeded"))
    error = error_of(await chat_stream(await make_request()))

    assert error["code"] == "chat_upstream_error"
    assert error["hint"] and error["hint"] != GENERIC_HINT


def test_unknown_code_does_not_invent_advice():
    """未知码给通用建议，不猜。猜出来的建议比没有更糟——用户会照着做，然后发现没用。"""
    assert hint_for("some_future_code") == GENERIC_HINT
    # 下游自造的码（没有 asr_ 前缀）本身认不出来，靠调用方指明链路才拿到建议
    assert hint_for("bad_audio") == GENERIC_HINT
    assert hint_for("bad_audio", link="asr") != GENERIC_HINT
    assert hint_for("asr_anything_new") != GENERIC_HINT


def test_no_hint_tells_the_user_to_contact_someone():
    """这台机器上没有"管理员"这个人。伪建议不如不写。"""
    from app import errors

    all_hints = list(errors._HINTS.values()) + list(errors._LINK_HINTS.values())
    offenders = [h for h in all_hints if "联系" in h or "管理员" in h]
    assert not offenders, f"这些建议没有可执行的动作：{offenders}"
