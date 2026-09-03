#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transactional publisher for the local construction-newspaper workbench.

Only managed blocks below ``09-建设新闻与报纸摘要`` are generated.  Existing
content outside those blocks is treated as user-owned and is preserved byte for
byte (apart from the final newline needed when a first managed block is added).
"""

import contextlib
import ctypes
import datetime as _datetime
import difflib
import fcntl
import functools
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
_PUBLISH_THREAD_LOCKS = {}
_PUBLISH_THREAD_LOCKS_GUARD = threading.Lock()
_DRAFT_THREAD_LOCKS = {}
_DRAFT_THREAD_LOCKS_GUARD = threading.Lock()
_PUBLISH_IO_CONTEXT = threading.local()
DEFAULT_PUBLISHER_STATE_ROOT = Path(
    "~/Library/Application Support/readdaily/publisher-state"
).expanduser()
_APPLY_SENTINEL_PHASES = frozenset({
    "prepared", "applying", "recovery_required",
    "applied_metadata_pending",
})
_ROLLBACK_SENTINEL_PHASES = frozenset({
    "rolling_back", "rollback_recovery_required",
    "rollback_metadata_pending",
})
_ALL_SENTINEL_PHASES = _APPLY_SENTINEL_PHASES | _ROLLBACK_SENTINEL_PHASES


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


class _VaultMutationTarget:
    """Display path plus the trusted Vault session used for mutation."""

    def __init__(
            self, session, relative_path, before_exists,
            before_hash, after_hash):
        self.session = session
        self.relative_path = str(relative_path)
        self.before_exists = bool(before_exists)
        self.before_hash = str(before_hash)
        self.after_hash = str(after_hash)

    def __fspath__(self):
        return os.path.join(self.session["canonical_path"], self.relative_path)


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


def _directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _fd_canonical_path(descriptor):
    if sys.platform == "darwin":
        try:
            raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
        except OSError as exc:
            raise PathSafetyError("无法读取已固定目录的真实路径") from exc
        value = raw.split(b"\0", 1)[0]
        if not value:
            raise PathSafetyError("已固定目录的真实路径为空")
        return os.fsdecode(value)
    proc_path = "/proc/self/fd/%s" % descriptor
    try:
        return os.path.realpath(os.readlink(proc_path))
    except OSError as exc:
        raise PathSafetyError("当前平台无法固定目录真实路径") from exc


def _verify_pinned_directory(session, label):
    for parent_fd, name, child_fd, identity in session["configured_links"]:
        try:
            opened = os.fstat(child_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PathSafetyError("%s配置路径在操作期间被替换" % label) from exc
        if (not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or _inode_identity(opened) != identity
                or _inode_identity(linked) != identity):
            raise PathSafetyError("%s配置路径在操作期间被替换" % label)


@contextlib.contextmanager
def _pinned_directory(configured_path, label, create=False, mode=0o755):
    """Open every configured path component without following symlinks."""
    configured = os.path.abspath(os.path.expanduser(str(configured_path)))
    components = [part for part in configured.split(os.sep) if part]
    descriptors = []
    links = []
    try:
        current = os.open(os.sep, _directory_open_flags())
        descriptors.append(current)
        for name in components:
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise PathSafetyError("%s不存在：%s" % (label, configured))
                try:
                    os.mkdir(name, mode, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise PublisherError("无法创建%s：%s" % (label, exc)) from exc
                try:
                    os.fsync(current)
                except OSError as exc:
                    raise PublisherError("%s创建无法持久化：%s" % (label, exc)) from exc
                try:
                    child = os.open(
                        name, _directory_open_flags(), dir_fd=current
                    )
                except OSError as exc:
                    raise PathSafetyError("%s新建目录被替换" % label) from exc
            except OSError as exc:
                raise PathSafetyError(
                    "%s配置路径含符号链接或非目录组件" % label
                ) from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise PathSafetyError("%s配置路径组件不是目录" % label)
            identity = _inode_identity(info)
            links.append((current, name, child, identity))
            descriptors.append(child)
            current = child
        session = {
            "configured_path": configured,
            "canonical_path": _fd_canonical_path(current),
            "configured_links": links,
            "root_fd": current,
            "root_identity": _inode_identity(os.fstat(current)),
            "label": label,
        }
        _verify_pinned_directory(session, label)
        yield session
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _active_archive_session():
    return getattr(_PUBLISH_IO_CONTEXT, "archive_session", None)


def _active_vault_session():
    return getattr(_PUBLISH_IO_CONTEXT, "vault_session", None)


def _reject_overlapping_roots(left_session, right_session, message):
    left = left_session["canonical_path"]
    right = right_session["canonical_path"]
    try:
        common = os.path.commonpath([left, right])
    except ValueError as exc:
        raise PathSafetyError(message) from exc
    if (common in (left, right)
            or left_session["root_identity"] == right_session["root_identity"]):
        raise PathSafetyError(message)


@contextlib.contextmanager
def _publisher_operation_io(archive_root, vault_root):
    """Pin both configured roots before any publisher-side archive access."""
    previous_archive = _active_archive_session()
    previous_vault = _active_vault_session()
    if previous_archive is not None or previous_vault is not None:
        raise PublisherError("发布目录会话不能嵌套")
    with _pinned_directory(archive_root, "归档根目录") as archive_session:
        with _vault_directory_session(vault_root) as vault_session:
            _reject_overlapping_roots(
                archive_session,
                vault_session,
                "归档根目录必须与 Vault 完全分离",
            )
            _PUBLISH_IO_CONTEXT.archive_session = archive_session
            _PUBLISH_IO_CONTEXT.vault_session = vault_session
            try:
                _verify_pinned_directory(archive_session, "归档根目录")
                _verify_vault_session(vault_session)
                yield archive_session, vault_session
            finally:
                try:
                    del _PUBLISH_IO_CONTEXT.archive_session
                except AttributeError:
                    pass
                try:
                    del _PUBLISH_IO_CONTEXT.vault_session
                except AttributeError:
                    pass


def _session_relative_path(session, path):
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    for base in (session["configured_path"], session["canonical_path"]):
        try:
            if os.path.commonpath([base, absolute]) != base:
                continue
        except ValueError:
            continue
        relative = os.path.relpath(absolute, base)
        if relative == ".":
            return ""
        parts = relative.split(os.sep)
        if any(part in ("", ".", "..") for part in parts):
            raise PathSafetyError("归档相对路径非法")
        return relative

    # On the default case-insensitive APFS volume, issue.json may retain a
    # different casing of the same archive root.  Do not trust lower-cased
    # strings: pin the candidate prefix component-by-component without
    # following symlinks and accept it only when it is the same directory
    # inode as the already active archive session.
    absolute_parts = [part for part in absolute.split(os.sep) if part]
    for base in (session["configured_path"], session["canonical_path"]):
        base_parts = [part for part in base.split(os.sep) if part]
        if len(absolute_parts) <= len(base_parts):
            continue
        prefix = os.sep + os.path.join(*absolute_parts[:len(base_parts)])
        suffix_parts = absolute_parts[len(base_parts):]
        if any(part in ("", ".", "..") for part in suffix_parts):
            raise PathSafetyError("归档相对路径非法")
        try:
            with _pinned_directory(
                    prefix, "归档路径别名前缀") as alias_session:
                if alias_session["root_identity"] != session["root_identity"]:
                    continue
                _verify_pinned_directory(session, "归档根目录")
                return os.path.join(*suffix_parts)
        except (PublisherError, OSError):
            continue
    return None


def _active_archive_relative(path):
    session = _active_archive_session()
    if session is None:
        return None, None
    relative = _session_relative_path(session, path)
    if relative is None:
        return None, None
    return session, relative


def _verify_archive_parent(handle):
    session = handle["session"]
    label = session.get("label", "归档根目录")
    _verify_pinned_directory(session, label)
    for parent_fd, name, child_fd, identity in handle["links"]:
        try:
            opened = os.fstat(child_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PathSafetyError("归档目标祖先目录在操作期间被替换") from exc
        if (not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or _inode_identity(opened) != identity
                or _inode_identity(linked) != identity):
            raise PathSafetyError("归档目标祖先目录在操作期间被替换")


@contextlib.contextmanager
def _archive_target_parent(session, relative_path, create=False):
    parts = str(relative_path).split(os.sep)
    if (not relative_path
            or any(part in ("", ".", "..") for part in parts)):
        raise PathSafetyError("归档目标相对路径非法")
    descriptors = []
    links = []
    current = session["root_fd"]
    missing = False
    try:
        _verify_pinned_directory(
            session, session.get("label", "归档根目录")
        )
        for name in parts[:-1]:
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=current)
            except FileNotFoundError:
                if not create:
                    missing = True
                    break
                try:
                    os.mkdir(name, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise PublisherError("无法创建归档目录：%s" % exc) from exc
                try:
                    os.fsync(current)
                except OSError as exc:
                    raise PublisherError("归档目录创建无法持久化：%s" % exc) from exc
                try:
                    child = os.open(
                        name, _directory_open_flags(), dir_fd=current
                    )
                except OSError as exc:
                    raise PathSafetyError("归档新建目录被替换") from exc
            except OSError as exc:
                raise PathSafetyError("归档目标祖先不是可信目录") from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise PathSafetyError("归档目标祖先不是目录")
            identity = _inode_identity(info)
            descriptors.append(child)
            links.append((current, name, child, identity))
            current = child
        if missing:
            yield None
        else:
            handle = {
                "session": session,
                "links": links,
                "parent_fd": current,
                "name": parts[-1],
                "relative_path": relative_path,
            }
            _verify_archive_parent(handle)
            yield handle
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _archive_read_bytes(session, relative_path, missing_ok=False):
    with _archive_target_parent(
            session, relative_path, create=False) as handle:
        if handle is None:
            if missing_ok:
                return None
            raise FileNotFoundError(relative_path)
        _verify_archive_parent(handle)
        try:
            raw = _read_regular_at(
                handle["parent_fd"], handle["name"], missing_ok=missing_ok
            )
        except ConflictError as exc:
            if missing_ok and "消失" in str(exc):
                return None
            raise
        _verify_archive_parent(handle)
        return raw


def _archive_regular_sha256(session, relative_path):
    """Stream a pinned archive file into SHA-256 without buffering it whole."""
    with _archive_target_parent(
            session, relative_path, create=False) as handle:
        if handle is None:
            return None
        _verify_archive_parent(handle)
        try:
            expected = os.stat(
                handle["name"], dir_fd=handle["parent_fd"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PathSafetyError("归档证据无法安全检查") from exc
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise PathSafetyError("归档证据必须是真实普通文件")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                handle["name"], flags, dir_fd=handle["parent_fd"]
            )
        except OSError as exc:
            raise PathSafetyError("归档证据无法安全打开") from exc
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or _inode_identity(opened) != _inode_identity(expected)):
                raise ConflictError("归档证据在校验前被替换")
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
            after = os.fstat(descriptor)
            try:
                linked_after = os.stat(
                    handle["name"], dir_fd=handle["parent_fd"],
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                raise ConflictError("归档证据在校验期间消失") from exc
            if (not stat.S_ISREG(linked_after.st_mode)
                    or _inode_identity(after) != _inode_identity(opened)
                    or _inode_identity(linked_after) != _inode_identity(opened)
                    or after.st_size != opened.st_size
                    or after.st_mtime_ns != opened.st_mtime_ns):
                raise ConflictError("归档证据在校验期间发生变化")
            _verify_archive_parent(handle)
            return digest.hexdigest(), size
        finally:
            os.close(descriptor)


@contextlib.contextmanager
def _archive_directory_handle(session, relative_path):
    """Open one archive directory through the pinned archive root."""
    relative = str(relative_path).strip(os.sep)
    if (not relative
            or any(part in ("", ".", "..")
                   for part in relative.split(os.sep))):
        raise PathSafetyError("归档目录相对路径非法")
    with _archive_target_parent(
            session, relative + os.sep + ".directory", create=False) as handle:
        if handle is None:
            yield None
            return
        _verify_archive_parent(handle)
        yield handle
        _verify_archive_parent(handle)


def _archive_list_directory(session, relative_path):
    with _archive_directory_handle(session, relative_path) as handle:
        if handle is None:
            return None
        try:
            names = os.listdir(handle["parent_fd"])
        except OSError as exc:
            raise PathSafetyError("归档目录无法安全枚举") from exc
        _verify_archive_parent(handle)
        return sorted(names)


def _atomic_write_session_bytes(session, relative_path, raw):
    with _archive_target_parent(
            session, relative_path, create=True) as handle:
        _verify_archive_parent(handle)
        try:
            existing = os.stat(
                handle["name"], dir_fd=handle["parent_fd"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise PathSafetyError("归档目标无法安全检查") from exc
        if (existing is not None
                and (stat.S_ISLNK(existing.st_mode)
                     or not stat.S_ISREG(existing.st_mode))):
            raise PathSafetyError("归档目标必须是普通文件")
        temporary_name = ".readdaily-%s" % uuid.uuid4().hex
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary_name, flags, 0o600, dir_fd=handle["parent_fd"]
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
                    raise ConflictError("归档新文件在提交时已存在") from exc
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
                os.fsync(handle["parent_fd"])
            _verify_archive_parent(handle)
        except (PublisherError, ConflictError):
            raise
        except OSError as exc:
            raise PublisherError("归档原子写入失败：%s" % exc) from exc
        finally:
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=handle["parent_fd"])
                    os.fsync(handle["parent_fd"])
                except OSError:
                    pass


def _archive_path_exists(session, relative_path):
    with _archive_target_parent(
            session, relative_path, create=False) as handle:
        if handle is None:
            return False
        _verify_archive_parent(handle)
        try:
            info = os.stat(
                handle["name"], dir_fd=handle["parent_fd"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise PathSafetyError("归档目标不能是符号链接")
        return True


def _archive_regular_path_exists(session, relative_path):
    """Check one archive-relative regular file through the pinned root."""
    with _archive_target_parent(
            session, relative_path, create=False) as handle:
        if handle is None:
            return False
        _verify_archive_parent(handle)
        try:
            info = os.stat(
                handle["name"], dir_fd=handle["parent_fd"],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise PathSafetyError("归档文件无法安全检查") from exc
        if stat.S_ISLNK(info.st_mode):
            raise PathSafetyError("归档文件不能是符号链接")
        if not stat.S_ISREG(info.st_mode):
            return False
        _verify_archive_parent(handle)
        return True


def _archive_ensure_directory(session, relative_path, exclusive=False):
    parts = str(relative_path).split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise PathSafetyError("归档目录相对路径非法")
    current = session["root_fd"]
    descriptors = []
    links = []
    try:
        for index, name in enumerate(parts):
            created = False
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=current)
                if exclusive and index == len(parts) - 1:
                    os.close(child)
                    raise ConflictError("发布事务目录已存在")
            except FileNotFoundError:
                try:
                    os.mkdir(name, 0o755, dir_fd=current)
                    created = True
                except FileExistsError:
                    if exclusive and index == len(parts) - 1:
                        raise ConflictError("发布事务目录已存在")
                except OSError as exc:
                    raise PublisherError("无法创建归档目录：%s" % exc) from exc
                try:
                    os.fsync(current)
                    child = os.open(
                        name, _directory_open_flags(), dir_fd=current
                    )
                except OSError as exc:
                    raise PathSafetyError("归档新建目录被替换") from exc
            except ConflictError:
                raise
            except OSError as exc:
                raise PathSafetyError("归档目录组件不可信") from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise PathSafetyError("归档目录组件不是目录")
            identity = _inode_identity(info)
            descriptors.append(child)
            links.append((current, name, child, identity))
            current = child
            _verify_archive_parent({"session": session, "links": links})
            if created:
                try:
                    os.fsync(current)
                except OSError as exc:
                    raise PublisherError(
                        "归档目录创建无法持久化：%s" % exc
                    ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _durable_makedirs(path, mode=0o755):
    """Create every missing directory and fsync its already-open parent."""
    archive_session, archive_relative = _active_archive_relative(path)
    if archive_session is not None:
        _archive_ensure_directory(archive_session, archive_relative)
        return Path(os.path.abspath(os.path.expanduser(str(path))))
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    missing = []
    probe = absolute
    while not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            raise PublisherError("无法找到目录创建锚点：%s" % absolute)
        missing.append(os.path.basename(probe))
        probe = parent
    if os.path.islink(probe) or not os.path.isdir(probe):
        raise PathSafetyError("目录创建锚点必须是真实目录：%s" % probe)
    try:
        descriptor = os.open(os.path.realpath(probe), _directory_open_flags())
    except OSError as exc:
        raise PublisherError("无法打开目录创建锚点：%s" % exc) from exc
    try:
        for name in reversed(missing):
            try:
                os.mkdir(name, mode, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise PublisherError("无法创建目录 %s：%s" % (name, exc)) from exc
            try:
                os.fsync(descriptor)
            except OSError as exc:
                raise PublisherError("目录创建无法持久化 %s：%s" % (name, exc)) from exc
            try:
                child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise PathSafetyError("新建目录被替换或不是可信目录：%s" % name) from exc
            child_stat = os.fstat(child)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child)
                raise PathSafetyError("新建路径不是目录：%s" % name)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return Path(absolute)


def _atomic_write_bytes(path, raw):
    if isinstance(path, _VaultMutationTarget):
        return _atomic_write_vault_target(path, raw)
    archive_session, archive_relative = _active_archive_relative(path)
    if archive_session is not None:
        return _atomic_write_session_bytes(
            archive_session, archive_relative, raw
        )
    path = Path(path)
    _durable_makedirs(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".readdaily-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, str(path))
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_fd = os.open(str(path.parent), directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise PublisherError(
            "原子写入无法持久化到目录 %s：%s" % (path.parent, exc)
        ) from exc
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_write_json(path, obj):
    _atomic_write_bytes(path, json.dumps(
        obj, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8"))


def _load_json(path):
    archive_session, archive_relative = _active_archive_relative(path)
    if archive_session is not None:
        try:
            raw = _archive_read_bytes(
                archive_session, archive_relative, missing_ok=True
            )
            if raw is None:
                return None
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, OSError) as exc:
            raise PublisherError("JSON 文件无法读取：%s" % path) from exc
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
    active = _active_archive_session()
    if (active is not None
            and root == active["configured_path"]):
        _verify_pinned_directory(active, "归档根目录")
        return Path(target)
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


@contextlib.contextmanager
def _fetch_date_evidence_lock(archive_root, day):
    """Share fetch.py's per-archive/day lock while validating source evidence."""
    try:
        normalized_day = _datetime.date.fromisoformat(str(day)).isoformat()
    except (TypeError, ValueError) as exc:
        raise PublisherError("发布日期无效") from exc
    archive_identity = _canonical_archive_root(archive_root)
    archive_key = hashlib.sha256(archive_identity.encode("utf-8")).hexdigest()
    lock_directory = os.path.join(
        tempfile.gettempdir(), "readdaily-fetch-locks", archive_key
    )
    os.makedirs(lock_directory, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_directory, normalized_day + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextlib.contextmanager
def _draft_rmw_lock(archive_root, source, day):
    """Serialize one persisted-draft RMW using the workbench lock identity."""
    archive_identity = _canonical_archive_root(archive_root)
    scope = "draft:%s:%s" % (source, day)
    lock_key = hashlib.sha256(
        (archive_identity + "\0" + scope).encode("utf-8")
    ).hexdigest()
    lock_directory = Path(tempfile.gettempdir()) / "readdaily-workbench-locks"
    lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_directory.is_symlink() or not lock_directory.is_dir():
        raise PathSafetyError("草稿锁目录必须是真实目录")
    lock_path = lock_directory / (lock_key + ".lock")
    with _DRAFT_THREAD_LOCKS_GUARD:
        thread_lock = _DRAFT_THREAD_LOCKS.setdefault(lock_key, threading.RLock())
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(lock_path), flags, 0o600)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PathSafetyError("草稿锁必须是真实普通文件")
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


