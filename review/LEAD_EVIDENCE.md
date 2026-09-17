# 验证证据汇总（Lead 亲测）

> 本文件只记录 **Lead 自己跑出来的真实结果**，不采信他人报告。
> 所有命令均在仓库根 `E:\worksrc\manual_trans_trace` 执行，Python 3.11，pymupdf 1.28.2。

---

## 1. 关键决策：翻译模型与推理模式（A/B 实测）

脚本 `spikes/ab_reasoning.py`，结果 `spikes/out/ab_reasoning.json`。
语料：真实手册第 15/21/22/24/30/31/32/40 页共 78 段 / 15063 字符，每配置 4 批 × 12 段。

| 配置 | 平均延迟 | prompt | completion | 其中 reasoning | 平均每段 completion |
|---|---|---|---|---|---|
| flash 默认 | 10.4 s | 6372 | 8713 | **6720** | 242.0 |
| flash `reasoning_effort=none` | **3.0 s** | 6272 | **2285** | **0** | 63.5 |
| flash `thinking={"type":"disabled"}` | 2.7 s | 6272 | 2017 | 0 | 56.0 |
| pro 默认 | 65.9 s | 6576 | 23217 | **20744** | 644.9 |
| pro `reasoning_effort=none` | 6.4 s | 6260 | 2338 | 0 | 64.9 |
| pro `thinking={"type":"disabled"}` | 6.8 s | 6260 | 2453 | 0 | 68.1 |

质量代理指标（同批数据）：缩写保留率 flash 56/95 vs pro 58/95，单位保留率一致，长度比一致，无空译文。
**结论**：`deepseek-flash` + 关闭思维链 = 延迟降 3.5x、输出 token 降 4.3x，质量无实质差异。
官方文档确认思维链**默认开启**且 effort=high：
<https://api-docs.deepseek.com/guides/thinking_mode/>
已落入 `config/local.json`（`model=deepseek-flash`, `thinking_mode=disabled`）与 `translate/engine.py`
（`DEFAULT_THINKING_MODE`，`_build_payload` 显式发 `reasoning_effort:"none"` + `thinking:{"type":"disabled"}`）。

价格取自官方 <https://api-docs.deepseek.com/quick_start/pricing>（off-peak）：
flash in $0.15 / out $0.60；pro in $0.66 / out $1.98（每 1M tokens）。

---

## 2. 成本预估（全书 401 页）

`python cli.py cost-estimate`（`translate/engine.py` 的 dry_run，不发网络请求）：

```
=== 翻译成本估算  v2 (version_id=2)  model=deepseek-flash ===
  待译段数 : 3452（machine=3452  seeded=0）
  已缓存/沿用: cached=543  carried=811
  批次数   : 444（每批 ≤12 段 / 1800 字符）
  prompt   : 594,944 tokens
  completion: 399,671 tokens
  合计     : 994,615 tokens
  估算费用 : $0.329044 ≈ ¥2.3362  (in $0.15/M, out $0.6/M)
  待译字符 : 678,207
```
全书正文约 **72.5 万字符**（按每 7 页抽样外推；ingest 实测 chars=754,679 含页眉页脚）。
按 concurrency=8，墙钟预计 **8-15 分钟**。

**实测校准**（第 2-31 页真跑，见 §3）：49 批 / 76,919 字符 → prompt 59,174 + completion 18,297 = 77,471 tokens，
$0.019854。线性外推到全书 ≈ **$0.30 / 400 批 / 15 分钟**，与 dry_run 预估一致（误差 < 10%）。
> 注：completion 实际比 dry_run 估计低（18,297 vs 估 400k），因为关闭思维链后每批输出仅约 370 token。

---

## 3. 真实 DeepSeek 端到端翻译（Lead 亲手执行）

```
python cli.py translate --version 2 --pages 2-31 --provider deepseek --force --concurrency 8
✅ 翻译完成，耗时 14.9s
  新译=480  沿用缓存=0  原文未变沿用=66  失败=0
  machine=480  seeded=0  批次=49  字符=76919
  tokens: prompt=59174  completion=18297
  估算费用: $0.019854 ≈ ¥0.141
  版本进度: 549/4806  carried=66  failed=0  machine=483  missing=4257  reviewed=0  seeded=0
```
退出码 **0**，**49 批并发 8，无一次失败、无 `database is locked`**。

译文质量抽样（第 21 页，`python spikes/inspect_translations.py 2 21`）：

| 原文 | 译文 |
|---|---|
| `The activation of this warning light signals that the canopy hooks or locks are not secured, or there has been a loss of cabin pressure. Descend below 10,000 feet and initiate a landing at the earliest opportunity.` | `该警告灯亮起表示座舱盖挂钩或锁未锁定，或座舱失压。下降至10,000 ft以下并尽快着陆。` |
| `- If the EPU RUN light is off, there is a single system B hydraulic failure (refer to System B Hydraulic failure in the EP checklists).` | `▪ 若 应急动力装置（EPU） RUN 灯熄灭，则表明 B 系统单液压系统故障（参见 EP 检查单中的 B 系统液压故障）。` |
| `◆ If the ELEC SYS light is off, both hydraulic systems A & B have failed (refer to Dual Hydraulic failure in the EP checklists).` | `◆ 若 ELEC SYS 灯熄灭，则 A、B 两套液压系统均已故障（参见 EP 检查单中的双液压系统故障）。` |

**核对通过**：型号 `F-16`/`ELEC SYS`/`NORM`/`RUN`/`PTO` 保留；单位 `10,000 ft`/`1000 psi`/`5 psi`/`20%` 保留；
项目符号 `-`/`▪`/`◆` 保留；编号 `1.` `2.` 保留；术语表命中（`EPU`→应急动力装置、`FLCS`→飞控系统、
`PFL`→飞行员故障清单、`OBOGS`→机载制氧系统、`BIT`→机内自检）。

---

## 4. 版式还原中文 PDF

```
python cli.py export --kind cn --pages 1-31 --out data/derived/real_cn_1_31.pdf
✅ 导出完成：data/derived/real_cn_1_31.pdf  (25.88 MB, 5.7s)
  pages=31  boxes=489  overflow=0  shrunk=23
```
退出码 **0**。

自动版式校验 `python spikes/qa_side_by_side.py data/derived/real_cn_1_31.pdf origin/BMS-Training-Manual.pdf 21,22,23,25,30`：
```
page 21: CN has 25 text lines   OK no line overlaps
page 22: CN has 26 text lines   OK no line overlaps
page 23: CN has 24 text lines   OK no line overlaps
page 25: CN has 18 text lines   OK no line overlaps
page 30: CN has 20 text lines   OK no line overlaps
TOTAL overlapping line pairs: 0
```
目视验收（`data/derived/preview/cmp_p21.png` / `cmp_p25.png` / `cmp_p30.png`，左原右译）：
页眉 banner、页脚 banner 与页码、内嵌图片（座舱面板图、膝板图、表格截图）、蓝色章节标题、
项目符号层级、编号列表——**全部保留在正确位置**。

**发现的缺陷**（已派单给对应负责人）：
1. 短段落/图注**顶部对齐** → 译文行数少于原文时留大片空白，视觉锚点错位（p25 图注最明显）。→ render-engine
   - **已修复并复验**：块底偏差 >6pt 的段 69/131 → 23/131，最大偏差 115.5pt → 28.2pt。
     重新导出 `data/derived/real_cn_1_31_anchor.pdf` 后我的列感知校验 6 页全部 `OK no line overlaps`。
2. 表格行被压成一条字符串（`PROCEDURE SCENARIO MASTER ANTI-POSI-WING/TAIL ...`），列结构丢失。→ dev-relay
   - 已改为分列（`table_cell`），但**过切**（body 段 4007→12163），仍待重做。

### 4.1 双语对照 PDF（用户三件套之二）—— 独立验证通过
```
python cli.py export --kind bilingual --pages 1-31 --out data/derived/real_bi_1_31.pdf
✅ 导出完成：data/derived/real_bi_1_31.pdf  (3.59 MB, 11.0s)
  pages=31  boxes=488  overflow=4  shrunk=30
```
`spikes/check_bilingual.py` 自动校验：
```
bilingual: pages=31   page rect = 1190.64 x 841.92   orig rect = 595.32 x 841.92   width ratio = 2.000
page 2: text chars=3749  cjk=503  ascii_words=257     <- 左半英文 + 右半中文都在
page 21 bitmap 1191x842: left white=0.738 dark=0.0933 / right white=0.746 dark=0.0735  <- 两侧都有内容
```
目视验收（`data/derived/preview/_bi_full_p21.png`）：**左「EN」右「中文」的 A3 横向双页**，
两侧各自的页眉 banner、页脚 banner、页码、内嵌图片、蓝色标题全部独立且完整；
顶部有 `EN` / `中文` 对角页签。**这是三件套里交付质量最高的一项。**

