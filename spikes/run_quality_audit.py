"""Lead: run the translation-quality audit + targeted repair on both DBs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")


def run(db_env: str, version_id: int, label: str, *, do_fix: bool):
    os.environ["MT_DATA_DIR"] = db_env
    import config
    config.load(reload=True)
    for m in [m for m in list(sys.modules) if m.startswith(("core", "translate"))]:
        del sys.modules[m]
    from core import db as cdb
    from translate import quality

    con = cdb.connect()
    items = quality.audit(con, version_id)
    print(f"\n{'=' * 78}\n=== {label}：可疑译文 {len(items)} 条 ===")
    for it in items[:25]:
        s = it["seg"]
        print(f"  p{s['page']:3d} seg={s['id']} {s['kind']:10s} [{it['why']}]")
        print(f"     EN: {s['text'][:110]!r}")
        print(f"     ZH: {it['zh'][:110]!r}")
    if do_fix and items:
        print(f"\n--- 定点修复（带上下文重译）---")
        res = quality.repair(con, version_id, items)
        print(f"修复结果: {res}")
    con.close()
    return len(items)


n1 = run("data/_to_probe", 1, "TO-34（669 页）", do_fix=True)
n2 = run("data", 2, "BMS 训练手册 v2（401 页）", do_fix=True)
print(f"\n汇总：TO-34 {n1} 条可疑；训练手册 {n2} 条可疑")
