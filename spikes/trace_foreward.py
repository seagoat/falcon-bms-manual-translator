"""Lead: trace exactly which segment the FOREWARD bookmark binds to."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

import pymupdf  # noqa: E402

from core import db as cdb, fingerprint as _fp, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
by_fp = {}
for s in segs:
    by_fp.setdefault(s["fingerprint"], []).append(s)

d = pymupdf.open("origin/TO 1F-16CMAM-1 BMS.pdf")
toc = d.get_toc() or []
d.close()

for lv, t, pg in toc[:4]:
    tt = (t or "").strip()
    f = _fp.fingerprint(tt)
    hit = (by_fp.get(f) or [None])[0]
    print(f"书签 L{lv} p{pg} {tt!r}")
    print(f"   fp={f}")
    if hit:
        print(f"   hit seg={hit['id']} p{hit['page']} {hit['kind']} {hit['text'][:60]!r}")
        print(f"   hit ZH={(tr.get(hit['id']) or {}).get('text','')[:70]!r}")
    else:
        print("   hit=None")
    # sec 匹配会不会抢走
    sec = re.match(r"^\s*(\d+[A-Z]?(?:\.\d+)*)", tt)
    print(f"   sec={sec.group(1) if sec else None}")
    print()
con.close()
