#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_pipeline —— 无人值守归纳（OpenAI 兼容 API）

流程：un-summarized 单位 → LLM 逐篇 JSON 摘要 → 写 _summaries → 校验/状态 → archive → digest → tracking
配置（优先级）：README：
  1) READDAILY_LLM_CONFIG 指向的 JSON（默认 <data_root>/llm.json）
     {"base_url": "...", "api_key": "...", "model": "..."}
  2) 环境变量 READDAILY_LLM_BASE_URL / READDAILY_LLM_API_KEY / READDAILY_LLM_MODEL
密钥只存本机（llm.json chmod 600），不入仓库。
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.normpath(os.path.join(ROOT, "newspaper-fetch", "scripts")))
from lib import archive_paths, load_json, save_json, state_mark, log_line  # noqa: E402

ARCHIVE = os.environ.get("READDAILY_ARCHIVE") or os.path.expanduser(
    "~/Library/Application Support/readdaily/news-archive")
VAULT = os.environ.get("READDAILY_VAULT") or os.path.expanduser(
    "~/Library/Application Support/readdaily/vault")

def vault_override(cfg=None):
    global VAULT
    if os.environ.get("READDAILY_VAULT"):
        return VAULT
    cfg = cfg or {}
    v = cfg.get("vault") or {}
    if v:
        VAULT = os.path.expanduser(v)
        os.environ["READDAILY_VAULT"] = VAULT  # 传给子进程（tracking 等）
    return VAULT
DAILY_LOG = ARCHIVE + "/_dailylog.jsonl"
TAGS = ["政治", "经济", "军事", "民生", "生产", "科技", "文化", "生态", "其他"]


def llm_config():
    cfg_path = os.environ.get("READDAILY_LLM_CONFIG") or os.path.join(ARCHIVE, "llm.json")
    cfg = {}
    if os.path.exists(cfg_path):
        cfg = load_json(cfg_path, {}) or {}
    base = (cfg.get("base_url") or os.environ.get("READDAILY_LLM_BASE_URL")
            or "https://api.deepseek.com/v1")
    key = (cfg.get("api_key") or os.environ.get("READDAILY_LLM_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY") or "")
    model = (cfg.get("model") or os.environ.get("READDAILY_LLM_MODEL") or "deepseek-chat")
    vault_override(cfg)
    return base.rstrip("/") + "/chat/completions", key, model


SYS_PROMPT = (
    "你是读报系统的新闻归纳引擎。对给定报纸文章文本，输出**严格 JSON 对象**（不要输出任何其他文字或代码块）："
    '{"summary":"中文摘要100-150字，事实导向，含关键数据与变化信号",'
    '"category":["政治","经济","军事","民生","生产","科技","文化","生态","其他"中选择1-2个],'
    '"entities":["机构/政策/行业/事件/人物 等主体名，2-5个"],'
    '"importance":1到5的整数,"keywords":["3-5个关键词"]}')


def chat(url, key, model, messages, temperature=0.3, use_json=True):
    import requests
    r = requests.post(url, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=120,
        json={"model": model, "messages": messages, "temperature": temperature,
              **({"response_format": {"type": "json_object"}} if use_json else {}),
              "max_tokens": 700})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    body = r.json()
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return content, None


def parse_json(content):
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    return json.loads(m.group(0))


