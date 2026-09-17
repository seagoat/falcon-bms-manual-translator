#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py —— 端到端编排层（抽取 / 入库 / 导出 / 新版 PDF 模拟）。

本文件是集成层，严格遵守 `CONTRACT.md`：

    extract_segments(doc, page_no) -> list[core.models.Segment]   # page_no 为 1-based
    ingest_pdf(conn, pdf_path, *, slug=None, title=None, note="", label=None) -> dict
    build_glossary_seed() -> list[dict]
    export_cn_pdf / export_bilingual_pdf 的编排包装

设计要点
--------
1. **延迟 import**：`core.*` / `translate.*` / `render.*` 全部在函数内部 import，
   这样并行开发期本模块可以被 import、被 review、被单测而不炸。
2. **内置最小等价兜底**：只有 `core.fingerprint` / `core.models` 尚不存在时才会启用
   `_Fallback*`（语义与 CONTRACT §2/§4 逐字一致），并在 stderr 打印一次警告。
   core 一旦落地即自动使用真实现，兜底代码不参与主流程。
3. **分段启发式**（Word 生成版式，实测于 401 页真实手册）：
   块 → 视觉行 → 风格 run → 段落。规则见 `_PAR_BREAK_RULES` 注释。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pymupdf

# --------------------------------------------------------------------------------------
# 路径 / 常量
# --------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
ORIGIN_DIR = ROOT / "origin"
DATA_DIR = ROOT / "data"
DERIVED_DIR = DATA_DIR / "derived"

try:  # config/ 由 lead 提供；缺失时退化为本文件的默认值
    import config as _config  # type: ignore
except Exception:  # pragma: no cover
    _config = None  # type: ignore


def data_dir() -> Path:
    if _config is not None:
        try:
            return Path(_config.data_dir())
        except Exception:
            pass
    p = DATA_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def derived_dir(*parts: str) -> Path:
    p = data_dir() / "derived"
    for part in parts:
        p = p / str(part)
    p.mkdir(parents=True, exist_ok=True)
    return p


def origin_dir() -> Path:
    if _config is not None:
        try:
            return Path(_config.origin_dir())
        except Exception:
            pass
    return ORIGIN_DIR


# 版式常量（A4 版面实测：595.32 x 841.92，见 CONTRACT §3.1）
HEADER_BAND_Y = 66.0     # 页眉区：bbox.y1 <= 66（页眉文字白色 Calibri-Bold 12pt）
FOOTER_BAND_Y = 776.0    # 页脚区：bbox.y0 >= 776（页码单独标 pagenum）
BODY_COLUMN_LEFT = 53.9  # 正文左边界（实测）
BODY_COLUMN_RIGHT = 545.0
BOLD_FLAG = 1 << 4       # 16
ITALIC_FLAG = 1 << 1     # 2
WARN_ONCE: set[str] = set()

# 表格识别（Lead + verifier 实测校准：既要修「一行多列被拼成假词」，又不能把正文切碎）
CELL_GAP = 12.0          # 同一基线上 > 该水平间隙才算不同单元格（≈2 个空格宽）
TABLE_MIN_COLS = 3       # 一行至少 3 个 run 才算表格行（2 列多是「标签+说明」版式，不切）
TABLE_MIN_ROWS = 2       # 表格区至少 2 个 baseline
TABLE_MAX_ROWS = 12      # 超过该行数视为「多列排版/术语表」，不做单元格切分（否则碎片爆炸）
CELL_MIN_CHARS = 2       # 单元格文本硬性下限：< 2 字符的单元格并入同行相邻单元格

# 列表 / 项目符号
_BULLET_CHARS = "•▪◦‣·∙●○–—"
LIST_MARKER_RE = re.compile(
    r"^(?:[" + _BULLET_CHARS + r"*\-]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)])\s+\S"
)
HEADING_NUM_RE = re.compile(r"^\(?\d+(?:\.\d+)*[.)]?\s+\S")
CAPTION_RE = re.compile(r"^(?:figure|fig\.?|table|chart|picture|image)\s*[\dA-Z]", re.I)
ROMAN_HEADING_RE = re.compile(r"^\(?[IVXLC]{1,5}[.)]\s+[A-Z]")
TOC_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")
TOC_TAIL_RE = re.compile(r"\.{3,}\s*\d*\s*$")        # 目录行尾部的 dot leader（页码可省）
UNIT_NUM_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(knots?|kts?|feet|foot|ft|psi|percent|%|seconds?|sec|minutes?|min|"
    r"degrees?|deg|miles?|nm|nautical miles?|gallons?|lbs?|pounds?|inches|inch|in\b)",
    re.I,
)
SENT_END_CHARS = ".!?:;。！？；："


# --------------------------------------------------------------------------------------
# core.* 延迟加载 + 兜底实现（core 落地后自动切回真实现）
# --------------------------------------------------------------------------------------

_models_cache: Any = None
_fp_cache: Any = None
_VOCAB: Any = None       # 当前 ingest 的全书语料词表（见 build_document_vocabulary）


class _FallbackModels:
    """CONTRACT §2 的最小等价实现；仅在 core.models 缺失时使用。"""

    import enum as _enum

    class SegmentKind(str, _enum.Enum):
        HEADING = "heading"
        PARAGRAPH = "paragraph"
        LIST_ITEM = "list_item"
        TABLE_CELL = "table_cell"
        CAPTION = "caption"
        HEADER = "header"
        FOOTER = "footer"
        PAGE_NUM = "page_num"
        OTHER = "other"

    class ChangeKind(str, _enum.Enum):
        UNCHANGED = "unchanged"
        MODIFIED = "modified"
        ADDED = "added"
        REMOVED = "removed"
        MOVED = "moved"
        REORDERED = "reordered"
        SPLIT = "split"
        MERGED = "merged"

    class TranslationStatus(str, _enum.Enum):
        MISSING = "missing"
        MACHINE = "machine"
        CARRIED = "carried"
        SEEDED = "seeded"
        REVIEWED = "reviewed"
        FAILED = "failed"

    @dataclass
    class Span:
        text: str
        bbox: list
        origin: list
        size: float
        font: str
        color: int
        flags: int = 0

    @dataclass
    class Segment:
        id: int = 0
        version_id: int = 0
        doc_id: int = 0
        page: int = 1
        order_index: int = 0
        kind: str = "paragraph"
        text: str = ""
        canonical_text: str = ""
        normalized: str = ""
        fingerprint: str = ""
        bbox: list = field(default_factory=list)
        line_boxes: list = field(default_factory=list)
        fonts: list = field(default_factory=list)
        style: dict = field(default_factory=dict)
        role: str = "body"
        content_hash: str = ""


class _FallbackFingerprint:
    """CONTRACT §4/§4.1 的最小等价实现；仅在 core.fingerprint 缺失时使用。"""

    _QUOTES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
               "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
               "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2015": "-", "\u2212": "-",
               "\u00ab": '"', "\u00bb": '"', "\u2032": "'", "\u2033": '"'}
    KNOWN_HYPHEN_PREFIX = {"F", "An", "Dash", "ANTI", "A", "B", "S", "T", "G", "N", "MAL",
                           "ELEC", "NORM", "M", "Q", "TAC", "RT", "IDM", "FLCS", "MFD",
                           "PFL", "VMS"}
    _BULLET_LINE = re.compile(r"^[-" + "\u2022\u25aa\u25c6\u203b\u00b7\u2043\u2045\u25e6*" + r"]$")

    @classmethod
    def normalize(cls, text: str) -> str:
        import unicodedata
        t = unicodedata.normalize("NFKC", text or "")
        for a, b in cls._QUOTES.items():
            t = t.replace(a, b)
        return re.sub(r"\s+", " ", t).strip()

    @classmethod
    def fingerprint(cls, text: str) -> str:
        return hashlib.blake2b(cls.normalize(text).casefold().encode("utf-8"), digest_size=16).hexdigest()

    @classmethod
    def content_hash(cls, text: str) -> str:
        return hashlib.blake2b(cls.normalize(text).encode("utf-8"), digest_size=16).hexdigest()

    @classmethod
    def ratio(cls, a: str, b: str) -> float:
        import difflib
        return difflib.SequenceMatcher(None, cls.normalize(a), cls.normalize(b)).ratio()

    @classmethod
    def dehyphenate(cls, lines: list, vocabulary: Any = None) -> str:
        """保守兜底：只做智能拼接，**不删连字符**（避免用旧弱规则拼坏真实词）。

        正常路径始终走 core.fingerprint.dehyphenate（见 pipeline.dehyphenate_lines）。
        """
        return _smart_join([str(t).strip() for t in (lines or []) if str(t).strip()])

    @classmethod
    def char_diffs(cls, old: str, new: str) -> list[dict]:
        import difflib
        a, b = cls.normalize(old), cls.normalize(new)
        sm = difflib.SequenceMatcher(None, a, b)
        out: list[dict] = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            kind = "equal" if op == "equal" else ("delete" if op == "delete" else "insert")
            if op == "replace":
                out.append({"op": "delete", "text": a[i1:i2]})
                out.append({"op": "insert", "text": b[j1:j2]})
                continue
            seg = a[i1:i2] if kind in ("equal", "delete") else b[j1:j2]
            if out and out[-1]["op"] == kind:
                out[-1]["text"] += seg
            else:
                out.append({"op": kind, "text": seg})
        return out


def models() -> Any:
    """返回 core.models（优先）或内置兜底。"""
    global _models_cache
    if _models_cache is not None:
        return _models_cache
    try:
        from core import models as _m  # type: ignore
        _models_cache = _m
    except ImportError:
        if "models" not in WARN_ONCE:
            WARN_ONCE.add("models")
            print("[pipeline] 警告: core.models 尚未就绪，使用 pipeline 内置兜底模型"
                  "（core 落地后自动切回）", file=sys.stderr)
        _models_cache = _FallbackModels
    return _models_cache


def fingerprint_mod() -> Any:
    """返回 core.fingerprint（优先）或内置兜底。"""
    global _fp_cache
    if _fp_cache is not None:
        return _fp_cache
    try:
        from core import fingerprint as _f  # type: ignore
        _fp_cache = _f
    except ImportError:
        if "fp" not in WARN_ONCE:
            WARN_ONCE.add("fp")
            print("[pipeline] 警告: core.fingerprint 尚未就绪，使用 pipeline 内置兜底实现"
                  "（core 落地后自动切回）", file=sys.stderr)
        _fp_cache = _FallbackFingerprint
    return _fp_cache


def _store() -> Any:
    from core import store  # type: ignore
    return store


def _db() -> Any:
    from core import db  # type: ignore
    return db


# —— 可移植性：入库时把源 PDF 路径存成「相对仓库根」的形式（见 core/paths.py）。
#    这样把仓库整体复制到别的机器/目录后，DB 不需要改就能用。
def _rel_path(p: Any) -> str:
    """绝对路径 → 相对仓库根（POSIX 风格）；在仓库外则原样保留绝对路径。"""
    try:
        from core import paths as _p  # type: ignore
        return _p.to_rel(str(p), root=ROOT)
    except Exception:
        return str(p)


def _abs_path(p: Any) -> str:
    """DB 里存的路径 → 本机绝对路径。"""
    try:
        from core import paths as _p  # type: ignore
        return _p.resolve(str(p))
    except Exception:
        return str(p)


def _render_pagebuild() -> Any:
    from render import pagebuild  # type: ignore
    return pagebuild


def _render_pdfout() -> Any:
    from render import pdfout  # type: ignore
    return pdfout


def connect(path: str | os.PathLike | None = None):
    """打开数据库连接（走 core.db.connect）。"""
    return _db().connect(path)


def normalize(text: str) -> str:
    return fingerprint_mod().normalize(text)


def fp_of(text: str) -> str:
    return fingerprint_mod().fingerprint(text)


def chash_of(text: str) -> str:
    return fingerprint_mod().content_hash(text)


def dehyphenate_lines(lines: list) -> str:
    """段内多行拼接 + 行尾软连字符复原（CONTRACT §4.1 / core.fingerprint.dehyphenate）。

    传入全书语料词表（`build_document_vocabulary()`）后，dehyphenate 能用「词表命中 +
    软换行后缀」双条件判定，实测把 17 处拼坏真实词降到 1 处；不传词表则退化为保守模式
    （保留连字符，会留下 `infor-mation` 这类残差）。
    """
    fn = getattr(fingerprint_mod(), "dehyphenate", None)
    if not callable(fn):
        # core.fingerprint 不可用：**保守拼接**（保留连字符），绝不按旧规则删连字符拼坏词
        if "dehy" not in WARN_ONCE:
            WARN_ONCE.add("dehy")
            print("[pipeline] 警告: 缺少 core.fingerprint.dehyphenate，采用保守拼接"
                  "（保留连字符，不做软连字符复原）", file=sys.stderr)
        return _smart_join([str(t).strip() for t in lines if str(t).strip()])
    if _VOCAB is not None:
        try:
            return fn(list(lines), vocabulary=_VOCAB)
        except TypeError:
            pass
    return fn(list(lines))


