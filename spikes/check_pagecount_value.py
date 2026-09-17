"""Lead: confirm S.pageCount is wrong/stale at deep-link time."""
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
        for qs in ("?doc=2&vid=3&page=351", "?doc=3&vid=4&page=300"):
            pg = b.new_page(viewport={"width": 1600, "height": 950})
            pg.goto(f"{BASE}/{qs}", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(6000)
            st = pg.evaluate("""() => {
                const pc = document.querySelector('#pageCount').textContent;
                const pages = document.querySelectorAll('#pages-source .page').length;
                const last = (() => { const a=[...document.querySelectorAll('#pages-source .page')];
                                      return a.length ? a[a.length-1].getAttribute('data-page') : null; })();
                return {pageCountText: pc, pageEls: pages, lastPage: last,
                        url: location.search,
                        pageInput: document.querySelector('#pageInput').value};
            }""")
            print(f"{qs}: {json.dumps(st, ensure_ascii=False)}")
            pg.close()
    finally:
        b.close()
