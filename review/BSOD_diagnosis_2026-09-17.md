# 蓝屏 / 异常关机"误报" — 修正后诊断报告

- 机器：LENOVO 21LE（ThinkBook）、Intel Core Ultra 9 185H、64 GB、Intel Arc 核显
- 系统：Windows 11 专业版 **Build 26200**（BIOS NJCN66WW，2025-09-18）
- 报告时间：2026-09-17 11:30
- **本版本修正了初版结论**（初版误判为"休眠期间断电"）。修正依据：用户确认这些时间点均为**主动在 Windows 里执行"关机"**，以及新增的证据采集。

---

## 一、结论（修正版）

**这台机器基本没有硬件问题，也没有在天天蓝屏。** 49 条 `Kernel-Power 41 / EventLog 6008` 中，**绝大多数是"快速启动（Fast Startup）"导致的误报**；真正的异常关机只有 **8 次，且全部集中在 8/15–9/6，最近 11 天一次都没有**。

三条独立证据同时指向"误报"：

| 证据 | 数值 | 含义 |
|---|---|---|
| 开机事件 `LastShutdownGood` | **61 / 69 = true** | **Windows 自己认定"上次关机是干净的"**，却又写了 41/6008"异常关机" → 自相矛盾 |
| 报 41 的那些开机里 `LastShutdownGood` | **41 次 true，仅 8 次 false** | 报 41 但关机被判定为干净 = 典型 Fast Startup 伪"意外关机" |
| 你的关机请求 `Event 1074` | 22 条"关闭电源"，全部来自 `StartMenuExperienceHost.exe` | 关机是你点的，系统正常记录 |
| `Microsoft-Windows-WER-SystemErrorReporting` | **0 条** | 内核从未写过 bugcheck 记录 |
| `WHEA-Logger` | **0 条** | **从未记录任何硬件错误** |
| Minidump | 停更于 **2026-08-06** | 8/6 之后再无崩溃转储 |

**关键实证**：9/16 18:14 与 9/15 18:05 这两次"异常关机"，事件日志完整记录了正常关机流程：

```
18:14:39 Event 1074  User32  ← 你在开始菜单点了"关闭电源"
18:14:44 Event 105   Kernel-Power  ← 内核开始关机
18:14:44~45 若干服务终止（DistributedCOM 10010）
（下一次开机）Kernel-Boot Id=20 → LastShutdownGood = true
```

整个过程 6 秒内完成，没有任何卡顿、没有错误、没有转储 —— **这是一次正常关机，却被 41/6008 标成"意外"**。这正是 Windows 的 Fast Startup（`HiberbootEnabled = 1`）在"会话休眠 → 关机"路径上不写关机标记所导致的已知误报。

---

## 二、那么"真正的"异常关机有几台？

用 `LastShutdownGood = false` 精确筛出（共 8 次）：

| 下次开机时间 | 异常关机发生在 |
|---|---|
| 2026-08-15 14:29:37 | 8/15 |
| 2026-08-25 11:17:57 | 8/25 |
| 2026-08-26 23:39:41 | 8/26 |
| 2026-08-30 12:40:00 | 8/30 |
| 2026-08-30 14:47:59 | 8/30 |
| 2026-09-03 10:07:08 | 9/3 |
| 2026-09-06 17:57:48 | 9/6 |
| 2026-09-06 18:18:15 | 9/6 |

**最后一次在 2026-09-06，之后 11 天为 0。**

这批事件的共同现场特征：
- 反复出现 `Service Control Manager 7000`（**服务启动失败**）+
  `Service Control Manager 7021` + `NDIS 10317`（网络接口异常）
- `Schannel 36871` 队列（TLS 握手失败，联网组件在异常收尾）
- `TPM-WMI`、`UserModePowerService`、`DriverFrameworks-UserMode` 集中出现
- 没有 minidump、没有 bugcheck 记录、没有 WHEA → **不是内核崩溃，更像关机/重启流程被卡住或直接断电**

### 关于 WER 里那些吓人的蓝屏码（0x19C / 0x133 / 0x50 / 0x7E）

初版把它们当成真实硬件故障，**这是错的**。核对发现：

- `Microsoft-Windows-WER-SystemErrorReporting` 事件 **0 条**、`WHEA-Logger` **0 条** → 系统从未记录过这些 bugcheck
- 这些签名来自 `Windows Error Reporting` 的 **BlueScreen 归档报告队列**，且**被反复重复上报**（同一个 `P5` 地址在多个时间戳重复出现）
- 报头里 `P6 = 10_0_26200`（当前系统），但同批还有 `10_0_26100` —— 说明是**跨系统版本的历史归档被重复汇报**

