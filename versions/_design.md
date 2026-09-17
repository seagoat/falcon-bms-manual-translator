# `versions/differ.py` 设计说明（task-3 / CONTRACT §8）

> 作者：team member `version-engine`。本文只描述 **版本变更引擎**；接口真相以 `CONTRACT.md` 为准。

## 1. 对外 API

```python
diff_versions(conn, doc_id, from_version_id, to_version_id) -> VersionDiff   # CONTRACT §2 §8
diff_summary_text(vd) -> str                                                # Markdown 摘要表
changes_by_segment(conn, doc_id, from_id, to_id) -> dict[int, Change]       # 键 = 新版 seg_id
removed_by_segment(conn, doc_id, from_id, to_id) -> dict[int, Change]       # 键 = 旧版 seg_id
```

* `VersionDiff.counts` 至少包含 §8 规定的 8 个键，另加 6 个**扩展键**用于覆盖度校验与 CLI 摘要：
  `split_old / split_new / merged_old / merged_new / old_total / new_total`（外加 `doc_id`、`from_version_id`、`to_version_id`）。
  覆盖度不变式（测试中逐条断言）：
  ```
  old_total == unchanged + modified + removed + moved + reordered + split_old + merged_old
  new_total == unchanged + modified + added   + moved + reordered + split_new + merged_new
  ```
* 稳定可重复：所有遍历顺序都来自 `order_index` 升序；排序键完整（ratio, 位置距离, 段索引），
  不存在依赖 dict/set 迭代顺序的分支。

## 2. 比对基准：`canonical_text`（CONTRACT §3.1.1 硬要求）

`_canon(seg) = seg["canonical_text"] or seg["text"]`（列缺失/为空时退化，保证对旧库可用）。

* 锚点 fingerprint：`core.fingerprint.fingerprint(canonical_text)`（= blake2b(normalize(canonical).casefold())）；
* `SequenceMatcher` 输入序列：`normalize(canonical_text)`；
* 模糊配对 `ratio()`：canonical_text；
* `char_diffs`：canonical_text。

这样「同一段落在两版之间只是重新断行/软连字符位置不同」不会产生伪变更；
反之 canonical 真的改了（哪怕只改一个词）才会进 `modified`。
`tests/test_versions.py::test_canonical_text` 专门用「text 不同、canonical 相同」的两版做回归。

## 3. 算法流水线（§8 的 8 步 + 实现细节）

```
① 取段        store.segments_of_version(version_id)，仅 role == "body"（§8 步骤 1）
② 精确锚点    fingerprint 全局单调匹配（LCS）+ 位置就近兜底（§8 步骤 2，见下）
③ 顺序 diff   未匹配段按 normalize(canonical) 序列 SequenceMatcher.get_opcodes()（autojunk=False）
④ 块内配对    相邻的 delete/replace/insert opcode 合并为一个「块」，块内按 ratio 贪心配对
              （ratio >= 0.55；先按长度比 >= 0.34 粗筛，候选按 ratio↓/位置距离↑排序）
⑤ 结构分组    块内 SPLIT（1 旧→N 新）/ MERGED（N 旧→1 新），ratio >= 0.55 且比原配对明显更像
⑥ 残余配对    见 §4.1（扩展）
⑦ 分类        MODIFIED / MOVED（跨页）/ REORDERED（同页相对次序前移）/ UNCHANGED
⑧ 写链接      store.record_segment_link（大版本走单事务批量写，见 §4.4）
⑨ 落库缓存    version_diffs（同 pair 复用，见 §5）
```

**锚点匹配为什么不是「逐个取最近」？** 直接贪心会出两个问题：
* 重复 fp 段（例如每页图注）会**交叉配对**（old4↔new5 与 old5↔new4），名次比较于是报出
  **伪 REORDERED**（实测：8 段里 4 段同文图注删 1 份 → 误报 reordered=1）；
