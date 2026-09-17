"""Lead: retry every untranslated body segment for a given doc/version.

The engine marks a batch FAILED when the model omits any requested id
(`model response missing ids: [...]`), leaving those segments empty.
A single retry pass almost always clears them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

DOC = sys.argv[1] if len(sys.argv) > 1 else "to-1"
VERSION = sys.argv[2] if len(sys.argv) > 2 else None

from core import db as cdb, store  # noqa: E402
from translate import engine  # noqa: E402

con = cdb.connect()
docs = [d for d in store.list_documents(con) if d["slug"] == DOC]
if not docs:
    print(f"找不到文档 {DOC}")
    raise SystemExit(1)
doc_id = int(docs[0]["doc_id"])
v = store.current_version(con, doc_id)
vid = int(v["version_id"])
print(f"文档 {DOC} (doc_id={doc_id}) 版本 {v['label']} (version_id={vid})")

ids = [r["id"] for r in con.execute(
    "SELECT s.id FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
    "WHERE s.version_id=? AND s.role='body' "
    "AND (t.id IS NULL OR trim(t.text)='' OR t.status='failed') "
    "ORDER BY s.order_index", (vid,)).fetchall()]
print(f"待补译段数: {len(ids)}")

if ids:
    res = engine.translate_segments(con, doc_id, vid, segment_ids=ids,
                                    provider="deepseek", concurrency=6)
    con.commit()
    print("补译结果:", {k: res[k] for k in
                     ("translated", "carried", "failed", "api_calls", "batches")})
    print("tokens:", res.get("tokens"))

left = con.execute(
    "SELECT COUNT(*) FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
    "WHERE s.version_id=? AND s.role='body' "
    "AND (t.id IS NULL OR trim(t.text)='' OR t.status='failed')", (vid,)).fetchone()[0]
print(f"剩余未译: {left}")

if left:
    print("\n=== 仍未译的段（前 20）===")
    for r in con.execute(
            "SELECT s.id, s.page, s.kind, s.text FROM segments s "
            "LEFT JOIN translations t ON t.segment_id=s.id "
            "WHERE s.version_id=? AND s.role='body' "
            "AND (t.id IS NULL OR trim(t.text)='' OR t.status='failed') "
            "ORDER BY s.order_index LIMIT 20", (vid,)).fetchall():
        print(f"  seg={r['id']} p{r['page']} {r['kind']:10s} {r['text'][:70]!r}")
con.close()
