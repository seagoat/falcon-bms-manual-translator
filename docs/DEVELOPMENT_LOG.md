# manual_trans_trace —— 飞行手册中文翻译与版本追踪

把英文飞行手册（Falcon BMS Training Manual，401 页正文 + 图纸）**元素级**重建为中文版，
并在手册出新版时**只重译改动过的段落**，同时保留一份可交互的 v1/v2 对照浏览器。

- **不栅格化**：逐页重放图片 + 矢量 + 文本层，导出的中文 PDF 仍是可选词、可搜索的 PDF。
- **表格不塌陷**：同一基线上的多个单元格按 x 间隙切成独立 `table_cell` 段
  （页眉跨行单元格按“同列 + 相邻基线 + 风格一致 + 网格不完整”合并），列结构与 bbox 都保留。
- **版本可追踪**：段级 fingerprint 锚点 + 顺序 diff，识别 `修改/新增/删除/移动/重排/拆分/合并`。
- **增量翻译**：原文没变 → 直接沿用旧译文；只改几个词 → 用旧译文当种子让模型修订。
- **中文界面**：CLI 与 Web UI 全中文；Web 侧栏对照支持双向联动高亮。

实测数据（本仓库 `origin/BMS-Training-Manual.pdf`，v4.38.1）：

| 指标 | 实测值 |
|---|---|
| 页数 / 图片 placement | 401 页 / 1518 张 |
| 抽取段数 | **5581** 段（v1）＝ body 4781（paragraph / list_item / table_cell / heading / caption）+ 页眉 400 + 页码 400 |
| 全量入库耗时 | **17–20 s**（单机、无 GPU） |
| v1→v2 diff 耗时 | 0.02–0.90 s（`/api/diff` 冷 80 ms / 热 29 ms / 6 KB） |
| mock 后端翻译 30 页 | 512 段 / 0.7 s / 0 失败（carried 63） |
| 单批耗时（Lead A/B 实测） | **flash 关思维链 3.0 s**；flash 默认 10.4 s；v4-pro 默认 65.9 s |

### 交付成果（Lead 端到端跑通并验收）

| 交付物 | 路径 | 实测 |
|---|---|---|
| 对照浏览器 UI | `python cli.py serve` → <http://127.0.0.1:8777> | 终局自动校验 **14/14 通过**；双向联动高亮、双栏滚动同步、变更徽章、详情面板全部可用 |
| 版式还原中文 PDF | `data/derived/full_cn.pdf` | **401 页 / 37.2 MB / 83 s**；`overflow=13` `shrunk=76`；原有英文正文已擦除（p21 仅剩 3 个 ASCII 词） |
| 双语对照 PDF | `data/derived/full_bi.pdf` | **401 页 / 38.5 MB / 139 s**；页宽 **2.000 × A4**（左英右中并排） |
| 全书中文译文 | `data/app.db` | **5580 / 5580 段，0 缺失 0 失败**（machine 3996 / carried 778 / seeded 6） |

**全书翻译实际花费 ≈ $0.10（¥0.7）**，约 500 批 / 3 分钟 API 时间（`deepseek-flash` + 关闭思维链）。
dry-run 预估 $0.33 偏保守，实测更低：关闭思维链后每批输出仅约 370 token，而预估按 900 计。

译文质量抽样（真实产出）：

| 原文 | 译文 |
|---|---|
| `The activation of this warning light signals that the canopy hooks or locks are not secured, or there has been a loss of cabin pressure.` | `该警告灯亮起表示座舱盖挂钩或锁未锁定，或座舱失压。` |
| `- If the EPU RUN light is off, there is a single system B hydraulic failure (refer to System B Hydraulic failure in the EP checklists).` | `▪ 若 应急动力装置（EPU） RUN 灯熄灭，则表明 B 系统单液压系统故障（参见 EP 检查单中的 B 系统液压故障）。` |
| `FAIR weather, few clouds at 5000 feet, winds 320°/5 knots, temperature 9°C` | `晴好天气，5000 ft 少云，风向 320°/5 kt，温度 9°C` |
| `MISSION 5: ILS LANDING AT NIGHT (TR_BMS_05_ILS_Landing)` | `任务 5：夜间仪表着陆系统着陆（TR_BMS_05_ILS_Landing）` ← 文件名逐字符保留 |

版本追踪实测（v1 → v2，`cli.py simulate-update` 构造的二版）：

```
counts: unchanged=4775  modified=3  added=1  removed=2  moved=1
微改识别: aircraft -> airframe (ratio 0.9873) 判 MODIFIED，行内 diff = "left click on the air[-craft-][+frame+] symbol"
改数字:   Level out at [-5-][+6,+]000 feet.
跨页移动: ratio=1.000  old_page=9 -> new_page=10
```

---

## 1. 架构