* 交叉本身也会把未变的相邻段判成 moved/reordered。
所以第 1 层用 `difflib.SequenceMatcher(...).get_matching_blocks()` 做**全局单调匹配**
（等价于最长公共子序列；与 §8「同 key 一一配对」一致），第 2 层对仍未配对的同 fp 段按
「位置最接近者」贪心补配——这样**真实的同页交换**（LCS 只能匹配其中一段）仍会被识别成 REORDERED。
重复 fp 的规模乘积超过 `ANCHOR_LCS_MAX_WORK = 2_000_000` 时跳过 LCS（避免二次开销），直接走第 2 层。

**为什么把连续非 equal opcode 合并成块？** 未匹配段序列天然互相不相等（相等的早被 fingerprint 锚点
吃掉了），所以整段未匹配集合往往被 SequenceMatcher 判成**一个** replace 块。把相邻 delete/replace/insert
合并处理既符合这个事实，又让「先删后插」式改写也能被识别成 SPLIT/MERGED。

**MOVED / REORDERED 的判定（§8 步骤 6）**
* `MOVED`：配对成功且 `normalize` 文本完全相同，但 `page` 变了。
* `REORDERED`：文本相同、同页，且该段在本页**已配对同文本段**中的名次 `new_rank < old_rank`
  （即它向前跳过了别人）。名次只在「同页 + 文本相同 + 已配对」的段之间计算，
  因此插入/删除导致的 `order_index` 平移不会误报。
  一次「同页重排 1 段」的操作因此恰好产生 **1** 条 `reordered`（跳前的那一段），
  与被它越过的段（其名次后移）区分开。

## 4. 与 CONTRACT §8 的差异（都是**已文档化的扩展**，可按常量关闭）

### 4.1 残余低相似度配对（`RESIDUAL_PAIR_RATIO = 0.30`）

§8 只说「replace 块内 ratio >= 0.55 配对，其余拆成 REMOVED/ADDED」。但 task-3 的验收要求是：
「大改（ratio < 0.5）必须仍被识别为 `MODIFIED`，而不是 add+remove」。两者在字面上冲突，
因此增加一次**残余配对**：主贪心（>= 0.55）与 SPLIT/MERGED 分组之后，块内仍未配对的旧/新段之间再做
一次贪心配对，条件更保守（三重同时满足）：

| 条件 | 常量 | 值 |
|---|---|---|
| 字符相似度 | `RESIDUAL_PAIR_RATIO` | `>= 0.30` |
| 长度比 min/max | `RESIDUAL_MIN_LEN_RATIO` | `>= 0.50` |
| 内容词包含度 `|A∩B| / min(|A|,|B|)`（去掉停用词、长度<3 的词） | `RESIDUAL_MIN_TOKEN_CONTAINMENT` | `>= 0.30` |

残余配对放在 SPLIT/MERGED 之后，避免把一个真正的 1 拆 2 拆散成两条 modified。
实测：整段重写 ratio 0.48 / 词包含度 0.36 → 配成 `MODIFIED`；而互不相关的「删 2 段 + 插 3 段」
（ratio <= 0.35，词包含度 <= 0.10）不会被误配。
关闭方式：把 `RESIDUAL_PAIR_RATIO` 调成 `1.01`（或把 `RESIDUAL_MIN_TOKEN_CONTAINMENT` 调到 1.0）。

### 4.2 SPLIT / MERGED 是「组级」变更

1 旧 → N 新 记为 **1 条** `split`（`old_segment_id` = 旧段，`new_segment_id` = 拆出的第一段），
N 旧 → 1 新 记为 **1 条** `merged`。组内成员 id 放在 `Change.meta = {"group_old_ids": [...], "group_new_ids": [...]}`
（`Change` 数据类字段不动，`meta` 是附加属性，随 `version_diffs` 的 JSON 一起落库/回读）。

* `counts["split"]/["merged"]` = **组数**；
* `counts["split_old"]/["split_new"]/["merged_old"]/["merged_new"]` = 组内段数合计（覆盖度校验用）；
* `counts` 与 `len(changes)` 在 8 个主类别上逐类一致（测试断言）。

### 4.3 `segment_links` 覆盖**每个新版 body 段** + 每条 REMOVED

