"""Lead: verify normalize() collapses Unicode punctuation variants that appear in
re-generated PDFs (U+2010 HYPHEN, U+2011 NB-HYPHEN, U+2012..U+2015, NBSP, ...)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import fingerprint as fp  # noqa: E402

CASES = [
    ("F-16", "F\u201016"),          # HYPHEN
    ("F-16", "F\u201116"),          # NON-BREAKING HYPHEN
    ("F-16", "F\u201316"),          # FIGURE DASH
    ("a - b", "a \u2013 b"),        # EN DASH
    ("a - b", "a \u2014 b"),        # EM DASH
    ("a b", "a\u00a0b"),            # NBSP
    ("a b", "a\u2009b"),            # THIN SPACE
    ("quote", "\u201cquote\u201d"), # curly quotes around
    ("...", "\u2026"),              # ellipsis
    ("\uff26\uff31", "Fq"),         # fullwidth -> NFKC
    ("F-16 \u00b0 45", "F\u201016 ° 45"),
    ("MASTER CAUTION", "Master Caution"),
    ('say "ALTITUDE" now', "say \u201cALTITUDE\u201d now"),
    ("it's fine", "it\u2019s fine"),
    ('"a" and "b"', "\u201ca\u201d and \u201cb\u201d"),
    ("40 < 50 > 30", "40 \u2039 50 \u203a 30"),
]
bad = 0
for a, b in CASES:
    na, nb = fp.normalize(a), fp.normalize(b)
    fa, fb = fp.fingerprint(a), fp.fingerprint(b)
    same = fa == fb
    mark = "OK " if same else "MISS"
    if not same:
        bad += 1
    print(f"{mark} {a!r:28s} vs {b!r:26s} -> norm {na!r} / {nb!r}  fp_eq={same}")

print(f"\ndistinct-variant failures: {bad}")

# The real v1/v2 strings from the database
V1 = "You will fly a series of 3 training missions around Gunsan airbase in Korea in a F-16DM block 52."
V2 = "You will fly a series of 3 training missions around Gunsan airbase in Korea in a F\u201016DM block 52."
print(f"\nreal v1/v2 sentence: fp_eq={fp.fingerprint(V1) == fp.fingerprint(V2)} "
      f"ratio={fp.ratio(V1, V2):.4f}")
V1b = "Please note: you need to select the F-16 flight in the left window of the and take the seat with a left click on the aircraft symbol"
V2b = "Please note: you need to select the F\u201016 flight in the left window of the and take the seat with a left click on the airframe symbol"
print(f"real v1/v2 modified: ratio={fp.ratio(V1b, V2b):.4f}")
sys.exit(1 if bad else 0)
