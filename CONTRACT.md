# CONTRACT — 冻结接口规范 (v1)

> 本文件是并行开发的**唯一接口真相**。任何人不得单方面修改；需要变更时通知 Lead。
> 所有路径相对仓库根 `E:\worksrc\manual_trans_trace`。Web/SI 单位一律为 PDF point（1/72 inch）。

---

## 0. 技术栈与运行环境（已实测可用）

| 项 | 值 |
|---|---|
| Python | 3.11 (`C:\Users\Brian\AppData\Local\Programs\Python\Python311\python.exe`) |
| 已装包 | pymupdf 1.28.2, Pillow 12.1.1, fastapi 0.135.1, uvicorn 0.41.0, numpy 2.4.6, onnxruntime, playwright 1.58.0 |
| **不可用** | lxml, bs4, reportlab, weasyprint, pdfplumber, torch —— 不要 import |
| 前端 | 原生 ES module + 无构建步骤；由 FastAPI 静态托管。**禁止引入 npm 依赖** |
| HTTP | `urllib.request`（Python 标准库），**不要 pip install** |
| LLM | DeepSeek `https://api.deepseek.com`，模型 `deepseek-v4-pro`（默认）/ `deepseek-flash`，Bearer key 见 `config/local.json` |
| 中文字体 | `C:\Windows\Fonts\Deng.ttf`(等线) `Dengb.ttf`(粗) `simhei.ttf` `msyh.ttc` |
| 拉丁字体 | `C:\Windows\Fonts\calibri.ttf` `calibrib.ttf` `calibrii.ttf` `arial.ttf` `times.ttf` |

> ⚠️ **沙箱注意**：`python -c "..."` 直连 stdout 正常；但 `python x.py | Select-Object` 会被沙箱拒绝（EPERM）。
> 需要过滤输出时请把结果写文件再读，或直接全量输出。同理 Python 内 `subprocess` 带 pipe 可能 EPERM，
> 统一用 `stdio="ignore"`/`inherit` 或写文件。

---

## 1. 仓库布局（与写作用域）

```
config/__init__.py            [core]  local.json 读取 + 默认值
config/local.json             [lead]  {"deepseek_api_key": "...", "model": "deepseek-v4-pro"}
core/__init__.py
core/models.py                [core]  数据类 + 枚举
core/db.py                    [core]  SQLite schema/连接/迁移
core/store.py                 [core]  DAO：段、译文、版本、快照、任务
core/pdfdoc.py                [core]  真实 PDF 几何工具（见 §6）
core/fingerprint.py           [core]  归一化 / 指纹 / 相似度
versions/__init__.py
versions/differ.py            [version] 版本间段匹配 + 变更集
translate/__init__.py
translate/glossary.py         [translate] 术语表加载 + 强制替换
translate/prompt.py           [translate] 提示词 + 响应解析
translate/engine.py           [translate] DeepSeek/占位 双后端 + 并发 + 缓存
render/__init__.py
render/pagebuild.py           [render] 元素级重建（§6）
render/pdfout.py              [render] 中文 PDF / 双语 PDF 导出
web/__init__.py
web/server.py                 [web]  FastAPI 应用 + REST API（§5）
web/static/**                 [webui] index.html / app.js / style.css / render.js
cli.py                        [lead]  命令行入口
pipeline.py                   [lead]  端到端编排
tests/fixtures.py             [core]  合成测试 PDF
tests/test_core.py            [core]
tests/test_versions.py        [version]
tests/test_translate.py       [translate]
tests/test_render.py          [render]
tests/test_web.py             [web]
tests/test_e2e.py             [lead]
data/                         [runtime] app.db / pdfs/ / derived/ / cache/
start_web.ps1                 [lead]
README.md                     [lead]
CONTRACT.md                   [lead]
```

---

## 2. `core/models.py` — 数据模型