def summarize_unit(url, key, model, src, edition, title, text):
    prompt = (f"【来源】{src} {edition}\n【标题】{title or '（无标题）'}\n\n"
              f"{text[:4500]}")
    for attempt in range(3):
        content, err = chat(url, key, model, [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": prompt}])
        if err:
            time.sleep(4 * (attempt + 1))
            continue
        try:
            d = parse_json(content)
            if not d or not d.get("summary") or len(d["summary"]) < 30:
                raise ValueError("summary 过短/缺失")
            cats = [c for c in d.get("category", []) if c in TAGS] or ["其他"]
            return {"summary": d["summary"][:400],
                    "category": cats[:3],
                    "entities": [str(e)[:40] for e in (d.get("entities") or [])[:6]],
                    "importance": int(d.get("importance") or 3),
                    "keywords": [str(k)[:24] for k in (d.get("keywords") or [])[:6]]}, None
        except Exception as e:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return None, f"LLM 三次失败（{src} {edition} {title[:20]}）"


def summarize(args):
    d = (datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
         if args.date else datetime.date.today())
    url, key, model = llm_config()
    if not key:
        print("!! 缺少 API Key：请配置 READDAILY_LLM_CONFIG / llm.json 或 DEEPSEEK_API_KEY")
        return 2
    jobs = []
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        st = load_json(archive_paths(ARCHIVE, src, d)["state"], {})
        if (st.get("stages") or {}).get("summarized"):
            continue
        issue = load_json(os.path.join(issue_dir, "issue.json"), {})
        for u in issue.get("units", []):
            docs = []
            if u.get("text"):
                docs.append(u["text"])
            for a in (u.get("articles") or []):
                if a.get("text"):
                    docs.append(a["text"])
            tp = u.get("text_path")
            if tp and os.path.exists(os.path.join(issue_dir, tp)):
                docs.append(open(os.path.join(issue_dir, tp), encoding="utf-8").read())
            text = "\n\n".join(docs)
            if len(text) >= 80:
                jobs.append({"src": src, "edition": u["title"], "title": u["title"],
                             "text": text, "id": u["id"]})
    print(f"待归纳 {len(jobs)} 单位（{d}）")
    done, failed = [], 0
    for n, jb in enumerate(jobs, 1):
        res, err = summarize_unit(url, key, model, jb["src"], jb["edition"],
                                  jb["title"], jb["text"])
        if err:
            print("  ✗", err)
            failed += 1
            continue
        done.append({"id": jb["id"], **res})
        if n % 5 == 0 or n == len(jobs):
            print(f"  进度 {n}/{len(jobs)}（失败 {failed}）")
    # 按源聚合写入
    by_src = {}
    for it in done:
        by_src.setdefault(it["id"].split("_")[0], []).append(it)
    for src, units in by_src.items():
        save_json(archive_paths(ARCHIVE, src, d)["summaries"], {"units": units})
        # 注入 issue.json（digest/archiver/tracking 均读 issue.json.units[].summary）
        issue = load_json(os.path.join(ARCHIVE, src, d.isoformat(), "issue.json"), {})
        by_id = {u["id"]: u for u in units}
        for unit in issue.get("units", []):
            if unit["id"] in by_id and not unit.get("summary"):
                unit["summary"] = {k: v for k, v in by_id[unit["id"]].items() if k != "id"}
        save_json(os.path.join(ARCHIVE, src, d.isoformat(), "issue.json"), issue)
        state_mark(archive_paths(ARCHIVE, src, d)["state"], "summarized", units=len(units))
        log_line(DAILY_LOG, {"source": src, "date": d.isoformat(), "stage": "summarized",
                             "units": len(units), "mode": "llm-api"})
    print(f"完成：{len(done)} 篇入库，失败 {failed}")
    return 0 if failed == 0 else 1


def digest(args):
    d = (datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
         if args.date else datetime.date.today())
    url, key, model = llm_config()
    bundle = []
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        src = os.path.basename(os.path.dirname(issue_dir))
        issue = load_json(os.path.join(issue_dir, "issue.json"), {})
        for u in issue.get("units", []):
            sm = (u.get("summary") or {})
            if sm:
                bundle.append({"source": issue.get("source_name", src),
                               "edition": u["title"],
                               "summary": sm.get("summary", ""),
                               "category": sm.get("category", []),
                               "entities": sm.get("entities", [])})
    if not bundle:
        print("无摘要，跳过 digest")
        return 0
    data = json.dumps(bundle[:120], ensure_ascii=False)
    sys_prompt = ("你是读报编辑。给定当日各报摘要列表，输出**每日新闻摘要 Markdown**（≤900字）："
                  "# {date} 全国新闻摘要\n> 数据源：{源列表}｜条目 {N}\n"
                  "## 主题块（3-6 个，按主题聚类）：每条含 **要点/信号/待观察**；"
                  "## 实体热点（按出现次数）；## 今日一句话（跨源综合判断）")
    content, err = chat(url, key, model, [
        {"role": "system", "content": sys_prompt.replace("{date}", d.isoformat())},
        {"role": "user", "content": data}], temperature=0.4, use_json=False)
    if err or not content:
        print("digest 失败:", err)
        return 1
    md = re.sub(r"^```[a-z]*\n|```$", "", content.strip(), flags=re.M)
    os.makedirs(os.path.join(VAULT, "每日摘要"), exist_ok=True)
    p = os.path.join(VAULT, "每日摘要", f"{d.isoformat()}_全国新闻摘要.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md if md.startswith("#") else f"# {d.isoformat()} 全国新闻摘要\n\n{md}")
    log_line(DAILY_LOG, {"date": d.isoformat(), "stage": "digest", "file": p, "mode": "llm-api"})
    print("digest ->", p)
    return 0


def top_entities(args, n=5):
    d = (datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
         if args.date else datetime.date.today())
    freq = {}
    for issue_dir in sorted(glob.glob(os.path.join(ARCHIVE, "*", d.isoformat()))):
        issue = load_json(os.path.join(issue_dir, "issue.json"), {})
        for u in issue.get("units", []):
            for e in (u.get("summary") or {}).get("entities", []):
                freq[e] = freq.get(e, 0) + 1
    return [e for e, _ in sorted(freq.items(), key=lambda x: -x[1])[:n]]


def main():
    ap = argparse.ArgumentParser(description="readdaily LLM 无人值守管线")
    ap.add_argument("cmd", choices=["summarize", "digest", "all"])
    ap.add_argument("--date", default=None)
    ap.add_argument("--track", type=int, default=5, help="auto tracking 主体数")
    args = ap.parse_args()
    d = args.date or datetime.date.today().isoformat()
    if args.cmd == "summarize":
        sys.exit(summarize(args))
    if args.cmd == "digest":
        sys.exit(digest(args))
    # all：抓取（幂等）→ 归纳 → 摘要 → 跟踪
    rc = subprocess.run([sys.executable,
                         os.path.normpath(os.path.join(ROOT, "newspaper-fetch", "scripts", "fetch.py")),
                         "--date", d, "--stage", "fetched,parsed"]).returncode
    rc = summarize(args) or rc
    if rc:
        print("归纳未全过，跳过 digest/tracking")
        return rc
    rc = digest(args) or rc
    ents = top_entities(args, args.track)
    for e in ents:
        subprocess.run([sys.executable, os.path.normpath(os.path.join(
            HERE, "reader.py")), "tracking", "--entity", e, "--date", d])
    print("all 完成 | 跟踪主体:", ents)
    return rc


if __name__ == "__main__":
    sys.exit(main())
