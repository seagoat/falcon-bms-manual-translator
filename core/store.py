"""DAO 层（CONTRACT.md §3）：段 / 译文 / 版本 / 快照 / 任务 / 术语表。

约定：
* 行 → dict 时 JSON 列自动 `json.loads`（stats/params/result/char_diffs/bbox/line_boxes/fonts/style）。
* 别名方便上层：segment 行同时含 `id` 与 `seg_id`；version 行含 `version_id` 与 `id`；
  document 行含 `doc_id` 与 `id`。
* 查询类函数对空输入 / 缺失记录返回 None / [] / {} / 0，不抛异常；
  只有真正非法的输入（未知 kind/status、必填字段为空、引用不存在的 doc/segment）抛 ValueError。
* 每个写函数自己 commit（调用方不必记着提交）；`insert_segments` 用 executemany + 单事务。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence, Union

from core import fingerprint as fp
from core.models import (
    CHANGE_KINDS,
    JOB_STATUSES,
    SEGMENT_KINDS,
    TRANSLATION_STATUSES,
    Segment,
)

__all__ = [
    # documents & versions
    "upsert_document", "get_document", "list_documents",
    "register_version", "update_version_stats", "get_version", "list_versions",
    "current_version", "set_current_version",
    # segments
    "insert_segments", "get_segment", "segments_of_version", "segments_by_ids", "count_segments",
    # translations
    "get_translation", "translations_of_version", "upsert_translation",
    "set_translation_status", "translation_stats",
    "get_cached_translation", "put_cached_translation",
    # change tracking
    "record_segment_link", "links_for_version", "links_for_diff", "diff_summary",
    "write_version_diff", "get_version_diff",
    # render snapshots
    "upsert_render_snapshot", "get_render_snapshot", "find_render_snapshot", "mark_renders_stale",
    # jobs
    "create_job", "update_job", "get_job", "list_jobs",
    # glossary
    "list_glossary", "upsert_glossary", "replace_glossary", "delete_glossary",
]

_JSON_SEP = (",", ":")

# 允许的 role（契约用 "pagenum" 作为 role，SegmentKind.PAGE_NUM 的值是 "page_num"，两者都收）
_ROLES = frozenset({"body", "header", "footer", "pagenum", "page_num", "noise"})


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    """ISO8601 本地时间字符串（秒精度，够用且便于排序）。"""
    return datetime.now().replace(microsecond=0).isoformat()


def _dumps(value: Any, default: str = "null") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value  # 已经是 JSON 文本，原样存
    try:
        return json.dumps(value, ensure_ascii=False, separators=_JSON_SEP)
    except (TypeError, ValueError):
        return default


def _loads(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """同时支持 dataclass（Segment）与 dict 输入。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _set_field(obj: Any, name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    elif hasattr(obj, name):
        try:
            setattr(obj, name, value)
        except (AttributeError, TypeError):
            pass


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# 行 → dict
# --------------------------------------------------------------------------- #
def _document_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["id"] = d.get("doc_id", 0)
    return d


def _version_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["stats"] = _loads(d.get("stats"), {})
    d["id"] = d.get("version_id", 0)
    return d


def _segment_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("bbox", "line_boxes", "fonts"):
        d[key] = _loads(d.get(key), [])
    d["style"] = _loads(d.get("style"), {})
    d["seg_id"] = d.get("id", 0)
    return d


def _translation_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["seg_id"] = d.get("segment_id", 0)
    return d


def _link_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["char_diffs"] = _loads(d.get("char_diffs"), [])
    d["change_id"] = d.get("id", 0)
    return d


def _snapshot_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _job_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = _loads(d.get("params"), {})
    d["result"] = _loads(d.get("result"), None)
    return d


def _glossary_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _rows(cur: sqlite3.Cursor, mapper) -> list[dict]:
    return [mapper(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def upsert_document(conn: sqlite3.Connection, *, slug: str, title: str, source_dir: str) -> int:
    """按 slug 取文档 id；不存在则创建。空字符串不会覆盖已有非空值。"""
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("slug must not be empty")
    now = _now()
    row = conn.execute("SELECT * FROM documents WHERE slug = ?", (slug,)).fetchone()
    if row is not None:
        doc_id = int(row["doc_id"])
        conn.execute(
            "UPDATE documents SET title = ?, source_dir = ?, updated_at = ? WHERE doc_id = ?",
            (title or row["title"] or "", source_dir or row["source_dir"] or "", now, doc_id),
        )
        conn.commit()
        return doc_id
    cur = conn.execute(
        "INSERT INTO documents (slug, title, source_dir, created_at, updated_at) VALUES (?,?,?,?,?)",
        (slug, title or "", source_dir or "", now, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_document(conn: sqlite3.Connection, doc_id: int) -> Optional[dict]:
    if not doc_id:
        return None
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return _document_row(row) if row is not None else None


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    """含 current_version_id / version_count / page_count / stats，直接满足 §5 列表接口。"""
    docs = _rows(conn.execute("SELECT * FROM documents ORDER BY doc_id"), _document_row)
    for doc in docs:
        versions = _rows(
            conn.execute(
                "SELECT * FROM document_versions WHERE doc_id = ? "
                "ORDER BY is_current DESC, version_no DESC",
                (doc["doc_id"],),
            ),
            _version_row,
        )
        head = versions[0] if versions else None
        doc["version_count"] = len(versions)
        doc["current_version_id"] = int(head["version_id"]) if head else None
        doc["page_count"] = int(head["page_count"]) if head else 0
        doc["stats"] = head["stats"] if head else {}
    return docs


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
def register_version(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    source_path: str,
    sha256: str,
    pdf_bytes: int,
    page_count: int,
    release_label: str = "",
    doc_date: str = "",
    note: str = "",
    label: str = "",
    make_current: bool = True,
) -> int:
    """登记版本；同一 doc 下 sha256 相同则复用已有行（ingest 幂等）。返回 version_id。"""
    doc = conn.execute("SELECT doc_id FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise ValueError(f"document {doc_id} not found")
    now = _now()
    row = None
    if sha256:
        row = conn.execute(
            "SELECT * FROM document_versions WHERE doc_id = ? AND sha256 = ? "
            "ORDER BY version_no LIMIT 1",
            (doc_id, sha256),
        ).fetchone()

    if row is not None:
        vid = int(row["version_id"])
        conn.execute(
            "UPDATE document_versions SET source_path = ?, pdf_bytes = ?, page_count = ?, "
            "release_label = ?, doc_date = ?, note = ?, label = ?, sha256 = ? WHERE version_id = ?",
            (
                source_path or row["source_path"] or "",
                _int_or_zero(pdf_bytes) or int(row["pdf_bytes"] or 0),
                _int_or_zero(page_count) or int(row["page_count"] or 0),
                release_label or row["release_label"] or "",
                doc_date or row["doc_date"] or "",
                note or row["note"] or "",
                label or row["label"] or "",
                sha256,
                vid,
            ),
        )
    else:
        max_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS n FROM document_versions WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        version_no = int(max_no["n"]) + 1
        cur = conn.execute(
            "INSERT INTO document_versions (doc_id, version_no, label, source_path, sha256, "
            "pdf_bytes, page_count, release_label, doc_date, created_at, is_current, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                doc_id, version_no, label or f"v{version_no}", source_path or "", sha256 or "",
                _int_or_zero(pdf_bytes), _int_or_zero(page_count), release_label or "",
                doc_date or "", now, note or "",
            ),
        )
        vid = int(cur.lastrowid or 0)

    if make_current:
        conn.execute(
            "UPDATE document_versions SET is_current = CASE WHEN version_id = ? THEN 1 ELSE 0 END "
            "WHERE doc_id = ?",
            (vid, doc_id),
        )
    conn.execute("UPDATE documents SET updated_at = ? WHERE doc_id = ?", (now, doc_id))
    conn.commit()
    return vid


def update_version_stats(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    segments: Optional[int] = None,
    chars: Optional[int] = None,
    pages: Optional[int] = None,
    images: Optional[int] = None,
) -> None:
    """合并式更新 stats（未传的键保持原值）。"""
    row = conn.execute(
        "SELECT stats FROM document_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        return
    stats = _loads(row["stats"], {})
    if not isinstance(stats, dict):
        stats = {}
    for key, value in (("segments", segments), ("chars", chars), ("pages", pages), ("images", images)):
        if value is not None:
            stats[key] = _int_or_zero(value)
    conn.execute(
        "UPDATE document_versions SET stats = ? WHERE version_id = ?",
        (_dumps(stats, "{}"), version_id),
    )
    conn.commit()


def get_version(conn: sqlite3.Connection, version_id: int) -> Optional[dict]:
    if not version_id:
        return None
    row = conn.execute(
        "SELECT * FROM document_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    return _version_row(row) if row is not None else None


def list_versions(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    if not doc_id:
        return []
    return _rows(
        conn.execute(
            "SELECT * FROM document_versions WHERE doc_id = ? ORDER BY version_no", (doc_id,)
        ),
        _version_row,
    )


def current_version(conn: sqlite3.Connection, doc_id: int) -> Optional[dict]:
    """is_current=1 的版本；没有标记则退回最新的版本；没有版本返回 None。"""
    if not doc_id:
        return None
    row = conn.execute(
        "SELECT * FROM document_versions WHERE doc_id = ? "
        "ORDER BY is_current DESC, version_no DESC LIMIT 1",
        (doc_id,),
    ).fetchone()
    return _version_row(row) if row is not None else None


def set_current_version(conn: sqlite3.Connection, doc_id: int, version_id: int) -> None:
    row = conn.execute(
        "SELECT doc_id FROM document_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"version {version_id} not found")
    if int(row["doc_id"]) != int(doc_id):
        raise ValueError(f"version {version_id} does not belong to document {doc_id}")
    conn.execute(
        "UPDATE document_versions SET is_current = CASE WHEN version_id = ? THEN 1 ELSE 0 END "
        "WHERE doc_id = ?",
        (version_id, doc_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# segments
# --------------------------------------------------------------------------- #
_INSERT_SEGMENT_SQL = (
    "INSERT INTO segments (id, version_id, doc_id, page, order_index, kind, text, canonical_text, "
    "normalized, fingerprint, bbox, line_boxes, fonts, style, role, content_hash) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_segments(conn: sqlite3.Connection, segs: list[Segment]) -> list[int]:
    """批量插入（executemany + 单事务），返回与输入同序的 id 列表。

    调用方没填 `canonical_text`/`normalized`/`fingerprint`/`content_hash` 时自动补算
    （§3）：`canonical_text = normalize(dehyphenate(text.splitlines()))`，
    `fingerprint = blake2b(normalize(canonical_text).casefold())`。
    补算结果会**写回**传入的 Segment 对象，方便调用方直接复用。
    """
    if not segs:
        return []

    version_ids = {_int_or_zero(_field(s, "version_id", 0)) for s in segs}
    version_docs: dict[int, int] = {}
    for vid in version_ids:
        if vid <= 0:
            raise ValueError("Segment.version_id must be set")
        row = conn.execute(
            "SELECT doc_id FROM document_versions WHERE version_id = ?", (vid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"version {vid} not found")
        version_docs[vid] = int(row["doc_id"])

    base = int(
        conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM segments").fetchone()["n"]
    )
    rows: list[tuple] = []
    ids: list[int] = []
    for offset, seg in enumerate(segs, start=1):
        seg_id = base + offset
        kind = _field(seg, "kind", "paragraph") or "paragraph"
        if kind not in SEGMENT_KINDS:
            raise ValueError(f"unknown segment kind: {kind!r}")
        role = _field(seg, "role", "body") or "body"
        if role not in _ROLES:
            raise ValueError(f"unknown segment role: {role!r}")
        text = _field(seg, "text", "") or ""
        canonical = _field(seg, "canonical_text", "") or ""
        if not canonical:
            # 有换行的原始段落先按 §4.1 拼行（复原软连字符），再归一化
            canonical = (
                fp.normalize(fp.dehyphenate(text.splitlines()))
                if "\n" in text
                else fp.normalize(text)
            )
        normalized = _field(seg, "normalized", "") or fp.normalize(canonical)
        fprint = _field(seg, "fingerprint", "") or fp.fingerprint(canonical)
        chash = _field(seg, "content_hash", "") or fp.content_hash(canonical)
        _set_field(seg, "canonical_text", canonical)
        _set_field(seg, "normalized", normalized)
        _set_field(seg, "fingerprint", fprint)
        _set_field(seg, "content_hash", chash)
        _set_field(seg, "id", seg_id)
        version_id = _int_or_zero(_field(seg, "version_id", 0))
        doc_id = _int_or_zero(_field(seg, "doc_id", 0)) or version_docs.get(version_id, 0)
        if not _int_or_zero(_field(seg, "doc_id", 0)):
            _set_field(seg, "doc_id", doc_id)      # 只填 version_id 时自动补 doc_id
        rows.append(
            (
                seg_id,
                version_id,
                doc_id,
                _int_or_zero(_field(seg, "page", 1)) or 1,
                _int_or_zero(_field(seg, "order_index", 0)),
                kind,
                text,
                canonical,
                normalized,
                fprint,
                _dumps(_field(seg, "bbox", []), "[]"),
                _dumps(_field(seg, "line_boxes", []), "[]"),
                _dumps(_field(seg, "fonts", []), "[]"),
                _dumps(_field(seg, "style", {}), "{}"),
                role,
                chash,
            )
        )
        ids.append(seg_id)
    conn.executemany(_INSERT_SEGMENT_SQL, rows)
    conn.commit()
    return ids


def get_segment(conn: sqlite3.Connection, segment_id: int) -> Optional[dict]:
    if not segment_id:
        return None
    row = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    return _segment_row(row) if row is not None else None


def segments_of_version(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    page: Optional[int] = None,
    kinds: Optional[Union[str, Iterable[str]]] = None,
) -> list[dict]:
    """版本内段（order_index 升序）；page / kinds 可选过滤。version 不存在返回 []。"""
    if not version_id:
        return []
    sql = "SELECT * FROM segments WHERE version_id = ?"
    params: list[Any] = [version_id]
    if page is not None:
        sql += " AND page = ?"
        params.append(_int_or_zero(page))
    kind_list = [kinds] if isinstance(kinds, str) else list(kinds or [])
    kind_list = [k for k in kind_list if k]
    if kind_list:
        sql += " AND kind IN (" + ",".join("?" * len(kind_list)) + ")"
        params.extend(kind_list)
    sql += " ORDER BY order_index, id"
    return _rows(conn.execute(sql, params), _segment_row)


def segments_by_ids(conn: sqlite3.Connection, segment_ids: Sequence[int]) -> dict[int, dict]:
    """按 id 取段（返回 {seg_id: row}），供翻译引擎等随机访问。"""
    ids = [_int_or_zero(i) for i in (segment_ids or [])]
    ids = [i for i in ids if i > 0]
    out: dict[int, dict] = {}
    for chunk_start in range(0, len(ids), 900):  # 规避 SQLite 变量数上限
        chunk = ids[chunk_start:chunk_start + 900]
        sql = "SELECT * FROM segments WHERE id IN (" + ",".join("?" * len(chunk)) + ")"
        for row in conn.execute(sql, chunk):
            seg = _segment_row(row)
            out[int(seg["id"])] = seg
    return out


def count_segments(conn: sqlite3.Connection, version_id: int) -> int:
    if not version_id:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM segments WHERE version_id = ?", (version_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# translations
# --------------------------------------------------------------------------- #
def get_translation(conn: sqlite3.Connection, segment_id: int) -> Optional[dict]:
    if not segment_id:
        return None
    row = conn.execute(
        "SELECT * FROM translations WHERE segment_id = ?", (segment_id,)
    ).fetchone()
    return _translation_row(row) if row is not None else None


def translations_of_version(conn: sqlite3.Connection, version_id: int) -> dict[int, dict]:
    """{segment_id: translation row}；无译文返回 {}。"""
    if not version_id:
        return {}
    rows = conn.execute(
        "SELECT t.* FROM translations t JOIN segments s ON s.id = t.segment_id "
        "WHERE s.version_id = ? ORDER BY s.order_index",
        (version_id,),
    ).fetchall()
    return {int(r["segment_id"]): _translation_row(r) for r in rows}


def upsert_translation(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    text: str,
    status: str,
    provider: str = "",
    model: str = "",
    confidence: float = 0.0,
    reuse_of: Optional[int] = None,
) -> int:
    """写入/更新段译文；文本变化时 revision +1。返回 translation id。"""
    if status not in TRANSLATION_STATUSES:
        raise ValueError(f"unknown translation status: {status!r}")
    if not segment_id:
        raise ValueError("segment_id is required")
    seg = conn.execute("SELECT id FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if seg is None:
        raise ValueError(f"segment {segment_id} not found")
    text = text or ""
    now = _now()
    row = conn.execute(
        "SELECT * FROM translations WHERE segment_id = ?", (segment_id,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO translations (segment_id, text, status, provider, model, confidence, "
            "revision, created_at, reuse_of) VALUES (?,?,?,?,?,?,1,?,?)",
            (segment_id, text, status, provider or "", model or "", _float_or_zero(confidence),
             now, reuse_of),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    old_text = row["text"] or ""
    revision = int(row["revision"] or 1)
    if text != old_text:
        revision += 1
    conn.execute(
        "UPDATE translations SET text = ?, status = ?, provider = ?, model = ?, confidence = ?, "
        "revision = ?, reuse_of = ? WHERE segment_id = ?",
        (
            text, status, provider or row["provider"] or "", model or row["model"] or "",
            _float_or_zero(confidence), revision,
            reuse_of if reuse_of is not None else row["reuse_of"], segment_id,
        ),
    )
    conn.commit()
    return int(row["id"])


def set_translation_status(
    conn: sqlite3.Connection, segment_id: int, status: str, *, reviewed_by: str = ""
) -> None:
    """只改状态/复核人（§5 /api/review）。段不存在时静默 no-op。"""
    if status not in TRANSLATION_STATUSES:
        raise ValueError(f"unknown translation status: {status!r}")
    if not segment_id:
        return
    seg = conn.execute("SELECT id FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if seg is None:
        return
    now = _now()
    row = conn.execute(
        "SELECT * FROM translations WHERE segment_id = ?", (segment_id,)
    ).fetchone()
    reviewed_at = now if (reviewed_by or status == "reviewed") else ""
    if row is None:
        conn.execute(
            "INSERT INTO translations (segment_id, text, status, revision, created_at, "
            "reviewed_by, reviewed_at) VALUES (?, '', ?, 1, ?, ?, ?)",
            (segment_id, status, now, reviewed_by or "", reviewed_at),
        )
    else:
        conn.execute(
            "UPDATE translations SET status = ?, reviewed_by = ?, reviewed_at = ? "
            "WHERE segment_id = ?",
            (status, reviewed_by or row["reviewed_by"] or "", reviewed_at or row["reviewed_at"] or "",
             segment_id),
        )
    conn.commit()


def translation_stats(conn: sqlite3.Connection, version_id: int) -> dict:
    """{"total": 段数, "translated": 有非空译文的段数, "by_status": {状态: 数}, "chars": 译文字符数}"""
    total = count_segments(conn, version_id)
    by_status: dict[str, int] = {s: 0 for s in TRANSLATION_STATUSES}
    translated = 0
    chars = 0
    if version_id:
        rows = conn.execute(
            "SELECT t.status AS status, t.text AS text FROM translations t "
            "JOIN segments s ON s.id = t.segment_id WHERE s.version_id = ?",
            (version_id,),
        ).fetchall()
        for row in rows:
            status = row["status"] or "missing"
            by_status[status] = by_status.get(status, 0) + 1
            text = row["text"] or ""
            if text.strip():
                translated += 1
                chars += len(text)
    by_status["missing"] = by_status.get("missing", 0) + max(0, total - sum(
        v for k, v in by_status.items() if k != "missing"
    ))
    return {"total": total, "translated": translated, "by_status": by_status, "chars": chars}


def get_cached_translation(conn: sqlite3.Connection, fingerprint: str) -> Optional[dict]:
    """translation_cache 按指纹查询（§7 步骤 2）。命中时 hits +1。"""
    if not fingerprint:
        return None
    row = conn.execute(
        "SELECT * FROM translation_cache WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE translation_cache SET hits = hits + 1 WHERE fingerprint = ?", (fingerprint,)
    )
    conn.commit()
    cached = dict(row)
    cached["hits"] = int(cached.get("hits") or 0) + 1    # 返回值要反映本次命中
    return cached


def put_cached_translation(
    conn: sqlite3.Connection, *, fingerprint: str, text: str, provider: str = "", model: str = ""
) -> None:
    """写跨版本译文缓存（§7 步骤 5）。空指纹忽略。"""
    if not fingerprint:
        return
    conn.execute(
        "INSERT INTO translation_cache (fingerprint, text, provider, model, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
        "text = excluded.text, provider = excluded.provider, model = excluded.model, "
        "created_at = excluded.created_at",
        (fingerprint, text or "", provider or "", model or "", _now()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# change tracking
# --------------------------------------------------------------------------- #
def record_segment_link(
    conn: sqlite3.Connection,
    *,
    new_segment_id: Optional[int],
    old_segment_id: Optional[int],
    change_kind: str,
    ratio: float,
    char_diffs: Optional[list] = None,
) -> int:
    """记录新旧段对应关系；同一 (old,new) 组合重复写入时先删再插（可重复执行）。"""
    if change_kind not in CHANGE_KINDS:
        raise ValueError(f"unknown change kind: {change_kind!r}")
    if new_segment_id is None and old_segment_id is None:
        raise ValueError("at least one of new_segment_id / old_segment_id is required")
    value = _float_or_zero(ratio)
    if value < 0.0 or value > 1.0:
        raise ValueError(f"ratio must be in [0, 1], got {ratio!r}")
    conn.execute(
        "DELETE FROM segment_links WHERE new_segment_id IS ? AND old_segment_id IS ?",
        (new_segment_id, old_segment_id),
    )
    cur = conn.execute(
        "INSERT INTO segment_links (new_segment_id, old_segment_id, change_kind, ratio, "
        "char_diffs, created_at) VALUES (?,?,?,?,?,?)",
        (
            new_segment_id, old_segment_id, change_kind, value,
            _dumps(char_diffs if char_diffs is not None else [], "[]"), _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def links_for_version(conn: sqlite3.Connection, version_id: int) -> dict[int, dict]:
    """{new_segment_id: link row}（新版段所属版本的链接）。REMOVED（new 为 NULL）不在此表内，
    需要全部变更请用 `links_for_diff`。"""
    if not version_id:
        return {}
    rows = conn.execute(
        "SELECT l.* FROM segment_links l JOIN segments s ON s.id = l.new_segment_id "
        "WHERE s.version_id = ? ORDER BY l.id",
        (version_id,),
    ).fetchall()
    return {int(r["new_segment_id"]): _link_row(r) for r in rows if r["new_segment_id"] is not None}


def links_for_diff(
    conn: sqlite3.Connection, from_version_id: int, to_version_id: int
) -> list[dict]:
    """该次 diff 的全部链接（含 REMOVED：new_segment_id 为 NULL 且旧段属于 from 版本）。"""
    if not from_version_id or not to_version_id:
        return []
    rows = conn.execute(
        "SELECT l.*, s.page AS new_page, o.page AS old_page, "
        "s.order_index AS new_order, o.order_index AS old_order "
        "FROM segment_links l "
        "LEFT JOIN segments s ON s.id = l.new_segment_id "
        "LEFT JOIN segments o ON o.id = l.old_segment_id "
        "WHERE s.version_id = ? OR (l.new_segment_id IS NULL AND o.version_id = ?) "
        "ORDER BY COALESCE(s.order_index, 1e9), l.id",
        (to_version_id, from_version_id),
    ).fetchall()
    return [_link_row(r) for r in rows]


def diff_summary(
    conn: sqlite3.Connection, doc_id: int, from_version_id: int, to_version_id: int
) -> dict:
    """版本间变更计数（优先实时统计 segment_links，回落 version_diffs 缓存）。"""
    counts: dict[str, int] = {k: 0 for k in CHANGE_KINDS}
    links = links_for_diff(conn, from_version_id, to_version_id)
    source = "links" if links else "empty"
    if links:
        for link in links:
            kind = link.get("change_kind") or "unchanged"
            counts[kind] = counts.get(kind, 0) + 1
        # 没有链接的新版段视为未变（differ 可以不记录 unchanged）
        matched = sum(1 for link in links if link.get("new_segment_id") is not None)
        total_new = count_segments(conn, to_version_id)
        counts["unchanged"] += max(0, total_new - matched)
    else:
        row = conn.execute(
            "SELECT counts FROM version_diffs WHERE doc_id = ? AND from_version_id = ? "
            "AND to_version_id = ?",
            (doc_id, from_version_id, to_version_id),
        ).fetchone()
        if row is not None:
            cached = _loads(row["counts"], {})
            if isinstance(cached, dict) and cached:
                for key in counts:
                    counts[key] = _int_or_zero(cached.get(key, 0))
                source = "cache"
    return {
        "doc_id": int(doc_id or 0),
        "from_version_id": int(from_version_id or 0),
        "to_version_id": int(to_version_id or 0),
        "from": int(from_version_id or 0),
        "to": int(to_version_id or 0),
        "counts": counts,
        "total": count_segments(conn, to_version_id),
        "source": source,
    }


def write_version_diff(
    conn: sqlite3.Connection, *, doc_id: int, from_version_id: int, to_version_id: int,
    counts: dict
) -> None:
    """缓存一次 diff 的计数（可选；供 web/cli 复用）。"""
    conn.execute(
        "INSERT INTO version_diffs (doc_id, from_version_id, to_version_id, counts, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(doc_id, from_version_id, to_version_id) DO UPDATE SET "
        "counts = excluded.counts, created_at = excluded.created_at",
        (int(doc_id or 0), int(from_version_id or 0), int(to_version_id or 0),
         _dumps(counts or {}, "{}"), _now()),
    )
    conn.commit()


def get_version_diff(
    conn: sqlite3.Connection, doc_id: int, from_version_id: int, to_version_id: int
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM version_diffs WHERE doc_id = ? AND from_version_id = ? AND to_version_id = ?",
        (doc_id, from_version_id, to_version_id),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["counts"] = _loads(d.get("counts"), {})
    return d


# --------------------------------------------------------------------------- #
# render snapshots
# --------------------------------------------------------------------------- #
def upsert_render_snapshot(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    kind: str,
    path: str,
    params_hash: str,
    stale: int = 0,
    bytes_: int = 0,
) -> int:
    """按 (version_id, kind, params_hash) 幂等保存渲染产物。返回 snapshot_id。"""
    if not version_id:
        raise ValueError("version_id is required")
    row = conn.execute(
        "SELECT snapshot_id FROM render_snapshots WHERE version_id = ? AND kind = ? "
        "AND params_hash = ?",
        (version_id, kind or "", params_hash or ""),
    ).fetchone()
    now = _now()
    if row is not None:
        sid = int(row["snapshot_id"])
        conn.execute(
            "UPDATE render_snapshots SET path = ?, stale = ?, bytes = ?, created_at = ? "
            "WHERE snapshot_id = ?",
            (path or "", _int_or_zero(stale), _int_or_zero(bytes_), now, sid),
        )
        conn.commit()
        return sid
    cur = conn.execute(
        "INSERT INTO render_snapshots (version_id, kind, path, params_hash, stale, bytes, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (version_id, kind or "", path or "", params_hash or "", _int_or_zero(stale),
         _int_or_zero(bytes_), now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def get_render_snapshot(
    conn: sqlite3.Connection, version_id: int, kind: str, params_hash: str
) -> Optional[dict]:
    if not version_id:
        return None
    row = conn.execute(
        "SELECT * FROM render_snapshots WHERE version_id = ? AND kind = ? AND params_hash = ?",
        (version_id, kind or "", params_hash or ""),
    ).fetchone()
    return _snapshot_row(row) if row is not None else None


def find_render_snapshot(
    conn: sqlite3.Connection, version_id: int, kind: str, *, fresh_only: bool = True
) -> Optional[dict]:
    """不看 params_hash，取该 kind 最新快照（web 图片接口按 dpi 命中失败时的回落）。"""
    if not version_id:
        return None
    sql = ("SELECT * FROM render_snapshots WHERE version_id = ? AND kind = ?"
           + (" AND stale = 0" if fresh_only else "")
           + " ORDER BY snapshot_id DESC LIMIT 1")
    row = conn.execute(sql, (version_id, kind or "")).fetchone()
    return _snapshot_row(row) if row is not None else None


def mark_renders_stale(
    conn: sqlite3.Connection, version_id: int, kinds: Optional[Union[str, Iterable[str]]] = None
) -> None:
    """译文变化后调用：把快照标记过期（kinds=None ⇒ 全部）。"""
    if not version_id:
        return
    kind_list = [kinds] if isinstance(kinds, str) else list(kinds or [])
    if kind_list:
        conn.execute(
            "UPDATE render_snapshots SET stale = 1 WHERE version_id = ? AND kind IN ("
            + ",".join("?" * len(kind_list)) + ")",
            [version_id, *kind_list],
        )
    else:
        conn.execute("UPDATE render_snapshots SET stale = 1 WHERE version_id = ?", (version_id,))
    conn.commit()


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def create_job(conn: sqlite3.Connection, kind: str, params: Any) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO jobs (kind, status, params, progress, total, message, created_at, updated_at) "
        "VALUES (?,?,?,0,0,'',?,?)",
        (kind or "", "queued", _dumps(params, "{}"), now, now),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    total: Optional[float] = None,
    message: Optional[str] = None,
    result: Any = None,
    error: Optional[str] = None,
) -> None:
    """部分更新（None 表示该列不动）。job 不存在时静默 no-op。"""
    if status is not None and status not in JOB_STATUSES:
        raise ValueError(f"unknown job status: {status!r}")
    if not job_id:
        return
    row = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if progress is not None:
        sets.append("progress = ?"); params.append(_float_or_zero(progress))
    if total is not None:
        sets.append("total = ?"); params.append(_float_or_zero(total))
    if message is not None:
        sets.append("message = ?"); params.append(message or "")
    if result is not None:
        sets.append("result = ?"); params.append(_dumps(result, "null"))
    if error is not None:
        sets.append("error = ?"); params.append(error or "")
    sets.append("updated_at = ?"); params.append(_now())
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", params)
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[dict]:
    if not job_id:
        return None
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _job_row(row) if row is not None else None


def list_jobs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 50
    if n <= 0:
        return []
    return _rows(conn.execute("SELECT * FROM jobs ORDER BY job_id DESC LIMIT ?", (n,)), _job_row)


# --------------------------------------------------------------------------- #
# glossary
# --------------------------------------------------------------------------- #
def list_glossary(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM glossary ORDER BY en"), _glossary_row)


def upsert_glossary(
    conn: sqlite3.Connection, *, en: str, zh: str, case_sensitive: int = 0, note: str = ""
) -> int:
    en = (en or "").strip()
    if not en:
        raise ValueError("glossary 'en' must not be empty")
    conn.execute(
        "INSERT INTO glossary (en, zh, case_sensitive, note, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(en) DO UPDATE SET zh = excluded.zh, case_sensitive = excluded.case_sensitive, "
        "note = excluded.note, updated_at = excluded.updated_at",
        (en, zh or "", 1 if case_sensitive else 0, note or "", _now()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM glossary WHERE en = ?", (en,)).fetchone()
    return int(row["id"]) if row is not None else 0


def replace_glossary(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """全量替换（§5 PUT /api/glossary）。返回写入条数。"""
    items = list(rows or [])
    conn.execute("DELETE FROM glossary")
    now = _now()
    payload = [
        (
            str(item.get("en") or "").strip(),
            str(item.get("zh") or ""),
            1 if item.get("case_sensitive") else 0,
            str(item.get("note") or ""),
            now,
        )
        for item in items
        if str(item.get("en") or "").strip()
    ]
    if payload:
        conn.executemany(
            "INSERT INTO glossary (en, zh, case_sensitive, note, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(en) DO UPDATE SET zh = excluded.zh, "
            "case_sensitive = excluded.case_sensitive, note = excluded.note, "
            "updated_at = excluded.updated_at",
            payload,
        )
    conn.commit()
    return len(payload)


def delete_glossary(conn: sqlite3.Connection, en: str) -> None:
    conn.execute("DELETE FROM glossary WHERE en = ?", ((en or "").strip(),))
    conn.commit()
