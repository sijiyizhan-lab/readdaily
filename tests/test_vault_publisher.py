import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
READER_SCRIPTS = ROOT / "skills" / "newspaper-reader" / "scripts"
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(READER_SCRIPTS))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publisher = load_module("vault_publisher", READER_SCRIPTS / "vault_publisher.py")


def _concurrent_apply_worker(
        archive, vault, plan_id, start_gate, write_barrier, results):
    """Fork worker that deterministically aligns competing Vault writes."""
    real_write = publisher._atomic_write_bytes
    target_root = (Path(vault) / publisher.TARGET_FOLDER).resolve()

    def synchronized_write(path, raw):
        resolved = Path(path).resolve()
        if resolved == target_root or target_root in resolved.parents:
            try:
                write_barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
        return real_write(path, raw)

    publisher._atomic_write_bytes = synchronized_write
    try:
        if not start_gate.wait(timeout=5):
            raise RuntimeError("并发测试启动门超时")
        result = publisher.apply_plan(archive, vault, plan_id)
        results.put({"ok": True, "result": result})
    except BaseException as exc:  # noqa: BLE001
        results.put({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })


def _concurrent_rollback_worker(
        archive, vault, transaction_id, start_gate, restore_barrier, results):
    real_restore = publisher._restore_entry

    def synchronized_restore(
            vault_root, transaction_dir, entry, snapshot_bytes=None,
            vault_session=None):
        try:
            restore_barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return real_restore(
            vault_root,
            transaction_dir,
            entry,
            snapshot_bytes=snapshot_bytes,
            vault_session=vault_session,
        )

    publisher._restore_entry = synchronized_restore
    try:
        if not start_gate.wait(timeout=5):
            raise RuntimeError("并发测试启动门超时")
        result = publisher.rollback_transaction(
            archive, vault, transaction_id
        )
        results.put({"ok": True, "result": result})
    except BaseException as exc:  # noqa: BLE001
        results.put({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })


def _fetch_state_marker_worker(
        archive, source, day, acquired, release, results):
    sys.path.insert(0, str(FETCH_SCRIPTS))
    import fetch
    try:
        with fetch.fetch_date_lock(archive, day):
            acquired.set()
            if not release.wait(timeout=5):
                raise RuntimeError("抓取状态写入测试未收到继续信号")
            state_path = str(
                Path(archive) / "_state" / source / f"{day}.json"
            )
            fetch.lib.state_mark(
                state_path,
                "fetched",
                concurrent_fetch_marker="preserved",
            )
        results.put({"ok": True})
    except BaseException as exc:  # noqa: BLE001
        results.put({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })


class VaultPublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.archive = self.base / "archive"
        self.vault = self.base / "vault"
        self.publisher_state = self.base / "publisher-state"
        self.publisher_state_patch = mock.patch.object(
            publisher, "DEFAULT_PUBLISHER_STATE_ROOT", self.publisher_state
        )
        self.publisher_state_patch.start()
        self.publisher_state_env_patch = mock.patch.dict(
            os.environ,
            {"READDAILY_PUBLISHER_STATE_ROOT": str(self.publisher_state)},
        )
        self.publisher_state_env_patch.start()
        self.archive.mkdir()
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()
        self.target_root = self.vault / publisher.TARGET_FOLDER
        self.target_root.mkdir()
        self.issue = {
            "source": "zgjsb",
            "source_name": "中国建设报",
            "date": "2026-09-01",
            "issue_no": "9168",
            "units": [
                {"id": "u1", "edition_no": 1, "edition_name": "要闻"},
                {"id": "u2", "edition_no": 2, "edition_name": "综合"},
            ],
        }
        common_fact = {
            "subject": "某市",
            "action": "改造",
            "object": "老旧小区",
            "value": "12",
            "unit": "个",
            "time": "2026年",
            "source": "中国建设报第1版",
        }
        self.draft = {
            "source": "zgjsb",
            "date": "2026-09-01",
            "units": [
                {
                    "id": "u1",
                    "title": "保交房资金监管",
                    "summary": "住房融资政策形成项目级资金监管和安全交付闭环。",
                    "importance": 5,
                    "topics": ["建设投资与房地产", "住房民生与社区服务"],
                    "facts": [common_fact],
                },
                {
                    "id": "u2",
                    "summary": "城市体检推动片区更新和公共设施韧性提升。",
                    "importance": 4,
                    "topics": ["城市更新与城市治理", "建设安全与城市韧性"],
                    "facts": [common_fact],
                },
            ],
        }
        self.write_issue_evidence(self.archive, self.issue)
        self.draft["evidence_sha256"] = self.issue["evidence_sha256"]
        self.persist_draft(self.archive, self.draft)

    def tearDown(self):
        self.publisher_state_env_patch.stop()
        self.publisher_state_patch.stop()
        self.tmp.cleanup()

    def write_issue_evidence(self, archive, issue):
        issue_dir = Path(archive) / issue["source"] / issue["date"]
        issue_dir.mkdir(parents=True, exist_ok=True)
        raw_issue = json.loads(json.dumps(issue, ensure_ascii=False))
        raw_issue.pop("evidence_sha256", None)
        raw_issue.pop("archive_evidence_sha256", None)
        (issue_dir / "issue.json").write_text(
            json.dumps(raw_issue, ensure_ascii=False), encoding="utf-8"
        )
        digest = publisher._issue_tree_evidence_sha256(
            archive, issue["source"], issue["date"]
        )
        issue["evidence_sha256"] = digest
        issue["archive_evidence_sha256"] = digest

    def persist_draft(self, archive, draft):
        draft_path = (
            Path(archive) / "_drafts" / draft["source"] /
            f"{draft['date']}.json"
        )
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(
            json.dumps(draft, ensure_ascii=False), encoding="utf-8"
        )

    def issue_and_draft_for(self, archive, day, issue_no):
        issue = json.loads(json.dumps(self.issue, ensure_ascii=False))
        issue["date"] = day
        issue["issue_no"] = issue_no
        issue.pop("evidence_sha256", None)
        issue.pop("archive_evidence_sha256", None)
        draft = json.loads(json.dumps(self.draft, ensure_ascii=False))
        draft["date"] = day
        draft.pop("evidence_sha256", None)
        self.write_issue_evidence(archive, issue)
        draft["evidence_sha256"] = issue["evidence_sha256"]
        self.persist_draft(archive, draft)
        return issue, draft

    def test_plan_apply_idempotent_and_rollback_preserve_manual_content(self):
        topic = self.target_root / "主题" / "建设投资与房地产.md"
        topic.parent.mkdir()
        manual = "# 建设投资与房地产\n\n这里是人工观察，必须保留。\n"
        topic.write_text(manual, encoding="utf-8")
        before_files = {p.relative_to(self.vault) for p in self.vault.rglob("*") if p.is_file()}

        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )

        # plan 仅保存到 archive，不得改动 Vault。
        after_plan_files = {p.relative_to(self.vault) for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(before_files, after_plan_files)
        self.assertEqual(len(plan["changes"]), 9)  # 索引 + 日报 + 7 个既有主题卡
        self.assertEqual(plan["pipeline_version"], publisher.PIPELINE_VERSION)
        self.assertEqual(plan["template_version"], publisher.TEMPLATE_VERSION)
        self.assertRegex(plan["source_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(plan["archive_evidence_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            plan["draft_sha256"],
            publisher._draft_content_sha256(self.draft),
        )
        self.assertTrue(all(c["relative_path"].startswith(publisher.TARGET_FOLDER + "/")
                            for c in plan["changes"]))
        self.assertTrue(any(c["diff"] for c in plan["changes"]))

        applied = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        self.assertTrue(applied["applied"])
        self.assertFalse(applied["idempotent"])
        topic_text = topic.read_text(encoding="utf-8")
        self.assertIn("这里是人工观察，必须保留。", topic_text)
        self.assertIn("READDAILY:BEGIN", topic_text)
        self.assertIn("保交房资金监管", topic_text)
        state = json.loads((self.archive / "_state" / "zgjsb" / "2026-09-01.json").read_text())
        self.assertIn("published", state["stages"])
        self.assertIn("archived", state["stages"])
        manifest = json.loads((self.archive / "_transactions" / applied["transaction_id"] /
                               "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_sha256"], plan["source_sha256"])
        self.assertEqual(
            manifest["archive_evidence_sha256"],
            plan["archive_evidence_sha256"],
        )
        self.assertEqual(manifest["draft_sha256"], plan["draft_sha256"])
        self.assertEqual(manifest["pipeline_version"], publisher.PIPELINE_VERSION)
        self.assertEqual(manifest["template_version"], publisher.TEMPLATE_VERSION)

        again = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        self.assertFalse(again["applied"])
        self.assertTrue(again["idempotent"])

        rolled = publisher.rollback_transaction(
            self.archive, self.vault, applied["transaction_id"]
        )
        self.assertTrue(rolled["rolled_back"])
        self.assertEqual(topic.read_text(encoding="utf-8"), manual)
        daily_files = list((self.target_root / "日报").glob("*.md"))
        self.assertEqual(daily_files, [])

    def test_publisher_state_root_must_be_outside_vault_before_write(self):
        forbidden = self.vault / "publisher-state"
        with mock.patch.dict(
            os.environ,
            {"READDAILY_PUBLISHER_STATE_ROOT": str(forbidden)},
        ):
            with self.assertRaisesRegex(
                publisher.PathSafetyError, "必须与 Vault 完全分离"
            ):
                publisher.create_plan(
                    self.archive, self.vault, self.issue, self.draft
                )
        self.assertFalse(forbidden.exists())

    def test_claim_and_update_sentinel_fsync_parent_directory(self):
        plan_id = "a" * 64
        transaction_id = "b" * 32
        real_fsync = publisher.os.fsync
        directory_syncs = []

        def track_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_syncs.append(descriptor)
            return real_fsync(descriptor)

        with mock.patch.object(publisher.os, "fsync", side_effect=track_fsync):
            publisher._claim_vault_sentinel(
                self.archive, self.vault, plan_id, transaction_id,
                "prepared", publisher._APPLY_SENTINEL_PHASES,
            )
            publisher._claim_vault_sentinel(
                self.archive, self.vault, plan_id, transaction_id,
                "applying", publisher._APPLY_SENTINEL_PHASES,
            )

        self.assertGreaterEqual(len(directory_syncs), 2)
        _path, record = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(record["phase"], "applying")
        publisher._clear_vault_sentinel(
            self.archive, self.vault, plan_id, transaction_id
        )

    def test_publisher_state_root_durably_creates_every_parent(self):
        deep_root = (
            self.base / "new-state-parent" / "inner" / "publisher-state"
        )
        directory_syncs = []
        real_fsync = publisher.os.fsync

        def track_fsync(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                directory_syncs.append((info.st_dev, info.st_ino))
            return real_fsync(descriptor)

        with mock.patch.dict(
            os.environ,
            {"READDAILY_PUBLISHER_STATE_ROOT": str(deep_root)},
        ), mock.patch.object(
            publisher.os, "fsync", side_effect=track_fsync
        ):
            result = publisher._publisher_state_root(self.vault)

        self.assertEqual(result, deep_root.resolve())
        expected = {
            (path.stat().st_dev, path.stat().st_ino)
            for path in (
                self.base,
                self.base / "new-state-parent",
                self.base / "new-state-parent" / "inner",
            )
        }
        self.assertTrue(expected.issubset(set(directory_syncs)))

    def test_publisher_state_ancestor_swap_never_writes_vault(self):
        safe_parent = self.base / "publisher-state-parent"
        safe_parent.mkdir()
        configured = safe_parent / "publisher-state"
        displaced = self.base / "publisher-state-parent-displaced"
        vault_trap = self.vault / "publisher-state-trap"
        vault_trap.mkdir()
        real_mkdir = publisher.os.mkdir
        swapped = False

        def swap_parent_before_state_mkdir(name, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if name == "publisher-state" and dir_fd is not None and not swapped:
                swapped = True
                safe_parent.rename(displaced)
                safe_parent.symlink_to(vault_trap, target_is_directory=True)
            return real_mkdir(name, mode, dir_fd=dir_fd)

        with mock.patch.dict(
            os.environ,
            {"READDAILY_PUBLISHER_STATE_ROOT": str(configured)},
        ), mock.patch.object(
            publisher.os, "mkdir", side_effect=swap_parent_before_state_mkdir
        ):
            with self.assertRaises(publisher.PathSafetyError):
                publisher._publisher_state_root(self.vault)

        self.assertTrue(swapped)
        self.assertEqual(list(vault_trap.rglob("*")), [])

    def test_sentinel_directory_fsync_failure_is_recovery_error(self):
        plan_id = "c" * 64
        transaction_id = "d" * 32
        publisher._publisher_state_root(self.vault)
        real_fsync = publisher.os.fsync

        def fail_directory_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            publisher.os, "fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaisesRegex(
                publisher.RecoveryError, "持久化发布事务哨兵"
            ):
                publisher._claim_vault_sentinel(
                    self.archive, self.vault, plan_id, transaction_id,
                    "prepared", publisher._APPLY_SENTINEL_PHASES,
                )

        # replace may already be visible even when directory durability cannot
        # be confirmed; ownership data remains recoverable and is not hidden.
        _path, record = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(record["transaction_id"], transaction_id)
        publisher._clear_vault_sentinel(
            self.archive, self.vault, plan_id, transaction_id
        )

    def test_restore_created_file_fsyncs_parent_after_unlink(self):
        relative_path = "%s/日报/durable-unlink.md" % publisher.TARGET_FOLDER
        target = self.vault / relative_path
        target.parent.mkdir(parents=True)
        target.write_text("待回滚", encoding="utf-8")
        parent_stat = target.parent.stat()
        expected_parent = (parent_stat.st_dev, parent_stat.st_ino)
        synced_directories = []
        real_fsync = publisher.os.fsync

        def track_fsync(descriptor):
            descriptor_stat = os.fstat(descriptor)
            if stat.S_ISDIR(descriptor_stat.st_mode):
                synced_directories.append(
                    (descriptor_stat.st_dev, descriptor_stat.st_ino)
                )
            return real_fsync(descriptor)

        with mock.patch.object(
            publisher.os, "fsync", side_effect=track_fsync
        ):
            publisher._restore_entry(
                self.vault,
                self.archive,
                {
                    "relative_path": relative_path,
                    "before_exists": False,
                    "after_hash": publisher._hash_bytes(
                        "待回滚".encode("utf-8")
                    ),
                },
            )

        self.assertFalse(target.exists())
        self.assertIn(expected_parent, synced_directories)

    def test_first_publish_durably_creates_vault_and_transaction_parents(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        self.target_root.rmdir()
        directory_syncs = []
        real_fsync = publisher.os.fsync

        def track_fsync(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode):
                directory_syncs.append((info.st_dev, info.st_ino))
            return real_fsync(descriptor)

        with mock.patch.object(
            publisher.os, "fsync", side_effect=track_fsync
        ):
            applied = publisher.apply_plan(
                self.archive, self.vault, plan["plan_id"]
            )

        transaction_root = self.archive / "_transactions"
        transaction = transaction_root / applied["transaction_id"]
        expected = {
            (path.stat().st_dev, path.stat().st_ino)
            for path in (
                self.vault,
                self.target_root,
                self.archive,
                transaction_root,
                transaction,
            )
        }
        self.assertTrue(expected.issubset(set(directory_syncs)))

    def test_vault_mkdir_fsync_failure_keeps_recovery_sentinel(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        self.target_root.rmdir()
        vault_info = self.vault.stat()
        vault_identity = (vault_info.st_dev, vault_info.st_ino)
        real_fsync = publisher.os.fsync

        def fail_vault_root_fsync(descriptor):
            info = os.fstat(descriptor)
            if ((info.st_dev, info.st_ino) == vault_identity
                    and stat.S_ISDIR(info.st_mode)):
                raise OSError("injected Vault root fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            publisher.os, "fsync", side_effect=fail_vault_root_fsync
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "recovery_required")

    def test_apply_rejects_plan_after_issue_evidence_changes_without_vault_write(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }
        issue_path = self.archive / "zgjsb" / "2026-09-01" / "issue.json"
        changed = json.loads(issue_path.read_text(encoding="utf-8"))
        changed["units"][0]["text"] = "发布预览之后替换的新正文"
        issue_path.write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(publisher.ConflictError, "原始证据"):
            publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        after = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(
            list((self.archive / "_transactions").glob("*"))
            if (self.archive / "_transactions").exists() else [],
            [],
        )

    def test_old_draft_cannot_create_or_apply_after_persisted_draft_changes(self):
        draft_a = json.loads(json.dumps(self.draft, ensure_ascii=False))
        plan_a = publisher.create_plan(
            self.archive, self.vault, self.issue, draft_a
        )
        draft_b = json.loads(json.dumps(draft_a, ensure_ascii=False))
        draft_b["units"][0]["summary"] = "B 版复核摘要，替代旧 A 草稿"
        self.persist_draft(self.archive, draft_b)
        vault_before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }

        with self.assertRaisesRegex(publisher.ConflictError, "草稿已变化"):
            publisher.create_plan(
                self.archive, self.vault, self.issue, draft_a
            )
        with self.assertRaisesRegex(publisher.ConflictError, "草稿.*变化"):
            publisher.apply_plan(
                self.archive, self.vault, plan_a["plan_id"]
            )

        vault_after = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }
        self.assertEqual(vault_after, vault_before)
        self.assertFalse((self.archive / "_transactions").exists())

    def test_create_plan_rejects_stale_issue_and_draft_after_locked_tree_replacement(self):
        stale_issue = json.loads(json.dumps(self.issue, ensure_ascii=False))
        stale_draft = json.loads(json.dumps(self.draft, ensure_ascii=False))
        issue_path = self.archive / "zgjsb" / "2026-09-01" / "issue.json"
        replacement = json.loads(issue_path.read_text(encoding="utf-8"))
        replacement["issue_no"] = "NEW"
        replacement["units"][0]["text"] = "锁内读取到的新证据"
        issue_path.write_text(
            json.dumps(replacement, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(publisher.ConflictError, "证据已变化"):
            publisher.create_plan(
                self.archive, self.vault, stale_issue, stale_draft
            )

        self.assertFalse((self.archive / "_plans").exists())
        self.assertEqual(
            [path for path in self.vault.rglob("*") if path.is_file()], []
        )

    def test_create_plan_archive_root_swap_never_writes_vault(self):
        displaced = self.base / "archive-create-plan-displaced"
        vault_trap = self.vault / "archive-create-plan-trap"
        vault_trap.mkdir()
        real_write = publisher._atomic_write_bytes
        swapped = False

        def swap_archive_before_plan_commit(path, raw):
            nonlocal swapped
            path_value = Path(os.fspath(path))
            if "_plans" in path_value.parts and not swapped:
                swapped = True
                self.archive.rename(displaced)
                self.archive.symlink_to(vault_trap, target_is_directory=True)
            return real_write(path, raw)

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=swap_archive_before_plan_commit,
        ):
            with self.assertRaises(publisher.PathSafetyError):
                publisher.create_plan(
                    self.archive, self.vault, self.issue, self.draft
                )

        self.assertTrue(swapped)
        self.assertEqual(list(vault_trap.rglob("*")), [])
        self.assertFalse(any(self.target_root.rglob("*.md")))

    def test_apply_archive_root_swap_before_manifest_never_writes_vault(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        displaced = self.base / "archive-apply-displaced"
        vault_trap = self.vault / "archive-apply-trap"
        vault_trap.mkdir()
        real_write = publisher._atomic_write_bytes
        swapped = False

        def swap_archive_before_transaction_commit(path, raw):
            nonlocal swapped
            path_value = Path(os.fspath(path))
            if ("_transactions" in path_value.parts
                    and not isinstance(path, publisher._VaultMutationTarget)
                    and not swapped):
                swapped = True
                self.archive.rename(displaced)
                self.archive.symlink_to(vault_trap, target_is_directory=True)
            return real_write(path, raw)

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=swap_archive_before_transaction_commit,
        ):
            with self.assertRaises(publisher.PathSafetyError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertTrue(swapped)
        self.assertEqual(list(vault_trap.rglob("*")), [])
        self.assertFalse(any(self.target_root.rglob("*.md")))

    def test_concurrent_apply_creates_one_transaction_and_safe_rollback(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        context = multiprocessing.get_context("fork")
        start_gate = context.Event()
        write_barrier = context.Barrier(2)
        results = context.Queue()
        processes = []
        # Configured root paths are intentionally opened component-by-component
        # with O_NOFOLLOW, so a symlink alias is no longer a supported way to
        # address a Vault.  Two callers using the same configured root still
        # exercise the cross-process publisher lock.
        for vault_path in (self.vault, self.vault):
            processes.append(context.Process(
                target=_concurrent_apply_worker,
                args=(
                    str(self.archive),
                    str(vault_path),
                    plan["plan_id"],
                    start_gate,
                    write_barrier,
                    results,
                ),
            ))
        for process in processes:
            process.start()
        start_gate.set()
        for process in processes:
            process.join(timeout=15)
        try:
            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            outcomes = [results.get(timeout=2) for _index in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            results.close()
            results.join_thread()

        self.assertTrue(all(outcome["ok"] for outcome in outcomes), outcomes)
        applied = [outcome["result"] for outcome in outcomes]
        self.assertEqual(sum(bool(item["applied"]) for item in applied), 1)
        self.assertEqual(sum(bool(item["idempotent"]) for item in applied), 1)
        transaction_ids = {item["transaction_id"] for item in applied}
        self.assertEqual(len(transaction_ids), 1)

        manifests = list(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "applied")
        transaction_id = transaction_ids.pop()
        self.assertEqual(manifest["transaction_id"], transaction_id)

        rolled = publisher.rollback_transaction(
            self.archive, self.vault, transaction_id
        )
        self.assertTrue(rolled["rolled_back"])
        self.assertEqual(
            list((self.target_root / "日报").glob("*.md")), []
        )
        self.assertFalse(any(
            "READDAILY:BEGIN" in path.read_text(encoding="utf-8")
            for path in self.target_root.rglob("*.md")
        ))

    def test_concurrent_rollback_is_single_effect_and_idempotent(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        context = multiprocessing.get_context("fork")
        start_gate = context.Event()
        restore_barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_rollback_worker,
                args=(
                    str(self.archive),
                    str(self.vault),
                    applied["transaction_id"],
                    start_gate,
                    restore_barrier,
                    results,
                ),
            )
            for _index in range(2)
        ]
        for process in processes:
            process.start()
        start_gate.set()
        for process in processes:
            process.join(timeout=15)
        try:
            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            outcomes = [results.get(timeout=2) for _index in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            results.close()
            results.join_thread()

        self.assertTrue(all(outcome["ok"] for outcome in outcomes), outcomes)
        rollbacks = [outcome["result"] for outcome in outcomes]
        self.assertEqual(
            sum(bool(item["rolled_back"]) for item in rollbacks), 1
        )
        self.assertEqual(
            sum(bool(item["idempotent"]) for item in rollbacks), 1
        )
        self.assertEqual(
            json.loads((
                self.archive / "_transactions" / applied["transaction_id"] /
                "manifest.json"
            ).read_text(encoding="utf-8"))["status"],
            "rolled_back",
        )
        self.assertEqual(list(self.target_root.rglob("*.md")), [])

    def test_rollback_waits_for_fetch_state_marker_and_preserves_it(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        context = multiprocessing.get_context("spawn")
        acquired = context.Event()
        release = context.Event()
        results = context.Queue()
        marker = context.Process(
            target=_fetch_state_marker_worker,
            args=(
                str(self.archive), "zgjsb", "2026-09-01",
                acquired, release, results,
            ),
        )
        rollback_done = threading.Event()
        rollback_result = {}

        def run_rollback():
            try:
                rollback_result["value"] = publisher.rollback_transaction(
                    self.archive, self.vault, applied["transaction_id"]
                )
            except BaseException as exc:  # noqa: BLE001
                rollback_result["error"] = exc
            finally:
                rollback_done.set()

        marker.start()
        try:
            self.assertTrue(acquired.wait(timeout=5), "抓取状态进程未取得日期锁")
            rollback_thread = threading.Thread(target=run_rollback)
            rollback_thread.start()
            self.assertFalse(
                rollback_done.wait(timeout=0.3),
                "抓取仍持有日期锁时 rollback 不应进入状态清理",
            )
        finally:
            release.set()

        marker.join(timeout=5)
        rollback_thread.join(timeout=5)
        self.assertFalse(marker.is_alive(), "抓取状态进程超时")
        self.assertFalse(rollback_thread.is_alive(), "rollback 线程超时")
        self.assertEqual(marker.exitcode, 0)
        self.assertEqual(results.get(timeout=2), {"ok": True})
        self.assertNotIn("error", rollback_result)
        self.assertTrue(rollback_result["value"]["rolled_back"])
        state = json.loads(
            (self.archive / "_state" / "zgjsb" / "2026-09-01.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(state["concurrent_fetch_marker"], "preserved")
        self.assertIn("fetched", state["stages"])
        self.assertNotIn("published", state["stages"])
        self.assertNotIn("archived", state["stages"])

    def test_apply_detects_hash_conflict_and_does_not_mark_published(self):
        topic = self.target_root / "主题" / "建设投资与房地产.md"
        topic.parent.mkdir()
        topic.write_text("人工原文", encoding="utf-8")
        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        topic.write_text("计划后由用户修改", encoding="utf-8")

        with self.assertRaises(publisher.ConflictError):
            publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        state = self.archive / "_state" / "zgjsb" / "2026-09-01.json"
        self.assertFalse(state.exists())
        self.assertEqual(topic.read_text(encoding="utf-8"), "计划后由用户修改")

    def test_plan_integrity_rejects_before_exists_tampering_before_writes(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        plan_path = self.archive / "_plans" / f"{plan['plan_id']}.json"
        tampered = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered["changes"][0]["before_exists"] = not tampered[
            "changes"
        ][0]["before_exists"]
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaises(publisher.ConflictError):
            publisher.apply_plan(
                self.archive, self.vault, plan["plan_id"]
            )

        self.assertFalse((self.archive / "_transactions").exists())
        self.assertEqual(
            [
                path
                for path in self.target_root.rglob("*.md")
            ],
            [],
        )

    def test_rollback_detects_user_edit_after_publish(self):
        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        applied = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        daily = next((self.target_root / "日报").glob("*.md"))
        daily.write_text(daily.read_text(encoding="utf-8") + "\n人工追加\n", encoding="utf-8")

        with self.assertRaises(publisher.ConflictError):
            publisher.rollback_transaction(
                self.archive, self.vault, applied["transaction_id"]
            )
        self.assertIn("人工追加", daily.read_text(encoding="utf-8"))

    def test_path_traversal_and_symlink_escape_are_rejected(self):
        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        plan_path = self.archive / "_plans" / f"{plan['plan_id']}.json"
        tampered = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered["changes"][0]["relative_path"] = "../escaped.md"
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(publisher.PathSafetyError):
            publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        outside = self.base / "outside"
        outside.mkdir()
        # 用一个干净 vault 验证现有目录符号链接不能把写入带出目标根。
        other_vault = self.base / "other-vault"
        other_root = other_vault / publisher.TARGET_FOLDER
        other_root.mkdir(parents=True)
        (other_vault / ".obsidian").mkdir()
        (other_root / "主题").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(publisher.PathSafetyError):
            publisher.create_plan(self.archive, other_vault, self.issue, self.draft)
        self.assertEqual(list(outside.iterdir()), [])

    def test_internal_final_target_symlink_blocks_plan_and_apply_without_writes(self):
        index_link = (
            self.target_root / "建设新闻与报纸摘要索引.md"
        )
        plan_referent = self.target_root / "人工索引.md"
        plan_referent.write_text("人工内容", encoding="utf-8")
        index_link.symlink_to(plan_referent)

        with self.assertRaises(publisher.PathSafetyError):
            publisher.create_plan(
                self.archive, self.vault, self.issue, self.draft
            )
        self.assertTrue(index_link.is_symlink())
        self.assertEqual(plan_referent.read_text(encoding="utf-8"), "人工内容")
        self.assertFalse((self.archive / "_plans").exists())

        index_link.unlink()
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        apply_referent = self.target_root / "空白内部文件.md"
        apply_referent.write_bytes(b"")
        index_link.symlink_to(apply_referent)

        with self.assertRaises(publisher.PathSafetyError):
            publisher.apply_plan(
                self.archive, self.vault, plan["plan_id"]
            )
        self.assertTrue(index_link.is_symlink())
        self.assertEqual(apply_referent.read_bytes(), b"")
        self.assertFalse((self.target_root / "日报").exists())
        self.assertFalse((self.archive / "_transactions").exists())

    def test_internal_final_target_symlink_blocks_rollback_before_mutation(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        index_link = (
            self.target_root / "建设新闻与报纸摘要索引.md"
        )
        published = index_link.read_bytes()
        rollback_referent = self.target_root / "人工保留发布内容.md"
        rollback_referent.write_bytes(published)
        index_link.unlink()
        index_link.symlink_to(rollback_referent)
        daily_files = list((self.target_root / "日报").glob("*.md"))
        self.assertTrue(daily_files)
        manifest_path = (
            self.archive / "_transactions" / applied["transaction_id"] /
            "manifest.json"
        )

        with self.assertRaises(publisher.PathSafetyError):
            publisher.rollback_transaction(
                self.archive, self.vault, applied["transaction_id"]
            )

        self.assertTrue(index_link.is_symlink())
        self.assertEqual(rollback_referent.read_bytes(), published)
        self.assertTrue(all(path.exists() for path in daily_files))
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "applied",
        )

    def test_rollback_preflights_missing_snapshot_before_any_vault_write(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        index.write_text("原有人工索引\n", encoding="utf-8")
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        transaction_dir = (
            self.archive / "_transactions" / applied["transaction_id"]
        )
        manifest_path = transaction_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        index_entry = next(
            entry for entry in manifest["entries"]
            if entry["relative_path"].endswith("建设新闻与报纸摘要索引.md")
        )
        (transaction_dir / index_entry["snapshot"]).unlink()
        vault_before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*")
            if path.is_file()
        }

        with self.assertRaises(publisher.PublisherError):
            publisher.rollback_transaction(
                self.archive, self.vault, applied["transaction_id"]
            )

        vault_after = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*")
            if path.is_file()
        }
        self.assertEqual(vault_after, vault_before)
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "applied",
        )

    def test_rollback_rejects_manifest_before_state_tampering_without_writes(self):
        mutations = {
            "before_exists": lambda entry: entry.update({
                "before_exists": not entry["before_exists"]
            }),
            "before_hash": lambda entry: entry.update({
                "before_hash": "0" * 64
            }),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field):
                case_root = self.base / field
                archive = case_root / "archive"
                vault = case_root / "vault"
                target_root = vault / publisher.TARGET_FOLDER
                archive.mkdir(parents=True)
                (vault / ".obsidian").mkdir(parents=True)
                target_root.mkdir()
                self.write_issue_evidence(archive, self.issue)
                self.persist_draft(archive, self.draft)
                plan = publisher.create_plan(
                    archive, vault, self.issue, self.draft
                )
                applied = publisher.apply_plan(
                    archive, vault, plan["plan_id"]
                )
                manifest_path = (
                    archive / "_transactions" / applied["transaction_id"] /
                    "manifest.json"
                )
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                mutate(manifest["entries"][0])
                manifest_path.write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                vault_before = {
                    path.relative_to(vault): path.read_bytes()
                    for path in vault.rglob("*")
                    if path.is_file()
                }

                with self.assertRaisesRegex(
                    publisher.ConflictError, "事务.*计划"
                ):
                    publisher.rollback_transaction(
                        archive, vault, applied["transaction_id"]
                    )

                vault_after = {
                    path.relative_to(vault): path.read_bytes()
                    for path in vault.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(vault_after, vault_before)
                self.assertEqual(
                    json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )["status"],
                    "applied",
                )

    def test_rejects_non_obsidian_root_and_symlink_into_vault_sibling(self):
        plain_directory = self.base / "plain-directory"
        plain_directory.mkdir()
        with self.assertRaises(publisher.PathSafetyError):
            publisher.create_plan(self.archive, plain_directory, self.issue, self.draft)

        guarded_vault = self.base / "guarded-vault"
        guarded_root = guarded_vault / publisher.TARGET_FOLDER
        sibling = guarded_vault / "人工资料"
        (guarded_vault / ".obsidian").mkdir(parents=True)
        guarded_root.mkdir()
        sibling.mkdir()
        (guarded_root / "主题").symlink_to(sibling, target_is_directory=True)

        with self.assertRaises(publisher.PathSafetyError):
            publisher.create_plan(self.archive, guarded_vault, self.issue, self.draft)
        self.assertEqual(list(sibling.iterdir()), [])

    def test_construction_publisher_rejects_other_newspaper_sources(self):
        issue = dict(self.issue, source="rmrb", source_name="人民日报")
        draft = dict(self.draft, source="rmrb")

        with self.assertRaises(publisher.PublisherError):
            publisher.create_plan(self.archive, self.vault, issue, draft)

    def test_plan_rejects_local_pdf_without_verified_issue_date(self):
        linked_by_channel = dict(
            self.issue,
            channel="local_pdf",
            local_pdf_date_verification="unverified",
        )
        linked_by_file = dict(
            self.issue,
            files={"local_pdf": str(self.base / "newspaper.pdf")},
        )

        for issue in (linked_by_channel, linked_by_file):
            with self.subTest(issue=issue):
                with self.assertRaisesRegex(
                    publisher.PublisherError, "PDF.*日期"
                ):
                    publisher.create_plan(
                        self.archive, self.vault, issue, self.draft
                    )

        verified = dict(
            linked_by_file, local_pdf_date_verification="verified"
        )
        plan = publisher.create_plan(
            self.archive, self.vault, verified, self.draft
        )
        self.assertEqual(plan["status"], "planned")

    def test_apply_restore_failure_is_recorded_and_next_retry_recovers(self):
        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        real_write = publisher._atomic_write_bytes
        vault_writes = 0

        def fail_second_vault_write(path, raw):
            nonlocal vault_writes
            path = Path(path).resolve()
            target_root = self.target_root.resolve()
            if path == target_root or target_root in path.parents:
                vault_writes += 1
                if vault_writes == 2:
                    raise OSError("injected apply failure")
            return real_write(path, raw)

        with mock.patch.object(publisher, "_atomic_write_bytes", side_effect=fail_second_vault_write), \
                mock.patch.object(publisher, "_restore_entry", side_effect=OSError("injected restore failure")):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        manifests = list((self.archive / "_transactions").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        failed_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(failed_manifest["status"], "recovery_required")
        self.assertNotEqual(failed_manifest["status"], "failed_restored")
        self.assertTrue(failed_manifest["recovery_errors"])

        recovered = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        self.assertTrue(recovered["applied"])
        self.assertTrue(recovered["recovered_pending_transaction"])
        old_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(old_manifest["status"], "failed_restored")

    def test_apply_never_marks_applied_when_earlier_file_drifts_mid_transaction(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        real_write = publisher._atomic_write_bytes
        target_root = self.target_root.resolve()
        first_target = None
        vault_writes = 0
        concurrent_content = b"Obsidian concurrent edit\n"

        def mutate_first_target_after_second_write(path, raw):
            nonlocal first_target, vault_writes
            path = Path(path)
            resolved = path.resolve()
            is_vault_target = resolved == target_root or target_root in resolved.parents
            result = real_write(path, raw)
            if is_vault_target:
                vault_writes += 1
                if vault_writes == 1:
                    first_target = path
                elif vault_writes == 2:
                    self.assertIsNotNone(first_target)
                    first_target.write_bytes(concurrent_content)
            return result

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=mutate_first_target_after_second_write,
        ):
            with self.assertRaisesRegex(
                publisher.RecoveryError, "无法自动恢复"
            ):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertEqual(first_target.read_bytes(), concurrent_content)
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        self.assertNotEqual(manifest["status"], "applied")
        first_entry = next(
            entry for entry in manifest["entries"]
            if entry["relative_path"] == str(
                first_target.resolve().relative_to(self.vault.resolve())
            )
        )
        self.assertNotEqual(
            publisher._hash_bytes(first_target.read_bytes()),
            first_entry["after_hash"],
        )

    def test_existing_file_edit_at_atomic_swap_is_not_overwritten(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        index.write_text("计划前人工索引\n", encoding="utf-8")
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        concurrent = "Obsidian 在 CAS 交换前保存的人工索引\n"
        real_renameatx = publisher._renameatx_np
        injected = False

        def edit_immediately_before_swap(
                source_fd, source, destination_fd, destination, flags):
            nonlocal injected
            if (not injected
                    and flags & publisher._RENAME_SWAP
                    and destination == index.name):
                injected = True
                index.write_text(concurrent, encoding="utf-8")
            return real_renameatx(
                source_fd, source, destination_fd, destination, flags
            )

        with mock.patch.object(
            publisher, "_renameatx_np", side_effect=edit_immediately_before_swap
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertTrue(injected)
        self.assertEqual(index.read_text(encoding="utf-8"), concurrent)
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "recovery_required")

    def test_target_drift_after_atomic_swap_preserves_both_versions(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        original = "发布计划前的人工索引\n"
        index.write_text(original, encoding="utf-8")
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        concurrent = "Obsidian 在原子交换之后继续保存的内容\n"
        real_renameatx = publisher._renameatx_np
        injected = False

        def edit_immediately_after_swap(
                source_fd, source, destination_fd, destination, flags):
            nonlocal injected
            result = real_renameatx(
                source_fd, source, destination_fd, destination, flags
            )
            if (not injected
                    and flags & publisher._RENAME_SWAP
                    and destination == index.name):
                injected = True
                index.write_text(concurrent, encoding="utf-8")
            return result

        with mock.patch.object(
            publisher, "_renameatx_np", side_effect=edit_immediately_after_swap
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertTrue(injected)
        self.assertEqual(index.read_text(encoding="utf-8"), original)
        preserved = list(index.parent.glob(".readdaily-*"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(encoding="utf-8"), concurrent)
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "recovery_required")

    def test_new_file_created_at_atomic_link_is_not_overwritten(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        concurrent = "Obsidian 在新文件提交前创建的人工索引\n"
        real_link = publisher.os.link
        injected = False

        def create_immediately_before_link(source, destination, **kwargs):
            nonlocal injected
            if not injected and destination == index.name:
                injected = True
                index.write_text(concurrent, encoding="utf-8")
            return real_link(source, destination, **kwargs)

        with mock.patch.object(
            publisher.os, "link", side_effect=create_immediately_before_link
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertTrue(injected)
        self.assertEqual(index.read_text(encoding="utf-8"), concurrent)
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "recovery_required")

    def test_vault_ancestor_swap_to_external_symlink_is_recoverable(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        displaced = self.base / "displaced-vault-target"
        external = self.base / "external-target"
        external.mkdir()
        real_write = publisher._atomic_write_bytes
        swapped = False

        def swap_target_root_before_first_write(path, raw):
            nonlocal swapped
            if isinstance(path, publisher._VaultMutationTarget) and not swapped:
                swapped = True
                self.target_root.rename(displaced)
                self.target_root.symlink_to(external, target_is_directory=True)
            return real_write(path, raw)

        with mock.patch.object(
            publisher, "_atomic_write_bytes",
            side_effect=swap_target_root_before_first_write,
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertTrue(swapped)
        self.assertEqual(list(external.rglob("*")), [])
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "recovery_required")

        # Recovery itself must fail closed while the external symlink remains.
        with self.assertRaises(publisher.PublisherError):
            publisher.apply_plan(
                self.archive, self.vault, plan["plan_id"]
            )
        self.assertEqual(list(external.rglob("*")), [])

        self.target_root.unlink()
        displaced.rename(self.target_root)
        recovered = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        self.assertTrue(recovered["applied"])
        self.assertTrue(recovered["recovered_pending_transaction"])
        self.assertEqual(list(external.rglob("*")), [])
        self.assertFalse(list(self.publisher_state.glob("*.json")))
        for change in plan["changes"]:
            self.assertEqual(
                publisher._hash_bytes(
                    (self.vault / change["relative_path"]).read_bytes()
                ),
                change["after_hash"],
            )

    def test_rollback_existing_file_edit_at_swap_survives(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        index.write_text("发布前人工索引\n", encoding="utf-8")
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        concurrent = "Obsidian 在回滚 CAS 前保存的人工索引\n"
        real_renameatx = publisher._renameatx_np
        injected = False

        def edit_immediately_before_restore_swap(
                source_fd, source, destination_fd, destination, flags):
            nonlocal injected
            if (not injected
                    and flags & publisher._RENAME_SWAP
                    and destination == index.name):
                injected = True
                index.write_text(concurrent, encoding="utf-8")
            return real_renameatx(
                source_fd, source, destination_fd, destination, flags
            )

        with mock.patch.object(
            publisher, "_renameatx_np",
            side_effect=edit_immediately_before_restore_swap,
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.rollback_transaction(
                    self.archive, self.vault, applied["transaction_id"]
                )

        self.assertTrue(injected)
        self.assertEqual(index.read_text(encoding="utf-8"), concurrent)
        manifest_path = (
            self.archive / "_transactions" / applied["transaction_id"] /
            "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "rollback_recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "rollback_recovery_required")

    def test_rollback_new_file_edit_at_delete_cas_survives(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        first_reversed_entry = plan["changes"][-1]
        target = self.vault / first_reversed_entry["relative_path"]
        self.assertFalse(first_reversed_entry["before_exists"])
        concurrent = "Obsidian 在回滚删除前保存的人工内容\n"
        real_renameatx = publisher._renameatx_np
        injected = False

        def edit_immediately_before_delete_move(
                source_fd, source, destination_fd, destination, flags):
            nonlocal injected
            if (not injected
                    and flags & publisher._RENAME_EXCL
                    and source == target.name
                    and destination.startswith(".readdaily-removed-")):
                injected = True
                target.write_text(concurrent, encoding="utf-8")
            return real_renameatx(
                source_fd, source, destination_fd, destination, flags
            )

        with mock.patch.object(
            publisher, "_renameatx_np",
            side_effect=edit_immediately_before_delete_move,
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.rollback_transaction(
                    self.archive, self.vault, applied["transaction_id"]
                )

        self.assertTrue(injected)
        self.assertEqual(target.read_text(encoding="utf-8"), concurrent)
        manifest_path = (
            self.archive / "_transactions" / applied["transaction_id"] /
            "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "rollback_recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "rollback_recovery_required")

    def test_apply_failure_preflights_corrupt_snapshot_without_restore_write(self):
        index = self.target_root / "建设新闻与报纸摘要索引.md"
        index.write_text("原有人工索引\n", encoding="utf-8")
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        real_write = publisher._atomic_write_bytes
        snapshot_path = None
        completed_vault_writes = 0
        failure_injected = False
        restore_vault_writes = 0

        def corrupt_snapshot_then_fail(path, raw):
            nonlocal snapshot_path, completed_vault_writes
            nonlocal failure_injected, restore_vault_writes
            path = Path(path)
            if path.parent.name == "before":
                result = real_write(path, raw)
                snapshot_path = path
                return result
            resolved = path.resolve()
            target_root = self.target_root.resolve()
            if resolved == target_root or target_root in resolved.parents:
                if not failure_injected and completed_vault_writes == 1:
                    self.assertIsNotNone(snapshot_path)
                    snapshot_path.write_bytes(b"corrupt snapshot")
                    failure_injected = True
                    raise OSError("injected second Vault write failure")
                if failure_injected:
                    restore_vault_writes += 1
                else:
                    completed_vault_writes += 1
            return real_write(path, raw)

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=corrupt_snapshot_then_fail,
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        self.assertEqual(completed_vault_writes, 1)
        self.assertEqual(restore_vault_writes, 0)
        self.assertEqual(
            index.read_text(encoding="utf-8"),
            plan["changes"][0]["after"],
        )
        self.assertFalse((self.target_root / "日报").exists())
        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "recovery_required",
        )

    def test_apply_recovers_after_abrupt_crash_between_file_writes(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        real_write = publisher._atomic_write_bytes
        vault_writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal vault_writes
            path = Path(path).resolve()
            target_root = self.target_root.resolve()
            if path == target_root or target_root in path.parents:
                vault_writes += 1
                if vault_writes == 2:
                    raise SimulatedCrash()
            return real_write(path, raw)

        with mock.patch.object(publisher, "_atomic_write_bytes", side_effect=crash_on_second_vault_write):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        manifest_path = next((self.archive / "_transactions").glob("*/manifest.json"))
        crashed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(crashed_manifest["status"], "applying")

        recovered = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        self.assertTrue(recovered["applied"])
        self.assertTrue(recovered["recovered_pending_transaction"])
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "failed_restored",
        )

    def test_changed_draft_does_not_block_pending_apply_recovery(self):
        class SimulatedCrash(BaseException):
            pass

        plan_a = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        vault_before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }
        real_write = publisher._atomic_write_bytes
        vault_writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal vault_writes
            resolved = Path(path).resolve()
            target_root = self.target_root.resolve()
            if resolved == target_root or target_root in resolved.parents:
                vault_writes += 1
                if vault_writes == 2:
                    raise SimulatedCrash()
            return real_write(path, raw)

        with mock.patch.object(
            publisher, "_atomic_write_bytes",
            side_effect=crash_on_second_vault_write,
        ):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(
                    self.archive, self.vault, plan_a["plan_id"]
                )

        draft_b = json.loads(json.dumps(self.draft, ensure_ascii=False))
        draft_b["units"][0]["summary"] = "崩溃后保存的 B 版复核摘要"
        self.persist_draft(self.archive, draft_b)

        with self.assertRaisesRegex(publisher.ConflictError, "草稿.*变化"):
            publisher.apply_plan(
                self.archive, self.vault, plan_a["plan_id"]
            )

        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "failed_restored",
        )
        self.assertFalse(list(self.publisher_state.glob("*.json")))
        self.assertEqual(
            {
                path.relative_to(self.vault): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()
            },
            vault_before,
        )

        plan_b = publisher.create_plan(
            self.archive, self.vault, self.issue, draft_b
        )
        applied_b = publisher.apply_plan(
            self.archive, self.vault, plan_b["plan_id"]
        )
        self.assertTrue(applied_b["applied"])
        daily_change = next(
            change for change in plan_b["changes"]
            if "/日报/" in change["relative_path"]
        )
        daily_path = self.vault / daily_change["relative_path"]
        self.assertIn("崩溃后保存的 B 版复核摘要", daily_path.read_text(encoding="utf-8"))

    def test_replanning_is_blocked_until_incomplete_apply_is_recovered(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        real_write = publisher._atomic_write_bytes
        vault_writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal vault_writes
            path = Path(path).resolve()
            target_root = self.target_root.resolve()
            if path == target_root or target_root in path.parents:
                vault_writes += 1
                if vault_writes == 2:
                    raise SimulatedCrash()
            return real_write(path, raw)

        with mock.patch.object(publisher, "_atomic_write_bytes", side_effect=crash_on_second_vault_write):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(self.archive, self.vault, plan["plan_id"])

        with self.assertRaises(publisher.RecoveryError):
            publisher.create_plan(self.archive, self.vault, self.issue, self.draft)

        old_manifest_path = next((self.archive / "_transactions").glob("*/manifest.json"))
        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(old_manifest["status"], "applying")

        recovered = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        self.assertTrue(recovered["applied"])
        self.assertTrue(recovered["recovered_pending_transaction"])

    def test_new_date_plan_is_blocked_by_incomplete_vault_transaction(self):
        class SimulatedCrash(BaseException):
            pass

        original_plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        real_write = publisher._atomic_write_bytes
        vault_writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal vault_writes
            resolved = Path(path).resolve()
            target_root = self.target_root.resolve()
            if resolved == target_root or target_root in resolved.parents:
                vault_writes += 1
                if vault_writes == 2:
                    raise SimulatedCrash()
            return real_write(path, raw)

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=crash_on_second_vault_write,
        ):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(
                    self.archive, self.vault, original_plan["plan_id"]
                )

        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "applying",
        )
        plan_paths_before = set((self.archive / "_plans").glob("*.json"))
        vault_before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*")
            if path.is_file()
        }
        next_issue = dict(self.issue, date="2026-09-02", issue_no="9169")
        next_draft = dict(self.draft, date="2026-09-02")

        with mock.patch.object(
            publisher,
            "_atomic_write_bytes",
            side_effect=AssertionError("新日期计划不得执行任何写入"),
        ) as blocked_write:
            with self.assertRaisesRegex(
                publisher.RecoveryError, "未完成发布事务"
            ):
                publisher.create_plan(
                    self.archive, self.vault, next_issue, next_draft
                )
        blocked_write.assert_not_called()
        self.assertEqual(
            set((self.archive / "_plans").glob("*.json")),
            plan_paths_before,
        )
        self.assertEqual(
            {
                path.relative_to(self.vault): path.read_bytes()
                for path in self.vault.rglob("*")
                if path.is_file()
            },
            vault_before,
        )

        recovered = publisher.apply_plan(
            self.archive, self.vault, original_plan["plan_id"]
        )
        self.assertTrue(recovered["applied"])
        self.assertTrue(recovered["recovered_pending_transaction"])

    def test_rollback_resumes_after_abrupt_crash(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        applied = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        transaction_id = applied["transaction_id"]
        real_restore = publisher._restore_entry
        restored = 0

        def crash_after_first_restore(
                vault_root, transaction_dir, entry, snapshot_bytes=None,
                vault_session=None):
            nonlocal restored
            real_restore(
                vault_root,
                transaction_dir,
                entry,
                snapshot_bytes=snapshot_bytes,
                vault_session=vault_session,
            )
            restored += 1
            if restored == 1:
                raise SimulatedCrash()

        with mock.patch.object(publisher, "_restore_entry", side_effect=crash_after_first_restore):
            with self.assertRaises(SimulatedCrash):
                publisher.rollback_transaction(self.archive, self.vault, transaction_id)

        manifest_path = self.archive / "_transactions" / transaction_id / "manifest.json"
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "rolling_back",
        )
        resumed = publisher.rollback_transaction(self.archive, self.vault, transaction_id)
        self.assertTrue(resumed["rolled_back"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "rolled_back",
        )

    def test_rollback_restore_failure_is_recorded_and_retryable(self):
        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        applied = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        transaction_id = applied["transaction_id"]

        with mock.patch.object(
            publisher, "_restore_entry", side_effect=OSError("injected rollback failure")
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.rollback_transaction(self.archive, self.vault, transaction_id)

        manifest_path = self.archive / "_transactions" / transaction_id / "manifest.json"
        failed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(failed_manifest["status"], "rollback_recovery_required")
        self.assertTrue(failed_manifest["recovery_errors"])

        retried = publisher.rollback_transaction(self.archive, self.vault, transaction_id)
        self.assertTrue(retried["rolled_back"])
        self.assertTrue(retried["resumed"])

    def test_rollback_unlink_fsync_failure_keeps_recovery_sentinel(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        transaction_id = applied["transaction_id"]
        vault_directories = {
            (path.stat().st_dev, path.stat().st_ino)
            for path in self.target_root.rglob("*") if path.is_dir()
        }
        vault_directories.add(
            (self.target_root.stat().st_dev, self.target_root.stat().st_ino)
        )
        real_fsync = publisher.os.fsync
        failed = False

        def fail_first_vault_directory_fsync(descriptor):
            nonlocal failed
            descriptor_stat = os.fstat(descriptor)
            identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if (not failed and stat.S_ISDIR(descriptor_stat.st_mode)
                    and identity in vault_directories):
                failed = True
                raise OSError("injected Vault directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            publisher.os, "fsync",
            side_effect=fail_first_vault_directory_fsync,
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.rollback_transaction(
                    self.archive, self.vault, transaction_id
                )

        self.assertTrue(failed)
        manifest_path = (
            self.archive / "_transactions" / transaction_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "rollback_recovery_required")
        _path, sentinel = publisher._load_vault_sentinel(self.vault)
        self.assertEqual(sentinel["phase"], "rollback_recovery_required")

        retried = publisher.rollback_transaction(
            self.archive, self.vault, transaction_id
        )
        self.assertTrue(retried["rolled_back"])
        self.assertTrue(retried["resumed"])
        self.assertFalse(list(self.publisher_state.glob("*.json")))

    def test_cross_archive_pending_apply_blocks_same_vault_until_recovered(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        real_write = publisher._atomic_write_bytes
        writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal writes
            resolved = Path(path).resolve()
            target_root = self.target_root.resolve()
            if resolved == target_root or target_root in resolved.parents:
                writes += 1
                if writes == 2:
                    raise SimulatedCrash()
            return real_write(path, raw)

        with mock.patch.object(
            publisher, "_atomic_write_bytes",
            side_effect=crash_on_second_vault_write,
        ):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        archive_b = self.base / "archive-b"
        archive_b.mkdir()
        next_issue, next_draft = self.issue_and_draft_for(
            archive_b, "2026-09-02", "9169"
        )
        vault_before = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }
        with self.assertRaisesRegex(publisher.RecoveryError, "原事务"):
            publisher.create_plan(
                archive_b, self.vault, next_issue, next_draft
            )
        self.assertEqual(
            {
                path.relative_to(self.vault): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()
            },
            vault_before,
        )

        recovered = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        self.assertTrue(recovered["recovered_pending_transaction"])
        self.assertFalse(list(self.publisher_state.glob("*.json")))

    def test_rolling_back_blocks_new_plan_until_original_rollback_resumes(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        applied = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        real_restore = publisher._restore_entry
        restores = 0

        def crash_after_one_restore(
                vault_root, transaction_dir, entry, snapshot_bytes=None,
                vault_session=None):
            nonlocal restores
            real_restore(
                vault_root, transaction_dir, entry,
                snapshot_bytes=snapshot_bytes,
                vault_session=vault_session,
            )
            restores += 1
            if restores == 1:
                raise SimulatedCrash()

        with mock.patch.object(
            publisher, "_restore_entry", side_effect=crash_after_one_restore
        ):
            with self.assertRaises(SimulatedCrash):
                publisher.rollback_transaction(
                    self.archive, self.vault, applied["transaction_id"]
                )

        next_issue, next_draft = self.issue_and_draft_for(
            self.archive, "2026-09-02", "9169"
        )
        with self.assertRaisesRegex(publisher.RecoveryError, "原事务"):
            publisher.create_plan(
                self.archive, self.vault, next_issue, next_draft
            )
        resumed = publisher.rollback_transaction(
            self.archive, self.vault, applied["transaction_id"]
        )
        self.assertTrue(resumed["resumed"])
        publisher.create_plan(
            self.archive, self.vault, next_issue, next_draft
        )

    def test_applied_metadata_failure_blocks_new_plan_until_same_plan_repairs(self):
        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        with mock.patch.object(
            publisher, "_mark_published", side_effect=OSError("disk full")
        ):
            with self.assertRaises(publisher.RecoveryError):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "applied")
        self.assertTrue(manifest["metadata_errors"])
        next_issue, next_draft = self.issue_and_draft_for(
            self.archive, "2026-09-02", "9169"
        )
        with self.assertRaisesRegex(publisher.RecoveryError, "原事务"):
            publisher.create_plan(
                self.archive, self.vault, next_issue, next_draft
            )

        repaired = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        self.assertTrue(repaired["idempotent"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn("metadata_errors", manifest)
        self.assertFalse(list(self.publisher_state.glob("*.json")))
        publisher.create_plan(
            self.archive, self.vault, next_issue, next_draft
        )

    def test_prewrite_prepared_crash_is_abandoned_without_overwriting_new_archive(self):
        class SimulatedCrash(BaseException):
            pass

        plan_a = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        with mock.patch.object(
            publisher, "_claim_vault_sentinel", side_effect=SimulatedCrash()
        ):
            with self.assertRaises(SimulatedCrash):
                publisher.apply_plan(
                    self.archive, self.vault, plan_a["plan_id"]
                )
        manifest_a_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(
            json.loads(manifest_a_path.read_text(encoding="utf-8"))["status"],
            "prepared",
        )
        self.assertFalse(list(self.publisher_state.glob("*.json")))

        archive_b = self.base / "archive-b"
        archive_b.mkdir()
        issue_b, draft_b = self.issue_and_draft_for(
            archive_b, "2026-09-02", "9169"
        )
        plan_b = publisher.create_plan(
            archive_b, self.vault, issue_b, draft_b
        )
        publisher.apply_plan(archive_b, self.vault, plan_b["plan_id"])
        vault_after_b = {
            path.relative_to(self.vault): path.read_bytes()
            for path in self.vault.rglob("*") if path.is_file()
        }

        with self.assertRaises(publisher.ConflictError):
            publisher.apply_plan(
                self.archive, self.vault, plan_a["plan_id"]
            )
        self.assertEqual(
            json.loads(manifest_a_path.read_text(encoding="utf-8"))["status"],
            "failed_restored",
        )
        self.assertFalse(list(self.publisher_state.glob("*.json")))
        self.assertEqual(
            {
                path.relative_to(self.vault): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()
            },
            vault_after_b,
        )

    def test_retry_clears_sentinel_left_after_failed_restored_manifest(self):
        class ApplyCrash(BaseException):
            pass

        class ClearCrash(BaseException):
            pass

        plan = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        real_write = publisher._atomic_write_bytes
        writes = 0

        def crash_on_second_vault_write(path, raw):
            nonlocal writes
            resolved = Path(path).resolve()
            target_root = self.target_root.resolve()
            if resolved == target_root or target_root in resolved.parents:
                writes += 1
                if writes == 2:
                    raise ApplyCrash()
            return real_write(path, raw)

        with mock.patch.object(
            publisher, "_atomic_write_bytes",
            side_effect=crash_on_second_vault_write,
        ):
            with self.assertRaises(ApplyCrash):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        with mock.patch.object(
            publisher, "_clear_vault_sentinel", side_effect=ClearCrash()
        ):
            with self.assertRaises(ClearCrash):
                publisher.apply_plan(
                    self.archive, self.vault, plan["plan_id"]
                )

        manifest_path = next(
            (self.archive / "_transactions").glob("*/manifest.json")
        )
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["status"],
            "failed_restored",
        )
        self.assertTrue(list(self.publisher_state.glob("*.json")))

        recovered = publisher.apply_plan(
            self.archive, self.vault, plan["plan_id"]
        )
        self.assertTrue(recovered["applied"])
        self.assertFalse(list(self.publisher_state.glob("*.json")))

    def test_matching_vault_content_without_local_transaction_is_not_false_success(self):
        plan_a = publisher.create_plan(
            self.archive, self.vault, self.issue, self.draft
        )
        publisher.apply_plan(self.archive, self.vault, plan_a["plan_id"])

        archive_b = self.base / "archive-b"
        archive_b.mkdir()
        issue_b, draft_b = self.issue_and_draft_for(
            archive_b, self.issue["date"], self.issue["issue_no"]
        )
        plan_b = publisher.create_plan(
            archive_b, self.vault, issue_b, draft_b
        )
        with self.assertRaisesRegex(
            publisher.ConflictError, "没有可验证的发布事务所有权记录"
        ):
            publisher.apply_plan(
                archive_b, self.vault, plan_b["plan_id"]
            )
        self.assertFalse((archive_b / "_state").exists())

    def test_issue_number_correction_reuses_one_stable_daily_card(self):
        initial_issue = json.loads(json.dumps(self.issue, ensure_ascii=False))
        initial_issue["issue_no"] = None
        initial_issue.pop("evidence_sha256", None)
        initial_issue.pop("archive_evidence_sha256", None)
        initial_draft = json.loads(json.dumps(self.draft, ensure_ascii=False))
        initial_draft.pop("evidence_sha256", None)
        self.write_issue_evidence(self.archive, initial_issue)
        initial_draft["evidence_sha256"] = initial_issue["evidence_sha256"]
        self.persist_draft(self.archive, initial_draft)

        first_plan = publisher.create_plan(
            self.archive, self.vault, initial_issue, initial_draft
        )
        publisher.apply_plan(
            self.archive, self.vault, first_plan["plan_id"]
        )

        corrected_issue = json.loads(json.dumps(initial_issue, ensure_ascii=False))
        corrected_issue["issue_no"] = "9168"
        corrected_issue.pop("evidence_sha256", None)
        corrected_issue.pop("archive_evidence_sha256", None)
        self.write_issue_evidence(self.archive, corrected_issue)
        corrected_draft = json.loads(json.dumps(initial_draft, ensure_ascii=False))
        corrected_draft["evidence_sha256"] = corrected_issue["evidence_sha256"]
        self.persist_draft(self.archive, corrected_draft)
        second_plan = publisher.create_plan(
            self.archive, self.vault, corrected_issue, corrected_draft
        )
        publisher.apply_plan(
            self.archive, self.vault, second_plan["plan_id"]
        )

        daily_cards = list((self.target_root / "日报").glob("*.md"))
        self.assertEqual(len(daily_cards), 1)
        self.assertEqual(
            daily_cards[0].name,
            "2026-09-01 中国建设报摘要.md",
        )
        self.assertIn("期号：9168", daily_cards[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
