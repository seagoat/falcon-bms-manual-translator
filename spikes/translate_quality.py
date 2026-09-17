"""Lead spike: real DeepSeek translation quality check on real manual content.

Standalone (no repo module dependencies) so it can run before teammates finish.
Groups PDF lines into pseudo-paragraphs, sends one batch, prints EN -> ZH pairs,
and measures basic fidelity signals: acronym retention, unit retention, length ratio.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
API = "https://api.deepseek.com/chat/completions"
# ⚠️ 不要把 API key 写进代码。优先读环境变量；也可走 config/local.json（已被 .gitignore 排除）。
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not KEY:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import config as _cfg

        KEY = (_cfg.load() or {}).get("deepseek_api_key") or ""
    except Exception:
        KEY = ""
if not KEY:
    sys.exit("缺少 DEEPSEEK_API_KEY：请设环境变量，或在 config/local.json 里填 deepseek_api_key")

SYSTEM = """你是资深航空技术翻译，专门翻译 F-16 战斗机模拟器（BMS）训练手册。
把用户给出的英文段落翻译成简体中文。严格遵守：
1. 这是技术手册，面向中文飞行模拟玩家与飞行员。使用航空/军事领域标准译法。
2. 必须保留：型号与编号（F-16、BMS 4.38.1、Dash-34）、单位（ft、kt、psi、NM、°、%、G、RPM）、
   键盘按键、MFD/DED 页面名、代码与文件名（如 TR_BMS_01_GroundOPS）。
3. 缩写在首次出现时用「中文（缩写）」形式，例如 MASTER CAUTION → 主警戒灯（MASTER CAUTION）；
   已经在术语表里给出译法的，直接使用术语表译法。
4. 保留原有的列表层级与项目符号含义，保留数字编号。不要输出 markdown 代码块。
5. 只输出译文；不要解释、不要加注释、不要输出英文原文。
6. 术语表（必须严格遵守）：
   MASTER CAUTION=主警戒灯; CAUTION=警戒; WARNING=警告; canopy=座舱盖; FLCS=飞控系统;
   landing gear=起落架; throttle=油门; afterburner=加力燃烧室; HUD=平视显示器(HUD);
   RWR=雷达告警接收机; TACAN=塔康; EPU=应急动力装置; OBOGS=机载制氧系统; PFLD=飞行员故障清单显示器;
   MFL=维护故障清单; VMS=语音告警系统; STPT=航路点; MFD=多功能显示器; ICP=综合控制面板;
   DED=数据输入显示器; FCR=火控雷达; CMDS=对抗措施撒放系统; BIT=内建测试; AOA=迎角;
   DTC=数据磁带; PFL=飞行员故障清单; HSI=水平位置指示器; ILS=仪表着陆系统; FLCC=飞控计算机;
   DBU=数字备份; FTIT=风扇涡轮进口温度; WOW=机轮承重; TFR=地形跟随雷达; UFC=上前方控制器;
   ELEC SYS=电气系统; BINGO=返航油量; CHAFF=箔条; FLARE=红外干扰弹; ECM=电子对抗;
   EMERGENCY=应急; checklists=检查单; preflight=飞行前; sortie=架次; formation=编队;
   flight lead=长机; element=双机编队; wingman=僚机; strafe=航炮扫射; ingress=进入; egress=退出;
   on-station=到位; fence in=进入战斗状态; fence out=退出战斗状态; TACAN=塔康;
   drag=阻力; trim=配平; flaps=襟翼; airbrake=减速板; pitch=俯仰; roll=滚转; yaw=偏航;
   overload=过载; supersonic=超声速; subsonic=亚声速; angle of attack=迎角; stall=失速;
   taxi=滑行; takeoff=起飞; landing=着陆; touchdown=接地; approach=进近; go-around=复飞;
   flare=拉平; ILS=仪表着陆系统; VFR=目视飞行规则; IFR=仪表飞行规则;
   AB=加力; mil power=军用推力; idle=慢车; shutdown=停车; start-up=启动; EPU=应急动力装置;
   navigation=导航; waypoint=航路点; steerpoint=航路点; bullseye=靶眼; radar=雷达;
   lock=锁定; track=跟踪; scan=扫描; range=距离; bearing=方位; altitude=高度;
   airspeed=空速; groundspeed=地速; heading=航向; climb=爬升; descend=下降; turn=转弯;
   brake=刹车; anti-skid=防滑; nosewheel=前轮; main gear=主起落架; gear handle=起落架手柄;
   caution light=警戒灯; warning light=警告灯; master arm=主武器开关; stores=外挂物;
   datalink=数据链; TWS=边扫描边跟踪; SAM=地空导弹; BVR=超视距; WVR=视距内;
   SEAD=压制敌防空; CAS=近距空中支援; DEAD=摧毁敌防空; OCA=进攻性制空; DCA=防御性制空."""

USER_TMPL = """把下面 {n} 个段落翻译成中文。返回 JSON：{{"translations":[{{"id":<整数 id>,"zh":"<译文>"}}]}}
必须包含全部 {n} 个 id，一一对应，顺序不限。段落按出现的先后排列。

