"""CONTRACT §7 翻译引擎：DeepSeek + 离线占位双后端 / 并发 / 缓存 / 增量复用与种子。

核心入口：
    translate_segments(conn, doc_id, version_id, *, segment_ids=None, page_from=None,
                       page_to=None, provider=None, model=None, concurrency=6,
                       force=False, job_id=None, cfg=None, dry_run=False) -> dict

返回（CONTRACT §7 字段齐全）：
    {"translated","carried","cached","failed","chars","tokens":{"prompt","completion"}}
额外字段（向后兼容的附加信息）：
    seeded / machine / api_calls / batches / provider / model / dry_run / cost / errors

并发：`ThreadPoolExecutor`；**每批独立 sqlite 连接**（sqlite3 连接不可跨线程共享），
批次结束即 close；写回遇 `database is locked` 自动重试。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

from config import load as load_config
from core import db as core_db
from core import store
from core.fingerprint import fingerprint as fp_digest
from core.fingerprint import ratio as text_ratio
from translate import glossary

# 状态字面量以 core.models.TranslationStatus 为准（CONTRACT §2：小写）
try:
    from core.models import TranslationStatus as _TS
    ST_MACHINE, ST_CARRIED = _TS.MACHINE.value, _TS.CARRIED.value
    ST_SEEDED, ST_FAILED = _TS.SEEDED.value, _TS.FAILED.value
except Exception:                                     # pragma: no cover
    ST_MACHINE, ST_CARRIED, ST_SEEDED, ST_FAILED = (
        "machine", "carried", "seeded", "failed")
from translate.prompt import (build_messages, estimate_messages_tokens,
                              estimate_tokens, extract_content, parse_response)

# --- 默认参数（可被 cfg 覆盖）------------------------------------------------
BATCH_SEGMENTS = 12
BATCH_CHARS = 1800
TIMEOUT = 120
MAX_RETRIES = 3
SEED_MIN_RATIO = 0.72
BACKOFF = (1.0, 2.0, 4.0)
DEFAULT_MAX_TOKENS = 4096
MIN_MAX_TOKENS = 2048

# 价格（USD / 1M tokens）—— 来源：https://api-docs.deepseek.com/quick_start/pricing（off-peak 半价，此处取 off-peak）
# 可用 cfg["price_in"]/["price_out"] 覆盖。cache hit 输入价极低（$0.003/$0.022），本估算未单独计。
PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"in": 0.66, "out": 1.98},
    "deepseek-flash": {"in": 0.15, "out": 0.60},
    "mock": {"in": 0.0, "out": 0.0},
}

# --- 推理模式（实测决定，勿随意改）------------------------------------------
# DeepSeek 官方文档：思维链**默认开启**，默认 effort=high。
# Lead 在真实语料上做了 A/B（spikes/ab_reasoning.py，4 批 × 36 段，结果见 spikes/out/ab_reasoning.json）：
#   flash 默认          avg 10.4s  completion 8713  其中 reasoning 6720  （可见译文仅 ~2000）
#   flash reasoning=none avg  3.0s  completion 2285  其中 reasoning 0
#   flash thinking=disabled avg 2.7s  completion 2017  其中 reasoning 0
#   pro   默认          avg 65.9s  completion 23217 其中 reasoning 20744
#   pro   reasoning=none avg  6.4s  completion 2338  其中 reasoning 0
# 关闭思维链后：延迟降 3.8x，输出 token 降 4.3x，而缩写保留率/长度比/完整性**无退化**。
# 因此默认关闭思维链；需要更高质量时把 cfg["thinking_mode"] 设为 "enabled"。
DEFAULT_THINKING_MODE = "disabled"


def _thinking_enabled(cfg: Mapping[str, Any]) -> bool:
    """是否开启思维链（`_build_payload` 与 dry_run 估算共用，保证两者永不脱节）。"""
    mode = str((cfg or {}).get("thinking_mode") or DEFAULT_THINKING_MODE).lower()
    return mode in ("enabled", "on", "true", "1")


_LETTER_RE = re.compile(r"[A-Za-z\u3400-\u9fff]")
_NON_BODY_ROLES = {"header", "footer", "pagenum", "noise"}

# 旧版 glossary.apply 的缺陷残留：标识符被中文截断，如 `TR_BMS_05_仪表着陆系统（ILS）_着陆`。
# 命中该特征的已有译文 / 缓存一律不信任并自动重译（自愈），无需 --force 全量重跑。
_BROKEN_IDENT_RE = re.compile(r"[A-Za-z0-9]_[\u3400-\u9fff]|[\u3400-\u9fff]_[A-Za-z0-9]")


def _looks_broken_identifier(text: str) -> bool:
    return bool(text) and bool(_BROKEN_IDENT_RE.search(text))


class HttpFatalError(RuntimeError):
    """不可重试的 HTTP 错误（鉴权/参数错误）。"""


# ==========================================================================
# 基础设施
# ==========================================================================
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(cfg: Mapping[str, Any]) -> sqlite3.Connection:
    """新建一个独立 sqlite 连接（每批/每线程一个）。"""
    path = cfg.get("db_path")
    conn = core_db.connect(path) if path else core_db.connect()
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    return conn


def _db_retry(fn, *, attempts: int = 6, base: float = 0.05):
    """对 `database is locked` / `database is busy` 做指数退避重试。"""
    last: Optional[BaseException] = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = exc
            time.sleep(base * (2 ** i))
    assert last is not None
    raise last


def _cell(row: Any, key: str, index: int) -> Any:
    """兼容 sqlite3.Row / tuple / dict 三种行。"""
    try:
        return row[key]
    except (TypeError, IndexError, KeyError):
        pass
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[index]
    except (TypeError, IndexError):
        return None


# ==========================================================================
# translation_cache（§7 步骤 5）
# ==========================================================================
def _cache_lookup(conn: sqlite3.Connection,
                  fingerprints: Sequence[str]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    fps = [f for f in dict.fromkeys(fingerprints) if f]
    for i in range(0, len(fps), 300):
        chunk = fps[i:i + 300]
        placeholders = ",".join("?" for _ in chunk)
        sql = ("SELECT fingerprint, text, provider, model FROM translation_cache "
               f"WHERE fingerprint IN ({placeholders})")
        try:
            rows = _db_retry(lambda: conn.execute(sql, chunk).fetchall())
        except sqlite3.Error:
            return found          # 表缺失等异常：视为无缓存，不影响主流程
        for r in rows:
            fp = _cell(r, "fingerprint", 0)
            text = _cell(r, "text", 1)
            if fp and text:
                found[str(fp)] = {"text": str(text),
                                  "provider": str(_cell(r, "provider", 2) or ""),
                                  "model": str(_cell(r, "model", 3) or "")}
    return found


def _cache_store(conn: sqlite3.Connection, fingerprint: str, text: str,
                 provider: str, model: str) -> None:
    """§7 步骤 5：写跨版本译文缓存（优先用 core.store 的 DAO，保留 hits 计数）。"""
    if not fingerprint or not text:
        return
    put = getattr(store, "put_cached_translation", None)
    sql = ("INSERT INTO translation_cache"
           "(fingerprint, text, provider, model, created_at) VALUES(?,?,?,?,?) "
           "ON CONFLICT(fingerprint) DO UPDATE SET text=excluded.text, "
           "provider=excluded.provider, model=excluded.model, created_at=excluded.created_at")
    try:
        if callable(put):
            _db_retry(lambda: put(conn, fingerprint=fingerprint, text=text,
                                  provider=provider, model=model))
        else:
            _db_retry(lambda: conn.execute(sql, (fingerprint, text, provider, model, _now())))
            _db_retry(conn.commit)
    except sqlite3.Error:
        pass                       # 缓存是可选项，写失败不阻断翻译


# ==========================================================================
# 段筛选与分批
# ==========================================================================
@dataclass
class _Item:
    seg_id: int
    text: str
    page: int = 1
    role: str = "body"
    kind: str = "paragraph"
    fingerprint: str = ""
    normalized: str = ""


@dataclass
class _Plan:
    cached: int = 0                                   # 同版本已有译文 -> 跳过
    local_carried: list = field(default_factory=list)  # 非 body / 琐碎段 -> CARRIED
    cache_carried: list = field(default_factory=list)  # (item, cache_row) -> CARRIED
    api_items: list = field(default_factory=list)      # 需要调用后端的段
    seeds: dict = field(default_factory=dict)          # seg_id -> {"en","zh","old_id"}


def _is_trivial(text: str) -> bool:
    """纯符号 / 纯数字 / 长度 < 2 的段。"""
    t = (text or "").strip()
    if len(t) < 2:
        return True
    return _LETTER_RE.search(t) is None


def _collect_items(conn: sqlite3.Connection, version_id: int,
                   segment_ids: Optional[Iterable[int]],
                   page_from: Optional[int], page_to: Optional[int],
                   doc_id: int = 0) -> list[_Item]:
    rows: list[Any]
    if segment_ids is not None:
        wanted = [int(s) for s in segment_ids]
        by_id = getattr(store, "segments_by_ids", None)
        if callable(by_id):
            mapping = by_id(conn, wanted) or {}
            rows = [mapping[sid] for sid in wanted if sid in mapping]
        else:
            rows = []
            for sid in wanted:
                row = store.get_segment(conn, sid)
                if row:
                    rows.append(row)
    else:
        rows = list(store.segments_of_version(conn, version_id) or [])

    items: list[_Item] = []
    for row in rows:
        page = int(row["page"] or 1)
        if page_from is not None and page < int(page_from):
            continue
        if page_to is not None and page > int(page_to):
            continue
        vid = row["version_id"] if "version_id" in row.keys() else version_id
        if doc_id and "doc_id" in row.keys() and row["doc_id"] not in (None, doc_id, 0):
            continue
        text = str(row["text"] or "")
        fp = str((row["fingerprint"] if "fingerprint" in row.keys() else "") or "")
        norm = str((row["normalized"] if "normalized" in row.keys() else "") or "")
        if not fp:
            try:
                fp = fp_digest(text)
            except Exception:
                fp = ""
        items.append(_Item(
            seg_id=int(row["id"]),
            text=text,
            page=page,
            role=str((row["role"] if "role" in row.keys() else "body") or "body"),
            kind=str((row["kind"] if "kind" in row.keys() else "paragraph") or "paragraph"),
            fingerprint=fp,
            normalized=norm,
        ))
    items.sort(key=lambda it: it.seg_id)
    return items


def _make_batches(items: Sequence[_Item], max_segments: int,
                  max_chars: int) -> list[list[_Item]]:
    batches: list[list[_Item]] = []
    cur: list[_Item] = []
    chars = 0
    for it in items:
        n = len(it.text)
        if cur and (len(cur) >= max_segments or chars + n > max_chars):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(it)
        chars += n
    if cur:
        batches.append(cur)
    return batches


def _seeds_for(conn: sqlite3.Connection, version_id: int,
               items: Sequence[_Item]) -> dict[int, dict[str, Any]]:
    """§7 步骤 3：用 `segment_links` 反查旧版本译文作为 seed（ratio >= 0.72）。

    返回 `{seg_id: {"en": 旧英文, "zh": 旧中文, "old_id": 旧段 id}}`；
    `old_id` 会写进 `translations.reuse_of`，便于前端追溯来源。
    """
    seeds: dict[int, dict[str, Any]] = {}
    try:
        links = store.links_for_version(conn, version_id) or {}
    except Exception:
        return seeds
    if not links:
        return seeds
    pairs = []
    for it in items:
        link = links.get(it.seg_id)
        if link:
            pairs.append((it, link))
    if not pairs:
        return seeds

    old_ids = {int(link["old_segment_id"]) for _it, link in pairs
               if link.get("old_segment_id")}
    old_segs: dict[int, Any] = {}
    for sid in old_ids:
        try:
            row = store.get_segment(conn, sid)
        except Exception:
            row = None
        if row:
            old_segs[sid] = row
    trs: dict[int, dict] = {}
    old_versions: set[int] = {int(row["version_id"]) for row in old_segs.values()}
    for vid in old_versions:
        try:
            trs.update(store.translations_of_version(conn, vid) or {})
        except Exception:
            pass

    for it, link in pairs:
        old_id = int(link.get("old_segment_id") or 0)
        old = old_segs.get(old_id)
        tr = trs.get(old_id)
        if not old or not tr or not (tr.get("text") or "").strip():
            continue
        r = float(link.get("ratio") or 0.0)
        if r <= 0.0:
            try:
                r = float(text_ratio(str(old["text"] or ""), it.text))
            except Exception:
                r = 0.0
        if r >= SEED_MIN_RATIO:
            seeds[it.seg_id] = {"en": str(old["text"] or ""),
                                "zh": str(tr["text"]), "old_id": old_id}
    return seeds


# ==========================================================================
# HTTP 层（测试通过替换 `translate.engine._http_post` 打桩）
# ==========================================================================
def _http_post(url: str, payload: Mapping[str, Any], timeout: float = TIMEOUT,
               api_key: str = "") -> Any:
    """用 urllib 发 POST JSON。返回已解析的响应体（dict）。

    4xx（除 408/429）视为不可重试错误，抛 `HttpFatalError`。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "manual-trans-trace/1.0")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        code = int(getattr(exc, "code", 0) or 0)
        if 400 <= code < 500 and code not in (408, 429):
            raise HttpFatalError(f"HTTP {code}: {detail}") from exc
        raise RuntimeError(f"HTTP {code}: {detail}") from exc
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from API: {raw[:300]!r}") from exc


