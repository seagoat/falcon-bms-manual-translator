"""Lead: screenshot the viewer for each of the 3 merged documents."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
os.makedirs(OUT, exist_ok=True)
results = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        pg.goto(f"{BASE}/?doc=1&vid=2&page=21&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(4000)

        opts = pg.evaluate("""() => {
            const s = document.querySelector('#docSel');
            return [...s.options].map(o => ({v: o.value, t: o.textContent}));
        }""")
        print(f"文档下拉框：{json.dumps(opts, ensure_ascii=False)}")
        results.append(("文档下拉框有 3 项", len(opts) == 3, str(len(opts))))

        for did, name in ((1, "训练手册"), (2, "TO-34"), (3, "TO-1")):
            pg.goto(f"{BASE}/?doc={did}&page=21&mode=compare", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(4500)
            st = pg.evaluate("""() => ({
                docTitle: (document.querySelector('#docTitle')||{}).textContent,
                pageCount: (document.querySelector('#pageCount')||{}).textContent,
                srcHots: document.querySelectorAll('#pages-source .hot[data-seg]').length,
                cnHots: document.querySelectorAll('#pages-cn .hot[data-seg]').length,
                toc: document.querySelectorAll('.toc-item').length,
                modalHidden: (document.querySelector('#modal')||{}).hidden,
            })""")
            print(f"\n[{name}] doc_id={did}")
            print("  " + json.dumps(st, ensure_ascii=False))
            results.append((f"{name} 渲染出热区", st["srcHots"] > 0 and st["cnHots"] > 0,
                            f"src={st['srcHots']} cn={st['cnHots']}"))
            results.append((f"{name} 目录非空", st["toc"] > 0, str(st["toc"])))
            results.append((f"{name} 无遮挡弹窗", st["modalHidden"] is True, str(st["modalHidden"])))
            path = os.path.join(OUT, f"merged_ui_{did}_{name}.png")
            pg.screenshot(path=path)
            print(f"  截图 -> {path}")

        results.append(("无 JS 页面错误", not errs, str(errs[:2])))
    finally:
        b.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n== 合并后 UI 校验 {passed}/{len(results)} 通过 ==")
for name, ok, detail in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
sys.exit(0 if passed == len(results) else 1)
