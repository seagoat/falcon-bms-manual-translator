/* ============================================================
 * app.js — 对照浏览器主逻辑
 *   · 三栏布局：目录树 | 原文 | 译文
 *   · 双栏滚动同步（页为锚 + 段内比例插值，rAF 节流 + syncing 防回环）
 *   · 点击/悬浮任一侧段落 → 两侧同 seg 热区 .sel/.peer 高亮 + 对侧居中
 *   · 变更可视化：色条 / 行内 diff / 删除幽灵块 / 统计徽章
 *   · 惰性渲染（视口 ±2 页）、深链、进度条、错误条
 * ============================================================ */
import api, { ApiError, IS_MOCK } from './api.js';
import * as R from './render.js';

const $ = (id) => document.getElementById(id);
const DEFAULT_DPI = 110;
const A4 = { w: 595.28, h: 841.89 };

/* ---------------- state ---------------- */
const S = {
  ready: false,
  docId: null, vid: null, fromVid: null,
  page: 1, pageCount: 1, seg: null,
  mode: 'compare', changesOnly: false, zoom: 1, dpi: DEFAULT_DPI,
  docs: [], versions: [],
  metric: { ...A4, unitsPerPx: 72 / DEFAULT_DPI, dpi: DEFAULT_DPI },
  recs: new Map(),              // page -> {data, layout, scale, srcPaint, cnPaint}
  segById: new Map(),
  changes: [], counts: {}, changeById: new Map(),
  pageDiffs: new Map(),
  diffToken: 0,
  imgRev: 0,                    // 每次翻译/复核后 +1 → 页面图 URL 换 key，避免浏览器旧缓存
  ghostsByPage: new Map(), ghostLoaded: new Set(), oldSegCache: new Map(),
  paneRatio: null, jobs: new Map(), toastTimer: null,
};

const scrollS = (side) => (side === 'cn' ? $('scroll-cn') : $('scroll-source'));
const pagesS = (side) => (side === 'cn' ? $('pages-cn') : $('pages-source'));
const paneS = (side) => (side === 'cn' ? $('pane-cn') : $('pane-source'));
const other = (side) => (side === 'cn' ? 'source' : 'cn');
const recOf = (p) => {
  let r = S.recs.get(p);
  if (!r) { r = { data: null, dataP: null, layout: null, layoutP: null, srcPaint: false, cnPaint: false }; S.recs.set(p, r); }
  return r;
};
const pageEl = (side, p) => pagesS(side).querySelector(`.page[data-page="${p}"]`);
const relTop = (node, sc) => node.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;

