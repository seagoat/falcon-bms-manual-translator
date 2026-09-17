# -*- coding: utf-8 -*-
"""versions/differ.py — 跨版本段匹配与变更集（CONTRACT.md §2 / §4.1 / §8）。

对外 API
--------
    diff_versions(conn, doc_id, from_version_id, to_version_id) -> VersionDiff
    diff_summary_text(vd) -> str
    changes_by_segment(conn, doc_id, from_version_id, to_version_id) -> dict[int, Change]
    removed_by_segment(conn, doc_id, from_version_id, to_version_id) -> dict[int, Change]

算法（严格按 §8，外加两处已文档化的扩展，见 versions/_design.md）：
    1. 取两侧 role=="body" 的段（order_index 升序）。
    2. fingerprint 精确锚点匹配（同 key 一一配对，取位置最接近者）。
    3. 未匹配段按 normalize(canonical_text) 序列做 SequenceMatcher.get_opcodes()。
    4. 非 equal 的连续 opcode 合并成块；块内贪心模糊配对 ratio >= 0.55。
    5. 块内 1:N -> SPLIT、N:1 -> MERGED（连带配对段的相邻自由段扩展）。
    6. 残余自由段用更保守的三重条件做第二次配对（避免「整段重写」被误报 REMOVED+ADDED）。
    7. 配对段判 MOVED（跨页）/ REORDERED（同页相对次序变化）。
    8. 写 store.record_segment_link，char_diffs 用 core.fingerprint.char_diffs。
    9. 结果落库 version_diffs，同 pair 重复调用直接复用（幂等）。

**比对基准是 `canonical_text`（CONTRACT §3.1.1 / §4.1）**：行尾软连字符已复原，
这样两版之间仅仅重新断行不会造成伪变更。
"""
from __future__ import annotations

import bisect
import difflib
import json
import re
import sqlite3
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any, Optional, Sequence

from core import store
from core.fingerprint import char_diffs as _fp_char_diffs
from core.fingerprint import fingerprint as _fp_fingerprint
from core.fingerprint import normalize as _fp_normalize
from core.fingerprint import ratio as _fp_ratio
from core.models import Change, VersionDiff

__all__ = [
    "diff_versions",
    "diff_summary_text",
    "changes_by_segment",
    "removed_by_segment",
]

# ---------------------------------------------------------------- 常量

DIFFER_REV = 1                 # 落库 payload 版本号；改动算法后自动失效缓存

BODY_ROLE = "body"             # §8 步骤 1：只 diff 正文段

MIN_PAIR_RATIO = 0.55          # §8 步骤 4：replace 块内模糊配对阈值
LEN_PREFILTER = 0.34           # 配对前的长度粗筛（min/max）

RESIDUAL_PAIR_RATIO = 0.30     # 扩展 1：残余低相似度配对（见 _design.md §4）
RESIDUAL_MIN_LEN_RATIO = 0.50
RESIDUAL_MIN_TOKEN_CONTAINMENT = 0.30
RESIDUAL_MIN_TOKENS = 3

GROUP_MIN_RATIO = 0.55         # §8 步骤 5：SPLIT / MERGED 组的相似度下限
GROUP_EXT_GAIN = 0.03          # 组扩展必须比原配对明显更像
GROUP_MAX_SIZE = 4             # 单个 SPLIT/MERGED 组最多段数
GROUP_CAND_BUDGET = 20000      # 单块组候选上限（保证最坏情况有界）
GROUP_SCAN_LIMIT = 600         # 自由段总数超过此值时跳过纯自由段组合枚举

MAX_BLOCK_PRODUCT = 120_000    # 块内两两比较上限；超出则按位置开窗
ANCHOR_LCS_MAX_WORK = 2_000_000  # 锚点全局单调匹配的工作量上限（重复 fp 过多时退回贪心就近）

BULK_LINK_THRESHOLD = 200      # links 数量超过此值时走单事务批量写入（性能硬指标）

CACHE_TABLE = "version_diffs"

_SORT_KIND_RANK = {
    "added": 0, "modified": 1, "unchanged": 2, "moved": 3,
    "reordered": 4, "split": 5, "merged": 6, "removed": 7,
}
_KIND_LABELS = {
    "unchanged": "未变更", "modified": "修改", "added": "新增", "removed": "删除",
    "moved": "移动（跨页）", "reordered": "重排（同页）", "split": "拆分", "merged": "合并",
}

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = frozenset("""
the and for that with from this these those are was were has have had not but you your its into out over
under then than when while will shall can may must should could would about after before being been done
only such same each other more most some any all any who whom which what there their they them his her
""".split())


# ---------------------------------------------------------------- 小工具

def _canon(seg: dict) -> str:
    """比对用文本：canonical_text（CONTRACT §3.1.1）；缺失时退化为 text。"""
    text = seg.get("canonical_text")
    if text is None or text == "":
        text = seg.get("text") or ""
    return text


def _sid(seg: dict) -> int:
    return int(seg.get("id") or 0)


def _page(seg: dict) -> int:
    return int(seg.get("page") or 0)


def _order(seg: dict) -> int:
    return int(seg.get("order_index") or 0)


def _len_ratio(a: str, b: str) -> float:
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 1.0 if la == lb else 0.0
    return min(la, lb) / max(la, lb)


def _content_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").casefold()) if t not in _STOPWORDS}


def _token_containment(a: str, b: str) -> float:
    ta, tb = _content_tokens(a), _content_tokens(b)
    if len(ta) < RESIDUAL_MIN_TOKENS or len(tb) < RESIDUAL_MIN_TOKENS:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _join_texts(texts: Sequence[str], run: Sequence[int]) -> str:
    return " ".join(texts[k] for k in run)


def _align_pos(rank: int, n_src: int, n_dst: int) -> int:
    """把一侧块内序号按比例映射到另一侧，用于自由段的组合枚举定位。"""
    if n_src <= 1 or n_dst <= 1:
        return 0
    return int(round(rank * (n_dst - 1) / (n_src - 1)))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_get(row: Any, name: str) -> Any:
    try:
        return row[name]
    except Exception:
        return None


