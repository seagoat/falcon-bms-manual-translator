#!/usr/bin/env python
"""tests/test_translate.py —— 翻译引擎测试（可直接运行，无需 pytest）。

    python tests\\test_translate.py            # 全离线（0 次网络调用）
    python tests\\test_translate.py --live     # 额外跑 1 次真实 DeepSeek 调用

覆盖：
1. glossary / prompt 单元行为（术语强制替换、提示词片段、响应容错解析）。
2. mock 后端离线全流程：合成 PDF -> 抽取段 -> 入库 -> 翻译 -> 断言状态分布；
   二次运行 0 次后端调用（缓存命中）。
3. 增量：新版本相同段走 translation_cache（CARRIED），小改段走 segment_links（SEEDED）。
4. deepseek 后端（打桩 `translate.engine._http_post`）：请求体结构、JSON 容错、
   重试路径、不可重试错误、最终失败标记、dry_run 零网络调用。
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- 隔离运行环境：数据库/术语表都落在临时目录 -----------------------------
import tempfile

TMP = Path(tempfile.mkdtemp(prefix="mt_translate_test_"))
os.environ["MT_DATA_DIR"] = str(TMP / "data")
os.environ["MT_CONFIG"] = str(TMP / "local.json")
(TMP / "local.json").write_text(json.dumps({
    "deepseek_api_key": "test-key-not-real",
    "data_dir": str(TMP / "data"),
    "model": "deepseek-v4-pro",
    "provider": "deepseek",
}, ensure_ascii=False), encoding="utf-8")

import config  # noqa: E402

config.load(reload=True)

import pymupdf  # noqa: E402

from core import db as core_db  # noqa: E402
from core import store  # noqa: E402
from core.fingerprint import fingerprint as fp_digest, normalize as fp_norm  # noqa: E402
from core.models import Segment  # noqa: E402
from translate import engine, glossary, prompt  # noqa: E402

DB_PATH = str(Path(config.data_dir()) / "app.db")
CFG = {"db_path": DB_PATH, "retry_backoff": [0, 0, 0], "concurrency": 4}
TESTS: list = []


def test(fn):
    TESTS.append(fn)
    return fn


def _conn():
    conn = core_db.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def _patched(module, name: str, value):
    """安全 monkeypatch：用完恢复原属性（不能 del，否则会删掉真函数）。"""
    orig = getattr(module, name)
    setattr(module, name, value)
    try:
        yield orig
    finally:
        setattr(module, name, orig)


# ==========================================================================
# 合成 PDF + 抽取
# ==========================================================================
def _flat(text: str) -> str:
    return " ".join(str(text).split())


# fixture 用的字体：▪ 只在 calibri、◆ 只在 Deng.ttf 有字形（base-14 会替换成 "?"）
_CALIBRI = "C:/Windows/Fonts/calibri.ttf"
_DENG = "C:/Windows/Fonts/Deng.ttf"


def _make_pdf(path: Path, pages: int = 3, tag: str = "x") -> Path:
    doc = pymupdf.open()
    body = [
        "The MASTER CAUTION light activates shortly after any individual light "
        "on the Caution Panel illuminates (excluding IFF).",
        "If the EPU RUN light is off, there is a single system B hydraulic failure "
        "(refer to System B Hydraulic failure in the EP checklists).",
        "The FLCS provides pitch, roll and yaw control through the side stick and rudder pedals.",
    ]
    for pno in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 40), f"BMS 4.38.1 Training Manual [{tag}] p{pno + 1}", fontsize=8)
        page.insert_text((300, 770), f"Page {pno + 1}", fontsize=8)
        y = 100
        if pno == 0:
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 30),
                                f"GROUND OPERATIONS [{tag}]", fontsize=16)
            y += 40
        for i, para in enumerate(body):
            # 页内标记 + 用例 tag：保证不同用例的文本唯一（缓存/链接测试需要）
            text = f"Page {pno + 1} step {i + 1} [{tag}]: {para}"
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 60), text, fontsize=10)
            y += 70
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 20),
                            f"\u25aa Before engine start, verify OBOGS is off. [{tag} p{pno + 1}]",
                            fontsize=10, fontname="calibri", fontfile=_CALIBRI)
        y += 24
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 20),
                            f"1. Set the STPT to the runway heading. [{tag} p{pno + 1}]",
                            fontsize=10)
        y += 26
        # ◆ 只有 Deng.ttf 有字形（base-14 Helvetica 会把它替换成 "?"，让 fixture 失真）
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 20), "\u25c6", fontsize=10,
                            fontname="deng", fontfile=_DENG)
        y += 30
        # 图片（验证抽取不因图片崩）
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 24))
        pix.set_rect(pix.irect, (180, 60, 60))
        page.insert_image(pymupdf.Rect(72, y, 192, y + 60), pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


def extract_segments(pdf_path: Path, doc_id: int, version_id: int) -> list[Segment]:
    """用 pymupdf 抽取文本块 -> Segment（header/footer/pagenum/body 角色判定）。"""
    doc = pymupdf.open(str(pdf_path))
    segs: list[Segment] = []
    order = 0
    for pno in range(doc.page_count):
        page = doc[pno]
        height = page.rect.height
        blocks = [b for b in page.get_text("blocks") if int(b[6]) == 0]
        blocks.sort(key=lambda b: (round(float(b[1]), 1), float(b[0])))
        for b in blocks:
            x0, y0, x1, y1 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            text = str(b[4]).strip()
            if not text:
                continue
            if y1 <= 60.0:
                role = "header"
            elif y0 >= height - 60.0:
                role = "footer"
            else:
                role = "body"
            if role == "footer" and re.fullmatch(r"Page\s+\d+|\d+", text):
                role = "pagenum"
            if text.isupper() and len(text) < 40:
                kind = "heading"
            elif text[:1] in "\u25aa\u25c6-*":
                kind = "list_item"
            else:
                kind = "paragraph"
            order += 1
            segs.append(Segment(
                id=0, version_id=version_id, doc_id=doc_id, page=pno + 1,
                order_index=order, kind=kind, text=text,
                normalized=fp_norm(text), fingerprint=fp_digest(text),
                bbox=[x0, y0, x1, y1], role=role,
            ))
    doc.close()
    return segs


def _new_version(conn, pdf_path: Path, pages: int = 3, label: str = "v1"):
    doc_id = store.upsert_document(conn, slug=f"doc-{label}", title="Training Manual",
                                   source_dir=str(TMP))
    raw = Path(pdf_path).read_bytes()
    import hashlib
    sha = hashlib.sha256(raw).hexdigest()
    with pymupdf.open(str(pdf_path)) as d:
        page_count = d.page_count
    version_id = store.register_version(conn, doc_id=doc_id, source_path=str(pdf_path),
                                        sha256=sha, pdf_bytes=len(raw),
                                        page_count=page_count, release_label="4.38.1",
                                        label=label)
    segs = extract_segments(pdf_path, doc_id, version_id)
    ids = store.insert_segments(conn, segs)
    conn.commit()
    return doc_id, version_id, ids, segs


# ==========================================================================
# 1. 单元：glossary / prompt
# ==========================================================================
@test
def t01_glossary_units():
    entries = glossary.load()
    assert len(entries) >= 60, f"术语表条目太少: {len(entries)}"
    for e in entries:
        assert set(e) == {"en", "zh", "case_sensitive", "note"}, e
    assert len(glossary.get_pairs()) >= 60
    src = "The MASTER CAUTION light activates."
    out = glossary.apply(src)
    assert "主警戒灯" in out and "MASTER CAUTION" in out, out
    assert glossary.apply(out) == out, "apply 必须幂等"
    assert "主警戒灯（MASTER CAUTION）" in out, out
    # 术语表全文块
    block = glossary.prompt_block()
    assert "MASTER CAUTION = 主警戒灯" in block
    # 只列批内术语
    small = glossary.prompt_block(["The canopy is closed."])
    assert "canopy = 座舱盖" in small and "TACAN" not in small
    # mock 占位：确定性 + 术语落地 + 未命中有标记
    t = "The MASTER CAUTION light activates."
    m1, m2 = glossary.mock_translate(t), glossary.mock_translate(t)
    assert m1 == m2, "mock 必须可复现"
    assert "主警戒灯" in m1 and "〔" in m1, m1
    assert glossary.mock_translate("") == ""


@test
def t13_apply_preserves_code_tokens_and_is_idempotent():
    """缺陷回归（Lead 真实译文第 4 页发现）：
    1) 文件名/标识符/路径/版本号里的英文不得被术语替换；
    2) apply 必须幂等，且不得在中文之间注入空格；
    3) 同段重复术语：首次展开「中文（缩写）」，后续只保留缩写。
    """
    g = glossary
    # --- 1. 代码样 token 必须逐字符原样保留 ---
    for s in ["(TR_BMS_05_ILS_Landing)", "TR_BMS_01_GroundOPS", "callsign.ini",
              "(/Docs/02 Aircraft Manuals & Checklists\\01 F-16)", "Dash-34",
              "BMS 4.38.1", "TR_BMS_05_ILS_Landing.pdf",
              "MISSION 5: (TR_BMS_05_ILS_Landing)"]:
        got = g.apply(s)
        assert got == s, f"代码样 token 被改写:\n  in ={s!r}\n  out={got!r}"
    # 文件名外的独立 ILS 仍应正常翻译，文件名内的不能动
    line = "MISSION 5: ILS LANDING AT NIGHT (TR_BMS_05_ILS_Landing) .......... 130"
    out_line = g.apply(line)
    assert out_line.count("(TR_BMS_05_ILS_Landing)") == 1, out_line
    assert "仪表着陆系统（ILS）" in out_line, out_line
    # 去掉文件名后，剩下的 ILS 只能是展开括注里的那一个
    assert out_line.replace("(TR_BMS_05_ILS_Landing)", "").count("ILS") == 1, out_line
    # --- 2. 中文之间不得有空格 ---
    got = g.apply("检查 OBOGS 与 EPU。")
    assert got == "检查机载制氧系统（OBOGS）与应急动力装置（EPU）。", got
    # --- 3. 幂等 ---
    for s in ["检查 OBOGS 与 EPU。",
              "The MASTER CAUTION light and the Caution Panel.",
              "主警戒灯（MASTER CAUTION）与警戒灯面板。",
              "使用 HOTAS 布局；HOTAS 功能。",
              "MISSION 5: ILS LANDING AT NIGHT (TR_BMS_05_ILS_Landing) ..... 130",
              "3. 我们将假定你使用真实的 HOTAS 布局；指代 HOTAS 功能"]:
        once = g.apply(s)
        twice = g.apply(once)
        assert twice == once, f"apply 不幂等:\n  s    ={s!r}\n  1st  ={once!r}\n  2nd  ={twice!r}"
    # --- 4. 同段重复：首次展开，后续只留缩写 ---
    out = g.apply("使用 HOTAS 布局；指代 HOTAS 功能")
    assert out.count("手不离杆操纵（HOTAS）") == 1, out
    assert out.count("HOTAS") == 2, out
    # --- 真实模型输出样本（含多余空格，必须清掉） ---
    lead = "3. 我们将假定你使用真实的 HOTAS 布局；指代 HOTAS 功能"
    fixed = g.apply(lead)
    assert " 手不离杆操纵" not in fixed and "（HOTAS） 布局" not in fixed, fixed
    assert fixed.count("手不离杆操纵（HOTAS）") == 1, fixed
    assert fixed.endswith("指代 HOTAS 功能"), fixed
    # --- 正常术语替换不受影响 ---
    out_canopy = g.apply("The canopy is closed.")
    assert "座舱盖" in out_canopy and "canopy" not in out_canopy, out_canopy
    assert "主警戒灯（MASTER CAUTION）" in g.apply("MASTER CAUTION illuminates.")
    # --- mock 后端同样保留代码样 token ---
    m = g.mock_translate("See (TR_BMS_05_ILS_Landing) and callsign.ini for details.")
    assert "TR_BMS_05_ILS_Landing" in m and "callsign.ini" in m, m


@test
def t14_apply_does_not_damage_good_translations():
    """Lead 认可的真实译文样本必须原样通过 apply（幂等且零改动）。"""
    good = [
        "当 ECS 供气低于 10 psi 时，机载制氧系统（OBOGS）警戒灯将亮起，表明氧气生产停止。",
        "全重：26531 lbs 燃油：5898 lbs 阻力系数：9.0 CAT I 最大过载：+9/-3 最大速度：AC",
        "你将驾驶多种 F-16 机型，包括 F-16CM block 50…… F/A-18C Hornet…… F-15C",
        "任务 5：夜间仪表着陆系统着陆（TR_BMS_05_ILS_Landing） .......... 130",
        "\u25aa 滑行前设置航路点（STPT）并检查平视显示器（HUD）符号。",
        "若应急动力装置（EPU）RUN 灯熄灭，则参阅 EP 检查单中的系统 B 液压故障。",
    ]
    for s in good:
        got = glossary.apply(s)
        assert got == s, f"apply 破坏了已正确的译文:\n  in ={s!r}\n  out={got!r}"
        assert glossary.apply(got) == got, f"不幂等: {got!r}"


@test
def t15_self_heals_broken_identifier_translations():
    """旧版 apply 留下的坏译文（标识符被中文截断）必须自动重译，且缓存同步自愈。"""
    broken = "MISSION 5: 夜间仪表着陆系统着陆（TR_BMS_05_仪表着陆系统（ILS）_着陆） ...... 130"
    conn = _conn()
    doc_id, vid, _ids, _segs = _fresh_version(conn, "heal")
    body = [s for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
    assert len(body) >= 3, body
    dirty, clean = body[0], body[1]

    # 坏译文：写进 translations 且写进 translation_cache（模拟旧缺陷产物）
    store.upsert_translation(conn, segment_id=dirty["id"], text=broken, status="machine",
                             provider="deepseek", model="deepseek-flash")
    store.put_cached_translation(conn, fingerprint=dirty["fingerprint"], text=broken,
                                 provider="deepseek", model="deepseek-flash")
    # 干净译文：必须被跳过（cached）
    store.upsert_translation(conn, segment_id=clean["id"], text="干净译文，保持不动。",
                             status="machine", provider="deepseek", model="deepseek-flash")
    conn.commit()

    res = engine.translate_segments(conn, doc_id, vid, provider="mock", cfg=CFG)
    assert res["repaired"] == 1, res
    assert res["failed"] == 0, res
    rows = store.translations_of_version(conn, vid)
    assert rows[clean["id"]]["text"] == "干净译文，保持不动。", "干净译文不得被重译"
    assert res["cached"] >= 1, res
    fixed = rows[dirty["id"]]["text"]
    assert fixed != broken and not engine._looks_broken_identifier(fixed), fixed
    # 缓存也被覆盖成新译文
    cached = store.get_cached_translation(conn, dirty["fingerprint"])
    assert cached and cached["text"] == fixed, cached
    # 再跑一次：坏译文已不存在 -> 不再需要修复，且 0 次后端调用
    res2 = engine.translate_segments(conn, doc_id, vid, provider="mock", cfg=CFG)
    assert res2["repaired"] == 0 and res2["translated"] == 0, res2
    conn.close()


@test
def t02_prompt_units():
    msgs = prompt.build_messages(
        [{"id": 7, "text": "The EPU RUN light is off."}, {"segment_id": 8, "text": "Check IFF."}],
        "EPU = 应急动力装置", seed={7: ("The EPU RUN light is off.", "EPU RUN 灯灭。")})
    assert [m["role"] for m in msgs] == ["system", "user"]
    sys_msg = msgs[0]["content"]
    for kw in ("MASTER CAUTION", "OBOGS", "术语表", "保留", "JSON"):
        assert kw in sys_msg, kw
    payload = json.loads(msgs[1]["content"][msgs[1]["content"].rindex('{"segments"'):])
    assert [s["id"] for s in payload["segments"]] == [7, 8]
    assert payload["segments"][0]["prev_zh"] == "EPU RUN 灯灭。"
    assert "prev_en" not in payload["segments"][1]

    want = [1, 2]
    # 围栏 + 前后废话
    raw = ('好的，以下是译文：\n```json\n{"translations":[{"id":1,"zh":"一"},{'
           '"id":2,"zh":"二"},{"id":9,"zh":"多余"}]}\n```\n希望有帮助！')
    assert prompt.parse_response(raw, want) == {1: "一", 2: "二"}
    # 裸 JSON / 顶层 list / {id:text} 映射
    assert prompt.parse_response('{"translations":[{"id":1,"zh":"一"},{"id":2,"zh":"二"}]}', want)
    assert prompt.parse_response('[{"id":1,"zh":"一"},{"segment_id":2,"translation":"二"}]', want)
    assert prompt.parse_response('{"1":"一","2":"二"}', want)
    # 缺失 id -> ValueError（触发重试）
    try:
        prompt.parse_response('{"translations":[{"id":1,"zh":"一"}]}', want)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("缺失 id 必须抛 ValueError")
    # 空译文视为缺失
    try:
        prompt.parse_response('{"translations":[{"id":1,"zh":"  "},{"id":2,"zh":"二"}]}', want)
    except ValueError:
        pass
    else:
        raise AssertionError("空译文必须抛 ValueError")
    # 完全不是 JSON
    try:
        prompt.parse_response("sorry, I cannot help", want)
    except ValueError:
        pass
    else:
        raise AssertionError("非 JSON 必须抛 ValueError")
    # API 响应体提取
    assert prompt.extract_content({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert prompt.estimate_tokens("主警戒灯") >= 3
    assert prompt.estimate_tokens("") == 0


# ==========================================================================
# 2. mock 离线全流程
# ==========================================================================
@test
def t03_mock_full_pipeline_and_cache():
    pdf = _make_pdf(TMP / "t3v1.pdf", 3, tag="t3")
    conn = _conn()
    doc_id, vid, seg_ids, segs = _new_version(conn, pdf, 3, "t3v1")
    assert len(seg_ids) >= 12, f"抽取段太少: {len(seg_ids)}"
    # fixture 自身完整性：▪/◆ 必须是真符号（用错字体时会被替换成 "?"）
    raw_texts = [s.text for s in segs]
    assert any(t.startswith("\u25aa") for t in raw_texts), f"fixture 丢了 ▪: {raw_texts[:5]}"
    assert any(t.strip() == "\u25c6" for t in raw_texts), f"fixture 丢了 ◆: {raw_texts[:5]}"

    calls = {"n": 0}
    real_run_batch = engine._run_batch

    def counting_run_batch(*a, **kw):
        calls["n"] += 1
        return real_run_batch(*a, **kw)

    with _patched(engine, "_run_batch", counting_run_batch):
        res = engine.translate_segments(conn, doc_id, vid, provider="mock", cfg=CFG)

    assert res["failed"] == 0, res
    assert res["translated"] > 0, res
    assert res["carried"] >= 2, res          # 页眉/页脚/页码/纯符号
    assert res["api_calls"] == res["batches"] == calls["n"] > 0, res
    assert res["batches"] >= 1
    assert res["tokens"]["prompt"] > 0 and res["tokens"]["completion"] > 0
    assert res["chars"] > 0

    rows = store.translations_of_version(conn, vid)
    assert len(rows) >= len(seg_ids) - 0, (len(rows), len(seg_ids))
    by_seg = {int(s["id"]): s for s in store.segments_of_version(conn, vid)}
    n_machine = n_carried = 0
    for sid, row in rows.items():
        seg = by_seg[sid]
        if row["status"] == "machine":
            n_machine += 1
            assert row["text"].strip(), f"段 {sid} 译文为空"
            assert seg["role"] == "body"
            assert not engine._is_trivial(seg["text"])
        elif row["status"] == "carried":
            n_carried += 1
            assert row["text"] == seg["text"], f"CARRIED 段 {sid} 文本必须等于原文"
        else:
            raise AssertionError(f"意外状态 {row['status']} on {sid}")
    assert n_machine == res["translated"] and n_carried == res["carried"], (n_machine, n_carried)
    assert n_machine >= 6
    # 术语表确实生效
    joined = "\n".join(r["text"] for r in rows.values())
    assert "主警戒灯" in joined and "应急动力装置" in joined, joined[:400]
    # role != body 一律 CARRIED 原文
    for sid, row in rows.items():
        if by_seg[sid]["role"] != "body":
            assert row["status"] == "carried" and row["text"] == by_seg[sid]["text"]

    # --- 二次运行：必须 0 次后端调用 -----------------------------------
    calls["n"] = 0
    with _patched(engine, "_run_batch", counting_run_batch):
        res2 = engine.translate_segments(conn, doc_id, vid, provider="mock", cfg=CFG)
    assert calls["n"] == 0, f"二次运行不得调用后端: {calls['n']}"
    assert res2["api_calls"] == 0 and res2["translated"] == 0 and res2["failed"] == 0, res2
    assert res2["cached"] >= len(seg_ids), res2
    assert res2["carried"] == 0, res2

    # force=True 时必须重译（仍不算 API 调用，用 mock）
    res3 = engine.translate_segments(conn, doc_id, vid, provider="mock", cfg=CFG, force=True)
    assert res3["translated"] > 0 and res3["failed"] == 0, res3
    conn.close()


@test
def t04_incremental_cache_and_seed():
    """v2：相同段走 translation_cache(CARRIED)；小改段走 links(SEEDED)。"""
    pdf1 = _make_pdf(TMP / "t4v1.pdf", 2, tag="t4")
    conn = _conn()
    doc_id, vid1, ids1, segs1 = _new_version(conn, pdf1, 2, "t4v1")

    # v2 基于同样版面生成，再补一段「小改」文本
    pdf2 = _make_pdf(TMP / "t4v2.pdf", 2, tag="t4")
    doc = pymupdf.open(str(pdf2))
    doc[0].insert_textbox(pymupdf.Rect(72, 600, 540, 660),
                          "The MASTER CAUTION light activates shortly after any "
                          "individual light on the Caution Panel illuminates.",
                          fontsize=10)
    doc.save(str(TMP / "t4v2b.pdf"))
    doc.close()
    pdf2 = TMP / "t4v2b.pdf"

    import hashlib
    raw = pdf2.read_bytes()
    with pymupdf.open(str(pdf2)) as d:
        page_count = d.page_count
    vid2 = store.register_version(conn, doc_id=doc_id, source_path=str(pdf2),
                                  sha256=hashlib.sha256(raw).hexdigest(),
                                  pdf_bytes=len(raw), page_count=page_count,
                                  release_label="4.38.1", label="t4v2")
    segs2 = extract_segments(pdf2, doc_id, vid2)
    ids2 = store.insert_segments(conn, segs2)
    conn.commit()

    # v1 翻译完成
    r1 = engine.translate_segments(conn, doc_id, vid1, provider="mock", cfg=CFG)
    assert r1["failed"] == 0 and r1["translated"] > 0, r1
    tr1 = store.translations_of_version(conn, vid1)

    # 建立 links：同文本段 unchanged，小改段 modified ratio=0.93
    linked = 0
    new_seg_by_id = {int(s["id"]): s for s in store.segments_of_version(conn, vid2)}
    for sid2, s2 in new_seg_by_id.items():
        for idx, s1 in enumerate(segs1):
            if s1.fingerprint == fp_digest(s2["text"]):
                store.record_segment_link(conn, new_segment_id=sid2,
                                          old_segment_id=ids1[idx],
                                          change_kind="unchanged", ratio=1.0)
                linked += 1
                break
    # 小改段：v2 里那条长句与 v1 对应段做 modified link
    target = None
    for sid2, s2 in new_seg_by_id.items():
        flat = _flat(s2["text"])
        if flat.startswith("The MASTER CAUTION light activates shortly after any") \
                and flat.endswith("Caution Panel illuminates."):
            target = sid2
    assert target is not None, "未找到小改段"
    old_id = None
    for idx, s1 in enumerate(segs1):
        flat = _flat(s1.text)
        if flat.startswith("Page 1 step 1 [t4]: The MASTER CAUTION light"):
            old_id = ids1[idx]
            break
    assert old_id is not None, "v1 缺少对应段"
    store.record_segment_link(conn, new_segment_id=target, old_segment_id=old_id,
                              change_kind="modified", ratio=0.93)
    conn.commit()
    assert linked > 0

    calls = {"n": 0}
    real_run_batch = engine._run_batch

    def counting_run_batch(*a, **kw):
        calls["n"] += 1
        return real_run_batch(*a, **kw)

    with _patched(engine, "_run_batch", counting_run_batch):
        r2 = engine.translate_segments(conn, doc_id, vid2, provider="mock", cfg=CFG)

    assert r2["failed"] == 0, r2
    assert r2["carried"] >= 1, f"缓存复用未生效: {r2}"
    assert r2["seeded"] >= 1, f"links 种子未生效: {r2}"
    # 只有需要模型处理的段才进批次；相同段一律不得调用后端
    assert calls["n"] <= 1, f"相同段不应进入批次: {r2}"

    rows2 = store.translations_of_version(conn, vid2)
    assert rows2[target]["status"] == "seeded", rows2[target]
    # 相同文本段必须沿用了 v1 的译文
    for sid2, s2 in new_seg_by_id.items():
        for s1, oid in zip(segs1, ids1):
            if s1.fingerprint == fp_digest(s2["text"]) and oid in tr1:
                assert rows2[sid2]["text"] == tr1[oid]["text"], (
                    sid2, oid, rows2[sid2]["status"],
                    repr(rows2[sid2]["text"]), repr(tr1[oid]["text"]),
                    repr(s2["text"]), repr(s1.text))
                assert rows2[sid2]["status"] == "carried", (sid2, rows2[sid2]["status"])
                break
    conn.close()


# ==========================================================================
# 3. deepseek 后端（打桩 _http_post）
# ==========================================================================
def _fake_response(payload, call_no=1, drop_first=False, fence=True):
    user = payload["messages"][-1]["content"]
    body = json.loads(user[user.rindex('{"segments"'):])
    items = body["segments"][1:] if drop_first else body["segments"]
    zh = {"translations": [{"id": s["id"], "zh": f"译文{call_no}-{s['id']}"} for s in items]}
    content = json.dumps(zh, ensure_ascii=False)
    if fence:
        content = "以下是结果：\n```json\n" + content + "\n```"
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 123, "total_tokens": 444}}


def _fresh_version(conn, tag: str, pages: int = 1):
    """建一个带唯一文本的版本，避免命中 translation_cache。"""
    pdf = TMP / f"{tag}.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 40), f"BMS {tag} Training Manual", fontsize=8)
    texts = [
        f"Ground operations checklist for {tag}: verify the canopy is closed.",
        f"The {tag} EPU RUN light must be off before taxi.",
        f"Set the {tag} STPT and check the HUD symbology.",
        f"Warning: the {tag} landing gear must be down.",
    ]
    y = 120
    for t in texts:
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 50), t, fontsize=10)
        y += 70
    doc.save(str(pdf))
    doc.close()
    return _new_version(conn, pdf, pages, tag)


@test
def t05_deepseek_request_structure():
    conn = _conn()
    doc_id, vid, ids, _segs = _fresh_version(conn, "req")
    seen = {}

    def fake_http_post(url, payload, timeout=None, api_key=""):
        seen.setdefault("urls", []).append(url)
        seen.setdefault("timeouts", []).append(timeout)
        seen["payload"] = payload
        seen["api_key"] = api_key
        return _fake_response(payload)

    with _patched(engine, "_http_post", fake_http_post):
        res = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                        model="deepseek-flash", concurrency=2, cfg=CFG)

    assert res["failed"] == 0, res
    assert res["translated"] >= 3, res
    p = seen["payload"]
    assert p["model"] == "deepseek-flash"
    assert p["response_format"] == {"type": "json_object"}
    assert p["max_tokens"] >= 2048, p["max_tokens"]
    assert p["stream"] is False
    assert [m["role"] for m in p["messages"]] == ["system", "user"]
    assert "术语表" in p["messages"][0]["content"]
    assert "MASTER CAUTION" in p["messages"][0]["content"]
    assert "F-16" in p["messages"][0]["content"]          # 保留型号的要求
    assert p["messages"][0]["content"].count("JSON") >= 1
    assert seen["urls"][0].endswith("/chat/completions"), seen["urls"][0]
    assert seen["urls"][0].startswith("https://api.deepseek.com")
    assert seen["api_key"] == "test-key-not-real", seen["api_key"]
    assert seen["timeouts"][0] == 120
    # usage 从响应体读取
    assert res["tokens"]["prompt"] == 321 * res["api_calls"], res["tokens"]
    assert res["tokens"]["completion"] == 123 * res["api_calls"]
    # 状态 + 译文落库
    rows = store.translations_of_version(conn, vid)
    body_ids = [s["id"] for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
    for sid in body_ids:
        if not engine._is_trivial(store.get_segment(conn, sid)["text"]):
            assert rows[sid]["status"] in ("machine", "seeded"), rows[sid]
            assert rows[sid]["text"].startswith("译文"), rows[sid]
    conn.close()


@test
def t06_deepseek_retry_then_success():
    conn = _conn()
    doc_id, vid, _ids, _segs = _fresh_version(conn, "retry")
    calls = {"n": 0}

    def flaky_http_post(url, payload, timeout=None, api_key=""):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("simulated 500 / connection reset")
        return _fake_response(payload, call_no=calls["n"])

    with _patched(engine, "_http_post", flaky_http_post):
        res = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                        model="deepseek-flash", cfg=CFG)
    assert res["failed"] == 0, res
    assert res["translated"] >= 3, res
    assert calls["n"] == 3, f"应当重试 2 次后成功: {calls['n']}"
    assert res["api_calls"] == 3, res
    conn.close()


@test
def t07_deepseek_missing_ids_and_failure_marking():
    conn = _conn()
    doc_id, vid, _ids, _segs = _fresh_version(conn, "fail")
    calls = {"n": 0}

    def bad_http_post(url, payload, timeout=None, api_key=""):
        calls["n"] += 1
        return _fake_response(payload, drop_first=True)     # 总是缺 1 个 id

    with _patched(engine, "_http_post", bad_http_post):
        res = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                        model="deepseek-flash", cfg=CFG)
    assert res["failed"] >= 1, res
    assert res["translated"] == 0, res
    assert res["errors"], "失败原因必须记录"
    assert calls["n"] >= 3, f"失败必须重试 3 次: {calls['n']}"
    rows = store.translations_of_version(conn, vid)
    failed_ids = [sid for sid, r in rows.items() if r["status"] == "failed"]
    assert failed_ids, "必须落库 FAILED"
    for sid in failed_ids:
        assert rows[sid]["text"] == "", "FAILED 行不得写入原文（否则下次会被误判为已完成）"
    # 失败不得抛异常中断整体
    conn.close()


@test
def t08_deepseek_fatal_error_no_retry():
    conn = _conn()
    doc_id, vid, _ids, _segs = _fresh_version(conn, "fatal")
    calls = {"n": 0}

    def fatal_http_post(url, payload, timeout=None, api_key=""):
        calls["n"] += 1
        raise engine.HttpFatalError("HTTP 401: invalid api key")

    with _patched(engine, "_http_post", fatal_http_post):
        res = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                        model="deepseek-flash", cfg=CFG)
    assert res["failed"] >= 1 and res["translated"] == 0, res
    assert calls["n"] == 1, f"401 不得重试: {calls['n']}"
    conn.close()


@test
def t09_dry_run_no_network():
    conn = _conn()
    doc_id, vid, _ids, _segs = _fresh_version(conn, "dry")
    calls = {"n": 0}

    def counting_http_post(url, payload, timeout=None, api_key=""):
        calls["n"] += 1
        raise AssertionError("dry_run 绝不能发网络请求")

    with _patched(engine, "_http_post", counting_http_post):
        res = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                        model="deepseek-flash", cfg=CFG, dry_run=True)
        # 开着思维链再估一次：同样不得发请求，且必须计入推理 token
        res_on = engine.translate_segments(conn, doc_id, vid, provider="deepseek",
                                           model="deepseek-flash", dry_run=True,
                                           cfg={**CFG, "thinking_mode": "enabled"})
    assert calls["n"] == 0, calls
    assert res["dry_run"] is True
    assert res["api_calls"] == 0
    assert res["translated"] >= 3, res
    assert res["tokens"]["prompt"] > 0 and res["tokens"]["completion"] > 0, res
    assert res["batches"] >= 1
    assert res["cost"]["usd"] > 0 and res["cost"]["cny"] >= res["cost"]["usd"]
    assert res["chars"] > 0
    # 估算假设必须与实际发送的 thinking 开关一致（否则报价会失真）
    a_off = res["cost"]["assumptions"]
    assert a_off["thinking_mode"] == "disabled", a_off
    assert a_off["reasoning_tokens_per_request"] == 0, a_off
    a_on = res_on["cost"]["assumptions"]
    assert a_on["thinking_mode"] == "enabled" and a_on["reasoning_tokens_per_request"] > 0, a_on
    assert res_on["tokens"]["completion"] > res["tokens"]["completion"], (res_on, res)
    assert res_on["cost"]["usd"] > res["cost"]["usd"], (res_on["cost"], res["cost"])
    # 价格取官方 off-peak 档
    assert res["cost"]["usd_per_mtok_in"] == 0.15, res["cost"]
    assert res["cost"]["usd_per_mtok_out"] == 0.60, res["cost"]
    # dry_run 不落库
    rows = store.translations_of_version(conn, vid)
    assert not rows, f"dry_run 不得写译文: {rows}"
    conn.close()


@test
def t10_http_post_local_server():
    """验证 _http_post 真实行为：Bearer 头、JSON 编码、响应解析。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured["path"] = self.path
            captured["auth"] = self.headers.get("Authorization")
            captured["ctype"] = self.headers.get("Content-Type")
            captured["body"] = self.rfile.read(length).decode("utf-8")
            payload = json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
            raw = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):        # 保持输出干净
            pass

    try:
        srv = HTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:                # 沙箱禁止监听则跳过
        print(f"    [skip] 无法启动本地 HTTP 服务: {exc}")
        return
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/chat/completions"
        try:
            resp = engine._http_post(url, {"model": "m", "messages": []}, 10, "sk-abc")
        except OSError as exc:            # 沙箱禁止本地连接则跳过
            print(f"    [skip] 本地 HTTP 连接被拒绝: {exc}")
            return
        assert resp["choices"][0]["message"]["content"] == '{"ok":true}'
        assert captured["auth"] == "Bearer sk-abc", captured
        assert captured["ctype"].startswith("application/json")
        assert json.loads(captured["body"])["model"] == "m"
    finally:
        srv.shutdown()
        srv.server_close()


