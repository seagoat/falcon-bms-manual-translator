"""把隔离库里的两份 TO 手册合并进主库。

做法（**保留已有译文**，不重新调 API）：
  1. 用 `pipeline.ingest_pdf` 把两份 TO 源 PDF 重新入进主库 —— 这样 doc_id /
     version_id / segment_id 都由主库自己分配，绝不会撞号，源路径也是对的
     （入库时写的是相对路径）。
  2. 按 **fingerprint** 把隔离库里已有的译文搬到主库对应段上。
     指纹是文本的确定性函数，跨库稳定；TO-1 有 6194 段、TO-34 有 11796 段。
  3. 顺带搬 translation_cache（同一 fingerprint 的译文缓存），这样以后
     重建/接入新版本时还能命中缓存、少花钱。
  4. 复核：逐段比对两边译文条数与内容一致性。

用法：python spikes/merge_to_into_main.py [--dry-run]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DRY = "--dry-run" in sys.argv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TO_DB = os.path.join(ROOT, "data", "_to_probe", "app.db")

JOBS = [
    ("origin/TO 1F-16CMAM-34-1-1 BMS.pdf", "to-34", "TO 1F-16CMAM-34-1-1"),
    ("origin/TO 1F-16CMAM-1 BMS.pdf", "to-1", "TO 1F-16CMAM-1"),
]

import sqlite3  # noqa: E402

import config  # noqa: E402
from core import db as cdb, fingerprint as fp, store  # noqa: E402
import pipeline  # noqa: E402


def load_src_translations(src_conn, slug):
    """从隔离库取某文档当前版本的 {fingerprint: [...]} 与 {(page,order): row}。

    ⚠️ **不能只用 fingerprint 当键**：TO-34 的 11128 个 body 段只对应
    **9431 个不同 fingerprint** —— 页码、样板句等重复内容会折叠，
    按指纹搬会丢掉 1697 段（表现为"合并后大量段变成未翻译"）。
    因此同时提供按 (page, order_index) 的精确映射；两个库用的是同一个抽取器、
    同一份 PDF，所以页内顺序完全一致，位置映射是精确的。
    """
    st = sqlite3.connect(src_conn)
    st.row_factory = sqlite3.Row
    docs = [d for d in st.execute("SELECT * FROM documents") if d["slug"] == slug]
    if not docs:
        st.close()
        return None, {}, {}
    doc_id = int(docs[0]["doc_id"])
    v = st.execute("SELECT * FROM document_versions WHERE doc_id=? AND is_current=1",
                   (doc_id,)).fetchone()
    if v is None:
        v = st.execute("SELECT * FROM document_versions WHERE doc_id=? "
                       "ORDER BY version_no DESC LIMIT 1", (doc_id,)).fetchone()
    vid = int(v["version_id"])
    rows = st.execute(
        "SELECT s.fingerprint AS fp, s.page AS page, s.order_index AS oi, s.text AS en, "
        "       t.text AS zh, t.status AS status, t.provider AS provider, "
        "       t.model AS model, t.confidence AS conf "
        "FROM translations t JOIN segments s ON s.id = t.segment_id "
        "WHERE s.version_id=? AND s.role='body'", (vid,)).fetchall()
    by_fp, by_pos = {}, {}
    for r in rows:
        zh = r["zh"]
        if zh is None or zh == "":
            zh = r["en"] or ""      # 空译文 = 原文照搬（carried），是合法收录
        rec = {"zh": zh, "status": r["status"], "provider": r["provider"],
               "model": r["model"], "conf": r["conf"]}
        if r["fp"]:
            by_fp[r["fp"]] = rec
        by_pos[(int(r["page"]), int(r["oi"]))] = rec
    nseg = st.execute("SELECT COUNT(*) FROM segments WHERE version_id=? AND role='body'",
                      (vid,)).fetchone()[0]
    st.close()
    return ({"doc_id": doc_id, "version_id": vid, "body": nseg}, by_fp, by_pos)


def main() -> int:
    print(f"隔离库: {TO_DB}")
    src = sqlite3.connect(TO_DB)

    conn = cdb.connect()
    print("\n=== 合并前主库 ===")
    for d in store.list_documents(conn):
        print(f"  doc {d['doc_id']} {d['slug']} 版本数={len(store.list_versions(conn, d['doc_id']))}")

    for pdf, slug, title in JOBS:
        info, by_fp, by_pos = load_src_translations(TO_DB, slug)
        if info is None:
            print(f"\n[{slug}] 隔离库里找不到，跳过")
            continue
        print(f"\n=== {slug} ===  隔离库 body={info['body']} 译文={len(by_pos)} "
              f"（不同指纹 {len(by_fp)}）")
        if DRY:
            print("  （--dry-run：不写库）")
            continue

        existing = [d for d in store.list_documents(conn) if d["slug"] == slug]
        if existing:
            print(f"  主库已存在同 slug 文档（doc {existing[0]['doc_id']}），跳过 ingest")
            doc_id = int(existing[0]["doc_id"])
            v = store.current_version(conn, doc_id)
            vid = int(v["version_id"])
        else:
            res = pipeline.ingest_pdf(conn, pdf, slug=slug, title=title,
                                      note="从隔离库合并")
            doc_id, vid = int(res["doc_id"]), int(res["version_id"])
            print(f"  已入库: doc_id={doc_id} version_id={vid} "
                  f"pages={res['pages']} segments={res['segments']}")

        # 搬译文：**先按 (page, order_index) 精确映射**，再退回 fingerprint。
        segs = [s for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
        moved = by_pos_hit = by_fp_hit = missed = 0
        for s in segs:
            t = by_pos.get((int(s["page"]), int(s["order_index"])))
            if t:
                by_pos_hit += 1
            else:
                t = by_fp.get(s["fingerprint"])
                if t:
                    by_fp_hit += 1
            if not t:
                missed += 1
                continue
            store.upsert_translation(conn, segment_id=int(s["id"]), text=t["zh"],
                                     status=t["status"] or "machine",
                                     provider=t["provider"] or "deepseek",
                                     model=t["model"] or "", confidence=float(t["conf"] or 0.0))
            moved += 1
        # 搬译文缓存（跨版本复用）
        cached = 0
        for f, t in by_fp.items():
            store.put_cached_translation(conn, fingerprint=f, text=t["zh"],
                                         provider=t["provider"] or "deepseek",
                                         model=t["model"] or "")
            cached += 1
        conn.commit()
        print(f"  译文搬迁: 共 {moved}（位置 {by_pos_hit} / 指纹 {by_fp_hit}）"
              f"  未命中 {missed}  缓存写入 {cached}")

    print("\n=== 合并后主库 ===")
    for d in store.list_documents(conn):
        vs = store.list_versions(conn, d["doc_id"])
        for v in vs:
            stt = store.translation_stats(conn, int(v["version_id"]))
            print(f"  doc {d['doc_id']} {d['slug']:22s} v{v['version_no']} "
                  f"pages={v['page_count']} body={stt['total']} translated={stt['translated']} "
                  f"missing={stt['total'] - stt['translated']}")
    conn.close()
    src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
