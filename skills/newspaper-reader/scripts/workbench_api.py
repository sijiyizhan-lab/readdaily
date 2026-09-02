#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned JSON command API for the native readdaily workbench."""

import argparse
import contextlib
import datetime as _datetime
import fcntl
import glob
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading


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
SOURCE_REGISTRY = HERE.parents[1] / "newspaper-fetch" / "sources.json"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FACT_FIELDS = ("subject", "action", "object", "value", "unit", "time", "source")
OCR_REVIEW_STATUSES = (
    "unreviewed", "edited", "confirmed",
)
READING_STATUSES = ("unread", "opened", "completed")
_RMW_THREAD_LOCKS = {}
_RMW_THREAD_LOCKS_GUARD = threading.Lock()

# This order is a product contract, not the incidental order of sources.json.
# Names, enabled state and channel metadata are still read from the repository
# registry so this API cannot silently invent a source configuration.
NEWSPAPER_GROUPS = (
    ("central_party", "中央党报", ("rmrb", "gmrb", "jjrb")),
    ("ministry_industry", "部委行业报", ("zgjsb", "kjrb", "nmrb")),
    ("local_party", "地方党报", ("nfrb", "bjrb")),
)
READ_DAILY_SOURCE_IDS = tuple(
    source for _category_id, _category_name, sources in NEWSPAPER_GROUPS
    for source in sources
)


class APIError(RuntimeError):
    code = "api_error"


class ValidationError(APIError):
    code = "validation_error"


class NotFoundError(APIError):
    code = "not_found"


class PersistenceError(APIError):
    code = "persistence_error"


class PathSafetyError(ValidationError):
    code = "path_safety_error"


class _ArchiveJSONTarget:
    """One archive-relative JSON target bound to a verified root inode."""

    def __init__(self, session, path):
        configured = session["configured_path"]
        absolute = os.path.abspath(str(path))
        try:
            relative = os.path.relpath(absolute, configured)
        except ValueError as exc:
            raise PathSafetyError("归档 JSON 路径无法安全绑定") from exc
        parts = relative.split(os.sep)
        if (relative == os.pardir or relative.startswith(os.pardir + os.sep)
                or any(part in ("", ".", "..") for part in parts)):
            raise PathSafetyError("归档 JSON 路径越界")
        self.session = session
        self.relative_path = relative
        self.absolute_path = absolute

    def __fspath__(self):
        return self.absolute_path


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


def _assert_outside_vault(path, vault_root, label):
    """Reject archive-side writes that resolve anywhere inside the Vault."""
    target_real = os.path.realpath(str(path))
    vault_real = os.path.realpath(str(_root(vault_root)))
    try:
        if os.path.commonpath([vault_real, target_real]) == vault_real:
            raise PathSafetyError("%s 必须保存在 Vault 之外" % label)
    except ValueError as exc:
        raise PathSafetyError("%s 与 Vault 路径无法安全比较" % label) from exc


def _assert_archive_isolated(archive_root, vault_root):
    """Require archive and Vault trees to be completely disjoint."""
    archive_real = os.path.realpath(str(_root(archive_root)))
    vault_real = os.path.realpath(str(_root(vault_root)))
    try:
        common = os.path.commonpath([archive_real, vault_real])
    except ValueError as exc:
        raise PathSafetyError("归档目录与 Vault 路径无法安全比较") from exc
    if common in (archive_real, vault_real):
        raise PathSafetyError("归档目录必须与 Vault 完全分离，不能互为父子目录或路径别名")


@contextlib.contextmanager
def _archive_rmw_lock(archive_root, scope):
    """Serialize one archive read-modify-write transaction across processes."""
    archive_identity = os.path.realpath(str(_root(archive_root)))
    lock_key = hashlib.sha256(
        (archive_identity + "\0" + str(scope)).encode("utf-8")
    ).hexdigest()
    lock_directory = Path(tempfile.gettempdir()) / "readdaily-workbench-locks"
    lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = lock_directory / (lock_key + ".lock")
    with _RMW_THREAD_LOCKS_GUARD:
        thread_lock = _RMW_THREAD_LOCKS.setdefault(lock_key, threading.RLock())
    with thread_lock:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield lock_path
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValidationError("JSON 无法读取：%s" % path) from exc


def _directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _verify_archive_session(session):
    try:
        opened = os.fstat(session["root_fd"])
    except OSError as exc:
        raise PathSafetyError("归档根目录在写入期间被替换") from exc
    try:
        publisher_verify = vault_publisher._verify_pinned_directory
        publisher_verify(session, "归档根目录")
    except vault_publisher.PathSafetyError as exc:
        raise PathSafetyError(str(exc)) from exc
    if (not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != session["root_identity"]):
        raise PathSafetyError("归档根目录在写入期间被替换")


