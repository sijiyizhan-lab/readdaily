#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import a local newspaper PDF into readdaily's normalized archive.

The importer is deliberately independent from the web adapters.  It copies the
source PDF into a content-addressed application-data directory, renders/OCRs it
with a small native macOS helper, and never writes to an Obsidian vault.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lib  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PDFOCR_SOURCE = REPO / "scripts" / "pdfocr.swift"
PREBUILT_PDFOCR = REPO / "skills" / "newspaper-fetch" / "bin" / "pdfocr"
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "READDAILY_ARCHIVE",
        str(Path.home() / "Library" / "Application Support" / "readdaily" / "news-archive"),
    )
).expanduser()
DEFAULT_VAULT = Path(
    os.environ.get(
        "READDAILY_VAULT",
        str(Path.home() / "Library" / "Application Support" / "readdaily" / "vault"),
    )
).expanduser()
MAX_PDF_BYTES = 250 * 1024 * 1024
LOCAL_PDF_SOURCE = "zgjsb"
_IMPORT_THREAD_LOCKS: Dict[Tuple[str, str], threading.Lock] = {}
_IMPORT_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for_issue(archive_identity: str, day: str) -> threading.Lock:
    key = (archive_identity, day)
    with _IMPORT_THREAD_LOCKS_GUARD:
        lock = _IMPORT_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _IMPORT_THREAD_LOCKS[key] = lock
        return lock


def _validate_local_pdf_source(source: str) -> str:
    """Local PDF ingestion is intentionally limited to 中国建设报."""
    if not isinstance(source, str) or source != LOCAL_PDF_SOURCE:
        raise ValueError("本地 PDF 仅允许导入中国建设报（zgjsb）")
    return source


@contextlib.contextmanager
def _issue_date_lock(archive_root: os.PathLike[str] | str, day: str):
    """Share the fetch/publisher evidence lock for local issue mutation."""
    normalized_day = dt.date.fromisoformat(str(day)).isoformat()
    archive_identity = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(archive_root)))
    )
    archive_key = hashlib.sha256(archive_identity.encode("utf-8")).hexdigest()
    lock_directory = os.path.join(
        tempfile.gettempdir(), "readdaily-fetch-locks", archive_key
    )
    os.makedirs(lock_directory, mode=0o700, exist_ok=True)
    if os.path.islink(lock_directory) or not os.path.isdir(lock_directory):
        raise ValueError("报纸证据锁目录必须是真实目录")
    lock_path = os.path.join(lock_directory, normalized_day + ".lock")
    thread_lock = _thread_lock_for_issue(archive_identity, normalized_day)
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("报纸证据锁不是可信普通文件")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _load_json(path: Path, default=None):
    return lib.load_json(str(path), default)


def _save_json(path: Path, obj: Dict[str, Any]) -> None:
    lib.save_json(str(path), obj)


def parse_filename_metadata(name: str) -> Dict[str, Optional[str]]:
    """Extract advisory date/issue metadata from a human-readable filename."""
    date_match = re.search(r"(20\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})(?:日)?", name)
    issue_match = re.search(r"第\s*(\d{3,7})\s*期", name)
    date_value = None
    if date_match:
        try:
            date_value = dt.date(
                int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            ).isoformat()
        except ValueError:
            date_value = None
    return {
        "date": date_value,
        "issue_no": issue_match.group(1) if issue_match else None,
    }


def _normalize_ocr_digits(value: str) -> str:
    table = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "丨": "1"})
    return value.translate(table).replace(" ", "")


def issue_no_from_header(header_text: str) -> Optional[str]:
    compact = re.sub(r"[\t\r\n]+", " ", header_text or "")
    match = re.search(r"第\s*([0-9OoIl丨\s]{3,9})\s*期", compact)
    if not match:
        return None
    value = _normalize_ocr_digits(match.group(1))
    return value if value.isdigit() else None


