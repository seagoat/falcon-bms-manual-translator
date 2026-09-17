"""Lead: build the CN outline from the current version's heading segments.

The v1 bookmark set is unreliable (it contains bare page numbers like '1' / '2' and
mis-attributed titles such as '2.2 The mission' landing on p66). The segments already
carry a clean, hierarchical heading set with Chinese translations, so build the TOC
from those instead.
"""
import re
import sys
from collections import Counter

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

NUM = re.compile(r"^\s*(\d+[A-Z]?(?:\.\d+)*)\s*(.*)$")
PART = re.compile(r"^\s*第\s*\d+\s*部分", re.I)
MISSION = re.compile(r"^\s*任务\s*\d", re.I)


def level_of(text: str) -> int:
    """按章节号推层级：'1' -> 1, '1.1' -> 2, '1.4.1' -> 3, 更细 -> 4。"""
    m = NUM.match(text or "")
    if m:
        sec = m.group(1)
        depth = sec.count(".") + 1
        return min(4, depth)
    if PART.match(text or ""):
        return 1
    if MISSION.match(text or ""):
        return 2
    return 2


con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)

# 只看 heading 段；page_num/header 排除
heads = [s for s in segs if s["kind"] == "heading" and s["role"] == "body"]
print(f"heading 段: {len(heads)}")

toc = []
skipped = 0
for s in heads:
    en = (s["text"] or "").strip()
    zh = (tr.get(s["id"]) or {}).get("text") or en
    zh = " ".join(zh.split())
    en1 = " ".join(en.split())
    # 太短 / 纯数字 / 纯符号的不要
    if len(en1) < 3 or re.fullmatch(r"[\d.\s]+", en1):
        skipped += 1
        continue
    if len(zh) > 110:
        zh = zh[:108] + "…"
    toc.append((level_of(en1), zh or en1, int(s["page"]), en1, s["id"]))

print(f"可用条目: {len(toc)}   跳过: {skipped}")
print(f"层级分布: {dict(Counter(t[0] for t in toc))}")
print(f"页码范围: {min(t[2] for t in toc)} .. {max(t[2] for t in toc)}")
print("\n=== 前 35 条 ===")
for lv, zh, pg, en, sid in toc[:35]:
    print(f"  {'  ' * (lv - 1)}L{lv} p{pg:3d}  {zh[:56]:58s} <- {en[:40]}")

print("\n=== 抽样 20 条（跨全书）===")
step = max(1, len(toc) // 20)
for lv, zh, pg, en, sid in toc[::step]:
    print(f"  {'  ' * (lv - 1)}L{lv} p{pg:3d}  {zh[:60]}")

import json  # noqa: E402
json.dump([{"level": lv, "title": zh, "page": pg, "en": en, "seg_id": sid}
           for lv, zh, pg, en, sid in toc],
          open("data/derived/ocr/_toc_v2.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"\n已写出 data/derived/ocr/_toc_v2.json（{len(toc)} 条）")
con.close()
