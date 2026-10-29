"""文档抽取：把 PDF / Word / Excel / 纯文本转成模型能直接读的纯字符串。

**为什么放在模型层（8000），而不是协议层（9000）**
- 解析需要重依赖：pymupdf 自带 onnxruntime（几十 MB）、python-docx 需要 lxml。
  协议层的 venv 目前只有 fastapi + httpx，保持这个干净很重要——它是唯一对外的一层。
- 它本质是"把非文本输入变成文本"，和前端压图片是同一类工作：模型前的预处理。

**为什么不在前端做**
前端不是不能解 docx（zip + XML），但 PDF 的中文字库映射、双栏阅读顺序，
浏览器端库的质量差一代以上。抽错了比抽不出来更糟：模型会基于错乱文本一本正经地回答。

**能力边界要说清楚**
- 能抽文字型 PDF；**扫描版 PDF（图片页）抽不出字**，会返回 scanned=true，由前端提示改发截图。
- .doc（97-2003 老格式）不支持，只能 .docx。
- .xls 老格式不支持，只能 .xlsx。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

# 单次抽取返回给前端的最大字符数。
# 定 20000 的理由：中文约 1.5 万 token，塞进 deepseek-flash 的上下文够用，
# 再长的话每轮都带全文，费用和首字节延迟都不划算。超出部分明确告知"已截断"。
MAX_CHARS = 20000

# 防御性上限：防止一个 500 页的 PDF 或十万行表格把进程拖死
MAX_PAGES = 300
MAX_SHEET_ROWS = 3000

# 判断扫描件的阈值：平均每页少于这么多可见字符，基本就是图片页
SCANNED_CHARS_PER_PAGE = 20

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".ini", ".toml", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh",
    ".css", ".scss", ".less",
}

SUPPORTED_HINT = "支持 PDF(.pdf)、Word(.docx)、Excel(.xlsx/.xlsm)、CSV 与常见纯文本"


class ExtractError(Exception):
    """抽取失败。message 直接给用户看，不要带堆栈信息。"""


@dataclass
class ExtractResult:
    name: str
    kind: str
    text: str
    chars: int
    truncated: bool = False
    pages: int = 0
    scanned: bool = False
    note: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "ok": True,
            "name": self.name,
            "kind": self.kind,
            "text": self.text,
            "chars": self.chars,
            "truncated": self.truncated,
            "pages": self.pages,
            "scanned": self.scanned,
            "note": self.note,
        }
        if self.extra:
            d.update(self.extra)
        return d


def _clean(text: str) -> str:
    """压掉多余空行和行尾空白。

    抽取出来的文本常有三个以上连续空行（PDF 换页、Word 空段落），
    原样发给模型纯属浪费 token。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cut(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS], True


# ---------------------------------------------------------------- PDF

def _extract_pdf(data: bytes) -> tuple[str, int, bool]:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"PDF 打不开（可能是加密文件或已损坏）：{str(e)[:120]}") from e

    try:
        if doc.needs_pass:
            # 加密 PDF 会静默返回空文本，不主动检查就会误报"扫描件"
            raise ExtractError("这个 PDF 有打开密码，请先解密再上传")
        pages = min(doc.page_count, MAX_PAGES)
        parts: list[str] = []
        total = 0
        for i in range(pages):
            # sort=True 让 pymupdf 按版面坐标重排，双栏论文不排会串行
            t = doc[i].get_text("text", sort=True)
            if t:
                parts.append(t)
                total += len(t)
            if total > MAX_CHARS * 2:
                break  # 已经远超截断线，没必要继续解后面的页
        return "\n".join(parts), doc.page_count, doc.page_count > pages
    finally:
        doc.close()


# ---------------------------------------------------------------- Word

