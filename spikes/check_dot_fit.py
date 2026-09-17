"""Lead: does the dot-leader fitter keep the page number, and what does the
translated-layout API hand to the front-end canvas?"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

print("=== 1) 库里的原文与译文（完整，不截断）===")
con = cdb.connect()
segs = store.segments_of_version(con, 2, page=4)
tr = store.translations_of_version(con, 2)
for s in segs:
    t = (s["text"] or "").strip()
    if "...." in t:
        zh = (tr.get(s["id"]) or {}).get("text") or ""
        print(f"  seg={s['id']} bbox_x1={s['bbox'][2]:.1f} lb_x1={s['line_boxes'][-1][2]:.1f}")
        print(f"     EN 尾: {t[-30:]!r}")
        print(f"     ZH 尾: {zh[-30:]!r}")

print("\n=== 2) _fit_dot_leader 结果（前 5 条）===")
n = 0
for s in segs:
    t = (s["text"] or "").strip()
    if "...." not in t:
        continue
    zh = (tr.get(s["id"]) or {}).get("text") or ""
    if not zh:
        continue
    lb = s["line_boxes"][-1]
    fitted, w = pb._fit_dot_leader(zh, 9.2, False, 470.0, lb[2], s["bbox"][0])
    print(f"  seg={s['id']} anchor={lb[2]:.1f} -> w={w:.1f}")
    print(f"     旧: {zh[-26:]!r}")
    print(f"     新: {fitted[-26:]!r}")
    n += 1
    if n >= 5:
        break
con.close()

print("\n=== 3) /translated-layout 交给前端的行（登录手册 p4）===")
try:
    with urllib.request.urlopen(
            "http://127.0.0.1:8777/api/documents/1/versions/2/pages/4/translated-layout",
            timeout=90) as r:
        lay = json.load(r)
    dots = [b for b in lay.get("boxes", [])
            if any("...." in ln.get("text", "") for ln in b.get("lines", []))]
    print(f"  含点线的 box: {len(dots)}")
    for b in dots[:5]:
        for ln in b["lines"]:
            print(f"    x={ln['x']:6.1f} w={ln['width']:6.1f} size={ln['size']} "
                  f"text尾={ln['text'][-26:]!r}")
except Exception as e:
    print("  ERR", e)
