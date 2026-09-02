import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
READER_SCRIPTS = ROOT / "skills" / "newspaper-reader" / "scripts"
sys.path.insert(0, str(READER_SCRIPTS))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publisher = load_module("vault_publisher", READER_SCRIPTS / "vault_publisher.py")


class VaultPublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.archive = self.base / "archive"
        self.vault = self.base / "vault"
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

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_rollback_resumes_after_abrupt_crash(self):
        class SimulatedCrash(BaseException):
            pass

        plan = publisher.create_plan(self.archive, self.vault, self.issue, self.draft)
        applied = publisher.apply_plan(self.archive, self.vault, plan["plan_id"])
        transaction_id = applied["transaction_id"]
        real_restore = publisher._restore_entry
        restored = 0

        def crash_after_first_restore(vault_root, transaction_dir, entry):
            nonlocal restored
            real_restore(vault_root, transaction_dir, entry)
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


if __name__ == "__main__":
    unittest.main()
