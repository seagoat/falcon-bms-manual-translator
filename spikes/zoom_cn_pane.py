"""Lead: high-zoom render of the viewer's CN pane to inspect text quality/overlap."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import json
import urllib.request
from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


# what does the server tell the client to draw?
lay = api("/api/documents/1/versions/2/pages/24/translated-layout")
print(f"page 24 layout: {len(lay['boxes'])} boxes, font={lay.get('font_file')}")
for b in lay["boxes"][:4]:
    print(f"\nbox seg={b['seg_id']} bbox={[round(x,1) for x in b['bbox']]} "
          f"shrink={b.get('shrink')} color={b.get('color')} bg={b.get('bg')}")
    for ln in b["lines"]:
        print(f"   line x={ln['x']:.1f} y={ln['y']:.1f} size={ln['size']:.2f} "
              f"slot={ln['font_slot']} w={ln['width']:.1f} text={ln['text'][:70]!r}")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--force-device-scale-factor=2"])
    try:
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg.goto(f"{BASE}/?doc=1&vid=2&page=24&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-cn .page .hot[data-seg]", timeout=45000)
        pg.wait_for_timeout(4000)
        # crop the CN pane's first page region
        el = pg.query_selector("#scroll-cn")
        box = el.bounding_box()
        print("\n#scroll-cn bbox:", box)
        clip = {"x": box["x"] + 60, "y": box["y"] + 60, "width": 560, "height": 620}
        pg.screenshot(path=os.path.join(OUT, "_zoom_cn_pane.png"), clip=clip)

        # also crop the source pane for a like-for-like look
        els = pg.query_selector("#scroll-source")
        bs = els.bounding_box()
        pg.screenshot(path=os.path.join(OUT, "_zoom_src_pane.png"),
                      clip={"x": bs["x"] + 60, "y": bs["y"] + 60, "width": 560, "height": 620})

        stats = pg.evaluate("""() => {
            const cn = document.querySelectorAll('#scroll-cn .hot[data-seg]');
            const src = document.querySelectorAll('#scroll-source .hot[data-seg]');
            const hots = [...cn].slice(0, 8).map(h => ({
                seg: h.dataset.seg,
                rect: (r => ({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}))(h.getBoundingClientRect()),
                cls: h.className,
                style: h.getAttribute('style'),
            }));
            const imgs = [...document.querySelectorAll('#scroll-cn img')].map(i => ({src: i.src.split('/').slice(-4).join('/'), w: i.naturalWidth, complete: i.complete}));
            return {cnHots: cn.length, srcHots: src.length, hots, imgs: imgs.slice(0, 4)};
        }""")
        print("\nDOM stats:", json.dumps(stats, ensure_ascii=False, indent=1))
    finally:
        b.close()
print("\nsaved _zoom_cn_pane.png / _zoom_src_pane.png")