def issue_dates_from_header(header_text: str) -> List[str]:
    """Return distinct full calendar dates visible near the page-one masthead.

    The OCR helper currently returns reading-order text without coordinates, so
    only the first 1,200 normalized characters are treated as the masthead
    region.  A result is authoritative only when this list has exactly one
    value; multiple full dates are deliberately considered ambiguous.
    """
    sample = unicodedata.normalize("NFKC", str(header_text or ""))[:1200]
    candidates = set()
    patterns = (
        r"(?<!\d)(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?",
        r"(?<!\d)(20\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, sample):
            try:
                value = dt.date(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                ).isoformat()
            except ValueError:
                continue
            candidates.add(value)
    return sorted(candidates)


def resolve_issue_date(
    chosen_date: str, filename_date: Optional[str], header_text: str
) -> Tuple[Optional[str], List[str], bool]:
    """Verify the chosen archive date against page-one OCR.

    ``False`` means the masthead date could not be established uniquely and
    the resulting local-PDF issue must remain review-only.  A unique conflicting
    date is stronger evidence and therefore rejects the import entirely.
    """
    warnings: List[str] = []
    header_dates = issue_dates_from_header(header_text)
    if not header_dates:
        warnings.append("未从第一页唯一识别报头日期；本地 PDF 仅供复核，禁止发布。")
        return None, warnings, False
    if len(header_dates) > 1:
        warnings.append(
            "第一页报头识别到多个完整日期（%s）；本地 PDF 仅供复核，禁止发布。"
            % "、".join(header_dates)
        )
        return None, warnings, False
    header_date = header_dates[0]
    if header_date != chosen_date:
        raise ValueError(
            "报头日期 %s 与归档日期 %s 不一致，已拒绝导入。"
            % (header_date, chosen_date)
        )
    if filename_date and filename_date != header_date:
        warnings.append(
            "文件名日期 %s 与报头日期 %s 不一致；报头已确认指定归档日期。"
            % (filename_date, header_date)
        )
    return header_date, warnings, True


def resolve_issue_no(
    filename_issue: Optional[str], header_text: str
) -> Tuple[Optional[str], List[str], bool]:
    """Prefer the issue number printed on page one over filename metadata."""
    warnings: List[str] = []
    header_issue = issue_no_from_header(header_text)
    if header_issue and filename_issue and header_issue != filename_issue:
        warnings.append(
            f"文件名期号 {filename_issue} 与报头期号 {header_issue} 不一致；已采用报头，需人工复核。"
        )
        return header_issue, warnings, True
    if header_issue:
        return header_issue, warnings, False
    if filename_issue:
        warnings.append(f"未从报头识别期号，暂采用文件名期号 {filename_issue}，需人工复核。")
        return filename_issue, warnings, True
    warnings.append("未识别到报纸期号，需人工补录。")
    return None, warnings, True


def validate_pdf(path: os.PathLike[str] | str, max_bytes: int = MAX_PDF_BYTES) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"PDF 文件不存在：{source}")
    if source.suffix.lower() != ".pdf":
        raise ValueError("仅支持 PDF 文件")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError("PDF 文件为空")
    if size > max_bytes:
        raise ValueError(f"PDF 超过 {max_bytes // (1024 * 1024)}MB 限制")
    with source.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            raise ValueError("文件扩展名为 PDF，但缺少有效 PDF 文件头")
    return source


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip(" .")
    return cleaned[:180] or "newspaper.pdf"


def _copy_content_addressed(
    source: Path, archive: Path, digest: str, original_name: Optional[str] = None
) -> Path:
    imports = archive / "_imports"
    lib.durable_makedirs(imports, exist_ok=True)
    target_dir = imports / digest
    lib.durable_makedirs(target_dir, exist_ok=True)
    target = target_dir / _safe_filename(original_name or source.name)
    if lib.path_exists(target):
        if hashlib.sha256(lib.read_bytes(target)).hexdigest() != digest:
            raise RuntimeError(f"内容寻址目录中存在哈希不符文件：{target}")
        return target
    try:
        lib.durable_copy_file(source, target, expected_sha256=digest)
    except RuntimeError as exc:
        raise RuntimeError(
            "稳定 PDF 副本在内容寻址提交前发生变化，已拒绝导入"
        ) from exc
    return target


