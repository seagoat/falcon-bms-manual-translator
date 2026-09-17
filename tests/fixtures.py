"""合成测试 PDF（CONTRACT.md §1 的 tests/fixtures.py）。

用 pymupdf 直接生成，不依赖外部文件；结构模拟真实手册：
首页封面（标题 + 全宽 banner 图 + 页眉页脚）、正文页（页眉/页脚/标题/正文段落/
编号列表/项目符号/表格/内联小图），文字全部可被 `get_text()` 还原。

`variant="v2"` 造一份"改过一处 + 多一条列表项 + 换页"的第二版，供版本 diff 测试。
"""
from __future__ import annotations

import io
import os
from typing import Optional

import pymupdf
from PIL import Image

__all__ = [
    "PAGE_W",
    "PAGE_H",
    "TITLE",
    "SUBTITLE",
    "HEADER_TEXT",
    "FOOTER_TEXT",
    "HEADINGS",
    "BODY_PARAGRAPHS",
    "BODY_PARAGRAPHS_V2",
    "LIST_ITEMS",
    "BULLET_MARKER",
    "BULLET_ITEM",
    "TABLE_CELLS",
    "HYPHEN_FIRST_LINE",
    "HYPHEN_SECOND_LINE",
    "make_synthetic_pdf",
    "make_synthetic_pair",
]

PAGE_W = 595.0
PAGE_H = 842.0

TITLE = "Synthetic Flight Manual"
SUBTITLE = "Release 1.0"
HEADER_TEXT = "SYNTHETIC MANUAL"
FOOTER_TEXT = "SYNTHETIC"

HEADINGS = [
    "1.1 Systems Overview",
    "1.2 Electrical Power",
    "1.3 Hydraulic System",
    "1.4 Flight Controls",
    "1.5 Fuel System",
]

BODY_PARAGRAPHS = [
    "The MASTER CAUTION light activates shortly after any individual light on the "
    "Caution Panel illuminates. It does not activate simultaneously with the warning lights.",
    "The FLCS provides electrical power to the flight control surfaces through the "
    "redundant channels and monitors the hydraulic pressure of each branch.",
    "Press the STPT button on the MFD to select the current steerpoint before "
    "engaging the autopilot in the navigation mode.",
    "Fuel is transferred from the external tanks to the internal tanks by the "
    "fuel transfer pumps when the fuel quantity drops below the planned value.",
    "The OBOGS supplies breathing air to the pilot during all phases of flight "
    "and requires the engine bleed air to be available.",
]

# v2 只改第一段（插一个词）+ 其余保持一致，diff 应得到 1 modified
BODY_PARAGRAPHS_V2 = [
    "The MASTER CAUTION light activates shortly after any individual light on the "
    "Caution Panel illuminates brightly. It does not activate simultaneously with the warning lights.",
    *BODY_PARAGRAPHS[1:],
]

LIST_ITEMS = [
    "1. First checklist step for the electrical power system.",
    "2. Second checklist step for the electrical power system.",
    "3. Third checklist step for the electrical power system.",
]
LIST_ITEMS_V2 = [
    *LIST_ITEMS,
    "4. Fourth checklist step added in the second release.",
]

BULLET_MARKER = "-"
BULLET_ITEM = "bullet item text on the following line"

# 表格单元（同一基线上分两栏，间隙 > 40pt → paragraph_kind 判 table_cell）
TABLE_CELLS = ["Item", "Value", "FLCS", "ON", "MFD", "STPT"]

# §3.1.1 的真实形态：行尾软连字符 Cau- / tion
HYPHEN_FIRST_LINE = "The MASTER CAUTION light activates shortly after the Cau-"
HYPHEN_SECOND_LINE = "tion Panel illuminates brightly."


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _draw_header_footer(page: pymupdf.Page, page_no: int) -> None:
    page.insert_text((36, 30), f"{HEADER_TEXT} ", fontsize=8, fontname="helv")
    page.insert_text((36, 40), SUBTITLE, fontsize=8, fontname="helv")
    page.insert_text((36, 812), f"{FOOTER_TEXT} ", fontsize=8, fontname="helv")
    page.insert_text((300, 812), f"- {page_no} -", fontsize=9, fontname="helv")


