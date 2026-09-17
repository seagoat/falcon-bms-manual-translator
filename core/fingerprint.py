"""文本归一化、指纹、相似度、段内换行拼接（CONTRACT.md §4 / §4.1）。

分工约定（很重要）：
* `normalize()` **不做**连字符处理 —— 连字符是"段内多行拼接"阶段的事（`dehyphenate`）。
* `fingerprint()` 输入必须是 `Segment.canonical_text`（已 dehyphenate + normalize 的文本）。

## §4.1 行尾连字符判定（2026-09 修订，修复 Verifier M-1/M-2）
旧规则「下一行首字母小写 → 删连字符」会把真实复合词拼坏（air-to-air → airto-air，
44 处删除里 17 处是错的），并在大写/数字续行时插入多余空格（`AGM- 65G`，11 类）。
新规则见 `dehyphenate()` / `_decide_join`，三种动作 soft/hard/space：
`Cau-`+`tion`→`Caution`；`air-`+`to-air`→`air-to-air`；`AGM-`+`65G`→`AGM-65G`；`word -`+`x`→`word - x`。

**401 页真实数据实测（120 对行尾连字符）**

| 模式 | soft(删连字符) | hard(保留且不空格) | space | 已知错误 |
|---|---|---|---|---|
| 旧规则 | 44 | 0 | 76 | 17 处拼坏真实词 + 11 类多余空格 |
| 新规则 standalone | 9 | 53 | 58 | 1 处误删(`two-ship`→`twoship`) + 约 13 处软断词未拼接 |
| 新规则 + `build_vocabulary(全书行)` | 23 | 38 | 58 | 1 处误保留(`In-`+`terchange`) |

→ **ingest 必须传语料词表**：`vocab = fingerprint.build_vocabulary(all_lines)`，
`canonical = fingerprint.dehyphenate(paragraph_lines, vocabulary=vocab)`。
不传时是保守模式（默认保留连字符），会留下 `infor-mation` 一类的可见残留。

## ⚠️ 影响面（改规则后必须知道）
* `Segment.text` / `canonical_text` / `fingerprint` 的内容会变化：受影响的是全书 44 处
  行尾连字符中的一部分 + 11 类大写/数字续行（实测约 119/120 对的结果都变了，
  因为 `hard` 不再插入空格）。
* `translation_cache` 是按 `fingerprint` 存的：重新 ingest 后**受影响段落的缓存条目会失配**，
  这是预期行为。本模块**不删除、不清空** `translation_cache`：未失配的条目继续命中，
  失配的段落按正常流程重新翻译（内容由 `translate/engine.py` 负责）。
* v1/v2 指纹差集在噪音消除后应变小；若仍有残差，应等于真实变更数（见 `spikes/analyze_drift.py`）。
* `pipeline.py` 里另有一份自带的 `_FallbackFingerprint.dehyphenate`（仅在导入本模块失败时使用），
  它仍是旧规则 —— 排查异常结果时请确认走的是本模块。
"""
from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections import Counter
from typing import Any, Iterable, Optional

__all__ = [
    "KNOWN_HYPHEN_PREFIX",
    "SOFT_TAIL_FRAGMENTS",
    "normalize",
    "fingerprint",
    "content_hash",
    "ratio",
    "char_diffs",
    "tokenize_cjk",
    "char_kind",
    "dehyphenate",
    "explain_hyphen_joins",
    "build_vocabulary",
]

# §4.1：这些前缀后的行尾 "-" 是真实复合词连字符（F-16 / Dash-34 / ANTI- / ELEC- …）。
# ⚠️ 2026-09 修订后**判定逻辑不再依赖它**（改用「大写/数字直接拼接 + 语料词表 + 后缀片段」），
#    本常量保留导出仅供调用方展示/审计，避免删除公开名字破坏兼容。
KNOWN_HYPHEN_PREFIX: set[str] = {
    "F", "An", "Dash", "ANTI", "A", "B", "S", "T", "G", "N", "MAL", "ELEC",
    "NORM", "M", "Q", "TAC", "RT", "IDM", "FLCS", "MFD", "PFL", "VMS",
}

