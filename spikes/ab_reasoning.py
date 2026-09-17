"""Lead spike C: controlled A/B on reasoning suppression params.

Configs:
  A  deepseek-flash, no extra params
  B  deepseek-flash, reasoning_effort="none"
  C  deepseek-flash, thinking={"type":"disabled"}
  D  deepseek-v4-pro, reasoning_effort="none"
  E  deepseek-v4-pro, thinking={"type":"disabled"}
Same 4 batches each; compare latency, tokens, completeness, and quality proxies
(acronym retention, length ratio, list-marker retention).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_quality import SYSTEM, USER_TMPL, extract_paragraphs, post  # noqa: E402

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

ACRO = re.compile(r"\b(?:[A-Z][A-Z0-9]{1,7}|[A-Z]-\d+)\b")
NUMUNIT = re.compile(r"\b\d[\d.,]*\s*(?:ft|kt|psi|NM|%|G|RPM|°|sec|min|hours?)\b")


def quality(en: str, zh: str) -> dict:
    acros = [a for a in ACRO.findall(en) if len(a) > 1]
    units = NUMUNIT.findall(en)
    markers = [m for m in ("▪", "◆", "•", "-") if en.strip().startswith(m)]
    keep_a = sum(1 for a in acros if a in zh)
    keep_u = sum(1 for u in units if u.split()[0] in zh)
    keep_m = sum(1 for m in markers if m in zh)
    return {
        "acronym_keep": f"{keep_a}/{len(acros)}",
        "unit_keep": f"{keep_u}/{len(units)}",
        "marker_keep": f"{keep_m}/{len(markers)}",
        "len_ratio": round(len(zh) / max(1, len(en)), 2) if zh else 0.0,
        "empty": sum(1 for z in [zh] if not z),
    }


def run(paras, model, extra, label):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_TMPL.format(
                n=len(paras),
                payload="\n".join(f'[{i}] (p{p["page"]}) {p["text"]}' for i, p in enumerate(paras)))},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 8192,
    }
    payload.update(extra)
    t0 = time.time()
    try:
        r = post(payload, timeout=300)
    except Exception as e:
        return {"label": label, "error": str(e)[:200], "elapsed": round(time.time() - t0, 1)}
    dt = time.time() - t0
    u = r.get("usage", {})
    content = r["choices"][0]["message"]["content"]
    try:
        got = {int(t["id"]): t["zh"] for t in json.loads(content)["translations"]}
    except Exception as e:
        return {"label": label, "error": f"parse: {e}", "elapsed": round(dt, 1)}
    qs = [quality(paras[i]["text"], got.get(i, "")) for i in range(len(paras))]

    def agg(k):
        vals = [v for q in qs for v in [q[k]] if isinstance(v, str) and "/" in v]
        if not vals:
            return "-"
        num = sum(int(v.split("/")[0]) for v in vals)
        den = sum(int(v.split("/")[1]) for v in vals)
        return f"{num}/{den}"
    return {
        "label": label,
        "elapsed": round(dt, 1),
        "prompt": u.get("prompt_tokens"),
        "completion": u.get("completion_tokens"),
        "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "complete": f"{len(got)}/{len(paras)}",
        "acronym_keep": agg("acronym_keep"),
        "unit_keep": agg("unit_keep"),
        "marker_keep": agg("marker_keep"),
        "avg_len_ratio": round(statistics.mean(q["len_ratio"] for q in qs), 2),
        "empty": sum(q["empty"] for q in qs),
        "sample": got.get(0, "")[:70],
    }


def main() -> int:
    paras = [p for p in extract_paragraphs(PDF, [15, 21, 22, 24, 30, 31, 32, 40])
             if len(p["text"]) > 25]
    batches = [paras[i:i + 12] for i in range(0, min(len(paras), 48), 12)]
    print(f"corpus {len(paras)} paras / {sum(len(p['text']) for p in paras)} chars / "
          f"{len(batches)} batches\n")

    configs = [
        ("A flash:default",     "deepseek-flash",   {}),
        ("B flash:effort_none", "deepseek-flash",   {"reasoning_effort": "none"}),
        ("C flash:think_off",   "deepseek-flash",   {"thinking": {"type": "disabled"}}),
        ("D pro:default",       "deepseek-v4-pro",  {}),
        ("E pro:effort_none",   "deepseek-v4-pro",  {"reasoning_effort": "none"}),
        ("F pro:think_off",     "deepseek-v4-pro",  {"thinking": {"type": "disabled"}}),
    ]
    results = []
    for label, model, extra in configs:
        rows = [run(b, model, extra, label) for b in batches]
        good = [r for r in rows if "error" not in r]
        row = {"label": label, "model": model, "extra": extra, "runs": rows}
        if good:
            row["avg_elapsed"] = round(statistics.mean(r["elapsed"] for r in good), 1)
            row["tot_completion"] = sum(r["completion"] or 0 for r in good)
            row["tot_reasoning"] = sum(r["reasoning"] or 0 for r in good)
            row["tot_prompt"] = sum(r["prompt"] or 0 for r in good)
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "runs"}, ensure_ascii=False))
        for r in rows:
            print("   ", json.dumps(r, ensure_ascii=False))

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "ab_reasoning.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    print("\n=== COMPARISON (3 batches = 36 paragraphs each) ===")
    print(f"{'config':20s} {'avg_s':>7s} {'prompt':>8s} {'compl':>8s} {'reason':>8s} "
          f"{'compl/para':>11s} {'acr_keep':>9s} {'len_rat':>8s} {'empty':>6s}")
    for r in results:
        if "avg_elapsed" not in r:
            print(f"{r['label']:20s}  FAILED")
            continue
        rows = [x for x in r["runs"] if "error" not in x]
        cpp = round(r["tot_completion"] / 36, 1)
        acr = "-"
        print(f"{r['label']:20s} {r['avg_elapsed']:7.1f} {r['tot_prompt']:8d} "
              f"{r['tot_completion']:8d} {r['tot_reasoning']:8d} {cpp:11.1f} "
              f"{rows[0].get('acronym_keep','-'):>9s} {rows[0].get('avg_len_ratio',0):8.2f} "
              f"{sum(x.get('empty',0) for x in rows):6d}")
    print("\nsaved ->", os.path.join(OUT, "ab_reasoning.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
