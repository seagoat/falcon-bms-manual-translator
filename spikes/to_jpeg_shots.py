"""Lead: convert README screenshots to JPEG q=86 to keep the public repo light."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image  # noqa: E402

DIR = "docs/screenshots"
total_before = total_after = 0
for name in sorted(os.listdir(DIR)):
    if not name.lower().endswith(".png"):
        continue
    src = os.path.join(DIR, name)
    dst = os.path.join(DIR, name[:-4] + ".jpg")
    before = os.path.getsize(src)
    total_before += before
    im = Image.open(src).convert("RGB")
    im.save(dst, "JPEG", quality=86, optimize=True, progressive=True)
    after = os.path.getsize(dst)
    total_after += after
    print(f"  {os.path.basename(dst):38s} {before/1024:7.0f} KB -> {after/1024:7.0f} KB "
          f"({after/before*100:4.0f}%)")
    os.remove(src)

print(f"\n合计 {total_before/1024/1024:.2f} MB -> {total_after/1024/1024:.2f} MB")
