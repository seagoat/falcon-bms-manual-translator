"""一次性诊断：translations 表结构 / render_rev / layout API 是否正常。

用法：python web/static/_devtest/probe_schema.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

import web.server as srv  # noqa: E402
from core import db as core_db  # noqa: E402

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901").rstrip("/")

with closing(core_db.connect()) as c:
    for t in ("translations", "segments", "render_snapshots"):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()]
        print(f"{t}: {cols}")
    print("translations 总数:", c.execute("SELECT COUNT(*) FROM translations").fetchone()[0])
    print("segments v2:", c.execute("SELECT COUNT(*) FROM segments WHERE version_id=2").fetchone()[0])
    try:
        print("render_rev(2) =", srv.render_rev(c, 2))
    except Exception as exc:
        print("render_rev 异常:", exc)
    rows = c.execute("SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
                     "WHERE s.version_id=2").fetchone()
    print("v2 挂上的译文:", rows[0])


def api(p):
    try:
        with urllib.request.urlopen(B + p, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return 0, str(exc)


st, ly = api("/api/documents/1/versions/2/pages/21/translated-layout")
print("layout p21:", st, "boxes:", len(ly.get("boxes", [])) if isinstance(ly, dict) else ly)
st, segs = api("/api/documents/1/versions/2/segments?page=21")
if isinstance(segs, list):
    print("segments p21:", len(segs), "有译文:", sum(1 for s in segs if s.get("translation")))
st, d = api("/api/documents/1/diff?from=1&to=2")
print("diff:", st, (json.dumps(d.get("counts"), ensure_ascii=False) if isinstance(d, dict) else d))
