"""Lead: crop and magnify the CN pane's page-number column as the BROWSER actually
renders it (canvas), to see whether the numbers are truly drawn."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402
from PIL import Image  # noqa: E402

BASE = "http://127.0.0.1:8777"
OUT = "data/derived/preview"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(f"{BASE}/?doc=1&vid=2&page=4&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-cn .page canvas", timeout=60000)
        pg.wait_for_timeout(9000)
        # 取两栏里 p4 页面元素的位置，分别截图
        for side in ("source", "cn"):
            el = pg.query_selector(f"#pages-{side} .page[data-page='4']")
            if el is None:
                print(f"{side}: 找不到 p4 元素")
                continue
            box = el.bounding_box()
            print(f"{side} p4 box: {box}")
            el.screenshot(path=os.path.join(OUT, f"_pane_{side}_p4.png"))
        # 右侧页码列放大
        el = pg.query_selector("#pages-cn .page[data-page='4']")
        box = el.bounding_box()
        if box:
            clip = {"x": box["x"] + box["width"] * 0.80, "y": box["y"],
                    "width": box["width"] * 0.20, "height": min(box["height"], 900)}
            pg.screenshot(path=os.path.join(OUT, "_cn_pagenum_zoom.png"), clip=clip)
            print(f"页码列裁切: {clip}")
    finally:
        b.close()

for name in ("_pane_cn_p4.png", "_cn_pagenum_zoom.png"):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        continue
    im = Image.open(path)
    print(f"{name}: {im.size}")
    if "zoom" in name:
        im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
        im.save(os.path.join(OUT, "_cn_pagenum_zoom_3x.png"))
        print(f"  -> _cn_pagenum_zoom_3x.png {im.size}")
