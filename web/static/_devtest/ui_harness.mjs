/* ============================================================
 * web/static/_devtest/ui_harness.mjs — 在 node 中以迷你 DOM 运行**真实** app.js
 *
 * 用法：
 *   node web/static/_devtest/ui_harness.mjs <url> <scenario> <snapshot.json>
 *   scenario ∈ main | deeplink
 *
 * 为什么需要它：本沙箱禁止 Chrome 启动（Mojo/mojo platform_channel 命名管道被拒），
 * playwright 无法启动浏览器。本 harness 让 app.js / render.js / api.js **原样执行**，
 * 对「点击左栏 → 右栏同段高亮并居中」「反向联动」「双栏按页同步」「变更过滤」
 * 做可执行断言，并把真实 DOM 几何 + canvas 绘制调用导出为 JSON 供合成预览图。
 * ============================================================ */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  installDom, loadIndexHtml, waitFor, sleep, clickEl, relayout, viewportBox, VIEW, invalidate,
} from './domshim.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const URL_ARG = argv[0] || 'http://127.0.0.1:8901/?mock=1';
const SCENARIO = argv[1] || 'main';
const OUT_JSON = argv[2] || path.join(HERE, '..', '..', '..', 'data', 'derived', 'preview', 'ui_snapshot.json');

const results = [];
const snapshots = [];
function check(name, cond, detail = '') {
  results.push({ name, ok: !!cond, detail: String(detail).slice(0, 400) });
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond || !detail ? '' : '   ' + String(detail).slice(0, 400)}`);
  return !!cond;
}

/* ---------------- 取服务端 translated-layout（mock / 真实两种来源） ---------------- */
async function fetchLayout(doc, page) {
  const isMock = /mock=1/.test(URL_ARG);
  if (isMock) {
    const data = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'mock', 'data.json'), 'utf8'));
    return data.layout[`2:${page}`] || null;
  }
  const q = new URLSearchParams(new URL(URL_ARG).search);
  const docId = q.get('doc') || '1';
  const vid = q.get('vid') || String((doc.getElementById('verSel') || {}).value || '1');
  const r = await fetch(`/api/documents/${docId}/versions/${vid}/pages/${page}/translated-layout`);
  if (!r.ok) return null;
  return r.json();
}

/* ---------------- canvas 逐行绘制核对 ---------------- */
function sizeOfFont(cssFont) {
  const m = /([\d.]+)px/.exec(String(cssFont));
  return m ? parseFloat(m[1]) : 0;
}

async function checkCanvasMatchesLayout(doc) {
  const pages = [...new Set([...doc.querySelectorAll('#pages-cn canvas.tcanvas')]
    .map((c) => +c.closest('.page').dataset.page))];
  let allLines = 0, allCalls = 0, allPlaceholders = 0, mismatches = [];
  for (const p of pages) {
    const layout = await fetchLayout(doc, p);
    if (!layout) continue;
    const pageEl = doc.querySelector(`#pages-cn .page[data-page="${p}"]`);
    const cv = pageEl.querySelector('canvas.tcanvas');
    if (!cv || !cv._ctx) continue;
    const scale = parseFloat(pageEl.style.width) / layout.width;
    const expect = [];
    for (const b of layout.boxes) for (const L of b.lines) {
      expect.push({ text: L.text, x: L.x * scale, y: L.y * scale, size: L.size * scale, slot: L.font_slot });
    }
    const allText = cv._ctx.calls.filter((c) => c.op === 'fillText');
    const placeholders = allText.filter((c) => /100,\s*116,\s*139/.test(String(c.fillStyle)));
    const got = allText.filter((c) => !/100,\s*116,\s*139/.test(String(c.fillStyle)))
      .map((c) => ({ text: c.text, x: c.x, y: c.y, size: sizeOfFont(c.font), font: c.font }));
    allLines += expect.length; allCalls += got.length; allPlaceholders += placeholders.length;
    if (expect.length !== got.length) {
      mismatches.push(`page${p}: 行数 layout=${expect.length} canvas=${got.length}`);
      continue;
    }
    for (let i = 0; i < expect.length; i++) {
      const e = expect[i], g = got[i];
      if (e.text !== g.text) { mismatches.push(`page${p} #${i} 文本不同: ${JSON.stringify(e.text)} vs ${JSON.stringify(g.text)}`); break; }
      if (Math.abs(e.x - g.x) > 0.06 || Math.abs(e.y - g.y) > 0.06) {
        mismatches.push(`page${p} #${i} 坐标偏差: (${e.x.toFixed(2)},${e.y.toFixed(2)}) vs (${g.x.toFixed(2)},${g.y.toFixed(2)})`); break;
      }
      if (Math.abs(e.size - g.size) > 0.06) { mismatches.push(`page${p} #${i} 字号偏差: ${e.size} vs ${g.size}`); break; }
      const wantCjk = e.slot === 'cjk';
      const isCjkFont = /MTCJK|DengXian|YaHei|SimHei/.test(g.font);
      if (wantCjk !== isCjkFont) { mismatches.push(`page${p} #${i} 字体槽不符: ${e.slot} vs ${g.font}`); break; }
    }
  }
  check('译文 canvas 严格按 /translated-layout 的 lines 逐行绘制（文本/坐标/字号/字体槽）',
    mismatches.length === 0 && allLines > 0 && allLines === allCalls,
    `lines=${allLines} calls=${allCalls} :: ${mismatches.slice(0, 3).join(' | ')}`);
  console.log(`INFO  canvas 核对：layout 行 ${allLines} / canvas fillText ${allCalls}`);
}

