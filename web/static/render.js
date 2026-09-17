/* ============================================================
 * render.js — 页面图 / canvas 译文层 / 热区层
 *
 * 坐标：服务端一律 PDF point（原点左上）。
 *   units_per_px = 72 / dpi
 *   页面自然像素宽 = width / units_per_px
 *   scale(S) = (fit × zoom) / units_per_px   ← point → CSS px
 * canvas 严格按 /translated-layout 的 lines 逐行绘制，**不再换行**。
 * ============================================================ */

export const CHANGE_COLORS = {
  added: '#22c55e', modified: '#f59e0b', moved: '#a855f7', removed: '#ef4444',
  reordered: '#38bdf8', split: '#14b8a6', merged: '#eab308',
};

export const FONT_CJK = '"MTCJK", "DengXian", "Microsoft YaHei", "SimHei", "Noto Sans SC", sans-serif';
export const FONT_LATIN = '"MTLatin", "Calibri", "Segoe UI", Arial, sans-serif';
export const FONT_LATIN_BOLD = '"MTLatinB", "Calibri", "Segoe UI", Arial, sans-serif';

let fontsReady = null;

/** 注入服务端字体（保证 canvas 字形与 PDF 一致）；失败则退回系统字体。 */
export function ensureFonts(specs) {
  if (fontsReady) return fontsReady;
  fontsReady = (async () => {
    if (!specs || !specs.length) return false;
    try {
      const css = specs.map((s) => `@font-face{font-family:"${s.family}";src:url("${s.url}") format("truetype");font-weight:${s.weight || 400};font-display:block;}`).join('\n');
      const st = document.createElement('style');
      st.textContent = css;
      document.head.appendChild(st);
      await Promise.all(specs.map((s) => document.fonts.load(`${s.weight === '700' ? 'bold ' : ''}16px "${s.family}"`, '中文Aa1')));
      await document.fonts.ready;
      return document.fonts.check(`16px "${specs[0].family}"`, '中');
    } catch (e) {
      console.warn('字体注入失败，使用系统字体', e);
      return false;
    }
  })();
  return fontsReady;
}

function fontFor(slot, sizePx) {
  if (slot === 'latin') return `${sizePx.toFixed(2)}px ${FONT_LATIN}`;
  if (slot === 'latin_bold') return `700 ${sizePx.toFixed(2)}px ${FONT_LATIN_BOLD}`;
  return `${sizePx.toFixed(2)}px ${FONT_CJK}`;
}

export const cssColor = (v, fallback = '#1a1a1a') => {
  if (v === null || v === undefined) return fallback;
  if (typeof v === 'string') return v.startsWith('#') ? v : `#${v}`;
  return `#${(v & 0xffffff).toString(16).padStart(6, '0')}`;
};

/**
 * 创建一页的外壳。
 * @param {{pageNo:number,wPt:number,hPt:number,scale:number,imgSrc:string|null,
 *          withCanvas:boolean,dpi?:number}} o
 */
export function createPageEl(o) {
  const el = document.createElement('div');
  el.className = 'page';
  el.dataset.page = String(o.pageNo);
  el.style.width = `${(o.wPt * o.scale).toFixed(2)}px`;
  el.style.height = `${(o.hPt * o.scale).toFixed(2)}px`;
  el.dataset.wpt = String(o.wPt);
  el.dataset.hpt = String(o.hPt);
  el.dataset.scale = String(o.scale);

  const img = document.createElement('img');
  img.className = 'pimg';
  img.decoding = 'async';
  img.alt = `page ${o.pageNo}`;
  if (o.imgSrc) img.src = o.imgSrc;
  el.appendChild(img);

  if (o.withCanvas) {
    const cv = document.createElement('canvas');
    cv.className = 'tcanvas';
    el.appendChild(cv);
  }
  const layer = document.createElement('div');
  layer.className = 'hotlayer';
  el.appendChild(layer);

  const pn = document.createElement('span');
  pn.className = 'pnum';
  pn.textContent = `p.${o.pageNo}`;
  el.appendChild(pn);
  return el;
}

export const pageImg = (el) => el.querySelector('img.pimg');
export const pageCanvas = (el) => el.querySelector('canvas.tcanvas');
export const pageLayer = (el) => el.querySelector('.hotlayer');

/** 按 translated-layout 逐行绘制；不换行、不缩放字号以外的任何东西。
 *  `pending` = 未翻译段 [{text,bbox,size}]：cn 背景图已擦除正文，这里画「原文淡色占位」。 */