def _columns(conn, table: str) -> list[str]:
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception:
        return []
    out: list[str] = []
    for r in rows:
        name = _row_get(r, "name")
        if name is None:
            try:
                name = r[1]
            except Exception:
                name = None
        if name:
            out.append(str(name))
    return out


# ---------------------------------------------------------------- 块内配对

def _pair_candidates(
    ids_o: Sequence[int],
    ids_n: Sequence[int],
    ocanon: Sequence[str],
    ncanon: Sequence[str],
    min_ratio: float,
    strict: bool,
) -> list[tuple[float, int, int, int, int]]:
    """返回按 (ratio 降序, 位置距离升序, 索引) 排好序的候选配对。

    strict=True 时使用残余配对的保守条件（长度比 + 内容词包含度）。
    """
    cands: list[tuple[float, int, int, int, int]] = []
    n_o, n_n = len(ids_o), len(ids_n)
    if n_o == 0 or n_n == 0:
        return cands
    window = None
    if n_o * n_n > MAX_BLOCK_PRODUCT:
        window = max(4, MAX_BLOCK_PRODUCT // max(n_o, n_n))
    min_len_ratio = RESIDUAL_MIN_LEN_RATIO if strict else LEN_PREFILTER
    for ai, i in enumerate(ids_o):
        ao = ocanon[i]
        lo, hi = 0, n_n
        if window is not None:
            lo = max(0, ai - window)
            hi = min(n_n, ai + window + 1)
        for aj in range(lo, hi):
            j = ids_n[aj]
            an = ncanon[j]
            if _len_ratio(ao, an) < min_len_ratio:
                continue
            if strict and _token_containment(ao, an) < RESIDUAL_MIN_TOKEN_CONTAINMENT:
                continue
            r = _fp_ratio(ao, an)
            if r < min_ratio:
                continue
            cands.append((r, abs(ai - aj), ai, aj, i, j))
    cands.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    return [(t[0], t[2], t[3], t[4], t[5]) for t in cands]


def _windows_near(
    all_idx: Sequence[int],
    free_set: set[int],
    pos: int,
    max_size: int = GROUP_MAX_SIZE,
    back: int = 2,
    anchor: Optional[int] = None,
) -> list[tuple[int, int]]:
    """枚举 [a, b]（块内下标闭区间）的连续窗口：含 anchor 的（或全部自由的）窗口。"""
    n = len(all_idx)
    out: list[tuple[int, int]] = []
    if n == 0:
        return out
    for a in range(max(0, pos - back), min(pos, n - 1) + 1):
        for k in range(2, max_size + 1):
            b = a + k - 1
            if b >= n:
                break
            members = all_idx[a:b + 1]
            if anchor is None:
                if any(m not in free_set for m in members):
                    continue
            else:
                if anchor not in members:
                    continue
                if any((m not in free_set and m != anchor) for m in members):
                    continue
            out.append((a, b))
    return out


def _find_groups(
    blk_o: Sequence[int],
    blk_n: Sequence[int],
    ocanon: Sequence[str],
    ncanon: Sequence[str],
    pairs: Sequence[tuple[int, int, float]],
    used_o: dict[int, bool],
    used_n: dict[int, bool],
) -> list[dict]:
    """块内 SPLIT / MERGED 候选生成 + 贪心选择（返回候选 dict 列表）。"""
    rank_o = {i: k for k, i in enumerate(blk_o)}
    rank_n = {j: k for k, j in enumerate(blk_n)}
    free_o = [i for i in blk_o if not used_o[i]]
    free_n = [j for j in blk_n if not used_n[j]]
    if not free_o and not free_n:
        return []
    free_o_set, free_n_set = set(free_o), set(free_n)
    cands: list[dict] = []
    budget = GROUP_CAND_BUDGET

    def add(score: float, kind: str, old_ids, new_ids, anchor_old, anchor_new) -> None:
        cands.append({
            "score": round(float(score), 6),
            "kind": kind,
            "old_ids": tuple(old_ids),
            "new_ids": tuple(new_ids),
            "extra_old": tuple(x for x in old_ids if x != anchor_old),
            "extra_new": tuple(x for x in new_ids if x != anchor_new),
            "anchor_old": anchor_old,
            "anchor_new": anchor_new,
        })

    # (a) 配对段 + 相邻自由新段 -> SPLIT
    if free_n:
        for (i, j, r) in pairs:
            for (a, b) in _windows_near(blk_n, free_n_set, rank_n[j], back=3, anchor=j):
                if budget <= 0:
                    break
                if b - a + 1 < 2:
                    continue
                run = tuple(blk_n[a:b + 1])
                budget -= 1
                gr = _fp_ratio(ocanon[i], _join_texts(ncanon, run))
                if gr >= GROUP_MIN_RATIO and gr >= r + GROUP_EXT_GAIN:
                    add(gr, "split", (i,), run, i, j)
    # (b) 配对段 + 相邻自由旧段 -> MERGED
    if free_o:
        for (i, j, r) in pairs:
            for (a, b) in _windows_near(blk_o, free_o_set, rank_o[i], back=3, anchor=i):
                if budget <= 0:
                    break
                if b - a + 1 < 2:
                    continue
                run = tuple(blk_o[a:b + 1])
                budget -= 1
                gr = _fp_ratio(_join_texts(ocanon, run), ncanon[j])
                if gr >= GROUP_MIN_RATIO and gr >= r + GROUP_EXT_GAIN:
                    add(gr, "merged", run, (j,), i, j)
    # (c)(d) 纯自由段的组合（1 旧 -> N 新 / N 旧 -> 1 新）
    if budget > 0 and (len(free_o) + len(free_n)) <= GROUP_SCAN_LIMIT:
        for i in free_o:
            if budget <= 0:
                break
            p = _align_pos(rank_o[i], len(blk_o), len(blk_n))
            for (a, b) in _windows_near(blk_n, free_n_set, p, back=2, anchor=None):
                if budget <= 0:
                    break
                if b - a + 1 < 2:
                    continue
                run = tuple(blk_n[a:b + 1])
                budget -= 1
                gr = _fp_ratio(ocanon[i], _join_texts(ncanon, run))
                if gr >= GROUP_MIN_RATIO:
                    add(gr, "split", (i,), run, None, None)
        for j in free_n:
            if budget <= 0:
                break
            p = _align_pos(rank_n[j], len(blk_n), len(blk_o))
            for (a, b) in _windows_near(blk_o, free_o_set, p, back=2, anchor=None):
                if budget <= 0:
                    break
                if b - a + 1 < 2:
                    continue
                run = tuple(blk_o[a:b + 1])
                budget -= 1
                gr = _fp_ratio(_join_texts(ocanon, run), ncanon[j])
                if gr >= GROUP_MIN_RATIO:
                    add(gr, "merged", run, (j,), None, None)
    if not cands:
        return []
    kind_rank = {"split": 0, "merged": 1}
    cands.sort(key=lambda c: (-c["score"], kind_rank[c["kind"]], -len(c["new_ids"]),
                              c["old_ids"], c["new_ids"]))
    chosen: list[dict] = []
    claimed_o: set[int] = set()
    claimed_n: set[int] = set()
    claimed_anchor: set[tuple] = set()
    for c in cands:
        if any(i in claimed_o for i in c["extra_old"]):
            continue
        if any(j in claimed_n for j in c["extra_new"]):
            continue
        if c["anchor_new"] is not None and (c["anchor_old"], c["anchor_new"]) in claimed_anchor:
            continue
        claimed_o.update(c["extra_old"])
        claimed_n.update(c["extra_new"])
        if c["anchor_new"] is not None:
            claimed_anchor.add((c["anchor_old"], c["anchor_new"]))
        chosen.append(c)
    return chosen


def _process_block(
    blk_o: Sequence[int],
    blk_n: Sequence[int],
    ocanon: Sequence[str],
    ncanon: Sequence[str],
) -> tuple[list[tuple[int, int, float]], list[tuple[str, tuple, tuple, float]],
           list[int], list[int]]:
    """处理一个非 equal 块：贪心配对 -> SPLIT/MERGED -> 残余配对 -> 剩余 ADD/REMOVE。"""
    used_o = {i: False for i in blk_o}
    used_n = {j: False for j in blk_n}
    pairs: list[tuple[int, int, float]] = []

    # 1) §8 步骤 4：ratio >= 0.55 贪心配对
    for (r, _ai, _aj, i, j) in _pair_candidates(blk_o, blk_n, ocanon, ncanon, MIN_PAIR_RATIO, False):
        if used_o[i] or used_n[j]:
            continue
        used_o[i] = True
        used_n[j] = True
        pairs.append((i, j, r))

    # 2) §8 步骤 5：SPLIT / MERGED
    groups = _find_groups(blk_o, blk_n, ocanon, ncanon, pairs, used_o, used_n)
    if groups:
        absorbed = {c["anchor_new"] for c in groups if c["anchor_new"] is not None}
        if absorbed:
            pairs[:] = [p for p in pairs if p[1] not in absorbed]
        for c in groups:
            for i in c["old_ids"]:
                used_o[i] = True
            for j in c["new_ids"]:
                used_n[j] = True

    # 3) 扩展：残余低相似度配对
    rem_o = [i for i in blk_o if not used_o[i]]
    rem_n = [j for j in blk_n if not used_n[j]]
    if rem_o and rem_n:
        for (r, _ai, _aj, i, j) in _pair_candidates(rem_o, rem_n, ocanon, ncanon,
                                                    RESIDUAL_PAIR_RATIO, True):
            if used_o[i] or used_n[j]:
                continue
            used_o[i] = True
            used_n[j] = True
            pairs.append((i, j, r))

    removed = [i for i in blk_o if not used_o[i]]
    added = [j for j in blk_n if not used_n[j]]
    result_groups = [(c["kind"], c["old_ids"], c["new_ids"], c["score"]) for c in groups]
    return pairs, result_groups, removed, added


# ---------------------------------------------------------------- 主计算

def _body_segments(conn, version_id: int) -> list[dict]:
    segs = store.segments_of_version(conn, version_id)
    out: list[dict] = []
    for s in segs:
        role = (s.get("role") or BODY_ROLE)
        if role != BODY_ROLE:
            continue
        out.append(s)
    return out


def _anchor_pairs(ofp: Sequence[str], nfp: Sequence[str],
                  old_used: list[bool], new_used: list[bool]) -> list[tuple[int, int]]:
    """§8 步骤 2：fingerprint 精确锚点，同 key 一一配对，取位置最接近者。

    实现分两层：
      1. 先做**全局单调匹配**（difflib 的 matching blocks，等价于最长公共子序列）：
         同 fingerprint 的重复段（例如每页都出现的图注）不会交叉配对，
         否则重复段之间会互相错配并产生伪 REORDERED。
      2. 仍未配对的同 fp 段再按「位置最接近者」贪心补配——
         保证「同页两段交换」这种真实重排仍能被识别（LCS 只会匹配其中一段）。
    重复 fp 极多（work 超上限）时直接走第 2 层，避免 LCS 的二次开销。
    """
    pairs: list[tuple[int, int]] = []
    if ofp and nfp:
        cnt_o: dict[str, int] = defaultdict(int)
        cnt_n: dict[str, int] = defaultdict(int)
        for f in ofp:
            cnt_o[f] += 1
        for f in nfp:
            cnt_n[f] += 1
        work = 0
        for f, c in cnt_o.items():
            work += c * cnt_n.get(f, 0)
            if work > ANCHOR_LCS_MAX_WORK:
                break
        if work <= ANCHOR_LCS_MAX_WORK:
            matcher = difflib.SequenceMatcher(None, list(ofp), list(nfp), autojunk=False)
            for blk in matcher.get_matching_blocks():
                for k in range(blk.size):
                    i, j = blk.a + k, blk.b + k
                    if old_used[i] or new_used[j]:
                        continue
                    old_used[i] = True
                    new_used[j] = True
                    pairs.append((i, j))

    old_by_fp: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(ofp):
        if not old_used[i]:
            old_by_fp[f].append(i)
    for j, f in enumerate(nfp):
        if new_used[j]:
            continue
        cands = old_by_fp.get(f)
        if not cands:
            continue
        pos = bisect.bisect_left(cands, j)
        lo, hi = pos - 1, pos
        picked = None
        while lo >= 0 or hi < len(cands):
            dl = j - cands[lo] if lo >= 0 else None
            dr = cands[hi] - j if hi < len(cands) else None
            if dl is not None and (dr is None or dl <= dr):
                if not old_used[cands[lo]]:
                    picked = cands[lo]
                    break
                lo -= 1
            else:
                if not old_used[cands[hi]]:
                    picked = cands[hi]
                    break
                hi += 1
        if picked is None:
            continue
        old_used[picked] = True
        new_used[j] = True
        pairs.append((picked, j))
    pairs.sort()
    return pairs


def _compute(old: list[dict], new: list[dict], doc_id: int,
             from_version_id: int, to_version_id: int) -> tuple[VersionDiff, list[tuple]]:
    ocanon = [_canon(s) for s in old]
    ncanon = [_canon(s) for s in new]
    onorm = [_fp_normalize(c) for c in ocanon]
    nnorm = [_fp_normalize(c) for c in ncanon]
    ofp = [_fp_fingerprint(c) for c in ocanon]
    nfp = [_fp_fingerprint(c) for c in ncanon]

    old_used = [False] * len(old)
    new_used = [False] * len(new)

    # --- 2) 精确锚点
    all_pairs: list[tuple[int, int, float]] = []
    for (i, j) in _anchor_pairs(ofp, nfp, old_used, new_used):
        all_pairs.append((i, j, 1.0))

    # --- 3) 未匹配段顺序 diff
    o_un = [i for i in range(len(old)) if not old_used[i]]
    n_un = [j for j in range(len(new)) if not new_used[j]]
    opcodes = difflib.SequenceMatcher(
        None, [onorm[i] for i in o_un], [nnorm[j] for j in n_un], autojunk=False
    ).get_opcodes()

    blocks: list[tuple[list[int], list[int]]] = []
    cur_o: list[int] = []
    cur_n: list[int] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            if cur_o or cur_n:
                blocks.append((cur_o, cur_n))
                cur_o, cur_n = [], []
            for k in range(i2 - i1):
                i, j = o_un[i1 + k], n_un[j1 + k]
                if not old_used[i] and not new_used[j]:
                    old_used[i] = True
                    new_used[j] = True
                    all_pairs.append((i, j, 1.0))
        else:
            cur_o.extend(o_un[i1:i2])
            cur_n.extend(n_un[j1:j2])
    if cur_o or cur_n:
        blocks.append((cur_o, cur_n))

    # --- 4~5) 块内配对 / 分组 / 残余配对
    removed_idx: list[int] = []
    added_idx: list[int] = []
    split_groups: list[tuple[tuple, tuple, float]] = []
    merged_groups: list[tuple[tuple, tuple, float]] = []
    for (blk_o, blk_n) in blocks:
        bpairs, groups, rem, add = _process_block(blk_o, blk_n, ocanon, ncanon)
        all_pairs.extend(bpairs)
        removed_idx.extend(rem)
        added_idx.extend(add)
        for (kind, oids, nids, gr) in groups:
            if kind == "split":
                split_groups.append((tuple(oids), tuple(nids), gr))
            else:
                merged_groups.append((tuple(oids), tuple(nids), gr))

    # --- 6) 配对段分类：MODIFIED / MOVED / REORDERED / UNCHANGED
    simple: list[tuple[int, int, float, bool]] = []
    for (i, j, r) in all_pairs:
        same = (onorm[i] == nnorm[j])
        if same:
            simple.append((i, j, 1.0, True))
        else:
            simple.append((i, j, _fp_ratio(ocanon[i], ncanon[j]), False))

    page_groups: dict[int, list[int]] = defaultdict(list)
    for k, (i, j, _r, same) in enumerate(simple):
        if same and _page(old[i]) == _page(new[j]):
            page_groups[_page(old[i])].append(k)
    reordered: set[int] = set()
    for _page_no, ks in page_groups.items():
        if len(ks) < 2:
            continue
        by_old = sorted(ks, key=lambda k: (_order(old[simple[k][0]]), k))
        by_new = sorted(ks, key=lambda k: (_order(new[simple[k][1]]), k))
        rank_new = {k: pos for pos, k in enumerate(by_new)}
        for pos, k in enumerate(by_old):
            if rank_new[k] < pos:
                reordered.add(k)

    changes: list[tuple[tuple, Change]] = []
    links: list[tuple] = []
    pair_map: dict[int, int] = {}
    for (i, j, _r, _same) in simple:
        pair_map[i] = j

    for k, (i, j, r, same) in enumerate(simple):
        o, n = old[i], new[j]
        nid, oid = _sid(n), _sid(o)
        slot = float(j)
        if not same:
            cds = _fp_char_diffs(ocanon[i], ncanon[j])
            ch = Change(kind="modified", old_segment_id=oid, new_segment_id=nid, ratio=r,
                        char_diffs=cds, old_page=_page(o), new_page=_page(n),
                        summary="文本修改（相似度 %.2f）" % r)
            links.append((nid, oid, "modified", r, cds))
        elif _page(o) != _page(n):
            ch = Change(kind="moved", old_segment_id=oid, new_segment_id=nid, ratio=1.0,
                        old_page=_page(o), new_page=_page(n),
                        summary="跨页移动：第 %d 页 → 第 %d 页" % (_page(o), _page(n)))
            links.append((nid, oid, "moved", 1.0, None))
        elif k in reordered:
            ch = Change(kind="reordered", old_segment_id=oid, new_segment_id=nid, ratio=1.0,
                        old_page=_page(o), new_page=_page(n),
                        summary="同页顺序变化（第 %d 页）" % _page(o))
            links.append((nid, oid, "reordered", 1.0, None))
        else:
            ch = Change(kind="unchanged", old_segment_id=oid, new_segment_id=nid, ratio=1.0,
                        old_page=_page(o), new_page=_page(n), summary="")
            links.append((nid, oid, "unchanged", 1.0, None))
        changes.append(((slot, _SORT_KIND_RANK[ch.kind], _order(o), _order(n)), ch))

    # --- SPLIT 组
    split_old_total = split_new_total = 0
    for (oids, nids, gr) in split_groups:
        i = oids[0]
        o = old[i]
        nid = _sid(new[nids[0]])
        txt_new = _join_texts(ncanon, nids)
        cds = _fp_char_diffs(ocanon[i], txt_new)
        ch = Change(kind="split", old_segment_id=_sid(o), new_segment_id=nid, ratio=gr,
                    char_diffs=cds, old_page=_page(o), new_page=_page(new[nids[0]]),
                    summary="1 段拆为 %d 段（旧段 id %d）" % (len(nids), _sid(o)))
        ch.meta = {"group_old_ids": [_sid(o)], "group_new_ids": [_sid(new[j]) for j in nids]}
        changes.append(((float(nids[0]), _SORT_KIND_RANK["split"],
                         _order(o), _order(new[nids[0]])), ch))
        split_old_total += 1
        split_new_total += len(nids)
        for j in nids:
            links.append((_sid(new[j]), _sid(o), "split",
                          _fp_ratio(ocanon[i], ncanon[j]), _fp_char_diffs(ocanon[i], ncanon[j])))

    # --- MERGED 组
    merged_old_total = merged_new_total = 0
    for (oids, nids, gr) in merged_groups:
        j = nids[0]
        n = new[j]
        o = old[oids[0]]
        txt_old = _join_texts(ocanon, oids)
        cds = _fp_char_diffs(txt_old, ncanon[j])
        ch = Change(kind="merged", old_segment_id=_sid(o), new_segment_id=_sid(n), ratio=gr,
                    char_diffs=cds, old_page=_page(o), new_page=_page(n),
                    summary="%d 段合并为 1 段（旧段 id %s）" % (
                        len(oids), ",".join(str(_sid(old[i])) for i in oids)))
        ch.meta = {"group_old_ids": [_sid(old[i]) for i in oids], "group_new_ids": [_sid(n)]}
        changes.append(((float(j), _SORT_KIND_RANK["merged"],
                         _order(old[oids[0]]), _order(n)), ch))
        merged_old_total += len(oids)
        merged_new_total += 1
        links.append((_sid(n), _sid(o), "merged", gr, cds))

    # --- REMOVED / ADDED
    pair_old_sorted = sorted(pair_map.keys())

    def _removed_slot(i: int) -> float:
        p = bisect.bisect_right(pair_old_sorted, i)
        if p < len(pair_old_sorted):
            return float(pair_map[pair_old_sorted[p]]) - 0.5
        return float(len(new))

    for i in sorted(removed_idx):
        o = old[i]
        ch = Change(kind="removed", old_segment_id=_sid(o), new_segment_id=None, ratio=0.0,
                    old_page=_page(o), new_page=None, summary="删除段落")
        changes.append(((_removed_slot(i), _SORT_KIND_RANK["removed"], _order(o), -1), ch))
        links.append((None, _sid(o), "removed", 0.0, None))

    for j in sorted(added_idx):
        n = new[j]
        ch = Change(kind="added", old_segment_id=None, new_segment_id=_sid(n), ratio=0.0,
                    old_page=None, new_page=_page(n), summary="新增段落")
        changes.append(((float(j), _SORT_KIND_RANK["added"], -1, _order(n)), ch))
        links.append((_sid(n), None, "added", 0.0, None))

    changes.sort(key=lambda t: t[0])
    ordered = [ch for (_k, ch) in changes]

    counts = {
        "unchanged": sum(1 for c in ordered if c.kind == "unchanged"),
        "modified": sum(1 for c in ordered if c.kind == "modified"),
        "added": sum(1 for c in ordered if c.kind == "added"),
        "removed": sum(1 for c in ordered if c.kind == "removed"),
        "moved": sum(1 for c in ordered if c.kind == "moved"),
        "reordered": sum(1 for c in ordered if c.kind == "reordered"),
        "split": sum(1 for c in ordered if c.kind == "split"),
        "merged": sum(1 for c in ordered if c.kind == "merged"),
        # --- 扩展字段（供覆盖度校验 / CLI 摘要）
        "split_old": split_old_total,
        "split_new": split_new_total,
        "merged_old": merged_old_total,
        "merged_new": merged_new_total,
        "old_total": len(old),
        "new_total": len(new),
        "from_version_id": from_version_id,
        "to_version_id": to_version_id,
        "doc_id": doc_id,
    }
    vd = VersionDiff(doc_id=doc_id, from_version_id=from_version_id,
                     to_version_id=to_version_id, changes=ordered, counts=counts)
    return vd, links


# ---------------------------------------------------------------- 落库 / 缓存

def _change_to_dict(c: Change) -> dict:
    d = {
        "kind": c.kind,
        "old_segment_id": c.old_segment_id,
        "new_segment_id": c.new_segment_id,
        "ratio": c.ratio,
        "char_diffs": c.char_diffs or [],
        "old_page": c.old_page,
        "new_page": c.new_page,
        "summary": c.summary,
    }
    meta = getattr(c, "meta", None)
    if meta:
        d["meta"] = meta
    return d


def _change_from_dict(d: dict) -> Change:
    c = Change(
        kind=str(d.get("kind") or ""),
        old_segment_id=d.get("old_segment_id"),
        new_segment_id=d.get("new_segment_id"),
        ratio=float(d.get("ratio") or 0.0),
        char_diffs=list(d.get("char_diffs") or []),
        old_page=d.get("old_page"),
        new_page=d.get("new_page"),
        summary=str(d.get("summary") or ""),
    )
    if d.get("meta"):
        c.meta = dict(d["meta"])
    return c


def _col_pick(cols: Sequence[str], names: Sequence[str]) -> Optional[str]:
    for n in names:
        if n in cols:
            return n
    return None


_DB_KEY_CACHE: dict[int, str] = {}
_CHANGES_COL_CACHE: dict[str, bool] = {}


def _db_key(conn) -> str:
    cid = id(conn)
    key = _DB_KEY_CACHE.get(cid)
    if key is not None:
        return key
    key = "?"
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if _row_get(row, "name") == "main":
                key = str(_row_get(row, "file") or ":memory:")
                break
    except sqlite3.Error:
        pass
    _DB_KEY_CACHE[cid] = key
    return key


def _ensure_changes_column(conn) -> bool:
    """core 的 version_diffs 只有 counts 列；惰性补一列 changes 以便同 pair 直接复用完整变更集。

    纯增量、可重复执行；旧库/新库都能跑。失败时返回 False，调用方退回进程内记忆。
    """
    key = _db_key(conn)
    known = _CHANGES_COL_CACHE.get(key)
    if known is not None:
        return known
    cols = _columns(conn, CACHE_TABLE)
    ok = False
    if cols:
        if _col_pick(cols, ("changes", "changes_json", "payload", "data")) is not None:
            ok = True
        else:
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN changes TEXT NOT NULL DEFAULT '[]'"
                             % CACHE_TABLE)
                conn.commit()
                ok = True
            except sqlite3.Error as exc:
                print("[differ] 提示：version_diffs 无 changes 列且无法自动补充（%s），"
                      "完整变更集改为进程内复用。" % exc, file=sys.stderr)
                ok = False
    _CHANGES_COL_CACHE[key] = ok
    return ok