def _issue_tree_evidence_sha256(archive_root, source, day):
    """Hash the issue tree and any referenced imported source PDF."""
    archive_session = _active_archive_session()
    if archive_session is None:
        # All evidence reads, including callers outside a publisher operation,
        # are rooted at one pinned directory inode.  This prevents a
        # swap-read-swap-back attack from supplying bytes from another tree.
        try:
            with _pinned_directory(
                    archive_root, "归档根目录") as pinned_archive:
                _PUBLISH_IO_CONTEXT.archive_session = pinned_archive
                try:
                    return _issue_tree_evidence_sha256(
                        archive_root, source, day
                    )
                finally:
                    try:
                        del _PUBLISH_IO_CONTEXT.archive_session
                    except AttributeError:
                        pass
        except (PublisherError, OSError, UnicodeError, ValueError):
            return None
    archive_session = _active_archive_session()
    if archive_session is not None:
        issue_relative = os.path.join(str(source), str(day))
        digest = hashlib.sha256()
        digest.update(b"readdaily-publish-evidence-v1\0")
        saw_issue = [False]
        issue_bytes = [None]

        def visit(directory_relative, output_prefix):
            names = _archive_list_directory(
                archive_session, directory_relative
            )
            if names is None:
                return False
            directories = []
            files = []
            with _archive_directory_handle(
                    archive_session, directory_relative) as handle:
                if handle is None:
                    return False
                for name in names:
                    try:
                        info = os.stat(
                            name,
                            dir_fd=handle["parent_fd"],
                            follow_symlinks=False,
                        )
                    except OSError:
                        return False
                    if stat.S_ISLNK(info.st_mode):
                        return False
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(name)
                    elif stat.S_ISREG(info.st_mode):
                        files.append((name, info))
                    else:
                        return False
                for name, before in files:
                    relative = (
                        (output_prefix + "/" if output_prefix else "") + name
                    )
                    if relative == "issue.json":
                        saw_issue[0] = True
                    try:
                        raw = _read_regular_at(handle["parent_fd"], name)
                    except (PublisherError, OSError):
                        return False
                    if len(raw) != before.st_size:
                        return False
                    if relative == "issue.json":
                        issue_bytes[0] = raw
                    relative_bytes = relative.encode("utf-8")
                    digest.update(len(relative_bytes).to_bytes(8, "big"))
                    digest.update(relative_bytes)
                    digest.update(before.st_size.to_bytes(8, "big"))
                    digest.update(raw)
            for name in directories:
                child_relative = os.path.join(directory_relative, name)
                child_prefix = (
                    (output_prefix + "/" if output_prefix else "") + name
                )
                if not visit(child_relative, child_prefix):
                    return False
            return True

        try:
            complete = visit(issue_relative, "")
            if not complete or not saw_issue[0] or issue_bytes[0] is None:
                return None
            issue = json.loads(issue_bytes[0].decode("utf-8"))
            if not isinstance(issue, dict):
                return None
            files = issue.get("files")
            files = files if isinstance(files, dict) else {}
            pdf_reference = files.get("local_pdf") or issue.get("pdf_path")
            if pdf_reference:
                reference = os.path.expanduser(str(pdf_reference))
                if not os.path.isabs(reference):
                    reference = os.path.join(
                        archive_session["configured_path"], reference
                    )
                pdf_relative = _session_relative_path(
                    archive_session, reference
                )
                if not pdf_relative:
                    return None
                pdf_evidence = _archive_regular_sha256(
                    archive_session, pdf_relative
                )
                if pdf_evidence is None:
                    return None
                pdf_sha256, _pdf_size = pdf_evidence
                recorded_sha256 = issue.get("source_sha256")
                if (not isinstance(recorded_sha256, str)
                        or not re.fullmatch(
                            r"[a-f0-9]{64}", recorded_sha256.lower()
                        )
                        or not hmac.compare_digest(
                            pdf_sha256, recorded_sha256.lower()
                        )):
                    return None
                # Keep the established v1 combined digest byte-for-byte
                # compatible. issue.json already commits the recorded source
                # hash and PDF path; the pinned streaming pass above proves
                # those recorded bytes still match the actual imported PDF.
        except (PublisherError, OSError, UnicodeError, ValueError):
            return None
        return digest.hexdigest()