def _chat_url(cfg: Mapping[str, Any]) -> str:
    base = str(cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"


def _build_payload(messages: Sequence[Mapping[str, str]], model: str,
                   cfg: Mapping[str, Any], src_chars: int) -> dict[str, Any]:
    est = MIN_MAX_TOKENS + int(src_chars * 1.6)
    max_tokens = int(cfg.get("max_tokens") or max(MIN_MAX_TOKENS, min(8192, est)))
    thinking_on = _thinking_enabled(cfg)
    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "response_format": {"type": "json_object"},
        "max_tokens": max(MIN_MAX_TOKENS, max_tokens),
        "stream": False,
    }
    if thinking_on:
        # 思维链模式不支持 temperature（传了也无效），显式给 effort
        payload["reasoning_effort"] = str(cfg.get("reasoning_effort") or "high")
        payload["thinking"] = {"type": "enabled"}
    else:
        # 关闭思维链：官方两种写法都生效（见 DEFAULT_THINKING_MODE 注释里的 A/B 数据）
        payload["reasoning_effort"] = "none"
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = float(cfg.get("temperature", 0.2))
    return payload


def _usage_from(resp: Any, messages: Sequence[Mapping[str, str]],
                out: Mapping[int, str]) -> dict[str, int]:
    usage = resp.get("usage") if isinstance(resp, Mapping) else None
    if isinstance(usage, Mapping):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if prompt is not None or completion is not None:
            return {"prompt": int(prompt or 0), "completion": int(completion or 0),
                    "estimated": 0}
    return {"prompt": estimate_messages_tokens(messages),
            "completion": sum(estimate_tokens(t) for t in out.values()),
            "estimated": 1}