# 英文软换行（Word 自动断词）下半截的常见片段：真实后缀 + 常见非单词碎片。
# 选它作判据的理由：真实复合词的后半截几乎不会恰好是这些片段
# —— air-to-air 的 "to-air"、mode-dependent 的 "dependent"、pop-up 的 "up" 都不在表内。
SOFT_TAIL_FRAGMENTS: frozenset[str] = frozenset({
    # 名词/动词/形容词后缀
    "tion", "sion", "ction", "ption", "ation", "ition", "ution", "ssion", "xion",
    "ing", "ings", "ingly", "ed", "edly", "es", "er", "ers", "or", "ors", "ar",
    "al", "ally", "ially", "ually", "ical", "ically", "ic", "ics", "ly",
    "ity", "ities", "ties", "ty", "cy", "gy", "py", "my", "ny", "ry", "sy", "dy", "ky",
    "ous", "ious", "eous", "uous", "ive", "ative", "itive", "sive", "tive",
    "able", "ible", "ably", "ibly", "ance", "ence", "ancy", "ency",
    "ant", "ent", "antly", "ently", "ment", "ments", "ness", "ism", "ist", "ists",
    "ize", "ise", "ized", "ised", "ify", "ful", "fully", "less", "ish",
    "ward", "wards", "wise", "ship", "hood", "dom", "age", "ages", "ure", "ures",
    "ial", "ual", "eal", "ian", "ean", "ology", "ological", "ograph", "ography",
    "ometer", "ular", "ularly", "tial", "cial", "nance", "nent",
    # 常见非单词碎片（断词后剩下的半截）
    "tor", "tors", "ters", "lar", "cal", "ture", "tric", "pic", "nal", "tem",
    "phy", "sis", "lic", "nin", "ning", "nism", "graph",
    # 复数/派生形式（p268 `func-`+`tions`、`person-`+`alize` 等真实断词）
    "tions", "sions", "ctions", "ptions", "ations", "itions", "utions", "ssions", "xions",
    "ings", "ments", "nesses", "ities", "ances", "ences", "ancies", "encies",
    "ants", "ents", "ives", "atives", "itives", "sives", "tives", "ous", "ouses",
    "ables", "ibles", "icals", "ers", "ors", "ars", "eds", "ures", "ages", "isms", "ists",
    "alize", "alise", "alized", "alised", "alizing", "alising", "ization", "isation",
    "izable", "isable", "izer", "iser", "izers", "isers",
})

# 单独成行的项目符号（§3.1.1 的 `'- '`），不能被当成软连字符吃掉
_BULLET_CHARS = "-•▪◆※·‣⁃◦*"
_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")

# 引号 / 破折号 / 省略号统一表（NFKC 处理不了弯引号，必须自己映射）
_CHAR_MAP: dict[int, str] = {
    # 引号
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"', 0x2033: '"',
    0x00AB: '"', 0x00BB: '"', 0x2039: "'", 0x203A: "'",
    0x0060: "'", 0x00B4: "'", 0x02BC: "'", 0x02B9: "'",
    # 破折号 / 减号
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
    0x2015: "-", 0x2212: "-",
    # 省略号
    0x2026: "...",
}

# --- tokenize_cjk 用的字符区间 -------------------------------------------------
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),    # CJK 部首
    (0x3000, 0x303F),    # CJK 标点（，。、）——全角，随中文字体
    (0x3040, 0x30FF),    # 假名
    (0x3100, 0x312F),    # 注音
    (0x31C0, 0x31EF),
    (0x3200, 0x32FF),
    (0x3300, 0x33FF),
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本汉字
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),    # 谚文
    (0xF900, 0xFAFF),    # 兼容汉字
    (0xFE10, 0xFE1F),
    (0xFE30, 0xFE4F),    # CJK 兼容标点
    (0xFF00, 0xFFEF),    # 全角形式
    (0x1F200, 0x1F2FF),
    (0x20000, 0x2FA1F),  # 扩展 B~F
)


def _as_text(value: Any) -> str:
    """None/非字符串安全转文本：DAO/工具函数对空输入不抛异常。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def normalize(text: str) -> str:
    """NFKC；统一引号/破折号/省略号；折叠所有空白为单空格；去首尾；保留大小写。

    注意：**不做**连字符处理（见 §4.1），幂等：`normalize(normalize(x)) == normalize(x)`。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", _as_text(text))
    s = s.translate(_CHAR_MAP)
    s = _ZERO_WIDTH_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def fingerprint(text: str) -> str:
    """blake2b(normalize(text).casefold(), digest_size=16) —— 跨版本内容匹配。

    调用方必须传 `Segment.canonical_text`（§3.1.1），不要传 `text`。
    """
    return _digest(normalize(text).casefold())


