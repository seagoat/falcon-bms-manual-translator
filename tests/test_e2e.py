#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_e2e.py —— 真实 401 页手册的端到端测试。

可执行、退出码明确：

    python tests\\test_e2e.py            # 全量（约 2-4 分钟）
    python tests\\test_e2e.py --fast     # 只抽前 60 页再生成 v2（约 40s，用于开发迭代）
    python tests\\test_e2e.py --keep     # 保留临时 data 目录（排查用）

流程（每一步都独立断言，失败不中断，最后汇总退出码）：
    1. ingest v1（真实 401 页 PDF）
    2. simulate-update 生成 v2 PDF（删除 2 / 修改 3 / 插入 1 / 跨页移动 1）
    3. ingest v2 + versions.differ.diff_versions，比对 manifest 期望计数
    4. translate（mock 离线后端）前 30 页
    5. export cn / bilingual，校验 PDF 可打开、页数、中文可抽取
    6. 起 HTTP 服务，校验关键 API 返回 200

环境：默认使用隔离的 data 目录（`data/derived/_e2e/`），不污染 data/app.db；
环境变量 `MT_E2E_DATA` 可覆盖。所有子步骤都不依赖网络。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PDF = ROOT / "origin" / "BMS-Training-Manual.pdf"
E2E_DATA = Path(os.environ.get("MT_E2E_DATA", str(ROOT / "data" / "derived" / "_e2e")))
SERVER_PORT = int(os.environ.get("MT_E2E_PORT", "8791"))

# 必须在 import config 之前设置，config 会缓存
os.environ["MT_DATA_DIR"] = str(E2E_DATA)

RESULTS: list[tuple[str, bool, str]] = []
E2E_FAST = [False]        # main() 里按 --fast 设定，供各步骤决定是否放宽绝对值断言


def norm(text: str) -> str:
    from core import fingerprint
    return fingerprint.normalize(text)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return bool(cond)


def step(title: str) -> None:
    print(f"\n{'='*78}\n▶ {title}\n{'='*78}", flush=True)


def http_get(url: str, timeout: float = 30.0) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("content-type", "")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc).encode(), ""


# --------------------------------------------------------------------------------------
# 步骤
# --------------------------------------------------------------------------------------

def step_unit_checks() -> None:
    """纯函数单测（不碰数据库）：形状校验阈值 + 译文清理分类。"""
    import pipeline
    from core.fingerprint import normalize

    step("0/6 单元检查 —— 形状校验 / 译文清理分类")
    shape_cases = [
        ("'G'→'G' 通过", "G", "G", True),
        ("'to'→'至' 通过", "to", "至", True),
        ("'a)'→'a)' 通过", "a)", "a)", True),
        ("'SEQ'→'序号' 通过", "SEQ", "序号", True),
        ("'AVIONICS'→整行长译文 拒绝", "AVIONICS",
         "飞控系统（FLCS） FAULT 发动机 FAULT 主警戒 液压 滑油 起落架 燃油 氧气 除冰", False),
        ("'PROCEDURE'→45 字整行译文 拒绝", "PROCEDURE",
         "程序 场景 主 防撞-位置-机翼/尾翼 保险丝-形式(%) AR (%) 隐蔽 收 集 置", False),
        ("长原文→正常长译文 通过", "HYD/OIL PRESS light indicates low pressure",
         "液压/滑油压力灯表示压力过低", True),
        ("空译文 拒绝", "PROCEDURE", "", False),
    ]
    for name, en, zh, want in shape_cases:
        got = pipeline._shape_ok(en, zh)
        check(f"形状校验：{name}", got == want, f"got={got} want={want}")

    cls_cases = [
        ("BMS 4.38.1", "BMS 4.38.1", "machine", "ok"),
        ("F-16", "F-16", "carried", "ok"),
        ("TR_BMS_01_GroundOPS", "TR_BMS_01_GroundOPS", "machine", "ok"),
        ("----", "----", "carried", "ok"),
        ("1000 psi", "1000 psi", "machine", "ok"),
        ("训练手册", "TRAINING MANUAL", "machine", "ok"),
        ("〔TRAINING〕 〔MANUAL〕", "TRAINING MANUAL", "machine", "mock"),
        ("", "something", "failed", "empty"),
        ("garbage transcription here", "PROCEDURE", "machine", "meaningless"),
    ]
    for text, src, status, want in cls_cases:
        got = pipeline.classify_translation(text, src, status)
        check(f"清理分类：{src[:22]!r}→{text[:18]!r} = {want}", got == want, f"got={got}")

    check("normalize 幂等", normalize(" a  b ") == normalize(normalize(" a  b ")))
    check("normalize_pages：(1,31)→31 页", len(pipeline.normalize_pages((1, 31), 401)) == 31)
    check("normalize_pages：21→1 页", pipeline.normalize_pages(21, 401) == [21])
    check("normalize_pages：[1,31]→2 页（列表不作区间）",
          pipeline.normalize_pages([1, 31], 401) == [1, 31])
    check("strip_toc_leader 去点号",
          pipeline.strip_toc_leader("I. EXTERNAL LIGHTNING SETTINGS ......")
          == "I. EXTERNAL LIGHTNING SETTINGS")


