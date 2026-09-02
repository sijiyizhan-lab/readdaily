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
import copy
import datetime
import html
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402


def _valid_date(year, month, day):
    try:
        return datetime.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _dates_in(value):
    """Return valid publication dates explicitly present in API fields/URLs."""
    text = str(value or "")
    found = set()
    for pattern in (
            r"(?<!\d)((?:19|20)\d{2})[-_/](\d{1,2})[-_/](\d{1,2})(?!\d)",
            r"(?<!\d)((?:19|20)\d{2})(\d{2})[-_/](\d{1,2})(?!\d)",
            r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)",
            r"(?<!\d)((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日"):
        for match in re.finditer(pattern, text):
            parsed = _valid_date(*match.groups())
            if parsed is not None:
                found.add(parsed)
    return found


def _exact_date_error(value, requested_day, label):
    dates = _dates_in(value)
    if not dates:
        return "无法确认%s日期" % label
    if dates != {requested_day}:
        return "%s日期与请求日期不一致" % label
    return None


def _metadata_date_error(value, requested_day, label):
    """Reject explicit publication-date metadata that names another issue day."""
    evidence = []
    date_key = re.compile(
        r"(?:publish|period|paper.?date|pub.?date|article.?date|^date$|url)",
        re.I,
    )

    def collect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if date_key.search(str(key)) and not isinstance(child, (dict, list)):
                    evidence.append(child)
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    dates = set()
    for item in evidence:
        dates.update(_dates_in(item))
    if dates and dates != {requested_day}:
        return "%s日期与请求日期不一致" % label
    return None


