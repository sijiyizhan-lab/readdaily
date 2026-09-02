#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""readdaily —— 多报每日读报系统 CLI（GitHub 发行版公共入口）

子命令：
  fetch                      抓取当日（默认全部启用源）  --date/--source/--stage/--offline
  prepare / ingest / archive  归纳流水线（Agent 配合）
  digest                     当日摘要汇总（供撰写每日摘要）
  tracking --entity 城市地下管网  主体档案追加
  query "城市更新"            全文检索（跨源跨版，输出命中清单）
  status                     各源状态与产出规模
环境变量（可选）：
  READDAILY_ARCHIVE  数据根（默认 ~/Library/Application Support/readdaily/news-archive）
  READDAILY_VAULT    Obsidian vault（默认 ~/Library/Application Support/readdaily/vault）
  READDAILY_VOCR     OCR 二进制（默认 skills/newspaper-fetch/bin/vocr）
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FETCHER = os.path.join(REPO, "skills", "newspaper-fetch", "scripts", "fetch.py")
READER = os.path.join(REPO, "skills", "newspaper-reader", "scripts", "reader.py")
REGISTRY = os.path.join(REPO, "skills", "newspaper-fetch", "sources.json")
ARCHIVE = os.environ.get("READDAILY_ARCHIVE") or os.path.expanduser(
    "~/Library/Application Support/readdaily/news-archive")
VAULT = os.environ.get("READDAILY_VAULT") or os.path.expanduser(
    "~/Library/Application Support/readdaily/vault")


def run(script, *args):
    cmd = [sys.executable, script] + list(args)
    p = subprocess.run(cmd)
    return p.returncode


def cmd_fetch(args):
    cmd = [FETCHER]
    if args.date:
        cmd += ["--date", args.date]
    if args.source:
        cmd += ["--source", args.source]
    if args.stage:
        cmd += ["--stage", args.stage]
    if args.offline:
        cmd += ["--offline"]
    return subprocess.run(cmd).returncode


def cmd_reader(args):
    return run(READER, args.cmd2, *(["--date", args.date] if args.date else []),
               *(["--entity", args.entity] if args.entity else []))


def cmd_status(args):
    d = args.date or datetime.date.today().isoformat()
    print(f"== 读报状态 @ {d} ==")
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    for s in reg["sources"]:
        st = os.path.join(ARCHIVE, "_state", s["id"], f"{d}.json")
        stages = list((json.load(open(st)).get("stages") or {}).keys()) if os.path.exists(st) else []
        mark = "✅" if (s.get("enabled") and stages) else ("⛔" if not s.get("enabled") else "⏳")
        print(f"  {mark} {s['name']:<6} {s['status']:<10} {stages}")
    issues = glob.glob(os.path.join(ARCHIVE, "*", d, "issue.json"))
    total = 0
    for p in issues:
        i = json.load(open(p))
        total += sum(len(u.get("text", "")) for u in i.get("units", []))
    print(f"\n  当日期数 {len(issues)}｜正文约 {total // 1000}K 字")
    return 0


def cmd_query(args):
    kw = args.keyword
    days = args.days
    dates = sorted({f"{args.date}"} if args.date else
                   {(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                    for i in range(days)}, reverse=True)
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    names = {s["id"]: s["name"] for s in reg["sources"]}
    hits = []
    for day in dates:
        for sid in names:
            d = os.path.join(ARCHIVE, sid, day)
            if not os.path.exists(os.path.join(d, "issue.json")):
                continue
            issue = json.load(open(os.path.join(d, "issue.json"), encoding="utf-8"))
            for u in issue.get("units", []):
                docs = []
                if u.get("text"):
                    docs.append((u["title"], u["text"]))
                for a in (u.get("articles") or []):
                    docs.append((a.get("title", ""), a.get("text", "") or ""))
                tp = u.get("text_path")
                if tp and os.path.exists(os.path.join(d, tp)):
                    docs.append((u["title"], open(os.path.join(d, tp), encoding="utf-8").read()))
                for t, x in docs:
                    if kw in x:
                        hits.append({"day": day, "src": issue["source_name"],
                                     "edition": u["title"], "title": t[:70],
                                     "text": x[:6000]})
    print(f"「{kw}」命中 {len(hits)} 条（{', '.join(dates)}）\n")
    seen = set()
    for n, h in enumerate(hits, 1):
        key = (h["day"], h["src"], h["edition"])
        if key in seen:
            continue
        seen.add(key)
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", h["text"]))
        i = t.find(kw)
        print(f"[{n}] {h['src']} {h['day']} {h['edition']} | {h['title']}")
        print(f"    …{t[max(0, i - 70):i + 140]}…\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description="readdaily CLI")
    ap.add_argument("cmd", choices=["fetch", "prepare", "ingest", "archive", "digest",
                                    "tracking", "query", "status"])
    ap.add_argument("cmd2", nargs="?", default=None, help="reader 子命令（prepare/ingest/…）")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--source", default=None, help="源 id（逗号分隔）")
    ap.add_argument("--stage", default=None, help="fetched,parsed")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--entity", default=None, help="tracking 主体")
    ap.add_argument("--keyword", default=None, help="query 关键词")
    ap.add_argument("--days", type=int, default=2, help="query 回溯天数")
    args = ap.parse_args()

    if args.cmd == "fetch":
        sys.exit(cmd_fetch(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    if args.cmd == "query":
        args.keyword = args.keyword or args.cmd2
        if not args.keyword:
            print("用法: readdaily query \"关键词\" [--days N] [--date YYYY-MM-DD]")
            return 2
        sys.exit(cmd_query(args))
    if args.cmd == "tracking":
        if not args.entity:
            print("用法: readdaily tracking --entity 主体名 --date YYYY-MM-DD")
            return 2
        sys.exit(cmd_reader(args))
    # reader 流水线
    sys.exit(cmd_reader(args))


if __name__ == "__main__":
    main()
