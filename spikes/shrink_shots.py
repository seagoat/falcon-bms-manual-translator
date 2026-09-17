"""Lead: downscale the curated screenshots so the repo stays light.

UI screenshots are wide (1600px+); 1400px max width is still plenty for a README
and cuts bytes a lot. Keeps PNG (text stays crisp).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image  # noqa: E402

DIR = "docs/screenshots"
MAXW = 1400
total_before = total_after = 0
for name in sorted(os.listdir(DIR)):
    if not name.lower().endswith(".png"):
        continue
    path = os.path.join(DIR, name)
    before = os.path.getsize(path)
    total_before += before
    im = Image.open(path)
    if im.width > MAXW:
        h = round(im.height * MAXW / im.width)
        im = im.resize((MAXW, h), Image.LANCZOS)
    tmp = path + ".tmp.png"
    im.save(tmp, "PNG", optimize=True)
    after = os.path.getsize(tmp)
    if after < before:
        os.replace(tmp, path)
    else:
        os.remove(tmp)
        after = before
    total_after += after
    print(f"  {name:38s} {im.width}x{im.height}  "
          f"{before/1024:7.0f} KB -> {after/1024:7.0f} KB")

print(f"\n合计 {total_before/1024/1024:.2f} MB -> {total_after/1024/1024:.2f} MB")