```python
from dataclasses import dataclass, field
from typing import Any, Optional
import enum

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
    MODIFIED  = "modified"
    ADDED     = "added"
    REMOVED   = "removed"
    MOVED     = "moved"       # 文本未变，页码变化
    REORDERED = "reordered"   # 文本未变，仅页内顺序变化
    SPLIT     = "split"       # 1 旧 -> N 新
    MERGED    = "merged"      # N 旧 -> 1 新

class TranslationStatus(str, enum.Enum):
    MISSING   = "missing"     # 尚未翻译
    MACHINE   = "machine"     # 机器翻译，未复核
    CARRIED   = "carried"     # 原文未变，沿用旧译文
    SEEDED    = "seeded"      # 原文小改，基于旧译修订
    REVIEWED  = "reviewed"    # 人工复核通过
    FAILED    = "failed"

class JobStatus(str, enum.Enum):
    QUEUED = "queued"; RUNNING = "running"; DONE = "done"; FAILED = "failed"; CANCELLED = "cancelled"

@dataclass
class Span:
    text: str
    bbox: list[float]        # [x0,y0,x1,y1]
    origin: list[float]      # [x, y] baseline start
    size: float
    font: str
    color: int               # 0xRRGGBB
    flags: int = 0

@dataclass
class Segment:
    id: int = 0
    version_id: int = 0
    doc_id: int = 0
    page: int = 1            # 1-based
    order_index: int = 0     # 版本内全局顺序
    kind: str = "paragraph"
    text: str = ""           # 展示/翻译用文本（括号式行尾连字符**保留**）
    canonical_text: str = "" # 跨版本比对用：行尾连字符已复原（见 §4.1）。diff 必须用它
    normalized: str = ""
    fingerprint: str = ""
    bbox: list[float] = field(default_factory=list)
    line_boxes: list[list[float]] = field(default_factory=list)
    fonts: list[dict] = field(default_factory=list)   # [{font,size,color,flags,count}]
    style: dict = field(default_factory=dict)         # {bold,italic,size,color,align,list_level,indent}
    role: str = "body"       # body|header|footer|pagenum|noise
    content_hash: str = ""

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
```

**`char_diffs` 元素格式**（供前端与导出共用）：
```python
{"op": "equal"|"delete"|"insert", "text": str}
```
基于 `difflib.SequenceMatcher(None, old_norm, new_norm).get_opcodes()`。

---

## 3. `core/db.py` / `core/store.py`

SQLite 文件 `data/app.db`，`PRAGMA journal_mode=WAL`，`PRAGMA foreign_keys=ON`。
`core/db.py` 只负责：`connect(path=None) -> sqlite3.Connection`（自动建表）与 `SCHEMA` 常量。
`core/store.py` 提供以下**必须实现**的函数（签名不得更改）：

```python
# --- documents & versions ---
def upsert_document(conn, *, slug, title, source_dir) -> int
def get_document(conn, doc_id) -> Optional[dict]
def list_documents(conn) -> list[dict]
def register_version(conn, *, doc_id, source_path, sha256, pdf_bytes, page_count,
                     release_label="", doc_date="", note="", label="",
                     make_current=True) -> int            # 返回 version_id
def update_version_stats(conn, version_id, *, segments=None, chars=None, pages=None, images=None)
def get_version(conn, version_id) -> Optional[dict]
def list_versions(conn, doc_id) -> list[dict]           # 按 version_no 升序
def current_version(conn, doc_id) -> Optional[dict]
def set_current_version(conn, doc_id, version_id) -> None

# --- segments ---
def insert_segments(conn, segs: list[Segment]) -> list[int]      # 批量，返回 id 列表（与输入同序）
def get_segment(conn, segment_id) -> Optional[dict]
def segments_of_version(conn, version_id, *, page=None, kinds=None) -> list[dict]  # order_index 升序
def count_segments(conn, version_id) -> int

# --- translations ---
def get_translation(conn, segment_id) -> Optional[dict]
def translations_of_version(conn, version_id) -> dict[int, dict]  # segment_id -> row
def upsert_translation(conn, *, segment_id, text, status, provider="", model="",
                       confidence=0.0, reuse_of=None) -> int
def set_translation_status(conn, segment_id, status, *, reviewed_by="") -> None
def translation_stats(conn, version_id) -> dict     # {"total":n,"translated":n,"by_status":{...}}

# --- change tracking ---
def record_segment_link(conn, *, new_segment_id, old_segment_id, change_kind, ratio,
                        char_diffs=None) -> int
def links_for_version(conn, version_id) -> dict[int, dict]      # new_segment_id -> link row
def diff_summary(conn, doc_id, from_version_id, to_version_id) -> dict

# --- render snapshots ---
def upsert_render_snapshot(conn, *, version_id, kind, path, params_hash, stale=0, bytes_=0) -> int
def get_render_snapshot(conn, version_id, kind, params_hash) -> Optional[dict]
def mark_renders_stale(conn, version_id, kinds=None) -> None

# --- jobs ---
def create_job(conn, kind, params) -> int
def update_job(conn, job_id, *, status=None, progress=None, total=None, message=None, result=None, error=None) -> None
def get_job(conn, job_id) -> Optional[dict]
def list_jobs(conn, limit=50) -> list[dict]
```