@test
def t11_config_and_helpers():
    assert engine._is_trivial("") is True
    assert engine._is_trivial("  ") is True
    assert engine._is_trivial("12") is True
    assert engine._is_trivial("\u25aa") is True
    assert engine._is_trivial("-") is True
    assert engine._is_trivial("F-16") is False
    assert engine._is_trivial("Go") is False
    bs = engine._make_batches([engine._Item(i, "x" * 100) for i in range(30)], 12, 1800)
    assert len(bs) == 3, [len(b) for b in bs]
    for b in bs:
        assert len(b) <= 12
        assert sum(len(it.text) for it in b) <= 1800 or len(b) == 1
    cost = engine.estimate_cost("deepseek-flash", 1_000_000, 1_000_000, {})
    assert cost["usd"] > 0 and cost["estimated"] is True
    # 思维链开关：payload 与 dry_run 估算共用 `_thinking_enabled`，两者不得脱节
    msgs = [{"role": "user", "content": "hi"}]
    p_off = engine._build_payload(msgs, "deepseek-flash", {}, 100)
    assert p_off["reasoning_effort"] == "none" and p_off["thinking"] == {"type": "disabled"}
    assert "temperature" in p_off and p_off["max_tokens"] >= 2048
    p_on = engine._build_payload(msgs, "deepseek-flash", {"thinking_mode": "enabled"}, 100)
    assert p_on["thinking"] == {"type": "enabled"} and p_on["reasoning_effort"] == "high"
    assert "temperature" not in p_on, "思维链模式不传 temperature"
    assert engine._thinking_enabled({}) is False
    assert engine._thinking_enabled({"thinking_mode": "ENABLED"}) is True
    assert engine._thinking_enabled({"thinking_mode": "off"}) is False
    # 不可重试错误
    assert issubclass(engine.HttpFatalError, RuntimeError)
    # 数据库重试封装不吞异常
    conn = _conn()
    assert engine._db_retry(lambda: conn.execute("SELECT 1").fetchone()) is not None
    conn.close()


