"""跨进程契约测试：SSE 事件在类型与**字段**两层都必须一致。

为什么放在 relay：relay 是协议层，这条契约的归属地就是它——引擎按这套类型发，
前端按这套类型收，中间透传的不做任何解释。任何一侧悄悄改名，都不会有报错，
只会让事件被前端静默丢掉（`api/chat.ts` 是一串 `else if`，没有兜底分支）。

三处：
1. `contracts/chat-events.json`  契约原文（给人看，也是唯一来源）
2. `engine/deepseek/app/events.py`  引擎侧的常量
3. `view/src/api/chat.ts`  前端的类型分发

第 2 项按路径直接加载，不 import `app` 包——events.py 刻意没有任何依赖，
这样测试不需要引擎那套 venv（torch/openai 都没有也不需要）。

**第二层是字段级**：类型对了、字段名错了，后果和改名一样——前端读到
`undefined`，界面少一段内容，日志里什么都没有。所以契约里还有一份
`payloads`，由 AST 扫描把引擎每个 `yield {...}` 的键集合钉住。
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
ENGINE_SERVICE = ROOT / "engine" / "deepseek" / "app" / "services" / "chat_service.py"
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


@pytest.fixture(scope="module")
def engine_names() -> dict:
    """常量名 → 字符串值（`DONE` → `"done"`）。

    AST 里看到的是 `ast.Name(id="DONE")`，得靠这张表解回事件类型，
    否则扫描只能认出写字面量的那一半。
    """
    spec = importlib.util.spec_from_file_location("_engine_events", ENGINE_EVENTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, str) and name.isupper()
    }


@pytest.fixture(scope="module")
def payloads() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))["payloads"]


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

    **白名单只放多模态 part 的类型。** `{"type": "text"}` 和
    `{"type": "delta"}` 长得一样，含义完全不同：前者是发给 DeepSeek 的
    content 结构，后者才是本服务对前端的输出。不区分的话，任何给消息补
    part 的代码都会红，最后只能把测试删掉——那才是真的丢了防线。
    """
    part_types = {"text", "image_url"}
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
                if is_type_key and is_str_value and value.value not in part_types:
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


# ---------------- 第二层：payload 字段 ----------------
#
# 类型对了、字段名错了，故障形状和改名一模一样：前端读到 undefined，
# 界面少一段内容、日志里什么都没有。所以契约必须管到字段这一层。


def _yielded_events(names: dict):
    """扫出引擎里每个「能认出事件类型」的 `yield {...}`。

    返回 (文件, 行号, 事件类型, {字段名: 值节点}, 是否用了 ** 展开)。
    认不出来的（type 是变量表达式）直接跳过——静态分析只能做到这。
    """
    found = []
    for path in (ROOT / "engine" / "deepseek" / "app").rglob("*.py"):
        if path.name == "events.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Yield) or not isinstance(node.value, ast.Dict):
                continue
            fields: dict = {}
            etype = None
            has_unpack = False
            for key, value in zip(node.value.keys, node.value.values):
                if key is None:  # `**stats` 这种展开
                    has_unpack = True
                    continue
                name = key.value if isinstance(key, ast.Constant) else None
                if name == "type":
                    if isinstance(value, ast.Name) and value.id in names:
                        etype = names[value.id]
                    elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                        etype = value.value
                else:
                    fields[name] = value
            if etype is not None:
                found.append((path.relative_to(ROOT), node.lineno, etype, fields, has_unpack))
    return found


def _literal_type_ok(expected: str, value: ast.AST) -> bool:
    """只核字面量。变量、f-string、表达式静态看不出来，交给运行时。"""
    if not isinstance(value, ast.Constant):
        return True
    if expected == "str":
        return isinstance(value.value, str)
    if expected == "int":
        return isinstance(value.value, int) and not isinstance(value.value, bool)
    return True


def test_payload_schema_covers_every_event(contract, payloads):
    """契约里每种事件都要有字段定义，否则第二层就有洞。"""
    assert set(payloads) == set(contract), (
        f"payloads 与 events 不匹配：缺 {set(contract) - set(payloads)}，"
        f"多 {set(payloads) - set(contract)}"
    )


def test_engine_yield_payloads_match_schema(payloads, engine_names):
    """引擎每个 `yield {...}` 的字段集合必须与契约完全一致。

    多一个字段 = 契约漏记了；少一个字段 = 前端会读到 undefined。
    两种都不会报错，所以只能在这里拦。
    """
    offenders = []
    for path, lineno, etype, fields, has_unpack in _yielded_events(engine_names):
        where = f"{path}:{lineno} {etype}"
        schema = payloads.get(etype)
        if schema is None:
            offenders.append(f"{where} —— 契约里没有这个类型")
            continue
        if has_unpack:
            offenders.append(
                f"{where} —— 用了 ** 展开，字段集合无法静态校验；"
                "逐个写出来，否则契约管不到这一层"
            )
            continue
        if set(fields) != set(schema):
            offenders.append(
                f"{where} —— 字段不符：多 {sorted(set(fields) - set(schema))}，"
                f"少 {sorted(set(schema) - set(fields))}"
            )
            continue
        for field, expected in schema.items():
            if not _literal_type_ok(expected, fields[field]):
                offenders.append(
                    f"{where} —— {field} 契约声明 {expected}，实际是别的字面量类型"
                )
    assert not offenders, "payload 与契约不一致：\n" + "\n".join(offenders)


