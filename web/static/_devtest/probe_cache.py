"""检查 render_snapshots 与页面图缓存键是否一致。

用法：python web/static/_devtest/probe_cache.py [version_id]
"""
from __future__ import annotations

import hashlib
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

import web.server as srv  # noqa: E402
from core import db as core_db  # noqa: E402

VID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
KIND = sys.argv[2] if len(sys.argv) > 2 else "cn"
DPI = int(sys.argv[3]) if len(sys.argv) > 3 else 110

with closing(core_db.connect()) as c:
    rows = c.execute("SELECT version_id, kind, params_hash, path, stale, bytes FROM render_snapshots "
                     "WHERE version_id = ? AND kind = ?", (VID, KIND)).fetchall()
    print(f"render_snapshots(vid={VID}, kind={KIND}):")
    for r in rows:
        p = Path(r[3] or "")
        print(f"  hash={r[2]} stale={r[4]} bytes={r[5]} exists={p.exists()} mtime="
              f"{p.stat().st_mtime if p.exists() else '-'} path={r[3]}")
    rev = srv.render_rev(c, VID)
    want = hashlib.sha1(f"{KIND}:{DPI}:{rev}|{srv.RENDER_VER}".encode()).hexdigest()[:16]
    print(f"RENDER_VER={srv.RENDER_VER} render_rev={rev} → 期望 params_hash={want}")
    got = srv.mod("core.store").get_render_snapshot(c, VID, KIND, want)
    print("get_render_snapshot(期望 hash) =", got)
