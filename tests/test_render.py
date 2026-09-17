"""tests/test_render.py — 渲染层自测（CONTRACT §6）。

直接运行：``python tests\\test_render.py``   （失败 sys.exit(1)）
可选：``python tests\\test_render.py --bench``  跑 401 页全量导出性能测试。

覆盖：
1. 合成 PDF：中文 PDF 页数/可打开/中文可抽取/**原英文被擦除**/图片不减少；双语 PDF 页宽 2 倍。
2. 真实 PDF 第 21 页：compute_layout 全部盒子不越界（不越下一段 y0、不越正文区、不超段落右界）。
3. 目视验收图：data/derived/preview/p21_cn.png、p21_bilingual.png、p21_overlay_diff.png。
4. **目录点线行必须保留页码**（test_toc_dot_leader）：回归防线。
   起因：为保证页码列对齐而重算点线时，`_split_dot_leader()` 曾把「点线之后的页码」
   一起当成点线吞掉，导致**整列页码在 UI 和 PDF 里消失**，而当时所有测试仍然全绿
   —— 因为它们只检查布局能跑通，没检查内容不丢。这里补上内容级断言。
"""

from __future__ import annotations

import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymupdf  # noqa: E402

from render import pagebuild as pb  # noqa: E402
from render import pdfout as po  # noqa: E402

REAL_PDF = os.path.join(ROOT, "origin", "BMS-Training-Manual.pdf")
PREVIEW_DIR = os.path.join(ROOT, "data", "derived", "preview")
TMP_DIR = os.path.join(ROOT, "data", "derived", "tmp_test_render")

FAILURES: list = []
SKIPS: list = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILURES.append(msg)
    return bool(cond)


def skip(msg):
    print(f"  SKIP {msg}")
    SKIPS.append(msg)


# ==========================================================================
# 本地抽取器（与 data/derived/preview/_seg.py 相同逻辑；core 抽取器完成后可替换）
# ==========================================================================

def _band_role(r, body_top, body_bottom):
    if r.y1 <= body_top + 2.0:
        return "header"
    if r.y0 >= body_bottom - 2.0:
        return "footer"
    return "body"


