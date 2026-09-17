"""Lead: exclude list markers properly, then list the genuine mid-sentence splits.

Observed false positives: 'b) < 75° bank', 'a. Select TGT-TO-WPT page', 'v. Marine' —
these are lower-case LEADING LIST MARKERS, not sentence continuations.
Genuine cases look like '... is allowed' + 'without the written permission of ...'.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")
TOC = re.compile(r"\.{4,}\s*\d*\s*$")
# 行首列表标记：(a) a) a. i. ii. 1. 1) - • ▪ ◆ (1)
LIST_MARK = re.compile(
    r"^\s*(?:"
    r"[-•▪◆●○◦*]"                                   # 项目符号
    r"|\d{1,3}[.)]"                                 # 1. 1)
    r"|\([a-zivx0-9]{1,4}\)"                        # (a) (iv) (1)
    r"|[a-z][.)]"                                   # a. a)
    r"|[ivx]{1,4}[.)]"                              # i. ii. iv.
    r")\s", re.I)
# 行内枚举：'i. HQ ii. Infantry iii. ...'
INLINE_ENUM = re.compile(r"(?:^|\s)(?:[ivx]{1,4}|\d{1,2})\.\s+\S+\s+(?:[ivx]{1,4}|\d{1,2})\.")


def genuine(version_id, label, *, max_gap_lines=2.5):
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
        if TOC.search(ta) or TOC.search(tb):
            continue
        if LIST_MARK.match(tb) or INLINE_ENUM.search(ta):
            continue
        if ta[-1] in SENT_END or not tb[:1].islower():
            continue
        sa, sb = (a.get("style") or {}), (b.get("style") or {})
        if abs(float(sa.get("size") or 0) - float(sb.get("size") or 0)) > 0.3:
            continue
        gap = b["bbox"][1] - a["bbox"][3]
        size = float(sa.get("size") or 10.0)
        if gap < -1.0 or gap > max_gap_lines * size:
            continue
        if min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0]) <= 0:
            continue
        hits.append((a, b))
    print(f"\n=== {label} ===  真·句中切分: {len(hits)} 处")
    for a, b in hits:
        print(f"  p{a['page']:3d} {a['kind']:10s} ...{a['text'][-58:]!r}")
        print(f"  {'':16s} + {b['text'][:58]!r}")
    return hits


os.environ["MT_DATA_DIR"] = "data/_to_probe"
import config  # noqa: E402
config.load(reload=True)
h1 = genuine(1, "TO 1F-16CMAM-34-1-1（669 页）")

os.environ["MT_DATA_DIR"] = "data"
config.load(reload=True)
h2 = genuine(2, "BMS 训练手册 v2（401 页）")
print(f"\n汇总：TO-34 {len(h1)} 处，训练手册 {len(h2)} 处")
