"""提示词构造与响应解析（只依赖标准库）。

`build_messages(batch, glossary_block, seed=None)` -> messages
`parse_response(raw, batch_ids)`                -> {seg_id: 中文文本}

解析要求「容错」：模型返回 ```json 围栏、前后多余说明文字、缺 id（抛 ValueError 触发重试）、
多出 id（忽略）都必须能处理。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

# --------------------------------------------------------------------------
# system 提示词（本项目核心质量点，勿轻易精简）
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """你是资深航空技术翻译，负责把 F-16 战斗机模拟器（BMS 4.38.1）训练手册从英文翻译成简体中文。
读者是中文飞行员/学员，译文必须像正规航空出版物，而不是口语化直译。

【译法】
1. 使用航空/军事领域标准译法（例如：MASTER CAUTION=主警戒灯，canopy=座舱盖，flight control system=飞控系统，
   steerpoint=航路点，multifunction display=多功能显示器，afterburner=加力燃烧室，landing gear=起落架）。
2. 术语严格按下方【术语表】执行；术语表未覆盖的，全书保持同一译法，不得同义反复换词。
3. 语序按中文习惯调整，被动改主动；祈使句保持祈使语气；条件句用「若……则……」。
4. 不得遗漏、不得合并、不得拆分段意；不要添加任何解释、注释、译者按、括注说明。

【必须原样保留（不要翻译、不要改写）】
- 型号与版本号：F-16、F-16C、BMS 4.38.1、Block 50 等。
- 缩写：**首次出现**用「中文（缩写）」形式，例如「主警戒灯（MASTER CAUTION）」「机载制氧系统（OBOGS）」；
  同一段落内**再次出现同一缩写时只写缩写本身**（如 HOTAS），或只用中文简称，
  不要重复展开成「手不离杆操纵（HOTAS）」。
- 文件名 / 标识符 / 路径 / 版本号 / 任务代号：**逐字符原样保留**，内部不得翻译、不得插入中文、
  不得增删下划线或点号。例如：TR_BMS_05_ILS_Landing、TR_BMS_01_GroundOPS、callsign.ini、
  BMS 4.38.1、（/Docs/02 Aircraft Manuals & Checklists\01 F-16）、Dash-34。
  即使其中含 ILS/HUD/MFD 等缩写，也**不要**替换或加中文括注。
- 计量单位与其数值：ft、kt、psi、NM、ft/min、°、%、lbs、G、RPM 等一律保留原单位符号。
- 键盘按键名（如 `Enter`）、菜单/页面/模式名（如 MFD 页面名 DED、TACAN 页）、
  座舱面板标注（如 MASTER CAUTION 面板丝印）。
- 项目符号与序号：▪、◆、●、-、*、以及 1. / 1) / (a) 等编号，逐字符保留在原位置。

【格式】
- 只输出纯文本译文，不要 markdown 代码块、不要加粗/标题记号。
- 输出必须是单个 JSON 对象，形如：
  {"translations":[{"id":12,"zh":"译文……"},{"id":13,"zh":"译文……"}]}
- id 必须与输入一一对应，段落数与输入完全相同，不得新增或省略 id。
- 输入中的页眉/页脚已被上游过滤；若仍遇到，按原文照抄不译。

【修订模式】
若某段提供了上一版英文（prev_en）与上一版中文（prev_zh），请在上一版译文基础上修订，
使其与新英文一致：保留未变部分、只改动受影响的表述，输出修订后的完整译文（不要只输出差异）。

