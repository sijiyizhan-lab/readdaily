#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transactional publisher for the local construction-newspaper workbench.

Only managed blocks below ``09-建设新闻与报纸摘要`` are generated.  Existing
content outside those blocks is treated as user-owned and is preserved byte for
byte (apart from the final newline needed when a first managed block is added).
"""

import datetime as _datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


SCHEMA_VERSION = 1
TARGET_FOLDER = "09-建设新闻与报纸摘要"
PIPELINE_VERSION = "workbench-api-1"
TEMPLATE_VERSION = "construction-vault-1"
SUPPORTED_SOURCE = "zgjsb"
TOPICS = (
    "建设投资与房地产",
    "城市更新与城市治理",
    "智能建造与智能制造",
    "产业创新与建筑业转型",
    "工程咨询、招投标与供应链",
    "住房民生与社区服务",
    "建设安全与城市韧性",
)
_ID_RE = re.compile(r"^[a-f0-9]{64}$")


class PublisherError(RuntimeError):
    """Base publisher error safe to surface through the JSON API."""


class PathSafetyError(PublisherError):
    """A destination escaped the configured Vault boundary."""


class ConflictError(PublisherError):
    """A file changed after the plan or transaction was created."""


class PlanNotFoundError(PublisherError):
    """A requested plan/transaction does not exist."""


class RecoveryError(PublisherError):
    """A transaction stopped in a recoverable but incomplete state."""


def _now():
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _json_bytes(obj):
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash_text(text):
    return _hash_bytes(text.encode("utf-8"))


def _empty_hash():
    return _hash_bytes(b"")


def _atomic_write_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".readdaily-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_write_json(path, obj):
    _atomic_write_bytes(path, json.dumps(
        obj, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8"))


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PublisherError("JSON 文件无法读取：%s" % path) from exc


def _safe_archive_path(archive_root, *parts):
    root = os.path.abspath(os.path.expanduser(str(archive_root)))
    target = os.path.abspath(os.path.join(root, *[str(p) for p in parts]))
    try:
        if os.path.commonpath([root, target]) != root:
            raise PathSafetyError("archive 路径越界")
    except ValueError as exc:
        raise PathSafetyError("archive 路径越界") from exc
    if os.path.lexists(root):
        if not os.path.isdir(root):
            raise PathSafetyError("archive 根路径不是目录")
        root_real = os.path.realpath(root)
        probe = target
        while not os.path.lexists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            if os.path.commonpath([root_real, os.path.realpath(probe)]) != root_real:
                raise PathSafetyError("archive 子路径存在符号链接越界")
        except ValueError as exc:
            raise PathSafetyError("archive 子路径存在符号链接越界") from exc
    return Path(target)


def validate_vault_root(vault_root):
    """Return the canonical Vault root after checking its Obsidian marker."""
    configured = os.path.abspath(os.path.expanduser(str(vault_root)))
    if not os.path.isdir(configured):
        raise PathSafetyError("Vault 根路径不存在或不是目录")
    canonical = os.path.realpath(configured)
    marker = os.path.join(canonical, ".obsidian")
    if os.path.islink(marker) or not os.path.isdir(marker):
        raise PathSafetyError("Vault 根路径缺少真实的 .obsidian 目录")
    try:
        if os.path.commonpath([canonical, os.path.realpath(marker)]) != canonical:
            raise PathSafetyError(".obsidian 目录越出 Vault 根路径")
    except ValueError as exc:
        raise PathSafetyError(".obsidian 目录越出 Vault 根路径") from exc
    return Path(canonical)


def _safe_target(vault_root, relative_path):
    """Resolve a plan target while rejecting traversal and symlink escapes."""
    vault = str(validate_vault_root(vault_root))
    rel = str(relative_path)
    if os.path.isabs(rel):
        raise PathSafetyError("Vault 目标必须是相对路径")
    normal = os.path.normpath(rel)
    if normal == ".." or normal.startswith(".." + os.sep):
        raise PathSafetyError("Vault 目标路径越界")
    expected_prefix = TARGET_FOLDER + os.sep
    if normal != TARGET_FOLDER and not normal.startswith(expected_prefix):
        raise PathSafetyError("目标只能位于 %s" % TARGET_FOLDER)
    target_root = os.path.abspath(os.path.join(vault, TARGET_FOLDER))
    if os.path.lexists(target_root):
        if os.path.islink(target_root) or not os.path.isdir(target_root):
            raise PathSafetyError("%s 必须是真实目录，不能是符号链接" % TARGET_FOLDER)
        target_root_real = os.path.realpath(target_root)
    else:
        target_root_real = target_root
    target = os.path.abspath(os.path.join(vault, normal))
    try:
        if os.path.commonpath([target_root, target]) != target_root:
            raise PathSafetyError("Vault 目标路径越界")
    except ValueError as exc:
        raise PathSafetyError("Vault 目标路径越界") from exc

    probe = target
    while not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    probe_real = os.path.realpath(probe)
    try:
        if os.path.lexists(target_root):
            contained = os.path.commonpath([target_root_real, probe_real]) == target_root_real
        else:
            # No descendant can exist while its target-root parent is absent.
            contained = probe_real == vault
        if not contained:
            raise PathSafetyError("Vault 目标存在符号链接越出 %s" % TARGET_FOLDER)
    except ValueError as exc:
        raise PathSafetyError("Vault 目标存在符号链接越出 %s" % TARGET_FOLDER) from exc
    return Path(target)


def _safe_name(value):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(value or "")).strip(" ._")
    return cleaned[:100] or "未命名"


def _managed_markers(key):
    if not re.fullmatch(r"[A-Za-z0-9:._-]+", key):
        raise PublisherError("managed block key 非法")
    return (
        "<!-- READDAILY:BEGIN %s -->" % key,
        "<!-- READDAILY:END %s -->" % key,
    )


def merge_managed_block(existing, key, generated):
    """Insert or replace one owned block, preserving all other text."""
    start, end = _managed_markers(key)
    has_start = start in existing
    has_end = end in existing
    if has_start != has_end:
        raise ConflictError("managed block 标记不完整，需人工修复")
    block = "%s\n%s\n%s" % (start, generated.rstrip(), end)
    if has_start:
        left, remainder = existing.split(start, 1)
        _old, right = remainder.split(end, 1)
        return left + block + right
    if not existing:
        return block + "\n"
    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + separator + block + "\n"


def _topic_link(topic):
    return "[[%s/主题/%s|%s]]" % (TARGET_FOLDER, topic, topic)


def _daily_title(issue):
    suffix = "第%s期" % issue.get("issue_no") if issue.get("issue_no") else "期号待核"
    return "%s %s%s摘要" % (issue["date"], issue.get("source_name") or issue["source"], suffix)


def _edition_label(issue_unit):
    no = issue_unit.get("edition_no")
    name = issue_unit.get("edition_name") or issue_unit.get("title") or "版面"
    return ("第%s版｜%s" % (no, name)) if no is not None else str(name)


def _published_unit_label(issue_unit, draft_unit):
    title = str(draft_unit.get("title") or "").strip()
    if not title:
        return _edition_label(issue_unit)
    no = issue_unit.get("edition_no")
    return ("第%s版｜%s" % (no, title)) if no is not None else title


def _render_fact(fact):
    value = str(fact.get("value", "")).strip()
    unit = str(fact.get("unit", "")).strip()
    amount = (value + unit) if value or unit else ""
    pieces = [
        "%s%s%s" % (fact.get("subject", ""), fact.get("action", ""), fact.get("object", "")),
    ]
    if amount:
        pieces.append(amount)
    if str(fact.get("time", "")).strip():
        pieces.append(str(fact["time"]).strip())
    pieces.append("来源：%s" % fact.get("source", ""))
    return "；".join(x for x in pieces if x)


def _draft_by_id(draft):
    return {str(unit.get("id")): unit for unit in draft.get("units", [])}


def _daily_generated(issue, draft):
    drafted = _draft_by_id(draft)
    lines = [
        "## 工作台归档",
        "",
        "- 来源：%s" % (issue.get("source_name") or issue["source"]),
        "- 日期：%s" % issue["date"],
        "- 期号：%s" % (issue.get("issue_no") or "待人工核对"),
        "- 归档状态：已由建设读报台复核并发布",
        "",
    ]
    for unit in issue.get("units", []):
        item = drafted.get(str(unit.get("id")), {})
        lines.extend([
            "### %s" % _published_unit_label(unit, item),
            "",
            str(item.get("summary", "")).strip(),
            "",
            "重要性：%s/5" % item.get("importance", 3),
            "主题：%s" % "、".join(_topic_link(x) for x in item.get("topics", [])),
        ])
        for fact in item.get("facts", []):
            lines.append("- 事实：%s" % _render_fact(fact))
        lines.append("")
    return "\n".join(lines).rstrip()


def _topic_generated(issue, draft, topic, daily_title):
    drafted = _draft_by_id(draft)
    issue_units = {str(unit.get("id")): unit for unit in issue.get("units", [])}
    issue_label = "%s｜%s%s" % (
        issue["date"],
        issue.get("source_name") or issue["source"],
        ("第%s期" % issue["issue_no"]) if issue.get("issue_no") else "（期号待核）",
    )
    lines = ["## %s" % issue_label, ""]
    matched = [x for x in draft.get("units", []) if topic in x.get("topics", [])]
    if not matched:
        lines.append("- 本期没有经人工归入该主题的版面。")
    for item in matched:
        unit = issue_units.get(str(item.get("id")), {})
        lines.append("- **%s**：%s" % (
            _published_unit_label(unit, item), item.get("summary", "")))
        for fact in item.get("facts", []):
            lines.append("  - 事实：%s" % _render_fact(fact))
    lines.extend([
        "",
        "来源：[[%s/日报/%s|%s]]。" % (TARGET_FOLDER, daily_title, daily_title),
    ])
    return "\n".join(lines).rstrip()


def _index_generated(issue, daily_title):
    return "- [[%s/日报/%s|%s]]｜%s｜%s版" % (
        TARGET_FOLDER,
        daily_title,
        daily_title,
        ("第%s期" % issue["issue_no"]) if issue.get("issue_no") else "期号待核",
        len(issue.get("units", [])),
    )


def _base_for(relative_path, title):
    rel = str(relative_path).replace(os.sep, "/")
    if "/日报/" in rel:
        return "---\ntype: construction_newspaper_daily\nmanaged_by: readdaily\n---\n\n# %s\n" % title
    if "/主题/" in rel:
        return "# %s\n" % title
    return "# 建设新闻与报纸摘要索引\n"


def _read_target(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return b"", ""
    except IsADirectoryError as exc:
        raise PathSafetyError("目标 Markdown 路径被目录占用：%s" % path) from exc
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublisherError("目标 Markdown 不是 UTF-8：%s" % path) from exc


def _change_for(vault_root, relative_path, title, key, generated):
    target = _safe_target(vault_root, relative_path)
    before_raw, existing = _read_target(target)
    source = existing if existing else _base_for(relative_path, title)
    after = merge_managed_block(source, key, generated)
    after_raw = after.encode("utf-8")
    diff = "".join(difflib.unified_diff(
        existing.splitlines(True),
        after.splitlines(True),
        fromfile="before/%s" % relative_path,
        tofile="after/%s" % relative_path,
    ))
    return {
        "relative_path": str(relative_path).replace(os.sep, "/"),
        "before_exists": target.exists(),
        "before_hash": _hash_bytes(before_raw),
        "after_hash": _hash_bytes(after_raw),
        "after": after,
        "diff": diff,
    }


def create_plan(archive_root, vault_root, issue, draft):
    """Build and persist a deterministic plan without modifying the Vault."""
    vault_root = validate_vault_root(vault_root)
    if issue.get("source") != draft.get("source") or issue.get("date") != draft.get("date"):
        raise PublisherError("草稿与报纸来源/日期不一致")
    source = str(issue["source"])
    day = str(issue["date"])
    if source != SUPPORTED_SOURCE:
        raise PublisherError("建设主题知识库仅允许发布中国建设报（zgjsb）")
    _reject_replan_with_pending_transaction(archive_root, vault_root, source, day)
    key = "issue:%s:%s" % (source, day)
    title = _daily_title(issue)
    daily_rel = "%s/日报/%s.md" % (TARGET_FOLDER, _safe_name(title))
    changes = [
        _change_for(
            vault_root,
            "%s/建设新闻与报纸摘要索引.md" % TARGET_FOLDER,
            "建设新闻与报纸摘要索引",
            key,
            _index_generated(issue, title),
        ),
        _change_for(vault_root, daily_rel, title, key, _daily_generated(issue, draft)),
    ]
    for topic in TOPICS:
        changes.append(_change_for(
            vault_root,
            "%s/主题/%s.md" % (TARGET_FOLDER, topic),
            topic,
            key,
            _topic_generated(issue, draft, topic, title),
        ))
    vault_identity = os.path.realpath(os.path.abspath(os.path.expanduser(str(vault_root))))
    source_sha256 = str(issue.get("source_sha256") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        source_sha256 = _hash_bytes(_json_bytes({"issue": issue, "draft": draft}))
    idempotency_key = _hash_bytes(_json_bytes({
        "vault_id": vault_identity,
        "source_sha256": source_sha256,
        "pipeline_version": PIPELINE_VERSION,
        "template_version": TEMPLATE_VERSION,
    }))
    basis = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "date": day,
        "issue_no": issue.get("issue_no"),
        "source_sha256": source_sha256,
        "pipeline_version": PIPELINE_VERSION,
        "template_version": TEMPLATE_VERSION,
        "idempotency_key": idempotency_key,
        "draft": draft,
        "changes": [{k: c[k] for k in ("relative_path", "before_hash", "after_hash")}
                    for c in changes],
        "vault_root": vault_identity,
    }
    plan_id = _hash_bytes(_json_bytes(basis))
    plan = dict(basis)
    plan.update({
        "plan_id": plan_id,
        "created_at": _now(),
        "changes": changes,
        "status": "planned",
    })
    plan_path = _safe_archive_path(archive_root, "_plans", plan_id + ".json")
    existing = _load_json(plan_path)
    if existing and existing.get("plan_id") == plan_id:
        # Preserve apply metadata when the exact same plan is requested again.
        for field in ("status", "applied_at", "applied_transaction_id"):
            if field in existing:
                plan[field] = existing[field]
    _atomic_write_json(plan_path, plan)
    return plan


def _load_plan(archive_root, plan_id):
    if not _ID_RE.fullmatch(str(plan_id or "")):
        raise PlanNotFoundError("plan id 非法或不存在")
    path = _safe_archive_path(archive_root, "_plans", str(plan_id) + ".json")
    plan = _load_json(path)
    if not plan or plan.get("plan_id") != plan_id:
        raise PlanNotFoundError("发布计划不存在")
    return path, plan


def _verify_plan_integrity(plan):
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ConflictError("发布计划缺少变更清单")
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("after"), str):
            raise ConflictError("发布计划结构损坏")
        if _hash_text(change["after"]) != change.get("after_hash"):
            raise ConflictError("发布计划内容哈希不一致")
    basis = {
        "schema_version": plan.get("schema_version"),
        "source": plan.get("source"),
        "date": plan.get("date"),
        "issue_no": plan.get("issue_no"),
        "source_sha256": plan.get("source_sha256"),
        "pipeline_version": plan.get("pipeline_version"),
        "template_version": plan.get("template_version"),
        "idempotency_key": plan.get("idempotency_key"),
        "draft": plan.get("draft"),
        "changes": [
            {key: change.get(key) for key in ("relative_path", "before_hash", "after_hash")}
            for change in changes
        ],
        "vault_root": plan.get("vault_root"),
    }
    if _hash_bytes(_json_bytes(basis)) != plan.get("plan_id"):
        raise ConflictError("发布计划完整性校验失败")


def _validate_plan_vault(plan, vault_root):
    actual = str(validate_vault_root(vault_root))
    if plan.get("vault_root") != actual:
        raise ConflictError("发布计划属于另一个 Vault")


def _state_path(archive_root, source, day):
    return _safe_archive_path(archive_root, "_state", source, day + ".json")


def _mark_published(archive_root, plan, transaction_id):
    path = _state_path(archive_root, plan["source"], plan["date"])
    state = _load_json(path) or {}
    stages = state.setdefault("stages", {})
    timestamp = _now()
    stages["published"] = timestamp
    stages["archived"] = timestamp
    state["publish_plan_id"] = plan["plan_id"]
    state["publish_transaction_id"] = transaction_id
    _atomic_write_json(path, state)


def _clear_published(archive_root, manifest):
    transaction_id = manifest["transaction_id"]
    state_path = _state_path(archive_root, manifest["source"], manifest["date"])
    state = _load_json(state_path) or {}
    if state.get("publish_transaction_id") != transaction_id:
        return
    state.setdefault("stages", {}).pop("published", None)
    state.setdefault("stages", {}).pop("archived", None)
    state.pop("publish_transaction_id", None)
    state.pop("publish_plan_id", None)
    state["last_rollback"] = {"transaction_id": transaction_id, "at": _now()}
    _atomic_write_json(state_path, state)


def _restore_entry(vault_root, transaction_dir, entry):
    target = _safe_target(vault_root, entry["relative_path"])
    if entry.get("before_exists"):
        snapshot_name = str(entry.get("snapshot") or "")
        if not re.fullmatch(r"before/\d{3,6}\.bin", snapshot_name):
            raise PathSafetyError("事务快照路径非法")
        snapshot = transaction_dir / snapshot_name
        tx_real = str(transaction_dir.resolve())
        try:
            if os.path.commonpath([tx_real, str(snapshot.resolve())]) != tx_real:
                raise PathSafetyError("事务快照路径越界")
        except ValueError as exc:
            raise PathSafetyError("事务快照路径越界") from exc
        if not snapshot.is_file():
            raise PublisherError("事务快照缺失：%s" % entry["relative_path"])
        _atomic_write_bytes(target, snapshot.read_bytes())
    elif target.exists():
        if target.is_symlink() or not target.is_file():
            raise PathSafetyError("回滚目标类型异常")
        target.unlink()


def _load_transaction(archive_root, transaction_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(transaction_id or "")):
        raise PlanNotFoundError("transaction id 非法或不存在")
    tx_dir = _safe_archive_path(archive_root, "_transactions", transaction_id)
    manifest = _load_json(tx_dir / "manifest.json")
    if not manifest or manifest.get("transaction_id") != transaction_id:
        raise PlanNotFoundError("发布事务不存在")
    return tx_dir, manifest


def _iter_transaction_manifests(archive_root):
    tx_root = _safe_archive_path(archive_root, "_transactions")
    if not tx_root.is_dir():
        return []
    records = []
    for candidate in tx_root.iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", candidate.name):
            continue
        if candidate.is_symlink():
            raise PathSafetyError("事务目录不能是符号链接")
        if not candidate.is_dir():
            continue
        manifest = _load_json(candidate / "manifest.json")
        if manifest and manifest.get("transaction_id") == candidate.name:
            records.append((candidate, manifest))
    records.sort(key=lambda item: item[1].get("created_at") or "")
    return records


def _transactions_for_plan(archive_root, plan_id, statuses=None):
    return [
        (tx_dir, manifest)
        for tx_dir, manifest in _iter_transaction_manifests(archive_root)
        if manifest.get("plan_id") == plan_id
        and (statuses is None or manifest.get("status") in statuses)
    ]


def _validate_manifest(manifest, vault_root, plan=None):
    actual_vault = str(validate_vault_root(vault_root))
    if manifest.get("vault_root") != actual_vault:
        raise ConflictError("发布事务属于另一个 Vault")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ConflictError("发布事务缺少文件清单")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConflictError("发布事务文件清单损坏")
        relative_path = entry.get("relative_path")
        _safe_target(vault_root, relative_path or "")
        if not _ID_RE.fullmatch(str(entry.get("before_hash") or "")):
            raise ConflictError("发布事务 before hash 损坏")
        if not _ID_RE.fullmatch(str(entry.get("after_hash") or "")):
            raise ConflictError("发布事务 after hash 损坏")
        if not isinstance(entry.get("before_exists"), bool):
            raise ConflictError("发布事务 before_exists 损坏")
        snapshot = str(entry.get("snapshot") or "")
        if not re.fullmatch(r"before/\d{3,6}\.bin", snapshot):
            raise ConflictError("发布事务快照字段损坏")
        normalized.append(entry)
    if plan is not None:
        expected = [
            (item.get("relative_path"), item.get("before_hash"), item.get("after_hash"))
            for item in plan.get("changes", [])
        ]
        actual = [
            (item.get("relative_path"), item.get("before_hash"), item.get("after_hash"))
            for item in normalized
        ]
        if actual != expected:
            raise ConflictError("发布事务与发布计划不一致")
    return normalized


def _entry_state(vault_root, entry):
    target = _safe_target(vault_root, entry["relative_path"])
    if target.exists() and not target.is_file():
        raise PathSafetyError("事务目标不是普通文件：%s" % entry["relative_path"])
    current = target.read_bytes() if target.exists() else b""
    current_hash = _hash_bytes(current)
    before = current_hash == entry["before_hash"]
    after = current_hash == entry["after_hash"]
    if before and after:
        return "unchanged"
    if before:
        return "before"
    if after:
        return "after"
    return "conflict"


def _write_manifest_status(manifest_path, manifest, status, **fields):
    manifest["status"] = status
    manifest.update(fields)
    _atomic_write_json(manifest_path, manifest)


def _record_recovery_required(manifest_path, manifest, status, errors, message):
    details = [str(error) for error in errors if str(error)] or [message]
    manifest["status"] = status
    manifest["recovery_required_at"] = _now()
    manifest["recovery_errors"] = details
    try:
        _atomic_write_json(manifest_path, manifest)
    except Exception as record_error:
        raise RecoveryError(
            "%s；且无法持久化恢复状态：%s" % (message, record_error)
        ) from record_error
    raise RecoveryError("%s：%s" % (message, "；".join(details)))


def _restore_apply_transaction(vault_root, tx_dir, manifest, reason):
    manifest_path = tx_dir / "manifest.json"
    entries = _validate_manifest(manifest, vault_root)
    states = [(entry, _entry_state(vault_root, entry)) for entry in entries]
    conflicts = [entry["relative_path"] for entry, state in states if state == "conflict"]
    if conflicts:
        _record_recovery_required(
            manifest_path,
            manifest,
            "recovery_required",
            ["文件内容既不等于发布前也不等于计划结果：%s" % path for path in conflicts],
            "发布事务无法自动恢复",
        )

    errors = []
    for entry, state in reversed(states):
        if state != "after":
            continue
        try:
            _restore_entry(vault_root, tx_dir, entry)
        except Exception as exc:
            errors.append("%s：%s" % (entry["relative_path"], exc))
    for entry in entries:
        try:
            if _entry_state(vault_root, entry) not in ("before", "unchanged"):
                errors.append("恢复后哈希不一致：%s" % entry["relative_path"])
        except Exception as exc:
            errors.append("恢复后无法校验 %s：%s" % (entry["relative_path"], exc))
    if errors:
        _record_recovery_required(
            manifest_path, manifest, "recovery_required", errors, "发布失败且自动恢复未完成"
        )

    manifest.pop("recovery_errors", None)
    _write_manifest_status(
        manifest_path,
        manifest,
        "failed_restored",
        failed_at=manifest.get("failed_at") or _now(),
        restored_at=_now(),
        failure=str(reason),
    )


def _write_plan_applied(plan_path, plan, transaction_id, applied_at):
    plan["status"] = "applied"
    plan["applied_at"] = applied_at
    plan["applied_transaction_id"] = transaction_id
    _atomic_write_json(plan_path, plan)


def _repair_applied_metadata(archive_root, plan_path, plan, manifest):
    errors = []
    try:
        _mark_published(archive_root, plan, manifest["transaction_id"])
    except Exception as exc:
        errors.append("发布状态：%s" % exc)
    try:
        _write_plan_applied(
            plan_path, plan, manifest["transaction_id"], manifest["applied_at"]
        )
    except Exception as exc:
        errors.append("发布计划状态：%s" % exc)
    if errors:
        manifest["metadata_errors"] = errors
        try:
            _atomic_write_json(
                _safe_archive_path(
                    archive_root, "_transactions", manifest["transaction_id"], "manifest.json"
                ),
                manifest,
            )
        except Exception as record_error:
            errors.append("事务元数据告警无法记录：%s" % record_error)
        raise RecoveryError("Vault 已发布且事务可回滚，但元数据待重试修复：%s" % "；".join(errors))
    manifest.pop("metadata_errors", None)


def _recover_pending_plan_transactions(archive_root, vault_root, plan_path, plan):
    pending_statuses = {"prepared", "applying", "recovery_required"}
    recovered = False
    for tx_dir, manifest in _transactions_for_plan(
        archive_root, plan["plan_id"], pending_statuses
    ):
        entries = _validate_manifest(manifest, vault_root, plan=plan)
        states = [_entry_state(vault_root, entry) for entry in entries]
        if all(state in ("after", "unchanged") for state in states):
            applied_at = manifest.get("applied_at") or _now()
            _write_manifest_status(
                tx_dir / "manifest.json", manifest, "applied", applied_at=applied_at
            )
            _repair_applied_metadata(archive_root, plan_path, plan, manifest)
            return True, manifest["transaction_id"]
        _restore_apply_transaction(
            vault_root, tx_dir, manifest, "恢复上次中断的发布事务"
        )
        recovered = True
    return recovered, None


def _reject_replan_with_pending_transaction(archive_root, vault_root, source, day):
    """Keep publish-plan read-only when an older apply still needs recovery."""
    canonical_vault = str(validate_vault_root(vault_root))
    pending_statuses = {"prepared", "applying", "recovery_required"}
    for _tx_dir, manifest in _iter_transaction_manifests(archive_root):
        if manifest.get("status") not in pending_statuses:
            continue
        if manifest.get("source") != source or manifest.get("date") != day:
            continue
        if manifest.get("vault_root") != canonical_vault:
            continue
        raise RecoveryError(
            "存在未完成发布事务 %s；请使用原计划 %s 重试 publish-apply 后再生成预览"
            % (manifest.get("transaction_id"), manifest.get("plan_id"))
        )


def apply_plan(archive_root, vault_root, plan_id):
    """Apply a plan atomically per file after optimistic hash validation."""
    plan_path, plan = _load_plan(archive_root, plan_id)
    _validate_plan_vault(plan, vault_root)
    for change in plan.get("changes", []):
        _safe_target(vault_root, change.get("relative_path", ""))
    _verify_plan_integrity(plan)
    recovered_pending, finalized_transaction = _recover_pending_plan_transactions(
        archive_root, vault_root, plan_path, plan
    )
    if finalized_transaction:
        return {
            "applied": False,
            "idempotent": True,
            "recovered_pending_transaction": True,
            "plan_id": plan_id,
            "transaction_id": finalized_transaction,
            "changed_files": 0,
        }
    checked = []
    for change in plan.get("changes", []):
        target = _safe_target(vault_root, change.get("relative_path", ""))
        current = target.read_bytes() if target.exists() else b""
        current_hash = _hash_bytes(current)
        checked.append((change, target, current, current_hash))
    if checked and all(current_hash == change["after_hash"]
                       for change, _target, _raw, current_hash in checked):
        applied_records = _transactions_for_plan(archive_root, plan_id, {"applied"})
        transaction_id = plan.get("applied_transaction_id")
        if applied_records:
            _tx_dir, applied_manifest = applied_records[-1]
            transaction_id = applied_manifest["transaction_id"]
            _repair_applied_metadata(archive_root, plan_path, plan, applied_manifest)
        return {
            "applied": False,
            "idempotent": True,
            "plan_id": plan_id,
            "transaction_id": transaction_id,
            "changed_files": 0,
            "recovered_pending_transaction": recovered_pending,
        }
    conflicts = [change["relative_path"] for change, _target, _raw, current_hash in checked
                 if current_hash != change["before_hash"]]
    if conflicts:
        raise ConflictError("文件在预览后发生变化：%s" % "、".join(conflicts))

    transaction_id = uuid.uuid4().hex
    tx_dir = _safe_archive_path(archive_root, "_transactions", transaction_id)
    before_dir = tx_dir / "before"
    before_dir.mkdir(parents=True, exist_ok=False)
    entries = []
    for index, (change, _target, current, _current_hash) in enumerate(checked):
        snapshot_name = "before/%03d.bin" % index
        if change.get("before_exists"):
            _atomic_write_bytes(tx_dir / snapshot_name, current)
        entries.append({
            "relative_path": change["relative_path"],
            "before_exists": bool(change.get("before_exists")),
            "before_hash": change["before_hash"],
            "after_hash": change["after_hash"],
            "snapshot": snapshot_name,
        })
    manifest_path = tx_dir / "manifest.json"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "plan_id": plan_id,
        "source": plan["source"],
        "date": plan["date"],
        "source_sha256": plan["source_sha256"],
        "pipeline_version": plan["pipeline_version"],
        "template_version": plan["template_version"],
        "idempotency_key": plan["idempotency_key"],
        "vault_root": plan["vault_root"],
        "created_at": _now(),
        "status": "prepared",
        "entries": entries,
    }
    _atomic_write_json(manifest_path, manifest)
    _write_manifest_status(manifest_path, manifest, "applying", started_at=_now())

    try:
        for change, target, _current, _current_hash in checked:
            # Re-check after creating snapshots, closing the most common edit race.
            live = target.read_bytes() if target.exists() else b""
            if _hash_bytes(live) != change["before_hash"]:
                raise ConflictError("写入前文件发生变化：%s" % change["relative_path"])
            _atomic_write_bytes(target, change["after"].encode("utf-8"))
        applied_at = _now()
        _write_manifest_status(
            manifest_path, manifest, "applied", applied_at=applied_at
        )
    except Exception as apply_error:
        manifest["failed_at"] = _now()
        try:
            _restore_apply_transaction(vault_root, tx_dir, manifest, apply_error)
        except RecoveryError as recovery_error:
            raise recovery_error from apply_error
        raise PublisherError("发布失败，Vault 已恢复到预览前状态：%s" % apply_error) from apply_error

    _repair_applied_metadata(archive_root, plan_path, plan, manifest)
    return {
        "applied": True,
        "idempotent": False,
        "plan_id": plan_id,
        "transaction_id": transaction_id,
        "changed_files": len(checked),
        "recovered_pending_transaction": recovered_pending,
    }


def rollback_transaction(archive_root, vault_root, transaction_id):
    tx_dir, manifest = _load_transaction(archive_root, transaction_id)
    entries = _validate_manifest(manifest, vault_root)
    if manifest.get("status") == "rolled_back":
        try:
            _clear_published(archive_root, manifest)
        except Exception as exc:
            raise RecoveryError("文件已回滚，但发布状态待重试清理：%s" % exc) from exc
        return {"rolled_back": False, "idempotent": True,
                "transaction_id": transaction_id}
    starting_status = manifest.get("status")
    resumable = {"applied", "rolling_back", "rollback_recovery_required"}
    if starting_status not in resumable:
        raise ConflictError("只有已完成的发布事务可以回滚")
    states = [(entry, _entry_state(vault_root, entry)) for entry in entries]
    if starting_status == "applied":
        conflicts = [entry["relative_path"] for entry, state in states
                     if state not in ("after", "unchanged")]
        if conflicts:
            raise ConflictError("发布后文件已被修改，拒绝回滚：%s" % "、".join(conflicts))
    else:
        conflicts = [entry["relative_path"] for entry, state in states
                     if state == "conflict"]
        if conflicts:
            _record_recovery_required(
                tx_dir / "manifest.json",
                manifest,
                "rollback_recovery_required",
                ["回滚期间文件出现未知内容：%s" % path for path in conflicts],
                "回滚无法自动续跑",
            )
    _write_manifest_status(
        tx_dir / "manifest.json", manifest, "rolling_back", rollback_started_at=_now()
    )
    restored = 0
    errors = []
    try:
        for entry, state in reversed(states):
            if state != "after":
                continue
            _restore_entry(vault_root, tx_dir, entry)
            restored += 1
        for entry in entries:
            if _entry_state(vault_root, entry) not in ("before", "unchanged"):
                errors.append("回滚后哈希不一致：%s" % entry["relative_path"])
        if errors:
            raise RecoveryError("；".join(errors))
        _write_manifest_status(
            tx_dir / "manifest.json", manifest, "rolled_back", rolled_back_at=_now()
        )
    except Exception as rollback_error:
        errors.append(str(rollback_error))
        _record_recovery_required(
            tx_dir / "manifest.json",
            manifest,
            "rollback_recovery_required",
            errors,
            "回滚未完成，可在排除故障后用同一事务重试",
        )
    try:
        _clear_published(archive_root, manifest)
    except Exception as exc:
        raise RecoveryError("文件已回滚，但发布状态待重试清理：%s" % exc) from exc
    return {"rolled_back": True, "idempotent": False,
            "transaction_id": transaction_id, "restored_files": restored,
            "resumed": starting_status != "applied"}


def history(archive_root, limit=50):
    records = []
    for _tx_dir, manifest in _iter_transaction_manifests(archive_root):
        records.append({
            key: manifest.get(key)
            for key in ("transaction_id", "plan_id", "source", "date", "status",
                        "created_at", "applied_at", "rolled_back_at")
        })
    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return records[:max(0, int(limit))]
