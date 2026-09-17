"""Lead: why does clicking a source hot zone not highlight the CN peer?"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
logs = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: logs.append((m.type, m.text[:250])))
        pg.on("pageerror", lambda e: logs.append(("PAGEERROR", str(e)[:400])))
        pg.goto(f"{BASE}/?doc=1&vid=2&page=21&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=45000)
        pg.wait_for_timeout(3000)

        pre = pg.evaluate("""() => {
            const hots = [...document.querySelectorAll('#pages-source .page .hot[data-seg]')];
            const inView = hots.filter(h => {
                const r = h.getBoundingClientRect();
                return r.top > 80 && r.bottom < 900 && r.height > 4;
            });
            return {
                total: hots.length, inView: inView.length,
                firstSegs: inView.slice(0,5).map(h => h.dataset.seg),
                classes: inView.slice(0,5).map(h => h.className),
                over: (() => { const h = inView[2]; if(!h) return null;
                    const r = h.getBoundingClientRect();
                    const el = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
                    return {hit: el ? el.tagName + '.' + el.className : null,
                            isHot: el ? el.classList.contains('hot') : null,
                            seg: el ? el.dataset.seg : null}; })(),
            };
        }""")
        print("=== before click ===")
        print(json.dumps(pre, ensure_ascii=False, indent=1))

        # click the 3rd in-view hot zone via real mouse coordinates
        target = pg.evaluate("""() => {
            const hots = [...document.querySelectorAll('#pages-source .page .hot[data-seg]')];
            const h = hots.filter(x => { const r = x.getBoundingClientRect();
                return r.top > 120 && r.bottom < 850 && r.height > 8; })[1];
            if (!h) return null;
            const r = h.getBoundingClientRect();
            return {seg: h.dataset.seg, x: r.left + r.width/2, y: r.top + r.height/2};
        }""")
        print("\ntarget:", target)
        if target:
            pg.mouse.move(target["x"], target["y"])
            pg.wait_for_timeout(400)
            hover = pg.evaluate("""() => ({
                sel: document.querySelectorAll('.sel').length,
                peer: document.querySelectorAll('.peer').length,
                hovered: document.querySelectorAll('.hot:hover').length })""")
            print("after hover:", hover)
            pg.mouse.click(target["x"], target["y"])
            pg.wait_for_timeout(1500)
            after = pg.evaluate("""(seg) => ({
                sel: document.querySelectorAll('.sel').length,
                peer: document.querySelectorAll('.peer').length,
                selSegs: [...document.querySelectorAll('.sel')].map(e => e.dataset.seg),
                peerSegs: [...document.querySelectorAll('.peer')].map(e => e.dataset.seg),
                detailHidden: document.querySelector('#detail') ? document.querySelector('#detail').hidden : null,
                detailSeg: document.querySelector('#detailSegId') ? document.querySelector('#detailSegId').textContent : null,
                askSeg: seg,
            })""", target["seg"])
            print("after click:", json.dumps(after, ensure_ascii=False))

        print("\n=== console/errors ===")
        for t, m in logs[-25:]:
            print(f"  [{t}] {m}")
    finally:
        b.close()
