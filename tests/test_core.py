# -*- coding: utf-8 -*-
"""api2ods v2 离线单元测试：不访问网络、不连 MaxCompute（requests/pyodps 没装也能跑）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api2ods  # noqa: E402
from api2ods import auth as auth_mod  # noqa: E402
from api2ods import config as config_mod  # noqa: E402
from api2ods import dates as dates_mod  # noqa: E402
from api2ods import fetch as fetch_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    init_wizard,  # noqa: E402
    parsers,
    utils,
)
from api2ods import mc as mc_mod  # noqa: E402
from api2ods import spool as spool_mod  # noqa: E402
from api2ods.cli import record_to_json  # noqa: E402


class OfflineTestCase(unittest.TestCase):
    """所有用例的基类：禁止真实 sleep（把 time.sleep 变成空操作），并统一控制台编码。"""

    def setUp(self):
        utils.setup_console()
        self._console = mock.patch.object(utils, "_console_patched", True)
        self._console.start()
        self.addCleanup(self._console.stop)
        patcher = mock.patch.object(time, "sleep", lambda *_args, **_kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)


def make_args(**overrides):
    base = dict(
        job="jobs/x.json", config="", check=False, bizdate="", days=None,
        dates="", start_date="", end_date="", pt="", workers=1,
        dry_run=False, allow_empty=False, endpoint="", mc_profile="", cli_profile="",
        sql_timeout=600, log_file="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def minimal_job(**overrides) -> dict:
    job = {
        "job": "demo",
        "request": {"base_url": "https://api.example.com", "path": "/v1/items",
                    "method": "GET", "records_path": "data.list",
                    "params": {"app": "demo"}},
        "pagination": {"type": "none"},
        "target": {"project": "demo_project", "table": "ods_demo_json_df", "pt": "${bizdate}"},
    }
    for key, value in overrides.items():
        job[key] = value
    return job


# =============================================================================
# 占位符 / 校验 / 告警
# =============================================================================

class TestPlaceholders(OfflineTestCase):
    def test_replace_nested_and_mixed(self):
        ctx = {"secrets": {"k": "S"}, "bizdate": "20260918"}
        value = {"a": "Bearer ${secrets.k}", "b": ["${bizdate}", 1], "c": {"d": "${bizdate}-x"}}
        self.assertEqual(config_mod.deep_substitute(value, ctx),
                         {"a": "Bearer S", "b": ["20260918", 1], "c": {"d": "20260918-x"}})

    def test_whole_placeholder_keeps_type(self):
        self.assertEqual(config_mod.deep_substitute("${secrets.lst}", {"secrets": {"lst": [1, 2]}}), [1, 2])

    def test_unknown_placeholder_raises_with_hint(self):
        with self.assertRaises(SystemExit) as ctx:
            config_mod.deep_substitute("${secrets.nope}", {"secrets": {}})
        self.assertIn("secrets", str(ctx.exception))

    def test_render_job_uses_bizdate(self):
        got = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        self.assertEqual(got["target"]["pt"], "20260918")

    def test_render_job_inline_secrets(self):
        job = minimal_job()
        job["secrets"] = {"onerway_secret_key": "inline-secret"}
        job["maxcompute"] = {"project": "p1", "access_key_id": "a", "access_key_secret": "b"}
        job["request"]["auth"] = {"type": "sha256_concat", "secret_key": "${secrets.onerway_secret_key}"}
        got = config_mod.render_job(job, {}, date(2026, 9, 18))
        self.assertEqual(got["request"]["auth"]["secret_key"], "inline-secret")
        self.assertEqual(got["secrets"]["onerway_secret_key"], "inline-secret")
        self.assertEqual(got["maxcompute"]["project"], "p1")
        # 作业内 maxcompute 优先于 config
        profile = config_mod.get_mc_profile_meta({}, got, make_args())
        self.assertEqual(profile["project"], "p1")

    def test_render_job_secret_not_substituted(self):
        job = minimal_job()
        job["secrets"] = {"k": "value-${bizdate}"}   # 密钥里的占位符样式内容不被替换
        got = config_mod.render_job(job, {}, date(2026, 9, 18))
        self.assertEqual(got["secrets"]["k"], "value-${bizdate}")


class TestValidateJob(OfflineTestCase):
    def test_ok(self):
        config_mod.validate_job(minimal_job())

    def test_missing_base_url(self):
        job = minimal_job()
        del job["request"]["base_url"]
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_missing_table(self):
        job = minimal_job()
        del job["target"]["table"]
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bad_method(self):
        job = minimal_job()
        job["request"]["method"] = "FETCH"
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_page_needs_total_path(self):
        job = minimal_job()
        job["pagination"] = {"type": "page", "page_param": "p", "size_param": "n"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_cursor_needs_path(self):
        job = minimal_job()
        job["pagination"] = {"type": "cursor"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_window_start_param_required(self):
        job = minimal_job()
        job["window"] = {"days": 3, "end_param": "endTime"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_range_needs_end_param(self):
        job = minimal_job()
        job["window"] = {"mode": "range", "start_param": "startTime"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bytes_needs_parse_format(self):
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bytes_rejects_pagination(self):
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["parse"] = {"format": "csv"}
        job["pagination"] = {"type": "page", "page_param": "p", "size_param": "n",
                             "total_pages_path": "t"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_custom_auth_needs_func(self):
        job = minimal_job()
        job["request"]["auth"] = {"type": "custom"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_aliyun_rpc_needs_keys(self):
        job = minimal_job()
        job["request"]["auth"] = {"type": "aliyun_rpc", "access_key_id": "x"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bad_fail_if(self):
        job = minimal_job()
        job["request"]["fail_if"] = [{"path": "Code"}]
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bad_records_missing(self):
        job = minimal_job()
        job["request"]["records_missing"] = "skip"
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bad_params_in(self):
        job = minimal_job()
        job["request"]["params_in"] = "cookie"
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)


class TestWarnings(OfflineTestCase):
    def test_unknown_keys_flagged(self):
        job = minimal_job()
        job["request"]["base_urll"] = "typo"
        job["window"] = {"start_param": "s", "end_param": "e", "dayss": 3}
        warnings = config_mod.collect_warnings(job)
        self.assertTrue(any("base_urll" in w for w in warnings))
        self.assertTrue(any("dayss" in w for w in warnings))

    def test_comment_keys_ignored(self):
        job = minimal_job()
        job["request"]["//base_url"] = "注释"
        self.assertEqual(config_mod.collect_warnings(job), [])


# =============================================================================
# JSON 路径 / 日期窗口
# =============================================================================

class TestGetPath(OfflineTestCase):
    data = {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}, "ok": True}

    def test_simple_and_nested(self):
        self.assertTrue(parsers.get_path(self.data, "ok"))
        self.assertEqual(parsers.get_path(self.data, "data.totalPages"), 3)

    def test_index(self):
        self.assertEqual(parsers.get_path(self.data, "data.list[1].id"), 2)

    def test_missing_returns_default(self):
        self.assertIsNone(parsers.get_path(self.data, "data.nope.x"))
        self.assertEqual(parsers.get_path(self.data, "data.list[9]", default="d"), "d")

    def test_empty_path_returns_input(self):
        self.assertIs(parsers.get_path(self.data, ""), self.data)


class TestResolveDays(OfflineTestCase):
    def test_bizdate_with_days(self):
        days = dates_mod.resolve_days(make_args(bizdate="20260918", days=3), {})
        self.assertEqual([d.isoformat() for d in days],
                         ["2026-09-16", "2026-09-17", "2026-09-18"])

    def test_env_bizdate(self):
        with mock.patch.dict(os.environ, {"bizdate": "20260918"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-18"])

    def test_start_end_range(self):
        days = dates_mod.resolve_days(make_args(start_date="2026-09-01", end_date="2026-09-03"), {})
        self.assertEqual(len(days), 3)

    def test_dates_list(self):
        days = dates_mod.resolve_days(make_args(dates="20260901,2026-09-05"), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-01", "2026-09-05"])

    def test_pair_required(self):
        with self.assertRaises(SystemExit):
            dates_mod.resolve_days(make_args(start_date="2026-09-01"), {})

    def test_default_uses_window_days(self):
        days = dates_mod.resolve_days(make_args(), {"window": {"days": 3, "date_tz": "UTC"}})
        self.assertEqual(len(days), 3)


class TestWindowParams(OfflineTestCase):
    win = {"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "pad_hours": 0,
           "start_param": "startTime", "end_param": "endTime",
           "format": "%Y-%m-%d %H:%M:%S"}

    def test_per_day(self):
        got = dates_mod.window_param_sets({"window": self.win}, [date(2026, 9, 18)])
        self.assertEqual(got, [{"startTime": "2026-09-18 08:00:00", "endTime": "2026-09-19 08:00:00"}])

    def test_per_day_with_pad(self):
        win = dict(self.win, pad_hours=1)
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])
        self.assertEqual(got, [{"startTime": "2026-09-18 07:00:00", "endTime": "2026-09-19 09:00:00"}])

    def test_range_mode(self):
        win = dict(self.win, mode="range")
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18), date(2026, 9, 20)])
        self.assertEqual(got, [{"startTime": "2026-09-18 08:00:00", "endTime": "2026-09-21 08:00:00"}])

    def test_no_window(self):
        self.assertEqual(dates_mod.window_param_sets({}, [date(2026, 9, 18)]), [None])

    def test_unix_format(self):
        win = {"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "pad_hours": 0,
               "start_param": "start", "end_param": "end", "format": "unix"}
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])[0]
        expected = int(datetime(2026, 9, 18, tzinfo=dates_mod.load_zone("UTC")).timestamp())
        self.assertEqual(got["start"], expected)
        self.assertEqual(got["end"], expected + 86400)

    def test_extra_params_and_missing_end(self):
        win = {"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "pad_hours": 0,
               "start_param": "BillingDate", "format": "%Y-%m-%d",
               "extra_params": {"BillingCycle": "%Y-%m"}}
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])[0]
        self.assertEqual(got, {"BillingDate": "2026-09-18", "BillingCycle": "2026-09"})


# =============================================================================
# 鉴权
# =============================================================================

def reference_aliyun_sign(params: dict, ak_id: str, secret: str, nonce: str, timestamp: str) -> str:
    """独立重写一遍阿里云官方算法（与 aliyun_bill_sync_daily.py 的 call_bss 一致），用于对照。"""
    merged = dict(params)
    merged.update({
        "Format": "JSON", "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "SignatureNonce": nonce, "Timestamp": timestamp, "AccessKeyId": ak_id,
    })
    canonical = "&".join(
        f"{urllib.parse.quote(str(k), safe='~')}={urllib.parse.quote(str(v), safe='~')}"
        for k, v in sorted(merged.items())
    )
    string_to_sign = "GET&" + urllib.parse.quote("/", safe="~") + "&" + urllib.parse.quote(canonical, safe="~")
    digest = hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TestAuth(OfflineTestCase):
    def test_sha256_concat_body(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "sha256_concat", "secret_key": "sec",
                                                 "sign_field": "sign", "sign_in": "body"}}, Path("."))
        params = {"b": "2", "a": "1", "empty": "", "sign": "old"}
        applier.apply(params, {})
        expected = hashlib.sha256(("12" + "sec").encode("utf-8")).hexdigest()
        self.assertEqual(params["sign"], expected)

    def test_sha256_concat_header(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "sha256_concat", "secret_key": "sec",
                                                 "sign_field": "X-Sign", "sign_in": "header"}}, Path("."))
        headers = {}
        applier.apply({"a": "1"}, headers)
        self.assertIn("X-Sign", headers)

    def test_token_with_prefix(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "token", "header": "Authorization",
                                                 "prefix": "Bearer ", "value": "abc"}}, Path("."))
        headers = {}
        applier.apply({}, headers)
        self.assertEqual(headers["Authorization"], "Bearer abc")

    def test_basic_and_query(self):
        headers = {}
        auth_mod.AuthApplier({"auth": {"type": "basic", "username": "u", "password": "p"}}, Path(".")).apply({}, headers)
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        params = {}
        auth_mod.AuthApplier({"auth": {"type": "query", "params": {"k": "v"}}}, Path(".")).apply(params, {})
        self.assertEqual(params["k"], "v")

    def test_aliyun_rpc_matches_reference(self):
        params = {"Action": "QueryInstanceBill", "Version": "2017-12-14", "BillingDate": "2026-09-18",
                  "BillingCycle": "2026-09", "PageNum": 1, "PageSize": 300}
        expected = reference_aliyun_sign(params, "AKID", "SECRET", "nonce123", "2026-09-18T00:00:00Z")
        signed = dict(params)
        got = auth_mod.sign_aliyun_rpc(signed, "AKID", "SECRET", nonce="nonce123",
                                       timestamp="2026-09-18T00:00:00Z")
        self.assertEqual(got, expected)
        self.assertEqual(signed["Signature"], expected)
        self.assertEqual(signed["AccessKeyId"], "AKID")

    def test_aliyun_rpc_via_applier(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "aliyun_rpc", "access_key_id": "AKID",
                                                 "access_key_secret": "SECRET"}}, Path("."))
        params = {"Action": "X"}
        applier.apply(params, {}, method="GET")
        for key in ("Signature", "SignatureNonce", "Timestamp", "AccessKeyId", "Format"):
            self.assertIn(key, params)
        expected = reference_aliyun_sign({"Action": "X"}, "AKID", "SECRET",
                                         params["SignatureNonce"], params["Timestamp"])
        self.assertEqual(params["Signature"], expected)

    def test_custom_signer_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.py").write_text(
                "def my_sign(ctx):\n"
                "    ctx['params']['sign'] = 'X' + str(ctx['params']['a'])\n"
                "    return {'headers': {'X-Extra': 'y'}}\n", encoding="utf-8")
            applier = auth_mod.AuthApplier({"auth": {"type": "custom", "module": "signers.py", "func": "my_sign"}},
                                           Path(tmp))
            params, headers = {"a": "1"}, {}
            applier.apply(params, headers)
            self.assertEqual(params["sign"], "X1")
            self.assertEqual(headers["X-Extra"], "y")

    def test_unknown_type(self):
        with self.assertRaises(SystemExit):
            auth_mod.AuthApplier({"auth": {"type": "nope"}}, Path(".")).apply({}, {})


# =============================================================================
# 解析：JSON 记录 / CSV / ZIP
# =============================================================================

class TestParsers(OfflineTestCase):
    def test_extract_json_records_ok(self):
        payload = {"data": {"list": [{"id": 1}]}}
        self.assertEqual(parsers.extract_json_records(payload, {"records_path": "data.list"}, "t"), [{"id": 1}])

    def test_extract_json_records_single_object(self):
        payload = {"data": {"item": {"id": 1}}}
        self.assertEqual(parsers.extract_json_records(payload, {"records_path": "data.item"}, "t"), [{"id": 1}])

    def test_extract_json_records_missing_raises_with_snippet(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.extract_json_records({"code": 1}, {"records_path": "data.list"}, "单次请求")
        message = str(ctx.exception)
        self.assertIn("records_path", message)
        self.assertIn("code", message)

    def test_extract_json_records_missing_ok(self):
        self.assertEqual(parsers.extract_json_records({"code": 1}, {"records_path": "data.list"},
                                                      "t", missing_ok=True), [])

    def test_csv_text(self):
        text = "a,b\n1,x\n2,y\n"
        records = parsers.parse_bytes(text.encode("utf-8"), {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])

    def test_csv_skip_rows_and_entry_field(self):
        text = "报表\n生成时间,今天\na,b\n1,x\n"
        records = parsers.parse_bytes(text.encode("utf-8"),
                                      {"format": "csv", "skip_rows": 2, "entry_field": "__file"}, "t")
        self.assertEqual(records, [{"a": "1", "b": "x", "__file": ""}])

    def test_jsonl(self):
        data = b'{"a": 1}\n\n{"a": 2}\n'
        records = parsers.parse_bytes(data, {"format": "jsonl"}, "t")
        self.assertEqual([r["a"] for r in records], [1, 2])

    def _zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("amount-2026.csv", "user,amount\nu1,5\n")
            archive.writestr("cost-2026.csv", "user,cost\nu1,1\n")
        return buffer.getvalue()

    def test_zip_filter_entries(self):
        records = parsers.parse_bytes(self._zip_bytes(),
                                      {"format": "csv", "unzip": True, "entry_contains": "amount",
                                       "entry_field": "__file"}, "t")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["user"], "u1")
        self.assertEqual(records[0]["__file"], "amount-2026.csv")

    def test_zip_no_match_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True,
                                                    "entry_contains": "nope"}, "t")

    def test_bad_zip_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b"not a zip", {"format": "csv", "unzip": True}, "t")

    def test_parse_payload_dispatch(self):
        records = parsers.parse_payload(b"a\n1\n", {"response_type": "bytes"}, {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1"}])

    def test_csv_skip_until_summary_section(self):
        text = ("Settlement Summary\nBatch,Total\nB1,10\n"
                "Settlement Date,Settlement Batch ID,Transaction ID\n"
                "2026-09-18,B1,T1\n2026-09-18,B1,T2\n")
        records = parsers.parse_bytes(text.encode("utf-8"),
                                      {"format": "csv", "skip_until": "Settlement Date,Settlement Batch ID"}, "t")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["Transaction ID"], "T1")

    def test_csv_skip_until_not_found_returns_empty(self):
        records = parsers.parse_bytes(b"nothing here\n", {"format": "csv", "skip_until": "明细"}, "t")
        self.assertEqual(records, [])

    def test_json_error_body_is_reported(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"respCode":"50217","respMsg":"No api query permissions"}',
                                {"format": "csv"}, "d")
        self.assertIn("50217", str(ctx.exception))


# =============================================================================
# HTTP / 脱敏
# =============================================================================

class FakeResponse:
    def __init__(self, status=200, payload=None, text="", headers=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestHttp(OfflineTestCase):
    def _patch_requests(self, response):
        fake_requests = mock.Mock()
        fake_requests.request.return_value = response
        patcher = mock.patch.object(http_mod, "requests", fake_requests)
        return patcher, fake_requests

    def test_request_once_4xx_fatal(self):
        patcher, _ = self._patch_requests(FakeResponse(status=403, text="denied"))
        with patcher:
            with self.assertRaises(utils.FatalApiError):
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)

    def test_request_once_429_retry_later_with_header(self):
        patcher, _ = self._patch_requests(FakeResponse(status=429, headers={"Retry-After": "7"}, text="slow"))
        with patcher:
            with self.assertRaises(http_mod.RetryLater) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertEqual(ctx.exception.seconds, 7)

    def test_request_once_json_parse_error_retryable(self):
        patcher, _ = self._patch_requests(FakeResponse(status=200, payload=None, text="<html>"))
        with patcher:
            with self.assertRaises(RuntimeError):
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)

    def test_fail_if_not_equals_fatal(self):
        payload = {"Code": "Throttling", "Message": "limit"}
        with self.assertRaises(utils.FatalApiError) as ctx:
            http_mod.check_fail_if(payload, [{"path": "Code", "not_equals": "Success",
                                              "message_path": "Message"}])
        self.assertIn("Throttling", str(ctx.exception))

    def test_fail_if_retry_flag(self):
        with self.assertRaises(RuntimeError):
            http_mod.check_fail_if({"Code": "Throttling"},
                                   [{"path": "Code", "not_equals": "Success", "retry": True}])

    def test_fail_if_accepts_number_and_bool(self):
        # 接口返回数字 20000 / 布尔 true，配置里写字符串也不误判
        http_mod.check_fail_if({"respCode": 20000}, [{"path": "respCode", "not_equals": "20000"}])
        http_mod.check_fail_if({"ok": True}, [{"path": "ok", "not_equals": "true"}])
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"respCode": 40001}, [{"path": "respCode", "not_equals": "20000"}])

    def test_request_with_retry_retries_then_success(self):
        calls = []

        def fake_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        with mock.patch.object(http_mod, "request_once", side_effect=fake_once), \
                mock.patch.object(http_mod.time, "sleep"):
            got = http_mod.request_with_retry("GET", "http://x", {}, {}, "json", 5,
                                              retry_times=3, retry_delay=0)
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_redact(self):
        text = 'GET http://x?a=1&sign=abcdef&token=xyz Authorization: Bearer abc.def.ghi {"secret_key": "s3cr3t"}'
        out = utils.redact(text)
        self.assertNotIn("abcdef", out)
        self.assertNotIn("xyz", out)
        self.assertNotIn("abc.def.ghi", out)
        self.assertNotIn("s3cr3t", out)
        self.assertIn("a=1", out)


# =============================================================================
# Fetcher：分页 / 错误 / 文件模式
# =============================================================================

class TestFetcher(OfflineTestCase):
    def _fetcher(self, job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_page_pagination_with_total_pages(self):
        job = minimal_job(pagination={"type": "page", "page_param": "current", "size_param": "size",
                                      "page_size": 2, "param_as_string": True,
                                      "total_pages_path": "data.totalPages", "delay_seconds": 0})
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(dict(params))
            if params["current"] == "1":
                return {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}}
            return {"data": {"list": [{"id": 3}], "totalPages": 2}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(calls[0]["current"], "1")

    def test_page_pagination_with_total_items_and_defensive_check(self):
        job = minimal_job(pagination={"type": "page", "page_param": "PageNum", "size_param": "PageSize",
                                      "page_size": 2, "total_items_path": "Data.TotalCount",
                                      "delay_seconds": 0})
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [1, 2]}, "TotalCount": 5}},   # page 1
            {"Data": {"Items": {"Item": []}, "TotalCount": 5}},       # 抖动：空页但没拉完 -> 报错
        ]

        def fake(*args, **kwargs):
            return responses.pop(0)

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("TotalCount", str(ctx.exception))

    def test_page_pagination_total_items_complete(self):
        job = minimal_job(pagination={"type": "page", "page_param": "PageNum", "size_param": "PageSize",
                                      "page_size": 2, "total_items_path": "Data.TotalCount",
                                      "delay_seconds": 0})
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [1, 2]}, "TotalCount": 3}},
            {"Data": {"Items": {"Item": [3]}, "TotalCount": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [1, 2, 3])
        self.assertEqual(responses, [])

    def test_cursor_pagination(self):
        job = minimal_job(pagination={"type": "cursor", "cursor_param": "cursor",
                                      "cursor_path": "data.next", "delay_seconds": 0})
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(dict(params))
            if "cursor" not in params:
                return {"data": {"list": [{"id": 1}], "next": "c2"}}
            return {"data": {"list": [{"id": 2}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual(calls[1]["cursor"], "c2")

    def test_none_pagination_single_call(self):
        job = minimal_job()
        calls = []
        with mock.patch.object(http_mod, "request_once",
                               side_effect=lambda *a, **k: (calls.append(a), {"data": {"list": [1]}})[1]):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(records, [1])

    def test_missing_records_path_raises(self):
        job = minimal_job()
        with mock.patch.object(http_mod, "request_once", return_value={"nope": 1}):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_missing_records_path_empty_mode(self):
        job = minimal_job()
        job["request"]["records_missing"] = "empty"
        with mock.patch.object(http_mod, "request_once", return_value={"Data": {"Items": {}}}):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])

    def test_missing_records_path_empty_mode_with_pagination(self):
        job = minimal_job(pagination={"type": "page", "page_param": "PageNum", "size_param": "PageSize",
                                      "page_size": 2, "total_items_path": "Data.TotalCount",
                                      "delay_seconds": 0})
        job["request"]["records_path"] = "Data.Items.Item"
        job["request"]["records_missing"] = "empty"
        with mock.patch.object(http_mod, "request_once",
                               return_value={"Data": {"Items": {}, "TotalCount": 0}}):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])

    def test_max_pages_guard(self):
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n",
                                      "page_size": 1, "max_pages": 3, "delay_seconds": 0,
                                      "total_pages_path": "t"})
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": [1], "t": 99}}):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_bytes_mode_zip_csv(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("amount.csv", "user,amount\nu1,5\n")
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["parse"] = {"format": "csv", "unzip": True, "entry_contains": "amount"}
        job["pagination"] = {"type": "none"}
        with mock.patch.object(http_mod, "request_once", return_value=buffer.getvalue()):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"user": "u1", "amount": "5"}])

    def test_add_fields(self):
        job = minimal_job()
        job["request"]["add_fields"] = {"source_account": "acct1"}
        payload = {"data": {"list": [{"id": 1}, {"id": 2, "source_account": "api"}]}}
        with mock.patch.object(http_mod, "request_once", return_value=payload):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records[0]["source_account"], "acct1")
        self.assertEqual(records[1]["source_account"], "api")   # API 原值优先

    def test_params_in_headers(self):
        job = minimal_job()
        job["request"]["params_in"] = "headers"
        job["request"]["auth"] = {"type": "sha256_concat", "secret_key": "sec", "sign_field": "sign"}
        captured = {}

        def fake(*args, **kwargs):
            captured.update(method=args[0], params=dict(args[2]), headers=dict(args[3]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(captured["params"], {})                       # URL 参数被清空
        self.assertEqual(captured["headers"]["app"], "demo")           # 参数搬到请求头
        self.assertIn("sign", captured["headers"])                     # 签名也在请求头

    def test_build_units_per_day_and_range(self):
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00",
                                  "start_param": "s", "end_param": "e"})
        units = self._fetcher(job).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual([u.label for u in units], ["2026-09-17", "2026-09-18"])
        job_range = minimal_job(window={"mode": "range", "date_tz": "UTC", "api_tz": "+08:00",
                                        "start_param": "s", "end_param": "e"})
        units = self._fetcher(job_range).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual(len(units), 1)

    def test_probe_limits_page_size(self):
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n",
                                      "page_size": 100, "total_pages_path": "t", "delay_seconds": 0})
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 1, "data": {"list": [{"id": 1}, {"id": 2}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 2)
        self.assertEqual(calls[0]["n"], 1)


class TestFetchAll(OfflineTestCase):
    def test_window_retry_then_success(self):
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00",
                                  "start_param": "s", "end_param": "e"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        attempts = {"n": 0}
        collected: list = []

        def flaky(unit):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("boom")
            return [{"id": attempts["n"]}]

        with mock.patch.object(fetcher, "fetch_unit", side_effect=flaky), \
                mock.patch.object(fetch_mod.time, "sleep"):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=1,
                                                on_records=collected.extend)
        self.assertEqual(failures, [])
        self.assertEqual(len(collected), 1)
        self.assertEqual(stats[0][1], 1)

    def test_failures_collected_and_order_kept(self):
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00",
                                  "start_param": "s", "end_param": "e"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 17), date(2026, 9, 18)]
        collected: list = []

        def flaky(unit):
            if unit.day.day == 17:
                raise RuntimeError("bad day")
            return [{"id": 2}]

        with mock.patch.object(fetcher, "fetch_unit", side_effect=flaky), \
                mock.patch.object(fetch_mod.time, "sleep"):
            stats, failures = fetcher.fetch_all(days, window_retries=0, on_records=collected.extend)
        self.assertEqual(len(failures), 1)
        self.assertEqual([r["id"] for r in collected], [2])
        self.assertEqual(stats, [("2026-09-18", 1)])


class TestSpool(OfflineTestCase):
    def test_write_read_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1, "名称": "甲"}, {"b": 2}])
            self.assertEqual(spool.count, 2)
            self.assertGreater(spool.bytes, 0)
            rows = list(spool.iter_rows())
            self.assertEqual(json.loads(rows[0])["名称"], "甲")
            self.assertEqual(json.loads(rows[1]), {"b": 2})
            spool.close()
            self.assertFalse(path.exists())          # 默认用完即删

    def test_keep_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1}])
            spool.close(keep=True)
            self.assertTrue(path.exists())


class TestInitWizard(OfflineTestCase):
    @staticmethod
    def _answers(mapping: dict, choice_answers: list):
        """按问题关键字给答案；编号选择题（请选择编号）按出现顺序从队列取。"""
        choices = list(choice_answers)

        def ask(prompt=""):
            if "请选择编号" in prompt:
                return choices.pop(0) if choices else ""
            for key, value in mapping.items():
                if key in prompt:
                    return value
            return ""
        return ask

    def test_generate_simple_token_page_job(self):
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "请求方法": "",                    # 直接回车 = GET
            "请求头名字": "",                  # Authorization
            "值前缀": "",                      # Bearer
            "Token/Key 的值": "tok123",
            "记录列表在返回": "data.list",
            "页码参数名": "", "每页条数参数名": "", "每页条数": "",   # page/size/100
            "总页数字段路径": "data.totalPages",
            "总条数字段路径": "",
            "最近几天": "15",
            "额外派生参数": "",
            "项目名": "", "表名": "",          # demo_project / ods_demo_api_json_di
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
            "endpoint": "",                    # 默认 us-west-1
        }
        # 选择题顺序：鉴权=1（Header Token）、翻页=1（页码）、窗口=1（按天）、返回=0（JSON）
        ask = self._answers(mapping, ["1", "1", "1", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(out_path=str(Path(tmp) / "demo_api.json"),
                                        ask=ask, echo=lambda *a: None, workdir=Path(tmp))
            self.assertEqual(code, 0)
            job = json.loads((Path(tmp) / "demo_api.json").read_text(encoding="utf-8"))
        self.assertEqual(job["job"], "demo_api")
        self.assertEqual(job["request"]["base_url"], "https://api.example.com")
        self.assertEqual(job["request"]["path"], "/v1/items")
        self.assertEqual(job["request"]["auth"]["value"], "tok123")
        self.assertEqual(job["request"]["records_path"], "data.list")
        self.assertEqual(job["pagination"]["type"], "page")
        self.assertEqual(job["pagination"]["total_pages_path"], "data.totalPages")
        self.assertEqual(job["window"]["mode"], "per_day")
        self.assertEqual(job["target"]["table"], "ods_demo_api_json_di")
        self.assertEqual(job["maxcompute"]["access_key_id"], "AKID")
        config_mod.validate_job(job)          # 生成的配置必须能通过校验

    def test_cancel_on_eof(self):
        def ask(_prompt=""):
            raise EOFError

        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(out_path=str(Path(tmp) / "x.json"), ask=ask,
                                        echo=lambda *a: None, workdir=Path(tmp))
        self.assertEqual(code, 1)
        self.assertFalse((Path(tmp) / "x.json").exists())


# =============================================================================
# MaxCompute：DDL / 校验 / 写入 / SQL 超时
# =============================================================================

class Col:
    def __init__(self, name, type_="string"):
        self.name, self.type = name, type_


class FakeSchema:
    def __init__(self, columns, partitions):
        self.columns, self.partitions = columns, partitions


class FakeTable:
    def __init__(self, columns=None, partitions=None, transactional=False, view=False):
        partitions = partitions if partitions is not None else [Col("pt")]
        columns = columns if columns is not None else [Col("json"), Col("pt")]
        self.table_schema = FakeSchema(columns, partitions)
        self.is_transactional = transactional
        self.is_virtual_view = view
        self.is_materialized_view = False
        self.deleted, self.created, self.writers = [], [], []
        self.fail_next_write = False
        self._writer = FakeWriter()

    def delete_partition(self, spec, if_exists=False):
        self.deleted.append((spec, if_exists))

    def create_partition(self, spec, if_not_exists=False):
        self.created.append((spec, if_not_exists))

    def open_writer(self, partition=None, **kwargs):
        self.writers.append(partition)
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("tunnel boom")
        return self._writer


class FakeWriter:
    def __init__(self):
        self.records = []
        self.closed = False

    def write(self, records):
        self.records.extend(records)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True


class TestTargetTable(OfflineTestCase):
    def test_ddl_contains_structure_and_options(self):
        ddl = mc_mod.build_target_ddl("demo_project", "ods_x_json_df", "json", "备注",
                                      stored_as="aliorc", lifecycle_days=36500)
        self.assertIn("create table if not exists demo_project.ods_x_json_df", ddl)
        self.assertIn("json string", ddl)
        self.assertIn("partitioned by (pt string", ddl)
        self.assertIn("stored as aliorc", ddl)
        self.assertIn("lifecycle 36500", ddl)

    def test_verify_ok_and_rejections(self):
        mc_mod.verify_target_schema(FakeTable(), "t", "json")
        for table in (
            FakeTable(columns=[Col("raw"), Col("pt")]),
            FakeTable(columns=[Col("json")], partitions=[]),
            FakeTable(transactional=True),
            FakeTable(view=True),
            FakeTable(columns=[Col("json", "bigint"), Col("pt")]),
        ):
            with self.assertRaises(SystemExit):
                mc_mod.verify_target_schema(table, "t", "json")

    def test_write_partition_batches_and_retry(self):
        table = FakeTable()
        rows = [json.dumps({"i": i}) for i in range(5)]

        def factory():
            return iter(rows)

        with mock.patch.object(mc_mod, "WRITE_BATCH_SIZE", 2), \
                mock.patch.object(utils.time, "sleep"):
            written = mc_mod.write_partition(table, "ods_x", "20260918", factory, total=5)
        self.assertEqual(written, 5)
        self.assertEqual(table.deleted, [("pt=20260918", True)])
        self.assertEqual(table.created, [("pt=20260918", True)])
        flat = [batch[0] for batch in table._writer.records]
        self.assertEqual(flat, rows)

        # 第一次写失败后自动重试（写前会重新删/建分区，幂等）
        table2 = FakeTable()
        table2.fail_next_write = True
        with mock.patch.object(utils.time, "sleep"):
            written = mc_mod.write_partition(table2, "ods_x", "20260918", factory, total=5, retries=2)
        self.assertEqual(written, 5)
        self.assertEqual(len(table2.deleted), 2)

    def test_write_partition_rejects_oversized_row(self):
        table = FakeTable()

        def factory():
            return iter(["x" * (mc_mod.MAX_ROW_BYTES + 1)])

        with self.assertRaises(SystemExit):
            mc_mod.write_partition(table, "ods_x", "20260918", factory, total=1)

    def test_write_partition_count_mismatch(self):
        table = FakeTable()

        def factory():
            return iter(['{"a":1}'])

        with mock.patch.object(utils.time, "sleep"):
            with self.assertRaises(RuntimeError):
                mc_mod.write_partition(table, "ods_x", "20260918", factory, total=99, retries=1)


class TestRunSqlWithTimeout(OfflineTestCase):
    class FakeInstance:
        def __init__(self, success=True, terminated=False):
            self.success, self.terminated, self.stopped = success, terminated, False

        def is_successful(self):
            return self.success

        def is_terminated(self):
            return self.terminated

        def wait_for_success(self, timeout=1):
            raise RuntimeError("sql failed")

        def stop(self):
            self.stopped = True

    class FakeODPS:
        def __init__(self, instance):
            self.instance = instance

        def run_sql(self, sql):
            self.sql = sql
            return self.instance

    def test_success(self):
        o = self.FakeODPS(self.FakeInstance(success=True))
        mc_mod.run_sql_with_timeout(o, "select 1", timeout=5, desc="测试")

    def test_timeout_stops_instance(self):
        instance = self.FakeInstance(success=False, terminated=False)
        o = self.FakeODPS(instance)

        class FakeTime:
            def __init__(self):
                self.now = 0.0

            def time(self):
                self.now += 1.0
                return self.now

            def sleep(self, _seconds):
                pass

        with mock.patch.object(mc_mod, "time", FakeTime()):
            with self.assertRaises(TimeoutError):
                mc_mod.run_sql_with_timeout(o, "select 1", timeout=5, desc="测试")
        self.assertTrue(instance.stopped)

    def test_terminated_failure_raises(self):
        instance = self.FakeInstance(success=False, terminated=True)
        with mock.patch.object(mc_mod.time, "sleep"):
            with self.assertRaises(RuntimeError):
                mc_mod.run_sql_with_timeout(self.FakeODPS(instance), "select 1", timeout=5, desc="测试")


class TestCountAndCredentials(OfflineTestCase):
    def test_count_partition(self):
        class Reader:
            def __enter__(self):
                return iter([{"cnt": 3}])

            def __exit__(self, *exc_info):
                return False

        class FakeODPS:
            def run_sql(self, sql):
                self.sql = sql
                return FakeODPS.Instance()

            class Instance:
                def is_successful(self):
                    return True

                def is_terminated(self):
                    return False

                def open_reader(self):
                    return Reader()

        o = FakeODPS()
        self.assertEqual(mc_mod.count_partition(o, "demo_project", "t", "20260918"), 3)
        self.assertIn("pt = '20260918'", o.sql)

    def test_load_credentials_from_profile(self):
        ak, sk, source = mc_mod.load_mc_credentials(
            {"name": "teamA", "access_key_id": "id1", "access_key_secret": "s1"}, "作业文件")
        self.assertEqual((ak, sk), ("id1", "s1"))
        self.assertIn("teamA", source)

    def test_load_credentials_env_fallback(self):
        with mock.patch.dict(os.environ, {"ALIYUN_ACCESS_KEY_ID": "env_id",
                                          "ALIYUN_ACCESS_KEY_SECRET": "env_sk"}, clear=False):
            ak, sk, source = mc_mod.load_mc_credentials({}, "作业文件")
        self.assertEqual((ak, sk), ("env_id", "env_sk"))
        self.assertIn("环境变量", source)

    def test_connect_odps_uses_profile_endpoint(self):
        captured = {}

        class FakeODPS:
            def __init__(self, ak, sk, project, endpoint=None):
                captured.update(ak=ak, sk=sk, project=project, endpoint=endpoint)

        with mock.patch.object(mc_mod, "ODPS", FakeODPS):
            mc_mod.connect_odps({}, "job.json",
                                {"access_key_id": "a", "access_key_secret": "b", "endpoint": "http://e"},
                                "proj")
        self.assertEqual(captured["endpoint"], "http://e")
        self.assertEqual(captured["project"], "proj")


# =============================================================================
# 目标解析 / profile / 其他
# =============================================================================

class TestResolveTarget(OfflineTestCase):
    def test_defaults(self):
        job = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        project, table, column, pt = config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))
        self.assertEqual((project, table, column, pt),
                         ("demo_project", "ods_demo_json_df", "json", "20260918"))

    def test_project_from_profile(self):
        job = config_mod.render_job(minimal_job(target={"table": "t"}), {"secrets": {}}, date(2026, 9, 18))
        project, _t, _c, _pt = config_mod.resolve_target(
            job, {"profiles": {"default": {"project": "p1"}}}, make_args(), date(2026, 9, 18))
        self.assertEqual(project, "p1")

    def test_legacy_maxcompute_block(self):
        job = config_mod.render_job(minimal_job(target={"table": "t"}), {"secrets": {}}, date(2026, 9, 18))
        project, _t, _c, _pt = config_mod.resolve_target(
            job, {"maxcompute": {"project": "legacy"}}, make_args(), date(2026, 9, 18))
        self.assertEqual(project, "legacy")

    def test_pt_override_and_validation(self):
        job = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        _p, _t, _c, pt = config_mod.resolve_target(job, {}, make_args(pt="2026-09-18"), date(2026, 9, 18))
        self.assertEqual(pt, "2026-09-18")
        with self.assertRaises(SystemExit):
            config_mod.resolve_target(job, {}, make_args(pt="2026/09/18"), date(2026, 9, 18))


class TestRunLock(OfflineTestCase):
    def test_lock_path_is_per_job(self):
        from api2ods.cli import _lock_path
        first = _lock_path(Path("jobs/onerway.json"))
        second = _lock_path(Path("jobs/aliyun.json"))
        self.assertNotEqual(first, second)
        self.assertTrue(str(first).endswith("onerway.lock"))

    @unittest.skipIf(utils.fcntl is None, "Windows 无 fcntl，运行锁为空操作")
    def test_same_lock_blocks_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path):
                with self.assertRaises(SystemExit):
                    with utils.RunLock(path):
                        pass


class TestLogging(OfflineTestCase):
    def test_log_falls_back_on_non_utf8_console(self):
        class AsciiOnlyStream:
            """模拟 Windows cp1252 控制台：写非 ASCII 字符就抛 UnicodeEncodeError。"""
            encoding = "cp1252"

            def __init__(self):
                self.chunks = []

            def write(self, text):
                text.encode("cp1252")  # 中文/emoji 会在这里抛错
                self.chunks.append(text)

            def flush(self):
                pass

        stream = AsciiOnlyStream()
        with mock.patch("sys.stdout", stream):
            utils.log("中文日志 ❌ 测试")      # 不应抛异常
        self.assertTrue(any("[20" in chunk for chunk in stream.chunks))
        self.assertTrue(any("? " in chunk for chunk in stream.chunks))

    def test_log_writes_to_file_sink(self):
        handle = io.StringIO()
        utils.add_log_sink(handle)
        try:
            utils.log("写一份到文件")
        finally:
            utils._sinks.remove(handle)
        self.assertIn("写一份到文件", handle.getvalue())


class TestRecordToJson(OfflineTestCase):
    def test_compact_and_unicode(self):
        row = record_to_json({"名称": "测试", "n": 1, "nested": {"a": [1, 2]}})
        self.assertIn("测试", row)
        self.assertNotIn(": ", row)
        self.assertEqual(json.loads(row)["nested"]["a"], [1, 2])


class TestVersion(OfflineTestCase):
    def test_version(self):
        self.assertTrue(api2ods.VERSION.startswith("2."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
