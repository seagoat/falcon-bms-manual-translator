# translate/ 开发笔记（task-2 · 翻译引擎）

最后更新：2026-09-17 · owner: `trans-engine`

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `translate/glossary.py` | 167 条 F-16/BMS 术语（内置默认表）→ `data/glossary.json`；`get_pairs()` / `apply()` / `prompt_block(texts=None)` / `mock_translate()` / `lookup_map()` |
| `translate/prompt.py` | `build_messages(batch, glossary_block, seed=None)`；`parse_response(raw, batch_ids)`；`extract_content(resp)`；`estimate_tokens()` |
| `translate/engine.py` | `translate_segments(...)`（CONTRACT §7 签名 + 追加 `dry_run=False`）；`_http_post()`；并发/重试/缓存/增量全流程 |
| `tests/test_translate.py` | 12 个用例，`python tests\test_translate.py` 直接运行（`--live` 追加 1 次真实调用） |
| `data/glossary.json` | 由 `glossary.ensure_file()` 生成，Web `PUT /api/glossary` 可直接覆盖 |

`python tests\test_translate.py` → **退出码 0，12 passed / 0 failed**（全离线，0 次网络调用）。

## 2. 关键设计决策（与 CONTRACT 的偏差都在这里）

1. **签名**：`translate_segments` 严格按 §7，另外追加 keyword-only 参数 `dry_run=False`
   （task-2 明确要求；不给 `dry_run` 就发请求会让 Lead 无法先报价）。其余参数/返回字段不变。
2. **状态字面量用小写**：`core.models.TranslationStatus` 的值是 `machine/carried/seeded/failed`
   （CONTRACT §2）。引擎从 `TranslationStatus` 取值，不硬编码大写，避免将来枚举变更时漂移。
3. **返回字段语义**（`{"translated","carried","cached","failed",...}`）：
   * `cached` = 同版本该段**已有译文**且 `force=False` → 跳过（§7 步骤 1，0 次调用）；
   * `carried` = ① `role!=body` / 纯符号 / 纯数字 / 长度<2（文本=原文）
     ② `translation_cache` 命中（文本=缓存里的旧译文）；
   * `translated` = 本次真正调用后端产出的段数（= `machine` + `seeded`）；
   * 额外字段：`seeded` `machine` `api_calls` `batches` `provider` `model` `dry_run` `cost` `errors`。
4. **`force=True` 同时绕过缓存**（步骤 1 + 步骤 2），但仍使用 `segment_links` 做 seed。
   只绕步骤 1 的话「强制重译」会变成全部 CARRIED，等于没重译（测试 t03 验证）。
5. **缓存键**：用段行里的 `fingerprint`（= `blake2b(normalize(canonical).casefold())`），
   与 §7「translation_cache(fingerprint)」一致；写入走 `store.put_cached_translation`（`ON CONFLICT`
   保留 `hits`）。读用批量 `IN (...)`（每 300 个一批），避免 3000 段逐段查询。
6. **`reuse_of`**：种子路径写旧 `segment_id`（可在 UI 里追溯来源）；缓存 CARRIED 只有在
   `segment_links` 里旧段指纹完全一致时才写旧 `segment_id`，否则 `None`（不编造）。
7. **失败语义**：批次内所有段重试 3 次（退避 1/2/4s，可被 `cfg["retry_backoff"]` 覆盖，测试用 `[0,0,0]`）；
   最终失败写 `status=failed` 且 **`text` 留空**——若把原文写进 text，下次运行的步骤 1 会把它
   误判成「已完成」而永久跳过。单项失败不抛异常、不中断其它批次。
8. **不可重试错误**：`_http_post` 对 4xx（除 408/429）抛 `HttpFatalError`，重试循环直接 break
   （401/403 重试 3 次纯属浪费）。
9. **术语强制替换**：`glossary.apply()` 只替换「译文里残留的英文术语」；若匹配处附近已出现对应中文
   （即已是「中文（缩写）」形式）则保留英文不替换，因此幂等，也不会把
   「应急动力装置（EPU）」改坏（实测通过）。默认对 deepseek 输出启用，`cfg["glossary_enforce"]=False` 可关。
10. **思维链默认关闭**（Lead 实测后合入）：`_build_payload` 发 `reasoning_effort:"none"` +
    `thinking:{"type":"disabled"}`；`_thinking_enabled(cfg)` 是**唯一**判定入口，
    `_build_payload` 与 `dry_run` 估算共用，避免「实际不发推理、估算却按推理计费」的脱节。
    `cfg["thinking_mode"]="enabled"` 可恢复高质量模式（此时不发 temperature，改发 `reasoning_effort`）。
