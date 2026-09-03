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
import copy
import datetime
from html.parser import HTMLParser
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

from urllib.parse import urljoin, urlsplit  # noqa: E402


_COMPACT_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{6})(?!\d)")
_DATED_EDITION_DIR_RE = re.compile(r"(?:^|/)(\d{8})_\d{3}(?:/|$)")
_EDITION_DIR_RE = re.compile(r"(?:^|/)((?:19|20)\d{6}_\d{3})(?:/|$)")
_ARTICLE_HREF_RE = re.compile(
    r"(?:^|/)content_[^/?#]+\.html?(?:[?#]|$)", re.IGNORECASE
)
_NAV_TEXT_RE = re.compile(r"^第\s*(\d+)\s*版(?:\s*(.*))?$", re.DOTALL)
_PAGE_IMAGE_RE = re.compile(
    r"(?:^|/)((?:19|20)\d{6}_\d{3})/[^/?#]*-m-\d{3}-300\.jpe?g(?:[?#]|$)",
    re.IGNORECASE,
)
_MIN_PAGE_IMAGE_BYTES = 60000
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
_IGNORED_CONTENT_TAGS = {"script", "style", "noscript", "template"}


def _normalized_text(parts):
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _edition_dir(href):
    match = _EDITION_DIR_RE.search(urlsplit(str(href or "")).path)
    return match.group(1) if match else None


class _IndexParser(HTMLParser):
    """Extract mobile index records independently of quote and attribute order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.navs = []
        self.articles = []
        self.page_images = []
        self._captures = []

    def _observe_attributes(self, attrs):
        for _name, value in attrs:
            if not value:
                continue
            match = _PAGE_IMAGE_RE.search(value)
            if match:
                self.page_images.append((value, match.group(1)))

    def _begin_capture(self, tag, attrs):
        attributes = {str(name).lower(): value for name, value in attrs}
        pdf_href = attributes.get("pdf_href")
        article_href = attributes.get("data-href")
        if pdf_href:
            self._captures.append({
                "tag": tag,
                "kind": "nav",
                "href": pdf_href,
                "text": [],
            })
        if article_href and _ARTICLE_HREF_RE.search(article_href):
            self._captures.append({
                "tag": tag,
                "kind": "article",
                "href": article_href,
                "text": [],
            })

    def handle_starttag(self, tag, attrs):
        self._observe_attributes(attrs)
        self._begin_capture(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._observe_attributes(attrs)
        before = len(self._captures)
        self._begin_capture(tag, attrs)
        while len(self._captures) > before:
            self._finish_capture(len(self._captures) - 1)

    def handle_data(self, data):
        for capture in self._captures:
            capture["text"].append(data)

    def handle_endtag(self, tag):
        for index in range(len(self._captures) - 1, -1, -1):
            if self._captures[index]["tag"] == tag:
                self._finish_capture(index)
                break

    def _finish_capture(self, index):
        capture = self._captures.pop(index)
        text = _normalized_text(capture["text"])
        if capture["kind"] == "nav":
            match = _NAV_TEXT_RE.match(text)
            if match:
                self.navs.append((
                    capture["href"], match.group(1), (match.group(2) or "").strip()
                ))
        else:
            self.articles.append((
                capture["href"], _edition_dir(capture["href"]), text
            ))


class _ContentParser(HTMLParser):
    """Extract only the explicitly marked article body, never the HTTP shell."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = False
        self.closed = False
        self.parts = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attributes = {str(name).lower(): value for name, value in attrs}
        if not self.found:
            if str(attributes.get("id") or "").strip() == "content":
                self.found = True
                self._stack = [tag]
            return
        if not self._stack:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag not in _VOID_TAGS:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        del attrs
        if self._stack and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._stack and not any(
            tag in _IGNORED_CONTENT_TAGS for tag in self._stack[1:]
        ):
            self.parts.append(data)

    def handle_endtag(self, tag):
        if not self._stack or tag not in self._stack:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        matching_index = len(self._stack) - 1 - self._stack[::-1].index(tag)
        del self._stack[matching_index:]
        if not self._stack:
            self.closed = True


