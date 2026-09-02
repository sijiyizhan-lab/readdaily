#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newspaper-fetch 编排器：注册表驱动，逐源逐期执行 fetched→parsed 两段状态。

用法：
  python3 fetch.py --date 2026-09-02                 # 全部启用源
  python3 fetch.py --date 2026-09-02 --source zgjsb  # 指定源
  python3 fetch.py --date 2026-09-02 --stage parsed  # 只做解析（OCR 等）
  python3 fetch.py --probe gmrb --date 2026-09-02    # 方正渠道模式探测
"""
import argparse
import datetime
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib  # noqa: E402

REGISTRY = os.path.expanduser("~/.agents/skills/newspaper-fetch/sources.json")
DAILY_LOG = os.path.expanduser("~/Library/Application Support/readdaily/news-archive/_dailylog.jsonl")


def load_registry(path=REGISTRY):
    reg = lib.load_json(path)
    if not reg:
        sys.exit("注册表缺失")
    return reg


def load_adapter(channel):
    return importlib.import_module(f"adapters.{channel}")


def main():
    ap = argparse.ArgumentParser(description="newspaper-fetch 编排器")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--source", default=None, help="指定源 id（可逗号）")
    ap.add_argument("--stage", default="fetched,parsed",
                    help="fetched,parsed（可逗号组合）")
    ap.add_argument("--offline", action="store_true", help="微信渠道离线回退（不搜索）")
    ap.add_argument("--probe", default=None, help="方正/PDF 渠道模式探测（给 source id）")
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--no-state-skip", action="store_true", help="忽略状态机直接重跑")
    args = ap.parse_args()

    reg = load_registry(args.registry)
    root = os.path.expanduser(reg.get("archive_root", "~/Downloads/news-archive"))
    d = lib.norm_day(args.date or datetime.date.today())
    want = [s for s in reg["sources"]
            if (args.source and s["id"] in args.source.split(",")) or (not args.source and s.get("enabled"))]

    if args.probe:
        src = next((s for s in reg["sources"] if s["id"] == args.probe), None)
        if not src:
            sys.exit("无此源")
        ad = load_adapter(src["channel"])
        res = ad.probe(src, d)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return

    for src in want:
        sid = src["id"]
        ad = load_adapter(src["channel"])
        print(f"\n=== [{sid}] {src['name']} {d} ===")
        if not getattr(ad, "acquire", None) and src["channel"] == "wechat_read":
            aps = lib.archive_paths(root, sid, d)
            if lib.state_has(lib.load_json(aps["state"]), "fetched") and not args.no_state_skip:
                print("  state=fetched 已存在，跳过")
                continue
        if "fetched" in args.stage.split(","):
            aps = lib.archive_paths(root, sid, d)
            if (lib.state_has(lib.load_json(aps["state"]), "fetched") and not args.no_state_skip
                    and not getattr(ad, "acquire", None)):
                print("  fetched 已完成，跳过")
            else:
                if callable(getattr(ad, "acquire", None)):
                    if src["channel"] == "wechat_read":
                        ok, note = ad.acquire(src, d, root, offline_ok=args.offline)
                        print("  acquire:", note)
                    if not ok and args.offline:
                        st = lib.state_mark(aps["state"], "failed", note=note)
                        continue
                issue, err = ad.fetch(src, d, root)
                if err:
                    print("  fetch 失败:", err)
                    lib.state_mark(aps["state"], "failed", note=err)
                    continue
                ok, chain = lib.chain_check(root, sid, d, issue.get("issue_no"))
                print("  fetch ok;", "👍" if ok else "⚠️", chain)
                st = lib.state_mark(aps["state"], "fetched", edition_no=len(issue.get("editions", [])))
                lib.log_line(os.path.expanduser(DAILY_LOG), {
                    "source": sid, "source_name": src["name"], "date": d.isoformat(),
                    "stage": "fetched", "editions": len(issue.get("editions", [])),
                    "issue_no": issue.get("issue_no"), "chain_ok": ok,
                    "issue_json": aps["issue_json"]})
        if "parsed" in args.stage.split(","):
            aps = lib.archive_paths(root, sid, d)
            if lib.state_has(lib.load_json(aps["state"]), "parsed") and not args.no_state_skip:
                print("  parsed 已完成，跳过")
                continue
            issue, err = ad.parse(src, d, root)
            if err:
                print("  parse 失败:", err)
                continue
            n = len(issue.get("units", []))
            lib.state_mark(aps["state"], "parsed", units=n)
            lib.log_line(os.path.expanduser(DAILY_LOG), {
                "source": sid, "source_name": src["name"], "date": d.isoformat(),
                "stage": "parsed", "units": n, "issue_json": aps["issue_json"]})
            print("  parsed ok:", n, "units")

    print("\n完成")


if __name__ == "__main__":
    main()
