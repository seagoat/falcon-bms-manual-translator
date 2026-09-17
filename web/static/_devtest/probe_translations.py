"""交叉核对：/api segments 与 translated-layout 与 DB 里的译文是否一致。

用法：python web/static/_devtest/probe_translations.py [base_url] [doc] [vid]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from core import db as core_db  # noqa: E402

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901").rstrip("/")
DOC = int(sys.argv[2]) if len(sys.argv) > 2 else 1
VID = int(sys.argv[3]) if len(sys.argv) > 3 else 2


def api(path):
    with urllib.request.urlopen(B + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    segs = api(f"/api/documents/{DOC}/versions/{VID}/segments?page=21")
    print(f"API page21 segments = {len(segs)}")
    for s in segs[:6]:
        t = s.get("translation")
        print(f"   {s['seg_id']:>7} {s['kind']:<11} {(t or {}).get('status')!s:<9} "
              f"{repr((t or {}).get('text'))[:56]}")
    ly = api(f"/api/documents/{DOC}/versions/{VID}/pages/21/translated-layout")
    print(f"layout boxes = {len(ly['boxes'])}  seg_ids={[b['seg_id'] for b in ly['boxes']][:8]}")

    with closing(core_db.connect()) as c:
        rows = c.execute(
            "SELECT s.page, COUNT(t.id) n FROM segments s JOIN translations t ON t.segment_id = s.id "
            "WHERE s.version_id = ? GROUP BY s.page ORDER BY s.page LIMIT 40", (VID,)).fetchall()
        print("DB 译文按页：", [(r[0], r[1]) for r in rows])
        tot = c.execute("SELECT COUNT(*) FROM translations t JOIN segments s ON s.id = t.segment_id "
                        "WHERE s.version_id = ?", (VID,)).fetchone()[0]
        nseg = c.execute("SELECT COUNT(*) FROM segments WHERE version_id = ?", (VID,)).fetchone()[0]
        print(f"DB 总译文 {tot} / 总段 {nseg}")
        # 有译文的段其 page 分布
        pages = c.execute(
            "SELECT DISTINCT s.page FROM segments s JOIN translations t ON t.segment_id = s.id "
            "WHERE s.version_id = ? ORDER BY s.page", (VID,)).fetchall()
        print("有译文的页码：", [r[0] for r in pages])
    return 0


if __name__ == "__main__":
    sys.exit(main())
