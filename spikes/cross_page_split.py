"""Lead: find sentences that continue ACROSS a page boundary and got mistranslated."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")
TOC = re.compile(r"\.{4,}\s*\d*\s*$")
LIST_MARK = re.compile(
    r"^\s*(?:[-•▪◆●○◦*]|\d{1,3}[.)]|\([a-zivx0-9]{1,4}\)|[a-z][.)]|[ivx]{1,4}[.)])\s", re.I)


def cross_page(version_id, label):
    con = cdb.connect()
    segs = [store.get_segment(con, s["id"]) for s in
            store.segments_of_version(con, version_id) if s["role"] == "body"]
    tr = store.translations_of_version(con, version_id)
    con.close()
    hits = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if not a or not b:
            continue
        ta, tb = (a["text"] or "").strip(), (b["text"] or "").strip()
        if not ta or not tb:
            continue
        # 跨页、a 在上、结尾无句末标点、b 小写开头
        if b["page"] != a["page"] + 1 or a["role"] != "body" or b["role"] != "body":
            continue
        if TOC.search(ta) or LIST_MARK.match(tb) or ta[-1] in SENT_END:
            continue
        if not tb[:1].islower():
            continue
        zh_a = (tr.get(a["id"]) or {}).get("text", "")
        zh_b = (tr.get(b["id"]) or {}).get("text", "")
        hits.append((a, b, zh_a, zh_b))
    print(f"\n=== {label} ===  跨页句中切分: {len(hits)} 处")
    for a, b, za, zb in hits:
        print(f"  p{a['page']} -> p{b['page']}")
        print(f"    EN-A 尾部: ...{a['text'][-70:]!r}")
        print(f"    ZH-A 尾部: ...{za[-60:]!r}")
        print(f"    EN-B 头部: {b['text'][:70]!r}")
        print(f"    ZH-B     : {zb[:70]!r}")
        print()
    return hits


os.environ["MT_DATA_DIR"] = "data/_to_probe"
import config  # noqa: E402
config.load(reload=True)
h1 = cross_page(1, "TO-34")

os.environ["MT_DATA_DIR"] = "data"
config.load(reload=True)
h2 = cross_page(2, "BMS 训练手册 v2")
print(f"\n汇总：TO-34 {len(h1)} 处，训练手册 {len(h2)} 处")

# 特别检查那个已知的坏例子
os.environ["MT_DATA_DIR"] = "data/_to_probe"
config.load(reload=True)
con = cdb.connect()
tr = store.translations_of_version(con, 1)
for sid in (33, 34):
    s = store.get_segment(con, sid)
    t = tr.get(sid)
    if s:
        print(f"\nseg={sid} p{s['page']} {s['kind']}")
        print(f"  EN: {s['text']!r}")
        print(f"  ZH: {(t['text'] if t else None)!r}")
con.close()
