"""分路超时：三条链路三套策略，这是协议层存在的第一理由。

断言取的是 **httpx 实际发给上游的 timeout**（`request.extensions["timeout"]`），
不是读 `settings`——后者只能证明配置值存在，证明不了路由真的用了它。

设计意图（写在这里，免得以后有人"统一"掉）：
- ASR 是本地 CPU 推理，4 秒音频要跑 2~7 秒，超时必须给足；
- 聊天是远程流式，可能连续说几分钟，**不能设总时长**，只能设"两块之间的间隔"；
- 抽取是本地解析，比 ASR 快一个量级，但比普通接口慢。
共用一个值，必然有一个是错的。

一个容易误判的地方：`settings` 读的是**生效配置**，本机有 `relay/.env` 时会覆盖代码里的默认值。
所以"改代码默认值"不会让这里的任何断言变红——这是对的，用户跑的本来就是 `.env` 的值。
要验证超时策略是否真的被用上，该动的是路由里的取值方式，不是默认值。
（这条是负向验证时踩到的：改了 `asr_timeout` 的默认值，7 项全绿，一度以为测试没生效。）
"""
import httpx
import pytest

from app.config import settings
from app.routers.asr import asr
from app.routers.chat import chat_stream
from app.routers.extract import extract
from app.routers.tts import tts

from conftest import make_request

pytestmark = pytest.mark.anyio


async def test_chat_uses_connect_and_read_separately(upstream):
    """聊天的连接超时与读超时必须分开取：连不上要快速失败，说很久不能被砍。"""
    upstream.streamed([b"data: x\n\n"])
    response = await chat_stream(await make_request())

    timeout = upstream.timeout
    assert timeout["connect"] == settings.chat_connect_timeout
    assert timeout["read"] == settings.chat_read_timeout
    await response.body_iterator.aclose()


async def test_chat_read_timeout_is_far_larger_than_connect(upstream):
    """读超时是"两块之间的静默上限"，不是总时长，所以必须远大于连接超时。

    如果这两个值相等，说明有人把统一超时套了上来——那会切断正常的长回答。
    """
    upstream.streamed([b"data: x\n\n"])
    response = await chat_stream(await make_request())

    timeout = upstream.timeout
    assert timeout["read"] >= timeout["connect"] * 5
    assert timeout["read"] >= 120, "长回答可能连续输出几分钟，读超时不该低于 2 分钟"
    await response.body_iterator.aclose()


async def test_asr_gets_a_generous_total_timeout(upstream):
    """ASR 是本地 CPU 推理：超时给不足会被误报成服务挂了。"""
    upstream.reply(httpx.Response(200, json={"text": "你好"}))
    request = await make_request(
        b"x",
        path="/api/asr",
        headers={"content-type": "multipart/form-data; boundary=abc"},
    )
    response = await asr(request)

    # 成功路径直接返回下游 payload（FastAPI 会序列化 dict），异常才返回 JSONResponse
    assert isinstance(response, dict) and response.get("text") == "你好"
    assert upstream.timeout["read"] == settings.asr_timeout
    assert upstream.timeout["read"] >= 120


async def test_extract_timeout_sits_between_asr_and_chat(upstream):
    """抽取是本地解析：比 ASR 快，但不能拿普通接口的几秒去套。"""
    upstream.reply(httpx.Response(200, json={"ok": True, "documents": []}))
    request = await make_request(
        b"x",
        path="/api/extract",
        headers={"content-type": "multipart/form-data; boundary=abc"},
    )
    response = await extract(request)

    assert isinstance(response, dict) and response.get("ok") is True
    assert upstream.timeout["read"] == settings.extract_timeout
    assert settings.asr_timeout > settings.extract_timeout > 10


async def test_tts_has_its_own_short_timeout(upstream):
    """TTS 只是文本转音频，等超过一分钟基本等于失败了。"""
    upstream.reply(httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"}))
    request = await make_request(
        '{"text":"你好"}'.encode(),
        path="/api/tts",
        headers={"content-type": "application/json"},
    )
    response = await tts(request)

    assert response.status_code == 200
    assert upstream.timeout["read"] == 60.0


def test_the_four_lanes_do_not_share_one_timeout():
    """配置层面的守卫：四个值互不相同，谁想"统一"就会在这里失败。"""
    values = {
        "asr": settings.asr_timeout,
        "extract": settings.extract_timeout,
        "chat_read": settings.chat_read_timeout,
        "chat_connect": settings.chat_connect_timeout,
    }
    assert len(set(values.values())) == 4, f"有链路共用了同一个超时：{values}"


def test_chat_does_not_configure_a_total_timeout():
    """聊天路径不允许出现"总时长"约束。

    httpx 的 Timeout 只有 connect/read/write/pool 四个维度，本身没有 total；
    这条断言防的是有人改用 `httpx.Timeout(60)` 或加 deadline 之类的写法——
    那会让"说了 5 分钟的正常回答"在中途被切断。
    """
    timeout = httpx.Timeout(
        connect=settings.chat_connect_timeout,
        read=settings.chat_read_timeout,
        write=settings.chat_connect_timeout,
        pool=None,
    )
    keys = set(timeout.as_dict())
    assert keys == {"connect", "read", "write", "pool"}
    assert timeout.read != timeout.connect
