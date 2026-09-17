"""Generate offline mock data for the comparison UI (`?mock=1`).

Run:  python web/static/mock/generate_mock.py
Writes: web/static/mock/data.json + web/static/mock/pages/*.webp

This lets the whole front-end (dual-pane linkage, change bars, canvas layout)
be developed and screenshot-tested before the real backend exists.  The JSON
shapes mirror CONTRACT.md §5 exactly; the mock adapter in api.js only filters
these files the way the server would.
"""
from __future__ import annotations

import json
import os
import sys

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES_DIR = os.path.join(HERE, "pages")
DPI = 110
LATIN = "helv"
CJK_PATH = "C:/Windows/Fonts/Deng.ttf"
PW, PH = 595.28, 841.89          # A4 in points

# --------------------------------------------------------------------------
# segment catalogue  (y = first baseline, x = left edge, size = font size)
# --------------------------------------------------------------------------
P1, P2, P3 = 1, 2, 3
SEGS = [
    dict(seg_id=101, page=P1, kind="header", role="header", x=72, y=46, size=9, maxw=451,
         text="TO 1F-16CMAM-1 BMS", trans="TO 1F-16CMAM-1 BMS", status="carried"),
    dict(seg_id=102, page=P1, kind="heading", role="body", x=72, y=104, size=13, maxw=451,
         text="CHAPTER 1  AIRCRAFT GENERAL", trans="第 1 章  飞机概述", status="machine"),
    dict(seg_id=103, page=P1, kind="paragraph", role="body", x=72, y=140, size=10, maxw=451,
         text=("The F-16 is a single-engine, multirole fighter aircraft. The flight control system is a "
               "full-authority fly-by-wire system with no mechanical backup. It provides artificial "
               "stability and limits the aircraft within its structural and aerodynamic envelope."),
         trans="F-16 是单发多用途战斗机。飞控系统为全权限电传操纵系统，无机械备份。它提供人工稳定性，并将飞机限制在结构和气动包线之内。",
         status="machine"),
    dict(seg_id=104, page=P1, kind="paragraph", role="body", x=72, y=215, size=10, maxw=451,
         text=("The pilot controls the aircraft through the sidestick controller and rudder pedals. The "
               "FLCS commands the horizontal stabilators, flaperons and rudder to achieve the "
               "commanded response."),
         trans="飞行员通过侧杆控制器和脚舵操纵飞机。FLCS 指令控制平尾、襟副翼和方向舵，以实现所指令的响应。",
         status="machine"),
    dict(seg_id=105, page=P1, kind="page_num", role="footer", x=290, y=806, size=9, maxw=60,
         text="1-1", trans="1-1", status="carried"),

    dict(seg_id=201, page=P2, kind="heading", role="body", x=72, y=104, size=13, maxw=451,
         text="1.2  FLIGHT CONTROL SYSTEM", trans="1.2  飞控系统", status="reviewed"),
    dict(seg_id=202, page=P2, kind="paragraph", role="body", x=72, y=140, size=10, maxw=451,
         text=("The FLCS is a four-channel digital system. Each channel processes the same inputs and "
               "compares its output with the other channels before the actuator commands are issued."),
         trans="FLCS 为四余度数字系统。每个通道处理相同的输入，并在发出作动器指令之前将其输出与其他通道进行比较。",
         status="machine"),
    dict(seg_id=203, page=P2, kind="list_item", role="body", x=90, y=196, size=10, maxw=433,
         text="\u2022 Sidestick controller \u2014 pitch and roll commands",
         trans="\u2022 侧杆控制器 \u2014 俯仰和滚转指令", status="machine"),
    dict(seg_id=204, page=P2, kind="list_item", role="body", x=90, y=212, size=10, maxw=433,
         text="\u2022 Rudder pedals \u2014 yaw commands",
         trans="\u2022 脚舵 \u2014 偏航指令", status="machine"),
    dict(seg_id=205, page=P2, kind="list_item", role="body", x=90, y=228, size=10, maxw=433,
         text="\u2022 Throttle \u2014 engine thrust and afterburner selection",
         trans="\u2022 油门 \u2014 发动机推力和加力选择", status="machine"),
    dict(seg_id=206, page=P2, kind="table_cell", role="body", x=110, y=272, size=10, maxw=200,
         text="FLCS  BIT  \u2014  NO GO", trans="FLCS BIT \u2014 不工作", status="machine"),
    dict(seg_id=207, page=P2, kind="paragraph", role="body", x=72, y=320, size=10, maxw=451,
         text=("The OBOGS provides breathing oxygen to the pilot during all phases of flight."),
         trans="OBOGS 在飞行的所有阶段为飞行员提供呼吸用氧。", status="machine"),
    dict(seg_id=208, page=P2, kind="page_num", role="footer", x=290, y=806, size=9, maxw=60,
         text="1-2", trans="1-2", status="carried"),

    dict(seg_id=301, page=P3, kind="heading", role="body", x=72, y=104, size=13, maxw=451,
         text="1.3  ENGINE AND FUEL SYSTEM", trans="1.3  发动机和燃油系统", status="machine"),
    dict(seg_id=302, page=P3, kind="paragraph", role="body", x=72, y=140, size=10, maxw=451,
         text=("The engine is controlled by the digital engine control unit, which schedules fuel flow, "
               "nozzle position and afterburner operation. The EPU provides emergency electrical and "
               "hydraulic power."),
         trans="发动机由数字发动机控制单元控制，该单元安排燃油流量、喷口位置和加力工作。EPU 提供应急电源和液压动力。",
         status="seeded"),
    dict(seg_id=303, page=P3, kind="paragraph", role="body", x=72, y=215, size=10, maxw=451,
         text=("The fuel system stores JP-8 in the fuselage and wing tanks and supplies the engine "
               "through two boost pumps."),
         trans="燃油系统在机身和机翼油箱中储存 JP-8，并通过两台增压泵向发动机供油。",
         status="machine"),
    dict(seg_id=304, page=P3, kind="paragraph", role="body", x=72, y=268, size=10, maxw=451,
         text="Fuel quantity is displayed on the fuel quantity indicator and on the MFD.",
         trans=None, status="missing"),
    dict(seg_id=305, page=P3, kind="page_num", role="footer", x=290, y=806, size=9, maxw=60,
         text="1-3", trans="1-3", status="carried"),
]

