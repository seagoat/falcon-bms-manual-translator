<#
.SYNOPSIS
    一键启动 manual_trans_trace 对照浏览器（Web 服务）。

.DESCRIPTION
    依次完成：检查 Python 与依赖 → 确保数据目录 → （必要时）入库原始手册 →
    起 uvicorn → 打印地址（-NoOpen 时不开浏览器）。

    注意：本脚本**不捕获**原生命令输出（`$x = & python ...` 在部分沙箱/宿主下会被拒绝），
    python 的版本/依赖/数据状态一律通过**退出码**或**临时文件**传递。

.EXAMPLE
    .\start_web.ps1
    .\start_web.ps1 -Port 8901 -NoOpen
    .\start_web.ps1 -Reload -NoIngest
#>
[CmdletBinding()]
param(
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 0,
    [switch]$NoOpen,
    [switch]$Reload,
    [switch]$NoIngest,
    [string]$Pdf = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn2($m){ Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "manual_trans_trace - Web 对照浏览器" -ForegroundColor Cyan
Write-Host "工作目录: $Root"
Write-Host ""

# ---------------------------------------------------------------- 1. Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Die "找不到 python，请先安装 Python 3.11 并加入 PATH"
}
& python --version
if ($LASTEXITCODE -ne 0) { Die "python 不可用（退出码 $LASTEXITCODE）" }

# ---------------------------------------------------------------- 2. 依赖
$null = New-Item -ItemType Directory -Path 'data\derived' -Force -ErrorAction SilentlyContinue
& python -c "import pymupdf, fastapi, uvicorn, PIL, numpy"
if ($LASTEXITCODE -ne 0) {
    Die "缺少依赖（pymupdf / fastapi / uvicorn / Pillow / numpy）；详见上方 ImportError"
}
Ok "依赖检查通过（pymupdf / fastapi / uvicorn / Pillow / numpy）"

# ---------------------------------------------------------------- 3. 数据目录
foreach ($d in @('data', 'data\derived', 'data\pdfs', 'data\cache', 'origin')) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Ok "数据目录就绪（data/ derived/ pdfs/ cache/）"

# ---------------------------------------------------------------- 4. 读取状态（端口 / 文档数）
# 通过临时文件回传，避免捕获原生命令 stdout；探针写成 .py 文件再执行，
# 因为 PowerShell 5.1 会把 `python -c "<含引号的代码>"` 的引号吃掉。
$statusFile = Join-Path $Root 'data\derived\_web_status.json'
$probePath = Join-Path $Root 'data\derived\_web_probe.py'
Remove-Item -LiteralPath $statusFile -Force -ErrorAction SilentlyContinue
$pyProbe = @'
import json, pathlib, sys
out = pathlib.Path(sys.argv[1])
info = {"port": 8777, "doc_count": 0, "error": ""}
try:
    import config
    info["port"] = int(config.get("web_port", 8777) or 8777)
    from core import db, store
    conn = db.connect()
    info["doc_count"] = len(store.list_documents(conn))
    conn.close()
except Exception as exc:
    info["error"] = type(exc).__name__ + ": " + str(exc)
out.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
'@
Set-Content -LiteralPath $probePath -Value $pyProbe -Encoding ASCII
# 探针脚本在 data\derived\ 下，Python 会把「脚本目录」放进 sys.path，
# 因此显式把仓库根加入 PYTHONPATH，否则 import config 会失败。
$env:PYTHONPATH = $Root
& python $probePath $statusFile
$status = $null
if (Test-Path -LiteralPath $statusFile) {
    try { $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json } catch { $status = $null }
}
if ($null -eq $status) {
    Warn2 "状态探测失败（$statusFile），改用默认端口 8777"
    $status = [pscustomobject]@{ port = 8777; doc_count = 0; error = "" }
}
if ($status.error) { Warn2 "状态探测异常：$($status.error)" }

if ($Port -le 0) { $Port = [int]$status.port }
if ($Port -le 0) { $Port = 8777 }

# ---------------------------------------------------------------- 5. 端口占用
$busy = $null
try { $busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue } catch { $busy = $null }
if ($busy) {
    $owner = ($busy | Select-Object -First 1).OwningProcess
    Warn2 "端口 $Port 已被占用（PID $owner）"
    Warn2 "换端口:  .\start_web.ps1 -Port $($Port + 1)"
    Warn2 "查进程:  Get-Process -Id $owner"
    Die "请换一个端口或先停掉占用进程"
}

$host_ = $BindHost
if (-not $host_) { $host_ = '127.0.0.1' }
$url = "http://${host_}:${Port}/"
Info "服务地址: $url"

# ---------------------------------------------------------------- 6. 确保有数据
$docCount = [int]$status.doc_count
if ($docCount -gt 0) {
    Ok "数据库已有 $docCount 个文档"
} elseif ($NoIngest) {
    Warn2 "数据库为空，且指定了 -NoIngest：界面会提示先入库"
} else {
    $target = $Pdf
    if (-not $target) { $target = Join-Path $Root 'origin\BMS-Training-Manual.pdf' }
    if (Test-Path -LiteralPath $target) {
        Info "数据库为空 -> 先抽取入库（401 页约 15-20 秒，仅首次）"
        & python cli.py ingest --pdf $target
        if ($LASTEXITCODE -ne 0) { Die "入库失败（退出码 $LASTEXITCODE）" }
        Ok "入库完成"
    } else {
        Warn2 "找不到原始 PDF：$target"
        Warn2 "可稍后手动执行: python cli.py ingest --pdf <你的.pdf>"
    }
}

# ---------------------------------------------------------------- 7. 起服务
Write-Host ""
Write-Host "启动中... 按 Ctrl+C 停止服务" -ForegroundColor Cyan
Write-Host ""

if (-not $NoOpen) {
    try {
        Start-Job -ScriptBlock {
            param($u)
            for ($i = 0; $i -lt 60; $i++) {
                Start-Sleep -Milliseconds 500
                try {
                    $r = Invoke-WebRequest -Uri "$u/api/health" -UseBasicParsing -TimeoutSec 3
                    if ($r.StatusCode -eq 200) { Start-Process $u; break }
                } catch { }
            }
        } -ArgumentList $url | Out-Null
    } catch {
        Warn2 "后台打开浏览器失败（不影响服务）: $_"
    }
}

$uvArgs = @('-m', 'uvicorn', 'web.server:app', '--host', $host_, '--port', "$Port", '--log-level', 'info')
if ($Reload) { $uvArgs += '--reload' }

& python @uvArgs
$code = $LASTEXITCODE
if ($code -ne 0) { Die "服务退出，退出码 $code" }
