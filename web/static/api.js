/* ============================================================
 * api.js — REST 客户端（CONTRACT.md §5）
 *
 * 两种模式：
 *   ?mock=1  读取 ./mock/data.json 静态桩数据（无需后端）
 *   默认     走 /api/* 真实服务
 * 所有路径/字段严格按 CONTRACT §5，前端不做任何字段猜测。
 * ============================================================ */

const QS = new URLSearchParams(location.search);
export const IS_MOCK = QS.get('mock') === '1';

export class ApiError extends Error {
  constructor(message, status = 0, url = '') {
    super(message); this.name = 'ApiError'; this.status = status; this.url = url;
  }
}

async function toJson(res, url) {
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('json')) {
    const txt = await res.text().catch(() => '');
    throw new ApiError(`响应不是 JSON (${res.status}) ${txt.slice(0, 160)}`, res.status, url);
  }
  const body = await res.json().catch((e) => { throw new ApiError(`JSON 解析失败: ${e.message}`, res.status, url); });
  if (!res.ok) throw new ApiError((body && body.error) || `HTTP ${res.status}`, res.status, url);
  if (body && body.error) throw new ApiError(body.error, res.status, url);
  return body;
}

async function jget(url) {
  let res;
  try { res = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' }); }
  catch (e) { throw new ApiError(`网络错误: ${e.message}（服务未启动？）`, 0, url); }
  return toJson(res, url);
}

async function jsend(url, method, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body == null ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (e) { throw new ApiError(`网络错误: ${e.message}（服务未启动？）`, 0, url); }
  return toJson(res, url);
}

const q = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj || {})) {
    if (v === undefined || v === null || v === '') continue;
    p.set(k, Array.isArray(v) ? v.join(',') : String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
};

/* ============================================================
 * mock 后端：把 data.json 当作服务端，做同样的过滤
 * ============================================================ */
let _mock = null;
async function mock() {
  if (_mock) return _mock;
  const res = await fetch('./mock/data.json', { cache: 'no-store' });
  if (!res.ok) throw new ApiError(`mock 数据缺失: ./mock/data.json (${res.status})`, res.status);
  _mock = await res.json();
  _mock.__jobs = {};
  _mock.__seq = 1000;
  return _mock;
}

function cjkWidth(text, size) {
  let w = 0;
  for (const ch of text) {
    if (/[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]/.test(ch)) w += size;
    else if (ch === ' ') w += size * 0.28;
    else w += size * 0.52;
  }
  return w;
}

function mockWrap(text, size, maxw) {
  const lines = []; let cur = '';
  for (const ch of text) {
    if (cjkWidth(cur + ch, size) > maxw && cur) { lines.push(cur); cur = ch; } else cur += ch;
  }
  if (cur) lines.push(cur);
  return lines;
}

/** 给 mock 里新翻译的段补一个 translated-layout box（模拟 compute_layout） */
function mockAddLayout(m, vid, seg, zh) {
  const key = `${vid}:${seg.page}`;
  const layout = m.layout[key];
  if (!layout) return;
  if (layout.boxes.some((b) => b.seg_id === seg.seg_id)) return;
  const size = Math.round((seg.style?.size || 10) * 0.92 * 100) / 100;
  const maxw = (seg.bbox[2] - seg.bbox[0]) || 300;
  const lh = Math.round(size * 1.24 * 100) / 100;
  const origin = seg.line_boxes?.[0]?.[3] - (seg.style?.size || 10) * 0.25;
  const lines = mockWrap(zh, size, maxw).map((t, i) => ({
    text: t, x: seg.bbox[0], y: Math.round((origin + i * lh) * 100) / 100,
    size, font_slot: 'cjk', width: Math.round(cjkWidth(t, size) * 100) / 100,
  }));
  layout.boxes.push({
    seg_id: seg.seg_id, bbox: seg.bbox, paragraph_rect: seg.bbox, lines,
    color: 0x1a1a1a, bg: 0xffffff, shrink: 0,
  });
}

function mockJob(m, kind, total, onTick) {
  const id = ++m.__seq;
  const job = { job_id: id, kind, status: 'running', progress: 0, total, message: `${kind} 进行中`, error: null, result: null };
  m.__jobs[id] = job;
  const t0 = Date.now();
  const timer = setInterval(() => {
    const p = Math.min(total, Math.round(((Date.now() - t0) / 900) * total));
    job.progress = p;
    if (p >= total) {
      clearInterval(timer);
      job.status = 'done'; job.message = `${kind} 完成`;
      job.result = onTick ? onTick() : { ok: true, job_id: id };
    }
  }, 120);
  m.__jobs[id] = job; job.__timer = timer;
  return { job_id: id };
}

export const api = {
  isMock: IS_MOCK,
  base: '',

  async health() {
    if (IS_MOCK) return { ok: true, version: '1-mock', db: 'mock/data.json' };
    return jget('/api/health');
  },

  async documents() {
    if (IS_MOCK) return (await mock()).documents;
    return jget('/api/documents');
  },

  async createDocument(body) {
    if (IS_MOCK) throw new ApiError('mock 模式不支持上传', 0);
    return jsend('/api/documents', 'POST', body);
  },

  async uploadDocument(file) {
    if (IS_MOCK) throw new ApiError('mock 模式不支持上传', 0);
    const fd = new FormData();
    fd.append('file', file, file.name);
    const res = await fetch('/api/documents/upload', { method: 'POST', body: fd });
    return toJson(res, '/api/documents/upload');
  },

  async versions(docId) {
    if (IS_MOCK) return (await mock()).versions[String(docId)] || [];
    return jget(`/api/documents/${docId}/versions`);
  },

  async outline(docId, vid, depth = 3) {
    if (IS_MOCK) return (await mock()).outline[String(vid)] || [];
    return jget(`/api/documents/${docId}/versions/${vid}/outline${q({ depth })}`);
  },

  async segments(docId, vid, opts = {}) {
    if (IS_MOCK) {
      const m = await mock();
      let rows = (m.segments[String(vid)] || []).slice();
      const { page, page_from, page_to, kinds, role } = opts;
      if (page) rows = rows.filter((s) => s.page === +page);
      if (page_from) rows = rows.filter((s) => s.page >= +page_from);
      if (page_to) rows = rows.filter((s) => s.page <= +page_to);
      if (kinds) { const k = String(kinds).split(','); rows = rows.filter((s) => k.includes(s.kind)); }
      if (role) rows = rows.filter((s) => s.role === role);
      return rows.sort((a, b) => a.order_index - b.order_index);
    }
    return jget(`/api/documents/${docId}/versions/${vid}/segments${q(opts)}`);
  },

  async markers(docId, vid, page) {
    if (IS_MOCK) {
      const m = await mock();
      const mk = m.markers[`${vid}:${page}`];
      if (!mk) throw new ApiError(`mock 无第 ${page} 页 markers`, 404);
      return mk;
    }
    return jget(`/api/documents/${docId}/versions/${vid}/pages/${page}/markers`);
  },

  async translatedLayout(docId, vid, page) {
    if (IS_MOCK) {
      const m = await mock();
      const ly = m.layout[`${vid}:${page}`];
      if (!ly) throw new ApiError(`mock 无第 ${page} 页 translated-layout`, 404);
      return ly;
    }
    return jget(`/api/documents/${docId}/versions/${vid}/pages/${page}/translated-layout`);
  },

  async diff(docId, from, to, opts = {}) {
    if (IS_MOCK) {
      const m = await mock();
      const d = m.diff[String(docId)];
      if (!d) throw new ApiError('mock 无 diff 数据', 404);
      if (opts.page) {
        const pg = +opts.page;
        return { ...d, changes: (d.changes || []).filter((c) => +c.old_page === pg || +c.new_page === pg) };
      }
      return d;
    }
    return jget(`/api/documents/${docId}/diff${q({ from, to, page: opts.page, include_unchanged: opts.includeUnchanged })}`);
  },

  pageImageUrl(docId, vid, page, { kind = 'source', dpi = 110 } = {}) {
    if (IS_MOCK) {
      const m = _mock;
      const k = kind === 'bilingual' ? 'source' : kind;
      const url = m?.page_images?.[String(vid)]?.[String(page)]?.[k];
      if (url) return url;
      return `./mock/pages/v${vid}_p${page}_${k}.webp`;
    }
    return `/api/documents/${docId}/versions/${vid}/pages/${page}/image${q({ kind, dpi })}`;
  },

  async pageSize(docId, vid, page) {
    if (IS_MOCK) {
      const m = await mock();
      const s = m.page_size[String(vid)];
      return s ? { width: s.width, height: s.height, dpi: s.dpi } : null;
    }
    const mk = await jget(`/api/documents/${docId}/versions/${vid}/pages/${page}/markers`);
    return { width: mk.width, height: mk.height, dpi: mk.dpi || 110, units_per_px: mk.units_per_px, crop_box: mk.crop_box };
  },

  async translate(body) {
    if (IS_MOCK) {
      const m = await mock();
      const vid = body.version_id;
      const rows = (m.segments[String(vid)] || []);
      let targets = rows;
      if (body.segment_ids) targets = rows.filter((s) => body.segment_ids.includes(s.seg_id));
      else if (body.page_from || body.page_to) {
        targets = rows.filter((s) => (!body.page_from || s.page >= body.page_from) && (!body.page_to || s.page <= body.page_to));
      }
      targets = targets.filter((s) => s.role === 'body' && !(s.translation && s.translation.status !== 'missing'));
      return mockJob(m, 'translate', Math.max(1, targets.length), () => {
        for (const s of targets) {
          s.translation = { text: '（占位译文）' + s.text, status: 'machine', revision: (s.translation?.revision || 0) + 1 };
          mockAddLayout(m, vid, s, s.translation.text);
        }
        return { translated: targets.length, carried: 0, cached: 0, failed: 0, chars: 0, tokens: { prompt: 0, completion: 0 } };
      });
    }
    return jsend('/api/translate', 'POST', body);
  },

  async job(id) {
    if (IS_MOCK) {
      const m = await mock();
      const j = m.__jobs[id];
      if (!j) throw new ApiError(`mock 无 job ${id}`, 404);
      const { __timer, ...clean } = j;
      return clean;
    }
    return jget(`/api/jobs/${id}`);
  },

  async jobs() {
    if (IS_MOCK) return Object.values((_mock && _mock.__jobs) || {});
    return jget('/api/jobs');
  },

  async exportPdf(docId, vid, kind) {
    if (IS_MOCK) {
      const m = await mock();
      return mockJob(m, 'export', 4, () => ({ path: `data/derived/mock/${kind}.pdf`, bytes: 123456 }));
    }
    return jget(`/api/documents/${docId}/versions/${vid}/export${q({ kind })}`);
  },

  downloadUrl(docId, vid, kind) {
    return `/api/documents/${docId}/versions/${vid}/download${q({ kind })}`;
  },

  async review(body) {
    if (IS_MOCK) {
      const m = await mock();
      for (const vid of Object.keys(m.segments)) {
        const s = m.segments[vid].find((x) => x.seg_id === body.segment_id);
        if (s) {
          s.translation = { text: body.text, status: body.status || 'reviewed', revision: (s.translation?.revision || 0) + 1 };
          mockAddLayout(m, +vid, s, body.text);
        }
      }
      return { ok: true };
    }
    return jsend('/api/review', 'POST', body);
  },

  async glossary() {
    if (IS_MOCK) return (await mock()).glossary;
    return jget('/api/glossary');
  },

  async putGlossary(rows) {
    if (IS_MOCK) { (await mock()).glossary = rows; return { ok: true }; }
    return jsend('/api/glossary', 'PUT', rows);
  },

  /** 服务端可提供的字体（真实模式下用于 canvas 与 PDF 完全一致的字形） */
  fontFaceSpecs() {
    if (IS_MOCK) return [];
    return [
      { family: 'MTCJK', url: '/api/fonts/cjk', weight: '400' },
      { family: 'MTLatin', url: '/api/fonts/latin', weight: '400' },
      { family: 'MTLatinB', url: '/api/fonts/latin_bold', weight: '700' },
    ];
  },
};

export default api;