**约定**：所有 `*_at` 时间为 ISO8601 本地字符串；`params`/`result`/`stats`/`char_diffs` 以 JSON 文本存库，DAO 对外返回已 `json.loads` 的对象。
`segments` 表需有 `canonical_text` 列（`text` 与 `fingerprint` 之间）。`store.insert_segments` 若调用方没填
`canonical_text`/`normalized`/`fingerprint`/`content_hash`，要**自动补算**（用 `core.fingerprint`），避免调用方遗漏。

---

## 3.1 真实 PDF 实测结构（`manual_trans_trace/origin/BMS-Training-Manual.pdf`，401 页）

Lead 已用 401 页真实文件逐页统计，**以下事实请直接采用，不要重新猜**：

| 事实 | 值 |
|---|---|
| 页面尺寸 | 595.32 × 841.92 pt（A4），rotation 0，mediabox == cropbox |
| 页眉 banner 图 | image xref **24**，bbox `[0.4, 0.0, 652.4, 65.2]`（超出页宽，为背景图放大） |
| 页脚 banner 图 | image xref **23**，bbox `[-0.2, 776.1, 654.8, 841.6]` |
| 正文字区 | y ∈ [65.2, 776.1]（实际内容 y ∈ [70, 770]） |
| 页眉文字 | `BMS TRAINING MANUAL ` / `4.38.1 `，Calibri-Bold 12pt，**白色** #ffffff |
| 页脚页码 | 数字，Calibri-Bold 12pt，白色 |
| 正文 | Calibri 10pt #000000；标题 Calibri-Bold 10/12/14pt；章节号 Cambria-BoldItalic #4f81bd |
| 字体分布 top | Calibri@10 (12248), ArialMT@10 (3317), Calibri-Bold@12 (1688), Calibri@9 (1547) |
| 模板图识别 | 「出现在 > 80% 页面上的 image xref」= {23, 24} |
| 图片数 | 正文内联图很多；**不可丢失** |

**`role` 判定（实测可靠）**
```python
if bbox.y1 <= 66 and color == 0xFFFFFF:  role = "header"
elif bbox.y0 >= 776:                     role = "footer"   # 页码单独标 "pagenum"
else:                                    role = "body"
```

### 3.1.1 ⚠️ 行尾连字符（**必须处理，否则跨版本匹配会崩**）
全书 **179 行**以 `-` 结尾，其中绝大多数是 Word 的软换行连字符，例如：
```
blk6 ln0: 'The MASTER CAUTION light activates shortly after any individual light on the Cau-'
blk6 ln1: 'tion Panel illuminates (excluding IFF). It does not activate simultaneously with the '
blk6 ln2: 'warning lights. '
blk8 ln1: 'this method, you can fine-tune specific sounds, such as reducing Betty sounds with the INTERCOM volume while increas-'
blk8 ln2: 'ing relevant COMMS volume. '
```
同时全书有**大量真实复合连字符**：`ANTI-`(p25 独立行)、`Dash-34`、`F-16`、`A-LOW`、`FTIT`、`MFL`、`elEC-`…
以及 `blk10 ln0: '- '` 这种**项目符号**（单独一行，x0=71.9）。

**判定规则（写进 `pipeline.extract_segments` 与 `core.fingerprint.normalize`）**
* 段落内：若**行 i 以 `-` 结尾** 且 **行 i+1 首字符是 `[a-z]`**（小写字母，说明是同一个词被切开）
  → 视为软连字符，拼段时**删除该 `-`** 并直接拼接下一行。
* 例外（不删）：下一行首字符是大写字母，或 `-` 前后构成的词在 `KNOWN_HYPHEN_WORDS = {"F-16","Dash-34","ANTI-","A-LOW","A-A","S-D","T-O","G-","B-","NORM-","ELEC-","MAL-"}` 中，
  或该行就是单独的项目符号行。
