"""Lead: verify the dot-leader fitter stays inside the anchor and keeps the number."""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

from render import pagebuild as pb

TESTS = [
    ("前言 .................. 2", 9.2, 541.1, 53.9),
    ("多人飞行与训练 .................. 7", 9.2, 541.1, 53.9),
    ("任务 16：SPICE (TR_BMS_16_Spice) ............ 166", 9.2, 541.1, 64.9),
    ("MISSION 1: GROUND OPS (TR_BMS_01_GroundOPS) ............ 10", 9.2, 541.1, 64.9),
    ("1.10 滑行 ..................................................... 1000", 9.2, 541.1, 91.3),
]
bad = 0
for zh, fs, anchor, x0 in TESTS:
    t, w = pb._fit_dot_leader(zh, fs, False, 470.0, anchor, x0)
    end = x0 + w
    ok = end <= anchor + 0.6
    keeps_num = t.rstrip().split()[-1] == zh.rstrip().split()[-1]
    if not (ok and keeps_num):
        bad += 1
    print(f"{'OK  ' if ok and keeps_num else 'FAIL'} x0={x0:5.1f} anchor={anchor:6.1f} "
          f"末端={end:6.1f} 保留页码={keeps_num}")
    print(f"      {t[-46:]!r}")
print(f"\n失败 {bad}/{len(TESTS)}")
sys.exit(1 if bad else 0)
