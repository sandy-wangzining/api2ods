"""分页完整性、内存边界和安全发布回归。"""

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
                    raise TimeoutError("response lost after commit")
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
                raise RuntimeError("publish rejected")
            return original(sql)

        with mock.patch.object(self.odps, "run_sql", side_effect=run_sql):
            with self.assertRaisesRegex(RuntimeError, "暂存表"):
                self.write()
        self.assert_old_target()
        self.assertEqual(len(self.odps.stage.writers), 1)
        self.assertTrue(self.odps.stages)

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