* `Segment.text` 保留原始换行效果（去掉多余空白，但连字符按上面的规则**已处理**）；
  `Segment.canonical_text` = 再统一引号/破折号/空白后的结果；`fingerprint = blake2b(canonical_text.casefold())`。
* **diff 引擎必须用 `canonical_text`**（不是 `text`），否则用户改一个词导致重排时会被误判为整段重写。

### 3.1.2 空 span 噪声（需清理）
抽取时**存在大量纯空白 span**，且由 `ArialMT`/`Calibri`/`Wingdings-Regular` 等产生，x1 竟然很宽（例：p15 有
`ArialMT 10pt bbox=[442.2, 217.7, 444.9, 228.8] text=' '`，还有 `bbox=[516.1, 370.9, 518.4, 380.9]`）。
**必须过滤** `not span["text"].strip()`，否则段落 bbox 会被撑宽、分段错误。

### 3.1.3 分块与分段实测
* 同一个 PDF block 内可以包含**多个语义段**（例：p24 blk12 是一个整体 block，内部含 12 个 `• ` 项目符号列表项，
  项目符号在 `x0=71.9`、正文在 `x0=89.9`）。
* 段落切分信号（实测有效）：
  1. 行首 `x0` 明显右移（> 6pt）→ 新段（列表缩进）；
  2. 行本身就是项目符号（`-`/`•`/`▪`/`◆`/`※`，x1-x0 < 12pt）→ 以下一行开始的段归为 `list_item`；
  3. `size` 或 `font` 权重变化 → 新段；
  4. 与上一行基线间距 > 1.6 × 上一下行距 → 新段（空行）；
  5. 编号模式 `^\d+\.\d*(\.\d+)*\s` 且字号为该块内最小 → `heading`；纯文本短行且 bold → `heading`。
* 标题块常见形态：`blk5 ln0 '1.4.1 ' (x1=78.4) + ln1 'MASTER CAUTION light ' (x0=89.9, 同 y0=392.2)`
  —— 同一 y 上的两段应当**合并为一个 heading 段**（`1.4.1 MASTER CAUTION light`）。
* 页眉块（blk2）内含**两个**逻辑段（`BMS TRAINING MANUAL` + `4.38.1`），同一 block 内 `x0` 相同但 `y0` 差 14.6pt
  → 判为两个 `role=header` 段（都跳过翻译，保留原样绘制）。

---

## 4. `core/fingerprint.py`

```python
def normalize(text: str) -> str:
    """NFKC；统一引号/破折号/省略号；折叠所有空白为单空格；去首尾；保留大小写。"""

def fingerprint(text: str) -> str:
    """blake2b(normalize(text).casefold(), digest_size=16).hexdigest() —— 用于跨版本内容匹配"""

def content_hash(text: str) -> str:
    """blake2b(normalize(text), digest_size=16).hexdigest() —— 保留大小写，用于精确变更判定"""

def ratio(a: str, b: str) -> float:
    """difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()"""

def char_diffs(old: str, new: str) -> list[dict]:
    """返回 §2 char_diffs 格式的 opcode 列表（合并相邻同类 op）"""

def tokenize_cjk(text: str) -> list[tuple[str, str]]:
    """切成 [(kind, run)]，kind ∈ {"cjk","latin","digit","space","punct"}；用于混合字体排版"""

def dehyphenate(lines: list[str]) -> str:
    """把同一段落的多行文本拼成一段，按 §3.1.1 规则处理行尾软连字符。"""
```

### 4.1 `dehyphenate` 规则（**已用 401 页真实数据验证必要**）
```python
KNOWN_HYPHEN_PREFIX = {"F", "An", "Dash", "ANTI", "A", "B", "S", "T", "G", "N", "MAL", "ELEC",
                       "NORM", "M", "Q", "TAC", "RT", "IDM", "FLCS", "MFD", "PFL", "VMS"}
# 行 i 以 "-" 结尾且 行 i+1 首字符为小写字母 且 行 i 末尾词的前缀不在 KNOWN_HYPHEN_PREFIX 中
#   -> 删除 "-" 并直接拼接
# 否则 -> 保留 "-" 并加一个空格拼接
```
**不要**在 `normalize()` 里做连字符处理（那是段内行拼接阶段的事，`dehyphenate` 之后再 `normalize`）。