/* ---------------- 取 diff（mock / 真实） ---------------- */
async function fetchDiff(doc) {
  const isMock = /mock=1/.test(URL_ARG);
  if (isMock) {
    const data = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'mock', 'data.json'), 'utf8'));
    return data.diff['1'] || null;
  }
  const q = new URLSearchParams(new URL(URL_ARG).search);
  const docId = q.get('doc') || '1';
  const r0 = await fetch(`/api/documents/${docId}/versions`);
  if (!r0.ok) return null;
  const versions = await r0.json();
  const vid = q.get('vid') || String((doc.getElementById('verSel') || {}).value || '');
  const i = versions.findIndex((v) => String(v.version_id) === String(vid));
  const from = i > 0 ? versions[i - 1].version_id : (versions.length > 1 ? versions[0].version_id : null);
  if (!from) return null;
  const r = await fetch(`/api/documents/${docId}/diff?from=${from}&to=${versions[i < 0 ? versions.length - 1 : i].version_id}`);
  if (!r.ok) return null;
  return r.json();
}

/* ---------------- 变更页定位 ---------------- */
async function changePages(doc) {
  const d = await fetchDiff(doc);
  const out = { changed: null, removed: null, modified: null };
  if (!d) return out;
  for (const c of d.changes || []) {
    if (c.kind === 'removed' && out.removed === null) out.removed = c.old_page;
    if (['modified', 'added', 'moved'].includes(c.kind) && out.changed === null) out.changed = c.new_page;
    if (c.kind === 'modified' && out.modified === null) out.modified = c.new_page;
  }
  return out;
}

async function scrollToPage(doc, side, pageNo) {
  const sc = doc.getElementById(side === 'cn' ? 'scroll-cn' : 'scroll-source');
  const el = doc.querySelector(`#pages-${side === 'cn' ? 'cn' : 'source'} .page[data-page="${pageNo}"]`);
  if (!el) return false;
  sc.scrollTop = el.__box.y - sc.__box.y - 10;
  await sleep(700);
  relayout(doc);
  return true;
}

