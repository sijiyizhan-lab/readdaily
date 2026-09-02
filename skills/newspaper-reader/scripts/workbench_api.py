#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned JSON command API for the native readdaily workbench."""

import argparse
import datetime as _datetime
import glob
import json
import os
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent
FETCH_SCRIPTS = HERE.parents[1] / "newspaper-fetch" / "scripts"
if str(FETCH_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(FETCH_SCRIPTS))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import local_pdf  # noqa: E402
import vault_publisher  # noqa: E402


SCHEMA_VERSION = 1
TOPICS = vault_publisher.TOPICS
CONSTRUCTION_SOURCE = vault_publisher.SUPPORTED_SOURCE
MAX_PDF_BYTES = local_pdf.MAX_PDF_BYTES
DEFAULT_ARCHIVE = os.environ.get("READDAILY_ARCHIVE") or os.path.expanduser(
    "~/Library/Application Support/readdaily/news-archive"
)
DEFAULT_VAULT = os.environ.get("READDAILY_VAULT") or os.path.expanduser(
    "~/Library/Application Support/readdaily/vault"
)
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FACT_FIELDS = ("subject", "action", "object", "value", "unit", "time", "source")


class APIError(RuntimeError):
    code = "api_error"


class ValidationError(APIError):
    code = "validation_error"


class NotFoundError(APIError):
    code = "not_found"


class PathSafetyError(ValidationError):
    code = "path_safety_error"


def _now():
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_source(source):
    source = str(source or "")
    if not _SOURCE_RE.fullmatch(source):
        raise ValidationError("source 只能包含字母、数字、点、下划线和连字符")
    return source


def _validate_day(day):
    try:
        return _datetime.date.fromisoformat(str(day)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError("日期必须为 YYYY-MM-DD") from exc


def _root(path):
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _safe_internal(root, *parts):
    base = _root(root)
    target = Path(os.path.abspath(os.path.join(str(base), *[str(x) for x in parts])))
    try:
        if os.path.commonpath([str(base), str(target)]) != str(base):
            raise PathSafetyError("内部路径越界")
    except ValueError as exc:
        raise PathSafetyError("内部路径越界") from exc
    if os.path.lexists(str(base)):
        if not base.is_dir():
            raise PathSafetyError("配置的数据根不是目录")
        base_real = os.path.realpath(str(base))
        probe = str(target)
        while not os.path.lexists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            if os.path.commonpath([base_real, os.path.realpath(probe)]) != base_real:
                raise PathSafetyError("内部路径存在符号链接越界")
        except ValueError as exc:
            raise PathSafetyError("内部路径存在符号链接越界") from exc
    return target


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValidationError("JSON 无法读取：%s" % path) from exc


def _atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, os.getpid()))
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _issue_dir(archive_root, source, day):
    return _safe_internal(archive_root, _validate_source(source), _validate_day(day))


def _safe_issue_file(issue_dir, relative, label, warnings):
    """Resolve an untrusted issue relative path without following it outside."""
    if not relative:
        return None
    rel = str(relative)
    if os.path.isabs(rel):
        warnings.append("%s 为绝对路径，已拒绝读取（路径越界）" % label)
        return None
    target = Path(os.path.abspath(os.path.join(str(issue_dir), os.path.normpath(rel))))
    base = os.path.abspath(str(issue_dir))
    try:
        if os.path.commonpath([base, str(target)]) != base:
            warnings.append("%s 路径越界，已拒绝读取：%s" % (label, rel))
            return None
    except ValueError:
        warnings.append("%s 路径越界，已拒绝读取：%s" % (label, rel))
        return None
    issue_real = os.path.realpath(base)
    probe = str(target)
    while not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        if os.path.commonpath([issue_real, os.path.realpath(probe)]) != issue_real:
            warnings.append("%s 存在符号链接越界，已拒绝读取：%s" % (label, rel))
            return None
    except ValueError:
        warnings.append("%s 存在符号链接越界，已拒绝读取：%s" % (label, rel))
        return None
    return target


def _normal_rel(value):
    if value is None:
        return None
    return os.path.normpath(str(value).replace("\\", "/")).replace("\\", "/")