```
                          origin/*.pdf（只读原始数据）
                                   │
                    ┌──────────────▼───────────────┐
                    │  pipeline.extract_segments   │  块 → 视觉行 → 风格 run → 段落
                    │  （pipeline.py）             │  role: header/footer/pagenum/body
                    └──────────────┬───────────────┘   kind: heading/paragraph/list_item/
                                   │                        table_cell/caption
                    ┌──────────────▼───────────────┐
                    │  core.fingerprint            │  dehyphenate（行尾软连字符）
                    │  normalize / fingerprint     │  → canonical_text / fingerprint
                    └──────────────┬───────────────┘
                                   │
   ┌───────────────────────────────▼────────────────────────────────┐
   │  core.store ←→ SQLite data/app.db                              │
   │  documents │ document_versions │ segments │ translations │     │
   │  translation_cache │ segment_links │ version_diffs │           │
   │  render_snapshots │ jobs │ glossary                            │
   └───┬───────────────────┬────────────────────┬───────────────────┘
       │                   │                    │
       │                   │                    │
┌──────▼──────┐   ┌────────▼────────┐   ┌───────▼────────┐
│ translate/  │   │ versions/       │   │ render/        │
│ glossary    │   │ differ.py       │   │ pagebuild.py   │
│ prompt      │   │ 锚点+顺序diff    │   │ 元素级重建      │
│ engine      │   │ 模糊配对→MODIFIED│   │ pdfout.py      │
│ deepseek/mock│  │ MOVED/REORDERED │   │ 中文/双语 PDF   │
└──────┬──────┘   └────────┬────────┘   └───────┬────────┘
       │  DeepSeek API     │                    │
       │  （urllib）        │                    │
       └───────────────────┴────────┬───────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │  web/server.py（FastAPI，REST API）       │
              │  web/static/（原生 ES module，无构建步骤） │
              │  双栏对照 / 滚动同步 / 联动高亮 / canvas   │
              └─────────────────────┬─────────────────────┘
                                    │
                        cli.py（10 个子命令）│ start_web.ps1
                                    │
                             用户浏览器 1440×900
```

数据流（一次典型的「原书更新」）：

```
新 PDF ──ingest──▶ segments(v2) ──differ──▶ changes ──▶ 只翻译 modified/added
                                     │                     （unchanged 用旧译文 CARRIED）
                                     ▼
                              对照浏览器 v1⇄v2 ──▶ 中文 PDF / 双语 PDF
```

---

## 2. 快速开始

```powershell
# 0) 环境：Python 3.11 + 已装依赖（pymupdf / fastapi / uvicorn / Pillow / numpy / playwright）
python -c "import pymupdf, fastapi, uvicorn; print(pymupdf.__doc__)"

# 1) 抽取入库（401 页约 15 秒）
python cli.py ingest --pdf origin/BMS-Training-Manual.pdf

# 2) 离线翻译前 30 页（mock 后端，不联网、不需要 API key）
python cli.py translate --pages 1-30 --provider mock

# 3) 生成演示用「新版」PDF 并自动比对（删除 2 / 修改 3 / 插入 1 / 跨页移动 1）
python cli.py simulate-update

# 4) 打开对照浏览器
.\start_web.ps1                 # 等价：python cli.py serve
```

浏览器打开 <http://127.0.0.1:8777/>，左侧原文、右侧译文，点击任一段两侧联动高亮。

---

## 3. CLI 用法

`python cli.py <命令> [选项]`　（`python cli.py --help` 查看全部；全局 `--db PATH` 可切换数据库）

| 命令 | 作用 | 常用选项 |
|---|---|---|
| `ingest` | 抽取 PDF → 登记为新版本 | `--pdf P` `--slug` `--title` `--note` `--label v2` `--pages a-b` `--force` `--rebuild --yes-i-understand` `--no-diff` |
| `translate` | 翻译（在线 DeepSeek / 离线 mock） | `--doc` `--version` `--pages a-b` `--page-to N` `--limit N` `--provider deepseek｜mock` `--model` `--concurrency` `--force` `--dry-run` |
| `diff` | 比较两版段落差异 | `--from 1 --to 2` `--doc` `--limit N` `--markdown` `-v` |
| `export` | 导出中文/双语 PDF | `--kind cn｜bilingual` `--version` `--pages a-b` `--out PATH` `--dpi N` |
| `render-page` | 单页渲染成图/PDF | `--version` `--page P` `--kind source｜cn｜bilingual` `--out PATH` `--dpi N` |
| `serve` | 起 Web 服务 | `--host` `--port` `--no-open` `--reload` |
| `list` | 列出文档与当前版本 | – |
| `status` | 版本/段数/翻译进度/变更统计 | `--doc` |
| `simulate-update` | 生成演示用 v2 PDF 并自动 diff | `--pdf` `--out` `--doc` `--label` `--page-from` `--page-to` |
| `cost-estimate` | 估算 token 与费用（不调用 API） | `--version` `--pages a-b` `--model` |
| `restore-translations` | 重建分段后回填译文（形状校验） | `--version` `--backup JSON` `--from-version` `--min-en-chars` `--clean` |

**`ingest` 三个开关的语义（重要）**

| 命令 | 行为 |
|---|---|
| `ingest --pdf P` | 幂等：同一文件已入库且已抽段 → 直接复用，不重复插段、不影响译文 |
| `ingest --pdf P --force` | **重建分段但保住译文**：先备份译文到 `data/derived/translations_backup_<ts>.json`，清段重抽，再按 `(页, fingerprint)` 三级匹配 + 形状校验回填 |
| `ingest --pdf P --rebuild --yes-i-understand` | 显式丢弃该版本全部译文（必须两个开关同时给，否则退出码 2） |

失败时**退出码非 0**（`0` 成功、`1` 业务失败、`2` 参数/文件缺失、`130` 中断），可直接串进 CI：