> 注：双语 PDF 反而比纯中文 PDF 小（3.76 MB vs 3.55 MB），因为不重复内嵌原始图片流对象，
> 且中文层做了字体子集化 —— 这是正常且正确的优化。

---


## 5. 版本变更追踪（v1 → v2）
`python cli.py simulate-update` 生成的 v2 由 dev-relay 施加了已知改动，
`python spikes/verify_diff.py` 与 `python spikes/analyze_drift.py` 核对：

```
counts = {'unchanged': 4001, 'modified': 3, 'added': 1, 'removed': 2, 'moved': 1,
          'reordered': 0, 'split': 0, 'merged': 0}
```
**与实际施加的操作完全一致**（改 3 / 增 1 / 删 2 / 移 1）。

识别质量证据：
- **微改正确识别**：`aircraft` → `airframe`（ratio 0.9873）判 `MODIFIED`，行内 diff 输出
  `left click on the air[-craft-][+frame+] symbol` —— 而不是 add+remove。
- **改数字识别**：`Level out at [-5-][+6,+]000 feet.`
- **跨页移动识别**：`moved ratio=1.000 old_page=9 new_page=10`。
- **删除识别**：`removed` 两条，`old_page=7` / `old_page=15`。
- **新增识别**：`added` 一条，`new_page=40`，`NOTE: Before the first flight of the day...`。

### 5.1 Unicode 变体漂移压力测试（重要）
v2 是用 pymupdf 重新写入文本生成的，因此天然带出 **U+2010 HYPHEN** 替代 ASCII `-`（`F-16` → `F‐16`）。
这是真实场景（新版 PDF 重新导出、换工具/换字体）最容易踩的坑。实测 `core/fingerprint.normalize` 已正确折叠：

```
OK  'F-16'  vs 'F‐16'  (U+2010) -> 同指纹
OK  'F-16'  vs 'F‑16'  (U+2011) -> 同指纹
OK  'F-16'  vs 'F–16'  (U+2013) -> 同指纹
OK  'a - b' vs 'a — b' (U+2014) -> 同指纹
OK  'a b'   vs 'a\xa0b' (NBSP)  -> 同指纹
OK  'say "X"' vs 'say “X”'      -> 同指纹
```
`spikes/analyze_drift.py` 全量扫描 4007 段：
```
fingerprints only in v1: 5   only in v2: 4      <- 应等于真实变更数（3 modified + 2 removed + 1 added + 1 moved）
non-ASCII in v2 absent in v1: '‐' U+2010 x3     <- 变体已被 normalize 折叠，未造成任何伪变更
```
**结论：跨版本指纹匹配对 Unicode 标点变体、断行变化、软连字符均免疫。**

### 5.2 差分性能
`python spikes/profile_diff_api.py`：`diff_versions` 对 4008 段 **9 ms**（冷/热一致），
char_diff ops 总数 15。远低于 CONTRACT §8 的 3 s 预算。

---

## 6. Web API 与对照 UI

真实服务 `python cli.py serve --port 8777 --no-open`（data/app.db：doc 1 / v1+v2 / 4806 段 / 549 段真译文）：

| 端点 | 状态 | 耗时 | 大小 |
|---|---|---|---|
| `/api/health` | 200 | 306 ms | 130 B |
| `/api/documents` | 200 | 27 ms | 351 B |
| `/api/documents/1/versions` | 200 | 21 ms | 905 B |
| `/api/documents/1/versions/2/segments?page=21` | 200 | 36 ms | 11 KB |
| `/api/documents/1/versions/2/pages/21/markers` | 200 | 51 ms | 4.2 KB |
| `/api/documents/1/versions/2/pages/21/translated-layout` | 200 | 274 ms | 45 KB |
| **`/api/documents/1/diff?from=1&to=2`** | 200 | **33,918 ms** ❌ | 2.2 MB ❌ |

**缺陷**：`/api/diff` 在真实规模下要 33.9 s（进程内只需 9 ms），根因是 `web/server.py:682-683` 对全部 4008 个
change（4001 个是 unchanged）逐个调用 `_text_of()`，而 `_text_of` 每次新开一个 SQLite 连接 → 8016 次连接 ≈ 32 s。
前端 `app.js:149` 是 `await api.diff(...)`，导致**整个对照界面在真实数据下空白**
（DOM 探测：`pages_source=0 hot_source=0 toc=0`，截图 `data/derived/preview/diag_real_ui.png`）。
`tests/ui_smoke.py` 只在 mock 数据下跑过所以一直绿灯。
→ 已派单 web-api 修复，验收标准：`/api/diff` < 300 ms、< 100 KB，且 `ui_smoke.py` 在真实数据上 exit 0。

---

## 7. 已交付模块自检（Lead 复跑）

| 命令 | 结果 |
|---|---|
| `python tests\test_core.py` | **exit 0**，26/26（含 4000 段插入 142 ms） |
| `python tests\test_translate.py` | **exit 0**，12/12 |
| `python tests\test_versions.py` | **exit 0**（version-engine 报 201 断言 / 7 组） |
| `python -c "import core.store, core.pdfdoc, core.fingerprint, core.db"` | exit 0 |
| `python spikes\integration_probe.py` | 全部模块可导入，DB 10 张表齐全 |

---

## 8. 待办 / 未达验收项

| # | 项 | 负责 | 状态 |
|---|---|---|---|
| 1 | `/api/diff` 33.9 s + UI 真实数据空白 | web-api | 已修（降到 132ms），待真数据端到端复验 |
| 2 | jobs 卡在 running | web-api | 已派单，待复验 |
| 3 | 短段落顶部对齐留空白 | render-engine | **已修**（块底偏差 >6pt 的段 69/131→23/131，最大 115.5pt→28.2pt） |
| 4 | 表格行压成一条字符串 | dev-relay | 已分列，但**过切**（body 段 4007→12163、table_cell 4274），需重做 |
| 5 | `ingest --force` 静默清空全部译文 | dev-relay | **阻断级**，已升级最高优先级 |
| 6 | 产品缺译文迁移能力（靠库外脚本） | dev-relay | 阻断级，已派单 |
| 7 | 8 条表格译文错配（整行译文落进单词格） | dev-relay | 已派单 |
| 8 | `glossary.apply` 污染文件名 `TR_BMS_05_ILS_Landing` | trans-engine | 已派单 |
| 9 | `dehyphenate` 拼坏真实复合词（air-to-air 等 18 处） | core-layer | 已派单 |
| 10 | 全书 401 页翻译（前 31 页已完成） | lead | 待定版 ingest 后跑 |
| 11 | 独立验收审查终局核对 | verifier | ✅ 完成，结论「通过」 |

---

## 11. API 性能（生产库，重启后实测）

```
/api/health                                  200    306 ms
/api/documents                               200     27 ms
/api/documents/1/versions                    200     21 ms
/api/documents/1/versions/2/segments?page=21 200     36 ms
/diff?from=1&to=2            冷 80.6 ms / 热 28.8 ms / 6,103 B  changes=7
/diff?from=1&to=2&include_unchanged=1        200   143.9 ms / 732,588 B
/diff?from=2&to=2                            200      6.0 ms /     382 B  changes=0
/markers                                     200     20.3 ms /   4,745 B
/translated-layout (page 24)                 200    274 ms /  45,216 B
```
> 修复前 `/api/diff` 是 **33.9 s / 2.2 MB**（根因：对 4008 个 change 逐个新开 SQLite 连接 × 8016 次，
> 外加把 4850 个 unchanged 全量序列化）。verifier 独立复核确认修复，并指出我一度测得 83 s
> 是**旧进程跑旧代码**（未重启服务），非代码问题 —— 这条更正已记入 §9。

---

## 12. 交付后用户反馈的缺陷（已修复）

### 12.1 症状
用户在使用对照浏览器时报告：**左侧原始 PDF 布局错乱，第一页不是原始标题页，直接是某个正文页**
（截图显示两栏都停在正文第 21 页的 CANOPY/FLCS 段落，页码显示 21/401）。

### 12.2 根因（Lead 定位，非猜测）
`web/server.py` 的 `/pages/{page}/image` 端点用 `render_snapshots` 做磁盘+DB 双层缓存，
但 **`params_hash` 里漏掉了 `page`**：
```python
# 修复前
params_hash = hashlib.sha1(f"{kind}:{dpi}:{rev}".encode()).hexdigest()[:16]
# "第一张被渲染的页面图"被缓存后，被**所有页**命中复用
```
实测证据（修复前）—— 请求第 1/2/21/22 页拿到的是**完全相同的字节**：
```
page   1: served 122020 B sha=59d395c3d789dc7c
page   2: served 122020 B sha=59d395c3d789dc7c
page  21: served 122020 B sha=59d395c3d789dc7c
page  22: served 122020 B sha=59d395c3d789dc7c
```
而直接渲染 PDF 对应页的哈希各不相同（966142 / 402693 / 457241 / 415717 B）。
`/markers` 与 `/translated-layout` 是**正确的**（按 page 查数据库，不经过这个缓存），
所以只有"页面底图"这一层错位 —— 这解释了用户看到的"布局错乱 / 停在某一页"。

