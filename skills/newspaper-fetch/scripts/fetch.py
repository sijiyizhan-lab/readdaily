#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newspaper-fetch 编排器：注册表驱动，有界并发执行各源 fetched→parsed 事务。

用法：
  python3 fetch.py --date 2026-09-02                 # 全部启用源
  python3 fetch.py --date 2026-09-02 --source zgjsb  # 指定源
  python3 fetch.py --date 2026-09-02 --stage parsed  # 只做解析（OCR 等）
  python3 fetch.py --probe gmrb --date 2026-09-02    # 方正渠道模式探测
"""
import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
import contextlib
import datetime
import errno
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib  # noqa: E402

REGISTRY = os.path.expanduser("~/.agents/skills/newspaper-fetch/sources.json")
DEFAULT_VAULT = os.path.expanduser(
    "~/Library/Application Support/readdaily/vault"
)
DEFAULT_WECHAT_OUT = os.path.expanduser(
    "~/Library/Application Support/readdaily/wechat-articles"
)
ALLOWED_CHANNELS = frozenset({
    "cms_index",
    "founder",
    "mobile_epaper",
    "paper_api",
    "pdf_site",
    "wechat_read",
})
_HELD_LOCKS = set()
_HELD_LOCKS_GUARD = threading.Lock()
_SOURCE_LOCKS = {}
_SOURCE_LOCKS_GUARD = threading.Lock()
STATE_SCHEMA_VERSION = 1
PIPELINE_CONTRACT_VERSION = 1
# Bump only the affected channel when its normalized fetch/parse contract
# changes.  Existing timestamps then lose skip eligibility automatically.
ADAPTER_CONTRACT_VERSIONS = {
    "cms_index": 1,
    "founder": 2,
    "mobile_epaper": 1,
    "paper_api": 1,
    "pdf_site": 1,
    "wechat_read": 1,
}
PARSER_CONTRACT_VERSIONS = {
    "cms_index": 1,
    "founder": 2,
    "mobile_epaper": 2,
    "paper_api": 1,
    "pdf_site": 1,
    "wechat_read": 1,
}


class FetchLockedError(RuntimeError):
    pass


class FetchFatalError(RuntimeError):
    """The archive transaction cannot safely continue with another source."""


def _source_print(source, *values):
    """Keep concurrent console lines atomic and attributable to one source."""
    lib.console_print("  [%s]" % source, *values)


def _source_header(source, name, day):
    """Preserve the established human-readable source section marker."""
    lib.console_print("\n=== [%s] %s %s ===" % (source, name, day))


def validate_registry(registry):
    """Validate untrusted registry identity fields before any side effect."""
    if not isinstance(registry, dict):
        raise ValueError("注册表必须是对象")
    if "archive_root" in registry and (
            not isinstance(registry["archive_root"], str)
            or not registry["archive_root"].strip()):
        raise ValueError("注册表 archive_root 必须是非空字符串")

    sources = registry.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("注册表 sources 必须是非空数组")

    seen_ids = set()
    for index, source in enumerate(sources):
        label = "注册表 sources[%s]" % index
        if not isinstance(source, dict):
            raise ValueError("%s 必须是对象" % label)
        try:
            source_id = lib.validate_source_id(source.get("id"))
        except ValueError as exc:
            raise ValueError("%s.id 无效：%s" % (label, exc)) from exc
        if source_id in seen_ids:
            raise ValueError("注册表 source id 重复：%s" % source_id)
        seen_ids.add(source_id)

        name = source.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("%s.name 必须是非空字符串" % label)
        channel = source.get("channel")
        if not isinstance(channel, str) or channel not in ALLOWED_CHANNELS:
            raise ValueError(
                "%s.channel 不在允许列表：%s" % (label, channel)
            )
        if "enabled" in source and not isinstance(source["enabled"], bool):
            raise ValueError("%s.enabled 必须是布尔值" % label)
        if "out" in source and (
                not isinstance(source["out"], str) or not source["out"].strip()):
            raise ValueError("%s.out 必须是非空字符串" % label)
    return registry


def load_registry(path=REGISTRY):
    reg = lib.load_json(path)
    if not reg:
        sys.exit("注册表缺失")
    try:
        return validate_registry(reg)
    except ValueError as exc:
        sys.exit(str(exc))


def resolve_archive_root(registry):
    """Use the caller-selected archive before the registry's installation default."""
    configured = os.environ.get("READDAILY_ARCHIVE") or registry.get(
        "archive_root", "~/Library/Application Support/readdaily/news-archive"
    )
    return os.path.expanduser(configured)


def resolve_vault_root(configured=None):
    return os.path.expanduser(
        configured or os.environ.get("READDAILY_VAULT") or DEFAULT_VAULT
    )


def _paths_overlap(first, second):
    first = os.path.realpath(os.path.abspath(os.path.expanduser(str(first))))
    second = os.path.realpath(os.path.abspath(os.path.expanduser(str(second))))
    try:
        common = os.path.commonpath([first, second])
    except ValueError as exc:
        raise ValueError("归档目录与 Vault 路径无法安全比较") from exc
    return common in (first, second)