@test
def t12_concurrency_sqlite_locks_and_job_progress():
    """多批次 + 高并发写回：不得出现 database is locked / 单批失败；job 进度要更新。"""
    pdf = _make_pdf(TMP / "t12.pdf", 8, tag="t12")
    conn = _conn()
    doc_id, vid, seg_ids, _segs = _new_version(conn, pdf, 8, "t12")
    assert len(seg_ids) >= 40, len(seg_ids)
    job_id = store.create_job(conn, "translate", {"doc_id": doc_id, "version_id": vid})
    cfg = dict(CFG)
    cfg["batch_segments"] = 6
    cfg["batch_chars"] = 900
    res = engine.translate_segments(conn, doc_id, vid, provider="mock", concurrency=8,
                                    job_id=job_id, cfg=cfg)
    assert res["failed"] == 0, res
    assert res["batches"] >= 6, res
    assert res["translated"] >= 40, res
    assert not res["errors"], res["errors"]
    job = store.get_job(conn, job_id)
    assert job["status"] == "running", job["status"]
    assert float(job["progress"]) >= float(job["total"]) > 0, (job["progress"], job["total"])
    assert "完成" in job["message"] or "已翻译" in job["message"], job["message"]
    # 译文与状态齐全
    rows = store.translations_of_version(conn, vid)
    for sid in seg_ids:
        assert sid in rows, f"段 {sid} 未写回"
        assert rows[sid]["text"].strip(), f"段 {sid} 译文为空"
    conn.close()


