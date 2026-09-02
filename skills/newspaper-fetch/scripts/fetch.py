#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newspaper-fetch 编排器：注册表驱动，逐源逐期执行 fetched→parsed 两段状态。

用法：
  python3 fetch.py --date 2026-09-02                 # 全部启用源
  python3 fetch.py --date 2026-09-02 --source zgjsb  # 指定源
  python3 fetch.py --date 2026-09-02 --stage parsed  # 只做解析（OCR 等）
  python3 fetch.py --probe gmrb --date 2026-09-02    # 方正渠道模式探测
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
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
STATE_SCHEMA_VERSION = 1
PIPELINE_CONTRACT_VERSION = 1
# Bump only the affected channel when its normalized fetch/parse contract
# changes.  Existing timestamps then lose skip eligibility automatically.
ADAPTER_CONTRACT_VERSIONS = {
    "cms_index": 1,
    "founder": 1,
    "mobile_epaper": 1,
    "paper_api": 1,
    "pdf_site": 1,
    "wechat_read": 1,
}
PARSER_CONTRACT_VERSIONS = {
    "cms_index": 1,
    "founder": 1,
    "mobile_epaper": 1,
    "paper_api": 1,
    "pdf_site": 1,
    "wechat_read": 1,
}


class FetchLockedError(RuntimeError):
    pass


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
    """Crash-recoverable exclusive lock scoped to one archive and issue date."""
    normalized_day = lib.norm_day(day).isoformat()
    archive_identity = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(archive_root)))
    )
    archive_key = hashlib.sha256(archive_identity.encode("utf-8")).hexdigest()
    lock_directory = os.path.join(
        tempfile.gettempdir(), "readdaily-fetch-locks", archive_key
    )
    os.makedirs(lock_directory, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_directory, normalized_day + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    held_key = (archive_key, normalized_day)
    acquired = False
    try:
        with _HELD_LOCKS_GUARD:
            if held_key in _HELD_LOCKS:
                raise FetchLockedError(
                    "同一归档与日期已有抓取任务运行：%s" % normalized_day
                )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise FetchLockedError(
                "同一归档与日期已有抓取任务运行：%s" % normalized_day
            ) from exc
        with _HELD_LOCKS_GUARD:
            if held_key in _HELD_LOCKS:
                raise FetchLockedError(
                    "同一归档与日期已有抓取任务运行：%s" % normalized_day
                )
            _HELD_LOCKS.add(held_key)
        acquired = True
        os.ftruncate(descriptor, 0)
        os.write(
            descriptor,
            ("pid=%s\narchive=%s\ndate=%s\n" % (
                os.getpid(), archive_identity, normalized_day
            )).encode("utf-8"),
        )
        os.fsync(descriptor)
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
    except (OSError, UnicodeError, ValueError):
        return None
    except (KeyError, json.JSONDecodeError, lib.ArchivePathSafetyError,
            lib.ArchiveConflictError):
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


def _stage_skip_status(state, stage, src, day, aps):
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
    current_digest = _issue_evidence_digest(aps["dir"], src["id"], day)
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
    except Exception as exc:  # noqa: BLE001
        print("  failed 状态保存失败:", exc)
    try:
        lib.log_line(daily_log, {
            "source": sid,
            "source_name": src.get("name") or sid,
            "date": day.isoformat(),
            "stage": "failed",
            "note": message,
        })
    except Exception as exc:  # noqa: BLE001
        print("  failed 日志保存失败:", exc)


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
        except BaseException:
            if lib.path_exists(self.snapshot_root):
                lib.durable_rmtree(self.snapshot_root)
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
            raise RuntimeError("流水线耐久回滚此前未完成，拒绝重复宣称完成")
        if self.commit_attempted:
            raise RuntimeError("流水线已进入提交收尾，不能再执行回滚")
        self.rollback_attempted = True
        try:
            if self.had_issue:
                if not lib.path_is_dir(self.snapshot_issue):
                    raise RuntimeError("旧期次快照缺失，无法恢复")
                lib.replace_issue_directory(self.snapshot_issue, self.issue_dir)
                self.snapshot_root = None
            elif lib.path_exists(self.issue_dir):
                if not lib.path_is_dir(self.issue_dir):
                    raise RuntimeError("新期次路径类型异常，拒绝自动删除")
                lib.durable_rmtree(self.issue_dir)

            if self.had_state:
                self._restore_bytes(self.state_path, self.state_bytes)
            elif lib.path_exists(self.state_path):
                if not lib.path_is_file(self.state_path):
                    raise RuntimeError("新状态路径类型异常，拒绝自动删除")
                lib.durable_unlink(self.state_path)
            if self.snapshot_root:
                lib.durable_rmtree(self.snapshot_root, missing_ok=True)
                self.snapshot_root = None
        except BaseException as exc:
            raise RuntimeError(
                "流水线耐久回滚未完成；请检查 issue=%s、state=%s、snapshot=%s"
                % (self.issue_dir, self.state_path, self.snapshot_root or "无")
            ) from exc
        self.finished = True

    def commit(self):
        if self.finished:
            return
        if self.commit_attempted:
            raise RuntimeError("流水线提交收尾此前未完成")
        if self.rollback_attempted:
            raise RuntimeError("流水线已经回滚，不能再提交")
        self.commit_attempted = True
        try:
            if self.snapshot_root:
                lib.durable_rmtree(self.snapshot_root, missing_ok=True)
                self.snapshot_root = None
        except BaseException as exc:
            raise RuntimeError(
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
            print("  acquire:", note)
            if not ok:
                transaction.rollback()
                print("  acquire 失败:", note)
                _record_source_failure(root, daily_log, src, day, note)
                return "failed"

        issue, err = ad.fetch(src, day, root)
        if err:
            transaction.rollback()
            print("  fetch 失败:", err)
            _record_source_failure(root, daily_log, src, day, err)
            return "failed"
        if not isinstance(issue, dict):
            raise RuntimeError("fetch 未返回有效 issue")
        ok, chain = lib.chain_check(root, sid, day, issue.get("issue_no"))
        print("  fetch ok;", "👍" if ok else "⚠️", chain)
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
            print("  parse 失败:", err)
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
        print("  parsed ok:", units, "units")
        return "ok"
    except BaseException:
        if not transaction.rollback_attempted and not transaction.commit_attempted:
            transaction.rollback()
        raise


def _run_source_in_archive_session(src, day, root, args, daily_log):
    sid = src["id"]
    aps = lib.archive_paths(root, sid, day)
    ad = load_adapter(src["channel"])
    print(f"\n=== [{sid}] {src['name']} {day} ===")
    acquire = getattr(ad, "acquire", None)

    stages = set(args.stage.split(","))
    requested_stages = [
        stage for stage in ("fetched", "parsed") if stage in stages
    ]
    skipped_stages = 0
    if "fetched" in stages:
        state = lib.load_json(aps["state"])
        can_skip, skip_note = _stage_skip_status(
            state, "fetched", src, day, aps
        )
        if can_skip and not args.no_state_skip:
            print("  fetched 已完成且%s，跳过" % skip_note)
            skipped_stages += 1
        else:
            if not args.no_state_skip:
                print("  fetched 状态不可复用：%s，强制重跑" % skip_note)
            if "parsed" in stages:
                return _run_atomic_refresh(
                    src, day, root, args, daily_log, ad, acquire, aps
                )
            if callable(acquire):
                ok, note = acquire(
                    src, day, root, offline_ok=args.offline
                ) if src["channel"] == "wechat_read" else acquire(src, day, root)
                print("  acquire:", note)
                if not ok:
                    print("  acquire 失败:", note)
                    _record_source_failure(root, daily_log, src, day, note)
                    return "failed"
            issue, err = ad.fetch(src, day, root)
            if err:
                print("  fetch 失败:", err)
                _record_source_failure(root, daily_log, src, day, err)
                return "failed"
            if not isinstance(issue, dict):
                raise RuntimeError("fetch 未返回有效 issue")
            ok, chain = lib.chain_check(root, sid, day, issue.get("issue_no"))
            print("  fetch ok;", "👍" if ok else "⚠️", chain)
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
            state, "parsed", src, day, aps
        )
        if can_skip and not args.no_state_skip:
            print("  parsed 已完成且%s，跳过" % skip_note)
            skipped_stages += 1
            return (
                "skipped"
                if skipped_stages == len(requested_stages)
                else "ok"
            )
        if not args.no_state_skip:
            print("  parsed 状态不可复用：%s，强制重跑" % skip_note)
        issue, err = ad.parse(src, day, root)
        if err:
            print("  parse 失败:", err)
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
        print("  parsed ok:", n, "units")
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
    args = ap.parse_args()

    reg = load_registry(args.registry)
    try:
        # Keep this explicit even though load_registry validates: tests and
        # embedders may inject an already-loaded registry object.
        validate_registry(reg)
        root = resolve_archive_root(reg)
        root = validate_archive_outside_vault(root, args.vault)
        validate_registry_write_roots_outside_vault(reg, args.vault)
    except ValueError as exc:
        sys.exit(str(exc))
    d = lib.norm_day(args.date or datetime.date.today())
    want = [s for s in reg["sources"]
            if (args.source and s["id"] in args.source.split(",")) or (not args.source and s.get("enabled"))]

    if args.probe:
        src = next((s for s in reg["sources"] if s["id"] == args.probe), None)
        if not src:
            sys.exit("无此源")
        ad = load_adapter(src["channel"])
        res = ad.probe(src, d)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return

    counts = {"ok": 0, "failed": 0, "skipped": 0}
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
                for src in want:
                    try:
                        try:
                            outcome = _run_source(
                                src, d, root, args, daily_log
                            )
                        except Exception as exc:  # noqa: BLE001
                            print("\n=== [%s] %s %s ===" % (
                                src.get("id", "unknown"),
                                src.get("name", "未知来源"), d
                            ))
                            print("  来源执行异常:", exc)
                            _record_source_failure(
                                root, daily_log, src, d, exc
                            )
                            outcome = "failed"
                    except Exception as exc:  # noqa: BLE001
                        # An identity conflict makes another archive write
                        # unsafe. The outer pinned session will also fail.
                        print("\n=== [%s] %s %s ===" % (
                            src.get("id", "unknown"),
                            src.get("name", "未知来源"), d
                        ))
                        print("  归档身份冲突，来源已失败:", exc)
                        outcome = "failed"
                    counts[outcome] += 1
    except (FetchLockedError, lib.ArchivePathSafetyError,
            lib.ArchiveConflictError) as exc:
        sys.exit(str(exc))

    print("\n完成：成功 %s，失败 %s，跳过 %s" % (
        counts["ok"], counts["failed"], counts["skipped"]
    ))


if __name__ == "__main__":
    main()
