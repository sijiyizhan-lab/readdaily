#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import a local newspaper PDF into readdaily's normalized archive.

The importer is deliberately independent from the web adapters.  It copies the
source PDF into a content-addressed application-data directory, renders/OCRs it
with a small native macOS helper, and never writes to an Obsidian vault.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[3]
PDFOCR_SOURCE = REPO / "scripts" / "pdfocr.swift"
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "READDAILY_ARCHIVE",
        str(Path.home() / "Library" / "Application Support" / "readdaily" / "news-archive"),
    )
).expanduser()
MAX_PDF_BYTES = 250 * 1024 * 1024


def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return default


def _save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _copy_content_addressed(source: Path, archive: Path, digest: str) -> Path:
    target_dir = archive / "_imports" / digest
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_filename(source.name)
    if target.exists():
        if sha256_file(target) != digest:
            raise RuntimeError(f"内容寻址目录中存在哈希不符文件：{target}")
        return target
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)
    return target


def _helper_binary(archive: Path) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("本地图片 PDF OCR 目前仅支持 macOS")
    if not PDFOCR_SOURCE.is_file():
        raise RuntimeError(f"缺少 PDF OCR 源码：{PDFOCR_SOURCE}")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError("未找到 swiftc；请安装 Xcode Command Line Tools 后重试")
    source_hash = hashlib.sha256(PDFOCR_SOURCE.read_bytes()).hexdigest()[:16]
    runtime = archive / "_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    binary = runtime / f"pdfocr-{source_hash}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    temporary = runtime / f".{binary.name}.{os.getpid()}.tmp"
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
        temporary.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise RuntimeError(f"PDF OCR 组件编译失败：{detail}")
    os.chmod(temporary, 0o755)
    try:
        os.replace(temporary, binary)
    except OSError:
        temporary.unlink(missing_ok=True)
        if not binary.exists():
            raise
    return binary


def run_pdfocr(
    pdf_path: os.PathLike[str] | str,
    output_dir: os.PathLike[str] | str,
    archive_root: os.PathLike[str] | str,
    accurate: bool = True,
) -> Dict[str, Any]:
    archive = Path(archive_root).expanduser().resolve()
    binary = _helper_binary(archive)
    command = [str(binary), str(Path(pdf_path).resolve()), str(Path(output_dir).resolve())]
    command.append("--accurate" if accurate else "--fast")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1600:]
        raise RuntimeError(f"PDF 渲染/OCR 失败：{detail}")
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("PDF OCR 组件返回了无效结果") from exc
    if not isinstance(manifest.get("pages"), list) or not manifest.get("page_count"):
        raise RuntimeError("PDF OCR 结果缺少页面清单")
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


def import_pdf(
    pdf_path: os.PathLike[str] | str,
    archive_root: os.PathLike[str] | str = DEFAULT_ARCHIVE,
    source: str = "zgjsb",
    source_name: str = "中国建设报",
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Import one PDF and return a JSON-friendly review result."""
    source_pdf = validate_pdf(pdf_path)
    archive = Path(archive_root).expanduser().resolve()
    archive.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(source_pdf)
    copied_pdf = _copy_content_addressed(source_pdf, archive, digest)
    filename_meta = parse_filename_metadata(source_pdf.name)

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

    issue_dir = archive / source / chosen_date
    issue_path = issue_dir / "issue.json"
    existing = _load_json(issue_path, None)
    if existing:
        first_text = ""
        if existing.get("units"):
            first_text = _unit_text(issue_dir, existing["units"][0])
        resolved_no, no_warnings, no_review = resolve_issue_no(
            filename_meta["issue_no"], first_text
        )
        warnings.extend(no_warnings)
        needs_review = needs_review or no_review
        authoritative = str(existing.get("issue_no") or resolved_no or "") or None
        if resolved_no and authoritative and resolved_no != authoritative:
            warnings.append(
                f"既有归档期号 {authoritative} 与导入文件识别期号 {resolved_no} 不一致；保留既有归档。"
            )
            needs_review = True
        files = dict(existing.get("files") or {})
        files["local_pdf"] = str(copied_pdf)
        existing["files"] = files
        existing["source_sha256"] = digest
        existing["import_warnings"] = warnings
        existing["imported_at"] = dt.datetime.now().isoformat(timespec="seconds")
        _save_json(issue_path, existing)
        return {
            "source": source,
            "date": chosen_date,
            "issue_no": authoritative,
            "page_count": _existing_page_count(existing),
            "pdf_path": str(copied_pdf),
            "issue_path": str(issue_path),
            "source_sha256": digest,
            "warnings": warnings,
            "needs_review": needs_review,
            "imported": False,
        }

    issue_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = issue_dir.parent / f".{chosen_date}.import-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        manifest = run_pdfocr(copied_pdf, staging, archive, accurate=True)
        pages = sorted(manifest["pages"], key=lambda item: int(item["number"]))
        first_text_path = staging / pages[0]["text"]
        first_text = first_text_path.read_text(encoding="utf-8") if first_text_path.exists() else ""
        issue_no, no_warnings, no_review = resolve_issue_no(
            filename_meta["issue_no"], first_text
        )
        warnings.extend(no_warnings)
        needs_review = needs_review or no_review

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
            "import_warnings": warnings,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        _save_json(staging / "issue.json", issue)
        os.replace(staging, issue_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    now = dt.datetime.now().isoformat(timespec="seconds")
    stages = {"imported": now, "fetched": now}
    if all_pages_parsed:
        stages["parsed"] = now
    state = {
        "stages": stages,
        "units": len(units),
        "source_sha256": digest,
        "needs_review": needs_review,
        "warnings": warnings,
    }
    state_path = archive / "_state" / source / f"{chosen_date}.json"
    _save_json(state_path, state)
    return {
        "source": source,
        "date": chosen_date,
        "issue_no": issue_no,
        "page_count": len(editions),
        "pdf_path": str(copied_pdf),
        "issue_path": str(issue_path),
        "source_sha256": digest,
        "warnings": warnings,
        "needs_review": needs_review,
        "imported": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导入本地报纸 PDF（macOS PDFKit + Vision OCR）")
    parser.add_argument("path")
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--source", default="zgjsb")
    parser.add_argument("--source-name", default="中国建设报")
    parser.add_argument("--date")
    args = parser.parse_args()
    try:
        result = import_pdf(
            args.path,
            args.archive,
            source=args.source,
            source_name=args.source_name,
            date=args.date,
        )
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
