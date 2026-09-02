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
import copy
import datetime
import hashlib
import html as html_module
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

from urllib.parse import urljoin, urlparse, urlunparse  # noqa: E402


def _best(raw):
    encodings = []
    charset = re.search(
        br"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)", raw[:4096], re.I
    )
    if charset:
        declared = charset.group(1).decode("ascii", "ignore").lower()
        encodings.append("gb18030" if declared in ("gbk", "gb2312") else declared)
    encodings.extend(("utf-8", "gb18030", "latin-1"))
    for enc in dict.fromkeys(encodings):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _get(url, ref=None):
    st, _final_url, text = _get_response(url, ref=ref)
    return st, text


def _get_response(url, ref=None):
    st, final_url, raw = lib.http_get(url, referer=ref)
    return st, final_url, _best(raw) if st == 200 else ""


def _hrefs(document):
    """Return href values with either HTML quote style, preserving order."""
    return [
        match.group(2)
        for match in re.finditer(
            r'\bhref\s*=\s*(["\'])(.*?)\1', document or "", re.I | re.S
        )
    ]


def _container_tail(document, identifiers):
    """Return content after a recognized article container opening tag."""
    accepted = {identifier.lower() for identifier in identifiers}
    for match in re.finditer(r'<(?:article|div|section)\b[^>]*>', document or "", re.I):
        identifier = re.search(
            r'\bid\s*=\s*(["\'])(.*?)\1', match.group(0), re.I | re.S
        )
        if identifier and identifier.group(2).strip().lower() in accepted:
            return document[match.end():]
    return None


def _element_text_by_id(document, identifier):
    pattern = (
        r'<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bid\s*=\s*(["\'])'
        + re.escape(identifier)
        + r'\2[^>]*>(?P<body>.*?)</(?P=tag)\s*>'
    )
    match = re.search(pattern, document or "", re.I | re.S)
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", " ", match.group("body"))
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def _article_title_fields(document):
    """Return the real headline and optional pre-title without truncation."""
    title = _element_text_by_id(document, "Title")
    if not title:
        for attributes in lib.html_tag_attributes(document, "meta"):
            identity = str(
                attributes.get("property") or attributes.get("name") or ""
            ).strip().lower()
            if identity == "og:title":
                title = re.sub(
                    r"\s+", " ",
                    html_module.unescape(str(attributes.get("content") or "")),
                ).strip()
                if title:
                    break
    pretitle = _element_text_by_id(document, "PreTitle")
    return title, pretitle


def _issue_data_url(page_url, day):
    """Resolve data.json at the directory named with the exact issue date."""
    parsed = urlparse(page_url)
    stamp = day.strftime("%Y%m%d")
    parts = parsed.path.split("/")
    try:
        index = len(parts) - 1 - parts[::-1].index(stamp)
    except ValueError:
        return None
    path = "/".join(parts[:index + 1] + ["data.json"])
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _same_https_origin(left, right):
    first, second = urlparse(left), urlparse(right)
    return (
        first.scheme.lower() == second.scheme.lower() == "https"
        and first.netloc.lower() == second.netloc.lower()
    )


_CHINA_TZ = datetime.timezone(datetime.timedelta(hours=8))


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
    """Read only title/date metadata, not historical dates in article bodies."""
    evidence = []
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if title:
        evidence.append(title.group(1))
    for meta in re.findall(r"<meta\b[^>]*>", html or "", re.I):
        if re.search(r"(?:date|publish|issue|pubtime)", meta, re.I):
            evidence.append(meta)
    return _dates_in_text("\n".join(evidence))


def _response_date_error(requested_day, requested_url, final_url, html):
    final_dates = _dates_in_text(final_url)
    document_dates = _document_dates(html)
    if final_dates and final_dates != {requested_day}:
        return "响应跳转日期与请求日期 %s 不一致" % requested_day.isoformat()
    if document_dates and requested_day not in document_dates:
        return "页面内容日期与请求日期 %s 不一致" % requested_day.isoformat()
    requested_dates = _dates_in_text(requested_url)
    if requested_day not in final_dates and requested_day not in document_dates:
        if final_url == requested_url and requested_dates == {requested_day}:
            return None
        return "页面无法确认请求日期 %s" % requested_day.isoformat()
    return None


