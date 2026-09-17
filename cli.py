#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli.py —— manual_trans_trace 命令行入口（中文交互，失败退出码非 0）。

用法：
    python cli.py ingest --pdf origin/BMS-Training-Manual.pdf
    python cli.py simulate-update                            # 生成 v2 演示版并自动 diff
    python cli.py translate --page-to 30 --provider mock
    python cli.py diff --from 1 --to 2
    python cli.py export --kind cn
    python cli.py render-page --version 1 --page 21 --out data/derived/p21.png
    python cli.py serve
    python cli.py list / status / cost-estimate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pipeline  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


# --------------------------------------------------------------------------------------
# 输出小工具
# --------------------------------------------------------------------------------------

def out(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"{GREEN}✅ {msg}{RESET}", flush=True)


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠️  {msg}{RESET}", flush=True)


def fail(msg: str) -> None:
    print(f"{RED}❌ {msg}{RESET}", file=sys.stderr, flush=True)


def head(msg: str) -> None:
    print(f"\n{BOLD}=== {msg} ==={RESET}", flush=True)


def table(rows: list[list[str]], headers: list[str]) -> str:
    cols = len(headers)
    widths = [len(headers[i]) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))
    def line(vals):
        return "  ".join(str(vals[i]).ljust(widths[i]) for i in range(cols))
    sep = "  ".join("-" * widths[i] for i in range(cols))
    body = [line(headers), sep] + [line(r) for r in rows]
    return "\n".join(body)


# --------------------------------------------------------------------------------------
# 通用解析
# --------------------------------------------------------------------------------------