/* ---------------- 错误 / 提示 ---------------- */
function showError(msg) {
  $('errorText').textContent = String(msg).slice(0, 400);
  $('errorbar').hidden = false;
}
function clearError() { $('errorbar').hidden = true; }
function toast(msg, kind = '', ms = 2600) {
  const t = $('toast');
  t.textContent = msg; t.className = `toast ${kind}`; t.hidden = false;
  clearTimeout(S.toastTimer);
  S.toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ---------------- URL 深链 ---------------- */
function readUrl() {
  const q = new URLSearchParams(location.search);
  return {
    doc: +(q.get('doc') || 0) || null,
    vid: +(q.get('vid') || 0) || null,
    page: +(q.get('page') || 0) || null,
    seg: +(q.get('seg') || 0) || null,
    mode: q.get('mode') || 'compare',
    changes: q.get('changes') === '1' || q.get('changes') === 'only',
    mock: q.get('mock') === '1',
  };
}
function syncUrl(replace = true) {
  const q = new URLSearchParams();
  if (S.docId) q.set('doc', S.docId);
  if (S.vid) q.set('vid', S.vid);
  q.set('page', S.page);
  if (S.seg) q.set('seg', S.seg);
  q.set('mode', S.mode);
  q.set('changes', S.changesOnly ? '1' : '0');
  if (IS_MOCK) q.set('mock', '1');
  const url = `${location.pathname}?${q.toString()}`;
  try { history[replace ? 'replaceState' : 'pushState'](null, '', url); } catch { /* ignore */ }
}

/* ---------------- 引导 ---------------- */
async function boot() {
  const u = readUrl();
  S.mode = ['compare', 'source', 'cn'].includes(u.mode) ? u.mode : 'compare';
  S.changesOnly = !!u.changes;
  applyMode();
  $('changesOnly').checked = S.changesOnly;
  if (IS_MOCK) $('mockTag').hidden = false;

  try {
    $('skel-source').classList.add('on'); $('skel-cn').classList.add('on');
    await api.health();
    S.docs = await api.documents();
    if (!S.docs.length) {
      throw new ApiError('数据库里还没有文档。用「导入 PDF」或命令行 `python cli.py ingest --pdf <文件>` 建库。', 0);
    }
    $('skel-source').classList.remove('on'); $('skel-cn').classList.remove('on');

    fillDocSelect();
    const doc = S.docs.find((d) => d.doc_id === u.doc) || S.docs.find((d) => d.current_version_id) || S.docs[0];
    await loadDoc(doc.doc_id, u.vid || doc.current_version_id, { page: u.page, seg: u.seg });
    S.ready = true;
    $('hint-source').textContent = `${S.pageCount} 页`;
    $('hint-cn').textContent = IS_MOCK ? 'mock 数据' : '译文按服务端断行绘制';
  } catch (e) {
    $('skel-source').classList.remove('on'); $('skel-cn').classList.remove('on');
    showError(e instanceof ApiError ? e.message : String(e && e.stack || e));
    $('pages-source').innerHTML = `<div class="empty">⚠️ ${escapeHtml(String(e.message || e))}</div>`;
    $('pages-cn').innerHTML = '';
  }
}

function fillDocSelect() {
  const sel = $('docSel');
  sel.innerHTML = '';
  for (const d of S.docs) {
    const o = document.createElement('option');
    o.value = String(d.doc_id); o.textContent = d.title || d.slug || `doc ${d.doc_id}`;
    sel.appendChild(o);
  }
  sel.onchange = () => loadDoc(+sel.value, null, {});
}

async function loadDoc(docId, vid, { page = null, seg = null } = {}) {
  clearError();
  S.docId = docId; S.page = 1; S.seg = null;
  S.recs.clear(); S.segById.clear(); S.ghostsByPage.clear(); S.ghostLoaded.clear(); S.oldSegCache.clear();
  S.pageDiffs.clear();
  const doc = S.docs.find((d) => d.doc_id === docId);
  $('docTitle').textContent = doc?.title || doc?.slug || `doc ${docId}`;
  $('docSel').value = String(docId);

  S.versions = await api.versions(docId);
  if (!S.versions.length) throw new ApiError('该文档没有任何版本记录', 0);
  const cur = S.versions.find((v) => v.is_current) || S.versions[S.versions.length - 1];
  S.vid = vid || cur.version_id;
  if (!S.versions.some((v) => v.version_id === S.vid)) S.vid = cur.version_id;
  const vrow = S.versions.find((v) => v.version_id === S.vid);
  S.pageCount = vrow?.page_count || doc?.page_count || 1;

  // 上一版本（用于 diff / 幽灵块）
  const idx = S.versions.findIndex((v) => v.version_id === S.vid);
  S.fromVid = idx > 0 ? S.versions[idx - 1].version_id : null;
  fillVerSelect();

  S.changes = []; S.counts = {}; S.changeById.clear();
  renderBadges();
  await loadOutline();
  buildShells();
  const startPage = Math.min(Math.max(1, page || 1), S.pageCount);
  S.deepPage = page ? startPage : null;      // 深链指定的页，作为"唯一真相"记下来
  S.deepControl = !!page;                    // 校正窗口内由深链说了算
  gotoPage(startPage, { silent: true });
  if (seg) { S.page = startPage; await selectSeg(seg, { origin: null, scroll: true, force: true }); }
  syncUrl(true);
  startDiff();          // 首屏先渲染，版本差异异步补上（绝不阻塞 pageInput / 页面渲染）

  // ⚠️ 深链容易被"抢走"。实测 `?doc=2&vid=3&page=351` 会落到 360：
  // boot 时 gotoPage(351) 是对的，但**约 3.7 秒后**首屏页面图加载完成、版面被撑开，
  // 产生一次滚动 -> onScroll 按"当前可见页"反推，把 360 写回页码与 URL。
  // 所以要在**用户真正操作之前**一直保护深链页；不能用固定超时（图加载耗时不定）。
  // 释放时机 = 首次真实用户输入（滚轮 / 触摸 / 按键 / 指针 / 点击）。
  if (S.deepPage) {
    const want = S.deepPage;
    const release = () => clearDeepPage();
    for (const ev of ["wheel", "touchstart", "keydown", "pointerdown"]) {
      window.addEventListener(ev, release, { once: true, passive: true });
    }
    // 兜底：最多保护 15 秒，避免用户什么都不动时永远锁着
    setTimeout(release, 15000);
    void want;
  }
}

/** 用户一有主动操作就交还控制权：深链校正不再干预。 */
function clearDeepPage() {
  S.deepControl = false;
  S.deepPage = null;
}

/** 版本差异：异步加载 + 失败重试；回来后补徽章、变更过滤与幽灵块 */
function startDiff() {
  if (!S.fromVid) { S.counts = {}; renderBadges(); return; }
  const token = ++S.diffToken;
  const docId = S.docId, fromVid = S.fromVid, vid = S.vid;
  const load = async (attempt = 0) => {
    try {
      const d = await api.diff(docId, fromVid, vid);
      if (token !== S.diffToken) return;
      S.changes = d.changes || [];
      S.counts = d.counts || {};
      S.changeById.clear();
      for (const c of S.changes) S.changeById.set(c.change_id, c);
      renderBadges();
      applyFilter();
      S.pageDiffs.clear();
      S.ghostLoaded.clear();
      const [lo, hi] = visibleRange('cn');
      for (let p = lo; p <= hi; p++) ensureGhosts(p).catch(() => {});
      if (S.seg) updateDetail(S.seg);
    } catch (e) {
      if (token !== S.diffToken) return;
      if (attempt < 2) { setTimeout(() => load(attempt + 1), 1200 * (attempt + 1)); return; }
      console.warn('diff 不可用（已重试 2 次）', e);
    }
  };
  load(0);
}

function fillVerSelect() {
  const sel = $('verSel');
  sel.innerHTML = '';
  for (const v of S.versions) {
    const o = document.createElement('option');
    o.value = String(v.version_id);
    o.textContent = `${v.label || 'v' + v.version_no}${v.release_label ? ' · ' + v.release_label : ''}`;
    sel.appendChild(o);
  }
  sel.value = String(S.vid);
  sel.onchange = async () => {
    const nv = +sel.value;
    const i = S.versions.findIndex((v) => v.version_id === nv);
    S.fromVid = i > 0 ? S.versions[i - 1].version_id : null;
    S.vid = nv; S.seg = null; S.recs.clear(); S.segById.clear();
    S.ghostsByPage.clear(); S.ghostLoaded.clear(); S.pageDiffs.clear();
    $('detail').hidden = true;
    S.changes = []; S.counts = {}; S.changeById.clear();
    renderBadges();
    buildShells(); gotoPage(1, { silent: true }); syncUrl(true); startDiff();
  };
}

/* ---------------- 目录 ---------------- */
async function loadOutline() {
  const list = $('tocList');
  list.innerHTML = '';
  try {
    const rows = await api.outline(S.docId, S.vid, 3);
    $('tocCount').textContent = rows.length ? `${rows.length}` : '';
    for (const r of rows) {
      const b = document.createElement('button');
      b.className = 'toc-item';
      b.title = `${r.text || ''}\n${r.translation || ''}`;
      const kind = r.kind || 'heading';
      const depth = kind === 'heading' ? (r.depth || 1) : 1;
      b.dataset.page = String(r.page); b.dataset.seg = String(r.seg_id);
      b.dataset.depth = String(depth);
      b.textContent = r.text || '(无标题)';
      const pg = document.createElement('span'); pg.className = 'pg'; pg.textContent = `p.${r.page}`;
      b.appendChild(pg);
      b.onclick = () => { gotoPage(r.page); selectSeg(r.seg_id, { origin: null, scroll: true, force: true }); };
      list.appendChild(b);
    }
    if (!rows.length) list.innerHTML = '<div class="empty" style="padding:16px">（无目录/大纲）</div>';
  } catch (e) { list.innerHTML = `<div class="empty" style="padding:16px">目录不可用</div>`; }
}

function highlightToc() {
  for (const b of document.querySelectorAll('.toc-item')) {
    b.classList.toggle('active', +b.dataset.page === S.page);
  }
}

/* ---------------- 顶部徽章 ---------------- */
function renderBadges() {
  const box = $('changeBadges');
  box.innerHTML = '';
  const defs = [['added', '新增'], ['modified', '修改'], ['moved', '移动'], ['removed', '删除'], ['reordered', '重排']];
  if (Object.keys(S.counts).length) {
    box.title = '本版与上一版的差异统计';
    for (const [k, label] of defs) {
      if (!S.counts[k]) continue;
      const s = document.createElement('span');
      s.className = `badge ${k}`;
      s.innerHTML = `<i></i>${label} ${S.counts[k]}`;
      box.appendChild(s);
    }
  }
}

/* ---------------- 页面外壳 ---------------- */
function shellWidth(side) {
  const sc = scrollS(side);
  return Math.max(240, sc.clientWidth - 20);
}
function computeScale(side) {
  const m = S.metric;
  return R.computeScale(m.w, m.unitsPerPx, S.zoom, shellWidth(side));
}
function buildShells() {
  for (const side of ['source', 'cn']) {
    const host = pagesS(side);
    host.innerHTML = '';
    const scale = computeScale(side);
    for (let p = 1; p <= S.pageCount; p++) {
      const el = R.createPageEl({
        pageNo: p, wPt: S.metric.w, hPt: S.metric.h, scale,
        imgSrc: null, withCanvas: side === 'cn',
      });
      el.classList.add('loading');
      host.appendChild(el);
      const img = R.pageImg(el);
      img.addEventListener('load', () => el.classList.remove('loading'));
      img.addEventListener('error', () => {
        el.classList.remove('loading');
        if (!el.querySelector('.imgerr')) {
          const d = document.createElement('div');
          d.className = 'imgerr';
          d.style.cssText = 'position:absolute;inset:0;display:grid;place-items:center;color:#94a3b8;font-size:12px';
          d.textContent = '页面图不可用 / 生成中…';
          el.appendChild(d);
        }
      });
    }
  }
}

/* ---------------- 惰性渲染 ---------------- */
function visibleRange(side) {
  const sc = scrollS(side);
  const pagesEl = pagesS(side);
  const kids = pagesEl.children;
  if (!kids.length) return [1, 1];
  const top = sc.scrollTop, bot = top + sc.clientHeight;
  let first = 1, last = S.pageCount, seen = false;
  for (let i = 0; i < kids.length; i++) {
    const t = relTop(kids[i], sc), b = t + kids[i].offsetHeight;
    if (b >= top && !seen) { first = i + 1; seen = true; }
    if (t <= bot) last = i + 1;
  }
  return [Math.max(1, first - 2), Math.min(S.pageCount, last + 2)];
}

async function ensureWindow(side) {
  if (!S.ready && !S.docId) return;
  const [lo, hi] = visibleRange(side);
  const tasks = [];
  for (let p = lo; p <= hi; p++) tasks.push(ensurePage(side, p));
  await Promise.all(tasks);
}

async function ensurePage(side, p, force = false) {
  const rec = recOf(p);
  const flag = side === 'cn' ? 'cnPaint' : 'srcPaint';
  try {
    if (!rec.data) await ensureData(p, force);
    const m = rec.data?.markers;
    if (m) adoptMetric(m);
    if (side === 'cn' && (!rec.layout || force)) await ensureLayout(p, force);
    // ⚠️ 关键：只有**真正绘制成功**才置位 flag。
    // paintSide 在 rec.data 未就绪时会提前返回（不设 img.src），
    // 若此时就置 flag，后续每次滚动进来都会因 `!rec[flag]` 为 false 而跳过重绘，
    // 该页会永久空白（实测：快速跳页/滚动时 CN 栏约 1/3 的页命中此路径，img.src 始终为 ''）。
    if (force || !rec[flag]) {
      if (paintSide(side, p, force)) rec[flag] = true;
    } else if (!R.pageImg(pageEl(side, p))?.src) {
      // 已置位但底图仍无 src（历史遗留/被 force 清过）→ 补一次
      if (paintSide(side, p, true)) rec[flag] = true;
    }
    if (side === 'cn') ensureGhosts(p).catch(() => {});
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return;   // 未生成的页面图/版式
    showError(`第 ${p} 页加载失败：${e.message}`);
  }
}

async function ensureData(p, force = false) {
  const rec = recOf(p);
  if (rec.data && !force) return rec.data;
  if (rec.dataP) return rec.dataP;
  rec.dataP = (async () => {
    const [markers, segs] = await Promise.all([
      api.markers(S.docId, S.vid, p),
      api.segments(S.docId, S.vid, { page: p }).catch(() => []),
    ]);
    for (const s of segs) S.segById.set(s.seg_id, s);
    rec.data = { markers, segs };
    return rec.data;
  })();
  try { return await rec.dataP; } catch (e) { rec.dataP = null; throw e; }
}

async function ensureLayout(p, force = false) {
  const rec = recOf(p);
  if (rec.layout && !force) return rec.layout;
  if (rec.layoutP && !force) return rec.layoutP;
  rec.layoutP = (async () => {
    try { rec.layout = await api.translatedLayout(S.docId, S.vid, p); }
    catch { rec.layout = { page: p, width: S.metric.w, height: S.metric.h, font_file: '', boxes: [] }; }
    return rec.layout;
  })();
  return rec.layoutP;
}

function adoptMetric(m) {
  if (!m || !m.width || !m.height) return;
  const changed = Math.abs(m.width - S.metric.w) > 0.5 || Math.abs(m.height - S.metric.h) > 0.5
    || Math.abs((m.units_per_px || 0) - S.metric.unitsPerPx) > 1e-6;
  S.metric = { w: m.width, h: m.height, unitsPerPx: m.units_per_px || (72 / (m.dpi || S.dpi)), dpi: m.dpi || S.dpi, crop: m.crop_box };
  if (changed && S.pageCount > 1) rescaleAll();
}

/* ---------------- 单页绘制 ---------------- */
function paintSide(side, p, force = false) {
  const rec = recOf(p);
  const el = pageEl(side, p);
  // 返回是否真正绘制完成；调用方据此决定是否置位 paint-flag（见 ensurePage 注释）
  if (!el || !rec.data) return false;
  const scale = computeScale(side);
  el.style.width = `${(S.metric.w * scale).toFixed(2)}px`;
  el.style.height = `${(S.metric.h * scale).toFixed(2)}px`;
  const img = R.pageImg(el);
  if (!img.src || force || img.dataset.rev !== String(S.imgRev)) {
    const base = api.pageImageUrl(S.docId, S.vid, p, { kind: side === 'cn' ? 'cn' : 'source', dpi: S.dpi });
    img.src = `${base}${base.includes('?') ? '&' : '?'}rev=${S.imgRev}`;
    img.dataset.rev = String(S.imgRev);
  }
  if (side === 'cn' && rec.layout) repaintCn(el, p, rec, scale);
  buildHots(side, p);
  if (side === 'cn') { rec.cnPaint = true; } else { rec.srcPaint = true; }
  return true;
}

/** cn 栏 canvas：严格按 translated-layout 逐行绘制译文；未翻译段画原文淡色占位。
 *  （cn 页面图只提供背景，正文已由服务端擦除，两层不会重影。） */
function repaintCn(el, p, rec, scale) {
  const boxSegs = new Set((rec.layout.boxes || []).map((b) => b.seg_id));
  const pending = (rec.data?.segs || [])
    .filter((s) => s.role === 'body' && !boxSegs.has(s.seg_id))
    .map((s) => ({ text: s.text, bbox: s.bbox, size: (s.style || {}).size || 10 }));
  R.paintCanvas(R.pageCanvas(el), rec.layout, scale, { pending });
}

function buildHots(side, p) {
  const rec = recOf(p);
  const el = pageEl(side, p);
  if (!el || !rec.data) return;
  const layer = R.pageLayer(el);
  const scale = computeScale(side);
  const crop = rec.data.markers.crop_box || [0, 0, 0, 0];
  layer.innerHTML = '';
  const frag = document.createDocumentFragment();
  const mk = rec.data.markers;
  const segById = new Map((rec.data.segs || []).map((s) => [s.seg_id, s]));
  const boxBySeg = new Map((rec.layout?.boxes || []).map((b) => [b.seg_id, b]));

  for (const it of mk.items || []) {
    const seg = segById.get(it.seg_id);
    const status = seg?.translation?.status;
    let bbox = it.bbox;
    if (side === 'cn') {
      const box = boxBySeg.get(it.seg_id);
      if (box) bbox = box.bbox || box.paragraph_rect || it.bbox;
    }
    const item = {
      ...it, bbox, page: p,
      subtle: it.role && it.role !== 'body',
      label: seg ? `${seg.text}` : '',
    };
    if (side === 'cn' && it.role === 'body') {
      const box = boxBySeg.get(it.seg_id);
      if (!box && (!seg || !seg.translation || status === 'missing' || status === 'failed')) item.pending = true;
    }
    frag.appendChild(R.hotEl(side, item, scale, crop));
  }

  if (side === 'cn') {
    for (const g of S.ghostsByPage.get(p) || []) {
      frag.appendChild(R.hotEl('cn', { ...g, page: p, ghost: true, change_kind: 'removed', label: g.text }, scale, crop));
    }
  }
  layer.appendChild(frag);
  applyFilter();
  applySelectionMarks();
}

/** 重建热区后必须重放当前选中态，否则滚动/惰性渲染会把高亮擦掉 */
function applySelectionMarks() {
  if (!S.seg) return;
  const segId = String(S.seg);
  const all = [...document.querySelectorAll(`.hot[data-seg="${segId}"]`)];
  for (const el of document.querySelectorAll('.hot.sel, .hot.peer')) {
    if (el.dataset.seg !== segId) el.classList.remove('sel', 'peer');
  }
  if (!all.length) return;
  const hasSource = all.some((e) => e.dataset.side === 'source');
  const selSide = S.selOrigin || (hasSource ? 'source' : 'cn');
  for (const el of all) {
    el.classList.toggle('sel', el.dataset.side === selSide);
    el.classList.toggle('peer', el.dataset.side !== selSide);
  }
}

/* ---------------- 变更过滤 ---------------- */
function applyFilter() {
  document.body.classList.toggle('changes-only', S.changesOnly);
  for (const h of document.querySelectorAll('.hot')) {
    if (h.dataset.ghost === '1') { h.classList.toggle('off', !S.changesOnly && false); continue; }
    const ck = h.dataset.change;
    const dim = S.changesOnly && (!ck || ck === 'unchanged');
    h.classList.toggle('off', dim);
  }
}

/* ---------------- 幽灵块（已删除段） ---------------- */
/** 按页取 diff（大文档下全量 changes 可能被截断；按页取永远完整） */
async function pageDiff(p) {
  if (S.pageDiffs.has(p)) return S.pageDiffs.get(p);
  if (!S.fromVid) return { changes: [] };
  const pr = api.diff(S.docId, S.fromVid, S.vid, { page: p })
    .catch(() => ({ changes: (S.changes || []).filter((c) => c.old_page === p || c.new_page === p) }));
  S.pageDiffs.set(p, pr);
  return pr;
}

async function ensureGhosts(p) {
  if (S.ghostLoaded.has(p) || !S.fromVid) return;
  S.ghostLoaded.add(p);
  const pd = await pageDiff(p);
  const removed = (pd.changes || []).filter((c) => c.kind === 'removed' && (c.old_page === p));
  if (!removed.length) return;
  let oldSegs = S.oldSegCache.get(p);
  try {
    if (!oldSegs) { oldSegs = await api.segments(S.docId, S.fromVid, { page: p }); S.oldSegCache.set(p, oldSegs); }
  } catch { oldSegs = []; }
  const norm = (t) => String(t || '').replace(/\s+/g, ' ').trim();
  const list = [];
  let slot = 0;
  for (const c of removed) {
    let cand = null;
    if (c.old_segment_id) cand = (oldSegs || []).find((s) => s.seg_id === c.old_segment_id);
    if (!cand && c.old_text) cand = (oldSegs || []).find((s) => norm(s.text) === norm(c.old_text));
    if (!cand && c.old_text) {
      const head = norm(c.old_text).slice(0, 24);
      cand = (oldSegs || []).find((s) => head && norm(s.text).startsWith(head));
    }
    const bbox = cand?.bbox || [72, 700 + slot * 46, 523, 700 + slot * 46 + 30];
    slot += 1;
    list.push({
      seg_id: cand?.seg_id || -(1000 + (c.change_id || 0)),
      bbox, text: cand?.text || c.old_text || '', change_id: c.change_id,
      change_kind: 'removed',
    });
  }
  S.ghostsByPage.set(p, list);
  if (pageEl('cn', p)) buildHots('cn', p);
}

/* ---------------- 缩放 ---------------- */
function rescaleAll() {
  for (const side of ['source', 'cn']) {
    const scale = computeScale(side);
    for (const el of pagesS(side).children) {
      const w = +el.dataset.wpt, h = +el.dataset.hpt;
      el.style.width = `${(w * scale).toFixed(2)}px`;
      el.style.height = `${(h * scale).toFixed(2)}px`;
      el.dataset.scale = String(scale);
    }
    for (const [p, rec] of S.recs) {
      if (!rec.data) continue;
      const el = pageEl(side, p);
      if (!el) continue;
      if (side === 'cn' && rec.layout) repaintCn(el, p, rec, scale);
      buildHots(side, p);
    }
  }
}

/* ---------------- 滚动同步 ---------------- */
let syncing = false;
let syncTimer = null;
let rafPending = false;
function programmaticScroll(target, top) {
  syncing = true;
  target.scrollTop = Math.max(0, Math.round(top));
  clearTimeout(syncTimer);
  syncTimer = setTimeout(() => { syncing = false; }, 140);
}
function probeOffset(sc) { return Math.min(Math.max(24, sc.clientHeight * 0.22), 160); }

function anchorFrom(side) {
  const sc = scrollS(side);
  const kids = pagesS(side).children;
  if (!kids.length) return null;
  const probe = sc.scrollTop + probeOffset(sc);
  for (let i = 0; i < kids.length; i++) {
    const el = kids[i];
    const t = relTop(el, sc), h = el.offsetHeight;
    if (probe >= t && probe < t + h) {
      const within = probe - t;
      const hots = el.querySelectorAll('.hot:not(.off)');
      for (const h of hots) {
        const ht = parseFloat(h.style.top), hh = parseFloat(h.style.height);
        if (within >= ht && within <= ht + hh) {
          return { page: +el.dataset.page, seg: +h.dataset.seg, frac: within / h, segFrac: (within - ht) / Math.max(1, hh) };
        }
      }
      return { page: +el.dataset.page, seg: null, frac: within / h, segFrac: 0 };
    }
  }
  return null;
}

function syncFrom(side) {
  if (!S.ready || syncing) return;
  if (S.mode !== 'compare') return;
  const a = anchorFrom(side);
  if (!a) return;
  const dSide = other(side), dsc = scrollS(dSide), dp = pageEl(dSide, a.page);
  if (!dp) return;
  const off = probeOffset(scrollS(side));
  let target;
  const peerHot = a.seg ? dp.querySelector(`.hot[data-seg="${a.seg}"]`) : null;
  if (peerHot) {
    target = relTop(peerHot, dsc) + a.segFrac * parseFloat(peerHot.style.height) - off;
  } else {
    target = relTop(dp, dsc) + a.frac * dp.offsetHeight - off;
  }
  if (Math.abs(dsc.scrollTop - target) < 2) return;
  programmaticScroll(dsc, target);
}

function onScroll(side) {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(async () => {
    rafPending = false;
    const a = anchorFrom(side);
    if (a && a.page !== S.page) {
      // 深链保护期内不要被"可见页"反推覆盖（见 loadDoc 里 deepControl 的说明）；
      // 用户首次真实输入后 deepControl 交还，恢复正常跟随滚动更新页码。
      if (S.deepControl) { ensureWindow(side).catch(() => {}); return; }
      S.page = a.page;
      $('pageInput').value = String(S.page);
      highlightToc();
      syncUrl(true);
    }
    if (!syncing) syncFrom(side);
    ensureWindow(side).catch(() => {});
    ensureWindow(other(side)).catch(() => {});
  });
}

/* ---------------- 选中 / 联动高亮 ---------------- */
function clearMarks() {
  for (const el of document.querySelectorAll('.hot.sel, .hot.peer')) el.classList.remove('sel', 'peer', 'flash');
}
function centerOn(node, sc) {
  const h = parseFloat(node.style.height) || node.offsetHeight;
  const target = relTop(node, sc) + h / 2 - sc.clientHeight / 2;
  programmaticScroll(sc, target);
}

async function selectSeg(segId, { origin = null, scroll = true, force = false } = {}) {
  segId = +segId;
  if (!segId) return;
  if (!force && S.seg === segId && origin === null) { /* keep */ }
  S.seg = segId;
  S.selOrigin = origin;
  const hits = [...document.querySelectorAll(`.hot[data-seg="${segId}"]`)];
  // 若该段尚未渲染，先确保所在页可见
  if (!hits.length && !origin) {
    const seg = S.segById.get(segId);
    if (seg && seg.page !== S.page) { gotoPage(seg.page, { silent: true }); await ensureWindow('source'); await ensureWindow('cn'); }
  }
  applySelectionMarks();
  if (scroll) {
    const from = origin || 'source';
    const all = [...document.querySelectorAll(`.hot[data-seg="${segId}"]`)];
    const peer = all.find((e) => e.dataset.side !== from) || all[0];
    if (peer) {
      ensureWindow(peer.dataset.side).then(() => {
        const p2 = document.querySelector(`.hot[data-seg="${segId}"][data-side="${peer.dataset.side}"]`);
        if (p2) {
          centerOn(p2, scrollS(peer.dataset.side));
          p2.classList.remove('flash');
          void p2.offsetWidth;
          p2.classList.add('flash');
          setTimeout(() => p2.classList.remove('flash'), 700);
        }
      }).catch(() => {});
    }
  }
  updateDetail(segId);
  syncUrl(true);
}

/* ---------------- 详情面板 ---------------- */
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
const escapeHtml = esc;

const STATUS_ZH = {
  missing: '⏳ 待翻译', machine: '机器翻译', carried: '沿用旧译', seeded: '基于旧译修订',
  reviewed: '已复核', failed: '翻译失败',
};
const CHANGE_ZH = { added: '新增', modified: '修改', removed: '删除', moved: '移动', reordered: '重排', split: '拆分', merged: '合并', unchanged: '未变' };
const KIND_ZH = {
  heading: '标题', paragraph: '正文', list_item: '列表项', table_cell: '表格格', caption: '图注',
  header: '页眉', footer: '页脚', page_num: '页码', other: '其他',
};

async function updateDetail(segId) {
  const box = $('detail');
  box.hidden = false;
  let seg = S.segById.get(segId);
  if (!seg) {
    const host = [...document.querySelectorAll(`.hot[data-seg="${segId}"]`)][0];
    const p = host ? +host.dataset.page : S.page;
    await ensureData(p).catch(() => {});
    seg = S.segById.get(segId);
  }
  const ghost = !seg;
  const gh = ghost ? [...S.ghostsByPage.values()].flat().find((g) => g.seg_id === segId) : null;
  const mkItem = seg ? markerItem(seg.page, segId) : null;
  const ck = mkItem?.change_kind || (ghost ? 'removed' : null);
  // 变更详情可能不在全量列表里（大 diff 会被截断）→ 再从该页的 diff 里找一次
  let change = mkItem?.change_id != null ? S.changeById.get(mkItem.change_id) : null;
  if (!change && seg && S.fromVid) {
    const pd = await pageDiff(seg.page).catch(() => null);
    change = (pd?.changes || []).find((c) => c.change_id === mkItem?.change_id)
      || (pd?.changes || []).find((c) => c.new_segment_id === segId)
      || null;
  }
  if (!change && ghost && gh) {
    const pd = await pageDiff(S.page).catch(() => null);
    change = (pd?.changes || []).find((c) => c.change_id === gh.change_id) || null;
  }

  $('detailKind').textContent = ghost ? '已删除段落' : (KIND_ZH[seg.kind] || seg.kind || '段落');
  $('detailPage').textContent = `p.${seg ? seg.page : (gh?.bbox ? (change?.old_page ?? '?') : '?')}`;
  $('detailSegId').textContent = `#${segId}`;
  const badges = $('detailBadges');
  badges.innerHTML = '';
  if (ck) {
    const b = document.createElement('span');
    b.className = `badge ${ck}`;
    b.innerHTML = `<i></i>${CHANGE_ZH[ck] || ck}${change && change.ratio != null ? ` · 相似度 ${(change.ratio * 100).toFixed(0)}%` : ''}`;
    badges.appendChild(b);
  }
  if (seg?.role && seg.role !== 'body') {
    const b = document.createElement('span'); b.className = 'badge'; b.textContent = `role: ${seg.role}`; badges.appendChild(b);
  }

  $('detailSrc').textContent = seg ? seg.text : (gh?.text || change?.old_text || '(无原文)');
  const st = seg?.translation?.status || (ghost ? 'missing' : 'missing');
  const stEl = $('detailStatus');
  stEl.textContent = STATUS_ZH[st] || st;
  stEl.className = `status ${st}`;
  $('detailTrans').value = seg?.translation?.text || '';
  $('detailTrans').disabled = false;
  $('detailTrans').placeholder = ghost ? '（该段在新版中已被删除）' : '（未翻译 — 点「翻译本段」）';

  const dOld = change?.old_text ?? '', dNew = change?.new_text ?? '';
  const showDiff = change && ['modified', 'split', 'merged', 'reordered'].includes(change.kind) && (dOld || dNew);
  $('fieldDiff').hidden = !showDiff;
  if (showDiff) {
    const ops = change.char_diffs && change.char_diffs.length ? change.char_diffs : wordDiff(dOld, dNew);
    $('detailDiff').innerHTML = ops.map((o) => o.op === 'insert' ? `<span class="ins">${esc(o.text)}</span>`
      : o.op === 'delete' ? `<span class="del">${esc(o.text)}</span>` : esc(o.text)).join('');
  }

  const hist = $('detailHistory');
  const kvs = [];
  if (seg?.translation) {
    kvs.push(`状态 <b>${esc(STATUS_ZH[seg.translation.status] || seg.translation.status)}</b>`);
    kvs.push(`revision <b>${seg.translation.revision ?? '–'}</b>`);
  } else kvs.push('尚无译文');
  kvs.push(`kind <b>${esc(seg?.kind || '–')}</b>`);
  if (change) {
    kvs.push(`change <b>${esc(CHANGE_ZH[change.kind] || change.kind)}</b>`);
    if (change.ratio != null) kvs.push(`ratio <b>${(+change.ratio).toFixed(2)}</b>`);
    if (change.old_page && change.new_page && change.old_page !== change.new_page) kvs.push(`p.${change.old_page} → p.${change.new_page}`);
  }
  kvs.push(`版本 <b>${esc(S.versions.find((v) => v.version_id === S.vid)?.label || S.vid)}</b>`);
  hist.innerHTML = kvs.map((k) => `<span class="kv">${k}</span>`).join('');
  $('detailTranslate').disabled = ghost;
  $('detailSave').disabled = false;
}

function markerItem(page, segId) {
  const rec = S.recs.get(page);
  const it = rec?.data?.markers?.items?.find((x) => x.seg_id === segId);
  if (it) return it;
  for (const [, r] of S.recs) {
    const f = r.data?.markers?.items?.find((x) => x.seg_id === segId);
    if (f) return f;
  }
  return null;
}

/** 轻量词级 diff（服务端没给 char_diffs 时的兜底） */
function wordDiff(a, b) {
  const tok = (s) => String(s || '').match(/[\u4e00-\u9fff]|[A-Za-z0-9_.\-]+|\s+|[^\s]/g) || [];
  const A = tok(a), B = tok(b);
  if (A.length * B.length > 250000) return [{ op: 'delete', text: a }, { op: 'insert', text: b }];
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  const push = (op, text) => { if (!text) return; const last = out[out.length - 1]; if (last && last.op === op) last.text += text; else out.push({ op, text }); };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { push('equal', A[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push('delete', A[i]); i++; }
    else { push('insert', B[j]); j++; }
  }
  while (i < n) { push('delete', A[i]); i++; }
  while (j < m) { push('insert', B[j]); j++; }
  return out;
}

/* ---------------- 页面跳转 ---------------- */
function gotoPage(p, { silent = false } = {}) {
  p = Math.min(Math.max(1, +p || 1), S.pageCount);
  S.page = p;
  $('pageInput').value = String(p);
  $('pageCount').textContent = `/ ${S.pageCount}`;
  highlightToc();
  for (const side of ['source', 'cn']) {
    const el = pageEl(side, p);
    if (!el) continue;
    programmaticScroll(scrollS(side), relTop(el, scrollS(side)) - 10);
  }
  ['source', 'cn'].forEach((s) => ensureWindow(s).catch(() => {}));
  if (!silent) syncUrl(true);
}

/* ---------------- 任务轮询 ---------------- */
async function pollJob(id, label) {
  const bar = $('progressBar'), txt = $('progressText');
  $('progress').hidden = false;
  const t0 = Date.now();
  for (;;) {
    let j;
    try { j = await api.job(id); } catch (e) { $('progress').hidden = true; throw e; }
    const pct = j.total ? Math.round((j.progress || 0) / j.total * 100) : (j.progress || 0);
    bar.style.width = `${Math.min(100, pct)}%`;
    txt.textContent = `${label} ${pct}% ${j.message || ''}`;
    if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') {
      $('progress').hidden = true;
      if (j.status === 'failed') throw new ApiError(j.error || `${label}失败`, 0);
      const secs = ((Date.now() - t0) / 1000).toFixed(1);
      const r = j.result || {};
      toast(`${label}完成 (${secs}s) ${JSON.stringify(r).slice(0, 120)}`, 'ok', 5000);
      return j;
    }
    await new Promise((r) => setTimeout(r, IS_MOCK ? 200 : 700));
  }
}

function refreshPages(pages) {
  for (const p of pages) {
    const rec = recOf(p);
    rec.data = null; rec.dataP = null; rec.layout = null; rec.layoutP = null;
    rec.srcPaint = false; rec.cnPaint = false;
  }
}

/* ---------------- 顶栏动作 ---------------- */
async function doTranslate(pages) {
  try {
    clearError();
    const body = pages ? { doc_id: S.docId, version_id: S.vid, segment_ids: null, page_from: pages[0], page_to: pages[1], provider: IS_MOCK ? 'mock' : null } : { doc_id: S.docId, version_id: S.vid, segment_ids: null, page_from: 1, page_to: S.pageCount, provider: IS_MOCK ? 'mock' : null };
    const { job_id } = await api.translate(body);
    toast(`翻译任务已提交 #${job_id}`, '', 2000);
    await pollJob(job_id, '翻译');
    S.imgRev += 1;
    refreshPages(pages ? Array.from({ length: pages[1] - pages[0] + 1 }, (_, i) => pages[0] + i) : [...Array(S.pageCount).keys()].map((i) => i + 1));
    S.segById.clear();
    await Promise.all([ensureWindow('source'), ensureWindow('cn')]);
    if (S.seg) updateDetail(S.seg);
  } catch (e) { showError(`翻译失败：${e.message}`); }
}

async function doExport(kind) {
  try {
    clearError();
    const r = await api.exportPdf(S.docId, S.vid, kind);
    let path = r.path, bytes = r.bytes;
    if (r.job_id) {
      const j = await pollJob(r.job_id, `导出${kind === 'cn' ? '中文' : '双语'} PDF`);
      path = j.result?.path; bytes = j.result?.bytes;
    }
    const label = kind === 'cn' ? '中文 PDF' : '双语 PDF';
    if (IS_MOCK) { toast(`${label} (mock) → ${path}`, 'ok'); return; }
    toast(`${label} 完成 ${path || ''} ${bytes ? '(' + (bytes / 1024 / 1024).toFixed(2) + ' MB)' : ''} — 正在下载…`, 'ok', 5000);
    const a = document.createElement('a');
    a.href = api.downloadUrl(S.docId, S.vid, kind);
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
  } catch (e) { showError(`导出失败：${e.message}`); }
}

async function openGlossary() {
  try {
    const rows = await api.glossary();
    $('modalTitle').textContent = `术语表 (${rows.length})`;
    $('modalBody').innerHTML = `<table class="glossary"><thead><tr><th>English</th><th>中文</th><th>区分大小写</th><th>备注</th></tr></thead><tbody>`
      + rows.map((r) => `<tr><td><code>${esc(r.en)}</code></td><td>${esc(r.zh)}</td><td>${r.case_sensitive ? '是' : '否'}</td><td class="muted">${esc(r.note || '')}</td></tr>`).join('')
      + '</tbody></table>';
    $('modal').hidden = false;
  } catch (e) { showError(`术语表加载失败：${e.message}`); }
}

/* ---------------- 键盘 ---------------- */
async function moveSeg(delta) {
  if (!S.seg) { if (S.segById.size) { const first = [...S.segById.values()].sort((a, b) => a.page - b.page || a.order_index - b.order_index)[0]; selectSeg(first.seg_id, { origin: null, scroll: true }); } return; }
  const seg = S.segById.get(S.seg);
  if (!seg) return;
  await ensurePage('source', seg.page);
  const rec = S.recs.get(seg.page);
  const list = (rec?.data?.segs || []).filter((s) => s.role === 'body').sort((a, b) => a.order_index - b.order_index);
  let i = list.findIndex((s) => s.seg_id === S.seg);
  if (i < 0) i = 0;
  let next = list[i + delta];
  if (!next) {
    const np = seg.page + (delta > 0 ? 1 : -1);
    if (np >= 1 && np <= S.pageCount) {
      await ensurePage('source', np);
      const l2 = (S.recs.get(np)?.data?.segs || []).filter((s) => s.role === 'body').sort((a, b) => a.order_index - b.order_index);
      next = delta > 0 ? l2[0] : l2[l2.length - 1];
    }
  }
  if (next) selectSeg(next.seg_id, { origin: 'source', scroll: true });
}

function bindKeys() {
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag)) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSeg(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSeg(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); gotoPage(S.page + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); gotoPage(S.page - 1); }
    else if (e.key === 'd' || e.key === 'D') { $('changesOnly').checked = !S.changesOnly; $('changesOnly').dispatchEvent(new Event('change')); }
  });
}

