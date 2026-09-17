"""Lead: diagnose why the viewer renders no hot zones against real backend data."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "data/derived/preview"
os.makedirs(OUT, exist_ok=True)

console, errors, responses = [], [], []

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("console", lambda m: console.append((m.type, m.text[:300])))
    pg.on("pageerror", lambda e: errors.append(str(e)[:500]))
    pg.on("response", lambda r: responses.append((r.status, r.url.replace(URL.rstrip("/"), ""))))

    pg.goto(URL, wait_until="load")
    pg.wait_for_timeout(9000)

    print("=== responses (non-200 first) ===")
    for st, u in responses:
        if st >= 400:
            print(f"  !! {st} {u}")
    print("  total responses:", len(responses))
    for st, u in responses[:40]:
        print(f"  {st} {u}")

    print("\n=== console ===")
    for t, m in console[:40]:
        print(f"  [{t}] {m}")
    print("\n=== page errors ===")
    for e in errors[:20]:
        print("  ", e)

    info = pg.evaluate("""() => ({
        readyState: document.documentElement.dataset.ready || null,
        appErr: document.querySelector('#errorBar') ? document.querySelector('#errorBar').textContent.trim().slice(0,300) : null,
        appErrVisible: document.querySelector('#errorBar') ? !document.querySelector('#errorBar').hidden : null,
        pagesSource: document.querySelectorAll('#pages-source .page').length,
        pagesCn: document.querySelectorAll('#pages-cn .page').length,
        hotSource: document.querySelectorAll('#pages-source .hot[data-seg]').length,
        hotCn: document.querySelectorAll('#pages-cn .hot[data-seg]').length,
        imgs: [...document.querySelectorAll('#pages-source img')].map(i => ({src: i.src.slice(-60), w: i.naturalWidth, h: i.naturalHeight, complete: i.complete})).slice(0,4),
        toc: document.querySelectorAll('#tocList li').length,
        docSel: document.querySelector('#docSelect') ? document.querySelector('#docSelect').options.length : -1,
        verSel: document.querySelector('#verSelect') ? document.querySelector('#verSelect').options.length : -1,
        status: document.querySelector('#statusText') ? document.querySelector('#statusText').textContent.trim().slice(0,200) : null,
    })""")
    print("\n=== DOM state ===")
    print(json.dumps(info, ensure_ascii=False, indent=1))

    pg.screenshot(path=os.path.join(OUT, "diag_real_ui.png"))
    pg.wait_for_timeout(500)
    b.close()
print("\nscreenshot ->", os.path.join(OUT, "diag_real_ui.png"))
