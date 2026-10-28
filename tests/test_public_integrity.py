"""公开安装的数值精度与分页完整性回归。"""

import tempfile
import tracemalloc
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from _helpers import OfflineTestCase, make_args, minimal_job

from api2ods import cli, config, http, parsers, spool
from api2ods.fetch import Fetcher
from api2ods.utils import ConfigError, FatalApiError


class TestPublicIntegrity(OfflineTestCase):
    def test_json_and_jsonl_keep_exact_numbers_and_numeric_types(self):
        raw = '{"amount":9007199254740993.01,"tiny":1e-400,"id":9007199254740993}'
        for record in (spool.loads_json(raw), parsers._parse_text(raw, {"format": "jsonl"})[0]):
            encoded = spool.dump_record(record)
            restored = spool.loads_json(encoded)
            self.assertEqual(restored["amount"], Decimal("9007199254740993.01"))
            self.assertEqual(restored["tiny"], Decimal("1e-400"))
            self.assertEqual(restored["id"], 9007199254740993)
            self.assertNotIn('"9007199254740993.01"', encoded)

    def test_non_finite_decimals_are_rejected(self):
        for number in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(number=number), self.assertRaises(RuntimeError):
                spool.dump_record({"amount": Decimal(number)})

    def test_business_condition_compares_decimal_and_config_number(self):
        http.check_fail_if(spool.loads_json('{"code":0.1}'), [{"path": "code", "not_equals": 0.1}])

    def test_decimal_error_body_is_rejected_even_with_single_line_opt_in(self):
        for raw in (b"0.0", b"-0.1", b"1e0", b"1e-400", b"0.0\n"):
            for fmt in ("csv", "tsv", "jsonl"):
                with self.subTest(raw=raw, fmt=fmt), self.assertRaisesRegex(RuntimeError, "JSON 标量"):
                    parsers.parse_bytes(raw, {"format": fmt, "allow_single_line": True}, "export")

    def test_decimal_error_body_cannot_clear_partition_when_empty_is_allowed(self):
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["pagination"]["window_retries"] = 0
        job["parse"] = {"format": "csv"}
        job["target"]["allow_empty"] = True
        job = config.normalize_job(config.render_job(job, {}, date(2026, 10, 26))[0])
        config.validate_job(job)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(Fetcher, "_do_request", return_value=b"0.0"),
            mock.patch.object(cli, "connect_odps") as connect,
            mock.patch.object(cli, "ensure_target_table"),
            mock.patch.object(cli, "write_partition") as write,
            mock.patch.object(cli, "count_partition", return_value=0),
        ):
            root = Path(tmp)
            result = cli.run_sync(job, {}, root / "config.json", make_args(), date(2026, 10, 26), root / "job.json")
        self.assertEqual(result, 1)
        connect.assert_not_called()
        write.assert_not_called()

    def test_fail_if_keeps_tolerance_after_decimal_json_parsing(self):
        for raw, expected in (
            ('{"code":0.30000000000000004}', 0.3),
            ('{"code":-0.30000000000000004}', "-0.3"),
            ('{"code":1e-13}', 0),
        ):
            with self.subTest(raw=raw):
                payload = spool.loads_json(raw)
                with self.assertRaises(FatalApiError):
                    http.check_fail_if(payload, [{"path": "code", "equals": expected}])
                http.check_fail_if(payload, [{"path": "code", "not_equals": expected}])

    def test_fail_if_still_distinguishes_outside_tolerance_and_large_numbers(self):
        for raw, expected in (
            ('{"code":0.3001}', 0.3),
            ('{"code":9007199254740993.0}', 9007199254740992),
            ('{"code":9007199254740993.01}', Decimal("9007199254740993.02")),
            ('{"code":1e999999999}', 0),
        ):
            with self.subTest(raw=raw):
                payload = spool.loads_json(raw)
                http.check_fail_if(payload, [{"path": "code", "equals": expected}])
                with self.assertRaises(FatalApiError):
                    http.check_fail_if(payload, [{"path": "code", "not_equals": expected}])

    def test_error_snippet_does_not_encode_the_whole_response(self):
        payload = {"code": Decimal("0.3"), "items": ["x" * 1000] * 20000}
        tracemalloc.start()
        try:
            snippet = parsers._json_snippet(payload)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(snippet), 300)
        self.assertIn("0.3", snippet)
        self.assertLess(peak, 1024 * 1024)

    def test_http_response_keeps_decimal_precision(self):
        response = http.requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response._content = b'{"items":[{"amount":9007199254740993.01}]}'
        response._content_consumed = True
        with mock.patch.object(http.requests, "request", return_value=response):
            payload = http.request_once("GET", "https://example.invalid", {}, {}, "json", 1, True, True, None)
        self.assertEqual(payload["items"][0]["amount"], Decimal("9007199254740993.01"))

    def fetch_pages(self, responses, **overrides):
        pagination = {"type": "page", "page_size": 2, "total_items_path": "total", **overrides}
        fetcher = Fetcher(minimal_job(pagination=pagination), Path("."))
        with mock.patch.object(fetcher, "_do_request", side_effect=responses):
            return fetcher.fetch_unit(fetcher.build_units([date(2026, 10, 26)])[0])

    def test_repeated_page_fails_before_total_count_can_hide_duplicates(self):
        page = {"total": 4, "data": {"list": [{"id": 1}, {"id": 2}]}}
        with self.assertRaisesRegex(ConfigError, "重复"):
            self.fetch_pages([page, page])

    def test_repeated_page_checks_object_content_not_key_order(self):
        first = {"total": 4, "data": {"list": [{"id": 1, "amount": 2}, {"id": 2}]}}
        second = {"total": 4, "data": {"list": [{"amount": 2, "id": 1}, {"id": 2}]}}
        with self.assertRaisesRegex(ConfigError, "重复"):
            self.fetch_pages([first, second])

    def test_source_with_identical_pages_can_explicitly_opt_out(self):
        page = {"total": 4, "data": {"list": [{"value": 1}, {"value": 1}]}}
        self.assertEqual(len(self.fetch_pages([page, page], allow_repeated_pages=True)), 4)

    def test_invalid_repeat_option_fails_during_configuration(self):
        job = config.normalize_job(
            minimal_job(pagination={"type": "page", "total_items_path": "total", "allow_repeated_pages": "flase"})
        )
        with self.assertRaises(ConfigError):
            config.validate_job(job)
