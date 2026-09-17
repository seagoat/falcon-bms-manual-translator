"""Spike: check extraction quality issues that would break cross-version fingerprint matching.

Checks:
  1. hyphenated line-break words ("Cau-\ntion") -> must be re-joined
  2. ligatures (fi, fl) and soft hyphens
  3. how many segments would collide on fingerprint
  4. paragraph grouping sanity on real pages
"""
import collections
import re
import unicodedata

import pymupdf

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"


def main() -> None:
    doc = pymupdf.open(PDF)
    hyphen_tail = 0
    soft = 0
    lig = collections.Counter()
    sample_lines = []
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                txt = "".join(s["text"] for s in line["spans"])
                if txt.rstrip().endswith("-"):
                    hyphen_tail += 1
                    if len(sample_lines) < 12:
                        sample_lines.append((pno + 1, txt.strip()))
                if "\u00ad" in txt:
                    soft += 1
                for ch in txt:
                    if ch in "\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06":
                        lig[ch] += 1
    print("lines ending with '-':", hyphen_tail)
    print("lines containing soft hyphen:", soft)
    print("ligature chars:", dict(lig))
    print("--- samples of hyphen-ended lines")
    for p, t in sample_lines:
        print(f"  p{p}: {t!r}")

    # unicode oddities
    odd = collections.Counter()
    for pno in range(doc.page_count):
        t = doc[pno].get_text()
        for ch in t:
            if ord(ch) > 127 and not (0x4E00 <= ord(ch) <= 0x9FFF):
                odd[ch] += 1
    print("--- non-ascii chars")
    for ch, n in odd.most_common(30):
        print(f"  {ch!r} U+{ord(ch):04X} {unicodedata.name(ch, '?')} x{n}")

    # punctuation style: which quote char
    allt = "".join(doc[p].get_text() for p in range(0, 60))
    for ch in ['"', "'", "\u2018", "\u2019", "\u201c", "\u201d"]:
        print(f"  quote {ch!r} U+{ord(ch):04X}: {allt.count(ch)}")


if __name__ == "__main__":
    main()
