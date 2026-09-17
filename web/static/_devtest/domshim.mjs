/* ============================================================
 * web/static/_devtest/domshim.mjs — 极简 DOM/布局/Canvas 模拟层（仅测试用）
 *
 * 目的：沙箱禁止 Chrome 启动（Mojo 命名管道被拒），无法用真实浏览器取证。
 * 本文件提供一个**足够真实**的 DOM 子集，让 web/static/app.js 与 render.js
 * **原样运行**，从而对「双栏联动高亮 / 滚动同步 / 变更过滤」做可执行的断言。
 *
 * 明确的能力边界（写在结果里，不含糊）：
 *   ✅ 真实执行 app.js / render.js / api.js 的逻辑与事件流（click→选段→对侧高亮→滚动目标）
 *   ✅ 真实网络请求（node fetch 打真服务）
 *   ✅ 真实 canvas 调用序列（fillText 逐行绘制可核对）
 *   ⚠️ 布局几何由本文件的简化盒模型计算（等价于 style.css 的关键规则），
 *      不是 Chromium 的 flexbox/字体排版结果。
 * ============================================================ */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

/* ---------------- CSS 选择器（子集） ---------------- */
function parseSimple(sel) {
  // 支持: tag / .cls / #id / [attr] / [attr="v"] / :not(...)，可复合
  const out = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let s = sel.trim();
  if (!s) return out;
  let i = 0;
  const re = /^([a-zA-Z][\w-]*)|^\.([\w-]+)|^#([\w-]+)|^\[([\w-]+)(?:([~^$*|]?=)"?([^\]"]*)"?)?\]|^:not\(([^)]*)\)/;
  while (s.length) {
    const m = re.exec(s);
    if (!m) break;
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.classes.push(m[2]);
    else if (m[3]) out.id = m[3];
    else if (m[4]) out.attrs.push({ name: m[4], op: m[5] || null, val: m[6] ?? null });
    else if (m[7]) out.nots.push(parseSimple(m[7]));
    s = s.slice(m[0].length);
  }
  return out;
}

function matchesSimple(el, s) {
  if (el.nodeType !== 1) return false;
  if (s.tag && el.tagName.toLowerCase() !== s.tag) return false;
  if (s.id && el.getAttribute('id') !== s.id) return false;
  for (const c of s.classes) if (!el.classList.contains(c)) return false;
  for (const a of s.attrs) {
    const v = el.getAttribute(a.name);
    if (v === null || v === undefined) return false;
    if (a.op === '=' && String(v) !== a.val) return false;
    if (a.op === '*=' && !String(v).includes(a.val)) return false;
    if (a.op === '^=' && !String(v).startsWith(a.val)) return false;
  }
  for (const n of s.nots) if (matchesSimple(el, n)) return false;
  return true;
}

export function matches(el, selector) {
  const parts = selector.split(',').map((x) => x.trim()).filter(Boolean);
  for (const part of parts) {
    const chain = part.split(/\s+/).map(parseSimple);
    // 从右往左匹配，支持后代组合
    let ok = matchesSimple(el, chain[chain.length - 1]);
    if (!ok) continue;
    let idx = chain.length - 2;
    let node = el.parentNode;
    while (idx >= 0 && node) {
      if (node.nodeType === 1 && matchesSimple(node, chain[idx])) idx--;
      node = node.parentNode;
    }
    if (idx < 0) return true;
    // 单段选择器（chain 只有 1 个）已在上方判定
    if (chain.length === 1) return true;
  }
  return false;
}

/* ---------------- 节点 ---------------- */
let UID = 1;

export class Txt {
  constructor(text) { this.nodeType = 3; this.data = String(text); this.parentNode = null; this.uid = UID++; }
  get textContent() { return this.data; }
}