@contextlib.contextmanager
def _archive_directory_session(archive_root, vault_root):
    """Pin the user-configured archive before any validation or read."""
    try:
        archive_context = vault_publisher._pinned_directory(
            archive_root, "归档根目录"
        )
        with archive_context as pinned_archive:
            with vault_publisher._vault_directory_session(
                    vault_root) as pinned_vault:
                archive_path = pinned_archive["canonical_path"]
                vault_path = pinned_vault["canonical_path"]
                try:
                    common = os.path.commonpath([archive_path, vault_path])
                except ValueError as exc:
                    raise PathSafetyError(
                        "归档目录与 Vault 路径无法安全比较"
                    ) from exc
                if common in (archive_path, vault_path):
                    raise PathSafetyError(
                        "归档目录必须与 Vault 完全分离，不能互为父子目录或路径别名"
                    )
                session = dict(pinned_archive)
                session["vault_identity"] = pinned_vault["root_identity"]
                _verify_archive_session(session)
                yield session
    except vault_publisher.PathSafetyError as exc:
        raise PathSafetyError(str(exc)) from exc


def _verify_archive_parent(handle):
    _verify_archive_session(handle["session"])
    for parent_fd, name, child_fd, identity in handle["links"]:
        try:
            opened = os.fstat(child_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PathSafetyError("归档 JSON 的祖先目录在写入期间被替换") from exc
        if (not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != identity
                or (linked.st_dev, linked.st_ino) != identity):
            raise PathSafetyError("归档 JSON 的祖先目录在写入期间被替换")


@contextlib.contextmanager
def _archive_target_parent(session, relative_path):
    parts = str(relative_path).split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise PathSafetyError("归档 JSON 相对路径非法")
    descriptors = []
    links = []
    current = session["root_fd"]
    try:
        _verify_archive_session(session)
        for name in parts[:-1]:
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                os.fsync(current)
                try:
                    child = os.open(
                        name, _directory_open_flags(), dir_fd=current
                    )
                except OSError as exc:
                    raise PathSafetyError(
                        "归档新建目录被替换或不是可信目录：%s" % name
                    ) from exc
            except OSError as exc:
                raise PathSafetyError(
                    "归档 JSON 的祖先不是可信目录：%s" % name
                ) from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise PathSafetyError("归档 JSON 的祖先不是目录：%s" % name)
            identity = (info.st_dev, info.st_ino)
            descriptors.append(child)
            links.append((current, name, child, identity))
            current = child
        handle = {
            "session": session,
            "links": links,
            "parent_fd": current,
            "name": parts[-1],
        }
        _verify_archive_parent(handle)
        yield handle
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _atomic_json(path, obj):
    if not isinstance(path, _ArchiveJSONTarget):
        raise PathSafetyError("归档 JSON 写入缺少已验证的根目录绑定")
    return _atomic_archive_json(path, obj)


def _atomic_archive_json(target, obj):
    raw = json.dumps(
        obj, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    session = target.session
    _verify_archive_session(session)
    with _archive_target_parent(
            session, target.relative_path) as handle:
        try:
            existing = os.stat(
                handle["name"],
                dir_fd=handle["parent_fd"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise PathSafetyError("归档 JSON 目标无法安全检查") from exc
        if (existing is not None
                and (stat.S_ISLNK(existing.st_mode)
                     or not stat.S_ISREG(existing.st_mode))):
            raise PathSafetyError("归档 JSON 目标必须是普通文件")

        temporary_name = ".%s.%s.tmp" % (
            handle["name"], hashlib.sha256(os.urandom(32)).hexdigest()
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary_name, flags, 0o600,
                dir_fd=handle["parent_fd"],
            )
            temporary_exists = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _verify_archive_parent(handle)
            if existing is None:
                try:
                    os.link(
                        temporary_name,
                        handle["name"],
                        src_dir_fd=handle["parent_fd"],
                        dst_dir_fd=handle["parent_fd"],
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise PersistenceError(
                        "归档 JSON 在提交时已由其他进程创建"
                    ) from exc
                _verify_archive_parent(handle)
                os.fsync(handle["parent_fd"])
                os.unlink(temporary_name, dir_fd=handle["parent_fd"])
                temporary_exists = False
                os.fsync(handle["parent_fd"])
            else:
                os.replace(
                    temporary_name,
                    handle["name"],
                    src_dir_fd=handle["parent_fd"],
                    dst_dir_fd=handle["parent_fd"],
                )
                temporary_exists = False
                _verify_archive_parent(handle)
                os.fsync(handle["parent_fd"])
            _verify_archive_parent(handle)
        finally:
            if temporary_exists:
                try:
                    os.unlink(
                        temporary_name, dir_fd=handle["parent_fd"]
                    )
                    os.fsync(handle["parent_fd"])
                except OSError:
                    pass


def _registered_newspapers():
    registry = _load_json(SOURCE_REGISTRY)
    if not isinstance(registry, dict) or not isinstance(registry.get("sources"), list):
        raise ValidationError("报纸来源注册表无效：%s" % SOURCE_REGISTRY)
    by_id = {
        str(item.get("id")): item
        for item in registry["sources"]
        if isinstance(item, dict) and item.get("id")
    }
    missing = [source for source in READ_DAILY_SOURCE_IDS if source not in by_id]
    if missing:
        raise ValidationError("报纸来源注册表缺少已锁定来源：%s" % ", ".join(missing))

    rows = []
    order = 0
    for category_id, category_name, sources in NEWSPAPER_GROUPS:
        for source in sources:
            order += 1
            configured = by_id[source]
            rows.append({
                "source": source,
                "source_name": str(configured.get("name") or source),
                "category_id": category_id,
                "category_name": category_name,
                "order": order,
                "enabled": bool(configured.get("enabled")),
                "channel": configured.get("channel"),
                "registry_status": configured.get("status"),
                "can_publish": source == CONSTRUCTION_SOURCE,
            })
    return rows


def newspaper_registry():
    newspapers = _registered_newspapers()
    by_category = {category_id: [] for category_id, _name, _sources in NEWSPAPER_GROUPS}
    for item in newspapers:
        by_category[item["category_id"]].append(dict(item))
    return {
        "expected_count": len(newspapers),
        "newspapers": newspapers,
        "categories": [
            {
                "id": category_id,
                "name": category_name,
                "newspapers": by_category[category_id],
            }
            for category_id, category_name, _sources in NEWSPAPER_GROUPS
        ],
    }


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


def _ocr_blocks(unit, text):
    """Expose source order without inferring headings or correcting OCR text."""
    article_blocks = []
    for article in unit.get("articles") or []:
        if not isinstance(article, dict):
            continue
        article_text = str(article.get("text") or "")
        if not article_text.strip():
            continue
        raw_title = str(article.get("title") or "")
        article_blocks.append({
            "kind": "article",
            "title": raw_title if raw_title.strip() else None,
            "text": article_text,
        })
    if article_blocks:
        return article_blocks
    if not text:
        return []
    # Blank lines are the only structural signal used for page-level OCR.
    # Single newlines remain untouched inside each paragraph.
    paragraphs = re.split(r"\r?\n[ \t]*\r?\n+", text)
    return [
        {"kind": "paragraph", "text": paragraph}
        for paragraph in paragraphs if paragraph
    ]


def _proofreading_from_draft(reviewed, unit_id=None, strict=False):
    corrected = reviewed.get("corrected_ocr_text")
    if corrected is not None and not isinstance(corrected, str):
        if strict:
            raise ValidationError("%s corrected_ocr_text 必须是字符串或 null" % unit_id)
        corrected = None
    if isinstance(corrected, str) and not corrected.strip():
        corrected = None

    status = reviewed.get("proofread_status", "unreviewed")
    if status not in OCR_REVIEW_STATUSES:
        if strict:
            raise ValidationError("%s proofread_status 非法：%s" % (unit_id, status))
        status = "unreviewed"

    raw_suspicions = reviewed.get("ocr_suspicions", [])
    if not isinstance(raw_suspicions, list) or any(
            not isinstance(item, str) for item in raw_suspicions):
        if strict:
            raise ValidationError("%s ocr_suspicions 必须是字符串数组" % unit_id)
        raw_suspicions = []
    suspicions = []
    for item in raw_suspicions:
        normalized = item.strip()
        if normalized and normalized not in suspicions:
            suspicions.append(normalized)
    return corrected, status, suspicions


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
    with vault_publisher._fetch_date_evidence_lock(archive_root, day):
        return _get_issue_locked(archive_root, source, day)


def _get_issue_locked(archive_root, source, day):
    source = _validate_source(source)
    day = _validate_day(day)
    issue_dir = _issue_dir(archive_root, source, day)
    issue_path = issue_dir / "issue.json"
    issue = _load_json(issue_path)
    if issue is None:
        raise NotFoundError("未找到 %s %s 的 issue.json" % (source, day))
    if not isinstance(issue, dict):
        raise ValidationError("issue.json 必须是 JSON 对象：%s" % issue_path)
    actual_source = issue.get("source")
    if actual_source != source:
        raise ValidationError(
            "issue.json source 身份不匹配：请求=%s，文件=%r" % (
                source, actual_source
            )
        )
    actual_day = issue.get("date")
    if actual_day != day:
        raise ValidationError(
            "issue.json date 身份不匹配：请求=%s，文件=%r" % (
                day, actual_day
            )
        )
    warnings = list(issue.get("import_warnings") or [])
    local_pdf_reference = bool(
        (issue.get("files") or {}).get("local_pdf") or issue.get("pdf_path")
    )
    local_pdf_date_verification = (
        str(issue.get("local_pdf_date_verification") or "unverified")
        if local_pdf_reference else "not_applicable"
    )
    local_pdf_date_publishable = (
        not local_pdf_reference or local_pdf_date_verification == "verified"
    )
    if not local_pdf_date_publishable:
        warnings.append("本地 PDF 的第一页报头日期尚未唯一核验，禁止发布。")
    if not issue.get("editions"):
        warnings.append("期次没有版面清单")
    if not issue.get("units"):
        warnings.append("期次没有正文单元")
    evidence_sha256 = vault_publisher._issue_tree_evidence_sha256(
        archive_root, source, day
    )
    if not evidence_sha256:
        raise ValidationError("本期报纸证据目录无法完整校验")
    draft = _load_draft(archive_root, source, day)
    draft_stale = bool(draft) and not (
        isinstance(draft.get("evidence_sha256"), str)
        and hmac.compare_digest(draft["evidence_sha256"], evidence_sha256)
    )
    if draft_stale:
        warnings.append("已有草稿基于旧版报纸证据，已隔离；请重新复核本期内容。")
    active_draft = {} if draft_stale else draft
    draft_sha256 = (
        vault_publisher._draft_content_sha256(active_draft)
        if active_draft else None
    )
    draft_by_id = {
        str(x.get("id")): x for x in active_draft.get("units", [])
        if isinstance(x, dict)
    }
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
        corrected_ocr_text, proofread_status, ocr_suspicions = (
            _proofreading_from_draft(reviewed)
        )
        if reviewed.get("summary") and (
                source != CONSTRUCTION_SOURCE or (topics and facts)):
            with_draft += 1
        normalized.append({
            "id": unit_id,
            "edition_no": edition.get("no") or index,
            "edition_name": edition.get("name") or "待复核",
            "title": title,
            "page_image": str(page_path) if page_path is not None else None,
            "text": text,
            "ocr_text": text,
            "ocr_blocks": _ocr_blocks(unit, text),
            "corrected_ocr_text": corrected_ocr_text,
            "proofread_status": proofread_status,
            "ocr_suspicions": ocr_suspicions,
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
    review_complete = bool(
        total
        and with_text == total
        and with_page == total
        and with_draft == total
        and local_pdf_date_publishable
    )
    return {
        "status": (
            "ready_to_publish" if review_complete and source == CONSTRUCTION_SOURCE
            else "review_complete" if review_complete
            else "needs_review"
        ),
        "source": source,
        "source_name": issue.get("source_name") or source,
        "can_publish": source == CONSTRUCTION_SOURCE,
        "date": day,
        "issue_no": issue.get("issue_no"),
        "source_sha256": issue.get("source_sha256"),
        "evidence_sha256": evidence_sha256,
        "archive_evidence_sha256": evidence_sha256,
        "draft_sha256": draft_sha256,
        "draft_stale": draft_stale,
        "channel": issue.get("channel"),
        "local_pdf_header_date": issue.get("local_pdf_header_date"),
        "local_pdf_date_verification": local_pdf_date_verification,
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


def _validate_fact(fact, unit_id, index, require_complete=True):
    if not isinstance(fact, dict):
        raise ValidationError("%s facts[%s] 必须是对象" % (unit_id, index))
    if require_complete:
        missing = [field for field in _FACT_FIELDS if field not in fact]
        if missing:
            raise ValidationError("%s facts[%s] 缺少字段：%s" % (
                unit_id, index, ", ".join(missing)))
        for field in ("subject", "action", "object", "source"):
            if not str(fact.get(field) or "").strip():
                raise ValidationError("%s facts[%s].%s 不能为空" % (unit_id, index, field))
        return {field: fact.get(field) for field in _FACT_FIELDS}

    normalized = {field: fact[field] for field in _FACT_FIELDS if field in fact}
    # Swift renders one empty FactFields row as an editing affordance.  It is
    # not a fact and must not become archival data.  Partially entered facts,
    # however, are legitimate incremental work and are preserved verbatim.
    if not any(str(value or "").strip() for value in normalized.values()):
        return None
    return normalized


def validate_draft(
        archive_root, draft, require_publish_ready=False, _locked_issue=None):
    """Validate one draft payload without inventing unsupplied review content.

    Save-time validation deliberately accepts a subset of editions and empty
    review fields so the native editor can persist work after every page.  The
    publish path opts into ``require_publish_ready`` and retains the complete,
    strict schema gate.
    """
    if not isinstance(draft, dict):
        raise ValidationError("草稿必须是 JSON 对象")
    source = _validate_source(draft.get("source"))
    if source not in READ_DAILY_SOURCE_IDS:
        raise ValidationError("Read Daily 只允许保存已锁定8家报纸的草稿")
    day = _validate_day(draft.get("date"))
    issue = (
        _locked_issue
        if _locked_issue is not None
        else get_issue(archive_root, source, day)
    )
    if issue.get("source") != source or issue.get("date") != day:
        raise ValidationError("草稿与锁定的报纸期次身份不匹配")
    supplied_evidence = draft.get("evidence_sha256")
    if (not isinstance(supplied_evidence, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_evidence)):
        raise ValidationError("草稿缺少有效的 evidence_sha256，请重新打开本期报纸")
    if not hmac.compare_digest(supplied_evidence, issue["evidence_sha256"]):
        raise ValidationError("草稿对应的报纸证据已变化，请重新打开并复核本期内容")
    if require_publish_ready and (
            not issue["coverage"]["editions"]
            or issue["coverage"]["missing_text"]
            or issue["coverage"]["missing_page"]):
        raise ValidationError(
            "报纸原始证据不完整，缺文=%s，缺图=%s，禁止发布" % (
                issue["coverage"]["missing_text"],
                issue["coverage"]["missing_page"],
            )
        )
    expected = [unit["id"] for unit in issue["units"]]
    units = draft.get("units")
    if not isinstance(units, list):
        raise ValidationError("草稿 units 必须是数组")
    if any(not isinstance(unit, dict) for unit in units):
        raise ValidationError("草稿 units 每项必须是对象")
    supplied = [str(unit.get("id") or "") for unit in units]
    if any(not unit_id for unit_id in supplied):
        raise ValidationError("草稿版面 id 不能为空")
    if len(supplied) != len(set(supplied)):
        raise ValidationError("草稿存在重复版面 id")
    missing = [unit_id for unit_id in expected if unit_id not in supplied]
    extra = [unit_id for unit_id in supplied if unit_id not in expected]
    if extra or (require_publish_ready and missing):
        raise ValidationError("草稿版面不完整；缺少=%s，多余=%s" % (missing, extra))
    normalized = []
    by_id = {str(unit["id"]): unit for unit in units}
    issue_by_id = {unit["id"]: unit for unit in issue["units"]}
    for unit_id in (expected if require_publish_ready else supplied):
        unit = by_id[unit_id]
        normalized_unit = {"id": unit_id}

        if "summary" in unit:
            if unit.get("summary") is not None and not isinstance(unit.get("summary"), str):
                raise ValidationError("%s summary 必须是字符串" % unit_id)
            summary = str(unit.get("summary") or "").strip()
            normalized_unit["summary"] = summary
        else:
            summary = ""
        if require_publish_ready and not summary:
            raise ValidationError("%s summary 不能为空" % unit_id)

        if "topics" in unit:
            topics = unit.get("topics")
            if not isinstance(topics, list):
                raise ValidationError("%s topics 必须是数组" % unit_id)
            invalid = [topic for topic in topics if topic not in TOPICS]
            if invalid:
                raise ValidationError("%s topics 非法：%s" % (unit_id, invalid))
            normalized_unit["topics"] = list(dict.fromkeys(topics))
        else:
            topics = None
        if (require_publish_ready and source == CONSTRUCTION_SOURCE
                and not topics):
            raise ValidationError("%s topics 至少选择一项" % unit_id)

        if "importance" in unit:
            importance = unit.get("importance")
            if (isinstance(importance, bool) or not isinstance(importance, int)
                    or not 1 <= importance <= 5):
                raise ValidationError("%s importance 必须是 1–5 的整数" % unit_id)
            normalized_unit["importance"] = importance
        elif require_publish_ready:
            normalized_unit["importance"] = issue_by_id[unit_id].get("importance", 3)

        if "facts" in unit:
            facts = unit.get("facts")
            if not isinstance(facts, list):
                raise ValidationError("%s facts 必须是数组" % unit_id)
            normalized_facts = []
            for index, fact in enumerate(facts):
                normalized_fact = _validate_fact(
                    fact, unit_id, index,
                    require_complete=require_publish_ready,
                )
                if normalized_fact is not None:
                    normalized_facts.append(normalized_fact)
            normalized_unit["facts"] = normalized_facts
        else:
            facts = None
        if (require_publish_ready and source == CONSTRUCTION_SOURCE
                and not facts):
            raise ValidationError("%s facts 至少填写一项" % unit_id)

        proofreading_keys = {
            "corrected_ocr_text", "proofread_status", "ocr_suspicions"
        }.intersection(unit)
        if proofreading_keys:
            corrected_ocr_text, proofread_status, ocr_suspicions = (
                _proofreading_from_draft(unit, unit_id=unit_id, strict=True)
            )
            if "corrected_ocr_text" in unit:
                normalized_unit["corrected_ocr_text"] = corrected_ocr_text
            if "proofread_status" in unit:
                normalized_unit["proofread_status"] = proofread_status
            if "ocr_suspicions" in unit:
                normalized_unit["ocr_suspicions"] = ocr_suspicions
        if "title" in unit:
            if not isinstance(unit.get("title"), str) or not unit["title"].strip():
                raise ValidationError("%s title 提供时必须是非空字符串" % unit_id)
            normalized_unit["title"] = unit["title"].strip()
        normalized.append(normalized_unit)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "date": day,
        "evidence_sha256": supplied_evidence,
        "units": normalized,
        "saved_at": _now(),
    }


def save_draft(archive_root, vault_root, draft):
    with _archive_directory_session(
            archive_root, vault_root) as archive_session:
        return _save_draft_in_session(
            archive_root, vault_root, draft, archive_session
        )


def _save_draft_in_session(
        archive_root, vault_root, draft, archive_session):
    if not isinstance(draft, dict):
        raise ValidationError("草稿必须是 JSON 对象")
    source = _validate_source(draft.get("source"))
    day = _validate_day(draft.get("date"))
    path = _draft_path(archive_root, source, day)
    _assert_outside_vault(path, vault_root, "草稿")

    # Global lock order is evidence -> draft RMW.  Keeping the evidence lock
    # through validation, merge, issue ordering and the atomic write prevents a
    # same-day fetch/import from replacing the issue between those operations.
    with vault_publisher._fetch_date_evidence_lock(archive_root, day):
        with vault_publisher._draft_rmw_lock(archive_root, source, day):
            locked_issue = _get_issue_locked(archive_root, source, day)
            incoming = validate_draft(
                archive_root, draft, require_publish_ready=False,
                _locked_issue=locked_issue,
            )
            previous = _load_draft(archive_root, source, day)
            previous_units = []
            if (previous and isinstance(previous.get("evidence_sha256"), str)
                    and hmac.compare_digest(
                        previous["evidence_sha256"], incoming["evidence_sha256"]
                    )):
                previous = validate_draft(
                    archive_root, previous, require_publish_ready=False,
                    _locked_issue=locked_issue,
                )
                previous_units = previous["units"]
            by_id = {str(unit["id"]): dict(unit) for unit in previous_units}
            for unit in incoming["units"]:
                unit_id = str(unit["id"])
                merged_unit = by_id.get(unit_id, {"id": unit_id})
                if (unit.get("proofread_status") == "unreviewed"
                        and "corrected_ocr_text" not in unit
                        and "corrected_ocr_text" in merged_unit):
                    # Reset-to-original clears a previously saved correction.  Keep a
                    # brand-new unreviewed draft sparse for legacy payload compatibility.
                    merged_unit["corrected_ocr_text"] = None
                merged_unit.update(unit)
                by_id[unit_id] = merged_unit
            issue_order = [unit["id"] for unit in locked_issue["units"]]
            normalized = {
                "schema_version": SCHEMA_VERSION,
                "source": source,
                "date": day,
                "evidence_sha256": incoming["evidence_sha256"],
                "units": [
                    by_id[unit_id] for unit_id in issue_order
                    if unit_id in by_id
                ],
                "saved_at": _now(),
            }
            try:
                _atomic_json(
                    _ArchiveJSONTarget(archive_session, path), normalized
                )
            except OSError as exc:
                raise PersistenceError("草稿保存失败：%s" % exc) from exc
    return {
        "status": "draft_saved",
        "source": normalized["source"],
        "date": normalized["date"],
        "evidence_sha256": normalized["evidence_sha256"],
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


def _publication_matches_evidence(state, issue):
    stages = state.get("stages") if isinstance(state, dict) else None
    if not isinstance(stages, dict) or not (
            stages.get("published") or stages.get("archived")):
        return False
    published_evidence = str(
        state.get("publish_archive_evidence_sha256") or ""
    )
    current_evidence = str(
        issue.get("evidence_sha256") or ""
    ) if isinstance(issue, dict) else ""
    published_draft = str(state.get("publish_draft_sha256") or "")
    current_draft = str(
        issue.get("draft_sha256") or ""
    ) if isinstance(issue, dict) else ""
    return (
        bool(re.fullmatch(r"[a-f0-9]{64}", published_evidence))
        and bool(re.fullmatch(r"[a-f0-9]{64}", current_evidence))
        and hmac.compare_digest(published_evidence, current_evidence)
        and bool(re.fullmatch(r"[a-f0-9]{64}", published_draft))
        and bool(re.fullmatch(r"[a-f0-9]{64}", current_draft))
        and hmac.compare_digest(published_draft, current_draft)
    )


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
        published = _publication_matches_evidence(state, issue)
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
            "warnings": list(dict.fromkeys(
                issue["warnings"]
                + list(state.get("warnings") or [])
                + (["既有发布记录对应旧版报纸证据，需重新复核发布"]
                   if (stages.get("published") or stages.get("archived"))
                   and not published else [])
            )),
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


def _activity_path(archive_root, day):
    return _safe_internal(archive_root, "_activity", _validate_day(day) + ".json")


def _load_daily_activity(archive_root, day):
    day = _validate_day(day)
    data = _load_json(_activity_path(archive_root, day), {}) or {}
    newspapers = data.get("newspapers")
    if not isinstance(newspapers, dict):
        newspapers = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "newspapers": newspapers,
    }


def _activity_record(data, source):
    raw = data.get("newspapers", {}).get(source)
    if not isinstance(raw, dict) or raw.get("reading_status") not in READING_STATUSES:
        return {
            "reading_status": "unread",
            "opened_at": None,
            "completed_at": None,
            "last_read_at": None,
        }
    opened_at = raw.get("opened_at") if isinstance(raw.get("opened_at"), str) else None
    completed_at = raw.get("completed_at") if isinstance(raw.get("completed_at"), str) else None
    return {
        "reading_status": raw["reading_status"],
        "opened_at": opened_at,
        "completed_at": completed_at,
        "last_read_at": completed_at or opened_at,
    }


def mark_reading_activity(archive_root, vault_root, source, day, status):
    with _archive_directory_session(
            archive_root, vault_root) as archive_session:
        return _mark_reading_activity_in_session(
            archive_root, vault_root, source, day, status, archive_session
        )


def _mark_reading_activity_in_session(
        archive_root, vault_root, source, day, status, archive_session):
    source = _validate_source(source)
    day = _validate_day(day)
    if source not in READ_DAILY_SOURCE_IDS:
        raise ValidationError("阅读动作仅支持已锁定8家报纸")
    if status not in READING_STATUSES:
        raise ValidationError("阅读状态必须是 unread、opened 或 completed")
    path = _activity_path(archive_root, day)
    _assert_outside_vault(path, vault_root, "阅读动作")
    with _archive_rmw_lock(archive_root, "activity:%s" % day):
        if (status == "completed"
                and not (_issue_dir(archive_root, source, day) / "issue.json").is_file()):
            raise ValidationError("缺报时不能标记阅读完成")
        data = _load_daily_activity(archive_root, day)
        existing = _activity_record(data, source)
        now = _now()
        opened_at = existing.get("opened_at")
        completed_at = existing.get("completed_at")
        if status == "opened":
            opened_at = opened_at or now
            completed_at = None
        elif status == "completed":
            opened_at = opened_at or now
            completed_at = now
        else:
            completed_at = None
        record = {
            "reading_status": status,
            "opened_at": opened_at,
            "completed_at": completed_at,
            "updated_at": now,
        }
        data["newspapers"][source] = record
        try:
            _atomic_json(
                _ArchiveJSONTarget(archive_session, path), data
            )
        except OSError as exc:
            raise PersistenceError("阅读动作保存失败：%s" % exc) from exc
    return {
        "status": "activity_saved",
        "source": source,
        "date": day,
        **_activity_record(data, source),
        "activity_path": str(path),
    }


def _action_status(status, at=None):
    return {"status": status, "at": at}


def _available_issue_dates(archive_root, limit=30):
    archive = _root(archive_root)
    dates = set()
    for source in READ_DAILY_SOURCE_IDS:
        for issue_path in glob.glob(str(archive / source / "*" / "issue.json")):
            raw_day = Path(issue_path).parent.name
            try:
                dates.add(_validate_day(raw_day))
            except ValidationError:
                continue
    return sorted(dates, reverse=True)[:limit]


def get_daily_dashboard(archive_root, day=None, source=None):
    available_dates = _available_issue_dates(archive_root)
    day = _validate_day(day) if day else (
        available_dates[0] if available_dates else _datetime.date.today().isoformat()
    )
    registry = newspaper_registry()
    newspapers = registry["newspapers"]
    if source:
        source = _validate_source(source)
        if source not in READ_DAILY_SOURCE_IDS:
            raise ValidationError("日报面板仅支持已锁定8家报纸")
        newspapers = [item for item in newspapers if item["source"] == source]
    activity = _load_daily_activity(archive_root, day)
    rows = []
    for configured in newspapers:
        source_id = configured["source"]
        state = _load_state(archive_root, source_id, day)
        stages = state.get("stages") or {}
        active_failure = _has_active_failure(stages)
        issue_path = _issue_dir(archive_root, source_id, day) / "issue.json"
        issue = None
        load_error = None
        if issue_path.is_file():
            try:
                issue = get_issue(archive_root, source_id, day)
            except APIError as exc:
                load_error = str(exc)
        available = issue is not None
        read_record = _activity_record(activity, source_id)
        coverage = issue["coverage"] if issue else {
            "editions": 0,
            "with_text": 0,
            "with_summary": 0,
            "with_page": 0,
            "with_draft": 0,
            "missing_text": 0,
            "missing_summary": 0,
            "missing_page": 0,
        }
        evidence_incomplete = bool(
            issue and (
                coverage["editions"] == 0
                or coverage["missing_text"] > 0
                or coverage["missing_page"] > 0
            )
        )
        publication_current = bool(
            issue and _publication_matches_evidence(state, issue)
        )

        if active_failure or load_error:
            status = "failed"
            acquisition_status = "failed"
            review_status = "blocked"
            publish_status = "blocked"
        elif not available:
            status = "missing"
            acquisition_status = "pending"
            review_status = "not_started"
            publish_status = "not_ready" if configured["can_publish"] else "not_supported"
        elif evidence_incomplete:
            status = "failed"
            acquisition_status = "failed"
            review_status = "blocked"
            publish_status = "blocked" if configured["can_publish"] else "not_supported"
        elif configured["can_publish"] and publication_current:
            status = "published"
            acquisition_status = "complete"
            review_status = "complete"
            publish_status = "published"
        elif issue["status"] in ("ready_to_publish", "review_complete"):
            status = issue["status"]
            acquisition_status = "complete"
            review_status = "complete"
            publish_status = "pending" if configured["can_publish"] else "not_supported"
        else:
            status = "needs_review"
            acquisition_status = "complete"
            review_status = "pending"
            publish_status = "not_ready" if configured["can_publish"] else "not_supported"

        text_length = sum(len(unit["ocr_text"]) for unit in issue["units"]) if issue else 0
        summarized = bool(
            coverage["editions"] and coverage["with_summary"] == coverage["editions"]
        )
        reading_action = {
            "unread": "pending",
            "opened": "in_progress",
            "completed": "complete",
        }[read_record["reading_status"]]
        acquired_at = (
            stages.get("parsed") or stages.get("fetched") or stages.get("acquired")
        )
        row = {
            **configured,
            "date": day,
            "available": available,
            "status": status,
            "acquisition_status": acquisition_status,
            "reading_status": read_record["reading_status"],
            "last_read_at": read_record["last_read_at"],
            "issue_no": issue.get("issue_no") if issue else None,
            "review_status": review_status,
            "publish_status": publish_status,
            "text_length": text_length,
            "edition_count": coverage["editions"],
            "coverage": coverage,
            "thumbnail": issue["units"][0].get("page_image") if issue and issue["units"] else None,
            "pdf_path": issue.get("pdf_path") if issue else None,
            "warnings": list(dict.fromkeys(
                ((issue.get("warnings") or []) if issue else [])
                + (list(state.get("warnings") or []))
                + ([load_error] if load_error else [])
                + (["既有发布记录对应旧版报纸证据，需重新复核发布"]
                   if issue and (
                       stages.get("published") or stages.get("archived")
                   ) and not publication_current else [])
            )),
            "daily_actions": {
                "acquired": _action_status(acquisition_status, acquired_at),
                "read": _action_status(reading_action, read_record["last_read_at"]),
                "summarized": _action_status(
                    "complete" if summarized else "pending",
                    stages.get("summarized"),
                ),
                "published": _action_status(
                    "complete" if publish_status == "published" else (
                        "pending" if configured["can_publish"] else "not_applicable"
                    ),
                    stages.get("published") or stages.get("archived"),
                ),
            },
        }
        rows.append(row)

    by_category = {category_id: [] for category_id, _name, _sources in NEWSPAPER_GROUPS}
    for row in rows:
        by_category[row["category_id"]].append(row)
    categories = [
        {"id": category_id, "name": category_name, "newspapers": by_category[category_id]}
        for category_id, category_name, _sources in NEWSPAPER_GROUPS
        if by_category[category_id]
    ]
    return {
        "date": day,
        "available_dates": available_dates,
        "categories": categories,
        "newspapers": rows,
        "stats": {
            "expected_count": len(rows),
            "available_count": sum(1 for row in rows if row["available"]),
            "missing_count": sum(1 for row in rows if not row["available"]),
            "failed_count": sum(1 for row in rows if row["status"] == "failed"),
            "reading_complete_count": sum(
                1 for row in rows if row["reading_status"] == "completed"
            ),
        },
    }


def import_file(archive_root, pdf_path, source="zgjsb", day=None, vault_root=None):
    vault_root = vault_root or DEFAULT_VAULT
    _assert_archive_isolated(archive_root, vault_root)
    source = _validate_source(source)
    if source != CONSTRUCTION_SOURCE:
        raise ValidationError("Read Daily 只允许导入中国建设报（zgjsb）")
    before_duplicate = False
    try:
        validated = local_pdf.validate_pdf(pdf_path, max_bytes=MAX_PDF_BYTES)
        digest = local_pdf.sha256_file(validated)
        filename_meta = local_pdf.parse_filename_metadata(validated.name)
        chosen_day = _validate_day(day or filename_meta.get("date"))
        import_dir = _safe_internal(archive_root, "_imports", digest)
        runtime_dir = _safe_internal(archive_root, "_runtime")
        issue_dir = _safe_internal(archive_root, source, chosen_day)
        for label, target in (
                ("PDF 内容寻址目录", import_dir),
                ("OCR 运行目录", runtime_dir),
                ("报纸期次目录", issue_dir)):
            _assert_outside_vault(target, vault_root, label)
        before_duplicate = any(import_dir.glob("*.pdf")) if import_dir.is_dir() else False
        result = local_pdf.import_pdf(
            validated,
            archive_root,
            source=source,
            date=chosen_day,
            vault_root=vault_root,
        )
    except PathSafetyError:
        raise
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
    _assert_archive_isolated(archive_root, vault_root)
    vault = vault_publisher.validate_vault_root(vault_root)
    return {
        "api_schema_version": SCHEMA_VERSION,
        "commands": [
            "capabilities", "newspaper-registry", "daily-dashboard", "inbox",
            "issue", "import-file", "draft-save", "reading-mark", "publish-plan",
            "publish-apply", "history", "rollback",
        ],
        "newspaper_registry": newspaper_registry(),
        "ocr_review_statuses": list(OCR_REVIEW_STATUSES),
        "reading_statuses": list(READING_STATUSES),
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
    if command == "newspaper-registry":
        return newspaper_registry(), []
    if command == "daily-dashboard":
        data = get_daily_dashboard(args.archive, args.date, args.source)
        return data, []
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
                           source=args.source or "zgjsb", day=args.date,
                           vault_root=args.vault)
        return data, data.get("warnings", [])
    if command == "draft-save":
        input_path = Path(_require(args.input, "--input")).expanduser()
        draft = _load_json(input_path)
        if draft is None:
            raise ValidationError("草稿 JSON 不存在或无效")
        return save_draft(args.archive, args.vault, draft), []
    if command == "reading-mark":
        return mark_reading_activity(
            args.archive,
            args.vault,
            _require(args.source, "--source"),
            _require(args.date, "--date"),
            _require(args.status, "--status"),
        ), []
    if command == "publish-plan":
        _assert_archive_isolated(args.archive, args.vault)
        source = _require(args.source, "--source")
        day = _require(args.date, "--date")
        issue = get_issue(args.archive, source, day)
        if issue.get("local_pdf_date_verification") == "unverified":
            raise ValidationError("本地 PDF 的第一页报头日期尚未唯一核验，禁止发布")
        draft = _load_draft(args.archive, source, day)
        if not draft:
            raise ValidationError("请先保存完整草稿")
        # Revalidate immediately before planning so stale/manual draft files
        # cannot bypass the review schema.
        draft = validate_draft(args.archive, draft, require_publish_ready=True)
        return vault_publisher.create_plan(args.archive, args.vault, issue, draft), issue["warnings"]
    if command == "publish-apply":
        _assert_archive_isolated(args.archive, args.vault)
        return vault_publisher.apply_plan(
            args.archive, args.vault, _require(args.plan_id, "--plan-id")
        ), []
    if command == "history":
        return {"transactions": vault_publisher.history(args.archive, args.limit)}, []
    if command == "rollback":
        _assert_archive_isolated(args.archive, args.vault)
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
        "capabilities", "newspaper-registry", "daily-dashboard", "inbox", "issue",
        "import-file", "draft-save", "reading-mark", "publish-plan",
        "publish-apply", "history", "rollback",
    ])
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--source")
    parser.add_argument("--date")
    parser.add_argument("--path")
    parser.add_argument("--input")
    parser.add_argument("--plan-id")
    parser.add_argument("--transaction-id")
    parser.add_argument("--status")
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
