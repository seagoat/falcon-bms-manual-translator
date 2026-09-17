"""Lead: confirm scrolling still updates the page after the deep-link protection releases."""
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
        pg.goto(f"{BASE}/?doc=2&vid=3&page=351&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(8000)
        start = pg.evaluate("() => (document.querySelector('#pageInput')||{}).value")
        print(f"深链初始页码: {start}")
        results.append(("深链落到 351", start == "351", start))

        # 模拟用户真实滚动（滚轮 -> 触发 deepControl 释放）
        pg.mouse.move(700, 500)
        for _ in range(12):
            pg.mouse.wheel(0, 900)
            pg.wait_for_timeout(120)
        pg.wait_for_timeout(1500)
        after = pg.evaluate("() => (document.querySelector('#pageInput')||{}).value")
        print(f"用户滚动后页码: {after}  (应 > 351)")
        results.append(("用户滚动后页码跟随更新", int(after) > 351, after))

        # 翻页输入框仍可用
        pg.evaluate("""() => {
            const i = document.querySelector('#pageInput');
            i.value = '400';
            i.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        pg.wait_for_timeout(2500)
        p400 = pg.evaluate("() => (document.querySelector('#pageInput')||{}).value")
        print(f"手动跳到 400 -> {p400}")
        results.append(("手动跳页生效", p400 == "400", p400))

        # 滚动同步仍然工作
        sync = pg.evaluate("""() => ({
            src: document.querySelector('#scroll-source').scrollTop,
            cn: document.querySelector('#scroll-cn').scrollTop })""")
        print(f"双栏 scrollTop: {sync}")
        results.append(("双栏滚动同步", abs(sync["src"] - sync["cn"]) < 250, json.dumps(sync)))
        results.append(("无 JS 错误", not errs, str(errs[:2])))
        pg.screenshot(path=os.path.join("data/derived/preview", "_deeplink_fixed.png"))
    finally:
        b.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n== {passed}/{len(results)} 通过 ==")
for n, ok, d in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}  {d}")
sys.exit(0 if passed == len(results) else 1)
