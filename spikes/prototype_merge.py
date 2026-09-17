"""Lead: prototype a mid-sentence continuation merge and measure impact.

The existing _merge_continuations is too strict: it requires
  gap <= max(6, 0.6*line_pitch)  AND  (indented or next-starts-lowercase)
so a split like
  "... is allowed"            / "without the written permission of the BMS Docs team."
or
  "The manufacturers ... represented in"  / "Falcon BMS in no way endorse ..."
is left split. A translator seeing only half a sentence produces garbage
(observed: "without the written permission..." -> "。", "Falcon BMS in no way endorse..." -> "绝不认可…").

This script measures a relaxed rule on BOTH manuals' existing segment data before
any pipeline change: how many merges, and are they genuinely continuations?
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")
TOC = re.compile(r"\.{4,}\s*\d*\s*$")
LIST_MARK = re.compile(r"^\s*(?:[-•▪◆]|\d+[.)]|[a-z][.)]|\(\d+\)|FIGURE|TABLE|NOTE|WARNING|CAUTION)\b",
                       re.I)


def candidate(a, b, *, max_gap_lines=2.2):
    """a 后面紧跟 b：是否是「句中切分」？"""
    ta, tb = (a["text"] or "").strip(), (b["text"] or "").strip()
    if not ta or not tb:
        return False
    if a["page"] != b["page"] or a["role"] != "body" or b["role"] != "body":
        return False
    if a["kind"] in ("heading", "caption", "table_cell") or b["kind"] in ("heading", "caption"):
        return False
    if TOC.search(ta) or TOC.search(tb):
        return False
    if LIST_MARK.match(tb):
        return False
    # 上游段不能以句末标点结束
    if ta[-1] in SENT_END:
        return False
    # 字号/字体要一致
    sa, sb = (a.get("style") or {}), (b.get("style") or {})
    if abs(float(sa.get("size") or 0) - float(sb.get("size") or 0)) > 0.3:
        return False
    # 垂直紧邻：b 必须在上方 a 的下方，且间距不超过 ~2.2 行
    gap = b["bbox"][1] - a["bbox"][3]
    size = float(sa.get("size") or 10.0)
    if gap < -1.0 or gap > max_gap_lines * size:
        return False
    # 横向要有重叠（同一栏）
    x_overlap = min(a["bbox"][2], b["bbox"][2]) - max(a["bbox"][0], b["bbox"][0])
    if x_overlap <= 0:
        return False
    # 关键新增：下一段首字母小写 **或** 上一段以连词/介词/冠词等「明显未完结」的词结尾
    lower = tb[:1].islower()
    dangling = re.search(
        r"\b(?:and|or|the|a|an|of|to|in|on|at|by|for|with|without|from|as|that|which|"
        r"is|are|was|were|be|been|will|would|can|could|should|may|might|must|not|"
        r"in|into|over|under|than|then|when|while|if|because|so|but|its|their|his|her|"
        r"this|these|those|such|each|any|all|no|only|also|both|either|neither)\s*$",
        ta, re.I)
    return bool(lower or dangling)


def scan(version_id, label, db):
    con = cdb.connect() if db is None else cdb.connect(db)
    segs = [store.get_segment(con, s["id"]) for s in
            store.segments_of_version(con, version_id) if s["role"] == "body"]
    con.close()
    hits = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if not a or not b:
            continue
        if candidate(a, b):
            hits.append((a, b))
    print(f"\n=== {label} ===  可合并（放宽规则）: {len(hits)} 处")
    for a, b in hits[:18]:
        print(f"  p{a['page']} {a['kind']:10s} ...{a['text'][-58:]!r}")
        print(f"  {' ' * 16} + {b['text'][:58]!r}")
    return hits


# TO-34（隔离库）
os.environ["MT_DATA_DIR"] = "data/_to_probe"
import config  # noqa: E402
config.load(reload=True)
h1 = scan(1, "TO 1F-16CMAM-34-1-1（669 页）", None)

# 训练手册（正式库）
os.environ["MT_DATA_DIR"] = "data"
config.load(reload=True)
h2 = scan(2, "BMS 训练手册 v2（401 页）", None)
print(f"\n汇总：TO-34 可合并 {len(h1)} 处；训练手册 {len(h2)} 处")
