"""Lead integration probe: config + DB state + pipeline/CLI surface."""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core import db as cdb

c = config.load(reload=True)
print("config:", {k: c[k] for k in ("model", "thinking_mode", "concurrency", "provider",
                                    "batch_segments", "render_dpi", "web_port")})
print("db path:", config.db_path(), "exists:", os.path.exists(config.db_path()))

con = cdb.connect()
tables = sorted(r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
print("tables:", tables)
for t in ("documents", "document_versions", "segments", "translations",
          "translation_cache", "segment_links", "version_diffs", "render_snapshots", "jobs"):
    if t in tables:
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:22s} rows={n}")
    else:
        print(f"  {t:22s} MISSING")
con.close()

# module surface check
import pipeline  # noqa: E402
print("pipeline:", [n for n in ("extract_segments", "ingest_pdf", "build_glossary_seed")
                    if hasattr(pipeline, n)])
import translate.engine as te  # noqa: E402
print("engine:", [n for n in ("translate_segments", "estimate_cost", "dry_run")
                  if hasattr(te, n)])
try:
    import versions.differ as vd
    print("differ:", [n for n in ("diff_versions", "diff_summary_text", "changes_by_segment",
                                  "removed_by_segment") if hasattr(vd, n)])
except Exception as e:
    print("differ: import failed ->", type(e).__name__, e)
try:
    import render.pagebuild as rp
    print("pagebuild:", [n for n in ("rebuild_page", "compute_layout", "draw_translated",
                                     "build_translated_page", "render_page_png")
                         if hasattr(rp, n)])
except Exception as e:
    print("pagebuild: import failed ->", type(e).__name__, e)
try:
    import render.pdfout as po
    print("pdfout:", [n for n in ("export_cn_pdf", "export_bilingual_pdf") if hasattr(po, n)])
except Exception as e:
    print("pdfout: import failed ->", type(e).__name__, e)
try:
    import web.server as ws
    print("web.server: app =", hasattr(ws, "app"), "run =", hasattr(ws, "run"))
except Exception as e:
    print("web.server: import failed ->", type(e).__name__, e)