# ---- change metadata for the current version (v2) -------------------------
CHANGE_OF = {302: "modified", 303: "added", 207: "moved"}
CHANGE_ID_OF = {302: 2, 303: 3, 207: 4}
# removed paragraph: existed in v1 page 2, gone in v2 (ghost block in the UI)
REMOVED_V1 = dict(seg_id=209, page=P2, order_index=8, kind="paragraph", role="body",
                  x=72, y=368, size=10, maxw=451,
                  text="The emergency power unit is armed automatically after takeoff.",
                  trans="应急动力装置在起飞后自动预位。", status="machine")
V1_302 = ("The engine is controlled by the digital engine control unit, which schedules fuel flow. "
          "The EPU provides emergency power.")
V1_207_PAGE = P1
V1_207_Y = 282


def wrap_latin(text, size, maxw):
    font = pymupdf.Font(LATIN)
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.text_length(trial, size) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def wrap_cjk(text, size, maxw):
    font = pymupdf.Font(fontfile=CJK_PATH)
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if font.text_length(cur + ch, size) <= maxw or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def measure(lines, text_size, cjk):
    if cjk:
        font = pymupdf.Font(fontfile=CJK_PATH)
    else:
        font = pymupdf.Font(LATIN)
    return [round(font.text_length(t, text_size), 2) for t in lines]