def validate_vault_root(vault_root):
    """Return the canonical Vault root after checking its Obsidian marker."""
    active = _active_vault_session()
    configured = os.path.abspath(os.path.expanduser(str(vault_root)))
    if active is not None and configured == active["configured_path"]:
        _verify_vault_session(active)
        return Path(active["canonical_path"])
    with _pinned_directory(vault_root, "Vault 根目录") as pinned:
        try:
            marker_fd = os.open(
                ".obsidian", _directory_open_flags(),
                dir_fd=pinned["root_fd"],
            )
        except OSError as exc:
            raise PathSafetyError("Vault 根路径缺少真实的 .obsidian 目录") from exc
        else:
            os.close(marker_fd)
        _verify_pinned_directory(pinned, "Vault 根目录")
        return Path(pinned["canonical_path"])


def _canonical_archive_root(archive_root):
    active = getattr(_PUBLISH_IO_CONTEXT, "archive_session", None)
    if (active is not None
            and os.path.abspath(os.path.expanduser(str(archive_root)))
            == active["configured_path"]):
        _verify_pinned_directory(active, "归档根目录")
        return active["canonical_path"]
    return os.path.realpath(
        os.path.abspath(os.path.expanduser(str(archive_root)))
    )


def _configured_publisher_state_root():
    configured = os.environ.get("READDAILY_PUBLISHER_STATE_ROOT")
    root = Path(configured).expanduser() if configured else Path(
        DEFAULT_PUBLISHER_STATE_ROOT
    )
    return os.path.abspath(str(root))


def _reject_lexical_state_overlap(state_path, vault_session):
    for vault_path in (
            vault_session["configured_path"],
            vault_session["canonical_path"]):
        try:
            common = os.path.commonpath([state_path, vault_path])
        except ValueError as exc:
            raise PathSafetyError(
                "持久发布事务目录与 Vault 无法安全比较"
            ) from exc
        if common in (state_path, vault_path):
            raise PathSafetyError(
                "持久发布事务目录必须与 Vault 完全分离"
            )


@contextlib.contextmanager
def _publisher_state_session(vault_root):
    configured = _configured_publisher_state_root()
    active_vault = _active_vault_session()
    if active_vault is not None:
        # Sentinel recovery must remain possible when the managed target tree
        # itself is the object that became uncertain.  Bind only the Vault
        # root identity here; target-tree trust is enforced by Vault mutations.
        _verify_pinned_directory(active_vault, "Vault 根目录")
        _reject_lexical_state_overlap(configured, active_vault)
        with _pinned_directory(
                configured, "持久发布事务目录", create=True,
                mode=0o700) as state_session:
            _reject_overlapping_roots(
                state_session,
                active_vault,
                "持久发布事务目录必须与 Vault 完全分离",
            )
            yield state_session, active_vault["canonical_path"]
        return
    # Reject obvious nesting before a missing state path can be created.  The
    # subsequent no-follow walks bind both roots and catch ancestor swaps.
    vault_configured = os.path.abspath(os.path.expanduser(str(vault_root)))
    try:
        common = os.path.commonpath([configured, vault_configured])
    except ValueError as exc:
        raise PathSafetyError(
            "持久发布事务目录与 Vault 无法安全比较"
        ) from exc
    if common in (configured, vault_configured):
        raise PathSafetyError("持久发布事务目录必须与 Vault 完全分离")
    with _pinned_directory(
            configured, "持久发布事务目录", create=True,
            mode=0o700) as state_session:
        with _pinned_directory(vault_root, "Vault 根目录") as vault_session:
            try:
                marker_fd = os.open(
                    ".obsidian",
                    _directory_open_flags(),
                    dir_fd=vault_session["root_fd"],
                )
            except OSError as exc:
                raise PathSafetyError(
                    "Vault 根路径缺少真实的 .obsidian 目录"
                ) from exc
            else:
                os.close(marker_fd)
            _reject_overlapping_roots(
                state_session,
                vault_session,
                "持久发布事务目录必须与 Vault 完全分离",
            )
            yield state_session, vault_session["canonical_path"]


def _publisher_state_root(vault_root=None):
    if vault_root is None:
        configured = _configured_publisher_state_root()
        with _pinned_directory(
                configured, "持久发布事务目录", create=True,
                mode=0o700) as state_session:
            return Path(state_session["canonical_path"])
    with _publisher_state_session(vault_root) as (state_session, _vault):
        return Path(state_session["canonical_path"])


def _vault_sentinel_path(vault_root):
    with _publisher_state_session(vault_root) as (
            state_session, canonical_vault):
        key = hashlib.sha256(canonical_vault.encode("utf-8")).hexdigest()
        return (
            Path(state_session["canonical_path"]) / (key + ".json"),
            canonical_vault,
        )


def _load_vault_sentinel(vault_root, state_session=None,
                         canonical_vault=None):
    if state_session is None:
        with _publisher_state_session(vault_root) as (
                opened_state, opened_vault):
            return _load_vault_sentinel(
                vault_root,
                state_session=opened_state,
                canonical_vault=opened_vault,
            )
    key = hashlib.sha256(canonical_vault.encode("utf-8")).hexdigest()
    name = key + ".json"
    path = Path(state_session["canonical_path"]) / name
    try:
        raw = _archive_read_bytes(state_session, name, missing_ok=True)
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("无法读取持久发布事务哨兵：%s" % exc) from exc
    if raw is None:
        return path, None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RecoveryError("持久发布事务哨兵损坏，需人工恢复") from exc
    if not isinstance(record, dict):
        raise RecoveryError("持久发布事务哨兵损坏，需人工恢复")
    valid = (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("vault_root") == canonical_vault
        and isinstance(record.get("archive_root"), str)
        and os.path.isabs(record.get("archive_root"))
        and re.fullmatch(r"[a-f0-9]{32}", str(record.get("transaction_id") or ""))
        and _ID_RE.fullmatch(str(record.get("plan_id") or ""))
        and record.get("phase") in _ALL_SENTINEL_PHASES
    )
    if not valid:
        raise RecoveryError("持久发布事务哨兵损坏，需人工恢复")
    return path, record


def _sentinel_owner_matches(record, archive_root, plan_id, transaction_id):
    return (
        record.get("archive_root") == _canonical_archive_root(archive_root)
        and record.get("plan_id") == plan_id
        and record.get("transaction_id") == transaction_id
    )