def step_ingest_v1(pages_max: int | None) -> dict:
    import pipeline
    from core import db, store

    fast = pages_max is not None
    step("1/6 ingest v1 —— 真实 PDF 抽取入库" + ("（--fast：仅前 %d 页）" % pages_max if fast else ""))
    if not PDF.exists():
        check("源 PDF 存在", False, str(PDF))
        return {}
    check("源 PDF 存在", True, f"{PDF.stat().st_size/1024/1024:.1f} MB")

    conn = db.connect()
    pages = (1, pages_max) if pages_max else None
    t0 = time.time()
    info = pipeline.ingest_pdf(conn, PDF, slug="bms-training-manual",
                               title="BMS Training Manual", note="e2e", pages=pages)
    dt = time.time() - t0
    print(f"    ingest: {info['pages']} 页 / {info['segments']} 段 / {dt:.1f}s", flush=True)

    check("页数 = 401", info["pages"] == 401, str(info["pages"]))
    check("段数 > 3000" if not fast else "段数 > 300（--fast）",
          info["segments"] > (3000 if not fast else 300), str(info["segments"]))
    check("release_label 解析为 4.38.1", info["release_label"] == "4.38.1", info["release_label"])
    check("入库耗时 < 480s", dt < 480, f"{dt:.1f}s")
    check("无空文本段", all(s["text"].strip() for s in store.segments_of_version(conn, info["version_id"])))
    segs = store.segments_of_version(conn, info["version_id"], page=21)
    check("第 21 页段数 10–30", 10 <= len(segs) <= 30, str(len(segs)))
    check("页眉被标为 header", any(s["role"] == "header" for s in segs))
    check("页码被标为 pagenum", any(s["role"] == "pagenum" for s in segs))
    fp_ok = len({s["fingerprint"] for s in segs}) > 0 and all(len(s["fingerprint"]) == 32 for s in segs)
    check("每段都有 32 位十六进制 fingerprint", fp_ok)

    # —— 分段形状断言（Lead 指定：不得把正文切碎，也不得把真表格拼成一行）
    all_segs = store.segments_of_version(conn, info["version_id"])
    body = [s for s in all_segs if s["role"] == "body"]
    cells = [s for s in all_segs if s["kind"] == "table_cell"]
    if fast:
        print("    [SKIP] --fast 模式跳过「body 4000–5000 / table_cell 数量」绝对值断言"
              f"（当前 body={len(body)} table_cell={len(cells)}）", flush=True)
    else:
        check("body 段数在 4000–5000 区间（防过切）", 4000 <= len(body) <= 5000, str(len(body)))
        check("table_cell 数量有意义（>150 且 <1600）", 150 < len(cells) < 1600, str(len(cells)))
    check("序数碎片（th/st/nd/rd/s 独立段）为 0",
          not [s for s in body if len(s["text"]) <= 3
               and s["text"].lower() in ("th", "st", "nd", "rd", "s")],
          str([s["text"] for s in body if len(s["text"]) <= 3
               and s["text"].lower() in ("th", "st", "nd", "rd", "s")][:6]))

    p6 = [s for s in store.segments_of_version(conn, info["version_id"], page=6)
          if "EXTERNAL LIGHTNING" in s["text"]]
    check("p6 'I. EXTERNAL LIGHTNING SETTINGS' 是单个 heading 段",
          len(p6) == 1 and p6[0]["kind"] == "heading",
          f"{len(p6)} 段: " + str([(s['kind'], s['text'][:28]) for s in p6]))

    p7 = [s for s in store.segments_of_version(conn, info["version_id"], page=7)
          if "pilot is selected" in s["text"]]
    check("p7 'Confirm that your pilot is selected.' 是单个段（未被拆词）",
          len(p7) == 1 and "Confirm that your" in p7[0]["text"],
          f"{len(p7)} 段: " + str([(s['kind'], s['text'][-40:]) for s in p7]))

    p25 = [s for s in store.segments_of_version(conn, info["version_id"], page=25)
           if s["kind"] == "table_cell"]
    p25_txt = [s["text"].strip() for s in p25]
    check("p25 表格列各自独立（PROCEDURE/SCENARIO 等）",
          "PROCEDURE" in p25_txt and "SCENARIO" in p25_txt and len(p25) >= 12,
          f"{len(p25)} 个单元格: {p25_txt[:6]}")
    check("p25 单元格 bbox 互不重叠",
          all(not (a["bbox"][2] > b["bbox"][0] + 0.5 and a["bbox"][0] < b["bbox"][2] - 0.5
                   and a["bbox"][3] > b["bbox"][1] + 0.5 and a["bbox"][1] < b["bbox"][3] - 0.5)
              for i, a in enumerate(p25) for b in p25[i + 1:]
              if abs(a["bbox"][1] - b["bbox"][1]) < 3))
    conn.close()
    return info


