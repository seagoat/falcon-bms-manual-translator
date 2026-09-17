"""render/pagebuild.py — 元素级页面重建 / 版式计算 / 中英混合绘制（CONTRACT §6）。

设计要点（Lead 已实测确认）：

* **禁止** ``page.show_pdf_page()``。它把整页塞进一个 Form XObject（并在含位图时
  直接走栅格化路径），文本层与矢量图会丢失，后续 redact 还会连图片一起擦掉。
* 正确流程：``new_page(w, h)`` → ``get_image_info(xrefs=True)`` 逐个
  ``insert_image(Rect(bbox), pixmap=Pixmap(doc, xref))`` → ``get_drawings()`` 用
  ``Shape`` 重放（``l``/``re``/``c``/``qu``）→ 文本用 ``TextWriter`` 重放。
* **混合字体**：中文用 ``Deng.ttf``（粗体 ``Dengb.ttf``），ASCII/拉丁用
  ``calibri.ttf``（粗 ``calibrib.ttf``）。逐 run 分别 append 并推进 x 光标；
  Deng.ttf 画拉丁字符是全角宽度，整段用一种字体既丑又浪费横向空间。
* ``compute_layout`` 的输出**同时**供 (a) PDF 绘制 (b) Web 前端 canvas 逐行绘制，
  两边必须逐像素一致 —— 断行只在服务端做一次，前端不再换行。
  为了让混合 run 的行也能精确对齐，每行额外给出 ``runs``（绝对 x + 字体槽 + 宽度）。
"""

from __future__ import annotations

import io
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf

try:  # numpy 只用于背景色众数统计（可选）
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]
CJK_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\Dengb.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
]
LATIN_REGULAR = r"C:\Windows\Fonts\calibri.ttf"
LATIN_BOLD = r"C:\Windows\Fonts\calibrib.ttf"
LATIN_ITALIC = r"C:\Windows\Fonts\calibrii.ttf"
LATIN_BOLD_ITALIC = r"C:\Windows\Fonts\calibriz.ttf"
SERIF_REGULAR = r"C:\Windows\Fonts\times.ttf"
SERIF_BOLD = r"C:\Windows\Fonts\timesbd.ttf"

#: 原页字体名 → {"" | "b" | "i" | "bi": 文件}
_ORIG_FONT_FAMILIES = [
    (re.compile(r"cambria", re.I), {b"": r"C:\Windows\Fonts\cambriab.ttf",
                                    b"b": r"C:\Windows\Fonts\cambriab.ttf",
                                    b"i": r"C:\Windows\Fonts\cambriai.ttf",
                                    b"bi": r"C:\Windows\Fonts\cambriaz.ttf"}),
    (re.compile(r"arial|helvetica", re.I), {b"": r"C:\Windows\Fonts\arial.ttf",
                                            b"b": r"C:\Windows\Fonts\arialbd.ttf",
                                            b"i": r"C:\Windows\Fonts\ariali.ttf",
                                            b"bi": r"C:\Windows\Fonts\arialbi.ttf"}),
    (re.compile(r"times|georgia|serif", re.I), {b"": SERIF_REGULAR, b"b": SERIF_BOLD,
                                                b"i": r"C:\Windows\Fonts\timesi.ttf",
                                                b"bi": r"C:\Windows\Fonts\timesbi.ttf"}),
    (re.compile(r"gothic|verdana|tahoma|segoe", re.I), {b"": r"C:\Windows\Fonts\GOTHIC.TTF",
                                                        b"b": r"C:\Windows\Fonts\GOTHICB.TTF",
                                                        b"i": r"C:\Windows\Fonts\GOTHICI.TTF",
                                                        b"bi": r"C:\Windows\Fonts\GOTHICBI.TTF"}),
    (re.compile(r"courier|consol|mono", re.I), {b"": r"C:\Windows\Fonts\cour.ttf",
                                                b"b": r"C:\Windows\Fonts\courbd.ttf"}),
    (re.compile(r".", re.I), {b"": LATIN_REGULAR, b"b": LATIN_BOLD,
                              b"i": LATIN_ITALIC, b"bi": LATIN_BOLD_ITALIC}),
]

_FONT_CACHE: Dict[str, pymupdf.Font] = {}
_EM_CACHE: Dict[Tuple[str, str], float] = {}
_SLOT_CACHE: Dict[str, str] = {}


def _font(path: str) -> pymupdf.Font:
    f = _FONT_CACHE.get(path)
    if f is None:
        f = pymupdf.Font(fontfile=path)
        _FONT_CACHE[path] = f
    return f


def _first_existing(paths: Sequence[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def resolve_cjk_font(bold: bool = False) -> str:
    """返回可用的中文字体文件路径（优先沿用 core.pdfdoc.resolve_cjk_font）。"""
    try:  # 与 core 保持同一选择；core 不可用时退回本地候选表
        from core import pdfdoc as _pd  # type: ignore

        p = None
        if hasattr(_pd, "resolve_cjk_font"):
            p = _pd.resolve_cjk_font()
        if p and os.path.exists(p):
            if bold:
                b = _first_existing([p.replace("Deng.ttf", "Dengb.ttf")] + CJK_BOLD_CANDIDATES)
                return b or p
            return p
    except Exception:
        pass
    p = _first_existing(CJK_BOLD_CANDIDATES if bold else CJK_FONT_CANDIDATES)
    if not p:
        raise RuntimeError("no CJK font found (Deng.ttf / simhei.ttf / msyh.ttc)")
    return p


def _slot_font_path(slot: str, bold: bool) -> str:
    """字体槽 → 实际字体文件。slot ∈ {cjk, latin, latin_bold}。"""
    if slot == "cjk":
        return resolve_cjk_font(bold)
    if slot == "latin_bold":
        return LATIN_BOLD if os.path.exists(LATIN_BOLD) else LATIN_REGULAR
    if bold:
        return LATIN_BOLD if os.path.exists(LATIN_BOLD) else LATIN_REGULAR
    return LATIN_REGULAR


def _orig_font_path(font_name: str, flags: int = 0) -> str:
    """原页字体名 + flags → Windows 字体文件（保证原页文本可重放）。"""
    name = font_name or ""
    bold = bool(re.search(r"bold|black|heavy|semibold", name, re.I)) or bool(flags & 16)
    italic = bool(re.search(r"italic|oblique", name, re.I)) or bool(flags & 2)
    key = (b"b" if bold else b"") + (b"i" if italic else b"")
    for rx, table in _ORIG_FONT_FAMILIES:
        if rx.search(name):
            p = table.get(key) or table.get(b"") or LATIN_REGULAR
            if os.path.exists(p):
                return p
            break
    return LATIN_REGULAR


# --------------------------------------------------------------------------
# Unicode 分类：run 切分 / 断行
# --------------------------------------------------------------------------

_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3130, 0x318F),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7FF),
    (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF), (0x20000, 0x2FA1F),
)
_FORBIDDEN_START = "。，、；：！？）］｝〕〉》」』】％’”：；！？,.:;)]}%"[:-1]
_FORBIDDEN_END = "（［｛〔〈《「『【‘“([{"

#: 译文块在原段纵向范围内的锚点：
#:   "conditional"（默认）= 段后间距 ≥8pt 时底部对齐，否则贴原首行
#:   "bottom" / "center" / "top" / "spread"(只撑行距不移动块顶)
VANCHOR = "conditional"
#: 译文行数少于原文、且下方有余量时撑开行距（上限 LINE_SPREAD_MAX * size）
LINE_SPREAD = True
LINE_SPREAD_MAX = 1.6
#: 行距下限系数（可读性硬约束）
LINE_SPACING = 1.24
LINE_SPACING_MIN = 1.15
#: 原始底线的 ascender / descender 比例（Calibri：行框 y0+0.75em == origin）
ASC_RATIO = 0.75
DESC_RATIO = 0.25
TABLE_SCALE_START = 1.0   # table_cell 优先用原字号，放不下再缩
BODY_SCALE_START = 0.92
MIN_SCALE = 0.55


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    for a, b in _CJK_RANGES:
        if a <= o <= b:
            return True
    return False


def char_slot(ch: str) -> str:
    """单字符 → 字体槽（cjk / latin）。以真实字形覆盖为准。"""
    s = _SLOT_CACHE.get(ch)
    if s is not None:
        return s
    if _is_cjk(ch):
        s = "cjk"
    else:
        o = ord(ch)
        s = "latin"
        try:
            if not _font(LATIN_REGULAR).has_glyph(o):
                s = "cjk" if _font(resolve_cjk_font()).has_glyph(o) else "latin"
        except Exception:
            s = "latin"
    _SLOT_CACHE[ch] = s
    return s


def split_runs(text: str, *, bold: bool = False) -> List[Tuple[str, str]]:
    """把文本切成 [(run, slot)]；相邻同类合并。slot ∈ {cjk, latin}。"""
    out: List[Tuple[str, str]] = []
    for ch in text:
        slot = char_slot(ch)
        if slot == "latin" and bold:
            slot = "latin_bold"
        if out and out[-1][1] == slot:
            out[-1] = (out[-1][0] + ch, slot)
        else:
            out.append((ch, slot))
    return out