# ==========================================================================
# live 真机调用（可选）
# ==========================================================================
def run_live() -> int:
    """1 次真实 API 调用（deepseek-flash，短文本），用于验证打通 + 记录证据。

    仍使用临时 data 目录；API key 显式从仓库 config/local.json 读取，
    不会污染项目 data/app.db。
    """
    import time
    real_cfg_path = ROOT / "config" / "local.json"
    try:
        real_key = json.loads(real_cfg_path.read_text(encoding="utf-8")).get("deepseek_api_key", "")
    except (OSError, ValueError):
        real_key = ""
    if not real_key:
        print("\n[live] 未找到 deepseek_api_key，跳过")
        return 1

    # 一个只有 1 个正文段的极小 PDF -> 正好 1 次 API 调用
    pdf = TMP / "live.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 40), "BMS 4.38.1 Training Manual [live]", fontsize=8)
    page.insert_textbox(pymupdf.Rect(72, 120, 540, 180),
                        "If the EPU RUN light is off, refer to the EP checklists.",
                        fontsize=10)
    doc.save(str(pdf))
    doc.close()

    conn = _conn()
    doc_id, vid, ids, segs = _new_version(conn, pdf, 1, "live1")
    body = [int(s["id"]) for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
    assert len(body) == 1, body

    cfg = {"db_path": DB_PATH, "deepseek_api_key": real_key, "request_timeout": 60,
           "max_retries": 2, "retry_backoff": [1.0]}
    captured: dict = {}
    real_post = engine._http_post

    def logging_post(url, payload, timeout=None, api_key=""):
        resp = real_post(url, payload, timeout, api_key)
        captured["url"] = url
        captured["payload"] = payload
        captured["resp"] = resp
        return resp

    t0 = time.time()
    try:
        with _patched(engine, "_http_post", logging_post):
            res = engine.translate_segments(conn, doc_id, vid, segment_ids=body,
                                            provider="deepseek", model="deepseek-flash",
                                            concurrency=1, cfg=cfg)
    except Exception as exc:                     # 网络失败：记录证据但不卡死
        print(f"\n[live] 真实 API 调用失败：{type(exc).__name__}: {exc}")
        conn.close()
        return 1
    dt = time.time() - t0
    row = store.get_translation(conn, body[0])
    src = store.get_segment(conn, body[0])["text"]
    raw_content = prompt.extract_content(captured.get("resp", {}))
    print("\n--- live deepseek 调用证据 ---")
    print(json.dumps({"model": "deepseek-flash", "url": captured.get("url"),
                      "elapsed_s": round(dt, 2),
                      "translated": res["translated"], "failed": res["failed"],
                      "api_calls": res["api_calls"], "tokens": res["tokens"],
                      "cost_usd": res["cost"]["usd"]}, ensure_ascii=False))
    print(f"  src : {_flat(src)}")
    print(f"  zh  : {(row or {}).get('text')!r}  status={(row or {}).get('status')}")
    sent = {k: v for k, v in (captured.get("payload") or {}).items() if k != "messages"}
    print(f"  payload(no messages): {json.dumps(sent, ensure_ascii=False)[:300]}")
    usage = (captured.get("resp") or {}).get("usage") or {}
    print(f"  usage: {usage}")
    print(f"  reasoning_tokens: "
          f"{(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')}")
    print(f"  raw_content({len(raw_content)}) : {raw_content[:400]!r}")
    conn.close()
    return 0 if res["failed"] == 0 and res["translated"] == 1 else 1


def _real_key() -> str:
    try:
        return json.loads((ROOT / "config" / "local.json").read_text(
            encoding="utf-8")).get("deepseek_api_key", "") or ""
    except (OSError, ValueError):
        return ""


# 真实手册风格的 3 句（含缩写/座舱标注/序号），用于思维链开关下的质量对比
AB_SENTENCES = [
    "The MASTER CAUTION light activates shortly after any individual light on the "
    "Caution Panel illuminates (excluding IFF).",
    "If the EPU RUN light is off, there is a single system B hydraulic failure "
    "(refer to System B Hydraulic failure in the EP checklists).",
    "\u25aa Set the STPT and check the HUD symbology before taxi.",
]
AB_KEEP = ["MASTER CAUTION", "IFF", "EPU", "RUN", "STPT", "HUD"]


def run_live_ab() -> int:
    """2 次真实调用：同一批文本分别用 thinking=disabled / enabled，比较缩写保留与耗时。"""
    import time
    key = _real_key()
    if not key:
        print("\n[live-ab] 未找到 deepseek_api_key，跳过")
        return 1
    pdf = TMP / "live_ab.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 40), "BMS 4.38.1 Training Manual [ab]", fontsize=8)
    y = 120
    for s in AB_SENTENCES:
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 60), s, fontsize=10)
        y += 70
    doc.save(str(pdf))
    doc.close()

    conn = _conn()
    doc_id, vid, _ids, _segs = _new_version(conn, pdf, 1, "ab1")
    body = [int(s["id"]) for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
    out: dict[str, dict] = {}
    for mode in ("disabled", "enabled"):
        cfg = {"db_path": DB_PATH, "deepseek_api_key": key, "request_timeout": 120,
               "max_retries": 1, "thinking_mode": mode, "batch_segments": 12}
        t0 = time.time()
        res = engine.translate_segments(conn, doc_id, vid, segment_ids=body,
                                        provider="deepseek", model="deepseek-flash",
                                        concurrency=1, force=True, cfg=cfg)
        dt = time.time() - t0
        trs = store.translations_of_version(conn, vid)
        outs = [trs[sid]["text"] for sid in body if sid in trs]
        kept = sum(1 for t in outs for tok in AB_KEEP if tok in t)
        out[mode] = {"elapsed_s": round(dt, 2), "tokens": res["tokens"],
                     "cost_usd": res["cost"]["usd"], "kept_abbrev": kept,
                     "texts": outs}

    print("\n--- thinking 开关 A/B（同一批 3 段，deepseek-flash）---")
    for mode in ("disabled", "enabled"):
        d = out[mode]
        print(f"[{mode}] {d['elapsed_s']}s  prompt={d['tokens']['prompt']} "
              f"completion={d['tokens']['completion']} 缩写保留={d['kept_abbrev']}/{len(AB_KEEP)}")
        for t in d["texts"]:
            print(f"    {t}")
    conn.close()
    return 0


# 真实手册第 4 页那一节（Lead 报告被改坏的行）
P4_LINES = [
    "MISSION 5: ILS LANDING AT NIGHT (TR_BMS_05_ILS_Landing) .......... 130",
    "MISSION 1: GROUND OPERATIONS (TR_BMS_01_GroundOPS) .......... 12",
]


def run_live_p4() -> int:
    """1 次真实调用：翻译第 4 页那一节，证明文件名不被术语替换（同时含 ILS/GroundOPS）。"""
    key = _real_key()
    if not key:
        print("\n[live-p4] 未找到 deepseek_api_key，跳过")
        return 1
    pdf = TMP / "live_p4.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 40), "BMS 4.38.1 Training Manual [p4]", fontsize=8)
    y = 120
    for s in P4_LINES:
        page.insert_textbox(pymupdf.Rect(72, y, 540, y + 40), s, fontsize=10)
        y += 50
    doc.save(str(pdf))
    doc.close()

    conn = _conn()
    doc_id, vid, _ids, _segs = _new_version(conn, pdf, 1, "p4")
    body = [int(s["id"]) for s in store.segments_of_version(conn, vid) if s["role"] == "body"]
    assert len(body) == 2, body
    cfg = {"db_path": DB_PATH, "deepseek_api_key": key, "request_timeout": 60,
           "max_retries": 2}
    import time
    t0 = time.time()
    res = engine.translate_segments(conn, doc_id, vid, segment_ids=body,
                                    provider="deepseek", model="deepseek-flash",
                                    concurrency=1, cfg=cfg)
    dt = time.time() - t0
    trs = store.translations_of_version(conn, vid)
    ok = True
    print("\n--- live 第 4 页文件名保留验证 ---")
    print(json.dumps({"elapsed_s": round(dt, 2), "api_calls": res["api_calls"],
                      "translated": res["translated"], "failed": res["failed"],
                      "tokens": res["tokens"]}, ensure_ascii=False))
    for sid in body:
        src = store.get_segment(conn, sid)["text"]
        zh = (trs.get(sid) or {}).get("text", "")
        print(f"  EN: {_flat(src)}")
        print(f"  ZH: {zh}")
        for name in ("TR_BMS_05_ILS_Landing", "TR_BMS_01_GroundOPS"):
            if name in _flat(src):
                intact = name in zh
                ok = ok and intact
                print(f"    {'OK  ' if intact else 'FAIL'} {name} 原样保留={intact}")
        # 术语替换不得插入中文破坏标识符
        for bad in ("_仪表着陆系统", "_着陆", "_地面运行"):
            if bad in zh:
                ok = False
                print(f"    FAIL 标识符被破坏: {bad!r}")
    conn.close()
    print(f"  => {'PASS' if ok else 'FAIL'}")
    return 0 if ok and res["failed"] == 0 else 1


def main() -> int:
    passed = failed = 0
    for fn in TESTS:
        name = fn.__name__
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\n{passed} passed, {failed} failed  (tmp={TMP})")
    return 1 if failed else 0


if __name__ == "__main__":
    rc = main()
    if "--live" in sys.argv and rc == 0:
        try:
            rc = run_live()
        except Exception:
            traceback.print_exc()
            rc = 1
    if "--live-ab" in sys.argv and rc == 0:
        try:
            rc = run_live_ab()
        except Exception:
            traceback.print_exc()
            rc = 1
    if "--live-p4" in sys.argv and rc == 0:
        try:
            rc = run_live_p4()
        except Exception:
            traceback.print_exc()
            rc = 1
    sys.exit(rc)