@contextlib.contextmanager
def _verified_pdf_snapshot(source: Path, archive: Path):
    """Freeze one source revision before OCR and verify the copy end to end."""
    source = validate_pdf(source)
    before_digest = sha256_file(source)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="readdaily-source-snapshot-", suffix=".pdf"
    )
    snapshot = Path(temporary_name)
    try:
        with source.open("rb") as source_stream, os.fdopen(
            descriptor, "wb"
        ) as snapshot_stream:
            descriptor = -1
            shutil.copyfileobj(source_stream, snapshot_stream, 1024 * 1024)
            snapshot_stream.flush()
            os.fsync(snapshot_stream.fileno())
        snapshot_digest = sha256_file(snapshot)
        after_digest = sha256_file(source)
        if not (
            before_digest == snapshot_digest == after_digest
        ):
            raise ValueError("导入期间源 PDF 内容发生变化，请待文件稳定后重试")
        validate_pdf(snapshot)
        yield snapshot, snapshot_digest
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if snapshot.exists():
            snapshot.unlink()


def _helper_binary(archive: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("本地图片 PDF OCR 目前仅支持 macOS")

    configured = os.environ.get("READDAILY_PDFOCR")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(f"READDAILY_PDFOCR 指向不可执行文件：{candidate}")
        return candidate

    if PREBUILT_PDFOCR.is_file() and os.access(PREBUILT_PDFOCR, os.X_OK):
        return PREBUILT_PDFOCR

    if not PDFOCR_SOURCE.is_file():
        raise RuntimeError(f"缺少 PDF OCR 源码：{PDFOCR_SOURCE}")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError("未找到 swiftc；请安装 Xcode Command Line Tools 后重试")
    source_hash = hashlib.sha256(PDFOCR_SOURCE.read_bytes()).hexdigest()[:16]
    runtime = archive / "_runtime"
    lib.durable_makedirs(runtime, exist_ok=True)
    binary = runtime / f"pdfocr-{source_hash}"
    if lib.path_is_file(binary) and os.access(binary, os.X_OK):
        return binary
    # swiftc is an external pathname-based writer. Compile only in a private
    # system staging directory, then copy the verified executable into the
    # pinned archive with the common atomic writer.
    build_root = Path(tempfile.mkdtemp(prefix="readdaily-pdfocr-build-"))
    temporary = build_root / binary.name
    try:
        command = [
            swiftc,
            "-O",
            "-framework",
            "PDFKit",
            "-framework",
            "Vision",
            "-framework",
            "AppKit",
            str(PDFOCR_SOURCE),
            "-o",
            str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1200:]
            raise RuntimeError(f"PDF OCR 组件编译失败：{detail}")
        os.chmod(temporary, 0o755)
        lib.fsync_file(temporary)
        binary_digest = sha256_file(temporary)
        lib.durable_copy_file(
            temporary, binary, expected_sha256=binary_digest
        )
        lib.durable_chmod(binary, 0o755)
    finally:
        shutil.rmtree(build_root, ignore_errors=True)
    return binary


def run_pdfocr(
    pdf_path: os.PathLike[str] | str,
    output_dir: os.PathLike[str] | str,
    archive_root: os.PathLike[str] | str,
    accurate: bool = True,
) -> Dict[str, Any]:
    archive = Path(archive_root).expanduser().absolute()
    binary = _helper_binary(archive)
    executable = binary
    execution_copy = None
    try:
        try:
            binary.relative_to(archive)
        except ValueError:
            pass
        else:
            descriptor, execution_name = tempfile.mkstemp(
                prefix="readdaily-pdfocr-exec-"
            )
            execution_copy = Path(execution_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(lib.read_bytes(binary))
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            os.chmod(execution_copy, 0o700)
            executable = execution_copy
        command = [
            str(executable), str(Path(pdf_path).resolve()),
            str(Path(output_dir).resolve())
        ]
        command.append("--accurate" if accurate else "--fast")
        result = subprocess.run(command, capture_output=True, text=True)
    finally:
        if execution_copy is not None:
            try:
                execution_copy.unlink()
            except FileNotFoundError:
                pass
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1600:]
        raise RuntimeError(f"PDF 渲染/OCR 失败：{detail}")
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("PDF OCR 组件返回了无效结果") from exc
    return _validate_pdfocr_manifest(manifest, output_dir)


def _manifest_file(
    output_dir: os.PathLike[str] | str,
    value: Any,
    field: str,
    expected_directory: str,
    page_number: int,
) -> Path:
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\\" in value or re.search(r"[\x00-\x1f]", value)):
        raise RuntimeError(
            f"PDF OCR 第{page_number}页 {field} 必须是安全相对路径"
        )
    raw_parts = value.split("/")
    if (not raw_parts or raw_parts[0] != expected_directory
            or any(part in ("", ".", "..") for part in raw_parts)):
        raise RuntimeError(
            f"PDF OCR 第{page_number}页 {field} 必须是安全相对路径"
        )

    root = Path(output_dir).resolve()
    candidate = root.joinpath(*raw_parts)
    cursor = root
    for part in raw_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeError(
                f"PDF OCR 第{page_number}页 {field} 不得包含符号链接，必须是普通文件"
            )
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"PDF OCR 第{page_number}页 {field} 必须指向存在的普通文件"
        ) from exc
    if not candidate.is_file():
        raise RuntimeError(
            f"PDF OCR 第{page_number}页 {field} 必须指向存在的普通文件"
        )
    return candidate


