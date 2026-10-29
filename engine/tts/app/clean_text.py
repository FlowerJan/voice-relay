"""文本预处理：把模型输出的 Markdown 变成"适合朗读的纯文本"。

**为什么必须做：** 回答里常带代码块、表格、链接。直接丢给 TTS，
用户会听到"反引号反引号反引号 python 换行 import os..."——查技术问题时会被念疯。

处理顺序有讲究：先整段删掉代码块（整块都是噪音），再处理行内元素。
"""
import re

# 1) 围栏代码块 ```...``` —— 整块丢弃（读代码没有意义）
_RE_FENCE = re.compile(r"```.*?```", re.S)
# 2) 行内代码 `x` —— 保留内容，去掉反引号
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
# 3) 图片 ![alt](url) —— 整个丢弃
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 4) 链接 [text](url) —— 只留文字
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# 5) 标题 #、引用 >、列表 -/*/+ —— 去掉行首标记
_RE_LINE_MARK = re.compile(r"^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)", re.M)
# 6) 强调 **x** / *x* / __x__ / _x_ / ~~x~~
_RE_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(.+?)\1")
# 7) 表格分隔行 |---|---|
_RE_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$", re.M)
# 8) 表格竖线 —— 换成逗号，读起来像正常停顿
_RE_TABLE_PIPE = re.compile(r"\s*\|\s*")
# 9) 多余空行
_RE_BLANK = re.compile(r"\n{3,}")
# 10) HTML 标签（模型偶尔吐 <br> 之类）
_RE_TAG = re.compile(r"<[^>]+>")


def to_speech_text(text: str, max_chars: int = 2000) -> str:
    """把 Markdown 清洗成朗读稿。返回的文本可能被截断，调用方可自行决定是否提示。"""
    if not text:
        return ""

    s = text
    s = _RE_FENCE.sub("（代码块略过）", s)
    s = _RE_IMAGE.sub("", s)
    s = _RE_LINK.sub(r"\1", s)
    s = _RE_TAG.sub("", s)
    s = _RE_TABLE_SEP.sub("", s)
    s = _RE_TABLE_PIPE.sub("，", s)
    s = _RE_LINE_MARK.sub("", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    s = _RE_EMPHASIS.sub(r"\2", s)
    s = _RE_BLANK.sub("\n\n", s)
    s = s.strip()

    # 顺序很重要：先把换行变成句号，再压标点。
    # 反过来的话"看这段代码：\n\n（代码块略过）"里的 "：" 后面还不是句号，压不掉。
    s = re.sub(r"\n+", "。", s)
    s = re.sub(r"。。+", "。", s)

    # 连续标点压一遍（表格竖线换逗号后容易出"， ，"）
    s = re.sub(r"([，。！？；：、])\s*([，。！？；：、])+", r"\1", s)
    # "：。" / "，。" 这类组合只留句号（代码块占位符前面常见）
    s = re.sub(r"[：:，,、]\s*[。！？]", lambda m: m.group(0)[-1], s)
    # 占位符后面紧跟的逗号/句号重复，收成一个
    s = re.sub(r"（代码块略过）[，,、。]+", "（代码块略过）。", s)
    s = re.sub(r"[：:]\s*（代码块略过）", "。（代码块略过）", s)
    s = re.sub(r"。。+", "。", s)

    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "……"
    return s.strip()
