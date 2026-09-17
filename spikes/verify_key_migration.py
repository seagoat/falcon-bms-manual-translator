"""Lead: 端到端复核 —— 密钥迁移后仍能正常翻译，且仓库内无明文密钥。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1) config.load() 拿到 key 的路径：环境变量
os.environ["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
if not os.environ["DEEPSEEK_API_KEY"]:
    # setx 只对新进程生效；这里从备份读一次，模拟新终端
    try:
        import json

        with io.open(os.path.join(os.path.expanduser("~"),
                                  "dsh_deepseek_key_backup.json"), encoding="utf-8") as f:
            os.environ["DEEPSEEK_API_KEY"] = json.load(f)["deepseek_api_key"]
        print("（本进程未继承 setx 变量，已从备份读入以模拟新终端）")
    except Exception as e:
        print(f"无法取得 key：{e}")
        sys.exit(1)

import config  # noqa: E402

config.load(reload=True)
key = (config.load() or {}).get("deepseek_api_key") or ""
print(f"1) config.load() 取到 key：{bool(key)}  长度={len(key)}")

# 2) 真的调一次 API，确认 key 可用
from translate import engine  # noqa: E402

from core import db as cdb  # noqa: E402

con = cdb.connect()
try:
    segs = con.execute(
        "SELECT id, text, page FROM segments WHERE version_id=2 AND role='body' "
        "AND length(text) BETWEEN 40 AND 120 ORDER BY id LIMIT 1").fetchone()
    if segs:
        item = engine._Item(int(segs["id"]), segs["text"], int(segs["page"]),
                            "body", "paragraph")
        gl = engine.glossary.prompt_block()
        r = engine._run_batch([item], {}, provider="deepseek",
                              model=config.get("model", "deepseek-flash"),
                              cfg=config.load(), api_key=key, glossary_block=gl)
        ok = bool(r.get("ok"))
        out = (r.get("out") or {}).get(int(segs["id"])) if ok else None
        print(f"2) 真实调用 DeepSeek：{'✅ 成功' if ok else '❌ 失败'}")
        if ok:
            print(f"   原文: {segs['text'][:70]}")
            print(f"   译文: {(out or '')[:70]}")
        else:
            print(f"   错误: {str(r.get('error'))[:160]}")
    else:
        print("2) 未找到测试段")
finally:
    con.close()

# 3) 仓库内（含 config/）是否还有明文密钥
print("\n3) 扫描仓库内明文密钥（排除 data/origin/dist/__pycache__）")
hits = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames
                   if d not in ("__pycache__", "data", "origin", "dist", "_chrome_profile")]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        try:
            if os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            with io.open(p, encoding="utf-8", errors="ignore") as f:
                t = f.read()
        except OSError:
            continue
        if key and key in t:
            hits.append(os.path.relpath(p, ROOT))
print("   " + ("⚠ 命中: " + ", ".join(hits) if hits else "✅ 无"))

# 4) git 是否跟踪 config/local.json
import subprocess  # noqa: E402

tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True).stdout
print(f"4) git 跟踪 config/local.json：{'⚠ 是' if 'config/local.json' in tracked else '✅ 否'}")
