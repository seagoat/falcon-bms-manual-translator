"""Lead: confirm TOC page numbers render in the Web UI CN pane canvas, and that
book-related regressions are gone."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"
results = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        pg.goto(f"{BASE}/?doc=1&vid=2&page=4&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-cn .page canvas", timeout=60000)
        pg.wait_for_timeout(9000)
        st = pg.evaluate("""() => {
            const cn = [...document.querySelectorAll('#pages-cn .page')]
                .find(p => p.getAttribute('data-page') === '4');
            const cv = cn ? cn.querySelector('canvas') : null;
            let painted = 0, rightBand = 0;
            if (cv) {
                const ctx = cv.getContext('2d');
                const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
                for (let i = 3; i < d.length; i += 4) if (d[i] > 0) painted++;
                // 右侧页码列（x > 0.88 * width）
                const x0 = Math.floor(cv.width * 0.88);
                const sub = ctx.getImageData(x0, 0, cv.width - x0, cv.height).data;
                for (let i = 3; i < sub.length; i += 4) if (sub[i] > 0) rightBand++;
            }
            return {pageInput: (document.querySelector('#pageInput')||{}).value,
                    canvas: cv ? cv.width + 'x' + cv.height : null,
                    painted, rightBand};
        }""")
        print(json.dumps(st, ensure_ascii=False))
        results.append(("CN 页 4 落到正确页", st["pageInput"] == "4", st["pageInput"]))
        results.append(("CN canvas 有内容", st["painted"] > 5000, str(st["painted"])))
        results.append(("右侧页码列有像素（页码可见）", st["rightBand"] > 200, str(st["rightBand"])))
        results.append(("无 JS 错误", not errs, str(errs[:2])))
        pg.screenshot(path=os.path.join("data/derived/preview", "_toc_ui_p4.png"))
        print("截图 -> data/derived/preview/_toc_ui_p4.png")
    finally:
        b.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n== {passed}/{len(results)} 通过 ==")
for n, ok, d in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}  {d}")
sys.exit(0 if passed == len(results) else 1)