def _validate_pdfocr_manifest(
    manifest: Any, output_dir: os.PathLike[str] | str
) -> Dict[str, Any]:
    """Validate the native helper's complete page manifest and local files."""
    if not isinstance(manifest, dict):
        raise RuntimeError("PDF OCR 结果必须是对象")
    page_count = manifest.get("page_count")
    pages = manifest.get("pages")
    if (isinstance(page_count, bool) or not isinstance(page_count, int)
            or page_count < 1 or not isinstance(pages, list)):
        raise RuntimeError("PDF OCR 结果缺少有效 page_count 或页面清单")
    if len(pages) != page_count:
        raise RuntimeError(
            "PDF OCR page_count=%s 与页面数量=%s 不一致" % (
                page_count, len(pages)
            )
        )
    if any(not isinstance(page, dict) for page in pages):
        raise RuntimeError("PDF OCR 页面清单每项必须是对象")

    numbers = [page.get("number") for page in pages]
    if (any(isinstance(number, bool) or not isinstance(number, int)
            for number in numbers)
            or numbers != list(range(1, page_count + 1))):
        raise RuntimeError(
            f"PDF OCR 页面编号必须恰为 1...{page_count}，不得缺失或重复"
        )

    seen_images = set()
    seen_texts = set()
    for page in pages:
        number = page["number"]
        image = page.get("image")
        text = page.get("text")
        _manifest_file(output_dir, image, "image", "pages", number)
        _manifest_file(output_dir, text, "text", "text", number)
        if image in seen_images or text in seen_texts:
            raise RuntimeError("PDF OCR 页面清单存在重复 image 或 text 路径")
        seen_images.add(image)
        seen_texts.add(text)
    return manifest


