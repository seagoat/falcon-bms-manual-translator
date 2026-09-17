"""真实数据驱动：起服务后用真实 BMS 手册（前 20 页）走 ingest → diff → translate(mock)。

用法：python web/static/_devtest/realtest_driver.py <base_url>
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8902"
V1 = ROOT / "origin" / "BMS-Training-Manual.pdf"
V2 = ROOT / "data" / "derived" / "_realtest" / "v2_demo.pdf"


def call(method: str, path: str, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def wait_job(jid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, j = call("GET", f"/api/jobs/{jid}")
        if j and j.get("status") in ("done", "failed", "cancelled"):
            return j
        time.sleep(1.0)
    raise SystemExit(f"job {jid} 超时")


def main() -> int:
    st, health = call("GET", "/api/health")
    print("health:", st, json.dumps(health, ensure_ascii=False))

    for label, pdf in (("v1", V1), ("v2", V2)):
        t0 = time.time()
        st, r = call("POST", "/api/documents", {"pdf_path": str(pdf), "slug": "bms-real",
                                                "title": "BMS Training Manual (真实前20页)",
                                                "label": label, "note": f"realtest {label}",
                                                "pages": "1-20"})
        print(f"ingest {label}: HTTP {st} {json.dumps(r, ensure_ascii=False)[:220]} ({time.time()-t0:.1f}s)")
        if not r or not r.get("doc_id"):
            if r and r.get("job_id"):
                j = wait_job(r["job_id"])
                print("  job:", j.get("status"), json.dumps(j.get("result"), ensure_ascii=False)[:220])
                r = j.get("result") or {}
            else:
                return 1
        if label == "v2":
            doc_id, vid2 = r["doc_id"], r["version_id"]
        else:
            doc_id, vid1 = r["doc_id"], r["version_id"]

    st, versions = call("GET", f"/api/documents/{doc_id}/versions")
    print("versions:", st, [(v["version_id"], v["label"], v["page_count"]) for v in versions])

    st, d = call("GET", f"/api/documents/{doc_id}/diff?from={vid1}&to={vid2}")
    print("diff:", st, json.dumps(d.get("counts"), ensure_ascii=False))
    kinds = {}
    for c in d.get("changes", []):
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print("changes:", len(d.get("changes", [])), kinds)

    st, segs = call("GET", f"/api/documents/{doc_id}/versions/{vid2}/segments?page=1")
    print("page1 segments:", st, len(segs), [s["kind"] for s in segs][:8])
    st, mk = call("GET", f"/api/documents/{doc_id}/versions/{vid2}/pages/1/markers")
    print("markers p1:", st, len(mk["items"]), "items, size", mk["width"], "x", mk["height"])
    tagged = [(i["seg_id"], i["change_kind"]) for i in mk["items"] if i.get("change_kind")]
    print("markers with change_kind:", tagged)

    t0 = time.time()
    st, r = call("POST", "/api/translate", {"doc_id": doc_id, "version_id": vid2, "segment_ids": None,
                                            "page_from": 1, "page_to": 4, "provider": "mock"})
    print("translate:", st, r, f"({time.time()-t0:.1f}s)")
    j = wait_job(r["job_id"])
    print("translate job:", j["status"], json.dumps(j.get("result"), ensure_ascii=False))

    st, ly = call("GET", f"/api/documents/{doc_id}/versions/{vid2}/pages/1/translated-layout")
    print("layout p1:", st, "boxes:", len(ly.get("boxes", [])),
          "lines:", sum(len(b["lines"]) for b in ly.get("boxes", [])))
    t0 = time.time()
    for pg, kind in ((1, "source"), (1, "cn"), (1, "bilingual")):
        req = urllib.request.Request(
            f"{BASE}/api/documents/{doc_id}/versions/{vid2}/pages/{pg}/image?kind={kind}&dpi=110")
        with urllib.request.urlopen(req, timeout=180) as r2:
            blob = r2.read()
            ctype = r2.headers.get("content-type")
        print(f"page image p{pg} kind={kind}: HTTP 200 {ctype} {len(blob)} bytes ({time.time()-t0:.1f}s)")
        t0 = time.time()

    print(json.dumps({"doc_id": doc_id, "vid1": vid1, "vid2": vid2,
                      "url": f"{BASE}/?doc={doc_id}&vid={vid2}"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
