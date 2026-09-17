"""数据模型：枚举 + dataclass（CONTRACT.md §2 冻结）。

其它模块一律按字段**名**取值，因此这里不额外重命名、不改类型；
新增字段只能追加在末尾并带默认值，避免破坏位置参数构造。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class SegmentKind(str, enum.Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    CAPTION = "caption"
    HEADER = "header"      # 页眉（默认不翻译）
    FOOTER = "footer"      # 页脚（默认不翻译）
    PAGE_NUM = "page_num"
    OTHER = "other"


class ChangeKind(str, enum.Enum):
    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"
    MOVED = "moved"          # 文本未变，页码变化
    REORDERED = "reordered"  # 文本未变，仅页内顺序变化
    SPLIT = "split"          # 1 旧 -> N 新
    MERGED = "merged"        # N 旧 -> 1 新


class TranslationStatus(str, enum.Enum):
    MISSING = "missing"     # 尚未翻译
    MACHINE = "machine"     # 机器翻译，未复核
    CARRIED = "carried"     # 原文未变，沿用旧译文
    SEEDED = "seeded"       # 原文小改，基于旧译修订
    REVIEWED = "reviewed"   # 人工复核通过
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 供 store/上层做参数校验使用，避免各模块重复写字符串字面量
SEGMENT_KINDS: tuple[str, ...] = tuple(k.value for k in SegmentKind)
CHANGE_KINDS: tuple[str, ...] = tuple(k.value for k in ChangeKind)
TRANSLATION_STATUSES: tuple[str, ...] = tuple(s.value for s in TranslationStatus)
JOB_STATUSES: tuple[str, ...] = tuple(s.value for s in JobStatus)

# 默认不翻译的角色（页眉页脚页码），翻译引擎与渲染层共用
NO_TRANSLATE_ROLES: tuple[str, ...] = ("header", "footer", "pagenum", "page_num", "noise")


@dataclass
class Span:
    text: str
    bbox: list[float]        # [x0,y0,x1,y1]
    origin: list[float]      # [x, y] baseline start
    size: float
    font: str
    color: int               # 0xRRGGBB
    flags: int = 0

    def as_dict(self) -> dict[str, Any]:
        """转成 pymupdf `get_text("dict")` 的 span 形状，便于两种输入混用。"""
        return {
            "text": self.text,
            "bbox": list(self.bbox),
            "origin": list(self.origin),
            "size": self.size,
            "font": self.font,
            "color": self.color,
            "flags": self.flags,
        }


@dataclass
class Segment:
    id: int = 0
    version_id: int = 0
    doc_id: int = 0
    page: int = 1            # 1-based
    order_index: int = 0     # 版本内全局顺序
    kind: str = "paragraph"
    text: str = ""            # 展示/翻译用文本（行尾连字符按 §4.1 已处理）
    canonical_text: str = ""  # 跨版本比对用（见 §3.1.1）；diff 必须用它
    normalized: str = ""
    fingerprint: str = ""
    bbox: list[float] = field(default_factory=list)
    line_boxes: list[list[float]] = field(default_factory=list)
    fonts: list[dict] = field(default_factory=list)   # [{font,size,color,flags,count}]
    style: dict = field(default_factory=dict)         # {bold,italic,size,color,align,list_level,indent}
    role: str = "body"       # body|header|footer|pagenum|noise
    content_hash: str = ""

    def as_row(self) -> dict[str, Any]:
        """DAO 友好的字典视图（键与 segments 表列同名）。"""
        return {
            "id": self.id,
            "version_id": self.version_id,
            "doc_id": self.doc_id,
            "page": self.page,
            "order_index": self.order_index,
            "kind": self.kind,
            "text": self.text,
            "canonical_text": self.canonical_text,
            "normalized": self.normalized,
            "fingerprint": self.fingerprint,
            "bbox": list(self.bbox),
            "line_boxes": [list(r) for r in self.line_boxes],
            "fonts": [dict(f) for f in self.fonts],
            "style": dict(self.style),
            "role": self.role,
            "content_hash": self.content_hash,
        }


@dataclass
class Translation:
    id: int = 0
    segment_id: int = 0
    text: str = ""
    status: str = "machine"
    provider: str = ""
    model: str = ""
    confidence: float = 0.0
    revision: int = 1
    created_at: str = ""
    reviewed_by: str = ""


@dataclass
class DocVersion:
    id: int = 0
    doc_id: int = 0
    version_no: int = 1
    label: str = ""          # "v1" / "v2"
    source_path: str = ""
    sha256: str = ""
    pdf_bytes: int = 0
    page_count: int = 0
    release_label: str = ""  # "4.38.1"（来自 PDF 元数据/文件名）
    doc_date: str = ""
    created_at: str = ""
    is_current: int = 0
    note: str = ""
    stats: dict = field(default_factory=dict)   # {segments, chars, pages, images}


@dataclass
class Change:
    kind: str
    old_segment_id: Optional[int]
    new_segment_id: Optional[int]
    ratio: float = 0.0                 # 0..1 similarity (1 = identical)
    char_diffs: list[dict] = field(default_factory=list)
    old_page: Optional[int] = None
    new_page: Optional[int] = None
    summary: str = ""


@dataclass
class VersionDiff:
    doc_id: int
    from_version_id: int
    to_version_id: int
    changes: list[Change] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
