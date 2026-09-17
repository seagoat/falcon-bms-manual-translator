"""Lead: tighten the merge rule to the unambiguous signal and re-measure.

Only join when the NEXT segment starts with a lowercase letter and the previous one
does not end with sentence punctuation. That is the case a translator genuinely
cannot handle (it sees half a sentence). Numbered list items / headings / capitalised
new paragraphs are excluded automatically.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")
TOC = re.compile(r"\.{4,}\s*\d*\s*$")
LIST_MARK = re.compile(
    r"^\s*(?:[-•▪◆]|\d+[.)]|[a-z][.)]|\([a-z0-9]+\)|FIGURE|TABLE|NOTE|WARNING|CAUTION)\b", re.I)


def candidates(version_id, label, *, max_gap_lines=2.5):
    con = cdb.connect()
    segs = [store.get_segment(con, s["id"]) for s in
            store.segments_of_version(con, version_id) if s["role"] == "body"]
    con.close()
    hits = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if not a or not b:
            continue
        ta, tb = (a["text"] or "").strip(), (b["text"] or "").strip()
        if not ta or not tb:
            continue
        if a["page"] != b["page"] or a["role"] != "body" or b["role"] != "body":
            continue
        if a["kind"] in ("heading", "caption", "table_cell") or b["kind"] in ("heading", "caption"):
            continue
        if TOC.search(ta) or TOC.search(tb) or LIST_MARK.match(tb):
            continue
        if ta[-1] in SENT_END:
            continue
        sa, sb = (a.get("style") or {}), (b.get("style") or {})
        if abs(float(sa.get("size") or 0) - float(sb.get("size") or 0)) > 0.3:
            continue
        if not tb[:1].islower():          # ← 关键：只认小写开头
            continue
        gap = b["bbox"][1] - a["bbox"][3]
        size = float(sa.get("size") or 10.0)
        if gap < -1.0 or gap > max_gap_lines * size:
            continue
        if min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0]) <= 0:
            continue
        hits.append((a, b))
    print(f"\n=== {label} ===  小写开头续段候选: {len(hits)} 处")
    for a, b in hits:
        print(f"  p{a['page']:3d} ...{a['text'][-52:]!r}")
        print(f"         + {b['text'][:52]!r}")
    return hits


os.environ["MT_DATA_DIR"] = "data/_to_probe"
import config  # noqa: E402
config.load(reload=True)
h1 = candidates(1, "TO 1F-16CMAM-34-1-1（669 页）")

os.environ["MT_DATA_DIR"] = "data"
config.load(reload=True)
h2 = candidates(2, "BMS 训练手册 v2（401 页）")
print(f"\n汇总：TO-34 {len(h1)} 处；训练手册 {len(h2)} 处")