def build():
    os.makedirs(PAGES_DIR, exist_ok=True)
    doc = pymupdf.open()
    page_by_no = {}
    removeds = {}
    for pno in (P1, P2, P3):
        page_by_no[pno] = doc.new_page(width=PW, height=PH)

    def draw_page(start_page, erase_body=False):
        """Draw the whole mock document; if erase_body, blank body text (simulates rebuild)."""
        for pg in doc:
            pg.clean_contents()
        for s in SEGS:
            pg = doc[s["page"] - 1]
            if erase_body and s["role"] == "body":
                continue
            for i, line in enumerate(s["_lines"]):
                y = s["y"] + i * s["_lh"]
                pg.insert_text((line["x"], y), line["text"], fontname=LATIN, fontsize=s["size"],
                               color=(0.1, 0.1, 0.1))
        if erase_body:
            for s in SEGS:
                if s["role"] != "body":
                    continue
                pg = doc[s["page"] - 1]
                b = pymupdf.Rect(s["bbox"])
                pg.draw_rect(b, color=None, fill=(1, 1, 1), overlay=True)

    # ---- geometry per segment (computed once, reused for both renders) ----
    for s in SEGS:
        lines = wrap_latin(s["text"], s["size"], s["maxw"])
        lh = round(s["size"] * 1.24, 2)
        widths = measure(lines, s["size"], False)
        line_boxes, items = [], []
        for i, (t, w) in enumerate(zip(lines, widths)):
            y = s["y"] + i * lh
            line_boxes.append([round(s["x"], 2), round(y - s["size"] * 0.8, 2),
                               round(s["x"] + w, 2), round(y + s["size"] * 0.25, 2)])
            items.append(dict(text=t, x=s["x"], y=y, size=s["size"], font_slot="latin", width=w))
        s["_lines"] = items
        s["_lh"] = lh
        s["line_boxes"] = line_boxes
        s["bbox"] = [round(min(b[0] for b in line_boxes), 2),
                     round(min(b[1] for b in line_boxes), 2),
                     round(max(b[2] for b in line_boxes), 2),
                     round(max(b[3] for b in line_boxes), 2)]
        s["_tlines"] = []
        if s["trans"]:
            tsize = round(s["size"] * 0.92, 2)
            tlh = round(tsize * 1.24, 2)
            tlines = wrap_cjk(s["trans"], tsize, s["maxw"])
            tw = measure(tlines, tsize, True)
            origin = s["line_boxes"][0][3] - s["size"] * 0.25
            for i, (t, w) in enumerate(zip(tlines, tw)):
                s["_tlines"].append(dict(text=t, x=round(s["x"], 2),
                                         y=round(origin + i * tlh, 2), size=tsize,
                                         font_slot="cjk" if any("\u4e00" <= c <= "\u9fff" for c in t)
                                         else "latin", width=w))

    # ---- images -----------------------------------------------------------
    img_paths = {}
    for variant, erase in (("source", False), ("cn", True)):
        draw_page(None, erase_body=erase)
        for pno in (P1, P2, P3):
            name = f"v2_p{pno}_{variant}"
            out = os.path.join(PAGES_DIR, name + ".webp")
            pix = doc[pno - 1].get_pixmap(dpi=DPI, alpha=False)
            try:
                pix.pil_save(out, format="WEBP", quality=88, method=4)
            except Exception:                                    # pragma: no cover
                out = os.path.join(PAGES_DIR, name + ".png")
                pix.save(out)
            img_paths.setdefault(pno, {})[variant] = "./mock/pages/" + os.path.basename(out)

    # ---- JSON assembly ----------------------------------------------------
    def seg_dict(s, page=None):
        d = dict(seg_id=s["seg_id"], page=page if page is not None else s["page"],
                 order_index=s["_order"], kind=s["kind"], bbox=s["bbox"],
                 line_boxes=s["line_boxes"], text=s["text"], role=s["role"],
                 style=dict(bold=s["kind"] == "heading", italic=False, size=s["size"],
                            color=0x1A1A1A, align="left", list_level=1 if s["kind"] == "list_item" else 0,
                            indent=0),
                 translation=(None if s["trans"] is None else
                              dict(text=s["trans"], status=s["status"], revision=1 if s["status"] != "missing" else 0)))
        if s["kind"] in ("header", "page_num"):
            d["translation"] = dict(text=s["trans"] or s["text"], status="carried", revision=0)
        return d

    # order_index across the version
    order = 0
    for s in SEGS:
        order += 1
        s["_order"] = order

    v2_segments = [seg_dict(s) for s in SEGS]
    # v1 segments: 207 on page 1, 302 shorter text, no 303, plus removed 209
    v1 = []
    for s in SEGS:
        if s["seg_id"] == 303:
            continue
        if s["seg_id"] == 207:
            v1.append(seg_dict(s, page=V1_207_PAGE))
            continue
        if s["seg_id"] == 302:
            c = dict(s)
            c["text"] = V1_302
            lines = wrap_latin(V1_302, s["size"], s["maxw"])
            lh = s["_lh"]
            w = measure(lines, s["size"], False)
            lb = []
            for i, (t, ww) in enumerate(zip(lines, w)):
                y = s["y"] + i * lh
                lb.append([round(s["x"], 2), round(y - s["size"] * 0.8, 2), round(s["x"] + ww, 2),
                           round(y + s["size"] * 0.25, 2)])
            c["line_boxes"] = lb
            c["bbox"] = [round(min(b[0] for b in lb), 2), round(min(b[1] for b in lb), 2),
                         round(max(b[2] for b in lb), 2), round(max(b[3] for b in lb), 2)]
            c["trans"] = "发动机由数字发动机控制单元控制，该单元安排燃油流量。EPU 提供应急电源。"
            c["status"] = "machine"
            v1.append(seg_dict(c))
            continue
        v1.append(seg_dict(s))
    r = dict(REMOVED_V1)
    lines = wrap_latin(r["text"], r["size"], r["maxw"])
    lb, items = [], []
    for i, t in enumerate(lines):
        y = r["y"] + i * round(r["size"] * 1.24, 2)
        w = pymupdf.Font(LATIN).text_length(t, r["size"])
        lb.append([float(r["x"]), round(y - r["size"] * 0.8, 2), round(r["x"] + w, 2),
                   round(y + r["size"] * 0.25, 2)])
        items.append(dict(text=t, x=r["x"], y=y, size=r["size"], font_slot="latin", width=round(w, 2)))
    r["line_boxes"] = lb
    r["bbox"] = [min(b[0] for b in lb), min(b[1] for b in lb), max(b[2] for b in lb), max(b[3] for b in lb)]
    r["_order"] = 9
    for s in v1:
        if s["page"] == P2 and s["order_index"] > 8:
            s["order_index"] += 1
    v1.append(seg_dict(r))
    v1.sort(key=lambda d: (d["page"], d["order_index"]))

    markers = {}
    layout = {}
    for pno in (P1, P2, P3):
        items = []
        for s in SEGS:
            if s["page"] != pno:
                continue
            items.append(dict(seg_id=s["seg_id"], kind=s["kind"],
                              kind_group="structural" if s["kind"] in ("header", "page_num", "heading") else "text",
                              bbox=s["bbox"], line_boxes=s["line_boxes"], role=s["role"],
                              change_kind=CHANGE_OF.get(s["seg_id"]), change_id=CHANGE_ID_OF.get(s["seg_id"])))
        markers[f"2:{pno}"] = dict(page=pno, width=PW, height=PH, dpi=DPI,
                                   units_per_px=round(72.0 / DPI, 6), items=items,
                                   crop_box=[0.0, 0.0, PW, PH])
        boxes = []
        for s in SEGS:
            if s["page"] != pno or not s["_tlines"] or s["role"] != "body":
                continue
            boxes.append(dict(seg_id=s["seg_id"], bbox=s["bbox"], paragraph_rect=s["bbox"],
                              lines=s["_tlines"], color=0x1A1A1A, bg=0xFFFFFF, shrink=0))
        layout[f"2:{pno}"] = dict(page=pno, width=PW, height=PH, font_file=CJK_PATH, boxes=boxes)

    changes = []
    v1_by_id = {d["seg_id"]: d for d in v1}
    v2_by_id = {d["seg_id"]: d for d in v2_segments}
    for cid, sid, kind, ratio, summary in ((2, 302, "modified", 0.62, "修改 1 句，新增喷口/加力描述"),
                                           (3, 303, "added", 0.0, "新版新增段落"),
                                           (4, 207, "moved", 1.0, "同文移至第 1-2 页")):
        old = v1_by_id.get(sid)
        changes.append(dict(change_id=cid, kind=kind, ratio=ratio,
                            old_page=(old or {}).get("page"), new_page=v2_by_id[sid]["page"],
                            old_text=(old or {}).get("text", ""), new_text=v2_by_id[sid]["text"],
                            char_diffs=None, summary=summary))
    changes.append(dict(change_id=1, kind="removed", ratio=0.0, old_page=P2, new_page=None,
                        old_text=REMOVED_V1["text"], new_text="",
                        char_diffs=None, summary="删除 1 段"))
    counts = dict(unchanged=len(v2_segments) - 3, modified=1, added=1, removed=1, moved=1,
                  reordered=0, split=0, merged=0)

    data = dict(
        documents=[dict(doc_id=1, slug="bms-training-manual", title="BMS Training Manual (mock)",
                        current_version_id=2, version_count=2, page_count=3,
                        stats=dict(segments=len(v2_segments), chars=sum(len(s["text"]) for s in SEGS),
                                   pages=3, translated=len([s for s in SEGS if s["trans"]])))],
        versions={"1": [dict(version_id=1, version_no=1, label="v1", release_label="4.38.0",
                             doc_date="2024-01-10", page_count=3, is_current=0, note="mock baseline"),
                        dict(version_id=2, version_no=2, label="v2", release_label="4.38.1",
                             doc_date="2025-03-02", page_count=3, is_current=1, note="mock update")]},
        segments={"2": v2_segments, "1": v1},
        markers=markers,
        layout=layout,
        outline={"2": [dict(page=P1, seg_id=102, kind="heading", text="CHAPTER 1  AIRCRAFT GENERAL",
                            translation="第 1 章  飞机概述"),
                       dict(page=P2, seg_id=201, kind="heading", text="1.2  FLIGHT CONTROL SYSTEM",
                            translation="1.2  飞控系统"),
                       dict(page=P3, seg_id=301, kind="heading", text="1.3  ENGINE AND FUEL SYSTEM",
                            translation="1.3  发动机和燃油系统")]},
        diff={"1": dict(from_version_id=1, to_version_id=2, counts=counts, changes=changes)},
        glossary=[dict(en="flight control system", zh="飞控系统", case_sensitive=False, note="FLCS"),
                  dict(en="canopy", zh="座舱盖", case_sensitive=False, note=""),
                  dict(en="landing gear", zh="起落架", case_sensitive=False, note=""),
                  dict(en="afterburner", zh="加力", case_sensitive=False, note=""),
                  dict(en="STPT", zh="航路点", case_sensitive=True, note="steerpoint"),
                  dict(en="MFD", zh="多功能显示器", case_sensitive=True, note=""),
                  dict(en="OBOGS", zh="机载制氧系统", case_sensitive=True, note=""),
                  dict(en="EPU", zh="应急动力装置", case_sensitive=True, note="")],
        page_images={"2": {str(p): img_paths[p] for p in (P1, P2, P3)}},
        page_size={"2": dict(width=PW, height=PH, dpi=DPI)},
        fonts=dict(cjk=CJK_PATH, latin="C:/Windows/Fonts/calibri.ttf"),
    )
    with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    doc.close()
    return data


if __name__ == "__main__":
    d = build()
    print("wrote data.json:", len(d["segments"]["2"]), "v2 segments")
    print("pages:", sorted(os.listdir(PAGES_DIR)))
