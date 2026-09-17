"""Lead: capture full-viewer screenshots + verify linked highlighting end to end.

Read-only against the running server, headless, single browser, try/finally close.
"""
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


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu",
                                              "--disable-dev-shm-usage"])
    try:
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)[:300]))
        pg.goto(f"{BASE}/?doc=1&vid=2&page=21&mode=compare", wait_until="load")
        pg.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=60000)
        pg.wait_for_timeout(4000)

        st = pg.evaluate("""() => ({
            docTitle: (document.querySelector('#docTitle')||{}).textContent,
            pageInput: (document.querySelector('#pageInput')||{}).value,
            badges: (document.querySelector('#changeBadges')||{}).textContent,
            srcHots: document.querySelectorAll('#pages-source .hot[data-seg]').length,
            cnHots: document.querySelectorAll('#pages-cn .hot[data-seg]').length,
            toc: document.querySelectorAll('.toc-item').length,
            modalHidden: (document.querySelector('#modal')||{}).hidden,
            errHidden: (document.querySelector('#errorbar')||{}).hidden,
            hscroll: document.documentElement.scrollWidth > window.innerWidth,
        })""")
        print("state:", json.dumps(st, ensure_ascii=False))
        check("无横向滚动", not st["hscroll"], f"scrollWidth>innerWidth={st['hscroll']}")
        check("原文/译文热区都渲染", st["srcHots"] > 20 and st["cnHots"] > 20,
              f"src={st['srcHots']} cn={st['cnHots']}")
        check("术语表弹窗隐藏", st["modalHidden"] is True, f"hidden={st['modalHidden']}")
        check("错误条隐藏", st["errHidden"] is True, f"hidden={st['errHidden']}")
        check("变更徽章非空", "修改" in (st["badges"] or ""), f"badges={st['badges']!r}")
        check("目录已加载", st["toc"] > 50, f"toc={st['toc']}")
        pg.screenshot(path=os.path.join(OUT, "_final_ui_compare.png"))

        # click a source paragraph -> peer must highlight in CN pane
        tgt = pg.evaluate("""() => {
            const h = [...document.querySelectorAll('#pages-source .page .hot[data-seg]')]
              .filter(x => { const r = x.getBoundingClientRect();
                             return r.top > 150 && r.bottom < 800 && r.height > 12; })[2];
            if (!h) return null;
            const r = h.getBoundingClientRect();
            return {seg: h.dataset.seg, x: r.left + r.width/2, y: r.top + r.height/2};
        }""")
        if tgt:
            pg.mouse.click(tgt["x"], tgt["y"])
            pg.wait_for_timeout(1600)
            link = pg.evaluate("""(seg) => {
                const peer = document.querySelector(`#pages-cn .hot[data-seg="${seg}"]`);
                const sel  = document.querySelector(`#pages-source .hot[data-seg="${seg}"]`);
                const pr = peer && peer.getBoundingClientRect();
                const cr = document.querySelector('#scroll-cn').getBoundingClientRect();
                return {
                    seg,
                    selCls: sel ? sel.className : null,
                    peerCls: peer ? peer.className : null,
                    peerInView: pr ? (pr.top < cr.bottom && pr.bottom > cr.top) : false,
                    detailHidden: (document.querySelector('#detail')||{}).hidden,
                    detailCased: (document.querySelector('#detailSrc')||{}).textContent?.slice(0,60),
                    peerClsAll: [...document.querySelectorAll('#pages-cn .peer')].map(e=>e.dataset.seg),
                };
            }""", tgt["seg"])
            print("link:", json.dumps(link, ensure_ascii=False))
            check("点击原文 → 原文段选中(.sel)", "sel" in (link["selCls"] or ""), link["selCls"])
            check("点击原文 → 译文同段高亮(.peer)",
                  "peer" in (link["peerCls"] or "") and link["peerClsAll"] == [tgt["seg"]],
                  f"peer={link['peerCls']} all={link['peerClsAll']}")
            check("译文对应段滚入视口", link["peerInView"], f"inView={link['peerInView']}")
            check("详情面板弹出并显示原文", link["detailHidden"] is False,
                  str(link["detailCased"])[:60])
            pg.screenshot(path=os.path.join(OUT, "_final_ui_linked.png"))

            # reverse direction
            c = pg.evaluate("""(seg) => {
                const h = document.querySelector(`#pages-cn .hot[data-seg="${seg}"]`);
                if (!h) return null;
                const r = h.getBoundingClientRect();
                return {x: r.left + r.width/2, y: r.top + r.height/2};
            }""", tgt["seg"])
            if c:
                pg.mouse.click(c["x"], c["y"])
                pg.wait_for_timeout(1400)
                rev = pg.evaluate("""(seg) => {
                    const s = document.querySelector(`#pages-source .hot[data-seg="${seg}"]`);
                    return {srcCls: s ? s.className : null,
                            srcPeers: [...document.querySelectorAll('#pages-source .peer')].map(e=>e.dataset.seg)};
                }""", tgt["seg"])
                print("reverse:", json.dumps(rev, ensure_ascii=False))
                check("反向：点击译文 → 原文同段高亮",
                      "peer" in (rev["srcCls"] or "") and rev["srcPeers"] == [tgt["seg"]],
                      f"cls={rev['srcCls']} peers={rev['srcPeers']}")

        # scroll sync
        pg.evaluate("document.querySelector('#scroll-cn').scrollTop = 3000")
        pg.wait_for_timeout(1200)
        sy = pg.evaluate("""() => ({
            src: document.querySelector('#scroll-source').scrollTop,
            cn: document.querySelector('#scroll-cn').scrollTop })""")
        check("双栏滚动同步", abs(sy["src"] - sy["cn"]) < 200, f"{sy}")

        # changes-only filter
        pg.check("#changesOnly")
        pg.wait_for_timeout(1800)
        ch = pg.evaluate("""() => ({
            off: document.querySelectorAll('.hot.off').length,
            bars: document.querySelectorAll('.changemark, .bar, .hot.added, .hot.modified, .hot.moved, .hot.removed').length,
            ghosts: document.querySelectorAll('.ghost').length })""")
        print("changesOnly:", ch)
        check("变更过滤生效", ch["off"] > 50, json.dumps(ch))
        pg.screenshot(path=os.path.join(OUT, "_final_ui_changes.png"))

        check("无 JS 页面错误", not errors, str(errors[:2]))
    finally:
        b.close()

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n== Lead 终局 UI 校验 {passed}/{len(results)} 通过 ==")
sys.exit(0 if passed == len(results) else 1)
