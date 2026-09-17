"""Lead: build a clean CN outline by combining v1 bookmark structure with v2 headings.

* v1 bookmarks : curated level + page + section number  (but bad titles on 44 entries)
* v2 headings  : clean text + Chinese translation       (but noisy "level")

Strategy: match each v1 bookmark to a v2 heading by **section number** (stable across
versions), take the v1 level and the v2 Chinese title. Bookmarks that are only a bare
number ('1', '1.1') are redundant with the real entries -> drop them.
"""
import json
import re
import sys
from collections import Counter

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

from core import db as cdb, fingerprint as fp, store

V1 = "origin/BMS-Training-Manual.pdf"
SEC = re.compile(r"^\s*(\d+[A-Z]?(?:\.\d+)*)")
BARE = re.compile(r"^\s*\d+[A-Z]?(?:\.\d+)*\s*$")


def sec_of(text: str) -> str:
    m = SEC.match(text or "")
    return m.group(1) if m else ""


d1 = pymupdf.open(V1)
toc1 = d1.get_toc() or []
d1.close()
print(f"v1 书签: {len(toc1)}")

con = cdb.connect()
segs2 = store.segments_of_version(con, 2)
tr2 = store.translations_of_version(con, 2)

# v2 所有段：按小节号建索引（heading 优先）
by_sec = {}
for s in sorted(segs2, key=lambda x: (x["kind"] != "heading", x["order_index"])):
    sec = sec_of(s["text"])
    if sec:
        by_sec.setdefault(sec, []).append(s)
# 按标题指纹建索引（处理没有编号的条目）
by_fp = {}
for s in segs2:
    by_fp.setdefault(s["fingerprint"], []).append(s)

out, dropped, matched, by_title, by_page = [], 0, 0, 0, 0
for level, title, page in toc1:
    t = (title or "").strip()
    if BARE.match(t):                       # 纯页码条目：冗余
        dropped += 1
        continue
    sec = sec_of(t)
    hit = None

    def _pick(cands, want_page, want_title):
        """在同小节号的候选里挑最合适的：优先 heading、优先靠近原书签页、优先标题一致。"""
        if not cands:
            return None
        tn = fp.normalize(want_title).casefold()

        def score(s):
            sc = 0.0
            if s["kind"] == "heading":
                sc += 2.0
            d = abs(int(s["page"]) - want_page)
            sc -= min(d, 40) * 0.05
            st = fp.normalize(s["text"]).casefold()
            if st.startswith(tn):
                sc += 1.5
            elif tn and tn in st:
                sc += 0.8
            # 目录页（前 10 页）多半是目录行，不是真正的节
            if int(s["page"]) <= 10 and d > 20:
                sc -= 2.0
            return sc

        return max(cands, key=score)

    if sec and sec in by_sec:
        cands = [s for s in by_sec[sec]]
        hit = _pick(cands, int(page), t)
        matched += 1
    if hit is None:
        hit = (by_fp.get(fp.fingerprint(t)) or [None])[0]
        if hit:
            by_title += 1
    if hit is None:
        # 退而取该页上文字与标题最接近的段
        cands = [s for s in segs2 if int(s["page"]) == int(page)]
        tn = fp.normalize(t).casefold()
        best, bestr = None, 0.0
        for s in cands:
            r = fp.ratio(s["text"], t)
            if r > bestr:
                best, bestr = s, r
        if best and bestr >= 0.55:
            hit = best
            by_page += 1
    if hit is None:
        dropped += 1
        continue
    # 合理性检查：解析出的页与书签页差太远，且不是同小节号命中 → 丢弃
    if sec and sec in by_sec:
        pass
    elif abs(int(hit["page"]) - int(page)) > 12:
        dropped += 1
        continue

    zh = (tr2.get(hit["id"]) or {}).get("text") or hit["text"]
    zh = " ".join(zh.split())
    if not zh:
        zh = t
    if len(zh) > 110:
        zh = zh[:108] + "…"
    out.append({"level": int(level), "title": zh, "page": int(hit["page"]),
                "en": t, "seg_id": int(hit["id"])})

print(f"\n=== 结果 ===")
print(f"  输出条目  : {len(out)}")
print(f"  丢弃      : {dropped}（其中纯页码条目冗余 / 无法定位）")
print(f"  定位方式  : 小节号 {matched} / 标题指纹 {by_title} / 同页模糊 {by_page}")
print(f"  层级分布  : {dict(sorted(Counter(o['level'] for o in out).items()))}")
print(f"  页码范围  : {min(o['page'] for o in out)} .. {max(o['page'] for o in out)}")

print("\n=== 前 30 条 ===")
for o in out[:30]:
    print(f"  {'  ' * (o['level'] - 1)}L{o['level']} p{o['page']:3d}  {o['title'][:60]}")

print("\n=== 抽样（全书均匀）===")
step = max(1, len(out) // 16)
for o in out[::step]:
    print(f"  {'  ' * (o['level'] - 1)}L{o['level']} p{o['page']:3d}  {o['title'][:60]}")

json.dump(out, open("data/derived/_toc_cn.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"\n已写出 data/derived/_toc_cn.json（{len(out)} 条）")
con.close()
