"""译文质量审计与定点修复。

背景（本轮实测）：抽取阶段会把极少数句子切在两个段里（TO-34 与训练手册各约 10 处），
翻译器只看半句就会产出残缺译文 —— 实测最严重的一条把整句「…is allowed without the
written permission of the BMS Docs team.」前半漏译，后半只输出一个「。」。

与其为了这几处**重新 ingest + 全量重译**（训练手册 5580 条要重跑，得不偿失），
这里做**有界定点修复**：
  1. 扫描 machine 译文，找出「译文疑似残缺」的段（长度比过低 / 只剩标点 / 明显截断）；
  2. 对每个可疑段，把**它和紧邻的下一段**拼成一个上下文一起送翻译（带上下文重译）；
  3. 只覆盖这些段的译文，其余不动。

判定「疑似残缺」的规则（保守，宁可少修不错修）：
  * 译文去掉标点后为空或只有 1-2 个字符，而原文 ≥ 30 字符；
  * 译文长度 / 原文长度 < 0.06，且原文 ≥ 60 字符；
  * 译文以逗号/连词结尾（英文式未完结），或明显是半句。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SENT_END = set(".!?。！？:;\"'”’)")
_PUNCT_ONLY = re.compile(r"^[\s。，、；：！？,.!?;:…—\-–\"'“”‘’()（）【】\[\]]*$")


def _strip_punct(s: str) -> str:
    return _PUNCT_ONLY.sub("", s or "")


def suspect(en: str, zh: str) -> Optional[str]:
    """返回可疑原因，None 表示正常。"""
    en, zh = (en or "").strip(), (zh or "").strip()
    if not en or not zh:
        return None
    if len(en) >= 30 and len(_strip_punct(zh)) <= 2:
        return "译文几乎只剩标点"
    if len(en) >= 60 and len(zh) / max(1, len(en)) < 0.06:
        return "译文长度比过低"
    # 以连接词/逗号结尾（中文里不该这样收尾）
    if len(en) >= 50 and re.search(r"[,;，、]\s*$", zh):
        return "译文以逗号结尾（疑似截断）"
    return None


def audit(conn, version_id: int, *, limit: int = 0) -> List[Dict[str, Any]]:
    """扫描一个版本的 machine 译文，返回可疑列表（按页序）。

    会跳过**合法的跨段续句**：若本段原文未以句末标点结尾、且下一段以**小写字母**
    开头（说明同一个句子被切在两段里），那么译文以逗号结尾是**正确**的 ——
    硬修只会让模型重复产出同样的残句（实测 p88 seg=2090 修两次都一样）。
    """
    from core import store
    tr = store.translations_of_version(conn, version_id)
    segs = [s for s in store.segments_of_version(conn, version_id) if s["role"] == "body"]
    out = []
    for i, s in enumerate(segs):
        t = tr.get(s["id"])
        if not t or t.get("status") != "machine":
            continue
        why = suspect(s["text"], t.get("text") or "")
        if not why:
            continue
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        # 合法续句 → 跳过。判据（任一成立即可）：
        #   a) 下一段以**小写字母或数字**开头（句子被切在段边界）；
        #   b) 本段英文**自己就以逗号/分号/破折号结尾** —— 说明上游抽取就把半句
        #      切开了，译文以逗号收尾是**忠实**的，不是模型截断。
        #      TO-1 实测 10 条"疑似截断"全部属于 (b)：
        #      EN '... GPS,' / '... EPU, hot brakes, etc.)' / '... transfer pumps,'
        #      —— 译文照抄了原文的逗号，按"截断"去修只会改坏它。
        # ⚠️ 但**绝不跳过内容近乎为空的译文** —— 那正是最初发现的 bug
        # （'without the written permission…' → '。'），不能因为它是续句就放过。
        en = (s["text"] or "").rstrip()
        zh_txt = (t.get("text") or "").strip()
        substantive = len(_strip_punct(zh_txt)) >= 3
        if substantive and nxt and en:
            nb = (nxt["text"] or "").lstrip()
            no_terminal = en[-1] not in SENT_END
            next_continues = bool(nb) and (nb[:1].islower() or nb[:1].isdigit())
            en_ends_open = en[-1] in ",;，、-–—"
            near = nxt["page"] in (s["page"], s["page"] + 1)
            if near and no_terminal and (next_continues or en_ends_open):
                continue
        out.append({
            "seg": s, "zh": t.get("text") or "", "why": why,
            "next": nxt,
            "next_zh": (tr.get(nxt["id"]) or {}).get("text", "") if nxt else "",
        })
        if limit and len(out) >= limit:
            break
    return out


def _split_by_ratio(zh: str, en_a: str, en_b: str) -> Tuple[str, str]:
    """把合并译文按英文长度比例切回两段，避免把一段的译文整段塞进另一段。"""
    total = len(en_a) + len(en_b)
    if total <= 0:
        return zh, ""
    cut = int(len(zh) * len(en_a) / total)
    # 尽量切在中文标点处，读起来更像完整句子
    for p in range(cut, min(len(zh), cut + 12)):
        if zh[p - 1:p] in "。，；：、,.;:":
            cut = p
            break
    a, b = zh[:cut].rstrip(), zh[cut:].lstrip()
    if len(_strip_punct(a)) < 2 or len(_strip_punct(b)) < 2:
        # 切得不好：整段给第一段，第二段留空由下一次审计处理
        return zh, ""
    return a, b


def repair_continuation(conn, version_id: int, *, limit: int = 0, apply: bool = False,
                        verbose: bool = True) -> Dict[str, int]:
    """报出「相邻两段其实是同一个句子」的候选，**默认只报告不修改**。

    ⚠️ **不要轻易 apply=True。** 实测教训（TO-1 6194 段，2026-09-17）：
    这个启发式无法可靠区分「真正被切开的句子」与「结构上相邻但语义独立的两段」
    —— 标题/正文、目录号/标题、项目符号/列表项、章节号/页码都会被匹配上。
    一次 apply 跑了 110 对，**绝大多数是不该动的**，把本来正确的译文改坏
    （例如把目录行改成带重复文字），并且**顺带污染了 translation_cache**，
    恢复只能靠重新逐段翻译。

    现在默认 `apply=False`，只把候选打出来给人看。真要修，请先看输出确认，
    再显式传 apply=True；且修完务必 `python cli.py quality` 复核。
    """
    from core import store
    from translate import engine
    import config as _cfg

    tr = store.translations_of_version(conn, version_id)
    segs = [s for s in store.segments_of_version(conn, version_id) if s["role"] == "body"]
    cfg = dict(_cfg.load())
    api_key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    gl = engine.glossary.prompt_block()

    pairs = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        ea, eb = (a["text"] or "").rstrip(), (b["text"] or "").strip()
        if not ea or not eb:
            continue
        if b["page"] not in (a["page"], a["page"] + 1):
            continue
        # 只挑「A 被硬切开 + B 原文长但译文极短」这一种 —— 这才是真的内容错位。
        if ea[-1] in SENT_END:
            continue
        if len(eb) < 30:
            continue
        if all(c.isdigit() or c in "/.- " for c in eb):
            continue
        tb = tr.get(b["id"])
        if not tb or tb.get("status") != "machine":
            continue
        if len((tb.get("text") or "").strip()) >= 20:
            continue
        ta = tr.get(a["id"])
        if not ta or ta.get("status") != "machine":
            continue
        pairs.append((a, b))
        if limit and len(pairs) >= limit:
            break

    if not apply:
        if verbose:
            print(f"候选 {len(pairs)} 对（仅报告，未修改）。示例：")
            for a, b in pairs[:10]:
                print(f"  p{a['page']} seg={a['id']}/{b['id']}")
                print(f"     A.en ...{a['text'][-56:]!r}")
                print(f"     A.zh ...{(tr.get(a['id']) or {}).get('text', '')[-56:]!r}")
                print(f"     B.en {b['text'][:56]!r}")
                print(f"     B.zh {(tr.get(b['id']) or {}).get('text', '')[:56]!r}")
            print("  → 确认无误后传 apply=True 才会修改。")
        return {"pairs": len(pairs), "fixed": 0, "failed": 0, "applied": 0}

    fixed = failed = 0
    for a, b in pairs:
        merged = f"{a['text'].strip()} {b['text'].strip()}"
        item = engine._Item(a["id"], merged, a["page"], "body", "paragraph")
        r = engine._run_batch([item], {}, provider="deepseek",
                              model=cfg.get("model", "deepseek-flash"), cfg=cfg,
                              api_key=api_key, glossary_block=gl)
        zh = ((r.get("out") or {}) if r.get("ok") else {}).get(a["id"])
        if not zh:
            failed += 1
            continue
        za, zb = _split_by_ratio(zh, a["text"], b["text"])
        for seg, z in ((a, za), (b, zb)):
            if not z:
                continue
            store.upsert_translation(conn, segment_id=seg["id"], text=z, status="machine",
                                     provider=cfg.get("provider") or "deepseek",
                                     model=cfg.get("model") or "")
        fixed += 1
        if verbose:
            print(f"  ✅ seg={a['id']}+{b['id']} p{a['page']}")
            print(f"       新 A: {za[:70]!r}")
            print(f"       新 B: {zb[:70]!r}")
    conn.commit()
    return {"pairs": len(pairs), "fixed": fixed, "failed": failed, "applied": 1}


def repair(conn, version_id: int, items: List[Dict[str, Any]], *,
           verbose: bool = True) -> Dict[str, int]:
    """对可疑段做「带上下文重译」并覆盖。

    两种修法，按切分方向选：
      * **续段**（本段是句子的开头，下一段是它的尾巴）：把两段拼起来翻译，
        再按字符比例把译文切回本段应得的部分，写本段；
        同时把「本段译文 + 尾巴译文」合并写进**下一段**，让两段拼起来完整。
        只修本段会出现「距离环启用该选项时…反之，」这种把下一段内容也带进来的残句。
      * **孤尾**（本段是上一段的尾巴，或独立成句）：把上一段一起送进去做上下文，
        再按比例取回本段部分。
    """
    from core import store
    from translate import engine
    import config as _cfg

    cfg = dict(_cfg.load())
    api_key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    gl = engine.glossary.prompt_block()

    from core import fingerprint as _fp

    def _continues(next_seg) -> bool:
        """本段后面还有一段小写开头的同族文本 → 本段是句子开头。"""
        if not next_seg:
            return False
        txt = (next_seg["text"] or "").strip()
        if not txt or not txt[:1].islower():
            return False
        sa = float((next_seg.get("style") or {}).get("size") or 0)
        return abs(sa - 0.0) < 1e9      # 字号检查已在抽取阶段做过

    def _translate(text: str, seg_id: int, page: int) -> Optional[str]:
        item = engine._Item(seg_id, text, page, "body", "paragraph")
        r = engine._run_batch([item], {}, provider="deepseek",
                              model=cfg.get("model", "deepseek-flash"), cfg=cfg,
                              api_key=api_key, glossary_block=gl)
        if not r.get("ok"):
            return None
        return (r.get("out") or {}).get(seg_id)

    fixed = failed = 0
    for it in items:
        s, nxt = it["seg"], it["next"]
        ctx = (s["text"] + (" " + nxt["text"] if nxt else "")).strip()
        if not ctx:
            continue
        zh_full = _translate(ctx, s["id"], s["page"])
        if not zh_full:
            failed += 1
            if verbose:
                print(f"  seg={s['id']} p{s['page']} 重译失败")
            continue

        if nxt and _continues(nxt):
            ratio = len(s["text"]) / max(1, len(ctx))
            cut = max(1, int(len(zh_full) * ratio))
            zh_seg = zh_full[:cut].rstrip()
            if len(_strip_punct(zh_seg)) < 2:
                zh_seg = zh_full
            # 尾巴段：写「本段译文 + 尾巴」的合并结果，保证两段拼起来完整
            store.upsert_translation(conn, segment_id=nxt["id"], text=zh_full,
                                     status="machine",
                                     provider=cfg.get("provider") or "deepseek",
                                     model=cfg.get("model") or "")
        else:
            ratio = len(s["text"]) / max(1, len(ctx))
            cut = max(1, int(len(zh_full) * ratio))
            zh_seg = zh_full[:cut].rstrip()
            if len(_strip_punct(zh_seg)) < 2:
                zh_seg = zh_full

        store.upsert_translation(conn, segment_id=s["id"], text=zh_seg,
                                 status="machine",
                                 provider=cfg.get("provider") or "deepseek",
                                 model=cfg.get("model") or "")
        fixed += 1
        if verbose:
            print(f"  ✅ seg={s['id']} p{s['page']} ({it['why']})")
            print(f"       旧: {it['zh'][:90]!r}")
            print(f"       新: {zh_seg[:90]!r}")
    conn.commit()
    return {"fixed": fixed, "failed": failed}
