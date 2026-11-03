"""解析超时：体积有上限，时长没有——后者才是用户真正会等到的那个。

20MB 是体积闸，但体积和耗时并不成正比：扫描件 PDF 几乎没有文本层，解析器
仍要逐页过页面结构；排版畸形的文档还可能让某一步卡住。没有时长上限时，
用户看到的是"上传后一直转圈"，而 relay 只能等满自己的预算再报一句
"解析超时"——那句话看不出是文件的问题还是服务的问题。

所以超时放在**这一层**：这里知道文件名、知道文件多大，能把原因说清楚。
"""
import asyncio
import io
import json
import re
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.config import settings
from app.routers import extract as extract_router
from app.services import extract_service
from app.services.extract_service import ExtractResult

pytestmark = pytest.mark.anyio


def _upload(data: bytes = b"%PDF-1.4 fake", name: str = "a.pdf") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


async def test_slow_parse_gives_a_reason_instead_of_spinning(monkeypatch):
    """超时必须变成一句可执行的话，不能只是"没返回"。"""

    def slow(filename: str, data: bytes) -> ExtractResult:
        import time

        time.sleep(0.5)
        return ExtractResult(name=filename, kind="pdf", text="", chars=0)

    monkeypatch.setattr(extract_service, "extract", slow)
    monkeypatch.setattr(settings, "extract_timeout", 0.05)

    response = await extract_router.extract(_upload())

    assert response.status_code == 504
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert "秒" in payload["message"], payload
    assert "拆" in payload["message"], "要告诉用户下一步做什么，不能只说超时了"


async def test_fast_parse_is_untouched(monkeypatch):
    """加了超时不能顺手把正常路径改坏。"""
    monkeypatch.setattr(
        extract_service,
        "extract",
        lambda filename, data: ExtractResult(name=filename, kind="pdf", text="正文", chars=2),
    )

    payload = await extract_router.extract(_upload())

    assert payload["ok"] is True
    assert payload["text"] == "正文"


async def test_timeout_does_not_block_other_requests(monkeypatch):
    """超时只是"这一单不等待了"，事件循环必须立刻空出来。

    `to_thread` 保证解析不堵事件循环，`wait_for` 保证不会无尽等待；
    这条测的是两者合起来的效果：一个卡住的解析不该冻住整个进程。
    """

    def very_slow(filename: str, data: bytes) -> ExtractResult:
        import time

        time.sleep(1.0)
        return ExtractResult(name=filename, kind="pdf", text="", chars=0)

    monkeypatch.setattr(extract_service, "extract", very_slow)
    monkeypatch.setattr(settings, "extract_timeout", 0.05)

    finished = False

    async def ticker() -> None:
        nonlocal finished
        await asyncio.sleep(0.01)
        finished = True

    response, _ = await asyncio.gather(extract_router.extract(_upload()), ticker())

    assert response.status_code == 504
    assert finished, "解析还没结束，事件循环里的其他任务就应当继续跑"


def test_engine_timeout_leaves_room_for_the_gateway():
    """引擎的超时必须短于网关的，否则那句原因永远送不到用户面前。

    网关（relay）在 90 秒放弃并回一句"文件解析超时"；如果这一层也设 90 或更久，
    用户只会看到网关那句——分不清是文件太复杂还是服务挂了。
    """
    relay_config = Path(__file__).resolve().parents[3] / "relay" / "app" / "config.py"
    match = re.search(r"extract_timeout:\s*float\s*=\s*([\d.]+)", relay_config.read_text(encoding="utf-8"))
    assert match, "relay 的 extract_timeout 找不到了，这条跨服务的约束得重新对一遍"

    assert settings.extract_timeout < float(match.group(1))
