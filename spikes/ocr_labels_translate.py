"""Lead: translate OCR'd in-image labels for the sample pages.

Reads data/derived/ocr/*.json (produced by ocr_spike.py), translates the labels via
the project's own DeepSeek engine (glossary-aware, cached), and writes
*_zh.json with {pixel bbox, en, zh, score} ready for the annotation renderer.

Usage:  python spikes/ocr_labels_translate.py 11 [12 ...]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402
from translate import engine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_DIR = os.path.join(ROOT, "data", "derived", "ocr")

# Words that are already international aviation shorthand: keep as-is.
KEEP_AS_IS = re.compile(
    r"^(?:AOA|VVI|RPM|FTIT|MFD|HUD|CMDS|HMCS|IFF|EPU|ELEC|ECM|AVTR|FLCS|PFLD|TWP|TWA|MPO|"
    r"ADI|PFD|HUD|KY58|0?2\s*PANEL|UHF|MISC|GEAR|FUEL|TEST|LEFT|RIGHT|CONSOLE|OIL|NOZZLE|"
    r"BACKUP|NUCLEAR|SENSOR|PANEL|CLOCK|COMPASS|AUDIO\d?|AIRCOND|MANUALTRIM|FUELQTY)$",
    re.I)

# Labels we do not want to translate (artefacts / notes that are not part labels)
SKIP_PATTERNS = [
    re.compile(r"^\(\s*N/?I\s+in\s+BMS\s*\)$", re.I),   # "(N/I in BMS)" note
    re.compile(r"^[*\-–—.\s]+$"),
]


# OCR (and the original artwork) run ALL-CAPS words together, e.g. "ALTGEAR",
# "MASTERCAUTIONLIGHT", "HUDWARN". Split them against a vocabulary of the words that
# actually occur in this manual's cockpit labels so they can be translated.
_VOCAB = [
    # longest-first so "LEFT" beats "LE", etc.
    "MASTER", "CAUTION", "WARNING", "INDICATOR", "REMAINING", "PRESSURE", "CONSOLE",
    "SENSOR", "INT", "SWITCH", "PANEL", "LIGHTS", "LIGHT", "EYEBROW", "INDEXER", "BACKUP",
    "AUDIO", "ENGINE", "START", "ALT", "INSTRUMENT", "MODE", "SPEED", "BRAKE", "CABIN",
    "CLOCK", "COMPASS", "NUCLEAR", "MANUAL", "TRIM", "EXTERNAL", "AVIONIC", "POWER",
    "FUEL", "FLOW", "GEAR", "LEFT", "RIGHT", "UHF", "HUD", "MFD", "AOA", "VVI", "RPM",
    "FTIT", "OIL", "NOZZLE", "MISC", "IFF", "EPU", "ELEC", "ECM", "AVTR", "FLCS", "PFLD",
    "TWP", "TWA", "MPO", "KY", "ANTI", "ICE", "DED", "ICP", "RWR", "ILI", "TEST", "CENTER",
    "QTY", "HYD", "COURSE", "STORES", "ADI", "NM", "EXT", "SET",
]
_VOCAB_SORTED = sorted(set(_VOCAB), key=len, reverse=True)


def _split_caps(word: str) -> list:
    """Greedy longest-match segmentation of an ALL-CAPS concatenation."""
    if not word.isupper() or len(word) < 5:
        return [word]
    out, i = [], 0
    while i < len(word):
        for v in _VOCAB_SORTED:
            if word.startswith(v, i):
                out.append(v)
                i += len(v)
                break
        else:
            # unknown remainder: keep it glued to the previous token
            if out:
                out[-1] += word[i]
            else:
                out.append(word[i])
            i += 1
    return out if len(out) > 1 else [word]


def norm_label(t: str) -> str:
    """Fix the common OCR artefact: ALLCAPS words glued together."""
    t = (t or "").strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"([A-Za-z])(\d)", r"\1 \2", t)
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    if t and t.isupper() and " " not in t:
        t = " ".join(_split_caps(t))
    return t


def main() -> int:
    pages = [int(x) for x in sys.argv[1:]] or [11]
    con = cdb.connect()

    for pno in pages:
        src = os.path.join(OCR_DIR, f"p{pno}_ocr.json")
        if not os.path.exists(src):
            print(f"p{pno}: no OCR file at {src} — run ocr_spike.py first")
            continue
        data = json.load(open(src, encoding="utf-8"))
        texts = data["texts"]
        print(f"\n=== page {pno}: {len(texts)} OCR boxes ===")

        keep = []
        for t in texts:
            lab = norm_label(t["text"])
            if any(p.search(lab) for p in SKIP_PATTERNS):
                continue
            if len(lab) < 2:
                continue
            keep.append({**t, "label": lab})

        need = [k for k in keep if not KEEP_AS_IS.match(k["label"])]
        todo = []
        seen = set()
        for k in need:
            if k["label"].lower() in seen:
                continue
            seen.add(k["label"].lower())
            todo.append(k)
        print(f"  labels kept={len(keep)}  to translate={len(todo)}  "
              f"kept-as-is={len(keep) - len(need)}")

        # build pseudo "segments" for the engine: id = index in todo
        class Seg:
            __slots__ = ("id", "text", "role", "kind", "page", "order_index", "bbox", "style")

        segs = []
        for i, k in enumerate(todo):
            s = Seg()
            s.id = 900000 + pno * 1000 + i
            s.text = k["label"]
            s.role = "body"
            s.kind = "paragraph"
            s.page = pno
            s.order_index = i
            s.bbox = [0, 0, 0, 0]
            s.style = {"size": 10.0, "bold": False}
            segs.append(s)

        # translate them directly through the engine's batched path
        import config
        cfg = dict(config.load())
        cfg["batch_segments"] = 12
        gl = engine.glossary.prompt_block()
        got = {}
        if segs:
            items = [engine._Item(s.id, s.text, s.page, s.role, s.kind) for s in segs]
            batches = [items[i:i + 12] for i in range(0, len(items), 12)]
            for bi, batch in enumerate(batches):
                r = engine._run_batch(batch, {}, provider="deepseek",
                                      model=cfg.get("model", "deepseek-flash"),
                                      cfg=cfg, api_key=cfg.get("deepseek_api_key", ""),
                                      glossary_block=gl)
                if r.get("ok"):
                    got.update(r["out"])
                else:
                    print(f"    batch {bi} failed: {r.get('error')}")
        print(f"  translated: {len(got)}/{len(segs)}")

        out = []
        for i, k in enumerate(todo):
            zh = got.get(segs[i].id)
            if not zh:
                zh = k["label"]          # fall back to English
            out.append({**k, "zh": zh})

        path = os.path.join(OCR_DIR, f"p{pno}_zh.json")
        json.dump({"page": pno, "dpi": data["dpi"], "image_bbox": data["image_bbox"],
                   "labels": out}, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"  -> {path}")
        for o in out[:45]:
            print(f"     [{o['score']:.2f}] {o['label']:26s} -> {o['zh']}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