def make_synthetic_pdf(path: str, pages: int = 3, *, variant: str = "v1") -> str:
    """生成合成 PDF，返回写入的路径（str）。

    * `pages`：总页数（>= 1）；首页为封面。
    * `variant`：`"v1"` 原始版；`"v2"` 改动第一段正文并多一条列表项（用于 diff/版本测试）。
    """
    total = max(1, int(pages))
    body_texts = BODY_PARAGRAPHS_V2 if variant == "v2" else BODY_PARAGRAPHS
    list_items = LIST_ITEMS_V2 if variant == "v2" else LIST_ITEMS
    shift = 1 if variant == "v2" else 0    # v2 正文整体下移，模拟排版变化

    out_dir = os.path.dirname(os.path.abspath(str(path)))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    doc = pymupdf.open()
    for index in range(total):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        _draw_header_footer(page, index + 1)
        # 页眉全宽 banner（贴顶、满宽、面积 > 2% → is_banner_image 为 True）
        page.insert_image(pymupdf.Rect(0.4, 0.0, PAGE_W - 0.4, 44.0),
                          stream=_png_bytes(600, 44, (30, 60, 120)))

        if index == 0:
            page.insert_text((96, 150), TITLE, fontsize=20, fontname="hebo")
            page.insert_text((96, 180), SUBTITLE, fontsize=12, fontname="helv")
            page.insert_text((96, 230), "Cover page of the synthetic test manual.",
                             fontsize=10, fontname="helv")
            # 居中小图：非满宽 → is_banner_image 为 False
            page.insert_image(pymupdf.Rect(250, 300, 345, 395),
                              stream=_png_bytes(120, 120, (200, 30, 30)))
            continue

        y = 90.0 + shift
        heading = HEADINGS[(index - 1) % len(HEADINGS)]
        page.insert_text((72, y), heading, fontsize=12, fontname="hebo")
        y += 22.0

        paragraph = body_texts[(index - 1) % len(body_texts)]
        page.insert_text((72, y), paragraph[:110], fontsize=10, fontname="helv")
        page.insert_text((72, y + 13), paragraph[110:], fontsize=10, fontname="helv")
        y += 40.0

        for item in list_items:
            page.insert_text((72, y), item, fontsize=10, fontname="helv")
            y += 13.0
        y += 8.0

        # 项目符号单独一行 + 下一行缩进的正文（§3.1.3 信号 2）
        page.insert_text((71.9, y), BULLET_MARKER, fontsize=10, fontname="helv")
        page.insert_text((89.9, y + 13), BULLET_ITEM, fontsize=10, fontname="helv")
        y += 34.0

        # 软连字符两行（§3.1.1）
        page.insert_text((72, y), HYPHEN_FIRST_LINE, fontsize=10, fontname="helv")
        page.insert_text((72, y + 13), HYPHEN_SECOND_LINE, fontsize=10, fontname="helv")
        y += 34.0

        # 表格：两栏 + 表格线
        page.draw_rect(pymupdf.Rect(72, y - 10, 380, y + 20), color=(0.2, 0.2, 0.2), width=0.6)
        page.draw_line(pymupdf.Point(150, y - 10), pymupdf.Point(150, y + 20), width=0.6)
        page.insert_text((80, y + 4), TABLE_CELLS[(index - 1) % 2], fontsize=9, fontname="helv")
        page.insert_text((240, y + 4), TABLE_CELLS[2 + (index - 1) % 2], fontsize=9, fontname="helv")
        y += 46.0

        # 内联小图（正文里不可丢失）
        page.insert_image(pymupdf.Rect(72, y, 152, y + 60),
                          stream=_png_bytes(160, 120, (40, 140, 60)))
        y += 76.0
        page.insert_text((72, y), f"Figure 1.{index} Detail of the {heading.split()[-1]}.",
                         fontsize=9, fontname="helv")

    doc.set_metadata({"title": TITLE, "author": "synthetic-fixture", "subject": SUBTITLE})
    doc.save(str(path))
    doc.close()
    return str(path)


def make_synthetic_pair(directory: str, pages: int = 3) -> tuple[str, str]:
    """生成 (v1, v2) 两份 PDF，供 versions/test_versions.py 直接使用。"""
    os.makedirs(str(directory), exist_ok=True)
    v1 = make_synthetic_pdf(os.path.join(str(directory), "synthetic_v1.pdf"), pages, variant="v1")
    v2 = make_synthetic_pdf(os.path.join(str(directory), "synthetic_v2.pdf"), pages, variant="v2")
    return v1, v2


def expected_text(page_no: int, variant: str = "v1") -> list[str]:
    """该页应当能被抽取到的关键文本（供断言用）。"""
    total_paragraphs = BODY_PARAGRAPHS_V2 if variant == "v2" else BODY_PARAGRAPHS
    if page_no <= 1:
        return [TITLE, SUBTITLE, "Cover page of the synthetic test manual.", HEADER_TEXT]
    body = total_paragraphs[(page_no - 2) % len(total_paragraphs)]
    return [
        HEADER_TEXT,
        HEADINGS[(page_no - 2) % len(HEADINGS)],
        body[:110],
        HYPHEN_FIRST_LINE,
        BULLET_ITEM,
        "- " + str(page_no) + " -",
    ]