def _backoff(cfg: Mapping[str, Any]) -> tuple[float, ...]:
    cfg_backoff = cfg.get("retry_backoff")
    if isinstance(cfg_backoff, (list, tuple)) and cfg_backoff:
        return tuple(float(x) for x in cfg_backoff)
    if isinstance(cfg_backoff, (int, float)):
        return (float(cfg_backoff),) * 3
    return BACKOFF


# ==========================================================================
# 单批执行（可被测试包裹计数）
# ==========================================================================
def _run_batch(items: Sequence[_Item], seeds: Mapping[int, Mapping[str, Any]], *,
               provider: str, model: str, cfg: Mapping[str, Any], api_key: str,
               glossary_block: str) -> dict[str, Any]:
    """执行一个批次的翻译（含重试）。返回 {ok,out,usage,attempts,api_calls,error}。"""
    ids = [it.seg_id for it in items]
    texts = [it.text for it in items]
    batch_seed = {sid: seeds[sid] for sid in ids if sid in seeds}
    attempts = max(1, int(cfg.get("max_retries", MAX_RETRIES)))
    backoff = _backoff(cfg)
    timeout = float(cfg.get("request_timeout", TIMEOUT))
    last_err: Optional[BaseException] = None
    calls = 0

    for attempt in range(attempts):
        calls += 1
        try:
            if provider == "mock":
                out = {it.seg_id: glossary.mock_translate(it.text) for it in items}
                usage = {"prompt": estimate_tokens(glossary_block) +
                                   sum(estimate_tokens(t) for t in texts),
                         "completion": sum(estimate_tokens(t) for t in out.values()),
                         "estimated": 1}
            else:
                messages = build_messages(items, glossary_block, seed=batch_seed)
                payload = _build_payload(messages, model, cfg, sum(len(t) for t in texts))
                resp = _http_post(_chat_url(cfg), payload, timeout, api_key)
                content = extract_content(resp)
                out = parse_response(content, ids)
                if cfg.get("glossary_enforce", True):
                    out = {sid: glossary.apply(txt) for sid, txt in out.items()}
                usage = _usage_from(resp, messages, out)
            return {"ok": True, "out": out, "usage": usage,
                    "attempts": attempt + 1, "api_calls": calls, "error": ""}
        except HttpFatalError as exc:
            last_err = exc
            break
        except Exception as exc:                    # noqa: BLE001 - 逐批降级
            last_err = exc
            if attempt < attempts - 1:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
    return {"ok": False, "out": {}, "usage": {"prompt": 0, "completion": 0, "estimated": 0},
            "attempts": calls, "api_calls": calls,
            "error": f"{type(last_err).__name__}: {last_err}"}