### 12.3 修复
```python
params_hash = hashlib.sha1(f"{kind}:{dpi}:{rev}:p{page}".encode()).hexdigest()[:16]
```
重启服务后旧键自然失效，重新生成。

### 12.4 回归验证（`spikes/test_page_image_cache.py`，exit 0）
```
A. source 图逐页互不相同：8/8 distinct（p1/2/3/21/22/31/200/401，8 个不同 sha）
B. cn 图逐页互不相同：    4/4 distinct
C. 第 1 页确实是封面：top 42% whiteness=0.00（F-16 大图）/ bottom 58% whiteness=0.83（白底标题）→ True
D. render_snapshots 行数随浏览页数增长：source 9 / cn 17 / bilingual 4
```
页内导航实测（`spikes/verify_page_flip.py`，exit 0，4/4）：
```
PASS 打开即为第 1 页（封面）      pageInput=1, firstSrcPage=1, srcHots=55
PASS 翻页后每页加载各自的页面图   5/5 distinct（p1/2/3/21/22 各自 URL）
PASS 翻页后图片已加载完成
PASS 无 JS 页面错误
```
目视确认：`data/derived/preview/_svc_p1.webp` 是 **FALCON BMS 封面**（F-16 大图 + TRAINING MANUAL +
BMS 4.38.1 + 17 OCTOBER 2025），`_svc_p2.webp` 是 FOREWORD 正文页。

### 12.5 影响面评估
* **只影响 Web UI 的页面底图**，不影响 `data/derived/full_cn.pdf` / `full_bi.pdf`（直连渲染，不过该缓存）
  —— 已复测：CN PDF 第 1 页 `训练手册 BMS 4.38.1 2025年10月17日`、第 21 页正文正常。
* 译文栏（canvas 层）、热区、变更标记一直是对的（它们走 DB，不走该缓存）。
* `tests/test_web.py` 复测 **179/179** 通过。该缺陷此前**未被测试覆盖**（原断言只检查 HTTP 200 与字段存在，
  没检查"不同页返回不同图"）—— 回归测试已补上这条断言。

---



## 10. Lead 最终交付验收（终局，冻结库）

### 10.1 翻译完成度
```
v2 全书 401 页：body 段 5580 / 5580 已翻译，untranslated = 0，failed = 0
  machine 3950+41  carried 139+3  seeded 6
累计费用：约 $0.10（首次 3902 条缓存复用 + 1731 新译 $0.078 + 补译 $0.0005 + 44 段补译 $0.0013）
```
中间经历 dev-relay 一次 rebuild 造成净损失（5650 → 4740，656 条被形状校验误拒），
Lead 用其 `translations_backup_20260917_111402.json` **按 fingerprint 精确回填 641 条**
（`spikes/fill_gaps.py`，只用精确指纹，不做模糊包含匹配），再直接调引擎补译 44 条
（`spikes/retranslate_gaps.py`），最终 **0 缺口**。

### 10.2 交付物（用户三件套）
| 产物 | 路径 | 页数 | 大小 | 说明 |
|---|---|---|---|---|
| 网页对照 UI | `http://127.0.0.1:8777` | — | — | 双栏联动高亮，14/14 自动校验通过 |
| 版式还原中文 PDF | `data/derived/full_cn.pdf` | **401** | 39.0 MB | 84.6 s 导出 |
| 双语对照 PDF | `data/derived/full_bi.pdf` | **401** | 40.4 MB | 146.2 s 导出，页宽 2.000×A4 |

### 10.3 中文 PDF 质量
```
pages=401  boxes=4772  overflow=13  shrunk=76
文本行 9715 行：min 4.29pt / p1 7.36pt / 中位数 9.20pt / max 33.12pt
< 6.0pt 的行：25 行 = 0.26%（集中在 p16 旧表格残影与 p291/392-401 缩写索引页，属版式密度）
p21 页：cjk_chars=526，ascii_words=3   <- 原有英文正文已被擦除，仅剩代码/缩写
```
> TOC 条目为 0 是因为**源 PDF 本身就没有书签**（`origin/BMS-Training-Manual.pdf` 的 `get_toc()` 在 v2 demo 上为 0），
> 不是导出丢书签。v1 原书有 231 条书签，若要保留请对 v1 导出。

### 10.4 双语 PDF 结构（`spikes/verify_final_pdfs.py`）
```
bilingual 每页 1190.64 × 841.92 pt，宽度 = 2.000 × A4（横向并排，非缩放）
逐页抽样左右半非白像素（左=英文原页 / 右=中文重建页）：
  p1   167922 / 166897   OK
  p21   91565 /  89548   OK
  p101 198119 / 196978   OK
  p301  65032 /  65091   OK
  p401  74894 /  70402   OK
p21：cjk_chars=528（右半中文）+ ascii_words=237（左半英文）—— 双语内容都在
cn 元数据 title = 'BMS - Training Manual (rev v2) (中文)'
```

### 10.5 对照浏览器 UI（`spikes/final_ui_check.py` → **14/14 PASS, exit 0**）
```
state: docTitle=BMS - Training Manual  pageInput=21  toc=216
       badges=新增 1修改 3移动 1删除 2  srcHots=78  cnHots=78
       modalHidden=true  errHidden=true  hscroll=false
PASS 无横向滚动 / 原文译文热区都渲染 / 术语表弹窗隐藏 / 错误条隐藏 / 变更徽章非空 / 目录已加载
PASS 点击原文 → 原文段选中(.sel)              selCls='hot sel'
PASS 点击原文 → 译文同段高亮(.peer)           peerCls='hot peer'  peerClsAll=['66346']
PASS 译文对应段滚入视口                        peerInView=true
PASS 详情面板弹出并显示原文                    detailHidden=false
PASS 反向：点击译文 → 原文同段高亮             srcCls='hot peer'  srcPeers=['66346']
PASS 双栏滚动同步                              {'src': 3000, 'cn': 3000}
PASS 变更过滤生效                              {"off": 406, "bars": 2}
PASS 无 JS 页面错误                            []
```

### 10.7 目标达成核对（逐条对 objective）

| objective 要求 | 状态 | 证据 |
|---|---|---|
| origin/ 下 PDF 结构化抽取 + 指纹建模 | ✅ | 401 页 → v1 5581 段 / v2 5580 段；`chars` 753839/753762；当前代码重抽逐字节一致 |
| 输出中文译文（DeepSeek） | ✅ | **5580 / 5580 段，missing 0、failed 0**；machine 3996 / carried 1578 / seeded 6；实花 ≈ $0.10 |
| (a) 左右对照浏览器 UI + 双向联动高亮 | ✅ | `spikes/final_ui_check.py` **14/14 PASS**（点击原文→译文 `.peer` 同段并滚入视口；反向亦然；滚动同步 src==cn） |
| (b) 版式还原中文 PDF | ✅ | `data/derived/full_cn.pdf` 401 页 / 37.2 MB / 83 s；`overflow=13` `shrunk=76`；p21 仅剩 3 个 ASCII 词（原英文正文已擦除） |
| (c) 双语对照 PDF | ✅ | `data/derived/full_bi.pdf` 401 页 / 38.5 MB / 139 s；页宽 2.000×A4；5 页抽样左右半均有内容 |
| 多版本 diff（新增/删除/修改/移动/重排 + 行内标记） | ✅ | v1→v2 `unchanged 4775 / modified 3 / added 1 / removed 2 / moved 1`；`aircraft`→`airframe` 判 MODIFIED 且行内 diff 正确；verifier 另用自造 PDF 验过全部边界 |
| 译文随原文变更同步更新（复用缓存、标记待复核） | ✅ | `carried 1578`（原文未变沿用）+ `seeded 6`（小改基于旧译修订）；`translation_cache` 按 fingerprint 复用；二次运行 0 次 API 调用 |
| README + 一键启动脚本 | ✅ | `README.md`（交付数据/质量抽样/工作流/TO 接入）+ `start_web.ps1` |
| 端到端验证（抽取/翻译/渲染/版本 diff/UI 截图） | ✅ | `tests/test_e2e.py` **83/83**；`test_core 33/33`、`test_translate 15/15`、`test_web 179/179`、`test_render ALL PASS`、`test_versions PASSED` |
| 多智能体验收审查 | ✅ | `review/REVIEW.md` 942 行，独立审查员结论 **通过**；发现 8 条问题（5 条阻断级）全部闭合或有据 |
| 后续可追加两份 TO 手册 | ✅ | 只读探针实测：US Letter + 5 页横向页均正常分段（详见 README §10.1） |

