"""Lead: reproduce the user's workflow - flip pages and confirm each renders differently."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
        pg.goto(f"{BASE}/?doc=1&vid=2&page=1&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(4000)

        # page 1 cover: source pane top page must be the cover (page attr 1)
        st = pg.evaluate("""() => ({
            pageInput: (document.querySelector('#pageInput')||{}).value,
            firstSrcPage: (document.querySelector('#pages-source .page')||{}).getAttribute?.('data-page'),
            firstCnPage: (document.querySelector('#pages-cn .page')||{}).getAttribute?.('data-page'),
            srcHots: document.querySelectorAll('#pages-source .hot[data-seg]').length,
            cnHots: document.querySelectorAll('#pages-cn .hot[data-seg]').length,
        })""")
        print("page 1 state:", st)
        check("打开即为第 1 页（封面）", st["pageInput"] == "1" and st["firstSrcPage"] == "1", str(st))
        pg.screenshot(path=os.path.join(OUT, "_fix_ui_page1.png"))

        # flip through pages and hash each source <img> to prove content changes
        seen = {}
        for n in (1, 2, 3, 21, 22):
            pg.evaluate(f"""() => {{
                const inp = document.querySelector('#pageInput');
                inp.value = '{n}';
                inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                inp.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', bubbles:true}}));
            }}""")
            pg.wait_for_timeout(2600)
            sig = pg.evaluate(f"""() => {{
                const img = document.querySelector('#pages-source .page[data-page="{n}"] img');
                if (!img) return null;
                return {{src: (img.getAttribute('src')||img.getAttribute('data-src')||''),
                         complete: img.complete, nw: img.naturalWidth}};
            }}""")
            seen[n] = sig
            print(f"  page {n:3d} -> {sig}")
        srcs = {n: (v or {}).get("src", "") for n, v in seen.items()}
        check("翻页后每页加载各自的页面图",
              len(set(srcs.values())) == len(srcs) and all(srcs.values()),
              f"{len(set(srcs.values()))}/{len(srcs)} distinct")
        check("翻页后图片已加载完成",
              all((v or {}).get("complete") for v in seen.values()))
        check("无 JS 页面错误", not errors, str(errors[:2]))
    finally:
        b.close()

print(f"\n== 结果 {sum(results)}/{len(results)} 通过 ==")
sys.exit(0 if all(results) else 1)