/* ---------------- 事件绑定 ---------------- */
function bindUi() {
  $('errorClose').onclick = clearError;
  $('modalClose').onclick = $('modalClose2').onclick = () => { $('modal').hidden = true; };
  $('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('modal').hidden = true; });
  $('detailClose').onclick = () => { $('detail').hidden = true; clearMarks(); S.seg = null; syncUrl(true); };

  $('tocToggle').onclick = () => {
    $('toc').classList.toggle('collapsed');
    setTimeout(() => { rescaleAll(); }, 200);
  };
  for (const b of $('modeBtns').querySelectorAll('button')) {
    b.onclick = () => { S.mode = b.dataset.mode; applyMode(); syncUrl(true); setTimeout(() => { rescaleAll(); ensureWindow('source'); ensureWindow('cn'); }, 30); };
  }
  $('changesOnly').onchange = (e) => { S.changesOnly = e.target.checked; applyFilter(); syncUrl(true); };
  // ⚠️ 页码输入框的 onchange **不在这里绑**（见 armPageInput 注释），
  // 由启动流程在 boot() 结束后调用 armPageInput() 才绑上。
  $('zoom').oninput = (e) => {
    S.zoom = +e.target.value / 100;
    $('zoomVal').textContent = `${e.target.value}%`;
    const anchor = anchorFrom(S.mode === 'cn' ? 'cn' : 'source');
    rescaleAll();
    if (anchor) gotoPage(anchor.page, { silent: true });
  };
  $('translateBtn').onclick = () => doTranslate([S.page, S.page]);
  $('translateAllBtn').onclick = () => doTranslate(null);
  $('exportCnBtn').onclick = () => doExport('cn');
  $('exportBiBtn').onclick = () => doExport('bilingual');
  $('glossaryBtn').onclick = openGlossary;
  $('detailSave').onclick = async () => {
    if (!S.seg) return;
    const text = $('detailTrans').value;
    try {
      await api.review({ segment_id: S.seg, text, status: 'reviewed' });
      const seg = S.segById.get(S.seg);
      if (seg) seg.translation = { text, status: 'reviewed', revision: (seg.translation?.revision || 0) + 1 };
      S.imgRev += 1;
      refreshPages([seg?.page || S.page]);
      await ensurePage('cn', seg?.page || S.page, true);
      updateDetail(S.seg);
      toast('已保存并标记为「已复核」', 'ok');
    } catch (e) { showError(`保存失败：${e.message}`); }
  };
  $('detailTranslate').onclick = () => doTranslate([S.segById.get(S.seg)?.page || S.page, S.segById.get(S.seg)?.page || S.page]);

  for (const side of ['source', 'cn']) {
    const sc = scrollS(side);
    sc.addEventListener('scroll', () => onScroll(side), { passive: true });
    pagesS(side).addEventListener('click', (e) => {
      const h = e.target.closest('.hot');
      if (!h) return;
      selectSeg(+h.dataset.seg, { origin: side, scroll: true, force: true });
    });
    pagesS(side).addEventListener('mouseover', (e) => {
      const h = e.target.closest('.hot');
      if (!h || h.dataset.ghost === '1') return;
      const id = h.dataset.seg;
      for (const el of document.querySelectorAll('.hot.sel, .hot.peer')) {
        if (el.dataset.seg !== id) el.classList.remove('sel', 'peer');
      }
      for (const el of document.querySelectorAll(`.hot[data-seg="${id}"]`)) {
        el.classList.add(el.dataset.side === side ? 'sel' : 'peer');
      }
    });
  }

  // 中缝拖拽
  const g = $('gutter');
  let dragging = false;
  g.addEventListener('mousedown', (e) => { dragging = true; g.classList.add('drag'); e.preventDefault(); });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = $('panes').getBoundingClientRect();
    const r = Math.min(0.85, Math.max(0.15, (e.clientX - rect.left) / rect.width));
    $('pane-source').style.flex = `0 0 ${(r * 100).toFixed(2)}%`;
    $('pane-cn').style.flex = `1 1 auto`;
    S.paneRatio = r;
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; g.classList.remove('drag');
    try { localStorage.setItem('mt.paneRatio', String(S.paneRatio)); } catch { /* ignore */ }
    rescaleAll();
    ensureWindow('source'); ensureWindow('cn');
  });
  g.addEventListener('dblclick', () => {
    $('pane-source').style.flex = '1 1 50%'; S.paneRatio = 0.5;
    try { localStorage.setItem('mt.paneRatio', '0.5'); } catch { /* ignore */ }
    rescaleAll();
  });
  const saved = parseFloat(localStorage.getItem('mt.paneRatio') || '');
  if (saved > 0.15 && saved < 0.85) {
    S.paneRatio = saved;
    $('pane-source').style.flex = `0 0 ${(saved * 100).toFixed(2)}%`;
    $('pane-cn').style.flex = '1 1 auto';
  }

  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { rescaleAll(); ensureWindow('source'); ensureWindow('cn'); }, 180); });
}

