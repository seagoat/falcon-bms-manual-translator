"""web/server.py — FastAPI 应用 + REST API（CONTRACT.md §5）

设计要点
--------
* **接口契约**：路径 / 参数 / 响应字段严格按 CONTRACT §5；`seg_id` 一律 int。
* **可降级启动**：core / render / translate / pipeline 尚未就绪时延迟 import，
  对应端点在缺依赖时返回 503 + `{"error": ...}` 而不是启动失败。
* **短连接**：每个请求 `core.db.connect()` 新建连接并 close（WAL 下并发读安全）。
* **长任务**：抽取 / 翻译 / 导出走后台线程 + jobs 表，POST 立即返回 `{job_id}`。
* **页面图**：按需生成 + 磁盘缓存 `data/derived/{slug}/v{n}/{kind}/page_XXXX.webp`。

启动：`python -m uvicorn web.server:app --port 8901`  或  `python cli.py serve`
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/json", ".json")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import config  # noqa: E402  (repo-root import)

# 可移植性：DB 里存相对路径时以此解析（见 core/paths.py）
try:
    from core import paths as core_paths  # noqa: E402
except Exception:                       # pragma: no cover - core 未就绪时降级
    class _NoPaths:                     # 最小兜底：原样返回，行为与旧代码一致
        @staticmethod
        def resolve(p, **_kw):
            return p

        @staticmethod
        def to_rel(p, **_kw):
            return p

        @staticmethod
        def is_absolute(p):
            return False

    core_paths = _NoPaths()  # type: ignore

STATIC_DIR = Path(__file__).resolve().parent / "static"
API_VERSION = "1"
RENDER_VER = "2"        # 页面图渲染策略版本（cn = 背景层，文字交 canvas）

# --------------------------------------------------------------------------
# 延迟 import（依赖模块可能还没写好）
# --------------------------------------------------------------------------
_MODS: dict[str, Any] = {}
_IMPORT_ERR: dict[str, str] = {}


def mod(name: str):
    """import 一个子模块；失败返回 None（不缓存失败，稍后写好可自愈）。"""
    m = _MODS.get(name)
    if m is not None:
        return m
    try:
        m = importlib.import_module(name)
    except Exception as exc:                                     # pragma: no cover
        _IMPORT_ERR[name] = f"{type(exc).__name__}: {exc}"
        return None
    _MODS[name] = m
    return m


def require(name: str):
    m = mod(name)
    if m is None:
        raise HTTPException(status_code=503, detail=f"模块 {name} 尚未就绪: {_IMPORT_ERR.get(name, '')}")
    return m


def have(name: str) -> bool:
    return mod(name) is not None


# --------------------------------------------------------------------------
# DB 短连接
# --------------------------------------------------------------------------
def db_conn():
    core_db = mod("core.db")
    if core_db is None:
        raise HTTPException(503, f"core.db 尚未就绪: {_IMPORT_ERR.get('core.db', '')}")
    conn = core_db.connect()
    try:
        conn.execute("PRAGMA busy_timeout=20000")    # 并发写（翻译/导出线程）时不立刻抛 locked
    except Exception:
        pass
    return conn


def store():
    return require("core.store")


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(text, default=None):
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# jobs（有 core.store 用表，没有则内存兜底，保证 UI 不白屏）
# --------------------------------------------------------------------------
class MemJobs:
    def __init__(self):
        self._d: dict[int, dict] = {}
        self._n = 0
        self._lock = threading.Lock()

    def create(self, kind, params):
        with self._lock:
            self._n += 1
            jid = self._n
            self._d[jid] = {"job_id": jid, "kind": kind, "status": "queued", "progress": 0,
                            "total": params.get("total", 0), "message": "", "error": None,
                            "result": None, "params": params, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            return jid

    def update(self, jid, **kw):
        with self._lock:
            j = self._d.get(jid)
            if not j:
                return
            for k in ("status", "progress", "total", "message", "result", "error"):
                if k in kw and kw[k] is not None:
                    j[k] = kw[k]

    def get(self, jid):
        return self._d.get(jid)

    def list(self, limit=50):
        return sorted(self._d.values(), key=lambda j: j["job_id"], reverse=True)[:limit]


_MEM_JOBS = MemJobs()
_JOB_LOCK = threading.Lock()


def job_create(kind: str, params: dict) -> int:
    st = mod("core.store")
    if st is not None:
        with closing(db_conn()) as c:
            jid = st.create_job(c, kind, params)
            c.commit()
            return int(jid)
    return _MEM_JOBS.create(kind, params)


def job_update(jid: int, **kw) -> None:
    """更新任务状态；SQLite 写竞争时重试，**绝不允许静默丢失终态**。"""
    st = mod("core.store")
    if st is None:
        _MEM_JOBS.update(jid, **kw)
        return
    last: Optional[Exception] = None
    for attempt in range(5):
        try:
            with closing(db_conn()) as c:
                st.update_job(c, jid, **kw)
                c.commit()
            return
        except Exception as exc:                       # database is locked / 临时 IO
            last = exc
            time.sleep(0.05 * (2 ** attempt))
    print(f"[job_update] job {jid} 更新失败（已重试 5 次）: {last}", file=sys.stderr)
    raise RuntimeError(f"job {jid} 状态更新失败: {last}")


def job_get(jid: int) -> Optional[dict]:
    st = mod("core.store")
    if st is not None:
        try:
            with closing(db_conn()) as c:
                row = st.get_job(c, jid)
                if row:
                    return _job_public(row)
        except Exception:
            traceback.print_exc()
    return _job_public(_MEM_JOBS.get(jid)) if _MEM_JOBS.get(jid) else None


def job_list(limit: int = 50) -> list[dict]:
    st = mod("core.store")
    rows = []
    if st is not None:
        try:
            with closing(db_conn()) as c:
                rows = [_job_public(r) for r in st.list_jobs(c, limit)]
        except Exception:
            rows = []
    if not rows:
        rows = [_job_public(r) for r in _MEM_JOBS.list(limit)]
    return rows


def _job_public(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return row
    r = dict(row)
    return {
        "job_id": int(r.get("job_id") or r.get("id") or 0),
        "kind": r.get("kind", ""),
        "status": r.get("status", "queued"),
        "progress": r.get("progress") or 0,
        "total": r.get("total") or 0,
        "message": r.get("message") or "",
        "error": r.get("error"),
        "result": jload(r.get("result"), r.get("result")),
        "created_at": r.get("created_at", ""),
        "updated_at": r.get("updated_at", ""),
    }


def run_job(kind: str, params: dict, fn) -> int:
    """后台线程执行 fn(job_id)；立即返回 job_id。

    **保证终态**：无论 fn 正常返回、抛异常还是 job_update 本身失败，
    最终都会把 status 落到 done/failed（`_finalize` 带重试），
    避免出现「进度 100% + message 已完成，但 status 仍为 running」的任务。
    """
    jid = job_create(kind, params)
    total = params.get("total") or 1
    job_update(jid, status="running", message=f"{kind} 开始")

    def _finalize(status: str, **fields) -> None:
        try:
            job_update(jid, status=status, **fields)
            return
        except Exception:
            traceback.print_exc()
        # 再试一次：换一条独立连接，避免上一次事务残留
        try:
            time.sleep(0.3)
            job_update(jid, status=status, **fields)
        except Exception:
            traceback.print_exc()
            print(f"[run_job] 警告：job {jid} 终态 {status} 写入失败", file=sys.stderr)

    def _work():
        try:
            res = fn(jid)
        except Exception as exc:                                  # pragma: no cover
            traceback.print_exc()
            _finalize("failed", error=f"{type(exc).__name__}: {exc}", message=str(exc)[:300])
            return
        try:
            _finalize("done", progress=total, total=total,
                      message=f"{kind} 完成",
                      result=res if res is not None else {"ok": True})
        except Exception:
            traceback.print_exc()

    threading.Thread(target=_work, name=f"job-{kind}-{jid}", daemon=True).start()
    return jid


# --------------------------------------------------------------------------
# 派生路径 / 页面图缓存
# --------------------------------------------------------------------------
_GEN_LOCKS: dict[str, threading.Lock] = {}
_GEN_MASTER = threading.Lock()


def _gen_lock(key: str) -> threading.Lock:
    with _GEN_MASTER:
        lk = _GEN_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _GEN_LOCKS[key] = lk
        return lk


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def derived_version_dir(slug: str, version_no: int, kind: str, dpi: int) -> Path:
    base = config.derived_dir(slug, f"v{version_no}", kind)
    if int(dpi) != int(config.get("render_dpi", 110)):
        base = base / f"dpi{dpi}"
        base.mkdir(parents=True, exist_ok=True)
    return base


def page_image_path(slug: str, version_no: int, kind: str, page: int, dpi: int) -> Path:
    return derived_version_dir(slug, version_no, kind, dpi) / f"page_{page:04d}.webp"


def _render_page_webp(src_pdf: str, page_no: int, dpi: int, kind: str = "source",
                      conn=None, doc_id=None, version_id=None) -> bytes:
    """生成单页 webp 字节；kind=cn/bilingual 需要 render 模块（否则退回 source）。"""
    import pymupdf
    pdb = mod("render.pagebuild")
    if kind == "source" or pdb is None:
        if pdb is not None and hasattr(pdb, "render_page_webp"):
            return pdb.render_page_webp(src_pdf, page_no, dpi=dpi, quality=88)
        with pymupdf.open(src_pdf) as doc:
            pm = doc[page_no - 1].get_pixmap(dpi=dpi, alpha=False)
            return _pix_to_webp(pm)
    if kind == "cn":
        # 译文栏的图片是「背景层」：图片/矢量/页眉页脚保留，**正文文字全部擦除**，
        # 译文由前端 canvas 严格按 /translated-layout 逐行绘制（两层都画会重影）。
        with pymupdf.open(src_pdf) as src:
            page = pdb.rebuild_page(src, page_no, erase_roles=("body",))
            return _pix_to_webp(page.get_pixmap(dpi=dpi, alpha=False))
    # bilingual：左右拼接
    from PIL import Image
    st = store()
    with closing(db_conn() if conn is None else _NoClose(conn)) as c:
        segs = st.segments_of_version(c, version_id, page=page_no)
        trans = st.translations_of_version(c, version_id)
    with pymupdf.open(src_pdf) as src:
        left = src[page_no - 1].get_pixmap(dpi=dpi, alpha=False)
        page = pdb.build_translated_page(src, page_no, segs, trans, cjk_font_path=resolve_cjk())
        right = page.get_pixmap(dpi=dpi, alpha=False)
    im_l = Image.open(io.BytesIO(left.tobytes("png"))).convert("RGB")
    im_r = Image.open(io.BytesIO(right.tobytes("png"))).convert("RGB")
    h = max(im_l.height, im_r.height)
    gap = 12
    out = Image.new("RGB", (im_l.width + gap + im_r.width, h), (40, 44, 52))
    out.paste(im_l, (0, 0))
    out.paste(im_r, (im_l.width + gap, 0))
    buf = io.BytesIO()
    out.save(buf, format="WEBP", quality=88, method=4)
    return buf.getvalue()


class _NoClose:
    """包装已有连接，供 closing() 使用但不真正关闭。"""

    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, item):
        return getattr(self._c, item)

    def close(self):
        pass


def _pix_to_webp(pix) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=88, method=4)
    return buf.getvalue()


def resolve_cjk() -> str:
    pdb = mod("core.pdfdoc")
    if pdb is not None and hasattr(pdb, "resolve_cjk_font"):
        return pdb.resolve_cjk_font()
    for cand in ("C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/simhei.ttf"):
        if os.path.exists(cand):
            return cand
    raise HTTPException(503, "找不到中文字体")


def version_row(conn, version_id: int) -> dict:
    st = store()
    v = st.get_version(conn, version_id)
    if not v:
        raise HTTPException(404, f"version {version_id} 不存在")
    # 可移植性：DB 里可能存的是「相对于仓库根」的路径（见 core/paths.py）。
    # 在这里统一解析成本机绝对路径，下游所有 v["source_path"] 用法都不用改。
    return _resolve_version_paths(v)


def doc_row(conn, doc_id: int) -> dict:
    st = store()
    d = st.get_document(conn, doc_id)
    if not d:
        raise HTTPException(404, f"document {doc_id} 不存在")
    d = dict(d)
    if d.get("source_dir"):
        d["source_dir"] = core_paths.resolve(d["source_dir"])
    return d


def _resolve_version_paths(v: dict) -> dict:
    """把 version 行里的 source_path 解析成本机绝对路径（相对路径基于仓库根）。"""
    out = dict(v)
    sp = out.get("source_path")
    if sp:
        out["source_path"] = core_paths.resolve(sp)
    return out


def check_version_belongs(conn, doc_id: int, version_id: int) -> dict:
    v = version_row(conn, version_id)
    if int(v.get("doc_id", 0)) != int(doc_id):
        raise HTTPException(404, f"version {version_id} 不属于文档 {doc_id}")
    return v


# --------------------------------------------------------------------------
# diff 索引缓存（供 /markers 着色）
# --------------------------------------------------------------------------
_DIFF_CACHE: dict[tuple, tuple[float, dict]] = {}
_DIFF_TTL = 90.0


def diff_index(conn, doc_id: int, version_id: int) -> dict:
    """返回 {seg_id: {"change_kind":..,"change_id":..}}（仅新版段）。"""
    st = store()
    versions = st.list_versions(conn, doc_id)
    ids = [v["version_id"] for v in versions]
    if version_id not in ids:
        return {}
    i = ids.index(version_id)
    if i == 0:
        return {}
    from_vid = ids[i - 1]
    key = (doc_id, from_vid, version_id)
    now = time.time()
    hit = _DIFF_CACHE.get(key)
    if hit and now - hit[0] < _DIFF_TTL:
        return hit[1]
    out: dict[int, dict] = {}
    differ = mod("versions.differ")
    if differ is not None:
        try:
            vd = differ.diff_versions(conn, doc_id, from_vid, version_id)
            for n, ch in enumerate(getattr(vd, "changes", []) or []):
                sid = getattr(ch, "new_segment_id", None)
                cid = n + 1
                if sid:
                    out[int(sid)] = {"change_kind": str(getattr(ch, "kind", "")),
                                     "change_id": cid}
        except Exception:
            traceback.print_exc()
    _DIFF_CACHE[key] = (now, out)
    return out


def clear_caches():
    _DIFF_CACHE.clear()
    _LAYOUT_CACHE.clear()


# --------------------------------------------------------------------------
# translated-layout 进程内 LRU
# --------------------------------------------------------------------------
_LAYOUT_CACHE: dict[tuple, dict] = {}
_LAYOUT_ORDER: list[tuple] = []
_LAYOUT_MAX = 96
_LAYOUT_LOCK = threading.Lock()


def render_rev(conn, version_id: int) -> str:
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(t.revision),0) AS mr, COUNT(t.id) AS n "
            "FROM translations t JOIN segments s ON s.id = t.segment_id WHERE s.version_id=?",
            (version_id,)).fetchone()
        return f"{row['mr'] if row else 0}:{row['n'] if row else 0}"
    except Exception:
        return "0:0"


# 渲染代码指纹：把 render/ 下各模块的最新 mtime 揉进缓存键。
# ⚠️ 起因：`_LAYOUT_CACHE` 只用 (version_id, page, revision) 做键，
# **改代码后它不会失效** —— 进程里还留着改之前算出来的布局。
# 实测踩过一次：修好目录页码后重新导出 PDF 是好的，但浏览器里页码仍旧不见，
# 因为页面布局来自缓存；必须重启服务才恢复。加上代码指纹后改代码自动失效。
_RENDER_FP_CACHE: dict = {"t": 0.0, "v": ""}


def render_fingerprint() -> str:
    now = time.time()
    if now - _RENDER_FP_CACHE["t"] < 2.0 and _RENDER_FP_CACHE["v"]:
        return _RENDER_FP_CACHE["v"]
    parts = []
    try:
        render_dir = Path(__file__).resolve().parent.parent / "render"
        for f in sorted(render_dir.glob("*.py")):
            try:
                parts.append(f"{f.name}:{int(f.stat().st_mtime)}")
            except OSError:
                pass
    except Exception:
        pass
    val = str(hash("|".join(parts))) if parts else "0"
    _RENDER_FP_CACHE.update({"t": now, "v": val})
    return val


def compute_layout(conn, doc_id: int, version_id: int, page: int) -> dict:
    st = store()
    rev = render_rev(conn, version_id)
    key = (version_id, page, rev, render_fingerprint())
    with _LAYOUT_LOCK:
        if key in _LAYOUT_CACHE:
            return _LAYOUT_CACHE[key]
    v = version_row(conn, version_id)
    segs = st.segments_of_version(conn, version_id, page=page)
    trans = st.translations_of_version(conn, version_id)
    pdb = mod("render.pagebuild")
    font_file = ""
    try:
        font_file = resolve_cjk()
    except HTTPException:
        font_file = ""
    boxes: list[dict] = []
    if pdb is not None and hasattr(pdb, "compute_layout"):
        fn = pdb.compute_layout
        src_doc = None
        if v.get("source_path") and Path(v["source_path"]).exists():
            try:
                import pymupdf
                src_doc = pymupdf.open(v["source_path"])
            except Exception:
                src_doc = None
        try:
            try:
                boxes = fn(segs, trans, page_no=page, src=src_doc)
            except TypeError:
                try:
                    boxes = fn(segs, trans, page_no=page)
                except TypeError:
                    boxes = fn(segs, trans)
        except Exception:
            traceback.print_exc()
            boxes = []
        finally:
            if src_doc is not None:
                try:
                    src_doc.close()
                except Exception:
                    pass
        boxes = [b for b in (boxes or []) if not (isinstance(b, dict) and b.get("pending"))]
    width, height = SEG_PAGE_SIZE.get(version_id) or (None, None)
    if width is None:
        width, height = page_size_points(v["source_path"]) if v.get("source_path") else (595.28, 841.89)
    out = {"page": page, "width": width, "height": height, "font_file": font_file, "boxes": boxes}
    with _LAYOUT_LOCK:
        _LAYOUT_CACHE[key] = out
        _LAYOUT_ORDER.append(key)
        if len(_LAYOUT_ORDER) > _LAYOUT_MAX:
            old = _LAYOUT_ORDER.pop(0)
            _LAYOUT_CACHE.pop(old, None)
    return out


SEG_PAGE_SIZE: dict[int, tuple[float, float]] = {}


def page_size_points(pdf_path: str) -> tuple[float, float]:
    try:
        import pymupdf
        with pymupdf.open(pdf_path) as d:
            r = d[0].rect
            return float(r.width), float(r.height)
    except Exception:
        return 595.28, 841.89


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(title="manual_trans_trace", version=API_VERSION, docs_url="/api/docs")

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"error": str(exc.detail) if exc.detail else "error"}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _any_exc(request: Request, exc: Exception):        # pragma: no cover
        traceback.print_exc()
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @app.middleware("http")
    async def _cache_headers(request: Request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p.startswith("/api/") and "/image" in p:
            resp.headers.setdefault("Cache-Control", "public, max-age=300")
        elif p.endswith((".html", ".js", ".css", ".json", ".webp")) or p == "/":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------- health ----------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": API_VERSION, "db": _rel(config.db_path()),
                "modules": {"core": have("core.store"), "render": have("render.pagebuild"),
                            "translate": have("translate.engine"), "differ": have("versions.differ"),
                            "pipeline": have("pipeline")}}

    # ---------------- documents ----------------
    @app.get("/api/documents")
    def list_documents():
        st = store()
        with closing(db_conn()) as c:
            rows = st.list_documents(c)
            out = []
            for d in rows:
                vids = st.list_versions(c, d["doc_id"])
                cur = st.current_version(c, d["doc_id"])
                stats = {}
                if cur:
                    try:
                        stats["translation"] = st.translation_stats(c, cur["version_id"])
                    except Exception:
                        stats = {}
                    stats["segments"] = st.count_segments(c, cur["version_id"])
                elif vids:
                    stats["segments"] = st.count_segments(c, vids[-1]["version_id"])
                merged = jload(d.get("stats"), {}) or {}
                merged.update({k: v for k, v in stats.items() if v is not None})
                out.append({"doc_id": d["doc_id"], "slug": d.get("slug", ""), "title": d.get("title", ""),
                            "current_version_id": (cur or {}).get("version_id") or d.get("current_version_id"),
                            "version_count": len(vids),
                            "page_count": (cur or (vids[-1] if vids else {})).get("page_count", 0),
                            "stats": merged})
            return out

    @app.post("/api/documents")
    def create_document(payload: dict = Body(default=None), wait: float = Query(default=25.0)):
        payload = payload or {}
        pdf_path = payload.get("pdf_path")
        if not pdf_path:
            raise HTTPException(400, "缺少 pdf_path")
        p = Path(pdf_path)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise HTTPException(400, f"文件不存在: {p}")
        return _ingest(p, slug=payload.get("slug"), title=payload.get("title"),
                       note=payload.get("note", ""), label=payload.get("label"),
                       pages=_parse_pages(payload.get("pages")), wait=wait)

    @app.post("/api/documents/upload")
    async def upload_document(file: UploadFile = File(...), slug: str = Query(default=""),
                              title: str = Query(default=""), wait: float = Query(default=25.0)):
        if not file.filename:
            raise HTTPException(400, "缺少文件")
        pdfs = config.pdfs_dir()
        tmp = pdfs / f"upload_{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        return _ingest(tmp, slug=slug or None, title=title or None, note="", label=None, wait=wait)

    @app.get("/api/documents/{doc_id}/versions")
    def versions(doc_id: int):
        st = store()
        with closing(db_conn()) as c:
            doc_row(c, doc_id)
            rows = st.list_versions(c, doc_id)
            out = []
            for v in rows:
                vv = dict(v)
                vv["stats"] = jload(vv.get("stats"), {}) or {}
                out.append(vv)
            return out

    @app.get("/api/documents/{doc_id}/versions/{vid}/outline")
    def outline(doc_id: int, vid: int, depth: int = Query(default=3)):
        st = store()
        with closing(db_conn()) as c:
            check_version_belongs(c, doc_id, vid)
            segs = st.segments_of_version(c, vid)
            trans = st.translations_of_version(c, vid)
        heads = [s for s in segs if s.get("kind") in ("heading", "caption") or s.get("role") == "body"
                 and (s.get("style") or {}).get("bold")]
        if not heads:
            heads = [s for s in segs if s.get("kind") == "heading"]
        sizes = sorted({round(float((s.get("style") or {}).get("size") or 0), 1) for s in heads}, reverse=True)
        out = []
        for s in heads:
            size = round(float((s.get("style") or {}).get("size") or 0), 1)
            d = sizes.index(size) + 1 if size in sizes else 1
            if d > depth:
                continue
            t = trans.get(s["id"]) or trans.get(s.get("seg_id"))
            out.append({"page": s["page"], "seg_id": s["id"], "kind": s.get("kind", "heading"),
                        "text": s.get("text", ""), "translation": (t or {}).get("text", ""), "depth": d})
        return out

    @app.get("/api/documents/{doc_id}/versions/{vid}/segments")
    def segments(doc_id: int, vid: int, page: Optional[int] = None, page_from: Optional[int] = None,
                 page_to: Optional[int] = None, kinds: Optional[str] = None, role: Optional[str] = None):
        st = store()
        with closing(db_conn()) as c:
            check_version_belongs(c, doc_id, vid)
            kinds_l = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
            rows = st.segments_of_version(c, vid, page=page, kinds=kinds_l)
            trans = st.translations_of_version(c, vid)
        out = []
        for s in rows:
            if page_from and s["page"] < page_from:
                continue
            if page_to and s["page"] > page_to:
                continue
            if role and s.get("role") != role:
                continue
            t = trans.get(s["id"])
            out.append({
                "seg_id": int(s["id"]), "page": int(s["page"]), "order_index": int(s.get("order_index") or 0),
                "kind": s.get("kind", "paragraph"), "bbox": jload(s.get("bbox"), []) or [],
                "line_boxes": jload(s.get("line_boxes"), []) or [],
                "text": s.get("text", ""), "role": s.get("role", "body"),
                "style": jload(s.get("style"), {}) or {},
                "translation": None if not t else {"text": t.get("text", ""), "status": t.get("status", ""),
                                                   "revision": t.get("revision", 1)},
            })
        return out

    @app.get("/api/documents/{doc_id}/diff")
    def diff(doc_id: int, from_v: int = Query(alias="from"), to_v: int = Query(alias="to"),
             include_unchanged: int = Query(default=0), page: Optional[int] = Query(default=None),
             full_text: int = Query(default=0)):
        """版本差异。

        性能约定（真实 4800+ 段文档实测，修复前后 33.9s/2.2MB → <0.3s/<100KB）：
        * **只开一次连接**，批量取文本（`store.segments_by_ids`），禁止循环内开连接；
        * `unchanged` 默认不进 `changes`（只进 `counts`），`include_unchanged=1` 可全量；
        * `removed` 不发 `old_text`（前端用 `old_segment_id` + `old_page` 去旧版 segments 取），
          这是 4600+ 段被删时 payload 爆炸的根因；
        * 文本默认截断 500 字符（`full_text=1` 关闭截断）；
        * `page=N` 只返回与该页相关的变更（前端按页取幽灵块/详情，与全量截断无关）。
        """
        from_v, to_v = int(from_v), int(to_v)
        differ = require("versions.differ")
        st = store()
        with closing(db_conn()) as c:
            doc_row(c, doc_id)
            check_version_belongs(c, doc_id, from_v)
            check_version_belongs(c, doc_id, to_v)
            if from_v == to_v:
                # 同版本比较：必然无差异 —— 秒回，不要跑 differ（曾要 23s / 2.4MB）
                try:
                    n = int(c.execute("SELECT COUNT(*) FROM segments WHERE version_id=? AND role='body'",
                                      (from_v,)).fetchone()[0])
                except Exception:
                    n = 0
                counts = {"unchanged": n, "modified": 0, "added": 0, "removed": 0, "moved": 0,
                          "reordered": 0, "split": 0, "merged": 0,
                          "old_total": n, "new_total": n,
                          "from_version_id": from_v, "to_version_id": to_v, "doc_id": doc_id}
                return {"from": from_v, "to": to_v, "from_version_id": from_v, "to_version_id": to_v,
                        "counts": counts, "changes": [], "unchanged_omitted": n,
                        "total_changes": n, "page_filter": page, "truncated": False,
                        "truncated_count": 0, "same_version": True}
            vd = differ.diff_versions(c, doc_id, from_v, to_v)
            all_changes = list(getattr(vd, "changes", []) or [])
            wanted = []
            need: set[int] = set()
            for n, ch in enumerate(all_changes):
                kind = str(getattr(ch, "kind", ""))
                if kind == "unchanged" and not include_unchanged:
                    continue
                if page is not None:
                    op, npp = getattr(ch, "old_page", None), getattr(ch, "new_page", None)
                    if int(page) not in (int(op) if op else -1, int(npp) if npp else -1):
                        continue
                wanted.append((n, ch, kind))
                if kind in ("unchanged", "removed"):
                    continue                      # 不需要文本：unchanged 只给 id；removed 用旧版 segments 取
                for sid in (getattr(ch, "old_segment_id", None), getattr(ch, "new_segment_id", None)):
                    if sid:
                        need.add(int(sid))
            texts: dict[int, str] = {}
            if need:
                if hasattr(st, "segments_by_ids"):
                    rows = st.segments_by_ids(c, sorted(need)) or {}
                    texts = {int(k): (v or {}).get("text", "") for k, v in rows.items()}
                else:                              # 兜底：同一个连接内循环查询（绝不再开连接）
                    for sid in sorted(need):
                        row = st.get_segment(c, sid)
                        if row:
                            texts[int(sid)] = row.get("text", "")

        MAX_CHANGES = 4000
        truncated = len(wanted) > MAX_CHANGES
        kept = wanted[:MAX_CHANGES] if truncated else wanted
        if truncated:
            wanted = kept
        counts = getattr(vd, "counts", {}) or {}
        omitted = int(counts.get("unchanged", 0) or 0) if not include_unchanged else 0

        def _txt(sid, kind) -> str:
            if kind in ("unchanged", "removed"):
                return ""
            t = texts.get(int(sid or 0), "")
            return t if (full_text or len(t) <= 500) else t[:500] + "…"

        changes = []
        for n, ch, kind in wanted:
            unc = kind == "unchanged"
            oid, nid = getattr(ch, "old_segment_id", None), getattr(ch, "new_segment_id", None)
            changes.append({
                "change_id": n + 1, "kind": kind,
                "ratio": float(getattr(ch, "ratio", 0.0) or 0.0),
                "old_page": getattr(ch, "old_page", None), "new_page": getattr(ch, "new_page", None),
                "old_segment_id": oid, "new_segment_id": nid,
                "old_text": _txt(oid, kind), "new_text": _txt(nid, kind),
                "char_diffs": [] if unc else (getattr(ch, "char_diffs", None) or []),
                "summary": getattr(ch, "summary", "") or "",
            })
        return {"from": from_v, "to": to_v, "from_version_id": from_v, "to_version_id": to_v,
                "counts": counts, "changes": changes,
                "unchanged_omitted": omitted, "total_changes": len(all_changes),
                "page_filter": page, "truncated": truncated,
                "truncated_count": max(0, len(all_changes) - len(changes)) if truncated else 0}

    # ---------------- page image / markers / layout ----------------
    @app.get("/api/documents/{doc_id}/versions/{vid}/pages/{page}/image")
    def page_image(doc_id: int, vid: int, page: int, dpi: int = Query(default=0),
                   kind: str = Query(default="source")):
        if kind not in ("source", "cn", "bilingual"):
            raise HTTPException(400, "kind 必须是 source|cn|bilingual")
        dpi = int(dpi or config.get("render_dpi", 110))
        st = store()
        with closing(db_conn()) as c:
            v = check_version_belongs(c, doc_id, vid)
            doc = doc_row(c, doc_id)
            n_pages = int(v.get("page_count") or 0)
            if n_pages and page > n_pages:
                raise HTTPException(404, f"第 {page} 页超出范围（共 {n_pages} 页）")
            # 缓存键包含译文版本号：翻译/复核后 render_rev 变化 → 自动重新生成，
            # 否则会出现「译文页图仍是旧的无译文原页」。
            # RENDER_VER 在渲染策略变化时手动 +1（例如 cn 改为「只留背景、文字交 canvas」）。
            #
            # ⚠️ 缓存键**必须**包含 page。曾经漏掉 page，导致第一张被渲染的页面图
            # 被渲染快照缓存后在**所有页**上复用（翻页后每一页都显示同一张图）。
            rev = "static" if kind == "source" else f"{render_rev(c, vid)}|{RENDER_VER}"
            params_hash = hashlib.sha1(
                f"{kind}:{dpi}:{rev}:p{page}".encode()).hexdigest()[:16]
            snap = None
            try:
                snap = st.get_render_snapshot(c, vid, kind, params_hash)
            except Exception:
                snap = None
            if snap and not snap.get("stale"):
                sp = Path(snap.get("path") or "")
                if sp.exists() and sp.stat().st_size > 0:
                    resp = FileResponse(sp, media_type="image/webp")
                    resp.headers["Cache-Control"] = "public, max-age=60"
                    resp.headers["X-MT-Cache"] = "hit"
                    return resp
            path = page_image_path(doc.get("slug", "doc"), int(v.get("version_no") or 1), kind, page, dpi)
            fallback = False
            # 走到这里说明当前 rev 的快照不新鲜（首次 / 译文变了 / RENDER_VER 变了）
            # → 必须重新生成并**覆盖**旧 rev 的产物，否则会一直返回过期页面图。
            with _gen_lock(str(path)):
                src = v.get("source_path") or ""
                if not src or not Path(src).exists():
                    raise HTTPException(404, "源 PDF 不可用")
                real_kind = kind
                if kind != "source" and not have("render.pagebuild"):
                    real_kind = "source"
                    fallback = True
                try:
                    data = _render_page_webp(src, page, dpi, real_kind,
                                             conn=c, doc_id=doc_id, version_id=vid)
                except HTTPException:
                    raise
                except Exception as exc:
                    if kind != "source":
                        data = _render_page_webp(src, page, dpi, "source")
                        real_kind, fallback = "source", True
                    else:
                        raise HTTPException(500, f"渲染失败: {exc}")
                if not fallback:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                else:
                    fb = path.parent / "_pending" / path.name
                    fb.parent.mkdir(parents=True, exist_ok=True)
                    fb.write_bytes(data)
                    path = fb
            if not path.exists():
                raise HTTPException(404, "页面图不存在")
            try:
                # fallback 也记录，避免渲染模块不可用时每个请求都重试
                st.upsert_render_snapshot(c, version_id=vid, kind=kind, path=str(path),
                                          params_hash=params_hash, stale=0,
                                          bytes_=path.stat().st_size)
                c.commit()
            except Exception:
                traceback.print_exc()
            resp = FileResponse(path, media_type="image/webp")
            resp.headers["Cache-Control"] = "public, max-age=60" if not fallback else "no-store"
            resp.headers["X-MT-Cache"] = "miss"
            if fallback:
                resp.headers["X-MT-Fallback"] = "source"
            return resp

    @app.get("/api/documents/{doc_id}/versions/{vid}/pages/{page}/markers")
    def markers(doc_id: int, vid: int, page: int):
        st = store()
        with closing(db_conn()) as c:
            v = check_version_belongs(c, doc_id, vid)
            segs = st.segments_of_version(c, vid, page=page)
            if not segs:
                n_pages = int(v.get("page_count") or 0)
                if n_pages and page > n_pages:
                    raise HTTPException(404, f"第 {page} 页超出范围（共 {n_pages} 页）")
            ch_index = diff_index(c, doc_id, vid)
            src = v.get("source_path") or ""
        dpi = int(config.get("render_dpi", 110))
        width, height = page_size_points(src) if src else (595.28, 841.89)
        SEG_PAGE_SIZE[vid] = (width, height)
        items = []
        for s in segs:
            ch = ch_index.get(int(s["id"]), {})
            items.append({
                "seg_id": int(s["id"]), "kind": s.get("kind", "paragraph"),
                "kind_group": _kind_group(s.get("kind", "")),
                "bbox": jload(s.get("bbox"), []) or [],
                "line_boxes": jload(s.get("line_boxes"), []) or [],
                "role": s.get("role", "body"),
                "change_kind": ch.get("change_kind"), "change_id": ch.get("change_id"),
            })
        return {"page": page, "width": width, "height": height, "dpi": dpi,
                "units_per_px": round(72.0 / dpi, 6), "crop_box": [0.0, 0.0, width, height],
                "items": items}

    @app.get("/api/documents/{doc_id}/versions/{vid}/pages/{page}/translated-layout")
    def translated_layout(doc_id: int, vid: int, page: int):
        with closing(db_conn()) as c:
            check_version_belongs(c, doc_id, vid)
            return compute_layout(c, doc_id, vid, page)

    # ---------------- translate / jobs ----------------
    @app.post("/api/translate")
    def translate(payload: dict = Body(...)):
        doc_id = int(payload.get("doc_id") or 0)
        version_id = int(payload.get("version_id") or 0)
        seg_ids = payload.get("segment_ids")
        page_from = payload.get("page_from")
        page_to = payload.get("page_to")
        provider = payload.get("provider") or None
        force = bool(payload.get("force"))
        model = payload.get("model") or None
        engine = require("translate.engine")
        st = store()
        with closing(db_conn()) as c:
            check_version_belongs(c, doc_id, version_id)
            if not seg_ids and page_from is None and page_to is None:
                total = st.count_segments(c, version_id)
            elif seg_ids:
                total = len(seg_ids)
            else:
                rows = st.segments_of_version(c, version_id)
                total = len([s for s in rows
                             if (page_from is None or s["page"] >= page_from)
                             and (page_to is None or s["page"] <= page_to)])
        params = {"doc_id": doc_id, "version_id": version_id, "segment_ids": seg_ids,
                  "page_from": page_from, "page_to": page_to, "provider": provider,
                  "force": force, "total": total}

        def _work(jid):
            conn = db_conn()
            try:
                res = engine.translate_segments(
                    conn, doc_id, version_id, segment_ids=seg_ids, page_from=page_from,
                    page_to=page_to, provider=provider, force=force, model=model, job_id=jid)
                conn.commit()
                try:
                    st.mark_renders_stale(conn, version_id)
                    conn.commit()
                except Exception:
                    pass
                clear_caches()
                return res
            finally:
                conn.close()

        jid = run_job("translate", params, _work)
        return {"job_id": jid}

    @app.get("/api/jobs")
    def jobs(limit: int = Query(default=50)):
        return job_list(limit)

    @app.get("/api/jobs/{jid}")
    def job(jid: int):
        j = job_get(jid)
        if not j:
            raise HTTPException(404, f"job {jid} 不存在")
        return j

    # ---------------- export / download ----------------
    @app.get("/api/documents/{doc_id}/versions/{vid}/export")
    def export(doc_id: int, vid: int, kind: str = Query(default="cn"),
               pages: Optional[str] = None, dpi: int = Query(default=0)):
        if kind not in ("cn", "bilingual"):
            raise HTTPException(400, "kind 必须是 cn|bilingual")
        dpi = int(dpi or config.get("render_dpi", 110))
        with closing(db_conn()) as c:
            v = check_version_belongs(c, doc_id, vid)
            doc = doc_row(c, doc_id)
            out_path = export_path(doc.get("slug", "doc"), int(v.get("version_no") or 1), kind)
            if out_path.exists():
                return {"path": _rel(out_path), "bytes": out_path.stat().st_size, "kind": kind}
            total = int(v.get("page_count") or 0)
        params = {"doc_id": doc_id, "version_id": vid, "kind": kind, "pages": pages, "dpi": dpi, "total": total}

        def _work(jid):
            return _do_export(jid, doc_id, vid, kind, out_path, pages=pages, dpi=dpi)

        jid = run_job("export", params, _work)
        return {"job_id": jid, "kind": kind}

    @app.get("/api/documents/{doc_id}/versions/{vid}/download")
    def download(doc_id: int, vid: int, kind: str = Query(default="cn")):
        with closing(db_conn()) as c:
            v = check_version_belongs(c, doc_id, vid)
            doc = doc_row(c, doc_id)
            p = export_path(doc.get("slug", "doc"), int(v.get("version_no") or 1), kind)
        if not p.exists():
            raise HTTPException(404, "导出文件尚未生成，请先调用 /export")
        return FileResponse(p, media_type="application/pdf",
                            filename=f"{doc.get('slug', 'doc')}_{kind}.pdf")

    # ---------------- review / glossary ----------------
    @app.post("/api/review")
    def review(payload: dict = Body(...)):
        seg_id = int(payload.get("segment_id") or 0)
        text = payload.get("text", "")
        status = payload.get("status") or "reviewed"
        if not seg_id:
            raise HTTPException(400, "缺少 segment_id")
        st = store()
        with closing(db_conn()) as c:
            seg = st.get_segment(c, seg_id)
            if not seg:
                raise HTTPException(404, f"segment {seg_id} 不存在")
            st.upsert_translation(c, segment_id=seg_id, text=text, status=status,
                                  provider="human", model="", confidence=1.0)
            if hasattr(st, "set_translation_status"):
                st.set_translation_status(c, seg_id, status)
            c.commit()
            try:
                st.mark_renders_stale(c, seg["version_id"])
                c.commit()
            except Exception:
                pass
        clear_caches()
        return {"ok": True, "seg_id": seg_id, "status": status}

    @app.get("/api/glossary")
    def glossary_get():
        gl = mod("translate.glossary")
        if gl is not None and hasattr(gl, "load"):
            try:
                rows = gl.load()
                if rows is not None:
                    out = []
                    for r in rows:
                        if isinstance(r, dict):
                            out.append({"en": str(r.get("en", "")), "zh": str(r.get("zh", "")),
                                        "case_sensitive": bool(r.get("case_sensitive", False)),
                                        "note": str(r.get("note", ""))})
                        elif isinstance(r, (tuple, list)) and len(r) >= 2:
                            out.append({"en": str(r[0]), "zh": str(r[1]),
                                        "case_sensitive": bool(r[2]) if len(r) > 2 else False,
                                        "note": str(r[3]) if len(r) > 3 else ""})
                    if out:
                        return out
            except Exception:
                traceback.print_exc()
        p = config.data_dir() / "glossary.json"
        if p.exists():
            return jload(p.read_text(encoding="utf-8"), []) or []
        return []

    @app.put("/api/glossary")
    def glossary_put(rows: list = Body(...)):
        clean = [{"en": str(r.get("en", "")), "zh": str(r.get("zh", "")),
                  "case_sensitive": bool(r.get("case_sensitive", False)),
                  "note": str(r.get("note", ""))} for r in (rows or [])]
        p = config.data_dir() / "glossary.json"
        gl = mod("translate.glossary")
        if gl is not None and hasattr(gl, "save"):
            try:
                p = Path(gl.save(clean))
                if hasattr(gl, "load"):
                    gl.load(reload=True)
                return {"ok": True, "count": len(clean), "path": _rel(p)}
            except Exception:
                traceback.print_exc()
        p.write_text(jdump(clean), encoding="utf-8")
        return {"ok": True, "count": len(clean)}

    # ---------------- 字体（canvas 与 PDF 字形一致） ----------------
    @app.get("/api/fonts/{slot}")
    def font(slot: str):
        pdb = mod("core.pdfdoc")
        cand_map: dict[str, list[str]] = {
            "cjk": ["C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyh.ttc"],
            "latin": ["C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/arial.ttf"],
            "latin_bold": ["C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/arialbd.ttf"],
            "latin_italic": ["C:/Windows/Fonts/calibrii.ttf", "C:/Windows/Fonts/ariali.ttf"],
        }
        if slot not in cand_map:
            raise HTTPException(404, "未知字体槽")
        if slot == "cjk" and pdb is not None and hasattr(pdb, "resolve_cjk_font"):
            try:
                cand_map["cjk"].insert(0, pdb.resolve_cjk_font())
            except Exception:
                pass
        for cand in cand_map[slot]:
            if os.path.exists(cand):
                return FileResponse(cand, media_type="font/ttf")
        raise HTTPException(404, "字体文件不存在")

    # ---------------- 静态托管（放最后，避免吞掉 /api） ----------------
    # 缓存头由上面的 `_cache_headers` 中间件统一处理：/app.js /index.html /style.css
    # 一律 `no-store`，所以改了前端文件刷新即生效，不需要额外包装。
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
_KIND_GROUPS = {
    "heading": "heading", "caption": "caption", "list_item": "list", "table_cell": "table",
    "header": "furniture", "footer": "furniture", "page_num": "furniture", "paragraph": "text",
}


def _kind_group(kind: str) -> str:
    return _KIND_GROUPS.get(kind, "text")


def _text_of(st, conn, seg_id) -> str:
    """⚠️ 只在**已持有连接**时使用；禁止在循环里新建连接（曾导致 /api/diff 慢 100 倍）。"""
    if not seg_id:
        return ""
    try:
        s = st.get_segment(conn, int(seg_id))
        return (s or {}).get("text", "")
    except Exception:
        return ""


def export_path(slug: str, version_no: int, kind: str) -> Path:
    d = config.derived_dir(slug, f"v{version_no}", "export")
    return d / f"{kind}.pdf"


def _do_export(jid: int, doc_id: int, vid: int, kind: str, out_path: Path,
               pages=None, dpi: int = 110) -> dict:
    st = store()
    pdfout = require("render.pdfout")
    with closing(db_conn()) as c:
        v = version_row(c, vid)
        src = v.get("source_path") or ""
        all_segs = st.segments_of_version(c, vid)
        trans = st.translations_of_version(c, vid)
    if not src or not Path(src).exists():
        raise HTTPException(404, "源 PDF 不可用")
    by_page: dict[int, list] = {}
    for s in all_segs:
        by_page.setdefault(int(s["page"]), []).append(s)
    page_list = None
    if pages:
        try:
            a, _, b = str(pages).partition("-")
            page_list = list(range(int(a), int(b or a) + 1))
        except Exception:
            page_list = None
    total = len(page_list) if page_list else max(by_page) if by_page else 1

    def on_page(done, tot):
        job_update(jid, progress=done, total=tot, message=f"导出第 {done}/{tot} 页")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fn = pdfout.export_cn_pdf if kind == "cn" else pdfout.export_bilingual_pdf
    kwargs = dict(pages=page_list, cjk_font_path=resolve_cjk(), dpi=dpi, on_page=on_page)
    try:
        res = fn(src, str(out_path), by_page, trans, **kwargs)
    except TypeError:
        kwargs.pop("on_page", None)
        res = fn(src, str(out_path), by_page, trans, **kwargs)
    res = dict(res or {})
    res.setdefault("path", _rel(out_path))
    res["bytes"] = out_path.stat().st_size if out_path.exists() else res.get("bytes", 0)
    res["kind"] = kind
    try:
        with closing(db_conn()) as c:
            st.upsert_render_snapshot(c, version_id=vid, kind=kind, path=str(out_path),
                                      params_hash=hashlib.sha1(f"{kind}:{dpi}".encode()).hexdigest()[:16],
                                      stale=0, bytes_=res["bytes"])
            c.commit()
    except Exception:
        pass
    return res


def _parse_pages(v) -> Optional[tuple]:
    """接受 "3-40" / [3, 40] / "5" → (from, to)（1-based，含端点）。"""
    if v in (None, "", []):
        return None
    if isinstance(v, (list, tuple)):
        try:
            a = int(v[0]); b = int(v[1]) if len(v) > 1 else a
            return (a, b)
        except Exception:
            return None
    m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(v))
    if not m:
        return None
    a = int(m.group(1)); b = int(m.group(2) or a)
    return (min(a, b), max(a, b))


def _ingest(p: Path, *, slug=None, title=None, note="", label=None,
            pages: Optional[tuple] = None, wait: float = 25.0) -> dict:
    pipeline = require("pipeline")
    st = store()

    def _work(jid):
        conn = db_conn()
        try:
            job_update(jid, message=f"抽取 {p.name}")
            kwargs = dict(slug=slug, title=title, note=note, label=label)
            if pages:
                kwargs["pages"] = tuple(pages)
            try:
                res = pipeline.ingest_pdf(conn, str(p), **kwargs)
            except TypeError:
                kwargs.pop("pages", None)
                res = pipeline.ingest_pdf(conn, str(p), **kwargs)
            conn.commit()
            clear_caches()
            try:
                doc_id = res.get("doc_id")
                vids = [v["version_id"] for v in st.list_versions(conn, doc_id)]
                if len(vids) >= 2:
                    differ = mod("versions.differ")
                    if differ is not None:
                        job_update(jid, message="计算版本差异")
                        differ.diff_versions(conn, doc_id, vids[-2], vids[-1])
                        conn.commit()
                        res["diff"] = "ok"
            except Exception:
                traceback.print_exc()
            return res
        finally:
            conn.close()

    jid = run_job("ingest", {"pdf_path": str(p), "slug": slug, "pages": list(pages) if pages else None, "total": 1}, _work)
    if wait and wait > 0:
        t0 = time.time()
        while time.time() - t0 < wait:
            j = job_get(jid)
            if j and j["status"] in ("done", "failed", "cancelled"):
                if j["status"] == "failed":
                    raise HTTPException(500, j.get("error") or "抽取失败")
                res = dict(j.get("result") or {})
                res["job_id"] = jid
                return res
            time.sleep(0.4)
    return {"job_id": jid, "status": "running"}


app = create_app()


def run(host: str = None, port: int = None, reload: bool = False):
    import uvicorn
    host = host or str(config.get("web_host", "127.0.0.1"))
    port = int(port or config.get("web_port", 8777))
    uvicorn.run("web.server:app" if reload else app, host=host, port=port, reload=reload, log_level="info")
    return 0


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(run())
