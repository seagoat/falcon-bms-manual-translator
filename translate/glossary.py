"""术语表：加载 / 强制替换 / 提示词片段 / 离线占位翻译。

数据文件：`data/glossary.json`（不存在时用内置默认表初始化写出）。
格式：`[{"en": "...", "zh": "...", "case_sensitive": false, "note": "..."}, ...]`

对外接口（CONTRACT §7）：
    get_pairs()           -> [(en, zh), ...]
    apply(text)           -> 把已译文本里残留的英文术语强制替换成中文
    prompt_block(texts)   -> 拼进提示词的术语清单文本
另外提供 mock 占位后端使用的 `mock_translate()`。

本模块只依赖标准库 + `config`（路径），不依赖 core 层。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# --------------------------------------------------------------------------
# 内置默认术语表（F-16 / BMS 训练手册高频术语，≥60 条）
# 译法遵循国内航空/军事领域常规译法；缩写统一在译文中以「中文（缩写）」形式出现。
# --------------------------------------------------------------------------
DEFAULT_GLOSSARY: list[dict[str, Any]] = [
    # --- 告警 / 座舱 -------------------------------------------------------
    {"en": "MASTER CAUTION", "zh": "主警戒灯", "case_sensitive": False,
     "note": "注意灯面板总警示灯；正文常写作 Master Caution"},
    {"en": "MASTER WARNING", "zh": "主警告灯", "case_sensitive": False, "note": ""},
    {"en": "CAUTION PANEL", "zh": "警戒灯面板", "case_sensitive": False, "note": ""},
    {"en": "caution light", "zh": "警戒灯", "case_sensitive": False, "note": ""},
    {"en": "warning light", "zh": "警告灯", "case_sensitive": False, "note": ""},
    {"en": "canopy", "zh": "座舱盖", "case_sensitive": False, "note": ""},
    {"en": "ejection seat", "zh": "弹射座椅", "case_sensitive": False, "note": ""},
    {"en": "emergency jettison", "zh": "应急抛放", "case_sensitive": False, "note": ""},
    {"en": "checklist", "zh": "检查单", "case_sensitive": False, "note": ""},
    {"en": "BOLD FACE", "zh": "黑体条目", "case_sensitive": False, "note": "检查单中必须背诵的条目"},

    # --- 飞控 / 气动 -------------------------------------------------------
    {"en": "flight control system", "zh": "飞控系统", "case_sensitive": False, "note": ""},
    {"en": "FLCS", "zh": "飞控系统", "case_sensitive": True, "note": ""},
    {"en": "side stick", "zh": "侧置驾驶杆", "case_sensitive": False, "note": ""},
    {"en": "stick", "zh": "驾驶杆", "case_sensitive": False, "note": ""},
    {"en": "aileron", "zh": "副翼", "case_sensitive": False, "note": ""},
    {"en": "elevator", "zh": "升降舵", "case_sensitive": False, "note": ""},
    {"en": "rudder", "zh": "方向舵", "case_sensitive": False, "note": ""},
    {"en": "flap", "zh": "襟翼", "case_sensitive": False, "note": ""},
    {"en": "speed brake", "zh": "减速板", "case_sensitive": False, "note": ""},
    {"en": "airbrake", "zh": "减速板", "case_sensitive": False, "note": ""},
    {"en": "trim", "zh": "配平", "case_sensitive": False, "note": ""},
    {"en": "autopilot", "zh": "自动驾驶", "case_sensitive": False, "note": ""},
    {"en": "angle of attack", "zh": "迎角", "case_sensitive": False, "note": ""},
    {"en": "AOA", "zh": "迎角", "case_sensitive": True, "note": ""},
    {"en": "stall", "zh": "失速", "case_sensitive": False, "note": ""},
    {"en": "departure", "zh": "偏离（失控）", "case_sensitive": False, "note": "飞控语境"},
    {"en": "pitch", "zh": "俯仰", "case_sensitive": False, "note": ""},
    {"en": "roll", "zh": "滚转", "case_sensitive": False, "note": ""},
    {"en": "yaw", "zh": "偏航", "case_sensitive": False, "note": ""},
    {"en": "bank", "zh": "坡度", "case_sensitive": False, "note": ""},
    {"en": "nose-up", "zh": "抬头", "case_sensitive": False, "note": ""},
    {"en": "nose-down", "zh": "低头", "case_sensitive": False, "note": ""},
    {"en": "G-force", "zh": "过载", "case_sensitive": False, "note": ""},

    # --- 动力 / 供氧 / 电气 -------------------------------------------------
    {"en": "EPU", "zh": "应急动力装置", "case_sensitive": True, "note": ""},
    {"en": "emergency power unit", "zh": "应急动力装置", "case_sensitive": False, "note": ""},
    {"en": "OBOGS", "zh": "机载制氧系统", "case_sensitive": True, "note": ""},
    {"en": "afterburner", "zh": "加力燃烧室", "case_sensitive": False, "note": ""},
    {"en": "AB detent", "zh": "加力止动位", "case_sensitive": False, "note": ""},
    {"en": "MIL power", "zh": "军用推力", "case_sensitive": False, "note": ""},
    {"en": "idle", "zh": "慢车", "case_sensitive": False, "note": ""},
    {"en": "throttle", "zh": "油门", "case_sensitive": False, "note": ""},
    {"en": "engine", "zh": "发动机", "case_sensitive": False, "note": ""},
    {"en": "inlet", "zh": "进气道", "case_sensitive": False, "note": ""},
    {"en": "nozzle", "zh": "尾喷口", "case_sensitive": False, "note": ""},
    {"en": "rpm", "zh": "转速", "case_sensitive": False, "note": ""},
    {"en": "generator", "zh": "发电机", "case_sensitive": False, "note": ""},
    {"en": "battery", "zh": "电瓶", "case_sensitive": False, "note": ""},
    {"en": "bus", "zh": "汇流条", "case_sensitive": False, "note": ""},
    {"en": "circuit breaker", "zh": "断路器", "case_sensitive": False, "note": ""},
    {"en": "fire warning", "zh": "火警", "case_sensitive": False, "note": ""},
    {"en": "hydraulic system", "zh": "液压系统", "case_sensitive": False, "note": ""},
    {"en": "fuel flow", "zh": "燃油流量", "case_sensitive": False, "note": ""},
    {"en": "fuel", "zh": "燃油", "case_sensitive": False, "note": ""},
    {"en": "external tank", "zh": "外挂副油箱", "case_sensitive": False, "note": ""},
    {"en": "drop tank", "zh": "副油箱", "case_sensitive": False, "note": ""},
    {"en": "aerial refueling", "zh": "空中加油", "case_sensitive": False, "note": ""},
    {"en": "air refueling", "zh": "空中加油", "case_sensitive": False, "note": ""},

    # --- 起降 / 地面 -------------------------------------------------------
    {"en": "landing gear", "zh": "起落架", "case_sensitive": False, "note": ""},
    {"en": "nose gear", "zh": "前起落架", "case_sensitive": False, "note": ""},
    {"en": "main gear", "zh": "主起落架", "case_sensitive": False, "note": ""},
    {"en": "anti-skid", "zh": "防滑刹车", "case_sensitive": False, "note": ""},
    {"en": "wheel brake", "zh": "机轮刹车", "case_sensitive": False, "note": ""},
    {"en": "arresting hook", "zh": "尾钩", "case_sensitive": False, "note": ""},
    {"en": "runway", "zh": "跑道", "case_sensitive": False, "note": ""},
    {"en": "taxiway", "zh": "滑行道", "case_sensitive": False, "note": ""},
    {"en": "taxi", "zh": "滑行", "case_sensitive": False, "note": ""},
    {"en": "takeoff", "zh": "起飞", "case_sensitive": False, "note": ""},
    {"en": "landing flare", "zh": "着陆拉平", "case_sensitive": False, "note": ""},
    {"en": "landing", "zh": "着陆", "case_sensitive": False, "note": ""},
    {"en": "approach", "zh": "进近", "case_sensitive": False, "note": ""},
    {"en": "go-around", "zh": "复飞", "case_sensitive": False, "note": ""},

    # --- 航电 / 显示 -------------------------------------------------------
    {"en": "head-up display", "zh": "平视显示器", "case_sensitive": False, "note": ""},
    {"en": "HUD", "zh": "平视显示器", "case_sensitive": True, "note": "常简称「平显」"},
    {"en": "multi-function display", "zh": "多功能显示器", "case_sensitive": False, "note": ""},
    {"en": "MFD", "zh": "多功能显示器", "case_sensitive": True, "note": ""},
    {"en": "MFDS", "zh": "多功能显示器组", "case_sensitive": True, "note": ""},
    {"en": "radar warning receiver", "zh": "雷达告警接收机", "case_sensitive": False, "note": ""},
    {"en": "RWR", "zh": "雷达告警接收机", "case_sensitive": True, "note": ""},
    {"en": "fire control radar", "zh": "火控雷达", "case_sensitive": False, "note": ""},
    {"en": "FCR", "zh": "火控雷达", "case_sensitive": True, "note": ""},
    {"en": "voice message system", "zh": "语音告警系统", "case_sensitive": False, "note": ""},
    {"en": "VMS", "zh": "语音告警系统", "case_sensitive": True, "note": ""},
    {"en": "pilot fault list", "zh": "飞行员故障清单", "case_sensitive": False, "note": ""},
    {"en": "PFL", "zh": "飞行员故障清单", "case_sensitive": True, "note": ""},
    {"en": "built-in test", "zh": "机内自检", "case_sensitive": False, "note": ""},
    {"en": "BIT", "zh": "机内自检", "case_sensitive": True, "note": ""},
    {"en": "data transfer cartridge", "zh": "数据传送盒", "case_sensitive": False, "note": ""},
    {"en": "DTC", "zh": "数据传送盒", "case_sensitive": True, "note": ""},
    {"en": "inertial navigation system", "zh": "惯性导航系统", "case_sensitive": False, "note": ""},
    {"en": "INS", "zh": "惯性导航系统", "case_sensitive": True, "note": ""},
    {"en": "horizontal situation indicator", "zh": "水平位置指示器", "case_sensitive": False, "note": ""},
    {"en": "HSI", "zh": "水平位置指示器", "case_sensitive": True, "note": ""},
    {"en": "attitude indicator", "zh": "姿态指示器", "case_sensitive": False, "note": ""},
    {"en": "airspeed indicator", "zh": "空速表", "case_sensitive": False, "note": ""},
    {"en": "radar altimeter", "zh": "雷达高度表", "case_sensitive": False, "note": ""},
    {"en": "altimeter", "zh": "高度表", "case_sensitive": False, "note": ""},
    {"en": "data entry display", "zh": "数据输入显示器", "case_sensitive": False, "note": ""},
    {"en": "DED", "zh": "数据输入显示器", "case_sensitive": True, "note": ""},
    {"en": "integrated control panel", "zh": "综合控制面板", "case_sensitive": False, "note": ""},
    {"en": "ICP", "zh": "综合控制面板", "case_sensitive": True, "note": ""},
    {"en": "up-front controller", "zh": "前上方控制器", "case_sensitive": False, "note": ""},
    {"en": "UFC", "zh": "前上方控制器", "case_sensitive": True, "note": ""},
    {"en": "option select button", "zh": "周边按键", "case_sensitive": False, "note": ""},
    {"en": "OSB", "zh": "周边按键", "case_sensitive": True, "note": ""},
    {"en": "HOTAS", "zh": "手不离杆操纵", "case_sensitive": True, "note": ""},
    {"en": "avionics", "zh": "航电系统", "case_sensitive": False, "note": ""},

    # --- 导航 --------------------------------------------------------------
    {"en": "steerpoint", "zh": "航路点", "case_sensitive": False, "note": ""},
    {"en": "STPT", "zh": "航路点", "case_sensitive": True, "note": ""},
    {"en": "waypoint", "zh": "航路点", "case_sensitive": False, "note": ""},
    {"en": "TACAN", "zh": "塔康", "case_sensitive": True, "note": "战术空中导航系统"},
    {"en": "instrument landing system", "zh": "仪表着陆系统", "case_sensitive": False, "note": ""},
    {"en": "ILS", "zh": "仪表着陆系统", "case_sensitive": True, "note": ""},
    {"en": "bullseye", "zh": "靶眼", "case_sensitive": False, "note": ""},
    {"en": "mark point", "zh": "标记点", "case_sensitive": False, "note": ""},
    {"en": "datalink", "zh": "数据链", "case_sensitive": False, "note": ""},
    {"en": "TOT", "zh": "目标到达时间", "case_sensitive": True, "note": ""},
    {"en": "IP", "zh": "起始点", "case_sensitive": True, "note": "攻击起始点"},

    # --- 武器 / 对抗 -------------------------------------------------------
    {"en": "identification friend or foe", "zh": "敌我识别", "case_sensitive": False, "note": ""},
    {"en": "IFF", "zh": "敌我识别", "case_sensitive": True, "note": ""},
    {"en": "weapon", "zh": "武器", "case_sensitive": False, "note": ""},
    {"en": "missile", "zh": "导弹", "case_sensitive": False, "note": ""},
    {"en": "cannon", "zh": "机炮", "case_sensitive": False, "note": ""},
    {"en": "gun", "zh": "机炮", "case_sensitive": False, "note": ""},
    {"en": "stores", "zh": "外挂物", "case_sensitive": False, "note": ""},
    {"en": "pylon", "zh": "挂架", "case_sensitive": False, "note": ""},
    {"en": "targeting pod", "zh": "瞄准吊舱", "case_sensitive": False, "note": ""},
    {"en": "TGP", "zh": "目标指示吊舱", "case_sensitive": True, "note": ""},
    {"en": "laser-guided bomb", "zh": "激光制导炸弹", "case_sensitive": False, "note": ""},
    {"en": "LGB", "zh": "激光制导炸弹", "case_sensitive": True, "note": ""},
    {"en": "JDAM", "zh": "联合直接攻击弹药", "case_sensitive": True, "note": ""},
    {"en": "HARM", "zh": "高速反辐射导弹", "case_sensitive": True, "note": ""},
    {"en": "chaff", "zh": "箔条", "case_sensitive": False, "note": ""},
    {"en": "countermeasure flare", "zh": "红外干扰弹", "case_sensitive": False, "note": ""},
    {"en": "jammer", "zh": "干扰机", "case_sensitive": False, "note": ""},
    {"en": "electronic countermeasures", "zh": "电子对抗", "case_sensitive": False, "note": ""},
    {"en": "ECM", "zh": "电子对抗", "case_sensitive": True, "note": ""},
    {"en": "lock", "zh": "锁定", "case_sensitive": False, "note": ""},
    {"en": "launch", "zh": "发射", "case_sensitive": False, "note": ""},
    {"en": "ripple", "zh": "连投", "case_sensitive": False, "note": ""},
    {"en": "dive", "zh": "俯冲", "case_sensitive": False, "note": ""},

    # --- 战术 / 战法 -------------------------------------------------------
    {"en": "radar", "zh": "雷达", "case_sensitive": False, "note": ""},
    {"en": "track while scan", "zh": "边扫描边跟踪", "case_sensitive": False, "note": ""},
    {"en": "TWS", "zh": "边扫描边跟踪", "case_sensitive": True, "note": ""},
    {"en": "air combat maneuvering", "zh": "空战机动", "case_sensitive": False, "note": ""},
    {"en": "ACM", "zh": "空战机动", "case_sensitive": True, "note": ""},
    {"en": "beyond visual range", "zh": "超视距", "case_sensitive": False, "note": ""},
    {"en": "BVR", "zh": "超视距", "case_sensitive": True, "note": ""},
    {"en": "within visual range", "zh": "视距内", "case_sensitive": False, "note": ""},
    {"en": "WVR", "zh": "视距内", "case_sensitive": True, "note": ""},
    {"en": "suppression of enemy air defenses", "zh": "压制敌方防空", "case_sensitive": False, "note": ""},
    {"en": "SEAD", "zh": "压制敌方防空", "case_sensitive": True, "note": ""},
    {"en": "close air support", "zh": "近距离空中支援", "case_sensitive": False, "note": ""},
    {"en": "CAS", "zh": "近距离空中支援", "case_sensitive": True, "note": ""},
    {"en": "wingman", "zh": "僚机", "case_sensitive": False, "note": ""},
    {"en": "flight lead", "zh": "长机", "case_sensitive": False, "note": ""},
    {"en": "element", "zh": "双机组", "case_sensitive": False, "note": ""},
    {"en": "tanker", "zh": "加油机", "case_sensitive": False, "note": ""},
    {"en": "AWACS", "zh": "预警机", "case_sensitive": True, "note": ""},
    {"en": "surface-to-air missile", "zh": "地空导弹", "case_sensitive": False, "note": ""},
    {"en": "SAM", "zh": "地空导弹", "case_sensitive": True, "note": ""},
    {"en": "anti-aircraft artillery", "zh": "高射炮", "case_sensitive": False, "note": ""},
    {"en": "AAA", "zh": "高射炮", "case_sensitive": True, "note": ""},
    {"en": "air-to-air", "zh": "空对空", "case_sensitive": False, "note": ""},
    {"en": "air-to-ground", "zh": "空对地", "case_sensitive": False, "note": ""},
    {"en": "ingress", "zh": "进入攻击", "case_sensitive": False, "note": ""},
    {"en": "egress", "zh": "退出攻击", "case_sensitive": False, "note": ""},
    {"en": "Viper", "zh": "毒蛇", "case_sensitive": True, "note": "F-16 绰号，通常保留原名"},
]

# 必须原样保留的编号/型号（提示词中明确列出，不参与替换）
KEEP_TOKENS = [
    "F-16", "F-16C", "F-16D", "BMS", "BMS 4.38.1", "Block 50", "TR_BMS_01_GroundOPS",
]

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']*")
_DEFAULT_MAX_BLOCK_CHARS = 6000

_CACHE: dict[str, Any] = {}


def glossary_path() -> Path:
    """`data/glossary.json` 的绝对路径（遵循 MT_DATA_DIR 覆盖）。"""
    try:
        import config  # 本地包

        return Path(config.data_dir()) / "glossary.json"
    except Exception:  # pragma: no cover - 极端环境下退化到仓库根
        return Path(__file__).resolve().parent.parent / "data" / "glossary.json"


def default_entries() -> list[dict[str, Any]]:
    """返回内置默认术语表的深拷贝。"""
    return [dict(e) for e in DEFAULT_GLOSSARY]


def _coerce_entries(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        en = str(item.get("en", "")).strip()
        zh = str(item.get("zh", "")).strip()
        if not en or not zh:
            continue
        out.append({
            "en": en,
            "zh": zh,
            "case_sensitive": bool(item.get("case_sensitive", False)),
            "note": str(item.get("note", "") or ""),
        })
    return out


def ensure_file(path: Optional[Any] = None) -> Path:
    """确保术语表文件存在；不存在则用内置表写出。返回路径。"""
    p = Path(path) if path else glossary_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(default_entries(), ensure_ascii=False, indent=2)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(p)
    return p


def load(path: Optional[Any] = None, reload: bool = False) -> list[dict[str, Any]]:
    """加载术语表；文件缺失/损坏时回退到内置默认表。"""
    p = Path(path) if path else glossary_path()
    key = str(p)
    if not reload and key in _CACHE:
        return _CACHE[key]
    entries: list[dict[str, Any]] = []
    try:
        if not p.exists():
            ensure_file(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        entries = _coerce_entries(raw)
    except (OSError, ValueError):
        entries = []
    if not entries:
        entries = default_entries()
    _CACHE[key] = entries
    return entries


def save(entries: Iterable[dict[str, Any]], path: Optional[Any] = None) -> Path:
    """写回术语表（原子替换）。"""
    p = Path(path) if path else glossary_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_coerce_entries(list(entries)), ensure_ascii=False, indent=2)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(p)
    _CACHE.pop(str(p), None)
    return p


def get_pairs(case_sensitive: Optional[bool] = None,
              path: Optional[Any] = None) -> list[tuple[str, str]]:
    """返回 [(en, zh)]；按 en 长度降序（长术语优先，避免部分替换）。"""
    pairs: list[tuple[str, str]] = []
    for e in load(path):
        if case_sensitive is not None and bool(e.get("case_sensitive")) != bool(case_sensitive):
            continue
        pairs.append((e["en"], e["zh"]))
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return pairs


def lookup_map(path: Optional[Any] = None) -> dict[str, str]:
    """返回 {匹配键: zh}；非大小写敏感项以 casefold 作为键。"""
    out: dict[str, str] = {}
    for e in load(path):
        key = e["en"] if e.get("case_sensitive") else e["en"].casefold()
        out.setdefault(key, e["zh"])
    return out


def _is_code_like(en: str) -> bool:
    """缩写/型号类术语（产出「中文（英文）」形式）。"""
    if any(ch.isdigit() for ch in en):
        return True
    letters = [c for c in en if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


# --------------------------------------------------------------------------
# 术语强制替换（apply）
#
# 真实译文缺陷回归（Lead 在第 4 页发现）：
#   `(TR_BMS_05_ILS_Landing)` 曾被替换成 `(TR_BMS_05_仪表着陆系统（ILS）_着陆)`。
# 两道防线：
#   1) **屏蔽**代码样 token（文件名/路径/版本号/型号），替换前用占位符换出、替换后还原；
#   2) 词边界把 `_ . \ / -` 也算作词内字符（不能在其中切断）。
# 另外：替换必须幂等，且不得在中文之间留下半角空格。
# --------------------------------------------------------------------------
_CODE_RUN_RE = re.compile(r"[A-Za-z0-9_.\\/\-]{2,}")     # 代码样 token 的候选
_CODE_SEP_RE = re.compile(r"[_.\\/]")                    # 含这些分隔符 => 一定是代码样
_CODE_MODEL_RE = re.compile(r"[A-Z]{2,}-?[0-9]+")        # AIM-120 / F16 / MFD2
_BOUNDARY_L = r"(?<![A-Za-z0-9_.\\/\-])"
_BOUNDARY_R = r"(?![A-Za-z0-9_.\\/\-])"
_PLACEHOLDER = "\x00{}\x00"

# 中文（含全角标点/括号）之间不得有空格
_CJK_CLASS = r"\u2e80-\u9fff\u3000-\u303f\uff00-\uffef"
_SPACE_BETWEEN_CJK_RE = re.compile(rf"(?<=[{_CJK_CLASS}])\s+(?=[{_CJK_CLASS}])")

_PATTERN_CACHE: dict[int, tuple[Any, Any, dict[str, dict[str, Any]]]] = {}


def _code_spans(text: str) -> list[tuple[int, int]]:
    """代码样 token 的区间：含 `_ . \\ /` 的连续串，或 `[A-Z]{2,}-?[0-9]+` 型号编号。"""
    spans: list[tuple[int, int]] = []
    for m in _CODE_RUN_RE.finditer(text):
        tok = m.group(0)
        if _CODE_SEP_RE.search(tok) or _CODE_MODEL_RE.fullmatch(tok):
            spans.append(m.span())
    return spans


def _mask_code(text: str) -> tuple[str, list[str]]:
    """把代码样 token 换成占位符，返回 (屏蔽后文本, 原 token 列表)。"""
    spans = _code_spans(text)
    if not spans:
        return text, []
    parts: list[str] = []
    kept: list[str] = []
    pos = 0
    for i, (start, end) in enumerate(spans):
        parts.append(text[pos:start])
        parts.append(_PLACEHOLDER.format(i))
        kept.append(text[start:end])
        pos = end
    parts.append(text[pos:])
    return "".join(parts), kept


def _unmask_code(text: str, kept: list[str]) -> str:
    for i, tok in enumerate(kept):
        text = text.replace(_PLACEHOLDER.format(i), tok)
    return text


def _collapse_cjk_spaces(text: str) -> str:
    """去掉「中文 中文」之间的半角空格（替换过程会把英文两侧的空白带进来）。"""
    return _SPACE_BETWEEN_CJK_RE.sub("", text)


def _term_pattern(path: Optional[Any] = None):
    """编译「所有术语」的合并正则 + 匹配表（按 en 长度降序，长术语优先）。

    按术语表**对象身份**缓存编译结果：`load()` 返回的是同一个 list 对象，
    文件重载/保存后对象更换会自动重建，不会命中过期正则。
    """
    entries = load(path)
    hit = _PATTERN_CACHE.get(id(entries))
    if hit is not None and hit[0] is entries:
        return hit[1], hit[2]
    parts: list[str] = []
    lut: dict[str, dict[str, Any]] = {}
    for e in sorted(entries, key=lambda x: len(x["en"]), reverse=True):
        body = re.escape(e["en"])
        parts.append(body if e.get("case_sensitive") else f"(?i:{body})")
        lut.setdefault(e["en"].casefold(), e)
    pattern = (re.compile(_BOUNDARY_L + "(?:" + "|".join(parts) + ")" + _BOUNDARY_R)
               if parts else None)
    _PATTERN_CACHE[id(entries)] = (entries, pattern, lut)
    return pattern, lut


def apply(text: str, path: Optional[Any] = None, *, with_abbrev: bool = True) -> str:
    """把已译文本里残留的英文术语强制替换为术语表中文。

    * **代码样 token（文件名/路径/版本号/型号）先屏蔽后还原，绝不被替换**；
    * 长术语优先，避免部分覆盖；
    * 同一术语在**同一段内**首次出现用「中文（缩写）」，其后只保留缩写（幂等）；
    * 若对应中文已在匹配点之前出现（含模型自己写出的「中文（缩写）」），保留英文原样；
    * 替换后清掉中文之间的半角空格，保证 `apply(apply(x)) == apply(x)`。
    """
    if not text:
        return text
    pattern, lut = _term_pattern(path)
    if pattern is None:
        return text

    masked, kept = _mask_code(text)
    out: list[str] = []
    emitted_zh: set[str] = set()
    pos = 0
    for m in pattern.finditer(masked):
        out.append(masked[pos:m.start()])
        matched = m.group(0)
        entry = lut.get(matched.casefold())
        if entry is None:
            out.append(matched)
            pos = m.end()
            continue
        zh = entry["zh"]
        if zh in emitted_zh or zh in masked[:m.start()]:
            out.append(matched)                       # 已展开过 -> 保留缩写
        elif with_abbrev and _is_code_like(entry["en"]):
            out.append(f"{zh}（{matched}）")           # 首次出现 -> 中文（缩写）
            emitted_zh.add(zh)
        else:
            out.append(zh)
            emitted_zh.add(zh)
        pos = m.end()
    out.append(masked[pos:])
    return _unmask_code(_collapse_cjk_spaces("".join(out)), kept)


def prompt_block(texts: Optional[Iterable[str]] = None, path: Optional[Any] = None,
                 max_chars: int = _DEFAULT_MAX_BLOCK_CHARS) -> str:
    """拼进提示词的术语清单。

    texts 给定则只列出「该批文本中实际出现」的术语（显著省 token）；
    未给定时列出全表（超出 max_chars 截断）。
    """
    entries = load(path)
    if texts is not None:
        joined = "\n".join(str(t or "") for t in texts)
        lowered = joined.casefold()
        picked = []
        for e in entries:
            needle = e["en"] if e.get("case_sensitive") else e["en"].casefold()
            if needle in (joined if e.get("case_sensitive") else lowered):
                picked.append(e)
        entries = picked or entries[:20]
    else:
        entries = sorted(entries, key=lambda x: len(x["en"]), reverse=True)

    lines = ["[术语表] 以下术语必须使用规定译法（英 = 中）："]
    used = 0
    for e in entries:
        line = f"{e['en']} = {e['zh']}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    lines.append("[保留原样] " + "、".join(KEEP_TOKENS))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 离线占位后端（mock）——纯术语表逐词替换，可复现、无网络
# --------------------------------------------------------------------------
def mock_translate(text: str, path: Optional[Any] = None) -> str:
    """术语表驱动的确定性占位翻译。

    规则：最长匹配（最多 5 个连续词）；命中 → 中文术语；
    未命中的英文词 → 前后加 `〔〕` 保留原文；代码样 token（文件名/路径/型号）原样保留；
    标点/数字/空白原样保留。
    """
    if not text:
        return ""
    lut = lookup_map(path)
    masked, kept = _mask_code(text)
    text = masked
    tokens = list(_WORD_RE.finditer(text))
    out: list[str] = []
    cursor = 0        # text 中已消费到的位置
    i = 0
    while i < len(tokens):
        best = None       # (end_token_index, zh)
        for span in range(min(5, len(tokens) - i), 0, -1):
            chunk = tokens[i:i + span]
            # 词语之间只允许空白分隔
            gap_ok = True
            for a, b in zip(chunk, chunk[1:]):
                if text[a.end():b.start()].strip():
                    gap_ok = False
                    break
            if not gap_ok:
                continue
            phrase = " ".join(m.group(0) for m in chunk)
            zh = lut.get(phrase) or lut.get(phrase.casefold())
            if zh is not None:
                best = (i + span, zh, phrase)
                break
        if best is None:
            tok = tokens[i]
            out.append(text[cursor:tok.start()])
            out.append("〔" + tok.group(0) + "〕")
            cursor = tok.end()
            i += 1
        else:
            end_idx, zh, _phrase = best
            start = tokens[i].start()
            out.append(text[cursor:start])
            out.append(zh)
            cursor = tokens[end_idx - 1].end()
            i = end_idx
    out.append(text[cursor:])
    return _unmask_code("".join(out), kept)