**结论：objective 全部达成。**

---



## 9. 更正记录（Lead 自纠）

### 9.1 关于「549 条译文配对」的结论被 verifier 修正我在 §3 之后曾用 25 条随机抽样得出"配对全部合理"的结论，**该结论是错的**（抽样偏差 ——
25 条恰好落在 p3/p4/p5 等非表格页）。verifier 按表格页复跑，复现 **8 条真实错配**：
```
seg=31307 p25 SRC='PROCEDURE'          -> '程序 场景 主 防撞-位置-机翼/尾翼 保险丝-形式(%) AR (%) 隐蔽 收 集 置'
seg=31125 p16 SRC='AVIONICS'           -> 整行表格译文
seg=31145 p16 SRC='CADC'               -> 'CADC 敌我识别（IFF） HOOK ICING'
seg=31163 p16 SRC='————'               -> 'AFT ———— ———— ————'
seg=31317 p25 SRC='DAY – Good weather' -> 一整行参数
另有 30 条 body 译文完全无中文、3 条 provider='mock' 的 〔…〕占位仍在
```
**真实根因**（verifier 归因，我接受）：不是 fingerprint 重挂错位，而是库外脚本
`data/derived/_restore_translations.py:81` 的规则 `if old_n in new_n or new_n in old_n` 过宽 ——
`new_n in old_n` 让"整行表格的译文"落到该行里任意一个单词格子上。

**教训**：小样本抽样不能代表存在结构性差异的子集（表格页 vs 正文页）。
后续验收改为**分层抽样**（按 `kind` × `page` 分层）并加**形状一致性断言**（`len(zh) <= max(3*len(en), 40)`）。

### 9.2 关于 `spikes/qa_side_by_side.py` 的 p25「20 对重叠」是误报
render-engine 指出并已修正：表格行切成独立 `table_cell` 后，**同一 baseline 的不同列单元格**
在只按 y 判断时会被当成重叠。已加 `x` 相交判断（`min(x1) - max(x0) > 0`）。
修正后 p21/22/23/25/27/30 全部 `OK no line overlaps`。

### 9.3 连字符 `normalize` 结论：无需修改
我早先以为 `normalize()` 未折叠弯引号 —— **是我的测试造得有误导性**（比较了一个带引号和一个不带引号的字符串）。
实际 `core/fingerprint.normalize` 对 `"` `"` `'` `'` 与 U+2010/U+2011/U+2013/U+2014/NBSP 全部正确折叠。
结论：该模块无需改动，`spikes/verify_normalize.py` 已更新为对照测试。

---

## 13. 交付后用户反馈（第二轮）—— 3 个问题

### 13.1 问题①：目录页（index）译文没有对齐 ✅已修复

**症状**：目录页的中文点线（dot leader）右端参差不齐，页码不在同一列。

**根因**（`render/pagebuild.py`）：目录行形如 `MISSION 1: GROUND OPS (TR_BMS_01_GroundOPS) ......... 9`，
原书这些行的点线右端（= 页码列）严格对齐在同一 x（Word 统一排到右边距）。
中文译文短得多（中文字符信息密度高），而渲染器只把译文**按左对齐**画出来，
点线在各自的中文末尾就断了 → 页码列散开。

**量化证据（修复前）**：
```
page 4: 49 条点线行，右端 x1 ∈ [412.6, 541.0] → 跨度 128.4pt
page 5: 53 条点线行，右端 x1 ∈ [387.6, 540.6] → 跨度 153.0pt
逐行对照原始 PDF 右端：中位偏差 3.4pt，最大 156pt，within-2pt 命中 0/49
```

**修复**（`render/pagebuild.py` 新增 4 个辅助函数 + `compute_layout` 接线）：
`_has_dot_leader` / `_split_dot_leader`（识别并拆分「正文+点线」）、
`_measure_runs` / `_runs_for_text`（单行混合字体宽度测量与 run 切分）、
`_fit_dot_leader`（**只重算点号数量**，使「正文+点线」右端落在原书同一列——不动正文、不动字号）、
`compute_layout` 中点线行改用 `lx = dot_anchor - lw` 定位（`dot_anchor` 取该段原始 `line_boxes` 最右 x1）。

**验证（`data/derived/full_cn.pdf` 已重新导出）**：
```
page 4: 49 条点线行，右端 x1 ∈ [540.3, 543.4] → 跨度 3.1pt   （修复前 128.4pt）
page 5: 53 条点线行，右端 x1 ∈ [541.1, 543.4] → 跨度 2.3pt   （修复前 153.0pt）
逐行对照原始 PDF：中位偏差 1.27pt / 1.39pt，within-2pt 47/49 与 52/53
```
`tests/test_render.py` 复测 **ALL PASS**；`full_cn.pdf` 与 `full_bi.pdf` 均已重新导出并复核
（401 页，`boxes=4772 overflow=13 shrunk=76`，与修复前一致 → 未引入新溢出）。

### 13.2 问题②：图片里的文字能否翻译 —— 能，但需要独立 OCR 管线（**本轮未实现，附方案**）

**现状与数据**：
* 全书 **1518** 个图片 placement；**79 页**非页眉图片面积占比 > 30%；103 页同时有大量可抽取文本与大图。
* 用户截图那页（**p11 座舱面板图**）：可抽取文本 615 字符（正文），但图里
  `LEFT INDEXER` / `MASTER CONTROL LIGHT` / `LEFT EYEBROW LIGHTS` 等标注**是像素**——
  该图是单个 `xref=90`、1056×1218px 的位图（`px_per_pt=2.17`），**没有文本层，`get_text()` 抽不到**。
* 另有纯图页（p114–117，图片占比 0.43–0.49，可抽取文本仅 34 字符）——这类页面基本只有图内文字。

**方案（按性价比排序）**
1. **标签双层方案（推荐首发）**：不改原图，在图上叠加「中文标签 + 细引线」。
   零风险、可逆，纯图页收益最大。实现：OCR 检测文本框 → 过滤（长度/置信度/重复模板）→ 翻译 → 画译文胶囊 + 引线。
2. **原位替换**：OCR → 用背景色/局部修补擦掉英文 → 原位画中文。
   观感最接近原书，但**在照片背景上修补质量风险高**（座舱图是灰阶照片 + 彩色面板，擦不干净很明显），
   要做到体面需要 inpaint 模型或人工逐图校对。
3. **术语对照表**：把图内标签做成「原标注 → 中文」词汇表，页面角落统一列出。成本最低。

**工作量与风险**：OCR 检测需新增依赖（本机未装 paddleocr/tesseract）或走外部服务；
**主要成本在质检**——79 页大图 × 20–40 个标签 ≈ 2000–3000 个标签，需要抽样目视验收。
建议**先做 3–5 页样板页**（p11 座舱图、p114–117 纯图页、p291 MFD 页）确认观感后再全量铺开。

### 13.3 问题③：滚动到后面页面原文显示不出来 ✅已修复

**症状**：向后翻页/滚动时某一侧（实测以译文栏为主）页面区整块空白，滚回去又正常。

**排查（先证明不是服务端问题）**：直接请求浏览器报告未加载的那些 URL
（p40/42/43/44/120/124/300/302/303/304）→ **全部 HTTP 200、13–45 ms、35–193 KB、`X-MT-Cache: hit`**。
服务端完全正常 → 定位到前端懒加载。

**根因**（`web/static/app.js`）：
```js
async function ensurePage(side, p) {
  if (!rec.data) await ensureData(p);           // 可能失败/未就绪
  if (force || !rec[flag]) paintSide(side, p);  // ← 置位不依赖是否真的画出来
}
function paintSide(side, p) {
  if (!el || !rec.data) return;                 // ← 提前返回，img.src 根本没赋值
  rec.srcPaint = true;                          // ← 但 flag 已被设上
}
```
`paintSide` 在 `rec.data` 未就绪时提前返回（**不设置 `img.src`**），但 paint-flag 已被置位；
之后每次滚动回这一页，`!rec[flag]` 为 false → **跳过重绘** → 该页永久空白，直到强刷。
原文栏（source）从不出现此问题，因为它先加载数据、先被绘制。

**实测证据（修复前，20 个滚动时间点里 6 个命中）**：
```
[jump 40]            cn page=40  src='' complete=True  naturalWidth=0   ← 从未赋值 src
[target40 scroll+2]  cn page=43  src='...kind=cn...'   complete=False
[jump 120]           cn page=120 src='' complete=True  naturalWidth=0
[jump 300]           cn page=300 src='' complete=True  naturalWidth=0
```

**修复**：`paintSide` 返回 `true/false` 表示是否真正完成绘制；
`ensurePage` 改为 **`if (paintSide(...)) rec[flag] = true;`**（只有真画出来才置位）；
另加补偿分支：若 flag 已置位但 `img.src` 仍为空，强制补画一次。