export function paintCanvas(canvas, layout, scale, { paintBg = true, pending = [] } = {}) {
  if (!canvas || !layout) return;
  const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  const w = layout.width * scale;
  const h = layout.height * scale;
  canvas.style.width = `${w.toFixed(2)}px`;
  canvas.style.height = `${h.toFixed(2)}px`;
  const bw = Math.max(1, Math.round(w * dpr));
  const bh = Math.max(1, Math.round(h * dpr));
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';

  for (const box of layout.boxes || []) {
    const b = box.bbox || box.paragraph_rect;
    if (!b) continue;
    if (paintBg && box.bg !== null && box.bg !== undefined) {
      ctx.fillStyle = cssColor(box.bg, '#ffffff');
      ctx.fillRect(b[0] * scale - 0.5, b[1] * scale - 0.5, (b[2] - b[0]) * scale + 1, (b[3] - b[1]) * scale + 1);
    }
    ctx.fillStyle = cssColor(box.color, '#1a1a1a');
    for (const ln of box.lines || []) {
      if (!ln.text) continue;
      ctx.font = fontFor(ln.font_slot, (ln.size || 10) * scale);
      ctx.fillText(ln.text, ln.x * scale, ln.y * scale);
    }
  }

  // 未翻译段：原文淡色占位（明确是占位，不参与与 PDF 的一致性约束）
  for (const p of pending) {
    const b = p.bbox;
    if (!b) continue;
    const size = Math.max(6, (p.size || 10) * 0.92 * scale);
    ctx.font = `${size.toFixed(2)}px ${FONT_LATIN}`;
    ctx.fillStyle = 'rgba(100,116,139,0.55)';
    const maxW = (b[2] - b[0]) * scale;
    let y = b[1] * scale + size;
    let line = '';
    for (const tok of String(p.text || '').split(/(\s+)/)) {
      const trial = line + tok;
      if (ctx.measureText(trial).width > maxW && line.trim()) {
        ctx.fillText(line.trimEnd(), b[0] * scale, y);
        y += size * 1.24;
        line = tok.trimStart();
        if (y > (b[3] * scale) + size * 1.3) break;
      } else line = trial;
    }
    if (line.trim() && y <= (b[3] * scale) + size * 1.3) ctx.fillText(line.trimEnd(), b[0] * scale, y);
  }
}

/**
 * 建一个热区元素。item: {seg_id,bbox,kind,role,change_kind,change_id,pending,ghost,label}
 */
export function hotEl(side, item, scale, crop = [0, 0, 0, 0]) {
  const b = item.bbox;
  const el = document.createElement('div');
  el.className = 'hot' + (item.pending ? ' pending' : '') + (item.ghost ? ' ghost' : '') + (item.subtle ? ' subtle' : '');
  el.dataset.seg = String(item.seg_id);
  el.dataset.side = side;
  el.dataset.page = String(item.page);
  if (item.kind) el.dataset.kind = item.kind;
  if (item.role) el.dataset.role = item.role;
  if (item.change_kind) el.dataset.change = item.change_kind;
  if (item.change_id != null) el.dataset.changeId = String(item.change_id);
  if (item.ghost) { el.dataset.ghost = '1'; el.dataset.oldPage = String(item.page); }
  el.style.left = `${((b[0] - crop[0]) * scale).toFixed(2)}px`;
  el.style.top = `${((b[1] - crop[1]) * scale).toFixed(2)}px`;
  el.style.width = `${Math.max(2, (b[2] - b[0]) * scale).toFixed(2)}px`;
  el.style.height = `${Math.max(2, (b[3] - b[1]) * scale).toFixed(2)}px`;

  const ck = item.change_kind;
  if (ck && ck !== 'unchanged') {
    const bar = document.createElement('i');
    bar.className = 'bar';
    bar.style.background = CHANGE_COLORS[ck] || 'transparent';
    el.appendChild(bar);
    const badge = document.createElement('span');
    badge.className = `rowbadge ${ck}`;
    badge.textContent = { added: '新增', modified: '修改', moved: '移动', removed: '删除', reordered: '重排', split: '拆分', merged: '合并' }[ck] || ck;
    el.appendChild(badge);
  }
  if (item.pending) {
    const ph = document.createElement('span');
    ph.className = 'ph';
    ph.textContent = '⏳ 待翻译';
    el.appendChild(ph);
  }
  if (item.ghost) {
    const gl = document.createElement('span');
    gl.className = 'ghostlabel';
    gl.textContent = `已删除 · ${item.label || ''}`.trim();
    el.appendChild(gl);
  }
  if (item.label) el.title = item.label;
  return el;
}

/** 依据 dpi / zoom / 栏宽 计算 point→CSS px 的缩放系数 */
export function computeScale(wPt, unitsPerPx, zoom, paneWidth) {
  const naturalPx = wPt / unitsPerPx;
  const fit = Math.max(0.05, (paneWidth - 22) / naturalPx);
  return (fit * zoom) / unitsPerPx;
}
