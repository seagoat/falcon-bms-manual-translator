"""可移植性：把 `data/app.db` 里的绝对路径重写为**相对于仓库根**的路径。

背景：documents.source_dir 与 document_versions.source_path 存的是创建时的绝对路径
（例如 E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf）。
把仓库复制到别的机器/目录后这些路径失效，页面图渲染会 404。

约定：DB 里存「相对于仓库根的路径」时，统一用 `/` 分隔且**不带**前导盘符，
例如 `origin/BMS-Training-Manual.pdf`。绝对路径（含盘符或前导 /）视为需要在
运行时解析的旧数据 —— 读取方（web/server.py 等）应调用 `resolve_source_path()`。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def is_absolute(p: str) -> bool:
    return bool(p) and bool(_ABS_RE.match(p))


def to_rel(p: str, *, root: Path | None = None) -> str:
    """把绝对路径转成相对于仓库根的 POSIX 风格路径；已在根外则原样返回。"""
    root = (root or ROOT).resolve()
    if not p:
        return p
    try:
        rp = Path(p).resolve()
        return rp.relative_to(root).as_posix()
    except (ValueError, OSError):
        # 不在仓库内（例如 PDF 放在 D:\manuals）→ 保留绝对路径
        return p


def resolve(p: str, *, root: Path | None = None) -> str:
    """把 DB 里存的路径解析成本机绝对路径（相对路径基于仓库根）。"""
    root = (root or ROOT).resolve()
    if not p:
        return p
    if is_absolute(p):
        return p                      # 旧数据里的绝对路径，直接用
    return str(root / Path(p))


def rewrite_db(conn, *, root: Path | None = None, apply: bool = False) -> dict:
    """把 documents.source_dir / document_versions.source_path 重写为相对路径。

    返回统计；apply=False 时只做演练（不写库）。
    """
    root = (root or ROOT).resolve()
    stat = {"documents": 0, "versions": 0, "outside_root": []}

    for row in conn.execute("SELECT doc_id, source_dir FROM documents").fetchall():
        old = row["source_dir"] or ""
        if not old or not is_absolute(old):
            continue
        new = to_rel(old, root=root)
        if new == old:
            stat["outside_root"].append(old)
            continue
        if apply:
            conn.execute("UPDATE documents SET source_dir=? WHERE doc_id=?",
                         (new, row["doc_id"]))
        stat["documents"] += 1

    for row in conn.execute(
            "SELECT version_id, source_path FROM document_versions").fetchall():
        old = row["source_path"] or ""
        if not old or not is_absolute(old):
            continue
        new = to_rel(old, root=root)
        if new == old:
            stat["outside_root"].append(old)
            continue
        if apply:
            conn.execute("UPDATE document_versions SET source_path=? WHERE version_id=?",
                         (new, row["version_id"]))
        stat["versions"] += 1

    if apply:
        conn.commit()
    return stat
