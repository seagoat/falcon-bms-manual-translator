"""tests/ui_smoke.py — 对照浏览器 UI 冒烟测试（双栏联动高亮取证）

用法：
    python tests/ui_smoke.py <base_url> <out_dir>

`<base_url>` 可带查询串：
    http://127.0.0.1:8901/?mock=1        离线桩数据
    http://127.0.0.1:8901/?doc=1&vid=2   真实数据

两条执行路径
------------
A. **Playwright（首选）**：真实 Chromium，1440x900 截图 + DOM 断言。
   点击左栏段 → 读右栏同 seg 元素的 classList 与 getBoundingClientRect()，
   断言带 `.peer` 且 rect 落在右栏滚动视口内；再反向验证一次。
B. **Node DOM harness（降级，本沙箱必需）**：本机沙箱禁止 Chrome 启动
   （Mojo `platform_channel` 命名管道被拒 → `拒绝访问(0x5)`，playwright 同样
   `PermissionError: WinError 5`），因此用 `web/static/_devtest/ui_harness.mjs`
   在 mini-DOM 中**原样执行 app.js / render.js / api.js**，做与 A 等价的断言，
   并把**真实 DOM 几何 + canvas 绘制调用**导出为 JSON；本脚本据此合成
   `data/derived/preview/ui_*.png`（标注为「DOM 快照合成图」，非浏览器截图）。

退出码 0/1。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
CHECKS: list[str] = []
MODE = "?"


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append(name)
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {str(detail)[:300]}")
        FAILS.append(name)
    return bool(cond)


# ==========================================================================
# A. Playwright 路径（有可用浏览器时）
# ==========================================================================
def _pipes_allowed() -> bool:
    """沙箱会拒绝命名管道/管道 stdio；playwright 启动浏览器必须用到，先探测一次。"""
    try:
        import multiprocessing
        a, b = multiprocessing.Pipe()
        a.close()
        b.close()
        return True
    except Exception:
        try:
            r, w = os.pipe()
            os.close(r)
            os.close(w)
            return True
        except Exception:
            return False


HEADLESS_ARGS = [
    "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars",
    "--disable-extensions", "--disable-background-networking", "--force-device-scale-factor=1",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
]


def _chrome_pids() -> set:  # noqa: D401  (仅用于**诊断打印**，禁止据此杀进程)
    """当前 chrome/headless_shell 进程 PID 集合。

    ⚠️ 安全约束：本函数只允许用于**打印/观测**。
    绝不允许用它的差集去 `taskkill` —— 那会误杀用户在测试期间打开的 Chrome/Edge，
    已经真实发生过（一次运行抹掉用户 10~16 个 Chrome 进程，浏览器整个退出）。
    清理浏览器请只依赖 Playwright 的 `browser.close()` / `pw.stop()`。
    """
    import tempfile
    out = Path(tempfile.gettempdir()) / "mt_ui_smoke_tasklist.txt"
    pids: set = set()
    try:
        with open(out, "w", encoding="utf-8", errors="replace") as fh:
            for exe in ("chrome.exe", "chrome-headless-shell.exe"):
                subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                               stdout=fh, stderr=subprocess.DEVNULL, timeout=30)
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) >= 2 and cells[1].isdigit():
                pids.add(int(cells[1]))
    except Exception as exc:
        print(f"  (进程列表不可用: {exc})")
    return pids


def run_playwright(url: str, out: Path, is_mock: bool):
    """返回 True/False 表示断言结果；返回 None 表示浏览器不可用。

    约束（Lead 2025 事故后新增）：
      * **只允许 headless**（headed Chromium 会耗尽 desktop heap → 0xc0000142 弹窗）；
      * 一次运行只起 1 个 browser / 1 个 page；
      * `try/finally` 必须回收 page/browser/driver；
      * 结束时检查是否有本进程残留的 chrome，残留则强杀并报错。
    """
    if not _pipes_allowed():
        print("  SKIP  当前沙箱禁止管道/命名管道 → 无法启动 Chromium（改用降级路径）")
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print(f"  SKIP  playwright 不可用: {exc}")
        return None

    def nonbg(png: Path, bg=(15, 17, 21)) -> float:
        from PIL import Image
        im = Image.open(png).convert("RGB")
        im.thumbnail((480, 480))
        px = list(im.getdata())
        same = sum(1 for c in px if abs(c[0] - bg[0]) <= 6 and abs(c[1] - bg[1]) <= 6 and abs(c[2] - bg[2]) <= 6)
        return 1.0 - same / max(1, len(px))

    pw = None
    browser = None
    page = None
    try:
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True, args=HEADLESS_ARGS)   # 只允许 headless
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      device_scale_factor=1, locale="zh-CN")
            page = ctx.new_page()                                             # 单 page
    except Exception as exc:
        reason = str(exc).splitlines()[0][:150]
        print(f"  SKIP  浏览器无法启动（沙箱限制）: {type(exc).__name__}: {reason}")
        for obj, meth in ((browser, "close"), (pw, "stop")):
            try:
                getattr(obj, meth)() if obj else None
            except Exception:
                pass
        return None

    try:
        errs, pageerrs = [], []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: pageerrs.append(str(e)))
        page.goto(url, wait_until="load")
        page.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=30000)
        page.wait_for_selector("#pages-cn .page .hot[data-seg]", timeout=30000)
        page.wait_for_timeout(1500)

        s1 = out / "ui_01_initial.png"
        page.screenshot(path=str(s1))
        info = page.evaluate("""() => ({docW: document.documentElement.scrollWidth, winW: window.innerWidth,
            src: document.querySelectorAll('#pages-source .hot[data-seg]').length,
            cn: document.querySelectorAll('#pages-cn .hot[data-seg]').length,
            err: !document.getElementById('errorbar').hidden})""")
        check("无横向滚动", info["docW"] <= info["winW"] + 1, json.dumps(info))
        check("双栏热区已渲染", info["src"] > 0 and info["cn"] > 0, json.dumps(info))
        check("无错误条", info["err"] is False, "")
        check("初始双栏截图非空白", nonbg(s1) > 0.6, f"{nonbg(s1):.2f}")

        t = page.evaluate("""() => { const h=[...document.querySelectorAll('#pages-source .hot[data-seg]')]
            .filter(x=>x.dataset.role==='body'); const e=h[h.length-1]||h[0];
            return e?{seg:e.dataset.seg, r:e.getBoundingClientRect().toJSON()}:null; }""")
        check("左栏有可点击 body 段", t is not None, "")
        seg = t["seg"]
        page.mouse.click(t["r"]["x"] + t["r"]["width"] / 2, t["r"]["y"] + t["r"]["height"] / 2)
        page.wait_for_timeout(1000)
        r1 = page.evaluate("""(seg) => {
            const cn=[...document.querySelectorAll(`#pages-cn .hot[data-seg="${seg}"]`)];
            const sc=document.getElementById('scroll-cn').getBoundingClientRect();
            const b=cn[0]?cn[0].getBoundingClientRect():null;
            return {cls:cn[0]?cn[0].className:null, sel:document.querySelectorAll('.hot.sel').length,
                    peer:document.querySelectorAll('.hot.peer').length,
                    inView: b?(b.top>=sc.top-2 && b.bottom<=sc.bottom+2):false, rect:b?{top:b.top,bottom:b.bottom}:null,
                    scroller:{top:sc.top,bottom:sc.bottom}};}""", seg)
        check("点击左栏 → 右栏同 seg 带 .peer", "peer" in (r1["cls"] or ""), json.dumps(r1))
        check("点击左栏 → 右栏对应段在视口内", r1["inView"] is True, json.dumps(r1))
        check("sel/peer 各一个", r1["sel"] == 1 and r1["peer"] == 1, json.dumps(r1))
        s2 = out / "ui_02_click_source_highlight.png"
        page.screenshot(path=str(s2))
        check("联动高亮截图非空白", nonbg(s2) > 0.6, f"{nonbg(s2):.2f}")

        c = page.evaluate("""() => { const h=[...document.querySelectorAll('#pages-cn .hot[data-seg]:not(.pending)')]
            .filter(x=>x.dataset.role==='body'); const e=h[0];
            return e?{seg:e.dataset.seg, r:e.getBoundingClientRect().toJSON()}:null; }""")
        if check("右栏有可点击 body 段", c is not None, ""):
            page.mouse.click(c["r"]["x"] + c["r"]["width"] / 2, c["r"]["y"] + c["r"]["height"] / 2)
            page.wait_for_timeout(1000)
            r2 = page.evaluate("""(seg) => { const s=[...document.querySelectorAll(`#pages-source .hot[data-seg="${seg}"]`)];
                return {srcCls:s[0]?s[0].className:null}; }""", c["seg"])
            check("反向：点击右栏 → 左栏同 seg 带 .peer", "peer" in (r2["srcCls"] or ""), json.dumps(r2))
        s3 = out / "ui_03_click_cn_reverse.png"
        page.screenshot(path=str(s3))

        page.evaluate("""() => { const sc=document.getElementById('scroll-source');
            const pages=[...sc.querySelectorAll('.page')]; sc.scrollTop = pages[pages.length-1].offsetTop-10; }""")
        page.wait_for_timeout(1400)
        sy = page.evaluate("""() => { const a=(id)=>{const sc=document.getElementById(id);
            const probe=sc.scrollTop+Math.min(160,sc.clientHeight*0.22);
            for (const p of sc.querySelectorAll('.page')) { const t=p.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
              if (probe>=t && probe<t+p.offsetHeight) return +p.dataset.page; } return null; };
            const s=document.getElementById('scroll-source'), c=document.getElementById('scroll-cn');
            return {src:a('scroll-source'), cn:a('scroll-cn'), st:s.scrollTop, ct:c.scrollTop}; }""")
        check("滚动同步：两栏锚定同一页", sy["src"] == sy["cn"] and sy["src"] is not None, json.dumps(sy))
        check("滚动同步：scrollTop 接近", abs(sy["st"] - sy["ct"]) < 30, json.dumps(sy))
        s4 = out / "ui_04_scroll_sync.png"
        page.screenshot(path=str(s4))

        page.check("#changesOnly")
        page.wait_for_timeout(900)
        f = page.evaluate("""() => ({off: document.querySelectorAll('.hot.off').length,
            ghosts: document.querySelectorAll('.hot.ghost').length, bars: document.querySelectorAll('.hot .bar').length,
            badges: (document.getElementById('changeBadges')||{}).textContent||''})""")
        check("变更过滤：出现 .hot.off", f["off"] > 0, json.dumps(f, ensure_ascii=False))
        check("变更过滤：统计徽章非空", f["badges"].strip() != "", json.dumps(f, ensure_ascii=False))
        if is_mock:
            check("已删除段幽灵块存在", f["ghosts"] > 0, json.dumps(f, ensure_ascii=False))
        s5 = out / "ui_05_changes_only.png"
        page.screenshot(path=str(s5))
        check("变更过滤截图非空白", nonbg(s5) > 0.6, f"{nonbg(s5):.2f}")

        page.goto(deeplink_url(url, seg), wait_until="load")
        page.wait_for_selector("#pages-source .page .hot[data-seg]", timeout=30000)
        try:
            page.wait_for_selector(".hot.sel, .hot.peer", timeout=15000)
        except Exception:
            pass
        d = page.evaluate("""(seg) => ({checked: document.getElementById('changesOnly').checked,
            selected: (document.querySelector('.hot.sel')||{dataset:{}}).dataset.seg, page: document.getElementById('pageInput').value})""", seg)
        check("深链：changes=1 恢复", d["checked"] is True, json.dumps(d, ensure_ascii=False))
        check("深链：seg 选中恢复", int(d["selected"] or 0) == int(seg), json.dumps(d, ensure_ascii=False))
        check("深链：page 恢复", d["page"] == "3", json.dumps(d, ensure_ascii=False))
        s6 = out / "ui_06_deeplink_restore.png"
        page.screenshot(path=str(s6))

        check("无 JS pageerror", len(pageerrs) == 0, "; ".join(pageerrs[:2]))
        check("无 console.error", len([e for e in errs if "favicon" not in e]) == 0, "; ".join(errs[:2]))
        return True
    finally:
        # 必须回收：page → context/browser → driver；否则会残留 headed 进程耗尽 desktop heap
        for obj, meth in ((page, "close"), (browser, "close"), (pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        time.sleep(0.6)
        # ⚠️ 绝对不要用「全系统 chrome PID 差集 + taskkill /F」来清理！
        # 那会误杀用户在测试期间自己打开的 Chrome / Edge 标签页
        # （已实际发生过：一次运行抹掉用户 10~16 个 Chrome 进程，浏览器整个退出）。
        # Playwright 的 browser.close() 会自行结束它启动的浏览器进程树；
        # 这里只做「自己的 browser 是否已关闭」的自检，绝不触碰任何其它进程。
        still_open = None
        try:
            still_open = browser.is_connected()
        except Exception:
            still_open = None
        check("playwright 已释放自建浏览器（未触碰用户浏览器）",
              still_open in (False, None),
              f"is_connected={still_open}（不执行任何 taskkill）")


def deeplink_url(url: str, seg) -> str:
    """构造深链 URL：覆盖 page/seg/changes（避免出现重复的 page 参数）。"""
    pr = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(pr.query)
    qs["page"] = ["3"]
    qs["seg"] = [str(seg)]
    qs["changes"] = ["1"]
    flat = {k: v[0] for k, v in qs.items()}
    return urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode(flat)))


def pagejump_url(url: str, page: int) -> str:
    """只改 page 参数：验证深链页码定位。"""
    pr = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(pr.query)
    qs["page"] = [str(page)]
    qs.pop("seg", None)
    qs.pop("changes", None)
    flat = {k: v[0] for k, v in qs.items()}
    return urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode(flat)))


# ==========================================================================
# B. Node harness 路径（降级）
# ==========================================================================
def pick_deeplink_seg(url: str, is_mock: bool, page: int = 3):
    """真实模式下从 API 取第 page 页的第一个 seg_id，保证深链指向真实存在的段。"""
    if is_mock:
        return 302
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    doc = (q.get("doc") or ["1"])[0]
    vid = (q.get("vid") or [""])[0]
    base = url.split("?")[0].rstrip("/")
    try:
        if not vid:
            with urllib.request.urlopen(f"{base}/api/documents/{doc}/versions", timeout=20) as r:
                vs = json.loads(r.read().decode())
            pick = [v for v in vs if v.get("is_current")] or vs
            vid = str(pick[-1]["version_id"])
        with urllib.request.urlopen(f"{base}/api/documents/{doc}/versions/{vid}/segments?page={page}",
                                    timeout=30) as r:
            segs = json.loads(r.read().decode())
        for s in segs:
            if s.get("role") == "body":
                return s["seg_id"]
        return segs[0]["seg_id"] if segs else 302
    except Exception as exc:
        print(f"  ! 取深链 seg 失败: {exc}")
        return 302


def run_harness(url: str, out: Path) -> dict:
    node = os.environ.get("MT_NODE") or "node"
    harness = ROOT / "web" / "static" / "_devtest" / "ui_harness.mjs"
    snap_json = out / "ui_snapshot.json"
    print("  → 降级：node DOM harness（沙箱禁止启动 Chromium / playwright）")
    is_mock = "mock=1" in url
    seg = pick_deeplink_seg(url, is_mock, 3)
    print(f"    深链目标：page=3 seg={seg}")
    # 深链 URL：**覆盖** page/seg/changes（不能用字符串拼接，会出现两个 page 参数）
    pr = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(pr.query)
    qs["page"] = ["3"]
    qs["seg"] = [str(seg)]
    qs["changes"] = ["1"]
    durl = urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode({k: v[0] for k, v in qs.items()})))
    scenarios = [("main", url, snap_json), ("deeplink", durl, out / "ui_snapshot_deeplink.json"),
                 ("pagejump", pagejump_url(url, 24), out / "ui_snapshot_pagejump.json")]
    codes = []
    for scen, u, jf in scenarios:
        log = out / f"ui_harness_{scen}.log"
        print(f"    node ui_harness.mjs \"{u}\" {scen}")
        # 注意：沙箱禁止管道 stdio，必须重定向到文件而不是 PIPE
        with open(log, "w", encoding="utf-8") as fh:
            proc = subprocess.run([node, str(harness), u, scen, str(jf)],
                                  cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=300)
        codes.append(proc.returncode)
        print(log.read_text(encoding="utf-8", errors="replace"))
    return {"exit": 0 if all(c == 0 for c in codes) else 1,
            "snapshot": str(snap_json), "snapshot_deeplink": str(out / "ui_snapshot_deeplink.json")}


# ==========================================================================
# C. DOM 快照 → PNG 合成（沙箱内唯一可用的可视化取证）
# ==========================================================================
_PAGE_CACHE: dict[str, object] = {}


def fetch_image(base_url: str, src: str):
    from PIL import Image
    full = urllib.parse.urljoin(base_url, src)
    if full in _PAGE_CACHE:
        return _PAGE_CACHE[full]
    try:
        with urllib.request.urlopen(full, timeout=25) as r:
            im = Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception as exc:
        print(f"      ! 页面图下载失败 {full}: {exc}")
        im = Image.new("RGB", (600, 800), (250, 250, 252))
    _PAGE_CACHE[full] = im
    return im


_FONTS: dict[tuple, object] = {}


def font_for(size: int, text: str):
    from PIL import ImageFont
    cjk = any("\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" for ch in str(text))
    path = ("C:/Windows/Fonts/Deng.ttf" if cjk else "C:/Windows/Fonts/calibri.ttf")
    if not os.path.exists(path):
        path = "C:/Windows/Fonts/simhei.ttf" if cjk else "C:/Windows/Fonts/arial.ttf"
    key = (path, size)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def render_snapshot(snap: dict, base_url: str, out_png: Path, caption: str) -> Path:
    from PIL import Image, ImageDraw
    W, H = snap["viewport"]["w"], snap["viewport"]["h"]
    im = Image.new("RGB", (W, H), (15, 17, 21))
    d = ImageDraw.Draw(im, "RGBA")
    txt = lambda xy, s, size=12, fill=(230, 233, 239), anchor="la": d.text(xy, str(s), font=font_for(size, s), fill=fill, anchor=anchor)
    clip = lambda box: (max(0, int(box[0])), max(0, int(box[1])), min(W, int(box[2])), min(H, int(box[3])))

    top = snap["topbar"]
    d.rectangle([top["x"], top["y"], top["x"] + top["w"], top["y"] + top["h"]], fill=(23, 28, 38))
    d.line([top["x"], top["y"] + top["h"], top["x"] + top["w"], top["y"] + top["h"]], fill=(38, 46, 64))
    d.rounded_rectangle([10, 14, 34, 38], 6, fill=(60, 90, 220))
    txt((22, 26), "MT", 11, (255, 255, 255), "mm")
    txt((42, 26), snap["chrome"]["docTitle"][:46], 13, (235, 238, 245), "lm")
    x = 320
    for label, val in (("文档", "bms-training-manual"), ("版本", "v2 · 4.38.1")):
        txt((x, 26), f"{label}", 11, (125, 135, 156), "lm")
        txt((x + 34, 26), val[:22], 11, (210, 216, 228), "lm")
        x += 190
    # 模式按钮
    for i, (label, on) in enumerate((("对照", True), ("仅原文", False), ("仅译文", False))):
        bx = x + i * 62
        d.rounded_rectangle([bx, 16, bx + 56, 36], 5, fill=(29, 78, 216) if on else (27, 33, 48),
                            outline=(51, 61, 82))
        txt((bx + 28, 26), label, 11, (255, 255, 255) if on else (184, 192, 208), "mm")
    # 变更统计徽章 + 「只显示变更」开关
    bx = x + 210
    chk_on = snap["chrome"]["bodyClass"] and "changes-only" in snap["chrome"]["bodyClass"]
    d.rectangle([bx, 19, bx + 11, 30], fill=(29, 78, 216) if chk_on else (27, 33, 48), outline=(90, 100, 122))
    if chk_on:
        txt((bx + 5.5, 24), "✓", 9, (255, 255, 255), "mm")
    txt((bx + 17, 26), "只显示变更", 11, (200, 206, 220), "lm")
    bx += 100
    for k, zh, col in (("added", "新增", (34, 197, 94)), ("modified", "修改", (245, 158, 11)),
                       ("moved", "移动", (168, 85, 247)), ("removed", "删除", (239, 68, 68))):
        n = re.search(rf"{zh} (\d+)", snap["chrome"]["badges"])
        if not n:
            continue
        label = f"{zh} {n.group(1)}"
        wpx = 30 + 8 * len(label)
        d.rounded_rectangle([bx, 17, bx + wpx, 35], 9, fill=(27, 33, 48), outline=(51, 61, 82))
        d.rounded_rectangle([bx + 7, 23, bx + 14, 30], 2, fill=col)
        txt((bx + 18, 26), label, 10, (184, 192, 208), "lm")
        bx += wpx + 6
    # 右侧控件
    rx = W - 20
    for label, wpx in (("术语表", 56), ("导出双语", 68), ("导出中文", 68), ("翻译全书", 68), ("翻译本页", 68)):
        d.rounded_rectangle([rx - wpx, 16, rx, 36], 5, fill=(29, 78, 216) if label == "翻译本页" else (27, 33, 48),
                            outline=(51, 61, 82))
        txt((rx - wpx / 2, 26), label, 10, (255, 255, 255) if label == "翻译本页" else (230, 233, 239), "mm")
        rx -= wpx + 6
    d.rounded_rectangle([rx - 96, 16, rx, 36], 5, fill=(27, 33, 48), outline=(51, 61, 82))
    txt((rx - 88, 26), f"100%", 10, (200, 206, 220), "lm")
    rx -= 102
    d.rounded_rectangle([rx - 92, 16, rx, 36], 5, fill=(27, 33, 48), outline=(51, 61, 82))
    txt((rx - 86, 26), f"页 {snap['chrome']['page']} {snap['chrome']['pageCount']}", 10, (200, 206, 220), "lm")

    if snap["errorbar"]["visible"]:
        d.rectangle([0, 0, W, 30], fill=(127, 29, 29))
        txt((12, 15), snap["errorbar"]["text"][:120], 12, (254, 226, 226), "lm")

    # 目录
    toc = snap["toc"]
    d.rectangle([toc["x"], toc["y"], toc["x"] + toc["w"], toc["y"] + toc["h"]], fill=(21, 25, 34))
    d.line([toc["x"] + toc["w"], toc["y"], toc["x"] + toc["w"], toc["y"] + toc["h"]], fill=(38, 46, 64))
    txt((toc["x"] + 10, toc["y"] + 18), "目录", 12, (230, 233, 239), "lm")
    ty = toc["y"] + 42
    for item in snap["chrome"]["toc"]:
        if item["active"]:
            d.rectangle([toc["x"], ty, toc["x"] + toc["w"], ty + 22], fill=(23, 35, 60))
        txt((toc["x"] + 22, ty + 11), item["text"][:26], 11,
            (207, 224, 255) if item["active"] else (184, 192, 208), "lm")
        ty += 24

    # 双栏
    for side, title, dot in (("source", "原文  EN", (56, 189, 248)), ("cn", "译文  中文", (52, 211, 153))):
        pane = snap["panes"][side]
        head, scr = pane["head"], pane["scroller"]
        if pane["box"]["w"] <= 1:
            continue
        d.rectangle([head["x"], head["y"], head["x"] + head["w"], head["y"] + head["h"]], fill=(21, 25, 34))
        d.ellipse([head["x"] + 10, head["y"] + 10, head["x"] + 18, head["y"] + 18], fill=dot)
        txt((head["x"] + 24, head["y"] + 14), title, 11, (205, 212, 226), "lm")
        txt((head["x"] + head["w"] - 10, head["y"] + 14),
            snap["chrome"]["paneHeads"][0 if side == "source" else 1][-28:], 10, (125, 135, 156), "rm")
        d.rectangle([scr["x"], scr["y"], scr["x"] + scr["w"], scr["y"] + scr["h"]], fill=(16, 19, 26))
        # 页面 + canvas 文本 + 热区（先画到 region，再整块贴回，天然裁剪到滚动视口）
        region = Image.new("RGB", (max(1, int(scr["w"])), max(1, int(scr["h"]))), (16, 19, 26))
        rg = ImageDraw.Draw(region, "RGBA")
        ox, oy = scr["x"], scr["y"]
        for pg in snap["pages"]:
            if pg["side"] != side:
                continue
            if pg["y"] + pg["h"] < scr["y"] - 2 or pg["y"] > scr["y"] + scr["h"] + 2:
                continue
            if pg["img"]:
                img = fetch_image(base_url, pg["img"])
                rg.rectangle([pg["x"] - ox - 1, pg["y"] - oy - 1,
                              pg["x"] - ox + pg["w"], pg["y"] - oy + pg["h"]], fill=(70, 78, 96))
                region.paste(img.resize((max(1, int(pg["w"])), max(1, int(pg["h"]))), Image.LANCZOS),
                             (int(pg["x"] - ox), int(pg["y"] - oy)))
        for c in snap["canvasText"]:
            if c["side"] != side:
                continue
            pb = c.get("pageBox") or {"x": 0, "y": 0}
            px_, py_ = pb["x"] + c["x"] - ox, pb["y"] + c["y"] - oy
            if py_ < -20 or py_ > region.height + 20:
                continue
            size = max(6, int(round(float(parse_float_font(c["font"])))))
            rg.text((px_, py_), c["text"], font=font_for(size, c["text"]), fill=(26, 26, 26), anchor="ls")
        for h in snap["hots"]:
            if h["side"] != side:
                continue
            cls = h["cls"]
            if "off" in cls:
                continue
            box = [h["x"] - ox, h["y"] - oy, h["x"] - ox + h["w"], h["y"] - oy + h["h"]]
            if box[3] < -10 or box[1] > region.height + 10:
                continue
            # 底色：待翻译 / 已删除
            if "pending" in cls:
                rg.rectangle(box, fill=(148, 163, 184, 22), outline=(148, 163, 184, 170), width=1)
            if h["ghost"]:
                rg.rectangle(box, fill=(239, 68, 68, 34), outline=(239, 68, 68, 220), width=2)
                rg.text((box[0] + 3, box[1] - 9), f"已删除 · {h['label'][:22]}",
                        font=font_for(10, "已删除"), fill=(254, 202, 202))
            # 选中/联动高亮
            if "sel" in cls:
                rg.rectangle(box, fill=(96, 165, 250, 66), outline=(96, 165, 250, 255), width=2)
            elif "peer" in cls and not h["ghost"]:
                rg.rectangle(box, fill=(251, 191, 36, 46), outline=(251, 191, 36, 220), width=2)
            if "pending" in cls:
                # ⏳ 徽标放在框右上角，避免压住「原文淡色占位」文字
                lbl = "⏳ 待翻译"
                lw = 11 * len(lbl)
                rg.rectangle([box[2] - lw - 2, box[1], box[2], box[1] + 13], fill=(15, 23, 42, 160))
                rg.text((box[2] - 3, box[1] + 6), lbl, font=font_for(9, lbl), fill=(148, 163, 184), anchor="rm")
            chg = h.get("change") or ""
            if chg and chg != "unchanged":
                col = {"added": (34, 197, 94), "modified": (245, 158, 11), "moved": (168, 85, 247),
                       "removed": (239, 68, 68)}.get(chg, (120, 120, 120))
                rg.rectangle([box[0] - 3, box[1], box[0], box[3]], fill=col)
                zh = {"added": "新增", "modified": "修改", "moved": "移动", "removed": "删除"}.get(chg, chg)
                rg.rectangle([box[2] - 26, box[1] - 13, box[2], box[1] - 1], fill=col)
                rg.text((box[2] - 13, box[1] - 7), zh, font=font_for(9, zh), fill=(11, 16, 32), anchor="mm")
        # 页码角标
        for pg in snap["pages"]:
            if pg["side"] != side:
                continue
            if pg["y"] + pg["h"] < scr["y"] - 2 or pg["y"] > scr["y"] + scr["h"] + 2:
                continue
            rg.text((pg["x"] + pg["w"] - 8 - ox, pg["y"] + pg["h"] - 8 - oy), f"p.{pg['page']}",
                    font=font_for(10, "p"), fill=(148, 163, 184), anchor="rs")
        im.paste(region, (int(scr["x"]), int(scr["y"])))
        d.rectangle([scr["x"], scr["y"], scr["x"] + scr["w"], scr["y"] + scr["h"]], outline=(38, 46, 64))
        # 滚动条示意
        if scr["maxScroll"] > 0:
            frac = scr["scrollTop"] / scr["maxScroll"]
            thumb_h = max(30, scr["h"] * scr["h"] / (scr["h"] + scr["maxScroll"]))
            ty0 = scr["y"] + frac * (scr["h"] - thumb_h)
            d.rounded_rectangle([scr["x"] + scr["w"] - 8, ty0, scr["x"] + scr["w"] - 3, ty0 + thumb_h], 3,
                                fill=(51, 64, 90))

    # 详情面板
    det = snap["detail"]
    if det["visible"]:
        x0, y0, x1, y1 = det["x"], det["y"], det["x"] + det["w"], det["y"] + det["h"]
        d.rounded_rectangle([x0, y0, x1, y1], 8, fill=(21, 25, 34, 250), outline=(51, 61, 82))
        d.rounded_rectangle([x0, y0, x1, y0 + 30], 8, fill=(27, 33, 48))
        txt((x0 + 10, y0 + 15), f"{det['kind']}  {det['page']}  {det['segId']}", 12, (230, 233, 239), "lm")
        txt((x1 - 16, y0 + 15), "×", 14, (160, 170, 186), "mm")
        yy = y0 + 40
        bx = x0 + 10
        for b in det["badges"]:
            wpx = 12 + 8 * len(b)
            d.rounded_rectangle([bx, yy, bx + wpx, yy + 18], 9, fill=(43, 33, 12), outline=(146, 64, 14))
            txt((bx + 7, yy + 9), b[:20], 10, (252, 211, 77), "lm")
            bx += wpx + 6
        if det["badges"]:
            yy += 26
        txt((x0 + 10, yy), "原文", 10, (125, 135, 156), "lm")
        yy += 8
        for line in wrap_text(det["source"], 46)[:4]:
            txt((x0 + 14, yy + 8), line, 12, (184, 192, 208), "lm")
            yy += 17
        yy += 8
        txt((x0 + 10, yy), f"译文  {det['status']}", 10, (125, 135, 156), "lm")
        yy += 14
        d.rounded_rectangle([x0 + 10, yy, x1 - 10, yy + 62], 6, fill=(27, 33, 48), outline=(51, 61, 82))
        for line in wrap_text(det["trans"] or "（未翻译）", 44)[:3]:
            txt((x0 + 16, yy + 12), line, 12, (230, 233, 239), "lm")
            yy += 17
        yy += 70
        if det["diffVisible"] and det["diff"]:
            txt((x0 + 10, yy), "行内差异", 10, (125, 135, 156), "lm")
            yy += 14
            for line in wrap_text(det["diff"], 46)[:3]:
                txt((x0 + 14, yy + 8), line, 11.5, (200, 206, 220), "lm")
                yy += 16
        yy += 12
        hx = x0 + 10
        for kv in det["history"][:4]:
            plain = kv.replace("\n", " ")
            wpx = 14 + 7 * len(plain)
            if hx + wpx > x1 - 10:
                break
            d.rounded_rectangle([hx, yy, hx + wpx, yy + 18], 4, fill=(27, 33, 48), outline=(38, 46, 64))
            txt((hx + 7, yy + 9), plain[:28], 10, (184, 192, 208), "lm")
            hx += wpx + 5

    # 变更过滤说明条 / 底部水印
    d.rectangle([0, H - 20, W, H], fill=(12, 15, 22))
    txt((10, H - 10), f"DOM 快照合成图（沙箱无浏览器）· {caption} · 1440x900", 10, (110, 120, 140), "lm")
    txt((W - 10, H - 10), snap["url"][:80], 10, (110, 120, 140), "rm")
    im.save(out_png)
    return out_png


def parse_float_font(css_font: str) -> str:
    m = re.search(r"([\d.]+)px", str(css_font))
    return m.group(1) if m else "10"


def wrap_text(s: str, width: int):
    s = str(s or "").replace("\n", " ")
    out, cur = [], ""
    for ch in s:
        cur += ch
        if len(cur) >= width * (2 if any("\u4e00" <= c <= "\u9fff" for c in cur) else 1):
            out.append(cur); cur = ""
    if cur:
        out.append(cur)
    return out or [""]


def render_all(snapshot_files: list[str], base_url: str, out: Path) -> list[Path]:
    # 前 6 张是 Lead 验收清单里的固定名字（顺序/名称不要改）
    names = {"initial": "ui_01_initial.png", "click_source": "ui_02_click_source_highlight.png",
             "click_cn": "ui_03_click_cn_reverse.png", "scroll_sync": "ui_04_scroll_sync.png",
             "changes_only": "ui_05_changes_only.png", "deeplink": "ui_06_deeplink_restore.png",
             "click_cross_page": "ui_07_click_from_scrolled_page.png",
             "ghost_removed": "ui_08_ghost_removed.png"}
    made = []
    for f in snapshot_files:
        p = Path(f)
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for snap in data.get("snapshots", []):
            name = names.get(snap["label"])
            if not name:
                continue
            png = render_snapshot(snap, base_url, out / name, snap["label"])
            made.append(png)
            print(f"      render → {png}")
    return made


def nonbg_ratio(png: Path, bg=(15, 17, 21)) -> float:
    from PIL import Image
    im = Image.open(png).convert("RGB")
    im.thumbnail((480, 480))
    px = list(im.getdata())
    same = sum(1 for c in px if abs(c[0] - bg[0]) <= 6 and abs(c[1] - bg[1]) <= 6 and abs(c[2] - bg[2]) <= 6)
    return 1.0 - same / max(1, len(px))


# ==========================================================================
def main() -> int:
    global MODE
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901/?mock=1"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "data" / "derived" / "preview")
    out.mkdir(parents=True, exist_ok=True)
    url = base if "?" in base else base.rstrip("/") + "/"
    is_mock = "mock=1" in url
    print(f"== ui_smoke.py ==\n  url={url}\n  out={out}")

    pw_result = run_playwright(url, out, is_mock)
    if pw_result is None:
        MODE = "node-dom-harness"
        info = run_harness(url, out)
        check("harness(main) 退出码 0", info["exit"] == 0, str(info))
        shots = render_all([info["snapshot"], info["snapshot_deeplink"]], url, out)
        for png in shots:
            check(f"{png.name} 非空白(>60%)", nonbg_ratio(png) > 0.6, f"{nonbg_ratio(png):.2f}")
        check("至少 3 张预览图", len(shots) >= 3, str([p.name for p in shots]))
    else:
        MODE = "playwright-chromium"
        check("playwright 断言整体通过", pw_result is True, "")

    print(f"\n== 模式={MODE} · 断言 {len(CHECKS) - len(FAILS)}/{len(CHECKS)} 通过 ==")
    for f in FAILS:
        print(f"  FAILED: {f}")
    print("OK" if not FAILS else "FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
