"""Lead: verify the running Web API serves all three merged documents."""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8777"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.load(r)


docs = get("/api/documents")
print(f"=== /api/documents（{len(docs)} 份）===")
for d in docs:
    print(f"  doc_id={d['doc_id']} slug={d.get('slug'):22s} pages={d.get('page_count')} "
          f"versions={d.get('version_count')} current={d.get('current_version_id')}")

print("\n=== 每份文档：版本 + 第 21 页段落数 + 译文抽查 ===")
for d in docs:
    did = d["doc_id"]
    vs = get(f"/api/documents/{did}/versions")
    for v in vs:
        vid = v["version_id"]
        try:
            segs = get(f"/api/documents/{did}/versions/{vid}/segments?page=21")
        except Exception as e:
            print(f"  doc{did} v{vid}: segments ERR {e}")
            continue
        tr = [s for s in segs if s.get("translation")]
        body = [s for s in segs if s.get("role") == "body"]
        sample = next((s for s in body if s.get("translation")), None)
        print(f"  doc{did} v{v['version_no']} ({d.get('slug')}): p21 段={len(segs)} "
              f"body={len(body)} 有译文={len(tr)}")
        if sample:
            print(f"        EN={sample['text'][:52]!r}")
            print(f"        ZH={sample['translation']['text'][:52]!r}")

print("\n=== 每份文档：markers（热区）可用性 ===")
for d in docs:
    did = d["doc_id"]
    vid = d.get("current_version_id")
    if not vid:
        continue
    try:
        m = get(f"/api/documents/{did}/versions/{vid}/pages/21/markers")
        print(f"  doc{did}: markers items={len(m.get('items', []))} "
              f"page={m.get('page')} units_per_px={m.get('units_per_px')}")
    except Exception as e:
        print(f"  doc{did}: markers ERR {e}")