def _claim_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id, phase,
        allowed_existing_phases):
    if phase not in _ALL_SENTINEL_PHASES:
        raise ValueError("未知发布哨兵阶段：%s" % phase)
    try:
        with _publisher_state_session(vault_root) as (
                state_session, canonical_vault):
            path, existing = _load_vault_sentinel(
                vault_root,
                state_session=state_session,
                canonical_vault=canonical_vault,
            )
            if existing is not None and (
                    not _sentinel_owner_matches(
                        existing, archive_root, plan_id, transaction_id
                    )
                    or existing.get("phase") not in set(
                        allowed_existing_phases
                    )):
                raise RecoveryError(
                    "Vault 存在未完成发布事务 %s（%s）；只能先恢复原事务"
                    % (existing.get("transaction_id"), existing.get("phase"))
                )
            record = {
                "schema_version": SCHEMA_VERSION,
                "vault_root": canonical_vault,
                "archive_root": _canonical_archive_root(archive_root),
                "plan_id": plan_id,
                "transaction_id": transaction_id,
                "phase": phase,
                "updated_at": _now(),
            }
            raw = json.dumps(
                record, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            _atomic_write_session_bytes(state_session, path.name, raw)
    except RecoveryError:
        raise
    except PublisherError as exc:
        raise RecoveryError("无法持久化发布事务哨兵：%s" % exc) from exc
    return record


def _guard_vault_for_apply(
        archive_root, vault_root, plan_id, vault_session=None):
    _path, existing = _load_vault_sentinel(vault_root)
    if existing is None:
        return None
    if (existing.get("archive_root") == _canonical_archive_root(archive_root)
            and existing.get("plan_id") == plan_id
            and existing.get("phase") in _APPLY_SENTINEL_PHASES):
        try:
            _tx_dir, manifest = _load_transaction(
                archive_root, existing["transaction_id"]
            )
        except PublisherError as exc:
            raise RecoveryError(
                "持久发布事务哨兵找不到原事务，需人工恢复"
            ) from exc
        if manifest.get("status") == "failed_restored":
            entries = _validate_manifest(manifest, vault_root)
            states = [
                _entry_state(
                    vault_root, entry, vault_session=vault_session
                )
                for entry in entries
            ]
            if not all(state in ("before", "unchanged") for state in states):
                raise RecoveryError(
                    "已恢复事务的 Vault 内容不再等于发布前状态，拒绝清除哨兵"
                )
            _clear_vault_sentinel(
                archive_root,
                vault_root,
                plan_id,
                existing["transaction_id"],
            )
            return None
        return existing
    raise RecoveryError(
        "Vault 存在未完成发布事务 %s（%s）；只能先恢复原事务"
        % (existing.get("transaction_id"), existing.get("phase"))
    )


def _guard_vault_for_rollback(
        archive_root, vault_root, plan_id, transaction_id):
    _path, existing = _load_vault_sentinel(vault_root)
    if existing is None:
        return None
    if (_sentinel_owner_matches(
            existing, archive_root, plan_id, transaction_id)
            and existing.get("phase") in _ROLLBACK_SENTINEL_PHASES):
        return existing
    raise RecoveryError(
        "Vault 存在未完成发布事务 %s（%s）；只能先恢复原事务"
        % (existing.get("transaction_id"), existing.get("phase"))
    )


def _clear_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id):
    try:
        with _publisher_state_session(vault_root) as (
                state_session, canonical_vault):
            path, existing = _load_vault_sentinel(
                vault_root,
                state_session=state_session,
                canonical_vault=canonical_vault,
            )
            if existing is None:
                return
            if not _sentinel_owner_matches(
                    existing, archive_root, plan_id, transaction_id):
                raise RecoveryError(
                    "持久发布事务哨兵所有权不一致，拒绝清除"
                )
            _verify_pinned_directory(
                state_session, "持久发布事务目录"
            )
            try:
                info = os.stat(
                    path.name,
                    dir_fd=state_session["root_fd"],
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise PathSafetyError(
                        "持久发布事务哨兵必须是普通文件"
                    )
                os.unlink(path.name, dir_fd=state_session["root_fd"])
                os.fsync(state_session["root_fd"])
                _verify_pinned_directory(
                    state_session, "持久发布事务目录"
                )
            except OSError as exc:
                raise RecoveryError(
                    "发布已完成，但持久事务哨兵无法清除：%s" % exc
                ) from exc
    except RecoveryError:
        raise
    except PublisherError as exc:
        raise RecoveryError(
            "发布已完成，但持久事务哨兵无法清除：%s" % exc
        ) from exc


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
    if os.path.islink(target):
        raise PathSafetyError("Vault 最终目标不能是符号链接")

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


def _vault_relative_parts(relative_path):
    rel = str(relative_path)
    if os.path.isabs(rel):
        raise PathSafetyError("Vault 目标必须是相对路径")
    normal = os.path.normpath(rel)
    parts = normal.split(os.sep)
    if (len(parts) < 2 or parts[0] != TARGET_FOLDER
            or any(part in ("", ".", "..") for part in parts)):
        raise PathSafetyError("目标只能位于 %s" % TARGET_FOLDER)
    return parts


def _inode_identity(info):
    return info.st_dev, info.st_ino


def _verify_vault_session(session):
    try:
        opened = os.fstat(session["root_fd"])
    except OSError as exc:
        raise PathSafetyError("Vault 根目录在操作期间被替换") from exc
    _verify_pinned_directory(session, "Vault 根目录")
    if (not stat.S_ISDIR(opened.st_mode)
            or _inode_identity(opened) != session["root_identity"]):
        raise PathSafetyError("Vault 根目录在操作期间被替换")
    target_fd = session.get("target_fd")
    target_identity = session.get("target_identity")
    try:
        target_link = os.stat(
            TARGET_FOLDER,
            dir_fd=session["root_fd"],
            follow_symlinks=False,
        )
    except FileNotFoundError:
        target_link = None
    except OSError as exc:
        raise PathSafetyError("Vault 目标根目录无法安全检查") from exc
    if target_fd is None:
        if target_link is not None:
            raise PathSafetyError("Vault 目标根目录在操作期间意外出现")
        return
    try:
        target_opened = os.fstat(target_fd)
    except OSError as exc:
        raise PathSafetyError("Vault 目标根目录句柄失效") from exc
    if (target_link is None
            or not stat.S_ISDIR(target_opened.st_mode)
            or not stat.S_ISDIR(target_link.st_mode)
            or _inode_identity(target_opened) != target_identity
            or _inode_identity(target_link) != target_identity):
        raise PathSafetyError("Vault 目标根目录在操作期间被替换")


@contextlib.contextmanager
def _vault_directory_session(vault_root):
    with _pinned_directory(vault_root, "Vault 根目录") as pinned:
        root_fd = pinned["root_fd"]
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise PathSafetyError("Vault 根路径不是可信目录")
        session = {
            "configured_path": pinned["configured_path"],
            "canonical_path": pinned["canonical_path"],
            "configured_links": pinned["configured_links"],
            "root_fd": root_fd,
            "root_identity": _inode_identity(root_stat),
            "target_fd": None,
            "target_identity": None,
        }
        try:
            try:
                marker_fd = os.open(
                    ".obsidian", _directory_open_flags(), dir_fd=root_fd
                )
            except OSError as exc:
                raise PathSafetyError("Vault 的 .obsidian 目录不可信") from exc
            else:
                os.close(marker_fd)
            try:
                target_fd = os.open(
                    TARGET_FOLDER, _directory_open_flags(), dir_fd=root_fd
                )
            except FileNotFoundError:
                target_fd = None
            except OSError as exc:
                raise PathSafetyError("Vault 目标根目录不可信") from exc
            if target_fd is not None:
                target_info = os.fstat(target_fd)
                if not stat.S_ISDIR(target_info.st_mode):
                    os.close(target_fd)
                    raise PathSafetyError("Vault 目标根目录不是目录")
                session["target_fd"] = target_fd
                session["target_identity"] = _inode_identity(target_info)
            _verify_vault_session(session)
            yield session
        finally:
            if session.get("target_fd") is not None:
                os.close(session["target_fd"])


def _vault_target_root_fd(session, create):
    _verify_vault_session(session)
    if session.get("target_fd") is not None:
        return session["target_fd"]
    if not create:
        return None
    try:
        os.mkdir(TARGET_FOLDER, 0o755, dir_fd=session["root_fd"])
    except FileExistsError as exc:
        raise PathSafetyError(
            "Vault 目标根目录在创建前由其他进程加入"
        ) from exc
    except OSError as exc:
        raise PublisherError("无法创建 Vault 目标根目录：%s" % exc) from exc
    try:
        os.fsync(session["root_fd"])
    except OSError as exc:
        raise RecoveryError(
            "Vault 目标根目录已创建但无法确认持久化：%s" % exc
        ) from exc
    try:
        target_fd = os.open(
            TARGET_FOLDER,
            _directory_open_flags(),
            dir_fd=session["root_fd"],
        )
    except OSError as exc:
        raise PathSafetyError("Vault 新建目标根目录被替换") from exc
    target_info = os.fstat(target_fd)
    if not stat.S_ISDIR(target_info.st_mode):
        os.close(target_fd)
        raise PathSafetyError("Vault 新建目标根路径不是目录")
    session["target_fd"] = target_fd
    session["target_identity"] = _inode_identity(target_info)
    _verify_vault_session(session)
    return target_fd


def _verify_vault_parent(handle):
    _verify_vault_session(handle["session"])
    for parent_fd, name, child_fd, identity in handle["links"]:
        try:
            opened = os.fstat(child_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise PathSafetyError("Vault 目标祖先目录在操作期间被替换") from exc
        if (not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or _inode_identity(opened) != identity
                or _inode_identity(linked) != identity):
            raise PathSafetyError("Vault 目标祖先目录在操作期间被替换")


@contextlib.contextmanager
def _vault_target_parent(session, relative_path, create=False):
    parts = _vault_relative_parts(relative_path)
    descriptors = []
    target_root_fd = _vault_target_root_fd(session, create)
    links = []
    current = target_root_fd
    missing_parent = False
    try:
        if target_root_fd is None:
            yield None
            return
        links.append((
            session["root_fd"],
            TARGET_FOLDER,
            target_root_fd,
            session["target_identity"],
        ))
        for name in parts[1:-1]:
            try:
                child = os.open(
                    name, _directory_open_flags(), dir_fd=current
                )
            except FileNotFoundError:
                if not create:
                    missing_parent = True
                    break
                try:
                    os.mkdir(name, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise PublisherError("无法创建 Vault 目录 %s：%s" % (name, exc)) from exc
                try:
                    os.fsync(current)
                except OSError as exc:
                    raise RecoveryError(
                        "Vault 目录 %s 已创建但无法确认持久化：%s"
                        % (name, exc)
                    ) from exc
                try:
                    child = os.open(
                        name, _directory_open_flags(), dir_fd=current
                    )
                except OSError as exc:
                    raise PathSafetyError("Vault 新建目录被替换：%s" % name) from exc
            except OSError as exc:
                raise PathSafetyError("Vault 目标祖先不是可信目录：%s" % name) from exc
            child_stat = os.fstat(child)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child)
                raise PathSafetyError("Vault 目标祖先不是目录：%s" % name)
            identity = _inode_identity(child_stat)
            descriptors.append(child)
            links.append((current, name, child, identity))
            current = child
        if missing_parent:
            yield None
        else:
            handle = {
                "session": session,
                "links": links,
                "parent_fd": current,
                "name": parts[-1],
                "relative_path": os.sep.join(parts),
            }
            _verify_vault_parent(handle)
            yield handle
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _vault_target_stat(handle):
    try:
        info = os.stat(
            handle["name"], dir_fd=handle["parent_fd"],
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PathSafetyError("Vault 目标无法安全检查") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PathSafetyError("Vault 最终目标必须是普通文件且不能是符号链接")
    return info


def _read_vault_target(session, relative_path):
    with _vault_target_parent(session, relative_path, create=False) as handle:
        if handle is None:
            return b"", False
        _verify_vault_parent(handle)
        expected = _vault_target_stat(handle)
        if expected is None:
            _verify_vault_parent(handle)
            return b"", False
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                handle["name"], flags, dir_fd=handle["parent_fd"]
            )
        except OSError as exc:
            raise PathSafetyError("Vault 目标无法安全读取") from exc
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or _inode_identity(opened) != _inode_identity(expected)):
                raise PathSafetyError("Vault 目标在读取前被替换")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            linked_after = _vault_target_stat(handle)
            if (linked_after is None
                    or _inode_identity(after) != _inode_identity(opened)
                    or _inode_identity(linked_after) != _inode_identity(opened)
                    or after.st_size != opened.st_size
                    or after.st_mtime_ns != opened.st_mtime_ns):
                raise ConflictError("Vault 目标在读取期间发生变化")
        finally:
            os.close(descriptor)
        _verify_vault_parent(handle)
        return b"".join(chunks), True


_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_RENAME_NOFOLLOW_ANY = 0x00000010


def _renameatx_np(source_fd, source, destination_fd, destination, flags):
    """Use Darwin's atomic rename extensions or fail closed."""
    if sys.platform != "darwin":
        raise PublisherError("当前平台不支持安全的 Vault 原子内容比较交换")
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except (AttributeError, OSError) as exc:
        raise PublisherError("当前系统缺少安全的 Vault 原子重命名能力") from exc
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _read_regular_at(parent_fd, name, missing_ok=False):
    """Read one directory entry without following it or accepting a swap."""
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ConflictError("Vault 目录项在内容校验前消失：%s" % name)
    except OSError as exc:
        raise PathSafetyError("Vault 目录项无法安全检查：%s" % name) from exc
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise PathSafetyError("Vault 目录项必须是普通文件：%s" % name)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PathSafetyError("Vault 目录项无法安全打开：%s" % name) from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or _inode_identity(opened) != _inode_identity(expected)):
            raise ConflictError("Vault 目录项在内容校验前被替换：%s" % name)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            linked_after = os.stat(
                name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError as exc:
            raise ConflictError(
                "Vault 目录项在内容校验期间消失：%s" % name
            ) from exc
        if (not stat.S_ISREG(linked_after.st_mode)
                or _inode_identity(after) != _inode_identity(opened)
                or _inode_identity(linked_after) != _inode_identity(opened)
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns):
            raise ConflictError("Vault 目录项在内容校验期间发生变化：%s" % name)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_vault_parent(handle, message):
    try:
        _verify_vault_parent(handle)
        descriptors = [link[0] for link in handle["links"]]
        descriptors.append(handle["parent_fd"])
        seen = set()
        for descriptor in descriptors:
            if descriptor in seen:
                continue
            seen.add(descriptor)
            os.fsync(descriptor)
        _verify_vault_parent(handle)
    except OSError as exc:
        raise PublisherError("%s：%s" % (message, exc)) from exc


def _swap_back_after_cas_conflict(handle, temporary_name, after_hash):
    """Restore the displaced entry and delete only our unchanged payload."""
    try:
        _verify_vault_parent(handle)
        _renameatx_np(
            handle["parent_fd"],
            temporary_name,
            handle["parent_fd"],
            handle["name"],
            _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
        )
        _fsync_vault_parent(handle, "Vault CAS 冲突恢复无法持久化")
    except Exception as exc:
        raise RecoveryError(
            "Vault CAS 冲突后无法原子恢复；已保留目录项 %s：%s"
            % (temporary_name, exc)
        ) from exc
    try:
        displaced = _read_regular_at(
            handle["parent_fd"], temporary_name, missing_ok=False
        )
    except Exception as exc:
        raise RecoveryError(
            "Vault CAS 冲突恢复后无法确认临时目录项；已保留 %s：%s"
            % (temporary_name, exc)
        ) from exc
    if not hmac.compare_digest(_hash_bytes(displaced), after_hash):
        raise RecoveryError(
            "Vault CAS 交换期间目标再次变化；已保留并发内容于 %s"
            % temporary_name
        )
    try:
        os.unlink(temporary_name, dir_fd=handle["parent_fd"])
        _fsync_vault_parent(handle, "Vault CAS 临时目录项清理无法持久化")
    except OSError as exc:
        raise RecoveryError(
            "Vault CAS 冲突已恢复，但临时目录项 %s 待清理：%s"
            % (temporary_name, exc)
        ) from exc


def _atomic_write_vault_target(target, raw):
    if not isinstance(raw, bytes):
        raise PublisherError("Vault 原子写入只接受字节内容")
    if not _ID_RE.fullmatch(target.before_hash):
        raise ConflictError("Vault CAS before hash 非法")
    if (not _ID_RE.fullmatch(target.after_hash)
            or not hmac.compare_digest(_hash_bytes(raw), target.after_hash)):
        raise ConflictError("Vault CAS after hash 与写入内容不一致")
    session = target.session
    with _vault_target_parent(
            session, target.relative_path, create=True) as handle:
        _verify_vault_parent(handle)
        temporary_name = ".readdaily-%s" % uuid.uuid4().hex
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_exists = False
        temporary_is_generated = True
        try:
            descriptor = os.open(
                temporary_name, flags, 0o600, dir_fd=handle["parent_fd"]
            )
            temporary_exists = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _verify_vault_parent(handle)
            existing = _vault_target_stat(handle)
            if not target.before_exists:
                if existing is not None:
                    raise ConflictError(
                        "Vault 新文件在写入前已由其他进程创建：%s"
                        % target.relative_path
                    )
                try:
                    os.link(
                        temporary_name,
                        handle["name"],
                        src_dir_fd=handle["parent_fd"],
                        dst_dir_fd=handle["parent_fd"],
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise ConflictError(
                        "Vault 新文件在提交时已由其他进程创建：%s"
                        % target.relative_path
                    ) from exc
                linked = _read_regular_at(
                    handle["parent_fd"], handle["name"], missing_ok=False
                )
                if not hmac.compare_digest(_hash_bytes(linked), target.after_hash):
                    raise ConflictError(
                        "Vault 新文件在原子提交后发生变化：%s"
                        % target.relative_path
                    )
                _fsync_vault_parent(handle, "Vault 新文件提交无法持久化")
                os.unlink(temporary_name, dir_fd=handle["parent_fd"])
                temporary_exists = False
                _fsync_vault_parent(handle, "Vault 临时目录项清理无法持久化")
                return

            if existing is None:
                raise ConflictError(
                    "Vault 文件在原子提交前消失：%s" % target.relative_path
                )
            # The swap is the content CAS linearization point.  It atomically
            # moves the exact prior inode to our temporary name so its digest
            # can be checked after the replacement becomes visible.
            _renameatx_np(
                handle["parent_fd"],
                temporary_name,
                handle["parent_fd"],
                handle["name"],
                _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
            )
            temporary_is_generated = False
            prior = None
            live_after = None
            try:
                prior = _read_regular_at(
                    handle["parent_fd"], temporary_name, missing_ok=False
                )
                live_after = _read_regular_at(
                    handle["parent_fd"], handle["name"], missing_ok=False
                )
            except Exception:
                # Both names may now contain user data; preserve them unless
                # the guarded reverse swap proves our payload is disposable.
                try:
                    _swap_back_after_cas_conflict(
                        handle, temporary_name, target.after_hash
                    )
                    temporary_exists = False
                    temporary_is_generated = True
                except Exception:
                    temporary_exists = False
                    raise
                raise
            prior_matches = hmac.compare_digest(
                _hash_bytes(prior), target.before_hash
            )
            after_matches = hmac.compare_digest(
                _hash_bytes(live_after), target.after_hash
            )
            if not prior_matches or not after_matches:
                try:
                    _swap_back_after_cas_conflict(
                        handle, temporary_name, target.after_hash
                    )
                    temporary_exists = False
                    temporary_is_generated = True
                except Exception:
                    temporary_exists = False
                    raise
                if not prior_matches:
                    raise ConflictError(
                        "Vault 文件在原子提交前已被修改：%s"
                        % target.relative_path
                    )
                raise ConflictError(
                    "Vault 文件在原子提交期间再次变化：%s"
                    % target.relative_path
                )
            _fsync_vault_parent(handle, "Vault 原子交换无法持久化")
            try:
                os.unlink(temporary_name, dir_fd=handle["parent_fd"])
            except OSError as exc:
                # The old inode is an expected snapshot and can safely remain
                # for recovery, but do not pretend the transaction committed.
                temporary_exists = False
                raise RecoveryError(
                    "Vault 旧版本临时目录项 %s 待清理：%s"
                    % (temporary_name, exc)
                ) from exc
            temporary_exists = False
            _fsync_vault_parent(handle, "Vault 旧版本清理无法持久化")
            return
        except (PublisherError, RecoveryError):
            raise
        except OSError as exc:
            raise PublisherError("Vault 原子写入失败：%s" % exc) from exc
        finally:
            if temporary_exists and temporary_is_generated:
                try:
                    os.unlink(temporary_name, dir_fd=handle["parent_fd"])
                    os.fsync(handle["parent_fd"])
                except OSError:
                    pass


def _durable_unlink_vault_target(session, relative_path, expected_hash):
    if not _ID_RE.fullmatch(str(expected_hash or "")):
        raise ConflictError("Vault 删除 CAS expected hash 非法")
    with _vault_target_parent(session, relative_path, create=False) as handle:
        if handle is None:
            return
        _verify_vault_parent(handle)
        existing = _vault_target_stat(handle)
        if existing is None:
            _fsync_vault_parent(handle, "回滚删除状态无法持久化")
            return
        temporary_name = ".readdaily-removed-%s" % uuid.uuid4().hex
        try:
            _renameatx_np(
                handle["parent_fd"],
                handle["name"],
                handle["parent_fd"],
                temporary_name,
                _RENAME_EXCL | _RENAME_NOFOLLOW_ANY,
            )
            removed = _read_regular_at(
                handle["parent_fd"], temporary_name, missing_ok=False
            )
            if not hmac.compare_digest(_hash_bytes(removed), expected_hash):
                try:
                    _renameatx_np(
                        handle["parent_fd"],
                        temporary_name,
                        handle["parent_fd"],
                        handle["name"],
                        _RENAME_EXCL | _RENAME_NOFOLLOW_ANY,
                    )
                    _fsync_vault_parent(handle, "删除 CAS 冲突恢复无法持久化")
                except Exception as restore_error:
                    raise RecoveryError(
                        "删除 CAS 冲突且目标再次变化；并发内容保留于 %s：%s"
                        % (temporary_name, restore_error)
                    ) from restore_error
                raise ConflictError(
                    "回滚删除前文件已被修改：%s" % relative_path
                )
            _fsync_vault_parent(handle, "回滚删除重命名无法持久化")
            os.unlink(temporary_name, dir_fd=handle["parent_fd"])
            _fsync_vault_parent(handle, "回滚删除无法持久化")
        except (PublisherError, RecoveryError):
            raise
        except OSError as exc:
            raise PublisherError(
                "回滚删除无法持久化：%s" % exc
            ) from exc
        # Once renamed, the temporary entry may contain user data.  There is
        # deliberately no best-effort unlink path for failures above.


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
    # The file identity must not depend on issue metadata that can be corrected
    # after OCR.  Issue number remains visible inside managed content and the
    # index, while one source/date always maps to exactly one daily card.
    return "%s %s摘要" % (
        issue["date"], issue.get("source_name") or issue["source"]
    )


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


def _draft_content_sha256(draft):
    """Hash publish-relevant draft content while ignoring save timestamps."""
    if not isinstance(draft, dict):
        return None
    payload = {
        key: draft.get(key)
        for key in ("schema_version", "source", "date", "evidence_sha256", "units")
    }
    return _hash_bytes(_json_bytes(payload))


def _persisted_draft_path(archive_root, source, day):
    return _safe_archive_path(
        archive_root, "_drafts", str(source), str(day) + ".json"
    )


def _load_persisted_draft(archive_root, source, day):
    draft = _load_json(_persisted_draft_path(
        archive_root, source, day
    ))
    if not isinstance(draft, dict):
        raise ConflictError("当前复核草稿不存在，请先保存草稿再生成发布预览")
    if draft.get("source") != source or draft.get("date") != day:
        raise ConflictError("当前复核草稿的来源或日期不匹配")
    digest = _draft_content_sha256(draft)
    if not isinstance(digest, str) or not _ID_RE.fullmatch(digest):
        raise ConflictError("当前复核草稿无法完整校验")
    return draft, digest


def _require_current_draft_digest(
        archive_root, source, day, expected_digest):
    current_draft, current_digest = _load_persisted_draft(
        archive_root, source, day
    )
    if (not isinstance(expected_digest, str)
            or not _ID_RE.fullmatch(expected_digest)
            or not hmac.compare_digest(current_digest, expected_digest)):
        raise ConflictError("复核草稿在发布预览后发生变化，请重新生成发布预览")
    return current_draft, current_digest


def _daily_generated(issue, draft):
    drafted = _draft_by_id(draft)
    lines = [
        "## 工作台归档",
        "",
        "- 来源：%s" % (issue.get("source_name") or issue["source"]),
        "- 日期：%s" % issue["date"],
        "- 期号：%s" % (issue.get("issue_no") or "待人工核对"),
        "- 归档状态：已由 Read Daily 复核并发布",
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
    _safe_target(vault_root, relative_path)
    session = _active_vault_session()
    if session is not None:
        before_raw, before_exists = _read_vault_target(session, relative_path)
    else:
        with _vault_directory_session(vault_root) as session:
            before_raw, before_exists = _read_vault_target(
                session, relative_path
            )
    try:
        existing = before_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublisherError(
            "目标 Markdown 不是 UTF-8：%s" % relative_path
        ) from exc
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
        "before_exists": before_exists,
        "before_hash": _hash_bytes(before_raw),
        "after_hash": _hash_bytes(after_raw),
        "after": after,
        "diff": diff,
    }


def _thread_lock_for_vault(vault_identity):
    with _PUBLISH_THREAD_LOCKS_GUARD:
        lock = _PUBLISH_THREAD_LOCKS.get(vault_identity)
        if lock is None:
            lock = threading.Lock()
            _PUBLISH_THREAD_LOCKS[vault_identity] = lock
        return lock


@contextlib.contextmanager
def _publisher_transaction_lock(archive_root, vault_root):
    """Serialize every publisher mutation for one canonical Vault.

    The persistent lock file is only an inode used by ``flock``; ownership is
    held by the open descriptor, so a process crash releases it automatically.
    Vault identity alone is used as the key so two archive roots cannot mutate
    the same Vault concurrently.
    """
    vault_session = _active_vault_session()
    archive_session = _active_archive_session()
    if vault_session is None or archive_session is None:
        raise PublisherError("发布事务锁缺少固定目录会话")
    _verify_vault_session(vault_session)
    _verify_pinned_directory(archive_session, "归档根目录")
    vault_identity = vault_session["canonical_path"]
    archive_identity = archive_session["canonical_path"]
    lock_key = hashlib.sha256(vault_identity.encode("utf-8")).hexdigest()
    lock_directory = os.path.join(
        tempfile.gettempdir(), "readdaily-publisher-locks"
    )
    try:
        os.makedirs(lock_directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PublisherError("无法创建发布锁目录：%s" % exc) from exc
    if os.path.islink(lock_directory) or not os.path.isdir(lock_directory):
        raise PathSafetyError("发布锁目录必须是真实目录")

    lock_path = os.path.join(lock_directory, lock_key + ".lock")
    thread_lock = _thread_lock_for_vault(vault_identity)
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise PublisherError("无法打开发布事务锁：%s" % exc) from exc
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PathSafetyError("发布事务锁不是普通文件")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            os.ftruncate(descriptor, 0)
            os.write(descriptor, (
                "pid=%s\narchive=%s\nvault=%s\n" % (
                    os.getpid(), archive_identity, vault_identity
                )
            ).encode("utf-8"))
            os.fsync(descriptor)
            yield
        finally:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _serialized_publisher_mutation(function):
    @functools.wraps(function)
    def serialized(archive_root, vault_root, *args, **kwargs):
        with _publisher_operation_io(archive_root, vault_root):
            with _publisher_transaction_lock(archive_root, vault_root):
                return function(archive_root, vault_root, *args, **kwargs)
    return serialized


def _require_verified_local_pdf_date(issue):
    files = issue.get("files")
    has_local_pdf = (
        issue.get("channel") == "local_pdf"
        or (
            isinstance(files, dict)
            and bool(files.get("local_pdf"))
        )
    )
    if (
        has_local_pdf
        and issue.get("local_pdf_date_verification") != "verified"
    ):
        raise PublisherError(
            "本地 PDF 的报纸日期尚未核验，不能创建发布计划"
        )


@_serialized_publisher_mutation
def create_plan(archive_root, vault_root, issue, draft):
    with _fetch_date_evidence_lock(archive_root, issue.get("date")):
        with _draft_rmw_lock(
                archive_root, issue.get("source"), issue.get("date")):
            return _create_plan_locked(
                archive_root, vault_root, issue, draft
            )


def _create_plan_locked(archive_root, vault_root, issue, draft):
    """Build and persist a deterministic plan without modifying the Vault."""
    vault_root = validate_vault_root(vault_root)
    if issue.get("source") != draft.get("source") or issue.get("date") != draft.get("date"):
        raise PublisherError("草稿与报纸来源/日期不一致")
    source = str(issue["source"])
    day = str(issue["date"])
    if source != SUPPORTED_SOURCE:
        raise PublisherError("建设主题知识库仅允许发布中国建设报（zgjsb）")
    _reject_replan_with_pending_transaction(archive_root, vault_root, source, day)
    persisted_draft, draft_sha256 = _load_persisted_draft(
        archive_root, source, day
    )
    supplied_draft_sha256 = _draft_content_sha256(draft)
    if (not isinstance(supplied_draft_sha256, str)
            or not hmac.compare_digest(
                supplied_draft_sha256, draft_sha256
            )):
        raise ConflictError("复核草稿已变化，请重新打开并生成发布预览")
    # Persisted content is the sole publication input. ``saved_at`` and other
    # non-publish metadata may differ without changing the canonical digest.
    draft = persisted_draft
    archive_evidence_sha256 = _issue_tree_evidence_sha256(
        archive_root, source, day
    )
    if not archive_evidence_sha256:
        raise PublisherError("报纸原始证据目录缺失或无法完整校验")
    issue_evidence = str(issue.get("evidence_sha256") or "")
    draft_evidence = str(draft.get("evidence_sha256") or "")
    if (not _ID_RE.fullmatch(issue_evidence)
            or not _ID_RE.fullmatch(draft_evidence)
            or not hmac.compare_digest(issue_evidence, archive_evidence_sha256)
            or not hmac.compare_digest(draft_evidence, archive_evidence_sha256)):
        raise ConflictError("报纸或草稿证据已变化，请重新打开、复核并生成发布预览")
    _require_verified_local_pdf_date(issue)
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
        "archive_evidence_sha256": archive_evidence_sha256,
        "draft_sha256": draft_sha256,
        "pipeline_version": PIPELINE_VERSION,
        "template_version": TEMPLATE_VERSION,
    }))
    basis = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "date": day,
        "issue_no": issue.get("issue_no"),
        "source_sha256": source_sha256,
        "archive_evidence_sha256": archive_evidence_sha256,
        "draft_sha256": draft_sha256,
        "pipeline_version": PIPELINE_VERSION,
        "template_version": TEMPLATE_VERSION,
        "idempotency_key": idempotency_key,
        "draft": draft,
        "changes": [{
            k: c[k]
            for k in (
                "relative_path", "before_exists", "before_hash", "after_hash"
            )
        }
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
    if _issue_tree_evidence_sha256(archive_root, source, day) != archive_evidence_sha256:
        raise ConflictError("报纸证据在生成发布预览期间发生变化，请重新预览")
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
        if not isinstance(change.get("before_exists"), bool):
            raise ConflictError("发布计划 before_exists 损坏")
        if _hash_text(change["after"]) != change.get("after_hash"):
            raise ConflictError("发布计划内容哈希不一致")
    basis = {
        "schema_version": plan.get("schema_version"),
        "source": plan.get("source"),
        "date": plan.get("date"),
        "issue_no": plan.get("issue_no"),
        "source_sha256": plan.get("source_sha256"),
        "archive_evidence_sha256": plan.get("archive_evidence_sha256"),
        "draft_sha256": plan.get("draft_sha256"),
        "pipeline_version": plan.get("pipeline_version"),
        "template_version": plan.get("template_version"),
        "idempotency_key": plan.get("idempotency_key"),
        "draft": plan.get("draft"),
        "changes": [
            {
                key: change.get(key)
                for key in (
                    "relative_path", "before_exists", "before_hash", "after_hash"
                )
            }
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
    state["publish_archive_evidence_sha256"] = plan[
        "archive_evidence_sha256"
    ]
    state["publish_draft_sha256"] = plan["draft_sha256"]
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
    state.pop("publish_archive_evidence_sha256", None)
    state.pop("publish_draft_sha256", None)
    state["last_rollback"] = {"transaction_id": transaction_id, "at": _now()}
    _atomic_write_json(state_path, state)


def _restore_entry(
        vault_root, transaction_dir, entry, snapshot_bytes=None,
        vault_session=None):
    if vault_session is None:
        with _vault_directory_session(vault_root) as opened_session:
            return _restore_entry_with_session(
                vault_root,
                transaction_dir,
                entry,
                snapshot_bytes=snapshot_bytes,
                vault_session=opened_session,
            )
    return _restore_entry_with_session(
        vault_root,
        transaction_dir,
        entry,
        snapshot_bytes=snapshot_bytes,
        vault_session=vault_session,
    )


def _restore_entry_with_session(
        vault_root, transaction_dir, entry, snapshot_bytes, vault_session):
    if entry.get("before_exists"):
        if snapshot_bytes is None:
            snapshots = _preflight_restore_snapshots(
                transaction_dir, [entry]
            )
            snapshot_bytes = snapshots[entry["snapshot"]]
        elif _hash_bytes(snapshot_bytes) != entry.get("before_hash"):
            raise ConflictError(
                "事务快照哈希不一致：%s" % entry["relative_path"]
            )
        _atomic_write_bytes(
            _VaultMutationTarget(
                vault_session,
                entry["relative_path"],
                True,
                entry["after_hash"],
                entry["before_hash"],
            ),
            snapshot_bytes,
        )
    else:
        _durable_unlink_vault_target(
            vault_session, entry["relative_path"], entry["after_hash"]
        )


def _preflight_restore_snapshots(transaction_dir, entries):
    """Read and validate every required snapshot before any Vault mutation."""
    required = [entry for entry in entries if entry.get("before_exists")]
    if not required:
        return {}

    archive_session = _active_archive_session()
    if archive_session is not None:
        transaction_relative = _session_relative_path(
            archive_session, transaction_dir
        )
        if transaction_relative is None or not transaction_relative:
            raise PathSafetyError("事务快照目录路径越界")
        snapshots = {}
        for entry in required:
            snapshot_name = str(entry.get("snapshot") or "")
            if not re.fullmatch(r"before/\d{3,6}\.bin", snapshot_name):
                raise PathSafetyError("事务快照路径非法")
            relative = os.path.join(transaction_relative, snapshot_name)
            try:
                raw = _archive_read_bytes(
                    archive_session, relative, missing_ok=False
                )
            except FileNotFoundError as exc:
                raise PublisherError(
                    "事务快照缺失：%s" % entry["relative_path"]
                ) from exc
            if _hash_bytes(raw) != entry.get("before_hash"):
                raise ConflictError(
                    "事务快照哈希不一致：%s" % entry["relative_path"]
                )
            snapshots[snapshot_name] = raw
        return snapshots

    transaction_dir = Path(transaction_dir)
    transaction_path = os.path.abspath(str(transaction_dir))
    if os.path.islink(transaction_path) or not os.path.isdir(transaction_path):
        raise PathSafetyError("发布事务目录必须是真实目录")
    transaction_real = os.path.realpath(transaction_path)
    before_path = os.path.join(transaction_path, "before")
    if os.path.islink(before_path) or not os.path.isdir(before_path):
        raise PathSafetyError("事务快照目录必须是真实目录")
    before_real = os.path.realpath(before_path)
    try:
        if os.path.commonpath([transaction_real, before_real]) != transaction_real:
            raise PathSafetyError("事务快照目录路径越界")
    except ValueError as exc:
        raise PathSafetyError("事务快照目录路径越界") from exc

    snapshots = {}
    for entry in required:
        snapshot_name = str(entry.get("snapshot") or "")
        if not re.fullmatch(r"before/\d{3,6}\.bin", snapshot_name):
            raise PathSafetyError("事务快照路径非法")
        snapshot_path = os.path.abspath(
            os.path.join(transaction_path, snapshot_name)
        )
        try:
            if os.path.commonpath([before_path, snapshot_path]) != before_path:
                raise PathSafetyError("事务快照路径越界")
        except ValueError as exc:
            raise PathSafetyError("事务快照路径越界") from exc
        if os.path.islink(snapshot_path):
            raise PathSafetyError("事务快照不能是符号链接")
        try:
            snapshot_stat = os.lstat(snapshot_path)
        except FileNotFoundError as exc:
            raise PublisherError(
                "事务快照缺失：%s" % entry["relative_path"]
            ) from exc
        except OSError as exc:
            raise PublisherError(
                "事务快照无法读取：%s" % entry["relative_path"]
            ) from exc
        if not stat.S_ISREG(snapshot_stat.st_mode):
            raise PathSafetyError("事务快照必须是真实普通文件")
        snapshot_real = os.path.realpath(snapshot_path)
        try:
            if os.path.commonpath([before_real, snapshot_real]) != before_real:
                raise PathSafetyError("事务快照路径越界")
        except ValueError as exc:
            raise PathSafetyError("事务快照路径越界") from exc
        try:
            raw = Path(snapshot_path).read_bytes()
        except OSError as exc:
            raise PublisherError(
                "事务快照无法读取：%s" % entry["relative_path"]
            ) from exc
        if _hash_bytes(raw) != entry.get("before_hash"):
            raise ConflictError(
                "事务快照哈希不一致：%s" % entry["relative_path"]
            )
        snapshots[snapshot_name] = raw
    return snapshots


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
    archive_session = _active_archive_session()
    if archive_session is not None:
        names = _archive_list_directory(archive_session, "_transactions")
        if names is None:
            return []
        records = []
        with _archive_directory_handle(
                archive_session, "_transactions") as handle:
            if handle is None:
                return []
            for name in names:
                if not re.fullmatch(r"[a-f0-9]{32}", name):
                    continue
                try:
                    info = os.stat(
                        name,
                        dir_fd=handle["parent_fd"],
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise PathSafetyError(
                        "事务目录无法安全检查"
                    ) from exc
                if stat.S_ISLNK(info.st_mode):
                    raise PathSafetyError("事务目录不能是符号链接")
                if not stat.S_ISDIR(info.st_mode):
                    continue
                candidate = tx_root / name
                manifest = _load_json(candidate / "manifest.json")
                if manifest and manifest.get("transaction_id") == name:
                    records.append((candidate, manifest))
        records.sort(key=lambda item: item[1].get("created_at") or "")
        return records
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
        metadata_fields = (
            "plan_id",
            "source",
            "date",
            "source_sha256",
            "archive_evidence_sha256",
            "draft_sha256",
            "pipeline_version",
            "template_version",
            "idempotency_key",
            "vault_root",
        )
        if any(
            manifest.get(field) != plan.get(field)
            for field in metadata_fields
        ):
            raise ConflictError("发布事务与发布计划元数据不一致")
        expected = [
            (
                item.get("relative_path"),
                item.get("before_exists"),
                item.get("before_hash"),
                item.get("after_hash"),
            )
            for item in plan.get("changes", [])
        ]
        actual = [
            (
                item.get("relative_path"),
                item.get("before_exists"),
                item.get("before_hash"),
                item.get("after_hash"),
            )
            for item in normalized
        ]
        if actual != expected:
            raise ConflictError("发布事务与发布计划不一致")
    return normalized


def _entry_state(vault_root, entry, vault_session=None):
    if vault_session is None:
        with _vault_directory_session(vault_root) as opened_session:
            return _entry_state(
                vault_root, entry, vault_session=opened_session
            )
    current, _exists = _read_vault_target(
        vault_session, entry["relative_path"]
    )
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


def _restore_apply_transaction(
        vault_root, tx_dir, manifest, reason, vault_session=None):
    if vault_session is None:
        with _vault_directory_session(vault_root) as opened_session:
            return _restore_apply_transaction(
                vault_root,
                tx_dir,
                manifest,
                reason,
                vault_session=opened_session,
            )
    manifest_path = tx_dir / "manifest.json"
    try:
        entries = _validate_manifest(manifest, vault_root)
        states = [
            (
                entry,
                _entry_state(vault_root, entry, vault_session=vault_session),
            )
            for entry in entries
        ]
    except Exception as exc:
        _record_recovery_required(
            manifest_path,
            manifest,
            "recovery_required",
            [str(exc)],
            "发布失败且 Vault 当前无法安全检查",
        )
    conflicts = [entry["relative_path"] for entry, state in states if state == "conflict"]
    if conflicts:
        _record_recovery_required(
            manifest_path,
            manifest,
            "recovery_required",
            ["文件内容既不等于发布前也不等于计划结果：%s" % path for path in conflicts],
            "发布事务无法自动恢复",
        )

    restore_entries = [
        entry for entry, state in states if state == "after"
    ]
    try:
        snapshots = _preflight_restore_snapshots(tx_dir, restore_entries)
    except Exception as exc:
        _record_recovery_required(
            manifest_path,
            manifest,
            "recovery_required",
            [str(exc)],
            "发布失败且事务快照预检未通过",
        )

    errors = []
    for entry, state in reversed(states):
        if (state != "after"
                and not (state == "before" and not entry.get("before_exists"))):
            continue
        try:
            _restore_entry(
                vault_root,
                tx_dir,
                entry,
                snapshot_bytes=(
                    snapshots.get(entry["snapshot"])
                    if entry.get("before_exists")
                    else None
                ),
                vault_session=vault_session,
            )
        except Exception as exc:
            errors.append("%s：%s" % (entry["relative_path"], exc))
    for entry in entries:
        try:
            if _entry_state(
                    vault_root, entry, vault_session=vault_session
                    ) not in ("before", "unchanged"):
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
    try:
        _atomic_write_json(
            _safe_archive_path(
                archive_root, "_transactions", manifest["transaction_id"],
                "manifest.json"
            ),
            manifest,
        )
    except Exception as exc:
        manifest["metadata_errors"] = ["事务元数据完成标记：%s" % exc]
        try:
            _atomic_write_json(
                _safe_archive_path(
                    archive_root, "_transactions", manifest["transaction_id"],
                    "manifest.json"
                ),
                manifest,
            )
        except Exception:
            pass
        raise RecoveryError("发布元数据已修复，但完成标记待重试：%s" % exc) from exc


def _recover_pending_plan_transactions(
        archive_root, vault_root, plan_path, plan, vault_session=None):
    pending_statuses = {"prepared", "applying", "recovery_required"}
    recovered = False
    candidates = [
        (tx_dir, manifest)
        for tx_dir, manifest in _transactions_for_plan(
            archive_root, plan["plan_id"]
        )
        if (manifest.get("status") in pending_statuses
            or (manifest.get("status") == "applied"
                and manifest.get("metadata_errors")))
    ]
    for tx_dir, manifest in candidates:
        transaction_id = manifest["transaction_id"]
        _sentinel_path, active_sentinel = _load_vault_sentinel(vault_root)
        if manifest.get("status") == "prepared" and active_sentinel is None:
            # The apply protocol persists its Vault-wide sentinel before
            # switching a manifest to applying or touching any Vault target.
            # Therefore an orphan prepared manifest is provably pre-write and
            # can be abandoned instead of replayed over a newer publication.
            _write_manifest_status(
                tx_dir / "manifest.json",
                manifest,
                "failed_restored",
                failed_at=_now(),
                restored_at=_now(),
                failure="发布在 Vault 写入前中断，prepared 事务已安全废弃",
            )
            recovered = True
            continue
        _claim_vault_sentinel(
            archive_root,
            vault_root,
            plan["plan_id"],
            transaction_id,
            (
                "applied_metadata_pending"
                if manifest.get("status") == "applied"
                else manifest.get("status")
            ),
            _APPLY_SENTINEL_PHASES,
        )
        try:
            entries = _validate_manifest(manifest, vault_root, plan=plan)
            states = [
                _entry_state(
                    vault_root, entry, vault_session=vault_session
                )
                for entry in entries
            ]
        except Exception as exc:
            try:
                _record_recovery_required(
                    tx_dir / "manifest.json",
                    manifest,
                    "recovery_required",
                    [str(exc)],
                    "未完成发布事务的 Vault 当前无法安全检查",
                )
            finally:
                _claim_vault_sentinel(
                    archive_root, vault_root, plan["plan_id"], transaction_id,
                    "recovery_required", _APPLY_SENTINEL_PHASES,
                )
        if all(state in ("after", "unchanged") for state in states):
            applied_at = manifest.get("applied_at") or _now()
            _write_manifest_status(
                tx_dir / "manifest.json", manifest, "applied", applied_at=applied_at
            )
            _claim_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id,
                "applied_metadata_pending", _APPLY_SENTINEL_PHASES,
            )
            _repair_applied_metadata(archive_root, plan_path, plan, manifest)
            _clear_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id
            )
            return True, transaction_id
        try:
            _restore_apply_transaction(
                vault_root,
                tx_dir,
                manifest,
                "恢复上次中断的发布事务",
                vault_session=vault_session,
            )
        except BaseException:
            live = _load_json(tx_dir / "manifest.json") or manifest
            phase = live.get("status")
            if phase not in _APPLY_SENTINEL_PHASES:
                phase = "recovery_required"
            _claim_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id,
                phase, _APPLY_SENTINEL_PHASES,
            )
            raise
        _clear_vault_sentinel(
            archive_root, vault_root, plan["plan_id"], transaction_id
        )
        recovered = True
    return recovered, None


def _reject_replan_with_pending_transaction(archive_root, vault_root, source, day):
    """Keep one Vault read-only while any older apply needs recovery."""
    canonical_vault = str(validate_vault_root(vault_root))
    _sentinel_path, sentinel = _load_vault_sentinel(vault_root)
    if sentinel is not None:
        raise RecoveryError(
            "Vault 存在未完成发布事务 %s（%s）；请先恢复原事务"
            % (sentinel.get("transaction_id"), sentinel.get("phase"))
        )
    pending_statuses = {
        "applying", "recovery_required",
        "rolling_back", "rollback_recovery_required",
    }
    for _tx_dir, manifest in _iter_transaction_manifests(archive_root):
        if (manifest.get("status") not in pending_statuses
                and not (
                    manifest.get("status") == "applied"
                    and manifest.get("metadata_errors")
                )):
            continue
        if manifest.get("vault_root") != canonical_vault:
            continue
        raise RecoveryError(
            "存在未完成发布事务 %s；请使用原计划 %s 重试 publish-apply 后再生成预览"
            % (manifest.get("transaction_id"), manifest.get("plan_id"))
        )


@_serialized_publisher_mutation
def apply_plan(archive_root, vault_root, plan_id):
    plan_path, plan = _load_plan(archive_root, plan_id)
    _validate_plan_vault(plan, vault_root)
    for change in plan.get("changes", []):
        _safe_target(vault_root, change.get("relative_path", ""))
    _verify_plan_integrity(plan)
    with _fetch_date_evidence_lock(archive_root, plan.get("date")):
        with _draft_rmw_lock(
                archive_root, plan.get("source"), plan.get("date")):
            vault_session = _active_vault_session()
            if vault_session is None:
                raise PublisherError("发布操作缺少固定 Vault 会话")
            return _apply_plan_locked(
                archive_root, vault_root, plan_id, plan_path, plan,
                vault_session,
            )


def _apply_plan_locked(
        archive_root, vault_root, plan_id, plan_path, plan, vault_session):
    """Apply a plan while its source issue tree is locked and unchanged."""
    # An interrupted transaction owns the Vault until it is recovered.  Its
    # recovery must not be blocked by a later editorial draft.
    _guard_vault_for_apply(
        archive_root, vault_root, plan_id, vault_session=vault_session
    )
    recovered_pending, finalized_transaction = _recover_pending_plan_transactions(
        archive_root,
        vault_root,
        plan_path,
        plan,
        vault_session=vault_session,
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
    # From here onward this call may begin a fresh transaction, so the plan
    # must still describe the currently persisted reviewed content.
    _require_current_draft_digest(
        archive_root,
        plan.get("source"),
        plan.get("date"),
        plan.get("draft_sha256"),
    )
    current_evidence = _issue_tree_evidence_sha256(
        archive_root, plan.get("source"), plan.get("date")
    )
    if (not current_evidence
            or not hmac.compare_digest(
                current_evidence, str(plan.get("archive_evidence_sha256") or "")
            )):
        raise ConflictError("报纸原始证据在发布预览后发生变化，请重新复核并生成预览")
    checked = []
    for change in plan.get("changes", []):
        current, _exists = _read_vault_target(
            vault_session, change.get("relative_path", "")
        )
        current_hash = _hash_bytes(current)
        checked.append((change, current, current_hash))
    if checked and all(current_hash == change["after_hash"]
                       for change, _raw, current_hash in checked):
        applied_records = _transactions_for_plan(archive_root, plan_id, {"applied"})
        if not applied_records:
            raise ConflictError(
                "Vault 内容虽与计划一致，但当前归档没有可验证的发布事务所有权记录"
            )
        _tx_dir, applied_manifest = applied_records[-1]
        transaction_id = applied_manifest["transaction_id"]
        if applied_manifest.get("metadata_errors"):
            _claim_vault_sentinel(
                archive_root, vault_root, plan_id, transaction_id,
                "applied_metadata_pending", _APPLY_SENTINEL_PHASES,
            )
        _repair_applied_metadata(archive_root, plan_path, plan, applied_manifest)
        _clear_vault_sentinel(
            archive_root, vault_root, plan_id, transaction_id
        )
        return {
            "applied": False,
            "idempotent": True,
            "plan_id": plan_id,
            "transaction_id": transaction_id,
            "changed_files": 0,
            "recovered_pending_transaction": recovered_pending,
        }
    conflicts = [change["relative_path"] for change, _raw, current_hash in checked
                 if current_hash != change["before_hash"]]
    if conflicts:
        raise ConflictError("文件在预览后发生变化：%s" % "、".join(conflicts))

    transaction_id = uuid.uuid4().hex
    tx_dir = _safe_archive_path(archive_root, "_transactions", transaction_id)
    before_dir = tx_dir / "before"
    archive_session = _active_archive_session()
    if archive_session is None:
        raise PublisherError("发布事务缺少固定归档会话")
    transaction_relative = os.path.join("_transactions", transaction_id)
    _archive_ensure_directory(
        archive_session, transaction_relative, exclusive=True
    )
    _archive_ensure_directory(
        archive_session, os.path.join(transaction_relative, "before")
    )
    entries = []
    for index, (change, current, _current_hash) in enumerate(checked):
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
        "archive_evidence_sha256": plan["archive_evidence_sha256"],
        "draft_sha256": plan["draft_sha256"],
        "pipeline_version": plan["pipeline_version"],
        "template_version": plan["template_version"],
        "idempotency_key": plan["idempotency_key"],
        "vault_root": plan["vault_root"],
        "created_at": _now(),
        "status": "prepared",
        "entries": entries,
    }
    _atomic_write_json(manifest_path, manifest)
    # The durable, Vault-scoped sentinel must exist before the first possible
    # Vault write.  A crash before this point leaves only a harmless prepared
    # archive manifest; a crash after it blocks every other archive/plan.
    _claim_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id,
        "prepared", _APPLY_SENTINEL_PHASES,
    )
    _write_manifest_status(manifest_path, manifest, "applying", started_at=_now())
    _claim_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id,
        "applying", _APPLY_SENTINEL_PHASES,
    )

    try:
        for change, _current, _current_hash in checked:
            # Re-check after creating snapshots, closing the most common edit race.
            live, _exists = _read_vault_target(
                vault_session, change["relative_path"]
            )
            if _hash_bytes(live) != change["before_hash"]:
                raise ConflictError("写入前文件发生变化：%s" % change["relative_path"])
            _atomic_write_bytes(
                _VaultMutationTarget(
                    vault_session,
                    change["relative_path"],
                    change["before_exists"],
                    change["before_hash"],
                    change["after_hash"],
                ),
                change["after"].encode("utf-8"),
            )
        # A sync client or Obsidian can still change an earlier target while a
        # later file is being written.  Never mark the group applied until all
        # targets simultaneously match the previewed after hashes.
        drifted = []
        for change, _current, _current_hash in checked:
            live, _exists = _read_vault_target(
                vault_session, change["relative_path"]
            )
            if _hash_bytes(live) != change["after_hash"]:
                drifted.append(change["relative_path"])
        if drifted:
            raise ConflictError("整组写入后文件发生变化：%s" % "、".join(drifted))
        applied_at = _now()
        _write_manifest_status(
            manifest_path, manifest, "applied", applied_at=applied_at
        )
    except Exception as apply_error:
        manifest["failed_at"] = _now()
        if isinstance(apply_error, RecoveryError):
            try:
                _record_recovery_required(
                    manifest_path,
                    manifest,
                    "recovery_required",
                    [str(apply_error)],
                    "发布写入进入需要显式重试的恢复状态",
                )
            finally:
                _claim_vault_sentinel(
                    archive_root, vault_root, plan_id, transaction_id,
                    "recovery_required", _APPLY_SENTINEL_PHASES,
                )
        try:
            _restore_apply_transaction(
                vault_root,
                tx_dir,
                manifest,
                apply_error,
                vault_session=vault_session,
            )
        except RecoveryError as recovery_error:
            live = _load_json(manifest_path) or manifest
            _claim_vault_sentinel(
                archive_root, vault_root, plan_id, transaction_id,
                "recovery_required", _APPLY_SENTINEL_PHASES,
            )
            raise recovery_error from apply_error
        _clear_vault_sentinel(
            archive_root, vault_root, plan_id, transaction_id
        )
        raise PublisherError("发布失败，Vault 已恢复到预览前状态：%s" % apply_error) from apply_error

    _claim_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id,
        "applied_metadata_pending", _APPLY_SENTINEL_PHASES,
    )
    _repair_applied_metadata(archive_root, plan_path, plan, manifest)
    _clear_vault_sentinel(
        archive_root, vault_root, plan_id, transaction_id
    )
    return {
        "applied": True,
        "idempotent": False,
        "plan_id": plan_id,
        "transaction_id": transaction_id,
        "changed_files": len(checked),
        "recovered_pending_transaction": recovered_pending,
    }