def step_simulate_v2(pages_max: int | None) -> tuple[dict, dict]:
    import pipeline
    from core import db, store

    step("2/6 simulate-update —— 生成可验证的新版 PDF")
    out_pdf = E2E_DATA / "BMS-Training-Manual_v2_demo.pdf"
    src_sha0 = pipeline.sha256_file(PDF)
    t0 = time.time()
    manifest = pipeline.simulate_update_pdf(PDF, out_pdf, page_from=4, page_to=40)
    print(f"    生成 v2: {out_pdf.stat().st_size/1024/1024:.1f} MB / {time.time()-t0:.1f}s", flush=True)

    check("源 PDF 未被修改", pipeline.sha256_file(PDF) == src_sha0)
    check("manifest 已写出", Path(manifest["manifest_path"]).exists())

    import pymupdf
    with pymupdf.open(PDF) as a, pymupdf.open(out_pdf) as b:
        check("v2 页数与 v1 一致", a.page_count == b.page_count == 401,
              f"{a.page_count} vs {b.page_count}")
        edited = set(manifest["pages_edited"])
        changed_untouched = [p for p in range(1, 41) if p not in edited
                             and a[p - 1].get_text() != b[p - 1].get_text()]
        check("未编辑页文本完全一致（无副作用）", not changed_untouched, str(changed_untouched[:6]))
        check("第 60/200/400 页原样保留",
              all(a[p].get_text() == b[p].get_text() for p in (60, 200, 400)))

    exp = manifest["expected_counts"]
    check("期望计数 = 2删/3改/1增/1移",
          exp == {"modified": 3, "added": 1, "removed": 2, "moved": 1}, str(exp))

    conn = db.connect()
    doc = store.get_document(conn, 1) or {}
    info_v1 = store.list_versions(conn, 1)[0]
    t0 = time.time()
    info_v2 = pipeline.ingest_pdf(conn, out_pdf, slug=doc.get("slug"),
                                  title=doc.get("title"), note="e2e v2")
    print(f"    ingest v2: {info_v2['segments']} 段 / {time.time()-t0:.1f}s", flush=True)
    conn.close()
    return manifest, {"v1": info_v1, "v2": info_v2, "fast": bool(E2E_FAST[0])}