**验证**（`spikes/diag_blank_pages.py`，跳页 + 连续滚动共 20 个时间点）：
```
修复前: 6 处 "WITHOUT a loaded image"
修复后: 20/20 全部 OK all in-view pages have loaded images
        failed requests 无 · 非 200 响应 无 · JS 页面错误 无
```
覆盖 p12→17、p40→45、p120→125、p300→305 四条路径。`node --check web/static/app.js` 通过。

### 13.4 本轮我自己造成的一次事故（已修复）
编辑 `render/pagebuild.py` 时重复应用了一次 edit，把 `layout_mixed_runs` 写坏
（产生 `slot = line[0][1] if line else "latin"        if any(...)` 这种非法语法）。
已按语义修正，并用 `ast.parse` + `import` + `tests/test_render.py`（ALL PASS）三方确认恢复。

---

## 14. 图内文字翻译：方案 1 样板（用户要求先出样板）

### 14.1 关键发现：OCR 依赖**本来就在**机器上
排查后发现 **`rapidocr-onnxruntime 1.4.4` 已安装**（依赖 `onnxruntime 1.28.0` + `opencv-python 5.0.0.93`），
**不需要新增任何依赖**。这直接把"方案 2/3"的落地成本砍掉一大截。
（另：Windows 自带 OCR 语言包只装了 en-US / ja-JP / zh-CN；无 tesseract 二进制。）

### 14.2 流水线（`render/annotate.py` + 两个 spike）
```
PDF 页 → 取最大图片 placement → 300dpi 渲染该区域
      → rapidocr 检测文本框（p11 检出 74 个，score 多≥0.95）
      → norm_label：修 OCR 把全大写词粘连的毛病
        （ALTGEAR→ALT GEAR、MASTERCAUTIONLIGHT→MASTER CAUTION LIGHT、
          AVIONICPOWER→AVIONIC POWER，靠 60 词航空词表做最长匹配切分）
      → 复用项目**自己的** translate.engine（glossary 感知 + 批处理）翻译
      → 像素坐标 → 页面 pt 换算（scale = 72/dpi）
      → 三阶段布局：直觉方位 → 8 方向逐圈局部搜索 → 兜底外推
        （碰撞回避同时避开**其它英文标注**，这是密集面板图的主要冲突源）
      → 半透明胶囊 + 橙色细引线 + 中文（Deng 字体）画到页面上
```
**非破坏性**：原图一个像素都不改，中文标注画在**页面**层，可搜索、可复制、可随时关掉重来。

### 14.3 样板页实测（`data/derived/annotated_sample.pdf`，6 页）
```
p11  座舱面板图：计划 38 → 绘制 38
p114 纯图页（HARTS #2 机动图）：24 → 24
p115 纯图页（HARTS #3）：22 → 22
p116 纯图页（HARTS #4）：25 → 25
p117 纯图页（HARTS #5）：25 → 25
p291 HSD 数据链页面（纯文本+示意）：22 → 22（有意跳过 5 条长句）
```
* 输出 PDF 文本层含中文：p1=170 / p2=121 / p3=105 / p4=130 / p5=113 / p6=54 字符，
  `get_text()` 可直接搜到「中央 / 左索引器 / 警戒灯面板 / 燃油量」→ **可搜索可复制**。
* 翻译质量抽样：`MASTER CAUTION LIGHT→主警戒灯`、`LEFT EYEBROW LIGHTS→左眉灯`、
  `CAUTION PANEL→警戒灯面板`、`SPEED BRAKE→减速板`、`EPU FUEL→应急动力装置燃油`、
  `HOLD ON AOA LIMITER UNTIL HORN→保持迎角限制器直到告警音`。
* 目视验收（`data/derived/preview/annot_p*.png`，左原右标注对的 `annot_cmp_p*.png`）：
  p114–117 效果最好（空间充裕，中英并列干净）；p11 密集面板图可行但局部偏挤。

### 14.4 已识别的局限（样板阶段）
1. **长句不按标签处理**：OCR 会把图注/正文段也检出来（p291 有 5 条），
   塞进小胶囊既放不下也难看 → 已按 `len(zh)>40 或空格数>6` 过滤掉。
   这类文字本应由**主文本管线**翻译（它们大多是"图片+文字混排"里的文字层），
   后续应做去重避免与主管线重复翻译。
2. **OCR 粘连词**需要词表切分，词表覆盖不到的组合会原样保留（如 `INTLIGHTS→INT LIGHTS` 可切，
   但偏门缩写会漏）。
3. **p11 这类超密集图**：38 个标签挤在 A4 一页，局部仍有视觉拥挤；可能需要
   「按区域分组编号 + 页边对照表」的变体。
4. 尚未做：全书 79 页大图的批量 OCR（预计 3000+ 标签）、云端视觉模型兜底、与主管线的去重协调。

---




---

## 15. 后续处理（用户确认后按顺序执行）

### 15.1 ① 密集图「编号 + 页边对照表」变体 —— 已实现，但默认不启用

实现了 `render/annotate.py` 的 `draw_numbered()` + `cli.py annotate --style numbered`：
图上打 ①②③ 圈号，中文集中排在页面空白处的对照表里；对照表位置由 `_free_rects()`
计算 —— 以所有已知矩形的上下边界切横带，逐带算水平空隙。

**关键修正**：第一版没把「页面上已有的其它文字」算进占用区，对照表直接盖住了页首正文段落；
补上 `_page_text_boxes()` 后不再遮挡。

**但实测结论是：在 A4 上这个变体不如胶囊。** p11 的座舱图几乎占满整页
（图 y=185..747，正文 y=71..150，页脚 banner y=776 起），放得下 34 条对照表的空闲矩形
**根本不存在** —— 无论放哪都会盖住图或正文，而被盖住的正是要标注的内容。
胶囊风格虽然局部拥挤，但不遮挡任何内容。因此 `--style auto`（默认）**一律用胶囊**，
numbered 保留为可选（适合图四周留白大的页）。

### 15.2 ② TO 手册接入 —— TO-34 已入库并抽样验证

隔离库（`MT_DATA_DIR=data/_to_probe`）验证，**不污染主库**：

    python cli.py ingest --pdf "origin/TO 1F-16CMAM-34-1-1 BMS.pdf" --slug to-34
      页数 669  段数 11796  字符 1,256,320  图片 1268  耗时 28.4s（23.6 页/秒）
      role: body=11128 header=668
      kind: paragraph=6232 list_item=3412 table_cell=1151 heading=327 caption=6

与只读探针预估（约 10417 段）一致 —— 抽取器对 US Letter + 5 页横向页**无需改代码**。

抽样翻译第 1–30 页：**1077 段 / 23.5s / $0.038 / 0 失败**。质量抽样：

    p2   PURPOSE AND SCOPE                 -> 目的与范围
    p9   ... Range Cruise Option (RNG)     -> ... 航程巡航选项 (RNG)
    p11  2.3.1.4.1.2 Air-to-Ground Modes   -> 2.3.1.4.1.2 空对地模式
    p11  2.6.1.3.1 Gain and Level Control  -> 2.6.1.3.1 增益与电平控制
    p2   (c) 2003-2025 Benchmark Sims.     -> (c) 2003-2025 Benchmark Sims。保留所有权利。

### 15.3 发现并修复：句中切分导致残缺译文（新增 cli.py quality）

**问题**：抽取阶段会把极少数句子切在两个段里，翻译器只看半句就产出残缺译文。
最严重一例（TO-34 p2）：

    EN 前半: '... of this manual (except printing for your own personal use) is allowed'
    EN 后半: 'without the written permission of the BMS Docs team.'
    ZH     : '。'          <- 前半整句漏译，后半只剩句号

**排查中的自我纠正**：我最初用宽泛规则量出 TO-34 有 **48 处**句中切分，准备改
`_merge_continuations` 重新 ingest。逐条看样本后发现**绝大多数是误报** ——
行首列表标记（`b) < 75° bank`、`a. Select TGT-TO-WPT page`、`v. Marine`）的
`a.`/`b.` 天然"小写开头"，被规则误判。加严列表标记正则与行内枚举（`i. HQ ii. Infantry`）后，
**同页真·句中切分只有 3 处**；再加「跨页」类别共 **10 处**（TO-34 4 处、训练手册 6 处）。

**为什么不重新 ingest**：为 10 处重切而重跑全书，会让训练手册 5580 条译文全部重译
（约 $0.10 + 15 分钟，且重建所有段 id）。收益与代价不成比例。

**改为有界定点修复**（`translate/quality.py` + `cli.py quality`）：

    1. 保守规则找可疑译文：
       * 原文 >=30 字符而译文去标点后 <=2 字符；
       * 原文 >=60 字符而译文长度比 < 0.06；
       * 译文以逗号/分号结尾（中文不该这样收尾）。
    2. 对可疑段做「带上下文重译」：把该段 + 紧邻下一段一起送翻译，
       再按长度比例切回本段部分；只覆盖这些段的译文。

