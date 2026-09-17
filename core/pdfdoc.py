"""PDF 几何与排版工具（CONTRACT.md §6 的 core 部分）。

字体度量全部走**真实字体文件**（`pymupdf.Font(fontfile=...).text_length`），
Font 对象用 lru_cache 复用，避免每个字符重新加载 TTF。
`char_width` / `mixed_text_width` / `layout_mixed` 三者用**同一套逐字符 advance**，
所以 layout 报出的宽度与 mixed_text_width 完全一致，不会出现"量出来没超、画出来超"。
"""
from __future__ import annotations

import os
import re
import statistics
from functools import lru_cache
from typing import Any, Iterable, Optional, Sequence

import pymupdf

from core import fingerprint as fp

__all__ = [
    "KNOWN_CJK",
    "KNOWN_LATIN",
    "open_pdf",
    "page_size",
    "resolve_cjk_font",
    "resolve_latin_font",
    "char_width",
    "mixed_text_width",
    "layout_mixed",
    "paragraph_kind",
    "is_banner_image",
    "spans_text",
    "fonts_summary",
    "style_of",
]

KNOWN_CJK = ["C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/simhei.ttf"]
KNOWN_LATIN = {
    "regular": "C:/Windows/Fonts/calibri.ttf",
    "bold": "C:/Windows/Fonts/calibrib.ttf",
    "italic": "C:/Windows/Fonts/calibrii.ttf",
    "serif": "C:/Windows/Fonts/times.ttf",
}

# resolve_cjk_font 的备用候选（契约常量之外的兜底，找不到才抛 RuntimeError）
_CJK_FALLBACKS = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/Dengb.ttf"]

# 中文标点里不属于 CJK 区间、但在中文字体下是等宽全角的那些
_CJK_PUNCT = set("—…“”‘’《》〈〉「」『』【】〔〕·°※")

_HEADER_BAND = 0.08   # 页眉带（相对页高）
_FOOTER_BAND = 0.92   # 页脚带
_TAB_GAP = 14.0       # 同基线内 x 间隙 > 14pt 视为分栏
_BIG_GAP = 40.0       # 单行内的超宽间隙

_BULLET_RE = re.compile(r"^(?:[-\u2022\u25aa\u25c6\u203b\u00b7\u2023\u2043\u25e6*]|\(?\d{1,3}[.)]|[a-z][.)])\s+")
_MULTI_NUM_RE = re.compile(r"^\d+(?:\.\d+)+\.?\s")
_PAGE_NUM_RE = re.compile(r"^[\[\(]?\s*[-\u2013\u2014]?\s*\d{1,4}\s*[-\u2013\u2014]?\s*[\]\)]?$")
_CAPTION_RE = re.compile(r"^(?:fig(?:ure)?|tab(?:le)?|exhibit|appendix|\u56fe|\u8868|\u56fe\u8868)\s*[-.\u3002\uff1a:]?\s*\d+", re.I)
_SENT_END = re.compile(r"[.:;\u3002\uff1a\uff1b]\s*$")


# --------------------------------------------------------------------------- #
# 字体度量
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=64)
def _font(path: str) -> "pymupdf.Font":
    """加载并缓存 Font（同一个 TTF 只解析一次）。"""
    return pymupdf.Font(fontfile=path)


@lru_cache(maxsize=1)
def _latin_regular() -> str:
    return resolve_latin_font("regular")


def _latin_path(bold: bool) -> str:
    try:
        return resolve_latin_font("bold" if bold else "regular")
    except RuntimeError:
        return _latin_regular()


def _resolve_first(candidates: Sequence[str], what: str) -> str:
    for path in candidates:
        if path and os.path.isfile(path):
            return path.replace("\\", "/")
    raise RuntimeError(f"no usable {what} font found, tried: {list(candidates)}")


def resolve_cjk_font() -> str:
    """返回可用的中文字体文件路径（Deng.ttf → simhei.ttf → 兜底），找不到抛 RuntimeError。"""
    return _resolve_first([*KNOWN_CJK, *_CJK_FALLBACKS], "CJK")


def resolve_latin_font(style: str = "regular") -> str:
    """拉丁字体路径；缺失时回落到 regular / serif / 系统首个可用项。"""
    order = [style, "regular", "bold", "italic", "serif"]
    tried: list[str] = []
    for key in order:
        path = KNOWN_LATIN.get(key)
        if path and path not in tried:
            tried.append(path)
    try:
        return _resolve_first(tried, "latin")
    except RuntimeError:
        return _resolve_first([*KNOWN_CJK, *_CJK_FALLBACKS], "fallback")


