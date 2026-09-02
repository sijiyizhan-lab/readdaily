#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器：微信「读报」渠道（示例：中国建设报）。

采集复用 jianshebao-daily 的已验证引擎（搜狗定位→src=11→版面图+电子报 PDF），
归一化输出统一 issue.json；版面文本用 Vision OCR（版级单位，文章粒度由 reader 处理）。
支持离线回退（文章/图已在本地时不再触发网络搜索）。
"""
import copy
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lib  # noqa: E402

DEFAULT_ENGINE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "wechat_engine.py"))
VOCR = os.environ.get("READDAILY_VOCR") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "vocr"))
MIN_OCR_CHARACTERS = 200
MIN_PAGE_IMAGE_BYTES = 1024
MIN_PAGE_SHORT_EDGE = 1200
MIN_PAGE_LONG_EDGE = 1600
OCR_MANIFEST_SCHEMA_VERSION = 1


def _jpeg_dimensions(raw):
    """Return JPEG SOF dimensions after structural marker checks."""
    if not raw.startswith(b"\xff\xd8"):
        return None
    if not raw.rstrip(b" \t\r\n").endswith(b"\xff\xd9"):
        return None
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    dimensions = None
    saw_scan = False
    while offset < len(raw):
        if raw[offset] != 0xFF:
            return None
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            return None
        marker = raw[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker == 0xDA:
            saw_scan = True
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(raw):
            return None
        segment_length = int.from_bytes(raw[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(raw):
            return None
        if marker in sof_markers:
            if segment_length < 8:
                return None
            height = int.from_bytes(raw[offset + 3:offset + 5], "big")
            width = int.from_bytes(raw[offset + 5:offset + 7], "big")
            dimensions = (width, height)
        offset += segment_length
    return dimensions if dimensions and saw_scan else None


def _png_dimensions(raw):
    """Return PNG IHDR dimensions when signature and terminal IEND exist."""
    signature = b"\x89PNG\r\n\x1a\n"
    iend = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
    if not raw.startswith(signature) or not raw.endswith(iend):
        return None
    if len(raw) < 33 or raw[12:16] != b"IHDR":
        return None
    if int.from_bytes(raw[8:12], "big") != 13:
        return None
    return (
        int.from_bytes(raw[16:20], "big"),
        int.from_bytes(raw[20:24], "big"),
    )


def _validated_page_image(raw):
    """Validate a newspaper page without optional image-library dependencies."""
    common_error = lib.image_validation_error(
        raw, min_bytes=MIN_PAGE_IMAGE_BYTES
    )
    if common_error:
        raise ValueError(common_error)
    image_format = lib.detect_image_format(raw)
    dimensions = None
    if image_format == "jpeg":
        dimensions = _jpeg_dimensions(raw)
    elif image_format == "png":
        dimensions = _png_dimensions(raw)
    if dimensions is None:
        raise ValueError("图片格式不支持版面尺寸校验，或文件结构/结尾不完整")
    width, height = dimensions
    if (min(width, height) < MIN_PAGE_SHORT_EDGE
            or max(width, height) < MIN_PAGE_LONG_EDGE):
        raise ValueError("尺寸过小：%sx%s" % (width, height))
    return {
        "format": image_format,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _ocr_manifest(unit, source_id, day, text_relative, image_sha256, text):
    return {
        "schema_version": OCR_MANIFEST_SCHEMA_VERSION,
        "source": source_id,
        "date": day,
        "unit_id": unit["id"],
        "page_image": unit["page_image"],
        "page_image_sha256": image_sha256,
        "text_path": text_relative,
        "text_sha256": _sha256_bytes(text.encode("utf-8")),
    }


def _reusable_ocr_text(
        unit, source_id, day, text_relative, text_path, manifest_path,
        image_sha256):
    """Reuse OCR only when issue, sidecar, image and text identities all bind."""
    if unit.get("page_image_sha256") != image_sha256:
        return None
    if unit.get("text_path") != text_relative:
        return None
    if unit.get("ocr_manifest_path") != os.path.join(
            "text", os.path.basename(manifest_path)):
        return None
    if not lib.path_is_file(text_path) or not lib.path_is_file(manifest_path):
        return None
    manifest = lib.load_json(manifest_path)
    expected = {
        "schema_version": OCR_MANIFEST_SCHEMA_VERSION,
        "source": source_id,
        "date": day,
        "unit_id": unit.get("id"),
        "page_image": unit.get("page_image"),
        "page_image_sha256": image_sha256,
        "text_path": text_relative,
    }
    if not isinstance(manifest, dict):
        return None
    if any(manifest.get(key) != value for key, value in expected.items()):
        return None
    try:
        text_bytes = lib.read_bytes(text_path)
        text = text_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if len(text.strip()) < MIN_OCR_CHARACTERS:
        return None
    if manifest.get("text_sha256") != _sha256_bytes(text_bytes):
        return None
    return text


def _replace_issue_directory(staging, target):
    """Compatibility wrapper around the shared durable issue-tree swap."""
    return lib.replace_issue_directory(staging, target)


def _load_engine():
    """动态加载 jianshebao-daily 的解析函数（parse_guide_and_pages / find_article_html）。"""
    spec = __import__("importlib").util.spec_from_file_location(
        "cjsb_engine", DEFAULT_ENGINE)
    mod = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verified_daily_artifacts(engine, out, d):
    """Check downloader claims; fetch still validates every image entity."""
    try:
        artifacts = engine.verify_artifacts(out, d)
    except Exception as exc:  # noqa: BLE001
        return None, "当日产物校验失败：%s" % exc
    if not isinstance(artifacts, dict):
        return None, "当日产物元数据无效"
    if artifacts.get("images_complete") is not True:
        return None, "当日产物 images_complete 必须严格为 true"
    expected_day = d.isoformat()
    actual_day = artifacts.get("publish_date")
    if actual_day != expected_day:
        return None, "当日产物日期不匹配：请求=%s，元数据=%r" % (
            expected_day, actual_day
        )
    return artifacts, None


def acquire(source_cfg, d, archive_root, offline_ok=True):
    """确保当日读报文章已采集（本地存在即跳过；否则调用捷报引擎）。"""
    out = os.path.expanduser(source_cfg.get("out", os.path.expanduser("~/Library/Application Support/readdaily/wechat-articles")))
    vault_root = (
        source_cfg.get("_vault_root")
        or os.environ.get("READDAILY_VAULT")
        or os.path.expanduser(
            "~/Library/Application Support/readdaily/vault"
        )
    )
    lib.assert_configured_roots_separate(out, vault_root, label="Vault")
    active_out = lib.current_archive_session(out)
    if active_out is None or active_out.relative_path(out) != ".":
        with lib.archive_session(out, create=True):
            return acquire(source_cfg, d, archive_root, offline_ok=offline_ok)
    lib.assert_session_isolated(active_out, vault_root, label="Vault")
    eng = _load_engine()
    if eng.already_done(out, d):
        _artifacts, error = _verified_daily_artifacts(eng, out, d)
        return ((False, error) if error else
                (True, "本地元数据已核验，版图将在归档前逐张校验"))
    if offline_ok:
        return False, "离线模式且本地无该日文章"
    # The downloader is a separate process and otherwise re-resolves ``out``
    # by pathname. Launch it from the already-open output directory and use a
    # relative output root, so an ancestor/root rename cannot redirect writes.
    launcher = (
        "import importlib.util,os,sys;"
        "os.fchdir(int(sys.argv[1]));"
        "p=sys.argv[2];"
        "s=importlib.util.spec_from_file_location('readdaily_wechat_child',p);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m.DEFAULT_OUT='.';m.DAILY_LOG='./_dailylog.jsonl';"
        "sys.argv=[p]+sys.argv[3:];m.main()"
    )
    active_out.assert_stable()
    p = subprocess.run([
        sys.executable, "-c", launcher, str(active_out._root_fd),
        DEFAULT_ENGINE, "--date", d.isoformat(), "--out", ".",
        "--max-retries", "1", "--retry-gaps", "60", "--no-notify",
        "--no-kb",
    ], pass_fds=(active_out._root_fd,),
       capture_output=True, text=True, timeout=900)
    active_out.assert_stable()
    ok = eng.already_done(out, d)
    if not ok:
        return False, (p.stdout or p.stderr or "")[-300:]
    _artifacts, error = _verified_daily_artifacts(eng, out, d)
    return ((False, error) if error else
            (True, "已采集；版图将在归档前逐张校验"))


def fetch(source_cfg, d, archive_root):
    """归一化：定位本地产物 → 版面/版名 → 复制页面图 → issue.json。"""
    if lib.current_archive_session(archive_root) is None:
        with lib.archive_session(archive_root, create=True):
            return fetch(source_cfg, d, archive_root)
    out = os.path.expanduser(source_cfg.get("out", os.path.expanduser("~/Library/Application Support/readdaily/wechat-articles")))
    active_out = lib.current_archive_session(out)
    if active_out is None or active_out.relative_path(out) != ".":
        with lib.archive_session(out, create=False):
            return fetch(source_cfg, d, archive_root)
    vault_root = (
        source_cfg.get("_vault_root")
        or os.environ.get("READDAILY_VAULT")
        or os.path.expanduser(
            "~/Library/Application Support/readdaily/vault"
        )
    )
    lib.assert_session_isolated(active_out, vault_root, label="Vault")
    eng = _load_engine()
    wacct = os.path.join(out, source_cfg["name"])
    artifacts, artifact_error = _verified_daily_artifacts(eng, out, d)
    if artifact_error:
        return None, artifact_error

    # 1) 原文.html
    html_path = eng.find_article_html(out, d)
    if not html_path:
        return None, "未找到本地文章 HTML"
    expected_html = artifacts.get("html")
    if (not expected_html
            or os.path.basename(os.path.abspath(html_path)) != expected_html):
        return None, "当日 HTML 与元数据不一致：%r" % expected_html
    try:
        rows, page_srcs = eng.parse_guide_and_pages(html_path)
    except ValueError as exc:
        return None, str(exc)
    if not rows or not page_srcs:
        return None, f"导读/版面图解析失败 rows={len(rows)} imgs={len(page_srcs)}"
    if len(rows) != len(page_srcs):
        return None, "导读版次与版面图数量不一致 rows=%s imgs=%s" % (
            len(rows), len(page_srcs)
        )

    aps = lib.archive_paths(archive_root, source_cfg["id"], d)

    # 2) 先校验并缓冲全部版面；任何缺失都不得触碰目标期次目录。
    ep_dirs = sorted(glob.glob(os.path.join(wacct, f"电子报_{d.isoformat()}")),
                     key=os.path.getmtime)
    ep_dir = ep_dirs[-1] if ep_dirs else None
    edition_numbers = []
    for row, src in zip(rows, page_srcs):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return None, "导读版次数据无效：%r" % (row,)
        no, name = row
        if isinstance(no, bool) or not isinstance(no, int) or no < 1:
            return None, "导读版号无效：%r" % no
        if not isinstance(name, str) or not name.strip():
            return None, "第%s版版名无效" % no
        if not isinstance(src, str) or not src.startswith("assets/"):
            return None, "第%s版图片路径无效：%r" % (no, src)
        edition_numbers.append(no)
    if edition_numbers != list(range(1, len(edition_numbers) + 1)):
        return None, "导读版号必须连续唯一且从 1 开始：%s" % edition_numbers

    seen_editions = set()
    buffered_pages = []
    for row, src in zip(rows, page_srcs):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return None, "导读版次数据无效：%r" % (row,)
        no, name = row
        if isinstance(no, bool) or not isinstance(no, int) or no < 1:
            return None, "导读版号无效：%r" % no
        if no in seen_editions:
            return None, "导读存在重复版号：%s" % no
        seen_editions.add(no)
        if not isinstance(name, str) or not name.strip():
            return None, "第%s版版名无效" % no
        if not isinstance(src, str) or not src.startswith("assets/"):
            return None, "第%s版图片路径无效：%r" % (no, src)

        page_src = None
        if ep_dir:
            cand = glob.glob(os.path.join(ep_dir, "高清热图", f"{no}版_*.jpg"))
            if cand:
                page_src = sorted(cand)[-1]
        if not page_src:
            base = os.path.join(wacct, "assets", os.path.basename(src))
            page_src = base if os.path.isfile(base) else None
        if not page_src or not os.path.isfile(page_src):
            return None, "第%s版缺图 %s" % (no, src)
        try:
            with open(page_src, "rb") as stream:
                page_bytes = stream.read()
        except OSError as exc:
            return None, "第%s版图片无法读取：%s" % (no, exc)
        try:
            image_meta = _validated_page_image(page_bytes)
        except ValueError as exc:
            return None, "第%s版图片无效：%s（%s）" % (no, exc, src)
        fname = f"{no:02d}版_{lib.safe_name(name)}.jpg"
        buffered_pages.append({
            "no": no,
            "name": name,
            "filename": fname,
            "source_path": page_src,
            "bytes": page_bytes,
            "image_meta": image_meta,
        })

    editions, units = [], []
    for page in buffered_pages:
        no = page["no"]
        name = page["name"]
        fname = page["filename"]
        image_meta = page["image_meta"]
        editions.append({"no": no, "name": name,
                         "page_image": os.path.join("pages", fname),
                         "page_image_sha256": image_meta["sha256"],
                         "page_image_width": image_meta["width"],
                         "page_image_height": image_meta["height"]})
        units.append({"id": f"{source_cfg['id']}_{d.isoformat().replace('-', '')}_{no:02d}",
                      "type": "edition_ocr", "title": f"{no}版 {name}",
                      "page_image": os.path.join("pages", fname),
                      "page_image_sha256": image_meta["sha256"]})

    # 期号：优先从电子报 PDF 名提取，否则对已缓冲头版的来源文件 OCR。
    issue_no = None
    if ep_dir:
        m = re.search(r"第(\d+)期", " ".join(
            os.path.basename(x) for x in glob.glob(os.path.join(ep_dir, "*_电子报_高清.pdf"))))
        if m:
            issue_no = m.group(1)
    if not issue_no:
        try:
            issue_no = ocr_issue(buffered_pages[0]["source_path"])
        except RuntimeError as exc:
            return None, "头版期号 OCR 失败：%s" % exc
    issue = {
        "source": source_cfg["id"], "source_name": source_cfg["name"],
        "date": d.isoformat(), "issue_no": issue_no,
        "channel": source_cfg["channel"], "editions": editions, "units": units,
        "files": {"article_html": os.path.abspath(html_path),
                  "epaper_dir": os.path.abspath(ep_dir) if ep_dir else None},
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    # 3) 缓冲字节只通过共享的 dirfd 原子整期提交器进入归档。
    source_archive = os.path.dirname(aps["dir"])
    # Directory-chain durability failure is a public operation failure, not a
    # normal source-content miss, so deliberately let it propagate.
    lib.durable_makedirs(source_archive, exist_ok=True)
    try:
        lib.commit_issue_tree(
            aps["dir"],
            [
                (os.path.join("pages", page["filename"]), page["bytes"])
                for page in buffered_pages
            ],
            issue,
        )
    except (OSError, RuntimeError) as exc:
        return None, "归档写入失败：%s" % exc
    return issue, None


def parse(source_cfg, d, archive_root):
    """版面图 → Vision OCR 文本（版级单位）。"""
    if lib.current_archive_session(archive_root) is None:
        with lib.archive_session(archive_root, create=True):
            return parse(source_cfg, d, archive_root)
    d = lib.norm_day(d)
    aps = lib.archive_paths(archive_root, source_cfg["id"], d)
    issue = lib.load_json(aps["issue_json"])
    if not issue:
        return None, "缺 issue.json"
    if issue.get("source") != source_cfg.get("id"):
        return issue, "归档来源与请求来源不一致"
    if issue.get("date") != d.isoformat():
        return issue, "归档日期与请求日期不一致"
    units = issue.get("units")
    if not isinstance(units, list) or not units:
        return issue, "issue.json 缺版次单元清单"

    parsed = copy.deepcopy(issue)
    buffered_text = []
    for unit_index, unit in enumerate(parsed["units"], 1):
        if not isinstance(unit, dict):
            return issue, "第%s版单元格式无效" % unit_index
        unit_id = unit.get("id")
        page_image = unit.get("page_image")
        if not isinstance(unit_id, str) or not unit_id:
            return issue, "第%s版缺单元 ID" % unit_index
        if not isinstance(page_image, str) or not page_image:
            return issue, "第%s版缺版面图路径" % unit_index
        suffix = unit_id.rsplit("_", 1)[-1]
        if not re.fullmatch(r"\d{1,3}", suffix):
            return issue, "第%s版单元 ID 无法生成文本路径" % unit_index

        img = os.path.join(aps["dir"], page_image)
        if not lib.path_is_file(img):
            return issue, "第%s版缺版面图：%s" % (unit_index, page_image)
        try:
            image_bytes = lib.read_bytes(img)
        except OSError as exc:
            return issue, "第%s版图片无法读取：%s" % (unit_index, exc)
        try:
            image_meta = _validated_page_image(image_bytes)
        except ValueError as exc:
            return issue, "第%s版图片无效：%s" % (unit_index, exc)

        filename = "edition_%s.txt" % suffix.zfill(2)
        manifest_filename = "edition_%s.ocr.json" % suffix.zfill(2)
        text_relative = os.path.join("text", filename)
        manifest_relative = os.path.join("text", manifest_filename)
        txt_path = os.path.join(aps["dir"], text_relative)
        manifest_path = os.path.join(aps["dir"], manifest_relative)
        text = _reusable_ocr_text(
            unit,
            source_cfg["id"],
            d.isoformat(),
            text_relative,
            txt_path,
            manifest_path,
            image_meta["sha256"],
        )
        if text is None:
            try:
                descriptor, ocr_input = tempfile.mkstemp(
                    prefix="readdaily-wechat-ocr-", suffix=".jpg"
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        descriptor = -1
                        stream.write(image_bytes)
                    text = ocr_image(ocr_input)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(ocr_input)
                    except FileNotFoundError:
                        pass
            except RuntimeError as exc:
                return issue, "第%s版 OCR 失败：%s" % (unit_index, exc)
        if not isinstance(text, str) or len(text.strip()) < MIN_OCR_CHARACTERS:
            return issue, "第%s版 OCR 文本少于 %s 字符" % (
                unit_index, MIN_OCR_CHARACTERS
            )

        manifest = _ocr_manifest(
            unit,
            source_cfg["id"],
            d.isoformat(),
            text_relative,
            image_meta["sha256"],
            text,
        )
        unit["page_image_sha256"] = image_meta["sha256"]
        unit["text_path"] = text_relative
        unit["ocr_manifest_path"] = manifest_relative
        for edition in parsed.get("editions", []):
            if (isinstance(edition, dict)
                    and edition.get("page_image") == page_image):
                edition["page_image_sha256"] = image_meta["sha256"]
                edition["page_image_width"] = image_meta["width"]
                edition["page_image_height"] = image_meta["height"]
        buffered_text.append({
            "filename": filename,
            "text": text,
            "manifest_filename": manifest_filename,
            "manifest": manifest,
        })

    try:
        files = [
            (relative, payload)
            for relative, payload in lib.read_tree_files(aps["dir"])
            if relative != "issue.json"
            and not relative.startswith("text" + os.sep)
        ]
        for buffered in buffered_text:
            files.append((
                os.path.join("text", buffered["filename"]),
                buffered["text"].encode("utf-8"),
            ))
            files.append((
                os.path.join("text", buffered["manifest_filename"]),
                json.dumps(
                    buffered["manifest"], ensure_ascii=False, indent=1
                ).encode("utf-8"),
            ))
        lib.commit_issue_tree(aps["dir"], files, parsed)
    except (OSError, RuntimeError, shutil.Error) as exc:
        return issue, "OCR 归档写入失败：%s" % exc
    return parsed, None


def ocr_image(img_path):
    if not os.path.isfile(VOCR) or not os.access(VOCR, os.X_OK):
        raise RuntimeError("VOCR 不存在或不可执行：%s" % VOCR)
    if not os.path.isfile(img_path):
        raise RuntimeError("OCR 输入图片不存在：%s" % img_path)
    try:
        p = subprocess.run([VOCR, img_path], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("VOCR 执行超时") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("VOCR 执行异常：%s" % exc) from exc
    if p.returncode != 0:
        detail = re.sub(r"\s+", " ", p.stderr or "").strip()[:200]
        raise RuntimeError(
            "VOCR 非零退出 %s%s" % (
                p.returncode, ("：" + detail) if detail else ""
            )
        )
    text = p.stdout or ""
    if not text.strip():
        raise RuntimeError("VOCR 输出为空")
    return text.strip()


def ocr_issue(img_path):
    text = ocr_image(img_path)
    m = re.search(r"第(\d{3,5})期", text)
    if m:
        return m.group(1)
    return None
