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
import copy
import datetime
import html as html_module
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
    """按页面声明优先解码，避免把合法 UTF-8 误判成更多汉字的乱码。"""
    encodings = []
    charset = re.search(
        br"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
        body_bytes[:4096],
        re.I,
    )
    if charset:
        declared = charset.group(1).decode("ascii", "ignore").lower()
        encodings.append("gb18030" if declared in ("gbk", "gb2312") else declared)
    encodings.extend(("utf-8", "gb18030", "latin-1"))
    for enc in dict.fromkeys(encodings):
        try:
            return body_bytes.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body_bytes.decode("utf-8", "replace")


def _hrefs(document):
    return [
        match.group(2)
        for match in re.finditer(
            r'\bhref\s*=\s*(["\'])(.*?)\1', document or "", re.I | re.S
        )
    ]


def _container_tail(document, identifiers):
    accepted = {identifier.lower() for identifier in identifiers}
    for match in re.finditer(r'<(?:article|div|section)\b[^>]*>', document or "", re.I):
        identifier = re.search(
            r'\bid\s*=\s*(["\'])(.*?)\1', match.group(0), re.I | re.S
        )
        if identifier and identifier.group(2).strip().lower() in accepted:
            return document[match.end():]
    return None


def _article_title(document):
    """Extract a complete headline without arbitrary character cutoffs."""
    match = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", document or "", re.I | re.S)
    if match:
        title = re.sub(r"<[^>]+>", " ", match.group(1))
        title = re.sub(r"\s+", " ", html_module.unescape(title)).strip()
        if title:
            return title.strip(" \u201c\u201d")
    for attributes in lib.html_tag_attributes(document, "meta"):
        identity = str(
            attributes.get("property") or attributes.get("name") or ""
        ).strip().lower()
        if identity != "og:title":
            continue
        title = re.sub(
            r"\s+", " ",
            html_module.unescape(str(attributes.get("content") or "")),
        ).strip()
        if title:
            return title.strip(" \u201c\u201d")
    return ""


def _js_string_property(document, name):
    match = re.search(r'\b%s\s*:\s*' % re.escape(name), document or "", re.I)
    if not match or match.end() >= len(document):
        return None
    quote = document[match.end()]
    if quote not in ('"', "'", "`"):
        return None
    escaped = False
    value = []
    for character in document[match.end() + 1:]:
        if escaped:
            value.append(character)
            escaped = False
        elif character == "\\":
            value.append(character)
            escaped = True
        elif character == quote:
            return "".join(value)
        else:
            value.append(character)
    return None