```powershell
python cli.py ingest --pdf origin/BMS-Training-Manual.pdf; if ($LASTEXITCODE -ne 0) { throw "ingest 失败" }
```

典型输出：

```
✅ 入库完成
  文档   : bms-training-manual (doc_id=1)  BMS - Training Manual
  版本   : v1 (version_id=1)  release=4.38.1  date=2025-10-18
  页数   : 401    段数: 4807    字符: 754679    图片: 1518
  role   : body=4007  header=400  pagenum=400
  kind   : paragraph=2278  list_item=1375  header=400  page_num=400  heading=314  table_cell=34  caption=6
```

---

## 4. Web UI 用法

```powershell
.\start_web.ps1            # 起服务并自动打开浏览器（默认 127.0.0.1:8777）
.\start_web.ps1 -Port 8901 -NoOpen -Reload
```

界面（1440×900 无横向滚动）：

| 区域 | 说明 |
|---|---|
| 顶栏 | 文档 / 版本（v1、v2…）/ 显示模式（对照·仅译文·仅原文）/ 页码跳转 / 缩放 / 「只显示变更」/ 翻译按钮+进度 / 导出（中文·双语） |
| 最左 | 可折叠目录（由 heading 层级生成，点击跳转，当前章节高亮） |
| 左栏 | 原文页面图 + 热区层；变更段左侧 3px 色条（新增绿 `#22c55e`、删除红 `#ef4444`、修改橙 `#f59e0b`、移动紫 `#a855f7`） |
| 右栏 | 中文页面图 + canvas 逐行绘制译文 + 热区层；未翻译显示「⏳ 待翻译」占位 |
| 右下浮动面板 | 选中段的原文/译文/状态/版本历史（`change_kind`、`ratio`、`revision`），可编辑译文并点「保存复核」 |

交互：

* **滚动同步**：以页为锚 + 段比例插值，滚动任一侧另一侧跟到同一段；
* **点击/悬浮联动**：点任一侧文字块 → 两侧同段高亮（`.sel` / `.peer`）并对侧 `scrollIntoView({block:'center'})`；
* **键盘**：`↑/↓` 段间移动、`←/→` 翻页、`d` 切换「只显示变更」；
* **深链**：`?doc=1&vid=2&page=15&seg=123&mode=compare&changes=1`，刷新后定位一致。

常用 REST API（完整表见 `CONTRACT.md` §5）：

```powershell
curl http://127.0.0.1:8777/api/health
curl http://127.0.0.1:8777/api/documents
curl "http://127.0.0.1:8777/api/documents/1/diff?from=1&to=2"
curl "http://127.0.0.1:8777/api/documents/1/versions/1/pages/21/markers"
```

---

## 5. 标准工作流：**原文更新了怎么办**

厂商/社区发了新版手册（例如 4.38.1 → 4.39.0），按下面 6 步走，**只重译改动过的段落**：

```powershell
# ① 把新 PDF 放进 origin/（例如 origin/BMS-Training-Manual_439.pdf）

# ② 登记为新版本（自动继承同一文档 slug；自动解析 release 号与日期）
python cli.py ingest --pdf origin/BMS-Training-Manual_439.pdf --note "官方 4.39.0"

#    入库后会自动跑一次 diff 并打印计数：
#    未变=4650  修改=96  新增=41  删除=32  移动=8  重排=3  拆分=2  合并=1

# ③ 先看差异（不花钱），确认变更范围
python cli.py diff --from 1 --to 2 --limit 40 -v
python cli.py diff --from 1 --to 2 --markdown > data/derived/changes_1_2.md

# ④ 估算成本（dry-run，只算 token 不调用 API）
python cli.py cost-estimate --version 2
#    待译段数 : 3452   批次: 444   prompt: 594,944 tokens  completion: 399,671 tokens
#    model=deepseek-flash（默认，关思维链）  估算费用 : $0.33 ≈ ¥2.34
#    想用高质量模型：python cli.py cost-estimate --version 2 --model deepseek-v4-pro

# ⑤ 翻译：未变的段自动沿用旧译文（CARRIED），小改动用旧译文当种子（SEEDED）
python cli.py translate --version 2 --provider deepseek
#    provider=deepseek（config/local.json 的 key，默认 model=deepseek-flash + 关思维链）
#    断网/无 key 时用 --provider mock 走离线占位译文
#    高价值段落想要更高质量：MT_MODEL=deepseek-v4-pro 并设 config 的 thinking_mode=enabled

# ⑥ 导出与复核
python cli.py export --kind cn   --version 2              # 中文 PDF
python cli.py export --kind bilingual --version 2         # 中英对照 PDF（页宽 ×2）
python cli.py serve                                      # 浏览器里逐段复核 v1⇄v2
```

要点：

* **永远不要改 `origin/*.pdf`**；`ingest` 只读它，输出写 `data/`。
* 每个版本各存一份段表，旧版本译文与快照都保留，可随时回退对照（顶栏版本切换）。
* `translation_cache(fingerprint)` 全局复用：同一句话在全书任何位置出现都只翻译一次。
* 想强制重译某几页：`python cli.py translate --version 2 --pages 30-45 --force`。

### 5.2 重新抽取不会丢译文（产品级保证）

段表重建时 `segments` 行会被删除，`translations` 表随外键 `ON DELETE CASCADE` 一起消失。
因此产品里对此做了三层保护（都在 `pipeline.py`，不依赖任何库外脚本）：

