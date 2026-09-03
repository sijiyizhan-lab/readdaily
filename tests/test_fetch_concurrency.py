import argparse
import builtins
import contextlib
import datetime
import errno
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
READER_SCRIPTS = ROOT / "skills" / "newspaper-reader" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))
sys.path.insert(0, str(READER_SCRIPTS))

import fetch  # noqa: E402
import lib  # noqa: E402
import vault_publisher  # noqa: E402
from adapters import cms_index, founder, mobile_epaper, paper_api, wechat_read  # noqa: E402


DAY = datetime.date(2026, 9, 3)


def _use_tmpdir(path):
    os.environ["TMPDIR"] = path
    tempfile.tempdir = None


def _publisher_source_lock_worker(
        archive, source, day, ready, entered, tmpdir=None):
    if tmpdir is not None:
        _use_tmpdir(tmpdir)
    ready.set()
    with vault_publisher._fetch_source_evidence_lock(
            archive, source, day):
        entered.set()


def _fetch_batch_lock_worker(
        archive, day, ready, entered, rejected, tmpdir):
    _use_tmpdir(tmpdir)
    ready.set()
    try:
        with fetch.fetch_date_lock(archive, day):
            entered.set()
    except fetch.FetchLockedError:
        rejected.set()


def _publisher_transaction_lock_worker(
        archive, vault, ready, entered, tmpdir):
    _use_tmpdir(tmpdir)
    with vault_publisher._publisher_operation_io(archive, vault):
        ready.set()
        with vault_publisher._publisher_transaction_lock(archive, vault):
            entered.set()


def _sources(count=8):
    return [
        {
            "id": "paper%s" % index,
            "name": "报纸%s" % index,
            "channel": "cms_index",
            "enabled": True,
        }
        for index in range(count)
    ]