@_serialized_publisher_mutation
def rollback_transaction(archive_root, vault_root, transaction_id):
    tx_dir, manifest = _load_transaction(archive_root, transaction_id)
    # Publisher lock is already held by the decorator.  The shared date lock
    # then covers every state check, Vault restore and publication-state RMW so
    # fetch cannot lose a concurrent state marker in _clear_published.
    with _fetch_date_evidence_lock(archive_root, manifest.get("date")):
        vault_session = _active_vault_session()
        if vault_session is None:
            raise PublisherError("回滚操作缺少固定 Vault 会话")
        return _rollback_transaction_locked(
            archive_root, vault_root, transaction_id, tx_dir, manifest,
            vault_session,
        )


def _rollback_transaction_locked(
        archive_root, vault_root, transaction_id, tx_dir, manifest,
        vault_session):
    _plan_path, plan = _load_plan(archive_root, manifest.get("plan_id"))
    _validate_plan_vault(plan, vault_root)
    _verify_plan_integrity(plan)
    entries = _validate_manifest(manifest, vault_root, plan=plan)
    sentinel = _guard_vault_for_rollback(
        archive_root, vault_root, plan["plan_id"], transaction_id
    )
    if manifest.get("status") == "rolled_back":
        try:
            _clear_published(archive_root, manifest)
        except Exception as exc:
            _claim_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id,
                "rollback_metadata_pending", _ROLLBACK_SENTINEL_PHASES,
            )
            raise RecoveryError("文件已回滚，但发布状态待重试清理：%s" % exc) from exc
        if sentinel is not None:
            _clear_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id
            )
        return {"rolled_back": False, "idempotent": True,
                "transaction_id": transaction_id}
    starting_status = manifest.get("status")
    resumable = {"applied", "rolling_back", "rollback_recovery_required"}
    if starting_status not in resumable:
        raise ConflictError("只有已完成的发布事务可以回滚")
    states = [
        (
            entry,
            _entry_state(vault_root, entry, vault_session=vault_session),
        )
        for entry in entries
    ]
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
    restore_entries = [
        entry for entry, state in states if state == "after"
    ]
    snapshots = _preflight_restore_snapshots(tx_dir, restore_entries)
    _claim_vault_sentinel(
        archive_root, vault_root, plan["plan_id"], transaction_id,
        "rolling_back", _ROLLBACK_SENTINEL_PHASES,
    )
    _write_manifest_status(
        tx_dir / "manifest.json", manifest, "rolling_back", rollback_started_at=_now()
    )
    restored = 0
    errors = []
    try:
        for entry, state in reversed(states):
            if (state != "after"
                    and not (
                        state == "before" and not entry.get("before_exists")
                    )):
                continue
            _restore_entry(
                vault_root,
                tx_dir,
                entry,
                snapshot_bytes=(
                    snapshots.get(entry["snapshot"])
                    if entry.get("before_exists")
                    else None
                ),
                vault_session=vault_session,
            )
            if state == "after":
                restored += 1
        for entry in entries:
            if _entry_state(
                    vault_root, entry, vault_session=vault_session
                    ) not in ("before", "unchanged"):
                errors.append("回滚后哈希不一致：%s" % entry["relative_path"])
        if errors:
            raise RecoveryError("；".join(errors))
        _write_manifest_status(
            tx_dir / "manifest.json", manifest, "rolled_back", rolled_back_at=_now()
        )
    except Exception as rollback_error:
        errors.append(str(rollback_error))
        try:
            _record_recovery_required(
                tx_dir / "manifest.json",
                manifest,
                "rollback_recovery_required",
                errors,
                "回滚未完成，可在排除故障后用同一事务重试",
            )
        finally:
            _claim_vault_sentinel(
                archive_root, vault_root, plan["plan_id"], transaction_id,
                "rollback_recovery_required", _ROLLBACK_SENTINEL_PHASES,
            )
    try:
        _clear_published(archive_root, manifest)
    except Exception as exc:
        _claim_vault_sentinel(
            archive_root, vault_root, plan["plan_id"], transaction_id,
            "rollback_metadata_pending", _ROLLBACK_SENTINEL_PHASES,
        )
        raise RecoveryError("文件已回滚，但发布状态待重试清理：%s" % exc) from exc
    _clear_vault_sentinel(
        archive_root, vault_root, plan["plan_id"], transaction_id
    )
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