def validate_archive_outside_vault(archive_root, vault_root=None):
    """Reject overlapping trees and archive symlinks that can reach the Vault."""
    archive = os.path.abspath(os.path.expanduser(str(archive_root)))
    vault = os.path.abspath(os.path.expanduser(str(resolve_vault_root(vault_root))))
    if _paths_overlap(archive, vault):
        raise ValueError("归档目录必须与 Vault 完全分离，不能互为父子目录或路径别名")

    if os.path.isdir(archive):
        for current, directories, files in os.walk(archive, followlinks=False):
            for name in directories + files:
                candidate = os.path.join(current, name)
                if os.path.islink(candidate) and _paths_overlap(
                        os.path.realpath(candidate), vault):
                    raise ValueError("归档目录包含指向 Vault 的符号链接：%s" % candidate)
    return archive


def _validate_write_root_outside_vault(write_root, vault_root, label):
    if write_root is None or not str(write_root).strip():
        raise ValueError("%s 写入根不能为空" % label)
    configured = lib._absolute_path(write_root)
    vault = os.path.abspath(os.path.expanduser(str(resolve_vault_root(vault_root))))
    if _paths_overlap(configured, vault):
        raise ValueError(
            "%s 必须与 Vault 完全分离，不能互为父子目录或路径别名" % label
        )

    # A safe-looking root can still route a predictable adapter subdirectory
    # into the Vault.  Inspect existing links before any adapter is loaded.
    if os.path.isdir(configured):
        for current, directories, files in os.walk(configured, followlinks=False):
            for name in directories + files:
                candidate = os.path.join(current, name)
                if os.path.islink(candidate) and _paths_overlap(
                        os.path.realpath(candidate), vault):
                    raise ValueError(
                        "%s 包含与 Vault 重叠的符号链接：%s" % (
                            label, candidate
                        )
                    )
    return configured


def validate_registry_write_roots_outside_vault(registry, vault_root=None):
    """Validate and canonicalize adapter write roots declared by the registry.

    ``out`` is currently the only source-level external write root in the
    adapter contract.  The wechat adapter also has a default when that field is
    omitted, so it must be validated and pinned to its canonical path too.
    Every configured source is checked, including disabled/unselected entries,
    before the first adapter can run.
    """
    sources = registry.get("sources") if isinstance(registry, dict) else None
    if not isinstance(sources, list):
        raise ValueError("注册表 sources 必须是数组")
    validated = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("注册表 source 必须是对象")
        has_explicit_out = "out" in source
        if not has_explicit_out and source.get("channel") != "wechat_read":
            continue
        source_id = str(source.get("id") or "unknown")
        roots = [(
            "out",
            source.get("out") if has_explicit_out else DEFAULT_WECHAT_OUT,
        )]
        if source.get("channel") == "wechat_read":
            # The current engine keeps its attempt log under DEFAULT_WECHAT_OUT
            # even when its caller configures a custom artifact root.
            roots.append(("wechat_engine_default", DEFAULT_WECHAT_OUT))

        canonical_out = None
        seen = set()
        for field, raw_root in roots:
            label = "%s.%s 外部写入根" % (source_id, field)
            canonical = _validate_write_root_outside_vault(
                raw_root, vault_root, label
            )
            if field == "out":
                canonical_out = canonical
            if canonical in seen:
                continue
            seen.add(canonical)
            validated.append({
                "source": source_id,
                "field": field,
                "path": canonical,
            })
        # Pin the adapter to the path that was actually checked, avoiding a
        # second expansion through a configured symlink after validation.
        source["out"] = canonical_out
        source["_vault_root"] = lib._absolute_path(
            resolve_vault_root(vault_root)
        )
    return validated