def _unit_text(issue_dir: Path, unit: Dict[str, Any]) -> str:
    if unit.get("text"):
        return str(unit["text"])
    text_path = unit.get("text_path")
    if text_path:
        candidate = (issue_dir / text_path).resolve()
        try:
            candidate.relative_to(issue_dir.resolve())
        except ValueError:
            return ""
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
    return ""


def _existing_page_count(issue: Dict[str, Any]) -> int:
    return len(issue.get("editions") or issue.get("units") or [])


def _validate_existing_pdf_binding(
    issue: Dict[str, Any], incoming_issue_no: Optional[str], page_count: int
) -> None:
    """Reject a PDF that cannot describe the already archived issue exactly."""
    existing_issue_no = str(issue.get("issue_no") or "").strip()
    normalized_incoming_issue_no = str(incoming_issue_no or "").strip()
    if (existing_issue_no and normalized_incoming_issue_no
            and existing_issue_no != normalized_incoming_issue_no):
        raise ValueError(
            "既有归档期号 %s 与导入 PDF 期号 %s 不一致，已拒绝绑定。"
            % (existing_issue_no, normalized_incoming_issue_no)
        )

    for field in ("editions", "units"):
        entries = issue.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"既有 issue.json 的 {field} 必须是数组")
        if len(entries) != page_count:
            raise ValueError(
                "PDF 页数 %s 与既有 %s 数量 %s 不一致，已拒绝绑定。"
                % (page_count, field, len(entries))
            )


def _invalidate_current_publication(archive: Path, source: str, day: str) -> None:
    """A same-day PDF rebind changes evidence, so old publish state is stale."""
    state_path = archive / "_state" / source / f"{day}.json"
    state = _load_json(state_path, {}) or {}
    if not isinstance(state, dict):
        state = {}
    stages = state.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    else:
        stages = dict(stages)
    previous = {
        key: state.get(key)
        for key in (
            "publish_plan_id", "publish_transaction_id",
            "publish_archive_evidence_sha256", "publish_draft_sha256",
        )
        if state.get(key)
    }
    for stage in ("summarized", "published", "archived"):
        stages.pop(stage, None)
    for key in (
        "publish_plan_id", "publish_transaction_id",
        "publish_archive_evidence_sha256", "publish_draft_sha256",
    ):
        state.pop(key, None)
    state["stages"] = stages
    if previous:
        state["stale_publication"] = {
            **previous,
            "invalidated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "reason": "本地 PDF 重新绑定后原发布证据已失效",
        }
    _save_json(state_path, state)