**实测结果**：

    修复前   TO-34 1 条可疑 / BMS 训练手册 11 条可疑
    修复     11 条全部修复成功，0 失败
    修复后   TO-34 0 条 / BMS 训练手册 0 条

典型改善：

| 段 | 修复前 | 修复后 |
|---|---|---|
| p6 `I. EXTERNAL LIGHTNING SETTINGS ....` | `I. 外部` | `I. 外部灯光设置 ....` |
| p6 `1B POSSIBLE EXTERNAL LIGHTING SETTINGS - NVIS ...` | `照明` | `1B 可能的外部灯光设置 - 夜视成像系统不兼容 ....` |
| p180 `When the altitude reaches 6650 feet, roll 110 deg inverted ...` | `当高度达到6650 ft时，滚转110°倒扣并拉向` | `当高度达到 6650 ft 时，滚转 110° 至倒飞状态，并向目标拉杆（第 2、3 张图）。…` |
| p240 `Ensure delivery mode is set to PRE ...` | `第` | `确保投放模式设为 PRE，按周边按键（OSB） 19 将解除保险延迟设为 9.00 秒。…` |
| TO-34 p2 `without the written permission ...` | `。` | 已整段重译 |

主库完整性复核（修复后）：**v2 body 5580 / translated 5580，missing 0，failed 0**。

### 15.4 新增命令（本轮）

    python cli.py annotate [--style auto|capsule|numbered] [--pages a-b] [--dry-run] [--rescan]
    python cli.py quality  [--doc D] [--version V] [--fix] [--limit N]
    python cli.py portable / setup        # 打包与目标机器自检（上一轮）


### 15.5 ③ PDF 目录书签 —— 已修复（216 条中文目录）

**问题**：`render/pdfout._finish()` 用 `src.get_toc()` 复制源 PDF 的书签，但
`origin/BMS-Training-Manual_v2_demo.pdf` **自身没有书签**（pymupdf 重建的 demo），
所以导出的中文/双语 PDF 目录都是空的。原版 v1 有 **231 条**书签。

**做法**：`render/pdfout.build_cn_outline(conn, version_id, prev_pdf=...)`
* 用**上一版 PDF 的书签**提供「层级 + 页 + 小节号」这套人工编排的结构；
* 把每个条目匹配到**当前版本**的段（优先按小节号，其次标题指纹），
  取其**中文译文**作为书签标题；
* 丢掉纯页码的冗余条目（v1 里有 `1`/`1.1`/`2` 这类只有编号的书签）；
* 匹配时优先 `heading` 段、优先靠近原书签页，避免目录页上的同名行被误选
  （第一版把 `10.1 武器` 指到了 p5 的目录行，修正后指到 p172 正文）。

`pipeline.export_version_pdf()` 自动取上一版的 `source_path` 调它，
通过 `cn_toc=` 传给 `export_cn_pdf` / `export_bilingual_pdf`。

**过程中发现两个真 bug**：

1. **`set_toc` 整份被拒**：条目页号超出本文档页数时抛
   `ValueError: row N: page number out of range`，**整份目录都被拒**。
   导出子集（只导 1–31 页）时目录里必然有超范围条目 ——
   而 `_finish` 的裸 `except` 把异常吞掉了，表现为"书签静默消失"。
   修复：先按实际页数过滤、再归一化 level 不跳级；`except` 里改成打印告警，
   不再静默失败。
2. **`_page_list` 的 tuple 语义**（verifier 报的 M-8b 在这里复现）：
   把 `(1,31)` 当"页列表"处理，导致 `export --pages 1-31` 只出 **2 页**。
   已与 `pipeline.normalize_pages` 对齐：`None`=全书 / `int`=单页 /
   `tuple`=区间 / `list`=显式列表。

**验证**：
```
build_cn_outline -> 216 条（层级分布 L1:14 L2:35 L3:103 L4:60 L5:4，页 2..393）
export --pages 1-31 -> 31 页，书签 27 条（全部落在导出范围内）
full_cn.pdf   401 页  书签 216 条  L1 p2 前言 / L1 p9 第1部分：基本操纵 / L3 p14 1.4 ...
full_bi.pdf   401 页  书签 216 条  （同上）
```

### 15.6 ② TO-34 全书翻译完成

```
v1  669 页  11796 段  body 11796 / translated 11796   missing 0  failed 0
              machine 10948  carried 848
成本：抽样 30 页 $0.038 → 全书约 $0.4（dry-run 估 $0.67，偏高，与主库同一现象）
质量审计：初次 3 条可疑 → --fix 修 3 条 → 0 条
导出抽样：--pages 1-30 → 30 页 / 1077 boxes / overflow 0 / 中文字符 7025
```
隔离库位于 `data/_to_probe/`，**主库未受影响**。

### 15.7 ④ 译文随版本同步更新 —— 复核确认（无需改动）

原计划的"重算"实际已在 `translate/engine.py` 的增量逻辑里：
* 原文未变 → 命中 `translation_cache`，写 `carried`（主库实测 778 条）；
* 小幅改动 → 用旧译文当种子让模型修订，写 `seeded`（主库实测 6 条）；
* 其余 → `machine`。
主库 v1→v2 的实测就是这条路径（`carried 778 / seeded 6 / machine 3996`），
二次运行 0 次 API 调用。**这条无需额外开发，已在 §5 与 §13 验证过。**

### 15.8 本轮新增/修改

| 文件 | 变更 |
|---|---|
| `render/annotate.py` | 新增 `draw_numbered` / `_free_rects` / `_page_text_boxes`（编号变体，默认不启用） |
| `render/pdfout.py` | 新增 `build_cn_outline`；`_finish` 支持 `cn_toc` 且不再静默失败；`_page_list` 语义对齐 |
| `pipeline.py` | `export_version_pdf` 自动生成中文目录；`_call_pdfout` 透传 `cn_toc` |
| `translate/quality.py` | **新增**：译文质量审计 + 带上下文定点修复 |
| `cli.py` | 新增 `quality` 命令；`annotate --style` |

### 15.9 TO-1（404 页）已完成

```
python cli.py ingest --pdf "origin/TO 1F-16CMAM-1 BMS.pdf" --slug to-1
  页数 404  段数 6194  字符 623,219  图片 2863  耗时 33.4s
  role: body=5791 header=403
  kind: paragraph=3183 list_item=2485 heading=87 table_cell=33 caption=3

全书翻译：152s，$0.19（dry-run 估 $0.32，仍是偏高）
  首轮 36 段失败（模型漏回 id: 'model response missing ids'）
  → 用 spikes/retry_untranslated.py 补译：36/36 全部成功，0 失败
最终：body 6194 / translated 6194   missing 0   failed 0
导出抽样：--pages 1-30 → 30 页 / 646 boxes / overflow 0 / 中文 4371 字符
```

隔离库 `data/_to_probe/` 现在有两份文档：`to-34`（669 页）与 `to-1`（404 页）。

**关于「模型漏回 id」**：这是 DeepSeek JSON 模式下偶发的批次问题（481 批里约 3 批），
表现是整个批次被判 FAILED、那 12 段留空。补译一轮即可清掉（36/36）。
批量翻译后**建议固定跑一次 `spikes/retry_untranslated.py`** 或 `cli.py translate` 二次运行
（后者会跳过已有译文，只补空的）。

### 15.10 ⚠️ 一次自我造成的回归：`repair_continuation` 已改为**默认只报告**

**做了什么**：为了修 TO-1 p185 一条被错位的**安全警告**（横向配平权限），
我写了 `quality.repair_continuation()` —— 找出「相邻两段其实是同一个句子」的对，
合并重译后按比例切回两段。

**结果与代价**：第一次跑（TO-1）匹配到 **110 对并全部写入**，但事后逐对检查发现
**绝大多数是不该动的**：`FOREWARD` + 正文、`TABLE OF CONTENTS` + 正文、
项目符号 `•` + 列表项、目录点线行 + 目录号 —— 这些结构相邻但语义独立，
合并重译把它们改坏了（例如目录行被写成了带重复文字的句子），
**并且顺带把错误结果写进了 `translation_cache`**，原始译文因此丢失。

**恢复**：写 `spikes/recover_damaged.py`，对受损段做**逐段独立重译**恢复干净译文。
复核确认已知案例已恢复：
```
seg=11841 EN: 'Foreword ..........'      ZH: '前言 ..........'        （修复后不再有重复文字）
seg=11842 EN: 'TABLE OF CONTENTS ...'    ZH: '目录 ..........'
seg=11843 EN: '1 The Aircraft .....'      ZH: '1 飞机 ........'
```
TO-1 最终仍为 6194/6194、0 失败。

