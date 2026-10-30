"""跨进程契约测试：SSE 事件类型在三处必须完全一致。

为什么放在 relay：relay 是协议层，这条契约的归属地就是它——引擎按这套类型发，
前端按这套类型收，中间透传的不做任何解释。任何一侧悄悄改名，都不会有报错，
只会让事件被前端静默丢掉（`api/chat.ts` 是一串 `else if`，没有兜底分支）。

三处：
1. `contracts/chat-events.json`  契约原文（给人看，也是唯一来源）
2. `engine/deepseek/app/events.py`  引擎侧的常量
3. `view/src/api/chat.ts`  前端的类型分发

第 2 项按路径直接加载，不 import `app` 包——events.py 刻意没有任何依赖，
这样测试不需要引擎那套 venv（torch/openai 都没有也不需要）。
"""
import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "chat-events.json"
ENGINE_EVENTS = ROOT / "engine" / "deepseek" / "app" / "events.py"
FRONTEND_DISPATCH = ROOT / "view" / "src" / "api" / "chat.ts"


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))["events"]


@pytest.fixture(scope="module")
def engine_events() -> tuple:
    spec = importlib.util.spec_from_file_location("_engine_events", ENGINE_EVENTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ALL_EVENTS


def test_contract_file_exists():
    assert CONTRACT.is_file(), f"契约文件不见了：{CONTRACT}"


def test_contract_has_events(contract):
    assert contract, "契约里一个事件都没有"


def test_engine_constants_match_contract(contract, engine_events):
    """引擎能发的类型，必须与契约一字不差。"""
    assert set(engine_events) == set(contract), (
        f"引擎常量与契约不一致：引擎多出 {set(engine_events) - set(contract)}，"
        f"契约多出 {set(contract) - set(engine_events)}"
    )


def test_engine_has_no_stray_type_literals():
    """引擎代码里不该再有 `{"type": "xxx"}` 这样的裸字面量。

    有的话说明有人绕过 events.py 直接写了一个新类型——那正是这套契约
    想防的事：能发出去，但契约和前端都不知道。

    用 AST 而不是正则：正则会把文档字符串里举例的 `{"type": "text"}`
    也算成违规，那个例子讲的是多模态 part，跟 SSE 事件不是一回事。
    """
    offenders = []
    for path in (ROOT / "engine" / "deepseek" / "app").rglob("*.py"):
        if path.name == "events.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                is_type_key = isinstance(key, ast.Constant) and key.value == "type"
                is_str_value = isinstance(value, ast.Constant) and isinstance(value.value, str)
                if is_type_key and is_str_value:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "发现硬编码的事件类型字面量：" + ", ".join(offenders)


def test_frontend_handles_every_event(contract):
    """前端必须为契约里的每一种类型写一个分支。"""
    src = FRONTEND_DISPATCH.read_text(encoding="utf-8")
    missing = [t for t in contract if f"data.type === '{t}'" not in src]
    assert not missing, f"前端没有处理这些事件：{missing}（会被静默丢掉）"


def test_frontend_handles_nothing_extra(contract):
    """反过来：前端不该处理契约里没有的类型，否则契约就落后了。"""
    src = FRONTEND_DISPATCH.read_text(encoding="utf-8")
    handled = set(re.findall(r"data\.type === '([^']+)'", src))
    assert handled == set(contract), (
        f"前端处理的事件与契约不一致：多出 {handled - set(contract)}，"
        f"契约多出 {set(contract) - handled}"
    )