{payload}"""


def extract_paragraphs(pdf: str, pages: list[int]) -> list[dict]:
    import pymupdf

    doc = pymupdf.open(pdf)
    out: list[dict] = []
    for pno in pages:
        page = doc[pno - 1]
        for bi, b in enumerate(page.get_text("dict")["blocks"]):
            if b["type"] != 0:
                continue
            lines = []
            for line in b.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                txt = "".join(s["text"] for s in spans)
                lines.append({
                    "text": txt,
                    "x0": line["bbox"][0], "y0": line["bbox"][1], "y1": line["bbox"][3],
                    "size": spans[0]["size"], "font": spans[0]["font"],
                })
            if not lines:
                continue
            # split block into paragraphs on indent change / size change / bullet line
            groups: list[list[dict]] = []
            cur: list[dict] = []
            for ln in lines:
                if not cur:
                    cur = [ln]
                    continue
                prev = cur[-1]
                new_para = False
                if abs(ln["size"] - prev["size"]) > 0.6:
                    new_para = True
                elif ln["x0"] - prev["x0"] > 6:
                    new_para = True
                elif prev["text"].strip() in {"-", "•", "▪", "◆"}:
                    new_para = True
                elif re.match(r"^\d+\.\s", ln["text"].strip()):
                    new_para = True
                if new_para:
                    groups.append(cur)
                    cur = [ln]
                else:
                    cur.append(ln)
            if cur:
                groups.append(cur)
            for g in groups:
                txt = " ".join(x["text"].strip() for x in g)
                txt = re.sub(r"\s+", " ", txt).strip()
                if len(txt) < 6:
                    continue
                y0 = min(x["y0"] for x in g)
                if y0 <= 66 or y0 >= 776:
                    continue  # header/footer
                out.append({"page": pno, "block": bi, "text": txt,
                            "size": g[0]["size"], "bold": "Bold" in g[0]["font"]})
    return out


def post(payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    pages = [int(x) for x in (sys.argv[1:] or ["15", "21", "24", "101"])]
    model = os.environ.get("MT_MODEL", "deepseek-v4-pro")
    paras = extract_paragraphs(PDF, pages)
    batch = paras[:12]
    print(f"model={model}  extracted {len(paras)} paragraphs from pages {pages}; "
          f"sending {len(batch)}")

    payload_lines = []
    for i, p in enumerate(batch):
        payload_lines.append(f'[{i}] (p{p["page"]}) {p["text"]}')
    user = USER_TMPL.format(n=len(batch), payload="\n".join(payload_lines))

    t0 = time.time()
    try:
        resp = post({
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 1.0,
            "max_tokens": 8192,
        })
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode("utf-8", "replace")[:600])
        return 1
    dt = time.time() - t0

    content = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage", {})
    data = json.loads(content)
    got = {int(t["id"]): t["zh"] for t in data.get("translations", [])}

    os.makedirs(OUT_DIR, exist_ok=True)
    result = {"model": model, "elapsed_s": round(dt, 2), "usage": usage,
              "pairs": [{"id": i, "page": batch[i]["page"], "en": batch[i]["text"],
                         "zh": got.get(i, "")} for i in range(len(batch))]}
    path = os.path.join(OUT_DIR, "translate_quality.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    print(f"elapsed={dt:.1f}s  usage={usage}  returned {len(got)}/{len(batch)}")
    missing = [i for i in range(len(batch)) if i not in got]
    if missing:
        print(f"!! MISSING IDS: {missing}")
    print("=" * 100)
    acro_re = re.compile(r"\b[A-Z][A-Z0-9]{1,7}\b")
    num_re = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:ft|kt|psi|NM|%|G|RPM|°)?")
    for i in range(len(batch)):
        en, zh = batch[i]["text"], got.get(i, "")
        acros = [a for a in acro_re.findall(en) if len(a) > 1]
        kept = [a for a in acros if a in zh]
        print(f"--- [{i}] p{batch[i]['page']} size={batch[i]['size']} bold={batch[i]['bold']}")
        print(f"EN: {en}")
        print(f"ZH: {zh}")
        print(f"len en={len(en)} zh={len(zh)} ratio={len(zh)/max(1,len(en)):.2f} "
              f"acronyms={len(kept)}/{len(acros)} lost={[a for a in acros if a not in zh][:6]}")
    print("=" * 100)
    print("saved ->", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