def _parse_index(html):
    parser = _IndexParser()
    parser.feed(html)
    parser.close()
    return parser.navs, parser.articles, parser.page_images


def _extract_content(html):
    parser = _ContentParser()
    parser.feed(html)
    parser.close()
    return parser.found, parser.closed, _normalized_text(parser.parts)


def _fmt(tpl, d):
    d = lib.norm_day(d)
    return tpl.format(y=d.year, yymmdd=d.strftime("%Y%m%d"))


def _parse_compact_date(value):
    try:
        return datetime.datetime.strptime(value, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _url_path_dates(url):
    """Return valid calendar dates embedded in a response URL path."""
    path = urlsplit(str(url or "")).path
    return {
        parsed
        for token in _COMPACT_DATE_RE.findall(path)
        for parsed in [_parse_compact_date(token)]
        if parsed is not None
    }


def _edition_dir_date(href):
    match = _DATED_EDITION_DIR_RE.search(urlsplit(str(href or "")).path)
    return _parse_compact_date(match.group(1)) if match else None


def _validate_index_date(d, final_url, navs, articles):
    """Require independent target-date evidence from URL and edition navigation."""
    d = lib.norm_day(d)
    final_dates = _url_path_dates(final_url)
    if not final_dates:
        return "无法确认索引最终 URL 日期"
    if final_dates != {d}:
        return "索引最终 URL 日期与请求日期不一致"

    if not navs:
        return "无法确认版次导航日期"
    nav_dates = [_edition_dir_date(pdf_href) for pdf_href, _, _ in navs]
    if any(nav_day is None for nav_day in nav_dates):
        return "无法确认版次导航日期"
    if any(nav_day != d for nav_day in nav_dates):
        return "版次导航日期与请求日期不一致"

    for article_href, _, _ in articles:
        article_day = _edition_dir_date(article_href)
        if article_day is None:
            return "无法确认文章导航日期"
        if article_day != d:
            return "文章导航日期与请求日期不一致"
    return None


def _inventory(src, d, final_url, navs, articles):
    """Validate the complete index before downloading or writing any page."""
    date_error = _validate_index_date(d, final_url, navs, articles)
    if date_error:
        return None, None, date_error
    if not navs:
        return None, None, "版次解析失败（索引结构可能变更）"

    max_pages = int(src["mob"].get("max_pages", 24))
    if len(navs) > max_pages:
        return None, None, (
            "版次导航发现 %s 版，超过配置上限 %s；为避免缺版已拒绝归档"
            % (len(navs), max_pages)
        )
    edition_numbers = [int(no) for _pdf_href, no, _name in navs]
    if edition_numbers != list(range(1, len(edition_numbers) + 1)):
        return None, None, "版次导航版号必须连续唯一且从 1 开始：%s" % edition_numbers

    nav_rows = []
    seen_edition_dirs = set()
    for pdf_href, raw_no, name in navs:
        no = int(raw_no)
        edition_dir = _edition_dir(pdf_href)
        if not edition_dir:
            return None, None, "第%s版导航缺少可验证版目录；整期未归档" % no
        if int(edition_dir.rsplit("_", 1)[-1]) != no:
            return None, None, "第%s版导航目录与真实版号不一致；整期未归档" % no
        if edition_dir in seen_edition_dirs:
            return None, None, "多个版次指向同一版目录 %s；整期未归档" % edition_dir
        seen_edition_dirs.add(edition_dir)
        nav_rows.append((pdf_href, no, name.strip(), edition_dir))

    articles_by_dir = {
        edition_dir: [] for _href, _no, _name, edition_dir in nav_rows
    }
    seen_article_urls = set()
    for article_href, edition_dir, title in articles:
        if edition_dir not in articles_by_dir:
            return None, None, "发现不属于任何版次的文章目录 %s；整期未归档" % edition_dir
        article_url = urljoin(final_url, article_href)
        if article_url in seen_article_urls:
            continue
        seen_article_urls.add(article_url)
        articles_by_dir[edition_dir].append({"title": title, "url": article_url})

    for _href, no, _name, edition_dir in nav_rows:
        if not articles_by_dir[edition_dir]:
            return None, None, "第%s版未发现任何文章，无法确认整期完整性" % no
    return nav_rows, articles_by_dir, None


def probe(src, d):
    d = lib.norm_day(d)
    st, final_url, raw = lib.http_get(
        _fmt(src["mob"]["index_tpl"], d), referer=src["mob"]["site"]
    )
    if st != 200:
        return [{"note": f"索引 {st}"}]
    h = lib.html_text(raw)
    navs, articles, _page_images = _parse_index(h)
    nav_rows, _articles_by_dir, inventory_error = _inventory(
        src, d, final_url, navs, articles
    )
    if inventory_error:
        return [{"note": inventory_error}]
    arts = len(articles)
    return [{"index_ok": True, "editions": [(int(n), nm.strip()) for _, n, nm in navs],
             "article_refs": arts}]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    index_url = _fmt(src["mob"]["index_tpl"], d)
    st, final_url, raw = lib.http_get(index_url, referer=src["mob"]["site"])
    if st != 200:
        return None, f"索引不可达 {st}"
    h = lib.html_text(raw)

    # 版次：pdf_href 携带目录前缀（../20260902_001/）
    navs, articles, page_images = _parse_index(h)
    nav_rows, articles_by_dir, inventory_error = _inventory(
        src, d, final_url, navs, articles
    )
    if inventory_error:
        return None, inventory_error

    aps = lib.archive_paths(archive_root, src["id"], d)
    # 版面图：优先用索引明示的图片 URL，没有时按站点契约构造。
    images_by_dir = {}
    for image_href, edition_dir in page_images:
        candidates = images_by_dir.setdefault(edition_dir, [])
        if image_href not in candidates:
            candidates.append(image_href)

    editions, units = [], []
    page_downloads = []

    for pdf_href, no, name, edition_dir in nav_rows:
        # 版图
        image_candidates = images_by_dir.get(edition_dir, [])
        if len(image_candidates) > 1:
            return None, "第%s版发现多个不同的版面图 URL，无法确认完整性" % no
        image_href = image_candidates[0] if image_candidates else (
            f"{edition_dir}/news-bjrb-00000-{d.strftime('%Y%m%d')}-m-{no:03d}-300.jpg"
        )
        purl = urljoin(final_url, image_href)
        try:
            st2, image_final_url, b2 = lib.http_get(purl, referer=final_url)
        except lib.PIPELINE_FATAL_EXCEPTIONS:
            raise
        except Exception as exc:  # noqa: BLE001
            return None, "第%s版版面图访问异常：%s" % (no, exc)
        if st2 != 200:
            return None, "第%s版版面图访问失败 %s" % (no, st2)
        image_dates = _url_path_dates(image_final_url)
        if not image_dates:
            return None, "无法确认第%s版版面图最终 URL 日期" % no
        if image_dates != {d}:
            return None, "第%s版版面图最终 URL 日期与请求日期不一致" % no
        image_meta, image_error = lib.validate_page_image(
            b2, min_bytes=_MIN_PAGE_IMAGE_BYTES
        )
        if image_error:
            return None, "第%s版版面图无效：%s；整期未归档" % (
                no, image_error
            )
        safe_edition_name = lib.safe_name(name)
        filename = "%02d版%s.jpg" % (
            no, ("_" + safe_edition_name) if safe_edition_name else ""
        )
        fimg = os.path.join(aps["pages"], filename)
        page_img = os.path.relpath(fimg, aps["dir"])
        page_downloads.append((page_img, b2))
        # 本版文章
        arts = articles_by_dir[edition_dir]
        editions.append({
            "no": no,
            "name": name,
            "page_image": page_img,
            "page_image_source_url": image_final_url,
            "page_image_sha256": image_meta["sha256"],
            "page_image_width": image_meta["width"],
            "page_image_height": image_meta["height"],
            "url": final_url,
            "pdf_href": pdf_href,
        })
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}",
                      "url": final_url, "page_image": page_img,
                      "page_image_sha256": image_meta["sha256"],
                      "articles": arts})
    if not units:
        return None, "版次解析失败（索引结构可能变更）"

    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "issue_no": None, "channel": "mobile_epaper", "editions": editions,
             "units": units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        lib.commit_issue_tree(aps["dir"], page_downloads, issue)
    except lib.PIPELINE_FATAL_EXCEPTIONS:
        raise
    except Exception as exc:  # noqa: BLE001
        return None, "整期归档事务失败：%s" % exc
    return issue, None


