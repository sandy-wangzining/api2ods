"""公开安装的数值精度与分页完整性回归。"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from _helpers import OfflineTestCase, minimal_job

from api2ods import config, http, parsers, spool
from api2ods.fetch import Fetcher
from api2ods.utils import ConfigError


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
