"""Lead: detect the real corruption signature instead of 'every split'.

The dangerous pattern is NOT every split (most splits translate fine) but:
    A's English continues into B, A's translation absorbed B's meaning,
    and B's translation is left as a tiny fragment.
Case: p185 'IT IS POSSIBLE TO EXCEED ... FOR ONSPEED' / 'TAKEOFF WITH A NET ASYMMETRIC ...'
      A.zh = '在净不对称（滚转）力矩小于飞机起飞限制的情况下进行准时起飞，'
      B.zh = '有可能超出飞机的横向配平权限。'          <- B's EN is long, B's ZH is only 14 chars

Signature: len(B.en) >= 30 but len(B.zh) < 20, with A ending mid-sentence.
This is precise and low-volume.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")
EN_MIN = 30
ZH_MAX = 20


def scan(db_env: str, vid: int, label: str):
    os.environ["MT_DATA_DIR"] = db_env
    import config
    config.load(reload=True)
    for m in [m for m in list(sys.modules) if m.startswith("core")]:
        del sys.modules[m]
    from core import db as cdb2, store as st2
    con = cdb2.connect()
    tr = st2.translations_of_version(con, vid)
    segs = [s for s in st2.segments_of_version(con, vid) if s["role"] == "body"]
    hits = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        ea, eb = (a["text"] or "").rstrip(), (b["text"] or "").strip()
        if not ea or not eb:
            continue
        if b["page"] not in (a["page"], a["page"] + 1):
            continue
        if len(eb) < EN_MIN:
            continue
        tb = tr.get(b["id"])
        if not tb or tb.get("status") != "machine":
            continue
        zb = (tb.get("text") or "").strip()
        if len(zb) >= ZH_MAX:
            continue
        # 额外要求：A 未以句末标点收尾（确实是被切开）
        if ea[-1] in SENT_END:
            continue
        # 且 B 不是数字/纯符号（排除页码）
        if all(c.isdigit() or c in "/.- " for c in eb):
            continue
        ta = tr.get(a["id"])
        hits.append((a, b, ta.get("text") if ta else "", zb))
    print(f"\n{'=' * 74}\n=== {label} 命中 {len(hits)} 对 ===")
    for a, b, za, zb in hits[:20]:
        print(f"  p{a['page']} seg={a['id']}/{b['id']}")
        print(f"     A.en ...{a['text'][-58:]!r}")
        print(f"     A.zh ...{za[-58:]!r}")
        print(f"     B.en {b['text'][:58]!r}   (len={len(b['text'])})")
        print(f"     B.zh {zb[:58]!r}          (len={len(zb)})")
    con.close()
    return len(hits)


n1 = scan("data/_to_probe", 2, "TO-1")
n2 = scan("data/_to_probe", 1, "TO-34")
n3 = scan("data", 2, "BMS 训练手册 v2")
print(f"\n汇总: TO-1 {n1} / TO-34 {n2} / 训练手册 {n3}")
