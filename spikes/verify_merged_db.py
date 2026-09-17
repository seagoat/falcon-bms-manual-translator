"""Lead: verify the merged main DB is coherent and the Web UI can serve all 3 docs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, paths as _paths, store  # noqa: E402

con = cdb.connect()
print("=== 合并后的主库 ===")
for d in store.list_documents(con):
    for v in store.list_versions(con, d["doc_id"]):
        stt = store.translation_stats(con, int(v["version_id"]))
        sp = _paths.resolve(v.get("source_path") or "")
        ok = os.path.exists(sp)
        print(f"  doc{d['doc_id']} {d['slug']:22s} v{v['version_no']} "
              f"pages={v['page_count']:4d} body={stt['total']:6d} "
              f"translated={stt['translated']:6d} src={'OK' if ok else 'MISSING'}")
        print(f"        by_status={stt['by_status']}")
        print(f"        source_path={v.get('source_path')}")

print("\n=== 库内总览 ===")
tot_seg = con.execute("SELECT COUNT(*) FROM segments WHERE role='body'").fetchone()[0]
tot_tr = con.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
tot_cache = con.execute("SELECT COUNT(*) FROM translation_cache").fetchone()[0]
print(f"  body 段合计={tot_seg}  译文合计={tot_tr}  缓存={tot_cache}")

print("\n=== 抽样：三份手册第 21 页各一条 ===")
for d in store.list_documents(con):
    v = store.current_version(con, d["doc_id"])
    vid = int(v["version_id"])
    tr = store.translations_of_version(con, vid)
    segs = [s for s in store.segments_of_version(con, vid, page=21)
            if s["role"] == "body" and len(s["text"]) > 30]
    if segs:
        s = segs[0]
        print(f"  {d['slug']:22s} EN={s['text'][:52]!r}")
        print(f"  {'':22s} ZH={(tr.get(s['id']) or {}).get('text','')[:52]!r}")
con.close()
