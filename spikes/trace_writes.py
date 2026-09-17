"""Lead: hook page-input writes to catch who sets page 360."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8777"

# 在页面脚本执行前安装一个 setter 拦截：任何对 #pageInput.value 的写入都记录调用栈。
HOOK = """
(() => {
  window.__writes = [];
  let pending = null;
  const orig = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  Object.defineProperty(HTMLInputElement.prototype, 'value', {
    get() { return orig.get.call(this); },
    set(v) {
      if (this.id === 'pageInput') {
        window.__writes.push({v: String(v), t: Math.round(performance.now()),
                              stack: (new Error()).stack.split('\\n').slice(1,5).join(' | ')});
      }
      return orig.set.call(this, v);
    },
    configurable: true,
  });
  // 记录 URL 变化
  window.__urls = [];
  const pushState = history.pushState.bind(history);
  history.pushState = function (...a) { window.__urls.push({m:'push', u:String(a[2]), t:Math.round(performance.now())}); return pushState(...a); };
  const replaceState = history.replaceState.bind(history);
  history.replaceState = function (...a) { window.__urls.push({m:'replace', u:String(a[2]), t:Math.round(performance.now())}); return replaceState(...a); };
})();
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        pg.add_init_script(HOOK)
        pg.goto(f"{BASE}/?doc=2&vid=3&page=351", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(8000)
        st = pg.evaluate("""() => ({
            url: location.search,
            pageInput: document.querySelector('#pageInput').value,
            writes: (window.__writes || []).slice(0, 25),
            urls: (window.__urls || []).slice(0, 25),
        })""")
        print("final URL:", st["url"], " pageInput:", st["pageInput"])
        print("\n=== #pageInput.value 写入历史 ===")
        for w in st["writes"]:
            print(f"  t={w['t']:6d} v={w['v']:>5s}")
            print(f"        {w['stack'][:200]}")
        print("\n=== pushState/replaceState ===")
        for u in st["urls"]:
            print(f"  t={u['t']:6d} {u['m']:8s} {u['u']}")
        pg.screenshot(path="data/derived/preview/_trace_deeplink.png")
    finally:
        b.close()