def _worker(batch: Sequence[_Item], seeds: Mapping[int, Mapping[str, Any]], *,
            provider: str, model: str, cfg: Mapping[str, Any], api_key: str,
            glossary_block: str, status_by_id: Mapping[int, str],
            reuse_by_id: Optional[Mapping[int, int]] = None,
            dry_run: bool = False) -> dict[str, Any]:
    """线程工作单元：新开 sqlite 连接 -> 调用后端 -> 写回 -> close。"""
    reuse_by_id = reuse_by_id or {}
    conn = None
    try:
        if not dry_run:
            conn = _connect(cfg)
        res = _run_batch(batch, seeds, provider=provider, model=model, cfg=cfg,
                         api_key=api_key, glossary_block=glossary_block)
        unit = {"ok": res["ok"], "error": res.get("error", ""),
                "api_calls": int(res.get("api_calls", 0)),
                "attempts": int(res.get("attempts", 0)),
                "usage": res.get("usage", {"prompt": 0, "completion": 0}),
                "written": [], "failed_ids": [], "chars": 0}
        if dry_run:
            unit["written"] = [(it.seg_id, res["out"].get(it.seg_id, ""),
                                status_by_id[it.seg_id]) for it in batch if res["ok"]]
            unit["failed_ids"] = [] if res["ok"] else [it.seg_id for it in batch]
            unit["chars"] = sum(len(it.text) for it in batch) if res["ok"] else 0
            return unit

        assert conn is not None
        if res["ok"]:
            out = res["out"]
            for it in batch:
                text = (out.get(it.seg_id) or "").strip()
                status = status_by_id[it.seg_id]
                if not text:
                    status = ST_FAILED
                    text = ""
                fingerprint = it.fingerprint or fp_digest(it.text)
                reuse_of = reuse_by_id.get(it.seg_id)
                try:
                    _db_retry(lambda sid=it.seg_id, tx=text, st=status, ro=reuse_of:
                              store.upsert_translation(
                                  conn, segment_id=sid, text=tx, status=st, provider=provider,
                                  model=model, confidence=0.9 if st != ST_FAILED else 0.0,
                                  reuse_of=ro if st == ST_SEEDED else None))
                except sqlite3.Error as exc:
                    unit["error"] = f"write failed: {exc}"
                    status = ST_FAILED
                    text = ""
                if status == ST_FAILED:
                    unit["failed_ids"].append(it.seg_id)
                else:
                    _cache_store(conn, fingerprint, text, provider, model)
                    unit["written"].append((it.seg_id, text, status))
                    unit["chars"] += len(it.text)
            try:
                _db_retry(conn.commit)
            except sqlite3.Error:
                pass
        else:
            for it in batch:
                try:
                    _db_retry(lambda sid=it.seg_id: store.upsert_translation(
                        conn, segment_id=sid, text="", status=ST_FAILED,
                        provider=provider, model=model, confidence=0.0))
                except sqlite3.Error:
                    pass
                unit["failed_ids"].append(it.seg_id)
            try:
                _db_retry(conn.commit)
            except sqlite3.Error:
                pass
        return unit
    except Exception as exc:                        # noqa: BLE001 - 单批失败不得中断整体
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "api_calls": 0,
                "attempts": 0, "usage": {"prompt": 0, "completion": 0},
                "written": [], "chars": 0,
                "failed_ids": [it.seg_id for it in batch]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ==========================================================================
