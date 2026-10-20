# -*- coding: utf-8 -*-
"""HTTP 请求、重试与 Retry-After。"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    _REQUESTS_AVAILABLE,
    FakeResponse,
    OfflineTestCase,
    make_args,
    minimal_job,
)

from api2ods import config as config_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)


class TestRetryCallGuards(OfflineTestCase):
    def test_deterministic_error_is_not_retried(self):
        import time as _time

        from api2ods import utils as utils_mod

        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise TypeError("字段名写错")

        started = _time.perf_counter()
        with self.assertRaises(RuntimeError) as ctx:
            utils_mod.retry_call(broken, attempts=5, base_delay=30, desc="x")
        self.assertEqual(calls["n"], 1)
        self.assertLess(_time.perf_counter() - started, 1.0)
        self.assertIn("确定性错误", str(ctx.exception))

    def test_headers_must_be_mapping(self):
        from api2ods import utils as utils_mod

        with self.assertRaises(utils_mod.ConfigError):
            utils_mod.check_header_values(["X-Api-Key: x"])

    def test_pt_with_surrounding_spaces_is_accepted(self):
        """--pt " 20260921 " 只多打了空格：按去掉空格后的值校验并使用（换行等仍拒）。"""
        job, _ = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        _p, _t, _c, pt = config_mod.resolve_target(job, {}, make_args(pt=" 20260921 "), date(2026, 9, 18))
        self.assertEqual(pt, "20260921")


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
            http_mod.check_fail_if(payload, [{"path": "Code", "not_equals": "Success", "message_path": "Message"}])
        self.assertIn("Throttling", str(ctx.exception))

    def test_fail_if_retry_flag(self):
        with self.assertRaises(RuntimeError):
            http_mod.check_fail_if({"Code": "Throttling"}, [{"path": "Code", "not_equals": "Success", "retry": True}])

    def test_fail_if_accepts_number_and_bool(self):
        # 接口返回数字 20000 / 布尔 true，配置里写字符串也不误判
        http_mod.check_fail_if({"respCode": 20000}, [{"path": "respCode", "not_equals": "20000"}])
        http_mod.check_fail_if({"ok": True}, [{"path": "ok", "not_equals": "true"}])
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"respCode": 40001}, [{"path": "respCode", "not_equals": "20000"}])
        # CHAR 定长列尾部补齐的空格不能影响比较
        http_mod.check_fail_if({"msg": "fail "}, [{"path": "msg", "not_equals": "fail"}])
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"msg": "fail "}, [{"path": "msg", "equals": "fail"}])
        # 浮点序列化误差（0.1+0.2）不能把业务错误漏判
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"v": 0.1 + 0.2}, [{"path": "v", "equals": 0.3}])
        http_mod.check_fail_if({"v": 0.1 + 0.2}, [{"path": "v", "not_equals": 0.3}])

    def test_request_with_retry_retries_then_success(self):
        calls = []

        def fake_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake_once),
            mock.patch.object(http_mod.time, "sleep"),
        ):
            got = http_mod.request_with_retry(
                "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=3, retry_delay=0
            )
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_request_with_retry_rebuilds_request_each_attempt(self):
        # 一次性签名（阿里云 nonce）重试必须重新生成，否则被判 400 SignatureNonceUsed
        nonces = []

        def build():
            nonces.append(len(nonces))
            return {"SignatureNonce": str(len(nonces))}, {}

        def fake_once(*args, **kwargs):
            if len(nonces) == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake_once),
            mock.patch.object(http_mod.time, "sleep"),
        ):
            http_mod.request_with_retry("GET", "http://x", build, "json", 5, retry_times=3, retry_delay=0)
        self.assertEqual(nonces, [0, 1])

    def test_config_error_does_not_retry(self):
        """鉴权/配置类错误重试不会变好：必须立刻抛出，不能空等 5 次退避。"""
        calls = []

        def build():
            calls.append(1)
            raise utils.ConfigError("自定义签名函数 my_sign 执行失败：ValueError('boom')")

        with mock.patch.object(http_mod.time, "sleep"):
            with self.assertRaises(utils.ConfigError):
                http_mod.request_with_retry("GET", "http://x", build, "json", 5, retry_times=5, retry_delay=15)
        self.assertEqual(len(calls), 1)

    def test_request_once_body_types(self):
        """GET 走 params、form 走 data、其余走 json：三种请求体的落点不能混。"""
        for method, body_type, expected in (
            ("GET", "json", "params"),
            ("POST", "form", "data"),
            ("POST", "json", "json"),
        ):
            patcher, fake = self._patch_requests(FakeResponse(status=200, payload={"ok": 1}))
            with patcher:
                http_mod.request_once(method, "http://x", {"a": 1}, {}, body_type, 5, True, True, None)
            kwargs = fake.request.call_args.kwargs
            self.assertIn(expected, kwargs, f"{method}/{body_type} 应落在 {expected}")
            self.assertEqual(kwargs[expected], {"a": 1})

    def test_request_once_passes_verify_and_proxies(self):
        patcher, fake = self._patch_requests(FakeResponse(status=200, payload={"ok": 1}))
        with patcher:
            http_mod.request_once("POST", "http://x", {}, {}, "json", 5, True, False, {"http": "http://127.0.0.1:7897"})
        kwargs = fake.request.call_args.kwargs
        self.assertFalse(kwargs["verify"])
        self.assertEqual(kwargs["proxies"], {"http": "http://127.0.0.1:7897"})
        # stream=True 是响应体大小上限生效的前提：否则 requests 在 send() 里先整包读入
        self.assertTrue(kwargs["stream"])

    def test_request_once_bytes_returns_raw_content(self):
        """response_type=bytes：原样返回二进制，不做 JSON 解析（ZIP/CSV 走这条）。"""
        response = FakeResponse(status=200, payload=None)
        response.content = b"\x89PNG binary"
        patcher, _ = self._patch_requests(response)
        with patcher:
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 5, False, True, None)
        self.assertEqual(got, b"\x89PNG binary")

    def test_response_body_over_limit_is_rejected(self):
        """Content-Length 超上限：读之前就失败，不能把整个大包读进内存。"""

        class NeverRead(FakeResponse):
            def iter_content(self, chunk_size=1):
                raise AssertionError("Content-Length 已超限，不应读取响应体")

        response = NeverRead(payload={"a": 1})
        response.headers["Content-Length"] = str(len(response.content) + 1)
        patcher, _ = self._patch_requests(response)
        with patcher:
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None, max_response_bytes=5)
        self.assertIn("max_response_bytes", str(ctx.exception))
        self.assertTrue(response.closed)

    def test_chunked_response_over_limit_is_rejected(self):
        """没有 Content-Length 的 chunked 响应：按块累计，超过上限立即停。"""

        class Chunked(FakeResponse):
            def iter_content(self, chunk_size=1):
                for start in range(0, len(self.content), chunk_size):
                    yield self.content[start : start + chunk_size]

        response = Chunked(payload={"a": "x" * 100})
        response.headers.pop("Content-Length", None)
        patcher, _ = self._patch_requests(response)
        with patcher:
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None, max_response_bytes=10)
        self.assertIn("max_response_bytes", str(ctx.exception))
        self.assertTrue(response.closed)

    def test_response_body_under_limit_passes(self):
        response = FakeResponse(payload={"a": 1})
        patcher, _ = self._patch_requests(response)
        with patcher:
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None, max_response_bytes=1024)
        self.assertEqual(got, {"a": 1})
        self.assertTrue(response.closed)

    def test_chunked_response_success_is_parsed(self):
        """按块读取的成功路径：块拼接后要能正常解析（线上的主路径）。"""

        class Chunked(FakeResponse):
            def iter_content(self, chunk_size=1):
                for start in range(0, len(self.content), chunk_size):
                    yield self.content[start : start + chunk_size]

        response = Chunked(payload={"姓名": "张三"})
        response.headers.pop("Content-Length", None)
        patcher, _ = self._patch_requests(response)
        with patcher:
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertEqual(got, {"姓名": "张三"})
        self.assertTrue(response.closed)

    def test_chunked_binary_response_returns_raw_bytes(self):
        """expect_json=False（ZIP/CSV）同样走块读取，返回的字节要完整。"""

        class Chunked(FakeResponse):
            def iter_content(self, chunk_size=1):
                for start in range(0, len(self.content), chunk_size):
                    yield self.content[start : start + chunk_size]

        response = Chunked(content=b"\x89PNG" + b"x" * 100)
        response.headers.pop("Content-Length", None)
        patcher, _ = self._patch_requests(response)
        with patcher:
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 5, False, True, None)
        self.assertEqual(bytes(got), response.content)
        self.assertTrue(response.closed)

    def test_4xx_large_body_keeps_reason_prefix(self):
        """4xx 错误体只取前 64KB：超出部分截断，但开头的原因不能整段丢弃。"""
        seen = {"bytes": 0}

        class Chunked(FakeResponse):
            def iter_content(self, chunk_size=1):
                for start in range(0, len(self.content), chunk_size):
                    chunk = self.content[start : start + chunk_size]
                    seen["bytes"] += len(chunk)
                    yield chunk

        response = Chunked(status=400, text="page size must be >= 10" + "x" * (64 * 1024))
        response.headers.pop("Content-Length", None)
        patcher, _ = self._patch_requests(response)
        with patcher:
            with self.assertRaises(utils.FatalApiError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertIn("page size must be >= 10", str(ctx.exception))
        # 真实只读了 64KB（旧实现 response.text[:300] 会把整包读进内存）
        self.assertLessEqual(seen["bytes"], 64 * 1024)
        self.assertTrue(response.closed)

    def test_4xx_error_body_is_not_decoded_as_latin1(self):
        """requests 对无 charset 的 text/* 默认 ISO-8859-1，不能拿它解错误体。"""
        response = FakeResponse(status=401)
        response.content = "<p>签名错误，请检查 AccessKey</p>".encode()
        response.encoding = "ISO-8859-1"
        patcher, _ = self._patch_requests(response)
        with patcher:
            with self.assertRaises(utils.FatalApiError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertIn("签名错误", str(ctx.exception))

    def test_4xx_invalid_charset_still_becomes_fatal_api_error(self):
        """响应头 charset 拼错（LookupError）也必须收口成 FatalApiError。"""
        response = FakeResponse(status=400)
        response.content = b"bad request"
        response.encoding = "utf8-8"
        patcher, _ = self._patch_requests(response)
        with patcher:
            with self.assertRaises(utils.FatalApiError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertIn("bad request", str(ctx.exception))

    def test_declared_gbk_charset_wins_when_utf8_cannot_decode(self):
        """GBK 字节不是合法 UTF-8 时，按响应头声明的 charset 解码。"""
        response = FakeResponse(content='{"名称": "交易"}'.encode("gbk"))
        response.encoding = "gbk"
        text = http_mod._decode_json_body(response, None)
        self.assertIn("名称", text)

    def test_ambiguous_declared_charset_requires_explicit_encoding(self):
        """声明 GBK、同一份字节也能按 UTF-8 解出另一份合法 JSON：拒绝猜测。"""
        response = FakeResponse(content='{"一": 1}'.encode("gbk"))
        response.encoding = "gbk"
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(response, None)
        self.assertIn("json_encoding", str(ctx.exception))
        # 按提示显式指定后必须能跑：声明与显式配置一致时信任用户，不能两条路径互相拒绝
        with mock.patch.object(http_mod, "log_once") as log_once:
            text = http_mod._decode_json_body(response, "gbk")
        self.assertIn("一", text)
        self.assertTrue(log_once.called)

    def test_declared_charset_that_cannot_parse_json_falls_back_to_utf8(self):
        """声明 utf-16、但字节其实是 UTF-8：UTF-8 能解出 JSON，不能返回乱码让上层白重试。"""
        body = '{"name": "张三"}'
        response = FakeResponse(content=body.encode())
        response.encoding = "utf-16"
        with mock.patch.object(http_mod, "log_once") as warn:
            text = http_mod._decode_json_body(response, None)
        self.assertEqual(text, body)
        self.assertTrue(warn.called)

    def test_explicit_charset_that_cannot_parse_json_is_config_error(self):
        """显式 json_encoding 解不出 JSON、UTF-8 能解出：配置错，不能继续用乱码。"""
        response = FakeResponse(content='{"name": "张三"}'.encode())
        response.encoding = "utf-16"
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(response, "utf-16")
        self.assertIn("json_encoding", str(ctx.exception))

    def test_utf8_body_declared_as_gbk_is_rejected(self):
        """服务端把 UTF-8 误声明成 GBK：GBK 能解成乱码 JSON，必须报错而不是写库。"""
        response = FakeResponse(content='{"name": "张三"}'.encode())
        response.encoding = "gbk"
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(response, None)
        self.assertIn("charset", str(ctx.exception))

    def test_request_once_missing_requests_reports_install_hint(self):
        with mock.patch.object(http_mod, "requests", None):
            with self.assertRaises(utils.FatalApiError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertIn("pip install requests", str(ctx.exception))

    def test_retry_after_garbage_is_ignored(self):
        """Retry-After 写了没法解析的内容：当成没给，用本地退避（不能因此报错）。"""
        response = FakeResponse(status=429, headers={"Retry-After": "一会儿再说"})
        self.assertIsNone(http_mod._retry_after_seconds(response))

    def test_retry_after_negative_clamped_to_zero(self):
        response = FakeResponse(status=429, headers={"Retry-After": "-1"})
        self.assertEqual(http_mod._retry_after_seconds(response), 0.0)

    def test_retry_after_http_date_without_timezone_treated_as_gmt(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=2)
        # format_datetime 固定输出英文 RFC 2822；[:-6] 去掉 " +0000" 得到无时区形式
        stamp = format_datetime(future)[:-6]
        response = FakeResponse(status=429, headers={"Retry-After": stamp})
        seconds = http_mod._retry_after_seconds(response)
        self.assertIsNotNone(seconds)
        self.assertGreater(seconds, 60)  # 约 2 分钟，允许一点执行误差

    def test_retry_after_http_date_is_capped(self):
        """服务端给个很远的未来时间：等下去等于把任务挂死，必须封顶。"""
        future = datetime.now(timezone.utc) + timedelta(hours=5)
        response = FakeResponse(status=429, headers={"Retry-After": format_datetime(future, usegmt=True)})
        self.assertEqual(http_mod._retry_after_seconds(response), http_mod.MAX_RETRY_AFTER)

    def test_fail_if_skipped_for_binary_payload(self):
        """文件类响应没有字段可查：抄了 fail_if 也不能让任务每天必失败。"""
        http_mod.check_fail_if(b"PK\x03\x04", [{"path": "Code", "not_equals": "Success"}])

    def test_fail_if_retry_later_carries_message(self):
        with self.assertRaises(http_mod.RetryLater) as ctx:
            http_mod.check_fail_if({"Code": "Throttling"}, [{"path": "Code", "not_equals": "Success", "retry": True}])
        self.assertIn("Throttling", str(ctx.exception))
        self.assertIsNone(ctx.exception.seconds)  # 没给秒数 → 上层用本地退避

    def test_retry_later_honours_server_delay(self):
        """429 带 Retry-After：必须按服务端要求等，而不是自己拍脑袋退避。"""
        waits = []
        calls = []

        def fake_once(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise http_mod.RetryLater(7, "HTTP 429")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake_once),
            mock.patch.object(http_mod.time, "sleep", side_effect=waits.append),
        ):
            got = http_mod.request_with_retry(
                "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=3, retry_delay=15
            )
        self.assertEqual(got, {"ok": True})
        self.assertEqual(waits, [7, 7])

    def test_exhausted_retries_report_last_error(self):
        with (
            mock.patch.object(http_mod, "request_once", side_effect=RuntimeError("connection reset")),
            mock.patch.object(http_mod.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                http_mod.request_with_retry(
                    "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=3, retry_delay=0, desc="拉取"
                )
        message = str(ctx.exception)
        self.assertIn("拉取", message)
        self.assertIn("connection reset", message)

    def test_zero_retry_times_makes_exactly_one_request(self):
        """retry_times=0 = 失败不重试（只发一次），不该白睡一轮再报"重试 0 次仍失败"。"""
        calls, waits = [], []

        def boom(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("boom")

        with (
            mock.patch.object(http_mod, "request_once", side_effect=boom),
            mock.patch.object(http_mod.time, "sleep", side_effect=waits.append),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                http_mod.request_with_retry(
                    "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=0, retry_delay=15, desc="拉取"
                )
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [])  # 一次退避都没有
        self.assertIn("重试 0 次仍失败", str(ctx.exception))

    def test_retry_times_means_additional_attempts(self):
        """retry_times=N 表示"失败后再试 N 次"，总请求数 = N+1（与文档一致）。"""
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("boom")

        with mock.patch.object(http_mod, "request_once", side_effect=boom), mock.patch.object(http_mod.time, "sleep"):
            with self.assertRaises(RuntimeError):
                http_mod.request_with_retry(
                    "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=2, retry_delay=0, desc="拉取"
                )
        self.assertEqual(len(calls), 3)

    def test_retry_log_is_redacted(self):
        """重试日志里带 URL 签名/token 时也要脱敏（日志一落盘就等于泄露）。"""
        logged = []
        with (
            mock.patch.object(http_mod, "request_once", side_effect=RuntimeError("bad token=SECRETVALUE")),
            mock.patch.object(http_mod.time, "sleep"),
            mock.patch.object(http_mod, "log", side_effect=logged.append),
        ):
            with self.assertRaises(RuntimeError):
                http_mod.request_with_retry(
                    "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=2, retry_delay=0
                )
        self.assertTrue(logged)
        self.assertNotIn("SECRETVALUE", "\n".join(logged))
        self.assertIn("token=***", logged[0])

    @unittest.skipUnless(_REQUESTS_AVAILABLE, "未安装 requests")
    def test_network_errors_are_retried(self):
        """连接抖动必须走请求级重试。

        回归：requests 的 ConnectionError / Timeout / SSLError 都是 OSError 子类，
        曾经误把 OSError 当"本地配置错误"直接抛出，导致重试被整个跳过。
        """
        from requests.exceptions import ConnectionError as RequestsConnectionError
        from requests.exceptions import Timeout

        calls = []

        def fake_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RequestsConnectionError("connection reset")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake_once),
            mock.patch.object(http_mod.time, "sleep"),
        ):
            got = http_mod.request_with_retry(
                "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=5, retry_delay=0
            )
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(calls), 2)

        # 超时同理（Timeout 也是 OSError 子类）
        calls.clear()

        def fake_timeout(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise Timeout("read timed out")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake_timeout),
            mock.patch.object(http_mod.time, "sleep"),
        ):
            got = http_mod.request_with_retry(
                "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=5, retry_delay=0
            )
        self.assertEqual(got, {"ok": True})
        self.assertEqual(len(calls), 3)

    def test_redact(self):
        text = 'GET http://x?a=1&sign=abcdef&token=xyz Authorization: Bearer abc.def.ghi {"secret_key": "s3cr3t"}'
        out = utils.redact(text)
        self.assertNotIn("abcdef", out)
        self.assertNotIn("xyz", out)
        self.assertNotIn("abc.def.ghi", out)
        self.assertNotIn("s3cr3t", out)
        self.assertIn("a=1", out)


class TestRetryAfterDate(OfflineTestCase):
    def test_http_date_accepted(self):
        # 必须用相对当前时刻的日期：写死某个未来日期，过一天这个用例就会自己变红
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        response = mock.Mock()
        response.headers = {"Retry-After": format_datetime(future, usegmt=True)}
        self.assertEqual(http_mod._retry_after_seconds(response), http_mod.MAX_RETRY_AFTER)

    def test_http_date_in_the_past_is_zero(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        response = mock.Mock()
        response.headers = {"Retry-After": format_datetime(past, usegmt=True)}
        self.assertEqual(http_mod._retry_after_seconds(response), 0.0)

    def test_seconds_still_work(self):
        response = mock.Mock()
        response.headers = {"Retry-After": "42"}
        self.assertEqual(http_mod._retry_after_seconds(response), 42)

    def test_garbage_returns_none(self):
        response = mock.Mock()
        response.headers = {"Retry-After": "soon"}
        self.assertIsNone(http_mod._retry_after_seconds(response))


class TestHttpRetryBranches(OfflineTestCase):
    """http 的 Retry-After 边界与 RetryLater 耗尽出口。"""

    def test_missing_retry_after_header(self):
        """429/5xx 没带 Retry-After：返回 None，由上层用默认退避（不能崩在取值上）。"""
        response = mock.Mock()
        response.headers = {}
        self.assertIsNone(http_mod._retry_after_seconds(response))

    def test_absurd_http_date_does_not_raise(self):
        """服务端给了离谱日期（9999 年这类）：取秒数不能抛 OverflowError，退回默认退避。"""

        class AbsurdDate(datetime):
            def __sub__(self, other):
                raise OverflowError("date value out of range")

        response = mock.Mock()
        response.headers = {"Retry-After": "Mon, 31 Dec 9999 23:59:59 GMT"}
        with mock.patch.object(
            http_mod, "parsedate_to_datetime", return_value=AbsurdDate(9999, 12, 31, tzinfo=timezone.utc)
        ):
            self.assertIsNone(http_mod._retry_after_seconds(response))

    def test_retry_later_exhausted_reports_failure(self):
        """429/5xx 一直重试仍失败：抛 RuntimeError，并且每轮都按 Retry-After 等（不是默认退避）。"""
        calls, sleeps = [], []

        def always_retry_later(*_args, **_kwargs):
            calls.append(1)
            raise http_mod.RetryLater(5, "HTTP 503")

        with (
            mock.patch.object(http_mod, "request_once", side_effect=always_retry_later),
            mock.patch.object(http_mod.time, "sleep", side_effect=sleeps.append),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                http_mod.request_with_retry(
                    "GET", "https://x", lambda: ({}, {}), "json", 5, retry_times=2, retry_delay=1, desc="拉取"
                )
        self.assertEqual(len(calls), 3)  # 首次 + 重试 2 次
        self.assertEqual(sleeps, [5, 5])  # 服务端要求等 5 秒
        self.assertIn("重试 2 次仍失败", str(ctx.exception))