def _js_array_property(document, name):
    match = re.search(r'\b%s\s*:\s*\[' % re.escape(name), document or "", re.I)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(document)):
        character = document[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ('"', "'", "`"):
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return document[start + 1:index]
    return None


def _js_objects(array_body):
    objects = []
    depth = 0
    start = None
    quote = None
    escaped = False
    for index, character in enumerate(array_body or ""):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ('"', "'", "`"):
            quote = character
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0 and start is not None:
                objects.append(array_body[start:index + 1])
                start = None
    if depth or quote:
        return None
    return objects


def _edition_name(document, number, fallback):
    """Prefer the edition label printed on the validated page itself."""
    fragments = []
    for pattern in (
        r'<span\b[^>]*\bclass\s*=\s*(["\'])[^"\']*\bmob-version\b[^"\']*\1[^>]*>(.*?)</span>',
        r'<span\b[^>]*\bid\s*=\s*(["\'])layout\1[^>]*>(.*?)</span>',
    ):
        fragments.extend(match.group(2) for match in re.finditer(
            pattern, document or "", re.I | re.S
        ))
    fragments.append(str(fallback or ""))
    for fragment in fragments:
        text = re.sub(r"<[^>]+>", "", fragment)
        text = re.sub(r"\s+", " ", text).strip()
        match = re.search(
            r"第\s*0*%s\s*版\s*[：:]?\s*(.{1,40})" % int(number), text
        )
        candidate = match.group(1) if match else text
        candidate = candidate.strip(" \t\r\n\"'/：:")
        if candidate and not re.fullmatch(r"第?0*%s版?" % int(number), candidate):
            return candidate
    return "第%s版" % int(number)


def _page_image_relatives(document):
    """Find the newspaper page image, excluding decorative template assets."""
    found = []
    for attributes in lib.html_tag_attributes(document, "img"):
        src = attributes.get("src")
        if not src:
            continue
        image_id = str(attributes.get("id") or "").strip().lower()
        classes = set(str(attributes.get("class") or "").lower().split())
        if (image_id in {"map", "pageimg"} or "preview" in classes
                or re.search(r"/(?:html/)?pic/20\d{4}/\d{1,2}/", src, re.I)):
            found.append(src)
    return list(dict.fromkeys(found))


def _valid_date(year, month, day):
    try:
        return datetime.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _dates_in_text(value):
    text = str(value or "")
    found = set()
    patterns = (
        r"(?<!\d)(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})[-_/](\d{1,2})(?!\d)",
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)",
        r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parsed = _valid_date(*match.groups())
            if parsed:
                found.add(parsed)
    return found


def _document_dates(html):
    """Extract issue-date evidence from title and date-related metadata only."""
    evidence = []
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if title:
        evidence.append(title.group(1))
    for meta in re.findall(r"<meta\b[^>]*>", html or "", re.I):
        if re.search(r"(?:date|publish|issue|pubtime)", meta, re.I):
            evidence.append(meta)
    return _dates_in_text("\n".join(evidence))


def _response_date_error(requested_day, requested_url, final_url, html):
    """Return a reason when a successful response cannot prove the target day."""
    final_dates = _dates_in_text(final_url)
    document_dates = _document_dates(html)
    if final_dates and final_dates != {requested_day}:
        return "响应跳转日期与请求日期 %s 不一致" % requested_day.isoformat()
    if document_dates and requested_day not in document_dates:
        return "页面内容日期与请求日期 %s 不一致" % requested_day.isoformat()
    requested_dates = _dates_in_text(requested_url)
    if requested_day not in final_dates and requested_day not in document_dates:
        # A non-redirecting 200 response to an explicitly dated URL is server
        # evidence too.  Undated static-index URLs are never accepted here.
        if final_url == requested_url and requested_dates == {requested_day}:
            return None
        return "页面无法确认请求日期 %s" % requested_day.isoformat()
    return None


def _probe_pages(src, d, verbose=False):
    """版页探测：返回 (base_url, pages_ok)。"""
    tpl = src.get("node_tpl")
    if not tpl:
        return None, 0
    base = None
    last_confirmed_page = 0
    missing_pages = []
    max_pages = int(src.get("max_pages", 20))
    # Probe one page beyond the configured cap so hitting the cap can never be
    # mistaken for a complete issue.
    for page in range(1, max_pages + 2):
        u = _fmt(tpl, d, page)
        try:
            st, final_url, raw = lib.http_get(u, referer=src.get("entry"))
        except lib.PIPELINE_FATAL_EXCEPTIONS:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("第%s版探测异常：%s" % (page, exc)) from exc
        if st in (404, 410):
            # A single 404 is not proof of the natural end: a later node may
            # still exist.  Continue through max+1 so gaps cannot be archived
            # as a deceptively complete shorter issue.
            missing_pages.append(page)
            continue
        if st != 200:
            raise RuntimeError("第%s版探测失败 %s" % (page, st))
        if missing_pages:
            raise RuntimeError(
                "版次不连续：第%s版不存在但第%s版存在；整期未归档"
                % (missing_pages[0], page)
            )
        if len(raw) < 3000:
            raise RuntimeError("第%s版响应过短，无法确认是否为期末" % page)
        h = _best(raw)
        if "版" not in h and "html" not in h:
            raise RuntimeError("第%s版响应无法确认版面内容" % page)
        date_error = _response_date_error(d, u, final_url, h)
        if date_error:
            raise RuntimeError("第%s版%s" % (page, date_error))
        if page > max_pages:
            raise RuntimeError(
                "版次超过配置上限 %s；为避免缺版已拒绝归档" % max_pages
            )
        base = base or u
        last_confirmed_page = page
        if verbose:
            print("   page", page, "ok", len(raw))
    return base, last_confirmed_page if base else 0


def probe(src, d):
    try:
        base, n = _probe_pages(src, d, verbose=True)
    except lib.PIPELINE_FATAL_EXCEPTIONS:
        raise
    except RuntimeError as exc:
        return [{"node_tpl": src.get("node_tpl"), "pages_ok": 0,
                 "error": str(exc)}]
    print(f"founder probe {src['id']}: base={base} pages={n}")
    if base:
        st, _, raw = lib.http_get(base, referer=src.get("entry"))
        h = _best(raw)
        arts = re.findall(r'href="([^"]*content[^"]*\.html)"', h)
        print("   article links:", list(dict.fromkeys(arts))[:4])
    return [{"node_tpl": src.get("node_tpl"), "pages_ok": n}]


def fetch(src, d, archive_root, with_text=False, max_articles=12):
    """抓当日：版次/版名/版面图/文章链接；with_text=True 时抓全文。"""
    # max_articles is retained for CLI/API compatibility.  A complete issue
    # must keep every discovered article; silently truncating the list makes a
    # successful archive indistinguishable from a partial one.
    del max_articles
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)

    # 1) 版页清单：优先 index_url（从索引页发现 node_XX 链接+版名）
    pages = []
    if src.get("index_url"):
        try:
            st, _, raw = lib.http_get(src["index_url"], referer=src.get("entry"))
            if st != 200:
                return None, "静态索引访问失败 %s，无法确认请求日期" % st
            h = _best(raw)
            for m in re.finditer(
                    r'<a[^>]*href=["\']([^"\']*node_[^"\']+\.html)["\'][^>]*>(.*?)</a>',
                    h, re.I | re.S):
                href, inner = m.group(1), m.group(2)
                resolved = urljoin(src["index_url"], href)
                resolved_dates = _dates_in_text(resolved)
                if resolved_dates != {d}:
                    return None, "静态索引含无法确认或日期不一致的版面链接；整期未归档"
                label = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', inner)).strip()
                mm = re.search(r'第\s*(\S+?)\s*版\s*(.{1,14})', label)
                if mm:
                    page_match = re.search(r'node_(\d+)', resolved, re.I)
                    if not page_match:
                        return None, "静态索引版面链接缺少可验证版号；整期未归档"
                    pages.append((resolved, int(page_match.group(1)), mm.group(1),
                                  re.sub(r'\s+', '', mm.group(2))))
        except lib.PIPELINE_FATAL_EXCEPTIONS:
            raise
        except Exception:  # noqa: BLE001
            return None, "静态索引读取失败，无法确认请求日期 %s" % d.isoformat()
        if not pages:
            return None, "静态索引无法确认请求日期 %s（当日缺报）" % d.isoformat()
        pages_by_number = {}
        for page in pages:
            page_url, page_number = page[0], page[1]
            existing = pages_by_number.get(page_number)
            if existing and existing[0] != page_url:
                return None, "静态索引存在重复版号 %s；整期未归档" % page_number
            pages_by_number[page_number] = page
        pages = [pages_by_number[number] for number in sorted(pages_by_number)]
        page_numbers = [page[1] for page in pages]
        if page_numbers != list(range(1, len(page_numbers) + 1)):
            return None, "静态索引版号必须从1连续排列；整期未归档"
    else:
        try:
            base, n = _probe_pages(src, d)
        except lib.PIPELINE_FATAL_EXCEPTIONS:
            raise
        except RuntimeError as exc:
            return None, str(exc)
        if not base:
            return None, f"当日无可用版面（node_tpl={src.get('node_tpl')}）"
        pages = [(_fmt(src["node_tpl"], d, p), p, f"{p:02d}", f"第{p}版")
                 for p in range(1, n + 1)]

    max_pages = int(src.get("max_pages", 20))
    if len(pages) > max_pages:
        return None, "静态索引发现 %s 版，超过配置上限 %s；为避免缺版已拒绝归档" % (
            len(pages), max_pages
        )
    expected_pages = list(pages)
    editions, all_units = [], []
    page_downloads = []
    for u, no_ord, label, fallback_name in expected_pages:
        try:
            st, final_url, raw = lib.http_get(u, referer=src.get("entry"))
        except lib.PIPELINE_FATAL_EXCEPTIONS:
            raise
        except Exception as exc:  # noqa: BLE001
            return None, "第%s版访问异常：%s" % (no_ord, exc)
        if st != 200:
            return None, "第%s版访问失败 %s；整期未归档" % (no_ord, st)
        h = _best(raw)
        page_date_error = _response_date_error(d, u, final_url, h)
        if page_date_error:
            return None, "第%s版%s；整期未归档" % (no_ord, page_date_error)
        u = final_url
        name = fallback_name
        layout_data = None
        m = re.search(r'window\.layoutData\s*=\s*(\{[\s\S]*?\})\s*;?\s*</script>', h)
        if m:
            layout_data = m.group(1)
            layout_name = _js_string_property(layout_data, "layoutName")
            if layout_name:
                name = layout_name.strip()
        elif not fallback_name.startswith("第"):
            pass
        # 文章：layoutData.articles（title/urlPad）优先，否则 content_*.html href
        arts = []
        by_url = {}
        article_array = _js_array_property(layout_data, "articles")
        if article_array is not None:
            objects = _js_objects(article_array)
            if not objects:
                return None, "第%s版 layoutData 文章清单为空或损坏；整期未归档" % no_ord
            base_for_rel = src.get("index_url") or u
            for article_index, article_object in enumerate(objects, 1):
                relative_url = (
                    _js_string_property(article_object, "urlPad")
                    or _js_string_property(article_object, "url")
                )
                if not relative_url:
                    return None, "第%s版 layoutData 第%s篇文章缺链接；整期未归档" % (
                        no_ord, article_index
                    )
                article_url = urljoin(base_for_rel, relative_url)
                article_dates = _dates_in_text(article_url)
                if article_dates and article_dates != {d}:
                    return None, "第%s版文章链接日期与请求日期不一致；整期未归档" % no_ord
                if article_url in by_url:
                    return None, "第%s版 layoutData 存在重复文章链接；整期未归档" % no_ord
                raw_title = _js_string_property(article_object, "title") or ""
                raw_content = _js_string_property(article_object, "content") or ""
                title = re.sub(r"<[^>]+>", "", raw_title)
                content = re.sub(r"<[^>]+>", " ", raw_content)
                article = {
                    "title": re.sub(r"\s+", " ", title).strip(),
                    "text": re.sub(r"\s+", " ", content).strip(),
                    "url": article_url,
                }
                arts.append(article)
                by_url[article_url] = article
        for href in dict.fromkeys(_hrefs(h)):
            if not re.search(r'content[^"\']*\.html(?:[?#][^"\']*)?$', href, re.I):
                continue
            article_url = urljoin(u, href)
            article_dates = _dates_in_text(article_url)
            if article_dates and article_dates != {d}:
                return None, "第%s版文章链接日期与请求日期不一致；整期未归档" % no_ord
            if article_url not in by_url:
                article = {"title": "", "text": "", "url": article_url}
                arts.append(article)
                by_url[article_url] = article
        if not arts:
            return None, "第%s版文章链接解析为空；整期未归档" % no_ord

        name = _edition_name(h, no_ord, name)
        # 版面图：新版 layoutData.picUrl；老版使用 id=map/class=preview。
        pic_rels = []
        layout_pic = _js_string_property(layout_data, "picUrl")
        if layout_pic:
            pic_rels.append(layout_pic)
        pic_rels.extend(_page_image_relatives(h))
        pic_rels = list(dict.fromkeys(pic_rels))
        if not pic_rels:
            return None, "第%s版未找到可验证版面图；整期未归档" % no_ord
        page_img = None
        page_image_meta = None
        page_image_source_url = None
        for pic_rel in pic_rels:
            purl = urljoin(u, pic_rel)
            candidates = ([purl[:-2]] if purl.endswith(".2") else []) + [purl]
            for cand in candidates:
                candidate_dates = _dates_in_text(cand)
                if not candidate_dates:
                    return None, "版面图候选 URL 无法确认请求日期 %s" % d.isoformat()
                if candidate_dates != {d}:
                    return None, "版面图候选 URL 日期与请求日期 %s 不一致" % d.isoformat()
                try:
                    st2, image_final_url, b2 = lib.http_get(cand, referer=u)
                except lib.PIPELINE_FATAL_EXCEPTIONS:
                    raise
                except Exception:  # noqa: BLE001
                    continue
                if st2 != 200:
                    continue
                final_dates = _dates_in_text(image_final_url)
                if not final_dates:
                    return None, "版面图最终 URL 无法确认请求日期 %s" % d.isoformat()
                if final_dates != {d}:
                    return None, "版面图最终 URL 日期与请求日期 %s 不一致" % d.isoformat()
                image_meta, image_error = lib.validate_page_image(
                    b2, min_bytes=50000
                )
                if image_error is not None:
                    continue
                extension = {
                    "jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"
                }[image_meta["format"]]
                fimg = os.path.join(
                    aps["pages"],
                    f"{no_ord:02d}版_{lib.safe_name(name)}.{extension}",
                )
                page_img = os.path.relpath(fimg, aps["dir"])
                page_downloads.append((page_img, b2))
                page_image_meta = image_meta
                page_image_source_url = image_final_url
                break
            if page_img is not None:
                break
        if page_img is None:
            return None, "第%s版版面图所有候选下载失败或不完整；整期未归档" % no_ord
        editions.append({
            "no": no_ord,
            "name": name,
            "page_image": page_img,
            "page_image_source_url": page_image_source_url,
            "page_image_sha256": page_image_meta["sha256"],
            "page_image_width": page_image_meta["width"],
            "page_image_height": page_image_meta["height"],
            "url": u,
            "label": label,
        })
        all_units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no_ord:02d}",
                          "type": "article_text", "title": f"{no_ord}版 {name}",
                          "url": u, "page_image": page_img,
                          "page_image_sha256": page_image_meta["sha256"],
                          "articles": arts})
    if len(editions) != len(expected_pages):
        return None, "发现 %s 版但仅完整解析 %s 版；整期未归档" % (
            len(expected_pages), len(editions)
        )
    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "issue_no": None, "channel": "founder", "editions": editions,
             "units": all_units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        lib.commit_issue_tree(aps["dir"], page_downloads, issue)
    except lib.PIPELINE_FATAL_EXCEPTIONS:
        raise
    except Exception as exc:  # noqa: BLE001
        return None, "整期归档事务失败：%s" % exc
    return issue, None


