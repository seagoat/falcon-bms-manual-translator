"""Lead: instrument gotoPage/syncUrl/onScroll to find who sets the wrong page."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"
TRACE = """() => {
  window.__trace = [];
  const push = (what, detail) => {
    window.__trace.push({t: Math.round(performance.now()), what, detail});
  };
  // 包装 gotoPage / syncUrl 不可行（它们是模块内函数），改为监听关键 DOM 变化
  const inp = document.querySelector('#pageInput');
  new MutationObserver(() => {
    push('pageInput.value', inp.value);
  }).observe(inp, {attributes: true, attributeFilter: ['value'], childList: false, characterData: false});
  inp.addEventListener('change', () => push('pageInput change', inp.value));
  inp.addEventListener('input', () => push('pageInput input', inp.value));
  window.addEventListener('scroll', () => push('window scroll', window.scrollY), true);
  push('armed', inp.value);
}"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        for qs in ("?doc=2&vid=3&page=351&mode=compare",
                   "?doc=3&vid=4&page=300&mode=compare"):
            pg = b.new_page(viewport={"width": 1600, "height": 950})
            logs = []
            pg.on("console", lambda m: logs.append(m.text[:160]))
            pg.add_init_script(TRACE)
            pg.goto(f"{BASE}/{qs}", wait_until="load")
            pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
            pg.wait_for_timeout(8000)
            st = pg.evaluate("""() => ({
                url: location.search,
                pageInput: (document.querySelector('#pageInput')||{}).value,
                trace: (window.__trace || []).slice(0, 40),
            })""")
            print(f"\n=== {qs}")
            print(f"  URL: {st['url']}   pageInput={st['pageInput']}")
            for t in st["trace"]:
                print(f"    t={t['t']:6d}  {t['what']:24s} {t['detail']}")
            pg.close()
    finally:
        b.close()
