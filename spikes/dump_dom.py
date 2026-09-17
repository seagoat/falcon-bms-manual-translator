"""Lead: dump the real viewer DOM structure so I stop guessing selectors."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
URL = sys.argv[2] if len(sys.argv) > 2 else f"{BASE}/"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        logs = []
        pg.on("console", lambda m: logs.append((m.type, m.text[:250])))
        pg.on("pageerror", lambda e: logs.append(("PAGEERROR", str(e)[:400])))
        pg.goto(URL, wait_until="load")
        pg.wait_for_timeout(12000)

        out = pg.evaluate("""() => {
          const summarize = (el, depth) => {
            if (depth > 3 || !el) return null;
            return {
              tag: el.tagName.toLowerCase(),
              id: el.id || null,
              cls: el.className && typeof el.className === 'string' ? el.className.slice(0,80) : null,
              childCount: el.children.length,
              children: [...el.children].slice(0, 8).map(c => summarize(c, depth + 1)),
            };
          };
          const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
          const counts = {};
          for (const sel of ['.page','.hot','img','canvas','.pane','.viewer','.toc-item',
                             '[data-seg]','.pane-body','.scroll','.mock']) {
            counts[sel] = document.querySelectorAll(sel).length;
          }
          const bodyHtml = document.body.innerHTML;
          return {
            ids: ids.slice(0, 60),
            counts,
            tree: summarize(document.body, 0),
            bodyLen: bodyHtml.length,
            bodyHead: bodyHtml.slice(0, 1200),
          };
        }""")
        print("=== ids ===")
        print(out["ids"])
        print("\n=== selector counts ===")
        print(json.dumps(out["counts"], ensure_ascii=False, indent=1))
        print(f"\n=== body length {out['bodyLen']} ===")
        print(out["bodyHead"])
        print("\n=== console / errors ===")
        for t, m in logs[:30]:
            print(f"  [{t}] {m}")
    finally:
        b.close()