_ESTIMATE = {"cjk": 1.0, "digit": 0.55, "space": 0.28, "punct": 0.35, "latin": 0.5}


def _estimate_width(size: float, ch: str) -> float:
    """字体里没有该字形时的保守估计（宁可宽一点，避免溢出）。"""
    kind = fp.char_kind(ch)
    if kind == "latin":
        factor = 0.62 if ch.isupper() or ch in "mwMW" else 0.5
    else:
        factor = _ESTIMATE.get(kind, 1.0 if kind == "cjk" else 0.5)
    return size * factor


@lru_cache(maxsize=1 << 17)
def _advance(path: str, size: float, ch: str) -> float:
    """单字符 advance 宽度（pt），带缓存。"""
    if not ch:
        return 0.0
    try:
        width = float(_font(path).text_length(ch, fontsize=size))
        if width > 0.0:
            return width
    except Exception:
        pass
    if ch.isspace():
        return size * _ESTIMATE["space"]
    return _estimate_width(size, ch)


def _slot_of(ch: str, bold: bool = False) -> str:
    """字符 → 字体槽位：cjk / latin / latin_bold。"""
    kind = fp.char_kind(ch)
    if kind == "cjk" or (kind == "punct" and ch in _CJK_PUNCT):
        return "cjk"
    return "latin_bold" if bold else "latin"


def _path_of(ch: str, bold: bool) -> str:
    if _slot_of(ch, bold) == "cjk":
        return resolve_cjk_font()
    return _latin_path(bold)


def char_width(font: Any, size: float, ch: str) -> float:
    """真实字体度量：`font` 可以是 Font 对象、字体文件路径，或 None（用 Calibri）。"""
    if not ch:
        return 0.0
    try:
        fs = float(size)
    except (TypeError, ValueError):
        return 0.0
    if fs <= 0:
        return 0.0
    if isinstance(font, str):
        return sum(_advance(font, fs, c) for c in ch)
    if font is None:
        return sum(_advance(_latin_path(False), fs, c) for c in ch)
    try:
        return float(font.text_length(ch, fontsize=fs))
    except Exception:
        return sum(_advance(_latin_path(False), fs, c) for c in ch)


def mixed_text_width(text: str, fs: float, bold: bool = False) -> float:
    """中英混排宽度（CJK 用 Deng.ttf，拉丁/数字用 Calibri，逐字符 advance 求和）。"""
    if not text:
        return 0.0
    try:
        size = float(fs)
    except (TypeError, ValueError):
        return 0.0
    if size <= 0:
        return 0.0
    cache: dict[str, float] = {}
    total = 0.0
    cjk_path = resolve_cjk_font()
    latin_path = _latin_path(bold)
    for ch in text:
        key = ch
        if key not in cache:
            path = cjk_path if _slot_of(ch, bold) == "cjk" else latin_path
            cache[key] = _advance(path, size, ch)
        total += cache[key]
    return total


# --------------------------------------------------------------------------- #
# 贪心断行
# --------------------------------------------------------------------------- #
def _build_atoms(text: str, fs: float, bold: bool, limit: float) -> list[tuple[str, float, bool, bool]]:
    """切成原子：(文本, 宽度, 是否空白, 是否含 CJK)。

    CJK 单字成原子（任意位置可断），拉丁/数字/ASCII 标点按"词"聚成原子（优先在空格断）。
    """
    atoms: list[tuple[str, float, bool, bool]] = []
    buf: list[str] = []
    buf_w = 0.0
    buf_slot: Optional[str] = None
    cjk_path = resolve_cjk_font()
    latin_path = _latin_path(bold)

    def flush() -> None:
        nonlocal buf, buf_w, buf_slot
        if buf:
            piece = "".join(buf)
            atoms.append((piece, buf_w, False, buf_slot == "cjk"))
        buf, buf_w, buf_slot = [], 0.0, None

    for ch in text:
        kind = fp.char_kind(ch)
        if kind == "space":
            flush()
            atoms.append((ch, _advance(cjk_path if ch == "\u3000" else latin_path, fs, ch), True, False))
            continue
        slot = _slot_of(ch, bold)
        if buf and slot != buf_slot:
            flush()
        buf_slot = slot
        buf.append(ch)
        buf_w += _advance(cjk_path if slot == "cjk" else latin_path, fs, ch)
    flush()

    if limit == float("inf"):
        return atoms
    # 单词本身超宽 → 硬断，保证任何一行都不超过 max_width
    out: list[tuple[str, float, bool, bool]] = []
    for a_text, a_w, is_space, has_cjk in atoms:
        if is_space or a_w <= limit:
            out.append((a_text, a_w, is_space, has_cjk))
            continue
        piece: list[str] = []
        piece_w = 0.0
        for ch in a_text:
            w = _advance(cjk_path if _slot_of(ch, bold) == "cjk" else latin_path, fs, ch)
            if piece and piece_w + w > limit:
                out.append(("".join(piece), piece_w, False, has_cjk))
                piece, piece_w = [], 0.0
            piece.append(ch)
            piece_w += w
        if piece:
            out.append(("".join(piece), piece_w, False, has_cjk))
    return out