def _persist(conn, vd: VersionDiff) -> bool:
    """落库 version_diffs；返回 True 表示**完整变更集**已持久化（同 pair 可直接复用）。"""
    cols = _columns(conn, CACHE_TABLE)
    if not cols:
        return False
    c_doc = _col_pick(cols, ("doc_id", "document_id"))
    c_from = _col_pick(cols, ("from_version_id", "from_id", "from_vid"))
    c_to = _col_pick(cols, ("to_version_id", "to_id", "to_vid"))
    if not (c_doc and c_from and c_to):
        return False
    _ensure_changes_column(conn)
    cols = _columns(conn, CACHE_TABLE)
    c_changes = _col_pick(cols, ("changes", "changes_json", "payload", "data"))
    changes_payload = json.dumps(
        {"differ_rev": DIFFER_REV, "changes": [_change_to_dict(c) for c in vd.changes]},
        ensure_ascii=False)

    # 1) 优先走 core 提供的 DAO（CONTRACT §3「store 已有则用」）
    writer = getattr(store, "write_version_diff", None)
    if callable(writer):
        try:
            writer(conn, doc_id=vd.doc_id, from_version_id=vd.from_version_id,
                   to_version_id=vd.to_version_id, counts=vd.counts)
        except Exception as exc:
            print("[differ] 提示：store.write_version_diff 失败（%s），改用直接写库。" % exc,
                  file=sys.stderr)
        else:
            if not c_changes:
                return False
            try:
                conn.execute(
                    "UPDATE %s SET %s=? WHERE %s=? AND %s=? AND %s=?"
                    % (CACHE_TABLE, c_changes, c_doc, c_from, c_to),
                    (changes_payload, vd.doc_id, vd.from_version_id, vd.to_version_id))
                conn.commit()
                return True
            except sqlite3.Error as exc:
                # 表可能被重建过（列没了）：失效标记，后续走进程内复用
                _CHANGES_COL_CACHE[_db_key(conn)] = False
                print("[differ] 提示：version_diffs 变更集缓存写失败（%s）。" % exc,
                      file=sys.stderr)
                return False

    # 2) 通用兜底：INSERT OR REPLACE（列名自适应）
    row: dict[str, Any] = {c_doc: vd.doc_id, c_from: vd.from_version_id, c_to: vd.to_version_id}
    c_counts = _col_pick(cols, ("counts", "counts_json", "counts_text"))
    if c_counts:
        row[c_counts] = json.dumps(vd.counts, ensure_ascii=False)
    if c_changes:
        row[c_changes] = changes_payload
    elif not c_counts:
        return False
    for name in ("created_at", "updated_at", "ts"):
        if name in cols:
            row[name] = _now_iso()
            break
    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
        CACHE_TABLE, ",".join(row.keys()), ",".join("?" for _ in row))
    try:
        conn.execute(sql, list(row.values()))
        conn.commit()
        return bool(c_changes)
    except sqlite3.Error as exc:
        print("[differ] 警告：version_diffs 落库失败（%s），本次 diff 不缓存。" % exc,
              file=sys.stderr)
        return False