1. **默认拒绝**：`pipeline.ingest_pdf(..., reingest=True)` 在该版本已有译文时直接抛错，
   提示改用 `rebuild_version()` 或显式接受丢失（`allow_translation_loss=True`）。
   `cli.py` 对应 `ingest --rebuild` —— 不带 `--yes-i-understand` 时退出码 2。
2. **`rebuild_version()` 先备份再回填**：导出全部译文到
   `data/derived/translations_backup_<时间戳>.json` → 清段重抽 → 三级匹配回填：
   `(版本, 页, fingerprint)` → `(版本, fingerprint)` 取最近页 → 同页文本包含（仅当长度比合理）。
3. **形状一致性校验**（Lead 指定的红线）：只有
   `len(zh) <= max(3.0 * len(en), 40)` 且 `len(en) >= 8`（正文段）才允许回填；
   不满足者**留空为 missing 等待重新翻译**，绝不允许「整行译文塞进一个单词格子」。
   校验逻辑在 `pipeline._shape_ok()`，`restore_translations()` 统计里单列 `skipped_shape`。

清空/回填也可以单独跑：

```powershell
python cli.py restore-translations --version 2 --backup data/derived/translations_backup_20260917_110249.json
python cli.py restore-translations --version 2 --clean    # 顺带删掉无中文 / mock 〔…〕占位译文
```

不变量（已写进 `tests/test_e2e.py` 锁死）：

* 重建后 `译文条数 == 重建前 − skipped_shape`，且 `lost == 0`（没有静默丢失）；
* `SELECT COUNT(*) FROM translations t JOIN segments s ON s.id=t.segment_id
  WHERE s.kind='table_cell' AND length(t.text) > 3*length(s.text)+40` 恒为 **0**。

### 5.1 演示/回归：`simulate-update`

没有新手册时，可以用 `simulate-update` 现场造一版可验证的「新版」来演示与自测：

```powershell
python cli.py simulate-update
```

它做的事（全部用 pymupdf 直接改文本层，**不改原始文件**）：

| # | 类型 | 页 | 实现方式 |
|---|---|---|---|
| 1 | 删除段落 | p7 | `add_redact_annot` + `apply_redactions`（不补写） |
| 2 | 删除段落 | p15 | 同上 |
| 3 | 整段跨页移动 | p9 → p10 | 源页 redact + 目标页空白带 `insert_textbox` 原文 |
| 4 | 只改 2 个词 | p10 | `aircraft` → `airframe` |
| 5 | 改数字/单位 | p10 | `5000 feet` → `6,000 feet` |
| 6 | 整句重写 | p20 | 替换段内最后一句 |
| 7 | 全新插入段落 | p40 | 在正文空白带插入一段 NOTE |

输出：`origin/BMS-Training-Manual_v2_demo.pdf`（401 页，35.8 MB）
+ 改动清单 `data/derived/simulate_update_manifest.json`
+ 自动 ingest & diff，逐项打印 ✅/❌：

```
=== manifest ↔ 实际 diff 比对 ===
变更类型       manifest 期望  实际 diff  结果
------------  -----------  -------  --
modified      3            3        ✅
added         1            1        ✅
removed       2            2        ✅
moved         1            1        ✅
unchanged     -            4001
✅ 全部改动都被 diff 正确识别（7 处施加改动 ↔ 计数完全一致）
```

---

## 6. 术语表维护

* 正式术语表：`data/glossary.json`，格式 `[{"en":"MASTER CAUTION","zh":"主警戒","case_sensitive":false,"note":"..."}]`。
  首次运行自动写入内置默认表（160+ 条 F-16/BMS 术语）。
* 种子快照：`data/glossary_seed.json`，由 `pipeline.build_glossary_seed()` 生成，用于给其他工具/评审比对。
* 生效方式（`translate/glossary.py`）：
  1. 提示词里注入术语清单（要求模型优先采用）；
  2. 译文落库前**强制替换**未遵守的术语（`apply()`，大小写不敏感但保留 `case_sensitive` 例外）；
  3. `mock` 后端逐词查表，未命中保留原文并加 `〔〕`，保证断网也能跑通全流程。
* 增删术语：

  ```powershell
  # 直接编辑 data/glossary.json（en/zh/case_sensitive/note 四个键），保存即生效
  python cli.py translate --version 2 --pages 21-21 --force   # 重跑受影响页验证
  ```

  Web UI 顶栏「术语表」入口也提供查看/编辑（`GET/PUT /api/glossary`）。
* 注意：型号/缩写/编号/单位（`F-16`、`OBOGS`、`MFD`、`STPT`、`1000 psi`）在提示词中被要求**原样保留**，
  术语表只负责给出中文注名，不改变正文里的英文代号。

---

## 7. 配置

优先级：环境变量 > `config/local.json` > 内置默认值。