def layout_mixed(
    text: str, fs: float, max_width: float, bold: bool = False
) -> list[tuple[str, str, float]]:
    """贪心断行 + 每行字体槽位。返回 [(line_text, font_slot, width)]。

    * `font_slot` ∈ {"cjk","latin","latin_bold"}：行内出现任何 CJK（含中文标点）即 "cjk"；
    * 宽度是真实混合度量，保证 `width <= max_width`（除非 max_width <= 0 视为不限宽）；
    * 空文本 / fs <= 0 → `[]`（渲染层直接跳过，不画空行）。
    """
    if not text or not text.strip():
        return []
    try:
        size = float(fs)
    except (TypeError, ValueError):
        return []
    if size <= 0:
        return []
    limit = float("inf")
    try:
        mw = float(max_width)
        if mw > 0:
            limit = mw
    except (TypeError, ValueError):
        pass

    lines: list[tuple[str, str, float]] = []
    cur: list[str] = []
    cur_w = 0.0
    cur_cjk = False

    def emit(has_cjk: bool) -> None:
        nonlocal cur, cur_w, cur_cjk
        line = "".join(cur).rstrip()
        if line:
            slot = "cjk" if (has_cjk or any(_slot_of(c, bold) == "cjk" for c in line)) else (
                "latin_bold" if bold else "latin"
            )
            lines.append((line, slot, mixed_text_width(line, size, bold)))
        cur, cur_w, cur_cjk = [], 0.0, False

    for a_text, a_w, is_space, has_cjk in _build_atoms(text, size, bold, limit):
        if not cur and is_space:
            continue                      # 行首空白丢弃
        if cur and cur_w + a_w > limit:
            emit(cur_cjk)
            if is_space:
                continue                  # 断行处的空格丢弃
        if is_space:
            cur.append(a_text)
            cur_w += a_w
        else:
            cur.append(a_text)
            cur_w += a_w
            cur_cjk = cur_cjk or has_cjk
    emit(cur_cjk)
    return lines


# --------------------------------------------------------------------------- #
# span 启发式
# --------------------------------------------------------------------------- #
def _sget(span: Any, key: str, default: Any = None) -> Any:
    """span 既可能是 pymupdf dict，也可能是 `models.Span`。"""
    if span is None:
        return default
    if isinstance(span, dict):
        return span.get(key, default)
    return getattr(span, key, default)


def _bbox_of(span: Any) -> list[float]:
    bbox = _sget(span, "bbox", None)
    if not bbox:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        values = [float(v) for v in bbox[:4]]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]
    while len(values) < 4:
        values.append(0.0)
    return values


def _is_bold(span: Any) -> bool:
    if int(_sget(span, "flags", 0) or 0) & 16:   # pymupdf: bit4 = bold
        return True
    name = str(_sget(span, "font", "") or "").lower()
    return "bold" in name or "black" in name or "hebo" in name


def _is_italic(span: Any) -> bool:
    if int(_sget(span, "flags", 0) or 0) & 2:    # bit1 = italic
        return True
    name = str(_sget(span, "font", "") or "").lower()
    return "italic" in name or "oblique" in name


def _clean_spans(spans: Optional[Iterable[Any]]) -> list[Any]:
    """过滤纯空白 span（§3.1.2：真实 PDF 里大量空白 span 会撑宽 bbox）。"""
    out = []
    for span in spans or []:
        text = _sget(span, "text", "") or ""
        if text.strip():
            out.append(span)
    return out


