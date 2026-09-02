#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reader 工具集：prepare（工作清单）/ ingest（摘要入库+校验）/ archiver / digest / tracking。

用法：
  python3 reader.py prepare --date 2026-09-02        # 列出未归纳单位（我读文本用）
  python3 reader.py ingest  --date 2026-09-02        # 校验 _summaries/*/*.json 并置 summarized → archive
  python3 reader.py digest  --date 2026-09-02        # 汇总当日各源摘要（供撰写每日摘要）
  python3 reader.py tracking --entity 城市地下管网 --date 2026-09-02   # 主体档案追加
  python3 reader.py archive  --date 2026-09-02       # 重跑 Obsidian 归档
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "newspaper-fetch", "scripts")))
from lib import (archive_paths, load_json, save_json, state_mark,  # noqa: E402
                 norm_day, log_line)

ARCHIVE = os.environ.get("READDAILY_ARCHIVE") or os.path.expanduser(
    "~/Library/Application Support/readdaily/news-archive")
VAULT = os.environ.get("READDAILY_VAULT") or os.path.expanduser(
    "~/Library/Application Support/readdaily/vault")
_SETTINGS = os.path.expanduser("~/Library/Application Support/readdaily/settings.json")
if not os.environ.get("READDAILY_VAULT") and os.path.exists(_SETTINGS):
    _v = (load_json(_SETTINGS, {}) or {}).get("vault")
    if _v:
        VAULT = os.path.expanduser(_v)
TAGS = ["政治", "经济", "军事", "民生", "生产", "科技", "文化", "生态", "其他"]
DAILY_LOG = ((os.environ.get("READDAILY_ARCHIVE")
              or os.path.expanduser("~/Library/Application Support/readdaily/news-archive"))
             + "/_dailylog.jsonl")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s)).strip("_")[:80]


def issues_of(d, only_unsummarized=True):
    """返回该日所有源的 issue 路径 + 状态。"""
    out = []
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        issue = load_json(os.path.join(issue_dir, "issue.json"))
        if not issue:
            continue
        st = load_json(archive_paths(ARCHIVE, src, d)["state"], {})
        out.append({"source": src, "issue": issue, "state": st})
    if only_unsummarized:
        out = [x for x in out if not (x["state"].get("stages") or {}).get("summarized")]
    return out


def read_unit_text(aunit, issue_dir):
    tp = aunit.get("text_path")
    if not tp:
        # 内嵌文本渠道（founder/paper_api 等：unit.text + articles[].text）
        parts = []
        if aunit.get("text"):
            parts.append(aunit["text"])
        for a in (aunit.get("articles") or []):
            if a.get("text"):
                parts.append(a["text"])
        return "\n\n".join(parts)
    p = os.path.join(issue_dir, tp)
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return ""


def cmd_prepare(args):
    d = norm_day(args.date)
    for x in issues_of(d):
        issue = x["issue"]
        i_dir = os.path.join(ARCHIVE, issue["source"], issue["date"])
        print(f"\n## {issue['source_name']} ({issue['source']}) {issue['date']}"
              f" 第{issue.get('issue_no')}期")
        for u in issue.get("units", []):
            txt = read_unit_text(u, i_dir)
            print(f"- [{u['id']}] {u['title']} ({len(txt)}字) -> {u.get('text_path')}")


def cmd_ingest(args):
    d = norm_day(args.date)
    n_units = 0
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        sums = load_json(archive_paths(ARCHIVE, src, d)["summaries"], {})
        units = (sums or {}).get("units", [])
        if not units:
            continue
        ids = set(u["id"] for u in units)
        issue = load_json(os.path.join(issue_dir, "issue.json"))
        for u in issue.get("units", []):
            if u["id"] in ids:
                u["summary"] = next(x for x in units if x["id"] == u["id"])
        # 校验
        bad = []
        for s in units:
            if not s.get("summary") or len(s["summary"]) < 30:
                bad.append(f"{s['id']} summary过短")
            if not set(s.get("category", [])) & set(TAGS):
                bad.append(f"{s['id']} category非法")
        if bad:
            print(f"[{src}] 校验未过:{bad}")
            continue
        save_json(os.path.join(issue_dir, "issue.json"), issue)
        st = state_mark(archive_paths(ARCHIVE, src, d)["state"], "summarized",
                        units=len(units))
        log_line(os.path.expanduser(DAILY_LOG), {"source": src, "date": d.isoformat(),
                                                 "stage": "summarized", "units": len(units)})
        n_units += len(units)
    print("ingested units:", n_units)