def build_document_vocabulary(doc, page_from: int = 1, page_to: Optional[int] = None) -> Any:
    """用全书（或该版本的页区间）**未拼接的行**构建语料词表，供 dehyphenate 使用。

    词表让「软换行连字符」判定从「下一行首字母小写」这种弱信号升级为
    「词表命中 + 软换行后缀」双条件，避免把 `ANTI-`/`F-16` 这类真实复合词拼坏。
    """
    fn = getattr(fingerprint_mod(), "build_vocabulary", None)
    if not callable(fn):
        return None
    hi = page_to or doc.page_count
    lines: list[str] = []
    for pno in range(page_from, min(hi, doc.page_count) + 1):
        try:
            lines.extend(doc[pno - 1].get_text().splitlines())
        except Exception:
            continue
    try:
        return fn([ln.strip() for ln in lines if ln and ln.strip()])
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 警告: build_vocabulary 失败（{exc}），使用保守模式", file=sys.stderr)
        return None


def ratio_of(a: str, b: str) -> float:
    return fingerprint_mod().ratio(a, b)


# --------------------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------------------

def sha256_file(path: str | os.PathLike, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", (name or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "doc"


def release_label_from(meta: dict | None, path: Path, text_hint: str = "") -> str:
    """从 PDF 元数据/文件名/首页文本里取版本号，如 '4.38.1'。"""
    meta = meta or {}
    pat = re.compile(r"\b(\d+\.\d+(?:\.\d+)*)\b")
    for src in (str(meta.get("title") or ""), path.stem, text_hint):
        m = pat.search(src)
        if m:
            return m.group(1)
    return ""


def doc_date_from(meta: dict | None, path: Path) -> str:
    meta = meta or {}
    raw = str(meta.get("creationDate") or meta.get("modDate") or "")
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(20\d{2})[-_ ]?(\d{2})[-_ ]?(\d{2})", path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def int_to_rgb(color: int) -> tuple[float, float, float]:
    return (((color >> 16) & 0xFF) / 255.0, ((color >> 8) & 0xFF) / 255.0, (color & 0xFF) / 255.0)


def page_of(doc, page_no) -> Any:
    """page_no 为 1-based；也允许直接传入 pymupdf.Page。"""
    if isinstance(page_no, pymupdf.Page):
        return page_no
    try:
        n = int(page_no)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"页码必须是整数(1-based)，收到 {page_no!r}") from exc
    if not 1 <= n <= doc.page_count:
        raise ValueError(f"页码 {n} 超出范围 1..{doc.page_count}")
    return doc[n - 1]


# --------------------------------------------------------------------------------------
# 分段：块 → 视觉行 → 风格 run → 段落
# --------------------------------------------------------------------------------------

@dataclass
class _Unit:
    """同一视觉行内、风格一致的一段文字。"""
    text: str
    bbox: tuple
    origin: tuple
    size: float
    font: str
    color: int
    flags: int
    bold: bool
    italic: bool
    vis_line: int = 0
    is_marker: bool = False
    block_idx: int = -1
    table: bool = False


@dataclass
class _Par:
    """内部段落（extract_segments 把它转成 core.models.Segment）。"""
    page: int
    text: str
    bbox: tuple
    line_boxes: list
    size: float
    font: str
    color: int
    flags: int
    bold: bool
    italic: bool
    kind: str
    role: str
    align: str
    indent: float
    list_level: int
    fonts: list
    n_lines: int
    first_origin: tuple
    pitch: float

    def as_dict(self) -> dict:
        return {
            "page": self.page, "text": self.text, "bbox": list(self.bbox),
            "kind": self.kind, "role": self.role, "style": {
                "bold": self.bold, "italic": self.italic, "size": self.size,
                "color": self.color, "align": self.align,
                "list_level": self.list_level, "indent": self.indent,
            },
        }


def _span_bold(sp: dict) -> bool:
    if sp.get("flags", 0) & BOLD_FLAG:
        return True
    f = (sp.get("font") or "").lower()
    return "bold" in f or f.endswith("-bd")


def _span_italic(sp: dict) -> bool:
    if sp.get("flags", 0) & ITALIC_FLAG:
        return True
    f = (sp.get("font") or "").lower()
    return "italic" in f or "oblique" in f


def _spans_of_block(block: dict) -> list[dict]:
    out = []
    for line in block.get("lines", []):
        for sp in line.get("spans", []):
            t = sp.get("text") or ""
            if not t or not t.strip():
                continue
            out.append(sp)
    return out


def _font_family(font: str) -> str:
    """字体族：'Calibri-Bold'→'calibri'，'Cambria-BoldItalic'→'cambria'，'ArialMT'→'arial'。

    用于区分「字族突变的换段」（Cambria 标题 + Calibri 正文）与「行内加粗」（Calibri vs Calibri-Bold）。
    """
    f = (font or "").split("-")[0].lower()
    for suf in ("psmt", "mt"):
        if f.endswith(suf) and len(f) > len(suf) + 2:
            f = f[: -len(suf)]
    return f


_NO_SPACE_BEFORE_RE = re.compile(r"^[.,;:!?%)\]}\"'\u2019\u201d\u00bb]")


def _needs_space(prev_text: str, cur_text: str) -> bool:
    if not prev_text or not cur_text:
        return False
    if prev_text.endswith(" ") or cur_text.startswith(" "):
        return False
    if prev_text.endswith(("(", "\u201c")):
        return False
    # 连字符只在词内断行时不留空格；单个 '-' 是项目符号，后面要留空格
    if len(prev_text.strip()) > 1 and prev_text.endswith(("-", "\u2013", "\u2014", "/")):
        return False
    return not _NO_SPACE_BEFORE_RE.match(cur_text)


def _smart_join(parts: Iterable[str]) -> str:
    out = ""
    for t in parts:
        if not t:
            continue
        if not out:
            out = t
            continue
        out += (" " if _needs_space(out, t) else "") + t
    return out


def _join_line_units(units: list[_Unit]) -> str:
    """同一视觉行内的 unit 拼接（按**几何间隙**决定是否加空格）。

    Word 的小型大写字母（Small Caps）会把一个词拆成多个 span（`E` + `XTERNAL`、
    `L` + `IGHTNING`），间隙只有 0.2–0.3pt。若按文本规则一律加空格，就会得到
    `I. E XTERNAL L IGHTNING S ETTINGS`；这里用间隙 < 1.5pt → 直接拼接来还原原文。
    """
    out = ""
    prev: Optional[_Unit] = None
    for u in sorted(units, key=lambda x: x.bbox[0]):
        if prev is None:
            out = u.text
        else:
            gap = u.bbox[0] - prev.bbox[2]
            if gap < 1.5:
                out += u.text
            else:
                out += (" " if _needs_space(out, u.text) else "") + u.text
        prev = u
    return out


def _is_punct_only(text: str) -> bool:
    s = text.strip()
    return bool(s) and all(ch in ".,;:!?()[]{}\"'\u2019\u201d\u2018\u201c\u2014-/\u2026" or ch.isspace()
                           for ch in s)


def _block_units(block: dict, page_index: int) -> list[_Unit]:
    """把块切成按视觉行分组的 unit（同视觉行、同风格的 span 合并）。

    视觉行的垂直容差**随字号放大**（`0.45 × max(size)`，下限 2pt）：这样正文 `8` 与其
    上标 `th`（字号 6.5、基线上移 3.5pt、水平间隙 0.24pt）会归入同一视觉行，正确还原
    `8th` / `1st` / `2nd`；而正常行距 ≈1.3–1.4 倍字号，不会被误并。
    """
    units: list[_Unit] = []
    groups: list[list[dict]] = []
    for sp in _spans_of_block(block):
        placed = False
        for g in groups:
            tol = max(2.0, 0.45 * max(float(g[0].get("size") or 0.0),
                                      float(sp.get("size") or 0.0)))
            if abs(g[0]["origin"][1] - sp["origin"][1]) <= tol:
                g.append(sp)
                placed = True
                break
        if not placed:
            groups.append([sp])
    groups.sort(key=lambda g: (round(g[0]["origin"][1], 1), round(g[0]["origin"][0], 1)))
    for vi, g in enumerate(groups):
        g.sort(key=lambda s: s["origin"][0])
        for sp in g:
            text = sp["text"]
            font = sp.get("font") or ""
            bbox = list(sp.get("bbox") or [0, 0, 0, 0])
            origin = list(sp.get("origin") or [bbox[0], bbox[3]])
            is_marker = font.startswith("Symbol") and len(text.strip()) == 1
            if is_marker:
                text = "\u2022"
            u = _Unit(
                text=text, bbox=tuple(round(v, 2) for v in bbox),
                origin=(round(origin[0], 2), round(origin[1], 2)),
                size=float(sp.get("size") or 0.0), font=font,
                color=int(sp.get("color") or 0), flags=int(sp.get("flags") or 0),
                bold=_span_bold(sp), italic=_span_italic(sp),
                vis_line=vi, is_marker=is_marker,
            )
            # 合并同视觉行、同风格、紧邻的 unit（Word 常把一个词拆成多个 span）
            if (units and units[-1].vis_line == u.vis_line
                    and units[-1].bold == u.bold and units[-1].italic == u.italic
                    and abs(units[-1].size - u.size) < 0.15
                    and units[-1].color == u.color
                    and u.bbox[0] - units[-1].bbox[2] < 1.5
                    and units[-1].bbox[0] <= u.bbox[0]):
                p = units[-1]
                p.text = p.text + u.text
                p.bbox = (p.bbox[0], min(p.bbox[1], u.bbox[1]),
                          max(p.bbox[2], u.bbox[2]), max(p.bbox[3], u.bbox[3]))
                continue
            units.append(u)
    return units



def _page_ctx(page) -> dict:
    """页面上下文：正文列边界、正文字号、是否多栏。"""
    rect = page.rect
    body_left = BODY_COLUMN_LEFT
    lefts: list[float] = []
    size_chars: dict[float, int] = {}
    narrow_tall = 0
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        sps = _spans_of_block(b)
        if not sps:
            continue
        bx = b["bbox"]
        is_band = bx[3] <= HEADER_BAND_Y or bx[1] >= FOOTER_BAND_Y
        if not is_band:
            lefts.append(round(bx[0], 1))
            w = bx[2] - bx[0]
            if w < 0.55 * rect.width and (bx[3] - bx[1]) > 200:
                narrow_tall += 1
        for sp in sps:
            k = round(float(sp.get("size") or 0.0), 1)
            size_chars[k] = size_chars.get(k, 0) + len(sp["text"])
    if lefts:
        body_left = statistics.mode([v for v in lefts if v < rect.width * 0.5] or lefts)
    body_size = max(size_chars.items(), key=lambda kv: kv[1])[0] if size_chars else 10.0
    return {
        "rect": rect, "body_left": body_left, "body_size": body_size,
        "multi_column": narrow_tall >= 3,
        "text_right": max([b["bbox"][2] for b in page.get_text("dict")["blocks"] if b.get("type") == 0] or [BODY_COLUMN_RIGHT]),
    }


def _order_blocks(blocks: list[dict], ctx: dict) -> list[dict]:
    """阅读顺序：单栏页按 (y0,x0) 排序（保证插入文本后顺序仍稳定）；多栏页保留 PDF 固有列序。"""
    if ctx.get("multi_column"):
        return blocks
    return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))


def _cell_runs(units: list[_Unit]) -> list[list[_Unit]]:
    """同一基线上按 x 间隙切成单元格 run（间隙 > CELL_GAP 即换格）。"""
    runs: list[list[_Unit]] = []
    for u in sorted(units, key=lambda x: x.bbox[0]):
        if runs and (u.bbox[0] - max(x.bbox[2] for x in runs[-1])) <= CELL_GAP:
            runs[-1].append(u)
        else:
            runs.append([u])
    return runs


def _zone_is_table(zone: list[int], row_runs: list[list[list[_Unit]]]) -> bool:
    """表格区判定（避免把正文/TOC/术语表切碎）：

    1. ≥TABLE_MIN_ROWS 个 baseline（单行不切：p7 的“pilot is selected.”就是被两端对齐
       撑开的一行，不能当表格）；
    2. 至少两行的 run 数相同（列对齐特征）；
    3. 行数 ≤TABLE_MAX_ROWS（术语表 p400 有 65 行×4 列，按列切会产生上千碎片，
       保持旧的「整列一段 + table_cell」行为）；
    4. 不是目录页的行（`......  12` 结尾的 dot leader 行 → 归 TOC 规则，不切单元格）。
    """
    if len(zone) < TABLE_MIN_ROWS or len(zone) > TABLE_MAX_ROWS:
        return False
    counts = [len(row_runs[i]) for i in zone]
    if not any(counts.count(c) >= 2 for c in set(counts)):
        return False
    dots = 0
    for i in zone:
        for run in row_runs[i]:
            if TOC_TAIL_RE.search(_smart_join([u.text for u in run])):
                dots += 1
                break
    if dots * 2 >= len(zone):          # 半数以上是目录行 → 不是表格
        return False
    return True


