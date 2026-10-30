"""抽取服务的回归测试：造真实文件，走真实解析库。

跑法（在 engine/deepseek 目录下）：
    venv/Scripts/python.exe _extract_test.py

不依赖网络、不依赖任何服务，纯函数级验证。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.extract_service import (  # noqa: E402
    MAX_CHARS,
    ExtractError,
    extract,
)

pass_n = 0
fail_n = 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print(f"  [ok]   {name}")
    else:
        fail_n += 1
        print(f"  [FAIL] {name} {extra}")


def expect_error(name: str, fn, keyword: str = "") -> None:
    try:
        fn()
    except ExtractError as e:
        ok(name, keyword in str(e), f"实际提示={str(e)[:80]}")
    except Exception as e:  # noqa: BLE001
        ok(name, False, f"抛了非 ExtractError: {type(e).__name__}: {e}")
    else:
        ok(name, False, "没有抛异常")


# ---------------------------------------------------------------- 造样本

def make_pdf(pages: int = 2, text: str = "季度营收报告") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 100), f"Page {i + 1} heading", fontsize=14)
        # china-s 是 pymupdf 内置的简体中文字体，不需要外部字体文件
        p.insert_text((72, 140), text, fontsize=12, fontname="china-s")
        p.insert_text((72, 180), "Revenue 1234 ten thousand", fontsize=12)
    buf = doc.tobytes()
    doc.close()
    return buf


def make_scanned_pdf(pages: int = 2) -> bytes:
    """只有图形、没有文字的 PDF——模拟扫描件。"""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        p = doc.new_page()
        p.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    buf = doc.tobytes()
    doc.close()
    return buf


def make_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("项目总结", level=1)
    d.add_paragraph("第一段正文，讲背景。")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "指标"
    t.cell(0, 1).text = "数值"
    t.cell(1, 0).text = "营收"
    t.cell(1, 1).text = "1234"
    d.add_paragraph("表格之后的正文，用来验证顺序。")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总"
    ws.append(["月份", "营收", "成本"])
    ws.append(["一月", 100, 60])
    ws.append(["二月", 120, None])
    ws2 = wb.create_sheet("明细")
    ws2.append(["备注"])
    ws2.append(["第二张表"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 用例

print("\n[PDF]")
r = extract("report.pdf", make_pdf())
ok("能抽到英文正文", "Page 1 heading" in r.text, r.text[:50])
ok("能抽到中文正文", "季度营收报告" in r.text, repr(r.text[:80]))
ok("page_count 正确", r.pages == 2, f"实际={r.pages}")
ok("未截断", r.truncated is False)
ok("kind=pdf", r.kind == "pdf")
ok("不是扫描件", r.scanned is False)

print("\n[扫描版 PDF]")
# 一个字都抽不到：直接报错，文案里要给出可行的替代方案（截图）
expect_error("纯扫描件给出截图建议", lambda: extract("scan.pdf", make_scanned_pdf()), "截图")

# 混合型：能抽到一点字，但不能当完整文档用 → 成功返回 + scanned 标记
def make_mixed_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 100), "has text", fontsize=12)
    for _ in range(5):
        q = doc.new_page()
        q.draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(0.8, 0.8, 0.8))
    buf = doc.tobytes()
    doc.close()
    return buf


r = extract("mixed.pdf", make_mixed_pdf())
ok("混合型标记 scanned=true", r.scanned is True, f"note={r.note}")
ok("混合型仍返回已抽到的文字", "has text" in r.text, r.text[:50])
ok("提示可能不完整", "不完整" in r.note, r.note)

print("\n[Word]")
r = extract("a.docx", make_docx())
ok("标题转成 Markdown", "# 项目总结" in r.text, r.text[:60])
ok("正文在", "第一段正文" in r.text)
ok("表格转成竖线分隔", "营收 | 1234" in r.text, r.text)
# 关键：表格必须夹在两段正文中间，而不是被挪到文末
i1 = r.text.find("第一段正文")
i2 = r.text.find("营收 | 1234")
i3 = r.text.find("表格之后的正文")
ok("段落与表格保持原顺序", i1 < i2 < i3, f"位置 {i1} {i2} {i3}")

print("\n[Excel]")
r = extract("b.xlsx", make_xlsx())
ok("包含两张表", "工作表：汇总" in r.text and "工作表：明细" in r.text, r.text[:80])
ok("单元格用 tab 分隔", "\t".join(["一月", "100", "60"]) in r.text, r.text)
ok("空单元格不报错", "二月" in r.text)
ok("note 说明表数", "2 个工作表" in r.note, r.note)

print("\n[纯文本]")
r = extract("note.md", "# 标题\n\n正文".encode())
ok("UTF-8 直读", "# 标题" in r.text)
r = extract("gbk.txt", "中文 GBK 编码测试".encode("gb18030"))
ok("GBK 自动识别", "GBK 编码测试" in r.text, r.text)
r = extract("bom.csv", "a,b\n1,2".encode("utf-8-sig"))
ok("UTF-8 BOM 不带残留字符", r.text.startswith("a,b"), repr(r.text[:10]))

print("\n[截断]")
big = ("长" * (MAX_CHARS + 500)).encode()
r = extract("big.txt", big)
ok("截到 MAX_CHARS", len(r.text) == MAX_CHARS, f"实际={len(r.text)}")
ok("truncated=true", r.truncated is True)
ok("note 提示截断", "前" in r.note, r.note)

print("\n[异常]")
expect_error("不支持的 .exe", lambda: extract("a.exe", b"MZ\x90\x00"), "不认识")
expect_error("老格式 .doc 给出去向", lambda: extract("a.doc", b"\xd0\xcf\x11\xe0"), ".docx")
expect_error("空文件", lambda: extract("a.txt", b""), "空的")
expect_error("损坏的 PDF", lambda: extract("bad.pdf", b"not a pdf at all"), "打不开")
expect_error("全空白文本", lambda: extract("blank.txt", b"   \n\n  "), "没有可读")

print(f"\n{pass_n} 项通过，{fail_n} 项失败")
sys.exit(1 if fail_n else 0)