def parse_pages(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    t = str(text).strip().replace("~", "-")
    if "-" in t:
        a, _, b = t.partition("-")
        try:
            lo = int(a) if a.strip() else 1
            hi = int(b) if b.strip() else 10 ** 9
        except ValueError as exc:
            raise SystemExit(f"--pages 格式应为 a-b，收到 {text!r}") from exc
    else:
        lo = hi = int(t)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def page_list(pages: tuple[int, int] | None, page_count: int | None = None) -> list[int] | None:
    if not pages:
        return None
    hi = pages[1]
    if page_count:
        hi = min(hi, page_count)
    return list(range(pages[0], hi + 1))


def open_db(db_path: str | None):
    from core import db
    return db.connect(db_path or None)


def store_mod():
    from core import store
    return store


def resolve_doc(conn, doc_arg) -> int:
    st = store_mod()
    docs = st.list_documents(conn)
    if not docs:
        raise SystemExit("数据库里还没有任何文档，请先运行：python cli.py ingest --pdf <PDF>")
    if doc_arg is None:
        if len(docs) == 1:
            return int(docs[0]["doc_id"])
        cur = [d for d in docs if d.get("current_version_id")]
        if len(cur) == 1:
            return int(cur[0]["doc_id"])
        out("可用文档：")
        out(table([[d["doc_id"], d["slug"], d["title"]] for d in docs],
                  ["doc_id", "slug", "title"]))
        raise SystemExit("存在多个文档，请用 --doc 指定")
    for d in docs:
        if str(d["doc_id"]) == str(doc_arg) or d["slug"] == str(doc_arg):
            return int(d["doc_id"])
    raise SystemExit(f"找不到文档：{doc_arg}")


def resolve_version(conn, doc_id: int, v_arg=None, *, label_hint: str = "current") -> dict:
    st = store_mod()
    versions = st.list_versions(conn, doc_id)
    if not versions:
        raise SystemExit(f"文档 {doc_id} 还没有版本，请先 ingest")
    if v_arg is None:
        cur = st.current_version(conn, doc_id)
        return cur or versions[-1]
    key = str(v_arg).strip()
    for v in versions:
        if str(v["version_id"]) == key or str(v["version_no"]) == key or str(v.get("label")) == key:
            return v
    out("可用版本：" + ", ".join(f"{v['version_no']}/{v.get('label')}#{v['version_id']}"
                                 for v in versions))
    raise SystemExit(f"找不到版本：{v_arg}（{label_hint}）")


def kind_stats(conn, version_id: int) -> tuple[dict, dict]:
    from collections import Counter
    segs = store_mod().segments_of_version(conn, version_id)
    kinds = Counter(s["kind"] for s in segs)
    roles = Counter(s["role"] for s in segs)
    return dict(kinds), dict(roles)


def fmt_counts(d: dict) -> str:
    order = ["unchanged", "modified", "added", "removed", "moved", "reordered", "split", "merged"]
    cn = {"unchanged": "未变", "modified": "修改", "added": "新增", "removed": "删除",
          "moved": "移动", "reordered": "重排", "split": "拆分", "merged": "合并"}
    return "  ".join(f"{cn.get(k, k)}={d.get(k, 0)}" for k in order if k in d) or "(空)"


# --------------------------------------------------------------------------------------
# 子命令：ingest
# --------------------------------------------------------------------------------------

def cmd_ingest(args) -> int:
    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = ROOT / pdf
    if not pdf.exists():
        fail(f"找不到 PDF：{pdf}")
        return 2
    pages = parse_pages(args.pages)
    head(f"抽取入库 {pdf.name}")
    if pages:
        warn(f"仅抽取第 {pages[0]}–{pages[1]} 页（调试用，段数不完整）")
    conn = open_db(args.db)
    t0 = time.time()
    last = {"p": 0}

    def prog(pno, total, n):
        if pno - last["p"] >= 25 or pno == total:
            last["p"] = pno
            out(f"  抽取 {pno}/{total} 页，累计 {n} 段 ...")

    # 已存在同 sha 版本时：--force = 重抽但**保住译文**；--rebuild = 显式丢译文重建
    st = store_mod()
    prior = None
    try:
        sha = pipeline.sha256_file(pdf)
        for d in st.list_documents(conn):
            if d["slug"] == (args.slug or pipeline.slugify(pdf.stem)):
                prior = next((v for v in st.list_versions(conn, int(d["doc_id"]))
                              if v.get("sha256") == sha), None)
                break
    except Exception:
        prior = None

    if prior is not None and (args.force or args.rebuild):
        n_tr = len(st.translations_of_version(conn, int(prior["version_id"])) or {})
        if args.rebuild and not args.yes_i_understand:
            fail(f"--rebuild 会删除版本 {prior['version_id']} 的 {n_tr} 条译文，"
                 f"必须同时加 --yes-i-understand 确认")
            conn.close()
            return 2
        out(f"  已有同内容版本 #{prior['version_id']}（{prior.get('label')}），"
            f"译文 {n_tr} 条")
        if args.rebuild:
            warn("--rebuild --yes-i-understand：重建分段并**丢弃**该版本全部译文")
        else:
            out("  --force：重建分段并自动回填译文（先备份到 data/derived/）")
        try:
            res = pipeline.rebuild_version(conn, int(prior["version_id"]),
                                           pdf_path=str(pdf),
                                           keep_translations=not args.rebuild,
                                           allow_translation_loss=bool(args.rebuild),
                                           progress=prog)
        except Exception as exc:  # noqa: BLE001
            fail(f"重建失败：{exc}")
            conn.close()
            return 1
        dt = time.time() - t0
        r = res.get("restore") or {}
        kinds, roles = kind_stats(conn, int(prior["version_id"]))
        ok(f"重建完成（{dt:.1f}s）")
        out(f"  版本   : {prior.get('label')} (version_id={prior['version_id']})  "
            f"段数={res['ingest'].get('segments')}")
        out(f"  role   : " + "  ".join(f"{k}={v}" for k, v in sorted(roles.items())))
        out(f"  kind   : " + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
        if r:
            out(f"  译文   : 备份 {res['backup_count']} 条 → 回填 {r.get('restored', 0)} "
                f"（精确={r.get('exact', 0)} 近似={r.get('near', 0) + r.get('contains', 0)}）"
                f"  形状不匹配跳过={r.get('skipped_shape', 0)}  未匹配={r.get('lost', 0)}")
            out(f"           库中现有 {res.get('translations_after', '-')} 条；备份文件 "
                f"{res.get('backup_path') or '-'}")
            if r.get("skipped"):
                out(f"           形状不匹配示例（留空待重译）: " +
                    "; ".join(f"p{s['page']} {s['kind']} {s['en']!r}" for s in r["skipped"][:3]))
        elif args.rebuild:
            out("  译文   : 已按 --rebuild 语义清空")
        conn.close()
        return 0

    try:
        info = pipeline.ingest_pdf(conn, pdf, slug=args.slug, title=args.title,
                                   note=args.note or "", label=args.label,
                                   pages=pages, progress=prog)
    except Exception as exc:  # noqa: BLE001
        fail(f"抽取失败：{exc}")
        conn.close()
        return 1
    dt = time.time() - t0
    kinds, roles = kind_stats(conn, info["version_id"])
    if info.get("reused"):
        warn("该 PDF 已入库且已抽段，本次直接复用（未重复插入）。"
             "想按新规则重抽请加 --force（会保住译文）")
    ok("入库完成")
    out(f"  文档   : {info['slug']} (doc_id={info['doc_id']})  {info['title']}")
    out(f"  版本   : {info['label']} (version_id={info['version_id']})  "
        f"release={info['release_label'] or '-'}  date={info['doc_date'] or '-'}")
    out(f"  页数   : {info['pages']}    段数: {info['segments']}    "
        f"字符: {info['chars']}    图片: {info['images']}")
    out(f"  role   : " + "  ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    out(f"  kind   : " + "  ".join(f"{k}={v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    out(f"  sha256 : {info['sha256'][:16]}...   PDF: {info['pdf_bytes']/1024/1024:.1f} MB")
    out(f"  耗时   : {dt:.1f}s" + (f"  ({info['pages']/dt:.1f} 页/秒)" if dt > 0 else ""))

    versions = store_mod().list_versions(conn, info["doc_id"])
    if len(versions) >= 2 and not args.no_diff:
        try:
            from versions import differ
            a, b = versions[-2], versions[-1]
            head(f"自动计算版本差异 {a.get('label')} → {b.get('label')}")
            vd = differ.diff_versions(conn, info["doc_id"], a["version_id"], b["version_id"])
            conn.commit()
            out("  " + fmt_counts(vd.counts))
        except Exception as exc:  # noqa: BLE001
            warn(f"自动 diff 失败（不影响入库）：{exc}")
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 子命令：translate
# --------------------------------------------------------------------------------------

def cmd_translate(args) -> int:
    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    vid = int(v["version_id"])
    pages = parse_pages(args.pages)
    st = store_mod()
    from translate import engine

    if pages:
        out(f"翻译范围：第 {pages[0]}–{pages[1]} 页")

    seg_ids = None
    if args.limit:
        segs = [s for s in st.segments_of_version(conn, vid)
                if s["role"] == "body"
                and (not pages or pages[0] <= int(s["page"]) <= pages[1])]
        if not args.force:
            done = {int(k) for k, r in (st.translations_of_version(conn, vid) or {}).items()
                    if (r or {}).get("text")}
            segs = [s for s in segs if int(s["id"]) not in done]
        seg_ids = [int(s["id"]) for s in segs[: args.limit]]
        out(f"--limit {args.limit} → 选定 {len(seg_ids)} 段")

    jid = st.create_job(conn, "translate", {
        "doc_id": doc_id, "version_id": vid, "page_from": pages[0] if pages else None,
        "page_to": pages[1] if pages else None, "provider": args.provider,
        "model": args.model, "limit": args.limit, "dry_run": bool(args.dry_run),
    })
    conn.commit()
    head(f"翻译 {v.get('label')} (version_id={vid})  provider={args.provider or config.get('provider')}"
         + ("  [dry-run 不调用 API]" if args.dry_run else ""))
    t0 = time.time()
    try:
        res = engine.translate_segments(
            conn, doc_id, vid, segment_ids=seg_ids,
            page_from=None if seg_ids else (pages[0] if pages else None),
            page_to=None if seg_ids else (pages[1] if pages else None),
            provider=args.provider, model=args.model or None,
            concurrency=args.concurrency, force=args.force, job_id=jid,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:  # noqa: BLE001
        try:
            st.update_job(conn, jid, status="failed", error=str(exc))
            conn.commit()
        except Exception:
            pass
        fail(f"翻译失败：{exc}")
        return 1
    dt = time.time() - t0
    toks = res.get("tokens") or {}
    # job 终态：成功必须落 done（否则 jobs 表会留下「已完成却 running」的行）
    try:
        total = None
        try:
            total = int((st.translation_stats(conn, vid) or {}).get("total") or 0) or None
        except Exception:
            total = None
        st.update_job(conn, jid, status="done",
                      progress=total or 0, total=total or 0,
                      message=f"完成：新译 {res.get('translated', 0)} / 失败 {res.get('failed', 0)}",
                      result={"translated": res.get("translated", 0),
                              "carried": res.get("carried", 0),
                              "cached": res.get("cached", 0),
                              "failed": res.get("failed", 0),
                              "batches": res.get("batches", 0),
                              "dry_run": bool(args.dry_run)})
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        warn(f"job {jid} 终态写入失败：{exc}")
    ok(f"翻译完成，耗时 {dt:.1f}s  (job {jid} → done)")
    out(f"  新译={res.get('translated', 0)}  沿用缓存={res.get('cached', 0)}  "
        f"原文未变沿用={res.get('carried', 0)}  失败={res.get('failed', 0)}")
    out(f"  machine={res.get('machine', 0)}  seeded={res.get('seeded', 0)}  "
        f"批次={res.get('batches', 0)}  字符={res.get('chars', 0)}")
    out(f"  tokens: prompt={toks.get('prompt', 0)}  completion={toks.get('completion', 0)}")
    cost = res.get("cost") or {}
    if cost:
        out(f"  估算费用: ${cost.get('usd', 0)} ≈ ¥{cost.get('cny', 0)}")
    if res.get("errors"):
        warn(f"{len(res['errors'])} 条错误，示例：{str(res['errors'][:2])[:200]}")
    if args.dry_run:
        warn("这是 dry-run：没有调用 API，也没有写入译文")
    try:
        stats = st.translation_stats(conn, vid)
        out(f"  版本进度: {stats.get('translated', 0)}/{stats.get('total', 0)}  "
            + "  ".join(f"{k}={v}" for k, v in sorted((stats.get("by_status") or {}).items())))
    except Exception:
        pass
    conn.close()
    return 1 if res.get("failed") else 0


# --------------------------------------------------------------------------------------
# 子命令：diff
# --------------------------------------------------------------------------------------

def cmd_diff(args) -> int:
    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    st = store_mod()
    versions = st.list_versions(conn, doc_id)

    def pick(x):
        for v in versions:
            if str(v["version_no"]) == str(x) or str(v["version_id"]) == str(x) or v.get("label") == str(x):
                return v
        raise SystemExit(f"找不到版本 {x}；可用：" +
                         ", ".join(f"{v['version_no']}({v.get('label')})" for v in versions))

    a, b = pick(args.from_ver), pick(args.to_ver)
    head(f"版本差异 {a.get('label')}(#{a['version_id']}) → {b.get('label')}(#{b['version_id']})")
    from versions import differ
    t0 = time.time()
    vd = differ.diff_versions(conn, doc_id, a["version_id"], b["version_id"])
    conn.commit()
    out(f"  {fmt_counts(vd.counts)}    耗时 {time.time()-t0:.2f}s")
    changed = [c for c in vd.changes if c.kind != "unchanged"]
    out(f"  段总数 {len(vd.changes)}，其中发生变更 {len(changed)} 段")
    limit = getattr(args, "limit", 20) or 20
    for i, c in enumerate(changed, 1):
        if i > limit:
            out(f"  ... 其余 {len(changed) - limit} 条省略（--limit 调整）")
            break
        out(f"  [{i}] {c.kind:<9} ratio={c.ratio:.3f} p{c.old_page}→p{c.new_page} "
            f"old#{c.old_segment_id} new#{c.new_segment_id}")
        if args.verbose:
            out(f"        {c.summary[:180]}")
    if args.markdown:
        head("Markdown 摘要")
        out(differ.diff_summary_text(vd))
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 子命令：export / render-page
# --------------------------------------------------------------------------------------

def cmd_export(args) -> int:
    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    pages = parse_pages(args.pages)
    vdict = store_mod().get_version(conn, int(v["version_id"])) or {}
    # pages 语义统一：tuple=(from,to) 区间、list=显式页列表（见 pipeline.normalize_pages）
    plist = pipeline.normalize_pages(pages, int(vdict.get("page_count") or 0))
    out_path = args.out or str(config.derived_dir(vdict.get("slug") or "doc",
                                                 f"v{vdict.get('version_no') or 1}",
                                                 "export") / f"{args.kind}.pdf")
    head(f"导出 {args.kind} PDF  ← {v.get('label')} (version_id={v['version_id']})")
    if plist:
        out(f"  页范围: {plist[0]}–{plist[-1]}（{len(plist)} 页）")
    t0 = time.time()
    try:
        res = pipeline.export_version_pdf(conn, doc_id, int(v["version_id"]), kind=args.kind,
                                          out_path=out_path, page_list=plist,
                                          dpi=args.dpi, on_page=None)
    except Exception as exc:  # noqa: BLE001
        fail(f"导出失败：{exc}")
        return 1
    p = Path(res.get("path") or out_path)
    if not p.exists():
        fail("导出函数没有生成文件")
        return 1
    size = p.stat().st_size
    ok(f"导出完成：{p}  ({size/1024/1024:.2f} MB, {time.time()-t0:.1f}s)")
    out(f"  pages={res.get('pages', '-')}  boxes={res.get('boxes', '-')}  "
        f"overflow={res.get('overflow', '-')}  shrunk={res.get('shrunk', '-')}")
    conn.close()
    return 0


def cmd_render_page(args) -> int:
    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    if not args.out:
        fail("需要 --out 指定输出文件")
        return 2
    head(f"渲染第 {args.page} 页  kind={args.kind}")
    try:
        p = pipeline.render_page_to_file(conn, doc_id, int(v["version_id"]), int(args.page),
                                         kind=args.kind, out_path=args.out, dpi=args.dpi)
    except Exception as exc:  # noqa: BLE001
        fail(f"渲染失败：{exc}")
        return 1
    ok(f"已写出：{p}  ({Path(p).stat().st_size/1024:.0f} KB)")
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 子命令：serve
# --------------------------------------------------------------------------------------

def cmd_serve(args) -> int:
    host = args.host or str(config.get("web_host", "127.0.0.1"))
    port = int(args.port or config.get("web_port", 8777))
    url = f"http://{host}:{port}/"
    head("启动 Web 服务")
    out(f"  地址: {url}")
    out(f"  数据库: {config.db_path()}")
    out(f"  文档: README.md › Web UI 用法")
    if not args.no_open:
        try:
            import threading
            import webbrowser

            def _open():
                time.sleep(1.5)
                webbrowser.open(url)
            threading.Thread(target=_open, daemon=True).start()
        except Exception:
            pass
    try:
        from web import server
        return int(server.run(host=host, port=port, reload=bool(args.reload)) or 0)
    except ImportError as exc:
        fail(f"Web 服务依赖缺失：{exc}")
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        fail(f"服务启动失败：{exc}")
        return 1


# --------------------------------------------------------------------------------------
# 子命令：list / status
# --------------------------------------------------------------------------------------

def cmd_list(args) -> int:
    conn = open_db(args.db)
    st = store_mod()
    docs = st.list_documents(conn)
    if not docs:
        warn("数据库里还没有文档")
        out("下一步: python cli.py ingest --pdf origin/BMS-Training-Manual.pdf")
        conn.close()
        return 0
    head("文档")
    rows = []
    for d in docs:
        versions = st.list_versions(conn, d["doc_id"])
        cur = next((v for v in versions if int(v.get("is_current") or 0)), versions[-1] if versions else {})
        rows.append([d["doc_id"], d["slug"], (d.get("title") or "")[:36], len(versions),
                     f"{cur.get('label', '-')}#{cur.get('version_id', '-')}",
                     cur.get("page_count", d.get("page_count", "-")),
                     cur.get("release_label") or "-",
                     (cur.get("stats") or {}).get("segments", "-")])
    out(table(rows, ["doc_id", "slug", "title", "版本数", "当前版本", "页数", "release", "段数"]))
    conn.close()
    return 0


def cmd_status(args) -> int:
    conn = open_db(args.db)
    st = store_mod()
    doc_ids = [resolve_doc(conn, args.doc)] if args.doc else [int(d["doc_id"]) for d in st.list_documents(conn)]
    if not doc_ids:
        warn("数据库里还没有文档")
        conn.close()
        return 0
    for doc_id in doc_ids:
        d = st.get_document(conn, doc_id) or {}
        head(f"文档 {doc_id} | {d.get('slug')} | {d.get('title')}")
        versions = st.list_versions(conn, doc_id)
        rows = []
        for v in versions:
            stats = v.get("stats") or {}
            rows.append([v.get("label"), v["version_id"], v.get("version_no"),
                         v.get("page_count"), stats.get("segments", "-"),
                         v.get("release_label") or "-", v.get("doc_date") or "-",
                         "← 当前" if int(v.get("is_current") or 0) else ""])
        out(table(rows, ["label", "vid", "no", "页数", "段数", "release", "日期", ""]))
        for v in versions:
            vid = int(v["version_id"])
            try:
                ts = st.translation_stats(conn, vid)
                out(f"  翻译进度 {v.get('label')}: {ts.get('translated', 0)}/{ts.get('total', 0)}  "
                    + "  ".join(f"{k}={n}" for k, n in sorted((ts.get('by_status') or {}).items())))
            except Exception:
                pass
        if len(versions) >= 2:
            try:
                from versions import differ
                ds = st.diff_summary(conn, doc_id, int(versions[-2]["version_id"]),
                                     int(versions[-1]["version_id"]))
                if ds:
                    out(f"  最近变更 {versions[-2].get('label')}→{versions[-1].get('label')}: "
                        + fmt_counts(ds.get("counts") or ds))
                else:
                    out(f"  最近变更 {versions[-2].get('label')}→{versions[-1].get('label')}: (未计算)")
            except Exception:
                pass
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 子命令：simulate-update（关键：生成可验证的新版 PDF）
# --------------------------------------------------------------------------------------

def cmd_simulate_update(args) -> int:
    src = Path(args.pdf) if args.pdf else ROOT / "origin" / "BMS-Training-Manual.pdf"
    if not src.is_absolute():
        src = ROOT / src
    dst = Path(args.out) if args.out else ROOT / "origin" / "BMS-Training-Manual_v2_demo.pdf"
    if not dst.is_absolute():
        dst = ROOT / dst
    if not src.exists():
        fail(f"找不到源 PDF：{src}")
        return 2
    if src.resolve() == dst.resolve():
        fail("输出不能覆盖原始 PDF（origin 只读）")
        return 2

    head("生成演示用新版 PDF (v2)")
    out(f"  源文件: {src}")
    out(f"  输出  : {dst}")
    src_sha_before = pipeline.sha256_file(src)
    try:
        manifest = pipeline.simulate_update_pdf(src, dst, page_from=args.page_from,
                                                page_to=args.page_to, progress=lambda m: out("  " + m))
    except Exception as exc:  # noqa: BLE001
        fail(f"生成失败：{exc}")
        return 1
    src_sha_after = pipeline.sha256_file(src)
    if src_sha_before != src_sha_after:
        fail("原始 PDF 被修改了！这是严重错误")
        return 1
    ok(f"新版 PDF 已生成：{dst}  ({dst.stat().st_size/1024/1024:.1f} MB)")
    out(f"  原始文件 sha256 未变化 ✅ ({src_sha_before[:16]}...)")

    head("本次实际施加的改动清单")
    rows = []
    for e in manifest["edits"]:
        old = (e.get("old_text") or "").replace("\n", " ")[:46]
        new = (e.get("new_text") or "").replace("\n", " ")[:46]
        rows.append([e["index"], e["type"], e["page"], old or "-", new or "-"])
    out(table(rows, ["#", "类型", "页", "原文片段", "新版片段"]))
    out(f"\n  期望变更计数: {manifest['expected_counts']}")
    out(f"  manifest: {manifest['manifest_path']}")

    conn = open_db(args.db)
    st = store_mod()
    doc_id = resolve_doc(conn, args.doc)
    doc = st.get_document(conn, doc_id) or {}
    v1 = st.current_version(conn, doc_id) or {}
    existing = next((v for v in st.list_versions(conn, doc_id)
                     if v.get("sha256") == manifest["output_sha256"]), None)
    head("入库新版并计算差异")
    if existing:
        warn(f"该 v2 文件已在库中（{existing.get('label')}#{existing['version_id']}），直接复用")
        v2 = existing
        info = {"version_id": int(existing["version_id"]), "segments": (existing.get("stats") or {}).get("segments")}
    else:
        try:
            info = pipeline.ingest_pdf(conn, dst, slug=doc.get("slug"), title=doc.get("title"),
                                       note="simulate-update 演示二版", label=args.label,
                                       progress=lambda p, t, n: out(f"  抽取 {p}/{t} 页，累计 {n} 段 ...")
                                       if p % 50 == 0 or p == t else None)
        except Exception as exc:  # noqa: BLE001
            fail(f"新版入库失败：{exc}")
            return 1
        v2 = st.get_version(conn, info["version_id"]) or {}
    out(f"  v1 = #{v1.get('version_id')} ({v1.get('label')})   "
        f"v2 = #{v2.get('version_id')} ({v2.get('label')})  段数={info.get('segments', '-')}")

    from versions import differ
    t0 = time.time()
    vd = differ.diff_versions(conn, doc_id, int(v1["version_id"]), int(v2["version_id"]))
    conn.commit()
    v2 = st.get_version(conn, int(v2["version_id"])) or v2
    out(f"  diff 耗时 {time.time()-t0:.2f}s")

    expected = manifest["expected_counts"]
    actual = vd.counts or {}
    head("manifest ↔ 实际 diff 比对")
    rows = []
    all_ok = True
    for k in ("modified", "added", "removed", "moved"):
        exp, act = int(expected.get(k, 0)), int(actual.get(k, 0))
        good = exp == act
        all_ok = all_ok and good
        rows.append([k, exp, act, "✅" if good else "❌"])
    rows.append(["unchanged", "-", actual.get("unchanged", 0), ""])
    for k in ("reordered", "split", "merged"):
        if actual.get(k):
            rows.append([k, 0, actual[k], "⚠️" if k in ("split", "merged") else ""])
    out(table(rows, ["变更类型", "manifest 期望", "实际 diff", "结果"]))
    out(f"  完整计数: {fmt_counts(actual)}")
    changed = [c for c in vd.changes if c.kind != "unchanged"]
    out(f"  变更段总数: {len(changed)}")
    for c in changed[:12]:
        snip = (c.summary or "").replace("\n", " ")[:96]
        out(f"    - {c.kind:<9} ratio={c.ratio:.3f} p{c.old_page}→p{c.new_page}  {snip}")
    if len(changed) > 12:
        out(f"    ... 其余 {len(changed)-12} 条见 `python cli.py diff --from {v1.get('version_no')} "
            f"--to {v2.get('version_no')} --verbose`")

    if all_ok:
        ok("全部改动都被 diff 正确识别（7 处施加改动 ↔ 计数完全一致）")
    else:
        fail("存在未被正确识别的改动，见上表 ❌")
    out("")
    out("  下一步: python cli.py translate --version 2 --provider mock --page-to 30")
    out("          python cli.py serve   # 在 Web UI 里看 v1/v2 对照")
    conn.close()
    return 0 if all_ok else 1


# --------------------------------------------------------------------------------------
# 子命令：restore-translations（重建分段后回填译文 / 从备份恢复）
# --------------------------------------------------------------------------------------

def cmd_restore_translations(args) -> int:
    conn = open_db(args.db)
    st = store_mod()
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    vid = int(v["version_id"])
    head(f"回填译文 → {v.get('label')} (version_id={vid})")

    if args.backup:
        rows = json.loads(Path(args.backup).read_text(encoding="utf-8"))
    else:
        if args.from_version and str(args.from_version) != str(vid):
            rows = pipeline.dump_translations(conn, int(args.from_version))
        else:
            rows = pipeline.dump_translations(conn, vid)
        if not rows:
            warn("没有可回填的译文（备份为空）；如已重建分段，请用 --backup 指定 json 备份")
    out(f"  备份条数: {len(rows)}")

    res = pipeline.restore_translations(conn, vid, rows,
                                        shape_check=not args.no_shape_check,
                                        min_en_chars=args.min_en_chars)
    ok("回填完成")
    out(f"  精确(同页同指纹)={res['exact']}  最近页={res['near']}  同页包含={res['contains']}")
    out(f"  形状不匹配跳过={res['skipped_shape']}  未匹配={res['lost']}  "
        f"实际写入={res['restored']}")
    for s in res.get("skipped", [])[:5]:
        out(f"    跳过: p{s['page']} {s['kind']} en={s['en']!r} zh_len={s['zh_len']}")
    ts = st.translation_stats(conn, vid)
    out(f"  版本进度: {ts.get('translated', 0)}/{ts.get('total', 0)}  "
        + "  ".join(f"{k}={n}" for k, n in sorted((ts.get("by_status") or {}).items())))

    if args.clean:
        rows = conn.execute(
            "SELECT t.segment_id, t.text, t.status, s.text AS src FROM translations t "
            "JOIN segments s ON s.id=t.segment_id WHERE s.role='body'").fetchall()
        bad = [(int(r[0]), pipeline.classify_translation(r[1], r[3], r[2]), r[1]) for r in rows]
        bad = [(sid, why, t) for sid, why, t in bad if why != "ok"]
        for sid, _why, _t in bad:
            conn.execute("DELETE FROM translations WHERE segment_id=?", (sid,))
        conn.commit()
        from collections import Counter as _C
        out(f"  清理: 删除 {len(bad)} 条 " +
            "  ".join(f"{k}={v}" for k, v in sorted(_C(w for _s, w, _t in bad).items())))
        out("        （判定见 pipeline.classify_translation：型号/编号/原样保留/含中文 一律保留）")
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 子命令：cost-estimate
# --------------------------------------------------------------------------------------

def cmd_cost_estimate(args) -> int:
    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    vid = int(v["version_id"])
    pages = parse_pages(args.pages)
    from translate import engine
    head(f"翻译成本估算  {v.get('label')} (version_id={vid})  model={args.model or config.get('model')}")
    try:
        res = engine.translate_segments(conn, doc_id, vid, page_from=pages[0] if pages else None,
                                        page_to=pages[1] if pages else None,
                                        provider=args.provider, model=args.model or None,
                                        dry_run=True)
    except Exception as exc:  # noqa: BLE001
        fail(f"估算失败：{exc}")
        return 1
    toks = res.get("tokens") or {}
    cost = res.get("cost") or {}
    out(f"  待译段数 : {res.get('translated', 0)}（machine={res.get('machine', 0)}  "
        f"seeded={res.get('seeded', 0)}）")
    out(f"  已缓存/沿用: cached={res.get('cached', 0)}  carried={res.get('carried', 0)}")
    out(f"  批次数   : {res.get('batches', 0)}（每批 ≤{config.get('batch_segments')} 段 / "
        f"{config.get('batch_chars')} 字符）")
    out(f"  prompt   : {toks.get('prompt', 0):,} tokens")
    out(f"  completion: {toks.get('completion', 0):,} tokens")
    out(f"  合计     : {toks.get('prompt', 0) + toks.get('completion', 0):,} tokens")
    out(f"  估算费用 : ${cost.get('usd', 0)} ≈ ¥{cost.get('cny', 0)}"
        f"  (in ${cost.get('usd_per_mtok_in', '-')}/M, out ${cost.get('usd_per_mtok_out', '-')}/M)")
    if cost.get("note"):
        out(f"  说明     : {cost['note']}")
    out(f"  待译字符 : {res.get('chars', 0):,}")
    conn.close()
    return 0


# --------------------------------------------------------------------------------------
# 可移植性：把仓库做成可复制到别的机器/目录的形式
# --------------------------------------------------------------------------------------

def cmd_portable(args) -> int:
    """① 把 DB 里的绝对路径重写为相对仓库根；② 体检并报告复制时要带/不要带什么。"""
    from core import db as _db, paths as _paths, store as _store

    conn = _db.connect()
    plan = _paths.rewrite_db(conn, apply=not args.dry_run)
    if args.dry_run:
        out("（演练模式，未写入）")
    out(f"路径重写: documents={plan['documents']}  document_versions={plan['versions']}")
    if plan["outside_root"]:
        out(f"⚠️ {len(plan['outside_root'])} 个路径在仓库之外，保持绝对路径：")
        for p in plan["outside_root"][:5]:
            out(f"    {p}")

    docs = _store.list_documents(conn)
    out(f"\n文档数: {len(docs)}")
    ok = True
    for d in docs:
        for v in _store.list_versions(conn, d["doc_id"]):
            sp = v.get("source_path") or ""
            resolved = _paths.resolve(sp)
            exists = Path(resolved).exists()
            flag = "OK " if exists else "MISS"
            if not exists:
                ok = False
            out(f"  {flag} v{v['version_no']}  {sp}  ->  {resolved}")
    conn.close()

    if args.check:
        root = Path(__file__).resolve().parent
        out("\n=== 体检 ===")
        must = ["cli.py", "pipeline.py", "config", "core", "render", "web", "versions",
                "translate", "web/static/index.html", "README.md", "start_web.ps1"]
        for m in must:
            out(f"  {'OK ' if (root / m).exists() else 'MISS'} {m}")
        dbp = root / "data" / "app.db"
        out(f"  {'OK ' if dbp.exists() else 'MISS'} data/app.db "
            f"({dbp.stat().st_size/1e6:.1f} MB)" if dbp.exists() else "  MISS data/app.db")
        out(f"  {'OK ' if (root / 'data' / 'glossary.json').exists() else '可选'} data/glossary.json")
        srcs = sorted((root / "origin").glob("*.pdf")) if (root / "origin").exists() else []
        out(f"  origin/ 下的 PDF: {len(srcs)} 个"
            + ("" if ok else " ⚠️ 有版本源 PDF 缺失，页面图会 404"))
        for f in ("core", "pdfdoc"):
            pass
        out("\n复制清单：")
        out("  必须带: cli.py pipeline.py config/ core/ render/ versions/ translate/ web/ "
            "tests/ data/app.db data/glossary.json")
        out("  源 PDF : origin/*.pdf（体积大；若已带 app.db 且不改版本，可只带当前版本那一份）")
        out("  不要带: data/derived/ review/ spikes/ __pycache__/ data/app.db.bak_*")
        out("  环境   : Python 3.11 + pip install pymupdf fastapi uvicorn pillow numpy "
            "rapidocr-onnxruntime（详见 requirements.txt）")
        out("  密钥   : config/local.json 里含 DeepSeek key —— 分享给别人请清空，"
            "改用环境变量 DEEPSEEK_API_KEY")
    return 0 if ok else 1


def cmd_setup(args) -> int:
    """在目标机器上校验环境是否可用（复制过来之后先跑这个）。"""
    import importlib
    out("=== 环境自检 ===")
    out(f"  Python {sys.version.split()[0]}  ({sys.executable})")
    mods = [("pymupdf", "pymupdf"), ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
            ("PIL", "Pillow"), ("numpy", "numpy"), ("rapidocr_onnxruntime", "rapidocr")]
    missing = []
    for mod, pkg in mods:
        try:
            m = importlib.import_module(mod)
            v = getattr(m, "__version__", "?")
            out(f"  OK   {pkg:22s} {v}")
        except Exception as e:
            out(f"  MISS {pkg:22s} {type(e).__name__}")
            missing.append(pkg)
    if missing:
        out(f"\n缺依赖，请执行:\n  pip install " + " ".join(missing))
        return 1

    root = Path(__file__).resolve().parent
    out("\n=== 仓库自检 ===")
    for f in ("cli.py", "pipeline.py", "config/local.json", "data/app.db",
              "web/static/index.html"):
        p = root / f
        out(f"  {'OK ' if p.exists() else 'MISS'} {f}")
    from config import load as _load
    cfg = _load(reload=True)
    key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY") or ""
    out(f"\n  DeepSeek key: {'已配置' if key else '未配置（离线 mock 可用，真翻译需配置）'}")
    from core import db as _db, paths as _paths, store as _store
    conn = _db.connect()
    for d in _store.list_documents(conn):
        for v in _store.list_versions(conn, d["doc_id"]):
            sp = _paths.resolve(v.get("source_path") or "")
            out(f"  源 PDF v{v['version_no']}: {'OK' if Path(sp).exists() else 'MISSING'}  {sp}")
    conn.close()
    out("\n下一步: .\\start_web.ps1   然后打开打印出来的地址")
    return 0


def cmd_quality(args) -> int:
    """译文质量审计与定点修复（translate/quality.py）。

    背景：抽取阶段会把极少数句子切在两个段里，翻译器只看半句就会产出残缺译文。
    审计用保守规则找出来，再用「带上下文重译」定点修好 —— 不需要重新 ingest，
    也不会碰其余 99.8% 的正常译文。
    """
    from core import db as _db, store as _store
    from translate import quality

    conn = open_db(args.db)
    doc_id = resolve_doc(conn, args.doc)
    v = resolve_version(conn, doc_id, args.version)
    vid = int(v["version_id"])
    out(f"=== 译文质量审计  {v.get('label', 'v' + str(v.get('version_no')))} "
        f"(doc_id={doc_id}, version_id={vid}) ===")

    items = quality.audit(conn, vid, limit=args.limit or 0)
    if not items:
        out("✅ 未发现可疑译文")
        conn.close()
        return 0

    out(f"\n发现 {len(items)} 条可疑译文：")
    for it in items:
        s = it["seg"]
        out(f"  p{s['page']} seg={s['id']} {s['kind']:10s} [{it['why']}]")
        out(f"     EN: {s['text'][:110]}")
        out(f"     ZH: {it['zh'][:110]}")

    if not args.fix:
        out("\n（只审计不改库；加 --fix 执行带上下文重译修复）")
        conn.close()
        return 0

    out("\n--- 定点修复（带上下文重译）---")
    res = quality.repair(conn, vid, items)
    out(f"✅ 修复 {res['fixed']} 条，失败 {res['failed']} 条")
    left = quality.audit(conn, vid)
    out(f"修复后剩余可疑：{len(left)} 条")
    conn.close()
    return 0


def cmd_annotate(args) -> int:
    """图内文字标注：OCR → 去重 → 翻译 → 生成标注 PDF（render/annotate*.py）。"""
    from render import annotate as _annot
    from render import annotate_build as _build

    pdf = args.pdf or _default_source_pdf()
    if not pdf or not Path(pdf).exists():
        warn(f"源 PDF 不存在：{pdf}（用 --pdf 指定，或把手册放进 origin/）")
        return 1
    out(f"源 PDF: {pdf}")

    pages = None
    if args.pages:
        rng = parse_pages(args.pages)
        pages = page_list(rng)
        out(f"指定页：{pages[:12]}{' ...' if len(pages) > 12 else ''}（共 {len(pages)} 页）")
    else:
        picked = _build.pick_pages(pdf, force=args.rescan)
        pages = [r["page"] for r in picked]
        out(f"自动选页：{len(pages)} 页需要图内标注"
            + ("（--rescan 已重新扫描）" if args.rescan else "（缓存；加 --rescan 重扫）"))

    out(f"\n=== 1/2 抽取与翻译标签（OCR dpi={args.dpi}）===")
    res = _build.build(pdf, pages, dpi=args.dpi, force=args.force, verbose=True)
    out(f"\n标签构建: 页 {res['built']}/{res['pages']}，翻译 {res['labels']} 条，"
        f"耗时 {res['seconds']}s，跳过 {res['skipped'] or '无'}")

    if args.dry_run:
        out("（--dry-run：不生成 PDF）")
        return 0

    out("\n=== 2/2 生成标注 PDF ===")
    import pymupdf
    src = pymupdf.open(pdf)
    doc = pymupdf.open()
    kept, planned, drawn = [], 0, 0
    for pno in pages:
        # ⚠️ 必须先算布局、确认这条页真的有东西要画，**再**新建页面。
        # 否则会往输出里塞进一堆"有标签数据但一条都放不下"的空白页
        # （实测 p92/p98 就是这样混进来的）。
        items = _annot.plan_annotations(src[pno - 1], pno, verbose=False)
        if not items:
            continue
        page = doc.new_page(width=src[pno - 1].rect.width,
                            height=src[pno - 1].rect.height)
        page.show_pdf_page(page.rect, src, pno - 1)
        style = getattr(args, "style", "auto")
        use = style
        if use == "auto":
            use = "capsule"
        n = 0
        if use == "numbered":
            n = _annot.draw_numbered(page, items)
            if n <= 0:                      # 对照表放不下 → 退回胶囊
                use = "capsule"
        if use == "capsule":
            n = _annot.draw_overlay(page, items)
        if n <= 0:
            doc.delete_page(doc.page_count - 1)
            continue
        kept.append(pno)
        planned += len(items)
        drawn += n
    src.close()
    if not kept:
        out("没有任何页产出了标注（可能标签都因空间不足被跳过）")
        doc.close()
        return 0
    out_path = Path(args.out) if args.out else Path("data/derived/annotated_full.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    doc.close()
    mb = out_path.stat().st_size / 1e6
    out(f"✅ 已写出：{out_path}  ({mb:.1f} MB, {len(kept)} 页)")
    out(f"  标签：计划 {planned} / 绘制 {drawn}")
    out(f"  说明：原图未改动，中文标注是**可选中的文本层**，可用 PDF 阅读器搜索中文。")
    return 0


def _default_source_pdf() -> str:
    """取「当前版本」的源 PDF（相对路径基于仓库根）。"""
    try:
        from core import db as _db, paths as _paths, store as _store
        con = _db.connect()
        for d in _store.list_documents(con):
            v = _store.current_version(con, d["doc_id"])
            if v and v.get("source_path"):
                p = _paths.resolve(v["source_path"])
                con.close()
                return p
        con.close()
    except Exception:
        pass
    p = Path("origin/BMS-Training-Manual_v2_demo.pdf")
    return str(p) if p.exists() else ""


# --------------------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli.py",
        description="manual_trans_trace —— 飞行手册翻译与版本追踪（中文 CLI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python cli.py ingest --pdf origin/BMS-Training-Manual.pdf\n"
               "  python cli.py simulate-update\n"
               "  python cli.py translate --page-to 30 --provider mock\n"
               "  python cli.py diff --from 1 --to 2\n"
               "  python cli.py export --kind bilingual\n"
               "  python cli.py serve\n")
    p.add_argument("--db", help="SQLite 路径（默认 data/app.db）")
    sub = p.add_subparsers(dest="cmd", metavar="<命令>")

    def add(name, fn, help_text, **kw):
        sp = sub.add_parser(name, help=help_text, description=help_text, **kw)
        sp.set_defaults(func=fn)
        return sp

    sp = add("ingest", cmd_ingest, "抽取 PDF → 入库为新版本")
    sp.add_argument("--pdf", required=True, help="PDF 路径")
    sp.add_argument("--slug", help="文档 slug（默认由文件名生成）")
    sp.add_argument("--title", help="文档标题（默认取 PDF 元数据）")
    sp.add_argument("--note", help="版本备注")
    sp.add_argument("--label", help="版本标签，如 v1/v2")
    sp.add_argument("--pages", help="仅抽取 a-b 页（调试）")
    sp.add_argument("--no-diff", action="store_true", help="入库后不自动 diff")
    sp.add_argument("--force", action="store_true",
                    help="已存在同内容版本时重建分段，并自动备份+回填译文（不丢译文）")
    sp.add_argument("--rebuild", action="store_true",
                    help="重建分段并丢弃该版本全部译文（必须配合 --yes-i-understand）")
    sp.add_argument("--yes-i-understand", dest="yes_i_understand", action="store_true",
                    help="确认接受 --rebuild 导致的译文丢失")

    sp = add("translate", cmd_translate, "翻译（deepseek 在线 / mock 离线占位）")
    sp.add_argument("--doc"), sp.add_argument("--version")
    sp.add_argument("--pages", help="页范围 a-b")
    sp.add_argument("--page-to", dest="pages_alt", help="等价 --pages 1-N")
    sp.add_argument("--page-from", dest="pages_from_alt")
    sp.add_argument("--limit", type=int, help="最多翻译 N 段")
    sp.add_argument("--provider", choices=["deepseek", "mock"], default=None)
    sp.add_argument("--model")
    sp.add_argument("--concurrency", type=int, default=int(config.get("concurrency", 6)))
    sp.add_argument("--force", action="store_true", help="忽略已有译文，强制重译")
    sp.add_argument("--dry-run", action="store_true", help="只估算，不调用 API")

    sp = add("diff", cmd_diff, "比较两个版本的段落差异")
    sp.add_argument("--from", dest="from_ver", required=True, help="起始版本号/标签")
    sp.add_argument("--to", dest="to_ver", required=True, help="目标版本号/标签")
    sp.add_argument("--doc")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--markdown", action="store_true", help="输出 Markdown 摘要")
    sp.add_argument("-v", "--verbose", action="store_true")

    sp = add("export", cmd_export, "导出中文 / 双语 PDF")
    sp.add_argument("--kind", choices=["cn", "bilingual"], default="cn")
    sp.add_argument("--doc"), sp.add_argument("--version")
    sp.add_argument("--pages", help="页范围 a-b")
    sp.add_argument("--out", help="输出 PDF 路径")
    sp.add_argument("--dpi", type=int, default=int(config.get("render_dpi", 110)))

    sp = add("render-page", cmd_render_page, "渲染单页为图片/PDF")
    sp.add_argument("--version"), sp.add_argument("--doc")
    sp.add_argument("--page", type=int, required=True)
    sp.add_argument("--kind", choices=["source", "cn", "bilingual"], default="source")
    sp.add_argument("--out", required=True)
    sp.add_argument("--dpi", type=int, default=int(config.get("render_dpi", 110)))

    sp = add("serve", cmd_serve, "启动 Web 服务（双栏对照浏览器）")
    sp.add_argument("--host"), sp.add_argument("--port", type=int)
    sp.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    sp.add_argument("--reload", action="store_true")

    sp = add("list", cmd_list, "列出文档与当前版本")
    sp = add("status", cmd_status, "查看版本/段数/翻译进度/变更统计")
    sp.add_argument("--doc")

    sp = add("simulate-update", cmd_simulate_update,
             "根据 v1 生成演示用 v2 PDF（删除/插入/修改/移动），自动入库并比对 diff")
    sp.add_argument("--pdf", help="源 PDF（默认 origin/BMS-Training-Manual.pdf）")
    sp.add_argument("--out", help="输出 PDF（默认 origin/BMS-Training-Manual_v2_demo.pdf）")
    sp.add_argument("--doc", help="目标文档（默认唯一文档）")
    sp.add_argument("--label", default=None, help="新版本标签（默认自动 v2/v3…）")
    sp.add_argument("--page-from", dest="page_from", type=int, default=4)
    sp.add_argument("--page-to", dest="page_to", type=int, default=40)

    sp = add("cost-estimate", cmd_cost_estimate, "估算翻译 token 与费用（不调用 API）")
    sp.add_argument("--doc"), sp.add_argument("--version")
    sp.add_argument("--pages", help="页范围 a-b")
    sp.add_argument("--provider", default="deepseek", choices=["deepseek", "mock"])
    sp.add_argument("--model")

    sp = add("restore-translations", cmd_restore_translations,
             "重建分段后按 fingerprint 回填译文（含形状一致性校验，防止长译文塞进小格子）")
    sp.add_argument("--doc"), sp.add_argument("--version", help="目标版本（默认当前版本）")
    sp.add_argument("--backup", help="备份 JSON 路径（默认取库内现有译文）")
    sp.add_argument("--from-version", dest="from_version", help="从该版本导出译文再回填")
    sp.add_argument("--min-en-chars", dest="min_en_chars", type=int, default=8,
                    help="形状校验：原文至少这么多字符才允许回填（默认 8）")
    sp.add_argument("--no-shape-check", dest="no_shape_check", action="store_true",
                    help="关闭形状一致性校验（不推荐）")
    sp.add_argument("--clean", action="store_true",
                    help="顺便清理无中文/mock 占位（〔〕）译文")

    sp = add("portable", cmd_portable,
             "把仓库做成可复制到别的机器/目录的形式（DB 路径改相对 + 体检清单）")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只演练不写库")
    sp.add_argument("--check", action="store_true", default=True,
                    help="附带体检与复制清单（默认开）")

    add("setup", cmd_setup, "在目标机器上自检环境与仓库（复制过来后先跑这个）")

    sp = add("annotate", cmd_annotate,
             "图内文字标注：OCR 图片里的标注文字 → 翻译 → 生成带中文标签的 PDF（原图不改）")
    sp.add_argument("--pdf", help="源 PDF（默认取当前版本的源文件）")
    sp.add_argument("--pages", help="只处理指定页，如 11 或 114-117（默认自动选页）")
    sp.add_argument("--dpi", type=int, default=300, help="OCR 渲染精度（默认 300）")
    sp.add_argument("--out", help="输出 PDF（默认 data/derived/annotated_full.pdf）")
    sp.add_argument("--force", action="store_true", help="忽略 OCR/翻译缓存，全部重跑")
    sp.add_argument("--rescan", action="store_true", help="重新扫描全库判定哪些页需要标注")
    sp.add_argument("--style", default="auto", choices=["auto", "capsule", "numbered"],
                    help="标注风格：capsule=就近中文胶囊（默认，不遮挡）；"
                         "numbered=图上圈号+页边对照表（适合图四周留白大的页）")
    sp.add_argument("--dry-run", action="store_true", help="只抽取与翻译标签，不生成 PDF")

    sp = add("quality", cmd_quality,
             "译文质量审计：找残缺/异常译文，并可用 --fix 定点重译修复")
    sp.add_argument("--doc"), sp.add_argument("--version")
    sp.add_argument("--limit", type=int, help="最多处理 N 条（调试）")
    sp.add_argument("--fix", action="store_true",
                    help="执行定点修复（默认只审计、不动库）")

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    # --page-from/--page-to 兼容写法
    if getattr(args, "pages_from_alt", None) or getattr(args, "pages_alt", None):
        a = args.pages_from_alt or 1
        b = args.pages_alt or a
        args.pages = f"{a}-{b}"
    try:
        return int(args.func(args) or 0)
    except SystemExit as exc:
        # SystemExit 的 code 可能是**字符串消息**（例如多文档时要求 --doc 指定），
        # 直接 int() 会抛 ValueError 把真实提示盖掉。
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(f"❌ {code}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        fail("已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        import traceback
        if os.environ.get("MT_DEBUG"):
            traceback.print_exc()
        fail(f"{exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