def content_hash(text: str) -> str:
    """blake2b(normalize(text), digest_size=16) —— 保留大小写，用于精确变更判定。"""
    return _digest(normalize(text))


def ratio(a: str, b: str) -> float:
    """difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()。"""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _push(ops: list[dict], op: str, text: str) -> None:
    """同类相邻 opcode 合并（§4），空文本直接丢弃。"""
    if not text:
        return
    if ops and ops[-1]["op"] == op:
        ops[-1]["text"] += text
    else:
        ops.append({"op": op, "text": text})


def char_diffs(old: str, new: str) -> list[dict]:
    """返回 §2 char_diffs 格式：[{"op": equal|delete|insert, "text": str}]。

    `replace` 拆成 delete + insert，保证前端可以简单地顺序渲染
    （删除线红字 + 绿字插入）；相邻同类 op 已合并。
    """
    a, b = normalize(old), normalize(new)
    if a == b:
        return [{"op": "equal", "text": a}] if a else []
    sm = difflib.SequenceMatcher(None, a, b)
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            _push(ops, "equal", a[i1:i2])
        elif tag == "delete":
            _push(ops, "delete", a[i1:i2])
        elif tag == "insert":
            _push(ops, "insert", b[j1:j2])
        else:  # replace -> delete then insert
            _push(ops, "delete", a[i1:i2])
            _push(ops, "insert", b[j1:j2])
    return ops


