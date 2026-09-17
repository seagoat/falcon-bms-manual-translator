"""Lead: re-check the API payload after the server restart."""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

with urllib.request.urlopen(
        "http://127.0.0.1:8777/api/documents/1/versions/2/pages/4/translated-layout",
        timeout=120) as r:
    lay = json.load(r)
print(f"boxes={len(lay.get('boxes', []))}")
n = 0
for b in lay.get("boxes", []):
    for ln in b.get("lines", []):
        t = ln.get("text", "")
        if "...." in t:
            print(f"  seg={b.get('seg_id')} x={ln['x']:.1f} w={ln['width']:.1f} "
                  f"末端={ln['x'] + ln['width']:.1f}")
            print(f"      {t[-38:]!r}")
            n += 1
            break
    if n >= 6:
        break
