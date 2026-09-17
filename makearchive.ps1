# 把当前仓库打包成「可复制到别的机器」的形式
#
#   & .\makearchive.ps1                  # 精简包 dist\manual_trans_trace_portable.zip（约 4 MB）
#   & .\makearchive.ps1 -WithOutputs     # 完整包 dist\manual_trans_trace_full.zip（含源 PDF + 中文/双语 PDF）
#   & .\makearchive.ps1 -WithSources     # 只额外带 origin\ 里的源 PDF
#   & .\makearchive.ps1 -Out D:\x.zip    # 指定输出路径
#
# 说明：**译文（data\app.db）在任何版本里都会带上**，接收方不需要重新翻译。
#       -WithOutputs 额外带上 data\derived\full_cn.pdf / full_bi.pdf 与源 PDF，
#       这样接收方连渲染都不用跑，直接就能看中文版和双语版 PDF。
#
# 打包内容：
#   代码（cli.py pipeline.py config core render versions translate web tests）
#   data\app.db（含已跑好的 5580 条译文）+ data\glossary.json
#   README.md CONTRACT.md requirements.txt start_web.ps1 makearchive.ps1 HOW_TO_USE.md
#
# 不打包：
#   data\derived（281 MB 可再生产物，-WithOutputs 时只挑几个成品 PDF）、
#   review（158 MB 审查证据）、spikes、dist、__pycache__、
#   data\app.db.bak_*、_chrome_profile
#
# ⚠️ 打包时会**清空** config\local.json 里的 deepseek_api_key，
#    避免把密钥带给别人。目标机器上用环境变量 DEEPSEEK_API_KEY 或自己填 local.json。