→ 结论：**这批 bugcheck 码是历史残留，不代表现在还在蓝屏。**

### 真实发生过的崩溃（有转储为证）

`C:\Windows\Minidump` 里 5 个文件，才是真正的蓝屏，且都很早：

| 时间 | 转储 |
|---|---|
| 2026-05-19 20:58 | 051926-16921-01.dmp |
| 2026-06-05 21:17 | 060526-18000-01.dmp |
| 2026-06-11 09:59 | 061126-12328-01.dmp |
| 2026-07-28 22:27 | 072826-16046-01.dmp |
| 2026-08-06 20:54 | 080626-16468-01.dmp |

**5–8 月每 2–4 周一次，8/6 之后彻底停止。**

---

## 三、那最初的困惑是怎么来的

1. **事件查看器只显示 41/6008 的"意外关机"**，你不会看到同一批日志里 `LastShutdownGood = true`，于是"每天两次意外关机"看起来像硬件在坏。
2. **你确实每次都点的是"关机"**（22 条 1074 全是 `StartMenuExperienceHost.exe` 的"关闭电源"），所以"意外"二字纯属误报。
3. **Chrome 一直提示"上次异常退出，是否恢复"**：这是 Chrome 自己的 `exit_type = Crashed` 清理标记（**当前仍是该值**）。它由「Chrome 进程未走正常退出流程就被终止」触发 —— 本轮已确认主要来自另一个 session 的 `tests/ui_smoke.py` 用 `taskkill /F /T` 强杀（10:59:19 实测抓到 10 个 Chrome 进程 50 ms 内同时消失，主进程 21112 在内）。**与系统"异常关机"无关**（但 Fast Startup 关机会顺带让 Chrome 记一次 unclean）。

---

## 四、建议

### A. 关掉 Fast Startup（消除 47 条误报噪音）
```powershell
# 管理员
powercfg /hibernate off
# 或保留休眠只关快速启动：
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f
```
之后 `Kernel-Power 41 / 6008` 应立刻消失。若还出现，那就变成了**真信号**，值得追。

### B. 针对那 8 次真实的异常关机（已 11 天未复发，属低优先级）
现场特征指向**网络驱动/服务在关机收尾时卡住**：
- 更新 **Intel Wi-Fi 6E AX211 驱动**（当前 24.70.0.3，2026-07-29）
- 更新 **Intel 以太网 I219-V 驱动**（当前 20.0.2.19，**2024-06-26，已两年**）
- 如不用 VMware：禁用 VMnet1/VMnet8 虚拟网卡
- 顺带更新 BIOS（当前 NJCN66WW / 2025-09-18）与 Intel 芯片组

### C. 让下次真崩溃留下证据
```powershell
reg add "HKLM\SYSTEM\CurrentControlSet\Control\CrashControl" /v EnableLiveKernelReports /t REG_DWORD /d 1 /f
```
（`CrashDumpEnabled=3` 已正确，页面文件 4 GB 也够。）

### D. 如果还想进一步验证"到底有没有硬件问题"
观察一周即可：关掉 Fast Startup 后若 41/6008 归零 → 设备健康；若数量变成"真异常关机"且带 minidump → 再按转储里的 faulting module 定位。

---

## 五、需要你确认

1. 你印象里 **8/15–9/6** 那段时间是否有过"点了关机但屏幕卡住/直接断电"的经历？（那是 8 次真异常关机的窗口）
2. 是否在用 **VMware**？（两次真异常关机的现场都有 VMnet 组件活动）

---

## 附：原始证据文件

| 文件 | 内容 |
|---|---|
| `%TEMP%\bsod\crash_timeline.txt` | 41/6008/WER/驱动/更新全量明细 |
| `%TEMP%\bsod\correlation.txt` | 41 事件全历史（49 次）与按月统计、日志覆盖范围 |
| `%TEMP%\bsod\shutdown_audit.txt` | 每次报 41 的关机前后事件 |
| `%TEMP%\bsod\clean_vs_dirty.txt` | 39 条 1074 与 41 的对应关系（含发起进程） |
| `%TEMP%\bsod\sleepstudy.html`、`battery.html` | powercfg 原始报告（电池健康 76,440/85,000 mWh，270 循环，正常） |