def _em_width(text: str, slot: str) -> float:
    """相对 em 的宽度（乘字号即得 pt），带缓存。"""
    key = (slot, text)
    w = _EM_CACHE.get(key)
    if w is None:
        path = _slot_font_path(slot, slot == "latin_bold")
        w = _font(path).text_length(text, 1.0)
        _EM_CACHE[key] = w
    return w


def mixed_text_width(text: str, fs: float, bold: bool = False) -> float:
    """混合中英的文本宽度（真实字体度量）。"""
    if not text:
        return 0.0
    return sum(_em_width(run, slot) * fs for run, slot in split_runs(text, bold=bold))


# --------------------------------------------------------------------------
# 断行（贪心 + 标点避头尾 + 拉丁优先在空格断）
# --------------------------------------------------------------------------

def _tokens(text: str, bold: bool) -> List[Tuple[str, str, str, float]]:
    """→ [(kind, text, slot, em_width)]；kind ∈ {word, cjk, space}。"""
    toks: List[Tuple[str, str, str, float]] = []
    buf, buf_slot = "", ""

    def flush_word() -> None:
        nonlocal buf, buf_slot
        if buf:
            toks.append(("word", buf, buf_slot, _em_width(buf, buf_slot)))
            buf, buf_slot = "", ""

    for ch in text:
        if ch in "\r\n\t":
            ch = " "
        if ch == " ":
            flush_word()
            toks.append(("space", " ", "latin", _em_width(" ", "latin")))
            continue
        slot = char_slot(ch)
        if slot == "latin" and bold:
            slot = "latin_bold"
        if slot == "cjk":
            flush_word()
            toks.append(("cjk", ch, slot, _em_width(ch, slot)))
        else:
            if buf_slot and buf_slot != slot:
                flush_word()
            buf_slot = slot
            buf += ch
    flush_word()
    return toks


def _hard_split(text: str, slot: str, size: float, max_w: float) -> List[List[Tuple[str, str, float]]]:
    """超长单词按字符硬切 → 每片一个 run 列表。"""
    parts: List[List[Tuple[str, str, float]]] = []
    cur, cur_w = "", 0.0
    for ch in text:
        w = _em_width(ch, slot) * size
        if cur and cur_w + w > max_w:
            parts.append([(cur, slot, cur_w / size)])
            cur, cur_w = "", 0.0
        cur += ch
        cur_w += w
    if cur:
        parts.append([(cur, slot, cur_w / size)])
    return parts


def _trim_line(line: List[Tuple[str, str, float]]) -> List[Tuple[str, str, float]]:
    out = list(line)
    while out and not out[-1][0].strip():
        out.pop()
    if out:
        txt = out[-1][0].rstrip()
        if not txt:
            out.pop()
        elif txt != out[-1][0]:
            out[-1] = (txt, out[-1][1], _em_width(txt, out[-1][1]))
    while out and not out[0][0].strip():
        out.pop(0)
    if out:
        txt = out[0][0].lstrip()
        if not txt:
            out.pop(0)
        elif txt != out[0][0]:
            out[0] = (txt, out[0][1], _em_width(txt, out[0][1]))
    return out


def wrap_runs(text: str, size: float, max_width: float, *, bold: bool = False) -> List[List[Tuple[str, str, float]]]:
    """贪心断行 → 每行 [(run_text, slot, em_width)]（em 宽度，乘 size 得 pt）。"""
    if max_width <= 0:
        max_width = 1.0
    toks = _tokens(text, bold)
    lines: List[List[Tuple[str, str, float]]] = []
    cur: List[Tuple[str, str, float]] = []
    cur_w = 0.0
    pending_space = 0.0

    for kind, txt, slot, em in toks:
        if kind == "space":
            pending_space = em
            continue
        w = em * size
        space_w = pending_space * size
        if not cur:
            if w > max_width:  # 单词比整行还长 → 硬切
                parts = _hard_split(txt, slot, size, max_width)
                for p in parts[:-1]:
                    lines.append(_trim_line(p))
                cur = list(parts[-1])
                cur_w = sum(e for _, _, e in cur) * size
            else:
                cur = [(txt, slot, em)]
                cur_w = w
            pending_space = 0.0
            continue
        if cur_w + space_w + w <= max_width + 0.01:
            if pending_space:
                cur.append((" ", "latin", pending_space))
            cur.append((txt, slot, em))
            cur_w += space_w + w
            pending_space = 0.0
        else:
            # 避头点：标点不在行首（允许最多 0.6em 悬挂）
            if kind == "cjk" and txt in _FORBIDDEN_START and cur_w + space_w + w <= max_width + 0.6 * size:
                if pending_space:
                    cur.append((" ", "latin", pending_space))
                cur.append((txt, slot, em))
                cur_w += space_w + w
                pending_space = 0.0
                continue
            lines.append(_trim_line(cur))
            cur = [(txt, slot, em)]
            cur_w = w
            pending_space = 0.0
    if cur:
        lines.append(_trim_line(cur))
    lines = [l for l in lines if l]
    if not lines:
        lines = [[("", "latin", 0.0)]]

    # 避尾点：开引号/开括号不在行尾
    for i in range(len(lines) - 1):
        guard = 0
        while len(lines[i]) > 1 and lines[i][-1][0] and lines[i][-1][0][-1] in _FORBIDDEN_END and guard < 4:
            tok = lines[i].pop()
            lines[i + 1].insert(0, tok)
            guard += 1
    return [_trim_line(l) for l in lines if l]


# --- 目录点线（dot leader）对齐 ------------------------------------------------
# 目录页的一行形如 `MISSION 1: GROUND OPS (TR_BMS_01_GroundOPS) .......... 9`，
# 原书里这些行的点线右端（= 页码所在列）**严格对齐**在同一个 x。
# 中文译文比英文短很多（中文字符信息密度高），若只按"左对齐 + 原文字里的点线"绘制，
# 点线就会在各自的中文末尾断掉，页码列参差不齐 —— 肉眼观感就是"目录页没对齐"。
# 实测（修复前，全书目录页 49/53 行）：右端偏差中位数 3.4pt、最大 156pt、within-2pt 命中 0/N。
_DOT_LEADER_MIN = 4          # 判定为点线所需的最少连续点号
_DOT_GAP_FRACTION = 1.5      # 正文与点线之间的间隙（按字号倍数）


def _has_dot_leader(text: str) -> bool:
    """文本末尾是否带点线（`....`），用于识别目录行。"""
    return "...." in (text or "")


def _split_dot_leader(text: str) -> tuple:
    """把目录行拆成 (正文, 点线段, 页码)。

    目录行形如 `MULTIPLAYER ... 7` 或 `1.1 Preparation ...... 11` —— **页码在点线之后**。
    早期版本把「第一个点之后的全部」都当成点线，于是页码被一起删掉，
    重新生成的点线还把页码列盖掉（实测：目录里所有页码消失）。
    因此这里把结尾的页码单独切出来，只重建中间的点线，页码原样保留。
    """
    t = text or ""
    # 1) 先切出结尾页码（允许 `12` / `12.` / `A-3` 之类）
    m = re.search(r"[\s\u00a0]([0-9]{1,4}[A-Za-z]?\.?|[A-Z]-\d{1,3})\s*$", t)
    if m:
        tail_num = m.group(1)
        body_raw = t[:m.start()]
    else:
        tail_num = ""
        body_raw = t
    # 2) 从 body 里切出点线
    i = body_raw.rfind(".")
    if i < 0:
        return t, "", ""
    j = i
    while j >= 0 and body_raw[j] == ".":
        j -= 1
    dots = body_raw[j + 1:i + 1]
    if len(dots) < _DOT_LEADER_MIN:
        return t, "", ""
    return body_raw[: j + 1], dots, tail_num


def _dot_leader_target(source_text: str, trans_text: str, bbox_x1: float,
                       line_boxes, fs: float, bold: bool):
    """算出「译文正文 + 点线」应该占据的总宽度，使其右端落在原书同一位置。

    返回 (target_width, body_text, dots_kept) 或 None（不是点线行 / 无法定位）。
    """
    if not _has_dot_leader(source_text) or not _has_dot_leader(trans_text):
        return None
    # 原书该行的右端（= 页码列）。用与译文同 y 的那条原始 line 最准。
    anchor = None
    if line_boxes:
        for lb in line_boxes:
            r = _as_rect(lb)
            if r is not None:
                anchor = r.x1 if anchor is None else max(anchor, r.x1)
    if anchor is None:
        anchor = bbox_x1
    return anchor


def _count_dot_runs(text: str) -> int:
    """数一条文本里有几段独立的点线（用于识别"两条目录项被并成一段"）。"""
    return len(re.findall(r"\.{4,}", text or ""))


