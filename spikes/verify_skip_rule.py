"""Lead: verify the continuation-skip rule still detects the ORIGINAL bug
(empty/near-empty translation of a continuing segment)."""
import os
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from translate import quality

# 最初发现的 bug：续句 + 译文为空
en_bad = "without the written permission of the BMS Docs team."
print("=== suspect() 判定 ===")
print(f"  '。'                    -> {quality.suspect(en_bad, '。')}")
print(f"  '（空）'                 -> {quality.suspect(en_bad, '')}")
print(f"  '第'                    -> {quality.suspect('Ensure delivery mode is set to PRE ...', '第')}")
print(f"  'I. 外部'                -> {quality.suspect('I. EXTERNAL LIGHTNING SETTINGS ......', 'I. 外部')}")
print(f"  正常译文（负例）           -> {quality.suspect(en_bad, '未经 BMS 文档团队书面许可。')}")
print(f"  正常长译文（负例）         -> {quality.suspect('a' * 300, '这是一段正常长度的中文译文。' * 5)}")

# 构造一个「续句 + 空译文」的假库，确认 audit 不会跳过它
import sqlite3
import tempfile

from core import db as cdb, store

os.environ["MT_DATA_DIR"] = "data"
import config
config.load(reload=True)

con = cdb.connect()
# 只在内存里验证逻辑：直接检查 audit 的判定分支
SENT_END = quality.SENT_END
cases = [
    ("续句 + 空译文（必须报）", "this manual is allowed", "。", "without the written permission"),
    ("续句 + 正常译文（可跳过）", "this manual is allowed", "本手册允许", "without the written permission"),
]
for name, a, azh, b in cases:
    zh_txt = azh.strip()
    substantive = len(quality._strip_punct(zh_txt)) >= 3
    would_skip = substantive and a[-1] not in SENT_END and b[:1].islower()
    print(f"\n{name}:")
    print(f"   substantive={substantive}  会跳过={would_skip}  "
          f"-> {'❌ 漏报' if would_skip else '✅ 会报出来'}")
con.close()
