"""Lead: call compute_layout directly for TOC page 4 and inspect the dot lines."""
import json
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2, page=4)
trs = store.translations_of_version(con, 2)
con.close()

boxes = pb.compute_layout(segs, trs, page_no=4)
print(f"boxes: {len(boxes)}")
n = 0
for b in boxes:
    for ln in b["lines"]:
        if "...." in ln["text"]:
            print(f"  seg={b['seg_id']} x={ln['x']:6.1f} w={ln['width']:6.1f} "
                  f"末端={ln['x']+ln['width']:6.1f} size={ln['size']}")
            print(f"      {ln['text'][-34:]!r}")
            n += 1
            break
    if n >= 6:
        break
