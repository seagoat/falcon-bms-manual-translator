# -*- coding: utf-8 -*-
"""tests/test_versions.py — versions/differ.py 的验收测试（可直接运行）。

    python tests\\test_versions.py        # 退出码 0 = 全部通过，1 = 有失败

覆盖：
  1. 主场景：v1 固定文本集 → v2 施加已知操作（改 3 段[微改/大改/中改]、删 2、插 3、
     跨页移 1、同页重排 1、1 拆 2、2 并 1），断言 counts 精确相等、ratio 合理、
     char_diffs 可还原原文、变更顺序稳定。
  2. 幂等：二次调用结果一致；version_diffs 只有一行；segment_links 不重复。
  3. 链接与辅助 API：changes_by_segment / removed_by_segment / diff_summary_text。
  4. canonical_text：两版仅行尾连字符/断行不同 → 必须判 unchanged（CONTRACT §3.1.1）。
  5. 边界：同一版本互 diff 全 unchanged；role!=body 的段不参与。
  6. 性能：401 页量级 ~4000 段的两版 diff < 3 秒。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 用 append 而不是 insert(0)：开发期可用 PYTHONPATH 指向 core 替身做并行验证。
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from core import store                                    # noqa: E402
from core.db import connect                               # noqa: E402
from core.fingerprint import normalize                    # noqa: E402
from core.models import Segment                           # noqa: E402
from versions.differ import (                             # noqa: E402
    changes_by_segment,
    diff_summary_text,
    diff_versions,
    removed_by_segment,
)

# ---------------------------------------------------------------- 合成数据

# v1：4 页 × 10 段 = 40 段，纯 ASCII、单空格（normalize 稳定）
V1_TEXTS = [
    # page 1
    "The MASTER CAUTION light activates shortly after any individual light on the Caution Panel illuminates excluding IFF.",
    "The landing gear handle is located on the left side of the instrument panel below the throttle quadrant.",
    "Fuel quantity is displayed on the forward instrument panel when the fuel quantity selector is set to the desired tank.",
    "The air data computer supplies altitude and airspeed information to the flight instruments and the mission computer.",
    "Engine oil pressure must remain within the green band during all phases of flight and ground operation.",
    "The pilot should verify that the canopy is fully locked before advancing the throttle for takeoff.",           # 5 微改
    "Electrical power for the essential bus is provided by the main generator or the emergency power unit.",
    "The oxygen system supplies breathing oxygen through the OBOGS concentrator during normal flight operations.",
    "Cockpit lighting can be adjusted with the console and instrument panel rheostats located on the left console.",
    "The ejection seat safety pin must be removed by the ground crew before the pilot enters the cockpit.",
    # page 2
    "Flight control system operation is monitored by the FLCS computer through redundant sensor channels.",
    "The HUD provides airspeed altitude and flight path information to the pilot during all mission phases.",
    "Hydraulic system pressure is supplied by two independent engine driven pumps and one emergency pump.",         # 12 删除
    "The fuel shutoff valve closes automatically when the engine fire warning is detected by the sensor.",          # 13 删除
    "Navigation data is entered into the mission computer through the upfront control panel keyboard.",
    "The radar warning receiver displays threat symbols on the HUD and the multifunction display.",
    "The throttle must be moved to the idle detent before the engine start switch is placed to the start position.",  # 16 跨页移动
    "Air conditioning and pressurization are controlled automatically by the environmental control system.",
    "The attitude indicator shows pitch and roll relative to the horizon during instrument flight conditions.",
    "Landing gear extension is indicated by three green lights on the instrument panel after the handle is lowered.",
    # page 3
    "The airspeed indicator is marked with colored arcs that define the operating ranges for the aircraft.",
    "The inertial navigation system must be aligned for at least eight minutes before the aircraft is considered ready for flight.",  # 21 大改
    "Trim inputs are made with the stick mounted trim switch or the manual trim panel on the left console.",
    "The stores management system displays weapon status and release parameters on the multifunction display.",     # 23 同页重排
    "Anti ice and defog operation is controlled by switches on the right console below the canopy rail.",           # 24 同页重排
    "The autopilot can hold altitude heading and attitude when the flight director is engaged.",
    "Ground crew signals must be acknowledged before the aircraft is moved from the parking area.",
    "The mission computer stores waypoints and steerpoints that are selected on the upfront control panel.",
    "Radar altitude is displayed on the HUD during low level flight and is used for the ground collision warning.",
    "The environmental control system supplies cooled air to the avionics bays through the heat exchanger.",
    # page 4
    "The head up display combiner glass must be cleaned with the approved optical cloth only.",                     # 30 拆 2
    "Emergency procedures are listed in the checklist for each major system of the aircraft.",
    "The identification friend or foe system responds automatically when it is interrogated by a ground station.",
    "Tacan and instrument landing system guidance are displayed on the horizontal situation indicator.",            # 33 中改
    "The air refueling receptacle is located on the upper fuselage behind the cockpit.",
    "Wing flaps and leading edge flaps are controlled by the flap switch on the left console.",
    "The brake system uses carbon disks that are cooled by air flowing through the wheel.",                         # 36 并 1
    "The parking brake is set with the handle on the left side of the cockpit.",                                    # 37 并 1
    "The landing light is mounted on the nose gear strut and is used during night operations.",
    "Caution and warning tones are generated by the audio system and are heard through the headset.",
]

MICRO_NEW = ("The pilot must confirm that the canopy is fully locked before advancing the throttle "
             "for takeoff.")
BIG_NEW = ("Before flight the pilot aligns the inertial navigation system and checks that the "
           "alignment status shown on the display is valid.")
MODERATE_NEW = ("Tacan and instrument landing system steering information are shown on the horizontal "
                "situation indicator when the approach mode is selected.")
SPLIT_A = "The head up display combiner glass must be cleaned"
SPLIT_B = "with the approved optical cloth only."
MERGE_NEW = ("The brake system uses carbon disks that are cooled by air flowing through the wheel. "
             "The parking brake is set with the handle on the left side of the cockpit.")
ADD_1 = "Do not insert or remove the safety pin while the canopy actuator is pressurized."
ADD_2 = ("Warning allow the exhaust nozzle to cool for at least fifteen minutes after shutdown "
         "before any maintenance action.")
ADD_3 = "Note that the auxiliary power unit may be used to supply electrical power on the ground."

EXPECTED_COUNTS = {
    "unchanged": 30,
    "modified": 3,
    "added": 3,
    "removed": 2,
    "moved": 1,
    "reordered": 1,
    "split": 1,          # 组数
    "merged": 1,         # 组数
    "split_old": 1,
    "split_new": 2,
    "merged_old": 2,
    "merged_new": 1,
    "old_total": 40,
    "new_total": 41,
}

# ---------------------------------------------------------------- 测试脚手架

FAILURES: list[str] = []
CHECKS = [0]


def check(cond, msg: str) -> None:
    CHECKS[0] += 1
    if not cond:
        raise AssertionError(msg)


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


class Env:
    """一个临时 sqlite 库 + 一个文档。"""

    def __init__(self, name: str):
        self.tmp = tempfile.mkdtemp(prefix="vt_%s_" % name)
        db = os.path.join(self.tmp, "app.db")
        try:
            self.conn = connect(db)
        except TypeError:
            self.conn = connect(path=db)
        self.doc_id = store.upsert_document(
            self.conn, slug="synthetic-%s" % name, title="Synthetic Manual", source_dir=self.tmp)

    def add_version(self, label: str, rows, page_count: int = 4):
        """rows: [(page, text)] 或 [(page, text, canonical_text)] 或 [dict]"""
        vid = store.register_version(
            self.conn, doc_id=self.doc_id, source_path="%s.pdf" % label,
            sha256=hashlib.sha256(label.encode("utf-8")).hexdigest(),
            pdf_bytes=4096, page_count=page_count, label=label)
        segs = []
        for k, row in enumerate(rows):
            if isinstance(row, dict):
                page, text = row["page"], row["text"]
                canon = row.get("canonical_text", text)
                role = row.get("role", "body")
                kind = row.get("kind", "paragraph")
            else:
                page, text = row[0], row[1]
                canon = row[2] if len(row) > 2 else text
                role = "body"
                kind = "paragraph"
            segs.append(Segment(version_id=vid, doc_id=self.doc_id, page=page, order_index=k,
                                kind=kind, text=text, canonical_text=canon, role=role))
        ids = store.insert_segments(self.conn, segs)
        return vid, list(ids)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def build_v2():
    """把已知操作施加到 V1 上，返回 (rows, tags)。"""
    rows: list[tuple] = []
    tags: dict[str, list[int]] = defaultdict(list)

    def push(page, text, tag):
        tags[tag].append(len(rows))
        rows.append((page, text, tag))

    for i, text in enumerate(V1_TEXTS):
        page = 1 + i // 10
        if i == 5:
            push(page, MICRO_NEW, "mod-micro")
        elif i == 8:
            push(page, text, "unchanged")
            push(1, ADD_1, "add-1")
        elif i in (12, 13):
            pass                                   # 删除 2 段
        elif i == 16:
            pass                                   # 跨页移动：最后追加到第 4 页
        elif i == 21:
            push(page, BIG_NEW, "mod-big")
        elif i == 23:
            push(3, V1_TEXTS[24], "reorder")       # 同页交换 23/24
        elif i == 24:
            push(3, V1_TEXTS[23], "reorder")
        elif i == 27:
            push(page, text, "unchanged")
            push(3, ADD_2, "add-2")
            push(3, ADD_3, "add-3")
        elif i == 30:
            push(4, SPLIT_A, "split-a")            # 1 拆 2
            push(4, SPLIT_B, "split-b")
        elif i == 33:
            push(page, MODERATE_NEW, "mod-moderate")
        elif i == 36:
            push(4, MERGE_NEW, "merged")           # 36+37 并 1
        elif i == 37:
            pass
        else:
            push(page, text, "unchanged")
    push(4, V1_TEXTS[16], "moved")                 # 跨页移动落点
    return rows, tags


def apply_char_diffs(cds, side: str) -> str:
    """把 char_diffs 按 old/new 侧拼接还原。"""
    out = []
    for d in cds:
        op = d["op"]
        if op == "equal" or (side == "old" and op == "delete") or (side == "new" and op == "insert"):
            out.append(d["text"])
    return "".join(out)


def change_by_old(vd, old_id):
    for c in vd.changes:
        if c.old_segment_id == old_id:
            return c
    return None


def by_kind(vd, kind):
    return [c for c in vd.changes if c.kind == kind]


# ---------------------------------------------------------------- 测试 1：主场景

def test_main_scenario():
    env = Env("main")
    try:
        conn = env.conn
        rows1 = [(1 + i // 10, t) for i, t in enumerate(V1_TEXTS)]
        v1, ids1 = env.add_version("v1", rows1)
        rows2, tags = build_v2()
        v2, ids2 = env.add_version("v2", [(p, t) for (p, t, _tag) in rows2])
        check(len(rows2) == 41, "v2 应有 41 段，实际 %d" % len(rows2))

        t0 = time.perf_counter()
        vd = diff_versions(conn, env.doc_id, v1, v2)
        elapsed = time.perf_counter() - t0
        counts = vd.counts

        # --- counts 精确相等
        for k, want in EXPECTED_COUNTS.items():
            got = counts.get(k)
            check(got == want, "counts[%s] 期望 %r，实际 %r（完整 counts=%s）"
                  % (k, want, got, json.dumps(counts, ensure_ascii=False)))
        check(counts.get("doc_id") == env.doc_id and counts.get("from_version_id") == v1
              and counts.get("to_version_id") == v2, "counts 里的 id 字段不对：%s" % counts)

        # --- 覆盖度不变式
        old_cover = (counts["unchanged"] + counts["modified"] + counts["removed"]
                     + counts["moved"] + counts["reordered"]
                     + counts["split_old"] + counts["merged_old"])
        new_cover = (counts["unchanged"] + counts["modified"] + counts["added"]
                     + counts["moved"] + counts["reordered"]
                     + counts["split_new"] + counts["merged_new"])
        check(old_cover == counts["old_total"],
              "旧版覆盖度 %d != old_total %d" % (old_cover, counts["old_total"]))
        check(new_cover == counts["new_total"],
              "新版覆盖度 %d != new_total %d" % (new_cover, counts["new_total"]))

        # --- 微改：只动两个词 → MODIFIED 且 ratio > 0.8（不能是 add+remove）
        micro = change_by_old(vd, ids1[5])
        check(micro is not None, "找不到第 5 段的变更")
        check(micro.kind == "modified", "微改段应判 modified，实际 %s" % micro.kind)
        check(micro.ratio > 0.8, "微改段 ratio 应 > 0.8，实际 %.3f" % micro.ratio)
        check(micro.new_segment_id == ids2[tags["mod-micro"][0]], "微改段 new id 不匹配")

        # --- 大改：ratio < 0.5，但仍识别为 MODIFIED（整段重写，不是 add+remove）
        big = change_by_old(vd, ids1[21])
        check(big is not None and big.kind == "modified",
              "大改段应判 modified，实际 %s" % (big.kind if big else "None"))
        check(big.ratio < 0.5, "大改段 ratio 应 < 0.5，实际 %.3f" % big.ratio)

        # --- 中改
        moderate = change_by_old(vd, ids1[33])
        check(moderate is not None and moderate.kind == "modified", "中改段应判 modified")
        check(0.55 <= moderate.ratio < 0.95, "中改段 ratio 应合理，实际 %.3f" % moderate.ratio)

        # --- char_diffs 可还原原文（逐字符拼回）
        for c, old_text, new_text in (
            (micro, V1_TEXTS[5], MICRO_NEW),
            (big, V1_TEXTS[21], BIG_NEW),
            (moderate, V1_TEXTS[33], MODERATE_NEW),
        ):
            check(c.char_diffs, "modified 段无 char_diffs")
            for d in c.char_diffs:
                check(set(d.keys()) == {"op", "text"} and d["op"] in ("equal", "delete", "insert"),
                      "char_diffs 元素格式非法：%r" % (d,))
            got_old = apply_char_diffs(c.char_diffs, "old")
            got_new = apply_char_diffs(c.char_diffs, "new")
            check(got_old == normalize(old_text), "char_diffs 还原旧文失败：%r" % got_old)
            check(got_new == normalize(new_text), "char_diffs 还原新文失败：%r" % got_new)

        del_words = "".join(d["text"] for d in micro.char_diffs if d["op"] == "delete")
        ins_words = "".join(d["text"] for d in micro.char_diffs if d["op"] == "insert")
        check(0 < len(del_words) <= 20 and 0 < len(ins_words) <= 20,
              "微改的字符级改动应很小（<=20 字符），实际删除 %r / 插入 %r" % (del_words, ins_words))
        # 词级独立核对：恰好 2 个词不同（should→must、verify→confirm）
        w_old = normalize(V1_TEXTS[5]).split()
        w_new = normalize(MICRO_NEW).split()
        check(len(w_old) == len(w_new), "微改不应改变词数")
        diff_pos = [k for k in range(len(w_old)) if w_old[k] != w_new[k]]
        check(len(diff_pos) == 2, "微改应恰好改 2 个词，实际 %d 个：%s"
              % (len(diff_pos), [w_old[k] + "->" + w_new[k] for k in diff_pos]))
        check(del_words and ins_words, "微改必须给出字符级 del/ins 片段")

        # --- 删除 2 段
        removed_ids = {c.old_segment_id for c in by_kind(vd, "removed")}
        check(removed_ids == {ids1[12], ids1[13]},
              "removed 的旧段 id 不对：%s" % sorted(removed_ids))

        # --- 新增 3 段
        added_new_ids = {c.new_segment_id for c in by_kind(vd, "added")}
        want_added = {ids2[k] for k in tags["add-1"] + tags["add-2"] + tags["add-3"]}
        check(added_new_ids == want_added, "added 的新段 id 不对：%s vs %s"
              % (sorted(added_new_ids), sorted(want_added)))
        for c in by_kind(vd, "added"):
            check(c.old_segment_id is None and c.old_page is None and c.ratio == 0.0,
                  "added 不应有旧段：%+r" % (c,))

        # --- 跨页移动：p2 → p4
        moved = by_kind(vd, "moved")
        check(len(moved) == 1, "moved 应恰好 1 条，实际 %d" % len(moved))
        check(moved[0].old_segment_id == ids1[16] and moved[0].new_segment_id == ids2[tags["moved"][0]],
              "moved 的段 id 不对")
        check(moved[0].old_page == 2 and moved[0].new_page == 4,
              "moved 页号不对：%s → %s" % (moved[0].old_page, moved[0].new_page))

        # --- 同页重排：交换 23/24 → 恰好 1 条 reordered（向前跳的那一段）
        reordered = by_kind(vd, "reordered")
        check(len(reordered) == 1, "reordered 应恰好 1 条，实际 %d" % len(reordered))
        check(reordered[0].old_segment_id == ids1[24],
              "reordered 应标记向前跳的 24 号段，实际 %s" % reordered[0].old_segment_id)
        check(reordered[0].old_page == reordered[0].new_page == 3,
              "reordered 必须同页：%s → %s" % (reordered[0].old_page, reordered[0].new_page))
        # 被重排的另一段仍是 unchanged（相对次序未变）
        partner = change_by_old(vd, ids1[23])
        check(partner is not None and partner.kind == "unchanged",
              "重排的另一段应是 unchanged，实际 %s" % (partner.kind if partner else "None"))

        # --- 1 拆 2
        split = by_kind(vd, "split")
        check(len(split) == 1, "split 组应 1 个，实际 %d" % len(split))
        sp = split[0]
        check(sp.old_segment_id == ids1[30], "split 旧段 id 不对：%s" % sp.old_segment_id)
        check(approx(sp.ratio, 1.0, 1e-6), "拆分段 A+B 与原文应完全相同，ratio=%.6f" % sp.ratio)
        check(apply_char_diffs(sp.char_diffs, "old") == normalize(V1_TEXTS[30]),
              "split char_diffs 还原旧文失败")
        check(apply_char_diffs(sp.char_diffs, "new") == normalize(SPLIT_A + " " + SPLIT_B),
              "split char_diffs 还原新文失败")
        # 新段的热区标记：拆出来的两段都能查到 change
        cbs = changes_by_segment(conn, env.doc_id, v1, v2)
        check(cbs.get(ids2[tags["split-a"][0]]) is not None
              and cbs.get(ids2[tags["split-b"][0]]) is not None,
              "拆出的两段都应能通过 changes_by_segment 查到")

        # --- 2 并 1
        merged = by_kind(vd, "merged")
        check(len(merged) == 1, "merged 组应 1 个，实际 %d" % len(merged))
        mg = merged[0]
        check(mg.new_segment_id == ids2[tags["merged"][0]], "merged 新段 id 不对")
        check(getattr(mg, "meta", {}).get("group_old_ids") == [ids1[36], ids1[37]],
              "merged 组应包含 36/37 两段：%r" % getattr(mg, "meta", {}))
        check(approx(mg.ratio, 1.0, 1e-6), "合并段与两旧段拼接应完全相同，ratio=%.6f" % mg.ratio)

        # --- 顺序稳定：带新段 id 的变更按新版顺序递增
        seq_new = [c.new_segment_id for c in vd.changes if c.new_segment_id is not None]
        check(seq_new == sorted(seq_new), "变更顺序未按新版段落顺序排列：%s" % seq_new)
        check(len({c.new_segment_id for c in vd.changes if c.new_segment_id}) == len(seq_new),
              "同一新段被重复计入多条变更")

        # --- counts 与 changes 列表一致
        for kind in ("unchanged", "modified", "added", "removed", "moved", "reordered"):
            check(counts[kind] == len(by_kind(vd, kind)),
                  "counts[%s]=%d 与 changes 中 %d 条不一致"
                  % (kind, counts[kind], len(by_kind(vd, kind))))

        print("  [main] counts = %s" % json.dumps(
            {k: counts[k] for k in ("unchanged", "modified", "added", "removed",
                                    "moved", "reordered", "split", "merged")},
            ensure_ascii=False))
        print("  [main] ratios = micro %.3f / big %.3f / moderate %.3f"
              % (micro.ratio, big.ratio, moderate.ratio))
        print("  [main] diff 耗时 %.3fs" % elapsed)
        return env, v1, ids1, v2, ids2, tags, vd
    except Exception:
        env.close()
        raise


# ---------------------------------------------------------------- 测试 2：幂等

def check_idempotency_impl(env, v1, ids1, v2, vd):
    conn = env.conn

    def sig(d):
        return [(c.kind, c.old_segment_id, c.new_segment_id, round(float(c.ratio), 9),
                 c.old_page, c.new_page, c.summary,
                 json.dumps(c.char_diffs, ensure_ascii=False, sort_keys=True))
                for c in d.changes]

    vd2 = diff_versions(conn, env.doc_id, v1, v2)
    check(sig(vd2) == sig(vd), "二次调用 changes 不一致")
    check(vd2.counts == vd.counts, "二次调用 counts 不一致")

    vd3 = diff_versions(conn, env.doc_id, v1, v2)
    check(sig(vd3) == sig(vd), "三次调用 changes 不一致")

    # version_diffs 只应有一行
    cols = [r[1] for r in conn.execute("PRAGMA table_info(version_diffs)").fetchall()]
    if {"doc_id", "from_version_id", "to_version_id"}.issubset(set(cols)):
        n = conn.execute("SELECT COUNT(*) FROM version_diffs WHERE doc_id=? AND from_version_id=? "
                         "AND to_version_id=?", (env.doc_id, v1, v2)).fetchone()[0]
        check(int(n) == 1, "version_diffs 应有 1 行，实际 %d" % n)
        rows = conn.execute("SELECT * FROM version_diffs WHERE doc_id=? AND from_version_id=? "
                            "AND to_version_id=?", (env.doc_id, v1, v2)).fetchall()
        row = rows[0]
        raw_counts = None
        for name in ("counts", "counts_json"):
            if name in cols:
                raw_counts = row[name]
                break
        check(raw_counts is not None, "version_diffs 缺 counts 列：%s" % cols)
        check(json.loads(raw_counts)["modified"] == 3, "落库 counts 不对")
    else:
        print("  [idempotency] SKIP version_diffs 行数断言（缺列：%s）" % cols)

    # segment_links 覆盖每个新版 body 段，且不重复
    links = store.links_for_version(conn, v2)
    check(len(links) == vd.counts["new_total"],
          "segment_links 应恰好 %d 条，实际 %d" % (vd.counts["new_total"], len(links)))
    kinds = defaultdict(int)
    for _nid, row in links.items():
        kinds[row.get("change_kind")] += 1
    check(kinds["added"] == 3 and kinds["removed"] == 0,
          "segment_links 的 change_kind 分布不对：%s" % dict(kinds))
    check(kinds["split"] == 2 and kinds["merged"] == 1,
          "split/merged 链接数不对：%s" % dict(kinds))
    # 逐条抽样核对
    for c in vd.changes:
        if c.kind == "modified" and c.new_segment_id:
            row = links.get(c.new_segment_id)
            check(row is not None and row.get("old_segment_id") == c.old_segment_id
                  and row.get("change_kind") == "modified",
                  "modified 链接不对：%s" % (row,))
            break
    # REMOVED 也必须写入链接（new_segment_id 为 NULL），否则 core 的 links_for_diff /
    # diff_summary 统计不到删除；logs 行数 = 新版段数 + 删除段数
    if hasattr(store, "links_for_diff"):
        all_links = store.links_for_diff(conn, v1, v2)
        check(len(all_links) == vd.counts["new_total"] + vd.counts["removed"],
              "links_for_diff 行数应为 %d，实际 %d"
              % (vd.counts["new_total"] + vd.counts["removed"], len(all_links)))
        removed_rows = [l for l in all_links if l.get("change_kind") == "removed"]
        check(len(removed_rows) == 2
              and {l.get("old_segment_id") for l in removed_rows} == {ids1[12], ids1[13]},
              "REMOVED 链接不对：%s" % removed_rows)
    if hasattr(store, "diff_summary"):
        summ = store.diff_summary(conn, env.doc_id, v1, v2)["counts"]
        check(summ.get("removed") == 2 and summ.get("modified") == 3 and summ.get("added") == 3
              and summ.get("moved") == 1 and summ.get("reordered") == 1,
              "store.diff_summary 与差异结果不一致：%s" % summ)
    print("  [idempotency] links=%d kind 分布=%s" % (len(links), dict(kinds)))


# ---------------------------------------------------------------- 测试 3：辅助 API

def check_helpers(env, v1, ids1, v2, ids2, vd):
    conn = env.conn
    cbs = changes_by_segment(conn, env.doc_id, v1, v2)
    all_new = set(ids2)
    check(set(cbs.keys()) == all_new,
          "changes_by_segment 应覆盖全部新版段：缺 %s，多 %s"
          % (sorted(all_new - set(cbs)), sorted(set(cbs) - all_new)))
    check(cbs[ids2[0]].kind == "unchanged", "第一段应 unchanged")

    rbs = removed_by_segment(conn, env.doc_id, v1, v2)
    check(set(rbs.keys()) == {ids1[12], ids1[13], ids1[30], ids1[36], ids1[37]},
          "removed_by_segment 键不对：%s" % sorted(rbs.keys()))
    check(rbs[ids1[12]].kind == "removed", "12 号段应为 removed")
    check(rbs[ids1[30]].kind == "split", "30 号段应为 split")
    check(rbs[ids1[37]].kind == "merged", "37 号段应归入 merged 组")

    txt = diff_summary_text(vd)
    check(isinstance(txt, str) and "| 变更类型 | 数量 | 占比 |" in txt, "diff_summary_text 缺表格头")
    for kind in ("unchanged", "modified", "added", "removed", "moved", "reordered", "split", "merged"):
        check("`%s`" % kind in txt, "diff_summary_text 缺 %s 行" % kind)
    check("| 修改 `modified` | 3 |" in txt, "diff_summary_text 的 modified 计数不对")
    print("  [helpers] changes_by_segment=%d removed_by_segment=%d summary 行数=%d"
          % (len(cbs), len(rbs), len(txt.splitlines())))


# ---------------------------------------------------------------- 测试 4：canonical_text

def test_canonical_text():
    env = Env("canon")
    try:
        a = "The MASTER CAUTION light activates shortly after any individual light on the Cau-"
        b = "tion Panel illuminates. It does not activate simultaneously with the warning lights."
        canon = ("The MASTER CAUTION light activates shortly after any individual light on the "
                 "Caution Panel illuminates. It does not activate simultaneously with the warning lights.")
        rows1 = [
            (1, a + " " + b, canon),
            (1, "The landing gear handle is located on the left side of the instrument panel."),
            (1, "Fuel quantity is displayed on the forward instrument panel when the selector is set."),
        ]
        # v2 只是重新导出导致的断行不同：text 变了，canonical_text 没变
        rows2 = [
            (1, "The MASTER CAUTION light activates shortly after any individual light on the "
                "Caution Panel illuminates. It does not activate simultaneously with the warning "
                "lights.", canon),
            (1, "The landing gear handle is located on the left side of the instrument panel."),
            (1, "Fuel quantity is displayed on the forward instrument panel when the selector is set."),
        ]
        v1, ids1 = env.add_version("v1", rows1, page_count=1)
        v2, ids2 = env.add_version("v2", rows2, page_count=1)
        vd = diff_versions(env.conn, env.doc_id, v1, v2)
        check(vd.counts["modified"] == 0, "canonical_text 相同不应判 modified：%s" % vd.counts)
        check(vd.counts["added"] == 0 and vd.counts["removed"] == 0,
              "canonical_text 相同不应产生 add/remove：%s" % vd.counts)
        check(vd.counts["unchanged"] == 3, "应全部 unchanged，实际 %s" % vd.counts)
        # 反向验证：canonical 真的改了就要判 modified
        changed = canon.replace("does not", "never")
        rows3 = [
            (1, changed, changed),
            (1, "The landing gear handle is located on the left side of the instrument panel."),
            (1, "Fuel quantity is displayed on the forward instrument panel when the selector is set."),
        ]
        v3, _ = env.add_version("v3", rows3, page_count=1)
        vd2 = diff_versions(env.conn, env.doc_id, v1, v3)
        check(vd2.counts["modified"] == 1, "canonical 改动应判 1 条 modified：%s" % vd2.counts)
        print("  [canonical] 断行重导出 -> unchanged=%d modified=%d；真实改写 -> modified=%d"
              % (vd.counts["unchanged"], vd.counts["modified"], vd2.counts["modified"]))
    finally:
        env.close()


# ---------------------------------------------------------------- 测试 5：边界

def test_edges():
    env = Env("edge")
    try:
        rows = [(1 + i // 5, "Edge paragraph number %d describing a unique system detail." % i)
                for i in range(10)]
        v1, ids1 = env.add_version("v1", rows, page_count=2)
        v_same, _ = env.add_version("copy", rows, page_count=2)
        vd = diff_versions(env.conn, env.doc_id, v1, v_same)
        check(vd.counts["unchanged"] == 10 and len(vd.changes) == 10, "同内容两版应全 unchanged")
        check(vd.counts["added"] == 0 and vd.counts["removed"] == 0 and vd.counts["modified"] == 0,
              "同内容两版不应有变更")

        # role != body 的段不参与 diff（CONTRACT §8 步骤 1）
        rows_h = [(1, "BMS TRAINING MANUAL", "BMS TRAINING MANUAL"),
                  (2, "4.38.1", "4.38.1")]
        v_h, _ = env.add_version("headers", [
            {"page": 1, "text": "BMS TRAINING MANUAL", "role": "header"},
            {"page": 2, "text": "4.38.1", "role": "footer"},
        ], page_count=2)
        v_h2, _ = env.add_version("headers2", [
            {"page": 1, "text": "BMS TRAINING MANUAL CHANGED", "role": "header"},
            {"page": 2, "text": "4.38.2", "role": "footer"},
        ], page_count=2)
        vd_h = diff_versions(env.conn, env.doc_id, v_h, v_h2)
        check(vd_h.counts["old_total"] == 0 and vd_h.counts["new_total"] == 0,
              "非 body 段不应进入 diff：%s" % vd_h.counts)
        check(len(vd_h.changes) == 0, "非 body 段不应产生变更")

        # 空版本 vs 有段版本
        v_empty, _ = env.add_version("empty", [], page_count=0)
        vd_e = diff_versions(env.conn, env.doc_id, v_empty, v1)
        check(vd_e.counts["added"] == 10 and vd_e.counts["removed"] == 0,
              "空版本 -> 有段应全 added：%s" % vd_e.counts)
        vd_e2 = diff_versions(env.conn, env.doc_id, v1, v_empty)
        check(vd_e2.counts["removed"] == 10 and vd_e2.counts["added"] == 0,
              "有段 -> 空版本应全 removed：%s" % vd_e2.counts)
        print("  [edges] 同内容两版 unchanged=10；非 body 段 old/new_total=0；空版本 add/remove 正确")
    finally:
        env.close()


# ---------------------------------------------------------------- 测试 7：审查式探针

def test_probes():
    """task-7 会做的对抗式探针：只改一个词、重复文本（同 fingerprint 多份）。"""
    env = Env("probes")
    try:
        base = [
            (1, "The aircraft must be parked with the wheel chocks installed before maintenance."),
            (1, "Hydraulic pressure is supplied by the engine driven pump during normal operation."),
            (1, "The canopy lock handle must be checked before ground crew removal of the seat pin."),
        ]
        v1, ids1 = env.add_version("p1", base, page_count=1)
        # 只改一个词：installed -> removed
        one_word = [(p, t.replace("installed", "removed")) for i, (p, t) in enumerate(base)]
        check(one_word[0][1] != base[0][1] and one_word[1][1] == base[1][1], "单词改动构造失败")
        v2, _ = env.add_version("p2", one_word, page_count=1)
        vd = diff_versions(env.conn, env.doc_id, v1, v2)
        check(vd.counts["modified"] == 1, "只改一个词应判 1 条 modified，实际 %s" % vd.counts)
        check(vd.counts["added"] == 0 and vd.counts["removed"] == 0,
              "只改一个词不应出现 add+remove：%s" % vd.counts)
        ch = by_kind(vd, "modified")[0]
        check(ch.old_segment_id == ids1[0] and ch.ratio > 0.8,
              "单词改动的 ratio 应 > 0.8，实际 %.3f" % ch.ratio)
        check(apply_char_diffs(ch.char_diffs, "old") == normalize(base[0][1])
              and apply_char_diffs(ch.char_diffs, "new") == normalize(one_word[0][1]),
              "单词改动的 char_diffs 还原失败")
        print("  [probes] 单词改动 -> %s ratio=%.3f" % (
            {k: vd.counts[k] for k in ("unchanged", "modified", "added", "removed")}, ch.ratio))

        # 重复文本：8 段里 4 段是同一句 caption（同 fingerprint），删掉中间一份
        cap = "1.2.3 MASTER CAUTION panel layout."
        dup = []
        for i in range(8):
            if i % 2 == 0:
                dup.append((1, cap))
            else:
                dup.append((1, "Unique paragraph %d describing a separate aircraft system detail." % i))
        w1, w_ids1 = env.add_version("dup1", dup, page_count=1)
        dup2 = [row for i, row in enumerate(dup) if i != 4]
        w2, w_ids2 = env.add_version("dup2", dup2, page_count=1)
        vd2 = diff_versions(env.conn, env.doc_id, w1, w2)
        check(vd2.counts["removed"] == 1 and vd2.counts["added"] == 0,
              "重复文本删一份应判 1 条 removed：%s" % vd2.counts)
        check(vd2.counts["unchanged"] == 7 and vd2.counts["modified"] == 0
              and vd2.counts["moved"] == 0 and vd2.counts["reordered"] == 0,
              "重复文本删除后其余段应保持 unchanged：%s" % vd2.counts)
        check(len(store.links_for_version(env.conn, w2)) == 7, "重复文本链接数不对")
        print("  [probes] 重复 caption 删 1 份 -> %s"
              % {k: vd2.counts[k] for k in ("unchanged", "removed", "added")})
    finally:
        env.close()


# ---------------------------------------------------------------- 测试 6：性能

PERF_WORDS = ("flight control system hydraulic pressure landing gear canopy throttle engine "
              "warning caution indicator panel switch valve pump sensor display altitude airspeed "
              "heading attitude navigation alignment checklist procedure inspection").split()


def perf_rows(n: int):
    rows = []
    for i in range(n):
        page = 1 + i // 40
        body = " ".join(PERF_WORDS[(i * 7 + k * 11) % len(PERF_WORDS)] for k in range(18))
        rows.append((page, "Paragraph %d covers the %s sequence in detail." % (i, body)))
    return rows


def perf_modify(text: str) -> str:
    words = text.split()
    for k in (3, 11):
        if k < len(words):
            words[k] = "REVISED" if words[k] != "REVISED" else "AMENDED"
    return " ".join(words)


def test_perf(n: int = 4000):
    env = Env("perf")
    try:
        base = perf_rows(n)
        v1, ids1 = env.add_version("v1", base, page_count=(n // 40) + 1)

        removed = {501, 1001, 1501, 2001, 2501}          # 删 5 段（分散）
        insert_at = {700, 1400, 2100, 2800, 3500}        # 插 5 段（分散）
        moved = {810: 30, 3010: 90}                      # 跨页移动 2 段（810: p21->p30, 3010: p76->p90）
        v2_rows = []
        for i, (page, text) in enumerate(base):
            if i in removed or i in moved:
                continue
            new_text = perf_modify(text) if i % 200 == 0 else text
            v2_rows.append((page, new_text))
            if i in insert_at:
                v2_rows.append((page, "Inserted paragraph %d about avionics cooling airflow "
                                      "routing through the equipment bay louver." % i))
        for i, page in moved.items():
            v2_rows.append((page, base[i][1]))
        v2, ids2 = env.add_version("v2", v2_rows, page_count=(n // 40) + 1)
        check(len(v2_rows) == n - len(removed) + len(insert_at), "v2 段数不对：%d" % len(v2_rows))
        check(len(moved) == 2 and all(base[i][0] != p for i, p in moved.items()),
              "移动段必须跨页")

        t0 = time.perf_counter()
        vd = diff_versions(env.conn, env.doc_id, v1, v2)
        elapsed = time.perf_counter() - t0
        c = vd.counts
        check(c["modified"] == n // 200, "modified 应 %d，实际 %d" % (n // 200, c["modified"]))
        check(c["removed"] == len(removed), "removed 应 %d，实际 %d" % (len(removed), c["removed"]))
        check(c["added"] == len(insert_at), "added 应 %d，实际 %d" % (len(insert_at), c["added"]))
        check(c["moved"] == len(moved), "moved 应 %d，实际 %d" % (len(moved), c["moved"]))
        check(c["split"] == 0 and c["merged"] == 0, "性能集不应有 split/merged")
        check(c["unchanged"] == c["old_total"] - c["modified"] - c["removed"] - c["moved"],
              "unchanged 数不对：%s" % c)
        check(elapsed < 3.0, "4000 段 diff 耗时 %.2fs，超过 3 秒硬指标" % elapsed)
        links = store.links_for_version(env.conn, v2)
        check(len(links) == vd.counts["new_total"],
              "大批量 links 覆盖不全：%d/%d" % (len(links), vd.counts["new_total"]))

        # 同 pair 复用：第二次调用必须命中缓存且结果一致
        t1 = time.perf_counter()
        vd_cached = diff_versions(env.conn, env.doc_id, v1, v2)
        t_cached = time.perf_counter() - t1
        check(vd_cached.counts == vd.counts, "缓存复用的 counts 不一致")
        check([(c.kind, c.old_segment_id, c.new_segment_id) for c in vd_cached.changes]
              == [(c.kind, c.old_segment_id, c.new_segment_id) for c in vd.changes],
              "缓存复用的 changes 不一致")
        check(t_cached < max(elapsed, 1e-3), "缓存复用应比首次计算更快：%.3fs vs %.3fs"
              % (t_cached, elapsed))
        cols = [r[1] for r in env.conn.execute("PRAGMA table_info(version_diffs)").fetchall()]
        if "changes" in cols:
            raw = env.conn.execute(
                "SELECT changes FROM version_diffs WHERE doc_id=? AND from_version_id=? "
                "AND to_version_id=?", (env.doc_id, v1, v2)).fetchone()[0]
            data = json.loads(raw)
            stored_changes = data.get("changes") if isinstance(data, dict) else data
            check(isinstance(stored_changes, list) and len(stored_changes) == len(vd.changes),
                  "version_diffs 缓存的变更集条数不对")
        print("  [perf] %d 段 v1 -> %d 段 v2：diff 耗时 %.3fs（预算 3.000s）复用耗时 %.4fs counts=%s"
              % (len(base), len(v2_rows), elapsed, t_cached,
                 {k: c[k] for k in ("unchanged", "modified", "added", "removed", "moved")}))
        return elapsed
    finally:
        env.close()


# ---------------------------------------------------------------- runner

def run(name, fn, *args):
    t0 = time.perf_counter()
    try:
        fn(*args)
    except Exception as exc:                      # noqa: BLE001
        FAILURES.append("%s: %s" % (name, exc))
        print("[FAIL] %s: %s" % (name, exc))
        traceback.print_exc()
    else:
        print("[ OK ] %s (%.2fs)" % (name, time.perf_counter() - t0))


def main() -> int:
    print("=" * 78)
    print("test_versions.py — versions/differ.py 验收测试")
    print("核心库：core = %s" % getattr(sys.modules.get("core"), "__file__", "?"))
    print("=" * 78)

    state = {}

    def t_main():
        env, v1, ids1, v2, ids2, tags, vd = test_main_scenario()
        state.update(env=env, v1=v1, ids1=ids1, v2=v2, ids2=ids2, tags=tags, vd=vd)

    run("1 主场景（计数/分类/ratio/char_diffs）", t_main)
    if "vd" in state:
        run("2 幂等（重复调用 + 落库 + 链接）", check_idempotency_impl,
            state["env"], state["v1"], state["ids1"], state["v2"], state["vd"])
        run("3 辅助 API（changes_by_segment / removed_by_segment / summary）",
            check_helpers, state["env"], state["v1"], state["ids1"], state["v2"],
            state["ids2"], state["vd"])
    else:
        FAILURES.append("主场景失败，后续依赖测试跳过")
        print("[SKIP] 2/3 因主场景失败而跳过")
    run("4 canonical_text（断行/连字符不应误判）", test_canonical_text)
    run("5 边界（同内容 / 非 body / 空版本）", test_edges)
    run("6 性能（4000 段 < 3s + 复用）", test_perf)
    run("7 审查探针（只改一个词 / 重复文本）", test_probes)

    env = state.get("env")
    if env is not None:
        env.close()

    print("-" * 78)
    print("断言数：%d；失败：%d" % (CHECKS[0], len(FAILURES)))
    for f in FAILURES:
        print("  - %s" % f)
    if FAILURES:
        print("结果：FAILED")
        return 1
    print("结果：PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
