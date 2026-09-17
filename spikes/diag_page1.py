"""Lead: diagnose the reported page-1 layout corruption in both panes.

Checks:
  * what page the viewer opens on, and which page images each pane loads
  * whether the source pane for page 1 shows the cover art + correct geometry
  * whether the CN pane page-1 text is positioned correctly (header/cover overlap)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        reqs = []
        pg.on("request", lambda r: reqs.append(r.url) if "pages/" in r.url else None)
        pg.on("console", lambda m: print(f"  [console.{m.type}] {m.text[:200]}"))
        pg.on("pageerror", lambda e: print(f"  [PAGEERROR] {str(e)[:300]}"))

        # open page 1 explicitly
        pg.goto(f"{BASE}/?doc=1&vid=2&page=1&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(5000)

        info = pg.evaluate("""() => {
            const pagesOf = (sel) => [...document.querySelectorAll(sel + ' .page')]
                .slice(0, 3).map(p => ({
                    attr: p.getAttribute('data-page'),
                    style: p.getAttribute('style'),
                    imgs: [...p.querySelectorAll('img')].map(i => ({
                        src: (i.getAttribute('src') || i.getAttribute('data-src') || '').split('/').slice(-4).join('/'),
                        nw: i.naturalWidth, nh: i.naturalHeight,
                        style: i.getAttribute('style'),
                    })),
                    hots: p.querySelectorAll('.hot[data-seg]').length,
                }));
            return {
                url: location.search,
                pageInput: (document.querySelector('#pageInput')||{}).value,
                srcTrees: pagesOf('#pages-source'),
                cnTrees: pagesOf('#pages-cn'),
            };
        }""")
        print(json.dumps(info, ensure_ascii=False, indent=1))
        pg.screenshot(path=os.path.join(OUT, "_diag_page1_viewer.png"))

        # also fetch the page-1 images directly and save for inspection
        import urllib.request
        for kind in ("source", "cn"):
            url = f"{BASE}/api/documents/1/versions/2/pages/1/image?kind={kind}"
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                ext = ".webp" if data[:4] == b"RIFF" else ".png"
                path = os.path.join(OUT, f"_diag_p1_{kind}{ext}")
                open(path, "wb").write(data)
                print(f"  saved {path} ({len(data)} B)")
            except Exception as e:
                print(f"  {kind} image ERR {e}")
    finally:
        b.close()
print("\nrequested page-image URLs:")
for u in reqs[:20]:
    print("  ", u.split("?")[0].split("/api")[-1])