def step_diff(manifest: dict, versions: dict) -> None:
    from core import db
    from versions import differ

    step("3/6 diff —— 比对 manifest 期望与实际识别")
    conn = db.connect()
    t0 = time.time()
    vd = differ.diff_versions(conn, 1, int(versions["v1"]["version_id"]),
                              int(versions["v2"]["version_id"]))
    dt = time.time() - t0
    conn.commit()
    conn.close()
    print(f"    counts = {vd.counts}   ({dt:.2f}s)", flush=True)
    if versions["fast"]:
        print("    [SKIP] --fast 模式跳过精确计数断言（v1 只抽了部分页，added 必然巨大）", flush=True)
        check("diff 能跑通且无 split/merged",
              vd.counts.get("split", 0) == 0 and vd.counts.get("merged", 0) == 0,
              f"split={vd.counts.get('split', 0)} merged={vd.counts.get('merged', 0)}")
        return
    exp = manifest["expected_counts"]
    for k in ("modified", "added", "removed", "moved"):
        check(f"diff {k} = {exp[k]}", int(vd.counts.get(k, 0)) == exp[k],
              f"实际 {vd.counts.get(k, 0)}")
    check("未识别出 split/merged（不应发生）",
          vd.counts.get("split", 0) == 0 and vd.counts.get("merged", 0) == 0,
          f"split={vd.counts.get('split', 0)} merged={vd.counts.get('merged', 0)}")
    check("diff 耗时 < 3s", dt < 3.0, f"{dt:.2f}s")
    moved = [c for c in vd.changes if c.kind == "moved"]
    check("moved 段落 ratio = 1.0", bool(moved) and moved[0].ratio > 0.999,
          f"{moved[0].ratio if moved else 'n/a'}")
    mods = [c for c in vd.changes if c.kind == "modified"]
    check("modified 最少 1 条 ratio > 0.95（改动很小）",
          any(c.ratio > 0.95 for c in mods), str([round(c.ratio, 3) for c in mods]))
    # 幂等：再跑一次计数一致
    conn = db.connect()
    vd2 = differ.diff_versions(conn, 1, int(versions["v1"]["version_id"]),
                               int(versions["v2"]["version_id"]))
    conn.close()
    check("diff 幂等（二次调用计数一致）", vd2.counts == vd.counts)


def step_translation_safety() -> None:
    """译文是用户唯一无法重建的资产：重建分段必须保住它（Lead 的红线）。"""
    import pipeline
    from core import db, store

    step("4.5/6 translation-safety —— 重建分段不得丢译文")
    conn = db.connect()
    vid = int((store.current_version(conn, 1) or {})["version_id"])
    before = len(store.translations_of_version(conn, vid) or {})
    check("已有译文（mock 翻译产物）", before > 50, f"{before} 条")

    # 1) 未授权的 reingest 必须被拒绝（用**带译文的 v2 文件**触发；v1 本来就没有译文）
    v2_pdf = E2E_DATA / "BMS-Training-Manual_v2_demo.pdf"
    refused = False
    try:
        pipeline.ingest_pdf(conn, v2_pdf, slug="bms-training-manual", reingest=True)
    except RuntimeError as exc:
        refused = "拒绝重建" in str(exc)
    check("直接 reingest 被安全护栏拒绝（不静默删译文）", refused)

    # 2) rebuild_version 必须备份+回填；丢弃的必须**全部有据可查**（形状不匹配），不得静默丢失
    rb = pipeline.rebuild_version(conn, vid)
    after = len(store.translations_of_version(conn, vid) or {})
    res = rb.get("restore") or {}
    skipped = int(res.get("skipped_shape", 0))
    lost = int(res.get("lost", 0))
    check("rebuild_version 后译文条数 = 重建前 − 形状不匹配数（无静默丢失）",
          after == before - skipped and lost == 0,
          f"{before} → {after}（形状跳过 {skipped}，未匹配 {lost}）")
    check("rebuild 前已写 JSON 备份", bool(rb.get("backup_path")), str(rb.get("backup_path")))
    check("回填无「形状不匹配」错配进入库",
          True, f"exact={res.get('exact')} near={res.get('near')} "
                f"contains={res.get('contains')} skipped_shape={skipped}")

    # 3) 形状一致性红线（Lead 的 SQL）
    bad = conn.execute(
        "SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
        "WHERE s.kind='table_cell' AND length(t.text) > 3*length(s.text)+40").fetchone()[0]
    check("table_cell 中无「长译文塞进小格子」错配（Lead SQL = 0）", bad == 0, f"{bad} 条")

    # 4) 形状校验函数本身（用 Lead 报的真实错配样例）
    lead_zh = "程序 场景 主 防撞-位置-机翼/尾翼 保险丝-形式(%) AR (%) 隐蔽 收 集 置"
    check(f"形状校验：整行译文({len(lead_zh)}字) 不得挂到 'PROCEDURE'(9字)",
          not pipeline._shape_ok("PROCEDURE", lead_zh),
          f"len(zh)={len(lead_zh)} > max(3*9,40)=40")
    check("形状校验：正常译文允许",
          pipeline._shape_ok("HYD/OIL PRESS light indicates low pressure", "液压/滑油压力灯表示压力过低"))
    conn.close()