* 新版侧（`new_segment_id` 非空）：每个新版 body 段一行，含 `added`（`old_segment_id = NULL`）。
  这样 `store.links_for_version(conn, to_vid)` 就是「新版段 → change_kind」的完整映射，
  `/markers` 可以按新版本直接着色。
* 旧版侧（`new_segment_id = NULL`）：每条 `removed` 一行 —— core 的 `store.links_for_diff()` /
  `diff_summary()` 正是按这个约定统计删除（实测 `diff_summary` 的
  removed/modified/added/moved/reordered 与 `diff_versions().counts` 完全一致）。
* `split` 的每个新分段都指向同一个旧段（N 行），因此 `diff_summary` 里的 `split` 计的是**新分段数**，
  而 `counts["split"]` 计的是**组数**（§4.2 的语义），两者在测试里都做了断言。
* `merged` 的非主旧段没有单独的行（靠 `Change.meta.group_old_ids` 暴露，`removed_by_segment` 会列出）。
* 写链接前会先清掉该组合既有链接（新版侧按 version 删；旧版侧按 `new_segment_id IS NULL AND
  old_segment_id ∈ from_version` 删），保证重复调用不产生重复行。

### 4.4 大版本走单事务批量写（性能硬指标）

`len(links) >= BULK_LINK_THRESHOLD (200)` 时用 `executemany` + 单次 `commit` 写 `segment_links`
（列名通过 `PRAGMA table_info` 自适应），否则逐条调 `store.record_segment_link`。
两条路径产出**完全相同的行**：小规模场景（41 条）走 store 接口，4000 段的性能场景走批量路径，
测试对两条路径都断言了「links 覆盖全部新版段」。

### 4.5 `version_diffs` 落库（实测 core 只有 `counts` 列）

实测 `core/db.py` 的 `version_diffs` = `(diff_id, doc_id, from_version_id, to_version_id, counts, created_at)`
—— 只有计数缓存，没有放变更集的地方。因此：

1. **计数**：优先调 `store.write_version_diff(conn, doc_id=…, from_version_id=…, to_version_id=…, counts=…)`
   （CONTRACT §3「store 已有则用」）；该函数不存在时用自适应 `INSERT OR REPLACE` 兜底。
2. **完整变更集**：惰性 `ALTER TABLE version_diffs ADD COLUMN changes TEXT NOT NULL DEFAULT '[]'`
   （纯增量、幂等、各自进程只做一次，用 `PRAGMA database_info/list` 记库路径），
   随后 `UPDATE … SET changes=?`。JSON 里带 `differ_rev`，算法升级后自动失效旧缓存。
3. 若某版本库不允许加列（或列名不匹配），自动退回**进程内 64 条 LRU 记忆**
   （键 = 库路径 + 版本对 + 两侧 `COUNT(*)/MAX(id)` 签名，段被重写即失效），
   功能不缺失，只是跨进程不再复用。

## 5. 幂等

* 第二次 `diff_versions(同 pair)`：命中 `version_diffs`（或进程内记忆），直接返回，
  **不重算、不重写链接**；实测 4000 段场景首次 0.47s、复用 0.008s。
  测试断言 change 列表（kind/id/ratio/page/summary/char_diffs）与 counts 完全一致，
  且表里仍只有 1 行、`segment_links` 行数不变。
* 缓存不可用时也会重算同样的结果（算法确定性），并先清链接再重写，不产生重复行。

## 6. 复杂度与实测

* 锚点匹配 O(n log n)（每个 fp 桶内二分 + 就近取未用项）。
* `SequenceMatcher` 只在**未匹配段**上跑；配对/分组只在**块内**做两两比较。
* 块内两两比较有硬上限 `MAX_BLOCK_PRODUCT = 120_000`，超出则按块内相对位置开窗（窗口宽度自适应）。
* 组候选有 `GROUP_CAND_BUDGET = 20000` 上限；自由段总数 > `GROUP_SCAN_LIMIT = 600` 时跳过纯自由段组合枚举
  （此时文档已是整体重写，配对扩展仍生效）。
