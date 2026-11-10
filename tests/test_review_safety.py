"""分页完整性、内存边界和安全发布回归。"""

import tempfile
import threading
import time
import tracemalloc
from datetime import date
from pathlib import Path
from unittest import mock

from _helpers import DdlOdps, FakeTable, OfflineTestCase, minimal_job

from api2ods import fetch as fetch_mod
from api2ods import mc, spool
from api2ods.fetch import Fetcher


class TestMissingRecordsTotals(OfflineTestCase):
    def fetch(self, payload, mode="page"):
        job = minimal_job(
            pagination={
                "type": mode,
                "page_size": 2,
                "total_items_path": "TotalCount",
                "total_pages_path": "TotalPages",
                "cursor_path": "NextToken",
            }
        )
        job["request"].update(records_path="Items.Item", records_missing="empty")
        fetcher = Fetcher(job, Path("."))
        with mock.patch.object(fetcher, "_do_request", return_value=payload):
            return fetcher.fetch_unit(fetcher.build_units([date(2026, 9, 27)])[0])

    def test_missing_records_with_positive_total_is_not_an_empty_snapshot(self):
        for mode in ("page", "cursor"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                self.fetch({"Items": {}, "TotalCount": 9}, mode)

    def test_missing_records_with_next_cursor_cannot_be_empty(self):
        for cursor in ("page2", 0, False, []):
            for extra in ({}, {"TotalCount": 0}):
                with self.subTest(cursor=cursor, extra=extra), self.assertRaises(RuntimeError):
                    self.fetch({"Items": {}, "NextToken": cursor, **extra}, "cursor")

    def test_missing_records_without_next_cursor_remains_empty(self):
        for cursor in (None, ""):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.fetch({"Items": {}, "NextToken": cursor}, "cursor"), [])

    def test_next_cursor_cannot_clear_existing_partition_even_when_empty_is_allowed(self):
        from _helpers import make_args

        from api2ods import cli, config

        job = minimal_job(pagination={"type": "cursor", "cursor_path": "NextToken", "window_retries": 0})
        job["request"].update(records_path="Items.Item", records_missing="empty")
        job["target"]["allow_empty"] = True
        day = date(2026, 9, 20)
        job = config.normalize_job(config.render_job(job, {}, day)[0])
        config.validate_job(job)
        target = FakeTable()
        target.written["pt=20260920"] = [["old-good-snapshot"]]
        odps = DdlOdps(target)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(Fetcher, "_do_request", return_value={"Items": {}, "NextToken": "page2"}),
            mock.patch.object(cli, "connect_odps", return_value=odps) as connect,
        ):
            root = Path(tmp)
            rc = cli.run_sync(job, {}, root / "config.json", make_args(bizdate="20260920"), day, root / "job.json")
        self.assertEqual(rc, 1)
        connect.assert_not_called()
        self.assertEqual(target.written["pt=20260920"], [["old-good-snapshot"]])

    def test_missing_records_before_last_page_is_not_an_empty_snapshot(self):
        with self.assertRaises(RuntimeError):
            self.fetch({"Items": {}, "TotalCount": 0, "TotalPages": 2})

    def test_missing_records_with_zero_or_absent_total_remains_compatible(self):
        for mode in ("page", "cursor"):
            for payload in ({"Items": {}, "TotalCount": 0}, {"Items": {}}):
                with self.subTest(mode=mode, payload=payload):
                    self.assertEqual(self.fetch(payload, mode), [])


