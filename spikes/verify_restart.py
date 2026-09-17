"""Lead: verify the restarted server serves the NEW code and fixed output."""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8777"
ok = []


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return r, json.load(r)


# 1) health
_, h = get("/api/health")
print(f"1) health ok={h.get('ok')} db={h.get('db')}")
ok.append(("health", h.get("ok") is True))

# 2) 三份文档
_, docs = get("/api/documents")
print(f"2) documents = {len(docs)}")
for d in docs:
    print(f"     doc{d['doc_id']} {d.get('slug'):22s} pages={d.get('page_count')}")
ok.append(("三份文档", len(docs) == 3))

# 3) 布局含页码
_, lay = get("/api/documents/1/versions/2/pages/4/translated-layout")
dots = []
for b in lay.get("boxes", []):
    for ln in b.get("lines", []):
        if "...." in ln.get("text", ""):
            dots.append(ln["text"])
with_num = [t for t in dots if t.rstrip()[-1:].isdigit()]
print(f"3) 目录点线行 {len(dots)} 条，其中带页码 {len(with_num)} 条")
print(f"     例: {dots[0][:30]!r}...{dots[0][-14:]!r}")
ok.append(("布局含页码", len(dots) > 0 and len(with_num) == len(dots)))

# 4) 静态资源缓存头
req = urllib.request.Request(BASE + "/app.js")
with urllib.request.urlopen(req, timeout=60) as r:
    cc = r.headers.get("Cache-Control")
    etag = r.headers.get("ETag")
    size = len(r.read())
print(f"4) /app.js  Cache-Control={cc!r}  ETag={etag!r}  bytes={size}")
# 中间件对 .js/.html/.css 设的是 no-store（比 no-cache 更彻底），两者都算合格
ok.append(("静态资源不被长期缓存", cc in ("no-store", "no-cache")))

# 5) app.js 里应含深链修复标记
with urllib.request.urlopen(BASE + "/app.js", timeout=60) as r:
    js = r.read().decode("utf-8", "replace")
has_fix = "scrollRestoration" in js and "deepControl" in js
print(f"5) /app.js 含深链修复(scrollRestoration+deepControl): {has_fix}")
ok.append(("app.js 是最新代码", has_fix))

# 6) 三份文档各取一页布局，确认都能出页码/内容
for did, vid, page in ((1, 2, 4), (2, 3, 351), (3, 4, 300)):
    _, L = get(f"/api/documents/{did}/versions/{vid}/pages/{page}/translated-layout")
    n = len(L.get("boxes", []))
    print(f"6) doc{did} p{page}: {n} boxes")
    ok.append((f"doc{did} p{page} 有布局", n > 0))

print()
passed = sum(1 for _, v in ok if v)
print(f"== {passed}/{len(ok)} 通过 ==")
for name, v in ok:
    print(f"  {'PASS' if v else 'FAIL'}  {name}")
sys.exit(0 if passed == len(ok) else 1)