def _iter_docx_blocks(doc):
    """按文档顺序交替产出段落与表格。

    不能简单地 `for p in doc.paragraphs` 再 `for t in doc.tables`：
    那样会把所有表格挪到文末，上下文顺序全乱，模型读到的是"正文说完再列一堆表"。
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield "p", Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield "tbl", Table(child, doc)


def _extract_docx(data: bytes) -> str:
    import docx

    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise ExtractError(
            f"Word 打不开：{str(e)[:120]}。只支持 .docx，老版 .doc 请先在 Word 里另存为 .docx"
        ) from e

    out: list[str] = []
    for tag, block in _iter_docx_blocks(doc):
        if tag == "p":
            t = block.text.strip()
            if not t:
                continue
            # 标题层级保留成 Markdown，模型的排版感会好很多
            style = (block.style.name or "").lower()
            if style.startswith("heading") or style.startswith("标题"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                out.append(f"{'#' * min(int(level), 6)} {t}")
            else:
                out.append(t)
        else:
            rows = []
            for row in block.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                out.append("\n".join(rows))
        if sum(len(x) for x in out) > MAX_CHARS * 2:
            break
    return "\n\n".join(out)


# ---------------------------------------------------------------- Excel

def _extract_xlsx(data: bytes) -> tuple[str, int]:
    import openpyxl

    try:
        # read_only + data_only：只读流式，且取公式的计算结果而不是 "=A1+B1"
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ExtractError(
            f"Excel 打不开：{str(e)[:120]}。只支持 .xlsx/.xlsm，老版 .xls 请另存为 .xlsx"
        ) from e

    try:
        chunks: list[str] = []
        sheets = 0
        for ws in wb.worksheets:
            sheets += 1
            lines: list[str] = [f"## 工作表：{ws.title}"]
            used = 0
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    lines.append(f"（本表超过 {MAX_SHEET_ROWS} 行，已省略其余部分）")
                    break
                cells = ["" if v is None else str(v) for v in row]
                # 整行空的情况很常见（格式残留），跳过避免大量空行
                if not any(c.strip() for c in cells):
                    continue
                line = "\t".join(cells).rstrip("\t")
                lines.append(line)
                used += len(line)
                if used > MAX_CHARS:
                    lines.append("（本表内容较长，已截断）")
                    break
            chunks.append("\n".join(lines))
            if sum(len(c) for c in chunks) > MAX_CHARS * 2:
                break
        return "\n\n".join(chunks), sheets
    finally:
        wb.close()


# ---------------------------------------------------------------- 纯文本

def _extract_text(data: bytes) -> str:
    # 顺序有讲究：utf-8-sig 失败才试 gbk。
    # 国内用户导出的 CSV 大量是 GBK，直接用 utf-8 会整篇乱码而不是报错。
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ExtractError("文本编码识别失败（试过 UTF-8 和 GBK），请另存为 UTF-8 再上传")


# ---------------------------------------------------------------- 入口

def extract(filename: str, data: bytes) -> ExtractResult:
    """按后缀分发。抛 ExtractError 表示不可恢复的错误（前端直接展示 message）。"""
    if not data:
        raise ExtractError("文件是空的")

    name = Path(filename or "未命名").name
    suffix = Path(name).suffix.lower()

    kind = ""
    pages = 0
    scanned = False
    note = ""
    raw = ""

    if suffix == ".pdf":
        kind = "pdf"
        raw, pages, page_capped = _extract_pdf(data)
        if page_capped:
            note = f"文档共 {pages} 页，只读了前 {MAX_PAGES} 页"
        # 每页平均字符数太少 → 说明大部分页面是扫描图片。
        # 注意这里**不抛错**：能抽到字说明至少部分可用，让用户自己判断要不要补截图。
        # 完全抽不到字的走下面的 `if not text`，那条路径才是真扫描件。
        plain = _clean(raw)
        if pages and len(plain) < pages * SCANNED_CHARS_PER_PAGE:
            scanned = True
            note = (
                f"这份 PDF 共 {pages} 页，只抽到 {len(plain)} 字，大部分页面应该是扫描图片，"
                "内容可能不完整。缺的部分截图发我，我可以直接看图。"
            )
    elif suffix == ".docx":
        kind = "docx"
        raw = _extract_docx(data)
    elif suffix in (".xlsx", ".xlsm"):
        kind = "xlsx"
        raw, sheets = _extract_xlsx(data)
        note = f"共 {sheets} 个工作表"
    elif suffix in (".doc", ".xls", ".ppt", ".pptx"):
        raise ExtractError(
            f"暂不支持 {suffix}，请另存为 .docx / .xlsx 再上传"
            + ("（PPT 建议直接截图发我）" if suffix in (".ppt", ".pptx") else "")
        )
    elif suffix in TEXT_SUFFIXES or suffix == "":
        kind = "text"
        raw = _extract_text(data)
    else:
        raise ExtractError(f"不认识的文件类型 {suffix or '（无后缀）'}。{SUPPORTED_HINT}")

    text = _clean(raw)
    if not text:
        if kind == "pdf":
            raise ExtractError("这个 PDF 抽不出任何文字，应该是扫描件。把关键页面截图发我可以直接看图")
        raise ExtractError("文件里没有可读的文字内容")

    text, truncated = _cut(text)
    if truncated:
        note = (note + "；" if note else "") + f"内容较长，只取了前 {MAX_CHARS} 字"

    return ExtractResult(
        name=name,
        kind=kind,
        text=text,
        chars=len(text),
        truncated=truncated,
        pages=pages,
        scanned=scanned,
        note=note,
    )