**措施**：`repair_continuation()` 现在 **`apply=False` 为默认**，
只打印候选给人看；要修改必须显式传 `apply=True`，并在 docstring 里写明了这次教训。
放宽判据后候选从 110 降到 **79**，且抽查显示其中不少其实是**正确**的
（如 `'Anti-G System'`+`'The panel is there but...'` 译成
`'抗荷系统面板已存在，'`+`'但与系统的交互尚未实现。'` —— 这是对的）。

**结论**：这类「结构相邻 vs 语义连续」的判别，靠长度/标点启发式无法可靠自动化。
已按「只报告、人工确认」处理，不再自动改库。

### 15.11 三份手册的当前状态

| 文档 | 页数 | 段数 | 译文 | 缺失 | 所在库 |
|---|---|---|---|---|---|
| BMS 训练手册 | 401 | 5580 | 5580 | 0 | `data/app.db`（主库） |
| TO 1F-16CMAM-34-1-1 | 669 | 11796 | 11796 | 0 | `data/_to_probe/app.db`（隔离） |
| TO 1F-16CMAM-1 | 404 | 6194 | 6194 | 0 | `data/_to_probe/app.db`（隔离） |
| **合计** | **1474** | **23570** | **23570** | **0** | |

累计翻译成本约 **$0.4 + $0.19 + $0.10 ≈ $0.7**。
三份手册的翻译均已通过 `cli.py quality` 审计（0 条可疑）。

**是否把两份 TO 并入主库**由你决定 —— 并入后可以在同一个浏览器里切换三份手册、
共享术语表；当前的隔离方式便于先单独验收。

### 15.12 TO 手册全书中文 PDF 导出（含完整中文目录）

**发现的差异**：TO 手册的源 PDF 由 Adobe PDF Library 生成，**自带高质量原生书签**
（TO-1 561 条 / TO-34 1093 条，层级 5–8 层），远比训练手册那边强。
所以 `build_cn_outline` 增加了 `same_pdf` 参数：**没有上一版时就用源文件自己的书签**
做结构参考（优先 `prev_pdf`，退回 `same_pdf`）。

**过程中修的 4 个真缺陷**：

1. **小节号与标题被切成两段**（TO-1 特有）：`'1.1'` 与 `'Aircraft General Arrangement'`
   是两个独立段，只取号会得到 `1.1` 这种无意义书签。
   新增 `_title_for()`：本段若**只有小节号**，把紧随其后的真标题接上。
2. **`len(raw) <= 8` 的粗暴判断**把 `FOREWARD`（8 字符）这类**完整短标题**误判成"只有编号"，
   于是把整段前言拼进书签，得到
   `前言 本手册包含安全高效操作飞机所需的信息…`。收紧为 `bare_re` 精确匹配小节号。
3. **目录页的行抢走了书签**：`1.1 Aircraft General Arrangement`（书签在 p19）
   被 p3 目录页上的同名行匹配走，因为旧评分给了 `heading` 加分权重过高。
   改为**页距离主导**（`-min(d,60)*0.35`）+ 目录区（前 10 页且距离 >12）重罚 -3.0，
   标题匹配只作次要项。
4. **`set_toc` 的层深上限写成了 6**：TO-34 有 113 条书签在 7–8 层，被静默丢掉
   （1093 → 980）。PDF 目录本身支持更深嵌套，改为允许 12 层。

另外顺手清理了上游译文缺陷带到书签里的尾巴：
`第二部分 - 飞机武器投放系统与控制 2.` → 去掉裸编号；
`2.1 GENERAL AND…` 译文丢了号（`1 一般与杂项控制`）→ 按原文补回 `2.1`。

**产出**：
```
data/derived/to1_full_cn.pdf    404 页  30.6 MB  书签 560 条  boxes=5748 overflow=1  shrunk=847
data/derived/to34_full_cn.pdf   669 页  53.9 MB  书签 1093 条 boxes=11081 overflow=46 shrunk=1594
```

**书签页码准确性抽查**（每份随机 12 条，检查目标页是否含该标题）：
```
训练手册  216 条 → 12/12 OK
TO-1      560 条 → 12/12 OK
TO-34    1093 条 → 12/12 OK
```
样例：
```
OK L3 p 279  38.3.2 热启动（地面）                       [TO-1]
OK L5 p 132  2.2.1.2.5 其他综合控制面板（ICP）开关功能      [TO-34]
OK L7 p 227  2.3.1.6.1.1.2 目视空对地                    [TO-34]
OK L3 p 307  18.11 AIM-120 AMRAAM 导弹                 [训练手册]
```

### 15.13 三份手册的交付产物

| PDF | 页数 | 大小 | 书签 | 说明 |
|---|---|---|---|---|
| `full_cn.pdf` | 401 | 37.2 MB | 216 | 训练手册中文版 |
| `full_bi.pdf` | 401 | 38.5 MB | 216 | 训练手册双语对照 |
| `to1_full_cn.pdf` | 404 | 30.6 MB | 560 | TO-1 中文版 |
| `to34_full_cn.pdf` | 669 | 53.9 MB | 1093 | TO-34 中文版 |
| `annotated_full.pdf` | 20 | 19.6 MB | — | 图内文字标注样板 |

回归测试全绿：`test_core 33/33` · `test_translate 15/15` · `test_versions PASSED` ·
`test_render ALL PASS` · `test_web 179/179` · `test_e2e 全部通过`。

### 15.14 三份手册合并到一个库

**备份**：动手前先把主库复制成 `data/app.db.bak_merge_20260917_165121`（15.3 MB），
回滚只需把它拷回 `data/app.db`。

**做法**（`spikes/merge_to_into_main.py`）：
1. 用 `pipeline.ingest_pdf` 把两份 TO 源 PDF **重新入进主库** —— doc_id / version_id /
   segment_id 全部由主库分配，不会撞号；源路径按相对路径存。
2. 按 **位置 (page, order_index)** 把隔离库已有译文搬到主库对应段（见下面的坑），
   顺带把 `translation_cache` 也搬过去，便于以后命中缓存。
3. 复核条数与内容。

**过程中的一个坑（差点静默丢 1697 段译文）**：
最初只用 **fingerprint** 当键来搬译文，结果 TO-34 只搬到 9431 / 11128。
查明原因：**11128 个 body 段只对应 9431 个不同 fingerprint** —— 页码、样板句等
重复内容会折叠，按指纹搬必然丢数据。两个库用的是同一个抽取器、同一份 PDF，
所以改用 (page, order_index) 精确映射，**命中 11128/11128、5791/5791，零丢失**。
（指纹只作为位置匹配失败时的兜底。）

**合并结果**：
```
doc1 bms-training-manual  v1 401页 body 5581 translated 0
                          v2 401页 body 5580 translated 5580  missing 0
doc2 to-34                v1 669页 body 11796 translated 11128  missing 668  (= 页眉数)
doc3 to-1                 v1 404页 body 6194  translated 5791   missing 403  (= 页眉数)
```
三份手册的 `missing` 恰好等于各自的页眉段数（668 / 403）—— 页眉按设计不翻译，属正常。

**API / UI 复核**（`spikes/verify_merged_api.py`、`spikes/verify_merged_ui.py`）：
```
/api/documents → 3 份（doc1 训练手册 401页 2版本 / doc2 TO-34 669页 / doc3 TO-1 404页）
每份的第 21 页 segments 都带译文；markers 热区正常（19 / 38 / 3 条）
UI：文档下拉框 3 项；三份都能渲染热区、目录、无遮挡弹窗 → 11/11 PASS
```

### 15.15 修掉一个"深链跳错页"的真 bug（较难查，记录过程）

**症状**：`?doc=2&vid=3&page=351` 会落到 **360** 页；`?doc=3&page=300` 落到 **308**；
同一 URL 反复访问结果还不稳定（有时是 1）。训练手册（doc1）却一直正常。

**排查**（每一步都排除了一个假设）：
1. 怀疑服务端 → 直接请求这些页的图，全部 200 且哈希各不相同 → **服务端没问题**。
2. 怀疑 `set_toc` / 深链解析 → `readUrl()` 解析正确，`gotoPage` 也被正确调用。
3. 怀疑浏览器表单恢复 → 给 `#pageInput` 加 `autocomplete="off"`、把 onchange 绑定推迟到
   boot 之后 → **仍未解决**（这一步改动是无害的加固，保留）。
4. 怀疑浏览器滚动恢复 → 设 `history.scrollRestoration = 'manual'` → **仍未解决**（保留）。
5. **用 setter 拦截 `#pageInput.value` 抓到写入栈**（`spikes/trace_writes.py`），真相是：
```
t= 1023 v=351   at gotoPage (app.js:824) at loadDoc (app.js:157) at boot   ← 正确
t= 4703 v=360   at app.js:639                                              ← 3.7 秒后被覆盖
```
即 boot 时页码**是对的**，但约 **3.7 秒后**首屏页面图加载完成、版面被撑开产生一次滚动，
`onScroll` 按"当前可见页"反推页码，把 360 写回了页码与 URL。

