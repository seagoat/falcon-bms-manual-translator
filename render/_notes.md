# render/ 实现说明（task-4 / CONTRACT §6）

本文记录渲染层的实现决策、实测数据与已知限制。**接口以 CONTRACT.md §6 为准**，
这里的补充都是"为什么这样做"和"怎么复现验证"。

---

## 1. 公开接口

### `render/pagebuild.py`

| 函数 | 说明 |
|---|---|
| `rebuild_page(doc, page_no, *, erase_roles=("body",), drop_text=False, erase_rects=None, target_doc=None, target_page=None, page_rect=None, dx=0.0, dy=0.0)` | 元素级重建：图片 + 矢量 + 文本。返回新页（`page.parent` 即新文档） |
| `draw_original_text(page, spans, *, dx=0, dy=0, src_doc=None, page_no=0)` | 重放原页文本，**优先用 PDF 内嵌子集字体** |
| `compute_layout(segments, translations, *, page_no, src=None, dx=0.0, body_band=None, only_translated=True, vanchor=None, spread=None)` | 版式计算（PDF 与 Web canvas 共用） |
| `draw_translated(page, *, boxes, cjk_font_path=None, dx=0, dy=0)` | 画译文（底色矩形 + 混合字体逐 run） |
| `build_translated_page(src_doc, page_no, segments, translations, *, cjk_font_path=None, target_doc=None, page_rect=None, dx=0, dy=0, erase_untracked_body=False, stats=None)` | 重建 + 擦除已翻译正文 + 画译文 |
| `render_page_png(doc_or_path, page_no, *, dpi=110, alpha=False)` / `render_page_webp(...)` | 单页位图 |
| `page_geometry(doc, page_no)` | 页眉/页脚带、模板图 xref、图片/矢量障碍物（按页缓存） |
| `banner_xrefs(doc)` | 出现 >80% 页面的 image xref（实测：23=页脚、24=页眉） |
| `resolve_cjk_font(bold=False)` / `mixed_text_width` / `wrap_runs` / `layout_mixed_runs` / `char_slot` / `split_runs` | 字体与断行工具 |
| `clear_caches()` | 释放跨页缓存（**导出每块后调用**，控制内存） |

### `render/pdfout.py`

| 函数 | 说明 |
|---|---|
| `export_cn_pdf(src_pdf, out_pdf, segments_by_page, translations, *, pages=None, cjk_font_path=None, dpi=110, chunk=20, on_page=None)` | 中文 PDF |
| `export_bilingual_pdf(..., layout="facing")` | 双语 PDF（每页 2×宽：左原文 1:1 / 右中文 1:1） |
| `build_cn_page` / `build_bilingual_page` / `render_cn_page_png` / `render_bilingual_page_png` | 单页构建与位图（Web 按需渲染用） |

返回值：`{"pages","boxes","overflow","shrunk","lines","drawn","bytes","path","elapsed"}`。

`segments_by_page` 既接受 `{page: [seg]}` 也接受扁平段列表；`translations` 接受
`{seg_id: str}` / `{seg_id: row}`（row 取 `.text`）。段可以是 dict 或 `core.models.Segment`。

---

## 2. 关键实现决策（都有实测依据）

### 2.1 绝不用 `page.show_pdf_page()`
按 Lead 要求，页面一律由 `new_page` + `insert_image` + `Shape` 重放 + `TextWriter` 组成，
保留可编辑文本层与矢量。

### 2.2 图片：优先复用原始压缩流
`doc.extract_image(xref)["image"]` 直接 `insert_image(rect, stream=...)`：
* 渲染结果与 `Pixmap(doc,xref)` 逐像素一致（实测 page101 大图两者 diff 都是 17.293%），
* 但单页 PDF 从 644KB → 332KB（保留原 JPEG/Flate 压缩流）。
* 有透明遮罩（SMask）时用 Pillow 合成 RGBA PNG；失败再回退 `Pixmap`。
* PyMuPDF 的 `insert_image` 对相同图内容会自动去重（实测同图 10 次插入 → 1 个 image xref）。

### 2.3 原页文本：内嵌子集字体 + 两端对齐逐字符定位
Word 导出的正文是**两端对齐**的：逐字符 advance 与系统 Calibri 完全一致（误差 < 0.1pt），
额外宽度全部落在空格上（实测每空格 +1.9~5.1pt）。因此：
* 用 `get_text("rawdict")` 的逐字符 origin，**逐词** append（只在需要时抽取 rawdict）；
* 优先用 `doc.extract_font(xref)` 的内嵌子集字体（字形与度量与原页一致）；
* 逐词而非整串 append 还有个副作用好处：`TextWriter` 会把整串里的空格变成无字形间隔，
  MuPDF 抽取时给的是 **NBSP**；逐词 append 抽取回来的是普通空格。

结果（dpi 110 与原页渲染对比）：文本像素差 6.3% → 1.9%(p401) / 4.7%(p21)，
图片 0.00%~0.58%，差异全部是字形边缘的亚像素抗锯齿。
**残留差异**：page101 那张大幅地图（840×638 缩到 487×370）重采样相位与原页不同，
边缘 17% 像素差（肉眼不可分辨），无法通过 API 消除。

### 2.4 版式（`compute_layout`）

* 起始基线 = `line_boxes[0].y0 + 0.75 * size`（= 原首行 origin.y，Calibri 实测 0.75em）。
* 行距 `size*1.24`；字号 原*0.92 起（`table_cell` 用 原*1.0），0.94 递减，下限 原*0.55。
* **横向**：可向右扩到"最近障碍物"（其它段 bbox / 图片 / 矢量）−3pt，且不超过正文右界；
  上限 `bbox.x1 + max(3*width, 160)`。**`table_cell` 绝不扩宽**（否则压到右邻单元）。