export class El {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.uid = UID++;
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = new Map();
    this._classes = new Set();
    this._listeners = new Map();
    this._style = new StyleDecl();
    this._scrollTop = 0;
    this._scrollEvents = 0;
    this.hidden = false;
    const self = this;
    this.classList = {
      add(...c) { c.forEach((x) => self._classes.add(x)); },
      remove(...c) { c.forEach((x) => self._classes.delete(x)); },
      toggle(c, force) {
        const on = force === undefined ? !self._classes.has(c) : !!force;
        if (on) self._classes.add(c); else self._classes.delete(c);
        return on;
      },
      contains(c) { return self._classes.has(c); },
      get length() { return self._classes.size; },
    };
    this.dataset = new Proxy({}, {
      get: (_, k) => self.getAttribute('data-' + String(k).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())),
      set: (_, k, v) => { self.setAttribute('data-' + String(k).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase()), v); return true; },
      has: (_, k) => self.hasAttribute('data-' + String(k)),
      ownKeys: () => [...self.attrs.keys()].filter((k) => k.startsWith('data-')),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
  }

  get className() { return [...this._classes].join(' '); }
  set className(v) { this._classes = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  get style() { return this._style; }
  set style(v) { this._style = new StyleDecl(v); }
  get id() { return this.getAttribute('id') || ''; }
  get src() { return this.getAttribute('src') || ''; }
  set src(v) { this.setAttribute('src', v); if (v === '' || v == null) this.removeAttribute('src'); }
  get value() { return this._value !== undefined ? this._value : (this.getAttribute('value') || ''); }
  set value(v) { this._value = String(v); }
  get checked() { return this._checked !== undefined ? this._checked : this.hasAttribute('checked'); }
  set checked(v) { this._checked = !!v; }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { if (v) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(v) {
    const max = this.__maxScroll === undefined ? 1e9 : this.__maxScroll;
    const nv = Math.max(0, Math.min(max, Number(v) || 0));
    if (nv === this._scrollTop) return;
    this._scrollTop = nv;
    this._scrollEvents++;
    if (process.env.MT_DEBUG) {
      const st = new Error().stack.split('\n').slice(2, 5).map((s) => s.trim().replace(/^at /, '')).join(' ← ');
      console.log(`  [scrollTop] ${this.id || this.className} = ${nv.toFixed(2)} (from ${v}, max=${max.toFixed(2)}) :: ${st}`);
    }
    // 模拟浏览器：scrollTop 改变会异步派发 scroll 事件（正是回环抖动的来源）
    setTimeout(() => this.dispatchEvent(mkEvent('scroll', this)), 0);
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }

  setAttribute(k, v) { this.attrs.set(String(k).toLowerCase(), String(v)); }
  getAttribute(k) { const v = this.attrs.get(String(k).toLowerCase()); return v === undefined ? null : v; }
  hasAttribute(k) { return this.attrs.has(String(k).toLowerCase()); }
  removeAttribute(k) { this.attrs.delete(String(k).toLowerCase()); }

  appendChild(node) {
    if (!node) return node;
    if (node.nodeType === 11 || node.tagName === 'FRAGMENT') {   // DocumentFragment：把子节点搬过来
      for (const c of [...node.childNodes]) this.appendChild(c);
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    invalidate();
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; invalidate(); }
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  insertBefore(n, ref) {
    if (!ref) return this.appendChild(n);
    const i = this.childNodes.indexOf(ref);
    n.parentNode = this;
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n);
    invalidate();
    return n;
  }

  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(v) { this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = []; if (v !== '' && v != null) this.appendChild(new Txt(v)); invalidate(); }

  get innerHTML() { return serialize(this); }
  set innerHTML(html) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    for (const n of parseHtml(String(html), this.ownerDocument || DOC)) this.appendChild(n);
    invalidate();
  }

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const a = this._listeners.get(type) || [];
    const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    const chain = [];
    let n = this;
    while (n) { chain.push(n); n = n.parentNode; }
    for (const node of chain) {
      const ls = node._listeners && node._listeners.get(ev.type);
      ev.currentTarget = node;
      if (ls) for (const fn of [...ls]) { fn.call(node, ev); }
      const inline = node['on' + ev.type];
      if (typeof inline === 'function') inline.call(node, ev);
    }
    return true;
  }
  click() { this.dispatchEvent(mkEvent('click', this)); }

  closest(sel) { let n = this; while (n) { if (n.nodeType === 1 && matches(n, sel)) return n; n = n.parentNode; } return null; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.childNodes) { if (c.nodeType === 1) { if (matches(c, sel)) out.push(c); walk(c); } } };
    walk(this);
    return out;
  }
  getElementsByTagName(tag) {
    const t = String(tag).toLowerCase();
    return this.querySelectorAll(t === '*' ? '*' : t);
  }

  /* 布局查询（由 layout engine 填充 __box）；真实浏览器里布局永远是新鲜的 */
  getBoundingClientRect() {
    ensureLayout();
    const b = viewportBox(this);
    return {
      x: b.x, y: b.y, left: b.x, top: b.y, right: b.x + b.w, bottom: b.y + b.h,
      width: b.w, height: b.h,
      toJSON() { return { x: b.x, y: b.y, top: b.y, bottom: b.y + b.h, left: b.x, right: b.x + b.w, width: b.w, height: b.h }; },
    };
  }
  get offsetWidth() { ensureLayout(); return this.getBoundingClientRect().width; }
  get offsetHeight() { ensureLayout(); return this.getBoundingClientRect().height; }
  get clientWidth() {
    ensureLayout();
    const b = this.__box || { w: 0 };
    const isScroller = this._classes.has('pane-scroll') || this._classes.has('toc-list') || this._classes.has('detail-body');
    return Math.max(0, Math.round(b.w - (isScroller ? 10 : 0)));
  }
  get clientHeight() {
    ensureLayout();
    const b = this.__box || { h: 0 };
    if (this._classes.has('pane-scroll')) return Math.max(0, Math.round(b.h));
    return Math.max(0, Math.round(b.h));
  }
  get offsetTop() { ensureLayout(); const b = this.__box || { y: 0 }; const p = this.offsetParent; return p ? b.y - (p.__box ? p.__box.y : 0) : b.y; }
  get offsetParent() { let n = this.parentNode; while (n) { if (n.nodeType === 1 && (n._classes.has('pages') || n._classes.has('pane') || n.tagName === 'BODY')) return n; n = n.parentNode; } return null; }
  get ownerDocument() { return this._ownerDocument || DOC; }
  set ownerDocument(d) { this._ownerDocument = d; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === DOC.body || n === DOC.documentElement; }
}

