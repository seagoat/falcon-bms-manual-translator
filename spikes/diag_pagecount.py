"""Lead: inspect how pageCount/startPage are derived for each doc on deep link."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        for qs in ("?doc=2&vid=3&page=351&mode=compare",
                   "?doc=3&vid=4&page=300&mode=compare",
                   "?doc=1&vid=2&page=99&mode=compare"):
            pg.goto(f"{BASE}/{qs}", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(5000)
            st = pg.evaluate("""() => {
                const verSel = document.querySelector('#verSel');
                return {
                    url: location.search,
                    pageInput: (document.querySelector('#pageInput')||{}).value,
                    pageCountText: (document.querySelector('#pageCount')||{}).textContent,
                    pageInputMax: (document.querySelector('#pageInput')||{}).max,
                    verOptions: verSel ? [...verSel.options].map(o => ({v:o.value,t:o.textContent})) : null,
                    verValue: verSel ? verSel.value : null,
                    docSelValue: (document.querySelector('#docSel')||{}).value,
                    pageEls: document.querySelectorAll('#pages-source .page').length,
                    lastPageAttr: (() => { const a=[...document.querySelectorAll('#pages-source .page')];
                                           return a.length? a[a.length-1].getAttribute('data-page'):null; })(),
                };
            }""")
            print(json.dumps(st, ensure_ascii=False, indent=1))
            print("---")
    finally:
        b.close()
