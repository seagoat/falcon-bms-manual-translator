"""Lead: reproduce the 'blank page after scrolling' report.

Scrolls progressively through the document and checks, for pages near the viewport,
whether each pane's <img> actually has a loaded bitmap or is an empty placeholder.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errs, failed, responses = [], [], []
        pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        pg.on("requestfailed", lambda r: failed.append((r.url.split("/api")[-1], r.failure)))
        pg.on("response", lambda r: responses.append((r.status, r.url.split("/api")[-1]))
              if "/api/" in r.url else None)

        pg.goto(f"{BASE}/?doc=1&vid=2&page=1&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(4000)

        def snapshot(tag):
            st = pg.evaluate("""() => {
                const pane = (sel) => [...document.querySelectorAll(sel + ' .page')].map(p => {
                    const r = p.getBoundingClientRect();
                    const inView = r.bottom > 80 && r.top < window.innerHeight;
                    const img = p.querySelector('img');
                    return {
                        page: p.getAttribute('data-page'),
                        inView,
                        imgSrc: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : null,
                        nw: img ? img.naturalWidth : -1,
                        complete: img ? img.complete : null,
                    };
                });
                return {src: pane('#pages-source'), cn: pane('#pages-cn')};
            }""")
            bad = []
            for side in ("src", "cn"):
                for e in st[side]:
                    if e["inView"] and (not e["imgSrc"] or e["nw"] <= 0):
                        bad.append((side, e))
            print(f"  [{tag}] in-view pages: "
                  f"src={[e['page'] for e in st['src'] if e['inView']]} "
                  f"cn={[e['page'] for e in st['cn'] if e['inView']]}")
            if bad:
                print(f"    !! {len(bad)} in-view page(s) WITHOUT a loaded image:")
                for side, e in bad:
                    print(f"       {side} page={e['page']} src={e['imgSrc']!r} "
                          f"complete={e['complete']} naturalWidth={e['nw']}")
            else:
                print("    OK all in-view pages have loaded images")
            return st, bad

        print("=== jump to a late page, then scroll ===")
        for target in (12, 40, 120, 300):
            pg.evaluate(f"""() => {{
                const inp = document.querySelector('#pageInput');
                inp.value = '{target}';
                inp.dispatchEvent(new Event('change', {{bubbles:true}}));
                inp.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', bubbles:true}}));
            }}""")
            pg.wait_for_timeout(3500)
            print(f"\n--- after jumping to page {target}")
            snapshot(f"jump {target}")

            # scroll within the page and also step forward
            for step in range(3):
                pg.evaluate("document.querySelector('#scroll-source').scrollTop += 1400;"
                            "document.querySelector('#scroll-cn').scrollTop += 1400;")
                pg.wait_for_timeout(1800)
                snapshot(f"target{target} scroll+{step+1}")

        print("\n=== failed requests ===")
        for u, f in failed[:20]:
            print("  ", u, f)
        print("=== non-200 API responses ===")
        for s, u in responses:
            if s >= 400:
                print(f"   {s} {u}")
        print(f"=== page errors: {errs[:5]}")
        pg.screenshot(path=os.path.join(OUT, "_diag_blank_page.png"))
    finally:
        b.close()
