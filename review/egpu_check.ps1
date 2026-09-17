# ============================================================
#  eGPU / Graphics Dock Boot Self-Check
#  Run AFTER booting with the OCuLink dock connected.
#  Usage: right-click PowerShell -> "Run as administrator", then run this file.
#  (ASCII-only on purpose: avoids the WinPS 5.1 UTF-8-without-BOM parsing bug)
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$out = "$env:USERPROFILE\Desktop\egpu_check_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"
$L = New-Object System.Collections.Generic.List[string]
function W($s) { $L.Add([string]$s); Write-Host $s }

W "=============================================="
W " eGPU Boot Self-Check Report   $stamp"
W "=============================================="
W ""

# ---------- 1. System / boot ----------
W "--- [1] System ---"
$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$bios = Get-CimInstance Win32_BIOS
W ("Model    : " + $cs.Manufacturer + " " + $cs.Model)
W ("OS       : " + $os.Caption + "  Build " + $os.BuildNumber)
W ("Booted   : " + $os.LastBootUpTime)
W ("Uptime   : " + [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalMinutes, 1) + " min")
W ("BIOS     : " + $bios.SMBIOSBIOSVersion)
W ""

# ---------- 2. eGPU enumeration ----------
W "--- [2] eGPU enumeration (KEY) ---"
$gpu = @(Get-PnpDevice -Class Display | Select-Object Status, Present, FriendlyName, InstanceId)
foreach ($g in $gpu) {
    $prob = (Get-PnpDeviceProperty -InstanceId $g.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode').Data
    $drv = (Get-PnpDeviceProperty -InstanceId $g.InstanceId -KeyName 'DEVPKEY_Device_DriverVersion').Data
    W ("  {0,-34} | {1,-8} | present={2,-6} | Problem={3,-3} | drv={4}" -f $g.FriendlyName, $g.Status, $g.Present, $prob, $drv)
}
$amd = @($gpu | Where-Object { $_.FriendlyName -match 'Radeon|AMD' -and $_.Present })
W ""
if ($amd.Count -gt 0) {
    W "  >>> eGPU DETECTED"
    foreach ($a in $amd) {
        $prob = (Get-PnpDeviceProperty -InstanceId $a.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode').Data
        if ($prob -ne 0) {
            W ("      !! ATTENTION ProblemCode = " + $prob + "  (12=out of resources, 43=driver failure, 31=cannot load driver)")
        } else {
            W "      OK (Problem=0)"
        }
        W ("      InstanceId: " + $a.InstanceId)
    }
} else {
    W "  >>> NO eGPU FOUND. If the OCuLink dock WAS connected at this boot, enumeration failed."
}
W ""

# ---------- 3. PCIe topology ----------
W "--- [3] PCIe topology (eGPU attach chain) ---"
$pci = @(Get-PnpDevice | Where-Object { $_.FriendlyName -match 'PCI Express Root Port|AMD PCI Express' } | Sort-Object FriendlyName)
foreach ($p in $pci) {
    W ("  {0,-40} | {1,-8} | present={2}" -f $p.FriendlyName, $p.Status, $p.Present)
}
W ""
W "  Present root ports with link info:"
$rp = @(Get-PnpDevice | Where-Object { $_.FriendlyName -eq 'PCI Express Root Port' -and $_.Present })
foreach ($r in $rp) {
    $ls = (Get-PnpDeviceProperty -InstanceId $r.InstanceId -KeyName 'DEVPKEY_PciDevice_CurrentLinkSpeed').Data
    $lw = (Get-PnpDeviceProperty -InstanceId $r.InstanceId -KeyName 'DEVPKEY_PciDevice_CurrentLinkWidth').Data
    $tail = ($r.InstanceId -split '\\')[-1]
    W ("    {0} | LinkSpeed={1} | LinkWidth={2}" -f $tail, $ls, $lw)
}
W ""

# ---------- 4. Display outputs ----------
W "--- [4] Display adapters & monitors ---"
$vc = @(Get-CimInstance Win32_VideoController)
foreach ($v in $vc) {
    W ("  {0,-32} | drv={1} | status={2}" -f $v.Name, $v.DriverVersion, $v.Status)
    W ("      mode: {0}x{1} @ {2}Hz" -f $v.CurrentHorizontalResolution, $v.CurrentVerticalResolution, $v.CurrentRefreshRate)
}
W ""
$mon = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID)
if ($mon.Count -gt 0) {
    W "  Monitors detected:"
    foreach ($m in $mon) {
        $nm = -join ($m.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ })
        if ($nm) { W ("    - " + $nm) }
    }
} else {
    W "  (no monitor names available)"
}
W ""

# ---------- 5. Device / driver warnings since boot ----------
W "--- [5] Device & driver warnings since this boot ---"
$since = $os.LastBootUpTime
$bad = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since} |
    Where-Object { $_.LevelDisplayName -in 'Error', 'Warning', 'Critical' -and $_.ProviderName -match 'Kernel-PnP|Display|amd|Dxgkrnl|UserPnp' })
if ($bad.Count -gt 0) {
    foreach ($b in ($bad | Sort-Object TimeCreated | Select-Object -First 15)) {
        $m = ($b.Message -replace "`r`n", ' ').Trim()
        W ("  {0} | {1} | Id={2}" -f $b.TimeCreated.ToString('HH:mm:ss'), ($b.ProviderName -replace 'Microsoft-Windows-', ''), $b.Id)
        W ("      " + $m.Substring(0, [Math]::Min(180, $m.Length)))
    }
} else {
    W "  none (clean)"
}
W ""

# ---------- 6. AMD services ----------
W "--- [6] AMD services ---"
foreach ($s in @(Get-Service | Where-Object { $_.DisplayName -match 'AMD' })) {
    W ("  {0,-32} | {1,-8} | {2}" -f $s.Name, $s.Status, $s.StartType)
}
W ""

# ---------- 7. Power ----------
W "--- [7] Power ---"
$b = Get-CimInstance Win32_Battery
if ($b) { W ("  Battery: {0}%  status={1}" -f $b.EstimatedChargeRemaining, $b.BatteryStatus) }
W ""

W "=============================================="
W " END OF REPORT"
W "=============================================="

$L | Set-Content -Path $out -Encoding UTF8
Write-Host ""
Write-Host ("Saved to: " + $out) -ForegroundColor Green