def _split_multi_dot_entries(text: str) -> List[str]:
    """把「两条目录项被并成一段」的文本拆回多条独立条目。

    例：``'8.1 Speed Limit check ...... 153 8.2 Fly-Up check ...... 153'``
        -> ``['8.1 Speed Limit check ...... 153', '8.2 Fly-Up check ...... 153']``

    只含 0 或 1 段点线时返回 ``[]``（表示不是这种畸形输入，走正常换行）。
    """
    t = text or ""
    if _count_dot_runs(t) <= 1:
        return []
    # 在「点线 + 空格 + 页码」之后、紧跟着下一个编号的地方断开。
    # 例：'... 153 8.2 上仰检查 ...' -> 在 '153' 与 '8.2' 之间切。
    # 不用变长 lookbehind（Python 的 re 不支持），改成显式捕获分隔片段。
    pieces = re.split(r"(\.{4,}\s\d{1,4}[A-Za-z]?\.?)\s+(?=\d+[A-Z]?(?:\.\d+)*\s)", t)
    if len(pieces) <= 1:
        return []
    # re.split 带捕获组会把分隔片段也放进结果：把「点线+页码」并回前一段
    parts: List[str] = []
    for piece in pieces:
        if not piece:
            continue
        if re.fullmatch(r"\.{4,}\s\d{1,4}[A-Za-z]?\.?", piece) and parts:
            parts[-1] = parts[-1] + piece
        else:
            parts.append(piece)
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) <= 1:
        return []
    # 拆完每一段仍要含点线，否则说明切错了，宁可不动
    if not all(_has_dot_leader(p) for p in parts):
        return []
    return parts


def _fit_dot_leader(trans_text: str, fs: float, bold: bool, max_width: float,
                    anchor_right: float, x_start: float) -> tuple:
    """在 [x_start, anchor_right] 内重新生成点线长度，**保留结尾页码**。

    目标：正文 + 点线 + 页码 三者总宽正好落到 anchor_right（原书页码列的右端），
    这样页码列才对得齐；同时页码一个字符都不能丢。

    ⚠️ **只处理「一条目录项」**。抽取阶段偶尔会把**两条目录项并成一个段**
    （实测训练手册 p4 seg=77262：
    `'8.1 Speed Limit check ...... 153 8.2 Fly-Up check ...... 153'`）。
    这种文本含 2 段点线，按单条重算会把整行拉到 1000+pt、直接压到下一行上。
    遇到就**原样返回**，交给正常换行逻辑处理。
    """
    if _count_dot_runs(trans_text) > 1:
        return trans_text, _measure_runs(trans_text, fs, bold)
    body, _dots, tail_num = _split_dot_leader(trans_text)
    if not tail_num:
        # 没有页码就是普通段落，别乱改
        return trans_text, _measure_runs(trans_text, fs, bold)
    body_w = _measure_runs(body, fs, bold)
    # 页码前有一个空格，宽度要算进去，否则点线会多出一两格、把页码挤出页码列
    num_w = _measure_runs(" " + tail_num, fs, bold) if body_w else _measure_runs(tail_num, fs, bold)
    dot_w = _measure_runs(".", fs, bold)
    if dot_w <= 0:
        return trans_text, body_w + num_w
    # 预算取两个上界的最小值：① 页锚点（原书页码列右端）② 本段可用宽度。
    # 只看①会让行右端越过正文右边界（实测 x 到 545.6，超出页面左边距 4.5pt）。
    budget = min(anchor_right - x_start, max_width if max_width > 0 else 1e9)
    avail = budget - body_w - num_w
    n = int(avail / dot_w)
    n = max(_DOT_LEADER_MIN, n)
    # 兜底：若算出来仍然超宽（字体度量取整误差），逐步减点号直到放得下
    while n > _DOT_LEADER_MIN and body_w + n * dot_w + num_w > budget:
        n -= 1
    if body_w + n * dot_w + num_w > budget:
        # 连最小点线都放不下：正文 + 页码 紧排，至少保住页码
        text = (body.rstrip() + " " + tail_num).strip()
        return text, _measure_runs(text, fs, bold)
    text = body + "." * n + " " + tail_num
    return text, body_w + n * dot_w + num_w


def _measure_runs(text: str, fs: float, bold: bool) -> float:
    """按混合字体规则量一段 plain text 的宽度（与绘制口径一致）。"""
    total = 0.0
    for _rt, _slot, rem in wrap_runs(text, fs, 1e9, bold=bold)[0] if text else []:
        total += rem * fs
    return total


def _runs_for_text(text: str, lx: float, y: float, size: float, bold: bool) -> list:
    """把单行文本按字体槽切成 runs（不做换行），供点线行的精确定位使用。"""
    runs = []
    cx = lx
    if not text:
        return runs
    for rt, rslot, rem in wrap_runs(text, size, 1e9, bold=bold)[0]:
        w = rem * size
        runs.append({"text": rt, "x": round(cx, 3), "y": round(y, 3),
                     "slot": rslot, "width": round(w, 3)})
        cx += w
    return runs


def layout_mixed_runs(text: str, fs: float, max_width: float, bold: bool = False):
    """兼容 core.pdfdoc.layout_mixed 的返回格式：(line_text, font_slot, width)。"""
    out = []
    for line in wrap_runs(text, fs, max_width, bold=bold):
        t = "".join(r[0] for r in line)
        slot = line[0][1] if line else "latin"
        if any(r[1] == "cjk" for r in line):
            slot = "cjk"
        out.append((t, slot, sum(r[2] for r in line) * fs))
    return out


# --------------------------------------------------------------------------
# 页面几何：模板图 / 正文带 / 障碍物
# --------------------------------------------------------------------------

_GEOM_CACHE: Dict[Tuple[str, int, int], dict] = {}
_BANNER_CACHE: Dict[Tuple[str, int], set] = {}
_BG_CACHE: Dict[Tuple[str, int, int], "_PageBG"] = {}
_PIXMAP_CACHE: Dict[Tuple[str, int], pymupdf.Pixmap] = {}
_PIXMAP_ORDER: List[Tuple[str, int]] = []
_MAX_PIXMAPS = 24
_BG_CACHE_LIMIT = 8


def clear_caches() -> None:
    """导出结束后释放跨页缓存（背景图 / 位图 / 几何）。"""
    _GEOM_CACHE.clear()
    _BG_CACHE.clear()
    _PIXMAP_CACHE.clear()
    _PIXMAP_ORDER.clear()


def _doc_key(doc) -> str:
    try:
        return f"{doc.name}:{doc.page_count}"
    except Exception:
        return f"mem:{id(doc)}"


def banner_xrefs(doc) -> set:
    """出现在 >80% 页面的 image xref → 页眉/页脚模板图（Lead 实测：23、24）。"""
    key = (_doc_key(doc), doc.page_count)
    hit = _BANNER_CACHE.get(key)
    if hit is not None:
        return hit
    from collections import Counter

    c: Counter = Counter()
    for p in doc:
        for im in p.get_images(full=True):
            c[im[0]] += 1
    thr = doc.page_count * 0.8
    out = {x for x, n in c.items() if n > thr}
    _BANNER_CACHE[key] = out
    return out


def page_geometry(doc, page_no: int) -> dict:
    """页眉/页脚模板图 + 正文带 + 图片/矢量障碍物（按页缓存）。"""
    key = (_doc_key(doc), page_no, 0)
    hit = _GEOM_CACHE.get(key)
    if hit is not None:
        return hit
    page = doc[page_no - 1]
    W, H = page.rect.width, page.rect.height
    try:
        banners = banner_xrefs(doc)
    except Exception:
        banners = set()
    images: List[pymupdf.Rect] = []
    header: Optional[pymupdf.Rect] = None
    footer: Optional[pymupdf.Rect] = None
    for info in page.get_image_info(xrefs=True):
        r = pymupdf.Rect(info["bbox"])
        images.append(r)
        if info.get("xref") in banners and r.height < H * 0.45 and r.width > W * 0.5:
            if r.y0 <= H * 0.06:
                header = r if header is None else (header | r)
            elif r.y1 >= H * 0.94:
                footer = r if footer is None else (footer | r)
    draws: List[pymupdf.Rect] = []
    try:
        for d in page.get_drawings():
            dr = pymupdf.Rect(d.get("rect") or (0, 0, 0, 0))
            if dr.is_empty or dr.get_area() > 0.8 * W * H:
                continue
            draws.append(dr)
    except Exception:
        pass
    geom = {
        "page": page,
        "rect": pymupdf.Rect(page.rect),
        "width": W,
        "height": H,
        "header_rect": header,
        "footer_rect": footer,
        "body_top": header.y1 if header else H * 0.085,
        "body_bottom": footer.y0 if footer else H * 0.925,
        "images": images,
        "drawings": draws,
        "banner_xrefs": banners,
    }
    _GEOM_CACHE[key] = geom
    return geom


def _band_role(r: pymupdf.Rect, geom: dict) -> str:
    if r.y1 <= geom["body_top"] + 2.0:
        return "header"
    if r.y0 >= geom["body_bottom"] - 2.0:
        return "footer"
    return "body"


# --------------------------------------------------------------------------
# 背景色（低分辨率整页渲染 + 量化 16 级的众数色）
# --------------------------------------------------------------------------

