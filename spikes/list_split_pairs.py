"""Lead: list the split pairs the refined detector finds in the TO DB before repairing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402
from translate import quality  # noqa: E402

for vid, label in ((2, "TO-1"), (1, "TO-34")):
    con = cdb.connect()
    tr = store.translations_of_version(con, vid)
    segs = [s for s in store.segments_of_version(con, vid) if s["role"] == "body"]
    hits = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        ea, eb = (a["text"] or "").rstrip(), (b["text"] or "").strip()
        if not ea or not eb:
            continue
        if b["page"] not in (a["page"], a["page"] + 1):
            continue
        why = None
        if ea[-1] in "(（[【":
            why = "开括号"
        else:
            mid = ea[-1].isalpha() and not ea.endswith(("etc.", "vs."))
            cont = bool(eb) and (eb[:1].islower() or eb[:1].isdigit()
                                 or (eb[:1].isupper() and ea[-1].islower()
                                     and not ea.endswith(".")))
            if mid and ea[-1] not in quality.SENT_END and cont:
                why = "句中切"
        if why:
            hits.append((a, b, why))
    print(f"\n{'=' * 74}\n=== {label} (version_id={vid}) 命中 {len(hits)} 对 ===")
    for a, b, why in hits[:14]:
        print(f"  [{why}] p{a['page']} seg={a['id']}")
        print(f"     A: ...{a['text'][-62:]!r}")
        print(f"     B: {b['text'][:62]!r}")
    con.close()