---

## 5. `web/server.py` — REST API（前端按此实现，不得擅改）

所有响应 `application/json; charset=utf-8`。出错返回 `{"error": str}` + 4xx/5xx。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/health` | – | `{"ok":true,"version":"1","db":"data/app.db"}` |
| GET | `/api/documents` | – | `[{doc_id,slug,title,current_version_id,version_count,page_count,stats}]` |
| POST | `/api/documents` | `{"pdf_path":"origin/x.pdf","title":"...","slug":"..."}` | `{doc_id, version_id, pages, segments}`；抓取抽取任务 |
| GET | `/api/documents/{doc_id}/versions` | – | `[{version_id,version_no,label,...}]` |
| GET | `/api/documents/{doc_id}/versions/{vid}/outline` | `?depth=` | `[{page,seg_id,kind,text,translation}]` 按 heading 层级 |
| GET | `/api/documents/{doc_id}/versions/{vid}/segments` | `?page=&page_from=&page_to=&kinds=&role=` | `[{seg_id,page,order_index,kind,bbox,line_boxes,text,role,style,translation:{text,status,revision}|null}]` |
| GET | `/api/documents/{doc_id}/diff` | `?from=&to=` | `{from,to,counts,changes:[{change_id,kind,ratio,old_page,new_page,old_text,new_text,char_diffs,summary}]}` |
| GET | `/api/documents/{doc_id}/versions/{vid}/pages/{page}/image` | `?dpi=&kind=source\|cn\|bilingual` | `image/webp`（缺失时按需生成并缓存） |
| GET | `/api/documents/{doc_id}/versions/{vid}/pages/{page}/markers` | – | `{page,width,height,dpi,units_per_px,items:[{seg_id,kind,kind_group,bbox,line_boxes,role,change_kind,change_id}]}` |
| GET | `/api/documents/{doc_id}/versions/{vid}/pages/{page}/translated-layout` | – | `{page,width,height,font_file,boxes:[{seg_id,bbox,paragraph_rect,lines:[{text,x,y,size,font_slot,width}],color,bg,shrink}]}` |
| POST | `/api/translate` | `{"doc_id","version_id","segment_ids":[...]\|null,"page_from","page_to","provider"}` | `{job_id}` |
| POST | `/api/documents/upload` | multipart `file` | 同 `/api/documents` |
| GET | `/api/jobs` / `/api/jobs/{id}` | – | `{job_id,kind,status,progress,total,message,error,result}` |
| GET | `/api/documents/{doc_id}/versions/{vid}/export` | `?kind=cn\|bilingual` | 若已存在返回 `{path,bytes}`；否则 `{job_id}` |
| GET | `/api/documents/{doc_id}/versions/{vid}/download` | `?kind=cn\|bilingual` | `application/pdf` 附件 |
| POST | `/api/review` | `{"segment_id","text","status"}` | `{ok:true}` |
| GET | `/api/glossary` / PUT | – | `[{en,zh,case_sensitive,note}]` |

**约定**
* `seg_id` 一律为 **int**。
* `bbox` 为 `[x0,y0,x1,y1]`，单位 point，原点左上；`markers` 额外返回 `dpi` 与 `units_per_px = 72/dpi`，前端用 `left = (x0 - crop_x0)/units_per_px` 定位。
* `/translated-layout` 的 `lines` 已由服务端完成断行；前端**只负责逐行绘制**，不要再次换行（保证与 PDF 导出最终一致）。
* 长任务（抽取、翻译、导出）一律走 `jobs`，POST 立即返回 `job_id`。

---

## 6. `core/pdfdoc.py` + `render/pagebuild.py` — 重建与绘制

**已由 Lead 用真实页面验证的流程**（`spikes/rebuild.py`，输出见 `spikes/out/p21_rebuild.png`）：

1. `new_page(width=src.rect.width, height=src.rect.height)`（**不要** `show_pdf_page`，那会整页栅格化）。
2. `page.get_image_info(xrefs=True)` → 每个 placement 用 `page.insert_image(Rect(bbox), pixmap=Pixmap(doc, xref))` 重放。
3. `page.get_drawings()` → `Shape` 重放（`l`/`re`/`c`/`qu` 四种 item，见 `spikes/rebuild.py`）。
4. 文本层：原文用 `TextWriter` + Calibri；译文用混合字体逐 run 追加。

**必须提供**：

```python
# core/pdfdoc.py
def open_pdf(path: str): ...
def page_size(page) -> tuple[float, float]
KNOWN_CJK = ["C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/simhei.ttf"]
KNOWN_LATIN = {"regular":"C:/Windows/Fonts/calibri.ttf", "bold":"C:/Windows/Fonts/calibrib.ttf",
               "italic":"C:/Windows/Fonts/calibrii.ttf", "serif":"C:/Windows/Fonts/times.ttf"}