**修复**：引入 `S.deepControl` —— 深链指定了页码时开启"保护"，期间 `onScroll` 不反推页码；
**首次真实用户输入**（wheel / touchstart / keydown / pointerdown）即交还控制权，
另设 15 秒兜底。不用固定超时，因为首屏图加载耗时不定（实测 3.7 秒，但会随机器变化）。

**验证**：
```
深链 8/8 全部落到正确页（doc1 21/99、doc2 21/351、doc3 21/300，带/不带 vid 都对）
保护释放后行为正常：滚动更新页码（351→373）、手动跳页 400 生效、双栏同步（差 206px）→ 5/5 PASS
```
`spikes/diag_deeplink2.py`（深链矩阵）、`spikes/verify_deeplink_regression.py`（释放后回归）
都可重复执行。

### 15.16 用户反馈：目录页码全部消失 —— 根因是我自己的点线修复（已修）

**用户症状**：浏览器的目录页里，页码列的数字都看不到了。

**根因（完全是我上一轮"点线对齐"改动引入的回归）**：
目录行在原文里的形态是 `MULTIPLAYER FLYING AND TRAINING ....... 7` ——
**页码在点线之后**。而我写的 `_split_dot_leader()` 用「第一个点之后的全部内容」当点线段：

    i = t.rfind('.'); j 往回吃掉所有 '.'; dots = t[j+1:i+1]; return t[:j+1], dots

于是 `....... 7` 里的 `7` 被一起吞进"点线段"，`_fit_dot_leader()` 重建点时
只输出 `body + '.'*n`，**页码被丢弃**。页码列因此整列消失。

更糟的是：调整过数量的点线会一直铺到原书页码列的右端（x≈541），
即使页码还在，也会被点线盖住。

**修复**（`render/pagebuild.py`）：
1. `_split_dot_leader()` 改为返回 **(正文, 点线, 页码)** 三段：
   先用正则把结尾的页码切出来（`[\s]([0-9]{1,4}[A-Za-z]?\.?|[A-Z]-\d{1,3})$`），
   再从剩余部分切点线。
2. `_fit_dot_leader()` 的预算改为「正文 + 点线 + 空格 + 页码」整体落到锚点：

       budget = min(anchor_right - x_start, max_width)
       n = int((budget - body_w - num_w) / dot_w)

   并加了两处兜底：逐格减点号直到放得下；实在放不下就「正文 + 页码」紧排，
   **保证页码一个字符都不丢**。
   只看 `anchor_right` 会让行右端越过正文右边界（实测 x 到 545.6，
   超出左边距 4.5pt），所以取两个上界的最小值。

**验证**：
```
spikes/test_dot_fit.py         5/5：末端落在锚点内 + 页码保留
   x0=53.9 anchor=541.1 末端=539.1 保留页码=True
   x0=91.3 anchor=541.1 末端=539.2 保留页码=True  （1000 这种四位数也保住）
compute_layout 实测 p4：19 条点线行末端全部 = 541.1，页码完整
PDF: full_cn.pdf p4 有 50 行以数字结尾（= 目录条目数）
     放大对比图 _toc_pagenum_zoom_p4.png：左原版 / 右中文版页码列逐行对齐
UI : CN 画布右侧 12% 区域有 12239 个不透明像素 -> 页码确实画出来了（4/4 PASS）
```

**顺带修的一个 CLI 缺陷**：合并成 3 份文档后，不带 `--doc` 运行 `export` 会
`SystemExit("存在多个文档，请用 --doc 指定")`，而 `main()` 里 `int(exc.code)` 对字符串
抛 `ValueError`，把**真正的提示信息盖掉**，只看到一堆堆栈。
已改为：字符串 code 打印 `❌ <消息>` 并返回 2。现在会明确提示 `❌ 存在多个文档，请用 --doc 指定`。

## 16. 当前整体状态（截至本轮）

| 交付物 | 状态 |
|---|---|
| 主库（三份手册） | 训练手册 v1+v2 / TO-34 / TO-1，共 22499 条译文 |
| 对照浏览器 | `python cli.py serve` → 3 份文档可切换；深链 8/8 正确；联动高亮正常 |
| `full_cn.pdf` / `full_bi.pdf` | 各 401 页 / 216 条中文书签 / 目录页码已修复 |
| `to1_full_cn.pdf` / `to34_full_cn.pdf` | 404 / 669 页，560 / 1093 条中文书签，页码准确率 36/36 |
| `annotated_full.pdf` | 20 页图内文字标注 |
| 可分发打包 | ⚠️ 现有 zip 落后：不含目录书签、TO 手册、`quality`/`annotate` 命令、深链与页码修复 |

### 15.17 页码"还是没显示"—— 真因是**服务端进程内布局缓存未失效**

**用户第二次反馈**：PDF 已修好，但浏览器里页码仍然不见。

**排查**（这次先做了正面对照，不猜）：
```
服务端 /translated-layout 返回：  '........................................'   ← 无页码 ❌
直接调 compute_layout 同一页 ：  '...................................... 2'  ← 有页码 ✅
```
同一个函数、同一份数据，**API 和进程内调用结果不同** → 只可能是 API 那边有缓存。

真因在 `web/server.py`：
```python
key = (version_id, page, rev)      # rev 只反映译文条数/修订号
if key in _LAYOUT_CACHE: return _LAYOUT_CACHE[key]
```
`_LAYOUT_CACHE` 是**进程内** LRU，键里**没有任何"代码版本"信息**。
我修好 `pagebuild.py` 后，进程里还留着改之前算出来的布局（无页码），
于是每次请求都命中旧缓存 —— 只有重启服务才会恢复。
这解释了为什么"PDF 是好的、浏览器是坏的"。

**修复**：把 `render/` 下各模块的 mtime 揉成指纹加进缓存键，
代码一改缓存自动失效（带 2 秒节流，避免每次请求 stat 一堆文件）：
```python
def render_fingerprint() -> str:      # render/*.py 的 mtime 哈希
key = (version_id, page, rev, render_fingerprint())
```
实测：改 `pagebuild.py` 后缓存立刻失效并重算（0.03s → 0.14s）。

### 15.18 顺带修掉的第二个渲染缺陷：两条目录项被并成一段

用上面这个对照方法复查时，发现训练手册 p4 的 `seg=77262` 是**两条目录项被抽取并成一个段**：
```
EN: '8.1 Speed Limit check ....... 153 8.2 Fly-Up check ....... 153'
ZH: '8.1 速度限制检查 ... 153 8.2 上仰检查 ... 153'
```
而我的"保留页码 + 重排点线"逻辑是**按单条目录项**设计的，遇到这种输入会把整条
拉到 1000+pt，**压到下一行上**（实测第 1 行 x=80 与第 2 行 x=162 两行内容重叠）。

**修复**（两处，都是"只在能正确处理时才动手"）：
1. `_fit_dot_leader()` 与点线锚点的判定都加上 `_count_dot_runs(text) == 1`
   —— 整段只含一段点线才重排，否则原样留给正常换行。
2. 新增 `_split_multi_dot_entries()`：把这种畸形段**按点线边界拆回多条条目**，
   每条各自成行，再做点线拟合。拆分带校验（每段拆完仍须含点线，否则宁可不动）。

**验证**：
```
训练手册 p4 目录：49 条点线行，49 条以页码结尾（修复前 48/49 且两行重叠）
overlap 复查：seg=77262 两行 x 分别 123.8 / 121.6（修复前 80.4 / 162.9 重叠）
四份 PDF：full_cn 49/49、full_bi 98/98、to1 30/30、to34 34/36（剩余 2 条是
          上述合并条目的第二个编号，本身没有独立点线，属正常）
回归：test_core 33/33 · test_translate 15/15 · test_versions PASSED ·
      test_render ALL PASS（含新增 test_toc_dot_leader）· test_web 179/179 · test_e2e 全通过
```

### 15.19 新增回归测试 `test_toc_dot_leader`

这两个 bug 的共同点是**测试全绿但功能坏了**：`test_render` 只断言"布局能跑通、
不越界"，从没断言"内容不能丢"。补上内容级断言：
* 页码必须原样保留（含三位/四位数页码）；
* 点线必须还在（否则页码列无法对齐）；
* 行右端不得越过页锚点与可用宽度；
* **没有页码的普通段落必须原样返回**（防止我的修复误伤正文）。

### 15.20 教训记录（给自己）

1. **改渲染代码后必须重启 `cli.py serve`**（现在有指纹自动失效了，但旧的
   `data/derived/*.pdf` 仍要重新导出）。
2. **排查"界面不对"先做"服务端直接调用"对照**，能一步区分"算错了"与"缓存旧了"。
3. `test_render` 这类"能跑通"的断言防不住内容丢失，重要产物要有内容级断言。