def spans_text(spans: Optional[Iterable[Any]]) -> str:
    """拼出 span 文本（不做连字符处理），空白折叠为单空格。"""
    items = _clean_spans(spans)
    return re.sub(r"\s+", " ", "".join(_sget(s, "text", "") or "" for s in items)).strip()


def fonts_summary(spans: Optional[Iterable[Any]]) -> list[dict]:
    """→ Segment.fonts 格式：[{font,size,color,flags,count}]（count = 字符数，按数量降序）。"""
    buckets: dict[tuple, dict] = {}
    for span in _clean_spans(spans):
        text = _sget(span, "text", "") or ""
        key = (
            str(_sget(span, "font", "") or ""),
            round(float(_sget(span, "size", 0.0) or 0.0), 1),
            int(_sget(span, "color", 0) or 0),
            int(_sget(span, "flags", 0) or 0),
        )
        item = buckets.get(key)
        if item is None:
            buckets[key] = {"font": key[0], "size": key[1], "color": key[2], "flags": key[3],
                            "count": len(text)}
        else:
            item["count"] += len(text)
    return sorted(buckets.values(), key=lambda d: (-d["count"], d["font"]))


def style_of(
    spans: Optional[Iterable[Any]],
    *,
    page_width: Optional[float] = None,
    left_margin: Optional[float] = None,
) -> dict:
    """→ Segment.style：{bold,italic,size,color,align,list_level,indent}。"""
    items = _clean_spans(spans)
    if not items:
        return {"bold": False, "italic": False, "size": 0.0, "color": 0,
                "align": "left", "list_level": 0, "indent": 0.0}
    sizes = [float(_sget(s, "size", 0.0) or 0.0) for s in items]
    colors: dict[int, int] = {}
    for span in items:
        color = int(_sget(span, "color", 0) or 0)
        colors[color] = colors.get(color, 0) + len(_sget(span, "text", "") or "")
    xs0 = min(_bbox_of(s)[0] for s in items)
    xs1 = max(_bbox_of(s)[2] for s in items)
    text = spans_text(items)
    align = "left"
    if page_width and page_width > 0:
        width = max(xs1 - xs0, 0.001)
        left_gap, right_gap = xs0, float(page_width) - xs1
        if abs(left_gap - right_gap) <= 8.0 and left_gap > 40.0:
            align = "center"
        elif right_gap <= 6.0 and int(round(max(sizes) if sizes else 0)) >= 9 and left_gap > 20.0:
            align = "justify"      # 右边贴边：可能是两端对齐的正文行
    lead_spaces = len(text) - len(text.lstrip(" "))
    margin = left_margin if left_margin is not None else 0.0
    return {
        "bold": any(_is_bold(s) for s in items),
        "italic": any(_is_italic(s) for s in items),
        "size": round(statistics.fmean(sizes), 2) if sizes else 0.0,
        "color": max(colors.items(), key=lambda kv: kv[1])[0] if colors else 0,
        "align": align,
        "list_level": lead_spaces // 3,
        "indent": round(xs0 - float(margin), 2),
    }


def _line_groups(items: Sequence[Any]) -> list[list[Any]]:
    """按基线 y 把 span 聚成同一行（1.5pt 容差）。"""
    groups: dict[int, list[Any]] = {}
    for span in items:
        bbox = _bbox_of(span)
        key = int(round(((bbox[1] + bbox[3]) / 2.0) / 1.5))
        groups.setdefault(key, []).append(span)
    return [sorted(g, key=lambda s: _bbox_of(s)[0]) for g in groups.values()]