def _group_visual_lines(units: list[_Unit]) -> list[list[_Unit]]:
    """把**全页** units 按基线聚成视觉行（跨 block）；返回按 y 升序的行列表。

    容差与 `_block_units` 一致（`0.45 × max(size)`，下限 2pt），这样上标 `th`/`st`
    （基线上移 3.5pt）会归入宿主行，而不是自成一行后被切成独立段。
    """
    lines: list[list[_Unit]] = []
    for u in sorted(units, key=lambda x: (round(x.origin[1], 1), x.bbox[0])):
        placed = False
        for line in lines:
            tol = max(2.0, 0.45 * max(float(line[0].size or 0.0), float(u.size or 0.0)))
            if abs(line[0].origin[1] - u.origin[1]) <= tol:
                line.append(u)
                placed = True
                break
        if not placed:
            lines.append([u])
    lines.sort(key=lambda ln: (round(min(x.origin[1] for x in ln), 1),
                              min(x.bbox[0] for x in ln)))
    return lines


def _style_class(units: list[_Unit]) -> tuple:
    dom = max(units, key=lambda u: len(u.text))
    return (dom.bold, dom.italic, round(dom.size, 1), _font_family(dom.font))


def _column_clusters(rows: list[tuple[float, list[list[_Unit]]]], *,
                     max_gap: float) -> list[list[list[_Unit]]]:
    """把表格各基线上的 cell run 按列聚簇（自上而下）。

    `rows` = [(y, [cell_run, ...]), ...]（按 y 升序），每列结果 = [[cell, ...], ...]。

    竖向合并（表头换行单元格 'ANTI-'+'COLL'）需同时满足：
      1. 只能并入**紧邻上一行**的列，且 baseline 间距 ≤ `max_gap`（约 1.35 倍行距）；
      2. 与上方单元格 **x 重叠 ≥ 较窄者一半**；
      3. **风格一致**（粗体/斜体/字号/字族）；
      4. **本行 cell 数 < 上一行 cell 数**（网格不完整 = 换行续接）。
         数据表每行 cell 数相同，因此不会被误合并成一列多行文本。
    """
    columns: list[dict] = []
    prev_n: Optional[int] = None
    for ri, (y, runs) in enumerate(rows):
        runs = sorted(runs, key=lambda r: min(u.bbox[0] for u in r))
        can_merge = prev_n is not None and len(runs) < prev_n
        for run in runs:
            x0 = min(u.bbox[0] for u in run)
            x1 = max(u.bbox[2] for u in run)
            placed = False
            if can_merge:
                for col in columns:
                    if col["row"] != ri - 1:
                        continue
                    if (y - col["y"]) > max_gap:
                        continue
                    if _style_class(col["cells"][-1]) != _style_class(run):
                        continue
                    ov = min(col["x1"], x1) - max(col["x0"], x0)
                    narrow = min(col["x1"] - col["x0"], x1 - x0)
                    if ov > 0 and narrow > 0 and ov >= 0.5 * narrow:
                        col["cells"].append(run)
                        col["row"] = ri
                        col["y"] = y
                        col["x0"] = min(col["x0"], x0)
                        col["x1"] = max(col["x1"], x1)
                        placed = True
                        break
            if not placed:
                columns.append({"x0": x0, "x1": x1, "y": y, "row": ri, "cells": [run]})
        prev_n = len(runs)
    return [c["cells"] for c in columns]


def _merge_tiny_cells(runs: list[list[_Unit]]) -> list[list[_Unit]]:
    """硬性下限：单元格文本 < CELL_MIN_CHARS 时并入同行的相邻单元格。

    真实表格里会有 '0' 这类单字符单元格（p25 最后一行），不能因此放弃整张表，
    但也不能让它单独成段（Lead 要求：< 2 字符不得单独成段）。
    """
    merged: list[list[_Unit]] = []
    for run in runs:
        txt = _smart_join([u.text for u in run]).strip()
        if merged and len(txt) < CELL_MIN_CHARS:
            merged[-1].extend(run)
        else:
            merged.append(list(run))
    if len(merged) >= 2 and len(_smart_join([u.text for u in merged[0]]).strip()) < CELL_MIN_CHARS:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def _table_zone_paragraphs(rows: list[tuple[float, list[list[_Unit]]]], ctx: dict,
                           page_no: int, *, max_gap: float) -> list[_Par]:
    """把「同一批基线上的多单元格行」切成每列一个 kind=table_cell 段。"""
    rows = [(y, _merge_tiny_cells(runs)) for y, runs in rows]
    out: list[_Par] = []
    for cells in _column_clusters(rows, max_gap=max_gap):
        flat = [u for cell in cells for u in cell]
        flat.sort(key=lambda u: (u.vis_line, u.bbox[0]))
        par = _make_par(flat, ctx, page_no, pitch=0.0, join_mode="smart")
        par.kind = "table_cell"
        out.append(par)
    return out


def _is_justified_last_line(group: list[_Unit], prev: _Unit) -> bool:
    """判断组内最后一行是否为「两端对齐后被撑开的段末行」。

    Word 会把段末行也做两端对齐，词数少时词间距被拉得很大；此时行距正常、风格不变，
    仅靠行距/缩进无法切段（真实 p7：'pilot is selected.' 三个词被撑到 144pt 整栏，
    词间距 38pt；而普通行的词间距只有 3–5pt）。
    判据：**行内词间距中位数 > 8pt** + 以句末标点结尾 + 词数 ≤ 8 + 组内已有 ≥2 行。
    """
    if prev.vis_line is None:
        return False
    line_units = sorted((u for u in group if u.vis_line == prev.vis_line),
                        key=lambda u: u.bbox[0])
    if not line_units or len({u.vis_line for u in group}) < 2:
        return False
    text = _smart_join([u.text for u in line_units]).strip()
    if not text or text[-1:] not in SENT_END_CHARS or len(text.split()) > 8:
        return False
    gaps = [b.bbox[0] - a.bbox[2] for a, b in zip(line_units, line_units[1:])
            if b.bbox[0] - a.bbox[2] > 1.0]
    if not gaps:
        return False
    return statistics.median(gaps) > 8.0


def _units_to_pars(units: list[_Unit], ctx: dict, page_no: int, pitch: float = 0.0) -> list[_Par]:
    """把（同一 block 的）units 切成段落 —— 正文启发式：

    同一视觉行内：
      * 字号变化 > 0.6pt → 断（标题字号混排）；
      * 字族变化（Cambria 标题 vs Calibri 正文）且两侧都是实词 → 断；
      * **仅字重变化（Calibri vs Calibri-Bold 行内强调）→ 不断**，否则会把
        “For that purpose, you have four important document sources:” 这类
        行内加粗切成一堆碎片。
    跨视觉行：
      * 字号变化 > 0.6pt → 断；
      * 字族变化（两侧都是实词）→ 断；
      * 行距 > 1.45 倍行距中位数（段间空行）/ < 0.7 倍（异常）→ 断；
      * 本行首个 unit 相对**上一行最右 unit 的 x0**（悬挂缩进基准）左右偏移 > 8pt → 断；
      * 本行以列表标记开头 → 断；
      * 目录行（`......  12`）→ 每行独立成段。
    """
    if not units:
        return []
    line_texts: dict[int, list[_Unit]] = {}
    for u in units:
        line_texts.setdefault(u.vis_line, []).append(u)

    # 目录页：每个条目一行 → 一行一段（有专门规则，避免把标题与页码拼一行）
    toc_lines = [vi for vi, us in line_texts.items()
                 if TOC_LINE_RE.search(_smart_join([u.text for u in us]))]
    if len(line_texts) >= 3 and len(toc_lines) >= 0.7 * len(line_texts):
        return [_make_par([u for u in units if u.vis_line == vi], ctx, page_no, pitch=0.0)
                for vi in sorted(line_texts)]

    if pitch <= 0:
        origins: list[float] = []
        for u in units:
            if not origins or abs(u.origin[1] - origins[-1]) > 0.5:
                origins.append(u.origin[1])
        pitch_vals = [b - a for a, b in zip(origins, origins[1:]) if 0 < (b - a) < 60]
        pitch = statistics.median(pitch_vals) if pitch_vals else 13.4

    groups: list[list[_Unit]] = [[units[0]]]
    for prev, cur in zip(units, units[1:]):
        brk = False
        fam_changed = (_font_family(cur.font) != _font_family(prev.font)
                       and len(prev.text.strip()) >= 8 and len(cur.text.strip()) >= 8
                       and not _is_punct_only(cur.text))
        if cur.vis_line == prev.vis_line:
            # 同一视觉行内断段必须伴随**真实水平间隙**（≥CELL_GAP）：
            # Word 小型大写字母会把一个词拆成多个不同字号的 span（p6 的
            # `I. EXTERNAL LIGHTNING SETTINGS` → `I. E`/`XTERNAL`/`L`/…），
            # 间隙只有 0.2–2.6pt，绝不能被当成独立段。
            gap = cur.bbox[0] - prev.bbox[2]
            brk = (gap >= CELL_GAP) and ((abs(cur.size - prev.size) > 0.6) or fam_changed)
        else:
            # 悬挂缩进基准 = 上一视觉行里**第一个非项目符号 unit** 的 x0
            prev_units = groups[-1]
            last_line = max(u.vis_line for u in prev_units)
            ref_x = next((u.bbox[0] for u in prev_units
                          if u.vis_line == last_line and not u.is_marker), prev.bbox[0])
            dy = cur.origin[1] - prev.origin[1]
            if abs(cur.size - prev.size) > 0.6:
                brk = True
            elif fam_changed:
                brk = True
            elif dy > pitch * 1.45 or dy < pitch * 0.7:
                brk = True
            elif _is_justified_last_line(prev_units, prev):
                # 上一行是「两端对齐的末行」（词少、被撑满整栏、以句末标点结尾）→ 段结束
                brk = True
            elif cur.bbox[0] > ref_x + 8.0 or cur.bbox[0] < ref_x - 8.0:
                brk = True
            elif LIST_MARKER_RE.match(cur.text.strip()):
                brk = True
        if brk:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return [_make_par(g, ctx, page_no, pitch) for g in groups]


def _make_par(g: list[_Unit], ctx: dict, page_no: int, pitch: float,
              join_mode: str = "dehyphenate") -> _Par:
    """unit 组 → _Par（文本按行智能拼接，避免 'glareshields .' 这类多余空格）。

    join_mode="dehyphenate"（正文）：按 CONTRACT §4.1 复原行尾软连字符；
    join_mode="smart"（表格单元格）：只做智能拼接，保留 'ANTI-' + 'COLL' → 'ANTI-COLL'。
    """
    by_line: dict[int, list[_Unit]] = {}
    for u in g:
        by_line.setdefault(u.vis_line, []).append(u)
    line_texts = [_join_line_units(by_line[vi]).strip() for vi in sorted(by_line)]
    if join_mode == "smart":
        text = _smart_join([t for t in line_texts if t])
    else:
        # 段内拼接：按 CONTRACT §4.1 复原行尾软连字符（Cau- + tion → Caution）
        text = dehyphenate_lines([t for t in line_texts if t])
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    bbox = (min(u.bbox[0] for u in g), min(u.bbox[1] for u in g),
            max(u.bbox[2] for u in g), max(u.bbox[3] for u in g))
    line_boxes = []
    for vi in sorted(by_line):
        us = by_line[vi]
        line_boxes.append([round(min(x.bbox[0] for x in us), 2), round(min(x.bbox[1] for x in us), 2),
                           round(max(x.bbox[2] for x in us), 2), round(max(x.bbox[3] for x in us), 2)])
    sizes: dict[float, int] = {}
    fonts: dict[tuple, int] = {}
    for u in g:
        sizes[round(u.size, 1)] = sizes.get(round(u.size, 1), 0) + len(u.text)
        key = (u.font, round(u.size, 1), u.color, u.flags)
        fonts[key] = fonts.get(key, 0) + len(u.text)
    size = max(sizes.items(), key=lambda kv: kv[1])[0]
    dom = max(g, key=lambda u: len(u.text))
    total_chars = max(1, len(text))
    return _Par(
        page=page_no, text=text, bbox=tuple(round(v, 2) for v in bbox),
        line_boxes=line_boxes, size=size, font=dom.font, color=dom.color, flags=dom.flags,
        bold=sum(len(u.text) for u in g if u.bold) > 0.6 * total_chars,
        italic=sum(len(u.text) for u in g if u.italic) > 0.6 * total_chars,
        kind="paragraph", role="body", align="left", indent=0.0, list_level=0,
        fonts=[{"font": k[0], "size": k[1], "color": k[2], "flags": k[3], "count": v}
               for k, v in sorted(fonts.items(), key=lambda kv: -kv[1])],
        n_lines=len(by_line), first_origin=g[0].origin, pitch=round(pitch, 2),
    )