def page_bands(doc, page_no):
    page = doc[page_no - 1]
    W, H = page.rect.width, page.rect.height
    top, bottom = 0.088 * H, 0.92 * H
    try:
        from collections import Counter
        c = Counter()
        step = max(1, doc.page_count // 60)
        idx = list(range(0, doc.page_count, step))
        for i in idx:
            for im in doc[i].get_images(full=True):
                c[im[0]] += 1
        banners = {x for x, n in c.items() if n > len(idx) * 0.8}
    except Exception:
        banners = set()
    for info in page.get_image_info(xrefs=True):
        if info.get("xref") in banners:
            r = pymupdf.Rect(info["bbox"])
            if r.y0 <= H * 0.06 and r.height < H * 0.45:
                top = r.y1
            elif r.y1 >= H * 0.94 and r.height < H * 0.45:
                bottom = r.y0
    return top, bottom, banners


def segments_from_pdf(doc, pages, *, start_id=1):
    """把 PDF 文本块转成 Segment 形状的 dict（bbox/line_boxes/fonts/style/role/kind）。"""
    out = []
    sid = start_id
    for pno in pages:
        page = doc[pno - 1]
        top, bottom, _ = page_bands(doc, pno)
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            text, lines, fonts = "", [], {}
            for line in b.get("lines", []):
                lt = ""
                for s in line["spans"]:
                    t = s["text"]
                    if not t:
                        continue
                    lt += t
                    f = fonts.setdefault(s["font"], {"font": s["font"], "size": s["size"],
                                                     "color": s["color"], "flags": s["flags"],
                                                     "count": 0})
                    f["count"] += len(t)
                if lt.strip():
                    lines.append(list(line["bbox"]))
                    text += lt
            if not text.strip():
                continue
            bb = pymupdf.Rect(b["bbox"])
            role = _band_role(bb, top, bottom)
            fl = sorted(fonts.values(), key=lambda x: -x["count"])
            best = fl[0]
            size = float(best["size"])
            bold = "Bold" in best["font"] or bool(int(best["flags"]) & 16)
            italic = "Italic" in best["font"] or bool(int(best["flags"]) & 2)
            norm = re.sub(r"\s+", " ", text).strip()
            if role == "footer":
                kind = "page_num" if re.fullmatch(r"\d{1,4}", norm) else "footer"
            elif role == "header":
                kind = "header"
            elif len(norm) < 80 and (size >= 12 or bold):
                kind = "heading"
            elif re.match(r"^[\-\u2013\u2022\u25aa\u25c6\d]+[\.\)]?\s", norm) and len(norm) > 3:
                kind = "list_item"
            else:
                kind = "paragraph"
            out.append({
                "id": sid, "page": pno, "order_index": sid, "kind": kind, "role": role,
                "text": norm, "normalized": norm, "bbox": [round(v, 3) for v in bb],
                "line_boxes": [[round(v, 3) for v in ln] for ln in lines],
                "fonts": fl,
                "style": {"size": size, "bold": bold, "italic": italic,
                          "color": int(best["color"]), "align": ""},
            })
            sid += 1
    return out


# ==========================================================================
# 合成测试 PDF
# ==========================================================================

SENT_A = "The activation of this warning light signals a loss of cabin pressure."
SENT_B = "Descend below ten thousand feet and initiate a landing immediately."
SENT_C = "Check the oil pressure gauge and verify the hydraulic system pressure."


def _own_synthetic(path, pages=3):
    """自造 3 页合成 PDF（core 的 tests/fixtures.py 不可用时的后备）。"""
    from PIL import Image, ImageDraw

    doc = pymupdf.open()
    img = Image.new("RGB", (120, 80), (40, 90, 160))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 110, 70], outline=(255, 220, 0), width=4)
    buf = os.path.join(TMP_DIR, "_fixture_img.png")
    os.makedirs(TMP_DIR, exist_ok=True)
    img.save(buf)
    for p in range(pages):
        page = doc.new_page(width=595.32, height=841.92)
        page.insert_text((54, 45), "BMS TRAINING MANUAL", fontsize=12,
                         fontname="helv", color=(0.2, 0.2, 0.2))
        page.insert_text((54, 62), "SYNTHETIC FIXTURE", fontsize=10, fontname="helv")
        page.insert_text((54, 90), f"CHAPTER {p + 1}", fontsize=14, fontname="hebo")
        _para(page, 54, 120, 470, [
            (SENT_A if p == 0 else f"Page {p + 1} " + SENT_A),
            SENT_B,
        ])
        _para(page, 54, 200, 470, [SENT_C])
        _para(page, 54, 260, 470, [
            "This paragraph exists so that layout and overflow behaviour can be checked "
            "with a longer source text that wraps over multiple lines in the fixture.",
        ])
        if p == 0:
            page.insert_image(pymupdf.Rect(54, 380, 300, 520), filename=buf)
        page.insert_text((540, 800), str(p + 1), fontsize=10, fontname="helv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc.save(path, deflate=True)
    doc.close()
    return path


def _para(page, x, y, width, sentences, size=10.0):
    """按宽度手动断行的英文段落（近似 Word 输出，左对齐）。"""
    f = pymupdf.Font("helv")
    for sent in sentences:
        line = ""
        for word in sent.split(" "):
            t = (line + " " + word).strip()
            if f.text_length(t, size) > width and line:
                page.insert_text((x, y), line, fontsize=size, fontname="helv")
                y += size * 1.24
                line = word
            else:
                line = t
        if line:
            page.insert_text((x, y), line, fontsize=size, fontname="helv")
            y += size * 1.35
    return y


def make_fixture(path, pages=3):
    """优先用 core 的 tests/fixtures.py（同一接口），否则用本地后备实现。"""
    try:
        from tests import fixtures  # type: ignore

        make = getattr(fixtures, "make_synthetic_pdf", None)
        if make:
            make(path, pages=pages)
            d = pymupdf.open(path)
            n = d.page_count
            d.close()
            if n >= pages:
                print(f"  (fixture: tests/fixtures.py, {n} pages)")
                return path
    except Exception as e:  # noqa: BLE001
        print(f"  (fixture: local fallback — {type(e).__name__}: {e})")
    return _own_synthetic(path, pages=pages)


# ==========================================================================
# 测试 1：合成 PDF 端到端导出
# ==========================================================================

ZH_A = "该警告灯亮起表示座舱失压，应立即下降并尽快着陆。"
ZH_B = "下降至一万英尺以下并立即开始着陆。"
ZH_C = "检查滑油压力表并确认液压系统压力。"
ZH_D = "本段用于验证较长原文的换行与溢出处理是否可在合成夹具上正常工作，并确认译文能够被抽取。"

_ZH_FILLER = "该段落的中文译文用于验证渲染与抽取流程能够正确处理中文内容并保持版式稳定"


def zh_for(text: str, seed: int = 0) -> str:
    """与原文无关的确定性中文占位译文（不含拉丁字母，便于断言原文被擦除）。"""
    n = max(3, int(len(text) * 0.7))
    return "".join(_ZH_FILLER[(seed + i * 7 + (i * i) % 5) % len(_ZH_FILLER)] for i in range(n))


def _norm(s: str) -> str:
    """空格归一（TextWriter 的间隔可能被抽取为 NBSP）。"""
    return re.sub(r"[\s\u00a0]+", " ", s)


def test_toc_dot_leader():
    """目录点线行必须保留页码，且行右端不越过页码列。

    回归用例（见文件头注释）：曾因点线重算把页码吞掉，整列页码消失而测试全绿。
    这里不依赖真实 PDF：直接喂带页码的点线行给 `_fit_dot_leader()` 做内容级断言。
    """
    cases = [
        ("前言 .................. 2", 9.2, 541.1, 53.9),
        ("多人飞行与训练 .................. 7", 9.2, 541.1, 53.9),
        ("任务 16：SPICE (TR_BMS_16_Spice) ............ 166", 9.2, 541.1, 64.9),
        ("MISSION 1: GROUND OPS (TR_BMS_01_GroundOPS) ............ 10", 9.2, 541.1, 64.9),
        ("1.10 滑行 ..................................... 1000", 9.2, 541.1, 91.3),
    ]
    for text, fs, anchor, x0 in cases:
        want_num = text.rstrip().split()[-1]
        out, w = pb._fit_dot_leader(text, fs, False, 470.0, anchor, x0)
        # ① 页码必须还在
        got_num = out.rstrip().split()[-1]
        if got_num != want_num:
            raise AssertionError(
                f"页码丢失或被改：输入尾 {want_num!r} -> 输出尾 {got_num!r}\n  输出={out!r}")
        # ② 点线要还在（否则说明走了「正文+页码紧排」的兜底分支，页码列会乱）
        if ".." not in out:
            raise AssertionError(f"点线消失，页码列无法对齐：{out!r}")
        # ③ 行右端不得越过页锚点
        end = x0 + w
        if end > anchor + 0.6:
            raise AssertionError(
                f"行右端越界：x0={x0} w={w:.1f} 末端={end:.1f} > 锚点 {anchor}")
        # ④ 不越正文可用宽度
        if w > 470.0 + 0.6:
            raise AssertionError(f"超出可用宽度 {w:.1f} > 470")
        print(f"   OK 末端={end:6.1f} 锚点={anchor:6.1f} 页码={got_num!r}")

    # ⑤ 没有页码的普通段落不得被改动
    plain = "这是一段普通的正文，没有点线也没有页码。"
    out2, _ = pb._fit_dot_leader(plain, 9.2, False, 470.0, 541.1, 53.9)
    if out2 != plain:
        raise AssertionError(f"普通段落被误改：{out2!r}")
    print("   OK 无页码段落原样返回")
    return 0


def test_synthetic():
    print("\n[1] 合成 PDF：中文 / 双语导出")
    os.makedirs(TMP_DIR, exist_ok=True)
    src_path = make_fixture(os.path.join(TMP_DIR, "fixture.pdf"), pages=3)
    doc = pymupdf.open(src_path)
    segs = segments_from_pdf(doc, range(1, doc.page_count + 1))
    body = [s for s in segs if s["role"] == "body" and re.search(r"[A-Za-z]{4}", s["text"])]
    check(len(body) >= 6, f"抽取到正文段 {len(body)} 个（>=6）")
    # 通用译文：只含中文，便于断言「原英文已被擦除」
    zh = {s["id"]: zh_for(s["text"], s["id"]) for s in body}
    check(len(zh) >= 6, f"覆盖译文段 {len(zh)} 个（>=6）")

    # 取最长的一段原文用于「已被擦除」断言（避免短词偶然残留）
    longest = max(body, key=lambda s: len(s["text"]))
    probe = " ".join(longest["text"].split()[:6])
    header_probe = None
    for s in segs:
        if s["role"] in ("header", "footer", "pagenum") and len(s["text"].strip()) >= 6:
            header_probe = s["text"].strip()
            break

    src_images = sum(len(doc[p].get_image_info()) for p in range(doc.page_count))
    src_pages = doc.page_count

    cn_path = os.path.join(TMP_DIR, "out_cn.pdf")
    st = po.export_cn_pdf(src_path, cn_path, segs, zh, chunk=2)
    check(st["pages"] == src_pages, f"中文 PDF 页数 {st['pages']} == {src_pages}")
    check(os.path.getsize(cn_path) > 1000, f"中文 PDF 字节数 {st['bytes']}")
    cn = pymupdf.open(cn_path)
    check(cn.page_count == src_pages, f"重新打开页数 {cn.page_count}")
    txt = "".join(cn[p].get_text() for p in range(cn.page_count))
    check(bool(re.search(r"[\u4e00-\u9fff]", txt)), "中文可被抽取（get_text 含 CJK）")
    check(probe not in _norm(txt), f"原英文正文已被擦除（probe={probe[:40]!r}）")
    got_img = sum(len(cn[p].get_image_info()) for p in range(cn.page_count))
    check(got_img >= src_images, f"图片数不减少：{got_img} >= {src_images}")
    check(all(abs(cn[p].rect.width - doc[p].rect.width) < 0.01 for p in range(cn.page_count)),
          "中文 PDF 页面尺寸与原文一致")
    if header_probe:
        check(header_probe.split()[0] in txt, f"页眉/页脚原文保留（{header_probe[:24]!r}）")
    cn.close()

    bi_path = os.path.join(TMP_DIR, "out_bi.pdf")
    st2 = po.export_bilingual_pdf(src_path, bi_path, segs, zh, chunk=2)
    check(st2["pages"] == src_pages, f"双语 PDF 页数 {st2['pages']} == {src_pages}")
    bi = pymupdf.open(bi_path)
    check(bi.page_count == src_pages, f"双语重新打开页数 {bi.page_count}")
    check(all(abs(bi[p].rect.width - 2 * doc[p].rect.width) < 0.01 for p in range(bi.page_count)),
          "双语 PDF 页宽为 2 倍")
    check(all(abs(bi[p].rect.height - doc[p].rect.height) < 0.01 for p in range(bi.page_count)),
          "双语 PDF 页高不变")
    btxt = "".join(bi[p].get_text() for p in range(bi.page_count))
    check(bool(re.search(r"[\u4e00-\u9fff]", btxt)), "双语 PDF 含中文")
    check(probe in _norm(btxt), "双语左半保留原英文")
    check("EN" in btxt and "中文" in btxt, "双语页签文字 EN / 中文 存在")
    bi.close()
    doc.close()
    return cn_path, bi_path


# ==========================================================================
# 测试 2：真实 PDF 第 21 页版式越界检查
# ==========================================================================

P21_ZH = {
    "CANOPY": "座舱盖（CANOPY）",
    "The activation of this warning light": "该警告灯亮起表示座舱盖锁钩或锁扣未锁定，或发生了座舱失压。下降到 10,000 英尺以下，并尽快开始着陆。",
    "FLCS": "飞控系统（FLCS）",
    "The FLCS warning light illuminates": "当存在与飞行控制系统（FLCS）相关的 PFL 警告信息时，FLCS 警告灯就会亮起。详情请查阅飞行员故障清单（PFL）信息。",
    "HYD/OIL PRESS": "液压/滑油压力（HYD/OIL PRESS）",
    "This warning light indicates a low-pressure": "该警告灯表示一个或两个液压系统存在低压状况，或滑油压力过低，需要彻底检查：",
    "1. Check the oil pressure gauge": "1. 检查滑油压力表。若压力读数正常，则该问题与滑油压力无关。若压力低于 15 psi，表示滑油压力过低，应限制油门移动并立即开始着陆。参见本节后面飞行中应急部分的 2.1.13 滑油泄漏。若在地面，应立即关闭发动机，并等待 RPM 降至 20% 以下后再尝试重新启动。",
    "2. Examine the hydraulic system": "2. 检查右侧辅助控制台上的液压系统 A 与 B 压力表并确认压力：",
    "- If only system A is below 1000 psi": "- 若仅系统 A 低于 1000 psi，表示系统 A 液压失效（参见 EP 检查单中的 System A Hydraulic failure）。",
    "- If the system B gauge registers": "- 若系统 B 压力表低于 1000 psi，检查应急动力装置（EPU）：",
    "\u25aa If the EPU RUN light is off": "\u25aa 若 EPU RUN 灯熄灭，表示系统 B 液压失效（参见“系统 B 液压失效”",
    "the EP checklists).": "的 EP 检查单）。",
    "\u25aa If the EPU run light is on": "\u25aa 若 EPU run 灯亮起，检查 ELEC SYS 警戒灯：",
    "\u25c6 If the ELEC SYS light is on": "\u25c6 若 ELEC SYS 灯亮起，问题为 PTO 轴失效。",
    "\u25c6 If the ELEC SYS light is off": "\u25c6 若 ELEC SYS 灯熄灭，则液压系统 A 与 B 均已失效（参见 EP 检查单中的双液压失效）。",
    "If both hydraulic indicators show less": "若在 EPU 开关处于 NORM 或运行时两个液压指示器均低于 1000 psi，表示液压完全失效，一旦 EPU 燃油耗尽，飞机将变得无法控制。若在此之前无法执行着陆，弹射是唯一可行的选择。",
    "OXY LOW": "氧气不足（OXY LOW）",
    "The OXY LOW warning light activates": "当机上制氧系统（OBOGS）自检（BIT）发现故障，或调节器压力降至 5 psi 以下时，OXY LOW 警告灯会亮起。详情参见 OBOGS 故障部分。",
}


def translate_page21(segs):
    zh = {}
    for s in segs:
        if s["role"] != "body":
            continue
        for k, v in P21_ZH.items():
            if s["text"].startswith(k):
                zh[s["id"]] = v
                break
    return zh


def test_layout_real():
    print("\n[2] 真实 PDF 第 21 页：compute_layout 越界检查")
    if not os.path.exists(REAL_PDF):
        skip(f"{REAL_PDF} 不存在")
        return None
    doc = pymupdf.open(REAL_PDF)
    segs = segments_from_pdf(doc, [21])
    zh = translate_page21(segs)
    check(len(zh) >= 15, f"第 21 页覆盖译文 {len(zh)} 段（>=15）")
    boxes = pb.compute_layout(segs, zh, page_no=21, src=doc)
    check(len(boxes) == len(zh), f"layout 生成 {len(boxes)} 个盒子 == {len(zh)}")

    top, bottom, _ = page_bands(doc, 21)
    body = [s for s in segs if s["role"] == "body"]
    body_left = min(s["bbox"][0] for s in body)
    body_right = max(s["bbox"][2] for s in body)

    bad_y, bad_x, bad_line, bad_next = [], [], [], []
    for b in boxes:
        pr = b["paragraph_rect"]
        bb = b["bbox"]
        if pr[1] < 70 - 0.5 or pr[3] > 770 + 0.5:
            bad_y.append((b["seg_id"], pr))
        if pr[0] < body_left - 0.5 or pr[2] > body_right + 0.5:
            bad_x.append((b["seg_id"], pr))
        # 绝不越过下一段 y0
        nxt = [s["bbox"][1] for s in body
               if s["bbox"][1] > bb[1] + 1.0 and s["id"] != b["seg_id"]]
        if nxt and pr[3] > min(nxt) + 0.6:
            bad_next.append((b["seg_id"], round(pr[3], 2), round(min(nxt), 2)))
        for ln in b["lines"]:
            if ln["y"] > pr[3] + 0.6 or ln["y"] < pr[1] - 1.5:
                bad_line.append((b["seg_id"], ln["y"], pr))
            if ln["x"] + ln["width"] > pr[2] + 0.62 * ln["size"] + 0.1:
                bad_line.append((b["seg_id"], "width", ln["x"] + ln["width"], pr[2]))
    check(not bad_y, f"全部盒子在正文区 y∈[70,770] 内（越界 {len(bad_y)}）")
    check(not bad_x, f"全部盒子在正文 x∈[{body_left:.1f},{body_right:.1f}] 内（越界 {len(bad_x)}）")
    check(not bad_next, f"没有盒子越过下一段 bbox.y0（越界 {len(bad_next)}）")
    check(not bad_line, f"所有基线/行宽都在盒子内（异常 {len(bad_line)}）")

    # 译文墨迹不得压在图片上：基线落进图片纵向范围且与该图横向相交才算冲突
    # （原页首行 origin 与图片下沿本来就允许有 ascender 级别的相切，见 seg9）
    geom = pb.page_geometry(doc, 21)
    imgs = geom["images"]
    ink_hits = []
    for b in boxes:
        for ln in b["lines"]:
            for ir in imgs:
                if ln["x"] + ln["width"] <= ir.x0 + 0.5:
                    continue
                if ir.y0 - 0.2 < ln["y"] < ir.y1 + 0.2:
                    ink_hits.append((b["seg_id"], round(ln["y"], 1), round(ir.y0, 1), round(ir.y1, 1)))
    check(not ink_hits, f"译文基线不落在图片上（冲突 {len(ink_hits)}）")
    guarded = [b["seg_id"] for b in boxes if not b["bg_fill"]]
    print(f"  info 与图片相交的盒子 bg_fill=False（不画底色，避免擦掉图片）：{guarded}")

    over = [b for b in boxes if b["overflow"]]
    shrunk = [b for b in boxes if b["shrink"] < 0.92 - 1e-6]
    print(f"  info overflow={len(over)} shrunk={len(shrunk)} lines={sum(len(b['lines']) for b in boxes)}")
    for b in boxes:
        first = b["lines"][0]["text"] if b["lines"] else ""
        print(f"       seg{b['seg_id']:>3} shrink={b['shrink']:.2f} lines={len(b['lines'])} "
              f"bg=0x{b['bg']:06x} color=0x{b['color']:06x} | {first[:38]}")
    doc.close()
    return boxes


# ==========================================================================
# 测试 3：目视验收图
# ==========================================================================

def _render(page, dpi=110):
    import numpy as np
    px = page.get_pixmap(dpi=dpi)
    a = np.frombuffer(px.samples, np.uint8).reshape(px.height, px.stride)
    return a[:, : px.width * px.n].reshape(px.height, px.width, px.n)[:, :, :3]


def write_previews():
    print("\n[3] 目视验收图 → data/derived/preview/")
    if not os.path.exists(REAL_PDF):
        skip("真实 PDF 不存在，跳过预览图")
        return
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    doc = pymupdf.open(REAL_PDF)
    segs = segments_from_pdf(doc, [21])
    zh = translate_page21(segs)

    cn_path = os.path.join(TMP_DIR, "p21_cn.pdf")
    st = po.export_cn_pdf(REAL_PDF, cn_path, segs, zh, pages=[21], chunk=1)
    print(f"  info p21 cn export: {st}")

    cndoc = pymupdf.open(cn_path)
    cn_page = cndoc[0]
    cn_png = pb.render_page_png(cn_page, 1, dpi=110)
    open(os.path.join(PREVIEW_DIR, "p21_cn.png"), "wb").write(cn_png)

    bi_path = os.path.join(TMP_DIR, "p21_bi.pdf")
    st2 = po.export_bilingual_pdf(REAL_PDF, bi_path, segs, zh, pages=[21], chunk=1)
    print(f"  info p21 bilingual export: {st2}")
    bidoc = pymupdf.open(bi_path)
    bi_png = pb.render_page_png(bidoc[0], 1, dpi=110)
    open(os.path.join(PREVIEW_DIR, "p21_bilingual.png"), "wb").write(bi_png)

    # 三轮对比图：原页 | 中文页 | 差异热图
    a = _render(doc[20]).astype(np.int16)
    b = _render(cn_page).astype(np.int16)
    d = np.abs(a - b).max(axis=2)
    vis = np.stack([np.clip(a[:, :, 0], 0, 255).astype(np.uint8)] * 3, axis=2)
    vis[d > 24] = [255, 60, 60]
    panels = [a.astype(np.uint8), b.astype(np.uint8), vis]
    gap = np.full((a.shape[0], 12, 3), 230, np.uint8)
    comp = np.hstack([panels[0], gap, panels[1], gap, panels[2]])
    img = Image.fromarray(comp)
    dr = ImageDraw.Draw(img)
    try:
        fnt = ImageFont.truetype(r"C:\Windows\Fonts\Deng.ttf", 22)
    except Exception:
        fnt = ImageFont.load_default()
    labels = ["原页 (EN)", "中文页 (ZH)", "像素差异 (red)"]
    for i, lab in enumerate(labels):
        x = i * (a.shape[1] + 12) + 10
        dr.rectangle([x, 6, x + 190, 34], fill=(15, 17, 21))
        dr.text((x + 8, 8), lab, font=fnt, fill=(240, 240, 240))
    img.save(os.path.join(PREVIEW_DIR, "p21_overlay_diff.png"))

    for f in ("p21_cn.png", "p21_bilingual.png", "p21_overlay_diff.png"):
        p = os.path.join(PREVIEW_DIR, f)
        check(os.path.getsize(p) > 5000, f"{f} 已生成（{os.path.getsize(p)} bytes）")
    cndoc.close()
    bidoc.close()
    doc.close()


def collisions_of(cn_pdf, pages=None, tol=0.8):
    """列感知的文本行碰撞检测。

    只有 **x 与 y 都相交** 的两行才算重叠：同一 baseline 上的表格单元（x 互不相交）不是碰撞。
    spikes/qa_side_by_side.py 只用 y 判断，表格行被切成独立 table_cell 后会误报。
    """
    doc = pymupdf.open(cn_pdf)
    out = []
    for pno in (pages or range(1, doc.page_count + 1)):
        lines = []
        for b in doc[pno - 1].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b.get("lines", []):
                if any(s["text"].strip() for s in ln["spans"]) and ln["bbox"][2] - ln["bbox"][0] > 1:
                    lines.append((tuple(round(v, 2) for v in ln["bbox"]),
                                  "".join(s["text"] for s in ln["spans"])))
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, ta = lines[i]
                c, tc = lines[j]
                ox = min(a[2], c[2]) - max(a[0], c[0])
                oy = min(a[3], c[3]) - max(a[1], c[1])
                if ox > tol and oy > tol:
                    out.append((pno, round(ox, 2), round(oy, 2), ta[:32], tc[:32]))
    doc.close()
    return out


def qa_cn_pdf(path, pages=None, orig=None):
    """对已导出的中文 PDF 做碰撞体检（--qa 模式）。

    表格行距小于字号行框时，**原文自身**相邻行 bbox 也会相交（实测 p16 原文 16 对）。
    因此同时统计原页碰撞，只把"原文没有、中文页新增"的页判为失败。
    """
    print(f"\n[QA] {path}")
    if not os.path.exists(path):
        print(f"  SKIP 文件不存在：{path}")
        return 1
    doc = pymupdf.open(path)
    rng = pages or range(1, doc.page_count + 1)
    txt = "".join(doc[p - 1].get_text() for p in rng)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
    sizes = [s["size"] for p in rng for b in doc[p - 1].get_text("dict")["blocks"] if b.get("type") == 0
             for l in b.get("lines", []) for s in l["spans"] if s["text"].strip()]
    print(f"  pages={doc.page_count}  CJK chars={cjk}  最小字号={min(sizes) if sizes else '-'}  "
          f"shrunk/overflow 见导出统计")
    doc.close()
    bad = collisions_of(path, rng)
    orig_pages = {}
    if orig and os.path.exists(orig):
        for r in collisions_of(orig, rng):
            orig_pages[r[0]] = orig_pages.get(r[0], 0) + 1
        print(f"  原页自身碰撞：{sum(orig_pages.values())} 对（页 {sorted(orig_pages)}）— 属原文版式，非新增")
    new_pages = {}
    for r in bad:
        new_pages[r[0]] = new_pages.get(r[0], 0) + 1
    novel = {p: n for p, n in new_pages.items() if p not in orig_pages}
    print(f"  中文页碰撞：{sum(new_pages.values())} 对（页 {sorted(new_pages)}）")
    if novel:
        print(f"  FAIL 原文没有、中文页新增的碰撞页：{novel}")
        for r in bad:
            if r[0] in novel:
                print(f"     p{r[0]} overlap={r[1]}x{r[2]} {r[3]!r} <> {r[4]!r}")
        return 1
    print("  ok  没有新增碰撞（x/y 同时相交的文本行仅出现在原文也有的页）")
    return 0


# ==========================================================================
# --bench：401 页全量导出
# ==========================================================================

_CH_POOL = "这是一份飞行手册的中文测试译文用于验证渲染性能与版式计算的中文字符集"


def pseudo_zh(text: str, seed: int) -> str:
    """确定性伪中文（长度约为英文 0.7），仅用于性能基准。"""
    n = max(2, int(len(text) * 0.7))
    h = abs(hash((seed, text[:24]))) if False else (seed * 2654435761 + len(text)) & 0xFFFFFFFF
    out = []
    for i in range(n):
        h = (h * 1103515245 + 12345) & 0x7FFFFFFF
        out.append(_CH_POOL[h % len(_CH_POOL)])
    return "".join(out)


def bench(pages=None, chunk=20):
    print("\n[BENCH] 401 页中文 PDF 全量导出")
    if not os.path.exists(REAL_PDF):
        skip("真实 PDF 不存在")
        return
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _k32.GetCurrentProcess.restype = ctypes.c_void_p

    def peak_mb():
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ok = _psapi.GetProcessMemoryInfo(ctypes.c_void_p(_k32.GetCurrentProcess()),
                                             ctypes.byref(pmc), pmc.cb)
            if not ok:
                return -1.0
            return pmc.PeakWorkingSetSize / 1048576.0
        except Exception:
            return -1.0

    print(f"  peak working set (before) ≈ {peak_mb():.0f} MB")

    doc = pymupdf.open(REAL_PDF)
    n = doc.page_count if pages is None else min(pages, doc.page_count)
    t0 = time.time()
    segs = segments_from_pdf(doc, range(1, n + 1))
    t_extract = time.time() - t0
    zh = {}
    for s in segs:
        if s["role"] == "body" and s["text"].strip():
            zh[s["id"]] = pseudo_zh(s["text"], s["id"])
    print(f"  extract: {n} pages, {len(segs)} segments ({t_extract:.1f}s), "
          f"{len(zh)} translations ({sum(len(v) for v in zh.values())} chars)")

    out = os.path.join(TMP_DIR, "bench_cn.pdf")
    t0 = time.time()
    st = po.export_cn_pdf(REAL_PDF, out, segs, zh, chunk=chunk)
    dt = time.time() - t0
    print(f"  export_cn_pdf: {st['pages']} pages in {dt:.1f}s  大小 {st['bytes']/1048576:.1f} MB  "
          f"boxes={st['boxes']} lines={st['lines']} shrunk={st['shrunk']} overflow={st['overflow']}")
    print(f"  peak working set ≈ {peak_mb():.0f} MB")
    chk = pymupdf.open(out)
    ok = chk.page_count == n
    txt = "".join(chk[p].get_text() for p in range(min(5, chk.page_count)))
    print(f"  reopen ok={ok} pages={chk.page_count}  前 5 页抽取字符={len(txt)}")
    chk.close()
    doc.close()
    st["elapsed_extract"] = round(t_extract, 1)
    st["peak_mb"] = round(peak_mb(), 1)
    return st


# ==========================================================================

def main(argv):
    t0 = time.time()
    print("=" * 72)
    print("tests/test_render.py — 渲染层自测")
    print("=" * 72)
    os.makedirs(TMP_DIR, exist_ok=True)
    if "--bench" in argv:
        bench(pages=None)
        print(f"\n共 {time.time()-t0:.1f}s")
        return 0
    if "--qa" in argv:
        i = argv.index("--qa")
        path = argv[i + 1] if len(argv) > i + 1 else ""
        pages = None
        if "--pages" in argv:
            j = argv.index("--pages")
            m = re.match(r"^(\d+)(?:-(\d+))?$", argv[j + 1] if len(argv) > j + 1 else "")
            if m:
                pages = list(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
        orig = REAL_PDF if "--no-orig" not in argv else None
        rc = qa_cn_pdf(path, pages, orig=orig)
        print(f"\n共 {time.time()-t0:.1f}s")
        return rc
    try:
        test_toc_dot_leader()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILURES.append(f"test_toc_dot_leader 异常: {type(e).__name__}: {e}")
    try:
        test_synthetic()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILURES.append(f"test_synthetic 异常: {type(e).__name__}: {e}")
    try:
        test_layout_real()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILURES.append(f"test_layout_real 异常: {type(e).__name__}: {e}")
    try:
        write_previews()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILURES.append(f"write_previews 异常: {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED {len(FAILURES)} / {len(FAILURES) + len(SKIPS)}  ({time.time()-t0:.1f}s)")
        for f in FAILURES:
            print("  -", f)
        return 1
    print(f"ALL PASS  （skip {len(SKIPS)}）  {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
