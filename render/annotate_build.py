"""图示标签标注：批量构建（OCR → 去重 → 翻译 → 标注）。

这是「方案 1：标签双层」的正式实现，与 render/annotate.py 配套：
  * annotate.py 负责布局与绘制
  * 本模块负责**抽取该页图内文字、与主文本管线去重、翻译**，并落盘成
    `data/derived/ocr/pN_zh.json`，供 annotate 使用。

关键设计
--------
1. **只处理真正需要的页**：若某页的"图片"其实是正文截图，文字本就可抽取并已由主
   管线翻译，再标一遍就是重复。用「非页眉图片占比 + 现有正文字数」筛，
   实测 401 页里只有 25 页需要。
2. **与主管线去重**（render/ocr_dedup.py）：OCR 会把落在图片 bbox 内的正文也检出来，
   这些按文字相同 / 几何包含两种规则剔除，避免同一句话出现两份译文。
3. **OCR 结果按 (页码, dpi) 缓存**：OCR 是本地 CPU 计算，重复跑没必要。
4. **翻译复用项目自己的引擎**（术语表一致、可缓存），并落盘 so 重复运行近乎免费。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from config import load as load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_DIR = os.path.join(ROOT, "data", "derived", "ocr")
SCAN_CACHE = os.path.join(OCR_DIR, "_page_scan.json")

# ALL-CAPS 词表：OCR 与原图都会把大写词连排（ALTGEAR / MASTERCAUTIONLIGHT），
# 用最长匹配切分后模型才肯翻译。
_VOCAB = [
    "MASTER", "CAUTION", "WARNING", "INDICATOR", "REMAINING", "PRESSURE", "CONSOLE",
    "SENSOR", "INT", "SWITCH", "PANEL", "LIGHTS", "LIGHT", "EYEBROW", "INDEXER", "BACKUP",
    "AUDIO", "ENGINE", "START", "ALT", "INSTRUMENT", "MODE", "SPEED", "BRAKE", "CABIN",
    "CLOCK", "COMPASS", "NUCLEAR", "MANUAL", "TRIM", "EXTERNAL", "AVIONIC", "POWER",
    "FUEL", "FLOW", "GEAR", "LEFT", "RIGHT", "UHF", "HUD", "MFD", "AOA", "VVI", "RPM",
    "FTIT", "OIL", "NOZZLE", "MISC", "IFF", "EPU", "ELEC", "ECM", "AVTR", "FLCS", "PFLD",
    "TWP", "TWA", "MPO", "KY", "ANTI", "ICE", "DED", "ICP", "RWR", "ILI", "TEST", "CENTER",
    "QTY", "HYD", "COURSE", "STORES", "ADI", "NM", "EXT", "SET", "HORN", "ROLL", "PULL",
    "STICK", "PITCH", "BANK", "HORIZON", "NOSE", "RECOVER", "ACCELERATE", "UNLOAD",
    "NEUTRAL", "BALANCED", "POWER", "MIN", "AGL", "CAS", "RECOVERY", "MANEUVER",
]
_VOCAB_SORTED = sorted(set(_VOCAB), key=len, reverse=True)

# 已经国际通用的缩写，保留原样
KEEP_AS_IS = re.compile(
    r"^(?:AOA|VVI|RPM|FTIT|MFD|HUD|CMDS|HMCS|IFF|EPU|ELEC|ECM|AVTR|FLCS|PFLD|TWP|TWA|MPO|"
    r"ADI|PFD|KY\s*58|UHF|MISC|GEAR|FUEL|TEST|OIL|NOZZLE|BACKUP|NUCLEAR|CLOCK|COMPASS|"
    r"AUDIO\s*\d?|AIRCOND|MANUAL\s*TRIM|FUEL\s*QTY|SYS|NWS|TACAN|ILS|FCR|HSD|RWR|DED|ICP)$",
    re.I)

SKIP_PATTERNS = [
    re.compile(r"^\(\s*N/?I\s+in\s+BMS\s*\)$", re.I),
    re.compile(r"^[*\-–—.\s]+$"),
]


def _split_caps(word: str) -> List[str]:
    if not word.isupper() or len(word) < 5:
        return [word]
    out, i = [], 0
    while i < len(word):
        for v in _VOCAB_SORTED:
            if word.startswith(v, i):
                out.append(v)
                i += len(v)
                break
        else:
            if out:
                out[-1] += word[i]
            else:
                out.append(word[i])
            i += 1
    return out if len(out) > 1 else [word]


def norm_label(t: str) -> str:
    """修 OCR 常见毛病：全大写词粘连、字母数字粘连、大小写边界。"""
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"([A-Za-z])(\d)", r"\1 \2", t)
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    if t and t.isupper() and " " not in t:
        t = " ".join(_split_caps(t))
    return t


# --------------------------------------------------------------------------
# 1) 选页
# --------------------------------------------------------------------------
def pick_pages(pdf_path: str, *, force: bool = False) -> List[Dict[str, Any]]:
    """返回需要图内标注的页。结果缓存到 data/derived/ocr/_page_scan.json。"""
    if os.path.exists(SCAN_CACHE) and not force:
        try:
            return [r for r in json.load(open(SCAN_CACHE, encoding="utf-8")) if r.get("needs")]
        except Exception:
            pass
    import pymupdf
    from core import db as _db
    con = _db.connect()
    seg_by_page: Dict[int, int] = {}
    for r in con.execute("SELECT page, text FROM segments WHERE version_id=2 AND role='body'"):
        seg_by_page[int(r["page"])] = seg_by_page.get(int(r["page"]), 0) + len(r["text"] or "")
    con.close()

    d = pymupdf.open(pdf_path)
    out = []
    for i in range(d.page_count):
        p = d[i]
        im_area = biggest = 0.0
        for info in p.get_image_info(xrefs=True):
            x0, y0, x1, y1 = info["bbox"]
            if (y1 - y0) < 80:
                continue
            a = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            im_area += a
            biggest = max(biggest, a)
        page_area = p.rect.width * p.rect.height
        frac = im_area / page_area
        live = seg_by_page.get(i + 1, 0)
        needs = (frac > 0.25 and live < 400) or (biggest / page_area > 0.45)
        out.append({"page": i + 1, "img_frac": round(frac, 3),
                    "live_chars": live, "needs": bool(needs)})
    d.close()
    os.makedirs(OCR_DIR, exist_ok=True)
    json.dump(out, open(SCAN_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return [r for r in out if r["needs"]]


# --------------------------------------------------------------------------
# 2) OCR
# --------------------------------------------------------------------------
_OCR_ENGINE = None


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_page(pdf_path: str, page_no: int, *, dpi: int = 300, force: bool = False) -> Optional[dict]:
    """OCR 一页的图内区域，返回 {page, dpi, image_bbox, texts:[...]}；结果带缓存。"""
    cache = os.path.join(OCR_DIR, f"p{page_no}_ocr.json")
    if os.path.exists(cache) and not force:
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    import pymupdf
    os.makedirs(OCR_DIR, exist_ok=True)
    d = pymupdf.open(pdf_path)
    p = d[page_no - 1]
    big = None
    for info in p.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        if (y1 - y0) > 80:
            big = info
            break
    if big is None:
        clip = pymupdf.Rect(0, 66, p.rect.width, 776)
    else:
        clip = pymupdf.Rect(big["bbox"])
    pix = p.get_pixmap(dpi=dpi, clip=clip)
    img = os.path.join(OCR_DIR, f"p{page_no}_diagram_{dpi}.png")
    pix.save(img)
    d.close()

    res, _ = _ocr_engine()(img)
    texts = []
    for box, text, score in (res or []):
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        texts.append({"text": text, "score": float(score),
                      "x0": round(min(xs), 1), "y0": round(min(ys), 1),
                      "x1": round(max(xs), 1), "y1": round(max(ys), 1)})
    data = {"page": page_no, "dpi": dpi,
            "image_bbox": [clip.x0, clip.y0, clip.x1, clip.y1], "texts": texts}
    json.dump(data, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return data


# --------------------------------------------------------------------------
# 3) 翻译标签
# --------------------------------------------------------------------------
def translate_labels(page_no: int, raw: dict, *, doc_id: int = 1, version_id: int = 2,
                     verbose: bool = False) -> Optional[dict]:
    """把 OCR 结果过滤 + 去重 + 翻译，落盘 pN_zh.json。"""
    from core import db as _db, store as _store
    from translate import engine

    con = _db.connect()
    segs = [{"bbox": json.loads(r["bbox"] or "[]"), "text": r["text"] or ""}
            for r in con.execute(
                "SELECT bbox, text FROM segments WHERE version_id=? AND page=?",
                (version_id, page_no))]
    con.close()

    from render.ocr_dedup import dedup
    kept, dropped_text, dropped_geo = dedup(page_no, raw["texts"], segs)

    labels = []
    for k in kept:
        lab = norm_label(k["text"])
        if len(lab) < 2 or any(p.search(lab) for p in SKIP_PATTERNS):
            continue
        # 长句是图注/正文，属于主管线职责
        if len(lab) > 60 or lab.count(" ") > 8:
            continue
        labels.append({**k, "label": lab})

    todo = []
    seen = set()
    for k in labels:
        if KEEP_AS_IS.match(k["label"]):
            k["zh"] = k["label"]
            continue
        if k["label"].lower() in seen:
            continue
        seen.add(k["label"].lower())
        todo.append(k)

    base = 900000 + page_no * 1000
    if todo:
        # 先查跨版本译文缓存：同一个图示标签（例如各页都有的 "MIL POWER"）只翻一次；
        # 重跑时直接命中，不再调用 API。
        from core import fingerprint as _fp
        cached = {}
        try:
            from core import db as _db2
            con2 = _db2.connect()
            for k in todo:
                row = con2.execute(
                    "SELECT text FROM translation_cache WHERE fingerprint=?",
                    (_fp.fingerprint(k["label"]),)).fetchone()
                if row and (row["text"] or "").strip():
                    cached[k["label"].lower()] = row["text"]
            con2.close()
        except Exception:
            cached = {}

        pending = [k for k in todo if k["label"].lower() not in cached]
        for k in todo:
            hit = cached.get(k["label"].lower())
            if hit:
                k["zh"] = hit
                k["cached"] = True

        got = {}
        if pending:
            items = [engine._Item(base + todo.index(k), k["label"], page_no,
                                  "body", "paragraph") for k in pending]
            cfg = dict(load_config())
            api_key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
            gl = engine.glossary.prompt_block()
            for i in range(0, len(items), 12):
                r = engine._run_batch(items[i:i + 12], {}, provider="deepseek",
                                      model=cfg.get("model", "deepseek-flash"), cfg=cfg,
                                      api_key=api_key, glossary_block=gl)
                if r.get("ok"):
                    got.update(r["out"])
        for k in todo:
            if k.get("zh"):
                continue
            zh = got.get(base + todo.index(k))
            if zh:
                k["zh"] = zh
            else:
                k["zh"] = k["label"]
        # 写回跨版本缓存：同一个标签（各页重复出现的 "MIL POWER" 等）只翻一次
        if got:
            try:
                from core import db as _db3, fingerprint as _fp2, store as _store3
                con3 = _db3.connect()
                cfg3 = load_config()
                for k in todo:
                    if k.get("cached"):
                        continue
                    _store3.put_cached_translation(
                        con3, fingerprint=_fp2.fingerprint(k["label"]), text=k["zh"],
                        provider=cfg3.get("provider") or "deepseek",
                        model=cfg3.get("model") or "")
                con3.commit()
                con3.close()
            except Exception:
                pass

    out = {"page": page_no, "dpi": raw["dpi"], "image_bbox": raw["image_bbox"],
           "labels": labels,
           "stats": {"ocr": len(raw["texts"]), "kept": len(kept),
                     "drop_text": dropped_text, "drop_geo": dropped_geo,
                     "translated": len(todo)}}
    path = os.path.join(OCR_DIR, f"p{page_no}_zh.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if verbose:
        print(f"  p{page_no}: ocr={len(raw['texts'])} kept={len(kept)} "
              f"drop(text/geo)={dropped_text}/{dropped_geo} translated={len(todo)} "
              f"-> {os.path.basename(path)}")
    return out


# --------------------------------------------------------------------------
# 4) 批量入口
# --------------------------------------------------------------------------
def build(pdf_path: str, pages: Optional[List[int]] = None, *, dpi: int = 300,
          force: bool = False, verbose: bool = True) -> dict:
    """对指定页（默认自动选页）跑完整流程，返回统计。"""
    targets = pages if pages else [r["page"] for r in pick_pages(pdf_path, force=force)]
    t0 = time.perf_counter()
    ok, skipped, labels_total = 0, [], 0
    for pno in targets:
        raw = ocr_page(pdf_path, pno, dpi=dpi, force=force)
        if not raw or not raw.get("texts"):
            skipped.append(pno)
            if verbose:
                print(f"  p{pno}: 未检出文字，跳过")
            continue
        res = translate_labels(pno, raw, verbose=verbose)
        if res:
            ok += 1
            labels_total += len([l for l in res["labels"] if l.get("zh") and l["zh"] != l["label"]])
    dt = time.perf_counter() - t0
    return {"pages": len(targets), "built": ok, "skipped": skipped,
            "labels": labels_total, "seconds": round(dt, 1)}