def resolve_cjk_font() -> str      # 返回可用的中文字体文件路径，找不到抛 RuntimeError
def char_width(font, size, ch) -> float
def mixed_text_width(text, fs, bold=False) -> float          # 混合中英宽度
def layout_mixed(text, fs, max_width, bold=False) -> list[tuple[str,str,float]]
    """贪心断行 + 每 run 字体归属。返回 [(line_text, font_slot, width)]，font_slot ∈ {"cjk","latin","latin_bold"}"""
def paragraph_kind(spans, prev_spans) -> str    # 启发式判定 SegmentKind
def is_banner_image(info, page, doc) -> bool    # 全宽贴边的大图 → 视为模板
```

```python
# render/pagebuild.py
def rebuild_page(doc, page_no, *, erase_roles=("body",)) -> pymupdf.Page
def draw_original_text(page, spans) -> None
def draw_translated(page, *, boxes, cjk_font_path) -> dict   # 返回 {"drawn":n,"shrunk":n,"overflow":n}
def render_page_png(doc_or_path, page_no, *, dpi=110) -> bytes
def build_translated_page(src_doc, page_no, segments, translations, *, cjk_font_path) -> pymupdf.Page
```

**排版规则（硬性）**
* 译文起点 = 原段首行 `origin`（`x0` 用 `bbox.x0`），行距 = `fontsize * 1.24`。
* 字号从 `原字号 * 0.92` 起，若超出 `可用高度 = 原 bbox 高度 + 段后间距(下一段 y0 - 本段 y1，上限 8pt)` 则按 0.94 递减，下限 `原字号 * 0.55`。
* **绝不越过下一段的 `bbox.y0`**；实在放不下时在末尾追加 `…` 并计入 `overflow`。
* 原段文字必须完全擦除（该 bbox 内先 `draw_rect(fill=背景色, color=None)`）。
* 背景色 = 该 bbox 在原页渲染图上的众数像素色（量化到 16 级）。
* 文字色：背景相对亮度 > 0.55 用 `0x1a1a1a`，否则用 `0xf2f2f2`。
* 保留原文中的拉丁缩写、型号、编号：`F-16`、`OBOGS`、`MFD`、`STPT` 等按原样出现在译文中（由提示词保证）。

---

## 7. `translate/engine.py`

```python
def translate_segments(conn, doc_id, version_id, *, segment_ids=None, page_from=None, page_to=None,
                       provider=None, model=None, concurrency=6, force=False,
                       job_id=None, cfg=None) -> dict
    """返回 {"translated":n,"carried":n,"cached":n,"failed":n,"chars":n,"tokens":{"prompt":n,"completion":n}}"""
