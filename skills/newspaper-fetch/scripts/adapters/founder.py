#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：方正/amucsite 数字报（通用）。

来源配置（sources.json）：
  "channel": "founder",
  "node_tpl": ".../{y}{m}/{d}/node_{page:02d}.html",   # 版页模板；{y}{m}{d}{page}
  "max_pages": 20,
  "pic_patterns": ["https://host/pc/pic/{y}{m}/{d}/{name}", ...]  # 版面图候选（{name}=页面提取文件名）
  "index_url": 可选（如 gmrb html/layout/index.html，用于发现版数与基址）
"""
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

from urllib.parse import urljoin, urlparse  # noqa: E402


def _fmt(tpl, d, page, name=""):
    d = lib.norm_day(d)
    return tpl.format(y=d.year, m="%02d" % d.month, d="%02d" % d.day,
                      page=page, name=name)


def _best(body_bytes):
    """多种编码解码，选 CJK 字符最多的结果。"""
    best, best_n = None, -1
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            t = body_bytes.decode(enc)
        except Exception:  # noqa: BLE001
            continue
        n = len(re.findall(r"[\u4e00-\u9fff]", t))
        if n > best_n:
            best, best_n = t, n
    return best or body_bytes.decode("utf-8", "ignore")


def _probe_pages(src, d, verbose=False):
    """版页探测：返回 (base_url, pages_ok)。"""
    tpl = src.get("node_tpl")
    if not tpl:
        return None, 0
    base = None
    for page in range(1, int(src.get("max_pages", 20)) + 1):
        u = _fmt(tpl, d, page)
        try:
            st, _, raw = lib.http_get(u, referer=src.get("entry"))
        except Exception:  # noqa: BLE001
            continue
        if st != 200 or len(raw) < 3000:
            if base:
                break
            continue
        h = _best(raw)
        if "版" not in h and "html" not in h:
            continue
        base = base or u
        if verbose:
            print("   page", page, "ok", len(raw))
    return base, page - 1 if base else 0


def probe(src, d):
    base, n = _probe_pages(src, d, verbose=True)
    print(f"founder probe {src['id']}: base={base} pages={n}")
    if base:
        st, _, raw = lib.http_get(base, referer=src.get("entry"))
        h = _best(raw)
        arts = re.findall(r'href="([^"]*content[^"]*\.html)"', h)
        print("   article links:", list(dict.fromkeys(arts))[:4])
    return [{"node_tpl": src.get("node_tpl"), "pages_ok": n}]


def fetch(src, d, archive_root, with_text=False, max_articles=12):
    """抓当日：版次/版名/版面图/文章链接；with_text=True 时抓全文。"""
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    os.makedirs(aps["pages"], exist_ok=True)
    os.makedirs(aps["text"], exist_ok=True)

    # 1) 版页清单：优先 index_url（从索引页发现 node_XX 链接+版名）
    pages = []
    if src.get("index_url"):
        try:
            st, _, raw = lib.http_get(src["index_url"], referer=src.get("entry"))
            h = _best(raw)
            for m in re.finditer(r'<a[^>]*href="([^"]*node_[^"]+\.html)"[^>]*>(.*?)</a>', h, re.S):
                href, inner = m.group(1), m.group(2)
                label = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', inner)).strip()
                mm = re.search(r'第\s*(\S+?)\s*版\s*(.{1,14})', label)
                if mm:
                    pages.append((urljoin(src["index_url"], href), mm.group(1),
                                  re.sub(r'\s+', '', mm.group(2))))
        except Exception:  # noqa: BLE001
            pass
    if not pages:
        base, n = _probe_pages(src, d)
        if not base:
            return None, f"当日无可用版面（node_tpl={src.get('node_tpl')}）"
        pages = [(_fmt(src["node_tpl"], d, p), f"{p:02d}", f"第{p}版") for p in range(1, n + 1)]

    editions, all_units = [], []
    for no, (u, label, fallback_name) in enumerate(
            [(p[0], p[1], p[2]) for p in pages][:int(src.get("max_pages", 20))]):
        st, _, raw = lib.http_get(u, referer=src.get("entry"))
        if st != 200:
            continue
        h = _best(raw)
        no_ord = no + 1
        name = fallback_name
        layout_data = None
        m = re.search(r'window\.layoutData\s*=\s*(\{[\s\S]{0,200000}?\})\s*;?\s*</script>', h)
        if m:
            layout_data = m.group(1)
            lm = re.search(r'layout:"([^"]+)"', layout_data)
            ln = re.search(r'layoutName:"([^"]+)"', layout_data)
            if ln:
                name = ln.group(1).strip()
        elif not fallback_name.startswith("第"):
            pass
        # 版面图（layoutData.picUrl 优先；否则节点页 pic 正则）
        page_img = None
        pic_rel = re.search(r'picUrl:"([^"]+)', layout_data or "")
        if pic_rel:
            pic_rel = pic_rel.group(1)
        else:
            # 老样式：pic 令牌（如 rmrb ../../../pc/pic/...）
            pass
        if pic_rel:
            purl = urljoin(u, pic_rel)
            if purl.endswith(".2"):
                purl2 = purl[:-2]  # 去 .2 取原图
            else:
                purl2 = None
            for cand in ([purl2] if purl2 else []) + [purl]:
                st2, _, b2 = lib.http_get(cand, referer=u)
                if st2 == 200 and len(b2) > 200000:
                    fimg = os.path.join(aps["pages"], f"{no_ord:02d}版_{lib.safe_name(name)}.jpg")
                    with open(fimg, "wb") as f:
                        f.write(b2)
                    page_img = os.path.relpath(fimg, aps["dir"])
                    break
        # 文章：layoutData.articles（title/urlPad）优先，否则 content_*.html href
        arts = []
        if layout_data:
            base_for_rel = src.get("index_url") or u
            seen = set()
            for am in re.finditer(
                    r'title:"([^"]{0,160})"[^{}]{0,500}?urlPad:"([^"]+)"[^{}]{0,500}?content:`([^`]{0,40000})`',
                    layout_data):
                t = re.sub(r"<[^>]+>", "", am.group(1))
                url = urljoin(base_for_rel, am.group(2))
                if url in seen:
                    continue
                seen.add(url)
                c = re.sub(r"<[^>]+>", " ", am.group(3))
                c = re.sub(r"\s+", " ", c).strip()
                arts.append({"title": t.strip(), "text": c[:30000], "url": url})
        if not arts:
            for href in dict.fromkeys(re.findall(r'href="([^"]*content[^"]*\.html)"', h)):
                arts.append({"title": "", "text": "", "url": urljoin(u, href)})
        editions.append({"no": no_ord, "name": name,
                         "page_image": page_img, "url": u, "label": label})
        all_units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no_ord:02d}",
                          "type": "article_text", "title": f"{no_ord}版 {name}",
                          "url": u, "articles": arts[:max_articles]})
    if not editions:
        return None, "版页全部解析失败"
    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "issue_no": None, "channel": "founder", "editions": editions,
             "units": all_units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    lib.save_json(aps["issue_json"], issue)
    return issue, None


def parse(src, d, archive_root, max_articles=8):
    """文章全文抽取：content 页 → ozoom/p 文本 → text/ 文件，注入 units。"""
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"] or aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    for u in issue.get("units", []):
        # layoutData 内嵌全文（≥800字）→ 直接聚合；否则走 content 页全文
        if u.get("articles") and all((len(a.get("text") or "")) >= 800 for a in u["articles"]):
            u["text"] = "\n\n".join(f"{a.get('title','')}\n{a.get('text','')}"
                                    for a in u["articles"])
            summary_done = True
        else:
            summary_done = False
        if summary_done:
            continue
        txts = []
        for art in u.get("articles", [])[:max_articles]:
            url = art.get("url") or ""
            if not url:
                continue
            try:
                st, _, raw = lib.http_get(url, referer=issue.get("editions", [{}])[0].get("url"))
                if st != 200:
                    continue
                h = _best(raw)
            except Exception:  # noqa: BLE001
                continue
            # 标题
            tm = re.search(r'<h1[^>]*>([^<]{2,80})</h1>', h) or re.search(
                r'og:title"\s+content="([^"]{2,80})"', h)
            title = (tm.group(1).strip() if tm else "").strip(" \u201c\u201d")
            # 正文：ozoom / articleContent 段落
            paras = []
            mi = h.find('id="ozoom"')
            if mi < 0:
                mi = h.find('id="articleContent"')
            seg = h[mi:mi + 120000] if mi >= 0 else h[:120000]
            for p in re.findall(r'<p[^>]*>(.*?)</p>', seg, flags=re.S):
                t = lib.html_text(p.encode("utf-8", "ignore")).replace("&nbsp;", " ").strip()
                t = re.sub(r'<[^>]+>', '', t)
                t = re.sub(r'\s+', ' ', t)
                if len(t) >= 15:
                    paras.append(t)
                if len(paras) >= 60:
                    break
            txt = "\n".join(paras)
            if not txt:
                # 兜底：无 <p> 包裹的裸 div 正文（如 nfnews content 页）
                txt = re.sub(r"<[^>]+>", " ", seg)
                txt = re.sub(r"\s+", " ", txt).replace("&nbsp;", " ").strip()
            art["title"] = title or art.get("title", "")
            art["text"] = txt[:30000] if txt else art.get("text") or ""
            if txt:
                txts.append(f"{art['title']}\n{txt}")
        u["text_path"] = None
        u["text"] = "\n\n".join(txts)
    lib.save_json(aps["issue_json"], issue)
    return issue, None