class TestAtomicPublication(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.table = FakeTable()
        self.odps = DdlOdps(self.table)
        self.table.written["pt=20260920"] = [["old"]]
        self.records = ["new1", "new2"]
        self.expected = [["new1"], ["new2"]]

    def write(self, **kwargs):
        return mc.write_partition(
            self.odps, self.table, "p", "t", "20260920", lambda: iter([self.records]), total=2, retries=2, **kwargs
        )

    def assert_old_target(self):
        self.assertEqual(self.table.written["pt=20260920"], [["old"]])
        self.assertEqual(self.table.deleted, [])

    def test_tunnel_failure_preserves_old_target(self):
        original = self.odps.get_table

        def get_table(name):
            table = original(name)
            if "tmp_" in name:
                table.open_writer = mock.Mock(side_effect=RuntimeError("Tunnel failed"))
            return table

        with mock.patch.object(self.odps, "get_table", side_effect=get_table):
            with self.assertRaises(RuntimeError):
                self.write()
        self.assert_old_target()
        self.assertFalse(any(sql.startswith("insert overwrite") for sql in self.odps.sqls))

    def test_staging_count_mismatch_cannot_publish(self):
        with mock.patch.object(mc, "count_partition", return_value=0):
            with self.assertRaises(RuntimeError):
                self.write()
        self.assert_old_target()
        self.assertFalse(any(sql.startswith("insert overwrite") for sql in self.odps.sqls))

    def test_lost_publication_response_reuses_verified_stage(self):
        original = self.odps.run_sql
        publications = []

        def run_sql(sql):
            result = original(sql)
            if sql.startswith("insert overwrite"):
                publications.append(sql)
                if len(publications) == 1:
                    result.id = "completed-publish"
                    result.is_successful = mock.Mock(side_effect=TimeoutError("response lost after commit"))
                    result.is_terminated = mock.Mock(return_value=True)
                    result.stop = mock.Mock()
            return result

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            self.assertEqual(self.write(), 2)
        self.assertEqual(len(publications), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)
        self.assertEqual(len(self.odps.stage.writers), 1)
        self.assertEqual(self.table.deleted, [])

    def test_failed_publication_keeps_verified_stage_for_retry(self):
        original = self.odps.run_sql

        def run_sql(sql):
            if sql.startswith("insert overwrite"):
                result = mock.Mock()
                result.id = "rejected-publish"
                result.is_successful.return_value = False
                result.is_terminated.return_value = True
                result.wait_for_success.side_effect = RuntimeError("publish rejected")
                return result
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            with self.assertRaisesRegex(RuntimeError, "暂存表"):
                self.write()
        self.assert_old_target()
        self.assertEqual(len(self.odps.stage.writers), 1)
        self.assertTrue(self.odps.stages)

    def test_unknown_publication_blocks_retry_and_a_later_writer(self):
        from api2ods import _common

        original = self.odps.run_sql
        instance = mock.Mock()
        instance.id = "pending-publish"
        instance.is_successful.side_effect = ConnectionError("poll failed")
        instance.is_terminated.return_value = False
        instance.stop.side_effect = ConnectionError("cancel failed")
        publications = []

        def run_sql(sql):
            if sql.startswith("insert overwrite"):
                publications.append(sql)
                return instance
            return original(sql)

        self.odps.get_instance = mock.Mock(return_value=instance)
        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            with self.assertRaises(_common.UnresolvedSqlError):
                self.write()
            with self.assertRaises(_common.UnresolvedSqlError):
                self.write()
        self.assertEqual(len(publications), 1)
        self.assert_old_target()
        instance.is_terminated.return_value = True
        self.assertEqual(self.write(), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)

    def test_cleanup_failure_does_not_fail_published_partition(self):
        original = self.odps.run_sql

        def run_sql(sql):
            if sql.startswith("drop table"):
                raise RuntimeError("cleanup failed")
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            self.assertEqual(self.write(), 2)
        self.assertEqual(self.table.written["pt=20260920"], self.expected)
        ddl = next(sql for sql in self.odps.sqls if sql.startswith("create table"))
        self.assertIn("'lifecycle.deletemeta'='true'", ddl)
        self.assertIn("lifecycle 1", ddl)

    def test_cleanup_log_redacts_supplied_secret_values(self):
        secret = "dummy-cleanup-credential-79431"
        original = self.odps.run_sql

        def run_sql(sql):
            if sql.startswith("drop table"):
                raise RuntimeError("provider rejected " + secret)
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql), mock.patch.object(mc, "log") as log:
            self.assertEqual(self.write(secrets=[secret]), 2)
        self.assertNotIn(secret, str(log.call_args_list))
        self.assertIn("***", str(log.call_args_list))

    def test_cancellation_logs_use_known_secrets_in_every_staging_phase(self):
        from api2ods import _common

        secret = "dummy-staging-credential-79431"
        for prefix in ("create table", "alter table", "select count"):
            with self.subTest(prefix=prefix):
                table = FakeTable()
                odps = DdlOdps(table)
                original = odps.run_sql
                instance = mock.Mock()
                instance.id = "staging-failure"
                instance.is_successful.side_effect = RuntimeError("poll " + secret)
                instance.is_terminated.return_value = False
                instance.stop.side_effect = RuntimeError("cancel " + secret)

                def run_sql(sql):
                    return instance if sql.startswith(prefix) else original(sql)

                with mock.patch.object(odps, "run_sql", side_effect=run_sql), mock.patch.object(mc, "log") as log:
                    with self.assertRaises((RuntimeError, _common.UnresolvedSqlError)):
                        mc.write_partition(
                            odps,
                            table,
                            "p",
                            "t",
                            "20260920",
                            lambda: iter([["new"]]),
                            total=1,
                            retries=1,
                            secrets=[secret],
                        )
                self.assertNotIn(secret, str(log.call_args_list))
                self.assertIn("***", str(log.call_args_list))

    def test_interrupt_preserves_old_target(self):
        original = self.odps.get_table

        def get_table(name):
            table = original(name)
            if "tmp_" in name:
                table.open_writer = mock.Mock(side_effect=KeyboardInterrupt())
            return table

        with mock.patch.object(self.odps, "get_table", side_effect=get_table):
            with self.assertRaises(KeyboardInterrupt):
                self.write()
        self.assert_old_target()


