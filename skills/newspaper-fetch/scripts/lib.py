#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newspaper-fetch 公共库：HTTP/状态机/校验/日志/路径。"""
import datetime
import glob
import json
import os
import re
import sys
import time

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
STAGES = ["fetched", "parsed", "summarized", "archived", "tracked"]

try:
    import requests
    _S = requests.Session()
    _S.headers["User-Agent"] = UA

    def http_get(url, referer=None, timeout=30, cookies=None):
        h = {"Referer": referer} if referer else {}
        r = _S.get(url, headers=h, timeout=timeout, cookies=cookies)
        return r.status_code, r.url, r.content
except ImportError:  # pragma: no cover
    import http.cookiejar
    import urllib.request
    _CJ = http.cookiejar.CookieJar()
    _OP = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_CJ))
    _OP.addheaders = [("User-Agent", UA)]

    def http_get(url, referer=None, timeout=30, cookies=None):
        h = {"Referer": referer} if referer else {}
        req = urllib.request.Request(url, headers=h)
        with _OP.open(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.read()


def html_text(raw, enc_candidates=("utf-8", "gb18030", "gbk")):
    """按候选编码解码 HTML。"""
    for enc in enc_candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def norm_day(d):
    if isinstance(d, datetime.date):
        return d
    return datetime.datetime.strptime(str(d), "%Y-%m-%d").date()


def archive_paths(archive_root, source_id, d):
    d = norm_day(d)
    root = os.path.expanduser(archive_root)
    issue_dir = os.path.join(root, source_id, d.isoformat())
    return {
        "root": root,
        "dir": issue_dir,
        "pages": os.path.join(issue_dir, "pages"),
        "text": os.path.join(issue_dir, "text"),
        "issue_json": os.path.join(issue_dir, "issue.json"),
        "state": os.path.join(root, "_state", source_id, f"{d.isoformat()}.json"),
        "summaries": os.path.join(root, "_summaries", source_id, f"{d.isoformat()}.json"),
    }


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def state_has(state, stage):
    return bool(state and state.get("stages", {}).get(stage))


def state_mark(state_path, stage, **extra):
    st = load_json(state_path, {}) or {}
    st.setdefault("stages", {})[stage] = datetime.datetime.now().isoformat(timespec="seconds")
    st.update(extra)
    save_json(state_path, st)
    return st


def chain_check(archive_root, source_id, d, issue_no):
    """期号连续性校验：与「上一期」比较（差 1 或空）。返回 (ok, 提示)。"""
    d = norm_day(d)
    prev = d - datetime.timedelta(days=1)
    pj = os.path.join(os.path.expanduser(archive_root), source_id,
                      prev.isoformat(), "issue.json")
    meta = load_json(pj)
    if not meta or not meta.get("issue_no") or not issue_no:
        return True, "无上一期或期号缺失，跳过连续性校验"
    try:
        diff = int(issue_no) - int(meta["issue_no"])
    except (TypeError, ValueError):
        return True, "期号非数字，跳过"
    if diff == 1:
        return True, f"期号连续（{meta['issue_no']}→{issue_no}）"
    return False, f"期号不连续：上一期 {meta['issue_no']}，本期 {issue_no}（差 {diff}，可能缺刊）"


def log_line(path, entry):
    entry.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("[log]", json.dumps(entry, ensure_ascii=False)[:300])


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s)).strip("_")[:80]


def unique_issue_no(issue):
    return issue.get("issue_no")