class _PageBG:
    """按页缓存一次低分辨率渲染，供 compute_layout 采样 bbox 背景色。"""

    def __init__(self, page, dpi: float = 36.0):
        self.dpi = dpi
        self.scale = float(dpi) / 72.0
        # 注意：PyMuPDF 1.28 的 get_pixmap(dpi=...) 只接受 int，传 float 会 TypeError
        pix = page.get_pixmap(dpi=int(round(dpi)), colorspace=pymupdf.csRGB, alpha=False)
        self.pix = pix
        self.n = pix.n
        self.arr = None
        if _np is not None:
            try:
                raw = _np.frombuffer(pix.samples, dtype=_np.uint8)
                raw = raw.reshape(pix.height, pix.stride)
                raw = raw[:, : pix.width * pix.n].reshape(pix.height, pix.width, pix.n)
                self.arr = raw[:, :, :3] if pix.n >= 3 else None
            except Exception:
                self.arr = None
        if self.arr is None:
            self.samples = pix.samples
            self.stride = pix.stride
        else:
            self.samples = None
            self.stride = 0

    def mode_color(self, rect) -> int:
        r = pymupdf.Rect(rect)
        x0 = max(0, int(r.x0 * self.scale))
        y0 = max(0, int(r.y0 * self.scale))
        x1 = min(self.pix.width, max(x0 + 1, int(r.x1 * self.scale + 0.5)))
        y1 = min(self.pix.height, max(y0 + 1, int(r.y1 * self.scale + 0.5)))
        if x1 <= x0 or y1 <= y0:
            return 0xFFFFFF
        if self.arr is not None:
            sub = self.arr[y0:y1, x0:x1].reshape(-1, 3).astype(_np.int32)
            q = sub >> 4
            key = (q[:, 0] << 8) | (q[:, 1] << 4) | q[:, 2]
            vals, counts = _np.unique(key, return_counts=True)
            modal = vals[int(counts.argmax())]
            m = key == modal
            px = sub[m].mean(axis=0)
            return (int(round(float(px[0]))) << 16) | (int(round(float(px[1]))) << 8) | int(round(float(px[2])))
        # 无 numpy：稀疏采样
        counts: Dict[int, int] = {}
        sums: Dict[int, List[int]] = {}
        s, st, n = self.samples, self.stride, self.n
        step = max(1, (x1 - x0) // 40)
        for y in range(y0, y1, max(1, (y1 - y0) // 40)):
            row = y * st
            for x in range(x0, x1, step):
                i = row + x * n
                rr, gg, bb = s[i], s[i + 1], s[i + 2]
                k = ((rr >> 4) << 8) | ((gg >> 4) << 4) | (bb >> 4)
                counts[k] = counts.get(k, 0) + 1
                acc = sums.setdefault(k, [0, 0, 0])
                acc[0] += rr
                acc[1] += gg
                acc[2] += bb
        if not counts:
            return 0xFFFFFF
        k = max(counts, key=lambda kk: counts[kk])
        a = sums[k]
        c = counts[k]
        return ((a[0] // c) << 16) | ((a[1] // c) << 8) | (a[2] // c)


def page_background(doc, page_no: int, dpi: float = 36.0) -> _PageBG:
    key = (_doc_key(doc), page_no, int(dpi))
    bg = _BG_CACHE.get(key)
    if bg is None:
        if len(_BG_CACHE) >= _BG_CACHE_LIMIT:
            _BG_CACHE.pop(next(iter(_BG_CACHE)))
        bg = _PageBG(doc[page_no - 1], dpi)
        _BG_CACHE[key] = bg
    return bg


# --------------------------------------------------------------------------
# 图片 / 矢量重放
# --------------------------------------------------------------------------

def _pixmap_for(doc, xref: int):
    key = (_doc_key(doc), xref)
    p = _PIXMAP_CACHE.get(key)
    if p is not None:
        return p
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is None or pix.n - pix.alpha not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    _PIXMAP_CACHE[key] = pix
    _PIXMAP_ORDER.append(key)
    while len(_PIXMAP_ORDER) > _MAX_PIXMAPS:
        old = _PIXMAP_ORDER.pop(0)
        if old in _PIXMAP_CACHE and old != key:
            _PIXMAP_CACHE.pop(old, None)
    return pix


def _to_np(pix):
    """Pixmap → numpy (h, w, n)，去掉行填充。"""
    if _np is None:
        raise RuntimeError("numpy is required")
    a = _np.frombuffer(pix.samples, dtype=_np.uint8)
    a = a.reshape(pix.height, pix.stride)
    return a[:, : pix.width * pix.n].reshape(pix.height, pix.width, pix.n)


def _rgba_png(doc, xref: int, smask: int) -> Optional[bytes]:
    """带透明遮罩的图 → RGBA PNG bytes（Pillow 合成），失败返回 None。"""
    try:
        from PIL import Image  # type: ignore

        base = pymupdf.Pixmap(doc, xref)
        m = pymupdf.Pixmap(doc, smask)
        w, h, n = base.width, base.height, base.n
        b = _to_np(base)[:, :, :3].astype("uint8")
        mm = _to_np(m)
        mm = mm[:, :, 0] if mm.shape[2] >= 1 else mm[:, :, 0]
        if (mm.shape[1], mm.shape[0]) != (w, h):
            mm = _np.asarray(Image.fromarray(mm).resize((w, h), Image.BILINEAR))
        rgba = _np.dstack([b, mm.astype("uint8")])
        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def _image_stream(doc, xref: int) -> Optional[bytes]:
    """尽量复用原始压缩数据（文件更小、渲染一致）；失败返回 None 由调用方回退 pixmap。"""
    try:
        im = doc.extract_image(xref)
    except Exception:
        return None
    if not im:
        return None
    if im.get("smask"):
        return _rgba_png(doc, xref, int(im["smask"]))
    return im.get("image")


def _shift_rect(r, dx: float, dy: float) -> pymupdf.Rect:
    r = pymupdf.Rect(r)
    return pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy)


def _shift_point(p, dx: float, dy: float) -> pymupdf.Point:
    return pymupdf.Point(p.x + dx, p.y + dy)


def _shift_quad(q, dx: float, dy: float) -> pymupdf.Quad:
    q = pymupdf.Quad(q)
    return pymupdf.Quad(_shift_point(q.ul, dx, dy), _shift_point(q.ur, dx, dy),
                        _shift_point(q.lr, dx, dy), _shift_point(q.ll, dx, dy))


def _place_images(target: pymupdf.Page, src_page, *, dx: float = 0.0, dy: float = 0.0) -> int:
    n = 0
    doc = src_page.parent
    for info in src_page.get_image_info(xrefs=True):
        r = _shift_rect(info["bbox"], dx, dy) if (dx or dy) else pymupdf.Rect(info["bbox"])
        xref = int(info.get("xref") or 0)
        try:
            stream = _image_stream(doc, xref) if xref > 0 else info.get("image")
            if stream:
                target.insert_image(r, stream=bytes(stream), keep_proportion=False)
            elif xref > 0:
                target.insert_image(r, pixmap=_pixmap_for(doc, xref), keep_proportion=False)
            else:
                continue
            n += 1
        except Exception:
            try:
                if xref > 0:
                    target.insert_image(r, pixmap=_pixmap_for(doc, xref), keep_proportion=False)
                    n += 1
            except Exception:
                continue
    return n


def _replay_drawings(target: pymupdf.Page, src_page, *, dx: float = 0.0, dy: float = 0.0) -> int:
    try:
        items = src_page.get_drawings()
    except Exception:
        return 0
    if not items:
        return 0
    shape = target.new_shape()
    n = 0
    for d in items:
        try:
            for it in d["items"]:
                k = it[0]
                if k == "l":
                    shape.draw_line(_shift_point(it[1], dx, dy), _shift_point(it[2], dx, dy))
                elif k == "re":
                    shape.draw_rect(_shift_rect(it[1], dx, dy))
                elif k == "c":
                    shape.draw_bezier(_shift_point(it[1], dx, dy), _shift_point(it[2], dx, dy),
                                      _shift_point(it[3], dx, dy), _shift_point(it[4], dx, dy))
                elif k == "qu":
                    shape.draw_quad(_shift_quad(it[1], dx, dy))
            shape.finish(
                color=d.get("color"),
                fill=d.get("fill"),
                width=d.get("width", 0) or 0,
                closePath=d.get("closePath", False),
                even_odd=d.get("even_odd", False),
                dashes=d.get("dashes"),
            )
            n += 1
        except Exception:
            continue
    try:
        shape.commit()
    except Exception:
        pass
    return n


def _iter_spans(page, with_chars: bool = False) -> List[dict]:
    """抽取原页 span。with_chars=True 时附带逐字符 origin（用于两端对齐的精确定位）。"""
    d = page.get_text("rawdict" if with_chars else "dict")
    out: List[dict] = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            for s in line["spans"]:
                txt = s.get("text")
                if txt is None:
                    txt = "".join(c.get("c", "") for c in s.get("chars", []))
                if not txt:
                    continue
                sp = {
                    "text": txt,
                    "bbox": s["bbox"],
                    "origin": s.get("origin") or (s["bbox"][0], s["bbox"][3]),
                    "size": s.get("size", 10.0),
                    "font": s.get("font", ""),
                    "flags": s.get("flags", 0),
                    "color": s.get("color", 0),
                }
                if with_chars:
                    sp["chars"] = [(c.get("c", ""), c["origin"][0], c["origin"][1])
                                   for c in s.get("chars", []) if c.get("c")]
                out.append(sp)
    return out


def _span_covered(r: pymupdf.Rect, rects: Sequence[pymupdf.Rect]) -> bool:
    cx, cy = (r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0
    for er in rects:
        if not r.intersects(er):
            continue
        if er.contains(pymupdf.Point(cx, cy)):
            return True
        inter = pymupdf.Rect(r) & er
        if not inter.is_empty and inter.get_area() > 0.5 * max(r.get_area(), 1e-6):
            return True
    return False


_EMBED_FONT_CACHE: Dict[Tuple[str, int], Optional[pymupdf.Font]] = {}
_PAGE_FONT_MAP: Dict[Tuple[str, int], Dict[str, int]] = {}


def _page_font_xrefs(doc, page_no: int) -> Dict[str, int]:
    """页面 basefont 名（去子集前缀）→ 字体 xref。"""
    key = (_doc_key(doc), page_no)
    m = _PAGE_FONT_MAP.get(key)
    if m is None:
        m = {}
        try:
            for row in doc[page_no - 1].get_fonts(full=True):
                m.setdefault(str(row[3]).split("+")[-1], int(row[0]))
        except Exception:
            pass
        _PAGE_FONT_MAP[key] = m
    return m


def embedded_font(doc, page_no: int, font_name: str) -> Optional[pymupdf.Font]:
    """取原 PDF 内嵌的子集字体（还原原文最精确：字形与 advance 完全一致）。"""
    xref = _page_font_xrefs(doc, page_no).get(font_name or "")
    if not xref:
        return None
    key = (_doc_key(doc), xref)
    if key in _EMBED_FONT_CACHE:
        return _EMBED_FONT_CACHE[key]
    f: Optional[pymupdf.Font] = None
    try:
        _name, _ext, _sub, buf = doc.extract_font(xref)
        if buf:
            f = pymupdf.Font(fontbuffer=buf)
    except Exception:
        f = None
    _EMBED_FONT_CACHE[key] = f
    return f


def _resolve_span_font(doc, page_no, s: dict) -> pymupdf.Font:
    """span → 字体对象；优先内嵌子集，退化为系统字体文件。"""
    if doc is not None and page_no:
        f = embedded_font(doc, page_no, s.get("font", ""))
        if f is not None:
            return f
    return _font(_orig_font_path(s.get("font", ""), int(s.get("flags") or 0)))


def _just_extra_f(font: pymupdf.Font, txt: str, w_pdf: float, size: float) -> float:
    nsp = txt.count(" ")
    if nsp <= 0:
        return 0.0
    try:
        nat = font.text_length(txt, size)
    except Exception:
        return 0.0
    extra = (w_pdf - nat) / nsp
    return extra if abs(extra) > 0.05 else 0.0


def _just_extra(s: dict, fontpath: str, size: float) -> float:
    """两端对齐时每个空格需要额外拉伸的宽度；不属于对齐行则返回 0。"""
    txt = s.get("text") or ""
    try:
        w_pdf = float(s["bbox"][2]) - float(s["bbox"][0])
    except Exception:
        return 0.0
    return _just_extra_f(_font(fontpath), txt, w_pdf, size)


def _needs_chars(s: dict) -> bool:
    """该 span 是否需要逐字符精确定位（两端对齐行）。"""
    return _just_extra(s, _orig_font_path(s.get("font", ""), int(s.get("flags") or 0)),
                       float(s.get("size") or 10.0)) != 0.0


def _word_appends(chars: Sequence[Tuple[str, float, float]]) -> List[Tuple[float, float, str]]:
    """逐字符 origin → [(x, y, word)]，空格由位置隐含（不产生墨迹）。"""
    out: List[Tuple[float, float, str]] = []
    cur = ""
    cx = cy = 0.0
    for ch, x, y in chars:
        if not ch or ch.isspace():
            if cur:
                out.append((cx, cy, cur))
                cur = ""
            continue
        if not cur:
            cx, cy = x, y
        cur += ch
    if cur:
        out.append((cx, cy, cur))
    return out


def _span_appends_f(s: dict, font: pymupdf.Font, size: float) -> List[Tuple[float, str]]:
    """原页 span → [(dx, text)] 追加序列。

    总是**逐词** append：``TextWriter`` 会把整串文本里的空格变成无字形间隔，MuPDF 再抽取时
    得到的是 NBSP；逐词定位既让抽取回到普通空格，位置也同样精确（字体 advance 与原页一致）。
    两端对齐的行另有 ``_word_appends`` 精确路径。
    """
    txt = s.get("text") or ""
    out: List[Tuple[float, str]] = []
    x = 0.0
    for part in re.split(r"( +)", txt):
        if not part:
            continue
        if part[0] == " ":
            try:
                x += font.text_length(part, size)
            except Exception:
                x += size * 0.25 * len(part)
            continue
        out.append((round(x, 4), part))
        try:
            x += font.text_length(part, size)
        except Exception:
            x += size * 0.5 * len(part)
    return out or [(0.0, txt)]


def _span_appends(s: dict, fontpath: str, size: float) -> List[Tuple[float, str]]:
    """兼容旧签名的包装（仅系统字体）。"""
    return _span_appends_f(s, _font(fontpath), size)


def draw_original_text(page, spans, *, dx: float = 0.0, dy: float = 0.0,
                       src_doc=None, page_no: int = 0) -> int:
    """用混合字体重放原页文本（spans 可为 get_text('dict') 或 span 列表）。

    * 优先使用原 PDF 内嵌的子集字体（字形 + advance 与原页完全一致）。
    * 两端对齐的行按逐字符 origin 精确定位（见 _word_appends），保证与原件几乎逐像素一致。
    """
    if spans is None:
        return 0
    if isinstance(spans, dict):
        flat: List[dict] = []
        for b in spans.get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                flat.extend(line.get("spans", []))
        spans = flat
    items: List[Tuple[int, float, float, str, pymupdf.Font, float]] = []
    n = 0
    for s in spans:
        txt = s.get("text") or ""
        if not txt:
            continue
        org = s.get("origin") or (s["bbox"][0], s["bbox"][3])
        size = float(s.get("size") or 10.0)
        if size <= 0:
            continue
        color = int(s.get("color", 0)) & 0xFFFFFF
        font = _resolve_span_font(src_doc, page_no, s)
        chars = s.get("chars")
        try:
            w_pdf = float(s["bbox"][2]) - float(s["bbox"][0])
        except Exception:
            w_pdf = 0.0
        if chars and _just_extra_f(font, txt, w_pdf, size) != 0.0:
            for x, y, part in _word_appends(chars):
                items.append((color, x + dx, y + dy, part, font, size))
                n += 1
            continue
        for ddx, part in _span_appends_f(s, font, size):
            items.append((color, org[0] + dx + ddx, org[1] + dy, part, font, size))
            n += 1
    by_color: Dict[int, List[Tuple[float, float, str, pymupdf.Font, float]]] = {}
    for color, x, y, part, font, size in items:
        by_color.setdefault(color, []).append((x, y, part, font, size))
    for color, lst in by_color.items():
        tw = pymupdf.TextWriter(page.rect)
        for x, y, part, font, size in lst:
            try:
                tw.append(pymupdf.Point(x, y), part, font=font, fontsize=size)
            except Exception:
                continue
        try:
            tw.write_text(page, color=(((color >> 16) & 255) / 255.0,
                                       ((color >> 8) & 255) / 255.0,
                                       (color & 255) / 255.0))
        except Exception:
            pass
    return n


def rebuild_page(doc, page_no: int, *, erase_roles: Sequence[str] = ("body",), drop_text: bool = False,
                 erase_rects: Optional[Sequence] = None, target_doc=None, target_page=None,
                 page_rect=None, dx: float = 0.0, dy: float = 0.0) -> pymupdf.Page:
    """元素级重建一页（图片 + 矢量 + 文本），返回新页。

    ``erase_roles``：按页眉/页脚/正文带归类，命中的文本**不重放**（默认擦掉正文）。
    ``erase_rects``：给定则只按这些矩形擦除（与 core 段 bbox 精确对应），忽略 erase_roles。
    ``drop_text``：完全不重放任何文本（只留图与矢量）。
    ``target_doc`` / ``target_page`` / ``dx`` / ``dy``：用于把多份内容画到同一页（双语 PDF）。
    """
    src = doc[page_no - 1]
    rect = page_rect or pymupdf.Rect(src.rect)
    out = target_doc if target_doc is not None else pymupdf.open()
    page = target_page if target_page is not None else out.new_page(width=rect.width, height=rect.height)
    if target_page is not None and (rect.width, rect.height) != (page.rect.width, page.rect.height):
        pass
    _place_images(page, src, dx=dx, dy=dy)
    _replay_drawings(page, src, dx=dx, dy=dy)
    if not drop_text:
        spans = _iter_spans(src, with_chars=False)
        if erase_rects is not None:
            rects = [pymupdf.Rect(r) for r in erase_rects]
            keep_idx = [i for i, s in enumerate(spans)
                        if not _span_covered(pymupdf.Rect(s["bbox"]), rects)]
        elif erase_roles:
            geom = page_geometry(doc, page_no)
            roles = set(erase_roles)
            keep_idx = [i for i, s in enumerate(spans)
                        if _band_role(pymupdf.Rect(s["bbox"]), geom) not in roles]
        else:
            keep_idx = list(range(len(spans)))
        if keep_idx and any(_needs_chars(spans[i]) for i in keep_idx):
            # 保留了两端对齐的正文 → 需要逐字符 origin 才能与原页对齐
            spans2 = _iter_spans(src, with_chars=True)
            keep = [spans2[i] for i in keep_idx if i < len(spans2)]
        else:
            keep = [spans[i] for i in keep_idx]
        draw_original_text(page, keep, dx=dx, dy=dy, src_doc=doc, page_no=page_no)
    return page


# --------------------------------------------------------------------------
# 版式计算
# --------------------------------------------------------------------------

def _val(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(key, default)
    else:
        v = getattr(obj, key, default)
    return default if v is None else v


def _as_rect(v) -> Optional[pymupdf.Rect]:
    if v is None:
        return None
    if isinstance(v, pymupdf.Rect):
        return pymupdf.Rect(v)
    try:
        if len(v) == 4:
            r = pymupdf.Rect(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
            return None if r.is_empty else r
    except Exception:
        return None
    return None


def _seg_style(seg) -> dict:
    st = _val(seg, "style", {}) or {}
    size = st.get("size")
    bold = st.get("bold")
    italic = st.get("italic")
    color = st.get("color")
    align = st.get("align")
    fonts = _val(seg, "fonts", []) or []
    best = None
    for f in fonts:
        if best is None or (f.get("count") or 0) > (best.get("count") or 0):
            best = f
    if best:
        nm = str(best.get("font") or "")
        if size in (None, 0):
            size = best.get("size")
        if bold is None:
            bold = bool(re.search(r"bold|black|heavy", nm, re.I)) or bool(int(best.get("flags") or 0) & 16)
        if italic is None:
            italic = bool(re.search(r"italic|oblique", nm, re.I)) or bool(int(best.get("flags") or 0) & 2)
        if color is None:
            color = best.get("color")
    if not size:
        lb = _val(seg, "line_boxes", []) or []
        r0 = _as_rect(lb[0]) if lb else None
        bbox = _as_rect(_val(seg, "bbox"))
        if r0 is not None and r0.height > 0:
            size = min(r0.height, 24.0)
        elif bbox is not None and bbox.height > 0:
            size = min(bbox.height, 24.0)
        else:
            size = 10.0
    return {"size": float(size), "bold": bool(bold), "italic": bool(italic),
            "color": int(color) if color is not None else 0x000000,
            "align": align or ""}


def _normalize_translations(translations) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not translations:
        return out
    if isinstance(translations, dict):
        for k, v in translations.items():
            try:
                sid = int(k)
            except Exception:
                continue
            if isinstance(v, str):
                out[sid] = v
            elif isinstance(v, dict):
                t = v.get("text") or v.get("zh") or ""
                if t:
                    out[sid] = t
            elif v is not None:
                t = _val(v, "text", "") or ""
                if t:
                    out[sid] = t
    else:
        for row in translations:
            sid = _val(row, "segment_id", None)
            if sid is None:
                sid = _val(row, "seg_id", None)
            t = _val(row, "text", "")
            if sid is not None and t:
                out[int(sid)] = t
    return out


def _luminance(color: int) -> float:
    r = ((color >> 16) & 255) / 255.0
    g = ((color >> 8) & 255) / 255.0
    b = (color & 255) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _intra_line_fragments(segs) -> set:
    """识别"行内碎片"段（上标 th / 上标编号等）：其 bbox 被另一段 bbox 大幅包住。

    这类段是抽取侧把同一行拆碎的结果，单独排版必然与宿主段重叠（实测 p13 的 "th"）。
    调用方对它们**不排版**（原文随宿主段一起被擦除）。
    """
    items = []
    for s in segs:
        role = str(_val(s, "role", "body") or "body")
        if role in ("header", "footer", "pagenum", "noise"):
            continue
        b = _as_rect(_val(s, "bbox"))
        if b is not None and b.get_area() > 0:
            items.append((int(_val(s, "id", 0) or 0), b))
    out = set()
    for sid, b in items:
        for oid, ob in items:
            if oid == sid or ob.get_area() <= 4.0 * b.get_area():
                continue
            inter = b & ob
            if not inter.is_empty and inter.get_area() >= 0.8 * b.get_area():
                out.add(sid)
                break
    return out


def _infer_align(seg, bbox: pymupdf.Rect, style: dict) -> str:
    a = (style.get("align") or "").lower()
    if a in ("left", "center", "right", "justify"):
        return "center" if a == "justify" else a
    lbs = [_as_rect(x) for x in (_val(seg, "line_boxes", []) or [])]
    lbs = [x for x in lbs if x is not None]
    if len(lbs) >= 1 and bbox.width > 30:
        lefts = [r.x0 - bbox.x0 for r in lbs]
        rights = [bbox.x1 - r.x1 for r in lbs]
        if min(lefts) > 4.0 and min(rights) > 4.0 and abs(min(lefts) - min(rights)) < 0.25 * bbox.width:
            return "center"
    return "left"


def compute_layout(segments, translations, *, page_no: int, src=None, dx: float = 0.0,
                   body_band: Optional[Tuple[float, float]] = None, only_translated: bool = True,
                   vanchor: Optional[str] = None, spread: Optional[bool] = None
                   ) -> List[dict]:
    """计算某页译文的逐行版式（PDF 与 Web 前端共用，保证逐像素一致）。

    返回 box 列表::

        {"seg_id","bbox","paragraph_rect","lines":[{"text","x","y","size","font_slot",
          "width","runs":[{"text","x","slot","width"}]}],"color","bg","shrink",
         "overflow","bg_fill","align"}

    * 起始基线 y = 原段首行 origin.y（由 line_boxes[0].y0 + ascender*size 还原）；
      x 起点 = 段 bbox.x0（居中/右对齐段落按行宽偏移）。
    * 行距 = size * 1.24；字号从 原字号*0.92 起，放不下按 0.94 递减，下限 原字号*0.55。
    * 绝不越过下一段 bbox.y0（gap 上限 8pt）与正文带下沿；放不下时末尾加 "…" 计 overflow。
    * bg = 原页低分辨率渲染图上 paragraph_rect 的众数像素色（量化 16 级）。
    """
    segs = [s for s in segments if int(_val(s, "page", 1) or 1) == page_no]
    if not segs:
        return []
    tr_map = _normalize_translations(translations)

    geom = None
    body_top = body_bottom = None
    bg = None
    if src is not None:
        if isinstance(src, pymupdf.Page):
            page = src
            body_top, body_bottom = 0.0, page.rect.height
        else:
            try:
                geom = page_geometry(src, page_no)
                page = geom["page"]
                body_top, body_bottom = geom["body_top"], geom["body_bottom"]
            except Exception:
                geom = None
        if geom is not None:
            try:
                bg = page_background(src, page_no)
            except Exception:
                bg = None
    if body_band:
        body_top, body_bottom = body_band

    entries = []
    frag_ids = _intra_line_fragments(segs)
    for s in segs:
        role = str(_val(s, "role", "body") or "body")
        if role in ("header", "footer", "pagenum", "noise"):
            continue
        if int(_val(s, "id", 0) or 0) in frag_ids:
            continue  # 行内碎片不单独排版（避免压到宿主段落上）
        kind = str(_val(s, "kind", "paragraph") or "paragraph")
        if kind in ("header", "footer", "page_num"):
            continue
        bbox = _as_rect(_val(s, "bbox"))
        if bbox is None or bbox.width <= 0 or bbox.height <= 0:
            continue
        text = _val(s, "text", "") or ""
        sid = int(_val(s, "id", 0) or 0)
        zh = tr_map.get(sid, "")
        if not zh and _val(s, "translation", None):
            t = _val(s, "translation", None)
            zh = (_val(t, "text", "") or "") if not isinstance(t, str) else t
        if not str(zh).strip():
            if only_translated:
                continue
            zh = text
        entries.append((s, sid, bbox, text, str(zh), kind))

    if not entries:
        return []

    # 正文带（无几何信息时用本页正文 bbox 的并集兜底）
    if body_top is None or body_bottom is None:
        u = pymupdf.Rect()
        for s, *_rest in entries:
            b = _as_rect(_val(s, "bbox"))
            if b is not None:
                u |= b
        body_top, body_bottom = u.y0, u.y1

    all_body = []
    obstacles: List[pymupdf.Rect] = []
    for s in segs:
        role = str(_val(s, "role", "body") or "body")
        b = _as_rect(_val(s, "bbox"))
        if b is None:
            continue
        if role not in ("header", "footer", "pagenum"):
            all_body.append((b, int(_val(s, "id", 0) or 0)))
        obstacles.append(b)
    if geom:
        obstacles.extend(geom["images"])
        obstacles.extend(geom["drawings"])
    body_left = min((b.x0 for b, _ in all_body), default=0.0)
    body_right = max((b.x1 for b, _ in all_body), default=(geom or {}).get("width", 595.0))

    boxes: List[dict] = []
    for s, sid, bbox, text, zh, kind in entries:
        style = _seg_style(s)
        fs0 = style["size"]
        bold = style["bold"]
        is_cell = (kind == "table_cell")
        if is_cell:
            a0 = str(style.get("align") or "").lower()
            align = a0 if a0 in ("left", "center", "right") else "left"
        else:
            align = _infer_align(s, bbox, style)
        lb0 = None
        lbN = None
        lbs = _val(s, "line_boxes", []) or []
        if lbs:
            lb0 = _as_rect(lbs[0])
            lbN = _as_rect(lbs[-1]) or lb0
        asc = ASC_RATIO
        base_y = (lb0.y0 + asc * fs0) if lb0 is not None else (bbox.y0 + asc * fs0)
        # 原段最后一行的 baseline（用于把译文块底部对齐原块底部）
        orig_last_base = (lbN.y1 - DESC_RATIO * fs0) if lbN is not None else base_y

        y_top = base_y - asc * fs0
        limit_prov = bbox.y1 + 8.0
        if body_bottom is not None:
            limit_prov = min(limit_prov, body_bottom)
        # 横向：可扩宽到最近障碍物 / 正文右边界；绝不窄于原 bbox。
        # 表格单元**绝不扩宽**（否则该单元的文字会压到右侧相邻单元上）。
        cap_right = bbox.x1 if is_cell else bbox.x1 + max(3.0 * bbox.width, 160.0)
        right = min(body_right, cap_right) if body_right else cap_right
        band_top = min(y_top, base_y - asc * fs0 * 0.55)
        for ob in obstacles:
            if ob is bbox or abs(ob.x0 - bbox.x0) < 0.3 and abs(ob.y0 - bbox.y0) < 0.3:
                continue
            if ob.x0 <= bbox.x1 + 0.5:
                continue
            if ob.y1 < band_top or ob.y0 > limit_prov:
                continue
            right = min(right, ob.x0 - 3.0)
        avail_right = max(right, bbox.x1)
        x_start = bbox.x0
        avail_w = max(avail_right - x_start, 4.0)

        # 下一段 y0 → 可用下沿（gap 上限 8pt）。
        # 只统计**与本文本列横向相交**的段：多栏/表格布局里旁边一列的低位段不能当作"下一段"，
        # 否则会把本段压扁（实测 p27 seg5303 因此少撑开 30pt）。
        nxt = None
        for b2, sid2 in all_body:
            if sid2 == sid or b2.y0 <= bbox.y0 + 1.0:
                continue
            if b2.x1 <= x_start + 0.5 or b2.x0 >= avail_right - 0.5:
                continue
            nxt = b2.y0 if nxt is None else min(nxt, b2.y0)
        gap = (nxt - bbox.y1) if nxt is not None else 8.0
        limit_bottom = bbox.y1 + min(max(gap, 0.0), 8.0)
        if body_bottom is not None:
            limit_bottom = min(limit_bottom, body_bottom)
        if nxt is not None:
            limit_bottom = min(limit_bottom, nxt - 0.5)
        limit_bottom = max(limit_bottom, bbox.y0 + 1.0)

        scale = TABLE_SCALE_START if is_cell else BODY_SCALE_START
        size = fs0 * scale
        # ⚠️ 抽取偶尔把**两条目录项并进一个段**（实测 seg=77262：
        # `'8.1 Speed Limit check ...... 153 8.2 Fly-Up check ...... 153'`）。
        # 直接 wrap 会在条目中间断开、再被两端对齐撑开，两条内容压在同一行上。
        # 这里按点线边界拆成多条逻辑行，每条各自成行。
        multi_entry_parts = ([] if is_cell else _split_multi_dot_entries(zh))
        if multi_entry_parts:
            lines = []
            for part in multi_entry_parts:
                lines.extend(wrap_runs(part, size, avail_w, bold=bold))
            overflow = False
        else:
            lines = wrap_runs(zh, size, avail_w, bold=bold)
            overflow = False
            while True:
                size = fs0 * scale
                lines = wrap_runs(zh, size, avail_w, bold=bold)
                line_h = size * LINE_SPACING
                bottom = base_y + (len(lines) - 1) * line_h + size * DESC_RATIO
                top = base_y - asc * size
                if bottom <= limit_bottom + 0.4 and top >= bbox.y0 - 1.5:
                    break
                if scale <= MIN_SCALE + 1e-9:
                    break
                scale = max(MIN_SCALE, scale * 0.94)

        size = fs0 * scale
        line_h = size * LINE_SPACING
        max_lines = int((limit_bottom - base_y - size * DESC_RATIO) // line_h) + 1
        max_lines = max(1, max_lines)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            overflow = True
            last = lines[-1]
            while last:
                cand = _trim_line(last + [("…", "latin", _em_width("…", "latin"))])
                if sum(e for _, _, e in cand) * size <= avail_w + 0.01:
                    last = cand
                    break
                last = _trim_line(last[:-1])
            lines[-1] = last or [("…", "latin", _em_width("…", "latin"))]

        # ---- 译文块的纵向落位 -------------------------------------------------
        # 译文比原文短时若仍贴段顶，段内下半部会留下大片空白，视觉上像"原文被删了"。
        # 策略：先把行距在上限内撑开（填满原块），再把整块按 vanchor 锚到原块底部/中线。
        # 硬约束：不上移越过段顶；绝不下探越过 limit_bottom（即下一段 bbox.y0）。
        n_lines = len(lines)
        if not is_cell:
            va = (vanchor or VANCHOR).lower()
            do_spread = LINE_SPREAD if spread is None else spread
            # (A) 底部对齐只在"该段后面确有 ≥8pt 间距"时才做（Lead 指定）：
            #     否则贴原首行更自然，避免标题与正文之间被拉开空隙。
            gap_after = (nxt - bbox.y1) if nxt is not None else 999.0
            if va == "conditional" and (gap_after < 8.0 or n_lines < 2):
                # 单行译文没有"块底"概念，下移只会把它从上方标题/段落拉开 → 仍贴原首行
                va = "top"
            orig_span = max(0.0, orig_last_base - base_y)
            room = max(0.0, limit_bottom - base_y - size * DESC_RATIO)
            max_span = min(orig_span, room)
            if do_spread and n_lines > 1 and max_span > (n_lines - 1) * line_h:
                line_h = min(LINE_SPREAD_MAX * size, max_span / (n_lines - 1))
                line_h = max(line_h, LINE_SPACING_MIN * size)
            span = (n_lines - 1) * line_h
            if va in ("bottom", "conditional"):
                new_first = orig_last_base - span
            elif va == "center":
                new_first = base_y + max(0.0, (orig_span - span) / 2.0)
            elif va == "spread":
                # 只撑开行距、不移动块顶：块底自然落到原块底附近
                new_first = base_y
            else:
                new_first = base_y
            new_first = max(new_first, base_y)                       # 只下移，不上移
            new_first = min(new_first, limit_bottom - size * DESC_RATIO - span)
            new_first = max(new_first, base_y)
            if new_first - asc * size >= bbox.y0 - 1.5:
                base_y = new_first
            else:
                line_h = size * LINE_SPACING

        # 目录点线行：点线右端（页码列）必须与原书对齐。
        # 中文比英文短，直接沿用原文里的点线会让页码列参差不齐。
        # ⚠️ 只对**恰好一条目录项**的行做重排（见 _fit_dot_leader 的说明）：
        #    · `len(lines) == 1`   —— 单行；
        #    · `_count_dot_runs(zh) == 1` —— 整段只含一段点线。
        # 抽取偶尔把两条目录项并成一段（实测 seg=77262：
        # `'8.1 Speed Limit check ...... 153 8.2 Fly-Up check ...... 153'`），
        # 那种行按单条重排会把整条拉到 1000+pt 压到下一行上，必须排除。
        dot_anchor = None
        if (not is_cell and align != "center" and _has_dot_leader(text)
                and _has_dot_leader(zh) and _count_dot_runs(zh) == 1):
            anchors = [r.x1 for r in (lb0, lbN) if r is not None]
            if anchors:
                dot_anchor = max(anchors)

        out_lines = []
        for i, line in enumerate(lines):
            y = base_y + i * line_h
            lw = sum(e for _, _, e in line) * size
            line_text = "".join(r[0] for r in line)
            # 点线行重算点号数量，使右端落在原书同一列（只改点号，不动正文）
            if dot_anchor is not None and len(lines) == 1:
                fitted_text, fitted_w = _fit_dot_leader(line_text, size, bold,
                                                        avail_w, dot_anchor, x_start)
                if fitted_w > 0:
                    line_text, lw = fitted_text, fitted_w
            if align == "center":
                # 居中/右对齐以**原段自身**的左右界为锚（表格单元/标题列不能借障碍物扩宽）
                anchor_right = min(avail_right, bbox.x1)
                lx = x_start + max(0.0, (anchor_right - x_start - lw) / 2.0)
            elif align == "right":
                anchor_right = min(avail_right, bbox.x1)
                lx = max(x_start, anchor_right - lw)
            else:
                lx = x_start
            if dot_anchor is not None and len(lines) == 1:
                # 用「原文右端 - 本行宽度」定位，保证页码列一致
                lx = max(x_start, min(dot_anchor - lw, avail_right - lw))
                runs = _runs_for_text(line_text, lx, y, size, bold)
            else:
                runs = []
                cx = lx
                for rt, rslot, rem in line:
                    w = rem * size
                    runs.append({"text": rt, "x": round(cx + dx, 3), "slot": rslot, "width": round(w, 3)})
                    cx += w
            out_lines.append({
                "text": "".join(r["text"] for r in runs),
                "x": round(lx + dx, 3),
                "y": round(y, 3),
                "size": round(size, 3),
                "font_slot": runs[0]["slot"] if runs else "latin",
                "width": round(lw, 3),
                "runs": runs,
            })

        p_rect = (x_start + dx, bbox.y0, avail_right + dx, limit_bottom)
        bgc = bg.mode_color((x_start, bbox.y0, avail_right, limit_bottom)) if bg is not None else 0xFFFFFF
        bg_fill = True
        if geom:
            pr = pymupdf.Rect(x_start, bbox.y0, avail_right, limit_bottom)
            for ir in geom["images"]:
                if pr.intersects(ir):
                    bg_fill = False
                    break
        boxes.append({
            "seg_id": sid,
            "bbox": [round(v, 2) for v in bbox],
            "paragraph_rect": [round(v, 2) for v in p_rect],
            "lines": out_lines,
            "color": 0x1A1A1A if _luminance(bgc) > 0.55 else 0xF2F2F2,
            "bg": bgc,
            "line_h": round(line_h, 3),
            "n_src_lines": len(lbs) if lbs else 1,
            "vanchor": (vanchor or VANCHOR),
            "shrink": round(scale, 4),
            "overflow": overflow,
            "bg_fill": bg_fill,
            "align": align,
            "kind": kind,
        })
    return boxes


# --------------------------------------------------------------------------
# 绘制译文
# --------------------------------------------------------------------------

def _rgb_floats(color: int) -> Tuple[float, float, float]:
    return (((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0)


def draw_translated(page, *, boxes, cjk_font_path: Optional[str] = None, dx: float = 0.0,
                    dy: float = 0.0) -> dict:
    """在重建页上绘制译文（背景矩形 + 混合字体逐 run）。

    返回 {"drawn":n,"shrunk":n,"overflow":n,"lines":n}。
    """
    stats = {"drawn": 0, "shrunk": 0, "overflow": 0, "lines": 0}
    if not boxes:
        return stats
    for b in boxes:
        if b.get("bg_fill", True) and b.get("bg") is not None:
            try:
                pr = _shift_rect(b["paragraph_rect"], dx, dy)
                page.draw_rect(pr, color=None, fill=_rgb_floats(int(b["bg"])), width=0, overlay=True)
            except Exception:
                pass
    by_color: Dict[int, List[tuple]] = {}
    for b in boxes:
        color = int(b.get("color", 0x1A1A1A))
        cnt = False
        for line in b.get("lines", []):
            y = float(line["y"]) + dy
            runs = line.get("runs")
            if not runs:
                runs = [{"text": line.get("text", ""), "x": line.get("x", 0.0),
                         "slot": line.get("font_slot", "latin"), "width": line.get("width", 0.0)}]
            for r in runs:
                txt = r.get("text") or ""
                if not txt.strip():
                    continue
                slot = r.get("slot") or "latin"
                size = float(line.get("size") or 10.0)
                path = cjk_font_path if (slot == "cjk" and cjk_font_path) else _slot_font_path(slot, False)
                try:
                    fnt = _font(path)
                except Exception:
                    fnt = _font(LATIN_REGULAR)
                by_color.setdefault(color, []).append(
                    (pymupdf.Point(float(r["x"]) + dx, y), txt, fnt, size))
                cnt = True
        if cnt:
            stats["drawn"] += 1
        if float(b.get("shrink", 0.92)) < 0.92 - 1e-6:
            stats["shrunk"] += 1
        if b.get("overflow"):
            stats["overflow"] += 1
        stats["lines"] += len(b.get("lines", []))
    for color, items in by_color.items():
        tw = pymupdf.TextWriter(page.rect)
        for pt, txt, fnt, size in items:
            try:
                tw.append(pt, txt, font=fnt, fontsize=size)
            except Exception:
                continue
        try:
            tw.write_text(page, color=_rgb_floats(color))
        except Exception:
            pass
    return stats


# --------------------------------------------------------------------------
# 单页构建 / 渲染
# --------------------------------------------------------------------------

def build_translated_page(src_doc, page_no: int, segments, translations, *,
                          cjk_font_path: Optional[str] = None, target_doc=None,
                          page_rect=None, dx: float = 0.0, dy: float = 0.0,
                          erase_untracked_body: bool = False, stats: Optional[dict] = None) -> pymupdf.Page:
    """重建 + 擦除已翻译正文 + 绘制译文，返回新页。

    没有译文的正文段保留原英文（不会出现空白页），除非 ``erase_untracked_body=True``。
    """
    page_segs = [s for s in segments if int(_val(s, "page", 1) or 1) == page_no]
    tr_map = _normalize_translations(translations)
    erase: List = []
    if not erase_untracked_body:
        for s in page_segs:
            role = str(_val(s, "role", "body") or "body")
            if role in ("header", "footer", "pagenum", "noise"):
                continue
            sid = int(_val(s, "id", 0) or 0)
            zh = tr_map.get(sid, "")
            if not zh:
                t = _val(s, "translation", None)
                if isinstance(t, str):
                    zh = t
                elif t:
                    zh = _val(t, "text", "") or ""
            if str(zh or "").strip():
                b = _as_rect(_val(s, "bbox"))
                if b is not None:
                    erase.append(b)
        page = rebuild_page(src_doc, page_no, erase_roles=(), erase_rects=erase,
                            target_doc=target_doc, page_rect=page_rect)
    else:
        page = rebuild_page(src_doc, page_no, erase_roles=(), erase_rects=None, target_doc=target_doc,
                            page_rect=page_rect)
    boxes = compute_layout(segments, translations, page_no=page_no, src=src_doc)
    st = draw_translated(page, boxes=boxes, cjk_font_path=cjk_font_path, dx=dx, dy=dy)
    st["boxes"] = len(boxes)
    if stats is not None:
        stats.clear()
        stats.update(st)
    return page


def _as_doc(doc_or_path):
    if isinstance(doc_or_path, pymupdf.Document):
        return doc_or_path, False
    if isinstance(doc_or_path, pymupdf.Page):
        return doc_or_path.parent, False
    return pymupdf.open(str(doc_or_path)), True


def render_page_png(doc_or_path, page_no: int, *, dpi: float = 110, alpha: bool = False) -> bytes:
    """渲染某页为 PNG bytes（page_no 1-based）。"""
    doc, close = _as_doc(doc_or_path)
    try:
        page = doc_or_path if isinstance(doc_or_path, pymupdf.Page) else doc[page_no - 1]
        pix = page.get_pixmap(dpi=int(round(dpi)), alpha=alpha)
        return pix.tobytes("png")
    finally:
        if close:
            doc.close()


def render_page_webp(doc_or_path, page_no: int, *, dpi: float = 110, quality: int = 80) -> bytes:
    """渲染某页为 WebP bytes（Pillow 转码）。"""
    png = render_page_png(doc_or_path, page_no, dpi=dpi)
    try:
        from PIL import Image  # type: ignore

        im = Image.open(io.BytesIO(png))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=int(quality), method=4)
        return buf.getvalue()
    except Exception:
        return png


def render_boxes_overlay(page, boxes, *, color=(1.0, 0.0, 0.0)) -> None:
    """调试：把 box 的 paragraph_rect / 基线画出来。"""
    for b in boxes or []:
        try:
            page.draw_rect(pymupdf.Rect(*b["paragraph_rect"]), color=color, width=0.4)
            for line in b.get("lines", []):
                page.draw_line(pymupdf.Point(line["x"], line["y"]),
                               pymupdf.Point(line["x"] + line["width"], line["y"]),
                               color=(0.0, 0.45, 0.9), width=0.3)
        except Exception:
            continue
