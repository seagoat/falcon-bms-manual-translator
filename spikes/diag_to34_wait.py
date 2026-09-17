"""Lead: re-check TO-34 viewer after a longer wait (was it just slow first paint?)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"
OUT = "data/derived/preview"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        for wait_ms in (5000, 10000, 20000):
            pg.goto(f"{BASE}/?doc=2&vid=3&page=351&mode=compare", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(wait_ms)
            st = pg.evaluate("""() => {
                const inView = (sel) => [...document.querySelectorAll(sel + ' .page')]
                    .filter(p => { const r = p.getBoundingClientRect();
                                   return r.bottom > 80 && r.top < window.innerHeight; })
                    .map(p => {
                        const img = p.querySelector('img');
                        const cv = p.querySelector('canvas');
                        let painted = null;
                        if (cv) {
                            try {
                                const d = cv.getContext('2d')
                                    .getImageData(0, 0, cv.width, cv.height).data;
                                let nz = 0;
                                for (let i = 3; i < d.length; i += 4) if (d[i] > 0) nz++;
                                painted = nz;
                            } catch (e) { painted = 'err'; }
                        }
                        return {page: p.getAttribute('data-page'),
                                img: img ? (img.getAttribute('src')||'').split('/pages/').pop() : null,
                                nw: img ? img.naturalWidth : -1,
                                canvas: cv ? cv.width + 'x' + cv.height : null,
                                painted};
                    });
                return {pageInput: (document.querySelector('#pageInput')||{}).value,
                        src: inView('#pages-source'), cn: inView('#pages-cn')};
            }""")
            print(f"\n=== wait {wait_ms}ms ===")
            print(json.dumps(st, ensure_ascii=False))
            print("  errors:", errs[:2])
            if wait_ms == 20000:
                pg.screenshot(path=os.path.join(OUT, "_diag_to34_p351_longwait.png"))
                print(f"  screenshot -> {OUT}/_diag_to34_p351_longwait.png")
    finally:
        b.close()
