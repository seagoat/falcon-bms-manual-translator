"""把真实库做一份只读快照到私有 data dir，用于稳定复现（不碰 data/app.db）。

用法：python web/static/_devtest/snapshot_db.py <dest_data_dir>
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC = ROOT / "data" / "app.db"
DEST_DIR = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "data/derived/_realtest2")
DEST_DIR.mkdir(parents=True, exist_ok=True)
DEST = DEST_DIR / "app.db"

import sqlite3  # noqa: E402

src = sqlite3.connect(f"file:{SRC.as_posix()}?mode=ro", uri=True)
dst = sqlite3.connect(str(DEST))
try:
    src.backup(dst)                      # 一致性快照（WAL 下也安全）
finally:
    dst.close()
    src.close()

gl = ROOT / "data" / "glossary.json"
if gl.exists():
    shutil.copy2(gl, DEST_DIR / "glossary.json")

chk = sqlite3.connect(str(DEST))
n_doc = chk.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
n_seg = chk.execute("SELECT COUNT(*) FROM segments WHERE version_id=2").fetchone()[0]
n_tr = chk.execute("SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id "
                   "WHERE s.version_id=2").fetchone()[0]
chk.close()
print(f"snapshot → {DEST}  documents={n_doc} v2_segments={n_seg} v2_translations={n_tr}")