def _classify(par: _Par, ctx: dict) -> None:
    """就地判定 kind / role / align / indent / list_level。"""
    rect = ctx["rect"]
    body_left = ctx["body_left"]
    body_size = ctx["body_size"]
    x0, y0, x1, y1 = par.bbox
    indent = round(x0 - body_left, 2)
    par.indent = indent
    par.list_level = max(0, min(4, int(round(max(0.0, indent) / 12.0))))

    # role
    if y1 <= HEADER_BAND_Y:
        par.role = "pagenum" if par.text.strip().isdigit() else "header"
    elif y0 >= FOOTER_BAND_Y:
        par.role = "pagenum" if par.text.strip().isdigit() else "footer"
    else:
        par.role = "body"

    # align
    center = (x0 + x1) / 2.0
    if abs(center - rect.width / 2.0) <= 8.0 and (x0 - body_left) > 20.0 and (rect.width - x1) > 20.0:
        par.align = "center"
    elif abs(x1 - (ctx.get("text_right") or BODY_COLUMN_RIGHT)) <= 4.0 and indent > 4.0:
        par.align = "right"
    else:
        par.align = "left"

    # kind
    stripped = par.text.strip()
    # 目录行的 dot leader（`......`）会掩盖真实文本长度/大小写，判定标题时先去掉
    core_text = TOC_TAIL_RE.sub("", stripped).strip() or stripped
    if par.role != "body":
        par.kind = "page_num" if par.role == "pagenum" else ("header" if par.role == "header" else "footer")
        return
    if par.kind == "table_cell":
        return
    # 罗马数字小节标题（附件条目，如「I. EXTERNAL LIGHTNING SETTINGS ……」）优先于列表标记判定
    if ROMAN_HEADING_RE.match(core_text) and len(core_text) <= 120:
        par.kind = "heading"
        return
    if LIST_MARKER_RE.match(stripped) or stripped[:1] in _BULLET_CHARS:
        par.kind = "list_item"
        return
    if CAPTION_RE.match(stripped):
        par.kind = "caption"
        return
    if indent >= 9.0:
        par.kind = "list_item"
        return
    is_heading = False
    if par.bold and len(stripped) <= 48 and not stripped.endswith((".", "。")) and stripped:
        if par.n_lines <= 2 or par.size > body_size:
            is_heading = True
    if par.size >= body_size + 1.5 and len(stripped) <= 120 and par.n_lines <= 3:
        is_heading = True
    if HEADING_NUM_RE.match(stripped) and len(stripped) <= 120 and par.bold:
        is_heading = True
    # 罗马数字小节标题（附件/附录条目，如 p6「I. EXTERNAL LIGHTNING SETTINGS ……」）
    if ROMAN_HEADING_RE.match(stripped) and len(stripped) <= 120:
        is_heading = True
    if stripped.isupper() and 2 <= len(stripped) <= 60 and par.bold:
        is_heading = True
    if par.align == "center" and par.size >= body_size and par.n_lines == 1 and len(stripped) <= 90:
        is_heading = True
    if is_heading:
        par.kind = "heading"
        if indent >= 9.0:
            par.kind = "heading"


def _mark_table_columns(pars: list[_Par], page, ctx: dict) -> None:
    """多栏页（术语表/索引）：把高瘦窄列的段标记为 table_cell，并按行拆分。"""
    if not ctx.get("multi_column"):
        return
    rect = ctx["rect"]
    for p in pars:
        if p.role != "body":
            continue
        w = p.bbox[2] - p.bbox[0]
        h = p.bbox[3] - p.bbox[1]
        if w < 0.55 * rect.width and h > 200:
            p.kind = "table_cell"


def _merge_continuations(pars: list[_Par], ctx: dict) -> list[_Par]:
    """跨块续段合并。

    PyMuPDF 偶尔把「一个段落的末行」单独切成一个块（真实手册第 21 页
    “▪ If the EPU RUN light is off … (refer to Sys” / “the EP checklists).”）。
    规则：上一段**未以句末标点结尾** + 下一段缩进或首字母小写 + 行距正常 → 视为同段。
    多栏页（术语表）与目录页不做合并。
    """
    if ctx.get("multi_column"):
        return pars
    out: list[_Par] = []
    for p in pars:
        if out:
            q = out[-1]
            gap = p.bbox[1] - q.bbox[3]
            cont_tail = bool(q.text) and q.text.rstrip()[-1:] not in SENT_END_CHARS
            indented = p.bbox[0] > q.bbox[0] + 4.0
            lower_start = bool(p.text[:1]) and p.text[:1].islower()
            ok = (
                q.page == p.page and q.role == "body" and p.role == "body"
                and q.kind not in ("heading", "caption", "table_cell")
                and p.kind not in ("heading", "caption", "table_cell")
                and not TOC_LINE_RE.search(q.text) and not TOC_LINE_RE.search(p.text)
                and not LIST_MARKER_RE.match(p.text.strip())
                and abs(p.size - q.size) <= 0.3
                and _font_family(p.font) == _font_family(q.font)
                # 只有「下一段在上段下方」才可能是同段续行；同一基线上的并排块（表格单元格）
                # 不能合并，否则会把一行多列拼成一段（gap 为负）
                and gap >= -1.0
                and gap <= max(6.0, 0.6 * max(q.pitch, 8.0))
                and cont_tail and (indented or lower_start)
            )
            if ok:
                q.text = _smart_join([q.text, p.text])
                q.bbox = (min(q.bbox[0], p.bbox[0]), min(q.bbox[1], p.bbox[1]),
                          max(q.bbox[2], p.bbox[2]), max(q.bbox[3], p.bbox[3]))
                q.line_boxes = list(q.line_boxes) + list(p.line_boxes)
                q.n_lines += p.n_lines
                q.fonts = sorted(list(q.fonts) + list(p.fonts), key=lambda f: -f["count"])[:6]
                continue
        out.append(p)
    return out


def page_paragraphs(doc, page_no=None) -> list[_Par]:
    """内部 API：抽取一页的段落（_Par 列表，含 kind/role/style）。

    第一个参数可以是 Document+1-based 页码，也可以直接是 pymupdf.Page。

    流程（表格必须在**全页**范围识别：表头各单元格常是彼此独立的 PDF block）：
      1. 每个 block → units（同视觉行、同风格合并）；
      2. 全页 units 按基线聚成视觉行（跨 block）；
      3. 某条基线若有 ≥CELL_MIN_RUNS 个 x 不相邻的 run → 判为表格行；
         相邻表格行组成表格区域 → 按列聚簇 → 每列一个 `kind="table_cell"` 段；
      4. 其余 units 按原 block 走正文启发式；
      5. 分类 → 多栏标记 → 跨块续段合并。
    """
    page = doc if isinstance(doc, pymupdf.Page) else page_of(doc, page_no)
    page_no = page.number + 1
    ctx = _page_ctx(page)
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0 and _spans_of_block(b)]
    ordered = _order_blocks(blocks, ctx)

    block_units: list[list[_Unit]] = [_block_units(b, page_no) for b in ordered]
    for bi, us in enumerate(block_units):
        for u in us:
            u.block_idx = bi
    all_units = [u for us in block_units for u in us]
    if not all_units:
        return []

    # 2) 全页视觉行（跨 block），并重编 vis_line（供 _make_par 分 line_boxes）
    lines = _group_visual_lines(all_units)
    for li, ln in enumerate(lines):
        for u in ln:
            u.vis_line = li
    row_runs = [_cell_runs(ln) for ln in lines]
    cand_rows = {i for i, runs in enumerate(row_runs) if len(runs) >= TABLE_MIN_COLS}

    if cand_rows:
        # 页面行距（用于判断表格单元格的竖向续接跨度）
        ys = sorted({round(min(x.origin[1] for x in ln), 1) for ln in lines})
        pitches = [b - a for a, b in zip(ys, ys[1:]) if 0 < (b - a) < 60]
        pitch = statistics.median(pitches) if pitches else 13.4
        # 3) 相邻候选行 → 表格区域；只有满足 _zone_is_table 的区域才切单元格
        zones: list[list[int]] = []
        for i in sorted(cand_rows):
            if zones and i - zones[-1][-1] == 1:
                zones[-1].append(i)
            else:
                zones.append([i])
        zones = [z for z in zones if _zone_is_table(z, row_runs)]

        table_pars: dict[int, list[_Par]] = {}
        for zone in zones:
            rows = [(round(min(x.origin[1] for x in lines[i]), 2), row_runs[i]) for i in zone]
            first = min((u for i in zone for u in lines[i]),
                        key=lambda u: (u.origin[1], u.bbox[0]))
            for u in (u for i in zone for u in lines[i]):
                u.table = True
            table_pars.setdefault(first.block_idx, []).extend(
                _table_zone_paragraphs(rows, ctx, page_no, max_gap=pitch * 1.35))
    else:
        table_pars = {}

    # 4) 其余 units 按 block 顺序做正文分段；表格段锚定在其起始 block 的位置
    pars: list[_Par] = []
    for bi, us in enumerate(block_units):
        for p in table_pars.get(bi, []):
            pars.append(p)
        rest = [u for u in us if not u.table]
        if rest:
            pars.extend(_units_to_pars(rest, ctx, page_no))

    for p in pars:
        _classify(p, ctx)
    _mark_table_columns(pars, page, ctx)
    pars = _merge_continuations(pars, ctx)
    return pars


def extract_segments(doc, page_no) -> list[Any]:
    """把一页拆成段，返回 list[core.models.Segment]。

    ⚠️ `page_no` 是 **1-based**（与 `Segment.page` 一致）；也接受 pymupdf.Page 对象。
    段内 `order_index` 由 ingest_pdf 统一编号（此处为页内序号占位）。
    """
    m = models()
    pars = page_paragraphs(doc, page_no)
    segs: list[Any] = []
    for i, p in enumerate(pars):
        # canonical_text = 已复原软连字符并归一化的文本（CONTRACT §3.1.1 / §4.1）
        canonical = normalize(p.text)
        seg = m.Segment(
            page=p.page, order_index=i, kind=p.kind, text=p.text,
            canonical_text=canonical, normalized=canonical,
            fingerprint=fp_of(canonical), content_hash=chash_of(canonical),
            bbox=list(p.bbox), line_boxes=[list(b) for b in p.line_boxes],
            fonts=[dict(f) for f in p.fonts],
            style={"bold": p.bold, "italic": p.italic, "size": p.size, "color": p.color,
                   "align": p.align, "list_level": p.list_level, "indent": p.indent},
            role=p.role,
        )
        segs.append(seg)
    return segs


# --------------------------------------------------------------------------------------
# 入库
# --------------------------------------------------------------------------------------

def _wipe_version_segments(conn, version_id: int) -> int:
    """删除某版本已抽取的段（translations 随 FK ON DELETE CASCADE 一并清理）。

    ⚠️ 会**级联删除该版本的全部译文**！只允许在
    `ingest_pdf(reingest=True, allow_translation_loss=True)` 或
    `rebuild_version(keep_translations=...)`（先备份再调用）内部使用。
    同时清掉 segment_links / version_diffs —— 段 id 变了，这两个缓存必须失效，
    否则 diff 会复用指向已删除段的旧链接（实测会给出错误的 removed 计数）。
    """
    conn.execute("DELETE FROM segment_links WHERE new_segment_id IN "
                 "(SELECT id FROM segments WHERE version_id = ?) "
                 "OR old_segment_id IN (SELECT id FROM segments WHERE version_id = ?)",
                 (int(version_id), int(version_id)))
    conn.execute("DELETE FROM version_diffs WHERE from_version_id = ? OR to_version_id = ?",
                 (int(version_id), int(version_id)))
    cur = conn.execute("DELETE FROM segments WHERE version_id = ?", (int(version_id),))
    conn.commit()
    return int(cur.rowcount or 0)


# --------------------------------------------------------------------------------------
# 译文保全：dump / restore / rebuild（重建分段不得丢译文）
# --------------------------------------------------------------------------------------

def dump_translations(conn, version_id: Optional[int] = None) -> list[dict]:
    """导出译文（连同来源段信息），用于重建后的回填与备份。

    每条包含：text/status/provider/model/confidence/revision/reviewed_by +
    来源段 version_id/page/order_index/fingerprint/canonical_text/kind。
    """
    sql = (
        "SELECT t.segment_id AS old_seg, t.text, t.status, t.provider, t.model, "
        "t.confidence, t.revision, COALESCE(t.reviewed_by,'') AS reviewed_by, "
        "s.version_id, s.page, s.order_index, s.fingerprint, s.canonical_text, s.kind "
        "FROM translations t JOIN segments s ON s.id = t.segment_id"
    )
    params: tuple = ()
    if version_id:
        sql += " WHERE s.version_id = ?"
        params = (int(version_id),)
    sql += " ORDER BY s.version_id, s.page, s.order_index"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def backup_translations(conn, version_id: Optional[int] = None, *, path: Optional[str] = None) -> dict:
    """把译文备份成 JSON 文件（默认 data/derived/translations_backup_<时间戳>.json）。"""
    rows = dump_translations(conn, version_id)
    if path is None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = str(derived_dir() / f"translations_backup_{stamp}.json")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(p), "count": len(rows), "rows": rows}


SHAPE_RATIO = 3.0        # 译文 / 原文 长度比上限
SHAPE_FLOOR = 40         # 长度比的绝对下限（短原文允许到 40 字符）
SHAPE_TINY_EN = 2        # 「极短原文」阈值
SHAPE_TINY_ZH = 12       # 极短原文允许的最长译文

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 型号/编号/纯符号数字（`BMS 4.38.1`、`F-16`、`TR_BMS_01_GroundOPS`、`----`、`1000 PSI`）
# 注意：**不含小写字母**，否则普通英文句子会被误判成「型号」而逃过清理。
MODEL_LIKE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ._/\-–—()%,°+#'\":]*$")


