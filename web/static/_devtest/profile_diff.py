"""在进程内对比 differ 与 HTTP 端点的耗时，定位 /api/diff 的真实瓶颈。

用法：python web/static/_devtest/profile_diff.py [from_vid] [to_vid]
"""
from __future__ import annotations

import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import db as core_db  # noqa: E402
from core import store as core_store  # noqa: E402
from versions import differ  # noqa: E402

FROM = int(sys.argv[1]) if len(sys.argv) > 1 else 1
TO = int(sys.argv[2]) if len(sys.argv) > 2 else 2


def main() -> int:
    with closing(core_db.connect()) as c:
        docs = core_store.list_documents(c)
        print("documents:", [(d["doc_id"], d.get("slug"), d.get("title")) for d in docs])
        for d in docs:
            vs = core_store.list_versions(c, d["doc_id"])
            for v in vs:
                n = core_store.count_segments(c, v["version_id"])
                print(f"  doc {d['doc_id']} version {v['version_id']} {v.get('label')} "
                      f"pages={v.get('page_count')} segments={n}")
        t0 = time.time()
        vd = differ.diff_versions(c, docs[0]["doc_id"], FROM, TO)
        dt = time.time() - t0
        print(f"\ndiff_versions(doc={docs[0]['doc_id']}, {FROM}->{TO}): {dt*1000:.0f} ms, "
              f"changes={len(vd.changes)}")
        print("counts:", vd.counts)
        ops = sum(len(ch.char_diffs or []) for ch in vd.changes)
        print("total char_diff ops:", ops)
        non_unch = [ch for ch in vd.changes if str(ch.kind) != "unchanged"]
        print("non-unchanged:", len(non_unch),
              "kinds:", {k: sum(1 for ch in non_unch if str(ch.kind) == k)
                         for k in {str(ch.kind) for ch in non_unch}})
        t0 = time.time()
        vd2 = differ.diff_versions(c, docs[0]["doc_id"], FROM, TO)
        print(f"diff_versions 二次(缓存): {(time.time()-t0)*1000:.0f} ms, changes={len(vd2.changes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