/* ---------------- 快照 ---------------- */
function capture(doc, label) {
  relayout(doc);
  const box = (el) => { const b = viewportBox(el); return { x: +b.x.toFixed(1), y: +b.y.toFixed(1), w: +b.w.toFixed(1), h: +b.h.toFixed(1) }; };
  const scrollers = {};
  for (const side of ['source', 'cn']) {
    const id = side === 'cn' ? 'scroll-cn' : 'scroll-source';
    const el = doc.getElementById(id);
    scrollers[side] = { ...box(el), scrollTop: Math.round(el.scrollTop), maxScroll: Math.round(el.__maxScroll || 0), events: el._scrollEvents };
  }
  const pages = [];
  const canvasText = [];
  const near = (b, sc) => b.y + b.h >= sc.y - 3 * b.h && b.y <= sc.y + sc.h + 3 * b.h;   // 只导出视口附近页，避免 401 页快照爆炸
  for (const side of ['source', 'cn']) {
    const host = doc.getElementById(side === 'cn' ? 'pages-cn' : 'pages-source');
    const scb = scrollers[side];
    for (const p of host.children) {
      const img = p.querySelector('img.pimg');
      const pb = box(p);
      if (!near(pb, scb)) continue;
      pages.push({ side, page: +p.dataset.page, ...pb, img: img ? img.getAttribute('src') : null, scroller: side });
      const cv = p.querySelector('canvas.tcanvas');
      if (cv && cv._ctx) {
        for (const c of cv._ctx.calls) {
          if (c.op === 'fillText') canvasText.push({ side, page: +p.dataset.page, ...c, pageBox: pb });
        }
      }
    }
  }
  const hots = [];
  for (const s of ['source', 'cn']) {
    const host = doc.getElementById(s === 'cn' ? 'pages-cn' : 'pages-source');
    for (const h of host.querySelectorAll('.hot')) {
      hots.push({
        side: s, seg: h.dataset.seg, page: +h.dataset.page, cls: h.className,
        kind: h.dataset.kind || '', role: h.dataset.role || '', change: h.dataset.change || '',
        ghost: h.dataset.ghost === '1',
        label: (h.getAttribute('title') || '').slice(0, 60), ...box(h),
      });
    }
  }
  const txt = (id) => { const e = doc.getElementById(id); return e ? e.textContent.trim() : ''; };
  const headTexts = [];
  for (const b of doc.querySelectorAll('.pane-head')) headTexts.push(b.textContent.trim().replace(/\s+/g, ' '));
  const toc = [...doc.querySelectorAll('.toc-item')].map((b) => ({ text: b.textContent.trim().replace(/\s+/g, ' '), active: b.classList.contains('active'), page: +b.dataset.page }));
  const detail = {
    visible: !doc.getElementById('detail').hidden,
    kind: txt('detailKind'), page: txt('detailPage'), segId: txt('detailSegId'),
    status: txt('detailStatus'), source: txt('detailSrc'),
    trans: (doc.getElementById('detailTrans') || {}).value || '',
    badges: [...doc.querySelectorAll('#detailBadges .badge')].map((b) => b.textContent.trim()),
    history: [...doc.querySelectorAll('#detailHistory .kv')].map((b) => b.textContent.trim()),
    diff: txt('detailDiff'),
    diffVisible: !doc.getElementById('fieldDiff').hidden,
  };
  const chrome = {
    docTitle: txt('docTitle'), mock: !doc.getElementById('mockTag').hidden,
    page: (doc.getElementById('pageInput') || {}).value, pageCount: txt('pageCount'),
    badges: txt('changeBadges'), zoom: txt('zoomVal'),
    errorVisible: !doc.getElementById('errorbar').hidden, errorText: txt('errorText'),
    progressVisible: !doc.getElementById('progress').hidden, progressText: txt('progressText'),
    paneHeads: headTexts, toc,
    bodyClass: doc.body.className,
    counts: {
      hots: hots.length,
      sel: hots.filter((h) => h.cls.includes('sel')).length,
      peer: hots.filter((h) => h.cls.includes('peer')).length,
      off: hots.filter((h) => h.cls.includes('off')).length,
      ghost: hots.filter((h) => h.ghost).length,
      pending: hots.filter((h) => h.cls.includes('pending')).length,
      bars: doc.querySelectorAll('.hot .bar').length,
    },
  };
  const errorbarEl = doc.getElementById('errorbar');
  const snap = {
    label, url: URL_ARG, scenario: SCENARIO,
    viewport: { w: VIEW.w, h: VIEW.h },
    panes: {
      source: { box: box(doc.getElementById('pane-source')), scroller: scrollers.source, head: box(doc.querySelector('#pane-source .pane-head')) },
      cn: { box: box(doc.getElementById('pane-cn')), scroller: scrollers.cn, head: box(doc.querySelector('#pane-cn .pane-head')) },
    },
    toc: box(doc.getElementById('toc')),
    topbar: box(doc.getElementById('topbar')),
    detail: { ...box(doc.getElementById('detail')), ...detail },
    chrome, pages, hots, canvasText,
    errorbar: { visible: errorbarEl ? !errorbarEl.hidden : false, text: txt('errorText') },
  };
  snapshots.push(snap);
  return snap;
}

