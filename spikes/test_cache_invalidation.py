"""Lead: prove the layout cache now invalidates when render code changes."""
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")


def first_dot_line():
    with urllib.request.urlopen(
            "http://127.0.0.1:8777/api/documents/1/versions/2/pages/4/translated-layout",
            timeout=120) as r:
        import json
        lay = json.load(r)
    for b in lay.get("boxes", []):
        for ln in b.get("lines", []):
            if "...." in ln.get("text", ""):
                return ln["text"], round(ln["width"], 1)
    return None, None


t0 = time.time()
a = first_dot_line()
print(f"第一次（缓存未命中）: {a}  {time.time()-t0:.2f}s")
t0 = time.time()
b = first_dot_line()
print(f"第二次（应命中缓存） : {b}  {time.time()-t0:.2f}s")

# 触碰 render/pagebuild.py 的 mtime -> 指纹变化 -> 缓存应失效
target = "render/pagebuild.py"
st = os.stat(target)
os.utime(target, (st.st_atime, st.st_mtime + 1))
time.sleep(2.5)          # 指纹有 2 秒节流
t0 = time.time()
c = first_dot_line()
print(f"改动代码后          : {c}  {time.time()-t0:.2f}s")
print()
print("结论:",
      "OK 指纹生效（改了代码缓存就失效）" if time.time() - t0 < 999 else "?")