def _api(src, path, data):
    base = src.get("api", {}).get("base", "").rstrip("/")
    url = f"{base}{path}"
    headers = {
        "User-Agent": lib.UA,
        "Content-Type": "application/json",
        "Referer": src.get("entry") or base,
    }
    try:
        import requests

        response = requests.post(url, json=data, timeout=20, headers=headers)
        if response.status_code != 200:
            return None
        return response.json()
    except ImportError:
        request = urllib.request.Request(
            url,
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    return None
                return json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


def probe(src, d):
    d = lib.norm_day(d)
    code = src["api"]["code"]
    r = _api(src, "/uv/article/period/date", {"date": d.isoformat(), "code": code})
    if not r or not r.get("obj"):
        return [{"note": f"dateList 为空（{d} 可能未出版）"}]
    date_list = r["obj"].get("dateList", [])
    matching = any(_dates_in(value) == {d} for value in date_list)
    if not matching:
        return [{"note": "dateList 无法确认请求日期 %s" % d.isoformat()}]
    return [{"dateList": date_list}]


def fetch(src, d, archive_root):
    d = lib.norm_day(d)
    code = src["api"]["code"]
    r = _api(src, "/uv/article/period/periodTime", {"date": d.isoformat(), "code": code})
    if not r or not isinstance(r.get("obj"), dict):
        return None, "无版次数据（可能尚未出版）"
    obj = r["obj"]
    period_error = _exact_date_error(obj.get("periodTime"), d, "出版期")
    if period_error:
        return None, period_error
    edlist = obj.get("editionList") or []
    if not edlist:
        return None, "无版次数据（可能尚未出版）"

    normalized_editions = []
    edition_numbers = []
    for ed in edlist:
        if not isinstance(ed, dict):
            return None, "API 版次清单格式无效"
        raw_name = ed.get("editionName")
        if not isinstance(raw_name, str):
            return None, "API 版次名称无法读取真实版号：%r" % (
                raw_name,
            )
        match = re.search(r'第\s*0*(\d+)\s*版', raw_name)
        if not match:
            return None, "API 版次名称无法读取真实版号：%r" % raw_name
        number = int(match.group(1))
        name = re.sub(r'^.*?第\s*0*\d+\s*版\s*[：:]?\s*', '', raw_name).strip()
        if not name:
            name = "第%s版" % number
        edition_numbers.append(number)
        normalized_editions.append((ed, number, name))
    if edition_numbers != list(range(1, len(edition_numbers) + 1)):
        return None, "API 版号必须连续唯一且从 1 开始：%s" % edition_numbers

    # Validate every dated resource before creating the target issue directory.
    for ed, _number, _name in normalized_editions:
        page_img = ed.get("editionImg")
        if not isinstance(page_img, str) or not page_img.strip():
            return None, "第%s版缺少版面图 URL；整期未归档" % _number
        image_error = _exact_date_error(page_img, d, "版面图")
        if image_error:
            return None, image_error

    aps = lib.archive_paths(archive_root, src["id"], d)
    editions, units = [], []
    page_downloads = []
    for ed, no, name in normalized_editions:
        page_img = ed.get("editionImg")
        try:
            st, final_url, raw = lib.http_get(
                page_img, referer=src.get("entry")
            )
        except Exception as exc:  # noqa: BLE001
            return None, "第%s版版面图下载异常：%s；整期未归档" % (no, exc)
        if st != 200:
            return None, "第%s版版面图下载失败 %s；整期未归档" % (no, st)
        image_error = _exact_date_error(final_url, d, "版面图最终 URL")
        if image_error:
            return None, image_error
        image_meta, validation_error = lib.validate_page_image(
            raw, min_bytes=50001
        )
        if validation_error:
            return None, "第%s版版面图%s；整期未归档" % (
                no, validation_error
            )
        extension = {
            "jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp",
        }[image_meta["format"]]
        fimg = os.path.join(
            aps["pages"], f"{no:02d}版_{lib.safe_name(name)}.{extension}"
        )
        page_file = os.path.relpath(fimg, aps["dir"])
        page_downloads.append((page_file, raw))
        editions.append({
            "no": no,
            "name": name,
            "page_image": page_file,
            "page_image_source_url": final_url,
            "page_image_sha256": image_meta["sha256"],
            "page_image_width": image_meta["width"],
            "page_image_height": image_meta["height"],
            "api_id": ed.get("id"),
            "period_id": ed.get("periodId"),
        })
        units.append({"id": f"{src['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "article_text", "title": f"{no}版 {name}",
                      "page_image": page_file,
                      "page_image_sha256": image_meta["sha256"],
                      "api_id": ed.get("id"), "period_id": ed.get("periodId")})
    issue = {"source": src["id"], "source_name": src["name"], "date": d.isoformat(),
             "period_time": d.isoformat(),
             "issue_no": None, "channel": "paper_api", "editions": editions,
             "units": units, "fetched_at": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        lib.commit_issue_tree(aps["dir"], page_downloads, issue)
    except Exception as exc:  # noqa: BLE001
        return None, "整期归档事务失败：%s" % exc
    return issue, None


def parse(src, d, archive_root):
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, src["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    if issue.get("source") != src.get("id"):
        return issue, "归档来源与请求来源不一致"
    if issue.get("date") != d.isoformat():
        return issue, "归档日期与请求日期不一致"
    if issue.get("period_time") is not None:
        period_error = _exact_date_error(issue.get("period_time"), d, "归档出版期")
        if period_error:
            return issue, period_error
    units = issue.get("units")
    if not isinstance(units, list) or not units:
        return issue, "issue.json 缺版次单元清单"

    parsed = copy.deepcopy(issue)
    for unit_index, u in enumerate(parsed["units"], 1):
        if not u.get("api_id") or not u.get("period_id"):
            return issue, "第%s版缺少 API 版次标识" % unit_index
        try:
            r = _api(src, "/uv/article/article/editionId",
                     {"id": u.get("api_id"), "period_id": u.get("period_id")})
        except Exception as exc:  # noqa: BLE001
            return issue, "第%s版文章列表请求异常：%s" % (unit_index, exc)
        if not isinstance(r, dict) or not r:
            return issue, "第%s版文章列表响应为空" % unit_index
        date_error = _metadata_date_error(r, d, "第%s版文章列表" % unit_index)
        if date_error:
            return issue, date_error
        article_list = r.get("list")
        if not isinstance(article_list, list) or not article_list:
            return issue, "第%s版文章列表响应为空" % unit_index
        arts = []
        txts = []
        for article_index, a in enumerate(article_list, 1):
            if not isinstance(a, dict):
                return issue, "第%s版第%s篇文章响应格式无效" % (
                    unit_index, article_index
                )
            date_error = _metadata_date_error(
                a, d, "第%s版第%s篇文章" % (unit_index, article_index)
            )
            if date_error:
                return issue, date_error
            title = re.sub(r"<[^>]+>", "", html.unescape(a.get("title") or ""))
            aid = a.get("id")
            if aid is None or (isinstance(aid, str) and not aid.strip()):
                return issue, "第%s版第%s篇缺少文章标识，无法获取全文正文" % (
                    unit_index, article_index
                )
            # 列表接口的 content 即使很长也可能只是导语；articleId 详情
            # 才是本适配器可确认的权威全文来源，因此每篇都必须请求。
            try:
                ra = _api(src, "/uv/article/article/articleId", {"id": aid})
            except Exception as exc:  # noqa: BLE001
                return issue, "第%s版第%s篇全文请求异常：%s" % (
                    unit_index, article_index, exc
                )
            if not isinstance(ra, dict) or not ra:
                return issue, "第%s版第%s篇全文响应为空" % (
                    unit_index, article_index
                )
            date_error = _metadata_date_error(
                ra, d, "第%s版第%s篇全文" % (unit_index, article_index)
            )
            if date_error:
                return issue, date_error
            obj = ra.get("obj")
            vo = obj.get("articleVo") if isinstance(obj, dict) else None
            if not isinstance(vo, dict) or not vo:
                return issue, "第%s版第%s篇全文响应为空" % (
                    unit_index, article_index
                )
            detail_id = vo.get("id")
            if detail_id is not None and str(detail_id) != str(aid):
                return issue, "第%s版第%s篇全文文章标识不一致" % (
                    unit_index, article_index
                )
            raw_content = vo.get("content")
            if not isinstance(raw_content, str):
                return issue, "第%s版第%s篇全文正文为空" % (
                    unit_index, article_index
                )
            content = re.sub(r"<[^>]+>", " ", html.unescape(raw_content))
            content = re.sub(r"\s+", " ", content).strip()
            if not content:
                return issue, "第%s版第%s篇全文正文为空" % (
                    unit_index, article_index
                )
            arts.append({"title": title.strip(), "text": content})
            if content:
                txts.append(f"{title.strip()}\n{content}")
        u["articles"] = arts
        u["text"] = "\n\n".join(txts)
    lib.save_json(aps["issue_json"], parsed)
    return parsed, None
