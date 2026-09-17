"""core 数据层测试（CONTRACT.md §2 §3 §4 §6-pdfdoc）。

pytest 兼容，同时**必须**能直接运行：
    python tests\\test_core.py        # 退出码 0 = 全部通过
环境变量 `MT_SKIP_REAL=1` 可跳过真实 PDF 健壮性用例（默认在存在 origin PDF 时执行）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from core import db as coredb  # noqa: E402
from core import fingerprint as fp  # noqa: E402
from core import pdfdoc  # noqa: E402
from core import store  # noqa: E402
from core.models import Segment  # noqa: E402

REAL_PDF = ROOT / "origin" / "BMS-Training-Manual.pdf"
REAL_PAGE = 21          # 1-based，契约里做重建验证的那一页


# --------------------------------------------------------------------------- #
# 测试基础设施
# --------------------------------------------------------------------------- #
class TempWorkspace:
    """每个用例一个临时目录（沙箱只允许会话 TEMP，tempfile 正好落在那里）。"""

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="mt_core_"))
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _conn(tmp: Path) -> sqlite3.Connection:
    return coredb.connect(tmp / "app.db")


def _seed(conn: sqlite3.Connection, *, slug: str = "bms", segs: int = 4) -> tuple[int, int]:
    doc_id = store.upsert_document(conn, slug=slug, title="BMS Training Manual",
                                   source_dir="origin")
    vid = store.register_version(conn, doc_id=doc_id, source_path="origin/x.pdf",
                                 sha256=f"sha-{slug}", pdf_bytes=123, page_count=3)
    if segs:
        store.insert_segments(conn, [
            Segment(version_id=vid, doc_id=doc_id, page=1 + i % 2, order_index=i,
                    kind="paragraph", text=f"Paragraph number {i} about the FLCS.",
                    bbox=[72.0, 100.0 + i * 20, 500.0, 112.0 + i * 20],
                    line_boxes=[[72.0, 100.0 + i * 20, 500.0, 112.0 + i * 20]],
                    fonts=[{"font": "Calibri", "size": 10.0, "color": 0, "flags": 0, "count": 32}],
                    style={"bold": False, "size": 10.0})
            for i in range(segs)
        ])
    return doc_id, vid


def assert_close(a: float, b: float, tol: float = 1e-6) -> None:
    assert abs(float(a) - float(b)) <= tol, f"{a} != {b} (tol {tol})"


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #
def test_normalize_is_idempotent_and_unifies() -> None:
    raw = "The  quick\u00a0brown \u201cFox\u201d \u2014 jumps\u2026 \u2018now\u2019 "
    norm = fp.normalize(raw)
    assert norm == 'The quick brown "Fox" - jumps... \'now\'', repr(norm)
    assert fp.normalize(norm) == norm, "normalize 必须幂等"
    assert fp.normalize(None) == ""
    assert fp.normalize("") == ""
    # 大小写保留
    assert fp.normalize("ABC def") == "ABC def"
    # 零宽字符被删除、全角空白折叠成普通空格
    assert fp.normalize("a\u200bb\u3000c") == "ab c"


def test_fingerprint_and_content_hash() -> None:
    a = "The MASTER  CAUTION light \u2014 activates."
    b = 'the master caution light - activates.'
    assert fp.fingerprint(a) == fp.fingerprint(b), "归一化等价文本必须同指纹"
    assert len(fp.fingerprint(a)) == 32, "blake2b digest_size=16 → 32 位 hex"
    assert fp.content_hash(a) != fp.content_hash(b), "content_hash 保留大小写"
    assert fp.fingerprint(None) == fp.fingerprint("")
    assert fp.content_hash("A") != fp.content_hash("a")


def test_ratio() -> None:
    assert fp.ratio("Hello world", "Hello world") == 1.0
    assert fp.ratio("Hello  world\u2014now", "Hello world-now") == 1.0, "ratio 先 normalize"
    assert fp.ratio("ABC", "abc") < 1.0, "ratio 不 casefold（契约如此）"
    assert fp.ratio("abc", "") == 0.0
    mid = fp.ratio("The quick brown fox", "The quick red fox")
    assert 0.5 < mid < 1.0, mid


def test_char_diffs_merges_adjacent_ops() -> None:
    old = "The quick brown fox jumps"
    new = "The quick red fox leaps"
    ops = fp.char_diffs(old, new)
    assert ops, "应当有差异"
    assert {o["op"] for o in ops} <= {"equal", "delete", "insert"}, ops
    for prev, cur in zip(ops, ops[1:]):
        assert prev["op"] != cur["op"], f"相邻同类 op 未合并: {ops}"
    assert all(o["text"] for o in ops), f"不应有空文本 op: {ops}"
    rebuilt_old = "".join(o["text"] for o in ops if o["op"] in ("equal", "delete"))
    rebuilt_new = "".join(o["text"] for o in ops if o["op"] in ("equal", "insert"))
    assert rebuilt_old == fp.normalize(old)
    assert rebuilt_new == fp.normalize(new)
    assert fp.char_diffs("same", "same") == [{"op": "equal", "text": "same"}]
    assert fp.char_diffs("", "") == []
    # replace 必须拆成 delete 后 insert
    ops2 = fp.char_diffs("abc", "xyz")
    assert [o["op"] for o in ops2] == ["delete", "insert"], ops2


def test_tokenize_cjk() -> None:
    toks = fp.tokenize_cjk("F-16 战斗机 OBOGS 123")
    kinds = [k for k, _ in toks]
    assert "cjk" in kinds and "latin" in kinds and "digit" in kinds and "space" in kinds
    assert ("cjk", "战斗机") in toks
    assert ("digit", "123") in toks
    assert ("latin", "F") in toks
    assert fp.tokenize_cjk("") == []
    # 中文标点归 cjk（全角，随中文字体）
    assert ("cjk", "你好，世界") in fp.tokenize_cjk("你好，世界")


def test_dehyphenate_rules() -> None:
    # 软连字符（尾部是典型后缀片段）→ 删除 "-" 直接拼接
    assert fp.dehyphenate(["... light on the Cau-", "tion Panel illuminates."]) == \
        "... light on the Cau" + "tion Panel illuminates."
    # 大写/数字续行 → 保留 "-" 且**不加空格**（Verifier M-2 修复）
    assert fp.dehyphenate(["ANTI-", "Skid"]) == "ANTI-Skid"
    assert fp.dehyphenate(["The F-", "16 block 50"]) == "The F-16 block 50"
    assert fp.dehyphenate(["Dash-", "34"]) == "Dash-34"
    # 单独的项目符号行 → 不当软连字符
    assert fp.dehyphenate(["- ", "item text"]) == "- item text"
    # 普通换行 → 单空格拼接
    assert fp.dehyphenate(["line one", "line two"]) == "line one line two"
    assert fp.dehyphenate([]) == ""
    assert fp.dehyphenate([""]) == ""
    # 行尾破折号（"-" 前有空格）→ 保留破折号 + 单空格
    assert fp.dehyphenate(["see the warning -", "then continue"]) == "see the warning - then continue"
    # 真实句子完整拼接
    got = fp.dehyphenate([fp.normalize("this method, you can fine-tune Betty sounds while increas-"),
                          "ing relevant COMMS volume."])
    assert "increasing relevant COMMS volume." in got, got
    # dehyphenate 之后才该做 normalize（normalize 本身不动连字符）
    assert fp.normalize("Cau-\ntion") != fp.normalize("Caution")
    assert fp.dehyphenate(["Cau-", "tion"]) == "Caution"


def test_dehyphenate_acceptance_cases() -> None:
    """Lead/Verifier 指定的验收断言（逐条）。"""
    assert fp.dehyphenate(["air-", "to-air missiles"]) == "air-to-air missiles"
    assert fp.dehyphenate(["AGM-", "65G"]) == "AGM-65G"
    assert fp.dehyphenate(["Multi-", "Function"]) == "Multi-Function"
    assert fp.dehyphenate(["the Cau-", "tion Panel"]) == "the Caution Panel"
    assert fp.dehyphenate(["increas-", "ing relevant volume"]) == "increasing relevant volume"


def test_dehyphenate_real_book_regressions() -> None:
    """Verifier M-1/M-2 报出的真实实例（行对来自 origin/BMS-Training-Manual.pdf 原始行）。"""
    # M-1：真实复合词不得被拼坏（旧规则会删连字符）
    compounds = [
        (["air-", "to-air refueling"], "air-to-air refueling", "p127"),
        (["edge-of-", "display segments"], "edge-of-display segments", "p44"),
        (["self-", "defense systems"], "self-defense systems", "p373"),
        (["They are mode-", "dependent"], "They are mode-dependent", "p338"),
        (["or pre-", "briefed."], "or pre-briefed.", "p40"),
        (["the first-", "level failure"], "the first-level failure", "p64"),
        (["a cross-", "turns"], "a cross-turns", "p110"),
        (["high off-", "boresight"], "high off-boresight", "p323"),
        (["a pop-", "up maneuver"], "a pop-up maneuver", "p337"),
        (["enemy surface-", "based air"], "enemy surface-based air", "p373"),
        (["higher-than-", "normal"], "higher-than-normal", "p51"),
        (["fired at R-", "aero range"], "fired at R-aero range", "p308"),
        (["At R-", "pi the ASEC"], "At R-pi the ASEC", "p308"),
    ]
    for lines, want, where in compounds:
        got = fp.dehyphenate(lines)
        assert got == want, f"{where}: {lines} -> {got!r} 期望 {want!r}"
    # M-2：大写/数字续行不得插入空格
    spaced = [
        (["including Multi-", "Function Displays (MFDs)"], "including Multi-Function Displays (MFDs)"),
        (["(check the ENABLE ROLL-", "LINKED NWS)"], "(check the ENABLE ROLL-LINKED NWS)",),
        (["use ELEC CAUTION RE-", "SET button"], "use ELEC CAUTION RE-SET button"),
        (["PPTs (Pre-", "Planned Targets)"], "PPTs (Pre-Planned Targets)"),
        (["power on the AGM-", "65G"], "power on the AGM-65G"),
        (["handling of the AGM-", "65K"], "handling of the AGM-65K"),
        (["the R-27R (AA-", "10A)"], "the R-27R (AA-10A)"),
        (["a spacing between 500-", "3000ft"], "a spacing between 500-3000ft"),
        (["a distance between 4000-", "6000ft"], "a distance between 4000-6000ft"),
        (["the first column is angled about 10-", "15\u00b0"],
         "the first column is angled about 10-15\u00b0"),
        (["see BMS1-F16CM-", "34-1-1"], "see BMS1-F16CM-34-1-1"),
        (["Team #5-", "#8 is set by BMS"], "Team #5-#8 is set by BMS"),
    ]
    for lines, want in spaced:
        got = fp.dehyphenate(lines)
        assert got == want, f"{lines} -> {got!r} 期望 {want!r}"

    # 真断词仍必须删除（尾部是后缀片段）
    soft = [
        (["The MASTER CAUTION light activates shortly after the Cau-",
          "tion Panel illuminates."], "Caution Panel"),
        (["while increas-", "ing relevant COMMS volume."], "increasing relevant COMMS volume."),
        (["the gyrosco-", "pic unit"], "the gyroscopic unit"),
        (["electri-", "cal power"], "electrical power"),
        (["compo-", "nent failure"], "component failure"),
        (["sys-", "tem reset"], "system reset"),
        (["pilots to person-", "alize their setups"], "personalize their setups"),
        (["the transponder and interrogator func-", "tions operate"], "functions operate"),
    ]
    for lines, want in soft:
        got = fp.dehyphenate(lines)
        assert want in got, f"{lines} -> {got!r} 期望包含 {want!r}"


def test_dehyphenate_list_markers_and_ranges() -> None:
    """独占一行的编号列表标记（p320）不能与正文粘连；区间写法（p391）不能拆开。"""
    assert fp.dehyphenate(["1- ", "Never fly straight "]) == "1- Never fly straight"
    assert fp.dehyphenate(["2- ", "Deflect shots on you -> Jink "]) == \
        "2- Deflect shots on you -> Jink"
    assert fp.dehyphenate(["4- ", "Create BFM problems"]) == "4- Create BFM problems"
    assert fp.dehyphenate(["10- ", "never fly straight"]) == "10- never fly straight"
    # 区间/编号不是独占一行时按词内连字符处理
    assert fp.dehyphenate(["between 500-", "3000ft"]) == "between 500-3000ft"
    assert fp.dehyphenate(["50-", "50 "]) == "50-50"
    assert fp.dehyphenate(["about 10-", "15\u00b0"]) == "about 10-15\u00b0"
    # 纯数字续行即使语料里有该数字也不能判为断词（p287 Team #5- / #8）
    assert fp.dehyphenate(["Team #5-", "#8 is set by BMS"]) == "Team #5-#8 is set by BMS"
    assert fp.dehyphenate(["Team #5-", "#8 is set by BMS"], vocabulary={"58": 9}) == \
        "Team #5-#8 is set by BMS"
    # 编号标记 + 词内连字符在解释里理由不同
    reasons = [d["reason"] for d in
               fp.explain_hyphen_joins(["1- ", "Never fly straight", "50-", "50 "])]
    assert reasons == ["list-marker", "no-trailing-hyphen", "upper-or-digit"], reasons


def test_build_vocabulary_and_corpus_mode() -> None:
    """语料词表构建 + 与 dehyphenate 的联动（ingest 路径）。"""
    from collections import Counter

    vocab = fp.build_vocabulary(["The MASTER CAUTION light activates.", "the Cau-",
                                 "tion Panel illuminates"])
    assert isinstance(vocab, Counter)
    assert vocab["caution"] == 1 and vocab["cau"] == 1 and vocab["tion"] == 1
    assert fp.dehyphenate(["the Cau-", "tion Panel"], vocabulary=vocab) == "the Caution Panel"
    counts = fp.build_vocabulary(["alpha beta", "alpha gamma", "alpha beta"], min_count=2)
    assert counts["alpha"] == 3 and counts["beta"] == 2 and "gamma" not in counts
    assert fp.build_vocabulary([]) == Counter()
    assert fp.build_vocabulary(None) == Counter()          # 空输入不抛异常


def test_fixture_corpus_vocabulary_joins_soft_hyphen() -> None:
    """用合成 PDF 的真实抽取行建词表，再拼回软连字符段落。"""
    from tests import fixtures as fx

    with TempWorkspace() as tmp:
        path = fx.make_synthetic_pdf(str(tmp / "syn.pdf"), pages=3)
        doc = pdfdoc.open_pdf(path)
        try:
            lines: list[str] = []
            for page in doc:
                lines.extend(page.get_text().splitlines())
        finally:
            doc.close()
        vocab = fp.build_vocabulary(lines)
        assert vocab["caution"] >= 1, "合成 PDF 里 Caution 应完整出现过"
        joined = fp.dehyphenate([fx.HYPHEN_FIRST_LINE, fx.HYPHEN_SECOND_LINE], vocabulary=vocab)
        assert "Caution Panel illuminates brightly." in joined, joined


def test_dehyphenate_vocabulary_mode() -> None:
    """语料词表模式：拼接形式在语料中出现过、连字符写法没有 → 判为断词。"""
    vocab = {"caution", "increasing", "helicopter", "air-to-air", "pop-up", "popup", "flcs"}
    assert fp.dehyphenate(["the Cau-", "tion Panel"], vocabulary=vocab) == "the Caution Panel"
    assert fp.dehyphenate(["the hel-", "icopter hovers"], vocabulary=vocab) == \
        "the helicopter hovers"                     # 尾部 "icopter" 不在后缀表，靠语料判定
    assert fp.dehyphenate(["air-", "to-air refueling"], vocabulary=vocab) == "air-to-air refueling"
    # 大小写不敏感
    assert fp.dehyphenate(["the Cau-", "tion Panel"], vocabulary={"CAUTION"}) == "the Caution Panel"
    # Counter：两种写法都出现过 → 词频高者胜（pop-up 5 次 vs popup 1 次 → 保留连字符）
    from collections import Counter
    counter = Counter({"pop-up": 5, "popup": 1, "caution": 40})
    assert fp.dehyphenate(["a pop-", "up attack"], vocabulary=counter) == "a pop-up attack"
    # 集合同样两种都有 → 无法判定时保守保留连字符
    assert fp.dehyphenate(["a pop-", "up attack"], vocabulary={"pop-up", "popup"}) == \
        "a pop-up attack"
    # 词频相反时判为断词
    assert fp.dehyphenate(["a pop-", "up attack"], vocabulary=Counter({"popup": 9, "pop-up": 1})) == \
        "a popup attack"
    # 语料为空/None 时退化为保守策略（后缀片段规则仍工作）
    assert fp.dehyphenate(["the Cau-", "tion Panel"], vocabulary=set()) == "the Caution Panel"
    assert fp.dehyphenate(["air-", "to-air refueling"], vocabulary=None) == "air-to-air refueling"


def test_dehyphenate_is_idempotent_and_explainable() -> None:
    """判定必须幂等且可解释（Verifier M-3）。"""
    cases = [
        ["... light on the Cau-", "tion Panel illuminates.", "Then continue."],
        ["air-", "to-air refueling"],
        ["AGM-", "65G"],
        ["see the warning -", "then continue"],
        ["- ", "item text"],
        ["ANTI-"],
        ["line one", "line two", "line three"],
    ]
    for lines in cases:
        once = fp.dehyphenate(lines)
        assert fp.dehyphenate(once.splitlines()) == once, f"不幂等: {lines} -> {once!r}"
        assert fp.normalize(once) == once, f"结果应已是归一化空白: {once!r}"
    # explain 与 dehyphenate 用同一判定：action 与实际拼接一致
    lines = ["the Cau-", "tion Panel", "and the air-", "to-air missile", "AGM-", "65G"]
    decisions = fp.explain_hyphen_joins(lines)
    assert len(decisions) == len(lines) - 1
    assert [d["reason"] for d in decisions] == [
        "suffix-fragment", "no-trailing-hyphen", "conservative-compound",
        "no-trailing-hyphen", "upper-or-digit",
    ], decisions
    assert [d["action"] for d in decisions] == ["soft", "space", "hard", "space", "hard"]
    assert decisions[0]["soft_form"] == "caution" and decisions[0]["hard_form"] == "cau-tion"
    assert decisions[2]["soft_form"] == "airto-air" and decisions[2]["hard_form"] == "air-to-air"
    assert fp.dehyphenate(lines) == "the Caution Panel and the air-to-air missile AGM-65G"
    # 语料词表也会体现在解释里
    explained = fp.explain_hyphen_joins(["the hel-", "icopter"], vocabulary={"helicopter"})
    assert explained[0]["action"] == "soft" and explained[0]["reason"] == "corpus-attested"
    assert fp.explain_hyphen_joins(["only one line"]) == []
    assert fp.explain_hyphen_joins([]) == []


# --------------------------------------------------------------------------- #
# db / store
# --------------------------------------------------------------------------- #
def test_db_schema_and_pragmas() -> None:
    with TempWorkspace() as tmp:
        path = tmp / "sub" / "nested" / "app.db"
        conn = coredb.connect(path)      # 父目录不存在：应自动创建
        assert path.is_file()
        try:
            assert coredb.schema_version(conn) == coredb.SCHEMA_VERSION
            assert coredb.ensure_schema(conn) == coredb.SCHEMA_VERSION   # 可重复执行
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("documents", "document_versions", "segments", "translations",
                          "translation_cache", "segment_links", "version_diffs",
                          "render_snapshots", "jobs", "glossary"):
                assert table in names, f"缺少表 {table}"
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(segments)")}
            assert "canonical_text" in cols and "fingerprint" in cols
            idx = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            for name in ("ix_segments_version_order", "ix_segments_version_page",
                         "ix_segments_version_fp", "ix_segments_version_role"):
                assert name in idx, f"缺少索引 {name}"
            assert conn.row_factory is sqlite3.Row
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        finally:
            conn.close()
        # 重新打开：schema 不重复建、数据保留
        conn2 = coredb.connect(path)
        try:
            assert coredb.schema_version(conn2) == coredb.SCHEMA_VERSION
        finally:
            conn2.close()


def test_store_documents_and_versions() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id = store.upsert_document(conn, slug="bms", title="BMS Manual", source_dir="origin")
            assert doc_id > 0
            assert store.upsert_document(conn, slug="bms", title="", source_dir="") == doc_id
            doc = store.get_document(conn, doc_id)
            assert doc and doc["doc_id"] == doc_id and doc["title"] == "BMS Manual"

            v1 = store.register_version(conn, doc_id=doc_id, source_path="origin/a.pdf",
                                        sha256="aaa", pdf_bytes=10, page_count=3,
                                        release_label="4.38.1", doc_date="2024-01-01")
            v2 = store.register_version(conn, doc_id=doc_id, source_path="origin/b.pdf",
                                        sha256="bbb", pdf_bytes=20, page_count=4, label="v2")
            assert (v1, v2) != (0, 0) and v1 != v2
            assert store.current_version(conn, doc_id)["version_id"] == v2
            assert [v["version_no"] for v in store.list_versions(conn, doc_id)] == [1, 2]
            assert store.get_version(conn, v1)["label"] == "v1"

            # 同 sha256 幂等复用
            again = store.register_version(conn, doc_id=doc_id, source_path="origin/a.pdf",
                                          sha256="aaa", pdf_bytes=10, page_count=3,
                                          make_current=False)
            assert again == v1, "相同 sha256 必须复用已有版本"

            store.set_current_version(conn, doc_id, v1)
            assert store.current_version(conn, doc_id)["version_id"] == v1
            assert store.get_version(conn, v1)["is_current"] == 1
            assert store.get_version(conn, v2)["is_current"] == 0

            store.update_version_stats(conn, v2, segments=1234, chars=56789, pages=4, images=88)
            store.update_version_stats(conn, v2, segments=1300)
            stats = store.get_version(conn, v2)["stats"]
            assert stats == {"segments": 1300, "chars": 56789, "pages": 4, "images": 88}, stats
            store.update_version_stats(conn, v1, segments=42)
            assert store.get_version(conn, v1)["stats"] == {"segments": 42}

            docs = store.list_documents(conn)
            assert len(docs) == 1
            assert docs[0]["version_count"] == 2
            assert docs[0]["current_version_id"] == v1
            assert docs[0]["page_count"] == 3
            assert docs[0]["stats"] == {"segments": 42}, "list_documents 取 current 版本 stats"
            store.set_current_version(conn, doc_id, v2)
            assert store.list_documents(conn)[0]["stats"]["segments"] == 1300
        finally:
            conn.close()


def test_store_segments_roundtrip_and_autofill() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id, vid = _seed(conn, segs=0)
            segs = [
                Segment(version_id=vid, doc_id=doc_id, page=1, order_index=0, kind="heading",
                        text="1.1  Systems  Overview",
                        bbox=[72.0, 90.0, 300.0, 104.0], line_boxes=[[72.0, 90.0, 300.0, 104.0]],
                        fonts=[{"font": "Calibri-Bold", "size": 12.0, "color": 0,
                                "flags": 16, "count": 20}],
                        style={"bold": True, "size": 12.0}),
                Segment(version_id=vid, doc_id=doc_id, page=1, order_index=1,
                        text="The Cau-\ntion Panel illuminates.", role="body"),
                Segment(version_id=vid, doc_id=doc_id, page=2, order_index=2,
                        kind="list_item", text="1. First step.", role="body"),
            ]
            ids = store.insert_segments(conn, segs)
            assert len(ids) == 3 and ids == sorted(ids) and len(set(ids)) == 3
            assert [s.id for s in segs] == ids, "补算的 id 应写回 Segment"

            # 自动补算 canonical_text / normalized / fingerprint / content_hash
            second = segs[1]
            assert second.canonical_text == "The Caution Panel illuminates.", second.canonical_text
            assert second.normalized == fp.normalize(second.canonical_text)
            assert second.fingerprint == fp.fingerprint(second.canonical_text)
            assert second.content_hash == fp.content_hash(second.canonical_text)

            row = store.get_segment(conn, ids[0])
            assert row["seg_id"] == ids[0] == row["id"]
            assert row["kind"] == "heading" and row["bbox"] == [72.0, 90.0, 300.0, 104.0]
            assert row["fonts"][0]["font"] == "Calibri-Bold" and row["style"]["bold"] is True
            assert row["canonical_text"] == "1.1 Systems Overview"

            rows = store.segments_of_version(conn, vid)
            assert [r["order_index"] for r in rows] == [0, 1, 2]
            assert [r["id"] for r in store.segments_of_version(conn, vid, page=1)] == ids[:2]
            assert [r["id"] for r in store.segments_of_version(conn, vid, kinds="list_item")] == ids[2:]
            assert len(store.segments_of_version(conn, vid, kinds=["heading", "list_item"])) == 2
            assert store.count_segments(conn, vid) == 3
            by_id = store.segments_by_ids(conn, ids)
            assert set(by_id) == set(ids) and by_id[ids[2]]["text"] == "1. First step."
            assert store.segments_by_ids(conn, []) == {}
            assert store.get_segment(conn, 999999) is None
        finally:
            conn.close()


def test_store_insert_segments_performance() -> None:
    """最热路径：4000 段批量插入必须 < 2 秒（executemany + 单事务）。"""
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id, vid = _seed(conn, segs=0)
            segs = [
                Segment(version_id=vid, doc_id=doc_id, page=1 + i // 40, order_index=i,
                        kind="paragraph",
                        text=f"Segment {i}: the FLCS provides electrical power to the "
                             f"flight control surfaces through redundant channels.",
                        bbox=[72.0, 100.0 + (i % 40) * 16, 500.0, 112.0 + (i % 40) * 16])
                for i in range(4000)
            ]
            start = time.perf_counter()
            ids = store.insert_segments(conn, segs)
            elapsed = time.perf_counter() - start
            assert len(ids) == 4000
            assert store.count_segments(conn, vid) == 4000
            assert elapsed < 2.0, f"4000 段插入耗时 {elapsed:.3f}s，要求 < 2s"
            print(f"    [perf] insert 4000 segments: {elapsed * 1000:.0f} ms")
        finally:
            conn.close()


def test_store_translations() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id, vid = _seed(conn)
            ids = [r["id"] for r in store.segments_of_version(conn, vid)]
            assert store.get_translation(conn, ids[0]) is None
            tid = store.upsert_translation(conn, segment_id=ids[0], text="主警戒灯亮起。",
                                           status="machine", provider="deepseek",
                                           model="deepseek-v4-pro", confidence=0.9)
            assert tid > 0
            row = store.get_translation(conn, ids[0])
            assert row["revision"] == 1 and row["text"] == "主警戒灯亮起。"
            assert row["seg_id"] == ids[0]

            same = store.upsert_translation(conn, segment_id=ids[0], text="主警戒灯亮起。",
                                            status="reviewed")
            assert same == tid and store.get_translation(conn, ids[0])["revision"] == 1, \
                "文本未变不该涨 revision"
            store.upsert_translation(conn, segment_id=ids[0], text="主警戒灯会亮起。",
                                     status="seeded", reuse_of=ids[1])
            row = store.get_translation(conn, ids[0])
            assert row["revision"] == 2 and row["reuse_of"] == ids[1]

            store.upsert_translation(conn, segment_id=ids[1], text="原文未变，沿用旧译。",
                                     status="carried")
            store.set_translation_status(conn, ids[1], "reviewed", reviewed_by="brian")
            row = store.get_translation(conn, ids[1])
            assert row["status"] == "reviewed" and row["reviewed_by"] == "brian"
            store.set_translation_status(conn, 999999, "reviewed")   # 缺失段：no-op 不抛异常

            all_tr = store.translations_of_version(conn, vid)
            assert set(all_tr) == {ids[0], ids[1]}

            stats = store.translation_stats(conn, vid)
            assert stats["total"] == 4 and stats["translated"] == 2
            assert stats["by_status"]["reviewed"] == 1
            assert stats["by_status"]["seeded"] == 1
            assert stats["by_status"]["missing"] == 2
            assert stats["chars"] == len("主警戒灯会亮起。") + len("原文未变，沿用旧译。")

            # 跨版本缓存
            fpr = fp.fingerprint("The MASTER CAUTION light activates.")
            assert store.get_cached_translation(conn, fpr) is None
            store.put_cached_translation(conn, fingerprint=fpr, text="主警戒灯亮起。",
                                         provider="mock", model="mock")
            cached = store.get_cached_translation(conn, fpr)
            assert cached and cached["text"] == "主警戒灯亮起。" and cached["hits"] == 1
            store.put_cached_translation(conn, fingerprint=fpr, text="主警戒灯亮起（修订）。")
            assert store.get_cached_translation(conn, fpr)["text"] == "主警戒灯亮起（修订）。"
            assert store.get_cached_translation(conn, "deadbeef") is None
        finally:
            conn.close()


def test_store_links_and_diff_summary() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id = store.upsert_document(conn, slug="d", title="D", source_dir="o")
            v1 = store.register_version(conn, doc_id=doc_id, source_path="a.pdf", sha256="1",
                                        pdf_bytes=1, page_count=1, make_current=False)
            v2 = store.register_version(conn, doc_id=doc_id, source_path="b.pdf", sha256="2",
                                        pdf_bytes=1, page_count=1)
            old_ids = store.insert_segments(conn, [
                Segment(version_id=v1, doc_id=doc_id, page=1, order_index=i,
                        text=f"Old paragraph {i}.", bbox=[0, 0, 100, 10])
                for i in range(3)
            ])
            new_ids = store.insert_segments(conn, [
                Segment(version_id=v2, doc_id=doc_id, page=1, order_index=i,
                        text=f"Old paragraph {i}.", bbox=[0, 0, 100, 10])
                for i in range(3)
            ])

            store.record_segment_link(conn, new_segment_id=new_ids[0], old_segment_id=old_ids[0],
                                      change_kind="modified", ratio=0.8,
                                      char_diffs=fp.char_diffs("Old paragraph 0.", "New paragraph 0."))
            store.record_segment_link(conn, new_segment_id=new_ids[1], old_segment_id=old_ids[1],
                                      change_kind="moved", ratio=1.0)
            store.record_segment_link(conn, new_segment_id=None, old_segment_id=old_ids[2],
                                      change_kind="removed", ratio=0.1)
            # 幂等：重复记录同一组合不产生重复行
            store.record_segment_link(conn, new_segment_id=new_ids[1], old_segment_id=old_ids[1],
                                      change_kind="moved", ratio=1.0)
            links = store.links_for_version(conn, v2)
            assert set(links) == {new_ids[0], new_ids[1]}, links
            assert links[new_ids[0]]["char_diffs"][0]["op"] in ("equal", "delete", "insert")
            assert links[new_ids[0]]["change_id"] > 0
            # links_for_diff 能带出 REMOVED（new 为 NULL）
            diff_links = store.links_for_diff(conn, v1, v2)
            assert len(diff_links) == 3, diff_links
            assert {item["change_kind"] for item in diff_links} == {"modified", "moved", "removed"}

            summary = store.diff_summary(conn, doc_id, v1, v2)
            assert summary["counts"]["modified"] == 1
            assert summary["counts"]["moved"] == 1
            assert summary["counts"]["removed"] == 1
            assert summary["counts"]["unchanged"] == 1, "未配对的第 3 段应为 unchanged"
            assert summary["total"] == 3 and summary["source"] == "links"
            assert summary["from"] == v1 and summary["to"] == v2

            store.write_version_diff(conn, doc_id=doc_id, from_version_id=v1, to_version_id=v2,
                                     counts={"modified": 7})
            assert store.get_version_diff(conn, doc_id, v1, v2)["counts"] == {"modified": 7}
            empty = store.diff_summary(conn, doc_id, 999, 998)
            assert set(empty["counts"]) == set(empty["counts"]) and empty["source"] == "empty"
            assert empty["counts"]["modified"] == 0
        finally:
            conn.close()


def test_store_snapshots_jobs_glossary() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id, vid = _seed(conn, segs=0)
            sid = store.upsert_render_snapshot(conn, version_id=vid, kind="page:1",
                                               path="data/cache/p1.webp", params_hash="dpi110")
            assert sid > 0
            snap = store.get_render_snapshot(conn, vid, "page:1", "dpi110")
            assert snap["path"] == "data/cache/p1.webp" and snap["stale"] == 0
            sid2 = store.upsert_render_snapshot(conn, version_id=vid, kind="page:1",
                                                path="data/cache/p1b.webp", params_hash="dpi110",
                                                bytes_=2048)
            assert sid2 == sid, "同 (version, kind, params_hash) 必须幂等"
            assert store.get_render_snapshot(conn, vid, "page:1", "dpi110")["bytes"] == 2048
            store.upsert_render_snapshot(conn, version_id=vid, kind="page:2",
                                         path="data/cache/p2.webp", params_hash="dpi110")
            assert store.find_render_snapshot(conn, vid, "page:1")["snapshot_id"] == sid
            store.mark_renders_stale(conn, vid, kinds=["page:1"])
            assert store.get_render_snapshot(conn, vid, "page:1", "dpi110")["stale"] == 1
            assert store.find_render_snapshot(conn, vid, "page:1") is None
            assert store.find_render_snapshot(conn, vid, "page:2") is not None
            store.mark_renders_stale(conn, vid)
            assert store.get_render_snapshot(conn, vid, "page:2", "dpi110")["stale"] == 1
            assert store.get_render_snapshot(conn, vid, "nope", "x") is None

            job = store.create_job(conn, "translate", {"doc_id": doc_id, "page_to": 30})
            assert job > 0
            assert store.get_job(conn, job)["status"] == "queued"
            store.update_job(conn, job, status="running", progress=3, total=12, message="批 1")
            store.update_job(conn, job, status="done", progress=12, result={"translated": 12},
                             error="")
            row = store.get_job(conn, job)
            assert row["status"] == "done" and row["params"]["page_to"] == 30
            assert row["result"] == {"translated": 12} and row["progress"] == 12
            assert [j["job_id"] for j in store.list_jobs(conn, limit=5)] == [job]
            store.update_job(conn, 999999, status="done")     # 缺失 job：no-op
            assert store.get_job(conn, 999999) is None
            assert store.list_jobs(conn, limit=0) == []

            store.upsert_glossary(conn, en="FLCS", zh="飞控系统", note="flight control system")
            store.upsert_glossary(conn, en="OBOGS", zh="机载制氧系统", case_sensitive=1)
            store.upsert_glossary(conn, en="FLCS", zh="飞行控制系统")
            rows = store.list_glossary(conn)
            assert [r["en"] for r in rows] == ["FLCS", "OBOGS"]
            assert rows[0]["zh"] == "飞行控制系统"
            assert store.replace_glossary(conn, [{"en": "MFD", "zh": "多功能显示器",
                                                   "case_sensitive": False, "note": ""}]) == 1
            assert [r["en"] for r in store.list_glossary(conn)] == ["MFD"]
            store.delete_glossary(conn, "MFD")
            assert store.list_glossary(conn) == []
            assert store.replace_glossary(conn, []) == 0
        finally:
            conn.close()


def test_store_empty_and_missing_inputs() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            assert store.get_document(conn, 12345) is None
            assert store.list_documents(conn) == []
            assert store.get_version(conn, 12345) is None
            assert store.list_versions(conn, 12345) == []
            assert store.current_version(conn, 12345) is None
            assert store.segments_of_version(conn, 12345) == []
            assert store.count_segments(conn, 12345) == 0
            assert store.segments_by_ids(conn, []) == {}
            assert store.translations_of_version(conn, 12345) == {}
            assert store.get_translation(conn, 12345) is None
            stats = store.translation_stats(conn, 12345)
            assert stats["total"] == 0 and stats["translated"] == 0
            assert store.links_for_version(conn, 12345) == {}
            assert store.links_for_diff(conn, 0, 0) == []
            assert store.find_render_snapshot(conn, 12345, "cn") is None
            assert store.list_jobs(conn) == []
            assert store.get_job(conn, 0) is None
            assert store.list_glossary(conn) == []
            # 空输入不写库、不抛异常
            assert store.insert_segments(conn, []) == []
            assert store.update_version_stats(conn, 999) is None
            assert store.mark_renders_stale(conn, 999) is None
            assert store.upsert_document is not None
        finally:
            conn.close()


def test_store_invalid_input_raises_value_error() -> None:
    with TempWorkspace() as tmp:
        conn = _conn(tmp)
        try:
            doc_id, vid = _seed(conn, segs=1)
            sid = store.segments_of_version(conn, vid)[0]["id"]
            for call in (
                lambda: store.upsert_document(conn, slug="  ", title="x", source_dir="y"),
                lambda: store.register_version(conn, doc_id=99999, source_path="p", sha256="s",
                                               pdf_bytes=1, page_count=1),
                lambda: store.set_current_version(conn, doc_id, 99999),
                lambda: store.insert_segments(conn, [Segment(version_id=99999, text="x")]),
                lambda: store.insert_segments(conn, [Segment(version_id=0, text="x")]),
                lambda: store.insert_segments(conn, [Segment(version_id=vid, kind="bogus",
                                                             text="x")]),
                lambda: store.insert_segments(conn, [Segment(version_id=vid, role="bogus",
                                                             text="x")]),
                lambda: store.upsert_translation(conn, segment_id=sid, text="x", status="nope"),
                lambda: store.upsert_translation(conn, segment_id=99999, text="x", status="machine"),
                lambda: store.set_translation_status(conn, sid, "nope"),
                lambda: store.record_segment_link(conn, new_segment_id=sid, old_segment_id=sid,
                                                  change_kind="nope", ratio=1.0),
                lambda: store.record_segment_link(conn, new_segment_id=sid, old_segment_id=sid,
                                                  change_kind="modified", ratio=1.5),
                lambda: store.record_segment_link(conn, new_segment_id=None, old_segment_id=None,
                                                  change_kind="modified", ratio=0.5),
                lambda: store.upsert_render_snapshot(conn, version_id=0, kind="cn", path="p",
                                                     params_hash="h"),
                lambda: store.update_job(conn, 1, status="bogus"),
                lambda: store.upsert_glossary(conn, en=" ", zh="x"),
            ):
                try:
                    call()
                except ValueError:
                    continue
                raise AssertionError(f"应当抛 ValueError: {call}")
        finally:
            conn.close()


def test_two_connections_wal() -> None:
    """翻译引擎每线程独立连接；确认第二连接能看到已提交数据。"""
    with TempWorkspace() as tmp:
        path = tmp / "app.db"
        conn_a = coredb.connect(path)
        conn_b = coredb.connect(path)
        try:
            doc_id, vid = _seed(conn_a, slug="wal", segs=2)
            assert store.count_segments(conn_b, vid) == 2
            assert store.upsert_document(conn_b, slug="other", title="O", source_dir="o") > 0
            assert len(store.list_documents(conn_a)) == 2
        finally:
            conn_a.close()
            conn_b.close()


# --------------------------------------------------------------------------- #
# pdfdoc
# --------------------------------------------------------------------------- #
def test_font_metrics_are_real() -> None:
    cjk_path = pdfdoc.resolve_cjk_font()
    latin_path = pdfdoc.resolve_latin_font("regular")
    assert os.path.isfile(cjk_path) and os.path.isfile(latin_path)
    assert pdfdoc._font(cjk_path) is pdfdoc._font(cjk_path), "Font 对象必须被缓存"

    # char_width 必须与 pymupdf 真实度量一致
    font = pymupdf.Font(fontfile=latin_path)
    assert_close(pdfdoc.char_width(latin_path, 12, "AVATAR"), font.text_length("AVATAR", 12), 1e-9)
    assert_close(pdfdoc.char_width(font, 12, "AVATAR"), font.text_length("AVATAR", 12), 1e-9)
    assert_close(pdfdoc.char_width(None, 12, "AVATAR"), font.text_length("AVATAR", 12), 1e-9)

    # 汉字全角（≈ 字号），拉丁字母更窄
    cjk_w = pdfdoc.mixed_text_width("战斗机", 12)
    assert_close(cjk_w, 36.0, 0.5), cjk_w
    assert pdfdoc.mixed_text_width("i", 12) < pdfdoc.mixed_text_width("W", 12)
    assert pdfdoc.mixed_text_width("F-16", 10) > pdfdoc.mixed_text_width("F", 10)
    assert pdfdoc.mixed_text_width("", 12) == 0.0
    assert pdfdoc.mixed_text_width("abc", 0) == 0.0
    # 混合文本宽度 = CJK 部分 + 拉丁部分
    mixed = pdfdoc.mixed_text_width("飞行 F-16", 12)
    assert mixed > pdfdoc.mixed_text_width("F-16", 12)
    assert mixed < pdfdoc.mixed_text_width("飞飞飞飞飞飞飞", 12)
    # 粗体更宽
    assert pdfdoc.mixed_text_width("AVATAR", 12, bold=True) != pdfdoc.mixed_text_width("AVATAR", 12)


def test_layout_mixed_respects_max_width() -> None:
    samples = [
        "The FLCS provides electrical power to the flight control surfaces through the "
        "redundant channels and monitors the hydraulic pressure of each branch.",
        "飞控系统通过冗余通道为飞行操纵面提供电力，并监控每个分支的液压压力。",
        "按 STPT 按钮选择当前转向点，然后再接通自动驾驶仪的导航模式（MFD）。",
        "Supercalifragilisticexpialidocious-extraordinarilylongunbreakabletoken",
        "F-16 block 50 OBOGS MFD STPT 12345",
    ]
    for text in samples:
        for fs, width in ((10.0, 423.0), (12.0, 480.0), (9.0, 120.0), (10.0, 40.0)):
            base = pdfdoc.mixed_text_width(text, fs)
            lines = pdfdoc.layout_mixed(text, fs, width)
            assert lines, (text, width)
            assert all(line.strip() for line, _, _ in lines)
            for line, slot, line_w in lines:
                assert slot in ("cjk", "latin", "latin_bold"), slot
                assert line_w <= width + 1e-6, f"{line_w} > {width}: {line!r}"
                assert_close(line_w, pdfdoc.mixed_text_width(line, fs), 1e-6)
            if base <= width:
                assert len(lines) == 1, (text, base, width, lines)
            # 断行不丢字符（空白折叠后比较）
            assert "".join(line for line, _, _ in lines).replace(" ", "") == \
                text.replace(" ", ""), (text, lines)
    assert pdfdoc.layout_mixed("", 12, 100) == []
    assert pdfdoc.layout_mixed("   ", 12, 100) == []
    assert pdfdoc.layout_mixed("abc", 0, 100) == []
    assert pdfdoc.layout_mixed("abc", -3, 100) == []
    # max_width <= 0 视为不限宽
    assert len(pdfdoc.layout_mixed("hello world", 10, 0)) == 1


def test_layout_mixed_word_break_preference() -> None:
    """拉丁文本优先在空格断，不硬切单词；中文可任意断。"""
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron"
    lines = pdfdoc.layout_mixed(text, 10, 120)
    assert len(lines) > 1
    for line, _, _ in lines[:-1]:
        assert line.endswith(tuple("abcdefghijklmnopqrstuvwxyz")) is True
        # 每个断点都是完整词（行尾没有半截词：下一个词能从这里接上）
    joined = " ".join(line for line, _, _ in lines).split()
    assert joined == text.split(), joined
    cjk_lines = pdfdoc.layout_mixed("飞控系统通过冗余通道为飞行操纵面提供电力", 12, 60)
    assert len(cjk_lines) > 1


def test_paragraph_kind_heuristics() -> None:
    def span(text: str, size: float, x0: float, y0: float, x1: float, *, bold: bool = False,
             font: str = "") -> dict:
        return {
            "text": text, "size": size, "font": font or ("Calibri-Bold" if bold else "Calibri"),
            "flags": 16 if bold else 0, "color": 0,
            "bbox": [x0, y0, x1, y0 + size * 1.2], "origin": [x0, y0 + size],
        }

    body = [span("The FLCS provides electrical power to the flight control surfaces.", 10, 72, 200, 500)]
    heading = [span("1.4.1 MASTER CAUTION light", 12, 89.9, 392.2, 300, bold=True)]
    assert pdfdoc.paragraph_kind(body) == "paragraph"
    assert pdfdoc.paragraph_kind(heading, body) == "heading"
    # 多级编号即使不粗体也是 heading（§3.1.3 信号 5）
    assert pdfdoc.paragraph_kind([span("1.4.1 MASTER CAUTION light", 10, 72, 392, 300)], body) == "heading"
    # 纯数字 → 页码
    assert pdfdoc.paragraph_kind([span("- 21 -", 12, 500, 815, 540)], None,
                                 page_height=842, page_no=21) == "page_num"
    assert pdfdoc.paragraph_kind([span("21", 9, 300, 810, 320)], None) == "page_num"
    # 页眉 / 页脚（需要 page_height）
    header = [span("4.38.1 ", 10, 36, 30, 90)]
    assert pdfdoc.paragraph_kind(header, None, page_height=842, page_no=3) == "header"
    assert pdfdoc.paragraph_kind([span("SYNTHETIC", 8, 36, 806, 100)], None,
                                 page_height=842, page_no=3) == "footer"
    # 编号/项目符号列表
    assert pdfdoc.paragraph_kind([span("1. First checklist step.", 10, 72, 300, 300)], body) == "list_item"
    assert pdfdoc.paragraph_kind([span("\u2022 Item text here", 10, 72, 300, 300)], body) == "list_item"
    # 项目符号后续缩进行（prev_kind 提示）
    assert pdfdoc.paragraph_kind([span("bullet item text", 10, 89.9, 313, 300)], None,
                                 prev_kind="list_item") == "list_item"
    # 表格单元：同一基线上大间隙
    cells = [span("Item", 9, 80, 500, 105), span("Value", 9, 240, 500, 275)]
    assert pdfdoc.paragraph_kind(cells, body) == "table_cell"
    # 标题双 span 间隙 11.5pt 不应误判为表格
    title_spans = [span("1.4.1 ", 10, 71.9, 392.2, 78.4), span("MASTER CAUTION light", 10, 89.9, 392.2, 200)]
    assert pdfdoc.paragraph_kind(title_spans, body) == "heading"
    # 图表标题
    assert pdfdoc.paragraph_kind([span("Figure 1.2 Detail of the panel.", 9, 72, 700, 300)], body) == "caption"
    assert pdfdoc.paragraph_kind([span("表 3 液压系统参数", 9, 72, 700, 300)], body) == "caption"
    # 空白 span 必须被忽略（§3.1.2）
    noisy = [span("   ", 10, 442.2, 217.7, 444.9), *body]
    assert pdfdoc.paragraph_kind(noisy) == "paragraph"
    assert pdfdoc.paragraph_kind([]) == "other"
    assert pdfdoc.paragraph_kind(None) == "other"
    assert pdfdoc.paragraph_kind([span("   ", 10, 0, 0, 10)]) == "other"


def test_spans_helpers() -> None:
    spans = [
        {"text": "Flight ", "font": "Calibri", "size": 10.0, "color": 0, "flags": 0,
         "bbox": [72, 100, 110, 112]},
        {"text": "Controls", "font": "Calibri", "size": 10.0, "color": 0, "flags": 0,
         "bbox": [110, 100, 150, 112]},
        {"text": " ", "font": "ArialMT", "size": 10.0, "color": 0, "flags": 0,
         "bbox": [442.2, 217.7, 444.9, 228.8]},
    ]
    assert pdfdoc.spans_text(spans) == "Flight Controls"
    fonts = pdfdoc.fonts_summary(spans)
    assert fonts[0]["font"] == "Calibri" and fonts[0]["count"] == len("Flight Controls")
    assert all(f["font"] != "ArialMT" for f in fonts), "空白 span 不进统计"
    style = pdfdoc.style_of(spans, page_width=595)
    assert style["bold"] is False and style["size"] == 10.0 and style["color"] == 0
    assert pdfdoc.style_of([])["size"] == 0.0
    bold_style = pdfdoc.style_of([{"text": "X", "font": "Calibri-Bold", "size": 12, "color": 0,
                                   "flags": 16, "bbox": [0, 0, 10, 12]}])
    assert bold_style["bold"] is True


def test_is_banner_image() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    try:
        full_width = {"bbox": (0.4, 0.0, 594.8, 44.0)}
        assert pdfdoc.is_banner_image(full_width, page) is True
        footer_banner = {"bbox": (-0.2, 776.1, 654.8, 841.6)}
        assert pdfdoc.is_banner_image(footer_banner, page) is True
        small = {"bbox": (250.0, 400.0, 310.0, 460.0)}
        assert pdfdoc.is_banner_image(small, page) is False
        thin_rule = {"bbox": (0.0, 400.0, 595.0, 402.0)}
        assert pdfdoc.is_banner_image(thin_rule, page) is False, "细装饰线不算 banner"
        floating = {"bbox": (10.0, 300.0, 585.0, 340.0)}
        assert pdfdoc.is_banner_image(floating, page) is False, "未贴边不算 banner"
        assert pdfdoc.is_banner_image({}, page) is False
        assert pdfdoc.is_banner_image(None, page) is False
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# fixtures / 真实数据
# --------------------------------------------------------------------------- #
def test_synthetic_fixture_is_extractable() -> None:
    from tests import fixtures as fx

    with TempWorkspace() as tmp:
        path = fx.make_synthetic_pdf(str(tmp / "syn.pdf"), pages=3)
        assert os.path.isfile(path)
        doc = pdfdoc.open_pdf(path)
        try:
            assert doc.page_count == 3
            assert pdfdoc.page_size(doc[0]) == (fx.PAGE_W, fx.PAGE_H)
            text_p1 = doc[0].get_text()
            for needle in (fx.TITLE, fx.SUBTITLE, fx.HEADER_TEXT, fx.BULLET_MARKER):
                assert needle in text_p1 or needle in doc[1].get_text(), needle
            text_p2 = doc[1].get_text()
            assert fx.HEADINGS[0] in text_p2
            assert fx.LIST_ITEMS[0] in text_p2
            assert fx.HYPHEN_FIRST_LINE.strip() in text_p2.replace("\n", " ").replace("  ", " ")
            assert fx.BULLET_ITEM in text_p2
            assert f"- 2 -" in text_p2
            # 页眉 banner 可识别，内联图保留
            infos = doc[1].get_image_info(xrefs=True)
            assert infos, "合成 PDF 应含图片"
            banners = [i for i in infos if pdfdoc.is_banner_image(i, doc[1])]
            assert banners, "页眉 banner 应被识别"
            assert len(infos) > len(banners), "应存在正文内联图（非 banner）"
            # 抽取出的 span 能喂给 paragraph_kind
            page = doc[1]
            blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
            heading_block = next(b for b in blocks
                                 if fx.HEADINGS[0] in "".join(s["text"] for ln in b["lines"]
                                                              for s in ln["spans"]))
            spans = heading_block["lines"][0]["spans"]
            assert pdfdoc.paragraph_kind(spans, None, page_height=fx.PAGE_H,
                                         page_no=2) in ("heading", "list_item")
            # 软连字符段落确实抽出成两行
            assert "Cau-" in text_p2
        finally:
            doc.close()

        v1, v2 = fx.make_synthetic_pair(str(tmp / "pair"), pages=2)
        assert os.path.isfile(v1) and os.path.isfile(v2)
        d1 = pdfdoc.open_pdf(v1)
        d2 = pdfdoc.open_pdf(v2)
        try:
            assert d1[1].get_text() != d2[1].get_text(), "v2 必须有可见差异"
        finally:
            d1.close()
            d2.close()


def test_open_pdf_errors() -> None:
    with TempWorkspace() as tmp:
        try:
            pdfdoc.open_pdf(str(tmp / "missing.pdf"))
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("缺失文件应抛 FileNotFoundError")
        try:
            pdfdoc.open_pdf("")
        except ValueError:
            pass
        else:
            raise AssertionError("空路径应抛 ValueError")
        bad = tmp / "bad.pdf"
        bad.write_bytes(b"not a pdf at all")
        try:
            pdfdoc.open_pdf(str(bad))
        except ValueError:
            pass
        else:
            raise AssertionError("坏文件应抛 ValueError")


def test_real_pdf_page21_layout() -> None:
    """真实数据健壮性：用 origin PDF 第 21 页的实际段落断行，宽度不得超限。"""
    if os.environ.get("MT_SKIP_REAL"):
        print("    [skip] MT_SKIP_REAL=1")
        return
    if not REAL_PDF.is_file():
        print(f"    [skip] 缺少真实 PDF：{REAL_PDF}")
        return
    doc = pdfdoc.open_pdf(str(REAL_PDF))
    try:
        page = doc[REAL_PAGE - 1]
        width, height = pdfdoc.page_size(page)
        assert_close(width, 595.32, 1.0)
        assert_close(height, 841.92, 1.0)
        checked = 0
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            spans = [s for ln in block["lines"] for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = pdfdoc.spans_text(spans)
            fs = max(float(s["size"]) for s in spans)
            box_w = max(float(s["bbox"][2]) for s in spans) - min(float(s["bbox"][0]) for s in spans)
            if len(text) < 20 or box_w < 60:
                continue
            for content, max_width in ((text, box_w), (text, 423.0)):
                lines = pdfdoc.layout_mixed(content, fs, max_width)
                assert lines
                for line, slot, line_w in lines:
                    assert line_w <= max_width + 1e-6, (text[:40], line_w, max_width)
                    assert slot in ("cjk", "latin", "latin_bold")
                    assert line.strip()
                checked += 1
            # 中文译文场景：按同宽度用中文字体断行
            zh = ("飞控系统通过冗余通道为飞行操纵面提供电力，并监控每个分支的液压压力。"
                  "按下 MFD 上的 STPT 按钮可选择当前转向点。")
            zh_lines = pdfdoc.layout_mixed(zh, fs, 423.0)
            assert len(zh_lines) > 1
            for line, slot, line_w in zh_lines:
                assert line_w <= 423.0 + 1e-6 and slot == "cjk"
            checked += 1
        assert checked >= 2, f"第 21 页可用段落太少：{checked}"
        print(f"    [real] page {REAL_PAGE} 断行校验段落数：{checked}")
    finally:
        doc.close()


def test_integration_extract_store_diff_flow() -> None:
    """端到端串起数据层：合成 PDF → 抽取 → 落库 → 跨版本指纹匹配 → diff_summary。

    模拟 pipeline/versions 的调用方式（真正的分段器是 dev-relay 的职责，这里只做最小实现）。
    """
    from tests import fixtures as fx

    def extract(path: str, version_id: int, doc_id: int) -> list[Segment]:
        doc = pdfdoc.open_pdf(path)
        segs: list[Segment] = []
        order = 0
        try:
            for pno in range(doc.page_count):
                page = doc[pno]
                height = pdfdoc.page_size(page)[1]
                blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
                for block in blocks:
                    spans = [s for ln in block["lines"] for s in ln["spans"] if s["text"].strip()]
                    if not spans:
                        continue
                    lines = ["".join(s["text"] for s in ln["spans"] if s["text"].strip())
                             for ln in block["lines"]]
                    lines = [ln for ln in lines if ln.strip()]
                    text = pdfdoc.spans_text(spans)
                    boxes = [[float(v) for v in s["bbox"]] for s in spans]
                    bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes)]
                    if bbox[3] <= 66:
                        role, kind = "header", "header"
                    elif bbox[1] >= height * 0.92:
                        role = "pagenum" if text.strip().startswith("-") else "footer"
                        kind = "page_num" if role == "pagenum" else "footer"
                    else:
                        role = "body"
                        kind = pdfdoc.paragraph_kind(spans, None, page_height=height,
                                                     page_no=pno + 1)
                    segs.append(Segment(
                        version_id=version_id, doc_id=doc_id, page=pno + 1, order_index=order,
                        kind=kind, text=text,
                        canonical_text=fp.normalize(fp.dehyphenate(lines)),
                        bbox=bbox, line_boxes=[boxes[0], boxes[-1]],
                        fonts=pdfdoc.fonts_summary(spans),
                        style=pdfdoc.style_of(spans, page_width=pdfdoc.page_size(page)[0]),
                        role=role,
                    ))
                    order += 1
        finally:
            doc.close()
        return segs

    with TempWorkspace() as tmp:
        v1_pdf, v2_pdf = fx.make_synthetic_pair(str(tmp / "pdfs"), pages=3)
        conn = _conn(tmp)
        try:
            doc_id = store.upsert_document(conn, slug="synthetic", title=fx.TITLE,
                                           source_dir="tests")
            v1 = store.register_version(conn, doc_id=doc_id, source_path=v1_pdf, sha256="s1",
                                        pdf_bytes=os.path.getsize(v1_pdf), page_count=3,
                                        label="v1", make_current=False)
            v2 = store.register_version(conn, doc_id=doc_id, source_path=v2_pdf, sha256="s2",
                                        pdf_bytes=os.path.getsize(v2_pdf), page_count=3, label="v2")
            ids1 = store.insert_segments(conn, extract(v1_pdf, v1, doc_id))
            ids2 = store.insert_segments(conn, extract(v2_pdf, v2, doc_id))
            assert len(ids1) > 20 and len(ids2) > 20, (len(ids1), len(ids2))

            store.update_version_stats(conn, v1, segments=len(ids1),
                                       chars=sum(len(s["text"]) for s in
                                                 store.segments_of_version(conn, v1)),
                                       pages=3, images=9)
            assert store.get_version(conn, v1)["stats"]["segments"] == len(ids1)

            rows1 = store.segments_of_version(conn, v1)
            roles = {r["role"] for r in rows1}
            assert {"header", "footer", "body"} <= roles, roles
            assert any(r["kind"] == "header" for r in rows1)
            assert any("- 2 -" in r["text"] for r in rows1), "页码文本应被抽取到"
            # 软连字符段落：canonical_text 已复原 "Caution"，且 fingerprint 不含 "- "
            hyphen_rows = [r for r in rows1 if "otion" in r["canonical_text"] or "Caution" in r["canonical_text"]]
            assert hyphen_rows, "应抽到含 Caution 的段落"
            assert all("- " not in r["canonical_text"] for r in hyphen_rows)

            # 跨版本指纹匹配：未改动的段落应能对上（diff 引擎的基础）
            v1_fps = {r["fingerprint"] for r in store.segments_of_version(conn, v1)
                      if r["role"] == "body"}
            v2_rows = [r for r in store.segments_of_version(conn, v2) if r["role"] == "body"]
            matched = [r for r in v2_rows if r["fingerprint"] in v1_fps]
            assert len(matched) >= 6, f"跨版本匹配到的段落太少：{len(matched)}"

            # 记录一条 modified 链接 + 一条 added，diff_summary 应统计出来
            modified_new = next(r for r in v2_rows if "brightly" in r["text"])
            old_text = " ".join(modified_new["text"].split(" brightly ")[0:1]) + "."
            store.record_segment_link(
                conn, new_segment_id=modified_new["id"], old_segment_id=ids1[0],
                change_kind="modified", ratio=fp.ratio(old_text, modified_new["canonical_text"]),
                char_diffs=fp.char_diffs(old_text, modified_new["canonical_text"]))
            summary = store.diff_summary(conn, doc_id, v1, v2)
            assert summary["counts"]["modified"] == 1
            assert summary["counts"]["unchanged"] >= len(matched) - 1
            assert summary["total"] == len(ids2)

            # web 层读法：段 + 译文（左原文右译文），markers 需要的 bbox/line_boxes
            store.upsert_translation(conn, segment_id=modified_new["id"], text="主警戒灯亮起。",
                                     status="machine", provider="mock", model="mock")
            trans = store.translations_of_version(conn, v2)
            assert trans[modified_new["id"]]["text"] == "主警戒灯亮起。"
            stats = store.translation_stats(conn, v2)
            assert stats["total"] == len(ids2) and stats["by_status"]["machine"] == 1
            marker = store.get_segment(conn, modified_new["id"])
            assert marker["bbox"] and marker["line_boxes"] and marker["seg_id"] == marker["id"]

            # 翻译完成后快照失效
            sid = store.upsert_render_snapshot(conn, version_id=v2, kind="page:1",
                                              path=str(tmp / "p1.webp"), params_hash="dpi110")
            store.mark_renders_stale(conn, v2, kinds=["page:1"])
            assert store.get_render_snapshot(conn, v2, "page:1", "dpi110")["stale"] == 1
            assert sid > 0
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 直接运行入口
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = sorted(
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failures: list[str] = []
    start = time.perf_counter()
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:                                     # noqa: BLE001
            failures.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    elapsed = time.perf_counter() - start
    total = len(tests)
    print(f"\n{total - len(failures)}/{total} 通过，耗时 {elapsed:.2f}s")
    if failures:
        print("失败用例：" + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
