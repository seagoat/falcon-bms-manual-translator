"""Lead: trace why seg=14556 is still flagged by the audit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402
from translate import quality  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
by_id = {int(s["id"]): i for i, s in enumerate(segs)}

sid = 14556
i = by_id[sid]
s = segs[i]
nxt = segs[i + 1] if i + 1 < len(segs) else None
en = (s["text"] or "").rstrip()
zh = (tr.get(sid) or {}).get("text") or ""
nb = (nxt["text"] or "").lstrip() if nxt else ""
print(f"EN : {en!r}")
print(f"ZH : {zh!r}")
print(f"next: {nb!r}")
print(f"  EN 末尾字符      : {en[-1]!r}")
print(f"  在 SENT_END 里?  : {en[-1] in quality.SENT_END}")
print(f"  在 ,;，、-–— 里? : {en[-1] in ',;，、-–—'}")
print(f"  下一段首字符     : {nb[:1]!r}")
print(f"  下一段小写或数字 : {bool(nb) and (nb[:1].islower() or nb[:1].isdigit())}")
print(f"  substantive      : {len(quality._strip_punct(zh)) >= 3}")
print(f"  页码差           : {nxt['page'] - s['page'] if nxt else None}")
print(f"suspect(): {quality.suspect(s['text'], zh)!r}")
print(f"\n完整 audit 命中: {[x['seg']['id'] for x in quality.audit(con, 2)]}")
con.close()