def _import_pdf_locked(
    pdf_path: os.PathLike[str] | str,
    archive_root: os.PathLike[str] | str = DEFAULT_ARCHIVE,
    source: str = "zgjsb",
    source_name: str = "中国建设报",
    date: Optional[str] = None,
    original_filename: Optional[str] = None,
    expected_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Import one PDF and return a JSON-friendly review result."""
    source = _validate_local_pdf_source(source)
    source_pdf = validate_pdf(pdf_path)
    filename_meta = parse_filename_metadata(original_filename or source_pdf.name)

    warnings: List[str] = []
    needs_review = False
    chosen_date = date or filename_meta["date"]
    if date and filename_meta["date"] and date != filename_meta["date"]:
        warnings.append(
            f"指定日期 {date} 与文件名日期 {filename_meta['date']} 不一致；已采用指定日期，需人工复核。"
        )
        needs_review = True
    if not chosen_date:
        raise ValueError("无法从文件名识别日期；请用 --date YYYY-MM-DD 指定")
    try:
        chosen_date = dt.date.fromisoformat(chosen_date).isoformat()
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD") from exc

    archive = Path(archive_root).expanduser().absolute()
    lib.durable_makedirs(archive, exist_ok=True)
    digest = sha256_file(source_pdf)
    if expected_digest is not None and digest != expected_digest:
        raise RuntimeError("稳定 PDF 副本哈希不一致，已拒绝导入")
    issue_dir = archive / source / chosen_date
    issue_path = issue_dir / "issue.json"
    existing = _load_json(issue_path, None)
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError(f"既有 issue.json 不是对象：{issue_path}")
        if existing.get("source") != source or existing.get("date") != chosen_date:
            raise ValueError("既有 issue.json 的来源或日期与归档目录不一致")

    lib.durable_makedirs(issue_dir.parent, exist_ok=True)
    # The native OCR helper writes by pathname. Keep that untrusted work in a
    # private system temporary directory, then import only validated bytes via
    # the pinned archive session's atomic tree committer.
    staging = Path(tempfile.mkdtemp(
        prefix=f"readdaily-{chosen_date}.import-"
    ))
    try:
        manifest = run_pdfocr(source_pdf, staging, archive, accurate=True)
        manifest = _validate_pdfocr_manifest(manifest, staging)
        if sha256_file(source_pdf) != digest:
            raise RuntimeError("稳定 PDF 副本在 OCR 期间发生变化，已拒绝导入")
        pages = sorted(manifest["pages"], key=lambda item: int(item["number"]))
        if not pages:
            raise RuntimeError("PDF OCR 结果没有任何页面")
        first_text_path = staging / pages[0]["text"]
        first_text = first_text_path.read_text(encoding="utf-8") if first_text_path.exists() else ""
        header_date, date_warnings, date_verified = resolve_issue_date(
            chosen_date, filename_meta["date"], first_text
        )
        warnings.extend(date_warnings)
        needs_review = needs_review or not date_verified
        issue_no, no_warnings, no_review = resolve_issue_no(
            filename_meta["issue_no"], first_text
        )
        warnings.extend(no_warnings)
        needs_review = needs_review or no_review

        if existing is not None:
            _validate_existing_pdf_binding(existing, issue_no, len(pages))
            authoritative = str(existing.get("issue_no") or issue_no or "") or None
            copied_pdf = _copy_content_addressed(
                source_pdf, archive, digest, original_filename
            )
            files = dict(existing.get("files") or {})
            files["local_pdf"] = str(copied_pdf)
            existing["files"] = files
            existing["source_sha256"] = digest
            existing["local_pdf_header_date"] = header_date
            existing["local_pdf_date_verification"] = (
                "verified" if date_verified else "unverified"
            )
            existing["import_warnings"] = warnings
            existing["imported_at"] = dt.datetime.now().isoformat(timespec="seconds")
            _save_json(issue_path, existing)
            _invalidate_current_publication(
                archive, source, chosen_date
            )
            lib.durable_rmtree(staging)
            return {
                "source": source,
                "date": chosen_date,
                "issue_no": authoritative,
                "page_count": _existing_page_count(existing),
                "pdf_path": str(copied_pdf),
                "issue_path": str(issue_path),
                "source_sha256": digest,
                "local_pdf_header_date": header_date,
                "local_pdf_date_verification": (
                    "verified" if date_verified else "unverified"
                ),
                "warnings": warnings,
                "needs_review": needs_review,
                "imported": False,
            }

        copied_pdf = _copy_content_addressed(
            source_pdf, archive, digest, original_filename
        )
        editions, units = [], []
        all_pages_parsed = True
        for page in pages:
            number = int(page["number"])
            characters = int(page.get("characters") or 0)
            name = "待复核"
            editions.append({"no": number, "name": name, "page_image": page["image"]})
            units.append(
                {
                    "id": f"{source}_{chosen_date.replace('-', '')}_{number:02d}",
                    "type": "edition_ocr",
                    "title": f"{number}版 {name}",
                    "page_image": page["image"],
                    "text_path": page["text"],
                }
            )
            if characters < 30:
                all_pages_parsed = False
                warnings.append(f"第{number}版 OCR 文字不足，需人工复核或重跑。")
                needs_review = True

        issue = {
            "source": source,
            "source_name": source_name,
            "date": chosen_date,
            "issue_no": issue_no,
            "channel": "local_pdf",
            "editions": editions,
            "units": units,
            "files": {"local_pdf": str(copied_pdf)},
            "source_sha256": digest,
            "local_pdf_header_date": header_date,
            "local_pdf_date_verification": (
                "verified" if date_verified else "unverified"
            ),
            "import_warnings": warnings,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        issue_files = []
        for page in pages:
            issue_files.append((
                page["image"], (staging / page["image"]).read_bytes()
            ))
            issue_files.append((
                page["text"], (staging / page["text"]).read_bytes()
            ))
        lib.commit_issue_tree(str(issue_dir), issue_files, issue)
        lib.durable_rmtree(staging)
        staging = None
    except Exception:
        if staging is not None and staging.exists():
            lib.durable_rmtree(staging)
        raise

    now = dt.datetime.now().isoformat(timespec="seconds")
    stages = {"imported": now, "fetched": now}
    if all_pages_parsed and date_verified:
        stages["parsed"] = now
    state = {
        "stages": stages,
        "units": len(units),
        "source_sha256": digest,
        "needs_review": needs_review,
        "warnings": warnings,
    }
    state_path = archive / "_state" / source / f"{chosen_date}.json"
    lib.durable_makedirs(state_path.parent, exist_ok=True)
    _save_json(state_path, state)
    return {
        "source": source,
        "date": chosen_date,
        "issue_no": issue_no,
        "page_count": len(editions),
        "pdf_path": str(copied_pdf),
        "issue_path": str(issue_path),
        "source_sha256": digest,
        "local_pdf_header_date": header_date,
        "local_pdf_date_verification": (
            "verified" if date_verified else "unverified"
        ),
        "warnings": warnings,
        "needs_review": needs_review,
        "imported": True,
    }


def import_pdf(
    pdf_path: os.PathLike[str] | str,
    archive_root: os.PathLike[str] | str = DEFAULT_ARCHIVE,
    source: str = "zgjsb",
    source_name: str = "中国建设报",
    date: Optional[str] = None,
    vault_root: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Import under the same archive/date lock used by fetch and publish."""
    source = _validate_local_pdf_source(source)
    source_pdf = validate_pdf(pdf_path)
    filename_meta = parse_filename_metadata(source_pdf.name)
    chosen_date = date or filename_meta.get("date")
    if not chosen_date:
        raise ValueError("无法从文件名识别日期；请用 --date YYYY-MM-DD 指定")
    try:
        chosen_date = dt.date.fromisoformat(str(chosen_date)).isoformat()
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD") from exc
    with _issue_date_lock(archive_root, chosen_date):
        archive = Path(archive_root).expanduser().absolute()
        selected_vault = (
            vault_root or os.environ.get("READDAILY_VAULT") or DEFAULT_VAULT
        )
        lib.assert_configured_roots_separate(
            archive, selected_vault, label="Vault"
        )
        with lib.archive_session(archive, create=True) as archive_handle:
            lib.assert_session_isolated(
                archive_handle,
                selected_vault,
                label="Vault",
            )
            archive = Path(archive_handle.canonical_root)
            lib.durable_makedirs(archive, exist_ok=True)
            with _verified_pdf_snapshot(source_pdf, archive) as (snapshot, digest):
                return _import_pdf_locked(
                    snapshot,
                    archive,
                    source=source,
                    source_name=source_name,
                    date=chosen_date,
                    original_filename=source_pdf.name,
                    expected_digest=digest,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地报纸 PDF（macOS PDFKit + Vision OCR）")
    parser.add_argument("path")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--source", default="zgjsb")
    parser.add_argument("--source-name", default="中国建设报")
    parser.add_argument("--date")
    parser.add_argument("--vault", default=None)
    args = parser.parse_args()
    try:
        result = import_pdf(
            args.path,
            args.archive,
            source=args.source,
            source_name=args.source_name,
            date=args.date,
            vault_root=args.vault,
        )
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
