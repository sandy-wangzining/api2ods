# -*- coding: utf-8 -*-
"""鉴权与签名。"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    reference_aliyun_sign,
)

from api2ods import auth as auth_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)


class TestAuth(OfflineTestCase):
    def test_sha256_concat_body(self):
        applier = auth_mod.AuthApplier(
            {"auth": {"type": "sha256_concat", "secret_key": "sec", "sign_field": "sign", "sign_in": "body"}}, Path(".")
        )
        params = {"b": "2", "a": "1", "empty": "", "sign": "old"}
        applier.apply(params, {})
        expected = hashlib.sha256(("12" + "sec").encode("utf-8")).hexdigest()
        self.assertEqual(params["sign"], expected)

    def test_sha256_concat_header(self):
        applier = auth_mod.AuthApplier(
            {"auth": {"type": "sha256_concat", "secret_key": "sec", "sign_field": "X-Sign", "sign_in": "header"}},
            Path("."),
        )
        headers = {}
        applier.apply({"a": "1"}, headers)
        self.assertIn("X-Sign", headers)

    def test_bearer(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "bearer", "token": "tok1"}}, Path("."))
        headers = {}
        applier.apply({}, headers)
        self.assertEqual(headers["Authorization"], "Bearer tok1")

    def test_token_with_prefix(self):
        applier = auth_mod.AuthApplier(
            {"auth": {"type": "token", "header": "Authorization", "prefix": "Bearer ", "value": "abc"}}, Path(".")
        )
        headers = {}
        applier.apply({}, headers)
        self.assertEqual(headers["Authorization"], "Bearer abc")

    def test_basic_and_query(self):
        headers = {}
        auth_mod.AuthApplier({"auth": {"type": "basic", "username": "u", "password": "p"}}, Path(".")).apply(
            {}, headers
        )
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        params = {}
        auth_mod.AuthApplier({"auth": {"type": "query", "params": {"k": "v"}}}, Path(".")).apply(params, {})
        self.assertEqual(params["k"], "v")

    def test_aliyun_rpc_matches_reference(self):
        params = {
            "Action": "QueryInstanceBill",
            "Version": "2017-12-14",
            "BillingDate": "2026-09-18",
            "BillingCycle": "2026-09",
            "PageNum": 1,
            "PageSize": 300,
        }
        expected = reference_aliyun_sign(params, "AKID", "SECRET", "nonce123", "2026-09-18T00:00:00Z")
        signed = dict(params)
        got = auth_mod.sign_aliyun_rpc(signed, "AKID", "SECRET", nonce="nonce123", timestamp="2026-09-18T00:00:00Z")
        self.assertEqual(got, expected)
        self.assertEqual(signed["Signature"], expected)
        self.assertEqual(signed["AccessKeyId"], "AKID")

    def test_aliyun_rpc_via_applier(self):
        applier = auth_mod.AuthApplier(
            {"auth": {"type": "aliyun_rpc", "access_key_id": "AKID", "access_key_secret": "SECRET"}}, Path(".")
        )
        params = {"Action": "X"}
        applier.apply(params, {}, method="GET")
        for key in ("Signature", "SignatureNonce", "Timestamp", "AccessKeyId", "Format"):
            self.assertIn(key, params)
        expected = reference_aliyun_sign(
            {"Action": "X"}, "AKID", "SECRET", params["SignatureNonce"], params["Timestamp"]
        )
        self.assertEqual(params["Signature"], expected)

    def test_custom_signer_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.py").write_text(
                "def my_sign(ctx):\n"
                "    ctx['params']['sign'] = 'X' + str(ctx['params']['a'])\n"
                "    return {'headers': {'X-Extra': 'y'}}\n",
                encoding="utf-8",
            )
            applier = auth_mod.AuthApplier(
                {"auth": {"type": "custom", "module": "signers.py", "func": "my_sign"}}, Path(tmp)
            )
            params, headers = {"a": "1"}, {}
            applier.apply(params, headers)
            self.assertEqual(params["sign"], "X1")
            self.assertEqual(headers["X-Extra"], "y")

    def test_unknown_type(self):
        with self.assertRaises(SystemExit):
            auth_mod.AuthApplier({"auth": {"type": "nope"}}, Path(".")).apply({}, {})

    def test_custom_signer_exception_becomes_config_error(self):
        """签名函数自己抛错属于配置/代码问题：要变成 ConfigError，不能被当网络抖动重试。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.py").write_text(
                "def bad(ctx):\n    raise ValueError('secret_key 没配')\n", encoding="utf-8"
            )
            applier = auth_mod.AuthApplier(
                {"auth": {"type": "custom", "module": "signers.py", "func": "bad"}}, Path(tmp)
            )
            with self.assertRaises(utils.ConfigError) as ctx:
                applier.apply({}, {})
            self.assertIn("bad", str(ctx.exception))
            self.assertIn("secret_key 没配", str(ctx.exception))

    def test_missing_signer_file_is_config_error(self):
        with self.assertRaises(utils.ConfigError):
            auth_mod.AuthApplier({"auth": {"type": "custom", "module": "nope.py", "func": "f"}}, Path("."))

    def test_non_python_signer_file_is_config_error(self):
        """module 指向非 .py 文件：spec_from_file_location 返回 None，不能漏成 AttributeError。

        这个文件是存在的，所以"文件不存在"那道检查拦不住它，必须在这里给出人话报错。
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.txt").write_text("不是 Python 代码", encoding="utf-8")
            with self.assertRaises(utils.ConfigError) as ctx:
                auth_mod.AuthApplier({"auth": {"type": "custom", "module": "signers.txt", "func": "f"}}, Path(tmp))
            self.assertIn("signers.txt", str(ctx.exception))
            self.assertNotIn("NoneType", str(ctx.exception))

    def test_query_auth_params_must_be_object(self):
        """auth.params 写成数组时不能裸 AttributeError——那会被当成网络抖动空等十几分钟。"""
        for bad in (["a", "b"], "a=b"):
            applier = auth_mod.AuthApplier({"auth": {"type": "query", "params": bad}}, Path("."))
            with self.assertRaises(utils.ConfigError) as ctx:
                applier.apply({}, {})
            self.assertIn("auth.params", str(ctx.exception))

    def test_custom_signer_malformed_result_is_config_error(self):
        """签名函数返回 {"params": [...]} 这类畸形结构时，并入阶段也要报配置错。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.py").write_text(
                "def weird(ctx):\n    return {'params': ['a', 'b']}\n", encoding="utf-8"
            )
            applier = auth_mod.AuthApplier(
                {"auth": {"type": "custom", "module": "signers.py", "func": "weird"}}, Path(tmp)
            )
            with self.assertRaises(utils.ConfigError):
                applier.apply({}, {})

    def test_signer_syntax_error_is_config_error(self):
        """语法错在正式运行时也不能裸 traceback（原来只有 --check 拦得住）。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "signers.py").write_text("def broken(:\n", encoding="utf-8")
            with self.assertRaises(utils.ConfigError):
                auth_mod.AuthApplier({"auth": {"type": "custom", "module": "signers.py", "func": "f"}}, Path(tmp))


class TestAuthErrorBranches(OfflineTestCase):
    """auth 的自定义签名加载与参数合并分支。"""

    @staticmethod
    def _write_signers(tmp: Path, source: str) -> auth_mod.AuthApplier:
        (tmp / "signers.py").write_text(source, encoding="utf-8")
        return auth_mod.AuthApplier({"auth": {"type": "custom", "module": "signers.py", "func": "my_sign"}}, tmp)

    def test_missing_function_reports_file_and_name(self):
        """signers.py 里没有配置的那个函数：构造时就报（别等发请求才发现）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(utils.ConfigError) as ctx:
                self._write_signers(Path(tmp), "def other(ctx):\n    return {}\n")
            message = str(ctx.exception)
            self.assertIn("my_sign", message)
            self.assertIn("signers.py", message)

    def test_custom_func_result_is_merged_into_request(self):
        """自定义函数返回 {"params": ..., "headers": ...} 时要并入本次请求（Onerway 式签名用法）。"""
        with tempfile.TemporaryDirectory() as tmp:
            applier = self._write_signers(
                Path(tmp),
                "def my_sign(ctx):\n    return {'params': {'sign': 'abc'}, 'headers': {'X-Sign': 'abc'}}\n",
            )
            params, headers = {"page": 1}, {"Accept": "application/json"}
            applier.apply(params, headers, "POST")
        self.assertEqual(params, {"page": 1, "sign": "abc"})
        self.assertEqual(headers, {"Accept": "application/json", "X-Sign": "abc"})

    def test_custom_func_systemexit_passes_through(self):
        """签名函数自己抛 SystemExit（用户按"配置错"处理）时原样透出，不要包成 ConfigError。"""
        with tempfile.TemporaryDirectory() as tmp:
            applier = self._write_signers(Path(tmp), "def my_sign(ctx):\n    raise SystemExit('私钥没配')\n")
            with self.assertRaises(SystemExit) as ctx:
                applier.apply({}, {}, "GET")
        self.assertIn("私钥没配", str(ctx.exception))

    def test_sha256_concat_requires_secret_key(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "sha256_concat"}}, Path("."))
        with self.assertRaises(utils.ConfigError) as ctx:
            applier.apply({"a": 1}, {}, "GET")
        self.assertIn("secret_key", str(ctx.exception))