def _shape_ok(en: str, zh: str, *, min_en: Optional[int] = None,
              ratio: float = SHAPE_RATIO, floor: float = SHAPE_FLOOR) -> bool:
    """形状一致性：译文长度必须与其原文**相称**（Lead 指定的红线）。

    判定（两条都满足才通过）：
      1. `len(zh) <= max(ratio * len(en), floor)` —— 真正要挡的是「整行译文落进单词格」，
         这条恒成立即保证 Lead 的验收 SQL（`length(zh) > 3*length(en)+40`）为 0；
      2. 原文极短（`len(en) <= SHAPE_TINY_EN`）时，译文不得超过 `SHAPE_TINY_ZH`。

    ⚠️ `min_en` 参数已废弃（保留只为兼容旧调用）：历史上用「`len(en) >= 8` 才允许挂」
    的写法把 `G→G`、`to→至`、`a)→a)` 这类**合法短段译文**一并拒掉，造成 658 条误伤。
    短段是否错配应看**长度相称性**，而不是原文的绝对长度。

    用例：`('G','G')→True`、`('to','至')→True`、`('AVIONICS', 40+ 字整行译文)→False`。
    """
    en = (en or "").strip()
    zh = (zh or "").strip()
    if not zh:
        return False
    if len(zh) > max(ratio * len(en), float(floor)):
        return False
    if len(en) <= SHAPE_TINY_EN and len(zh) > SHAPE_TINY_ZH:
        return False
    return True


def classify_translation(text: str, source: str = "", status: str = "") -> str:
    """给译文分类，供「清理无语义译文」使用。返回值：

    * `"ok"`          —— 正常译文（含中文；或**原样保留型号/编号/符号**）
    * `"mock"`        —— mock 占位（machine 且含 `〔…〕`）
    * `"empty"`       —— failed 且空文本
    * `"meaningless"` —— machine 译文里既没有中文、也没有保留原文（真正的污染）

    关键：`BMS 4.38.1`、`F-16`、`TR_BMS_01_GroundOPS`、`----`、`1000 psi` 这类
    **本来就该原样保留**的内容属于 `"ok"`，不能因为「和原文一样」或「没有中文」就删掉。
    """
    t = (text or "").strip()
    s = (source or "").strip()
    if status == "failed" and not t:
        return "empty"
    if status == "machine" and "〔" in t:
        return "mock"
    if CJK_RE.search(t):
        return "ok"
    if not t:
        return "meaningless"
    if s and t == s:                       # CARRIED / 原样保留
        return "ok"
    if MODEL_LIKE_RE.match(t):             # 型号、编号、纯符号/数字组合
        return "ok"
    if status and status != "machine":     # carried/reviewed/seeded 一律保留
        return "ok"
    return "meaningless"


def restore_translations(conn, version_id: int, backup: list[dict], *,
                         shape_check: bool = True, min_en_chars: int = 8,
                         dry_run: bool = False) -> dict:
    """把 `dump_translations()` 的备份按三级匹配回填到重建后的段上。

    匹配优先级：
      1. 同版本 + **同页 + 同 fingerprint**（最严，正文/表格都是 1:1）；
      2. 同版本 + 同 fingerprint，取页码/序最接近且未被占用者；
      3. 同页 + 文本包含（仅当 `len(en) >= min_en_chars` 且长度比合理 —— 不允许
         `new in old` 这种「整行 → 单词」的子串包含单独立论）。

    形状一致性（`shape_check=True`）：`len(zh) <= max(3*len(en), 40)` 且 `len(en) >= 8`；
    不满足者**不挂**，留作 `missing` 等待重新翻译。表头/短单元格尤其严格。
    一个目标段只接一条译文。
    """
    stats = {"backup": len(backup), "exact": 0, "near": 0, "contains": 0,
             "skipped_shape": 0, "lost": 0, "restored": 0, "skipped": []}
    if not backup:
        return stats
    if dry_run:
        return stats

    by_page_fp: dict[tuple, list[dict]] = {}
    by_fp: dict[tuple, list[dict]] = {}
    by_page: dict[tuple, list[dict]] = {}
    for vid in {int(d["version_id"]) for d in backup}:
        for s in _store().segments_of_version(conn, vid):
            key_page = (vid, int(s["page"]), s["fingerprint"])
            by_page_fp.setdefault(key_page, []).append(s)
            by_fp.setdefault((vid, s["fingerprint"]), []).append(s)
            by_page.setdefault((vid, int(s["page"])), []).append(s)

    used: set[int] = set()
    for d in backup:
        vid = int(d["version_id"])
        page = int(d["page"])
        fp = d["fingerprint"]
        order = int(d["order_index"] or 0)
        old_en = d.get("canonical_text") or ""
        zh = d.get("text") or ""
        target = None
        tag = ""

        for s in by_page_fp.get((vid, page, fp), []):
            if int(s["id"]) not in used:
                target, tag = s, "exact"
                break
        if target is None:
            cands = [s for s in by_fp.get((vid, fp), []) if int(s["id"]) not in used]
            if cands:
                cands.sort(key=lambda s: (abs(int(s["page"]) - page),
                                          abs(int(s["order_index"]) - order)))
                target, tag = cands[0], "near"
        if target is None:
            old_n = normalize(old_en)
            best = None
            for s in by_page.get((vid, page), []):
                if int(s["id"]) in used:
                    continue
                new_n = normalize(s["canonical_text"] or s["text"])
                if not new_n or not old_n or len(new_n) < min_en_chars:
                    continue
                ratio = len(new_n) / max(1, len(old_n))
                if old_n in new_n or (new_n in old_n and ratio >= 0.5):
                    score = abs(len(new_n) - len(old_n))
                    if best is None or score < best[0]:
                        best = (score, s)
            if best:
                target, tag = best[1], "contains"

        if target is None:
            stats["lost"] += 1
            continue
        en_new = target.get("canonical_text") or target.get("text") or ""
        # 形状一致性：只看译文与原文的**长度相称性**（不再按「原文至少 8 字符」一刀切，
        # 否则 G→G / to→至 / a)→a) 这类合法短段会被误伤）
        if shape_check and not _shape_ok(en_new, zh):
            stats["skipped_shape"] += 1
            if len(stats["skipped"]) < 20:
                stats["skipped"].append({"page": page, "kind": target.get("kind"),
                                         "en": en_new[:40], "zh_len": len(zh)})
            continue

        used.add(int(target["id"]))
        _store().upsert_translation(conn, segment_id=int(target["id"]), text=zh,
                                    status=d.get("status") or "machine",
                                    provider=d.get("provider") or "",
                                    model=d.get("model") or "",
                                    confidence=float(d.get("confidence") or 0.0))
        conn.execute("UPDATE translations SET revision=?, reviewed_by=? WHERE segment_id=?",
                     (int(d.get("revision") or 1), d.get("reviewed_by") or "", int(target["id"])))
        stats[tag] += 1
        stats["restored"] += 1
    conn.commit()
    return stats


def rebuild_version(conn, version_id: int, *, pdf_path: Optional[str] = None,
                    keep_translations: bool = True,
                    allow_translation_loss: bool = False,
                    progress: Optional[Callable[[int, int, int], None]] = None) -> dict:
    """安全重抽某个版本：**先备份译文 → 清段重抽 → 按 fingerprint 回填**。

    这是「重建分段」的唯一推荐入口（CLI: `ingest --force`）。译文是用户唯一无法重建的
    资产，因此默认 keep_translations=True；显式 keep_translations=False 时还必须
    allow_translation_loss=True，否则拒绝执行。
    """
    store = _store()
    v = store.get_version(conn, version_id)
    if not v:
        raise RuntimeError(f"版本不存在：version_id={version_id}")
    src = pdf_path or _abs_path(v.get("source_path") or "")
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"版本源 PDF 不存在：{src}")
    doc = store.get_document(conn, int(v["doc_id"])) or {}

    backup = dump_translations(conn, version_id)
    if backup and not keep_translations and not allow_translation_loss:
        raise RuntimeError(
            f"拒绝执行：版本 {version_id} 有 {len(backup)} 条译文，重建分段会级联删除它们。"
            f"请用 keep_translations=True（备份→重建→回填），"
            f"或显式 allow_translation_loss=True 表示接受丢失。")
    result = {"version_id": int(version_id), "backup_count": len(backup),
              "backup_path": "", "restore": {}, "ingest": {}}
    if keep_translations and backup:
        bak = backup_translations(conn, version_id)
        result["backup_path"] = bak["path"]

    info = ingest_pdf(conn, src, slug=doc.get("slug") or None, title=doc.get("title"),
                      note=v.get("note") or "", label=v.get("label"),
                      pages=None, progress=progress, reingest=True,
                      allow_translation_loss=True)
    result["ingest"] = {"segments": info["segments"], "pages": info["pages"]}
    if keep_translations and backup:
        result["restore"] = restore_translations(conn, version_id, backup)
        result["translations_after"] = len(store.translations_of_version(conn, version_id) or {})
    return result


def ingest_pdf(conn, pdf_path, *, slug: Optional[str] = None, title: Optional[str] = None,
               note: str = "", label: Optional[str] = None,
               pages: Optional[tuple] = None,
               progress: Optional[Callable[[int, int, int], None]] = None,
               reingest: bool = False,
               allow_translation_loss: bool = False) -> dict:
    """把 PDF 登记为新版本并抽取全部段落。

    必填参数与返回结构见 CONTRACT（web/server.py 依赖）：
        return {"doc_id":int,"version_id":int,"pages":int,"segments":int,
                "slug":str,"sha256":str,"release_label":str, ...}

    额外可选参数（向后兼容，均有默认值）：
        pages     —— (from_page, to_page) 1-based，只抽取该区间（快速调试用）
        progress  —— 回调 (page, total, segs_so_far)
        reingest  —— True 时先清空该版本旧段再重抽；默认 False（幂等）

    幂等性：`core.store.register_version` 对同一 doc 下相同 sha256 会**复用**版本行，
    因此这里必须自己判断「该版本是否已抽过」——否则重复调用会把段重复插入同一版本。
    """
    store = _store()
    path = Path(pdf_path)
    if not path.is_absolute():
        cand = (ROOT / path)
        path = cand if cand.exists() else path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"找不到 PDF：{path}")

    sha = sha256_file(path)
    pdf_bytes = path.stat().st_size
    with pymupdf.open(str(path)) as doc:
        if doc.needs_pass:
            raise RuntimeError(f"PDF 已加密，无法处理：{path}")
        page_count = doc.page_count
        meta = doc.metadata or {}
        first_text = ""
        try:
            first_text = doc[0].get_text()[:400]
        except Exception:
            pass
        release = release_label_from(meta, path, first_text)
        ddate = doc_date_from(meta, path)
        slug = slug or slugify(path.stem)
        title = title or (str(meta.get("title") or "").strip() or path.stem)

        doc_id = store.upsert_document(conn, slug=slug, title=title,
                                       source_dir=_rel_path(path.parent))
        existing = store.list_versions(conn, doc_id)
        prior = next((v for v in existing if v.get("sha256") == sha), None)

        # —— 幂等：同一文件、同一文档、且已经抽过段 → 直接复用，不重复插段/不新建版本
        if prior is not None and not reingest:
            have = store.count_segments(conn, int(prior["version_id"]))
            if have > 0:
                store.set_current_version(conn, doc_id, int(prior["version_id"]))
                conn.commit()
                stats = prior.get("stats") or {}
                return {
                    "doc_id": int(doc_id), "version_id": int(prior["version_id"]),
                    "pages": int(prior.get("page_count") or page_count),
                    "segments": int(have), "slug": slug, "sha256": sha,
                    "release_label": prior.get("release_label") or release,
                    "label": prior.get("label") or label or "", "title": title,
                    "chars": int(stats.get("chars") or 0),
                    "images": int(stats.get("images") or 0),
                    "pdf_bytes": int(prior.get("pdf_bytes") or pdf_bytes),
                    "source_path": prior.get("source_path") or _rel_path(path),
                    "doc_date": prior.get("doc_date") or ddate,
                    "reused": True,
                }

        if label is None and prior is None:
            label = f"v{len(existing) + 1}"
        version_id = store.register_version(
            conn, doc_id=doc_id, source_path=_rel_path(path), sha256=sha, pdf_bytes=pdf_bytes,
            page_count=page_count, release_label=release, doc_date=ddate, note=note,
            label=label or (prior.get("label") if prior else ""), make_current=True,
        )
        if prior is not None and reingest:
            # 🚨 产品级红线：重建分段会级联删除该版本全部译文。译文是用户唯一无法
            # 重建的资产（花钱 + 人工复核），因此默认**拒绝执行**，除非调用方显式接受
            # （CLI: --yes-i-understand）或走 rebuild_version()（先备份再回填）。
            have_tr = len(store.translations_of_version(conn, int(prior["version_id"])) or {})
            if have_tr and not allow_translation_loss:
                raise RuntimeError(
                    f"拒绝重建：版本 {prior['version_id']}（{prior.get('label')}）已有 "
                    f"{have_tr} 条译文，重建分段会级联删除它们。\n"
                    f"  · 想保留译文：pipeline.rebuild_version(conn, {prior['version_id']})"
                    f"（先备份→重建→按 fingerprint 回填），CLI 用 "
                    f"`python cli.py ingest --pdf ... --force`；\n"
                    f"  · 确实要丢弃译文：显式传 allow_translation_loss=True"
                    f"（CLI: --rebuild --yes-i-understand）。")
            removed = _wipe_version_segments(conn, version_id)
            print(f"[pipeline] reingest：已清空版本 {version_id} 的旧段 {removed} 条"
                  + (f"（其中译文 {have_tr} 条已随外键级联删除）" if have_tr else ""),
                  file=sys.stderr)


        order = 0
        total = 0
        chars = 0
        images = 0
        p_from = int(pages[0]) if pages else 1
        p_to = int(pages[1]) if pages else page_count
        # 全书语料词表：让 dehyphenate 用「词表命中 + 软换行后缀」判定软连字符
        global _VOCAB
        _VOCAB = build_document_vocabulary(doc, p_from, p_to)
        for pno in range(1, page_count + 1):
            if not (p_from <= pno <= p_to):
                continue
            segs = extract_segments(doc, pno)
            for s in segs:
                s.version_id = version_id
                s.doc_id = doc_id
                s.page = pno
                s.order_index = order
                order += 1
            if segs:
                ids = store.insert_segments(conn, segs)
                for s, sid in zip(segs, ids):
                    s.id = sid
            total += len(segs)
            chars += sum(len(s.text) for s in segs)
            try:
                images += len(doc[pno - 1].get_image_info())
            except Exception:
                pass
            if progress and (pno % 25 == 0 or pno == p_to):
                progress(pno, page_count, total)
        _VOCAB = None

        store.update_version_stats(conn, version_id, segments=total, chars=chars,
                                   pages=page_count, images=images)
    try:
        conn.commit()
    except Exception:
        pass
    return {
        "doc_id": int(doc_id), "version_id": int(version_id), "pages": int(page_count),
        "segments": int(total), "slug": slug, "sha256": sha, "release_label": release,
        "label": label, "title": title, "chars": int(chars), "images": int(images),
        "pdf_bytes": int(pdf_bytes), "source_path": str(path), "doc_date": ddate,
        "reused": False,
    }