def cmd_archive(args):
    d = norm_day(args.date)
    vault_src = os.path.join(VAULT, "报纸原文", d.isoformat())
    existing = set()
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        issue = load_json(os.path.join(issue_dir, "issue.json"))
        folder = os.path.join(vault_src, issue["source_name"])
        os.makedirs(folder, exist_ok=True)
        for ed in issue.get("editions", []):
            unit = next((u for u in issue.get("units", [])
                         if u.get("page_image") == ed.get("page_image")), None)
            sm = (unit or {}).get("summary") or {}
            text = ""
            if unit:
                text = read_unit_text(unit, os.path.dirname(
                    os.path.join(ARCHIVE, src, d.isoformat())))
            fname = f"{ed['no']:02d}版_{safe_name(ed['name'])}.md"
            fpath = os.path.join(folder, fname)
            fm = {
                "date": d.isoformat(), "source": issue["source_name"],
                "issue_no": issue.get("issue_no"), "edition": f"{ed['no']}版 {ed['name']}",
                "category": sm.get("category", []), "entities": sm.get("entities", []),
                "importance": sm.get("importance"), "keywords": sm.get("keywords", []),
                "url": issue.get("url", ""),
            }
            body = f"# {ed['no']}版 {ed['name']}\n\n"
            body += f"> {sm.get('summary', '')}\n\n"
            body += f"> 版面图：`{issue['source']}/{d.isoformat()}/pages/...`（见 ~/Downloads/news-archive）\n\n"
            body += "---\n\n" + text[:8000]
            ys = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                                     if not isinstance(v, (str, int, type(None)))
                                     else f"{k}: {v if v is not None else ''}"
                                     for k, v in fm.items()) + "\n---\n"
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(ys + body)
            existing.add(os.path.relpath(fpath, VAULT))
    dg = os.path.join(VAULT, "每日摘要", f"{d.isoformat()}_全国新闻摘要.md")
    print("archived:", len(existing))
    if os.path.exists(dg):
        print("每日摘要已存在:", dg)


def cmd_digest(args):
    d = norm_day(args.date)
    bundle = []
    cats, ents = {}, {}
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        issue = load_json(os.path.join(issue_dir, "issue.json"))
        for u in issue.get("units", []):
            sm = (u.get("summary") or {})
            if not sm:
                continue
            bundle.append({"source": issue["source_name"], "edition": u["title"],
                           "summary": sm.get("summary", ""),
                           "category": sm.get("category", []),
                           "entities": sm.get("entities", []),
                           "importance": sm.get("importance")})
            for c in sm.get("category", []):
                cats[c] = cats.get(c, 0) + 1
            for e in sm.get("entities", []):
                ents[e] = ents.get(e, 0) + 1
    print(json.dumps({"date": d.isoformat(), "units": len(bundle),
                      "categories": cats, "entities_top": sorted(
                          ents.items(), key=lambda x: -x[1])[:15],
                      "bundle": bundle}, ensure_ascii=False, indent=1))
    print("\n提示：据上述 info 撰写《每日摘要》（模板见 vault/_templates/每日摘要模板.md）")


def cmd_tracking(args):
    d = norm_day(args.date)
    ent = args.entity
    folder = os.path.join(VAULT, "主体跟踪")
    os.makedirs(folder, exist_ok=True)
    fpath = os.path.join(folder, f"{ent}_档案.md")
    # 收集该实体的今日条目
    entries = []
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        issue = load_json(os.path.join(issue_dir, "issue.json"))
        for u in issue.get("units", []):
            sm = u.get("summary") or {}
            if ent in sm.get("entities", []):
                entries.append({"source": issue["source_name"], "edition": u["title"],
                                "summary": sm.get("summary", "")})
    if not os.path.exists(fpath):
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("---\ntype: 主体档案\ncreated: %s\ntracked: true\n---\n\n"
                    f"# {ent} 档案\n\n## 背景\n\n（待补充：主体背景/关注维度）\n\n"
                    "## 时间线\n\n## 宏观趋势\n\n（每次更新后写趋势判断）\n\n"
                    "## 微观场景\n\n（城市/企业/个体实例）\n" % d.isoformat())
    with open(fpath, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(f"\n- **{d.isoformat()}** 【{e['source']}·{e['edition']}】{e['summary']}\n")
    print("tracked:", ent, "entries:", len(entries), "->", fpath)


def main():
    ap = argparse.ArgumentParser(description="newspaper-reader 工具")
    ap.add_argument("cmd", choices=["prepare", "ingest", "archive", "digest", "tracking"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--entity", default=None)
    args = ap.parse_args()
    {"prepare": cmd_prepare, "ingest": cmd_ingest, "archive": cmd_archive,
     "digest": cmd_digest, "tracking": cmd_tracking}[args.cmd](args)


if __name__ == "__main__":
    main()