@contextlib.contextmanager
def fetch_date_lock(archive_root, day):
    """Prevent two complete fetch batches for one archive/date.

    Source evidence has a separate, finer-grained lock below.  Keeping this
    coordinator lock for the full batch preserves duplicate-run protection
    while allowing the workbench to read a source that another worker is not
    currently replacing.
    """
    normalized_day = lib.norm_day(day).isoformat()
    archive_identity = lib.archive_lock_identity(archive_root)
    archive_key = hashlib.sha256(archive_identity.encode("utf-8")).hexdigest()
    lock_directory = os.path.join(
        lib._absolute_path(lib.FETCH_BATCH_LOCK_ROOT), archive_key
    )
    lock_path = os.path.join(lock_directory, normalized_day + ".lock")
    held_key = (archive_key, normalized_day)
    with lib.pinned_user_lock_directory(
            lock_directory, "抓取批次锁目录") as (
                lock_directory_fd, pinned_lock_directory):
        if pinned_lock_directory != lock_directory:
            raise lib.ArchivePathSafetyError("抓取批次锁目录身份不一致")
        try:
            descriptor = lib.open_lock_file_at(
                lock_directory_fd, os.path.basename(lock_path), 0o600,
            )
        except OSError as exc:
            raise lib.ArchivePathSafetyError(
                "抓取批次锁无法安全打开"
            ) from exc
        acquired = False
        try:
            try:
                lock_info = os.fstat(descriptor)
            except OSError as exc:
                raise lib.ArchivePathSafetyError(
                    "抓取批次锁无法安全校验"
                ) from exc
            if (not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_uid != os.geteuid()
                    or lock_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise lib.ArchivePathSafetyError(
                    "抓取批次锁必须是当前用户持有的可信普通文件"
                )
            with _HELD_LOCKS_GUARD:
                if held_key in _HELD_LOCKS:
                    raise FetchLockedError(
                        "同一归档与日期已有抓取任务运行：%s"
                        % normalized_day
                    )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FetchLockedError(
                    "同一归档与日期已有抓取任务运行：%s" % normalized_day
                ) from exc
            except OSError as exc:
                if exc.errno in (
                        errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise FetchLockedError(
                        "同一归档与日期已有抓取任务运行：%s"
                        % normalized_day
                    ) from exc
                raise lib.ArchivePathSafetyError(
                    "抓取批次锁操作失败"
                ) from exc
            with _HELD_LOCKS_GUARD:
                if held_key in _HELD_LOCKS:
                    raise FetchLockedError(
                        "同一归档与日期已有抓取任务运行：%s"
                        % normalized_day
                    )
                _HELD_LOCKS.add(held_key)
            acquired = True
            try:
                os.ftruncate(descriptor, 0)
                os.write(
                    descriptor,
                    ("pid=%s\narchive=%s\ndate=%s\n" % (
                        os.getpid(), archive_identity, normalized_day
                    )).encode("utf-8"),
                )
                os.fsync(descriptor)
            except OSError as exc:
                raise lib.ArchivePathSafetyError(
                    "抓取批次锁状态写入失败"
                ) from exc
            yield lock_path
        finally:
            if acquired:
                with _HELD_LOCKS_GUARD:
                    _HELD_LOCKS.discard(held_key)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _source_evidence_lock_path(archive_root, source, day):
    """Return the canonical cross-process source/date lock identity."""
    normalized_source = lib.validate_source_id(source)
    normalized_day = lib.norm_day(day).isoformat()
    archive_identity = lib.archive_lock_identity(archive_root)
    lock_key = hashlib.sha256((
        archive_identity + "\0source-evidence\0"
        + normalized_source + "\0" + normalized_day
    ).encode("utf-8")).hexdigest()
    lock_directory = lib._absolute_path(lib.SOURCE_EVIDENCE_LOCK_ROOT)
    return (
        os.path.join(lock_directory, lock_key + ".lock"),
        lock_key,
        archive_identity,
        normalized_source,
        normalized_day,
    )


@contextlib.contextmanager
def fetch_source_evidence_lock(archive_root, source, day):
    """Serialize mutation of one source/date evidence tree.

    The lock identity and acquisition order are shared with local-PDF import,
    workbench reads/drafts, and publishing.  Fetch ordering is always
    coordinator -> source evidence; downstream operations never acquire the
    coordinator, so no inverse order exists.
    """
    lock_path, lock_key, archive_identity, source, normalized_day = (
        _source_evidence_lock_path(archive_root, source, day)
    )
    with _SOURCE_LOCKS_GUARD:
        thread_lock = _SOURCE_LOCKS.setdefault(lock_key, threading.Lock())
    with thread_lock:
        with lib.source_evidence_lock_directory() as (
                lock_directory_fd, pinned_lock_directory):
            if pinned_lock_directory != os.path.dirname(lock_path):
                raise lib.ArchivePathSafetyError(
                    "报纸来源证据锁目录身份不一致"
                )
            try:
                descriptor = lib.open_lock_file_at(
                    lock_directory_fd, os.path.basename(lock_path), 0o600,
                )
            except OSError as exc:
                raise lib.ArchivePathSafetyError(
                    "报纸来源证据锁无法安全打开"
                ) from exc
            locked = False
            try:
                try:
                    lock_info = os.fstat(descriptor)
                    if (not stat.S_ISREG(lock_info.st_mode)
                            or lock_info.st_uid != os.geteuid()
                            or lock_info.st_mode
                            & (stat.S_IWGRP | stat.S_IWOTH)):
                        raise lib.ArchivePathSafetyError(
                            "报纸来源证据锁必须是当前用户持有的可信普通文件"
                        )
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    locked = True
                    os.ftruncate(descriptor, 0)
                    os.write(descriptor, (
                        "pid=%s\narchive=%s\nsource=%s\ndate=%s\n" % (
                            os.getpid(), archive_identity, source,
                            normalized_day,
                        )
                    ).encode("utf-8"))
                    os.fsync(descriptor)
                except OSError as exc:
                    raise lib.ArchivePathSafetyError(
                        "报纸来源证据锁操作失败"
                    ) from exc
                yield lock_path
            finally:
                if locked:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(descriptor)


def load_adapter(channel):
    return importlib.import_module(f"adapters.{channel}")


def _stage_contract_version(src, stage):
    """Return the explicit adapter/parser contract persisted in state."""
    channel = str(src.get("channel") or "")
    if stage == "fetched":
        versions, label = ADAPTER_CONTRACT_VERSIONS, "fetch"
    elif stage == "parsed":
        versions, label = PARSER_CONTRACT_VERSIONS, "parse"
    else:
        raise ValueError("未知流水线阶段：%s" % stage)
    if channel not in versions:
        raise ValueError("渠道缺少%s契约版本：%s" % (label, channel))
    return "%s:%s:v%s" % (channel, label, versions[channel])


def _issue_evidence_value(value):
    """Remove downstream editorial annotations from acquisition evidence."""
    if isinstance(value, dict):
        return {
            key: _issue_evidence_value(child)
            for key, child in value.items()
            if key != "summary"
        }
    if isinstance(value, list):
        return [_issue_evidence_value(child) for child in value]
    return value


def _issue_evidence_digest(issue_dir, source_id, day):
    """Hash the current issue manifest and every archived evidence file.

    A timestamp alone cannot prove that the issue still exists or still
    contains the bytes that were validated.  The digest is path-delimited and
    covers the complete issue tree; symlinks, malformed metadata, and files
    changing while being read make the evidence unverifiable.
    """
    day = lib.norm_day(day)
    try:
        snapshot = dict(lib.read_tree_files(issue_dir))
        issue = json.loads(snapshot["issue.json"].decode("utf-8"))
    except (lib.ArchivePathSafetyError, lib.ArchiveConflictError):
        raise
    except (OSError, UnicodeError, ValueError):
        return None
    except (KeyError, json.JSONDecodeError):
        return None
    if (not isinstance(issue, dict)
            or issue.get("source") != source_id
            or issue.get("date") != day.isoformat()):
        return None

    digest = hashlib.sha256()
    digest.update(b"readdaily-issue-evidence-v1\0")
    for relative, payload in sorted(snapshot.items()):
        relative_bytes = relative.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        if relative == "issue.json":
            # Summary annotations do not alter acquired evidence identity.
            payload = json.dumps(
                _issue_evidence_value(issue),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _stage_skip_status(
        state, stage, src, day, aps, evidence_digest_getter=None):
    """Return whether a completed marker is bound to current code/evidence."""
    if not isinstance(state, dict):
        return False, "无状态记录"
    stages = state.get("stages")
    if not isinstance(stages, dict) or not stages.get(stage):
        return False, "%s 未完成" % stage
    if state.get("state_schema_version") != STATE_SCHEMA_VERSION:
        return False, "状态模式版本缺失或不匹配"
    if state.get("pipeline_contract_version") != PIPELINE_CONTRACT_VERSION:
        return False, "流水线契约版本缺失或不匹配"
    if state.get("adapter_contract_version") != _stage_contract_version(
            src, "fetched"):
        return False, "fetched 契约版本缺失或不匹配"
    if (stage == "parsed"
            and state.get("parser_contract_version")
            != _stage_contract_version(src, "parsed")):
        return False, "parsed 契约版本缺失或不匹配"
    current_digest = (
        evidence_digest_getter()
        if evidence_digest_getter is not None
        else _issue_evidence_digest(aps["dir"], src["id"], day)
    )
    if not current_digest:
        return False, "当前 issue 缺失或证据不可验证"
    if state.get("issue_evidence_sha256") != current_digest:
        return False, "当前 issue 证据摘要与状态不匹配"
    return True, "版本与 issue 证据摘要一致"


def _mark_versioned_stage(aps, src, day, stage, **extra):
    """Persist a stage marker together with code and issue evidence identity."""
    evidence = _issue_evidence_digest(aps["dir"], src["id"], day)
    if not evidence:
        raise RuntimeError("无法为当前 issue 生成可信证据摘要")
    state = lib.load_json(aps["state"], {}) or {}
    if not isinstance(state, dict):
        state = {}
    stages = state.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    else:
        stages = dict(stages)
    stages.pop("failed", None)
    stages[stage] = datetime.datetime.now().isoformat(timespec="seconds")
    state["stages"] = stages
    state["state_schema_version"] = STATE_SCHEMA_VERSION
    state["pipeline_contract_version"] = PIPELINE_CONTRACT_VERSION
    state["issue_evidence_sha256"] = evidence
    if stage == "fetched":
        state["adapter_contract_version"] = _stage_contract_version(
            src, "fetched"
        )
        # A fresh fetch replaces the issue tree, so every prior parse marker
        # and parser contract belongs to different bytes.
        stages.pop("parsed", None)
        stages.pop("summarized", None)
        stages.pop("published", None)
        stages.pop("archived", None)
        state.pop("parser_contract_version", None)
        state.pop("units", None)
        state.pop("publish_plan_id", None)
        state.pop("publish_transaction_id", None)
        state.pop("publish_archive_evidence_sha256", None)
        state.pop("publish_draft_sha256", None)
    elif stage == "parsed":
        state["parser_contract_version"] = _stage_contract_version(
            src, "parsed"
        )
    else:
        raise ValueError("未知流水线阶段：%s" % stage)
    state.update(extra)
    lib.save_json(aps["state"], state)
    return state


def _record_source_failure(root, daily_log, src, day, note):
    sid = src.get("id") or "unknown"
    message = str(note or "未知错误")
    aps = lib.archive_paths(root, sid, day)
    try:
        lib.state_mark(aps["state"], "failed", note=message)
    except lib.PIPELINE_FATAL_EXCEPTIONS:
        raise
    except Exception as exc:  # noqa: BLE001
        _source_print(sid, "failed 状态保存失败:", exc)
    try:
        lib.log_line(daily_log, {
            "source": sid,
            "source_name": src.get("name") or sid,
            "date": day.isoformat(),
            "stage": "failed",
            "note": message,
        })
    except lib.PIPELINE_FATAL_EXCEPTIONS:
        raise
    except Exception as exc:  # noqa: BLE001
        _source_print(sid, "failed 日志保存失败:", exc)


class _IssueRefreshTransaction:
    """Keep the last complete issue usable until fetch *and* parse succeed.

    Adapters commit each stage atomically, but a successful fetch intentionally
    replaces the old issue tree before parse starts.  When both stages were
    requested as one user action, preserve the previous complete tree and its
    state so a downstream parse failure cannot downgrade a readable archive to
    a shallow fetch result.
    """

    def __init__(self, issue_dir, state_path):
        self.issue_dir = os.path.abspath(str(issue_dir))
        self.state_path = os.path.abspath(str(state_path))
        self.parent = os.path.dirname(self.issue_dir)
        lib.durable_makedirs(self.parent, exist_ok=True)
        self.snapshot_root = lib.durable_mkdtemp(
            self.parent,
            prefix=".%s.pipeline." % os.path.basename(self.issue_dir),
        )
        # The snapshot itself must remain a sibling of the live issue so the
        # atomic directory swap helper can restore it without crossing a
        # filesystem boundary.
        self.snapshot_issue = self.snapshot_root
        self.had_issue = lib.path_exists(self.issue_dir)
        self.had_state = lib.path_exists(self.state_path)
        self.state_bytes = None
        self.finished = False
        self.rollback_attempted = False
        self.commit_attempted = False
        try:
            if self.had_issue:
                self._validate_tree(self.issue_dir)
                lib.copy_directory_tree(self.issue_dir, self.snapshot_issue)
                lib.fsync_tree(self.snapshot_issue)
                lib.fsync_directory(self.parent)
            if self.had_state:
                if not lib.path_is_file(self.state_path):
                    raise ValueError("流水线状态必须是普通文件")
                self.state_bytes = lib.read_bytes(self.state_path)
        except BaseException as primary_error:
            try:
                if lib.path_exists(self.snapshot_root):
                    lib.durable_rmtree(self.snapshot_root)
            except BaseException as cleanup_error:
                failure = FetchFatalError(
                    "流水线快照初始化失败，且暂存目录未完成耐久清理：%s"
                    % self.snapshot_root
                )
                failure.primary_error = primary_error
                failure.cleanup_error = cleanup_error
                raise failure from primary_error
            raise

    @staticmethod
    def _validate_tree(root):
        if not lib.path_is_dir(root):
            raise ValueError("期次目录必须是真实目录")
        lib.read_tree_files(root)

    @staticmethod
    def _restore_bytes(path, raw):
        lib.durable_atomic_write_bytes(path, raw)

    def rollback(self):
        if self.finished:
            return
        if self.rollback_attempted:
            raise FetchFatalError("流水线耐久回滚此前未完成，拒绝重复宣称完成")
        if self.commit_attempted:
            raise FetchFatalError("流水线已进入提交收尾，不能再执行回滚")
        self.rollback_attempted = True
        try:
            if self.had_issue:
                if not lib.path_is_dir(self.snapshot_issue):
                    raise FetchFatalError("旧期次快照缺失，无法恢复")
                lib.replace_issue_directory(self.snapshot_issue, self.issue_dir)
                self.snapshot_root = None
            elif lib.path_exists(self.issue_dir):
                if not lib.path_is_dir(self.issue_dir):
                    raise FetchFatalError("新期次路径类型异常，拒绝自动删除")
                lib.durable_rmtree(self.issue_dir)

            if self.had_state:
                self._restore_bytes(self.state_path, self.state_bytes)
            elif lib.path_exists(self.state_path):
                if not lib.path_is_file(self.state_path):
                    raise FetchFatalError("新状态路径类型异常，拒绝自动删除")
                lib.durable_unlink(self.state_path)
            if self.snapshot_root:
                lib.durable_rmtree(self.snapshot_root, missing_ok=True)
                self.snapshot_root = None
        except BaseException as exc:
            raise FetchFatalError(
                "流水线耐久回滚未完成；请检查 issue=%s、state=%s、snapshot=%s"
                % (self.issue_dir, self.state_path, self.snapshot_root or "无")
            ) from exc
        self.finished = True

    def commit(self):
        if self.finished:
            return
        if self.commit_attempted:
            raise FetchFatalError("流水线提交收尾此前未完成")
        if self.rollback_attempted:
            raise FetchFatalError("流水线已经回滚，不能再提交")
        self.commit_attempted = True
        try:
            if self.snapshot_root:
                lib.durable_rmtree(self.snapshot_root, missing_ok=True)
                self.snapshot_root = None
        except BaseException as exc:
            raise FetchFatalError(
                "新期次已完成，但事务快照未完成耐久清理：%s"
                % (self.snapshot_root or "无")
            ) from exc
        self.finished = True


def _run_atomic_refresh(src, day, root, args, daily_log, ad, acquire, aps):
    """Run a fresh fetched→parsed chain as one archive-level transaction."""
    sid = src["id"]
    transaction = _IssueRefreshTransaction(aps["dir"], aps["state"])
    try:
        if callable(acquire):
            ok, note = acquire(
                src, day, root, offline_ok=args.offline
            ) if src["channel"] == "wechat_read" else acquire(src, day, root)
            _source_print(sid, "acquire:", note)
            if not ok:
                transaction.rollback()
                _source_print(sid, "acquire 失败:", note)
                _record_source_failure(root, daily_log, src, day, note)
                return "failed"

        issue, err = ad.fetch(src, day, root)
        if err:
            transaction.rollback()
            _source_print(sid, "fetch 失败:", err)
            _record_source_failure(root, daily_log, src, day, err)
            return "failed"
        if not isinstance(issue, dict):
            raise RuntimeError("fetch 未返回有效 issue")
        ok, chain = lib.chain_check(root, sid, day, issue.get("issue_no"))
        _source_print(sid, "fetch ok;", "👍" if ok else "⚠️", chain)
        _mark_versioned_stage(
            aps, src, day, "fetched",
            edition_no=len(issue.get("editions", [])),
        )
        lib.log_line(daily_log, {
            "source": sid, "source_name": src["name"], "date": day.isoformat(),
            "stage": "fetched", "editions": len(issue.get("editions", [])),
            "issue_no": issue.get("issue_no"), "chain_ok": ok,
            "issue_json": aps["issue_json"],
        })

        issue, err = ad.parse(src, day, root)
        if err:
            transaction.rollback()
            _source_print(sid, "parse 失败:", err)
            _record_source_failure(root, daily_log, src, day, err)
            return "failed"
        if not isinstance(issue, dict):
            raise RuntimeError("parse 未返回有效 issue")
        units = len(issue.get("units", []))
        _mark_versioned_stage(aps, src, day, "parsed", units=units)
        lib.log_line(daily_log, {
            "source": sid, "source_name": src["name"], "date": day.isoformat(),
            "stage": "parsed", "units": units,
            "issue_json": aps["issue_json"],
        })
        transaction.commit()
        _source_print(sid, "parsed ok:", units, "units")
        return "ok"
    except BaseException:
        if not transaction.rollback_attempted and not transaction.commit_attempted:
            transaction.rollback()
        raise


def _run_source_in_archive_session(src, day, root, args, daily_log):
    sid = src["id"]
    aps = lib.archive_paths(root, sid, day)
    ad = load_adapter(src["channel"])
    _source_header(sid, src["name"], day)
    acquire = getattr(ad, "acquire", None)

    stages = set(args.stage.split(","))
    requested_stages = [
        stage for stage in ("fetched", "parsed") if stage in stages
    ]
    skipped_stages = 0
    cached_evidence = []

    def current_evidence_digest():
        # A completed fetched→parsed skip path never mutates the issue tree.
        # Reuse its one strong digest across both marker checks instead of
        # reading every page twice on routine daily reruns.
        if not cached_evidence:
            cached_evidence.append(
                _issue_evidence_digest(aps["dir"], sid, day)
            )
        return cached_evidence[0]

    if "fetched" in stages:
        state = lib.load_json(aps["state"])
        can_skip, skip_note = _stage_skip_status(
            state, "fetched", src, day, aps,
            evidence_digest_getter=current_evidence_digest,
        )
        if can_skip and not args.no_state_skip:
            _source_print(sid, "fetched 已完成且%s，跳过" % skip_note)
            skipped_stages += 1
        else:
            if not args.no_state_skip:
                _source_print(
                    sid, "fetched 状态不可复用：%s，强制重跑" % skip_note
                )
            if "parsed" in stages:
                return _run_atomic_refresh(
                    src, day, root, args, daily_log, ad, acquire, aps
                )
            if callable(acquire):
                ok, note = acquire(
                    src, day, root, offline_ok=args.offline
                ) if src["channel"] == "wechat_read" else acquire(src, day, root)
                _source_print(sid, "acquire:", note)
                if not ok:
                    _source_print(sid, "acquire 失败:", note)
                    _record_source_failure(root, daily_log, src, day, note)
                    return "failed"
            issue, err = ad.fetch(src, day, root)
            if err:
                _source_print(sid, "fetch 失败:", err)
                _record_source_failure(root, daily_log, src, day, err)
                return "failed"
            if not isinstance(issue, dict):
                raise RuntimeError("fetch 未返回有效 issue")
            ok, chain = lib.chain_check(root, sid, day, issue.get("issue_no"))
            _source_print(sid, "fetch ok;", "👍" if ok else "⚠️", chain)
            _mark_versioned_stage(
                aps, src, day, "fetched",
                edition_no=len(issue.get("editions", [])),
            )
            lib.log_line(daily_log, {
                "source": sid, "source_name": src["name"], "date": day.isoformat(),
                "stage": "fetched", "editions": len(issue.get("editions", [])),
                "issue_no": issue.get("issue_no"), "chain_ok": ok,
                "issue_json": aps["issue_json"],
            })
    if "parsed" in stages:
        state = lib.load_json(aps["state"])
        can_skip, skip_note = _stage_skip_status(
            state, "parsed", src, day, aps,
            evidence_digest_getter=current_evidence_digest,
        )
        if can_skip and not args.no_state_skip:
            _source_print(sid, "parsed 已完成且%s，跳过" % skip_note)
            skipped_stages += 1
            return (
                "skipped"
                if skipped_stages == len(requested_stages)
                else "ok"
            )
        if not args.no_state_skip:
            _source_print(
                sid, "parsed 状态不可复用：%s，强制重跑" % skip_note
            )
        issue, err = ad.parse(src, day, root)
        if err:
            _source_print(sid, "parse 失败:", err)
            _record_source_failure(root, daily_log, src, day, err)
            return "failed"
        if not isinstance(issue, dict):
            raise RuntimeError("parse 未返回有效 issue")
        n = len(issue.get("units", []))
        _mark_versioned_stage(aps, src, day, "parsed", units=n)
        lib.log_line(daily_log, {
            "source": sid, "source_name": src["name"], "date": day.isoformat(),
            "stage": "parsed", "units": n, "issue_json": aps["issue_json"],
        })
        _source_print(sid, "parsed ok:", n, "units")
    if requested_stages and skipped_stages == len(requested_stages):
        return "skipped"
    return "ok"


def _run_source(src, day, root, args, daily_log):
    """Run one source while the canonical archive identity stays pinned."""
    active = lib.current_archive_session(root)
    if active is not None:
        return _run_source_in_archive_session(
            src, day, root, args, daily_log
        )
    with lib.archive_session(root, create=True):
        return _run_source_in_archive_session(
            src, day, root, args, daily_log
        )


_FATAL_SOURCE_EXCEPTIONS = (
    FetchFatalError,
) + lib.PIPELINE_FATAL_EXCEPTIONS


def _run_source_task(
        src, day, root, args, daily_log, coordinator_archive=None):
    """Run and account for one isolated source while its evidence is locked."""
    session_context = (
        lib.fork_archive_session(coordinator_archive)
        if coordinator_archive is not None
        else contextlib.nullcontext()
    )
    # Bind the exact descriptor pinned by main before computing the source lock
    # or touching evidence.  A pathname reopened here could have been replaced
    # with another valid directory after the coordinator's isolation check.
    try:
        with session_context:
            with fetch_source_evidence_lock(root, src["id"], day):
                try:
                    return _run_source(src, day, root, args, daily_log)
                except _FATAL_SOURCE_EXCEPTIONS:
                    # Archive/process safety failures abort the batch; they are
                    # never downgraded to a routine upstream-source failure.
                    raise
                except Exception as exc:  # noqa: BLE001
                    _source_print(
                        src.get("id", "unknown"), "来源执行异常:", exc
                    )
                    _record_source_failure(root, daily_log, src, day, exc)
                    return "failed"
    finally:
        lib.close_http_client()


def run_sources(
        sources, day, root, args, daily_log, workers=4,
        coordinator_archive=None):
    """Run independent source pipelines with bounded concurrency.

    One submitted task owns a source's complete fetch→parse transaction, so
    stages never overlap within a source.  Exceptions from ordinary upstream
    failures are isolated by ``_run_source_task``; archive safety conflicts
    abort the batch after already-running workers unwind safely.
    """
    sources = list(sources)
    if not sources:
        return {"ok": 0, "failed": 0, "skipped": 0}
    if not isinstance(workers, int) or isinstance(workers, bool):
        raise ValueError("workers 必须是整数")
    if not 1 <= workers <= 8:
        raise ValueError("workers 必须在 1 到 8 之间")
    if coordinator_archive is None:
        # Public callers receive the same root-identity guarantee as main:
        # create/pin once, then fd-clone that exact directory into each worker.
        with lib.archive_session(root, create=True) as archive_handle:
            return run_sources(
                sources, day, archive_handle.canonical_root, args, daily_log,
                workers=workers, coordinator_archive=archive_handle,
            )

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    fatal_error = None
    executor = None
    futures = {}
    try:
        executor = ThreadPoolExecutor(
            max_workers=min(workers, len(sources)),
            thread_name_prefix="readdaily-source",
        )
        for src in sources:
            task_args = (src, day, root, args, daily_log)
            if coordinator_archive is not None:
                task_args += (coordinator_archive,)
            futures[executor.submit(_run_source_task, *task_args)] = src
        for future in as_completed(futures):
            try:
                outcome = future.result()
            except CancelledError:
                continue
            except _FATAL_SOURCE_EXCEPTIONS as exc:
                if fatal_error is None:
                    fatal_error = exc
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                continue
            counts[outcome] += 1
    except BaseException:
        # Ctrl-C, executor submission failures, and unexpected collection
        # errors must not allow queued sources to start afterward.  Running
        # source transactions still unwind before control returns to caller.
        for pending in futures:
            pending.cancel()
        raise
    finally:
        # Running adapter transactions cannot be interrupted without risking
        # partial external work.  Pending tasks are cancelled on a fatal
        # archive error; running ones are allowed to reach their atomic unwind.
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    if fatal_error is not None:
        raise fatal_error
    return counts


def main():
    ap = argparse.ArgumentParser(description="newspaper-fetch 编排器")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--source", default=None, help="指定源 id（可逗号）")
    ap.add_argument("--stage", default="fetched,parsed",
                    help="fetched,parsed（可逗号组合）")
    ap.add_argument("--offline", action="store_true", help="微信渠道离线回退（不搜索）")
    ap.add_argument("--probe", default=None, help="方正/PDF 渠道模式探测（给 source id）")
    ap.add_argument("--registry", default=REGISTRY)
    ap.add_argument("--vault", default=None, help="Obsidian Vault，用于写入边界校验")
    ap.add_argument("--no-state-skip", action="store_true", help="忽略状态机直接重跑")
    ap.add_argument(
        "--workers", type=int, choices=range(1, 9), default=4,
        metavar="N", help="跨报纸并发数（1-8，默认 4；单报纸内仍串行）",
    )
    args = ap.parse_args()

    reg = load_registry(args.registry)
    try:
        # Keep this explicit even though load_registry validates: tests and
        # embedders may inject an already-loaded registry object.
        validate_registry(reg)
        root = resolve_archive_root(reg)
        root = validate_archive_outside_vault(root, args.vault)
        validate_registry_write_roots_outside_vault(reg, args.vault)
        requested_stages = [
            value.strip() for value in str(args.stage).split(",")
        ]
        invalid_stages = [
            value for value in requested_stages
            if value not in ("fetched", "parsed")
        ]
        if not requested_stages or invalid_stages:
            raise ValueError(
                "--stage 仅支持 fetched、parsed，且不能为空：%s"
                % (", ".join(invalid_stages) or "空值")
            )
        args.stage = ",".join(requested_stages)

        requested_ids = None
        if args.source is not None:
            requested_ids = [
                value.strip() for value in str(args.source).split(",")
            ]
            if not requested_ids or any(not value for value in requested_ids):
                raise ValueError("--source 不能为空或包含空来源 id")
            for source_id in requested_ids:
                lib.validate_source_id(source_id)
            known_ids = {source["id"] for source in reg["sources"]}
            unknown_ids = sorted(set(requested_ids) - known_ids)
            if unknown_ids:
                raise ValueError("未知来源 id：%s" % ", ".join(unknown_ids))
        want = [
            source for source in reg["sources"]
            if (
                source["id"] in requested_ids
                if requested_ids is not None
                else source.get("enabled")
            )
        ]
    except ValueError as exc:
        sys.exit(str(exc))
    d = lib.norm_day(args.date or datetime.date.today())

    if args.probe:
        src = next((s for s in reg["sources"] if s["id"] == args.probe), None)
        if not src:
            sys.exit("无此源")
        ad = load_adapter(src["channel"])
        res = ad.probe(src, d)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return

    try:
        # Open the user-configured path itself without following links, then
        # repeat Vault isolation against that pinned identity. This closes the
        # validation -> open race and the same session spans the full batch.
        with lib.archive_session(root, create=True) as archive_handle:
            lib.assert_session_isolated(
                archive_handle, resolve_vault_root(args.vault), label="Vault"
            )
            root = archive_handle.canonical_root
            daily_log = os.path.join(root, "_dailylog.jsonl")
            with fetch_date_lock(root, d):
                counts = run_sources(
                    want, d, root, args, daily_log, workers=args.workers,
                    coordinator_archive=archive_handle,
                )
    except (FetchLockedError, FetchFatalError, lib.ArchivePathSafetyError,
            lib.ArchiveConflictError, lib.ArchiveTransactionError) as exc:
        sys.exit(str(exc))

    print("\n完成：成功 %s，失败 %s，跳过 %s" % (
        counts["ok"], counts["failed"], counts["skipped"]
    ))


if __name__ == "__main__":
    main()