```

* **provider**：`"deepseek"`（默认，读 `config/local.json` 的 key）| `"mock"`（离线占位，逐词查术语表，未命中保留原文并在前后加 `〔〕`，**必须**能在无网络下跑通全流程）。
* 跳过 `role != "body"` 的段（页眉页脚页码），以及纯符号/纯数字/长度 < 2 的段（直接落库为 `CARRIED`，文本=原文）。
* 请求：`POST /chat/completions`，`response_format={"type":"json_object"}`，JSON 形如
  `{"translations":[{"id":<seg_id>,"zh":"..."}]}`；system 提示词要求：
  保留型号/缩写/编号/单位/符号，术语按术语表，纯文本输出不解释，段落数与 id 一一对应。
* **每个批次 12 段**（或 ≤1800 字符，先到为准）；HTTP 超时 120s；失败重试 3 次（指数退避 1/2/4s）。
* 并发用 `concurrent.futures.ThreadPoolExecutor`，**每批独立 `conn`**（`sqlite3` 连接不可跨线程共享；
  用 `core.db.connect()` 新建，完成后 `close`）。写回时用 `store.upsert_translation`。
* **缓存与增量**：
  1. 若同版本该段已有译文且 `force=False` → 跳过。
  2. 否则查 `translation_cache(fingerprint)`：命中 → 写 `status=CARRIED`，`reuse_of` 记来源。
  3. 否则若存在旧版本链接（`store.links_for_version` 反查）且旧段有译文且 `ratio >= 0.72`
     → 作为 `seed` 交给模型（提示词附加「上一版英文 + 上一版中文译文，请修订」），结果 `status=SEEDED`。
  4. 否则正常翻译 → `status=MACHINE`。
  5. 每次成功写回后 `INSERT OR REPLACE INTO translation_cache(fingerprint, text, provider, model, created_at)`。

---

## 8. `versions/differ.py`

```python
def diff_versions(conn, doc_id, from_version_id, to_version_id) -> VersionDiff
```
算法（必须稳定、可重复）：
1. 取两侧 `segments_of_version`（`order_index` 升序，仅 `role=="body"`）。
2. **锚点匹配**：先按 `fingerprint` 精确匹配（同 key 一一配对，取位置最接近者）。
3. **顺序 diff**：对未匹配段按 `normalize()` 文本序列做 `difflib.SequenceMatcher` 的 `get_opcodes()`。
4. `replace` 块内做贪心模糊配对：`ratio >= 0.55` 配对为 `MODIFIED`，其余拆成 `REMOVED`/`ADDED`。
5. `delete` → `REMOVED`；`insert` → `ADDED`；1:N → `SPLIT`；N:1 → `MERGED`。
6. 配对成功的段再判 `MOVED`（page 变化且文本相同）或 `REORDERED`（同页 order_index 反转）。
7. 写 `store.record_segment_link`，`char_diffs` 用 `fingerprint.char_diffs`。
8. `counts` 形如 `{"unchanged":n,"modified":n,"added":n,"removed":n,"moved":n,"reordered":n,"split":n,"merged":n}`。

---

## 9. 前端 `web/static/`（`[webui]` 所有）

**布局**：顶部工具条（文档/版本/模式切换/进度）+ 左侧「原文」+ 右侧「译文」双栏 + 左侧可折叠目录。
**对照逻辑（核心）**
* 左右各自渲染「页面图片 + 绝对定位热区层 + 译文行覆盖层」。
* 双栏滚动**按页同步**：以 `segments` 的 `page` 为锚，滚动任一栏到某页时另一栏滚到对应页 `seg_id` 的 y 位置（比例插值）。
* **点击/悬浮任一栏的文字块** → 该块加 `.sel` 高亮，另一栏对应块加 `.peer` 高亮并 `scrollIntoView({block:'center'})`。
* 顶栏可切「只显示变更」；变更段按 `change_kind` 加左侧 3px 色条：
  `added` 绿 `#22c55e` / `removed` 红 `#ef4444` / `modified` 橙 `#f59e0b` / `moved` 紫 `#a855f7` / `unchanged` 无。
* 新增/删除段以「幽灵块」显示（无对应侧时用虚线框 + 原文摘要）。
* 修改段在区块内展示行内 diff：删除线红字 + 绿字插入。
* 右下角浮动面板：选中段的原文/译文/状态/版本历史（`translation.revision`、`change_kind`、`ratio`）。
* 译文未完成时在右侧显示原文淡色占位 + `⏳ 待翻译`。

**要求**：无外部 CDN、无构建步骤；ES module；`fetch` 相对路径；支持 `?doc=1&vid=2&page=15&seg=123` 深链（刷新后定位一致）。
样式走深色主题（`#0f1115` 背景），中文无衬线。**必须**在 1440×900 下无横向滚动条。

---

## 10. 验收基线

* `python cli.py ingest --pdf origin/BMS-Training-Manual.pdf` 全 401 页成功，段数 > 3000，耗时 < 8 分钟。
* `python cli.py translate --page-to 30 --provider deepseek` 成功产出译文，无 `FAILED`。
* `python cli.py export --kind cn` / `--kind bilingual` 生成可打开 PDF，页数与原文一致（401）。
* `python cli.py diff --from 1 --to 2` 在合成第二版上给出正确计数。
* `python cli.py serve` 后 curl 全部 API 返回 200；浏览器截图 1440×900 显示双栏 + 联动高亮。