def step_translate() -> None:
    import config
    from core import db, store
    from translate import engine

    step("4/6 translate —— mock 离线后端翻前 30 页")
    conn = db.connect()
    vid = int((store.current_version(conn, 1) or {})["version_id"])
    t0 = time.time()
    res = engine.translate_segments(conn, 1, vid, page_from=1, page_to=30,
                                    provider="mock", concurrency=4)
    dt = time.time() - t0
    print(f"    {res}", flush=True)
    check("翻译失败数 = 0", res.get("failed", 1) == 0, str(res.get("failed")))
    check("有产出译文", res.get("translated", 0) > 50, str(res.get("translated")))
    check("耗时 < 120s", dt < 120, f"{dt:.1f}s")
    tr = store.translations_of_version(conn, vid)
    zh = [r for r in tr.values() if any("\u4e00" <= ch <= "\u9fff" for ch in (r.get("text") or ""))]
    check("库中存在含中文的译文", len(zh) > 50, f"{len(zh)} 段含中文")
    non_body_translated = [
        s["id"] for s in store.segments_of_version(conn, vid, page=1)
        if s["role"] != "body" and (tr.get(int(s["id"])) or {}).get("text")
    ]
    check("页眉/页码未被翻译（role!=body 跳过）", not non_body_translated,
          f"{len(non_body_translated)} 个非正文段有译文")
    conn.close()


def step_export() -> None:
    import pymupdf
    import pipeline
    from core import db, store

    step("5/6 export —— 中文 PDF / 双语 PDF")
    conn = db.connect()
    doc_id = 1
    vid = int((store.current_version(conn, doc_id) or {})["version_id"])
    cn = E2E_DATA / "e2e_cn_p10_12.pdf"
    bi = E2E_DATA / "e2e_bi_p21_22.pdf"
    try:
        res_cn = pipeline.export_version_pdf(conn, doc_id, vid, kind="cn",
                                            out_path=str(cn), pages=[10, 11, 12])
    except Exception as exc:  # noqa: BLE001
        check("中文 PDF 导出成功", False, f"{exc.__class__.__name__}: {exc}")
        res_cn = None
    if res_cn:
        with pymupdf.open(cn) as doc:
            check("中文 PDF 页数 = 3", doc.page_count == 3, str(doc.page_count))
            text = "".join(doc[i].get_text() for i in range(doc.page_count))
            check("中文 PDF 含中文字符", any("\u4e00" <= ch <= "\u9fff" for ch in text))
            check("中文 PDF 页宽与原页一致", abs(doc[0].rect.width - 595.32) < 1,
                  f"{doc[0].rect.width:.2f}")
    try:
        res_bi = pipeline.export_version_pdf(conn, doc_id, vid, kind="bilingual",
                                            out_path=str(bi), pages=[21, 22])
    except Exception as exc:  # noqa: BLE001
        check("双语 PDF 导出成功", False, f"{exc.__class__.__name__}: {exc}")
        res_bi = None
    if res_bi:
        with pymupdf.open(bi) as doc:
            check("双语 PDF 页数 = 2", doc.page_count == 2, str(doc.page_count))
            check("双语 PDF 页宽 = 2 × 原页宽", abs(doc[0].rect.width - 2 * 595.32) < 2,
                  f"{doc[0].rect.width:.2f}")
    conn.close()


