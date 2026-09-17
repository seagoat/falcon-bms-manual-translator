"""Lead: recover TO-1 segments damaged by the over-broad repair_continuation.

The damaged pairs are identifiable: B's translation is now long (>=30 chars) even
though B's English is short-ish and A did NOT end with sentence punctuation.
Re-translate such groups INDIVIDUALLY (each segment on its own) to restore clean text.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402
from translate import engine  # noqa: E402
import config as _cfg  # noqa: E402

SENT_END = set(".!?。！？:;\"'”’)")

con = cdb.connect()
tr = store.translations_of_version(con, 2)
segs = [s for s in store.segments_of_version(con, 2) if s["role"] == "body"]

# 受损特征：A 未以句末标点结尾，B 原文 < 60 字符，但 B 译文却很长（>=40 字符）
damaged = []
for i in range(len(segs) - 1):
    a, b = segs[i], segs[i + 1]
    ea, eb = (a["text"] or "").rstrip(), (b["text"] or "").strip()
    if not ea or not eb or ea[-1] in SENT_END:
        continue
    if b["page"] not in (a["page"], a["page"] + 1):
        continue
    if len(eb) >= 60:
        continue
    tb = tr.get(b["id"])
    if not tb:
        continue
    zb = (tb.get("text") or "").strip()
    if len(zb) < 40:
        continue
    damaged.append((a, b))

print(f"疑似受损对: {len(damaged)}")
for a, b in damaged[:10]:
    print(f"  p{a['page']} A={a['text'][:40]!r}")
    print(f"          B={b['text'][:40]!r} -> {len((tr.get(b['id']) or {}).get('text',''))} 字")

# 受影响段集合
ids = sorted({int(x["id"]) for p in damaged for x in p})
print(f"受影响段: {len(ids)}")

# 逐段独立重译（不带上下文），恢复干净译文
cfg = dict(_cfg.load())
api_key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
gl = engine.glossary.prompt_block()
items = [engine._Item(s["id"], s["text"], s["page"], "body", s["kind"])
         for s in segs if int(s["id"]) in set(ids)]
print(f"逐段独立重译 {len(items)} 段 ...")
got = {}
for i in range(0, len(items), 8):
    r = engine._run_batch(items[i:i + 8], {}, provider="deepseek",
                          model=cfg.get("model", "deepseek-flash"), cfg=cfg,
                          api_key=api_key, glossary_block=gl)
    if r.get("ok"):
        got.update(r["out"])
print(f"  成功 {len(got)}/{len(items)}")

fixed = 0
for s in segs:
    sid = int(s["id"])
    if sid not in got:
        continue
    store.upsert_translation(con, segment_id=sid, text=got[sid], status="machine",
                             provider="deepseek", model=cfg.get("model", "deepseek-flash"))
    fixed += 1
con.commit()
print(f"已恢复 {fixed} 段")

print("\n=== 复核已知案例 ===")
tr2 = store.translations_of_version(con, 2)
for sid in (14398, 14399, 11841, 11842, 11843):
    s = store.get_segment(con, sid)
    if s:
        print(f"  seg={sid} EN: {s['text'][:60]!r}")
        print(f"           ZH: {(tr2.get(sid) or {}).get('text','')[:60]!r}")
con.close()