# --------------------------------------------------------------------------------------
# 术语表种子
# --------------------------------------------------------------------------------------

SEED_GLOSSARY: list[dict] = [
    {"en": "MASTER CAUTION", "zh": "主警戒", "case_sensitive": False, "note": "告警灯"},
    {"en": "canopy", "zh": "座舱盖", "case_sensitive": False, "note": ""},
    {"en": "flight control system", "zh": "飞控系统", "case_sensitive": False, "note": "FLCS"},
    {"en": "STPT", "zh": "航路点", "case_sensitive": True, "note": "steerpoint"},
    {"en": "MFD", "zh": "多功能显示器", "case_sensitive": True, "note": ""},
    {"en": "OBOGS", "zh": "机载制氧系统", "case_sensitive": True, "note": ""},
    {"en": "EPU", "zh": "应急动力装置", "case_sensitive": True, "note": ""},
    {"en": "landing gear", "zh": "起落架", "case_sensitive": False, "note": ""},
    {"en": "throttle", "zh": "油门", "case_sensitive": False, "note": ""},
    {"en": "afterburner", "zh": "加力", "case_sensitive": False, "note": "AB"},
    {"en": "HUD", "zh": "平视显示器", "case_sensitive": True, "note": ""},
    {"en": "RWR", "zh": "雷达告警接收机", "case_sensitive": True, "note": ""},
    {"en": "TACAN", "zh": "塔康", "case_sensitive": True, "note": ""},
    {"en": "ILS", "zh": "仪表着陆系统", "case_sensitive": True, "note": ""},
    {"en": "pitot", "zh": "空速管", "case_sensitive": False, "note": ""},
    {"en": "airspeed", "zh": "空速", "case_sensitive": False, "note": ""},
    {"en": "altitude", "zh": "高度", "case_sensitive": False, "note": ""},
    {"en": "angle of attack", "zh": "攻角", "case_sensitive": False, "note": "AOA"},
    {"en": "runway", "zh": "跑道", "case_sensitive": False, "note": ""},
    {"en": "taxi", "zh": "滑行", "case_sensitive": False, "note": ""},
    {"en": "takeoff", "zh": "起飞", "case_sensitive": False, "note": ""},
    {"en": "approach", "zh": "进近", "case_sensitive": False, "note": ""},
    {"en": "checklist", "zh": "检查单", "case_sensitive": False, "note": ""},
    {"en": "warning light", "zh": "警告灯", "case_sensitive": False, "note": ""},
    {"en": "caution light", "zh": "注意灯", "case_sensitive": False, "note": ""},
    {"en": "fuel", "zh": "燃油", "case_sensitive": False, "note": ""},
    {"en": "radar", "zh": "雷达", "case_sensitive": False, "note": ""},
    {"en": "missile", "zh": "导弹", "case_sensitive": False, "note": ""},
    {"en": "bomb", "zh": "炸弹", "case_sensitive": False, "note": ""},
    {"en": "target", "zh": "目标", "case_sensitive": False, "note": ""},
    {"en": "steerpoint", "zh": "导航点", "case_sensitive": False, "note": ""},
    {"en": "waypoint", "zh": "航路点", "case_sensitive": False, "note": ""},
    {"en": "waypoint list", "zh": "航路点列表", "case_sensitive": False, "note": ""},
    {"en": "DED", "zh": "数据输入显示", "case_sensitive": True, "note": "Data Entry Display"},
    {"en": "PFL", "zh": "飞行员故障提示", "case_sensitive": True, "note": ""},
    {"en": "stick", "zh": "驾驶杆", "case_sensitive": False, "note": ""},
    {"en": "rudder", "zh": "方向舵", "case_sensitive": False, "note": ""},
    {"en": "aileron", "zh": "副翼", "case_sensitive": False, "note": ""},
    {"en": "trim", "zh": "配平", "case_sensitive": False, "note": ""},
    {"en": "airbrake", "zh": "减速板", "case_sensitive": False, "note": ""},
    {"en": "speedbrake", "zh": "减速板", "case_sensitive": False, "note": ""},
    {"en": "gear", "zh": "起落架", "case_sensitive": False, "note": ""},
    {"en": "engine", "zh": "发动机", "case_sensitive": False, "note": ""},
    {"en": "gauge", "zh": "仪表", "case_sensitive": False, "note": ""},
    {"en": "warning", "zh": "警告", "case_sensitive": False, "note": ""},
    {"en": "caution", "zh": "注意", "case_sensitive": False, "note": ""},
    {"en": "emergency", "zh": "应急", "case_sensitive": False, "note": ""},
    {"en": "oil pressure", "zh": "滑油压力", "case_sensitive": False, "note": ""},
    {"en": "hydraulic", "zh": "液压", "case_sensitive": False, "note": ""},
    {"en": "avionics", "zh": "航空电子设备", "case_sensitive": False, "note": ""},
    {"en": "mission", "zh": "任务", "case_sensitive": False, "note": ""},
    {"en": "training mission", "zh": "训练任务", "case_sensitive": False, "note": ""},
    {"en": "flight", "zh": "飞行", "case_sensitive": False, "note": ""},
    {"en": "aircraft", "zh": "飞机", "case_sensitive": False, "note": ""},
    {"en": "cockpit", "zh": "座舱", "case_sensitive": False, "note": ""},
    {"en": "instrument panel", "zh": "仪表板", "case_sensitive": False, "note": ""},
    {"en": "side console", "zh": "侧操纵台", "case_sensitive": False, "note": ""},
    {"en": "auxiliary console", "zh": "辅助操纵台", "case_sensitive": False, "note": ""},
    {"en": "nose gear", "zh": "前起落架", "case_sensitive": False, "note": ""},
    {"en": "wheel brake", "zh": "机轮刹车", "case_sensitive": False, "note": ""},
    {"en": "ground ops", "zh": "地面操作", "case_sensitive": False, "note": ""},
    {"en": "ramp start", "zh": "停机坪开车", "case_sensitive": False, "note": ""},
]


def glossary_seed_path() -> Path:
    return data_dir() / "glossary_seed.json"


def build_glossary_seed(*, write: bool = True, overwrite: bool = False) -> list[dict]:
    """返回术语表种子（优先 translate.glossary 的真实现，其次本文件内置表）。

    write=True 时若 data/glossary_seed.json 不存在则写出（不覆盖已有文件）。
    """
    entries: list[dict] | None = None
    try:
        from translate import glossary as _g  # type: ignore
        for name in ("load", "load_glossary", "get_entries", "get_pairs"):
            fn = getattr(_g, name, None)
            if callable(fn):
                raw = fn()
                if raw:
                    entries = [dict(e) if not isinstance(e, dict) or "en" in e else e for e in raw]
                    break
    except Exception:
        entries = None
    if not entries:
        entries = [dict(e) for e in SEED_GLOSSARY]
    p = glossary_seed_path()
    if write and (overwrite or not p.exists()):
        p.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entries


# --------------------------------------------------------------------------------------
# 导出编排（render/pdfout 由 T5 提供）
# --------------------------------------------------------------------------------------

def normalize_pages(pages: Any, page_count: Optional[int] = None) -> Optional[list]:
    """统一 `pages` 语义（Lead 要求：同一个参数名不能有两种互斥含义）。

    * `None`                 → 全书（返回 None，由调用方展开）
    * `int`                  → 单页
    * `(from, to)` 二元组     → **页区间**
    * `[p1, p2, ...]` 列表    → **显式页列表**（不做区间解释；区间请用二元组）

    返回 `list[int]` 或 None。列表与二元组按类型区分，避免 `(1, 31)` 被当成 2 页。
    """
    if pages is None:
        return None
    if isinstance(pages, int):
        out = [pages]
    elif isinstance(pages, tuple):
        if len(pages) != 2:
            out = [int(p) for p in pages]
        else:
            lo, hi = int(pages[0]), int(pages[1])
            if lo > hi:
                lo, hi = hi, lo
            out = list(range(lo, hi + 1))
    else:  # list / 其它可迭代 → 显式页列表
        out = [int(p) for p in pages]
    if page_count:
        out = [p for p in out if 1 <= p <= int(page_count)]
    return out


def strip_toc_leader(text: str) -> str:
    """去掉目录行尾部的连续引导点号（`......`），供翻译前清理提示词用。

    例：`I. EXTERNAL LIGHTNING SETTINGS ..........` → `I. EXTERNAL LIGHTNING SETTINGS`

    ⚠️ 只在**送模型之前**用（提示词层），不要写回 `Segment.text`：那会改变 fingerprint，
    使已有译文无法按指纹复用。渲染层按原宽度重排即可。
    """
    return re.sub(r"[\s.．·]{3,}$", "", (text or "").rstrip()).rstrip()


def version_bundle(conn, version_id: int) -> dict:
    """取导出/渲染所需的全部数据：版本行、按页分段、译文映射。"""
    store = _store()
    v = store.get_version(conn, version_id)
    if not v:
        raise RuntimeError(f"版本不存在：version_id={version_id}")
    segs = store.segments_of_version(conn, version_id)
    by_page: dict[int, list[dict]] = {}
    for s in segs:
        by_page.setdefault(int(s["page"]), []).append(s)
    trans_rows = store.translations_of_version(conn, version_id)
    trans_text: dict[int, str] = {}
    trans_row: dict[int, dict] = {}
    for sid, row in (trans_rows or {}).items():
        text = (row or {}).get("text") or ""
        trans_text[int(sid)] = text
        trans_row[int(sid)] = row
    src = _abs_path(v.get("source_path") or "")
    return {"version": v, "segments": segs, "by_page": by_page,
            "translations": trans_text, "translation_rows": trans_row, "src_pdf": src}


def _cjk_font() -> str:
    try:
        from core.pdfdoc import resolve_cjk_font  # type: ignore
        return resolve_cjk_font()
    except Exception:
        for cand in (r"C:\Windows\Fonts\Deng.ttf", r"C:\Windows\Fonts\simhei.ttf",
                     r"C:\Windows\Fonts\msyh.ttc"):
            if os.path.exists(cand):
                return cand
    raise RuntimeError("找不到中文字体（Deng.ttf / simhei.ttf / msyh.ttc）")


def _call_pdfout_fn(fn_name: str, *args, **kwargs):
    """调用 render.pdfout 里的任意函数（用于 build_cn_outline 这类辅助函数）。"""
    pdfout = _render_pdfout()
    fn = getattr(pdfout, fn_name, None)
    if fn is None:
        return None
    return fn(*args, **kwargs)


