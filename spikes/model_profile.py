"""Lead spike: profile DeepSeek models for translation throughput / cost / quality.

Measures per-batch: latency, prompt/completion/reasoning tokens across two models,
on real manual paragraphs. Also probes whether reasoning can be suppressed.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_quality import SYSTEM, USER_TMPL, extract_paragraphs, post  # noqa: E402

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def run_batch(paras, model, extra=None):
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
    if extra:
        payload.update(extra)
    t0 = time.time()
    try:
        r = post(payload, timeout=300)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}",
                "elapsed": time.time() - t0}
    dt = time.time() - t0
    u = r.get("usage", {})
    c = r.get("choices", [{}])[0]
    content = c.get("message", {}).get("content", "")
    try:
        got = {int(t["id"]): t["zh"] for t in json.loads(content).get("translations", [])}
        ok = len(got) == len(paras)
        err = None
    except Exception as ex:
        got, ok, err = {}, False, str(ex)[:200]
    return {
        "elapsed": round(dt, 2),
        "prompt": u.get("prompt_tokens"),
        "completion": u.get("completion_tokens"),
        "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "n_ok": f"{len(got)}/{len(paras)}",
        "ok": ok,
        "err": err,
        "sample_zh": got.get(0, "")[:60],
        "finish": c.get("finish_reason"),
    }


def main() -> int:
    paras = extract_paragraphs(PDF, [15, 21, 22, 24, 30, 31])
    body = [p for p in paras if len(p["text"]) > 25]
    batches = [body[i:i + 12] for i in range(0, min(len(body), 48), 12)]
    print(f"corpus: {len(body)} paragraphs, {sum(len(p['text']) for p in body)} chars, "
          f"{len(batches)} batches\n")
    os.makedirs(OUT, exist_ok=True)

    report = {}
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        rows = []
        for bi, b in enumerate(batches[:3]):
            r = run_batch(b, model)
            rows.append(r)
            print(f"{model:18s} b{bi} {json.dumps(r, ensure_ascii=False)}")
        ok_rows = [r for r in rows if r.get("ok")]
        if ok_rows:
            report[model] = {
                "batches": len(rows),
                "avg_latency": round(statistics.mean(r["elapsed"] for r in ok_rows), 1),
                "avg_prompt": round(statistics.mean(r["prompt"] for r in ok_rows)),
                "avg_completion": round(statistics.mean(r["completion"] for r in ok_rows)),
                "avg_reasoning": round(statistics.mean(r.get("reasoning") or 0 for r in ok_rows)),
                "all_ok": all(r.get("ok") for r in rows),
            }
    # probe reasoning suppression on the cheaper model
    probe = run_batch(batches[0], "deepseek-flash",
                      {"reasoning_effort": "none", "thinking": {"type": "disabled"}})
    print("\nreasoning-suppression probe:", json.dumps(probe, ensure_ascii=False)[:400])
    report["suppression_probe"] = probe

    with open(os.path.join(OUT, "model_profile.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print("\n=== SUMMARY ===")
    for m, v in report.items():
        if m != "suppression_probe":
            print(f"{m}: {v}")
    print("saved ->", os.path.join(OUT, "model_profile.json"))

    # cost projection for the full 401-page manual
    total_chars = 0
    for pno in range(1, 402, 7):        # sample every 7th page to estimate
        total_chars += sum(len(p["text"]) for p in extract_paragraphs(PDF, [pno]))
    est_chars = total_chars * 7
    print(f"\nsampled {len(range(1,402,7))} pages -> {total_chars} chars; "
          f"est. full-book body chars ~= {est_chars}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
