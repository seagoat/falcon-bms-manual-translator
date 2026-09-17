"""Lead: inspect ingested documents / versions / translations / diffs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
from core import db as cdb, store  # noqa: E402

con = cdb.connect()
for d in store.list_documents(con):
    print(f"doc {d['doc_id']}: slug={d.get('slug')} title={d.get('title')!r}")
    for v in store.list_versions(con, d["doc_id"]):
        st = store.translation_stats(con, v["version_id"])
        print(f"  v{v['version_no']} (id={v['version_id']}) label={v.get('label')!r} "
              f"release={v.get('release_label')!r} pages={v.get('page_count')} "
              f"current={v.get('is_current')} src={os.path.basename(v.get('source_path') or '')}")
        print(f"     stats={v.get('stats')}")
        print(f"     segs={store.count_segments(con, v['version_id'])} translations={st}")
    vs = store.list_versions(con, d["doc_id"])
    if len(vs) >= 2:
        a, b = vs[0]["version_id"], vs[1]["version_id"]
        print(f"  diff summary {a}->{b}: {store.diff_summary(con, d['doc_id'], a, b)}")
print("\nrecent jobs:")
for j in store.list_jobs(con, 10):
    print(f"  #{j['job_id']} {j['kind']} {j['status']} {j.get('progress')}/{j.get('total')} "
          f"{str(j.get('message'))[:60]}")
print("\nfirst 5 segments of v1:")
for s in store.segments_of_version(con, vs[0]["version_id"])[:5]:
    print(f"  p{s['page']} #{s['order_index']} {s['kind']:10s} role={s['role']:7s} "
          f"bbox={[round(x,1) for x in s['bbox']]}")
    print(f"     text={s['text'][:90]!r}")
    print(f"     canon={s['canonical_text'][:90]!r}")
    print(f"     fp={s['fingerprint'][:16]} lines={len(s.get('line_boxes') or [])} "
          f"style={s.get('style')}")
con.close()