def _paper_date(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            stamp = float(value)
            if stamp > 100000000000:
                stamp /= 1000.0
            return datetime.datetime.fromtimestamp(stamp, _CHINA_TZ).date()
        except (OverflowError, OSError, ValueError):
            return None
    dates = _dates_in_text(value)
    return next(iter(dates)) if len(dates) == 1 else None


def _walk_entries(value):
    if isinstance(value, dict):
        if "paperCode" in value and "pagePath" in value:
            yield value
        for child in value.values():
            yield from _walk_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_entries(child)


def _index_entries(raw):
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(_walk_entries(payload))


def _matching_entry(cms, raw, requested_day):
    expected_code = str(cms.get("paper_code") or "")
    for entry in _index_entries(raw):
        if str(entry.get("paperCode") or "") != expected_code:
            continue
        path_dates = _dates_in_text(entry.get("pagePath"))
        if (_paper_date(entry.get("paperDate")) == requested_day
                and path_dates == {requested_day}):
            return entry
    return None


def probe(src, d):
    d = lib.norm_day(d)
    st, h = _get(src["cms"]["index_json"], ref=src["cms"]["site"])
    entry = _matching_entry(src["cms"], h, d) if st == 200 else None
    return [{
        "index_ok": bool(entry),
        "requested_date": d.isoformat(),
        "sample": json.dumps(entry, ensure_ascii=False)[:180] if entry else "",
    }]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    cms = src["cms"]
    st, h = _get(cms["index_json"], ref=cms["site"])
    entries = _index_entries(h) if st == 200 else []
    if not entries:
        return None, "index.json 无条目（当日可能未发布）"
    entry = _matching_entry(cms, h, d)
    if entry is None:
        return None, "index.json 无法确认请求日期 %s（当日缺报）" % d.isoformat()
    page1_url = urljoin(
        cms["site"] + ("/" if not cms["site"].endswith("/") else ""),
        str(entry["pagePath"]),
    )

    st, page1_final_url, p1 = _get_response(page1_url)
    if st != 200:
        return None, f"页1 访问失败 {st}"
    date_error = _response_date_error(d, page1_url, page1_final_url, p1)
    if date_error:
        return None, date_error
    page1_url = page1_final_url

    # 版次导航：nmrb_..._N.html（无哈希） 与文章：nmrb_..._N_HASH.html
    page_urls = []
    seen_pages = {}
    for u in _hrefs(p1):
        page_match = re.search(r'(?:\d{8})_\d+_(\d+)\.html$', u)
        if not page_match:
            continue
        number = int(page_match.group(1))
        resolved = urljoin(page1_url, u)
        resolved_dates = _dates_in_text(resolved)
        if resolved_dates and resolved_dates != {d}:
            return None, "版次导航链接日期与请求日期不一致；整期未归档"
        if number in seen_pages:
            if seen_pages[number] == resolved:
                continue
            return None, "版次导航同一版号指向不同链接：%s" % number
        seen_pages[number] = resolved
        page_urls.append((resolved, number))
    if not page_urls:
        return None, "版次导航解析为空；为避免缺版已拒绝归档"
    page_numbers = [number for _url, number in page_urls]
    if page_numbers != list(range(1, len(page_numbers) + 1)):
        return None, "版次导航版号必须连续唯一且从 1 开始：%s" % page_numbers
    max_pages = int(cms.get("max_pages", 16))
    if len(page_urls) > max_pages:
        return None, "版次导航发现 %s 版，超过配置上限 %s；为避免缺版已拒绝归档" % (
            len(page_urls), max_pages
        )

    editions, units = [], []
    for p_url, no in page_urls:
        st, final_url, ph = (
            _get_response(p_url, ref=page1_url) if p_url else (0, p_url, "")
        )
        if st != 200:
            return None, "第%s版访问失败 %s；整期未归档" % (no, st)
        page_date_error = _response_date_error(d, p_url, final_url, ph)
        if page_date_error:
            return None, "第%s版%s；整期未归档" % (no, page_date_error)
        p_url = final_url
        m = re.search(rf'第\s*0?{no}\s*版\s*[：:]\s*([^<"{{}}]{{1,16}})', ph)
        if not m:
            m = re.search(r'第\s*0?\d+\s*版\s*[：:]\s*([^<"]{1,16})', ph)
        name = m.group(1).strip() if m else f"第{no}版"
        art_urls = []
        for u in dict.fromkeys(_hrefs(ph)):
            if not re.search(r'\d{8}_\d+_\d+_\d+\.html$', u):
                continue
            article_url = urljoin(p_url, u)
            article_dates = _dates_in_text(article_url)
            if article_dates and article_dates != {d}:
                return None, "第%s版文章链接日期与请求日期不一致；整期未归档" % no
            art_urls.append(article_url)
        if not art_urls:
            return None, "第%s版文章链接解析为空；整期未归档" % no

        editions.append({"no": no, "name": name, "url": p_url,
                         "article_count": len(art_urls)})
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}", "url": p_url,
                      "article_urls": art_urls})
    if len(units) != len(page_urls):
        return None, "发现 %s 版但仅完整解析 %s 版；整期未归档" % (
            len(page_urls), len(units)
        )
    # The visible #pageImg is only a small thumbnail.  The dated issue-level
    # data.json is the canonical manifest for full-resolution page images.
    data_url = _issue_data_url(page1_url, d)
    if not data_url or not _same_https_origin(cms["site"], data_url):
        return None, "无法在报纸官方 HTTPS 同源路径定位高清版面清单；整期未归档"
    try:
        data_status, data_final_url, data_text = _get_response(
            data_url, ref=page1_url
        )
    except Exception as exc:  # noqa: BLE001
        return None, "高清版面清单下载异常：%s；整期未归档" % exc
    if data_status != 200:
        return None, "高清版面清单下载失败 %s；整期未归档" % data_status
    if not _same_https_origin(cms["site"], data_final_url):
        return None, "高清版面清单发生跨域或非 HTTPS 跳转；整期未归档"
    data_date_error = _response_date_error(d, data_url, data_final_url, data_text)
    if data_date_error:
        return None, "高清版面清单%s；整期未归档" % data_date_error
    try:
        data_rows = json.loads(data_text)
    except (TypeError, ValueError):
        return None, "高清版面清单 JSON 无效；整期未归档"
    if not isinstance(data_rows, list) or not data_rows:
        return None, "高清版面清单为空；整期未归档"
    rows_by_number = {}
    expected_issue = (
        str(entry.get("paperIssueNum")).rsplit("_", 1)[-1]
        if entry.get("paperIssueNum") else None
    )
    for row in data_rows:
        if not isinstance(row, dict):
            return None, "高清版面清单条目格式无效；整期未归档"
        try:
            number = int(str(row.get("pageNo") or ""))
        except ValueError:
            return None, "高清版面清单缺少有效版号；整期未归档"
        if number in rows_by_number:
            return None, "高清版面清单存在重复版号 %s；整期未归档" % number
        if (_paper_date(row.get("paperDate")) != d
                or _paper_date(row.get("issueDate")) != d):
            return None, "高清版面清单第%s版日期不一致；整期未归档" % number
        row_page_href = str(row.get("pageHref") or "")
        row_page_dates = _dates_in_text(row_page_href)
        row_identity = re.search(r'_(\d+)_(\d+)\.html$', row_page_href)
        if (row_page_dates != {d} or not row_identity
                or int(row_identity.group(2)) != number
                or (expected_issue and row_identity.group(1) != expected_issue)):
            return None, "高清版面清单第%s版链接身份不一致；整期未归档" % number
        rows_by_number[number] = row
    if sorted(rows_by_number) != page_numbers:
        return None, "高清版面清单版号与页面导航不一致；整期未归档"

    aps = lib.archive_paths(archive_root, src["id"], d)
    page_downloads = []
    for edition, unit in zip(editions, units):
        number = int(edition["no"])
        row = rows_by_number[number]
        data_articles = []
        for article in row.get("onePageArticleList") or []:
            if not isinstance(article, dict) or not article.get("articleHref"):
                return None, "高清版面清单第%s版文章条目无效；整期未归档" % number
            data_articles.append(urljoin(unit["url"], article["articleHref"]))
        data_articles = list(dict.fromkeys(data_articles))
        if not data_articles or set(data_articles) != set(unit["article_urls"]):
            return None, "第%s版文章链接与高清版面清单不一致；整期未归档" % number
        image_reference = str(row.get("pageBigImgPath") or "")
        if not image_reference:
            return None, "高清版面清单第%s版缺原图；整期未归档" % number
        image_url = urljoin(cms["site"], image_reference)
        if not _same_https_origin(cms["site"], image_url):
            return None, "第%s版原图不是报纸官方 HTTPS 同源资源；整期未归档" % number
        try:
            image_status, image_final_url, image_bytes = lib.http_get(
                image_url, referer=unit["url"]
            )
        except Exception as exc:  # noqa: BLE001
            return None, "第%s版原图下载异常：%s；整期未归档" % (number, exc)
        if image_status != 200:
            return None, "第%s版原图下载失败 %s；整期未归档" % (
                number, image_status
            )
        if not _same_https_origin(cms["site"], image_final_url):
            return None, "第%s版原图发生跨域或非 HTTPS 跳转；整期未归档" % number
        final_image_dates = _dates_in_text(image_final_url)
        if final_image_dates and final_image_dates != {d}:
            return None, "第%s版原图最终 URL 日期不一致；整期未归档" % number
        image_error = lib.image_validation_error(image_bytes, min_bytes=50000)
        if image_error:
            return None, "第%s版原图%s；整期未归档" % (number, image_error)
        width, height = lib.image_dimensions(image_bytes)
        if min(width, height) < 1200 or max(width, height) < 1600:
            return None, "第%s版原图尺寸过小：%sx%s；整期未归档" % (
                number, width, height
            )
        image_format = lib.detect_image_format(image_bytes)
        extension = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}[
            image_format
        ]
        name = str(row.get("pageName") or edition["name"]).strip() or edition["name"]
        image_path = os.path.join(
            aps["pages"], "%02d版_%s.%s" % (number, lib.safe_name(name), extension)
        )
        page_image = os.path.relpath(image_path, aps["dir"])
        page_downloads.append((page_image, image_bytes))
        edition.update({
            "name": name,
            "page_image": page_image,
            "page_image_source_url": image_final_url,
            "page_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "page_image_width": width,
            "page_image_height": height,
            "pdf_url": urljoin(cms["site"], str(row.get("pdfHref") or "")),
        })
        unit.update({
            "title": f"{number}版 {name}",
            "page_image": page_image,
            "page_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        })

    issue_meta = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
                  "issue_no": (str(entry.get("paperIssueNum")).rsplit("_", 1)[-1]
                               if entry.get("paperIssueNum") else None),
                  "channel": "cms_index", "editions": editions, "units": units,
                  "data_json_url": data_final_url,
                  "data_json_sha256": hashlib.sha256(
                      data_text.encode("utf-8")
                  ).hexdigest(),
                  "index_entry": json.dumps(entry, ensure_ascii=False)[:300],
                  "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        lib.commit_issue_tree(aps["dir"], page_downloads, issue_meta)
    except Exception as exc:  # noqa: BLE001
        return None, "整期归档事务失败：%s" % exc
    return issue_meta, None


def parse(src, d, archive_root, max_per_edition=30):
    # Retained for API compatibility only. A successful parse must preserve
    # every article discovered during fetch, regardless of this legacy cap.
    del max_per_edition
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    if issue.get("date") != d.isoformat() or issue.get("source") != src.get("id"):
        return None, "issue.json 来源或日期与请求不一致"
    if not isinstance(issue.get("editions"), list) or not issue["editions"]:
        return issue, "issue.json 缺版次清单"
    if not isinstance(issue.get("units"), list) or not issue["units"]:
        return issue, "issue.json 缺版次单元清单"

    parsed = copy.deepcopy(issue)
    for unit_index, u in enumerate(parsed["units"], 1):
        txts = []
        article_urls = u.get("article_urls") or []
        if not isinstance(article_urls, list) or not article_urls:
            return issue, "第%s版文章清单格式无效" % unit_index
        for article_index, au in enumerate(article_urls, 1):
            if not isinstance(au, str) or not au:
                return issue, "第%s版第%s篇文章缺 URL" % (
                    unit_index, article_index
                )
            url_dates = _dates_in_text(au)
            if url_dates and url_dates != {d}:
                return issue, "第%s版第%s篇文章 URL 日期与请求日期不一致" % (
                    unit_index, article_index
                )
            try:
                st, final_url, h = _get_response(au, ref=u.get("url"))
            except Exception as exc:  # noqa: BLE001
                return issue, "第%s版第%s篇文章访问异常：%s" % (
                    unit_index, article_index, exc
                )
            if st != 200:
                return issue, "第%s版第%s篇文章访问失败 %s" % (
                    unit_index, article_index, st
                )
            if not h:
                return issue, "第%s版第%s篇文章响应为空" % (
                    unit_index, article_index
                )
            date_error = _response_date_error(d, au, final_url, h)
            if date_error:
                return issue, "第%s版第%s篇文章%s" % (
                    unit_index, article_index, date_error
                )
            # 标题
            title, pretitle = _article_title_fields(h)
            seg = _container_tail(h, ("ozoom",))
            if seg is None:
                return issue, "第%s版第%s篇文章缺少正文容器" % (
                    unit_index, article_index
                )
            paras = []
            for p in re.findall(r'<p[^>]*>(.*?)</p>', seg, flags=re.S):
                t = re.sub(r"<[^>]+>", " ", lib.html_text(p.encode("utf-8", "ignore")))
                t = re.sub(r"\s+", " ", t).replace("&nbsp;", " ").strip()
                if t:
                    paras.append(t)
            txt = "\n".join(paras)
            if not txt:
                return issue, "第%s版第%s篇文章正文解析为空" % (
                    unit_index, article_index
                )
            txts.append((title, pretitle, txt))
        u["articles"] = [
            {
                "title": title,
                **({"pretitle": pretitle} if pretitle else {}),
                "text": text,
            }
            for title, pretitle, text in txts
        ]
        u["text"] = "\n\n".join(
            f"{title}\n{text}" for title, _pretitle, text in txts
        )
    lib.save_json(aps["issue_json"], parsed)
    return parsed, None