| 键 | 默认 | 说明 | 环境变量 |
|---|---|---|---|
| `deepseek_api_key` | 见 `config/local.json` | DeepSeek API Key | `DEEPSEEK_API_KEY` |
| `base_url` | `https://api.deepseek.com` | API 端点 | `MT_BASE_URL` |
| `model` | `deepseek-flash` | 默认模型（**实测比 v4-pro 快 20 倍、便宜 3 倍**，见下表） | `MT_MODEL` |
| `fallback_model` | `deepseek-flash` | 备用模型 | – |
| `provider` | `deepseek` | `deepseek` / `mock` | `MT_PROVIDER` |
| `thinking_mode` | `disabled` | `disabled` 时显式发 `reasoning_effort:"none"` + `thinking:{type:disabled}`；`enabled` 走高质量慢档 | – |
| `concurrency` | 8 | 并发批次数（每批独立 sqlite 连接） | – |
| `batch_segments` / `batch_chars` | 12 / 1800 | 每批段数 / 字符上限（先到为准） | – |
| `request_timeout` / `max_retries` | 120 / 3 | HTTP 超时 / 指数退避重试 | – |
| `usd_cny` | 7.1 | 费用换算汇率 | – |
| `data_dir` | `data` | 数据库与派生产物目录 | `MT_DATA_DIR` |
| `origin_dir` | `origin` | 原始 PDF 目录（只读） | `MT_ORIGIN_DIR` |
| `render_dpi` | 110 | 页面位图默认 DPI | `MT_RENDER_DPI` |
| `web_host` / `web_port` | `127.0.0.1` / `8777` | Web 服务地址 | `MT_WEB_PORT` |

### 7.1 成本与模型选择（实测依据）

Lead 的 A/B 测试（`spikes/out/ab_reasoning.json`，同样批次同样提示词）：

| 配置 | 平均每批耗时 | completion tokens | 其中 reasoning | 缩写保留率 |
|---|---|---|---|---|
| **flash + 关思维链（默认）** | **3.0 s** | 2285 | 0 | 56/95 |
| flash + 开思维链 | 10.4 s | 8713 | 6720 | 58/95 |
| v4-pro + 开思维链 | 65.9 s | 23217 | 20744 | 58/95 |

质量代理指标（缩写保留率、单位保留、长度比）三者差异可忽略，而速度差 20 倍、费用差约 3 倍，
所以默认 `model=deepseek-flash` + `thinking_mode=disabled`。

全书（`--version 1`，3456 个待译段 / 445 批）`cost-estimate` **dry-run 实测**：

```
model=deepseek-flash     prompt 596,139 + completion 400,524 = 996,663 tokens → $0.33 ≈ ¥2.34
model=deepseek-v4-pro    （同 token）→ $1.19 ≈ ¥8.42
```

* 价格取 [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing) 的 off-peak 档
  （flash in $0.15 / out $0.60 每百万 token；pro in $0.66 / out $1.98），可在 `config/local.json` 覆盖。
* 与 Lead 的独立估算（flash ≈ $0.24）差异约 1.4 倍，原因是 dry-run 按 **completion ≈ 0.6 token/字符**
  外推（400k），而 Lead 按 4800 段 × 约 50 token/段外推（≈240k）。中文输出比英文更紧凑，**实际用量可能落在两者之间**；
  以 `cost-estimate` 的 dry-run 为准时请把它当**上界**。
* dry-run 假设「关思维链」。若切 `thinking_mode=enabled`，completion 会膨胀到约 13 倍（见上表 reasoning 列），
  费用和墙钟都要按此重估——高价值段落（术语密集、安全相关）建议单独用 `--pages` 开高质量模式重译。

`config/__init__.py` 另支持 `MT_CONFIG` 指向其他配置文件；`MT_DEBUG=1` 让 CLI 打印完整 traceback。
中文渲染依赖 `C:\Windows\Fonts\Deng.ttf`（等线）、拉丁文用 `calibri.ttf`（由 `core.pdfdoc.resolve_cjk_font()` 解析，缺字体时抛错）。

---

## 8. 目录结构

```
manual_trans_trace/
├─ CONTRACT.md                  接口真相（冻结签名；改动需 Lead 批准）
├─ README.md                    本文件
├─ cli.py                       命令行入口（10 个子命令）
├─ pipeline.py                  抽取/入库/导出编排 + simulate-update 生成器
├─ start_web.ps1                一键启动 Web 服务
├─ config/                      运行时配置（local.json + 读取逻辑）
├─ core/                        models / db / store / fingerprint / pdfdoc
├─ versions/differ.py           跨版本段匹配与变更集
├─ translate/                   glossary / prompt / engine（deepseek + mock）
├─ render/                      pagebuild（元素级重建）/ pdfout（中文·双语 PDF）
├─ web/                         server.py（FastAPI）+ static/（原生 ES module UI）
├─ tests/                       test_core / test_versions / test_translate /
│                               test_render / test_web / test_e2e（真实 401 页）
├─ spikes/                      Lead 的前期可行性验证（rebuild / redraw / probe）
├─ origin/                      原始 PDF（只读）
│   ├─ BMS-Training-Manual.pdf               401 页，37.7 MB
│   ├─ BMS-Training-Manual_v2_demo.pdf       simulate-update 产物
│   ├─ TO 1F-16CMAM-1 BMS.pdf                待接入
│   └─ TO 1F-16CMAM-34-1-1 BMS.pdf           待接入
└─ data/                        运行期数据（不进版本库）
    ├─ app.db                   SQLite（WAL）
    ├─ glossary.json            术语表
    ├─ glossary_seed.json       术语表种子快照
    ├─ pdfs/{slug}/             上传/复制的 PDF
    ├─ cache/                   中间缓存
    └─ derived/
        ├─ simulate_update_manifest.json      v2 改动清单（验收用）
        ├─ {slug}/v{n}/export/                导出 PDF
        ├─ {slug}/v{n}/{cn,bilingual,source}/ 页面位图缓存（webp）
        └─ preview/                           渲染/UI 截图
```