def _call_pdfout(fn_name: str, src_pdf: str, out_pdf: str, by_page: dict, trans_text: dict,
                 trans_rows: dict, *, pages=None, dpi: int = 110, on_page=None,
                 cn_toc=None) -> dict:
    """调用 render.pdfout.export_*；translations 形态兼容 {seg_id: text} 与 {seg_id: row}。"""
    pdfout = _render_pdfout()
    fn = getattr(pdfout, fn_name, None)
    if fn is None:
        raise RuntimeError(f"render.pdfout 缺少 {fn_name}()（T5 未完成或签名不符）")
    kwargs = dict(pages=pages, cjk_font_path=_cjk_font(), dpi=dpi)
    if on_page is not None:
        kwargs["on_page"] = on_page
    if cn_toc:
        kwargs["cn_toc"] = cn_toc
    try:
        return fn(src_pdf, out_pdf, by_page, trans_text, **kwargs)
    except (TypeError, AttributeError) as exc:
        # T5 也可能期望 {seg_id: row}；仅在疑似参数形态问题时重试一次
        if not trans_rows:
            raise
        print(f"[pipeline] {fn_name} 首次调用失败({exc.__class__.__name__}: {exc})，改用 row 形态译文重试",
              file=sys.stderr)
        return fn(src_pdf, out_pdf, by_page, trans_rows, **kwargs)


def export_version_pdf(conn, doc_id: int, version_id: int, *, kind: str = "cn",
                       out_path: Optional[str] = None, pages: Any = None,
                       page_list: Optional[list] = None, dpi: int = 110,
                       on_page=None) -> dict:
    """导出中文 / 双语 PDF。kind ∈ {cn, bilingual}。

    `pages` 语义见 `normalize_pages()`：`None`=全书，`int`=单页，`(from,to)` 二元组=区间，
    `[...]` 列表=显式页列表；也可用 `page_list=` 显式传页列表（优先级更高）。
    """
    b = version_bundle(conn, version_id)
    v = b["version"]
    if not b["src_pdf"] or not Path(b["src_pdf"]).exists():
        raise FileNotFoundError(f"版本源 PDF 不存在：{b['src_pdf']}")
    if out_path is None:
        name = f"{v.get('doc_id')}_{v.get('label') or version_id}_{kind}.pdf"
        out_path = str(derived_dir("exports") / name)
    out_path = str(Path(out_path).resolve())
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fn = "export_cn_pdf" if kind == "cn" else "export_bilingual_pdf"
    plist = page_list if page_list is not None else normalize_pages(
        pages, int(v.get("page_count") or 0))
    # 中文目录：源 PDF（v2 demo）自身没有书签，用**上一版本书签的结构** +
    # **当前版本段的中文译文**合成（见 render.pdfout.build_cn_outline）。
    cn_toc = None
    try:
        versions = _store().list_versions(conn, doc_id)
        idx = next((i for i, x in enumerate(versions)
                    if int(x["version_id"]) == int(version_id)), None)
        prev_pdf = None
        if idx is not None and idx > 0:
            prev_pdf = _abs_path(versions[idx - 1].get("source_path") or "")
        # TO 手册这类源文件**自带原生书签**（TO-1 561 条 / TO-34 1093 条）时，
        # 直接用它自己的书签作结构参考，质量比"用上一版"更好。
        cn_toc = _call_pdfout_fn("build_cn_outline", conn, version_id,
                                 prev_pdf=prev_pdf, same_pdf=b["src_pdf"])
    except Exception as exc:
        print(f"[pipeline] 生成中文目录失败（导出继续，无书签）: {type(exc).__name__}: {exc}")
    t0 = time.time()
    res = _call_pdfout(fn, b["src_pdf"], out_path, b["by_page"], b["translations"],
                       b["translation_rows"], pages=plist, dpi=dpi, on_page=on_page,
                       cn_toc=cn_toc)
    res = dict(res or {})
    res.setdefault("path", out_path)
    res["seconds"] = round(time.time() - t0, 2)
    res["kind"] = kind
    try:
        store = _store()
        ph = hashlib.blake2b(f"{kind}|{pages}|{dpi}|{len(b['translations'])}".encode(),
                             digest_size=8).hexdigest()
        store.upsert_render_snapshot(conn, version_id=version_id, kind=kind, path=out_path,
                                     params_hash=ph, stale=0,
                                     bytes_=os.path.getsize(out_path) if os.path.exists(out_path) else 0)
    except Exception:
        pass
    return res


def render_page_to_file(conn, doc_id: int, version_id: int, page: int, *, kind: str = "source",
                        out_path: str = "", dpi: int = 110) -> str:
    """渲染单页：kind=source 直接位图；cn/bilingual 走重建页。"""
    b = version_bundle(conn, version_id)
    src = b["src_pdf"]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "source":
        with pymupdf.open(src) as doc:
            pix = doc[page - 1].get_pixmap(dpi=dpi)
            if out.suffix.lower() in (".png",):
                pix.save(str(out))
            elif out.suffix.lower() in (".webp",):
                pix.save(str(out), output="webp")
            else:
                pix.save(str(out))
        return str(out)
    if out.suffix.lower() in (".png", ".webp"):
        pb = _render_pagebuild()
        with pymupdf.open(src) as doc:
            built = pb.build_translated_page(
                doc, page, b["by_page"].get(page, []), b["translations"],
                cjk_font_path=_cjk_font())
            pix = built.get_pixmap(dpi=dpi)
            pix.save(str(out))
        return str(out)
    res = export_version_pdf(conn, doc_id, version_id, kind=kind, out_path=str(out),
                             pages=page, dpi=dpi)
    return res.get("path", str(out))


# --------------------------------------------------------------------------------------
# 新版 PDF 模拟（simulate-update）
# --------------------------------------------------------------------------------------

_CALIBRI = r"C:\Windows\Fonts\calibri.ttf"
_CALIBRI_BOLD = r"C:\Windows\Fonts\calibrib.ttf"

SIM_NEW_PARAGRAPH = ("NOTE: Before the first flight of the day, verify that the EPU "
                     "battery charge indicator reads fully charged. If the indicator is "
                     "below the green band, notify maintenance before engine start.")
SIM_DELETE_NOTE = "已在 v2 中删除该段（redact 后不再补写）"
SIM_MOVE_NOTE = "整段从第 {src} 页搬到第 {dst} 页（文本不变，用于验证 MOVED 识别）"


def _text_blocks_with_bboxes(page) -> list[tuple]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") == 0 and _spans_of_block(b):
            out.append(tuple(round(v, 2) for v in b["bbox"]))
    return out


def free_bands(page, *, min_height: float = 72.0) -> list[tuple]:
    """在正文区找完整空白的垂直带，用于插入新段落。"""
    x0c, x1c = 50.0, 545.0
    y0c, y1c = HEADER_BAND_Y + 2, FOOTER_BAND_Y - 6
    occ: list[tuple] = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") == 0 and not _spans_of_block(b):
            continue
        bx = b["bbox"]
        if bx[2] > x0c and bx[0] < x1c:
            occ.append((bx[1], bx[3]))
    for info in page.get_image_info():
        bx = info["bbox"]
        if bx[2] > x0c and bx[0] < x1c:
            occ.append((bx[1], bx[3]))
    try:
        for d in page.get_drawings():
            r = d["rect"]
            if r.x1 > x0c and r.x0 < x1c and (r.y1 - r.y0) > 1.0:
                occ.append((r.y0, r.y1))
    except Exception:
        pass
    occ = sorted((max(a, y0c), min(b, y1c)) for a, b in occ if b > y0c and a < y1c)
    merged: list[list[float]] = []
    for a, b in occ:
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    bands: list[tuple] = []
    cur = y0c
    for a, b in merged:
        if a - cur >= min_height:
            bands.append((cur, a))
        cur = max(cur, b)
    if y1c - cur >= min_height:
        bands.append((cur, y1c))
    return bands


def _page_par_blocks(page) -> list[dict]:
    """页面正文段（内部 _Par）+ 相邻约束，供编辑使用。"""
    pars = page_paragraphs(page, page.number + 1)
    return [p for p in pars if p.role == "body" and p.kind in ("paragraph", "list_item", "heading")]


def _next_par_y0(pars: list[_Par], target: _Par) -> float:
    cands = [p.bbox[1] for p in pars if p.bbox[1] > target.bbox[1] + 2 and p.bbox[0] < target.bbox[2]]
    return min(cands) if cands else FOOTER_BAND_Y - 6


def _redact(page, rect) -> None:
    page.add_redact_annot(rect, fill=(1, 1, 1))
    kw = {}
    for name, val in (("images", getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)),
                      ("graphics", getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0)),
                      ("text", getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0))):
        kw[name] = val
    try:
        page.apply_redactions(**kw)
    except TypeError:
        page.apply_redactions()


def _insert_paragraph(page, rect, text: str, *, size: float = 10.0, bold: bool = False,
                      color: int = 0, lineheight: float = 1.34) -> float:
    """在 rect 内写一段文字；返回剩余高度（负数表示没放下）。"""
    fontfile = _CALIBRI_BOLD if bold else _CALIBRI
    kw = dict(fontsize=size, fontname="Calibri" if not bold else "Calibri-Bold",
              fontfile=fontfile, color=int_to_rgb(color), align=0)
    try:
        return page.insert_textbox(pymupdf.Rect(rect), text, lineheight=lineheight, **kw)
    except TypeError:
        return page.insert_textbox(pymupdf.Rect(rect), text, **kw)


def _find_paragraph(doc, *, pages: tuple, min_len: int = 140, min_lines: int = 2,
                    max_x1: float = 456.0, accept: Optional[Callable[[str], bool]] = None,
                    used: set | None = None) -> Optional[tuple]:
    """在给定页区间里找一个「可编辑」的正文段落，返回 (page_no, _Par)。

    accept(text) 为可选谓词：只有能施加目标改写的段落才会被选中（避免 3 个修改计划
    全部落在同一个段落上）。
    """
    used = used or set()
    for pno in range(pages[0], pages[1] + 1):
        page = doc[pno - 1]
        pars = _page_par_blocks(page)
        for p in sorted(pars, key=lambda x: (x.bbox[1], x.bbox[0])):
            if p.kind == "heading" or p.role != "body":
                continue
            key = (pno, round(p.bbox[1], 1))
            if key in used:
                continue
            if len(p.text) < min_len or p.n_lines < min_lines:
                continue
            if p.bbox[2] > max_x1 or p.bbox[0] > 90:
                continue
            # 段落区域不得被图片/图形覆盖
            area = pymupdf.Rect(p.bbox)
            covered = False
            for info in page.get_image_info():
                if pymupdf.Rect(info["bbox"]).intersects(area):
                    covered = True
                    break
            if covered:
                continue
            if accept is not None and not accept(p.text):
                continue
            return pno, p
    return None


def _rewrite_two_words(text: str) -> Optional[tuple]:
    """只改两个词（保持高相似度）。返回 (new_text, old_words, new_words)。"""
    for a, b in ((" warning ", " caution "), (" aircraft ", " airframe "),
                 (" engine ", " motor "), (" landing ", " approach "),
                 (" immediately ", " promptly ")):
        if a in text:
            return text.replace(a, b, 1), a.strip(), b.strip()
    return None


def _rewrite_sentence(text: str) -> Optional[tuple]:
    """整句重写：替换最后一句。返回 (new_text, old_sentence, new_sentence)。"""
    m = list(re.finditer(r"[.!?]\s+", text))
    if len(m) < 2:
        return None
    cut = m[-1].end()
    old = text[cut:].strip()
    if len(old) < 30:
        return None
    new = ("In this revision the procedure has been rewritten: set the mode switch to "
           "the appropriate position, wait for the alignment to complete, and only then "
           "select the destination steerpoint.")
    return text[:cut] + new, old, new


def _rewrite_number_unit(text: str) -> Optional[tuple]:
    """改数字/单位。"""
    m = UNIT_NUM_RE.search(text)
    if not m:
        return None
    old = m.group(0)
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2)
    new_val = val * 1.2 if val >= 100 else val + 5
    if float(new_val).is_integer():
        new_str = f"{int(new_val):,}" if val >= 1000 else str(int(new_val))
    else:
        new_str = f"{new_val:.1f}"
    new = f"{new_str} {unit}"
    return text.replace(old, new, 1), old, new