def _read_text_file(path, label, warnings):
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.append("%s 文件缺失：%s" % (label, path.name))
    except (OSError, UnicodeError):
        warnings.append("%s 无法读取：%s" % (label, path.name))
    return ""


def _unit_text(unit, issue_dir, warnings, edition_no=None):
    """Read one unit without double-counting aggregate and article text."""
    text_path = unit.get("text_path")
    if text_path:
        path = _safe_issue_file(issue_dir, text_path, "正文", warnings)
        text = _read_text_file(path, "正文", warnings)
        if text.strip():
            return text.strip(), "text_path"

    # Some early wechat imports wrote the conventional OCR files but omitted
    # ``text_path`` from issue.json.  Recover those files without allowing a
    # broad directory scan or an out-of-tree path.
    inferred_no = edition_no
    if inferred_no is None:
        match = re.search(r"_(\d{1,3})$", str(unit.get("id") or ""))
        if not match:
            match = re.match(r"\s*(\d{1,3})\s*版", str(unit.get("title") or ""))
        inferred_no = int(match.group(1)) if match else None
    if inferred_no is not None:
        for relative in (
            "text/edition_%02d.txt" % int(inferred_no),
            "text/%02d.txt" % int(inferred_no),
        ):
            path = _safe_issue_file(issue_dir, relative, "正文", warnings)
            if path is not None and path.is_file():
                text = _read_text_file(path, "正文", warnings)
                if text.strip():
                    return text.strip(), "conventional_text_path"

    embedded = str(unit.get("text") or "").strip()
    parts = [embedded] if embedded else []
    normalized_seen = {re.sub(r"\s+", "", embedded)} if embedded else set()
    for article in unit.get("articles") or []:
        article_text = str(article.get("text") or "").strip()
        if not article_text:
            continue
        compact = re.sub(r"\s+", "", article_text)
        if not compact:
            continue
        # Aggregate unit.text commonly already contains every article.  Do not
        # append any article text that is already represented there.
        if embedded and compact in re.sub(r"\s+", "", embedded):
            continue
        if compact in normalized_seen:
            continue
        title = str(article.get("title") or "").strip()
        parts.append((title + "\n" if title else "") + article_text)
        normalized_seen.add(compact)
    if parts:
        return "\n\n".join(parts), "embedded"
    return "", "none"


def _draft_path(archive_root, source, day):
    return _safe_internal(archive_root, "_drafts", _validate_source(source),
                          _validate_day(day) + ".json")


def _load_draft(archive_root, source, day):
    return _load_json(_draft_path(archive_root, source, day), {}) or {}


def _summary_sidecar(archive_root, source, day):
    path = _safe_internal(archive_root, "_summaries", _validate_source(source),
                          _validate_day(day) + ".json")
    data = _load_json(path, {}) or {}
    return {str(item.get("id")): item for item in data.get("units", [])
            if isinstance(item, dict) and item.get("id") is not None}


def _existing_summary(unit, sidecar_item):
    embedded = unit.get("summary")
    if isinstance(embedded, dict):
        embedded_record = embedded
    elif isinstance(embedded, str):
        embedded_record = {"summary": embedded}
    else:
        embedded_record = {}
    summary = str(embedded_record.get("summary") or sidecar_item.get("summary") or "").strip()
    importance = embedded_record.get("importance")
    if importance is None:
        importance = sidecar_item.get("importance")
    if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 5:
        importance = 3
    return summary, importance


def _match_editions(issue):
    editions = list(issue.get("editions") or [])
    units = list(issue.get("units") or [])
    unused = set(range(len(units)))
    matched = []
    for index, edition in enumerate(editions):
        edition_image = _normal_rel(edition.get("page_image"))
        unit_index = None
        if edition_image:
            unit_index = next((candidate for candidate in sorted(unused)
                               if _normal_rel(units[candidate].get("page_image")) == edition_image), None)
        if unit_index is None and index in unused:
            unit_index = index
        unit = units[unit_index] if unit_index is not None else {}
        if unit_index is not None:
            unused.discard(unit_index)
        matched.append((edition, unit))
    for unit_index in sorted(unused):
        unit = units[unit_index]
        number = unit_index + 1
        title = str(unit.get("title") or "")
        match = re.match(r"\s*(\d+)\s*版\s*(.*)", title)
        if match:
            number = int(match.group(1))
            name = match.group(2).strip() or "待复核"
        else:
            name = title or "待复核"
        matched.append(({
            "no": number,
            "name": name,
            "page_image": unit.get("page_image"),
        }, unit))
    return matched