不要输出 JSON 之外的任何字符。"""

USER_INSTRUCTION = (
    "请翻译下面 JSON 中的 segments，返回 JSON 对象 {\"translations\":[{\"id\":<int>,\"zh\":\"...\"}]}。"
    "只输出 JSON。保留型号/缩写/单位/符号与序号；文件名/标识符/路径逐字符照抄，段落与 id 一一对应。"
)


def _norm_items(batch: Iterable[Any]) -> list[dict[str, Any]]:
    """把 batch 归一化成 [{"id": int, "text": str}]。

    支持 dict（id / segment_id / seg_id；text / source / en）、具有同名属性的对象、
    以及 (id, text) 二元组。
    """
    items: list[dict[str, Any]] = []
    for it in batch:
        if isinstance(it, (tuple, list)) and len(it) >= 2:
            sid, text, *rest = list(it)
            extra = rest[0] if rest and isinstance(rest[0], dict) else {}
            items.append({"id": int(sid), "text": str(text), **extra})
            continue
        if isinstance(it, Mapping):
            sid = it.get("id", it.get("segment_id", it.get("seg_id")))
            text = it.get("text", it.get("source", it.get("en", "")))
        else:
            sid = getattr(it, "id", None)
            if sid is None:
                sid = getattr(it, "segment_id", None)
            if sid is None:
                sid = getattr(it, "seg_id", None)
            text = getattr(it, "text", "")
        if sid is None:
            raise ValueError(f"batch item without id: {it!r}")
        items.append({"id": int(sid), "text": str(text)})
    return items


def _norm_seed(seed: Optional[Any]) -> dict[int, dict[str, str]]:
    """seed -> {seg_id: {"en":..., "zh":...}}。"""
    out: dict[int, dict[str, str]] = {}
    if not seed:
        return out
    if isinstance(seed, Mapping):
        for k, v in seed.items():
            try:
                sid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, Mapping):
                en = str(v.get("en", v.get("text", "")) or "")
                zh = str(v.get("zh", v.get("translation", "")) or "")
            elif isinstance(v, (tuple, list)) and len(v) >= 2:
                en, zh = str(v[0] or ""), str(v[1] or "")
            else:
                continue
            if zh:
                out[sid] = {"en": en, "zh": zh}
    return out


def build_messages(batch: Iterable[Any], glossary_block: str = "",
                   seed: Optional[Any] = None) -> list[dict[str, str]]:
    """构造 messages；返回 [{"role":"system","content":...},{"role":"user","content":...}]。"""
    items = _norm_items(batch)
    seeds = _norm_seed(seed)

    system = SYSTEM_PROMPT
    if glossary_block:
        system += "\n\n" + str(glossary_block).strip()

    payload_items: list[dict[str, Any]] = []
    for it in items:
        entry: dict[str, Any] = {"id": it["id"], "text": it["text"]}
        s = seeds.get(it["id"])
        if s:
            entry["prev_en"] = s["en"]
            entry["prev_zh"] = s["zh"]
        payload_items.append(entry)

    user = USER_INSTRUCTION + "\n" + json.dumps(
        {"segments": payload_items}, ensure_ascii=False, separators=(",", ":")
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# 响应解析
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_fences(raw: str) -> str:
    m = _FENCE_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _extract_balanced(text: str) -> Optional[str]:
    """从可能夹带说明文字的字符串里抽出第一个完整的 JSON 对象/数组。"""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def extract_content(resp: Any) -> str:
    """从 API 响应体里取出模型文本（兼容 OpenAI/DeepSeek chat.completions 结构）。"""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, Mapping):
        raise ValueError(f"unexpected response type: {type(resp)!r}")
    choices = resp.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            msg = first.get("message")
            if isinstance(msg, Mapping):
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):     # 分段 content
                    return "".join(
                        str(c.get("text", "")) for c in content if isinstance(c, Mapping)
                    )
            if isinstance(first.get("text"), str):
                return str(first["text"])
    for key in ("content", "output_text", "text"):
        if isinstance(resp.get(key), str):
            return str(resp[key])
    raise ValueError("no model content found in response")


def loads_lenient(raw: Any) -> Any:
    """宽松 JSON 解析：去围栏、抽平衡块、容忍前后说明文字。"""
    if isinstance(raw, (Mapping, list)):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"cannot parse {type(raw)!r} as JSON")
    text = _strip_fences(raw)
    if not text:
        raise ValueError("empty model response")
    try:
        return json.loads(text)
    except ValueError:
        pass
    block = _extract_balanced(text)
    if block is None:
        raise ValueError("no JSON object found in model response")
    try:
        return json.loads(block)
    except ValueError as exc:
        raise ValueError(f"invalid JSON in model response: {exc}") from exc


def _iter_records(data: Any) -> list[Any]:
    """把解析出的结构统一成记录列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in ("translations", "items", "results", "segments", "data", "output"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, Mapping) and all(
                str(k).lstrip("-").isdigit() for k in val.keys()
            ):
                return [{"id": k, "zh": v} for k, v in val.items()]
        # 直接 {id: text} 映射
        if data and all(str(k).lstrip("-").isdigit() for k in data.keys()):
            return [{"id": k, "zh": v} for k, v in data.items()]
        return [data]
    return []


_ID_KEYS = ("id", "segment_id", "seg_id", "segmentId")
_TEXT_KEYS = ("zh", "text", "translation", "target", "translated", "zh_text", "chinese")


def _pick(rec: Mapping, keys: Sequence[str]) -> Any:
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return None


def parse_response(raw: Any, batch_ids: Iterable[int]) -> dict[int, str]:
    """把模型响应解析成 {seg_id: text}。

    * 缺失 id / 空译文 -> ValueError（由上层重试）；
    * 多出的 id 直接忽略；
    * 重复 id 取最后一次非空值。
    """
    wanted = [int(x) for x in batch_ids]
    data = loads_lenient(raw)
    records = _iter_records(data)
    out: dict[int, str] = {}
    for rec in records:
        if not isinstance(rec, Mapping):
            if isinstance(rec, str):
                continue
            raise ValueError(f"unexpected record type: {type(rec)!r}")
        sid_raw = _pick(rec, _ID_KEYS)
        text = _pick(rec, _TEXT_KEYS)
        if sid_raw is None:
            continue
        try:
            sid = int(str(sid_raw).strip())
        except (TypeError, ValueError):
            continue
        if text is None:
            continue
        text = str(text).strip()
        if text:
            out[sid] = text
    missing = [sid for sid in wanted if sid not in out]
    if missing:
        raise ValueError(f"model response missing ids: {missing[:20]}"
                         f"{' ...' if len(missing) > 20 else ''}")
    return {sid: out[sid] for sid in wanted}


# --------------------------------------------------------------------------
# 估算（dry_run / 预算）
# --------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中日韩字符约 1 token/字，拉丁约 1 token/4 字符。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff")
    other = len(text) - cjk
    return int(cjk + max(0.0, other / 3.6) + 0.5)


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("content", ""))) + 4
    return total
