#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：index.json + 慢页 HTML 数字报（样例：农民日报 szb.farmer.com.cn）。

来源配置：
  "channel": "cms_index",
  "cms": {"index_json": "https://szb.farmer.com.cn/index.json",
          "site": "https://szb.farmer.com.cn/", "paper_code": "nmrb", "max_pages": 16}
链路：index.json（当日最新期 paperDate/pagePath）→ 页1 → 版次导航（N版）与文章链接 →
      文章页 ozoom 全文（GBK/UTF-8 自识别）。显式文件直链均绕过 WAF 目录封禁。
"""
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

from urllib.parse import urljoin  # noqa: E402


def _best(raw):
    best, bn = None, -1
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            t = raw.decode(enc)
        except Exception:  # noqa: BLE001
            continue
        n = len(re.findall(r"[\u4e00-\u9fff]", t))
        if n > bn:
            best, bn = t, n
    return best or raw.decode("utf-8", "ignore")


def _get(url, ref=None):
    st, _, raw = lib.http_get(url, referer=ref)
    return st, _best(raw) if st == 200 else ""


def probe(src, d):
    d = lib.norm_day(d)
    st, h = _get(src["cms"]["index_json"], ref=src["cms"]["site"])
    hits = [x for x in re.findall(r'\{[^{}]*"paperCode"[^{}]*\}', h)]
    return [{"index_ok": st == 200 and bool(hits), "sample": (hits[0] if hits else "")[:180]}]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    cms = src["cms"]
    st, h = _get(cms["index_json"], ref=cms["site"])
    entries = re.findall(r'\{[^{}]*"paperCode"[^{}]*\}', h)
    if not entries:
        return None, "index.json 无条目（当日可能未发布）"
    e0 = entries[0]
    page = re.search(r'"pagePath"\s*:\s*"([^"]+)"', e0)
    date = re.search(r'"paperDate"\s*:\s*(\d+)', e0)
    issue = re.search(r'"paperIssueNum"\s*:\s*"([^"]+)"', e0)
    if not page:
        return None, "index.json 缺 pagePath"
    page1_url = urljoin(cms["site"] + ("/" if not cms["site"].endswith("/") else ""), page.group(1))

    aps = lib.archive_paths(archive_root, src["id"], d)
    os.makedirs(aps["pages"], exist_ok=True)
    os.makedirs(aps["text"], exist_ok=True)
    st, p1 = _get(page1_url)
    if st != 200:
        return None, f"页1 访问失败 {st}"

    # 版次导航：nmrb_..._N.html（无哈希） 与文章：nmrb_..._N_HASH.html
    nav = re.findall(r'href="([^"]*?(?:\d{8})_\d+_(\d+)\.html)"', p1)
    page_dirs = sorted(set((u, n) for u, n in nav if "_" not in u.rsplit("/", 1)[-1].rsplit("_", 3)[2:3][0]), key=lambda x: -1) if False else []
    page_urls = []
    seen_pages = set()
    for u, n in re.findall(r'href="([^"]*?(?:\d{8})_\d+_(\d+)\.html)"', p1):
        if re.search(r'(?:\d{8}_\d+_\d+_\d+\.html)$', u):
            continue
        if n in seen_pages:
            continue
        seen_pages.add(n)
        page_urls.append((urljoin(page1_url, u), int(n)))
    if not page_urls:
        page_urls = [(page1_url, 1)]
    page_urls.sort(key=lambda x: x[1])
    page_urls = page_urls[: int(cms.get("max_pages", 16))]

    editions, units = [], []
    for p_url, no in page_urls:
        st, ph = p_url and _get(p_url, ref=page1_url) or (0, "")
        if st != 200:
            continue
        m = re.search(rf'第\s*0?{no}\s*版\s*[：:]\s*([^<"{{}}]{{1,16}})', ph)
        if not m:
            m = re.search(r'第\s*0?\d+\s*版\s*[：:]\s*([^<"]{1,16})', ph)
        name = m.group(1).strip() if m else f"第{no}版"
        art_urls = []
        for u in dict.fromkeys(re.findall(r'href="([^"]*(?:\d{8}_\d+_\d+_\d+\.html))"', ph)):
            art_urls.append(urljoin(p_url, u))
        editions.append({"no": no, "name": name, "url": p_url, "article_count": len(art_urls)})
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}", "url": p_url,
                      "article_urls": art_urls})
        if len(units) >= int(cms.get("max_pages", 16)):
            break
    if not units:
        return None, "版次解析失败"
    issue_meta = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
                  "issue_no": (issue.group(1).rsplit("_", 1)[-1] if issue else None),
                  "channel": "cms_index", "editions": editions, "units": units,
                  "index_entry": e0[:300],
                  "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    lib.save_json(aps["issue_json"], issue_meta)
    return issue_meta, None


def parse(src, d, archive_root, max_per_edition=30):
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue or not issue.get("editions"):
        return issue, None
    for u in issue.get("units", []):
        txts = []
        for au in u.get("article_urls", [])[:max_per_edition]:
            st, h = _get(au, ref=u.get("url"))
            if st != 200 or not h:
                continue
            # 标题
            tm = re.findall(r'(?:id="(?:Title|PreTitle)"[^>]*>|(?:og:title)"\s+content=")([^<"]{4,90})', h)
            mi = h.find('id="ozoom"')
            seg = h[mi:mi + 150000] if mi >= 0 else h[:150000]
            paras = []
            for p in re.findall(r'<p[^>]*>(.*?)</p>', seg, flags=re.S):
                t = re.sub(r"<[^>]+>", " ", lib.html_text(p.encode("utf-8", "ignore")))
                t = re.sub(r"\s+", " ", t).replace("&nbsp;", " ").strip()
                if len(t) >= 12:
                    paras.append(t)
                if len(paras) >= 70:
                    break
            txt = "\n".join(paras)
            if txt:
                txts.append((tm[0].strip() if tm else "", txt))
        u["articles"] = [{"title": t, "text": x[:30000]} for t, x in txts]
        u["text"] = "\n\n".join(f"{t}\n{x}" for t, x in txts)
    lib.save_json(aps["issue_json"], issue)
    return issue, None