def _parse_changes_cell(raw: Any) -> Optional[list]:
    if raw is None or raw == "":
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if data.get("differ_rev") != DIFFER_REV:
            return None
        inner = data.get("changes")
        return inner if isinstance(inner, list) else None
    return None


def _load_cached(conn, doc_id: int, from_version_id: int, to_version_id: int) -> Optional[VersionDiff]:
    cols = _columns(conn, CACHE_TABLE)
    if not cols:
        return None
    c_doc = _col_pick(cols, ("doc_id", "document_id"))
    c_from = _col_pick(cols, ("from_version_id", "from_id", "from_vid"))
    c_to = _col_pick(cols, ("to_version_id", "to_id", "to_vid"))
    c_changes = _col_pick(cols, ("changes", "changes_json", "payload", "data"))
    if not (c_doc and c_from and c_to and c_changes):
        return None
    try:
        rows = conn.execute(
            "SELECT * FROM %s WHERE %s=? AND %s=? AND %s=? ORDER BY rowid DESC LIMIT 1"
            % (CACHE_TABLE, c_doc, c_from, c_to),
            (doc_id, from_version_id, to_version_id)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    changes_raw = _parse_changes_cell(_row_get(rows[0], c_changes))
    if changes_raw is None:
        return None
    counts_raw: Any = None
    c_counts = _col_pick(cols, ("counts", "counts_json", "counts_text"))
    if c_counts:
        raw = _row_get(rows[0], c_counts)
        try:
            counts_raw = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            counts_raw = None
    changes = [_change_from_dict(d) for d in changes_raw if isinstance(d, dict)]
    counts = counts_raw if isinstance(counts_raw, dict) else _counts_from_changes(changes)
    return VersionDiff(doc_id=doc_id, from_version_id=from_version_id,
                       to_version_id=to_version_id, changes=changes, counts=counts)


def _counts_from_changes(changes: Sequence[Change]) -> dict:
    """缓存里只有变更集时的 counts 兜底（不参与正常路径）。"""
    out = {k: 0 for k in ("unchanged", "modified", "added", "removed",
                          "moved", "reordered", "split", "merged")}
    for c in changes:
        if c.kind in out:
            out[c.kind] += 1
    out["split_old"] = out["split"]
    out["split_new"] = sum(len(getattr(c, "meta", {}).get("group_new_ids") or [])
                           for c in changes if c.kind == "split") or out["split"] * 2
    out["merged_new"] = out["merged"]
    out["merged_old"] = sum(len(getattr(c, "meta", {}).get("group_old_ids") or [])
                            for c in changes if c.kind == "merged") or out["merged"] * 2
    out["old_total"] = (out["unchanged"] + out["modified"] + out["removed"] + out["moved"]
                        + out["reordered"] + out["split_old"] + out["merged_old"])
    out["new_total"] = (out["unchanged"] + out["modified"] + out["added"] + out["moved"]
                        + out["reordered"] + out["split_new"] + out["merged_new"])
    return out


def _clear_links(conn, to_version_id: int, from_version_id: Optional[int] = None) -> None:
    """清掉该 (from, to) 组合既有的链接，保证重复调用不产生重复行。

    * 新版侧：new_segment_id 属于 to_version 的所有行（含 added / split / merged）。
    * 旧版侧：new_segment_id IS NULL 且 old_segment_id 属于 from_version 的 REMOVED 行。
    """
    todo = [
        ("DELETE FROM segment_links WHERE new_segment_id IN "
         "(SELECT id FROM segments WHERE version_id=?)", (to_version_id,)),
    ]
    if from_version_id:
        todo.append(
            ("DELETE FROM segment_links WHERE new_segment_id IS NULL AND old_segment_id IN "
             "(SELECT id FROM segments WHERE version_id=?)", (from_version_id,)))
    for sql, args in todo:
        try:
            conn.execute(sql, args)
            conn.commit()
        except sqlite3.Error:
            pass


def _record_link(conn, nid: Optional[int], oid: Optional[int], kind: str,
                 ratio: float, cds) -> None:
    try:
        store.record_segment_link(conn, new_segment_id=nid, old_segment_id=oid,
                                  change_kind=kind, ratio=ratio, char_diffs=cds)
    except Exception:
        if oid is not None or nid is None:
            raise
        # 某些实现不接受 NULL old_segment_id：退化为 0（无旧段）
        store.record_segment_link(conn, new_segment_id=nid, old_segment_id=0,
                                  change_kind=kind, ratio=ratio, char_diffs=cds)


def _bulk_insert_links(conn, links: Sequence[tuple]) -> Optional[int]:
    """单事务批量写 segment_links（大版本必须走这条路才能满足 <3s 的性能指标）。

    返回写入行数；表结构无法识别时返回 None，由调用方退回 store.record_segment_link。
    """
    cols = _columns(conn, "segment_links")
    if not cols:
        return None
    c_new = _col_pick(cols, ("new_segment_id", "new_id", "new_seg_id"))
    c_old = _col_pick(cols, ("old_segment_id", "old_id", "old_seg_id"))
    c_kind = _col_pick(cols, ("change_kind", "kind"))
    c_ratio = _col_pick(cols, ("ratio", "similarity"))
    c_cd = _col_pick(cols, ("char_diffs", "char_diffs_json", "diffs"))
    c_at = _col_pick(cols, ("created_at", "ts"))
    if not (c_new and c_kind):
        return None
    names = [c_new]
    if c_old:
        names.append(c_old)
    names.append(c_kind)
    if c_ratio:
        names.append(c_ratio)
    if c_cd:
        names.append(c_cd)
    if c_at:
        names.append(c_at)
    rows = []
    now = _now_iso()
    for (nid, oid, kind, ratio, cds) in links:
        row: list[Any] = [int(nid) if nid is not None else None]
        if c_old:
            row.append(int(oid) if oid is not None else None)
        row.append(str(kind))
        if c_ratio:
            row.append(float(ratio))
        if c_cd:
            row.append(json.dumps(cds or [], ensure_ascii=False))
        if c_at:
            row.append(now)
        rows.append(tuple(row))
    sql = "INSERT OR REPLACE INTO segment_links (%s) VALUES (%s)" % (
        ",".join(names), ",".join("?" for _ in names))
    try:
        conn.executemany(sql, rows)
        conn.commit()
        return len(rows)
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        print("[differ] 提示：批量写 segment_links 失败（%s），退回逐条 store 接口。" % exc,
              file=sys.stderr)
        return None


def _write_links(conn, to_version_id: int, from_version_id: Optional[int],
                 links: Sequence[tuple]) -> tuple[int, int]:
    """写入段链接；返回 (成功数, 失败数)。幂等：先清掉该组合既有的链接。"""
    _clear_links(conn, to_version_id, from_version_id)
    if len(links) >= BULK_LINK_THRESHOLD:
        n = _bulk_insert_links(conn, links)
        if n is not None:
            return n, 0
    ok = 0
    failed = 0
    for (nid, oid, kind, ratio, cds) in links:
        try:
            _record_link(conn, (int(nid) if nid is not None else None),
                         (int(oid) if oid is not None else None), kind, float(ratio), cds)
            ok += 1
        except Exception as exc:
            failed += 1
            if failed <= 3:
                print("[differ] 警告：record_segment_link 失败（new=%s old=%s kind=%s）：%s"
                      % (nid, oid, kind, exc), file=sys.stderr)
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return ok, failed


# ---------------------------------------------------------------- 对外 API

_MEMO: "OrderedDict[tuple, VersionDiff]" = OrderedDict()
_MEMO_MAX = 64


def _memo_key(conn, doc_id: int, from_version_id: int, to_version_id: int) -> Optional[tuple]:
    """进程内记忆键：库路径 + 版本对 + 两侧段集合签名（段被重新写入时自动失效）。"""
    try:
        a = conn.execute("SELECT COUNT(*) AS c, COALESCE(MAX(id),0) AS m FROM segments "
                         "WHERE version_id=?", (from_version_id,)).fetchone()
        b = conn.execute("SELECT COUNT(*) AS c, COALESCE(MAX(id),0) AS m FROM segments "
                         "WHERE version_id=?", (to_version_id,)).fetchone()
        return (_db_key(conn), int(doc_id), int(from_version_id), int(to_version_id),
                int(_row_get(a, "c") or 0), int(_row_get(a, "m") or 0),
                int(_row_get(b, "c") or 0), int(_row_get(b, "m") or 0))
    except sqlite3.Error:
        return None


def _memo_get(key: Optional[tuple]) -> Optional[VersionDiff]:
    if key is None:
        return None
    hit = _MEMO.get(key)
    if hit is not None:
        _MEMO.move_to_end(key)
    return hit


def _memo_put(key: Optional[tuple], vd: VersionDiff) -> None:
    if key is None:
        return
    _MEMO[key] = vd
    _MEMO.move_to_end(key)
    while len(_MEMO) > _MEMO_MAX:
        _MEMO.popitem(last=False)


def diff_versions(conn, doc_id, from_version_id, to_version_id) -> VersionDiff:
    """CONTRACT §8：两版本之间的段级变更集（稳定、可重复、幂等）。"""
    doc_id = int(doc_id)
    from_version_id = int(from_version_id)
    to_version_id = int(to_version_id)

    # 完整变更集无法落库时（version_diffs 无 changes 列且不能补充）退回进程内复用
    memo_key = None if _ensure_changes_column(conn) else _memo_key(
        conn, doc_id, from_version_id, to_version_id)
    hit = _memo_get(memo_key)
    if hit is not None:
        return hit

    cached = _load_cached(conn, doc_id, from_version_id, to_version_id)
    if cached is not None:
        return cached

    old = _body_segments(conn, from_version_id)
    new = _body_segments(conn, to_version_id)
    vd, links = _compute(old, new, doc_id, from_version_id, to_version_id)
    try:
        ok, failed = _write_links(conn, to_version_id, from_version_id, links)
        if failed:
            print("[differ] 警告：%d/%d 条 segment_links 写入失败。" % (failed, ok + failed),
                  file=sys.stderr)
    except Exception as exc:      # 链路写失败不影响 diff 结果本身
        print("[differ] 警告：segment_links 写入失败（%s）" % exc, file=sys.stderr)
    stored = _persist(conn, vd)
    if not stored:
        _memo_put(memo_key, vd)
    return vd


def diff_summary_text(vd: VersionDiff) -> str:
    """人类可读摘要（Markdown 表格），供 CLI 与页面使用。"""
    counts = vd.counts or {}
    base = int(counts.get("new_total") or counts.get("old_total") or 0)
    lines = [
        "**版本变更摘要**：v%s → v%s（文档 %s）" % (
            vd.from_version_id, vd.to_version_id, vd.doc_id),
        "",
        "| 变更类型 | 数量 | 占比 |",
        "| --- | ---: | ---: |",
    ]
    for kind in ("unchanged", "modified", "added", "removed",
                 "moved", "reordered", "split", "merged"):
        n = int(counts.get(kind) or 0)
        pct = ("%.1f%%" % (100.0 * n / base)) if base else "-"
        lines.append("| %s `%s` | %d | %s |" % (_KIND_LABELS[kind], kind, n, pct))
    old_total = int(counts.get("old_total") or 0)
    new_total = int(counts.get("new_total") or 0)
    lines += [
        "| **合计（旧版段）** | %d | - |" % old_total,
        "| **合计（新版段）** | %d | - |" % new_total,
        "",
    ]
    if counts.get("split_new") or counts.get("merged_old"):
        lines.append("结构变化：拆分 %d 组（%d 旧段 → %d 新段）；合并 %d 组（%d 旧段 → %d 新段）。"
                     % (int(counts.get("split") or 0), int(counts.get("split_old") or 0),
                        int(counts.get("split_new") or 0), int(counts.get("merged") or 0),
                        int(counts.get("merged_old") or 0), int(counts.get("merged_new") or 0)))
    return "\n".join(lines)


def changes_by_segment(conn, doc_id, from_version_id, to_version_id) -> dict[int, Change]:
    """键 = **新版** segment_id → Change（供 UI 给新版段打变更标记）。"""
    vd = diff_versions(conn, doc_id, from_version_id, to_version_id)
    out: dict[int, Change] = {}
    for c in vd.changes:
        meta = getattr(c, "meta", None) or {}
        ids: list[int] = []
        if c.kind == "split" and meta.get("group_new_ids"):
            ids = [int(x) for x in meta["group_new_ids"]]
        elif c.new_segment_id:
            ids = [int(c.new_segment_id)]
        for nid in ids:
            out[nid] = c
    return out


def removed_by_segment(conn, doc_id, from_version_id, to_version_id) -> dict[int, Change]:
    """键 = **旧版** segment_id → Change（用于「已删除」幽灵块）。

    包含 removed / split 的旧段，以及 merged 组内的全部旧段。
    """
    vd = diff_versions(conn, doc_id, from_version_id, to_version_id)
    out: dict[int, Change] = {}
    for c in vd.changes:
        meta = getattr(c, "meta", None) or {}
        if c.kind == "merged":
            ids = [int(x) for x in (meta.get("group_old_ids") or [])] or (
                [int(c.old_segment_id)] if c.old_segment_id else [])
        elif c.kind in ("removed", "split") and c.old_segment_id:
            ids = [int(c.old_segment_id)]
        else:
            ids = []
        for oid in ids:
            out[oid] = c
    return out
