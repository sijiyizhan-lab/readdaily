#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：JSON API 型数字报（样例：科技日报 epaper.stdaily.com）。

来源配置：
  "channel": "paper_api",
  "api": {"base": "https://epaper.stdaily.com/stdailynewspaperapi",
          "code": "KJRB", "premium": false}
链路：POST /uv/article/period/date {date,code} → dateList
     POST /uv/article/period/periodTime {date,code} → editionList(id/periodId/版名/版面图)
     POST /uv/article/article/editionId {id,periodId} → 文章列表(标题+正文全文)
（uv=匿名通道；premium 通道 403 No access，勿混淆）
"""
import datetime
import html
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402


def _api(src, path, data):
    base = src.get("api", {}).get("base", "").rstrip("/")
    import requests
    r = requests.post(f"{base}{path}", json=data, timeout=20,
                      headers={"User-Agent": lib.UA, "Content-Type": "application/json",
                               "Referer": src.get("entry") or base})
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def probe(src, d):
    d = lib.norm_day(d)
    code = src["api"]["code"]
    r = _api(src, "/uv/article/period/date", {"date": d.isoformat(), "code": code})
    if not r or not r.get("obj"):
        return [{"note": f"dateList 为空（{d} 可能未出版）"}]
    return [{"dateList": r["obj"].get("dateList", [])}]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    code = src["api"]["code"]
    r = _api(src, "/uv/article/period/periodTime", {"date": d.isoformat(), "code": code})
    if not r or not r.get("obj"):
        return None, "无版次数据（可能尚未出版）"
    edlist = r["obj"].get("editionList") or []
    aps = lib.archive_paths(archive_root, src["id"], d)
    os.makedirs(aps["pages"], exist_ok=True)
    os.makedirs(aps["text"], exist_ok=True)
    editions, units = [], []
    for ed in edlist:
        m = re.search(r'第\s*0?(\d+)\s*版\s*[：:]\s*([^：:]{1,14})', ed.get("editionName", ""))
        no = int(m.group(1)) if m else len(editions) + 1
        name = m.group(2).strip() if m else f"第{no}版"
        page_img = ed.get("editionImg")
        page_file = None
        if page_img:
            try:
                st, _, raw = lib.http_get(page_img, referer=src.get("entry"))
                if st == 200 and len(raw) > 50000:
                    fimg = os.path.join(aps["pages"], f"{no:02d}版_{lib.safe_name(name)}.jpg")
                    with open(fimg, "wb") as f:
                        f.write(raw)
                    page_file = os.path.relpath(fimg, aps["dir"])
            except Exception:  # noqa: BLE001
                pass
        editions.append({"no": no, "name": name, "page_image": page_file,
                         "api_id": ed.get("id"), "period_id": ed.get("periodId")})
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}",
                      "api_id": ed.get("id"), "period_id": ed.get("periodId")})
    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "issue_no": None, "channel": "paper_api", "editions": editions,
             "units": units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    lib.save_json(aps["issue_json"], issue)
    return issue, None


def parse(src, d, archive_root):
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    for u in issue.get("units", []):
        r = _api(src, "/uv/article/article/editionId",
                 {"id": u.get("api_id"), "period_id": u.get("period_id")})
        arts = []
        txts = []
        for a in (r or {}).get("list") or []:
            title = re.sub(r"<[^>]+>", "", html.unescape(a.get("title") or ""))
            content = re.sub(r"<[^>]+>", " ", html.unescape(a.get("content") or ""))
            content = re.sub(r"\s+", " ", content).strip()
            # 列表接口只给导语，全文走 article/articleId
            aid = a.get("id")
            if aid and len(content) < 500:
                ra = _api(src, "/uv/article/article/articleId", {"id": aid})
                vo = ((ra or {}).get("obj") or {}).get("articleVo") or {}
                full = re.sub(r"<[^>]+>", " ", html.unescape(vo.get("content") or ""))
                full = re.sub(r"\s+", " ", full).strip()
                if len(full) > len(content):
                    content = full
            arts.append({"title": title.strip(), "text": content[:30000]})
            if content:
                txts.append(f"{title.strip()}\n{content}")
        u["articles"] = arts
        u["text"] = "\n\n".join(txts)
    lib.save_json(aps["issue_json"], issue)
    return issue, None
