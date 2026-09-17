"""render/pdfout.py — 中文 PDF / 双语 PDF 导出（CONTRACT §6）。

* 每页用 ``pagebuild.rebuild_page`` 做元素级重建（图片 + 矢量 + 非正文文本），
  再叠加译文；**不使用 show_pdf_page**。
* 401 页必须能在 10 分钟内跑完、内存 < 2GB：每 ``chunk``（默认 20）页 save 到临时文件，
  最后用 ``insert_pdf`` 组装（insert_pdf 保留可编辑文本层，不会栅格化）。
* 页眉 / 页脚 / 页码保持原样（白字 + banner 图重建），原文书签（TOC）与元数据复制到输出。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf

from . import pagebuild as pb

__all__ = [
    "export_cn_pdf",
    "export_bilingual_pdf",
    "build_cn_page",
    "build_bilingual_page",
    "render_cn_page_png",
    "render_bilingual_page_png",
]


# --------------------------------------------------------------------------
# 输入归一化
# --------------------------------------------------------------------------

def _open_src(src_pdf):
    if isinstance(src_pdf, pymupdf.Document):
        return src_pdf, False
    if isinstance(src_pdf, pymupdf.Page):
        return src_pdf.parent, False
    return pymupdf.open(str(src_pdf)), True


def _group_by_page(segments_by_page) -> Dict[int, list]:
    """{page: [seg]}；也接受「扁平段列表」（按 seg.page 分组）。"""
    if segments_by_page is None:
        return {}
    if isinstance(segments_by_page, dict):
        out: Dict[int, list] = {}
        for k, v in segments_by_page.items():
            try:
                out[int(k)] = list(v or [])
            except Exception:
                continue
        return out
    out = {}
    for s in segments_by_page:
        p = int(pb._val(s, "page", 1) or 1)
        out.setdefault(p, []).append(s)
    return out


def _page_list(pages, page_count: int) -> List[int]:
    """归一化页参数（与 pipeline.normalize_pages 语义一致）。

    * None      -> 全书
    * int       -> 单页
    * (from,to) -> 区间（这是 CLI `--pages a-b` 的形态）
    * list      -> 显式页列表

    ⚠️ 曾经把 tuple 当"页列表"处理，导致 `pages=(1,31)` 只导出 2 页
    （verifier 报的 M-8b）。这里与 pipeline 对齐，避免同一个参数名两种语义。
    """
    if pages is None:
        return list(range(1, page_count + 1))
    if isinstance(pages, int):
        return [int(pages)] if 1 <= int(pages) <= page_count else []
    if isinstance(pages, tuple) and len(pages) == 2:
        a, b = int(pages[0]), int(pages[1])
        if a > b:
            a, b = b, a
        return [p for p in range(max(1, a), min(page_count, b) + 1)]
    out = []
    for p in pages:
        q = int(p)
        if 1 <= q <= page_count:
            out.append(q)
    return out


def _pick_font(cjk_font_path: Optional[str]) -> str:
    return cjk_font_path or pb.resolve_cjk_font()


def _accumulate(total: dict, st: dict) -> None:
    total["boxes"] += int(st.get("boxes", 0))
    total["overflow"] += int(st.get("overflow", 0))
    total["shrunk"] += int(st.get("shrunk", 0))
    total["lines"] += int(st.get("lines", 0))
    total["drawn"] += int(st.get("drawn", 0))


def _write_chunk(part: pymupdf.Document, final: pymupdf.Document, tmpdir: str, idx: int) -> None:
    """分块落盘再 insert_pdf，控制内存峰值。"""
    tmp = os.path.join(tmpdir, f"part_{idx:04d}.pdf")
    part.save(tmp, deflate=True, garbage=3, clean=True)
    part.close()
    doc = pymupdf.open(tmp)
    final.insert_pdf(doc)
    doc.close()
    try:
        os.remove(tmp)
    except OSError:
        pass
    pb.clear_caches()


def build_cn_outline(conn, version_id: int, *, prev_pdf: Optional[str] = None,
                     same_pdf: Optional[str] = None) -> list:
    """构造中文书签（TOC）。

    由来：源 PDF（v2 demo）本身没有书签，直接 `src.get_toc()` 会得到空目录。
    做法：用一份**带书签的参考 PDF** 提供「层级 + 页 + 小节号」这套经过人工编排的结构，
    再把每个条目匹配到**当前版本**的段，取其**中文译文**作为书签标题。
    匹配优先用小节号（跨版本稳定），其次标题指纹。

    参考 PDF 的取法（按优先级）：
      1. `prev_pdf`（上一版源 PDF）—— 适用于"同一本书出了新版"的场景；
      2. `same_pdf`（当前版自己的源 PDF）—— 适用于 TO 手册这种**源文件自带 561/1093 条
         原生书签**的情况（Adobe PDF Library 生成，质量很好）。

    返回 pymupdf `set_toc` 需要的 `[[level, title, page], ...]`。
    """
    import re
    from core import db as _db, fingerprint as _fp, store as _store
    try:
        import pymupdf
    except Exception:
        return []

    used_ref = None
    toc1 = []
    for ref in (prev_pdf, same_pdf):
        if not ref or not os.path.exists(ref):
            continue
        try:
            d = pymupdf.open(ref)
            t = d.get_toc() or []
            d.close()
        except Exception:
            continue
        if t:
            toc1, used_ref = t, ref
            break
    if not toc1:
        return []

    sec_re = re.compile(r"^\s*(\d+[A-Z]?(?:\.\d+)*)")
    bare_re = re.compile(r"^\s*\d+[A-Z]?(?:\.\d+)*\s*$")

    segs = _store.segments_of_version(conn, version_id)
    tr = _store.translations_of_version(conn, version_id)
    by_sec: dict = {}
    by_fp: dict = {}
    # 按页 + order_index 排序，便于找"下一段"
    ordered = sorted(segs, key=lambda x: (int(x["page"]), int(x["order_index"])))
    pos = {int(s["id"]): i for i, s in enumerate(ordered)}
    for s in sorted(segs, key=lambda x: (x["kind"] != "heading", x["order_index"])):
        m = sec_re.match(s["text"] or "")
        if m:
            by_sec.setdefault(m.group(1), []).append(s)
        by_fp.setdefault(s["fingerprint"], []).append(s)

    def _title_for(s) -> str:
        """取段的中文标题。

        TO-1 这类手册把**小节号与标题切成两个段**（`'1.1'` + `'Aircraft General Arrangement'`），
        只取号会得到 '1.1' 这种没意义的书签。规则：
          * 本段文本若**只有小节号**（或极短），则把紧随其后的段文本接上；
          * 优先用两段各自的中文译文拼接。
        """
        raw = " ".join((s["text"] or "").split())
        zh = " ".join(((tr.get(s["id"]) or {}).get("text") or s["text"] or "").split())
        # 尾部残留的裸编号（'…与控制 2.'）是上游译文缺陷：模型把下一段的编号
        # （'2.1 GENERAL AND...'）截了一半贴到本段末尾。书签是导航用的，
        # 直接去掉这个尾巴，不要让它出现在目录里。
        zh = re.sub(r"\s*\d+[A-Z]?(?:\.\d+)*\.?\s*$", "", zh).strip() or zh
        # 反过来，译文的**编号被吃掉**的情况（'2.1 GENERAL AND…' → '1 一般与杂项控制'）
        # 也要补回来：原文带小节号而译文开头不是同一个号时，用原文的号。
        if sec and sec.group(1):
            m_zh = re.match(r"^\s*(\d+[A-Z]?(?:\.\d+)*)", zh)
            if not m_zh or m_zh.group(1) != sec.group(1):
                if re.match(r"^\s*\d+\s", zh) or not m_zh:
                    stripped = re.sub(r"^\s*\d+[A-Z]?(?:\.\d+)*\s*", "", zh).strip()
                    zh = (sec.group(1) + " " + stripped).strip()
        # 「只有小节号」的形态：'1.1' / '1.5.2' / '2.10'（允许带尾部点）。
        # ⚠️ 不要用 len(raw) <= 8 这种粗暴判断 —— 'FOREWARD'(8) / '2.2 The Mission'
        # 这类**完整短标题**会被误判，于是把紧随其后的正文段拼进书签，
        # 得到「前言 本手册包含安全高效操作飞机所需的信息…」这种超长条目。
        only_num = bool(bare_re.match(raw))
        if only_num:
            i = pos.get(int(s["id"]))
            # 往后找第一个"真标题"：跳过纯数字/纯符号/空段。
            # TO-34 的 '2.' 后面紧跟 '1'（另一个编号段）再才是标题，
            # 只跳过紧邻一段会把 '1 一般与杂项控制' 拼进来。
            if i is not None:
                for j in range(i + 1, min(i + 5, len(ordered))):
                    nxt = ordered[j]
                    if int(nxt["page"]) != int(s["page"]):
                        break
                    nraw = " ".join((nxt["text"] or "").split())
                    if not nraw or bare_re.match(nraw) or nraw.isdigit():
                        continue
                    if not re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", nraw):
                        continue
                    nzh = " ".join(((tr.get(nxt["id"]) or {}).get("text") or nraw).split())
                    zh = f"{zh} {nzh}".strip()
                    break
        return zh

    out = []
    for level, title, page in toc1:
        t = (title or "").strip()
        if not t or bare_re.match(t):
            continue
        sec = sec_re.match(t)
        hit = None
        want = int(page)
        if sec and sec.group(1) in by_sec:
            def _score(s):
                # 权重顺序很重要（实测教训）：
                # 书签的**页码**是人工编排的结果，是最可信的线索；
                # 只按"标题匹配 + heading 加分"会让目录页上的同名行胜出
                # （TO-1 的 '1.1 Aircraft General Arrangement' 书签在 p19，
                #  但 p3 目录页也有一行 '1.1 飞机总体布局....'，曾因此把书签指到 p3）。
                d = abs(int(s["page"]) - want)
                sc = -min(d, 60) * 0.35                 # ① 页距离：主导项
                tn = _fp.normalize(t).casefold()
                st = _fp.normalize(s["text"]).casefold()
                if st.startswith(tn):
                    sc += 1.6                            # ② 标题完全对得上
                elif tn and tn in st:
                    sc += 0.8
                if int(s["page"]) <= 10 and d > 12:
                    sc -= 3.0                            # ③ 目录区行，强烈降权
                if s["kind"] == "heading":
                    sc += 0.4                            # ④ 轻微偏好 heading
                return sc
            hit = max(by_sec[sec.group(1)], key=_score)
        if hit is None:
            hit = (by_fp.get(_fp.fingerprint(t)) or [None])[0]
        if hit is None:
            continue
        zh = _title_for(hit)
        if not zh:
            continue
        # 书签是导航用的，太长反而看不清（TO-1 的 FOREWARD 一度把整段前言塞了进去）
        if len(zh) > 64:
            zh = zh[:62] + "…"
        out.append([int(level), zh, int(hit["page"])])
    return out


def _finish(final: pymupdf.Document, out_pdf: str, src: pymupdf.Document, full: bool,
            title_suffix: str, extra_meta: Optional[dict] = None,
            cn_toc: Optional[list] = None) -> int:
    """TOC + 元数据 + 落盘。返回字节数。

    书签优先用调用方传进来的 `cn_toc`（中文目录，由 build_cn_outline 生成）；
    没有时才回退到源 PDF 自带的书签（v1 有 231 条，v2 demo 为 0）。

    ⚠️ `set_toc` 对超出本文档页数的条目会抛
    `ValueError: row N: page number out of range`，**整份目录都会被拒**。
    导出子集（如只导 1–31 页）时目录里必然有超范围的条目，
    所以这里先按实际页数过滤，而不是让它整个失败。
    """
    try:
        toc = list(cn_toc or [])
        if not toc and full:
            toc = src.get_toc() or []
        if toc:
            npages = final.page_count
            kept = [[int(lv), str(t), int(pg)] for lv, t, pg in toc
                    if 1 <= int(pg) <= npages and 1 <= int(lv) <= 12]
            # level 不能跳级（首条必须是 1，且一次最多降/升一级）
            norm, prev = [], 0
            for lv, t, pg in kept:
                lv = 1 if prev == 0 else min(lv, prev + 1)
                norm.append([lv, t, pg])
                prev = lv
            if norm:
                final.set_toc(norm)
    except Exception as exc:                      # 不再静默：至少留个痕迹
        print(f"[pdfout] 写入 PDF 目录失败（已跳过）: {type(exc).__name__}: {exc}")
    try:
        md = dict(src.metadata or {})
    except Exception:
        md = {}
    md = {k: v for k, v in md.items() if k in
          ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate")}
    if md.get("title"):
        md["title"] = f"{md['title']}{title_suffix}"
    else:
        md["title"] = os.path.splitext(os.path.basename(str(out_pdf)))[0]
    md.pop("encryption", None)
    if extra_meta:
        md.update(extra_meta)
    try:
        final.set_metadata(md)
    except Exception:
        pass
    d = os.path.dirname(os.path.abspath(out_pdf))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    try:
        # 中文字体（Deng.ttf ≈ 10MB）全量嵌入会让输出暴涨，按实际用到的字形子集化
        final.subset_fonts()
    except Exception:
        pass
    final.save(out_pdf, deflate=True, garbage=3)
    return os.path.getsize(out_pdf)


# --------------------------------------------------------------------------
# 单页构建
# --------------------------------------------------------------------------

def build_cn_page(src_doc, page_no: int, segments, translations, *, target_doc=None,
                  cjk_font_path: Optional[str] = None, dx: float = 0.0,
                  stats: Optional[dict] = None) -> pymupdf.Page:
    """单页中文页（重建 + 擦除已翻译正文 + 绘制译文）。"""
    return pb.build_translated_page(src_doc, page_no, segments, translations,
                                    cjk_font_path=_pick_font(cjk_font_path),
                                    target_doc=target_doc, dx=dx, stats=stats)


def _erase_rects_for(page_segs, translations) -> List[pymupdf.Rect]:
    tr = pb._normalize_translations(translations)
    out: List[pymupdf.Rect] = []
    for s in page_segs:
        role = str(pb._val(s, "role", "body") or "body")
        if role in ("header", "footer", "pagenum", "noise"):
            continue
        sid = int(pb._val(s, "id", 0) or 0)
        zh = tr.get(sid, "")
        if not zh:
            t = pb._val(s, "translation", None)
            if isinstance(t, str):
                zh = t
            elif t:
                zh = pb._val(t, "text", "") or ""
        if str(zh or "").strip():
            r = pb._as_rect(pb._val(s, "bbox"))
            if r is not None:
                out.append(r)
    return out


def _draw_tabs(page, W: float, H: float, cjk_font_path: str, labels=("EN", "中文")) -> None:
    """双语页左右两个页签（深蓝底 + 白字），并画中缝分隔线。"""
    try:
        page.draw_line(pymupdf.Point(W, 0), pymupdf.Point(W, H),
                       color=(0.42, 0.44, 0.48), width=0.8)
    except Exception:
        pass
    for i, lab in enumerate(labels):
        x0 = i * W + W - 34.0
        rect = pymupdf.Rect(x0, 4.0, x0 + 28.0, 19.5)
        try:
            page.draw_rect(rect, color=None, fill=(0.13, 0.30, 0.47), width=0)
            f = pb._font(cjk_font_path) if any(ord(c) > 127 for c in lab) else pb._font(pb.LATIN_BOLD)
            tw = pymupdf.TextWriter(page.rect)
            tw.append(pymupdf.Point(rect.x0 + 5.0, rect.y0 + 11.0), lab, font=f, fontsize=9.0)
            tw.write_text(page, color=(1, 1, 1))
        except Exception:
            continue


def build_bilingual_page(src_doc, page_no: int, segments, translations, *, target_doc=None,
                         cjk_font_path: Optional[str] = None, layout: str = "facing",
                         gap: float = 0.0, stats: Optional[dict] = None) -> pymupdf.Page:
    """双语页：宽 = 2 × 原页宽。左半 = 原页 1:1 元素级重建，右半 = 中文页。"""
    src_page = src_doc[page_no - 1]
    W = src_page.rect.width
    H = src_page.rect.height
    out = target_doc if target_doc is not None else pymupdf.open()
    page = out.new_page(width=2 * W + (gap if layout == "facing_gap" else 0.0), height=H)
    dx = gap if layout == "facing_gap" else 0.0
    # 左半：完整原页（含两端对齐正文）
    pb.rebuild_page(src_doc, page_no, erase_roles=(), target_doc=out, target_page=page)
    # 右半：擦掉已翻译正文后重建
    page_segs = [s for s in segments if int(pb._val(s, "page", 1) or 1) == page_no]
    erase = _erase_rects_for(page_segs, translations)
    pb.rebuild_page(src_doc, page_no, erase_roles=(), erase_rects=erase,
                    target_doc=out, target_page=page, dx=W + dx)
    boxes = pb.compute_layout(segments, translations, page_no=page_no, src=src_doc, dx=W + dx)
    st = pb.draw_translated(page, boxes=boxes, cjk_font_path=_pick_font(cjk_font_path))
    st["boxes"] = len(boxes)
    _draw_tabs(page, W, H, _pick_font(cjk_font_path))
    if stats is not None:
        stats.clear()
        stats.update(st)
    return page


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------

def export_cn_pdf(src_pdf, out_pdf, segments_by_page, translations, *, pages=None,
                  cjk_font_path: Optional[str] = None, dpi: int = 110, chunk: int = 20,
                  on_page: Optional[Callable[[int, int], None]] = None,
                  cn_toc: Optional[list] = None) -> dict:
    """导出中文 PDF。返回 {pages,boxes,overflow,shrunk,lines,bytes,path,elapsed}。

    `cn_toc` 由 `build_cn_outline()` 生成；不传则回退到源 PDF 自带书签。
    """
    src, close_src = _open_src(src_pdf)
    by_page = _group_by_page(segments_by_page)
    plist = _page_list(pages, src.page_count)
    cjk = _pick_font(cjk_font_path)
    total = {"pages": 0, "boxes": 0, "overflow": 0, "shrunk": 0, "lines": 0, "drawn": 0}
    final = pymupdf.open()
    tmpdir = tempfile.mkdtemp(prefix="mt_cn_")
    t0 = time.time()
    try:
        for ci in range(0, len(plist), max(1, chunk)):
            part = pymupdf.open()
            for pno in plist[ci:ci + max(1, chunk)]:
                st: dict = {}
                build_cn_page(src, pno, by_page.get(pno, []), translations,
                              target_doc=part, cjk_font_path=cjk, stats=st)
                _accumulate(total, st)
                total["pages"] += 1
                if on_page is not None:
                    on_page(total["pages"], len(plist))
            _write_chunk(part, final, tmpdir, ci // max(1, chunk))
        nbytes = _finish(final, str(out_pdf), src, len(plist) == src.page_count, " (中文)",
                         cn_toc=cn_toc)
    finally:
        try:
            final.close()
        except Exception:
            pass
        if close_src:
            src.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        pb.clear_caches()
    total.update({"bytes": nbytes, "path": str(out_pdf), "elapsed": round(time.time() - t0, 1)})
    return total


def export_bilingual_pdf(src_pdf, out_pdf, segments_by_page, translations, *, pages=None,
                         cjk_font_path: Optional[str] = None, dpi: int = 110, chunk: int = 20,
                         layout: str = "facing", on_page: Optional[Callable[[int, int], None]] = None,
                         cn_toc: Optional[list] = None) -> dict:
    """导出中英对照 PDF：每页宽 = 2 × 原页宽（左原文 / 右中文）。"""
    src, close_src = _open_src(src_pdf)
    by_page = _group_by_page(segments_by_page)
    plist = _page_list(pages, src.page_count)
    cjk = _pick_font(cjk_font_path)
    total = {"pages": 0, "boxes": 0, "overflow": 0, "shrunk": 0, "lines": 0, "drawn": 0}
    final = pymupdf.open()
    tmpdir = tempfile.mkdtemp(prefix="mt_bi_")
    t0 = time.time()
    try:
        for ci in range(0, len(plist), max(1, chunk)):
            part = pymupdf.open()
            for pno in plist[ci:ci + max(1, chunk)]:
                st: dict = {}
                build_bilingual_page(src, pno, by_page.get(pno, []), translations,
                                     target_doc=part, cjk_font_path=cjk, layout=layout, stats=st)
                _accumulate(total, st)
                total["pages"] += 1
                if on_page is not None:
                    on_page(total["pages"], len(plist))
            _write_chunk(part, final, tmpdir, ci // max(1, chunk))
        nbytes = _finish(final, str(out_pdf), src, len(plist) == src.page_count, " (中英对照)",
                         cn_toc=cn_toc)
    finally:
        try:
            final.close()
        except Exception:
            pass
        if close_src:
            src.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        pb.clear_caches()
    total.update({"bytes": nbytes, "path": str(out_pdf), "elapsed": round(time.time() - t0, 1)})
    return total


# --------------------------------------------------------------------------
# 单页图片（Web 前端按需渲染）
# --------------------------------------------------------------------------

def render_cn_page_png(src_doc, page_no: int, segments, translations, *, cjk_font_path=None,
                       dpi: int = 110, webp: bool = False, quality: int = 80) -> bytes:
    page = build_cn_page(src_doc, page_no, segments, translations, cjk_font_path=cjk_font_path)
    doc = page.parent
    try:
        png = pb.render_page_png(page, 1, dpi=dpi)
        if not webp:
            return png
        return pb.render_page_webp(page, 1, dpi=dpi, quality=quality)
    finally:
        doc.close()
        pb.clear_caches()


def render_bilingual_page_png(src_doc, page_no: int, segments, translations, *, cjk_font_path=None,
                              dpi: int = 110, webp: bool = False, quality: int = 80) -> bytes:
    page = build_bilingual_page(src_doc, page_no, segments, translations, cjk_font_path=cjk_font_path)
    doc = page.parent
    try:
        png = pb.render_page_png(page, 1, dpi=dpi)
        if not webp:
            return png
        return pb.render_page_webp(page, 1, dpi=dpi, quality=quality)
    finally:
        doc.close()
        pb.clear_caches()
