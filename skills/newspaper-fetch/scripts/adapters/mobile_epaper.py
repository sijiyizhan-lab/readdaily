#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：移动版数字报（样例：北京日报 bjrbdzb.bjd.com.cn）

来源配置：
  "channel": "mobile_epaper",
  "mob": {"index_tpl": "https://bjrbdzb.bjd.com.cn/bjrb/mobile/{y}/{yymmdd}/{yymmdd}_m.html",
          "site": "https://bjrbdzb.bjd.com.cn/", "max_pages": 24}
索引页内嵌：版次导航（pdf_href + 第N版 名称）、每版文章（data-href content_*.htm + 标题）、
版面图（news-bjrb-...-m-{NNN}-300.jpg）；全文页 id="content"（服务端渲染，无需 OCR）。
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


def _fmt(tpl, d):
    d = lib.norm_day(d)
    return tpl.format(y=d.year, yymmdd=d.strftime("%Y%m%d"))


def probe(src, d):
    st, _, raw = lib.http_get(_fmt(src["mob"]["index_tpl"], d), referer=src["mob"]["site"])
    if st != 200:
        return [{"note": f"索引 {st}"}]
    h = lib.html_text(raw)
    navs = re.findall(r'pdf_href="([^"]+)"[^>]*>\s*第\s*(\d+)\s*版\s*([^<]{1,16})', h)
    arts = len(re.findall(r'data-href="[^"]*content_', h))
    return [{"index_ok": True, "editions": [(int(n), nm.strip()) for _, n, nm in navs],
             "article_refs": arts}]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    index_url = _fmt(src["mob"]["index_tpl"], d)
    st, _, raw = lib.http_get(index_url, referer=src["mob"]["site"])
    if st != 200:
        return None, f"索引不可达 {st}"
    h = lib.html_text(raw)
    aps = lib.archive_paths(archive_root, src["id"], d)
    os.makedirs(aps["pages"], exist_ok=True)
    os.makedirs(aps["text"], exist_ok=True)

    # 版次：pdf_href 携带目录前缀（../20260902_001/）
    navs = re.findall(r'pdf_href="([^"]+)"[^>]*>\s*第\s*(\d+)\s*版\s*([^<]{1,16})', h)
    # 版面图：每版目录下 news-…-m-{no:03d}-300.jpg（按出现顺序配目录）
    imgs = dict()
    for m in re.finditer(r'(\./(\d{8}_\d{3})/[^"\']*?/)?(news-bjrb-\S+?-m-0\d{2}-300\.jpg)', h):
        p = m.group(0)
        if p.startswith("./"):
            p = p[2:]
        key = os.path.basename(p).split("-m-")[-1].split("-")[0]  # 版号
        imgs.setdefault(p.split("/")[0] if "/" in p else key, p)

    editions, units = [], []
    for pdf_href, no, name in navs[: int(src["mob"].get("max_pages", 24))]:
        no = int(no)
        name = name.strip()
        ddir = re.search(r'(\.\./)?(\d{8}_\d{3})', pdf_href)
        # 版图
        page_img = None
        rel = None
        if ddir:
            rel = f"{ddir.group(2)}/news-bjrb-00000-{d.strftime('%Y%m%d')}-m-{no:03d}-300.jpg"
        if rel:
            purl = urljoin(index_url, rel)
            st2, _, b2 = lib.http_get(purl, referer=index_url)
            if st2 == 200 and len(b2) > 60000:
                fimg = os.path.join(aps["pages"], f"{no:02d}版_{lib.safe_name(name)}.jpg")
                with open(fimg, "wb") as f:
                    f.write(b2)
                page_img = os.path.relpath(fimg, aps["dir"])
        # 本版文章
        arts = []
        if ddir:
            for am in re.finditer(
                    r'data-href="((?:\./)?(\d{8}_\d{3})/content_[^"]+\.htm[^"]*)"[^>]*>([^<]{4,90})',
                    h):
                if am.group(2) != ddir.group(2):
                    continue
                title = re.sub(r'\s+', '', am.group(3))
                arts.append({"title": title, "url": urljoin(index_url, am.group(1))})
        editions.append({"no": no, "name": name, "page_image": page_img,
                         "url": index_url, "pdf_href": pdf_href})
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}",
                      "url": index_url, "articles": arts})
    if not units:
        return None, "版次解析失败（索引结构可能变更）"
    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "issue_no": None, "channel": "mobile_epaper", "editions": editions,
             "units": units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    lib.save_json(aps["issue_json"], issue)
    return issue, None


def parse(src, d, archive_root, max_per_edition=20):
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue or not issue.get("units"):
        return issue, None
    for u in issue.get("units", []):
        txts = []
        for a in u.get("articles", [])[:max_per_edition]:
            st, _, raw = lib.http_get(a["url"], referer=u.get("url"))
            if st != 200:
                continue
            h = lib.html_text(raw)
            mi = h.find('id="content"')
            seg = h[mi:mi + 120000] if mi >= 0 else h[:120000]
            t = re.sub(r"<[^>]+>", " ", seg)
            t = re.sub(r"\s+", " ", t).replace("&nbsp;", " ").strip()
            a["text"] = t[:30000]
            if len(t) >= 40:
                txts.append(f"{a.get('title','')}\n{t}")
        u["articles"] = [a for a in u.get("articles", []) if a.get("text")]
        u["text"] = "\n\n".join(txts)
    lib.save_json(aps["issue_json"], issue)
    return issue, None