---

## 9. 已知限制

1. **表格抽取**已按「同一基线按 x 间隙切单元格 + 同列跨行合并」处理（全库 2137 个 `table_cell`，
   覆盖 57 页），修复了原先「一行多列被拼成一段、产生 `ANTI-POSI-TION` 之类假词」的缺陷。仍有两点边界：
   * 只有 **1 行**多单元格、且该页没有任何矢量表格线的“单行布局”，会被当作普通文字行（实际出现 0 次）；
   * 两端对齐且词间距被撑到 >8pt 的文字行会被识别为“段末行”（用于切段），
     规则见 `pipeline._is_justified_last_line()`。
2. **目录页**（第 5–8 页）按「`......  12` 目录行」每行一段（有专门规则），
   行内的点线填充字符会保留在原文里（译文版由渲染层覆盖）。
3. **同类多段合并/拆分**：diff 只有在原文确实发生 1:N / N:1 变化时才判 `SPLIT`/`MERGED`；
   个别段落因换行位置变化被切成不同段时，可能显示为 `modified` 而不是 `split`——这是**保守的正确行为**（不虚报结构变更）。
4. **`--pages a-b` 局部入库**仅用于调试：版本 `page_count` 仍是真实页数，但段表不完整，
   不要在这种版本上跑 diff/翻译。`ingest` 已做 sha256 幂等（同一文件重复入库不会重复插段），
   需要重建请用 `--force`（会先清空该版本旧段）。
5. **PDF 文本对象粒度**：PyMuPDF 的 `apply_redactions` 按**块/文本对象**删字，
   `simulate-update` 在删段后会自动校验并补回被连带删除的相邻段落（manifest 里的 `repaired_paragraphs`）。
6. **mock 后端不是真翻译**：未命中术语表的词保留英文并加 `〔〕`，只用于离线跑通全流程与 UI 演示。
7. **渲染溢出**：中文比英文短，但术语长的段落仍可能触发收缩（导出会返回 `shrunk`/`overflow` 计数）。
   实测第 10–12 页导出：`boxes=50, shrunk=19, overflow=3`——`overflow` 表示该段放不下时末尾加了 `…`，
   需在 Web UI 里人工复核这几段。
8. **字体依赖 Windows**：`Deng.ttf` / `calibri.ttf` 是硬依赖，换到 Linux 需替换 `core/pdfdoc` 的字体表。
9. **DeepSeek key 明文**存在 `config/local.json`：仅适合本机使用，不要提交到公共仓库。
10. **并发注意**：重建版本（`--force` 重抽）期间若 Web 服务正在运行，服务端可能缓存到“抽到一半”的
    diff 结果（`version_diffs` 表）。重建后如发现 diff 计数异常，先
    `DELETE FROM version_diffs` 再重跑 `python cli.py diff`。

---

## 10. 后续扩展：接入两份 TO 手册

`origin/` 里还有两份更大的技术手册（`TO 1F-16CMAM-1 BMS.pdf`、`TO 1F-16CMAM-34-1-1 BMS.pdf`），
它们正是训练手册反复引用的「延伸阅读」（`FOREWORD` 第 1、2 条），接入路径已留好：

```powershell
# ① 直接入库（同一套 pipeline，无需改代码）
python cli.py ingest --pdf "origin/TO 1F-16CMAM-1 BMS.pdf" --slug to-1f16cmam-1 --title "TO 1F-16CMAM-1"
python cli.py ingest --pdf "origin/TO 1F-16CMAM-34-1-1 BMS.pdf" --slug to-1f16cmam-34-1-1

# ② 翻译：先估算再分批跑（两份手册体量远大于训练手册）
python cli.py cost-estimate --doc to-1f16cmam-1
python cli.py translate --doc to-1f16cmam-1 --provider deepseek --concurrency 8

# ③ 导出
python cli.py export --doc to-1f16cmam-1 --kind cn
```

需要注意的差异（**Lead 已用只读探针实测**，见下）：

* 这两份 TO 手册页数更多、`table_cell` 与图纸占比更高 → 分段更细，`table_cell` 比例上升；
* 交叉引用（`refer to TO 1F-16CMAM-1, Chapter 2`）在术语表之外，建议在 `data/glossary.json` 里补
  「章节名 → 中文标准译名」条目，保持三本书口径一致；
* 若后续要做**跨文档术语一致性与引用跳转**，可在 `core/store.py` 之上加一层
  「chapter → segment → translation」索引（现有表结构已够用，不需要迁移）。

### 10.1 只读探针实测结果（`python spikes\probe_to_manuals.py` / `spikes\probe_to_extract.py`）

| | TO 1F-16CMAM-1 | TO 1F-16CMAM-34-1-1 |
|---|---|---|
| 页数 / 书签 | **404** / 561 | **669** / 1093 |
| 体积 | 36.2 MB | 29.0 MB |
| 正文字符 | 654,321 | 1,336,553 |
| 图片 placement | 2,863 | 1,268 |
| 页面尺寸 | 612×792（US Letter，**非 A4**） | 612×792 + **5 页横向 792×612** |
| PDF producer | Adobe PDF Library 25.1.51（原训练手册是 MS Word） | 同左 |
| 主字体 | Calibri 12 / TimesNewRomanPSMT 12 | Calibri 10 / ArialMT 10 |