class StyleDecl {
  constructor(text) {
    this._p = new Map();
    if (text) this.cssText = text;
  }
  setProperty(k, v) { this._p.set(String(k), String(v)); invalidate(); }
  getPropertyValue(k) { return this._p.get(String(k)) || ''; }
  set cssText(v) {
    this._p.clear();
    for (const part of String(v).split(';')) {
      const i = part.indexOf(':');
      if (i > 0) this._p.set(part.slice(0, i).trim(), part.slice(i + 1).trim());
    }
    invalidate();
  }
  get cssText() { return [...this._p].map(([k, v]) => `${k}:${v}`).join(';'); }
}
for (const k of ['width', 'height', 'left', 'top', 'right', 'bottom', 'flex', 'display', 'background', 'opacity', 'font', 'border']) {
  Object.defineProperty(StyleDecl.prototype, k, {
    get() { return this._p.get(k) || ''; },
    set(v) { this._p.set(k, String(v)); invalidate(); },
  });
}

function serialize(el) {
  if (el.nodeType === 3) return el.data;
  const attrs = [];
  if (el.id) attrs.push(`id="${el.id}"`);
  if (el.className) attrs.push(`class="${el.className}"`);
  for (const [k, v] of el.attrs) if (k !== 'id' && k !== 'class') attrs.push(`${k}="${v}"`);
  const tag = el.tagName.toLowerCase();
  const inner = el.childNodes.map(serialize).join('');
  return `<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>${inner}</${tag}>`;
}

