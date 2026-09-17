"""Lead: test the TO-34 page images the UI left blank."""
import hashlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8777"
for kind in ("source", "cn"):
    for pno in (351, 360, 361, 21):
        url = (f"{BASE}/api/documents/2/versions/3/pages/{pno}/image"
               f"?kind={kind}&dpi=110&rev=0")
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            sig = hashlib.sha256(data).hexdigest()[:12]
            cache = r.headers.get("X-MT-Cache", "-")
            print(f"  doc2 p{pno:4d} {kind:7s}: {r.status} {len(data):8d} B "
                  f"sha={sig} cache={cache}")
        except urllib.error.HTTPError as e:
            body = e.read()[:160].decode("utf-8", "replace")
            print(f"  doc2 p{pno:4d} {kind:7s}: HTTP {e.code}  {body}")
        except Exception as e:
            print(f"  doc2 p{pno:4d} {kind:7s}: ERR {type(e).__name__} {str(e)[:90]}")