def step_server() -> None:
    step("6/6 serve —— 启动 HTTP 服务并校验关键 API")
    env = dict(os.environ)
    env["MT_DATA_DIR"] = str(E2E_DATA)
    env["MT_WEB_PORT"] = str(SERVER_PORT)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import uvicorn; from web.server import app; "
         f"uvicorn.run(app, host='127.0.0.1', port={SERVER_PORT}, log_level='warning')"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{SERVER_PORT}"
    try:
        up = False
        for _ in range(60):
            code, body, _ct = http_get(f"{base}/api/health", timeout=2)
            if code == 200:
                up = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        check("服务启动（/api/health 200）", up, f"pid={proc.pid}")
        if not up:
            return
        endpoints = [
            ("/api/health", "health"),
            ("/api/documents", "documents"),
            ("/api/documents/1/versions", "versions"),
            ("/api/documents/1/versions/1/segments?page=21", "segments"),
            ("/api/documents/1/versions/1/outline", "outline"),
            ("/api/documents/1/diff?from=1&to=2", "diff"),
            ("/api/documents/1/versions/1/pages/21/markers", "markers"),
            ("/api/documents/1/versions/1/pages/21/image?kind=source&dpi=60", "page image"),
            ("/api/glossary", "glossary"),
            ("/api/jobs", "jobs"),
        ]
        for path, label in endpoints:
            code, body, ctype = http_get(base + path, timeout=60)
            extra = ""
            if code == 200 and ctype.startswith("application/json"):
                try:
                    data = json.loads(body.decode("utf-8"))
                    if isinstance(data, list):
                        extra = f"{len(data)} 条"
                except Exception:
                    pass
            elif code == 200:
                extra = f"{len(body)} bytes {ctype.split(';')[0]}"
            check(f"GET {path} → 200", code == 200, f"HTTP {code} {extra}".strip())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="manual_trans_trace 端到端测试（真实 401 页 PDF）")
    ap.add_argument("--fast", action="store_true", help="只抽前 60 页（开发迭代用）")
    ap.add_argument("--keep", action="store_true", help="保留临时 data 目录")
    ap.add_argument("--skip-server", action="store_true", help="跳过 HTTP 服务检查")
    args = ap.parse_args(argv)

    pages_max = 60 if args.fast else None
    E2E_FAST[0] = bool(args.fast)
    # 每次都在干净的 data 目录上跑（否则上次的库会让 sha 幂等命中，测不到真实抽取）
    shutil.rmtree(E2E_DATA, ignore_errors=True)
    E2E_DATA.mkdir(parents=True, exist_ok=True)

    print(f"manual_trans_trace E2E\n  工作目录: {ROOT}\n  隔离数据: {E2E_DATA}"
          f"\n  模式: {'--fast (前 60 页)' if args.fast else '全量 401 页'}", flush=True)
    t_all = time.time()
    try:
        step_unit_checks()
        step_ingest_v1(pages_max)
        manifest, versions = step_simulate_v2(pages_max)
        if manifest and versions:
            step_diff(manifest, versions)
        step_translate()
        step_translation_safety()
        step_export()
        if not args.skip_server:
            step_server()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        RESULTS.append(("测试运行未抛异常", False, "见上方 traceback"))

    passed = sum(1 for _n, ok_, _d in RESULTS if ok_)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'='*78}")
    print(f"结果: {passed}/{len(RESULTS)} 项通过，耗时 {time.time()-t_all:.1f}s")
    for name, _ok, detail in failed:
        print(f"  ❌ {name}  {detail}")
    if not RESULTS:
        print("没有执行任何检查")
        return 1
    if failed:
        print("结论: ❌ 失败")
        return 1
    print("结论: ✅ 全部通过")
    if not args.keep:
        shutil.rmtree(E2E_DATA, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
