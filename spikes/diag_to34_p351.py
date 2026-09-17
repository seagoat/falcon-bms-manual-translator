"""Lead: why is the CN pane blank for TO-34 p351 in the viewer?"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"
OUT = "data/derived/preview"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.load(r)


print("=== 服务端数据（TO-34 = doc2 / version 3, page 351）===")
st = api("/api/documents/2/versions/3/pages/351/translated-layout")
print(f"translated-layout: {len(st.get('boxes', []))} boxes, font={st.get('font_file')}")
if st.get("boxes"):
    b = st["boxes"][0]
    print(f"  首个 box seg={b['seg_id']} lines={len(b.get('lines', []))} "
          f"text={b.get('lines', [{}])[0].get('text', '')[:50]!r}")
segs = api("/api/documents/2/versions/3/segments?page=351")
print(f"segments: {len(segs)} 其中带 translation 的 "
      f"{sum(1 for s in segs if s.get('translation'))}")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        pg.goto(f"{BASE}/?doc=2&vid=3&page=351&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(7000)
        st2 = pg.evaluate("""() => {
            const pages = [...document.querySelectorAll('#pages-cn .page')]
                .filter(p => { const r = p.getBoundingClientRect();
                               return r.bottom > 80 && r.top < window.innerHeight; })
                .map(p => {
                    const img = p.querySelector('img');
                    const cv = p.querySelector('canvas');
                    let painted = null;
                    if (cv) {
                        const ctx = cv.getContext('2d');
                        const d = ctx.getImageData(0, 0, Math.min(cv.width, 400), Math.min(cv.height, 400)).data;
                        let nz = 0;
                        for (let i = 3; i < d.length; i += 4) if (d[i] > 0) nz++;
                        painted = nz;
                    }
                    return {
                        page: p.getAttribute('data-page'),
                        imgSrc: img ? (img.getAttribute('src')||'') .split('/pages/').pop() : null,
                        imgLoaded: img ? img.naturalWidth : -1,
                        canvasW: cv ? cv.width : -1,
                        canvasH: cv ? cv.height : -1,
                        canvasNonTransparent: painted,
                    };
                });
            return {pages, srcHots: document.querySelectorAll('#pages-source .hot[data-seg]').length,
                    cnHots: document.querySelectorAll('#pages-cn .hot[data-seg]').length};
        }""")
        print("\n=== 浏览器状态 ===")
        print(json.dumps(st2, ensure_ascii=False, indent=1))
        print("errors:", errs[:3])
        pg.screenshot(path=os.path.join(OUT, "_diag_to34_p351.png"))
        print(f"screenshot -> {os.path.join(OUT, '_diag_to34_p351.png')}")
    finally:
        b.close()