def parse(src, d, archive_root, max_articles=8):
    """文章全文抽取：content 页 → ozoom/p 文本 → text/ 文件，注入 units。"""
    # Kept for callers that still pass the legacy option.  Parsing is complete
    # or fails explicitly; it never silently drops articles.
    del max_articles
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"] or aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    if issue.get("date") != d.isoformat() or issue.get("source") != src.get("id"):
        return None, "issue.json 来源或日期与请求不一致"
    if not isinstance(issue.get("units"), list) or not issue["units"]:
        return issue, "issue.json 缺版次单元清单"

    parsed = copy.deepcopy(issue)
    for unit_index, u in enumerate(parsed["units"], 1):
        txts = []
        articles = u.get("articles") or []
        if not isinstance(articles, list) or not articles:
            return issue, "第%s版文章清单格式无效" % unit_index
        for article_index, art in enumerate(articles, 1):
            if not isinstance(art, dict):
                return issue, "第%s版第%s篇文章格式无效" % (
                    unit_index, article_index
                )
            embedded = art.get("text") or ""
            url = art.get("url") or ""
            if not url:
                # Some historical imports contain only an embedded full text.
                # When a detail URL exists, however, that endpoint is always
                # authoritative even if layoutData carries a long lead.
                if len(embedded) >= 800:
                    txts.append(f"{art.get('title','')}\n{embedded}")
                    continue
                return issue, "第%s版第%s篇文章缺 URL" % (
                    unit_index, article_index
                )
            url_dates = _dates_in_text(url)
            if url_dates and url_dates != {d}:
                return issue, "第%s版第%s篇文章 URL 日期与请求日期不一致" % (
                    unit_index, article_index
                )
            try:
                st, final_url, raw = lib.http_get(
                    url, referer=issue.get("editions", [{}])[0].get("url")
                )
                if st != 200:
                    return issue, "第%s版第%s篇文章访问失败 %s" % (
                        unit_index, article_index, st
                    )
                if not raw:
                    return issue, "第%s版第%s篇文章响应为空" % (
                        unit_index, article_index
                    )
                h = _best(raw)
                date_error = _response_date_error(d, url, final_url, h)
                if date_error:
                    return issue, "第%s版第%s篇文章%s" % (
                        unit_index, article_index, date_error
                    )
            except lib.PIPELINE_FATAL_EXCEPTIONS:
                raise
            except Exception as exc:  # noqa: BLE001
                return issue, "第%s版第%s篇文章访问异常：%s" % (
                    unit_index, article_index, exc
                )
            # 标题
            title = _article_title(h)
            # 正文：ozoom / articleContent 段落
            paras = []
            seg = _container_tail(h, ("ozoom", "articleContent"))
            if seg is None:
                return issue, "第%s版第%s篇文章缺少正文容器" % (
                    unit_index, article_index
                )
            for p in re.findall(r'<p[^>]*>(.*?)</p>', seg, flags=re.S):
                t = lib.html_text(p.encode("utf-8", "ignore")).replace("&nbsp;", " ").strip()
                t = re.sub(r'<[^>]+>', '', t)
                t = re.sub(r'\s+', ' ', t)
                if t:
                    paras.append(t)
            txt = "\n".join(paras)
            if not txt:
                # 已确认正文容器后，兼容无 <p> 包裹的裸文本正文。
                txt = re.sub(r"<[^>]+>", " ", seg)
                txt = re.sub(r"\s+", " ", txt).replace("&nbsp;", " ").strip()
            if not txt:
                return issue, "第%s版第%s篇文章正文解析为空" % (
                    unit_index, article_index
                )
            art["title"] = title or art.get("title", "")
            art["text"] = txt
            txts.append(f"{art['title']}\n{txt}")
        u["text_path"] = None
        u["text"] = "\n\n".join(txts)
    lib.save_json(aps["issue_json"], parsed)
    return parsed, None