def _in_ranges(cp: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    for lo, hi in ranges:
        if lo <= cp <= hi:
            return True
    return False


def char_kind(ch: str) -> str:
    """单字符分类，kind ∈ {"cjk","latin","digit","space","punct"}。

    全角数字/字母按语义归 digit/latin；CJK 标点（，。、）归 "cjk"（全角，随中文字体）。
    """
    if not ch:
        return "punct"
    if ch.isspace():
        return "space"
    cp = ord(ch)
    if 0x30 <= cp <= 0x39:            # 0-9
        return "digit"
    if 0xFF10 <= cp <= 0xFF19:        # 全角 ０-９
        return "digit"
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "latin"
    if 0xFF21 <= cp <= 0xFF3A or 0xFF41 <= cp <= 0xFF5A:  # 全角字母
        return "latin"
    if _in_ranges(cp, _CJK_RANGES):
        return "cjk"
    cat = unicodedata.category(ch)
    if cat.startswith("L"):
        return "latin"     # 希腊/西里尔等：拉丁字体槽位
    if cat.startswith("N"):
        return "digit"
    return "punct"


def tokenize_cjk(text: str) -> list[tuple[str, str]]:
    """切成 [(kind, run)]，kind ∈ {"cjk","latin","digit","space","punct"}；用于混合字体排版。"""
    out: list[tuple[str, str]] = []
    if not text:
        return out
    run_kind = ""
    run_chars: list[str] = []
    for ch in _as_text(text):
        k = char_kind(ch)
        if k == run_kind:
            run_chars.append(ch)
        else:
            if run_chars:
                out.append((run_kind, "".join(run_chars)))
            run_kind, run_chars = k, [ch]
    if run_chars:
        out.append((run_kind, "".join(run_chars)))
    return out


def _rstrip_ws(text: str) -> str:
    return text.rstrip(" \t\r\n")


def _lstrip_ws(text: str) -> str:
    return text.lstrip(" \t\r\n")


def _is_bullet_line(stripped_line: str) -> bool:
    """整行就是一个项目符号（§3.1.1 的 `'- '`）——不能当软连字符。"""
    s = stripped_line.strip()
    return bool(s) and len(s) <= 3 and all(c in _BULLET_CHARS for c in s)


_WORD_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z'\-]*")
# 独占一行的编号列表标记（"1-" / "10-"），要与 "50-" + "50" 这类区间写法区分
_LIST_MARKER_RE = re.compile(r"\d{1,2}")


def _first_word(text: str) -> str:
    """取文本里第一个"词"（字母/数字/内部连字符/撇号），casefold；CJK 等非拉丁返回 ""。"""
    m = _WORD_RE.search(text or "")
    return m.group(0).casefold() if m else ""


def _vocab_has(vocabulary: Any, token: str) -> bool:
    """语料词表成员判定（大小写不敏感；vocabulary 可以是 set / frozenset / Counter / dict 的键）。"""
    if not vocabulary or not token:
        return False
    for cand in (token, token.casefold(), token.lower(), token.upper(), token.capitalize()):
        try:
            if cand in vocabulary:
                return True
        except TypeError:      # 不是容器：当作没有语料
            return False
    return False


def _vocab_count(vocabulary: Any, token: str) -> int:
    """语料词频（set 无计数 → 0；Counter/dict 取最大值）。"""
    if not vocabulary or not token:
        return 0
    best = 0
    for cand in (token, token.casefold(), token.lower(), token.upper(), token.capitalize()):
        try:
            value = vocabulary.get(cand)          # type: ignore[union-attr]
        except AttributeError:
            return 0
        except TypeError:
            return 0
        if isinstance(value, (int, float)) and value > best:
            best = int(value)
    return best


def _decide_join(prev_chunk: str, next_line: str, vocabulary: Any = None) -> dict:
    """判定「上一行以 "-" 结尾」时该怎么和下一行拼接。

    返回 {"action", "reason", "soft_form", "hard_form"}，action ∈：
    * `"soft"`  —— 软换行断词：删除 "-" 并直接拼接（`Cau-` + `tion` → `Caution`）
    * `"hard"`  —— 词内连字符：保留 "-" 并**不加空格**（`AGM-` + `65G` → `AGM-65G`、
                   `air-` + `to-air` → `air-to-air`、`R-` + `aero` → `R-aero`）
    * `"space"` —— 破折号/项目符号/无连字符：保留原样并以单空格拼接

    判定顺序（从确定性信号到需要语料的信号，全部可解释）：
    1. 行尾无 "-" / 整行是项目符号 / 下一行空白 → space
    2. "-" 前是空白（`word -`）→ 破折号 → space
    3. 整行就是一个 1~2 位数字且下一行以字母开头（编号列表 `1- `）→ space
    4. 下一行首字符是大写字母或数字 → hard（AGM-65G / Multi-Function / 500-3000ft）
    5. 候选词里没有字母（纯数字续行 `#5-` + `#8`）→ hard
    6. 行尾词只有一个字符（`R-`）→ hard
    7. 下一行没有拉丁词（中文/符号开头）→ space
    8. 语料规则：删连字符后的整词在语料中出现过、而连字符写法没有（或前者词频更高）→ soft
    9. 后缀规则：下一行首个 token 是典型软换行尾部片段（tion/ing/…，见 SOFT_TAIL_FRAGMENTS）
       且连字符写法没在语料中出现 → soft
    10. 其余一律 hard（保守：绝不把真实复合词拼坏）
    """
    prev = _rstrip_ws(prev_chunk)
    nxt = _lstrip_ws(next_line)
    info: dict[str, Any] = {"action": "space", "reason": "no-trailing-hyphen",
                            "soft_form": "", "hard_form": ""}
    if not prev.endswith("-"):
        return info
    if _is_bullet_line(prev):
        return {**info, "reason": "bullet-line"}
    if not nxt:
        return {**info, "reason": "empty-next-line"}
    body = prev[:-1]
    if not body or body[-1].isspace():
        return {**info, "reason": "dash-with-space"}      # "word -" 是破折号，不是词内连字符
    head = re.split(r"\s+", body)[-1]
    head_word = _first_word(head)
    tail = _first_word(nxt)
    soft_form = (head_word + tail) if tail else head_word
    hard_form = (f"{head_word}-{tail}") if tail else head_word
    info.update(soft_form=soft_form, hard_form=hard_form)

    # 编号列表标记独占一行（"1- " / "10- " + 下一行正文，p320 实测）→ 破折号 + 空格。
    # 注意只在整行就是一个 1~2 位数字时成立：p391 的 "50-" + "50" 是区间写法，不能误伤。
    if _LIST_MARKER_RE.fullmatch(body.strip()) and tail[:1].isalpha():
        return {**info, "action": "space", "reason": "list-marker"}

    first = nxt[0]
    if first.isupper() or first.isdigit():
        return {**info, "action": "hard", "reason": "upper-or-digit"}
    if not re.search(r"[A-Za-z]", soft_form):
        # 纯数字续行（"Team #5-" + "#8"）不是软换行；否则语料里 "58" 这种数字会骗过词表规则
        return {**info, "action": "hard", "reason": "numeric-tail"}
    if len(head) <= 1:
        return {**info, "action": "hard", "reason": "single-letter-head"}
    if not tail:
        return {**info, "reason": "no-word-tail"}          # 中文/符号开头：保守用空格

    soft_attested = _vocab_has(vocabulary, soft_form)
    hard_attested = _vocab_has(vocabulary, hard_form)
    if soft_attested and not hard_attested:
        return {**info, "action": "soft", "reason": "corpus-attested"}
    if soft_attested and hard_attested:
        if _vocab_count(vocabulary, soft_form) > _vocab_count(vocabulary, hard_form):
            return {**info, "action": "soft", "reason": "corpus-more-frequent"}
        return {**info, "action": "hard", "reason": "corpus-ambiguous"}
    if tail in SOFT_TAIL_FRAGMENTS and not hard_attested:
        return {**info, "action": "soft", "reason": "suffix-fragment"}
    return {**info, "action": "hard", "reason": "conservative-compound"}


def dehyphenate(lines: list[str], vocabulary: Any = None) -> str:
    """把同一段落的多行文本拼成一段，处理行尾连字符（§4.1 修订版）。

    与旧规则的区别（Verifier M-1/M-2）：
    * 不再用"下一行首字母小写就删"这种弱信号 —— 真实复合词（air-to-air / edge-of-display /
      mode-dependent / R-aero）不再被拼坏；
    * 下一行以大写字母或数字开头时按 `xxx-yyy` 直接拼接，不再插入多余空格
      （AGM-65G / Multi-Function / 500-3000ft / BMS1-F16CM-34-1-1）。

    `vocabulary`（可选）：语料词表，用于把「删连字符后的整词确实是文档里出现过的词」的
    软换行也判为断词（例如尾部片段不常见时）。可传 `set`/`frozenset`，
    也可传 `collections.Counter`/`dict`（带词频，冲突时词频高者胜）。
    典型用法：ingest 时先扫全书所有"未断行 token"建表，再传给本函数。
    `vocabulary=None` 时退化为保守策略：默认保留连字符，只删明显是断词的（后缀片段规则）。
    判定过程可用 `explain_hyphen_joins()` 查看。
    """
    if not lines:
        return ""
    chunks: list[str] = []
    for raw in lines:
        line = _as_text(raw)
        if not chunks:
            chunks.append(line)
            continue
        action = _decide_join(chunks[-1], line, vocabulary)["action"]
        if action == "space":
            chunks.append(line)
            continue
        prev = _rstrip_ws(chunks[-1])
        nxt = _lstrip_ws(line)
        if action == "soft":
            chunks[-1] = prev[:-1] + nxt          # 软连字符：删除 "-" 并直接拼接
        else:                                     # hard：保留 "-"，不加空格
            chunks[-1] = prev + nxt
    return _WS_RE.sub(" ", " ".join(chunks)).strip()


def build_vocabulary(lines: Iterable[str], *, min_count: int = 1) -> "Counter[str]":
    """从"未断行的原始行"统计 token 词频，供 `dehyphenate(vocabulary=...)` 使用。

    ingest 时先扫全书建表，再逐段拼接：

        vocab = fingerprint.build_vocabulary(all_lines_of_book)
        text = fingerprint.dehyphenate(paragraph_lines, vocabulary=vocab)

    行尾被切断的半截词（`Cau-`）会以 `cau` 的形式进入表，不影响判定
    —— 判定查的是"拼接后的整词"，只在别处完整出现过才算 attested。
    """
    counter: "Counter[str]" = Counter()
    for line in lines or []:
        for token in _WORD_RE.findall(_as_text(line)):
            token = token.strip("-'").casefold()
            if token:
                counter[token] += 1
    if min_count > 1:
        return Counter({token: count for token, count in counter.items() if count >= min_count})
    return counter


def explain_hyphen_joins(lines: list[str], vocabulary: Any = None) -> list[dict]:
    """逐对相邻行给出拼接判定（action/reason/两种候选写法），供审计与回归测试。

    与 `dehyphenate` 共用同一个判定函数，因此解释与结果永远一致。
    """
    out: list[dict] = []
    for index, (a, b) in enumerate(zip(lines or [], list(lines or [])[1:])):
        decision = _decide_join(_as_text(a), _as_text(b), vocabulary)
        out.append({
            "index": index,
            "prev": _as_text(a)[-40:],
            "next": _as_text(b)[:40],
            "action": decision["action"],
            "reason": decision["reason"],
            "soft_form": decision["soft_form"],
            "hard_form": decision["hard_form"],
        })
    return out