def simulate_update_pdf(src_pdf: str | os.PathLike, out_pdf: str | os.PathLike, *,
                        page_from: int = 4, page_to: int = 40,
                        progress: Optional[Callable[[str], None]] = None) -> dict:
    """生成「新版」PDF：复制前 40 页（其余原样），在正文层施加可验证的改动。

    改动集（与 task-8 要求一一对应）：
      * removed  x2 —— redact 后不补写
      * added    x1 —— 在空白带 insert_textbox 全新段落
      * modified x3 —— (a) 只改 2 个词 (b) 整句重写 (c) 改数字/单位
      * moved    x1 —— 第 N 页整段搬到第 N+1 页

    返回 manifest dict（同时写出 data/derived/simulate_update_manifest.json）。
    """
    src_pdf = str(Path(src_pdf).resolve())
    out_pdf = str(Path(out_pdf).resolve())
    if Path(src_pdf).resolve() == Path(out_pdf).resolve():
        raise RuntimeError("输出文件不能覆盖原始 PDF（origin 只读）")
    page_to = max(page_from + 2, min(page_to, 40))

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    edits: list[dict] = []
    with pymupdf.open(src_pdf) as src:
        if src.page_count < page_to:
            raise RuntimeError(f"源 PDF 只有 {src.page_count} 页，无法取前 {page_to} 页")
        out = pymupdf.open()
        out.insert_pdf(src, from_page=0, to_page=page_to - 1)
        out.insert_pdf(src, from_page=page_to, to_page=src.page_count - 1)

        edits = _plan_edits(out, src, page_from, page_to)
        log(f"[模拟] 计划改动 {len(edits)} 处：" + ", ".join(
            f"p{e['page']}#{e['type']}" for e in edits))

        # 逐页快照「未被编辑的段落」——PyMuPDF 的 redaction 是按**整块/整对象**删除文字的，
        # 同一个 PDF block 里的相邻段落会被一起抹掉（实测 p7：删一段把下一段也删了）。
        # 所以编辑完一页后必须校验，缺了就用 insert_textbox 补回去。
        edited_pages = sorted({e["page"] for e in edits})
        targets: dict[int, list[tuple]] = {}
        for e in edits:
            targets.setdefault(e["page"], []).append(tuple(e["_rect"]))
        snapshots: dict[int, list[_Par]] = {}
        for pno in edited_pages:
            page = out[pno - 1]
            keep = []
            for par in _page_par_blocks(page):
                if par.role != "body" or not par.text.strip():
                    continue
                if any(not (par.bbox[2] < r[0] or par.bbox[0] > r[2]
                            or par.bbox[3] < r[1] or par.bbox[1] > r[3]) for r in targets[pno]):
                    continue          # 本段就是编辑目标
                keep.append(par)
            snapshots[pno] = keep

        for i, e in enumerate(edits, 1):
            page = out[e["page"] - 1]
            rect = pymupdf.Rect(e["_rect"])
            if not e.get("_no_redact"):
                _redact(page, rect)
            if e["new_text"]:
                # 允许向下扩张到下一个段落的 y0 之前
                limit = e.get("_limit_y", rect.y1 + 4)
                ins = pymupdf.Rect(rect.x0, rect.y0 - 1.5,
                                   max(rect.x1 + 2, rect.x0 + 160), min(limit - 2, rect.y1 + 6))
                size = e.get("_size", 10.0)
                rc = _insert_paragraph(page, ins, e["new_text"], size=size, bold=bool(e.get("_bold")),
                                       color=int(e.get("_color", 0)))
                tries = 0
                while rc < 0 and tries < 6:
                    tries += 1
                    ins = pymupdf.Rect(rect.x0, rect.y0 - 1.5, max(rect.x1 + 2, rect.x0 + 160),
                                       min(limit - 2.0, ins.y1 + 26))
                    size = max(8.5, size - 0.4)
                    rc = _insert_paragraph(page, ins, e["new_text"], size=size,
                                           bold=bool(e.get("_bold")), color=int(e.get("_color", 0)))
                e["_fit"] = {"remain": round(float(rc), 2), "size": round(size, 2),
                             "rect": [round(v, 2) for v in ins]}
                if rc < 0:
                    log(f"[模拟] ⚠ 第 {i} 处 (#{e['type']} p{e['page']}) 文本未完全放入")
            shown = (e["new_text"] or "")[:44] if e["type"] in ("added", "added_move") else e["old_text"][:52]
            log(f"[模拟] {i}/{len(edits)} {e['type']:8s} p{e['page']:>3} {shown!r}")

        # —— 校验 + 修复：同 block 的相邻段被 redaction 连带删除时补写回去
        repaired: list[dict] = []
        for pno in edited_pages:
            page = out[pno - 1]
            page_txt = normalize(page.get_text())
            for par in snapshots.get(pno, []):
                head = normalize(par.text)[:60]
                if head and head in page_txt:
                    continue
                rect = pymupdf.Rect(par.bbox[0] - 1.0, par.bbox[1] - 1.5,
                                    par.bbox[2] + 2.0, par.bbox[3] + 4.0)
                _insert_paragraph(page, rect, par.text, size=par.size, bold=par.bold,
                                  color=par.color)
                repaired.append({"page": pno, "reason": "redaction 连带删除，已补写",
                                 "text": par.text[:120], "bbox": [round(v, 2) for v in par.bbox]})
                log(f"[模拟] ⚠ 修复：p{pno} 段落被连带删除，已补写 {par.text[:40]!r}")
        out.set_metadata(dict(src.metadata or {}))
        out.set_metadata({"title": f"{src.metadata.get('title') or 'BMS Training Manual'} "
                                   f"(rev {_bump_release(src)})",
                          "producer": "manual_trans_trace simulate-update"})
        # 先写临时文件再原子替换：目标 PDF 可能被正在运行的 Web 服务/渲染进程短暂占用
        # （实测：服务端按需渲染页面时会持有文件句柄数秒到数十秒，直接 save 会 Access Denied）
        tmp_out = out_pdf + ".tmp"
        out.save(tmp_out, garbage=3, deflate=True)
        out.close()
        told = False
        for attempt in range(60):
            try:
                os.replace(tmp_out, out_pdf)
                break
            except PermissionError:
                if not told:
                    told = True
                    log(f"[模拟] {Path(out_pdf).name} 被其它进程占用，等待写入…"
                        f"（可先停掉 python cli.py serve）")
                if attempt == 59:
                    raise
                time.sleep(1.0)

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_pdf": os.path.relpath(src_pdf, ROOT).replace("\\", "/"),
        "output_pdf": os.path.relpath(out_pdf, ROOT).replace("\\", "/"),
        "source_sha256": sha256_file(src_pdf),
        "output_sha256": sha256_file(out_pdf),
        "pages_total": page_to,
        "pages_edited": sorted({e["page"] for e in edits}),
        "expected_counts": {
            "modified": sum(1 for e in edits if e["type"] == "modified"),
            "added": sum(1 for e in edits if e["type"] == "added"),
            "removed": sum(1 for e in edits if e["type"] == "removed"),
            "moved": sum(1 for e in edits if e["type"] == "moved"),
        },
        "repaired_paragraphs": repaired,
        "edits": [{**{k: v for k, v in e.items() if not k.startswith("_")},
                   **({"fit": e["_fit"]} if "_fit" in e else {})} for e in edits],
    }
    mpath = derived_dir() / "simulate_update_manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(mpath)
    return manifest


def _bump_release(src) -> str:
    meta = src.metadata or {}
    m = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", f"{meta.get('title') or ''} {src.name}")
    if not m:
        return "v2"
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"


def _plan_edits(out, src, page_from: int, page_to: int) -> list[dict]:
    """挑选并规划 7 处改动（确定性：同样输入 → 同样改动）。

    期望计数：removed=2, modified=3, added=1, moved=1（moved 由「源页删除 + 目标页写入」
    两条 PDF 操作实现，但只算 1 处逻辑改动）。
    """
    used: set = set()
    edits: list[dict] = []

    def par_rect(p, page_no) -> tuple:
        page = out[page_no - 1]
        pars = _page_par_blocks(page)
        return pymupdf.Rect(p.bbox[0] - 1.2, p.bbox[1] - 1.2, p.bbox[2] + 1.6, p.bbox[3] + 1.6), \
            _next_par_y0(pars, p)

    def add_target_edit(e: dict, p, pno: int) -> None:
        rect, limit = par_rect(p, pno)
        used.add((pno, round(p.bbox[1], 1)))
        e.update({"_rect": [rect.x0, rect.y0, rect.x1, rect.y1], "_limit_y": limit,
                  "_size": p.size, "_bold": p.bold, "_color": p.color})
        e["index"] = len(edits) + 1
        edits.append(e)

    # (1)(2) 删除 2 段（不同页，避免相邻段互相影响）
    del_plan = [(page_from, page_from + 9), (page_from + 10, page_from + 24)]
    for lo, hi in del_plan:
        found = _find_paragraph(out, pages=(lo, min(hi, page_to)), min_len=150,
                                min_lines=3, used=used)
        if not found:
            found = _find_paragraph(out, pages=(page_from, page_to), min_len=150,
                                    min_lines=3, used=used)
        if not found:
            break
        pno, p = found
        add_target_edit({"type": "removed", "page": pno, "old_text": p.text,
                         "new_text": None, "note": SIM_DELETE_NOTE}, p, pno)

    # (3)(4)(5) 修改 3 段：只改两词 / 整句重写 / 改数字单位
    plans = [
        ("two_words", _rewrite_two_words, "只改 2 个词（验证高相似度 → MODIFIED）",
         (page_from + 6, page_from + 16)),
        ("sentence", _rewrite_sentence, "整句重写（保留同段其余句子）",
         (page_from + 16, page_from + 30)),
        ("number_unit", _rewrite_number_unit, "修改数字/单位（如 250 knots → 300 knots）",
         (page_from, page_to)),
    ]
    for tag, fn, note, (lo, hi) in plans:
        found = None
        for window in ((lo, min(hi, page_to)), (page_from, page_to)):
            found = _find_paragraph(out, pages=window, min_len=140, min_lines=2, used=used,
                                    accept=lambda t, _fn=fn: _fn(t) is not None)
            if found:
                break
        if not found:
            continue
        pno, p = found
        rw = fn(p.text)
        if not rw:
            continue
        new_text, old_frag, new_frag = rw
        add_target_edit({"type": "modified", "page": pno, "variant": tag,
                         "old_text": p.text, "new_text": new_text,
                         "old_fragment": old_frag, "new_fragment": new_frag,
                         "note": note}, p, pno)

    # (6) 跨页移动：第 N 页整段 → 第 N+1 页空白带（目标页必须有足够空白）
    move_done = False
    for pno in range(page_from + 2, page_to):
        found = _find_paragraph(out, pages=(pno, pno), min_len=120, min_lines=2, used=used)
        if not found:
            continue
        p_page, p = found
        bands = free_bands(out[p_page], min_height=80)  # 0-based → 下一页
        if not bands:
            continue
        rect, limit = par_rect(p, p_page)
        used.add((p_page, round(p.bbox[1], 1)))
        band = bands[0]
        ty0 = band[0] + 12.0
        edits.append({
            "index": len(edits) + 1, "type": "moved", "page": p_page, "page_to": p_page + 1,
            "old_text": p.text, "new_text": None,
            "note": SIM_MOVE_NOTE.format(src=p_page, dst=p_page + 1),
            "_rect": [rect.x0, rect.y0, rect.x1, rect.y1], "_limit_y": limit,
            "_size": p.size, "_bold": p.bold, "_color": p.color,
        })
        edits.append({
            "index": len(edits) + 1, "type": "added_move", "page": p_page + 1,
            "move_of_page": p_page, "old_text": "", "new_text": p.text,
            "note": f"被搬来的段落（原第 {p_page} 页，文本不变 → 应识别为 MOVED）",
            "_rect": [p.bbox[0], ty0 - 8, p.bbox[2], max(band[1], ty0 + 10)],
            "_limit_y": band[1], "_size": p.size, "_bold": p.bold, "_color": p.color,
            "_no_redact": True,
        })
        move_done = True
        break
    if not move_done:
        raise RuntimeError("未找到可跨页搬移的段落（需要目标页有 ≥80pt 空白带）")

    # (7) 插入 1 段全新文本（空白带，与上下段至少留 12pt 间距 → 会被切成独立段）
    inserted = False
    for pno in range(page_to, page_from, -1):
        for band in free_bands(out[pno - 1], min_height=88):
            edits.append({
                "index": len(edits) + 1, "type": "added", "page": pno,
                "old_text": "", "new_text": SIM_NEW_PARAGRAPH,
                "note": "全新插入的段落（insert_textbox 到空白带）",
                "_rect": [BODY_COLUMN_LEFT, band[0] + 12, 545.0, max(band[0] + 30, band[1])],
                "_limit_y": band[1], "_size": 10.0, "_bold": False, "_color": 0,
                "_no_redact": True,
            })
            inserted = True
            break
        if inserted:
            break
    if not inserted:
        raise RuntimeError("未找到可用于插入新段落的空白带")

    edits.sort(key=lambda e: (e["page"], e["type"]))
    for i, e in enumerate(edits, 1):
        e["index"] = i
    return edits


# --------------------------------------------------------------------------------------
# 便捷入口
# --------------------------------------------------------------------------------------

def open_ingested(pdf_path: str | os.PathLike, *, slug=None, label=None):
    """独立使用：建库并 ingest，返回 (conn, info)。"""
    conn = connect()
    info = ingest_pdf(conn, pdf_path, slug=slug, label=label)
    return conn, info


if __name__ == "__main__":  # pragma: no cover - 手工自检
    d = pymupdf.open(str(ORIGIN_DIR / "BMS-Training-Manual.pdf"))
    for pno in (2, 21):
        segs = extract_segments(d, pno)
        print(f"--- page {pno}: {len(segs)} segments")
        for s in segs[:12]:
            print(f"  [{s.role}/{s.kind}] {s.text[:70]!r}")
    d.close()
