"""SQLite 连接与 schema（CONTRACT.md §3）。

只做两件事：`connect()` 与 `SCHEMA` 常量 + `user_version` 迁移。
业务 DAO 全在 `core/store.py`。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "SCHEMA",
    "SCHEMA_VERSION",
    "MIGRATIONS",
    "DEFAULT_DB_PATH",
    "connect",
    "ensure_schema",
    "schema_version",
]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "app.db"

SCHEMA_VERSION = 1

# 全部 DDL 必须 IF NOT EXISTS：迁移可以重复执行（§3 要求）。
SCHEMA = """
-- 文档（一个 PDF 系列；doc_id 自增）
CREATE TABLE IF NOT EXISTS documents (
    doc_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL DEFAULT '',
    source_dir  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT ''
);

-- 版本（同一文档的多次 ingest）
CREATE TABLE IF NOT EXISTS document_versions (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    version_no    INTEGER NOT NULL DEFAULT 1,
    label         TEXT    NOT NULL DEFAULT '',
    source_path   TEXT    NOT NULL DEFAULT '',
    sha256        TEXT    NOT NULL DEFAULT '',
    pdf_bytes     INTEGER NOT NULL DEFAULT 0,
    page_count    INTEGER NOT NULL DEFAULT 0,
    release_label TEXT    NOT NULL DEFAULT '',
    doc_date      TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT '',
    is_current    INTEGER NOT NULL DEFAULT 0,
    note          TEXT    NOT NULL DEFAULT '',
    stats         TEXT    NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_versions_doc_no ON document_versions(doc_id, version_no);
CREATE INDEX IF NOT EXISTS ix_versions_doc ON document_versions(doc_id, is_current);
CREATE INDEX IF NOT EXISTS ix_versions_sha ON document_versions(doc_id, sha256);

-- 段（抽取结果；canonical_text 用于跨版本比对，见 §3.1.1）
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id     INTEGER NOT NULL REFERENCES document_versions(version_id) ON DELETE CASCADE,
    doc_id         INTEGER NOT NULL DEFAULT 0,
    page           INTEGER NOT NULL DEFAULT 1,
    order_index    INTEGER NOT NULL DEFAULT 0,
    kind           TEXT    NOT NULL DEFAULT 'paragraph',
    text           TEXT    NOT NULL DEFAULT '',
    canonical_text TEXT    NOT NULL DEFAULT '',
    normalized     TEXT    NOT NULL DEFAULT '',
    fingerprint    TEXT    NOT NULL DEFAULT '',
    bbox           TEXT    NOT NULL DEFAULT '[]',
    line_boxes     TEXT    NOT NULL DEFAULT '[]',
    fonts          TEXT    NOT NULL DEFAULT '[]',
    style          TEXT    NOT NULL DEFAULT '{}',
    role           TEXT    NOT NULL DEFAULT 'body',
    content_hash   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_segments_version_order ON segments(version_id, order_index);
CREATE INDEX IF NOT EXISTS ix_segments_version_page  ON segments(version_id, page);
CREATE INDEX IF NOT EXISTS ix_segments_version_fp    ON segments(version_id, fingerprint);
CREATE INDEX IF NOT EXISTS ix_segments_version_role  ON segments(version_id, role);

-- 译文（每段最多一条当前译文；历史靠 revision 字段，不另存表）
CREATE TABLE IF NOT EXISTS translations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  INTEGER NOT NULL UNIQUE REFERENCES segments(id) ON DELETE CASCADE,
    text        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'machine',
    provider    TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    confidence  REAL    NOT NULL DEFAULT 0.0,
    revision    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT '',
    reviewed_by TEXT    NOT NULL DEFAULT '',
    reviewed_at TEXT    NOT NULL DEFAULT '',
    reuse_of    INTEGER
);
CREATE INDEX IF NOT EXISTS ix_translations_status ON translations(status);

-- 跨版本译文缓存：主键就是 normalize(canonical).casefold() 的指纹
CREATE TABLE IF NOT EXISTS translation_cache (
    fingerprint TEXT PRIMARY KEY,
    text        TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0
);

-- 版本间的段对应关系（differ 写入）
CREATE TABLE IF NOT EXISTS segment_links (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    new_segment_id   INTEGER,
    old_segment_id   INTEGER,
    change_kind      TEXT    NOT NULL DEFAULT 'unchanged',
    ratio            REAL    NOT NULL DEFAULT 0.0,
    char_diffs       TEXT    NOT NULL DEFAULT '[]',
    created_at       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_links_new ON segment_links(new_segment_id);
CREATE INDEX IF NOT EXISTS ix_links_old ON segment_links(old_segment_id);

-- 版本 diff 计数缓存（可选；diff_summary 优先实时统计 segment_links）
CREATE TABLE IF NOT EXISTS version_diffs (
    diff_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL,
    from_version_id INTEGER NOT NULL,
    to_version_id   INTEGER NOT NULL,
    counts          TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL DEFAULT '',
    UNIQUE(doc_id, from_version_id, to_version_id)
);

-- 渲染快照（页面图 / 导出 PDF）
CREATE TABLE IF NOT EXISTS render_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id  INTEGER NOT NULL REFERENCES document_versions(version_id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT '',
    path        TEXT    NOT NULL DEFAULT '',
    params_hash TEXT    NOT NULL DEFAULT '',
    stale       INTEGER NOT NULL DEFAULT 0,
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT '',
    UNIQUE(version_id, kind, params_hash)
);

-- 长任务
CREATE TABLE IF NOT EXISTS jobs (
    job_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'queued',
    params     TEXT    NOT NULL DEFAULT '{}',
    progress   REAL    NOT NULL DEFAULT 0,
    total      REAL    NOT NULL DEFAULT 0,
    message    TEXT    NOT NULL DEFAULT '',
    result     TEXT,
    error      TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, job_id);

-- 术语表
CREATE TABLE IF NOT EXISTS glossary (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    en             TEXT    NOT NULL UNIQUE,
    zh             TEXT    NOT NULL DEFAULT '',
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    note           TEXT    NOT NULL DEFAULT '',
    updated_at     TEXT    NOT NULL DEFAULT ''
);
"""

# (目标 user_version, SQL)。新增迁移只能追加，不能改动历史项。
MIGRATIONS: list[tuple[int, str]] = [(SCHEMA_VERSION, SCHEMA)]


def schema_version(conn: sqlite3.Connection) -> int:
    """当前库的 user_version（0 = 尚未初始化）。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def ensure_schema(conn: sqlite3.Connection) -> int:
    """按需执行未应用的迁移，返回最终 user_version。可重复调用。"""
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
            current = version
    return current


def _default_path() -> str:
    """默认 data/app.db；若 config 模块可用则尊重 MT_DATA_DIR / MT_CONFIG。"""
    try:
        import config  # 同仓库的 [core] 配置模块

        return str(config.db_path())
    except Exception:
        return str(DEFAULT_DB_PATH)


def connect(path: Optional[Union[str, "os.PathLike[str]"]] = None) -> sqlite3.Connection:
    """打开（必要时创建）数据库，自动建表 / 迁移。

    * `path=None` → `data/app.db`（`:memory:` 亦可用，主要给测试）
    * WAL + foreign_keys=ON + Row 工厂
    * 每个线程必须各自 `connect()`（sqlite3 连接不可跨线程共享）
    """
    target = _default_path() if path is None else str(path)
    is_memory = target == ":memory:" or target.startswith("file::memory:")
    if not is_memory:
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
    except sqlite3.DatabaseError:
        pass
    ensure_schema(conn)
    return conn