**抽取器在两者上都直接可用，未改任何代码**：
```
TO 1F-16CMAM-1 : 抽 14 页 -> 202 段（14.4/页，0 空页），外推全书约 5829 段 / 26 s
                 kind: list_item 101, paragraph 84, header 13, heading 4
TO 1F-16CMAM-34-1-1: 抽 14 页 -> 218 段（15.6/页，0 空页），外推约 10417 段 / 32 s
                 kind: paragraph 112, list_item 66, table_cell 16, header 13, heading 11
   横向页实测 p656 = 107 段、p657 = 106 段、p658 = 41 段  → 横向页正常分段 ✅
```

**结论**：页面尺寸、producer、字体与训练手册完全不同，但抽取器**没有硬编码 A4 或特定字体**，
US Letter 与横向页均正常。两份手册合计约 **16,200 段 / 约 195 万字符**，
按实测单价（flash in $0.15 / out $0.60 每 1M，关闭思维链）估算翻译成本约 **$0.25–0.35**，
入库约 1 分钟、翻译约 5–8 分钟。

**唯一建议**：TO-34 的 `paragraph` 占比明显偏高（112/218），说明它排得更密；
首次接入时建议对 `pipeline.extract_segments` 的**段内行合并阈值**做一次抽样目视校验
（训练手册那段调过两轮才收敛），再全量跑。

---

## 11. 复制到别的机器使用

### 11.1 一条命令打包

```powershell
pwsh -File makearchive.ps1                    # → dist\manual_trans_trace_portable.zip（约 17 MB）
pwsh -File makearchive.ps1 -WithSources       # 连 origin\ 里的源 PDF 一起打包（约 90 MB）
```

脚本做三件事：① 把 `data/app.db` 里的**绝对路径改写成相对仓库根**；
② 只打包必要文件；③ **清空 `config/local.json` 里的 `deepseek_api_key`**（避免把密钥带给别人）。
产物里带一份 `HOW_TO_USE.md`。

### 11.2 为什么能随便换目录（本轮为此专门改过）

`data/app.db` 原来存的是**绝对路径**（`E:\worksrc\manual_trans_trace\origin\...`），
换机器/换目录后页面图会 404。现在：

* **写入侧**：`pipeline.ingest_pdf` 用 `core/paths.py` 的 `to_rel()` 存**相对路径**（`origin/xxx.pdf`）；
* **读取侧**：`web/server.py` 的 `version_row()` / `doc_row()` 统一调用 `resolve()` 还原成本机绝对路径，
  所以下游 6 处 `v["source_path"]` 用法一行都不用改；
* **老库迁移**：`python cli.py portable` 一次性重写（已在本仓库执行过）。
* 仓库外的路径（例如 PDF 放在 `D:\manuals`）**保持绝对路径不动**，不会被误改。

### 11.3 目标机器上的步骤

```powershell
# 1) 解压到任意目录，例如 D:\manual_trans_trace
# 2) 装依赖（Windows + Python 3.11）
pip install -r requirements.txt

# 3) 自检（会报告依赖/仓库/源 PDF/密钥状态）
python cli.py setup

# 4) 配置翻译密钥（可选；不配也能用离线 mock 与已有译文）
setx DEEPSEEK_API_KEY "sk-你的key"

# 5) 启动
powershell -ExecutionPolicy Bypass -File .\start_web.ps1
```

### 11.4 实测验证（本机模拟换机器）

把「代码 + `data/app.db` + 两份源 PDF」复制到 **`E:\worksrc\_portable_test\mt_copy_elsewhere`**
（不同盘符路径、不同目录名），共 **90 MB / 71 个文件**，然后在副本里直接跑：

```
python cli.py setup
  OK   pymupdf 1.28.2 / fastapi / uvicorn / Pillow / numpy / rapidocr
  源 PDF v1: OK  ...\mt_copy_elsewhere\origin\BMS-Training-Manual.pdf      ← 相对路径解析成功
  源 PDF v2: OK  ...\mt_copy_elsewhere\origin\BMS-Training-Manual_v2_demo.pdf

python cli.py serve --port 8899
  health 200，modules 全部 true
  页面图：source 8/8 distinct、cn 4/4 distinct、第 1 页判定为封面 ✅
  /api/diff：冷 96 ms / 热 32 ms / 6,103 B
  counts = unchanged 4775 / modified 3 / added 1 / removed 2 / moved 1   ← 与原机完全一致
```
**结论：换机器/换目录后功能完全可用。**

### 11.5 要带 / 不要带

| | 内容 | 体积 |
|---|---|---|
| **必须带** | `cli.py` `pipeline.py` `config/` `core/` `render/` `versions/` `translate/` `web/` `tests/` `data/app.db` `data/glossary.json` `requirements.txt` `start_web.ps1` | ~18 MB |
| **建议带** | `origin/*.pdf` 源文件（重新抽取、页面图、PDF 导出都要用） | 75 MB |
| **不要带** | `data/derived/`（281 MB 可再生产物）、`review/`（158 MB 审查证据）、`spikes/`、`__pycache__/`、`data/app.db.bak_*`、`data/derived/_chrome_profile/` | — |

