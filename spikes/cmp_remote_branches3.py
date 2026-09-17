"""Lead: 通过 GitHub API（而非 raw CDN）取两个分支的 README，验证内容真正一致。"""
import base64
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

REPO = "seagoat/falcon-bms-manual-translator"


def api(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "check"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


print("=== 各分支的 HEAD commit ===")
for br in ("main", "master"):
    b = api(f"/branches/{br}")
    print(f"  {br:8s} sha={b['commit']['sha'][:8]}  "
          f"author={b['commit']['commit']['author']['name']} "
          f"<{b['commit']['commit']['author']['email']}>")
    print(f"           msg={b['commit']['commit']['message'].splitlines()[0]}")

print("\n=== 各分支 README 的 blob sha 与大小 ===")
for br in ("main", "master"):
    c = api(f"/contents/README.md?ref={br}")
    txt = base64.b64decode(c["content"]).decode("utf-8")
    print(f"  {br:8s} blob_sha={c['sha'][:10]}  size={c['size']}  "
          f"含密钥安全={'## 密钥安全' in txt}")
