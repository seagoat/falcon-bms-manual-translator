"""Lead: find paragraph segments that were split mid-sentence (TO-34).

A segment that ends without terminal punctuation and whose next segment starts with
a lowercase letter is almost certainly a mid-sentence split, which breaks translation
(translators cannot see the other half).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("MT_DATA_DIR", "data/_to_probe")

from core import db as cdb, store  # noqa: E402

TERM = re.compile(r"[.?!:;”\"')\]]\s*$")


def scan(version_id: int, label: str):
    con = cdb.connect()
    rows = [store.get_segment(con, s["id"]) for s in
            store.segments_of_version(con, version_id) if s["role"] == "body"]
    con.close()
    bad = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if not a or not b:
            continue
        ta, tb = (a["text"] or "").strip(), (b["text"] or "").strip()
        if not ta or not tb:
            continue
        # 同一页、同一 kind、a 结尾无终止标点、b 以小写字母开头
        if (a["page"] == b["page"] and a["kind"] == b["kind"]
                and not TERM.search(ta) and tb[:1].islower()
                and len(ta) > 25):
            bad.append((a, b))
    print(f"\n=== {label}：疑似句中切分 {len(bad)} 处 ===")
    for a, b in bad[:14]:
        print(f"  p{a['page']} {a['kind']}")
        print(f"    A尾部: ...{a['text'][-70:]!r}")
        print(f"    B头部: {b['text'][:70]!r}")
    return bad


# TO-34
b34 = scan(1, "TO 1F-16CMAM-34-1-1（669 页）")

# 对照：训练手册
os.environ["MT_DATA_DIR"] = "data"
import importlib  # noqa: E402
import config  # noqa: E402
config.load(reload=True)
from core import db as cdb2  # noqa: E402
con2 = cdb2.connect()
if os.path.realpath(str(config.db_path())).endswith("app.db"):
    segs = [store.get_segment(con2, s["id"]) for s in
            store.segments_of_version(con2, 2) if s["role"] == "body"]
    bad = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if not a or not b:
            continue
        ta, tb = (a["text"] or "").strip(), (b["text"] or "").strip()
        if not ta or not tb:
            continue
        if (a["page"] == b["page"] and a["kind"] == b["kind"]
                and not TERM.search(ta) and tb[:1].islower() and len(ta) > 25):
            bad.append((a, b))
    print(f"\n=== 对照：BMS 训练手册 v2（401 页）：疑似句中切分 {len(bad)} 处 ===")
    for a, b in bad[:6]:
        print(f"  p{a['page']} A尾部: ...{a['text'][-60:]!r}")
        print(f"        B头部: {b['text'][:60]!r}")
con2.close()