# 费用估算
# ==========================================================================
def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  cfg: Optional[Mapping[str, Any]] = None) -> dict[str, float]:
    cfg = cfg or {}
    price = dict(PRICES.get(model, PRICES.get("deepseek-v4-pro", {"in": 0.66, "out": 1.98})))
    if cfg.get("price_in") is not None:
        price["in"] = float(cfg["price_in"])
    if cfg.get("price_out") is not None:
        price["out"] = float(cfg["price_out"])
    usd = prompt_tokens / 1e6 * price["in"] + completion_tokens / 1e6 * price["out"]
    rate = float(cfg.get("usd_cny", 7.1))
    return {"usd": round(usd, 6), "cny": round(usd * rate, 4),
            "usd_per_mtok_in": price["in"], "usd_per_mtok_out": price["out"],
            "estimated": True,
            "note": "价格取自 api-docs.deepseek.com/quick_start/pricing 的 off-peak 档；"
                    "thinking_mode=disabled 时不再产生推理 token"}


# ==========================================================================
# 主入口
# ==========================================================================
def translate_segments(conn, doc_id, version_id, *, segment_ids=None, page_from=None,
                       page_to=None, provider=None, model=None, concurrency=6,
                       force=False, job_id=None, cfg=None, dry_run=False) -> dict:
    """按 CONTRACT §7 翻译一个版本（或指定段）。"""
    merged = dict(load_config())
    if cfg:
        merged.update(dict(cfg))            # 入参优先
    cfg = merged

    provider = str(provider or cfg.get("provider") or "deepseek").lower()
    if provider not in ("deepseek", "mock"):
        raise ValueError(f"unknown provider: {provider!r} (expected 'deepseek' or 'mock')")
    model = str(model or ("mock" if provider == "mock" else cfg.get("model") or
                          "deepseek-v4-pro"))
    api_key = str(cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY") or "")
    max_segments = int(cfg.get("batch_segments", BATCH_SEGMENTS))
    max_chars = int(cfg.get("batch_chars", BATCH_CHARS))

    own_conn = False
    if conn is None:
        conn = _connect(cfg)
        own_conn = True

    result: dict[str, Any] = {
        "translated": 0, "carried": 0, "cached": 0, "failed": 0, "chars": 0,
        "tokens": {"prompt": 0, "completion": 0},
        "seeded": 0, "machine": 0, "api_calls": 0, "batches": 0,
        "provider": provider, "model": model, "dry_run": bool(dry_run),
        "repaired": 0, "errors": [],
    }

    try:
        items = _collect_items(conn, version_id, segment_ids, page_from, page_to,
                               doc_id=doc_id)
        if not items:
            result["cost"] = estimate_cost(model, 0, 0, cfg)
            return result

        try:
            existing = store.translations_of_version(conn, version_id) or {}
        except Exception:
            existing = {}

        # --- [1] 同版本已有译文 -> 跳过（标识符被截断的旧译文除外，重译自愈）------
        pending: list[_Item] = []
        repaired_ids: set[int] = set()
        for it in items:
            row = existing.get(it.seg_id)
            text = str((row or {}).get("text") or "").strip()
            if row and not force and text:
                if _looks_broken_identifier(text):
                    repaired_ids.add(it.seg_id)     # 旧缺陷残留 -> 不信任，重译
                    pending.append(it)
                else:
                    result["cached"] += 1
            else:
                pending.append(it)

        # --- 跳过 role != body 与琐碎段 -> CARRIED ----------------------
        plan = _Plan()
        to_translate: list[_Item] = []
        for it in pending:
            if it.role != "body" or _is_trivial(it.text):
                plan.local_carried.append(it)
            else:
                to_translate.append(it)

        # --- [2] translation_cache 命中 -> CARRIED ----------------------
        # force=True 时连缓存一起绕过（"强制重译"语义）；仍保留 [3] 的 links 种子
        if to_translate and not force:
            cache = _cache_lookup(conn, [it.fingerprint for it in to_translate])
        else:
            cache = {}
        cache_left: list[_Item] = []
        for it in to_translate:
            hit = cache.get(it.fingerprint)
            hit_text = str((hit or {}).get("text") or "").strip()
            if hit_text and _looks_broken_identifier(hit_text):
                # 缓存里是旧缺陷产物 -> 视为未命中，重新翻译并覆盖缓存
                repaired_ids.add(it.seg_id)
                cache_left.append(it)
            elif hit_text:
                plan.cache_carried.append((it, hit))
            else:
                cache_left.append(it)

        result["repaired"] = len(repaired_ids)

        # --- [3] 旧版本链接 ratio>=0.72 -> SEEDED -----------------------
        plan.seeds = _seeds_for(conn, version_id, cache_left) if cache_left else {}
        machine_items = [it for it in cache_left if it.seg_id not in plan.seeds]

        result["carried"] = len(plan.local_carried) + len(plan.cache_carried)
        result["chars"] += sum(len(it.text) for it in plan.local_carried)
        result["chars"] += sum(len(it.text) for it, _hit in plan.cache_carried)

        # 写回 CARRIED（dry_run 不落库）
        # 若该段在 segment_links 里与旧段指纹一致，则把旧段 id 记进 reuse_of（来源可追溯）
        if not dry_run:
            cache_reuse: dict[int, int] = {}
            try:
                links = store.links_for_version(conn, version_id) or {}
            except Exception:
                links = {}
            for it, _hit in plan.cache_carried:
                link = links.get(it.seg_id) or {}
                old_id = link.get("old_segment_id")
                if old_id:
                    try:
                        old = store.get_segment(conn, int(old_id))
                    except Exception:
                        old = None
                    if old and str(old.get("fingerprint") or "") == it.fingerprint:
                        cache_reuse[it.seg_id] = int(old_id)
            for it in plan.local_carried:
                _safe_upsert(conn, it.seg_id, it.text, ST_CARRIED, provider, model)
            for it, hit in plan.cache_carried:
                _safe_upsert(conn, it.seg_id, str(hit.get("text") or ""), ST_CARRIED,
                             provider, model, reuse_of=cache_reuse.get(it.seg_id))
        # --- [4] 批次与并发 --------------------------------------------
        seeded_items = [it for it in cache_left if it.seg_id in plan.seeds]
        batches = _make_batches(seeded_items + machine_items, max_segments, max_chars)
        status_by_id = {it.seg_id: (ST_SEEDED if it.seg_id in plan.seeds else ST_MACHINE)
                        for it in seeded_items + machine_items}
        reuse_by_id = {it.seg_id: int(plan.seeds[it.seg_id]["old_id"])
                       for it in seeded_items
                       if plan.seeds[it.seg_id].get("old_id")}
        result["batches"] = len(batches)

        total_units = len(items)
        _job_update(conn, job_id, status="running", total=total_units,
                    progress=result["cached"] + result["carried"],
                    message=f"翻译中 provider={provider} model={model} 批次={len(batches)}")

        if dry_run:
            prompt_tokens = 0
            completion_tokens = 0
            # 推理 token 只在思维链开启时产生：默认 disabled 时实测 reasoning=0
            # （A/B 见文件头 DEFAULT_THINKING_MODE 注释）；开启时按 flash 实测 ~6720/请求估。
            thinking_on = _thinking_enabled(cfg)
            reasoning = int(cfg.get("reasoning_tokens_per_request",
                                    6720 if thinking_on else 0))
            for batch in batches:
                texts = [it.text for it in batch]
                block = glossary.prompt_block(texts)
                messages = build_messages(batch, block,
                                          seed={k: v for k, v in plan.seeds.items()
                                                if any(it.seg_id == k for it in batch)})
                prompt_tokens += estimate_messages_tokens(messages)
                # 英译中：中文 token ≈ 源字符数 * 0.6，再加每批推理开销（默认 0）
                completion_tokens += int(sum(len(t) for t in texts) * 0.6) + reasoning
            result["tokens"] = {"prompt": prompt_tokens, "completion": completion_tokens}
            result["translated"] = len(seeded_items) + len(machine_items)
            result["seeded"] = len(seeded_items)
            result["machine"] = len(machine_items)
            result["chars"] += sum(len(it.text) for it in seeded_items + machine_items)
            result["api_calls"] = 0
            result["cost"] = estimate_cost(model, prompt_tokens, completion_tokens, cfg)
            result["cost"]["assumptions"] = {
                "batches": len(batches), "segments": result["translated"],
                "prompt_tokens_estimated": True,
                "completion_chars_ratio": 0.6,
                "thinking_mode": "enabled" if thinking_on else "disabled",
                "reasoning_tokens_per_request": reasoning,
                "note": "prompt 命中缓存时单价更低；实际以 API usage 为准",
            }
            return result

        if not batches:
            result["cost"] = estimate_cost(model, 0, 0, cfg)
            return result

        workers = int(concurrency or cfg.get("concurrency", 6) or 1)
        workers = max(1, workers)
        done = result["cached"] + result["carried"]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for batch in batches:
                block = glossary.prompt_block([it.text for it in batch])
                fut = pool.submit(_worker, batch, plan.seeds, provider=provider,
                                  model=model, cfg=cfg, api_key=api_key,
                                  glossary_block=block, status_by_id=status_by_id,
                                  reuse_by_id=reuse_by_id)
                futures[fut] = batch
            for fut in as_completed(futures):
                batch = futures[fut]
                try:
                    unit = fut.result()
                except Exception as exc:            # noqa: BLE001
                    unit = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "api_calls": 0, "usage": {"prompt": 0, "completion": 0},
                            "written": [], "failed_ids": [it.seg_id for it in batch],
                            "chars": 0}
                result["api_calls"] += int(unit.get("api_calls", 0))
                usage = unit.get("usage") or {}
                result["tokens"]["prompt"] += int(usage.get("prompt", 0) or 0)
                result["tokens"]["completion"] += int(usage.get("completion", 0) or 0)
                for _sid, _text, status in unit.get("written", []):
                    if status == ST_SEEDED:
                        result["seeded"] += 1
                    else:
                        result["machine"] += 1
                result["translated"] += len(unit.get("written", []))
                result["chars"] += int(unit.get("chars", 0))
                failed_ids = unit.get("failed_ids") or []
                if failed_ids:
                    result["failed"] += len(failed_ids)
                    if unit.get("error"):
                        result["errors"].append({"segment_ids": failed_ids,
                                                 "error": unit["error"]})
                done += len(batch)
                _job_update(conn, job_id, progress=done, total=total_units,
                            message=(unit.get("error") if failed_ids else
                                     f"已翻译 {result['translated']} 段"))

        result["cost"] = estimate_cost(model, result["tokens"]["prompt"],
                                       result["tokens"]["completion"], cfg)
        _job_update(conn, job_id, progress=total_units, total=total_units,
                    message=f"翻译完成：translated={result['translated']} "
                            f"carried={result['carried']} failed={result['failed']}")
        return result
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ==========================================================================
# 小工具
# ==========================================================================
def _safe_upsert(conn, segment_id: int, text: str, status: str, provider: str,
                 model: str, reuse_of: Optional[int] = None) -> bool:
    """写回译文；遇 sqlite 锁自动重试，绝不让任务崩掉。"""
    base = dict(segment_id=segment_id, text=text, status=status,
                provider=provider, model=model,
                confidence=1.0 if status == ST_CARRIED else 0.0)

    def _do():
        try:
            return store.upsert_translation(conn, reuse_of=reuse_of, **base)
        except TypeError:                             # 老 DAO 无 reuse_of 参数
            if reuse_of is None:
                raise
            return store.upsert_translation(conn, **base)

    try:
        _db_retry(_do)
        _db_retry(conn.commit)
        return True
    except sqlite3.Error:
        return False


def _job_update(conn, job_id, **fields) -> None:
    if not job_id:
        return
    try:
        _db_retry(lambda: store.update_job(conn, int(job_id), **fields))
    except Exception:
        pass
