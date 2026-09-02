#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：微信「读报」渠道（示例：中国建设报）。

采集复用 jianshebao-daily 的已验证引擎（搜狗定位→src=11→版面图+电子报 PDF），
归一化输出统一 issue.json；版面文本用 Vision OCR（版级单位，文章粒度由 reader 处理）。
支持离线回退（文章/图已在本地时不再触发网络搜索）。
"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

DEFAULT_ENGINE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "wechat_engine.py"))
VOCR = os.environ.get("READDAILY_VOCR") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "vocr"))


def _load_engine():
    """动态加载 jianshebao-daily 的解析函数（parse_guide_and_pages / find_article_html）。"""
    spec = __import__("importlib").util.spec_from_file_location(
        "cjsb_engine", DEFAULT_ENGINE)
    mod = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def acquire(source_cfg, d, archive_root, offline_ok=True):
    """确保当日读报文章已采集（本地存在即跳过；否则调用捷报引擎）。"""
    eng = _load_engine()
    out = os.path.expanduser(source_cfg.get("out", os.path.expanduser("~/Library/Application Support/readdaily/wechat-articles")))
    if eng.already_done(out, d):
        return True, "本地已有"
    if offline_ok:
        return False, "离线模式且本地无该日文章"
    p = subprocess.run([sys.executable, DEFAULT_ENGINE, "--date", d.isoformat(),
                        "--max-retries", "1", "--retry-gaps", "60", "--no-notify",
                        "--no-kb"],
                       capture_output=True, text=True, timeout=900)
    ok = eng.already_done(out, d)
    return ok, (p.stdout or p.stderr or "")[-300:] if not ok else "已采集"


def fetch(source_cfg, d, archive_root):
    """归一化：定位本地产物 → 版面/版名 → 复制页面图 → issue.json。"""
    eng = _load_engine()
    out = os.path.expanduser(source_cfg.get("out", os.path.expanduser("~/Library/Application Support/readdaily/wechat-articles")))
    wacct = os.path.join(out, source_cfg["name"])
    # 1) 原文.html
    html_path = eng.find_article_html(out, d)
    if not html_path:
        return None, "未找到本地文章 HTML"
    rows, page_srcs = eng.parse_guide_and_pages(html_path)
    if not rows or not page_srcs:
        return None, f"导读/版面图解析失败 rows={len(rows)} imgs={len(page_srcs)}"

    aps = lib.archive_paths(archive_root, source_cfg["id"], d)
    os.makedirs(aps["pages"], exist_ok=True)
    os.makedirs(aps["text"], exist_ok=True)

    # 2) 版面图：优先「电子报_YYYY-MM-DD/高清热图/」（1280 高清），否则 assets/
    ep_dirs = sorted(glob.glob(os.path.join(wacct, f"电子报_{d.isoformat()}")),
                     key=os.path.getmtime)
    ep_dir = ep_dirs[-1] if ep_dirs else None
    editions, units = [], []
    for idx, (no, name) in enumerate(rows):
        src = page_srcs[idx] if idx < len(page_srcs) else None
        page_src = None
        if ep_dir:
            cand = glob.glob(os.path.join(ep_dir, "高清热图", f"{no}版_*.jpg"))
            if cand:
                page_src = sorted(cand)[-1]
        if not page_src and src:
            base = os.path.join(wacct, "assets", os.path.basename(src))
            page_src = base if os.path.exists(base) else None
        if not page_src:
            continue
        fname = f"{no:02d}版_{lib.safe_name(name)}.jpg"
        from shutil import copy2
        copy2(page_src, os.path.join(aps["pages"], fname))
        editions.append({"no": no, "name": name,
                         "page_image": os.path.join("pages", fname)})
        units.append({"id": f"{source_cfg['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "edition_ocr", "title": f"{no}版 {name}",
                      "page_image": os.path.join("pages", fname)})
    if editions:
        # 期号：优先从电子报 PDF 名提取，否则 OCR 头版
        issue_no = None
        if ep_dir:
            m = re.search(r"第(\d+)期", " ".join(
                os.path.basename(x) for x in glob.glob(os.path.join(ep_dir, "*_电子报_高清.pdf"))))
            if m:
                issue_no = m.group(1)
        if not issue_no:
            issue_no = ocr_issue(os.path.join(aps["pages"], editions[0]["page_image"]))
    issue = {
        "source": source_cfg["id"], "source_name": source_cfg["name"],
        "date": d.isoformat(), "issue_no": issue_no,
        "channel": source_cfg["channel"], "editions": editions, "units": units,
        "files": {"article_html": os.path.abspath(html_path),
                  "epaper_dir": os.path.abspath(ep_dir) if ep_dir else None},
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    lib.save_json(aps["issue_json"], issue)
    return issue, None


def parse(source_cfg, d, archive_root):
    """版面图 → Vision OCR 文本（版级单位）。"""
    aps = lib.archive_paths(archive_root, source_cfg["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    for u in issue.get("units", []):
        txt_path = os.path.join(aps["text"], f"edition_{u['id'].rsplit('_', 1)[-1]}.txt")
        if os.path.exists(txt_path) and os.path.getsize(txt_path) > 200:
            u["text_path"] = os.path.relpath(txt_path, aps["dir"])
            continue
        img = os.path.join(aps["dir"], u["page_image"])
        text = ocr_image(img)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        u["text_path"] = os.path.relpath(txt_path, aps["dir"])
        time.sleep(1)
    lib.save_json(aps["issue_json"], issue)
    return issue, None


def ocr_image(img_path):
    if not os.path.exists(VOCR):
        return ""
    try:
        p = subprocess.run([VOCR, img_path], capture_output=True, text=True, timeout=180)
        return (p.stdout or "")
    except Exception:  # noqa: BLE001
        return ""


def ocr_issue(img_path):
    text = ocr_image(img_path)
    m = re.search(r"第(\d{3,5})期", text)
    if m:
        return m.group(1)
    return None
