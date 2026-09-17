"""从最近一次真实数据 UI 快照里提取验收证据（不跑浏览器）。

用法：python web/static/_devtest/show_ui_evidence.py [snapshot.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
P = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "derived" / "preview" / "ui_snapshot.json"
d = json.loads(P.read_text(encoding="utf-8"))
print("snapshot:", P)
print("url     :", d.get("url"))
print("result  :", d.get("passed"), "/", d.get("passed", 0) + d.get("failed", 0), "passed")
print()
print("--- 各阶段状态 ---")
for s in d.get("snapshots", []):
    c = s["chrome"]
    print(f"  {s['label']:14s} page={c['page']:>3} badges={c['badges']!r}")
    print(f"                 counts={c['counts']}  errorVisible={c['errorVisible']} "
          f"progressVisible={c['progressVisible']} detailVisible={s['detail']['visible']}")
print()
print("--- 关键断言 ---")
KEYS = ("弹窗", "进度条", "canvas", "幽灵", "徽章", "色条", "sel", "peer", "同步", "居中", "视口", "占位", "详情")
for r in d.get("results", []):
    if any(k in r["name"] for k in KEYS):
        print(("  PASS  " if r["ok"] else "  FAIL  ") + r["name"])