function applyMode() {
  document.body.classList.remove('mode-compare', 'mode-source', 'mode-cn');
  document.body.classList.add(`mode-${S.mode}`);
  for (const b of $('modeBtns').querySelectorAll('button')) b.classList.toggle('on', b.dataset.mode === S.mode);
}

/* ---------------- 启动 ---------------- */
/** 把页码输入框的 onchange 绑上（**boot 之后**才调，见 bindUi 注释）。 */
function armPageInput() {
  const el = $('pageInput');
  if (!el || el.dataset.armed === '1') return;
  el.dataset.armed = '1';
  el.onchange = (e) => { clearDeepPage(); gotoPage(+e.target.value); };
}

(async function main() {
  // ⚠️ 必须关掉浏览器的**滚动位置恢复**。它是深链失效的根因：
  // SPA 导航到 ?doc=2&page=351 后，浏览器会把上一页的 scrollTop 恢复回来，
  // 触发我们的滚动监听 -> 监听里按"当前可见页"反推页码 -> 覆盖掉 URL 指定的页。
  // 实测同一 URL 会随机落到 351 / 360 / 308 / 1 页，非常难查。
  // 页面位置一律以 URL 的 page 参数为唯一真相。
  try { if ('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch { /* ignore */ }
  window.addEventListener('error', (e) => showError(`脚本错误: ${e.message}`));
  window.addEventListener('unhandledrejection', (e) => showError(`请求失败: ${e.reason && e.reason.message || e.reason}`));
  bindUi();
  bindKeys();
  await R.ensureFonts(api.fontFaceSpecs());
  // mock 模式先预热 data.json，pageImageUrl 才能解析桩图路径
  if (IS_MOCK) { try { await api.health(); await api.documents(); } catch (e) { showError(e.message); } }
  await boot();
  // boot 完成后才武装页码输入框 —— Chrome 的表单恢复事件在这个时间点之前跑完，
  // 就不会再用上一页的数字把深链覆盖掉。之后再把 URL 里的 page 校正一次。
  armPageInput();
  const u = readUrl();
  if (u.page && S.page !== Math.min(Math.max(1, u.page), S.pageCount)) {
    gotoPage(Math.min(Math.max(1, u.page), S.pageCount), { silent: true });
  }
})();