11. **价格**：`PRICES` 取官方 off-peak 档 flash `in 0.15 / out 0.60`、pro `in 0.66 / out 1.98`
    （USD / 1M token）。`dry_run` 的 `reasoning_tokens_per_request` 随思维链开关取值
    （disabled→0，enabled→6720 实测值），并写进 `result["cost"]["assumptions"]`。

## 3. 真实 API 验证（deepseek-flash · 思维链开关实测）

命令：`python tests\test_translate.py --live`（单段打通）/ `--live-ab`（开关对比，均默认不跑）。
仍写临时 data 目录，key 从仓库 `config/local.json` 现读，不污染项目 `data/app.db`。

**单段打通（思维链关闭后重测）**
```
URL            https://api.deepseek.com/chat/completions
payload        {"model":"deepseek-flash","response_format":{"type":"json_object"},
                "max_tokens":2137,"stream":false,"reasoning_effort":"none",
                "thinking":{"type":"disabled"},"temperature":0.2}
耗时 0.78s     api_calls=1  translated=1  failed=0
usage          prompt=812  completion=28   reasoning_tokens=None   cost≈$0.000139
src  If the EPU RUN light is off, refer to the EP checklists.
zh   若应急动力装置运行灯熄灭，则参阅 EP 检查单。   status=machine
```
对比关闭前（同一段）：4.38s / completion=738（其中 reasoning=701）—— 关闭思维链后
延迟 5.6x↓、completion 26x↓。关闭前那次输出带「应急动力装置（EPU）」，关闭后这次没有；
单段样本不足以说明退化，故补做了下面的成批对比。

**思维链开关 A/B（同一批 3 段真实手册风格文本，各 1 次调用）**
```
[disabled] 1.34s  prompt=943  completion=123  缩写保留 6/6
  ▪ 任一警戒灯亮起后不久，主警戒灯（MASTER CAUTION）即点亮（敌我识别（IFF）除外）。
  ▪ 若应急动力装置（EPU）RUN 灯熄灭，则表明系统 B 液压系统发生单一故障（参见 EP 检查单中的系统 B 液压故障）。
  ▪ 滑行前设置航路点（STPT）并检查平视显示器（HUD）符号。
[enabled]  3.63s  prompt=968  completion=678  缩写保留 6/6
  ▪ 主警戒灯（MASTER CAUTION）会在警戒灯面板（CAUTION PANEL）上任一独立指示灯亮起后不久亮起，但敌我识别（IFF）除外。
  ▪ 若应急动力装置（EPU）RUN 指示灯熄灭，则说明 B 系统液压发生单一故障（参见应急程序检查单中的 B 系统液压故障）。
  ▪ 滑行前设置航路点（STPT）并检查平视显示器（HUD）符号。
```
结论：**缩写保留两种模式都是 6/6**，`disabled` 已满足提示词要求，与 Lead 的 A/B 一致；
`enabled` 略更啰嗦（补出 CAUTION PANEL 全称），但慢 2.7x、completion 5.5x。默认 `disabled` 合理。

> fixture 提示：早期版本用 base-14 Helvetica 写 `▪`/`◆`，pymupdf 会把它替换成 `?`（源文本里就是
> `? Set the STPT...`），一度被误判成「模型吃掉项目符号」。现已改为 `▪` 用 calibri.ttf、
> `◆` 用 Deng.ttf（各自唯一有字形的字体），并在 t03 里断言 fixture 里真的存在 `▪`/`◆`。

## 4. 测试覆盖（tests/test_translate.py）