def parse(src, d, archive_root, max_per_edition=20):
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"

    if issue.get("source") != src.get("id"):
        return issue, "归档来源与请求来源不一致"
    if issue.get("date") != d.isoformat():
        return issue, "归档日期与请求日期不一致"
    units = issue.get("units")
    if not isinstance(units, list) or not units:
        return issue, "issue.json 缺版次单元清单"

    parsed = copy.deepcopy(issue)
    for unit_index, u in enumerate(parsed["units"], 1):
        txts = []
        parsed_articles = []
        articles = u.get("articles")
        if not isinstance(articles, list):
            return issue, "第%s版文章清单格式无效" % unit_index
        if not articles:
            return issue, "第%s版文章清单为空，拒绝标记整期已解析" % unit_index
        for article_index, a in enumerate(articles, 1):
            if not isinstance(a, dict) or not a.get("url"):
                return issue, "第%s版第%s篇文章缺 URL" % (
                    unit_index, article_index
                )
            article_dates = _url_path_dates(a["url"])
            if not article_dates:
                return issue, "无法确认文章 URL 日期"
            if article_dates != {d}:
                return issue, "文章 URL 日期与请求日期不一致"

            try:
                st, final_url, raw = lib.http_get(
                    a["url"], referer=u.get("url")
                )
            except lib.PIPELINE_FATAL_EXCEPTIONS:
                raise
            except Exception as exc:  # noqa: BLE001
                return issue, "第%s版第%s篇文章访问异常：%s" % (
                    unit_index, article_index, exc
                )
            if st != 200:
                return issue, "第%s版第%s篇文章访问失败 %s" % (
                    unit_index, article_index, st
                )
            if not raw:
                return issue, "第%s版第%s篇文章响应为空" % (
                    unit_index, article_index
                )
            final_dates = _url_path_dates(final_url)
            if not final_dates:
                return issue, "无法确认文章最终 URL 日期"
            if final_dates != {d}:
                return issue, "文章最终 URL 日期与请求日期不一致"
            h = lib.html_text(raw)
            content_found, content_closed, t = _extract_content(h)
            if not content_found:
                return issue, "第%s版第%s篇文章缺少明确 id=content 正文容器" % (
                    unit_index, article_index
                )
            if not content_closed:
                return issue, "第%s版第%s篇文章 id=content 正文容器不完整" % (
                    unit_index, article_index
                )
            parsed_article = dict(a)
            parsed_article["url"] = final_url
            parsed_article["text"] = t
            if len(t) < 40:
                return issue, "第%s版第%s篇文章正文为空或过短" % (
                    unit_index, article_index
                )
            parsed_articles.append(parsed_article)
            txts.append(f"{a.get('title','')}\n{t}")
        u["articles"] = parsed_articles
        u["text"] = "\n\n".join(txts)

    lib.save_json(aps["issue_json"], parsed)
    return parsed, None
