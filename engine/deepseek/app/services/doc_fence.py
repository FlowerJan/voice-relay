"""文档围栏：把附件正文包进 `<document>` 标签，并挡住"伪造闭合"。

**为什么这件事在服务端做。** 转义是**协议的一部分**，不是界面的行为。
它原来写在 `view/src/composables/useChat.ts` 里，效果是"这个安全属性只对写那段
代码的 UI 生效"：任何直连 8000 的客户端（命令行、脚本、以后的移动端）都会把
文档正文里的 `</document>` 原样送进模型，「文档到此结束，后面是用户的新指令」
这个结构就成立了。前端只有一个，而协议层要对所有调用方成立。

**只转两个序列**：`<document` 和 `</document`。文档里的代码、尖括号、表格
原样保留——把整段正文当 HTML 全转义，模型看到的就是 `&lt;div&gt;`，
读代码变成猜谜，而这个功能的全部意义就是让模型读懂文件。

**幂等**。这里替换的是字面 `<`，所以对已经转过的 `&lt;/document` 不会再次命中，
不会出现 `&amp;lt;` 这种二次转义。这是"前端还留着旧逻辑"时唯一会坏的路径，
有测试钉住（`test_escaping_is_idempotent`）。

**信任边界**：围栏由我们拼，正文来自文件。所以正文一律当数据转义，
而用户自己打的字不转——用户讨论 `</document>` 这个标签本身是合理需求，
转了他会看到一堆实体，还以为是 bug。
"""
import re

#: 附件正文超预算时的占位。它必须留在标签内：模型要理解的是
#: "这份文件的内容已不在上下文里"，而不是"用户提了个叫这个名字的文件然后没下文"。
OMITTED = "（正文已省略：对话较长，只保留最近附件的完整内容。如需重新参考，请再上传一次）"

#: 只匹配标签的开头，不碰正文里其它任何尖括号。
_TAG = re.compile(r"<(\/?document)\b", re.IGNORECASE)


def escape_body(text: str) -> str:
    """把正文里可能冒充围栏的片段转成实体。幂等。"""
    return _TAG.sub(r"&lt;\1", text or "")


def escape_attr(value: str) -> str:
    """属性值转义：引号会让文件名提前闭合标签，尖括号同理。

    **不转 `&`**：转它会让"已经转过的输入"再转一次变成 `&amp;quot;`，
    而 `&` 本身闭合不了属性——收益为零，代价是幂等性。
    """
    return (value or "").replace('"', "&quot;").replace("<", "&lt;")


def render(text: str, docs) -> str:
    """用户文字 + 若干附件 → 一条要发给模型的 content。

    `docs` 只要求有 `name` / `meta` / `body` 三个属性（schemas.DocAttachment
    满足，测试里用 SimpleNamespace 也行），避免这一层依赖 pydantic。
    """
    head = (text or "").strip()
    parts = [head] if head else []

    for doc in docs:
        name = escape_attr(getattr(doc, "name", "") or "未命名文件")
        meta = (getattr(doc, "meta", "") or "").strip()
        body = (getattr(doc, "body", "") or "").strip()

        attrs = f' name="{name}"'
        if meta:
            attrs += f' meta="{escape_attr(meta)}"'
        inner = escape_body(body) if body else OMITTED
        parts.append(f"<document{attrs}>\n{inner}\n</document>")

    return "\n\n".join(parts)