class FetchConcurrencyTests(unittest.TestCase):
    def test_archive_sessions_race_safely_when_creating_shared_state_parent(self):
        worker_count = 4
        repeat_count = 12
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            archive.mkdir()
            real_mkdir = lib.os.mkdir

            for round_index in range(repeat_count):
                shared_parent = "_state_race_%02d" % round_index
                at_mkdir = threading.Barrier(worker_count)
                errors = []
                errors_guard = threading.Lock()

                def synchronized_mkdir(name, mode=0o777, **kwargs):
                    if name == shared_parent:
                        at_mkdir.wait(timeout=5)
                    return real_mkdir(name, mode, **kwargs)

                def create_source_parent(worker_index):
                    try:
                        with lib.archive_session(
                                archive, create=False) as session:
                            session.makedirs(
                                os.path.join(
                                    shared_parent,
                                    "paper%s" % worker_index,
                                ),
                                exist_ok=True,
                            )
                    except BaseException as exc:  # noqa: BLE001
                        with errors_guard:
                            errors.append(exc)

                threads = [
                    threading.Thread(
                        target=create_source_parent, args=(worker_index,)
                    )
                    for worker_index in range(worker_count)
                ]
                with mock.patch.object(
                        lib.os, "mkdir", side_effect=synchronized_mkdir):
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=10)

                self.assertTrue(
                    all(not thread.is_alive() for thread in threads),
                    "并发目录创建线程超时",
                )
                self.assertEqual(errors, [])
                parent = archive / shared_parent
                self.assertTrue(parent.is_dir())
                self.assertFalse(parent.is_symlink())
                self.assertEqual(
                    {path.name for path in parent.iterdir()},
                    {"paper%s" % index for index in range(worker_count)},
                )

    def test_eight_sources_use_bounded_four_worker_parallelism(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        archive = Path(temporary.name) / "archive"
        archive.mkdir()
        sources = _sources()
        release = threading.Event()
        first_wave_ready = threading.Event()
        guard = threading.Lock()
        observed = {"active": 0, "maximum": 0, "started": 0}
        result = {}

        def fake_task(
                src, day, root, args, daily_log,
                coordinator_archive=None):
            del src, day, root, args, daily_log, coordinator_archive
            with guard:
                observed["active"] += 1
                observed["started"] += 1
                observed["maximum"] = max(
                    observed["maximum"], observed["active"]
                )
                if observed["active"] == 4:
                    first_wave_ready.set()
            if not release.wait(timeout=5):
                raise RuntimeError("并发测试未收到释放信号")
            with guard:
                observed["active"] -= 1
            return "ok"

        def run_batch():
            try:
                result["counts"] = fetch.run_sources(
                    sources, DAY, str(archive), argparse.Namespace(),
                    str(archive / "_dailylog.jsonl"), workers=4,
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        with mock.patch.object(
                fetch, "_run_source_task", side_effect=fake_task):
            runner = threading.Thread(target=run_batch)
            runner.start()
            self.assertTrue(
                first_wave_ready.wait(timeout=5),
                "首批四个来源未并发进入",
            )
            time.sleep(0.05)
            with guard:
                self.assertEqual(observed["maximum"], 4)
                self.assertEqual(observed["started"], 4)
            release.set()
            runner.join(timeout=5)

        self.assertFalse(runner.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["counts"], {
            "ok": 8, "failed": 0, "skipped": 0,
        })
        self.assertEqual(observed["maximum"], 4)

    def test_collector_interrupt_cancels_every_queued_source(self):
        sources = _sources(5)
        release = threading.Event()
        started = []
        started_guard = threading.Lock()

        def blocking_task(src, *_args):
            with started_guard:
                started.append(src["id"])
            if not release.wait(timeout=5):
                raise RuntimeError("中断测试未释放运行中来源")
            return "ok"

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                fetch, "_run_source_task", side_effect=blocking_task
        ), mock.patch.object(
                fetch, "as_completed", side_effect=KeyboardInterrupt
        ):
            timer = threading.Timer(0.2, release.set)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    fetch.run_sources(
                        sources, DAY, td, argparse.Namespace(),
                        str(Path(td) / "_dailylog.jsonl"), workers=1,
                    )
            finally:
                release.set()
                timer.cancel()
                timer.join(timeout=1)

        self.assertEqual(started, ["paper0"])

    def test_submit_interrupt_cancels_and_waits_for_running_source(self):
        sources = _sources(5)
        release = threading.Event()
        started = []
        real_submit = fetch.ThreadPoolExecutor.submit
        submit_count = {"value": 0}
        untracked = []

        def blocking_task(src, *_args):
            started.append(src["id"])
            if not release.wait(timeout=5):
                raise RuntimeError("提交中断测试未释放运行中来源")
            return "ok"

        def interrupt_second_submit(executor, *args, **kwargs):
            submit_count["value"] += 1
            future = real_submit(executor, *args, **kwargs)
            if submit_count["value"] == 2:
                untracked.append(future)
                raise KeyboardInterrupt()
            return future

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
                fetch, "_run_source_task", side_effect=blocking_task
        ), mock.patch.object(
                fetch.ThreadPoolExecutor, "submit",
                new=interrupt_second_submit,
        ):
            timer = threading.Timer(0.2, release.set)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    fetch.run_sources(
                        sources, DAY, td, argparse.Namespace(),
                        str(Path(td) / "_dailylog.jsonl"), workers=1,
                    )
            finally:
                release.set()
                timer.cancel()
                timer.join(timeout=1)

        self.assertEqual(submit_count["value"], 2)
        self.assertEqual(started, ["paper0"])
        self.assertEqual(len(untracked), 1)
        self.assertTrue(untracked[0].cancelled())

    def test_upstream_failure_isolated_but_archive_conflict_is_fatal(self):
        sources = _sources(3)
        recorded = []

        def isolated_run(src, *_args):
            if src["id"] == "paper0":
                raise TimeoutError("upstream timeout")
            return "ok"

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(fetch, "_run_source", side_effect=isolated_run), \
                mock.patch.object(
                    fetch, "_record_source_failure",
                    side_effect=lambda _root, _log, src, _day, _note:
                    recorded.append(src["id"]),
                ), contextlib.redirect_stdout(io.StringIO()):
            counts = fetch.run_sources(
                sources, DAY, td, argparse.Namespace(),
                str(Path(td) / "_dailylog.jsonl"), workers=3,
            )

        self.assertEqual(counts, {"ok": 2, "failed": 1, "skipped": 0})
        self.assertEqual(recorded, ["paper0"])

        fatal_errors = tuple(
            error_type("pipeline cannot continue")
            for error_type in lib.PIPELINE_FATAL_EXCEPTIONS
        )
        for fatal_error in fatal_errors:
            with self.subTest(error=type(fatal_error).__name__):
                def fatal_run(src, *_args):
                    if src["id"] == "paper0":
                        raise fatal_error
                    return "ok"

                with tempfile.TemporaryDirectory() as td, \
                        mock.patch.object(
                            fetch, "_run_source", side_effect=fatal_run
                        ), mock.patch.object(
                            fetch, "_record_source_failure"
                        ) as record, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(type(fatal_error)):
                        fetch.run_sources(
                            sources, DAY, td, argparse.Namespace(),
                            str(Path(td) / "_dailylog.jsonl"), workers=1,
                        )
                record.assert_not_called()

    def test_issue_digest_archive_fatal_is_not_downgraded_to_missing_evidence(self):
        for error_type in (
                lib.ArchivePathSafetyError,
                lib.ArchiveConflictError):
            with self.subTest(error=error_type.__name__), mock.patch.object(
                    lib, "read_tree_files",
                    side_effect=error_type("evidence tree changed"),
            ):
                with self.assertRaises(error_type):
                    fetch._issue_evidence_digest(
                        "/unused/issue", "paper0", DAY
                    )

        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            archive.mkdir()
            paths = lib.archive_paths(archive, "paper0", DAY)
            state_path = Path(paths["state"])
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "stages": {"fetched": "2026-09-03T09:00:00"},
                "state_schema_version": fetch.STATE_SCHEMA_VERSION,
                "pipeline_contract_version": fetch.PIPELINE_CONTRACT_VERSION,
                "adapter_contract_version": fetch._stage_contract_version(
                    _sources(1)[0], "fetched"
                ),
            }), encoding="utf-8")
            args = argparse.Namespace(
                stage="fetched", no_state_skip=False, offline=False
            )
            fatal = lib.ArchivePathSafetyError(
                "evidence tree escaped archive"
            )
            with mock.patch.object(
                    fetch, "_issue_evidence_digest", side_effect=fatal
            ), mock.patch.object(fetch, "_record_source_failure") as record:
                with self.assertRaises(lib.ArchivePathSafetyError):
                    fetch.run_sources(
                        _sources(1), DAY, archive, args,
                        str(archive / "_dailylog.jsonl"), workers=1,
                    )
            record.assert_not_called()

    def test_completed_fetch_and_parse_reuse_one_evidence_digest(self):
        source = _sources(1)[0]
        digest = "a" * 64
        state = {
            "stages": {
                "fetched": "2026-09-03T09:00:00",
                "parsed": "2026-09-03T09:01:00",
            },
            "state_schema_version": fetch.STATE_SCHEMA_VERSION,
            "pipeline_contract_version": fetch.PIPELINE_CONTRACT_VERSION,
            "adapter_contract_version": fetch._stage_contract_version(
                source, "fetched"
            ),
            "parser_contract_version": fetch._stage_contract_version(
                source, "parsed"
            ),
            "issue_evidence_sha256": digest,
        }
        args = argparse.Namespace(
            stage="fetched,parsed", no_state_skip=False, offline=False
        )
        with tempfile.TemporaryDirectory() as td, \
                lib.archive_session(td, create=False), \
                mock.patch.object(fetch.lib, "load_json", return_value=state), \
                mock.patch.object(
                    fetch, "_issue_evidence_digest", return_value=digest
                ) as evidence, mock.patch.object(
                    fetch, "load_adapter", return_value=mock.Mock()
                ), contextlib.redirect_stdout(io.StringIO()):
            result = fetch._run_source_in_archive_session(
                source, DAY, td, args, str(Path(td) / "_dailylog.jsonl")
            )

        self.assertEqual(result, "skipped")
        evidence.assert_called_once()

    def test_refresh_snapshot_double_failure_remains_batch_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td)
            issue = archive / "paper0" / DAY.isoformat()
            state = archive / "_state" / "paper0" / (DAY.isoformat() + ".json")
            issue.mkdir(parents=True)
            (issue / "issue.json").write_text("{}", encoding="utf-8")
            primary = lib.ArchiveConflictError("archive identity changed")
            cleanup = OSError(errno.EIO, "cleanup failed")
            with mock.patch.object(
                    lib, "copy_directory_tree", side_effect=primary
            ), mock.patch.object(
                    lib, "durable_rmtree", side_effect=cleanup
            ):
                with self.assertRaises(fetch.FetchFatalError) as caught:
                    fetch._IssueRefreshTransaction(issue, state)

        self.assertIs(caught.exception.primary_error, primary)
        self.assertIs(caught.exception.cleanup_error, cleanup)
        self.assertIs(caught.exception.__cause__, primary)

    def test_archive_temporary_cleanup_double_failures_are_typed(self):
        with tempfile.TemporaryDirectory() as td, \
                lib.archive_session(td, create=False) as session:
            conflict = lib.ArchiveConflictError("archive changed")
            with mock.patch.object(
                    lib.os, "replace", side_effect=conflict
            ), mock.patch.object(
                    lib, "fsync_directory",
                    side_effect=OSError(errno.EIO, "cleanup sync failed"),
            ):
                with self.assertRaises(
                        lib.ArchiveTransactionError) as atomic_error:
                    session.atomic_write("state.json", b"{}")
            self.assertIs(atomic_error.exception.primary_error, conflict)
            self.assertIs(atomic_error.exception.__cause__, conflict)

            source = Path(td) / "source.pdf"
            source.write_bytes(b"payload")
            conflict = lib.ArchiveConflictError("archive changed")
            with mock.patch.object(
                    lib.os, "replace", side_effect=conflict
            ), mock.patch.object(
                    lib, "fsync_directory",
                    side_effect=OSError(errno.EIO, "cleanup sync failed"),
            ):
                with self.assertRaises(
                        lib.ArchiveTransactionError) as copy_error:
                    session.copy_file_from_path(source, "copy.pdf")
            self.assertIs(copy_error.exception.primary_error, conflict)
            self.assertIs(copy_error.exception.__cause__, conflict)

            with mock.patch.object(
                    lib, "fsync_directory",
                    side_effect=[
                        MemoryError("out of memory"),
                        OSError(errno.EIO, "cleanup sync failed"),
                    ],
            ):
                with self.assertRaises(
                        lib.ArchiveTransactionError) as directory_error:
                    session.make_temp_dir(prefix=".double-failure.")
            self.assertIsInstance(
                directory_error.exception.primary_error, MemoryError
            )
            self.assertIsInstance(
                directory_error.exception.cleanup_error,
                lib.ArchiveTransactionError,
            )

            primary = MemoryError("stream allocation failed")
            with mock.patch.object(
                    lib.os, "close",
                    side_effect=OSError(errno.EIO, "close failed"),
            ):
                with self.assertRaises(
                        lib.ArchiveTransactionError) as close_error:
                    lib._close_archive_descriptor(
                        123, "归档读取", primary_error=primary
                    )
            self.assertIs(close_error.exception.primary_error, primary)
            self.assertIs(close_error.exception.__cause__, primary)

    def test_adapter_broad_handlers_propagate_archive_fatal_errors(self):
        day = DAY

        def invoke_founder(error):
            source = {
                "id": "gmrb", "name": "光明日报",
                "entry": "https://example.test/",
                "node_tpl": "https://example.test/{y}{m}{d}/node_{page:02d}.html",
                "max_pages": 1,
            }
            with mock.patch.object(
                    founder.lib, "http_get", side_effect=error):
                founder._probe_pages(source, day)

        def invoke_mobile(error):
            source = {
                "id": "bjrb", "name": "北京日报",
                "mob": {
                    "index_tpl": "https://example.test/{y}/{yymmdd}/index.html",
                    "site": "https://example.test/",
                },
            }
            final_url = "https://example.test/20260903/index.html"
            with mock.patch.object(
                    mobile_epaper.lib, "http_get",
                    side_effect=[(200, final_url, b"index"), error],
            ), mock.patch.object(
                    mobile_epaper, "_parse_index", return_value=([], [], [])
            ), mock.patch.object(
                    mobile_epaper, "_inventory",
                    return_value=([
                        ("page.pdf", 1, "要闻", "20260903_001")
                    ], {"20260903_001": []}, None),
            ):
                mobile_epaper.fetch(source, day, "/unused/archive")

        def invoke_paper_api(error):
            source = {
                "entry": "https://example.test/",
                "api": {"base": "https://example.test/api"},
            }
            with mock.patch.object(
                    paper_api.lib, "http_post_json", side_effect=error):
                paper_api._api(source, "/period", {"date": day.isoformat()})

        def invoke_cms(error):
            site = "https://example.test/"
            page_url = (
                "https://example.test/20260903/20260903_1_1.html"
            )
            source = {
                "id": "nmrb", "name": "农民日报",
                "cms": {
                    "site": site,
                    "index_json": site + "index.json",
                    "paper_code": "nmrb",
                    "max_pages": 2,
                },
            }
            entry = {
                "paperCode": "nmrb",
                "pagePath": "20260903/20260903_1_1.html",
                "paperDate": "2026-09-03",
                "paperIssueNum": "nmrb_1",
            }
            with mock.patch.object(
                    cms_index, "_get", return_value=(
                        200, json.dumps({"papers": [entry]})
                    )
            ), mock.patch.object(
                    cms_index, "_matching_entry", return_value=entry
            ), mock.patch.object(
                    cms_index, "_get_response", side_effect=[
                        (200, page_url, "page-one"),
                        (200, page_url, "edition-one"),
                        error,
                    ]
            ), mock.patch.object(
                    cms_index, "_hrefs", side_effect=[
                        ["20260903_1_1.html"],
                        ["20260903_1_1_1.html"],
                    ]
            ):
                cms_index.fetch(source, day, "/unused/archive")

        def invoke_wechat(error):
            engine = mock.Mock()
            engine.verify_artifacts.side_effect = error
            wechat_read._verified_daily_artifacts(
                engine, "/unused/out", day
            )

        invokers = {
            "founder": invoke_founder,
            "mobile_epaper": invoke_mobile,
            "paper_api": invoke_paper_api,
            "cms_index": invoke_cms,
            "wechat_read": invoke_wechat,
        }
        for error_type in lib.PIPELINE_FATAL_EXCEPTIONS:
            for adapter_name, invoke in invokers.items():
                with self.subTest(
                        adapter=adapter_name, error=error_type.__name__):
                    with self.assertRaises(error_type):
                        invoke(error_type("archive safety failure"))

    def test_lock_io_errors_are_typed_fatal_not_false_contention(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            archive.mkdir()

            with mock.patch.object(
                    fetch.fcntl, "flock",
                    side_effect=OSError(errno.EIO, "lock I/O failed")):
                with self.assertRaises(lib.ArchivePathSafetyError):
                    with fetch.fetch_source_evidence_lock(
                            archive, "paper0", DAY):
                        pass
                with self.assertRaises(lib.ArchivePathSafetyError):
                    with fetch.fetch_date_lock(archive, DAY):
                        pass

            real_fstat = os.fstat
            operations = ("fstat", "ftruncate", "write", "fsync")
            for operation in operations:
                with self.subTest(operation=operation):
                    real_operation = getattr(os, operation)

                    def fail_regular_descriptor(
                            descriptor, *args, _real=real_operation):
                        if stat.S_ISREG(real_fstat(descriptor).st_mode):
                            raise OSError(errno.EIO, "lock metadata I/O failed")
                        return _real(descriptor, *args)

                    with mock.patch.object(
                            fetch.os, operation,
                            side_effect=fail_regular_descriptor):
                        with self.assertRaises(lib.ArchivePathSafetyError):
                            with fetch.fetch_source_evidence_lock(
                                    archive, "paper0", DAY):
                                pass

    def test_json_and_log_symlinks_abort_batch_without_external_write(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            archive = base / "archive"
            archive.mkdir()
            outside_json = base / "outside.json"
            outside_json.write_text('{"outside":true}', encoding="utf-8")
            linked_json = archive / "issue.json"
            linked_json.symlink_to(outside_json)
            with self.assertRaises(lib.ArchivePathSafetyError):
                lib.load_json(linked_json, {"fallback": True})

            linked_json.unlink()
            outside_log = base / "outside.log"
            outside_log.write_bytes(b"sentinel")
            (archive / "_dailylog.jsonl").symlink_to(outside_log)

            def write_log(_src, _day, _root, _args, daily_log):
                lib.log_line(daily_log, {"source": "paper0"})
                return "ok"

            with mock.patch.object(
                    fetch, "_run_source", side_effect=write_log
            ), mock.patch.object(fetch, "_record_source_failure") as record:
                with self.assertRaises(lib.ArchivePathSafetyError):
                    fetch.run_sources(
                        _sources(1), DAY, archive, argparse.Namespace(),
                        str(archive / "_dailylog.jsonl"), workers=1,
                    )
            record.assert_not_called()
            self.assertEqual(outside_log.read_bytes(), b"sentinel")

    def test_opened_directory_swap_is_a_batch_fatal_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            archive = base / "archive"
            victim = archive / "victim"
            outside = base / "outside"
            victim.mkdir(parents=True)
            outside.mkdir()
            real_open = os.open
            swapped = {"value": False}

            def swap_before_secure_open(path, flags, *args, **kwargs):
                if path == "victim" and not swapped["value"]:
                    victim.rename(archive / "victim-original")
                    victim.symlink_to(outside, target_is_directory=True)
                    swapped["value"] = True
                return real_open(path, flags, *args, **kwargs)

            def open_victim(*_args):
                session = lib.current_archive_session(archive)
                with session.opened_dir("victim"):
                    pass
                return "ok"

            with mock.patch.object(
                    lib.os, "open", side_effect=swap_before_secure_open
            ), mock.patch.object(
                    fetch, "_run_source", side_effect=open_victim
            ), mock.patch.object(fetch, "_record_source_failure") as record:
                with self.assertRaises(lib.ArchiveConflictError):
                    fetch.run_sources(
                        _sources(1), DAY, archive, argparse.Namespace(),
                        str(archive / "_dailylog.jsonl"), workers=1,
                    )
            self.assertTrue(swapped["value"])
            record.assert_not_called()

    def test_failure_accounting_does_not_swallow_pipeline_fatal_errors(self):
        for fatal in (
                lib.ArchiveTransactionError("rollback incomplete"),
                MemoryError("out of memory")):
            with self.subTest(error=type(fatal).__name__), \
                    tempfile.TemporaryDirectory() as td, mock.patch.object(
                        lib, "state_mark", side_effect=fatal
                    ):
                with self.assertRaises(type(fatal)):
                    fetch._record_source_failure(
                        td, str(Path(td) / "_dailylog.jsonl"),
                        _sources(1)[0], DAY, "upstream failed",
                    )

    def test_batch_coordinator_is_held_until_source_runner_returns(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            vault = Path(td) / "vault"
            archive.mkdir()
            vault.mkdir()
            registry = {
                "archive_root": str(archive),
                "sources": _sources(1),
            }
            observed = {"rejected": False}

            def inspect_lock(_sources_arg, day, root, *_args, **_kwargs):
                try:
                    with fetch.fetch_date_lock(root, day):
                        pass
                except fetch.FetchLockedError:
                    observed["rejected"] = True
                return {"ok": 1, "failed": 0, "skipped": 0}

            argv = [
                "fetch.py", "--date", DAY.isoformat(),
                "--registry", "unused.json",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        fetch, "load_registry", return_value=registry
                    ), mock.patch.object(
                        fetch, "run_sources", side_effect=inspect_lock
                    ), mock.patch.dict(
                        "os.environ", {"READDAILY_VAULT": str(vault)}
                    ), contextlib.redirect_stdout(io.StringIO()):
                fetch.main()

            self.assertTrue(observed["rejected"])
            with fetch.fetch_date_lock(archive, DAY):
                pass

    def test_source_lock_is_shared_with_publisher_and_does_not_cross_sources(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            archive.mkdir()
            start = threading.Barrier(3)
            same_entered = threading.Event()
            other_entered = threading.Event()

            def publisher_waiter(source, entered):
                start.wait(timeout=5)
                with vault_publisher._fetch_source_evidence_lock(
                        archive, source, DAY.isoformat()):
                    entered.set()

            with fetch.fetch_source_evidence_lock(
                    archive, "paper0", DAY):
                same = threading.Thread(
                    target=publisher_waiter,
                    args=("paper0", same_entered),
                )
                other = threading.Thread(
                    target=publisher_waiter,
                    args=("paper1", other_entered),
                )
                same.start()
                other.start()
                start.wait(timeout=5)
                self.assertTrue(other_entered.wait(timeout=5))
                self.assertFalse(same_entered.wait(timeout=0.15))

            self.assertTrue(same_entered.wait(timeout=5))
            same.join(timeout=5)
            other.join(timeout=5)
            self.assertFalse(same.is_alive())
            self.assertFalse(other.is_alive())

    def test_source_and_batch_locks_cannot_be_bypassed_by_different_tmpdirs(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            archive = base / "archive"
            archive.mkdir()
            tmp_a = base / "tmp-a"
            tmp_b = base / "tmp-b"
            tmp_a.mkdir()
            tmp_b.mkdir()
            previous_environment = os.environ.get("TMPDIR")
            previous_cache = tempfile.tempdir
            try:
                _use_tmpdir(str(tmp_a))

                source_ready = context.Event()
                source_entered = context.Event()
                source_process = context.Process(
                    target=_publisher_source_lock_worker,
                    args=(
                        str(archive), "paper0", DAY.isoformat(),
                        source_ready, source_entered, str(tmp_b),
                    ),
                )
                with fetch.fetch_source_evidence_lock(
                        archive, "paper0", DAY):
                    source_process.start()
                    self.assertTrue(source_ready.wait(timeout=5))
                    self.assertFalse(
                        source_entered.wait(timeout=0.3),
                        "不同 TMPDIR 不得绕过来源证据锁",
                    )
                self.assertTrue(source_entered.wait(timeout=5))
                source_process.join(timeout=5)
                self.assertFalse(source_process.is_alive())
                self.assertEqual(source_process.exitcode, 0)

                batch_ready = context.Event()
                batch_entered = context.Event()
                batch_rejected = context.Event()
                batch_process = context.Process(
                    target=_fetch_batch_lock_worker,
                    args=(
                        str(archive), DAY.isoformat(), batch_ready,
                        batch_entered, batch_rejected, str(tmp_b),
                    ),
                )
                with fetch.fetch_date_lock(archive, DAY):
                    batch_process.start()
                    self.assertTrue(batch_ready.wait(timeout=5))
                    self.assertTrue(
                        batch_rejected.wait(timeout=5),
                        "不同 TMPDIR 的重复批次必须被拒绝",
                    )
                    self.assertFalse(batch_entered.is_set())
                batch_process.join(timeout=5)
                self.assertFalse(batch_process.is_alive())
                self.assertEqual(batch_process.exitcode, 0)
            finally:
                if previous_environment is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous_environment
                tempfile.tempdir = previous_cache

    def test_publisher_global_lock_cannot_be_bypassed_by_different_tmpdirs(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            archive = base / "archive"
            vault = base / "vault"
            tmp_a = base / "tmp-a"
            tmp_b = base / "tmp-b"
            archive.mkdir()
            vault.mkdir()
            (vault / ".obsidian").mkdir()
            tmp_a.mkdir()
            tmp_b.mkdir()
            previous_environment = os.environ.get("TMPDIR")
            previous_cache = tempfile.tempdir
            ready = context.Event()
            entered = context.Event()
            process = context.Process(
                target=_publisher_transaction_lock_worker,
                args=(
                    str(archive), str(vault), ready, entered, str(tmp_b),
                ),
            )
            try:
                _use_tmpdir(str(tmp_a))
                with vault_publisher._publisher_operation_io(
                        archive, vault):
                    with vault_publisher._publisher_transaction_lock(
                            archive, vault):
                        process.start()
                        self.assertTrue(ready.wait(timeout=5))
                        self.assertFalse(
                            entered.wait(timeout=0.3),
                            "不同 TMPDIR 不得绕过 Vault 全局发布锁",
                        )
                self.assertTrue(entered.wait(timeout=5))
                process.join(timeout=5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if previous_environment is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous_environment
                tempfile.tempdir = previous_cache

    def test_insecure_precreated_user_lock_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "archive"
            archive.mkdir()
            attacker_root = base / "precreated-user-lock-root"
            attacker_root.mkdir()
            attacker_root.chmod(0o777)
            lock_root = attacker_root / "locks"
            source_root = lock_root / "source-evidence"
            patches = (
                mock.patch.object(
                    lib, "READDAILY_USER_LOCK_ROOT", str(attacker_root)
                ),
                mock.patch.object(lib, "READDAILY_LOCK_ROOT", str(lock_root)),
                mock.patch.object(
                    lib, "SOURCE_EVIDENCE_LOCK_ROOT", str(source_root)
                ),
            )
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(lib.ArchivePathSafetyError):
                    with fetch.fetch_source_evidence_lock(
                            archive, "paper0", DAY):
                        pass

            publisher_patches = (
                mock.patch.object(
                    vault_publisher, "READDAILY_USER_LOCK_ROOT",
                    str(attacker_root),
                ),
                mock.patch.object(
                    vault_publisher, "READDAILY_LOCK_ROOT", str(lock_root)
                ),
                mock.patch.object(
                    vault_publisher, "SOURCE_EVIDENCE_LOCK_ROOT",
                    str(source_root),
                ),
            )
            with publisher_patches[0], publisher_patches[1], \
                    publisher_patches[2]:
                with self.assertRaises(vault_publisher.PathSafetyError):
                    with vault_publisher._fetch_source_evidence_lock(
                            archive, "paper0", DAY.isoformat()):
                        pass

    def test_datetime_uses_the_same_natural_day_lock_identity(self):
        instant = datetime.datetime(2026, 9, 3, 23, 59, 58)
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "archive"
            archive.mkdir()
            fetch_path = fetch._source_evidence_lock_path(
                archive, "paper0", instant
            )[0]
            publisher_path = vault_publisher._source_evidence_lock_path(
                archive, "paper0", DAY
            )[0]
            self.assertEqual(lib.norm_day(instant), DAY)
            self.assertEqual(fetch_path, publisher_path)

    def test_case_alias_source_lock_is_shared_across_processes(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "case-archive"
            archive.mkdir()
            alias = archive.with_name(archive.name.upper())
            try:
                same_directory = os.path.samefile(archive, alias)
            except (FileNotFoundError, OSError):
                self.skipTest("测试文件系统区分路径大小写")
            if not same_directory:
                self.skipTest("测试文件系统区分路径大小写")

            fetch_path = fetch._source_evidence_lock_path(
                archive, "paper0", DAY
            )[0]
            publisher_path = vault_publisher._source_evidence_lock_path(
                alias, "paper0", DAY.isoformat()
            )[0]
            self.assertEqual(fetch_path, publisher_path)

            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            entered = context.Event()
            process = context.Process(
                target=_publisher_source_lock_worker,
                args=(
                    str(alias), "paper0", DAY.isoformat(), ready, entered,
                ),
            )
            with fetch.fetch_source_evidence_lock(
                    archive, "paper0", DAY):
                process.start()
                self.assertTrue(ready.wait(timeout=5))
                self.assertFalse(
                    entered.wait(timeout=0.3),
                    "同一 inode 的大小写别名不得绕过来源锁",
                )
            self.assertTrue(entered.wait(timeout=5))
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

    def test_replaced_archive_root_is_rejected_before_worker_writes(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            archive = base / "archive"
            original = base / "original-archive"
            vault = base / "vault"
            archive.mkdir()
            vault.mkdir()
            registry = {
                "archive_root": str(archive),
                "sources": _sources(1),
            }
            real_isolation = lib.assert_session_isolated
            swapped = {"value": False}

            def isolate_then_replace(session, forbidden, label="Vault"):
                result = real_isolation(session, forbidden, label=label)
                os.rename(archive, original)
                archive.mkdir()
                swapped["value"] = True
                return result

            def unsafe_if_reached(*_args, **_kwargs):
                (archive / "replacement-written").write_text(
                    "unsafe", encoding="utf-8"
                )
                return "ok"

            argv = [
                "fetch.py", "--date", DAY.isoformat(),
                "--registry", "unused.json",
            ]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(
                        fetch, "load_registry", return_value=registry
                    ), mock.patch.object(
                        lib, "assert_session_isolated",
                        side_effect=isolate_then_replace,
                    ), mock.patch.object(
                        fetch, "_run_source", side_effect=unsafe_if_reached
                    ) as run_source, mock.patch.dict(
                        "os.environ", {"READDAILY_VAULT": str(vault)}
                    ), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    fetch.main()

            self.assertTrue(swapped["value"])
            run_source.assert_not_called()
            self.assertEqual(list(archive.iterdir()), [])
            self.assertEqual(list(original.iterdir()), [])

    @unittest.skipUnless(hasattr(lib, "requests"), "requests 未安装")
    def test_http_session_is_reused_within_thread_but_isolated_across_threads(self):
        created = []
        guard = threading.Lock()
        start = threading.Barrier(3)
        results = []

        class FakeSession:
            def __init__(self):
                self.headers = {}
                with guard:
                    created.append(self)

        def use_client():
            start.wait(timeout=5)
            first = lib._http_client()
            second = lib._http_client()
            with guard:
                results.append((first, second))

        old_context = lib._HTTP_CONTEXT
        lib._HTTP_CONTEXT = threading.local()
        try:
            with mock.patch.object(lib.requests, "Session", FakeSession):
                first = threading.Thread(target=use_client)
                second = threading.Thread(target=use_client)
                first.start()
                second.start()
                start.wait(timeout=5)
                first.join(timeout=5)
                second.join(timeout=5)
        finally:
            lib._HTTP_CONTEXT = old_context

        self.assertEqual(len(created), 2)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0][0], results[0][1])
        self.assertIs(results[1][0], results[1][1])
        self.assertIsNot(results[0][0], results[1][0])

    @unittest.skipUnless(hasattr(lib, "requests"), "requests 未安装")
    def test_one_worker_closes_and_replaces_http_client_between_sources(self):
        created = []
        observed = []

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.closed = False
                created.append(self)

            def close(self):
                self.closed = True

        def use_source_client(src, *_args):
            first = lib._http_client()
            second = lib._http_client()
            observed.append((src["id"], first, second))
            return "ok"

        old_context = lib._HTTP_CONTEXT
        lib._HTTP_CONTEXT = threading.local()
        try:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(
                    lib.requests, "Session", FakeSession
            ), mock.patch.object(
                    fetch, "_run_source", side_effect=use_source_client
            ):
                counts = fetch.run_sources(
                    _sources(2), DAY, td, argparse.Namespace(),
                    str(Path(td) / "_dailylog.jsonl"), workers=1,
                )
        finally:
            lib._HTTP_CONTEXT = old_context

        self.assertEqual(counts, {"ok": 2, "failed": 0, "skipped": 0})
        self.assertEqual(len(created), 2)
        self.assertEqual([item[0] for item in observed], ["paper0", "paper1"])
        self.assertIs(observed[0][1], observed[0][2])
        self.assertIs(observed[1][1], observed[1][2])
        self.assertIsNot(observed[0][1], observed[1][1])
        self.assertTrue(all(client.closed for client in created))

    @unittest.skipUnless(hasattr(lib, "requests"), "requests 未安装")
    def test_http_client_cleanup_memory_error_aborts_batch(self):
        class FakeSession:
            def __init__(self):
                self.headers = {}

            def close(self):
                raise MemoryError("client cleanup out of memory")

        def use_source_client(*_args):
            lib._http_client()
            return "ok"

        old_context = lib._HTTP_CONTEXT
        lib._HTTP_CONTEXT = threading.local()
        try:
            with tempfile.TemporaryDirectory() as td, mock.patch.object(
                    lib.requests, "Session", FakeSession
            ), mock.patch.object(
                    fetch, "_run_source", side_effect=use_source_client
            ), mock.patch.object(fetch, "_record_source_failure") as record:
                with self.assertRaises(MemoryError):
                    fetch.run_sources(
                        _sources(1), DAY, td, argparse.Namespace(),
                        str(Path(td) / "_dailylog.jsonl"), workers=1,
                    )
        finally:
            lib._HTTP_CONTEXT = old_context

        record.assert_not_called()

    def test_stdlib_http_opener_is_thread_local_when_requests_is_unavailable(self):
        module_name = "readdaily_concurrency_lib_without_requests"
        spec = importlib.util.spec_from_file_location(
            module_name, FETCH_SCRIPTS / "lib.py"
        )
        fallback = importlib.util.module_from_spec(spec)
        real_import = builtins.__import__

        def import_without_requests(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("requests intentionally unavailable")
            return real_import(name, *args, **kwargs)

        with mock.patch(
                "builtins.__import__", side_effect=import_without_requests):
            spec.loader.exec_module(fallback)

        created = []
        opened = []
        guard = threading.Lock()
        start = threading.Barrier(3)
        results = []

        class FakeOpener:
            addheaders = []

            def open(self, request, timeout):
                opened.append((request, timeout))

                class FakeResponse:
                    status = 201

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def geturl(self):
                        return request.full_url

                    def read(self):
                        return b'{"ok":true}'

                return FakeResponse()

        def build_opener(*_args):
            opener = FakeOpener()
            with guard:
                created.append(opener)
            return opener

        def use_client():
            start.wait(timeout=5)
            first = fallback._http_client()
            second = fallback._http_client()
            with guard:
                results.append((first, second))

        with mock.patch.object(
                fallback.urllib.request,
                "build_opener",
                side_effect=build_opener,
        ):
            first = threading.Thread(target=use_client)
            second = threading.Thread(target=use_client)
            first.start()
            second.start()
            start.wait(timeout=5)
            first.join(timeout=5)
            second.join(timeout=5)

            post_result = fallback.http_post_json(
                "https://example.test/post",
                {"title": "建设新闻"},
                headers={"Referer": "https://example.test/"},
                timeout=7,
            )

        self.assertEqual(len(created), 3)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0][0], results[0][1])
        self.assertIs(results[1][0], results[1][1])
        self.assertIsNot(results[0][0], results[1][0])
        self.assertEqual(post_result, (
            201, "https://example.test/post", b'{"ok":true}',
        ))
        request, timeout = opened[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"title": "建设新闻"},
        )

    def test_concurrent_daily_log_lines_remain_complete_jsonl(self):
        workers = 8
        per_worker = 8
        start = threading.Barrier(workers + 1)
        append_guard = threading.Lock()
        append_activity = {"active": 0, "maximum": 0}
        real_append = lib.ArchiveSession.append_bytes

        def deliberately_split_append(session, relative, payload):
            with append_guard:
                append_activity["active"] += 1
                append_activity["maximum"] = max(
                    append_activity["maximum"], append_activity["active"]
                )
            try:
                midpoint = len(payload) // 2
                real_append(session, relative, payload[:midpoint])
                time.sleep(0.001)
                real_append(session, relative, payload[midpoint:])
            finally:
                with append_guard:
                    append_activity["active"] -= 1

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "archive" / "_dailylog.jsonl"

            def write_rows(worker):
                start.wait(timeout=5)
                for index in range(per_worker):
                    lib.log_line(str(path), {
                        "source": "paper%s" % worker,
                        "sequence": index,
                        "payload": "建设新闻" * 30,
                    })

            threads = [
                threading.Thread(target=write_rows, args=(worker,))
                for worker in range(workers)
            ]
            with mock.patch.object(
                    lib.ArchiveSession,
                    "append_bytes",
                    deliberately_split_append,
            ), contextlib.redirect_stdout(io.StringIO()):
                for thread in threads:
                    thread.start()
                start.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

            raw_lines = path.read_text(encoding="utf-8").splitlines()
            rows = [json.loads(line) for line in raw_lines]

        self.assertEqual(len(rows), workers * per_worker)
        self.assertEqual(append_activity["maximum"], 1)
        self.assertEqual(
            {(row["source"], row["sequence"]) for row in rows},
            {
                ("paper%s" % worker, index)
                for worker in range(workers)
                for index in range(per_worker)
            },
        )

    def test_source_and_log_console_records_share_one_output_lock(self):
        start = threading.Barrier(3)
        guard = threading.Lock()
        observed = {"active": 0, "maximum": 0, "calls": []}

        def slow_print(*values, **_kwargs):
            with guard:
                observed["active"] += 1
                observed["maximum"] = max(
                    observed["maximum"], observed["active"]
                )
            time.sleep(0.03)
            with guard:
                observed["calls"].append(values)
                observed["active"] -= 1

        with tempfile.TemporaryDirectory() as td, mock.patch(
                "builtins.print", side_effect=slow_print):
            def source_record():
                start.wait(timeout=5)
                fetch._source_print("paper0", "ready")

            def log_record():
                start.wait(timeout=5)
                lib.log_line(
                    str(Path(td) / "_dailylog.jsonl"),
                    {"source": "paper1", "stage": "fetched"},
                )

            threads = [
                threading.Thread(target=source_record),
                threading.Thread(target=log_record),
            ]
            for thread in threads:
                thread.start()
            start.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(observed["maximum"], 1)
        self.assertEqual(len(observed["calls"]), 2)


if __name__ == "__main__":
    unittest.main()