def _resolve_imported_pdf(archive_root, issue, warnings):
    raw = (issue.get("files") or {}).get("local_pdf") or issue.get("pdf_path")
    if not raw:
        return None
    archive = _root(archive_root)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = archive / candidate
    candidate = Path(os.path.abspath(str(candidate)))
    archive_real = os.path.realpath(str(archive))
    try:
        if os.path.commonpath([archive_real, os.path.realpath(str(candidate))]) != archive_real:
            warnings.append("PDF 路径越界，已忽略")
            return None
    except ValueError:
        warnings.append("PDF 路径越界，已忽略")
        return None
    if not candidate.is_file():
        warnings.append("已关联 PDF 文件缺失")
        return None
    return str(candidate)


def get_issue(archive_root, source, day):
    source = _validate_source(source)
    day = _validate_day(day)
    issue_dir = _issue_dir(archive_root, source, day)
    issue_path = issue_dir / "issue.json"
    issue = _load_json(issue_path)
    if not issue:
        raise NotFoundError("未找到 %s %s 的 issue.json" % (source, day))
    warnings = list(issue.get("import_warnings") or [])
    if not issue.get("editions"):
        warnings.append("期次没有版面清单")
    if not issue.get("units"):
        warnings.append("期次没有正文单元")
    draft = _load_draft(archive_root, source, day)
    draft_by_id = {str(x.get("id")): x for x in draft.get("units", [])}
    sidecar_by_id = _summary_sidecar(archive_root, source, day)
    normalized = []
    with_text = 0
    with_summary = 0
    with_page = 0
    with_draft = 0
    for index, (edition, unit) in enumerate(_match_editions(issue), 1):
        item_warnings = []
        unit_id = str(unit.get("id") or "%s_%s_%02d" % (
            source, day.replace("-", ""), int(edition.get("no") or index)))
        page_rel = unit.get("page_image") or edition.get("page_image")
        page_path = _safe_issue_file(issue_dir, page_rel, "版面图", item_warnings)
        if page_path is not None and not page_path.is_file():
            item_warnings.append("版面图文件缺失：%s" % page_path.name)
            page_path = None
        if page_path is None:
            item_warnings.append("第%s版缺少可用版面图" % (edition.get("no") or index))
        else:
            with_page += 1
        text, text_source = _unit_text(
            unit, issue_dir, item_warnings, edition.get("no") or index
        )
        if text:
            with_text += 1
        else:
            item_warnings.append("第%s版正文为空" % (edition.get("no") or index))
        reviewed = draft_by_id.get(unit_id) or {}
        old_summary, old_importance = _existing_summary(
            unit, sidecar_by_id.get(unit_id) or {})
        summary = str(reviewed.get("summary") or old_summary or "").strip()
        reviewed_title = reviewed.get("title")
        title = str(reviewed_title).strip() if reviewed_title is not None else ""
        if not title:
            title = unit.get("title") or "%s版 %s" % (
                edition.get("no") or index, edition.get("name") or "待复核")
        importance = reviewed.get("importance", old_importance)
        if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 5:
            importance = old_importance
        if summary:
            with_summary += 1
        else:
            item_warnings.append("第%s版摘要尚未完成" % (edition.get("no") or index))
        topics = list(reviewed.get("topics") or [])
        facts = list(reviewed.get("facts") or [])
        if reviewed.get("summary") and topics and facts:
            with_draft += 1
        normalized.append({
            "id": unit_id,
            "edition_no": edition.get("no") or index,
            "edition_name": edition.get("name") or "待复核",
            "title": title,
            "page_image": str(page_path) if page_path is not None else None,
            "text": text,
            "text_source": text_source,
            "text_length": len(text),
            "summary": summary,
            "importance": importance,
            "topics": topics,
            "facts": facts,
            "warnings": item_warnings,
        })
        warnings.extend(item_warnings)
    total = len(normalized)
    return {
        "status": "ready_to_publish" if total and with_draft == total else "needs_review",
        "source": source,
        "source_name": issue.get("source_name") or source,
        "date": day,
        "issue_no": issue.get("issue_no"),
        "source_sha256": issue.get("source_sha256"),
        "channel": issue.get("channel"),
        "pdf_path": _resolve_imported_pdf(archive_root, issue, warnings),
        "issue_path": str(issue_path),
        "units": normalized,
        "coverage": {
            "editions": total,
            "with_text": with_text,
            "with_summary": with_summary,
            "with_page": with_page,
            "with_draft": with_draft,
            "missing_text": total - with_text,
            "missing_summary": total - with_summary,
            "missing_page": total - with_page,
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def _validate_fact(fact, unit_id, index):
    if not isinstance(fact, dict):
        raise ValidationError("%s facts[%s] 必须是对象" % (unit_id, index))
    missing = [field for field in _FACT_FIELDS if field not in fact]
    if missing:
        raise ValidationError("%s facts[%s] 缺少字段：%s" % (
            unit_id, index, ", ".join(missing)))
    for field in ("subject", "action", "object", "source"):
        if not str(fact.get(field) or "").strip():
            raise ValidationError("%s facts[%s].%s 不能为空" % (unit_id, index, field))
    return {field: fact.get(field) for field in _FACT_FIELDS}


def validate_draft(archive_root, draft):
    if not isinstance(draft, dict):
        raise ValidationError("草稿必须是 JSON 对象")
    source = _validate_source(draft.get("source"))
    if source != CONSTRUCTION_SOURCE:
        raise ValidationError("建设读报台只允许保存中国建设报（zgjsb）草稿")
    day = _validate_day(draft.get("date"))
    issue = get_issue(archive_root, source, day)
    expected = [unit["id"] for unit in issue["units"]]
    units = draft.get("units")
    if not isinstance(units, list):
        raise ValidationError("草稿 units 必须是数组")
    supplied = [str(unit.get("id")) for unit in units if isinstance(unit, dict)]
    if len(supplied) != len(set(supplied)):
        raise ValidationError("草稿存在重复版面 id")
    missing = [unit_id for unit_id in expected if unit_id not in supplied]
    extra = [unit_id for unit_id in supplied if unit_id not in expected]
    if missing or extra:
        raise ValidationError("草稿版面不完整；缺少=%s，多余=%s" % (missing, extra))
    normalized = []
    by_id = {str(unit["id"]): unit for unit in units}
    issue_by_id = {unit["id"]: unit for unit in issue["units"]}
    for unit_id in expected:
        unit = by_id[unit_id]
        summary = str(unit.get("summary") or "").strip()
        if not summary:
            raise ValidationError("%s summary 不能为空" % unit_id)
        topics = unit.get("topics")
        if not isinstance(topics, list) or not topics:
            raise ValidationError("%s topics 至少选择一项" % unit_id)
        invalid = [topic for topic in topics if topic not in TOPICS]
        if invalid:
            raise ValidationError("%s topics 非法：%s" % (unit_id, invalid))
        importance = unit.get("importance", issue_by_id[unit_id].get("importance", 3))
        if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 5:
            raise ValidationError("%s importance 必须是 1–5 的整数" % unit_id)
        facts = unit.get("facts")
        if not isinstance(facts, list) or not facts:
            raise ValidationError("%s facts 至少填写一项" % unit_id)
        normalized_unit = {
            "id": unit_id,
            "summary": summary,
            "importance": importance,
            "topics": list(dict.fromkeys(topics)),
            "facts": [_validate_fact(fact, unit_id, index)
                      for index, fact in enumerate(facts)],
        }
        if "title" in unit:
            if not isinstance(unit.get("title"), str) or not unit["title"].strip():
                raise ValidationError("%s title 提供时必须是非空字符串" % unit_id)
            normalized_unit["title"] = unit["title"].strip()
        normalized.append(normalized_unit)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "date": day,
        "units": normalized,
        "saved_at": _now(),
    }


def save_draft(archive_root, vault_root, draft):
    # ``vault_root`` is intentionally accepted to keep every API call fully
    # parameterized; drafts are archive-only and never dereference this path.
    del vault_root
    normalized = validate_draft(archive_root, draft)
    path = _draft_path(archive_root, normalized["source"], normalized["date"])
    _atomic_json(path, normalized)
    return {
        "status": "draft_saved",
        "source": normalized["source"],
        "date": normalized["date"],
        "unit_count": len(normalized["units"]),
        "draft_path": str(path),
    }


def _load_state(archive_root, source, day):
    path = _safe_internal(archive_root, "_state", source, day + ".json")
    return _load_json(path, {}) or {}


def _stage_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = _datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.timestamp()


def _has_active_failure(stages):
    """A historical failure stops blocking once a later stage succeeds."""
    failed = stages.get("failed")
    if not failed:
        return False
    failed_at = _stage_timestamp(failed)
    if failed_at is None:
        return True
    success_times = [
        _stage_timestamp(stages.get(name))
        for name in ("fetched", "parsed", "summarized", "archived", "published", "tracked")
    ]
    latest_success = max((item for item in success_times if item is not None), default=None)
    return latest_success is None or failed_at >= latest_success


def get_inbox(archive_root, day=None, source=None):
    archive = _root(archive_root)
    day_filter = _validate_day(day) if day else None
    source_filter = _validate_source(source) if source else None
    pattern = str(archive / (source_filter or "*") / (day_filter or "*") / "issue.json")
    rows = []
    for issue_path_string in sorted(glob.glob(pattern), reverse=True):
        issue_path = Path(issue_path_string)
        source = issue_path.parent.parent.name
        issue_day = issue_path.parent.name
        if source.startswith("_"):
            continue
        try:
            issue = get_issue(archive, source, issue_day)
        except APIError as exc:
            rows.append({
                "source": source, "date": issue_day, "status": "failed",
                "review_status": "blocked", "publish_status": "blocked",
                "text_length": 0, "warnings": [str(exc)],
            })
            continue
        state = _load_state(archive, source, issue_day)
        stages = state.get("stages") or {}
        failed = _has_active_failure(stages)
        published = bool(stages.get("published") or stages.get("archived"))
        if failed:
            status = "failed"
            review_status = "blocked"
            publish_status = "blocked"
        elif published:
            status = "published"
            review_status = "complete"
            publish_status = "published"
        elif issue.get("status") == "ready_to_publish":
            status = "ready_to_publish"
            review_status = "complete"
            publish_status = "pending"
        else:
            status = "needs_review"
            review_status = "pending"
            publish_status = "not_ready"
        rows.append({
            "source": source,
            "source_name": issue["source_name"],
            "date": issue_day,
            "issue_no": issue.get("issue_no"),
            "status": status,
            "review_status": review_status,
            "publish_status": publish_status,
            "text_length": sum(len(unit["text"]) for unit in issue["units"]),
            "coverage": issue["coverage"],
            "pdf_path": issue.get("pdf_path"),
            "warnings": list(dict.fromkeys(issue["warnings"] + list(state.get("warnings") or []))),
        })
    return {
        "date": day_filter,
        "issues": rows,
        "stats": {
            "issue_count": len(rows),
            "success_count": sum(1 for row in rows
                                 if row["status"] != "failed" and row.get("text_length", 0) > 0),
            "failed_count": sum(1 for row in rows if row["status"] == "failed"),
            "needs_review_count": sum(1 for row in rows if row["review_status"] == "pending"),
            "published_count": sum(1 for row in rows if row["publish_status"] == "published"),
        },
    }


def import_file(archive_root, pdf_path, source="zgjsb", day=None):
    source = _validate_source(source)
    if source != CONSTRUCTION_SOURCE:
        raise ValidationError("建设读报台只允许导入中国建设报（zgjsb）")
    before_duplicate = False
    try:
        validated = local_pdf.validate_pdf(pdf_path, max_bytes=MAX_PDF_BYTES)
        digest = local_pdf.sha256_file(validated)
        import_dir = _safe_internal(archive_root, "_imports", digest)
        before_duplicate = any(import_dir.glob("*.pdf")) if import_dir.is_dir() else False
        result = local_pdf.import_pdf(
            validated, archive_root, source=source, date=day
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValidationError(str(exc)) from exc
    result = dict(result)
    result["sha256"] = result.get("source_sha256") or digest
    result["deduplicated"] = before_duplicate
    result["issue_linked"] = Path(result.get("issue_path", "")).is_file()
    if result.get("needs_review"):
        result["status"] = "needs_review"
    elif result.get("imported"):
        result["status"] = "imported"
    else:
        result["status"] = "linked"
    return result


def capabilities(archive_root, vault_root):
    vault = vault_publisher.validate_vault_root(vault_root)
    return {
        "api_schema_version": SCHEMA_VERSION,
        "commands": [
            "capabilities", "inbox", "issue", "import-file", "draft-save",
            "publish-plan", "publish-apply", "history", "rollback",
        ],
        "topics": list(TOPICS),
        "archive": str(_root(archive_root)),
        "vault": str(vault),
        "publish_root": str(vault / vault_publisher.TARGET_FOLDER),
        "drafts_write_vault": False,
        "publish_requires_plan": True,
        "rollback_conflict_detection": True,
    }


def _require(value, flag):
    if value is None or str(value) == "":
        raise ValidationError("缺少参数 %s" % flag)
    return value


def _dispatch(args):
    command = args.command
    if command == "capabilities":
        return capabilities(args.archive, args.vault), []
    if command == "inbox":
        data = get_inbox(args.archive, args.date, args.source)
        warnings = [warning for issue in data["issues"] for warning in issue.get("warnings", [])]
        return data, list(dict.fromkeys(warnings))
    if command == "issue":
        data = get_issue(args.archive, _require(args.source, "--source"),
                         _require(args.date, "--date"))
        return data, data.get("warnings", [])
    if command == "import-file":
        data = import_file(args.archive, _require(args.path, "--path"),
                           source=args.source or "zgjsb", day=args.date)
        return data, data.get("warnings", [])
    if command == "draft-save":
        input_path = Path(_require(args.input, "--input")).expanduser()
        draft = _load_json(input_path)
        if draft is None:
            raise ValidationError("草稿 JSON 不存在或无效")
        return save_draft(args.archive, args.vault, draft), []
    if command == "publish-plan":
        source = _require(args.source, "--source")
        day = _require(args.date, "--date")
        issue = get_issue(args.archive, source, day)
        draft = _load_draft(args.archive, source, day)
        if not draft:
            raise ValidationError("请先保存完整草稿")
        # Revalidate immediately before planning so stale/manual draft files
        # cannot bypass the review schema.
        draft = validate_draft(args.archive, draft)
        return vault_publisher.create_plan(args.archive, args.vault, issue, draft), issue["warnings"]
    if command == "publish-apply":
        return vault_publisher.apply_plan(
            args.archive, args.vault, _require(args.plan_id, "--plan-id")
        ), []
    if command == "history":
        return {"transactions": vault_publisher.history(args.archive, args.limit)}, []
    if command == "rollback":
        return vault_publisher.rollback_transaction(
            args.archive, args.vault, _require(args.transaction_id, "--transaction-id")
        ), []
    raise ValidationError("未知命令")


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationError(message)


def _parser():
    parser = _JSONArgumentParser(description="readdaily 工作台 JSON API")
    parser.add_argument("command", choices=[
        "capabilities", "inbox", "issue", "import-file", "draft-save",
        "publish-plan", "publish-apply", "history", "rollback",
    ])
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--source")
    parser.add_argument("--date")
    parser.add_argument("--path")
    parser.add_argument("--input")
    parser.add_argument("--plan-id")
    parser.add_argument("--transaction-id")
    parser.add_argument("--limit", type=int, default=50)
    return parser


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        data, warnings = _dispatch(args)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "data": data,
            "warnings": list(dict.fromkeys(warnings)),
        }
        code = 0
    except (APIError, vault_publisher.PublisherError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "data": None,
            "warnings": [],
            "error": {
                "code": getattr(exc, "code", exc.__class__.__name__),
                "message": str(exc),
            },
        }
        code = 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
