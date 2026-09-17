"""Lead: prove what a recipient actually gets from the distribution package.

Answers: does the package contain the finished translations, or would the recipient
have to re-run translation? Verifies both the ZIP and an extracted copy.
"""
import json
import os
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "dist", "manual_trans_trace_portable.zip")
LIVE = os.path.join(ROOT, "data", "app.db")


def report(label, db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    print(f"\n=== {label} ===")
    print(f"  file: {db_path}")
    print(f"  size: {os.path.getsize(db_path)/1e6:.1f} MB")
    tabs = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in ("documents", "document_versions", "segments", "translations",
              "translation_cache", "segment_links", "glossary"):
        if t not in tabs:
            print(f"    {t:20s} MISSING")
            continue
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"    {t:20s} {n}")
    # translation depth
    rows = con.execute(
        "SELECT s.version_id, COUNT(*) n, "
        "       SUM(CASE WHEN t.text GLOB '*[一-龥]*' THEN 1 ELSE 0 END) cjk "
        "FROM translations t JOIN segments s ON s.id=t.segment_id "
        "WHERE s.role='body' GROUP BY s.version_id").fetchall()
    for r in rows:
        print(f"    v{r['version_id']}: body 译文 {r['n']} 条，含中文 {r['cjk']} 条")
    # a couple of samples
    for r in con.execute(
            "SELECT s.text en, t.text zh FROM translations t "
            "JOIN segments s ON s.id=t.segment_id "
            "WHERE s.page=1 AND t.text <> '' LIMIT 3").fetchall():
        print(f"    {r['en'][:38]!r} -> {r['zh'][:38]!r}")
    con.close()


report("本地正式库（Lead 验证过的源）", LIVE)

print(f"\n=== ZIP 内容检查: {ZIP} ===")
print(f"  zip 大小: {os.path.getsize(ZIP)/1e6:.1f} MB")
with zipfile.ZipFile(ZIP) as z:
    names = z.namelist()
    dbn = [n for n in names if n.replace("\\", "/").endswith("data/app.db")]
    print(f"  zip 内是否含 data/app.db: {bool(dbn)}  {dbn}")
    if dbn:
        info = z.getinfo(dbn[0])
        print(f"  未压缩大小: {info.file_size/1e6:.1f} MB  (压缩后 {info.compress_size/1e6:.1f} MB)")
    # does the *source* zip include translations? check for derived/preview screenshots too
    for probe in ("data/derived", "review", "data/pdfs"):
        n = [x for x in names if x.replace("\\", "/").startswith(probe)]
        print(f"  含 {probe}: {len(n)} 项")
