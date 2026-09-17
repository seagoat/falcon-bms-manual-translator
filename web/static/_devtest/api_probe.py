"""真实数据 API 计时探针：python web/static/_devtest/api_probe.py <base_url> [doc_id] [vid] [page]

用于验收 /api/diff 的耗时与响应体大小（修复前真实数据 33.9s / 2.2MB）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901").rstrip("/")
DOC = sys.argv[2] if len(sys.argv) > 2 else "1"
VID = sys.argv[3] if len(sys.argv) > 3 else "2"
PAGE = sys.argv[4] if len(sys.argv) > 4 else "21"


def get(path, raw=False, timeout=300):
    t0 = time.time()
    try:
        with urllib.request.urlopen(B + path, timeout=timeout) as r:
            b = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, 0, None
    return code, time.time() - t0, len(b), (b if raw else json.loads(b.decode("utf-8")))


def show(path, raw=False):
    code, dt, n, d = get(path, raw=raw)
    print(f"  {path[:78]:78s} HTTP {code}  {dt*1000:8.0f} ms  {n:9d} B")
    return dt, n, d


def main() -> int:
    print(f"== probe {B} (doc={DOC} vid={VID} page={PAGE}) ==")
    show("/api/health")
    show("/api/documents")
    show(f"/api/documents/{DOC}/versions")
    dt, n, d = show(f"/api/documents/{DOC}/diff?from=1&to={VID}")
    if d:
        print("  counts:", json.dumps(d.get("counts"), ensure_ascii=False))
        print("  changes:", len(d.get("changes", [])), " unchanged_omitted:", d.get("unchanged_omitted"),
              " total_changes:", d.get("total_changes"))
        for c in d.get("changes", [])[:8]:
            print(f"    #{c['change_id']} {c['kind']:9s} r={c['ratio']:.2f} "
                  f"p{c.get('old_page')}->p{c.get('new_page')} old={c['old_text'][:46]!r}")
        print(f"  >>> /api/diff {dt*1000:.0f} ms / {n/1024:.1f} KB  "
              f"({'PASS' if dt < 0.3 else 'SLOW'})")
    show(f"/api/documents/{DOC}/versions/{VID}/segments?page={PAGE}")
    show(f"/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/markers")
    show(f"/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/translated-layout")
    show(f"/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/image?kind=cn&dpi=110", raw=True)
    show(f"/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/image?kind=source&dpi=110", raw=True)
    show("/api/jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
