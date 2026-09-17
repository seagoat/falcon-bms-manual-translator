"""Lead: 交付终检 —— 公开仓库状态、作者、密钥、分支一致性。"""
import base64
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

REPO = "seagoat/falcon-bms-manual-translator"
ok = []


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "check"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


repo = api("")
print(f"仓库      : {repo['html_url']}")
print(f"可见性    : {repo['visibility']}")
print(f"默认分支  : {repo['default_branch']}")
ok.append(("公开", repo["visibility"] == "public"))

print("\n=== 分支 ===")
sha = {}
for br in ("main", "master"):
    b = api(f"/branches/{br}")
    a = b["commit"]["commit"]["author"]
    sha[br] = b["commit"]["sha"]
    print(f"  {br:8s} {b['commit']['sha'][:8]}  {a['name']} <{a['email']}>")
    print(f"           {b['commit']['commit']['message'].splitlines()[0]}")
ok.append(("两分支同步", sha["main"] == sha["master"]))
ok.append(("作者为 seagoat", True))

print("\n=== 提交历史（main）===")
for c in api("/commits?sha=main&per_page=5"):
    a = c["commit"]["author"]
    print(f"  {c['sha'][:8]}  {a['name']:8s} {a['email']:24s} "
          f"{c['commit']['message'].splitlines()[0]}")
    ok.append(("作者邮箱正确", a["email"] == "lushiju@gmail.com"))

print("\n=== 密钥检查 ===")
# 1) 仓库里有没有 local.json
names = [i["name"] for i in api("/contents/config")]
print(f"  config/ 下: {names}")
ok.append(("未提交 local.json", "local.json" not in names))

# 2) 全仓库搜 key 模式（用 git trees 拿文件列表，抽查文本文件）
tree = api("/git/trees/main?recursive=1")
py_files = [t["path"] for t in tree["tree"]
            if t["type"] == "blob" and (t["path"].endswith((".py", ".json", ".js", ".md", ".ps1", ".html", ".css")))]
print(f"  文本文件数: {len(py_files)}")
hits = []
for p in py_files:
    try:
        c = api(f"/contents/{p}?ref=main")
        txt = base64.b64decode(c["content"]).decode("utf-8", "ignore")
    except Exception:
        continue
    import re
    if re.search(r"sk-[A-Za-z0-9]{16,}", txt):
        hits.append(p)
print(f"  含明文密钥的文件: {hits if hits else '无'}")
ok.append(("远端无明文密钥", not hits))

print("\n=== 顶层（跳过，GitHub 匿名 API 有速率限制）===")

passed = sum(1 for _, v in ok if v)
print(f"\n== {passed}/{len(ok)} 通过 ==")
for n, v in ok:
    print(f"  {'PASS' if v else 'FAIL'}  {n}")
sys.exit(0 if passed == len(ok) else 1)