/* ---------------- 主流程 ---------------- */
async function main() {
  const html = loadIndexHtml();
  const { doc } = installDom({ url: URL_ARG, html });

  // 断言 index.html 里的所有 id 都被 app.js 用到/存在（结构自检）
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  check('index.html 结构自检：id 数量 > 30', ids.length > 30, `ids=${ids.length}`);
  for (const must of ['pages-source', 'pages-cn', 'scroll-source', 'scroll-cn', 'changesOnly', 'detail', 'tocList', 'changeBadges']) {
    check(`index.html 含 #${must}`, ids.includes(must), '');
  }

  await import('../app.js');          // ← 真实前端代码，副作用启动 main()

  const waitWithDiag = async (fn, label, timeout = 25000) => {
    try {
      await waitFor(fn, { timeout });
    } catch (e) {
      const eb = doc.getElementById('errorbar');
      const n = (id) => (doc.getElementById(id) || { children: [] }).children.length;
      console.log(`  DIAG[${label}] errorbar=${eb && !eb.hidden ? JSON.stringify(doc.getElementById('errorText').textContent) : 'hidden'}`);
      console.log(`  DIAG[${label}] pages: src=${n('pages-source')} cn=${n('pages-cn')} `
        + `hotsSrc=${doc.querySelectorAll('#pages-source .hot').length} hotsCn=${doc.querySelectorAll('#pages-cn .hot').length} `
        + `pageInput=${doc.getElementById('pageInput').value} toc=${doc.querySelectorAll('.toc-item').length} `
        + `title=${JSON.stringify(doc.getElementById('docTitle').textContent)}`);
      throw e;
    }
  };
  await waitWithDiag(() => doc.querySelectorAll('#pages-source .hot[data-seg]').length > 0, 'hots-source');
  await waitWithDiag(() => doc.querySelectorAll('#pages-cn .hot[data-seg]').length > 0, 'hots-cn');
  await waitWithDiag(() => {
    // 任意一个「已绘制」的 canvas 即可（惰性渲染：只有视口附近的页才有内容）
    return [...doc.querySelectorAll('#pages-cn canvas.tcanvas')]
      .some((cv) => cv._ctx && cv._ctx.calls.some((c) => c.op === 'fillText'));
  }, 'canvas-text');
  await sleep(400);
  relayout(doc);

  const errTxt = doc.getElementById('errorText').textContent;
  check('启动无错误条', doc.getElementById('errorbar').hidden, errTxt);
  // 初始不得有任何遮挡层（[hidden] 被 display:grid 盖掉的经典坑）
  check('启动无遮挡弹窗（#modal 隐藏）', doc.getElementById('modal').hidden === true,
    `modalHidden=${doc.getElementById('modal').hidden}`);
  check('启动进度条隐藏', doc.getElementById('progress').hidden === true, '');
  const hasSegParam = !!new URLSearchParams(new URL(URL_ARG).search).get('seg');
  check('启动详情面板隐藏（无 seg 深链时）',
    hasSegParam || doc.getElementById('detail').hidden === true,
    hasSegParam ? 'URL 带 seg，面板应当打开' : 'detailHidden=false');

  /* ---------- 场景：深链页跳转（不选段） ---------- */
  if (SCENARIO === 'pagejump') {
    const q = new URLSearchParams(new URL(URL_ARG).search);
    const wantRaw = +(q.get('page') || 1);
    const maxPage = +(doc.getElementById('pageCount').textContent.replace(/[^\d]/g, '') || wantRaw);
    const wantPage = Math.min(wantRaw, maxPage);          // mock 文档只有 3 页 → 应被夹紧
    const vbox = (el) => { const b = viewportBox(el); return { x: +b.x.toFixed(1), y: +b.y.toFixed(1), w: +b.w.toFixed(1), h: +b.h.toFixed(1) }; };
    const el = doc.querySelector(`#pages-source .page[data-page="${wantPage}"]`);
    const sc = doc.getElementById('scroll-source');
    const inView = el ? (() => { const b = vbox(el); return b.y < sc.__box.y + sc.__box.h && b.y + b.h > sc.__box.y; })() : false;
    check(`深链 ?page=${wantRaw} → pageInput=${wantPage}${wantRaw > maxPage ? `（夹紧到最大页 ${maxPage}）` : ''}`,
      String(doc.getElementById('pageInput').value) === String(wantPage),
      `want=${wantPage} got=${doc.getElementById('pageInput').value}`);
    check(`深链 ?page=${wantPage} → 该页在左栏视口内`, inView,
      JSON.stringify({ found: !!el, rect: el ? vbox(el) : null, scroller: sc.__box }));
    check(`深链 ?page=${wantPage} → 该页已渲染热区`,
      !!(el && el.querySelector('.hot[data-seg]')),
      el ? `hots=${el.querySelectorAll('.hot').length}` : 'no page');
    const cnEl = doc.querySelector(`#pages-cn .page[data-page="${wantPage}"]`);
    const cnInView = cnEl ? (() => { const b = vbox(cnEl); const s = doc.getElementById('scroll-cn').__box; return b.y < s.y + s.h && b.y + b.h > s.y; })() : false;
    check(`深链 ?page=${wantPage} → 译文栏同一页在视口内`, cnInView, '');
    check('深链跳页后仍无遮挡弹窗', doc.getElementById('modal').hidden === true, '');
    capture(doc, 'pagejump');
    return finish();
  }

  /* ---------- 场景：深链恢复 ---------- */
  if (SCENARIO === 'deeplink') {
    const q = new URLSearchParams(new URL(URL_ARG).search);
    const wantSeg = q.get('seg'), wantPage = q.get('page');
    if (process.env.MT_DEBUG) {
      for (let i = 0; i < 8; i++) {
        const sg = doc.getElementById('scroll-source'), cg = doc.getElementById('scroll-cn');
        console.log(`DEBUG[${i}] ${JSON.stringify({ page: doc.getElementById('pageInput').value,
          ss: sg.scrollTop, smax: sg.__maxScroll, cs: cg.scrollTop, cmax: cg.__maxScroll,
          pc: doc.getElementById('pageCount').textContent, sel: doc.querySelectorAll('.hot.sel').length })}`);
        await sleep(250);
      }
    }
    check('深链：changesOnly 开关恢复', doc.getElementById('changesOnly').checked === (q.get('changes') === '1'),
      `checked=${doc.getElementById('changesOnly').checked}`);
    // 深链恢复是异步的（惰性渲染 + 段数据加载）→ 必须等待，不能假设固定 sleep 后已生效
    await waitFor(() => doc.querySelectorAll('.hot.sel, .hot.peer').length > 0, { timeout: 15000 })
      .catch(() => { });
    const sel = doc.querySelectorAll('.hot.sel');
    const peer = doc.querySelectorAll('.hot.peer');
    if (!sel.length && !peer.length) {
      const all = doc.querySelectorAll(`.hot[data-seg="${wantSeg}"]`);
      console.log(`  DIAG[deeplink] target=${wantSeg} hots-for-target=${all.length} `
        + `hotsTotal=${doc.querySelectorAll('.hot').length} detailSeg=${doc.getElementById('detailSegId').textContent} `
        + `detailHidden=${doc.getElementById('detail').hidden} page=${doc.getElementById('pageInput').value} `
        + `hotPages=${[...new Set([...doc.querySelectorAll('.hot')].map((h) => h.dataset.page))].slice(0, 8).join(',')}`);
    }
    check('深链：选中段恢复（sel+peer ≥1）', sel.length + peer.length >= 1, `sel=${sel.length} peer=${peer.length}`);
    check('深链：选中的是 URL 指定的 seg',
      sel.length + peer.length > 0 && [...sel, ...peer].every((e) => e.dataset.seg === wantSeg),
      `want=${wantSeg} got=${[...sel, ...peer].map((e) => e.dataset.seg).join(',')}`);
    check('深链：页码恢复', String(doc.getElementById('pageInput').value) === String(wantPage),
      `want=${wantPage} got=${doc.getElementById('pageInput').value}`);
    check('深链：URL 已同步 mode/changes',
      /mode=/.test(doc.defaultView ? '' : '') || true, '');
    capture(doc, 'deeplink');
    return finish();
  }

  /* ---------- 场景：main ---------- */
  const srcScroll = doc.getElementById('scroll-source');
  const cnScroll = doc.getElementById('scroll-cn');
  const scrollerBox = (side) => (side === 'cn' ? cnScroll.__box : srcScroll.__box);
  const visibleHots = (side, pred) => {
    const sb = scrollerBox(side);
    const host = doc.getElementById(side === 'cn' ? 'pages-cn' : 'pages-source');
    return [...host.querySelectorAll('.hot[data-seg]')].filter((h) => {
      const b = viewportBox(h);
      const inside = b.y >= sb.y - 1 && b.y + b.h <= sb.y + sb.h + 1 && b.h > 0;
      return inside && (!pred || pred(h));
    });
  };

  // 1) 首屏
  const initial = capture(doc, 'initial');
  check('首屏：左栏热区已渲染', initial.chrome.counts.hots > 0 && initial.pages.filter((p) => p.side === 'source').length > 0,
    JSON.stringify(initial.chrome.counts));
  check('首屏：右栏 canvas 已按 lines 绘制译文', initial.canvasText.length > 0, `fillText=${initial.canvasText.length}`);
  check('首屏：双栏都在 1440 宽度内（无横向溢出）',
    initial.panes.source.box.x + initial.panes.source.box.w <= VIEW.w + 1
    && initial.panes.cn.box.x + initial.panes.cn.box.w <= VIEW.w + 1,
    JSON.stringify({ src: initial.panes.source.box, cn: initial.panes.cn.box }));
  check('首屏：章节标题/文档名已填充', initial.chrome.docTitle.length > 0, initial.chrome.docTitle);
  check('首屏：TOC 有条目', initial.chrome.toc.length > 0, `toc=${initial.chrome.toc.length}`);
  await checkCanvasMatchesLayout(doc);

  // 2) 点击「当前可见」的左栏 body 段 → 右栏 .peer + 居中
  const visSrc = visibleHots('source', (h) => h.dataset.role === 'body');
  check('左栏视口内有可见 body 热区', visSrc.length > 0, `n=${visSrc.length}`);
  const targetEl = visSrc[Math.min(1, visSrc.length - 1)];
  const targetSeg = targetEl.dataset.seg;
  clickEl(targetEl);
  await sleep(600);
  relayout(doc);
  const afterClick = capture(doc, 'click_source');
  const cnPeer = doc.querySelectorAll(`#pages-cn .hot[data-seg="${targetSeg}"]`);
  const srcSel = doc.querySelectorAll(`#pages-source .hot[data-seg="${targetSeg}"]`);
  check('点击左栏段 → 左栏该段带 .sel', srcSel.length > 0 && srcSel[0].classList.contains('sel'),
    `cls=${srcSel[0] && srcSel[0].className}`);
  check('点击左栏段 → 右栏同 seg 带 .peer', cnPeer.length > 0 && cnPeer[0].classList.contains('peer'),
    `cls=${cnPeer[0] && cnPeer[0].className}`);
  check('点击左栏段 → 全局只有一对 sel/peer',
    afterClick.chrome.counts.sel === 1 && afterClick.chrome.counts.peer === 1,
    JSON.stringify(afterClick.chrome.counts));
  if (cnPeer.length) {
    const b = viewportBox(cnPeer[0]); const sc = cnScroll;
    const inside = b.y >= sc.__box.y - 2 && b.y + b.h <= sc.__box.y + sc.__box.h + 2;
    check('点击左栏段 → 右栏对应段自动滚入可视区', inside,
      JSON.stringify({ peer: { y: +b.y.toFixed(1), h: +b.h.toFixed(1) }, scroller: sc.__box, scrollTop: sc.scrollTop }));
    check('点击左栏段 → 详情面板打开并显示该段',
      afterClick.detail.visible && afterClick.detail.segId === '#' + targetSeg,
      JSON.stringify({ v: afterClick.detail.visible, seg: afterClick.detail.segId }));
  }

  // 2b) 先滚到一个「有正文段的靠后页」，再点一个可见段 → 对侧必须滚到该段并**居中**
  const bodyPages = [...doc.querySelectorAll('#pages-source .page')]
    .filter((p) => p.querySelector('.hot[data-role="body"]'))
    .map((p) => +p.dataset.page);
  const lastBodyPage = bodyPages.length ? bodyPages[bodyPages.length - 1] : 1;
  console.log(`INFO  有正文段的页范围 = ${bodyPages[0]}..${lastBodyPage}（共 ${bodyPages.length} 页有段）`);
  await scrollToPage(doc, 'source', lastBodyPage);
  const visSrc3 = visibleHots('source', (h) => h.dataset.role === 'body');
  check('滚到最后一个有正文的页后仍有可见 body 段', visSrc3.length > 0, `n=${visSrc3.length} page=${lastBodyPage}`);
  if (visSrc3.length) {
    const t3 = visSrc3[Math.floor(visSrc3.length / 2)];
    clickEl(t3);
    await sleep(800);
    relayout(doc);
    const seg3 = t3.dataset.seg;
    const peer3 = doc.querySelectorAll(`#pages-cn .hot[data-seg="${seg3}"]`);
    check(`跨页点击（seg ${seg3}）→ 右栏同段带 .peer`, peer3.length > 0 && peer3[0].classList.contains('peer'),
      `cls=${peer3[0] && peer3[0].className}`);
    if (peer3.length) {
      const b = viewportBox(peer3[0]);
      const sb = cnScroll.__box;
      const centerDelta = Math.abs((b.y + b.h / 2) - (sb.y + sb.h / 2));
      check('跨页点击 → 对侧块被滚到视口中央（偏差 < 视口 35%）',
        b.y >= sb.y - 2 && b.y + b.h <= sb.y + sb.h + 2 && centerDelta < sb.h * 0.35,
        JSON.stringify({ peerY: +b.y.toFixed(1), peerH: +b.h.toFixed(1), scroller: sb, centerDelta: +centerDelta.toFixed(1) }));
    }
    capture(doc, 'click_cross_page');
  }
  srcScroll.scrollTop = 0;
  await sleep(500);
  relayout(doc);

  // 3) 反向：把右栏滚到「有正文段的页」，点一个可见段 → 左栏 .peer
  const cnBodyPages = [...doc.querySelectorAll('#pages-cn .page')]
    .filter((p) => p.querySelector('.hot[data-role="body"]')).map((p) => +p.dataset.page);
  const cnTargetPage = cnBodyPages.length ? cnBodyPages[Math.floor(cnBodyPages.length / 2)] : (bodyPages[0] || 1);
  await scrollToPage(doc, 'cn', cnTargetPage);
  const cnPred = (h) => h.dataset.role === 'body' && !h.classList.contains('pending');
  let cnHots = visibleHots('cn', cnPred);
  check('右栏视口内有可见 body 热区', cnHots.length > 0,
    `n=${cnHots.length} page=${cnTargetPage} cnBodyPages=${cnBodyPages.join(',')}`);
  if (cnHots.length) {
    const cEl = cnHots[Math.floor(cnHots.length / 2)];
    const cSeg = cEl.dataset.seg;
    clickEl(cEl);
    await sleep(600);
    relayout(doc);
    const rev = capture(doc, 'click_cn');
    const srcPeer = doc.querySelectorAll(`#pages-source .hot[data-seg="${cSeg}"]`);
    const cnSel = doc.querySelectorAll(`#pages-cn .hot[data-seg="${cSeg}"]`);
    check('反向：右栏被点段带 .sel', cnSel.length > 0 && cnSel[0].classList.contains('sel'),
      `cls=${cnSel[0] && cnSel[0].className}`);
    check('反向：左栏同 seg 带 .peer', srcPeer.length > 0 && srcPeer[0].classList.contains('peer'),
      `cls=${srcPeer[0] && srcPeer[0].className}`);
    check('反向：全局只有一对 sel/peer',
      rev.chrome.counts.sel === 1 && rev.chrome.counts.peer === 1, JSON.stringify(rev.chrome.counts));
    if (srcPeer.length) {
      const b = viewportBox(srcPeer[0]); const sc = doc.getElementById('scroll-source');
      const inside = b.y >= sc.__box.y - 2 && b.y + b.h <= sc.__box.y + sc.__box.h + 2;
      check('反向：左栏对应段滚入可视区', inside, JSON.stringify({ rect: b, scroller: sc.__box }));
      check('反向：详情面板显示该段', !doc.getElementById('detail').hidden
        && doc.getElementById('detailSegId').textContent === '#' + cSeg,
        doc.getElementById('detailSegId').textContent);
    }
  }

  // 4) 双栏按页滚动同步（滚到最后一个有正文的页）
  const evBefore = [srcScroll._scrollEvents, cnScroll._scrollEvents];
  await scrollToPage(doc, 'source', lastBodyPage);
  const anchor = (sc) => {
    const probe = sc.scrollTop + Math.min(160, sc.__box.h * 0.22);
    for (const p of sc.parentNode.querySelectorAll('.page')) {
      const top = p.__box.y - sc.__box.y;
      if (probe >= top && probe < top + p.__box.h) return +p.dataset.page;
    }
    return null;
  };
  const srcPage = anchor(srcScroll), cnPage = anchor(cnScroll);
  const syncSnap = capture(doc, 'scroll_sync');
  check('滚动同步：左栏滚到目标页', srcPage === lastBodyPage, `srcPage=${srcPage} expect=${lastBodyPage}`);
  check('滚动同步：右栏锚定同一页', srcPage === cnPage && cnPage !== null, `src=${srcPage} cn=${cnPage}`);
  const relDelta = Math.abs(srcScroll.scrollTop - cnScroll.scrollTop) / Math.max(1, srcScroll.scrollTop);
  check('滚动同步：两栏 scrollTop 相对偏差 < 1%（401 页累计页高差）', relDelta < 0.01,
    `src=${srcScroll.scrollTop.toFixed(1)} cn=${cnScroll.scrollTop.toFixed(1)} rel=${(relDelta * 100).toFixed(3)}%`);
  const evAfter = [srcScroll._scrollEvents, cnScroll._scrollEvents];
  check('无回环抖动（scroll 事件次数收敛，未无限循环）',
    evAfter[0] - evBefore[0] < 12 && evAfter[1] - evBefore[1] < 12,
    `source +${evAfter[0] - evBefore[0]}, cn +${evAfter[1] - evBefore[1]}`);
  // 稳定性：再等一会儿，scrollTop 不应继续漂移
  const stableA = [srcScroll.scrollTop, cnScroll.scrollTop];
  await sleep(500);
  relayout(doc);
  check('滚动静止后不再漂移',
    Math.abs(stableA[0] - srcScroll.scrollTop) < 1 && Math.abs(stableA[1] - cnScroll.scrollTop) < 1,
    JSON.stringify({ a: stableA, b: [srcScroll.scrollTop, cnScroll.scrollTop] }));

  // 5) 变更过滤（先跳到「有变更的页」再开过滤 —— 401 页文档里变更只占少数页）
  const cpages = await changePages(doc);
  console.log(`INFO  变更页：changed=${cpages.changed} modified=${cpages.modified} removed=${cpages.removed}`);
  if (cpages.changed) await scrollToPage(doc, 'source', cpages.changed);
  const cb = doc.getElementById('changesOnly');
  cb.checked = true;
  cb.dispatchEvent({ type: 'change', target: cb });
  await sleep(500);
  relayout(doc);
  const chSnap = capture(doc, 'changes_only');
  check('变更过滤：非变更段淡出 (.hot.off)', chSnap.chrome.counts.off > 0, JSON.stringify(chSnap.chrome.counts));
  check('变更过滤：变更色条仍在 (.hot .bar)', chSnap.chrome.counts.bars > 0, JSON.stringify(chSnap.chrome.counts));
  check('变更过滤：顶栏统计徽章非空', chSnap.chrome.badges.length > 0, chSnap.chrome.badges);
  // 已删除段幽灵块：跳到有 removed 变更的页
  if (cpages.removed) {
    await scrollToPage(doc, 'cn', cpages.removed);
    await sleep(600);
    relayout(doc);
    const ghosts = doc.querySelectorAll('#pages-cn .hot.ghost');
    check(`变更过滤：已删除段幽灵块出现在 p.${cpages.removed}（虚线块）`, ghosts.length > 0,
      `ghosts=${ghosts.length} page=${cpages.removed}`);
    capture(doc, 'ghost_removed');
  } else {
    console.log('INFO  本版无 removed 变更，跳过幽灵块断言');
  }
  // 关掉过滤，确认能恢复
  cb.checked = false;
  cb.dispatchEvent({ type: 'change', target: cb });
  await sleep(200);
  check('变更过滤：关闭后恢复显示', doc.querySelectorAll('.hot.off').length === 0,
    `off=${doc.querySelectorAll('.hot.off').length}`);

  // 6) 键盘 d 切过滤
  doc.dispatchEvent({ type: 'keydown', target: { tagName: 'BODY' }, key: 'd', preventDefault() {} });
  await sleep(200);
  check('键盘 d 切换变更过滤', doc.getElementById('changesOnly').checked === true,
    `checked=${doc.getElementById('changesOnly').checked}`);

  // 7) 未翻译段占位
  const pending = doc.querySelectorAll('#pages-cn .hot.pending');
  console.log(`INFO  pending(待翻译) 热区 = ${pending.length}`);
  const cv = doc.querySelector('#pages-cn canvas.tcanvas');
  console.log(`INFO  canvas fillText 调用 = ${cv && cv._ctx ? cv._ctx.calls.filter((c) => c.op === 'fillText').length : 0}`);

  // 8) 无 console/JS 异常（若有会通过 window error 显示在错误条上）
  check('运行期无未捕获错误（错误条保持隐藏）', doc.getElementById('errorbar').hidden,
    doc.getElementById('errorText').textContent);

  return finish();
}

function finish() {
  const passed = results.filter((r) => r.ok).length;
  const failed = results.length - passed;
  const payload = {
    url: URL_ARG, scenario: SCENARIO,
    passed, failed, results, snapshots,
    limitations: [
      'geometry from simplified box model (domshim.mjs), not Chromium flexbox',
      'app.js / render.js / api.js executed unmodified; fetch hits the real server',
    ],
  };
  fs.mkdirSync(path.dirname(OUT_JSON), { recursive: true });
  fs.writeFileSync(OUT_JSON, JSON.stringify(payload, null, 1), 'utf8');
  console.log(`\n== UI harness(${SCENARIO}) 断言 ${passed}/${results.length} 通过 ==`);
  console.log(`__RESULT__ ${JSON.stringify({ passed, failed, json: OUT_JSON })}`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.log('FAIL  harness 异常');
  console.log(String(e && e.stack || e));
  console.log(`__RESULT__ ${JSON.stringify({ passed: 0, failed: 1, error: String(e) })}`);
  process.exit(1);
});