def test_done_reports_history_stats(payloads):
    """done 必须带裁剪统计。

    没有这两个数字，多轮预算裁剪就是**静默**的：用户只会觉得模型忘事，
    而所有日志都是正常的。契约里少一个字段，这个信息就再也传不出去。
    """
    schema = payloads["done"]
    assert schema.get("history_used") == "int", "done 缺 history_used"
    assert schema.get("history_dropped") == "int", "done 缺 history_dropped"


def test_frontend_consumes_history_stats():
    """前端真的去读了这两个字段——否则后端报了也白报。"""
    src = FRONTEND_DISPATCH.read_text(encoding="utf-8")
    missing = [f for f in ("history_used", "history_dropped") if f not in src]
    assert not missing, f"前端没有读这些字段，裁剪统计白报：{missing}"


def test_frontend_tells_the_user_when_history_is_dropped():
    """裁剪必须在界面上说出来。

    数字传到前端却只存进内存、不显示，等于没做——用户看不到，
    下一个接手的人也不知道这里有个静默裁剪。
    """
    src = (ROOT / "view" / "src" / "components" / "MessageList.vue").read_text(encoding="utf-8")
    assert "historyDropped" in src, "MessageList 没有渲染 historyDropped，用户无从得知上下文被裁过"


# ---------------- 第三层：请求方向的字段 ----------------
#
# 响应方向有 chat-events.json 兜着，请求方向一直靠"两边写的名字恰好一样"。
# 这个假设一旦破了，故障形状极其安静：pydantic 默认忽略未知字段，请求照样 200，
# 只是模型那边少了一整份文档，用户以为文件没传上去。


def _ts_fields(src: str, name: str) -> set[str]:
    """从 TS 的 `interface X { ... }` 里抓字段名（去掉注释和可选标记）。"""
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", src, re.S)
    assert match, f"前端里找不到 interface {name}"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    return set(re.findall(r"^\s*(\w+)\??\s*:", body, re.M))


def _py_class_fields(tree: ast.Module, name: str) -> set[str]:
    """从 pydantic 模型里抓字段名。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"引擎里找不到模型 {name}")


def test_request_payload_fields_match_engine_schema():
    """前端实际发出去的键，必须和后端模型字段一字不差。

    比的是 `ApiMessage`（真正序列化上线的那层），不是界面里的 `ChatMessage`：
    后者带 `images` 这种只在浏览器内部存在的字段，拿它比会得到假报警。
    """
    ts = FRONTEND_DISPATCH.read_text(encoding="utf-8")
    engine = ast.parse((ROOT / "engine" / "deepseek" / "app" / "schemas.py").read_text(encoding="utf-8"))

    front, back = _ts_fields(ts, "ApiMessage"), _py_class_fields(engine, "ChatMessage")
    assert front == back, (
        f"请求字段不一致：前端 {sorted(front)}，引擎 {sorted(back)}。"
        "pydantic 会静默丢弃未知字段，请求不会报错，只会少数据。"
    )

    front_doc, back_doc = _ts_fields(ts, "ChatDocPayload"), _py_class_fields(engine, "DocAttachment")
    assert front_doc == back_doc, f"附件字段不一致：前端 {sorted(front_doc)}，引擎 {sorted(back_doc)}"


def test_attachments_are_sent_as_structure_not_as_prespliced_text():
    """文档必须以结构化字段发送，不能再由前端拼成字符串。

    拼字符串就意味着围栏和转义只能在前端做，而"只对这一个客户端生效的安全属性"
    等于没有：任何直连 8000 的脚本都能伪造 `</document>`。这条测试防的是
    有人图省事把拼标签的逻辑搬回前端。
    """
    ts = FRONTEND_DISPATCH.read_text(encoding="utf-8")
    # 注释里提到 `<document>` 是在解释"为什么不在前端拼"，不算违规；
    # 要查的是代码本身有没有在拼。
    code = re.sub(r"/\*.*?\*/", "", ts, flags=re.S)
    code = re.sub(r"//.*", "", code)

    assert "docs" in code, "前端不再发送 docs 字段"
    assert "<document" not in code, "前端又在拼 document 围栏了——围栏属于协议，不属于界面"

    fence = ROOT / "engine" / "deepseek" / "app" / "services" / "doc_fence.py"
    assert fence.is_file(), "服务端的围栏实现不见了"