/* ---------------- 迷你 HTML 解析 ---------------- */
const VOID_TAGS = new Set(['img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'area', 'base', 'col', 'embed', 'wbr']);

function decode(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

function parseHtml(html, doc) {
  const root = [];
  const stack = [];
  const push = (n) => { if (stack.length) stack[stack.length - 1].appendChild(n); else root.push(n); };
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { const t = html.slice(i); if (t.trim()) push(new Txt(decode(t))); break; }
    if (lt > i) { const t = html.slice(i, lt); if (t.trim()) push(new Txt(decode(t))); }
    if (html.startsWith('<!--', lt)) { i = html.indexOf('-->', lt) + 3; continue; }
    const gt = html.indexOf('>', lt);
    if (gt < 0) break;
    const raw = html.slice(lt + 1, gt).trim();
    if (raw.startsWith('/')) { if (stack.length) stack.pop(); i = gt + 1; continue; }
    const m = /^([a-zA-Z][\w-]*)([\s\S]*?)(\/?)$/.exec(raw);
    if (!m) { i = gt + 1; continue; }
    const tag = m[1].toLowerCase();
    const el = new El(tag);
    el.ownerDocumentRef = doc;
    const attrRe = /([\w:-]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    let am;
    while ((am = attrRe.exec(m[2]))) {
      const name = am[1].toLowerCase();
      const val = am[3] ?? am[4] ?? am[5] ?? '';
      if (name === 'class') el.className = val;
      else if (name === 'id') el.setAttribute('id', val);
      else if (name === 'hidden') el.hidden = true;
      else el.setAttribute(name, val);
    }
    push(el);
    if (!VOID_TAGS.has(tag) && !m[3]) stack.push(el);
    i = gt + 1;
  }
  return root;
}

/* ---------------- 事件 ---------------- */
export function mkEvent(type, target, extra = {}) {
  return { type, target, currentTarget: target, defaultPrevented: false, ...extra,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
}

/* ---------------- 布局引擎（简化盒模型） ---------------- */
export const VIEW = { w: 1440, h: 900 };
let LAYOUT_DIRTY = true;
export function invalidate() { LAYOUT_DIRTY = true; }

/** 真实浏览器里布局随样式/结构变化立即生效；这里用脏标记 + 惰性重算等价模拟 */
export function ensureLayout() {
  if (LAYOUT_DIRTY || !DOC || !DOC.__laid) relayout(DOC);
}

const px = (v, def = 0) => {
  if (v === undefined || v === null || v === '') return def;
  const m = /^(-?[\d.]+)px$/.exec(String(v));
  return m ? parseFloat(m[1]) : def;
};

export function relayout(doc) {
  LAYOUT_DIRTY = false;
  if (!doc) return;
  doc.__laid = true;
  const body = doc.body;
  if (!body) return;
  const get = (id) => doc.getElementById(id);
  const topbar = get('topbar');
  const errbar = get('errorbar');
  const errH = errbar && !errbar.hidden ? 30 : 0;
  const main = get('main');
  const toc = get('toc');
  const panes = get('panes');
  const topH = topbar ? 52 : 0;

  body.__box = { x: 0, y: 0, w: VIEW.w, h: VIEW.h };
  if (topbar) topbar.__box = { x: 0, y: errH, w: VIEW.w, h: topH };
  if (main) main.__box = { x: 0, y: errH + topH, w: VIEW.w, h: VIEW.h - errH - topH };
  const mainH = VIEW.h - errH - topH;

  const tocCollapsed = toc && toc.classList.contains('collapsed');
  const tocW = toc ? (tocCollapsed ? 0 : 246) : 0;
  if (toc) toc.__box = { x: 0, y: errH + topH, w: tocW, h: mainH };
  if (panes) panes.__box = { x: tocW, y: errH + topH, w: VIEW.w - tocW, h: mainH };

  const paneBoxes = panes ? computePanes(doc, panes) : [];
  for (const side of ['source', 'cn']) {
    const pane = get(side === 'cn' ? 'pane-cn' : 'pane-source');
    const scroll = get(side === 'cn' ? 'scroll-cn' : 'scroll-source');
    const pages = get(side === 'cn' ? 'pages-cn' : 'pages-source');
    const box = paneBoxes.find((p) => p.el === pane);
    if (!pane) continue;
    const pb = box ? box.box : { x: 0, y: 0, w: 0, h: 0 };
    pane.__box = pb;
    const head = pane.querySelector('.pane-head');
    if (head) head.__box = { x: pb.x, y: pb.y, w: pb.w, h: 28 };
    if (scroll) scroll.__box = { x: pb.x, y: pb.y + 28, w: pb.w, h: pb.h - 28 };
    if (pages && scroll) {
      const PAD = 12, GAP = 12;
      let y = scroll.__box.y + PAD;
      const kids = pages.children;
      for (const p of kids) {
        const w = px(p.style.width, 600), h = px(p.style.height, 800);
        p.__box = { x: scroll.__box.x + Math.max(0, (scroll.__box.w - w) / 2), y, w, h };
        y += h + GAP;
        // 页面内部：img/canvas 铺满，hot 绝对定位（hot 挂在 .hotlayer 下）
        for (const c of p.children) {
          if (c.classList.contains('hot')) {
            const l = px(c.style.left, 0), t = px(c.style.top, 0);
            const cw = px(c.style.width, 10), ch = px(c.style.height, 10);
            c.__box = { x: p.__box.x + l, y: p.__box.y + t, w: cw, h: ch };
          } else {
            c.__box = { x: p.__box.x, y: p.__box.y, w: p.__box.w, h: p.__box.h };
          }
        }
        const layer = p.querySelector('.hotlayer');
        if (layer) {
          layer.__box = { ...p.__box };
          for (const c of layer.children) {
            const l = px(c.style.left, 0), t = px(c.style.top, 0);
            const cw = px(c.style.width, 10), ch = px(c.style.height, 10);
            c.__box = { x: p.__box.x + l, y: p.__box.y + t, w: cw, h: ch };
          }
        }
      }
      const contentH = y - GAP + PAD - scroll.__box.y;
      scroll.__contentH = Math.max(0, contentH);
      const maxScroll = Math.max(0, contentH - scroll.__box.h);
      scroll.__maxScroll = maxScroll;
      if (scroll._scrollTop > maxScroll) { scroll._scrollTop = maxScroll; }
    }
  }
  // 其余元素给个占位盒子（无断言依赖）
  const fill = (el, box) => { if (!el.__box) el.__box = box; };
  if (get('detail')) get('detail').__box = { x: 1026, y: 500, w: 400, h: 388 };
  if (get('toast')) get('toast').__box = { x: 600, y: 860, w: 240, h: 30 };
  for (const el of doc.body.querySelectorAll('*')) fill(el, { x: 0, y: 0, w: 0, h: 0 });
}

function computePanes(doc, panes) {
  const src = doc.getElementById('pane-source');
  const cn = doc.getElementById('pane-cn');
  const gutter = doc.getElementById('gutter');
  const mode = doc.body.className;
  const W = panes.__box.w, H = panes.__box.h, X = panes.__box.x, Y = panes.__box.y;
  const out = [];
  const g = 7;
  if (mode.includes('mode-source')) {
    if (src) out.push({ el: src, box: { x: X, y: Y, w: W, h: H } });
    if (cn) out.push({ el: cn, box: { x: X, y: Y, w: 0, h: 0 } });
    return out;
  }
  if (mode.includes('mode-cn')) {
    if (cn) out.push({ el: cn, box: { x: X, y: Y, w: W, h: H } });
    if (src) out.push({ el: src, box: { x: X, y: Y, w: 0, h: 0 } });
    return out;
  }
  const flex = (src && src.style.flex) || '';
  const pct = /(\d+(?:\.\d+)?)%/.exec(flex);
  let w1 = W - g;
  w1 = pct ? Math.round(((W - g) * parseFloat(pct[1])) / 100) : Math.round((W - g) / 2);
  if (src) out.push({ el: src, box: { x: X, y: Y, w: w1, h: H } });
  if (cn) out.push({ el: cn, box: { x: X + w1 + g, y: Y, w: W - g - w1, h: H } });
  return out;
}

export function viewportBox(el) {
  ensureLayout();
  const b = el.__box || { x: 0, y: 0, w: 0, h: 0 };
  let dy = 0;
  let n = el.parentNode;
  while (n) {
    if (n.__box && typeof n.scrollTop === 'number' && (n.classList.has?.('pane-scroll') || (n._classes && n._classes.has('pane-scroll')))) {
      dy += n.scrollTop;
    }
    n = n.parentNode;
  }
  return { x: b.x, y: b.y - dy, w: b.w, h: b.h };
}

/* ---------------- Document ---------------- */
class Doc {
  constructor() {
    this.nodeType = 9;
    this.documentElement = new El('html');
    this.head = new El('head');
    this.body = new El('body');
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.body.ownerDocument = this;
    this.body.__isBody = true;
    this._listeners = new Map();
  }
  createElement(tag) { const e = new El(tag); e.ownerDocument = this; return e; }
  createElementNS(ns, tag) { return this.createElement(tag); }
  createTextNode(t) { return new Txt(t); }
  createDocumentFragment() { const f = new El('fragment'); f.ownerDocument = this; return f; }
  getElementById(id) {
    if (this.body.getAttribute('id') === id) return this.body;
    return this._walk((e) => e.getAttribute('id') === id);
  }
  _walk(fn) {
    const stack = [this.documentElement];
    while (stack.length) {
      const n = stack.pop();
      if (n.nodeType === 1 && n !== this.documentElement && n !== this.head && fn(n)) return n;
      for (const c of n.childNodes) if (c.nodeType === 1) stack.push(c);
    }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) { return this.documentElement.querySelectorAll(sel); }
  addEventListener(type, fn) { if (!this._listeners.has(type)) this._listeners.set(type, []); this._listeners.get(type).push(fn); }
  removeEventListener(type, fn) {
    const a = this._listeners.get(type) || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    const node = this;
    for (const fn of [...(this._listeners.get(ev.type) || [])]) fn.call(node, ev);
    const inline = this['on' + ev.type];
    if (typeof inline === 'function') inline.call(node, ev);
    return true;
  }
  get defaultView() { return globalThis.window; }
  get fonts() { return { ready: Promise.resolve(), load: async () => [], check: () => true }; }
}

let DOC = null;

/* ---------------- canvas ---------------- */
class Ctx2D {
  constructor(canvas) {
    this.canvas = canvas;
    this.calls = [];
    this.fillStyle = '#000';
    this.font = '10px sans-serif';
    this.textBaseline = 'alphabetic';
    this.textAlign = 'left';
    this._t = [1, 0, 0, 1, 0, 0];
  }
  setTransform(a, b, c, d, e, f) { this._t = [a, b, c, d, e, f]; }
  clearRect() { this.calls.push({ op: 'clearRect' }); }
  fillRect(x, y, w, h) { this.calls.push({ op: 'fillRect', x, y, w, h, fillStyle: this.fillStyle }); }
  fillText(text, x, y) { this.calls.push({ op: 'fillText', text, x, y, font: this.font, fillStyle: this.fillStyle }); }
  measureText(t) { return { width: String(t).length * 6 }; }
  getImageData(x, y, w, h) { return { data: new Uint8ClampedArray(Math.max(1, w * h * 4)) }; }
  save() {} restore() {} beginPath() {} closePath() {} stroke() {} fill() {} moveTo() {} lineTo() {} rect() {}
}

class CanvasEl extends El {
  constructor() { super('canvas'); this._ctx = null; this.width = 0; this.height = 0; }
  getContext() { if (!this._ctx) this._ctx = new Ctx2D(this); return this._ctx; }
}

/* ---------------- 安装全局环境 ---------------- */
export function installDom({ url = 'http://127.0.0.1:8901/?mock=1', html = null, viewport = VIEW } = {}) {
  VIEW.w = viewport.w || 1440;
  VIEW.h = viewport.h || 900;
  const doc = new Doc();
  DOC = doc;
  if (html) {
    for (const n of parseHtml(html, doc)) doc.body.appendChild(n);
  }
  doc.body.ownerDocument = doc;

  const u = new URL(url);
  const location = {
    href: url, origin: u.origin, protocol: u.protocol, host: u.host, hostname: u.hostname,
    port: u.port, pathname: u.pathname, search: u.search, hash: u.hash,
    toString() { return this.href; },
    assign() {}, replace() {}, reload() {},
  };
  const history = {
    state: null,
    replaceState(_s, _t, next) { if (next) { const nu = new URL(next, location.href); location.search = nu.search; location.pathname = nu.pathname; location.href = nu.href; } },
    pushState(_s, _t, next) { this.replaceState(_s, _t, next); },
  };
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
  const listeners = new Map();
  const win = {
    innerWidth: VIEW.w, innerHeight: VIEW.h, devicePixelRatio: 1,
    location, history, localStorage, document: doc,
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) { for (const fn of listeners.get(ev.type) || []) fn(ev); return true; },
    requestAnimationFrame(fn) { return setTimeout(() => fn(Date.now()), 16); },
    cancelAnimationFrame(id) { clearTimeout(id); },
    setTimeout, clearTimeout, setInterval, clearInterval,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    alert() {}, console,
    URL, URLSearchParams,
    __boxes: [],
  };
  const origFetch = globalThis.fetch;
  const fetchShim = (input, init) => {
    let target = input;
    if (typeof input === 'string' && !/^https?:/i.test(input)) {
      if (input.startsWith('//')) target = 'http:' + input;
      else target = new URL(input, url).href;
    }
    return origFetch(target, init);
  };

  const g = globalThis;
  g.window = win;
  g.document = doc;
  g.location = location;
  g.history = history;
  g.localStorage = localStorage;
  try { Object.defineProperty(g, 'navigator', { value: { userAgent: 'node-domshim', language: 'zh-CN' }, configurable: true }); } catch { /* node>=21 只读 */ }
  g.requestAnimationFrame = win.requestAnimationFrame;
  g.cancelAnimationFrame = win.cancelAnimationFrame;
  g.getComputedStyle = win.getComputedStyle;
  g.matchMedia = win.matchMedia;
  g.fetch = fetchShim;
  g.devicePixelRatio = 1;
  g.DOMParser = class { parseFromString(s) { const d = new Doc(); for (const n of parseHtml(s, d)) d.body.appendChild(n); return d; } };
  g.CustomEvent = class { constructor(type, o = {}) { return mkEvent(type, null, o); } };
  g.Event = class { constructor(type) { return mkEvent(type, null); } };

  const origCreate = doc.createElement.bind(doc);
  doc.createElement = (tag) => (String(tag).toLowerCase() === 'canvas' ? (() => { const c = new CanvasEl(); c.ownerDocument = doc; return c; })() : origCreate(tag));

  return { doc, win, location, history };
}

/* ---------------- 工具 ---------------- */
export function loadIndexHtml() {
  return fs.readFileSync(path.join(HERE, '..', 'index.html'), 'utf8');
}
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function waitFor(fn, { timeout = 20000, step = 25 } = {}) {
  const t0 = Date.now();
  for (;;) {
    let v;
    try { v = fn(); } catch { v = null; }
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error('waitFor 超时');
    await sleep(step);
  }
}

export function clickEl(el) { el.dispatchEvent(mkEvent('click', el)); }

export function scrollTo(el, top) {
  const max = el.__maxScroll ?? 1e9;
  const next = Math.max(0, Math.min(max, top));
  if (next === el.scrollTop) return;
  el.scrollTop = next;
  el._scrollEvents++;
  // 浏览器异步派发 scroll 事件（本 shim 用 microtask 之后派发，制造同样的回环风险）
  setTimeout(() => el.dispatchEvent(mkEvent('scroll', el)), 0);
}
export const scrollCount = (el) => el._scrollEvents;