* **纵向**：
  * `limit_bottom = bbox.y1 + min(gap, 8)`，再被正文带下沿与"下一段 y0−0.5"收窄；
  * "下一段"只统计**与本文本列横向相交**的段（多栏/表格里旁边一列的低位段不是下一段）；
  * 绝不越过 `limit_bottom`；放不下 → 末尾 `"…"` 并计 `overflow`。
* **译文块落位（本轮的缺陷修复）**：译文比原文短时若仍贴段顶，段内下部会留大片空白。
  1. 先**撑行距**（`LINE_SPREAD`，上限 `1.6*size`，下限 `1.24*size`，受 `limit_bottom` 约束）；
  2. 再按 `VANCHOR` 把整块**下移**到原块底部（`conditional`：仅当段后间距 ≥8pt 且译文 ≥2 行；
     单行译文仍贴原首行，避免被从上方标题/段落拉开）。
  实测 pages 1-31：译文块底高于原文块底 >6pt 的段从 **69/131 降到 23/131**，最大偏差
  115.5pt → 28.2pt（剩下的都是 1 行译文对 2 行原文，属固有差异）。
* **行内碎片**：`bbox` 被另一段 bbox 包住 ≥80% 且面积 <1/4 的段（上标 `th`、行内编号）
  不单独排版，避免压到宿主段落上（实测 p13 的 `th`）。
* `bg` = 原页 36dpi 渲染图在 `paragraph_rect` 内的量化(16 级)众数色；文字色按亮度选
  `0x1a1a1a`/`0xf2f2f2`。
* `bg_fill=False`：`paragraph_rect` 与任何图片相交的盒子不画底色，防止把 callout 图擦掉。
* 每行额外输出 `runs:[{text,x,slot,width}]`（绝对 x + 字体槽 + 宽度），
  前端 canvas 逐 run 绘制即与 PDF 完全一致（`lines[].x/y/width/size` 仍然可用）。

### 2.5 导出
* 每 `chunk=20` 页 `part.save()` 到临时文件再 `insert_pdf` 进最终文档（内存有界，
  且 `insert_pdf` 保留可编辑文本层，不栅格化）。
* `subset_fonts()`：Deng.ttf 约 10MB，子集化后单页 PDF 从 12MB → 276KB。
* TOC（仅整本导出时）、Title/Author 等元数据从原 PDF 复制。
* 双语页 = `2 × 原宽`，左半完整原页、右半中文页、中缝分隔线 + 右上角「EN / 中文」页签。

---

## 3. 性能实测（`python tests\test_render.py --bench`）

401 页全量中文导出（真实 PDF + 4,114 段 + 合成中文译文）：

| 指标 | 实测 | 要求 |
|---|---|---|
| 抽取 + 组段 | ~17s | – |
| 导出 | **112.8s**（约 0.28s/页） | < 600s |
| 输出大小 | 36.4 MB | – |
| 峰值内存（PeakWorkingSet） | **699 MB** | < 2GB |
| `boxes/lines` | 3286 / 12048 | – |

`boxes=3286` 中 `shrunk=1449`、`overflow=177`（合成译文长度仅按 0.7×英文估算，
真实译文会更小；`compute_layout` 单页 < 50ms，背景色按页 36dpi 缓存一次）。

真实数据（DB v2，pages 1-31，489 boxes）下：`shrunk=30`、`overflow=4`，
其中 **4 个 overflow 全部是 `table_cell`**（译文数据错配，见 §5.1），
非表格段 overflow = 0；`shrunk` 里 10 个是表格单元、20 个是正文段
（list_item 15 / paragraph 4 / heading 1）。

---

## 4. 测试与 QA

```
python tests\test_render.py                     # 全部断言，exit 0
python tests\test_render.py --qa <cn.pdf> [--pages 1-31]
python tests\test_render.py --bench             # 401 页性能
```

`--qa` 是**列感知**碰撞检测：只有 x 与 y 同时相交的文本行才算碰撞。
`spikes/qa_side_by_side.py` 只按 y 判断，在"同一 baseline 的多个 table_cell"
（表格行被切成独立段后）上会误报（p25 报 20 对，实测 18 对是同行不同列、
2 对是不同行且 x 不相交）。`--qa` 还会扣除"原页自身也有"的碰撞
（p16 原文表格行距小于行框，原文自身就有 16 对）。

目视验收图（`data/derived/preview/`）：`p21_cn.png`、`p21_bilingual.png`、
`p21_overlay_diff.png`（原页｜中文页｜像素差异三联）、
`cmp_p21/p25/p30.png`（Lead 脚本产出的原/译并排）。

---

## 5. 已知限制 / 待办

1. **译文数据错配**会直接反映到版面上。实测 DB 里 `seg17095`（bbox 40×10.5pt，原文
   `AVIONICS`）拿到了整行译文 `飞控系统（FLCS） FAULT 发动机 FAULT …`，`seg17057`
   （原文 `th`）拿到 `韩国`。渲染层只能缩到 0.55 再截断（`overflow`），
   根因是拆分表格单元后的 segment_id 与旧译文未对齐（属 core/pipeline 侧）。
2. 表格单元文本超出单元格时，最小字号会到 `0.55×原字号`（9pt → 4.95pt），
   可读性差；正确做法是修数据，而不是继续在渲染层放宽下限。
3. page101 类大幅位图的**重采样相位**与原页不一致（边缘 ~17% 像素差 >16 级），
   肉眼不可分辨但非逐像素一致。
4. `erase_roles=("body",)` 的正文带由模板 banner 图推出（页眉 banner 下沿 / 页脚 banner 上沿）；
   无 banner 时退化为 `0.088H / 0.92H`。若换成别的版式需复核这两个阈值。