class TestPageSpooling(OfflineTestCase):
    def fetcher(self, pages=2):
        job = minimal_job(pagination={"type": "page", "page_size": 1, "total_pages_path": "pages"})
        return Fetcher(job, Path("."))

    def test_retry_discards_partial_unit_and_spools_before_next_page(self):
        fetcher = self.fetcher()
        attempts = [0]
        files = []
        delivered = []
        real_spool = spool.SpoolWriter

        def create_spool():
            data = real_spool()
            files.append(data.path)
            return data

        def request(params, *args, **kwargs):
            page = params["page"]
            if page == 1:
                attempts[0] += 1
                if attempts[0] == 2:
                    self.assertFalse(files[-2].exists())
            else:
                self.assertGreater(files[-1].stat().st_size, 0)
                if attempts[0] == 1:
                    raise RuntimeError("page 2 failed")
            value = "discard" if attempts[0] == 1 else f"new{page}"
            return {"pages": 2, "data": {"list": [{"id": value}]}}

        with (
            mock.patch.object(fetch_mod, "SpoolWriter", side_effect=create_spool),
            mock.patch.object(fetcher, "_do_request", side_effect=request),
            mock.patch.object(fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all(
                [date(2026, 9, 27)], workers=1, window_retries=1, on_records=delivered.extend
            )
        self.assertEqual(failures, [])
        self.assertEqual(sum(count for _, count in stats), 2)
        self.assertEqual(delivered, [{"id": "new1"}, {"id": "new2"}])
        self.assertTrue(all(not path.exists() for path in files))

    def test_large_unit_memory_depends_on_pages_and_batches(self):
        fetcher = self.fetcher()
        sizes = []

        def request(params, *args, **kwargs):
            page = params["page"]
            rows = [{"id": f"{page}-{i}", "text": f"{page}:{i}:" + "x" * 20000} for i in range(80)]
            return {"pages": 40, "data": {"list": rows}}

        with mock.patch.object(fetcher, "_do_request", side_effect=request), mock.patch.object(spool, "progress_log"):
            tracemalloc.start()
            try:
                stats, failures = fetcher.fetch_all(
                    [date(2026, 9, 27)],
                    workers=1,
                    window_retries=0,
                    on_records=lambda rows: sizes.append(len(rows)),
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(failures, [])
        self.assertEqual(sum(sizes), 3200)
        self.assertEqual(sum(count for _, count in stats), 3200)
        self.assertLess(peak, 40 * 1024 * 1024)

    def test_fatal_unit_also_cleans_up_late_worker_spool(self):
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "day"})
        fetcher = Fetcher(job, Path("."))
        started = threading.Event()
        release = threading.Event()
        files = []
        delivered = []
        real_spool = spool.SpoolWriter

        def create_spool():
            data = real_spool()
            files.append(data.path)
            return data

        def fetch(unit, **kwargs):
            if unit.label == "2026-09-27":
                if not started.wait(2):
                    raise RuntimeError("worker did not start")
                raise fetch_mod.FatalApiError("unauthorized")
            started.set()
            if not release.wait(2):
                raise RuntimeError("worker was not released")
            kwargs["_on_records"]([{"id": "late"}])
            return []

        with (
            mock.patch.object(fetch_mod, "SpoolWriter", side_effect=create_spool),
            mock.patch.object(fetcher, "fetch_unit", side_effect=fetch),
        ):
            try:
                with self.assertRaises(fetch_mod.FatalApiError):
                    fetcher.fetch_all([date(2026, 9, 27), date(2026, 9, 28)], workers=2, on_records=delivered.extend)
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while any(path.exists() for path in files) and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        self.assertEqual(delivered, [])
        self.assertTrue(all(not path.exists() for path in files))