def paragraph_kind(
    spans: Optional[Iterable[Any]],
    prev_spans: Optional[Iterable[Any]] = None,
    *,
    page_height: Optional[float] = None,
    page_no: Optional[int] = None,
    prev_kind: Optional[str] = None,
) -> str:
    """启发式判定 `SegmentKind`（§2 的值字符串）。与 §3.1.3 的实测信号保持一致。

    判定顺序：page_num → header/footer（需要 page_height）→ caption → table_cell →
    heading → list_item → paragraph。`role`（body/header/footer/pagenum）由抽取层按
    §3.1 的 y 坐标规则赋值；这里只给 `kind`。
    """
    items = _clean_spans(spans)
    if not items:
        return "other"
    text = spans_text(items)
    if not text:
        return "other"

    sizes = [float(_sget(s, "size", 0.0) or 0.0) for s in items]
    own_size = max(sizes) if sizes else 0.0
    own_min = min(sizes) if sizes else 0.0
    bold = any(_is_bold(s) for s in items)
    boxes = [_bbox_of(s) for s in items]
    y0 = min(b[1] for b in boxes)
    y1 = max(b[3] for b in boxes)
    x0 = min(b[0] for b in boxes)

    if len(text) <= 8 and _PAGE_NUM_RE.match(text):
        return "page_num"

    if page_height and page_height > 0:
        if y1 <= page_height * _HEADER_BAND and (page_no is None or page_no > 1):
            return "header"
        if y0 >= page_height * _FOOTER_BAND:
            return "footer"

    if _CAPTION_RE.match(text):
        return "caption"

    # 同一基线上的大间隙 → 表格单元（阈值 14pt：§3.1.3 的标题双 span 间隙 11.5pt 不误伤）
    for line in _line_groups(items):
        gaps = [
            _bbox_of(line[i + 1])[0] - _bbox_of(line[i])[2]
            for i in range(len(line) - 1)
        ]
        wide = [g for g in gaps if g > _TAB_GAP]
        if len(wide) >= 2 or (wide and max(wide) > _BIG_GAP and len(text) < 60):
            return "table_cell"

    prev_items = _clean_spans(prev_spans)
    prev_size = 0.0
    prev_x0: Optional[float] = None
    if prev_items:
        prev_sizes = [float(_sget(s, "size", 0.0) or 0.0) for s in prev_items]
        prev_size = statistics.fmean(prev_sizes) if prev_sizes else 0.0
        prev_x0 = min(_bbox_of(s)[0] for s in prev_items)

    # heading：多级编号（1.4.1）/ 短且粗 / 明显大于上文
    if _MULTI_NUM_RE.match(text) and len(text) <= 140:
        return "heading"
    short = len(text) <= 140
    if short and bold and not _SENT_END.search(text):
        return "heading"
    if prev_size > 0 and own_size >= prev_size * 1.15 and short and not _SENT_END.search(text):
        return "heading"

    if _BULLET_RE.match(text) or _MULTI_NUM_RE.match(text):
        return "list_item"
    if prev_kind == "list_item" and (prev_x0 is None or x0 > prev_x0 + 6.0):
        return "list_item"      # §3.1.3 信号 2：项目符号后缩进的下一段仍属该列表项

    if len(text) <= 3 and own_min <= 10 and all(c in "-•▪◆※·‣⁃◦*" for c in text.replace(" ", "")):
        return "list_item"
    return "paragraph"


def is_banner_image(info: Any, page: Any, doc: Any = None) -> bool:
    """全宽贴边的大图 → 模板（页眉/页脚 banner，重建时不要重复绘制）。

    `info` 为 `page.get_image_info()` 的元素；`doc` 保留给跨页比对（当前用几何判定）。
    """
    try:
        bbox = info.get("bbox") if isinstance(info, dict) else info
        rect = pymupdf.Rect(*[float(v) for v in bbox[:4]]) if not isinstance(bbox, pymupdf.Rect) else bbox
        page_rect = page.rect
    except Exception:
        return False
    if rect.is_empty or rect.is_infinite:
        return False
    pw, ph = float(page_rect.width), float(page_rect.height)
    if pw <= 0 or ph <= 0:
        return False
    if rect.width / pw < 0.88:                      # 未接近满宽
        return False
    tol = 6.0
    touches_edge = rect.y0 <= page_rect.y0 + tol or rect.y1 >= page_rect.y1 - tol
    if not touches_edge:
        return False
    if (rect.width * rect.height) / (pw * ph) < 0.02:   # 细装饰线不算 banner
        return False
    return rect.height >= 18.0


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
def open_pdf(path: str) -> "pymupdf.Document":
    """打开 PDF（不存在 → FileNotFoundError；加密/坏文件 → ValueError）。"""
    target = str(path or "")
    if not target:
        raise ValueError("path is required")
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    try:
        doc = pymupdf.open(target)
    except Exception as exc:  # pymupdf 自身异常类型不稳定，统一成 ValueError
        raise ValueError(f"cannot open PDF: {target}: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError(f"PDF is password protected: {target}")
    return doc


def page_size(page: Any) -> tuple[float, float]:
    """(width, height)，单位 point，已考虑页面旋转。"""
    rect = page.rect
    return float(rect.width), float(rect.height)
