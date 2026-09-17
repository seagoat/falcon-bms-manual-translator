"""Lead: find why viewer page <img> elements have empty src, and test deep-link page."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
reqs = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("request", lambda r: reqs.append(r.url) if "/pages/" in r.url or "/image" in r.url else None)
        pg.on("console", lambda m: print(f"  [console.{m.type}] {m.text[:200]}"))
        pg.goto(f"{BASE}/", wait_until="load")
        pg.wait_for_timeout(8000)

        info = pg.evaluate("""() => {
          const all = [...document.querySelectorAll('img')].map(i => ({
              id: i.id || null, cls: i.className,
              srcAttr: i.getAttribute('src'),
              srcProp: i.src,
              dataSrc: i.getAttribute('data-src'),
              complete: i.complete, nw: i.naturalWidth, nh: i.naturalHeight,
          }));
          const pages = [...document.querySelectorAll('.page')].slice(0,4).map(p => ({
              id: p.id || null, cls: p.className,
              style: p.getAttribute('style'),
              html: p.innerHTML.slice(0, 300),
          }));
          return {imgCount: all.length, imgs: all.slice(0, 12), pages};
        }""")
        print("=== images ===")
        print(json.dumps(info["imgs"], ensure_ascii=False, indent=1))
        print(f"\ntotal imgs={info['imgCount']}")
        print("\n=== first pages ===")
        for pp in info["pages"]:
            print(json.dumps(pp, ensure_ascii=False, indent=1)[:900])

        print("\n=== image-ish network requests ===")
        for u in reqs[:20]:
            print("  ", u)

        # deep link test
        print("\n=== deep link ?page=24 ===")
        pg.goto(f"{BASE}/?doc=1&vid=2&page=24&mode=compare", wait_until="load")
        pg.wait_for_timeout(7000)
        dl = pg.evaluate("""() => ({
            url: location.search,
            activePages: [...document.querySelectorAll('.page')].filter(p => {
                const r = p.getBoundingClientRect();
                return r.bottom > 0 && r.top < window.innerHeight;
            }).map(p => p.dataset.page || p.getAttribute('data-page') || p.id),
            pgLabel: document.querySelector('#pgLabel') ? document.querySelector('#pgLabel').textContent : null,
            pageInput: document.querySelector('#pageInput') ? document.querySelector('#pageInput').value : null,
            firstPageStyle: document.querySelector('#pages-cn .page') ? document.querySelector('#pages-cn .page').getAttribute('style') : null,
        })""")
        print(json.dumps(dl, ensure_ascii=False, indent=1))
    finally:
        b.close()