* 实测（`tests/test_versions.py::test_perf`，Windows / Python 3.11 / 本机）：
  **4000 段 vs 4000 段（20 改 / 5 删 / 5 插 / 2 跨页移动）diff_versions 0.46~0.50 s**（预算 3 s），
  其中含 4000 条 `segment_links` 批量写入；同 pair 复用 0.008 s。
  另测「4000 段全部同 fingerprint」的极端重复场景：0.25 s（LCS 守卫触发，走就近贪心）。
* **真实数据实测**（`data/app.db`：401 页 BMS 手册 v1=4807 段 / v2=4806 段，正文 body 4007/4006，
  v2 由 Lead 的 `simulate-update` 生成）：
  ```
  清缓存后首次 diff_versions(1, 1, 2) = 0.355 s
  counts = {unchanged:4001, modified:3, added:1, removed:2, moved:1, reordered:0, split:0, merged:0}
  覆盖度 old 4007/4007、new 4006/4006；二次调用（缓存复用）= 0.008~0.033 s，结果一致
  样例：aircraft → airframe（ratio 0.9873）判 modified，char_diffs = [equal, delete"craft", insert"frame", equal]
  ```
  与 simulate-update 施加的操作（改 3 段、删 2 段、插 1 段、移 1 段）**完全一致**。

## 7. 测试覆盖（`tests/test_versions.py`，201 条断言，退出码 0）

| 用例 | 内容 |
|---|---|
| 1 主场景 | v1 固定 40 段 → v2 施加：微改(2 词)/大改/中改、删 2、插 3、跨页移 1、同页重排 1、拆 2、并 1；断言 counts **精确等于** `{unchanged:30, modified:3, added:3, removed:2, moved:1, reordered:1, split:1, merged:1}`、ratio（微改 0.903 > 0.8、大改 0.480 < 0.5、中改 0.717）、`char_diffs` 逐字符还原旧/新文、变更按新版顺序稳定 |
| 2 幂等 | 二次/三次调用结果一致；`version_diffs` 仅 1 行且落库了完整变更集；`links_for_version` 恰好 `new_total` 条、`links_for_diff` = `new_total + removed` 条（含 REMOVED 行）；core 的 `store.diff_summary` 计数与 `counts` 一致 |
| 3 辅助 API | `changes_by_segment` 覆盖全部新版段；`removed_by_segment` 键 = {删 2 + 拆 1 + 并 2}；`diff_summary_text` 含 8 类 Markdown 表格行 |
| 4 canonical_text | 仅断行/连字符变化 → 全 unchanged；canonical 真改 → 1 modified |
| 5 边界 | 同内容两版全 unchanged；`role != body` 段不进 diff；空版本 ↔ 有段版本 全 added / 全 removed |
| 6 性能 | 4000 段 < 3 s（实测约 0.47 s），同 pair 复用不再重算 |
| 7 审查探针 | 「只改一个词」→ 1 modified（不是 add+remove）；重复同文图注删 1 份 → 1 removed、其余 7 段 unchanged（无伪 moved/reordered） |

## 8. 已知限制

1. 残余配对（§4.1）本质是「改写」与「删旧插新」的权衡；阈值见上表，可按需调参。
2. `SPLIT/MERGED` 组内最多 `GROUP_MAX_SIZE = 4` 段；超过 4 段的结构变化会退化为 modified/removed/added。
3. `merged` 组在 `segment_links` 里只能记一条（新段 ← 主旧段），其余旧段靠 `Change.meta.group_old_ids`
   暴露（`removed_by_segment` 会把它们全部列出）。
4. 只 diff `role == "body"`（§8 步骤 1）；页眉/页脚/页码的变化不在版本追踪范围内。
5. `char_diffs` 是**字符级** LCS 结果，删除片段不保证是连续子串（例如 `should`→`must` 会被拆成交错的
   del/ins 片段）；「还原原文」是它的正确性判据（测试按此断言）。
6. **大规模同文重复段**（例如 4000 段完全同一句话）位置本身不可区分，就近贪心可能给出少量伪
   `moved`；真实文档里的重复段（图注/表头）数量有限，走 LCS 路径，实测无误报。
7. `diff_summary_text` 的占比以 `new_total` 为分母（无新版段时退回 `old_total`）。
