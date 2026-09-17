"""Lead: 从公开 API 复核 GitHub 仓库状态（不需要 token）。"""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

REPO = "seagoat/falcon-bms-manual-translator"


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "manual-trans-trace-check"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


try:
    repo = api("")
except Exception as e:
    print(f"❌ 仓库不可访问: {type(e).__name__}: {e}")
    sys.exit(1)

print(f"仓库     : {repo['full_name']}")
print(f"可见性   : {repo['visibility']}")
print(f"默认分支 : {repo['default_branch']}  ← 在 GitHub 上打开时显示的分支")
print(f"大小     : {repo['size']} KB")
print(f"文件/描述: {repo.get('description') or '(无描述)'}")
print(f"创建时间 : {repo['created_at']}")
print(f"URL      : {repo['html_url']}")

print("\n=== 分支 ===")
for b in api("/branches"):
    print(f"  {b['name']:8s} {b['commit']['sha'][:8]}")

print("\n=== 顶层内容 ===")
for item in api("/contents/"):
    kind = "dir " if item["type"] == "dir" else "file"
    size = item.get("size", 0)
    print(f"  {kind} {item['name']:24s} {size:>8} B")

print("\n=== README 前 3 行 ===")
try:
    req = urllib.request.Request(
        f"https://raw.githubusercontent.com/{REPO}/main/README.md",
        headers={"User-Agent": "manual-trans-trace-check"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r.read().decode("utf-8").split("\n")[:3]:
            print(f"  {line}")
except Exception as e:
    print(f"  读 README 失败: {e}")