[CmdletBinding()]
param(
  [string]$Out = "",
  [switch]$WithSources,
  [switch]$IncludeDerived,
  [switch]$WithOutputs
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

if (-not $Out) {
  $name = if ($WithOutputs) { "manual_trans_trace_full.zip" } else { "manual_trans_trace_portable.zip" }
  $Out = Join-Path $Root ("dist\" + $name)
}
$OutDir = Split-Path -Parent $Out
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

Write-Host "=== 1/5 库路径改相对（DB 可移植）===" -ForegroundColor Cyan
& python cli.py portable
if ($LASTEXITCODE -ne 0) { Write-Warning "portable 返回 $LASTEXITCODE（可能有源 PDF 缺失，继续）" }

Write-Host "`n=== 2/5 组装 staging 目录 ===" -ForegroundColor Cyan
$stage = Join-Path $Root "dist\_stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$code = @("cli.py", "pipeline.py", "README.md", "CONTRACT.md", "requirements.txt",
          "start_web.ps1", "makearchive.ps1")
foreach ($f in $code) {
  if (Test-Path $f) { Copy-Item $f $stage -Force }
}
foreach ($d in @("config", "core", "render", "versions", "translate", "web", "tests")) {
  if (Test-Path $d) {
    Copy-Item $d $stage -Recurse -Force
    Get-ChildItem (Join-Path $stage $d) -Recurse -Directory -Filter "__pycache__" |
      Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# data: 只带 app.db 与术语表
New-Item -ItemType Directory -Force -Path (Join-Path $stage "data") | Out-Null
Copy-Item "data\app.db" (Join-Path $stage "data") -Force
foreach ($f in @("glossary.json", "glossary_seed.json")) {
  $p = Join-Path "data" $f
  if (Test-Path $p) { Copy-Item $p (Join-Path $stage "data") -Force }
}
if ($IncludeDerived) {
  Write-Host "  -IncludeDerived：连 data\derived 一起带（体积很大）" -ForegroundColor Yellow
  Copy-Item "data\derived" (Join-Path $stage "data") -Recurse -Force
}

if ($WithOutputs) {
  Write-Host "  -WithOutputs：带上成品 PDF（中文版/双语版）与源 PDF" -ForegroundColor Yellow
  $WithSources = $true
  New-Item -ItemType Directory -Force -Path (Join-Path $stage "data\derived") | Out-Null
  foreach ($f in @("full_cn.pdf", "full_bi.pdf", "annotated_sample.pdf")) {
    $p = Join-Path "data\derived" $f
    if (Test-Path $p) {
      Copy-Item $p (Join-Path $stage "data\derived") -Force
      Write-Host ("    + {0} ({1:N1} MB)" -f $f, ((Get-Item $p).Length / 1MB))
    }
  }
  if (Test-Path "data\derived\ocr") {
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "data\derived\ocr") | Out-Null
    Get-ChildItem "data\derived\ocr" -Filter "*_zh.json" -ErrorAction SilentlyContinue |
      Copy-Item -Destination (Join-Path $stage "data\derived\ocr") -Force
    Write-Host "    + ocr\*_zh.json（图示标注数据，可复现标注样板）"
  }
}

if ($WithSources) {
  Write-Host "  -WithSources：带 origin\ 下的源 PDF" -ForegroundColor Yellow
  Copy-Item "origin" $stage -Recurse -Force
} else {
  Write-Host "  未带源 PDF（目标机器上若不需要重新抽取，可以不拷；" -ForegroundColor DarkGray
  Write-Host "  需要页面图/导出时会用到，缺失会报 404，见 README「复制到别的机器」）" -ForegroundColor DarkGray
}

Write-Host "`n=== 3/5 清理密钥 ===" -ForegroundColor Cyan
$cfg = Join-Path $stage "config\local.json"
if (Test-Path $cfg) {
  $j = Get-Content $cfg -Raw | ConvertFrom-Json
  if ($j.PSObject.Properties.Name -contains "deepseek_api_key") {
    $j.deepseek_api_key = ""
  }
  $j | ConvertTo-Json -Depth 6 | Set-Content $cfg -Encoding UTF8
  Write-Host "  已清空 config\local.json 的 deepseek_api_key" -ForegroundColor Green
}

Write-Host "`n=== 4/5 生成「目标机器怎么用」说明 ===" -ForegroundColor Cyan
$howto = @'
# 在目标机器的操作步骤

## 0. 先看这里：译文**已经在这个包里了**

`data\app.db` 里带着**已经翻译好的 5580 条译文**（以及术语表、版本差异记录）。
**你不需要重新翻译，也不会再花一分钱 API 费用。**

建好环境后直接 `python cli.py setup` + `start_web.ps1`，
就能看到中英对照界面、版本变更标记、以及全部译文。

只有这几种情况才需要重新跑东西：
| 想做的事 | 需要什么 | 花不花钱 |
|---|---|---|
| 浏览译文 / 看版本 diff / 查变更标记 | 只要本包 | **免费** |
| 看 PDF 原始页面底图 | 需要 origin\*.pdf | 免费 |
| 重新导出中文/双语 PDF | 需要 origin\*.pdf | 免费（复用已有译文） |
| 接入新版本的原文 PDF | 需要新版 PDF + API key | 只翻译新增/改动段落，通常几分钱 |
| 全量重译（一般不需要） | API key | 约 $0.10 |

> 提示：即使误跑了 `python cli.py translate`，它也会**优先复用已有译文**。
> 实测在干净副本上重跑：`新译=0  沿用缓存=5580  tokens=0` —— 零调用、零费用。

## 1. 环境
需要 Windows + Python 3.11（3.10/3.12 未验证）。
```
pip install -r requirements.txt
```

## 2. 放到任意目录
把本文件夹放到你想放的位置即可（路径随意，不要求与原机器相同），
例如 D:\manual_trans_trace 。**DB 里存的是相对路径，换目录不用改任何配置。**

## 3. 自检
```
python cli.py setup
```
会检查依赖、仓库文件、源 PDF 是否齐全、以及 DeepSeek key 是否配置。

## 4. 配置翻译密钥（**可选**，只有要翻译新内容时才需要）
不配也能用（浏览已有译文、离线 mock 后端）：
- 方式一（推荐）：设置环境变量
  ```
  setx DEEPSEEK_API_KEY "sk-你的key"
  ```
- 方式二：填 config\local.json 的 deepseek_api_key

## 5. 启动对照浏览器
```
powershell -ExecutionPolicy Bypass -File .\start_web.ps1
```
然后打开脚本打印出来的地址（默认 http://127.0.0.1:8777）。

## 6. 源 PDF
如果要看页面底图、或导出 PDF，需要 origin\ 下这两份文件：
- BMS-Training-Manual.pdf
- BMS-Training-Manual_v2_demo.pdf

缺失的症状：网页页面区空白 / 导出报「源 PDF 不可用」。
把 PDF 放进 origin\ 再跑 `python cli.py setup` 即可确认恢复。

**没有源 PDF 也能用的功能**：浏览已入库的全部译文、版本 diff、热区与变更标记。
'@
Set-Content (Join-Path $stage "HOW_TO_USE.md") $howto -Encoding UTF8

Write-Host "`n=== 5/5 压缩 ===" -ForegroundColor Cyan
if (Test-Path $Out) { Remove-Item $Out -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $Out -CompressionLevel Optimal
Remove-Item -Recurse -Force $stage

$mb = [math]::Round((Get-Item $Out).Length / 1MB, 1)
Write-Host "`n完成: $Out  ($mb MB)" -ForegroundColor Green
Write-Host "内容清单:" -ForegroundColor Green
Write-Host "  HOW_TO_USE.md  README.md  CONTRACT.md  requirements.txt  start_web.ps1"
Write-Host "  cli.py  pipeline.py  config\ core\ render\ versions\ translate\ web\ tests\"
Write-Host "  data\app.db（含 5580 条译文）  data\glossary.json"
if ($WithSources) { Write-Host "  origin\*.pdf（源 PDF）" }
if ($WithOutputs) { Write-Host "  data\derived\full_cn.pdf  中文版 PDF" ; Write-Host "  data\derived\full_bi.pdf  双语对照 PDF" ; Write-Host "  data\derived\annotated_sample.pdf  图示标注样板" }
Write-Host "`n注意：zip 里 config\local.json 的 deepseek_api_key 已清空，且未包含 dist\ 自身。" -ForegroundColor Yellow
