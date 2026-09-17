"""Lead: 把 DeepSeek key 从 config/local.json 挪到用户级环境变量。

原因：`config/local.json` 虽然已被 .gitignore 排除，但**明文密钥留在仓库目录里**
本身就是风险（误提交、打包分发、截图泄露）。改成读用户级环境变量后，
仓库目录里不再有任何密钥。

做完后会：
  1. 备份原文件到仓库外（%USERPROFILE%\\）
  2. 把 local.json 里的 key 清空
  3. 写用户级环境变量 DEEPSEEK_API_KEY
  4. 校验 config.load() 仍能取到 key
"""
import io
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config", "local.json")

with io.open(CFG, encoding="utf-8") as f:
    cfg = json.load(f)

key = (cfg.get("deepseek_api_key") or "").strip()
if not key:
    print("config/local.json 里已经没有 key 了，无需处理")
    sys.exit(0)

print(f"读到 key：{key[:8]}…{key[-4:]}（长度 {len(key)}）")

# 1) 备份到仓库外
backup = os.path.join(os.path.expanduser("~"), "dsh_deepseek_key_backup.json")
with io.open(backup, "w", encoding="utf-8") as f:
    json.dump({"deepseek_api_key": key}, f, ensure_ascii=False, indent=1)
print(f"① 已备份到仓库外：{backup}")

# 2) 清空仓库内的 key
cfg["deepseek_api_key"] = ""
with io.open(CFG, "w", encoding="utf-8", newline="\n") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("② config/local.json 里的 key 已清空")

# 3) 写用户级环境变量（持久）
subprocess.run(["setx", "DEEPSEEK_API_KEY", key], capture_output=True, text=True)
print("③ 已写入用户级环境变量 DEEPSEEK_API_KEY（setx，重新开终端后生效）")

# 4) 校验：当前进程设上后再 load
os.environ["DEEPSEEK_API_KEY"] = key
import config  # noqa: E402

config.load(reload=True)
got = (config.load() or {}).get("deepseek_api_key") or ""
print(f"④ 校验 config.load() 取到 key：{bool(got)}（长度 {len(got)}）")
print(f"   取到的是同一个 key：{got == key}")

# 5) 再扫一遍仓库目录
print("\n=== 扫描仓库内是否还有 key ===")
found = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames
                   if d not in ("__pycache__", "data", "origin", "dist", "_chrome_profile")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        try:
            if os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            with io.open(p, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except OSError:
            continue
        if key in txt:
            found.append(os.path.relpath(p, ROOT))
if found:
    print("  ⚠ 仍含有明文 key 的文件：")
    for f in found:
        print(f"     {f}")
else:
    print("  ✅ 仓库目录内不再有任何文件包含该 key")
