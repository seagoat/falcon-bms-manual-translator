"""Lead: why does TO-1 bookmark '1.1 Aircraft General Arrangement' (p19) map to p3?"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

import pymupdf  # noqa: E402

from core import db as cdb, fingerprint as _fp, store  # noqa: E402

VID = 2
WANT = 19
T = "1.1 Aircraft General Arrangement"

d = pymupdf.open("origin/TO 1F-16CMAM-1 BMS.pdf")
toc = d.get_toc() or []
print("书签里 '1.1' 相关条目：")
for lv, t, pg in toc:
    if re.match(r"^\s*1\.1\b", t or ""):
        print(f"  L{lv} p{pg:4d}  {t!r}")
d.close()

con = cdb.connect()
segs = store.segments_of_version(con, VID)
sec_re = re.compile(r"^\s*(\d+[A-Z]?(?:\.\d+)*)")
cands = [s for s in segs if (sec_re.match(s["text"] or "") or [None]) and
         sec_re.match(s["text"] or "").group(1) == "1.1"]
print(f"\n小节号 '1.1' 的候选段: {len(cands)}")
for s in cands:
    print(f"  seg={s['id']} p{s['page']:4d} {s['kind']:10s} {s['text'][:64]!r}")

# 让真实书签标题参与匹配
print(f"\n书签文本: {T!r}")
all_toc = None
d = pymupdf.open("origin/TO 1F-16CMAM-1 BMS.pdf")
for lv, t, pg in (d.get_toc() or []):
    if (t or "").strip() == T:
        all_toc = (lv, t, pg)
d.close()
print(f"  在书签表里: {all_toc}")
con.close()
