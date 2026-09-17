"""Lead: find why ?page=351 lands on page 360."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"

CASES = [
    ("doc1", "?doc=1&vid=2&page=21&mode=compare", "21"),
    ("doc1-99", "?doc=1&vid=2&page=99&mode=compare", "99"),
    ("doc2-21", "?doc=2&vid=3&page=21&mode=compare", "21"),
    ("doc2-351", "?doc=2&vid=3&page=351&mode=compare", "351"),
    ("doc2-351-novid", "?doc=2&page=351&mode=compare", "351"),
    ("doc3-21", "?doc=3&vid=4&page=21&mode=compare", "21"),
    ("doc3-300", "?doc=3&vid=4&page=300&mode=compare", "300"),
    ("doc3-300-novid", "?doc=3&page=300&mode=compare", "300"),
]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        for name, qs, want in CASES:
            pg.goto(f"{BASE}/{qs}", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(6000)
            got = pg.evaluate("""() => ({
                pageInput: (document.querySelector('#pageInput')||{}).value,
                url: location.search,
                firstSrcPage: (document.querySelector('#pages-source .page')||{})
                    .getAttribute ? document.querySelector('#pages-source .page').getAttribute('data-page') : null,
            })""")
            ok = got["pageInput"] == want
            print(f"  {'OK ' if ok else 'FAIL'} {name:16s} want={want:4s} got={got['pageInput']:4s} "
                  f"firstSrcPage={got['firstSrcPage']}  url={got['url']}")
    finally:
        b.close()