### 11.6 没有源 PDF 时的降级行为

可以正常浏览**已入库的全部译文**、看版本 diff、查热区与变更标记；
但**页面底图与 PDF 导出会失效**（报「源 PDF 不可用」/ 页面区空白）。
把 PDF 放回 `origin/` 再跑一次 `python cli.py setup` 就能确认恢复。

### 11.7 依赖说明

`requirements.txt`：`pymupdf` `fastapi` `uvicorn` `pillow` `numpy` 必需；
`rapidocr-onnxruntime` + `onnxruntime` 可选（图内文字 OCR 标注用，**纯 onnx，不需要 torch**）；
`playwright` 可选（UI 自动化测试）。

---

## 12. 图内文字翻译（图示标签双层标注）

有些内容是**烧在图片里的**（座舱面板标注、流程图节点、9-line 简令表字段），
`get_text()` 抽不到，主文本管线覆盖不了。这一节是给它们补的独立管线。

### 12.1 效果与原则

在图片上叠加**中文标签胶囊 + 橙色细引线**，指向英文原标注。

* **非破坏性**：原图一个像素都不改，标注画在**页面层**；
* **可搜索可复制**：中文是真的 PDF 文本层，能选中、能搜（实测 `主警戒灯`/`减速板`/`迎角`/`地平线` 都可搜到）；
* **中英对照**：英文原标注保留在旁边，便于核对；
* **自动避让**：三阶段布局（直觉方位 → 8 方向逐圈局部搜索 → 兜底外推），
  碰撞回避同时避开**其它中文标签**与**其它英文标注**。

### 12.2 用法

```powershell
python cli.py annotate                  # 自动选页 → 抽取 → 翻译 → 生成标注 PDF
python cli.py annotate --dry-run        # 只抽取与翻译，不出 PDF
python cli.py annotate --pages 11       # 只做座舱面板图那一页
python cli.py annotate --pages 114-117  # HARTS 机动图
python cli.py annotate --rescan         # 重新扫描全库判定哪些页需要标注
python cli.py annotate --force          # 忽略 OCR/翻译缓存，全部重跑
```
产物：`data/derived/annotated_full.pdf`（默认）。

### 12.3 实测数据（401 页全书）

| 项 | 数值 |
|---|---|
| 判定需要标注的页 | **25 / 401 页** |
| 实际产出（有标签可画的页） | **20 页** |
| 标签总数 | **373 条**（100% 成功放置） |
| OCR 耗时 | 约 2.8 s/页（本地 CPU，无网络） |
| 翻译耗时 | 首次 76 s；**二次运行 0.3 s**（命中缓存） |
| 标注 PDF | 19.6 MB |

### 12.4 为什么只有 25 页需要做

一开始按「图片面积占比」粗筛出 124 页，但其中大多数页的"图片"其实是**正文截图**
（表格、检查单），文字本来就可抽取、也已经被主管线翻译了，再标一遍是重复。
改用「非页眉图片占比 > 25% 且现有正文字数 < 400」+「单张图占页 > 45%」判定后，
收敛到 **25 页** —— 都是真正的图（座舱面板、HARTS 机动图、BVR 流程图、9-line 简令表）。

### 12.5 与主管线的去重

OCR 会把落在图片 bbox 内的正文也检出来（实测 p300 有 12 条、p304/305/306 各 2 条）。
`render/ocr_dedup.py` 用两种规则剔除：
* **文字相同**：OCR 文本 ≈ 已有段落的文本（归一化后相等或互相包含）；
* **几何包含**：OCR 框 >75% 落在某条"长文本段"（≥25 字符）的 bbox 内。

另外 `len(label) > 60 或空格数 > 8` 的长句也跳过 —— 那是图注/正文，属于主管线职责。

### 12.6 真实产出样例

```
p11  座舱面板图       38 条：MASTER CAUTION LIGHT→主警戒灯、LEFT EYEBROW LIGHTS→左眉灯、
                            CAUTION PANEL→警戒灯面板、SPEED BRAKE→减速板、EPU FUEL→应急动力装置燃油
p114-117 HARTS 机动图  97 条：HOLD ON AOA LIMITER UNTIL HORN→保持迎角限制器直到告警音
p302/303 BVR 流程图  127 条：Sort Echelon→梯队排序、Commit on Contact→目标接触后开火
p356  9-line 简令表   36 条：CALLSIGN→呼号、AIRCRAFT→机型、CARRIER→母机
```

### 12.7 OCR 的两个坑（已处理）

1. **全大写词粘连**：OCR 把 `ALT GEAR` 读成 `ALTGEAR`、`MASTER CAUTION LIGHT` 读成
   `MASTERCAUTIONLIGHT`，模型会原样返回不翻译。用 80 词航空词表做**最长匹配切分**
   （`AVIONICPOWER → AVIONIC POWER`），切完才正常翻译。
2. **OCR 置信度**：实测多数 ≥0.95；个别误检（如 `FAUDIO 1`）仍会带上，
   但目前不做低置信度过滤，宁可多标不漏标。

### 12.8 已知限制

* 密集面板图（p11）局部仍偏挤；如需更整洁可改用「区域编号 + 页边对照表」变体。
* 纯文字页不做标注（那是主管线的活）。
* 只对 `current version` 的源 PDF 做，不随版本 diff 自动重算 —— 换了新版原文需要重跑 `annotate`。