| 用例 | 内容 |
|---|---|
| t01 | 术语表 ≥60 条、字段完整、`apply` 幂等与「中文（缩写）」、`prompt_block` 只列批内术语、mock 可复现 |
| t02 | 提示词含术语/保留要求/JSON 格式；解析容错（```json 围栏、前后废话、裸 JSON、顶层 list、`{id:text}` 映射、多出 id 忽略、缺 id/空译文/非 JSON → ValueError） |
| t03 | 合成 PDF→抽取→入库→mock 全流程；状态分布；二次运行 **0 次后端调用**；`force=True` 重译 |
| t04 | 跨版本增量：相同段走 `translation_cache`（carried，译文与 v1 完全一致）、小改段走 `segment_links`（seeded，`ratio=0.93`），只发 1 个批次 |
| t05 | deepseek 打桩：请求体（model/response_format/max_tokens≥2048/system 内容/JSON 段 id）、URL、Bearer key、timeout、usage 读取 |
| t06 | 前 2 次抛错 → 第 3 次成功：重试路径 + `api_calls=3` |
| t07 | 模型缺 id：重试 3 次后标 `failed`、`text=""`、`errors` 记录、不抛异常 |
| t08 | `HttpFatalError`（401）不重试，1 次即失败 |
| t09 | `dry_run=True`：0 次 `_http_post`、token/费用估算 >0、**不落库** |
| t10 | 本地 HTTP server 验证 `_http_post` 真实行为（Bearer/Content-Type/UTF-8 body/响应解析）；沙箱禁止监听时 skip |
| t11 | `_is_trivial`、分批（12 段 / 1800 字符）、费用函数、`_db_retry` |
| t12 | 8 页 65 段、6 段/批、`concurrency=8`：**无 database is locked**、无批次失败、`job` 进度推进到 total |
| t13 | `apply` 缺陷回归：文件名/路径/版本号/型号逐字符保留；幂等；中文间不注入空格；同段重复只展开一次 |
| t14 | Lead 认可的 6 条真实译文样本经 `apply` **零改动**（不得修坏已正确的译文） |
| t15 | 自愈：坏译文（标识符被中文截断）与坏缓存被识别并重译，`repaired` 计数、缓存同步覆盖；干净译文仍跳过 |
| live | 1 次真实 deepseek-flash 单段调用（`--live`，见 §3） |
| live-ab | 2 次真实调用：同批 3 段分别 `thinking=disabled/enabled`，比缩写保留与 token（`--live-ab`） |
| live-p4 | 1 次真实调用：第 4 页 TOC 行，证明文件名不被替换（`--live-p4`，见 §7） |

## 5. 与集成方的接口核对（只读检查，未改他人文件）

* `cli.py:281` `cmd_translate`：`translate_segments(conn, doc_id, vid, segment_ids=…, page_from=…,
  page_to=…, provider=…, model=… or None, concurrency=…, force=…, job_id=jid, dry_run=…)`
  —— 参数名与语义全部匹配；读取的键 `translated/cached/carried/failed/machine/seeded/batches/chars/
  tokens/cost/errors` 全部存在于返回值。
* `cli.py:657` `cost-estimate`：`dry_run=True`，不产生网络调用、不写库 —— 符合 t09。
* `web/server.py:806` 后台任务线程内持自己的连接调用，未传 `concurrency`（走 cfg 默认值）；
  引擎只在调用线程使用传入连接，批次线程各自 `core.db.connect()` 新建并 close。
* `concurrency=None` 视为「未指定」→ 回落到 `cfg["concurrency"]`（默认 6）。

## 6. 风险与已知限制

* **t10 依赖本地端口**：Windows 沙箱若禁止 listen/connect 会打印 `[skip]` 而不是失败（当前环境可跑通）。
* **dry_run 估算**：prompt token 按「每批都按全价」计算（未扣 prompt 缓存折扣），故偏保守；
  completion = 源字符数 × 0.6 + 每批推理开销，推理开销默认 0（思维链关闭时实测 reasoning=0），
  `thinking_mode=enabled` 时按 flash 实测 6720/请求估，`cfg["reasoning_tokens_per_request"]` 可覆盖。
  实测 3 段/批时 completion=123 vs 估算 3×~150×0.6+0 ≈ 270，同一量级（估算偏保守）。
* **思维链选择**：默认关闭的依据是 Lead 的 4 批 × 36 段 A/B + 本次 3 段 A/B（缩写保留 6/6 vs 6/6）。
  个别短句（单段样本）关闭后可能省略「（缩写）」括注，属抽样波动；若最终验收发现括注缺失率高，
  可在 cfg 里整体切回 `thinking_mode="enabled"`（成本约 ×5 completion、延迟 ×2.7）。
* **同一 fingerprint 不同大小写**：`fingerprint()` casefold，理论上 `STPT` 与 `stpt` 会共享缓存条目，
  对手册正文影响可忽略（也符合 core 的跨版本匹配语义）。
* **术语表 `apply()`** 可能对极少数多义短语（如 `bus`→汇流条）产生误替换；术语表任何条目都可通过
  `data/glossary.json` 或 `PUT /api/glossary` 覆盖/删除。代码样 token 现已屏蔽（见 §7），不会再破坏文件名。
* `translate/__init__.py` 保持空文件；本模块只依赖标准库 + `config` + `core.{db,store,fingerprint,models}`。

## 7. 缺陷修复记录（第 2 轮：`apply` 破坏文件名 / 不幂等 / 注入空格）

**缺陷 1（严重）**：`(TR_BMS_05_ILS_Landing)` → `(TR_BMS_05_仪表着陆系统（ILS）_着陆)`。
第 4 页真实译文实测复现；另有 5 段同类中招（见下方 blast radius）。
**缺陷 2**：`apply` 不幂等，且把术语两侧空白带进中文（`检查 OBOGS 与 EPU。` → `检查 机载制氧系统（OBOGS） 与 应急动力装置（EPU）。`）。

**修法**（`translate/glossary.py`，两道防线 + 一条后处理）：
1. **屏蔽代码样 token**：`[A-Za-z0-9_.\/\-]{2,}` 且含 `_ . \ /`，或形如 `[A-Z]{2,}-?[0-9]+`
   （`TR_BMS_05_ILS_Landing`、`callsign.ini`、`BMS 4.38.1`、`(/Docs/02 …\01 F-16)`）→ 占位符换出、
   替换后原样还原；`mock_translate` 同样屏蔽。
2. **词边界**把 `_ . \ / -` 视作词内字符：`(?<![A-Za-z0-9_.\\/\-])…(?![A-Za-z0-9_.\\/\-])`。
3. **合并正则单遍替换**（167 分支按 en 长度降序 + `(?i:)` 分支），替换后 `_collapse_cjk_spaces()`
   删除「中文 中文」之间的半角空格（保留 `10 psi`、`▪ 项目`、`任务 5` 这类跨语种空格）。
   合并正则按术语表对象身份缓存，避免每段重复编译。
4. **首次展开、后续缩写**：同一段内同一术语只在首次出现时写成「中文（缩写）」，
   之后出现（含模型自己写好的括注）保留英文原样 → **幂等** `apply(apply(x)) == apply(x)`。
   提示词同步补了这条规则 + 「文件名/标识符逐字符保留、即使含 ILS/HUD 也不得替换」。
5. **自愈（engine）**：已有译文或缓存命中时若发现「ASCII 标识符被中文截断」特征
   （`[A-Za-z0-9]_[中文]` / `[中文]_[A-Za-z0-9]`），一律不信任并重译，新译文覆盖缓存；
   返回值新增 `repaired` 计数。**无需 `--force` 全量重跑**即可修掉历史坏数据。

**真实调用证据**（`python tests\test_translate.py --live-p4`，deepseek-flash，1 次调用 0.79s，tokens 1008/67）：
```
EN: MISSION 5: ILS LANDING AT NIGHT (TR_BMS_05_ILS_Landing) .......... 130
ZH: 任务 5：夜间仪表着陆系统着陆（TR_BMS_05_ILS_Landing） .......... 130        <- 文件名完整
EN: MISSION 1: GROUND OPERATIONS (TR_BMS_01_GroundOPS) .......... 12
ZH: 任务 1：地面操作（TR_BMS_01_GroundOPS） .......... 12                      <- 文件名完整
```
**真实库 blast radius（只读统计 `data/app.db`）**：549 条译文中 **6 条**受缺陷 1 影响，
`translation_cache` 对应 6 条；把库**复制**到临时目录后用 mock 跑自愈：
`repaired=6 translated=6 failed=0 still_broken=0`，三条修复后文本（文件名已完整）：
```
〔MISSION〕 5: 仪表着陆系统 着陆 〔AT〕 〔NIGHT〕 (TR_BMS_05_ILS_Landing) ..................
〔MISSION〕 6: 仪表着陆系统 着陆 〔IN〕 〔BAD〕 〔WEATHER〕 (TR_BMS_06_ILS_Weather) ........
〔MISSION〕 28: 〔SEAD-EW〕 (TR_BMS_28_SEAD-EW) ............................................
```
（原库未被修改；Lead 只需对对应版本重跑一次 translate 即可自愈这 6 段。）
* `translate/__init__.py` 保持空文件；本模块只依赖标准库 + `config` + `core.{db,store,fingerprint,models}`。
