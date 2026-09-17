"""图示标签双层标注（方案 1）：在图片上叠加中文标签 + 细引线，不改动原图。

两种呈现风格：
  * ``style="capsule"``（默认）—— 每条英文标注旁边直接放一个中文胶囊 + 引线。
    观感直观、就近可读；适合空间充裕的图（HARTS 机动图、流程图、简令表）。
  * ``style="numbered"`` —— 图上只打 ①②③… 小圆点，中文集中排在页面空白处的
    **对照表**里。适合 p11 那种 30+ 条标注挤在 A4 一页的密集面板图，
    避免胶囊互相挤压。

设计要点
  * 非破坏性：原图一个像素都不改，标注画在**页面**层，
    PDF 文本层可搜索、可复制，随时可关掉重来。
  * 位置来自 OCR：`data/derived/ocr/pN_zh.json` 给的是**图内像素坐标**，
    这里换算回页面 pt。
  * 不遮挡：胶囊风格优先放英文标注右侧，放不下就翻转/上下错位，
    并用已占用矩形列表做碰撞回避（同时避开其它中文标签与其它英文标注）。
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_DIR = os.path.join(ROOT, "data", "derived", "ocr")

CJK_FONT = "C:/Windows/Fonts/Deng.ttf"
CJK_FONT_BOLD = "C:/Windows/Fonts/Dengb.ttf"
LATIN_FONT = "C:/Windows/Fonts/calibri.ttf"

# 密集页自动切到编号风格的阈值（标注条数）
NUMBERED_AUTO_THRESHOLD = 28

PAD_X = 3.0          # 标签内左右留白 (pt)
PAD_Y = 1.6          # 标签内上下留白 (pt)
GAP = 2.0            # 标签与英文标注之间的间隙 (pt)
LINE_H = 1.0         # 引线宽度
MIN_FS = 4.6         # 最小字号，低于此值改用短译或跳过
BG_ALPHA = 0.82      # 底衬不透明度
BORDER = 0.4

# —— 编号风格（numbered）参数 ——
NUM_R = 4.2          # 编号圆点半径 (pt)
LEGEND_FS = 6.2      # 对照表字号
LEGEND_LH = 8.0      # 对照表行高
LEGEND_PAD = 4.0     # 对照表内边距
LEGEND_COLS = 2      # 对照表列数
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳" \
          "㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"


def load_labels(page_no: int) -> Optional[dict]:
    path = os.path.join(OCR_DIR, f"p{page_no}_zh.json")
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))


def make_transform(page: pymupdf.Page, image_bbox, dpi: int, px_w: int):
    """像素坐标 -> 页面 pt。OCR 是在 clip=image_bbox、指定 dpi 的渲染图上做的，
    所以 scale = 72/dpi 即可（影像与 placement 一一对应，无旋转）。"""
    scale = 72.0 / dpi
    x0, y0 = float(image_bbox[0]), float(image_bbox[1])
    return lambda px, py: (x0 + px * scale, y0 + py * scale), scale


def _overlaps(a, b, slack=0.0):
    return not (a[2] <= b[0] + slack or b[2] <= a[0] + slack
                or a[3] <= b[1] + slack or b[3] <= a[1] + slack)


def _place(placed, box, en_box, pw, ph, others=None, *, slack=0.0, ban_en=True):
    """box 是否可用：在页内、不压其它标签、不压它自己对应的英文标注。"""
    if box[0] < 2 or box[2] > pw - 2 or box[1] < 2 or box[3] > ph - 2:
        return False
    if any(_overlaps(box, p, slack) for p in placed):
        return False
    if ban_en and _overlaps(box, en_box, 0.0):
        return False
    # 也不要压到**别的**英文标注（p11 密集面板图里这是主要冲突来源）
    if ban_en and others:
        for ob in others:
            if ob is en_box:
                continue
            if _overlaps(box, ob, 0.0):
                return False
    return True


def _fit_font(text: str, font, max_w: float, want_fs: float) -> tuple:
    """在 max_w 内选择合适的字号；返回 (fs, width)。"""
    fs = want_fs
    while fs >= MIN_FS:
        w = font.text_length(text, fs)
        if w <= max_w:
            return fs, w
        fs -= 0.2
    return 0.0, 0.0


def plan_annotations(page: pymupdf.Page, page_no: int, *, verbose=False) -> list:
    """算好每个中文标签的矩形与引线，返回 draw 指令列表。

    两阶段：
      1. 按直觉位置（右→下→上→左）尝试放置，带碰撞回避；
      2. 放置失败的标签做**局部搜索**——在英文标注周围一圈候选偏移上找空位，
         避免 p11 那种密集面板图里大量标签因"右侧被占"而丢失。
    """
    data = load_labels(page_no)
    if not data:
        return []
    to_pt, scale = make_transform(page, data["image_bbox"], data["dpi"], 0)
    font = pymupdf.Font(fontfile=CJK_FONT)
    font_b = pymupdf.Font(fontfile=CJK_FONT_BOLD)

    pw, ph = page.rect.width, page.rect.height
    placed: List[List[float]] = []
    out = []

    # 所有 OCR 到的英文标注矩形：中文标签不应压在它们上面
    all_en = []
    for l in data["labels"]:
        all_en.append([to_pt(l["x0"], 0)[0], to_pt(0, l["y0"])[1],
                       to_pt(l["x1"], 0)[0], to_pt(0, l["y1"])[1]])

    labels = sorted(data["labels"], key=lambda l: -l["score"])
    for lab in labels:
        en_x0 = to_pt(lab["x0"], 0)[0]
        en_y0 = to_pt(0, lab["y0"])[1]
        en_x1 = to_pt(lab["x1"], 0)[0]
        en_y1 = to_pt(0, lab["y1"])[1]
        en_h = max(1.0, en_y1 - en_y0)
        en_w = max(1.0, en_x1 - en_x0)
        zh = (lab.get("zh") or "").strip()
        # 长句（图注/正文段）不按标签处理：它们是段落文字，应由主文本管线翻译。
        # 放在小胶囊里既放不下也难看（p291 实测 5 条被丢弃）。
        if not zh or zh == lab.get("label") or len(zh) > 40 or zh.count(" ") > 6:
            continue
        want_fs = max(MIN_FS, en_h * 0.78)
        f = font_b if len(zh) <= 8 else font
        en_box = [en_x0, en_y0, en_x1, en_y1]

        chosen = None
        # 阶段 1：四个直觉方位
        for where, cx, cy in (
            ("right", en_x1 + GAP, en_y0),
            ("below", en_x0, en_y1 + GAP * 0.6),
            ("above", en_x0, en_y0 - en_h - GAP),
        ):
            for k in (1.0, 0.92, 0.84, 0.76):
                fs, tw = _fit_font(zh, f, 96.0, want_fs * k)
                if fs <= 0:
                    continue
                h = fs * 1.30 + 2 * PAD_Y
                bx0 = cx if where != "left" else en_x0 - GAP - (tw + 2 * PAD_X)
                by0 = cy - PAD_Y if where == "right" else cy
                box = [bx0, by0, bx0 + tw + 2 * PAD_X, by0 + h]
                if _place(placed, box, en_box, pw, ph, all_en, slack=0.5):
                    chosen = (where, box, fs)
                    break
            if chosen:
                break

        # 阶段 2：局部搜索（沿 8 个方向、逐圈外扩）；允许压英文（保位置，但最后才用）
        if not chosen:
            for k in (1.0, 0.9, 0.8):
                fs, tw = _fit_font(zh, f, 96.0, want_fs * k)
                if fs <= 0:
                    continue
                w = tw + 2 * PAD_X
                h = fs * 1.30 + 2 * PAD_Y
                for ring in (1, 2, 3, 4, 6, 8):
                    stepx = (en_w * 0.5 + w * 0.5 + GAP * ring)
                    stepy = (en_h + h) * 0.5 + GAP * 1.5 * ring
                    for dx, dy in ((stepx, 0), (-stepx, 0), (0, stepy), (0, -stepy),
                                   (stepx, stepy), (-stepx, stepy),
                                   (stepx, -stepy), (-stepx, -stepy)):
                        bx0 = en_x0 + dx
                        by0 = en_y0 + dy
                        box = [bx0, by0, bx0 + w, by0 + h]
                        if _place(placed, box, en_box, pw, ph, all_en, slack=0.5):
                            chosen = ("near", box, fs)
                            break
                    if chosen:
                        break
                if chosen:
                    break

        # 阶段 3：实在放不下就让位（不压标签，允许压英文），保证不漏标
        if not chosen:
            for k in (0.9, 0.8):
                fs, tw = _fit_font(zh, f, 96.0, want_fs * k)
                if fs <= 0:
                    continue
                w = tw + 2 * PAD_X
                h = fs * 1.30 + 2 * PAD_Y
                for ring in (1, 2, 3, 5, 8, 12):
                    bx0 = en_x1 + GAP + (GAP + 6) * ring
                    box = [bx0, en_y0 - PAD_Y, bx0 + w, en_y0 - PAD_Y + h]
                    if _place(placed, box, en_box, pw, ph, all_en,
                              slack=0.5, ban_en=False):
                        chosen = ("push", box, fs)
                        break
                if chosen:
                    break

        if not chosen:
            if verbose:
                print(f"    skip (no room): {lab['label']!r} -> {zh!r}")
            continue
        where, box, fs = chosen
        placed.append(box)
        out.append({
            "en": lab["label"], "zh": zh, "score": lab["score"],
            "where": where, "box": [round(v, 2) for v in box], "fs": round(fs, 2),
            "en_box": [round(en_x0, 2), round(en_y0, 2), round(en_x1, 2), round(en_y1, 2)],
            "bold": len(zh) <= 8,
        })
    return out


def _page_text_boxes(page: pymupdf.Page) -> list:
    """页面上**已有文字**的 bbox（正文段落、标题、图注……）。

    对照表必须避开它们，否则会盖住正文 —— 第一版把对照表放在了页首段落上。
    """
    boxes = []
    try:
        d = page.get_text("dict")
    except Exception:
        return boxes
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            if "".join(s["text"] for s in ln["spans"]).strip():
                boxes.append(list(ln["bbox"]))
    return boxes


def _free_rects(page: pymupdf.Page, avoid: list, *, pad: float = 3.0) -> list:
    """把页面切成若干条横向带，返回每条带里**不与 avoid 相交**的可插入矩形。

    实现：以 avoid 矩形的上下边界为切割点，得到一组 y 区间；对每个 y 区间，
    取所有与它相交的 avoid 矩形，按 x 排序后算出区间的空隙。
    """
    pw, ph = page.rect.width, page.rect.height
    # 页眉/页脚 banner 固定占用
    ys = {66.0, 776.0}
    for r in avoid:
        if r is None or len(r) < 4:
            continue
        ys.add(max(0.0, min(ph, r[1])))
        ys.add(max(0.0, min(ph, r[3])))
    cuts = sorted(y for y in ys if 0 <= y <= ph)
    rects = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 8:
            continue
        band = (a, b)
        blockers = [r for r in avoid if r and len(r) >= 4
                    and r[3] > band[0] + 0.5 and r[1] < band[1] - 0.5]
        blockers.sort(key=lambda r: r[0])
        xs = 0.0
        for r in blockers:
            if r[0] - xs > 40:
                rects.append(pymupdf.Rect(xs + pad, band[0] + pad,
                                          r[0] - pad, band[1] - pad))
            xs = max(xs, r[2])
        if pw - xs > 40:
            rects.append(pymupdf.Rect(xs + pad, band[0] + pad,
                                      pw - pad, band[1] - pad))
    # 大块优先
    rects.sort(key=lambda r: r.width * r.height, reverse=True)
    return [r for r in rects if r.width > 50 and r.height > 20]


def _legend_size(n_items: int, cols: int, fs: float, lh: float):
    per_col = (n_items + cols - 1) // cols
    return per_col * lh + 2 * LEGEND_PAD + lh * 0.9, per_col


def draw_numbered(page: pymupdf.Page, items: list, *, title: str = "图注对照 / Label legend") -> int:
    """编号风格：图上打圈号，中文集中排在页面空白处的对照表里。返回处理条数。

    关键：对照表必须放在**真正空闲**的矩形里（不与任何英文标注/其它内容相交），
    否则会像第一版那样把整张图盖住。所以这里先算空闲区，再按可用空间
    自适应列数与字号。
    """
    if not items:
        return 0
    pw, ph = page.rect.width, page.rect.height
    avoid = [it["en_box"] for it in items]
    # 页面上已有的**其它文字**（正文段落、标题、图注）也必须避开，
    # 否则对照表会盖住它们（第一版就盖住了页首那段说明文字）。
    placed = [it.get("box") for it in items if it.get("box")]
    avoid += _page_text_boxes(page) + [r for r in placed if r]
    rects = _free_rects(page, avoid)
    if not rects:
        return 0

    n = len(items)
    # 由宽到窄、由多列到少列、由大到小字号，找第一个塞得下的组合
    for rect in rects:
        for cols in (3, 2, 1):
            for fs, lh in ((LEGEND_FS, LEGEND_LH), (5.6, 7.2), (5.0, 6.4), (4.4, 5.8)):
                need_h, per_col = _legend_size(n, cols, fs, lh)
                if need_h <= rect.height and rect.width >= 90 * cols * 0.55:
                    return _draw_legend(page, rect, items, cols, fs, lh, title)
    return 0


def _draw_legend(page: pymupdf.Page, rect: pymupdf.Rect, items: list,
                 cols: int, fs: float, lh: float, title: str) -> int:
    n = len(items)
    # 底衬
    sh = page.new_shape()
    sh.draw_rect(rect)
    sh.finish(fill=(1.0, 0.99, 0.94), color=(0.86, 0.33, 0.10), width=0.5,
              fill_opacity=0.94, stroke_opacity=0.9)
    sh.commit()

    colw = (rect.width - 2 * LEGEND_PAD) / cols
    per_col = (n + cols - 1) // cols
    font_cjk = pymupdf.Font(fontfile=CJK_FONT)

    page.insert_text((rect.x0 + LEGEND_PAD, rect.y0 + LEGEND_PAD + fs * 0.9),
                     title, fontname="Cal", fontfile=LATIN_FONT,
                     fontsize=max(4.2, fs - 0.8), color=(0.55, 0.25, 0.08))

    for i, it in enumerate(items):
        num = CIRCLED[i] if i < len(CIRCLED) else f"({i + 1})"
        col, row = i // per_col, i % per_col
        x = rect.x0 + LEGEND_PAD + col * colw
        y = rect.y0 + LEGEND_PAD + lh * 1.1 + row * lh
        page.insert_text((x, y), num, fontname="Deng", fontfile=CJK_FONT,
                         fontsize=fs, color=(0.75, 0.28, 0.06))
        zh = it["zh"]
        avail = colw - fs - 3
        if avail > 6:
            while zh and font_cjk.text_length(zh, fs) > avail:
                zh = zh[:-1]
            if zh != it["zh"]:
                zh = (zh[:-1] + "…") if len(zh) > 1 else zh
        page.insert_text((x + fs + 1.5, y), zh, fontname="Deng", fontfile=CJK_FONT,
                         fontsize=fs, color=(0.10, 0.10, 0.10))

    # 图上打圈号
    for i, it in enumerate(items):
        num = CIRCLED[i] if i < len(CIRCLED) else f"({i + 1})"
        ex0, ey0, ex1, ey1 = it["en_box"]
        cx, cy = ex0 - NUM_R - 1.0, (ey0 + ey1) / 2.0
        if cx < 1:
            cx = ex1 + NUM_R + 1.0
        sh = page.new_shape()
        sh.draw_circle(pymupdf.Point(cx, cy), NUM_R)
        sh.finish(fill=(1.0, 0.93, 0.80), color=(0.86, 0.33, 0.10), width=0.45,
                  fill_opacity=0.95)
        sh.commit()
        tw = font_cjk.text_length(num, NUM_R * 1.45)
        page.insert_text((cx - tw / 2.0, cy + NUM_R * 0.5), num,
                         fontname="Deng", fontfile=CJK_FONT, fontsize=NUM_R * 1.45,
                         color=(0.55, 0.18, 0.02))
    return len(items)


def draw_overlay(page: pymupdf.Page, items: list, *, leader=True) -> int:
    """把标注画到页面上（半透明底衬 + 边框 + 中文 + 引线）。返回绘制条数。"""
    font = pymupdf.Font(fontfile=CJK_FONT)
    font_b = pymupdf.Font(fontfile=CJK_FONT_BOLD)
    n = 0
    for it in items:
        bx0, by0, bx1, by1 = it["box"]
        fs = it["fs"]
        f = font_b if it["bold"] else font
        # 引线：英文标注矩形 -> 中文标签矩形，取最近边中点
        if leader:
            ex0, ey0, ex1, ey1 = it["en_box"]
            if it["where"] == "right":
                p1 = (ex1, (ey0 + ey1) / 2); p2 = (bx0, (by0 + by1) / 2)
            elif it["where"] == "below":
                p1 = ((ex0 + ex1) / 2, ey1); p2 = ((bx0 + bx1) / 2, by0)
            elif it["where"] == "above":
                p1 = ((ex0 + ex1) / 2, ey0); p2 = ((bx0 + bx1) / 2, by1)
            else:
                p1 = (ex0, (ey0 + ey1) / 2); p2 = (bx1, (by0 + by1) / 2)
            sh = page.new_shape()
            sh.draw_line(p1, p2)
            sh.finish(color=(0.86, 0.33, 0.10), width=LINE_H * 0.6)
            sh.commit()
        # 底衬
        sh = page.new_shape()
        sh.draw_rect(pymupdf.Rect(bx0, by0, bx1, by1))
        sh.finish(fill=(1.0, 0.97, 0.88), color=(0.86, 0.33, 0.10), width=BORDER,
                  fill_opacity=BG_ALPHA, stroke_opacity=0.95)
        sh.commit()
        # 文本：垂直居中于底衬
        tw = f.text_length(it["zh"], fs)
        tx = bx0 + ((bx1 - bx0) - tw) / 2.0
        ty = by0 + (by1 - by0) / 2.0 + fs * 0.36
        page.insert_text((tx, ty), it["zh"], fontname="Deng", fontfile=CJK_FONT,
                         fontsize=fs, color=(0.08, 0.08, 0.08))
        n += 1
    return n


def annotate_page(src_doc: pymupdf.Document, page_no: int, out_doc: pymupdf.Document,
                  *, verbose=False, style: str = "auto") -> dict:
    """把 src_doc 的第 page_no 页复制到 out_doc（原页 + 标注层），返回统计。

    style: "auto"（默认，条目多时自动切编号风格）/ "capsule" / "numbered"
    """
    page = out_doc.new_page(width=src_doc[page_no - 1].rect.width,
                            height=src_doc[page_no - 1].rect.height)
    page.show_pdf_page(page.rect, src_doc, page_no - 1)
    items = plan_annotations(src_doc[page_no - 1], page_no, verbose=verbose)

    use = style
    if use == "auto":
        # 实测结论（2026-09-17）：在 A4 上「编号 + 页边对照表」并不比胶囊好 ——
        # p11 那张座舱图几乎占满整页，对照表无论放哪都会盖住图或正文，
        # 而被盖住的正是要标注的内容。胶囊风格虽然局部拥挤，但不遮挡任何内容。
        # 因此 **auto 一律用胶囊**；numbered 保留为可选（适合图四周留白大的页）。
        use = "capsule"
    drawn = 0
    if use == "numbered":
        drawn = draw_numbered(page, items)
        if drawn <= 0:                       # 编号表放不下 → 退回胶囊
            use = "capsule"
    if use == "capsule":
        drawn = draw_overlay(page, items)
    return {"page": page_no, "planned": len(items), "drawn": drawn,
            "style": use, "items": items}
