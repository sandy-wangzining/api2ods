# -*- coding: utf-8 -*-
"""api2ods v2 离线单元测试：不访问网络、不连 MaxCompute（requests/pyodps 没装也能跑）。

没装 requests 时，少数必须借用真实 requests 异常类型的用例会 skip（见 _REQUESTS_AVAILABLE），
其余用例照常执行——离线环境/最小编译机上 `python -m unittest discover -s tests` 应当全绿。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import base64
import csv
import errno
import gc
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import weakref
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api2ods  # noqa: E402
from api2ods import auth as auth_mod  # noqa: E402
from api2ods import cli as cli_mod  # noqa: E402
from api2ods import config as config_mod  # noqa: E402
from api2ods import dates as dates_mod  # noqa: E402
from api2ods import fetch as fetch_mod  # noqa: E402
from api2ods import fieldwatch as fieldwatch_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    init_wizard,  # noqa: E402
    parsers,
    utils,
)
from api2ods import mc as mc_mod  # noqa: E402
from api2ods import notify as notify_mod  # noqa: E402
from api2ods import spool as spool_mod  # noqa: E402
from api2ods.cli import record_to_json  # noqa: E402

# 少数用例要构造真实的 requests.exceptions（ConnectionError/Timeout…），或依赖 requests 自己
# 抛出的 MissingSchema；没装 requests 时这些类型根本不存在。用一个开关把它们 skip 掉，
# 其余 560+ 个用例仍然照跑——文档承诺的"没装 requests 也能跑"才是真的。
_REQUESTS_AVAILABLE = getattr(http_mod, "requests", None) is not None


class OfflineTestCase(unittest.TestCase):
    """所有用例的基类：禁止真实 sleep（把 time.sleep 变成空操作），并统一控制台编码。"""

    def setUp(self):
        utils.setup_console()
        self._console = mock.patch.object(utils, "_console_patched", True)
        self._console.start()
        self.addCleanup(self._console.stop)
        # 离线用例不能在真实退避里空等（重试间隔 15s 起步、封顶 300s）：把 sleep 变成空操作。
        # 每个用例独立 start/stop（addCleanup），不会跨用例泄漏；"退避时长是否按预期被请求"
        # 由 TestEleventhPassReview.test_retry_backoff_delays_are_requested 用记录调用验证
        patcher = mock.patch.object(time, "sleep", lambda *_args, **_kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 运行锁默认落在仓库根的 .run-locks/：单测用的临时作业路径每次哈希都不同，
        # 锁文件会无限累积（生产行为不变，这里只把锁根目录重定向到临时目录并在收尾清理）
        lock_root = tempfile.mkdtemp(prefix="api2ods-test-locks-")
        self.addCleanup(shutil.rmtree, lock_root, ignore_errors=True)
        lock_patcher = mock.patch.object(cli_mod, "ROOT", Path(lock_root))
        lock_patcher.start()
        self.addCleanup(lock_patcher.stop)


def make_args(**overrides):
    base = dict(
        job="jobs/x.json",
        config="",
        check=False,
        bizdate=None,  # 与 argparse 的默认值一致：None = 没给；空串 = 显式给了但为空（要报错）
        days=None,
        dates=None,  # 与 argparse 的默认值一致：None = 没给；空串 = 显式给了但为空（要报错）
        start_date="",
        end_date="",
        pt="",
        workers=1,
        dry_run=False,
        allow_empty=False,
        keep_spool=False,
        endpoint="",
        mc_profile="",
        cli_profile="",
        sql_timeout=600,
        log_file="",
        no_notify=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def minimal_job(**overrides) -> dict:
    job = {
        "job": "demo",
        "request": {
            "base_url": "https://api.example.com",
            "path": "/v1/items",
            "method": "GET",
            "records_path": "data.list",
            "params": {"app": "demo"},
        },
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
    def test_redact_survives_recursion_error_from_json(self):
        """反转义时 json.loads 抛 RecursionError（超深嵌套）不能打穿脱敏流程。"""
        with mock.patch.object(utils.json, "loads", side_effect=RecursionError("too deep")):
            out = utils.redact('{"k": "v"x"}')
        self.assertIsInstance(out, str)

    def test_replace_nested_and_mixed(self):
        ctx = {"secrets": {"k": "S"}, "bizdate": "20260918"}
        value = {"a": "Bearer ${secrets.k}", "b": ["${bizdate}", 1], "c": {"d": "${bizdate}-x"}}
        self.assertEqual(
            config_mod.deep_substitute(value, ctx), {"a": "Bearer S", "b": ["20260918", 1], "c": {"d": "20260918-x"}}
        )

    def test_whole_placeholder_keeps_type(self):
        self.assertEqual(config_mod.deep_substitute("${secrets.lst}", {"secrets": {"lst": [1, 2]}}), [1, 2])

    def test_unknown_placeholder_raises_with_hint(self):
        with self.assertRaises(SystemExit) as ctx:
            config_mod.deep_substitute("${secrets.nope}", {"secrets": {}})
        self.assertIn("secrets", str(ctx.exception))

    def test_credential_field_keeps_literal_placeholder_chars(self):
        """凭据字段是自由文本：密钥里的 "${" 只是字符，不该让整份配置加载失败。"""
        value = {"headers": {"Authorization": "Bearer p@ss${word"}, "params": {"password": "a${b}c"}}
        self.assertEqual(
            config_mod.deep_substitute(value, {"secrets": {}}),
            {"headers": {"Authorization": "Bearer p@ss${word"}, "params": {"password": "a${b}c"}},
        )

    def test_credential_field_keeps_whole_literal_placeholder(self):
        """整串 ${...} 但键不存在：凭据字段按字面量保留（密钥本身可能就是这个字符串）。"""
        self.assertEqual(
            config_mod.deep_substitute({"headers": {"Authorization": "${secrets.old}"}}, {"secrets": {}}),
            {"headers": {"Authorization": "${secrets.old}"}},
        )

    def test_credential_field_still_resolves_real_placeholders(self):
        value = {"headers": {"Authorization": "Bearer ${secrets.t}"}}
        self.assertEqual(
            config_mod.deep_substitute(value, {"secrets": {"t": "S"}}),
            {"headers": {"Authorization": "Bearer S"}},
        )

    def test_non_credential_field_still_rejects_unclosed(self):
        with self.assertRaises(SystemExit):
            config_mod.deep_substitute({"url": "https://x/${bad"}, {"secrets": {}})

    def test_inline_placeholder_null_raises(self):
        """secrets.x 为 null 时内联解析不能静默变 "None"：与键路径同口径报错。

        普通字段与凭据字段都算——"Bearer ${secrets.t}" 拼出 "Bearer None"
        只会让请求 401，报错却指不到配置上。
        """
        with self.assertRaises(SystemExit) as ctx:
            config_mod.deep_substitute({"url": "https://x/${secrets.ns}/y"}, {"secrets": {"ns": None}})
        self.assertIn("不是标量", str(ctx.exception))
        with self.assertRaises(SystemExit):
            config_mod.deep_substitute(
                {"headers": {"Authorization": "Bearer ${secrets.ns}"}}, {"secrets": {"ns": None}}
            )
        # 整串占位符解析成 null 同样拒绝（None 到 requests 的 params 会被编码成 "None" 发出去）
        with self.assertRaises(SystemExit):
            config_mod.deep_substitute({"params": {"token": "${secrets.ns}"}}, {"secrets": {"ns": None}})

    def test_render_job_uses_bizdate(self):
        got, _ = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        self.assertEqual(got["target"]["pt"], "20260918")

    def test_render_job_inline_secrets(self):
        job = minimal_job()
        job["secrets"] = {"onerway_secret_key": "inline-secret"}
        job["maxcompute"] = {"project": "p1", "access_key_id": "a", "access_key_secret": "b"}
        job["request"]["auth"] = {"type": "sha256_concat", "secret_key": "${secrets.onerway_secret_key}"}
        got, _ = config_mod.render_job(job, {}, date(2026, 9, 18))
        self.assertEqual(got["request"]["auth"]["secret_key"], "inline-secret")
        self.assertEqual(got["secrets"]["onerway_secret_key"], "inline-secret")
        self.assertEqual(got["maxcompute"]["project"], "p1")
        # 作业内 maxcompute 优先于 config
        profile = config_mod.get_mc_profile_meta({}, got, make_args())
        self.assertEqual(profile["project"], "p1")

    def test_render_job_twice_with_different_secrets_no_contamination(self):
        """同一份 --config 用不同 secrets 连渲染两次：第二次不能再拿第一次替换过的字面量。

        原来的实现就地改写 config：第二次替换时 ${...} 已经不见了，第二个作业会静默
        使用第一个作业的 AK（多实例复用同一份 config 的用法正好踩这一条）。
        """
        config = {"secrets": {"ak": "AK_ONE"}, "maxcompute": {"access_key_id": "${secrets.ak}"}}
        _j1, cfg1 = config_mod.render_job(minimal_job(), config, date(2026, 9, 18))
        config2 = {"secrets": {"ak": "AK_TWO"}, "maxcompute": {"access_key_id": "${secrets.ak}"}}
        _j2, cfg2 = config_mod.render_job(minimal_job(), config2, date(2026, 9, 18))
        self.assertEqual(cfg1["maxcompute"]["access_key_id"], "AK_ONE")
        self.assertEqual(cfg2["maxcompute"]["access_key_id"], "AK_TWO")

    def test_render_job_secret_not_substituted(self):
        job = minimal_job()
        job["secrets"] = {"k": "value-${bizdate}"}  # 密钥里的占位符样式内容不被替换
        got, _ = config_mod.render_job(job, {}, date(2026, 9, 18))
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
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("target.table", str(ctx.exception))
        self.assertNotIn("KeyError", str(ctx.exception))

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

    def test_page_stop_when_short_ok(self):
        """接口不返回总数时（如 XMP Open API）用 stop_when_short 按短页翻完，校验放行。"""
        job = minimal_job()
        job["pagination"] = {
            "type": "page",
            "page_param": "page",
            "size_param": "page_size",
            "stop_when_short": True,
        }
        config_mod.validate_job(job)

    def test_page_stop_when_short_conflicts_with_total_paths(self):
        job = minimal_job()
        job["pagination"] = {
            "type": "page",
            "stop_when_short": True,
            "total_items_path": "data.totalCount",
        }
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_page_stop_when_short_bad_bool_rejected(self):
        job = minimal_job()
        job["pagination"] = {"type": "page", "stop_when_short": "flase"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_cursor_stop_when_short_rejected(self):
        job = minimal_job()
        job["pagination"] = {"type": "cursor", "cursor_path": "data.next", "stop_when_short": True}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_stop_when_short_infers_page_type(self):
        """只写 stop_when_short（没写 page_param/终点路径）时也应推断成 page。"""
        job = minimal_job()
        job["pagination"] = {"stop_when_short": True}
        got = config_mod.normalize_job(job)
        self.assertEqual(got["pagination"]["type"], "page")
        self.assertEqual(got["pagination"]["page_param"], "page")

    def test_cursor_needs_path(self):
        job = minimal_job()
        job["pagination"] = {"type": "cursor"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_notify_bad_webhook_type(self):
        job = minimal_job()
        job["notify"] = {"webhook": 123}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_notify_bad_enabled(self):
        job = minimal_job()
        job["notify"] = {"enabled": "flase"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_notify_unknown_key_warns(self):
        job = minimal_job()
        job["notify"] = {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc1234", "wehbook": "typo"}
        warnings = config_mod.collect_warnings(job)
        self.assertTrue(any("notify.wehbook" in item for item in warnings))

    def test_notify_block_must_be_dict(self):
        job = minimal_job()
        job["notify"] = [1, 2]
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_resolve_notify_merges_with_job_priority(self):
        merged = config_mod.resolve_notify(
            {"notify": {"webhook": "job-hook", "enabled": False}},
            {"notify": {"webhook": "shared-hook", "enabled": True}},
        )
        self.assertEqual(merged, {"webhook": "job-hook", "enabled": False})
        self.assertEqual(config_mod.resolve_notify({}, {"notify": {"webhook": "shared"}}), {"webhook": "shared"})
        self.assertEqual(config_mod.resolve_notify({}, {}), {})

    def test_window_defaults_param_names(self):
        job = minimal_job()
        job["window"] = {"days": 3}
        got = config_mod.normalize_job(job)
        self.assertEqual(got["window"]["start_param"], "startTime")
        self.assertEqual(got["window"]["end_param"], "endTime")
        config_mod.validate_job(got)  # 补完默认值后必须能过校验

    def test_range_needs_end_param(self):
        job = minimal_job()
        job["window"] = {"mode": "range", "start_param": "startTime", "end_param": None}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_range_gets_default_end_param(self):
        job = minimal_job()
        job["window"] = {"mode": "range"}
        got = config_mod.normalize_job(job)
        self.assertEqual(got["window"]["start_param"], "startTime")
        self.assertEqual(got["window"]["end_param"], "endTime")
        config_mod.validate_job(got)

    def test_range_custom_start_param_without_end_param_rejected(self):
        """只自定义 start_param 时不再凭空补 endTime；range 模式缺结束参数直接报错。"""
        job = minimal_job()
        job["window"] = {"mode": "range", "start_param": "start"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(config_mod.normalize_job(job))

    def test_block_wrong_type_is_friendly_error(self):
        """window/pagination 写成字符串时不能裸抛 "dictionary update sequence" traceback。

        这道检查要在**读文件之后立刻**跑：cli 紧接着就调 date_tz_of(job["window"]["date_tz"])，
        normalize_job 里也马上 dict(job["pagination"])，两边拿到字符串都是裸 AttributeError。
        """
        for block in ("window", "pagination", "request", "parse", "target"):
            job = minimal_job()
            job[block] = "per_day"
            with self.assertRaises(utils.ConfigError) as ctx:
                config_mod.check_block_types(job)
            self.assertIn(block, str(ctx.exception))

    def test_request_params_and_headers_must_be_object(self):
        """params 写成数组会被 requests 当"参数名列表"，固定参数静默全丢。"""
        for block in ("params", "headers", "proxies"):
            job = minimal_job()
            job["request"][block] = ["a=1", "b=2"] if block != "proxies" else ["http://p:1"]
            with self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(job)
            self.assertIn(block, str(ctx.exception))

    def test_bytes_needs_parse_format(self):
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bytes_rejects_pagination(self):
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["parse"] = {"format": "csv"}
        job["pagination"] = {"type": "page", "page_param": "p", "size_param": "n", "total_pages_path": "t"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_custom_auth_needs_func(self):
        job = minimal_job()
        job["request"]["auth"] = {"type": "custom"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_bearer_auth_needs_token(self):
        job = minimal_job()
        job["request"]["auth"] = {"type": "bearer"}
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
        self.assertEqual([d.isoformat() for d in days], ["2026-09-16", "2026-09-17", "2026-09-18"])

    def test_env_bizdate(self):
        with mock.patch.dict(os.environ, {"bizdate": "20260918"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-18"])

    def test_malformed_env_bizdate_errors(self):
        """环境变量畸形必须报错：静默回退"昨天"会把数据写进错的分区（还是先删再填）。"""
        with mock.patch.dict(os.environ, {"bizdate": "2026-09-1"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.env_bizdate()
            self.assertIn("2026-09-1", str(ctx.exception))

    def test_blank_env_bizdate_errors_in_strict_mode(self):
        """只含空白的 bizdate 是"设了但没法用"：严格模式必须报错，不能静默回退昨天。

        （原来 strip 后当"没设置"返回 None → 数据写进错的分区且退出码为 0。）
        """
        for blank in ("   ", "	"):
            with mock.patch.dict(os.environ, {"bizdate": blank}, clear=False):
                os.environ.pop("SKYNET_BIZDATE", None)
                with self.assertRaises(SystemExit) as ctx:
                    dates_mod.env_bizdate()
                self.assertIn("空白", str(ctx.exception))
        # 非严格（--check）按"未设置"处理并继续看下一个环境变量
        with mock.patch.dict(os.environ, {"bizdate": "   ", "SKYNET_BIZDATE": "20260918"}, clear=False):
            self.assertEqual(dates_mod.env_bizdate(strict=False), date(2026, 9, 18))

    def test_malformed_env_bizdate_not_swallowed_by_resolve_days(self):
        with mock.patch.dict(os.environ, {"bizdate": "202609181"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            with self.assertRaises(SystemExit):
                dates_mod.resolve_days(make_args(), {})

    def test_explicit_bizdate_wins_over_malformed_env(self):
        """畸形 env 不能压过显式 --bizdate：否则文档/报错教的"用 --bizdate 指定"永远走不通。"""
        with mock.patch.dict(os.environ, {"bizdate": "2026-09-1"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(bizdate="20260918"), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-18"])

    def test_dates_mode_ignores_malformed_env(self):
        """--dates 只指定"拉哪些天"，根本不取业务日，畸形的 env 不该拦。"""
        with mock.patch.dict(os.environ, {"bizdate": "oops"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(dates="20260918"), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-18"])

    def test_check_tolerates_malformed_env(self):
        """只读体检不写库：环境变量脏了也要能看连通性（按默认业务日继续并打警告）。"""
        with mock.patch.dict(os.environ, {"bizdate": "oops"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(check=True), {"window": {"date_tz": "UTC"}})
        self.assertEqual(len(days), 1)

    def test_start_date_without_end_date_is_config_error(self):
        """精简 namespace（库调用方只传 start_date、没有 end_date 属性）也要给配置错，不抛 AttributeError。"""

        class SlimArgs:
            start_date = "2026-09-01"

        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(SlimArgs(), {})
        self.assertIn("成对", str(ctx.exception))

    def test_start_end_range(self):
        days = dates_mod.resolve_days(make_args(start_date="2026-09-01", end_date="2026-09-03"), {})
        self.assertEqual(len(days), 3)

    def test_dates_list(self):
        days = dates_mod.resolve_days(make_args(dates="20260901,2026-09-05"), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-01", "2026-09-05"])

    def test_pair_required(self):
        with self.assertRaises(SystemExit):
            dates_mod.resolve_days(make_args(start_date="2026-09-01"), {})

    def test_float_days_rejected_not_truncated(self):
        """window.days=2.5 原来 int() 静默截成 2（少回拉一天）：与 0/非法字符串同口径报错。"""
        job = minimal_job()
        job["window"] = {"mode": "per_day", "date_tz": "UTC", "days": 2.5}
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(), job)
        self.assertIn("整数", str(ctx.exception))

    def test_extra_params_none_uses_default_format(self):
        """extra_params 值为 None 不能提前 str 成字面量 "None" 发给接口。"""
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 0,
            "start_param": "BillingDate",
            "format": "%Y-%m-%d",
            "extra_params": {"statDate": None},
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])[0]
        self.assertNotEqual(got["statDate"], "None")
        self.assertIn("2026-09-18", got["statDate"])

    def test_non_list_warnings_key_does_not_crash_validate(self):
        """用户误写 "__warnings__": "note"（非 list）时，validate_job 挂告警不能裸崩。"""
        job = minimal_job(pagination={"type": "none", "size_param": "size", "page_size": 100})
        job["__warnings__"] = "note"
        config_mod.validate_job(job)  # 不抛 AttributeError

    def test_explicit_empty_bizdate_errors(self):
        """显式传空 --bizdate（脚本变量未展开）必须报错，不能按"未指定"静默回退。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                dates_mod.resolve_days(make_args(bizdate=""), {})

    def test_default_uses_window_days(self):
        days = dates_mod.resolve_days(make_args(), {"window": {"days": 3, "date_tz": "UTC"}})
        self.assertEqual(len(days), 3)


class TestWindowParams(OfflineTestCase):
    win = {
        "mode": "per_day",
        "date_tz": "UTC",
        "api_tz": "+08:00",
        "pad_hours": 0,
        "start_param": "startTime",
        "end_param": "endTime",
        "format": "%Y-%m-%d %H:%M:%S",
    }

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

    def test_mode_is_normalized_and_validated(self):
        """mode 去空白后仍按 range 走；拼错的 mode 要报错——静默按 per_day 会让
        range 语义与区间校验（extra_params）一起失效。"""
        days = [date(2026, 9, 18), date(2026, 9, 20)]
        got = dates_mod.window_param_sets({"window": dict(self.win, mode="range ")}, days)
        self.assertEqual(got, dates_mod.window_param_sets({"window": dict(self.win, mode="range")}, days))
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.window_param_sets({"window": dict(self.win, mode="rang")}, days)
        self.assertIn("window.mode", str(ctx.exception))

    def test_date_only_format_ignores_pad(self):
        """纯日期格式 + pad_hours：必须忽略 pad，而不是把日期顶到前一天。

        业务日 2026-09-18 发出去的就该是 2026-09-18；减 pad 会变成 09-17，
        数据落进 pt=20260918 就是整表错一天。
        """
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 2,
            "start_param": "BillingDate",
            "end_param": None,
            "format": "%Y-%m-%d",
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])
        self.assertEqual(got, [{"BillingDate": "2026-09-18"}])

    def test_date_only_format_range_ignores_pad(self):
        """range + 纯日期：start 取区间首日、end 取区间末日（闭区间），都不 +1。

        end 输出"次日"会让日期参数接口多查一天，9/18 的分区里混进 9/19 的数据。
        """
        win = {
            "mode": "range",
            "date_tz": "UTC",
            "pad_hours": 3,
            "start_param": "start",
            "end_param": "end",
            "format": "%Y%m%d",
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18), date(2026, 9, 20)])
        self.assertEqual(got, [{"start": "20260918", "end": "20260920"}])

    def test_date_only_end_is_closed_interval(self):
        """per_day + 纯日期 + end_param：end 就是当天，不是次日。"""
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 0,
            "start_param": "startDate",
            "end_param": "endDate",
            "format": "%Y-%m-%d",
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])
        self.assertEqual(got, [{"startDate": "2026-09-18", "endDate": "2026-09-18"}])

    def test_time_format_end_still_next_day(self):
        """带时分的格式语义不变：窗口仍是 [当天00:00, 次日00:00)。"""
        got = dates_mod.window_param_sets({"window": self.win}, [date(2026, 9, 18)])
        self.assertEqual(got[0]["endTime"], "2026-09-19 08:00:00")

    def test_pad_warning_printed_only_once_per_process(self):
        """同一条告警在一条命令里会被多条路径看到（unit_count / fetch_all / probe）：只打一遍。"""
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 2,
            "start_param": "BillingDate",
            "end_param": None,
            "format": "%Y-%m-%d",
        }
        printed = []
        with (
            mock.patch.object(utils, "log", lambda message: printed.append(message)),
            mock.patch.object(utils, "_logged_once", set()),
        ):
            for _ in range(3):
                dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])
        self.assertEqual(len(printed), 1)
        self.assertIn("已忽略 pad_hours=2", printed[0])

    def test_no_window(self):
        self.assertEqual(dates_mod.window_param_sets({}, [date(2026, 9, 18)]), [None])

    def test_unix_format(self):
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 0,
            "start_param": "start",
            "end_param": "end",
            "format": "unix",
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])[0]
        expected = int(datetime(2026, 9, 18, tzinfo=dates_mod.load_zone("UTC")).timestamp())
        self.assertEqual(got["start"], expected)
        self.assertEqual(got["end"], expected + 86400)

    def test_extra_params_and_missing_end(self):
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 0,
            "start_param": "BillingDate",
            "format": "%Y-%m-%d",
            "extra_params": {"BillingCycle": "%Y-%m"},
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])[0]
        self.assertEqual(got, {"BillingDate": "2026-09-18", "BillingCycle": "2026-09"})

    def test_range_extra_params_checked_on_every_day(self):
        """range 只比首尾会漏掉周期性格式：%d 在 20260101~20260201 首尾都是 01、
        中间每天都在变——必须逐日求值，任何一天不同就报配置错。"""
        win = {
            "mode": "range",
            "date_tz": "UTC",
            "api_tz": "+08:00",
            "pad_hours": 0,
            "start_param": "BillingDate",
            "format": "%Y-%m-%d",
            "extra_params": {"Day": "%d"},
        }
        days = [date(2026, 1, 1), date(2026, 1, 15), date(2026, 2, 1)]
        with self.assertRaises(utils.ConfigError) as ctx:
            dates_mod.window_param_sets({"window": win}, days)
        self.assertIn("Day", str(ctx.exception))
        win["extra_params"] = {"Year": "%Y"}  # 全程一致 → 放行
        dates_mod.window_param_sets({"window": win}, days)


# =============================================================================
# 鉴权
# =============================================================================


def reference_aliyun_sign(params: dict, ak_id: str, secret: str, nonce: str, timestamp: str) -> str:
    """独立重写一遍阿里云官方算法（与 aliyun_bill_sync_daily.py 的 call_bss 一致），用于对照。"""
    merged = dict(params)
    merged.update(
        {
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": nonce,
            "Timestamp": timestamp,
            "AccessKeyId": ak_id,
        }
    )
    canonical = "&".join(
        f"{urllib.parse.quote(str(k), safe='~')}={urllib.parse.quote(str(v), safe='~')}"
        for k, v in sorted(merged.items())
    )
    string_to_sign = "GET&" + urllib.parse.quote("/", safe="~") + "&" + urllib.parse.quote(canonical, safe="~")
    digest = hmac.new((secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


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
        self.assertEqual(
            parsers.extract_json_records({"code": 1}, {"records_path": "data.list"}, "t", missing_ok=True), []
        )

    def test_csv_text(self):
        text = "a,b\n1,x\n2,y\n"
        records = parsers.parse_bytes(text.encode("utf-8"), {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])

    def test_skip_rows_bool_is_config_error(self):
        """skip_rows: true（笔误）不能 int(True)=1 静默切掉真表头。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(b"a,b\n1,2\n", {"format": "csv", "skip_rows": True}, "t")
        self.assertIn("skip_rows", str(ctx.exception))

    def test_csv_skip_rows_and_entry_field(self):
        text = "报表\n生成时间,今天\na,b\n1,x\n"
        records = parsers.parse_bytes(
            text.encode("utf-8"), {"format": "csv", "skip_rows": 2, "entry_field": "__file"}, "t"
        )
        # 非 ZIP 来源没有条目名：不能写入空串——CSV 本来就有同名列时会被整列清空
        self.assertEqual(records, [{"a": "1", "b": "x"}])

    def test_csv_entry_field_does_not_clobber_same_named_column(self):
        """CSV 自带 entry_field 同名列（非 ZIP 响应）时保留原值，不被空串覆盖。"""
        text = "__file,a\ncrash_2026.csv,1\n"
        records = parsers.parse_bytes(text.encode("utf-8"), {"format": "csv", "entry_field": "__file"}, "t")
        self.assertEqual(records, [{"__file": "crash_2026.csv", "a": "1"}])

    def test_jsonl(self):
        data = b'{"a": 1}\n\n{"a": 2}\n'
        records = parsers.parse_bytes(data, {"format": "jsonl"}, "t")
        self.assertEqual([r["a"] for r in records], [1, 2])

    def test_zip_total_uncompressed_cap(self):
        """单条目各自合规的"多条目炸弹"要被累计上限拦住（256MB 单条上限可被绕开）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "user,amount\nu2,6\n")
        with mock.patch.object(parsers, "MAX_ZIP_TOTAL_BYTES", 5):
            with self.assertRaises(utils.ConfigError) as ctx:
                parsers.parse_bytes(
                    buffer.getvalue(),
                    {"format": "csv", "unzip": True, "allow_multi_entry": True},
                    "t",
                )
        self.assertIn("累计", str(ctx.exception))

    def test_zip_truncated_entry_is_retryable_runtimeerror(self):
        """压缩流被截断时 zipfile 抛的是 EOFError（不是 BadZipFile）：要按"条目损坏、
        可能重拉就好"的可重试语义包成 RuntimeError，不能裸异常冒出。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
        boom = EOFError("Compressed file ended before the end-of-stream marker was reached")
        with mock.patch.object(zipfile.ZipFile, "read", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(buffer.getvalue(), {"format": "csv", "unzip": True}, "t")
        self.assertIn("读取失败", str(ctx.exception))

    def test_small_multiline_json_body_still_rejected(self):
        """小响应保持原语义：以 { 开头、解不成整包 JSON 的内容在非 jsonl 格式下按错误体拦下。"""
        data = b'{"code": 500}\n{"x": 1}'
        with self.assertRaises(RuntimeError):
            parsers._reject_json_error_body(data, {"format": "csv"}, "t")

    def test_wide_multiline_jsonl_not_whole_decoded(self):
        """单行宽于嗅探前缀的 JSONL 也要判成多行（扫整包字节，不看前 4KB）：
        不做整包解码，也就不再走"整包是一个 JSON 值"的错误体拦截。"""
        wide = '{"a": "' + "x" * 300 + '"}\n'
        data = (wide * 3).rstrip("\n").encode()  # 以 } 收尾：旧实现会按"截断的 JSON"报错
        with mock.patch.object(parsers, "_SNIFF_MAX_BYTES", 100):
            # jsonl 格式不做整包解码、也不做尾部采样（本来就是逐行 JSON）
            self.assertIsNone(parsers._reject_json_error_body(data, {"format": "jsonl"}, "t"))

    def test_big_multiline_response_not_whole_decoded(self):
        """超过嗅探阈值、开头 4KB 就有换行的内容不再整包解码（百 MB 级 JSONL 不该为看首字符
        付整包 str 的内存）：表现为不走"整包是一个 JSON 值"的判定。"""
        data = b'{"code": 500}\n{"x": 1}'
        with mock.patch.object(parsers, "_SNIFF_MAX_BYTES", 4):
            # jsonl 格式不做整包解码、也不做尾部采样（本来就是逐行 JSON）
            self.assertIsNone(parsers._reject_json_error_body(data, {"format": "jsonl"}, "t"))

    def _zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("amount-2026.csv", "user,amount\nu1,5\n")
            archive.writestr("cost-2026.csv", "user,cost\nu1,1\n")
        return buffer.getvalue()

    def test_zip_filter_entries(self):
        records = parsers.parse_bytes(
            self._zip_bytes(),
            {"format": "csv", "unzip": True, "entry_contains": "amount", "entry_field": "__file"},
            "t",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["user"], "u1")
        self.assertEqual(records[0]["__file"], "amount-2026.csv")

    def test_zip_duplicate_entry_names_all_parsed(self):
        """ZIP 允许重名条目：按名字读会读同一条目两遍、另一条静默丢数据——必须按条目分别解析。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("2024-06.csv", "user,amount\nu1,5\n")
            archive.writestr("2024-06.csv", "user,amount\nu2,6\n")
        records = parsers.parse_bytes(
            buffer.getvalue(),
            {"format": "csv", "unzip": True, "allow_multi_entry": True},
            "t",
        )
        self.assertEqual([(r["user"], r["amount"]) for r in records], [("u1", "5"), ("u2", "6")])

    def test_entry_field_collision_rejected(self):
        """entry_field 与源文件自带同名列冲突时会静默覆盖真实列值——必须报错（宁失败勿写错）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "file,amount\nreal-1,5\n")
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(buffer.getvalue(), {"format": "csv", "unzip": True, "entry_field": "file"}, "t")
        self.assertIn("重名", str(ctx.exception))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            # 两行才不会被"整包是单个 JSON = 疑似错误体"的先检拦下（那是另一条防线）
            archive.writestr("a.jsonl", '{"file": "real-1", "amount": 5}\n{"file": "real-2", "amount": 6}\n')
        with self.assertRaises(utils.ConfigError):
            parsers.parse_bytes(buffer.getvalue(), {"format": "jsonl", "unzip": True, "entry_field": "file"}, "t")

    def test_zip_no_match_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True, "entry_contains": "nope"}, "t")

    def test_bad_zip_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b"not a zip", {"format": "csv", "unzip": True}, "t")

    def test_zip_multiple_entries_without_filter_raises(self):
        """没筛条目却有多份文件（表头还可能不同）：不能无脑串成一份记录。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True}, "t")
        self.assertIn("entry_contains", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t")
        self.assertIn("表头不一致", str(ctx.exception))

    def test_zip_multi_entry_same_columns_different_order_merges(self):
        """同构条目（列集合相同、列序不同）要能合并：列序变化不该被当成"表头不一致"。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "amount,user\n6,u2\n")
        records = parsers.parse_bytes(
            buffer.getvalue(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t"
        )
        self.assertEqual(records[0]["amount"], "5")
        self.assertEqual(records[1]["amount"], "6")

    def test_zip_multi_entry_same_headers_merge(self):
        """表头一致的多条目才允许合并。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "user,amount\nu2,6\n")
        records = parsers.parse_bytes(
            buffer.getvalue(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t"
        )
        self.assertEqual(records, [{"user": "u1", "amount": "5"}, {"user": "u2", "amount": "6"}])

    def test_encoding_mismatch_warns_and_strict_raises(self):
        """GBK 字节按默认编码解出乱码：默认告警（行数仍 >0），strict_encoding=true 直接报错。"""
        gbk = "用户名,金额\nu1,5\n".encode("gbk")
        records = parsers.parse_bytes(gbk, {"format": "csv"}, "t")
        self.assertEqual(len(records), 1)  # 默认仍按容错处理
        self.assertIn("�", list(records[0])[0])  # 列名是乱码（所以会有警告日志）
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(gbk, {"format": "csv", "strict_encoding": True}, "t")
        self.assertIn("gbk", str(ctx.exception))
        self.assertEqual(list(parsers.parse_bytes(gbk, {"format": "csv", "encoding": "gbk"}, "t")[0])[0], "用户名")

    def test_parse_payload_dispatch(self):
        records = parsers.parse_payload(b"a\n1\n", {"response_type": "bytes"}, {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1"}])

    def test_csv_skip_until_summary_section(self):
        text = (
            "Settlement Summary\nBatch,Total\nB1,10\n"
            "Settlement Date,Settlement Batch ID,Transaction ID\n"
            "2026-09-18,B1,T1\n2026-09-18,B1,T2\n"
        )
        records = parsers.parse_bytes(
            text.encode("utf-8"), {"format": "csv", "skip_until": "Settlement Date,Settlement Batch ID"}, "t"
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["Transaction ID"], "T1")

    def test_csv_skip_until_not_found_returns_empty(self):
        """空文件/纯空白 = 当天真的没出账：按空结果处理，不算失败。"""
        records = parsers.parse_bytes(b"", {"format": "csv", "skip_until": "明细"}, "t")
        self.assertEqual(records, [])
        self.assertEqual(parsers.parse_bytes(b"\n  \n", {"format": "csv", "skip_until": "明细"}, "t"), [])

    def test_csv_skip_until_not_found_with_content_raises(self):
        """有内容却找不到明细段 = 结构变了/拿到错误页，必须报错（否则静默少拉一天）。"""
        NL = bytes([10])
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(
                b"left,right" + NL + b"1,2" + NL,
                {"format": "csv", "skip_until": "Settlement Date"},
                "settlement",
            )
        self.assertIn("Settlement Date", str(ctx.exception))

    def test_csv_unterminated_quote_raises(self):
        """引号未闭合时 csv 会把后续行吞进同一个字段，行数照常>0——必须报错。"""
        bad = b'a,b,c\n1,"x\n2,3,4\n5,6,7\n'
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(bad, {"format": "csv"}, "报表")
        self.assertIn("CSV 解析失败", str(ctx.exception))

    def test_bom_stripped_in_strict_encoding_mode(self):
        """strict_encoding=true 也要去 BOM，否则列名变成 \\ufeffdate，下游静默取空。"""
        data = "date,amount\n2026-09-18,10\n".encode("utf-8-sig")
        for strict in (False, True):
            records = parsers.parse_bytes(data, {"format": "csv", "encoding": "utf-8", "strict_encoding": strict}, "t")
            self.assertEqual(list(records[0]), ["date", "amount"], f"strict={strict}")

    def test_multi_char_delimiter_rejected(self):
        """delimiter 写 "\\t"（两个字面字符）是常见笔误，跨 Python 版本行为还不一致。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(b"a||b\n1||2\n", {"format": "csv", "delimiter": "||"}, "t")
        self.assertIn("单个字符", str(ctx.exception))

    def test_csv_extra_columns_raise(self):
        """数据行列数多于表头：多出来的字段会被 csv 静默丢掉，宁可报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,b\n1,2,3,4\n", {"format": "csv"}, "t")
        self.assertIn("列数多于表头", str(ctx.exception))

    def test_csv_duplicate_headers_raise(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,a\n1,2\n", {"format": "csv"}, "t")
        self.assertIn("重复列名", str(ctx.exception))

    def test_json_error_body_is_reported(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"respCode":"50217","respMsg":"No api query permissions"}', {"format": "csv"}, "d")
        self.assertIn("50217", str(ctx.exception))


# =============================================================================
# HTTP / 脱敏
# =============================================================================


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", headers=None, content=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}
        # request_once 解析的是 content（bytes）：没显式给就按 JSON 文本编码，
        # 与 requests 的真实行为一致（文本响应也是 bytes 落地）
        self.content = content if content is not None else self.text.encode("utf-8")
        self.closed = False  # 出错分支会 close()：连接要归还连接池

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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

    def test_request_once_bytes_returns_raw_content(self):
        """response_type=bytes：原样返回二进制，不做 JSON 解析（ZIP/CSV 走这条）。"""
        response = FakeResponse(status=200, payload=None)
        response.content = b"\x89PNG binary"
        patcher, _ = self._patch_requests(response)
        with patcher:
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 5, False, True, None)
        self.assertEqual(got, b"\x89PNG binary")

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
        stamp = future.strftime("%a, %d %b %Y %H:%M:%S")  # 故意不带 GMT 后缀
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


# =============================================================================
# Fetcher：分页 / 错误 / 文件模式
# =============================================================================


class TestFetcher(OfflineTestCase):
    def _fetcher(self, job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_page_pagination_with_total_pages(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "param_as_string": True,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
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

    def test_page_stop_when_short_stops_at_short_page(self):
        """接口不返回总数（如 XMP）：本页条数 < page_size 即判末页，多页照常翻满。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            page = args[2].get("page")
            calls.append(page)
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}
            return {"data": {"list": pages.get(page, []), "page": page}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(calls, [1, 2, 3])

    def test_page_stop_when_short_exact_multiple_ends_on_empty_page(self):
        """条数恰好是 page_size 整数倍：末页之后会多一页空页，按短页规则收尾而不是报错。

        默认严格模式没有总数时会把空页视为"无法确认翻完"报错；stop_when_short
        下空页（0 条 < page_size）就是"没有下一页"，应正常收尾。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            page = args[2].get("page")
            calls.append(page)
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}]}
            return {"data": {"list": pages.get(page, [])}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(records), 4)
        self.assertEqual(calls, [1, 2, 3])

    def test_page_stop_when_short_empty_first_page_is_empty_day(self):
        """首屏空页 = 该窗口确实没数据：0 条成功收尾，不发第二页、不触发严格报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[2].get("page"))
            return {"data": {"list": []}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])
        self.assertEqual(calls, [1])

    def test_boolean_total_items_path_does_not_end_pagination_early(self):
        """终点字段指到布尔字段（data.ok: true）时不能当成"总共 1 条"提前收尾。

        float(True) == 1.0 能过数字检查，原来会只拉第一页就停、还显示成功——
        静默少数据正是本工具最不能接受的失败。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.ok",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(params.get("page"))
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}
            return {"data": {"list": pages.get(params.get("page"), []), "ok": True}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        # 关键是它不再停在第 1 页：第 1、2 页照常拉满，空页 + 终点字段读不出数字 ->
        # 默认 strict 视为"无法确认翻完"，报错让调度看见（而不是只拉 2 条还显示成功）
        self.assertEqual(calls[:3], [1, 2, 3])

    def test_boolean_total_pages_does_not_end_pagination_early(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.ok",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(params.get("page"))
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}
            return {"data": {"list": pages.get(params.get("page"), []), "ok": True}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(calls, [1, 2, 3])

    def test_aliyun_nonce_regenerated_on_retry(self):
        """请求重试必须重新签名：复用 SignatureNonce 会被阿里云判 400 SignatureNonceUsed（生产实测）。"""
        job = minimal_job()
        job["request"]["auth"] = {"type": "aliyun_rpc", "access_key_id": "ak", "access_key_secret": "sk"}
        seen = []

        def fake(*args, **kwargs):
            seen.append(args[2].get("SignatureNonce"))
            if len(seen) == 1:
                raise RuntimeError("boom")  # 网络抖动 -> 触发重试
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake), mock.patch.object(http_mod.time, "sleep"):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0] and seen[0] != seen[1])

    def test_page_pagination_with_total_items_and_defensive_check(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 5}},  # page 1
            {"Data": {"Items": {"Item": []}, "TotalCount": 5}},  # 抖动：空页但没拉完 -> 报错
        ]

        def fake(*args, **kwargs):
            return responses.pop(0)

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("TotalCount", str(ctx.exception))

    def test_strict_false_tolerates_stale_total_count(self):
        """TotalCount 不准的接口（翻页期间数据在变）：strict=false 按已拉到的收尾，但打警告。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 9}},
            {"Data": {"Items": {"Item": []}, "TotalCount": 9}},  # 空页但总数没够
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_strict_accepts_string_false(self):
        """JSON 里写 "false"（带引号）也应按 false 处理，不能因为不是布尔就当成开。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "strict": "false",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 9}},
            {"Data": {"Items": {"Item": []}, "TotalCount": 9}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_total_pages_empty_page_midway_raises(self):
        """只配 total_pages_path 时，中间的抖动空页不能提前收尾（原来会静默少数据且显示成功）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 3}},  # 第 2 页空，但总页数说还有第 3 页
            {"data": {"list": [{"id": 3}], "totalPages": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))
        self.assertEqual(len(responses), 1)  # 没有继续翻第 3 页

    def test_total_pages_empty_first_page_raises(self):
        """第 1 页就是空、总页数却说有数据：同样是接口抖动，不能静默当成"今天没数据"。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [{"data": {"list": [], "totalPages": 2}}]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_total_pages_empty_page_on_last_page_is_normal(self):
        """最后一页为空是正常的（接口按"第 N 页 / 共 N 页"给终点），必须照常收尾。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}},
            {"data": {"list": [], "totalPages": 2}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_total_pages_only_on_first_page(self):
        """终点字段只在第一页返回（很常见）：后续页取不到时要用最后一次读到的值判断。

        修复前：末页之后那一页空页取不到 totalPages，被判"无法确认已翻完"，
        默认 strict 下整窗失败——数据明明已经拉全了。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},  # 只有第一页带终点
            {"data": {"list": [{"id": 3}, {"id": 4}]}},
            {"data": {"list": [{"id": 5}]}},
            {"data": {"list": []}},  # 末页之后的空页
        ]
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[2]["current"])
            return responses.pop(0)

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(calls, [1, 2, 3])  # 读到第 3 页（共 3 页）就收尾，没多翻

    def test_total_items_only_on_first_page(self):
        """TotalCount 只在首页返回时同样要用缓存值收尾。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalCount": 3}},
            {"data": {"list": [{"id": 3}]}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])

    def test_nonpositive_total_pages_is_not_an_end_marker(self):
        """totalPages = -1 / 0（"未知"哨兵、字段占位）不能当终点：否则第 1 页就 break，
        数据只拉到一页却校验通过，是静默少数据。这里只有 totalCount 可信，按它翻完。"""
        for sentinel in (-1, 0):
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "current",
                    "size_param": "size",
                    "page_size": 2,
                    "total_pages_path": "data.totalPages",
                    "total_items_path": "data.totalCount",
                    "delay_seconds": 0,
                }
            )
            responses = [
                {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": sentinel, "totalCount": 6}},
                {"data": {"list": [{"id": 3}, {"id": 4}], "totalPages": sentinel, "totalCount": 6}},
                {"data": {"list": [{"id": 5}, {"id": 6}], "totalPages": sentinel, "totalCount": 6}},
            ]
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6], f"totalPages={sentinel} 时不该提前收尾")

    def test_total_items_shrinking_midway_does_not_end_early(self):
        """TotalCount 中途变小（窗口期数据被删、或某页只回本页条数）时终点不能跟着缩水。

        修复前：取"最近一次读到的值"，第 2 页回 2 时 len(records)=4 >= 2 直接收尾，
        少拉一整页却不报错。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalCount": 6}},
            {"data": {"list": [{"id": 3}, {"id": 4}], "totalCount": 2}},  # 回落到"本页条数"
            {"data": {"list": [{"id": 5}, {"id": 6}], "totalCount": 6}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6])

    def test_no_endpoint_field_anywhere_still_raises(self):
        """缓存不等于放宽：全程没拿到过终点字段时，空页仍要报错（不能静默少数据）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "max_pages": 4,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}]}},
            {"data": {"list": []}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("无法确认已翻完", str(ctx.exception))

    def test_cached_total_pages_still_catches_shrinking_page(self):
        """缓存的是终点值，不是"翻完了"这个结论：缓存说还有数据就得继续报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 5}},  # 首页说共 5 页
            {"data": {"list": []}},  # 第 2 页就空了
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))

    def test_total_pages_empty_page_strict_false_stops_with_warning(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])

    def test_fail_if_skipped_for_binary_response(self):
        """文件类响应没有字段可查：fail_if 不能把 bytes 判成业务错误（照抄模板会每天必失败）。"""
        http_mod.check_fail_if(b"PK\x03\x04binary", [{"path": "code", "not_equals": "0"}])
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["request"]["fail_if"] = [{"path": "code", "not_equals": "0", "retry": True}]
        job["parse"] = {"format": "csv"}
        with (
            mock.patch.object(http_mod, "request_once", return_value=b"id\n1\n"),
            mock.patch.object(http_mod, "check_fail_if", wraps=http_mod.check_fail_if) as spy,
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": "1"}])
        self.assertIsInstance(spy.call_args.args[0], bytes)  # 走的是真实请求路径，且命中的是二进制分支

    def test_cursor_can_disable_size_param(self):
        """size_param 显式写 null 时不带页大小参数（有些游标接口不认 size）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "cursor",
                "cursor_path": "data.next",
                "size_param": None,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertNotIn("size", calls[0])

    def test_page_pagination_total_items_complete(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 3}},
            {"Data": {"Items": {"Item": [{"id": 3}]}, "TotalCount": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(responses, [])

    def test_page_size_capped_by_server_still_pulls_all(self):
        """接口把 PageSize 压小（返回条数 < 请求的 page_size）时不能提前判定拉完（静默丢数回归）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 5,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 3}, {"id": 4}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 5}, {"id": 6}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 7}, {"id": 8}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 9}, {"id": 10}]}, "TotalCount": 10}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(responses, [])

    def test_missing_records_path_after_first_page_raises(self):
        """翻到第 2 页 records_path 取不到 → 报错，不能当“拉完了”静默截断。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 2,
                "total_pages_path": "Data.TotalPages",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"TotalPages": 3, "Items": {"Item": [1, 2]}}},
            {"Data": {"TotalPages": 3, "Items": {}}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("records_path", str(ctx.exception))

    def test_records_missing_empty_only_applies_to_first_page(self):
        """records_missing=empty 只认第一页：翻到一半缺字段是接口/配置问题，必须报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        job["request"]["records_missing"] = "empty"
        responses = [
            {"Data": {"Items": {"Item": [1, 2]}, "TotalCount": 6}},
            {"Data": {"Items": {}}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_cursor_pagination(self):
        job = minimal_job(
            pagination={"type": "cursor", "cursor_param": "cursor", "cursor_path": "data.next", "delay_seconds": 0}
        )
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
        # 游标模式同样要带页大小，否则接口用默认值（可能很小）
        self.assertEqual(calls[0]["size"], 100)

    def test_window_retries_infinity_is_config_error(self):
        """window_retries=1e999（json 解析成 inf）时 int(inf) 抛 OverflowError：
        要变成"必须是整数"的配置错，而不是裸 traceback。"""
        job = minimal_job(pagination={"type": "page", "total_pages_path": "d.p", "window_retries": float("inf")})
        fetcher = self._fetcher(job)
        with self.assertRaises(SystemExit) as ctx:
            fetcher.fetch_all([date(2026, 9, 18)], window_retries=float("inf"))
        self.assertIn("window_retries", str(ctx.exception))

    def test_cursor_missing_path_rejected_before_request(self):
        """库调用方绕过 validate_job：type=cursor 而 cursor_path 为空时必须在发请求前报错——
        "取不到游标"会被当成"翻完了"，只拉第一页还报成功（静默截断）。"""
        job = minimal_job(pagination={"type": "cursor", "cursor_param": "cursor", "delay_seconds": 0})
        with mock.patch.object(http_mod, "request_once") as request_mock:
            with self.assertRaises(utils.ConfigError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        request_mock.assert_not_called()

    def test_unknown_pagination_type_rejected(self):
        """拼错/带空格的 type 原来落到 else 被当成 cursor、只拉第一页还报成功——
        必须在发请求前白名单拦截（先 strip 再比，大小写/空白归一）。"""
        for bad in ("pages", "offset", "cursorx", "nonee"):
            job = minimal_job(pagination={"type": bad, "cursor_path": "data.next", "delay_seconds": 0})
            with mock.patch.object(http_mod, "request_once") as request_mock:
                with self.assertRaises(utils.ConfigError):
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            request_mock.assert_not_called()
        job = minimal_job(pagination={"type": " NONE ", "delay_seconds": 0})
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": []}}):
            self.assertEqual(self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None)), [])

    def test_none_pagination_single_call(self):
        job = minimal_job()
        calls = []
        with mock.patch.object(
            http_mod, "request_once", side_effect=lambda *a, **k: (calls.append(a), {"data": {"list": [{"id": 1}]}})[1]
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(records, [{"id": 1}])

    def test_scalar_records_rejected(self):
        """记录数组里混进数字/字符串时直接报错：写进 json 列下游全取不到，却显示成功。"""
        job = minimal_job()
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": [1, "x", {"a": 1}]}}):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("不是对象", str(ctx.exception))

    def test_scalar_records_rejected_on_paginated_path(self):
        """分页路径同样要拦标量元素：原来只有单页路径有这道检查，
        分页接口（很常见的形态）会把数字/字符串原样写进 json 列且显示成功。"""
        for bad_records, page_no in (
            ([{"id": 1}, 42], 1),  # 首页就混进数字
            ([{"id": 1}], 2),  # 首页正常、第 2 页混进字符串
        ):
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "page",
                    "size_param": "size",
                    "page_size": 2,
                    "total_pages_path": "data.totalPages",
                    "delay_seconds": 0,
                }
            )
            responses = [{"data": {"list": [{"id": 1}], "totalPages": 2}}]
            if page_no == 2:
                responses.append({"data": {"list": ["oops"], "totalPages": 2}})
            else:
                responses[0] = {"data": {"list": bad_records, "totalPages": 2}}
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertIn("不是对象", str(ctx.exception))

    def test_scalar_records_rejected_on_cursor_path(self):
        """游标分页走的是同一段取数代码，同样要拦。"""
        job = minimal_job(
            pagination={"type": "cursor", "cursor_param": "cursor", "cursor_path": "data.next", "delay_seconds": 0}
        )
        with mock.patch.object(
            http_mod, "request_once", return_value={"data": {"list": [{"id": 1}, None], "next": None}}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("不是对象", str(ctx.exception))

    def test_zero_data_day_empty_page_with_zero_total_succeeds(self):
        """零数据日：空数组 + 总数 0 = 接口明说"这个窗口没有数据"，成功写 0 行。

        原来只认正数终点，把 0 一律当"字段没填"忽略，于是零数据日被判成"接口抖动"，
        整窗重试几次后失败——正常没数据的一天变成天天报警。
        """
        for marker in ({"data.total": 0}, {"data.totalPages": 0}, {"data.total": "0"}):
            path, value = next(iter(marker.items()))
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "page",
                    "size_param": "size",
                    "page_size": 100,
                    "total_items_path": path,
                    "delay_seconds": 0,
                }
            )
            with mock.patch.object(
                http_mod, "request_once", return_value={"data": {"list": [], "total": value, "totalPages": value}}
            ):
                records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertEqual(records, [], f"{path}=0 时应按零数据日成功返回")

    def test_zero_data_day_only_applies_to_first_page(self):
        """中途翻出空页却报 0 条：与前几页的记录自相矛盾，仍按未翻完报错。

        零数据日的豁免只认"一条都没拉到过"的情况，否则接口某页异常返回 0
        会把前面的天数/页数悄悄截掉。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 0}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))

    def test_negative_total_is_still_not_zero_count(self):
        """total=-1（"未知"哨兵）不是"零数据日"：第一页有空记录时才收尾，否则照旧报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.total",
                "max_pages": 3,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "total": -1}},
            {"data": {"list": [], "total": -1}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_weird_end_markers_do_not_raise_raw_exceptions(self):
        """终点字段是 inf / "inf" / NaN / 10**400 时不能抛裸异常。

        原来 int() 在 try 之外：float("inf") 过得了 _is_number，随后 int(inf) 抛
        OverflowError、int(nan) 抛 ValueError，都被当成"接口抖动"整窗重试白等。
        """
        for value in ("inf", float("inf"), float("nan"), 10**400, "-inf", "abc"):
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "page",
                    "size_param": "size",
                    "page_size": 2,
                    "total_items_path": "data.total",
                    "max_pages": 3,
                    "delay_seconds": 0,
                }
            )
            responses = [
                {"data": {"list": [{"id": 1}, {"id": 2}], "total": value}},
                {"data": {"list": [], "total": value}},
            ]
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertIn("无法确认已翻完", str(ctx.exception))

    def test_nan_in_numeric_config_is_rejected(self):
        """NaN/inf 过得了 float()，却会让所有范围判断失效（NaN 跟谁比都是 False）。"""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod._as_number(value, 0.0, "pagination.delay_seconds")
            self.assertIn("有限数字", str(ctx.exception))

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
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        job["request"]["records_missing"] = "empty"
        with mock.patch.object(http_mod, "request_once", return_value={"Data": {"Items": {}, "TotalCount": 0}}):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])

    def test_max_pages_guard(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 1,
                "max_pages": 3,
                "delay_seconds": 0,
                "total_pages_path": "t",
            }
        )
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": [{"id": 1}], "t": 99}}):
            with self.assertRaises(utils.ConfigError):
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
        self.assertEqual(records[1]["source_account"], "api")  # API 原值优先

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
        self.assertEqual(captured["params"], {})  # URL 参数被清空
        self.assertEqual(captured["headers"]["app"], "demo")  # 参数搬到请求头
        self.assertIn("sign", captured["headers"])  # 签名也在请求头

    def test_build_units_per_day_and_range(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        units = self._fetcher(job).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual([u.label for u in units], ["2026-09-17", "2026-09-18"])
        job_range = minimal_job(
            window={"mode": "range", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        units = self._fetcher(job_range).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual(len(units), 1)

    def test_example_signer_rejects_non_scalar_params(self):
        """示例签名器对非标量参数显式拒绝（str() 与框架实际发送形态不一致会恒定 401）。"""
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("signers_example", root / "signers.example.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for bad_params in ({"ids": [1, 2]}, {"raw": b"x"}):
            with self.assertRaises(ValueError) as err:
                module.onerway_sign({"params": bad_params, "request": {"secret_key": "s"}})
            self.assertIn("非标量", str(err.exception))
        ok = module.onerway_sign({"params": {"a": "1", "b": 2}, "request": {"secret_key": "s"}})
        self.assertIn("sign", ok["params"])

    def test_window_retries_fractional_or_negative_rejected(self):
        """window_retries 先校验再取整：2.5/-1 不能被 int() 截断/被 max(1,…) 吞掉。"""
        for bad in (2.5, -1, float("inf")):
            with self.assertRaises(SystemExit) as ctx:
                self._fetcher(minimal_job()).fetch_all([date(2026, 9, 18)], window_retries=bad)
            self.assertIn("window_retries", str(ctx.exception))

    def test_params_in_typo_is_config_error(self):
        """params_in 拼错（header）不能静默退回 query（签名/密钥参数进 URL 明文）。"""
        with self.assertRaises(SystemExit) as ctx:
            self._fetcher(minimal_job(request={"params_in": "header"}))
        self.assertIn("params_in", str(ctx.exception))

    def test_retry_times_fractional_or_negative_rejected(self):
        """retry_times 先校验再取整：-0.5/2.5 不能被 int() 截断后绕过校验。"""
        for bad in (-0.5, 2.5, -1):
            with self.assertRaises(SystemExit) as ctx:
                self._fetcher(minimal_job(request={"retry_times": bad}))
            self.assertIn("retry_times", str(ctx.exception))

    def test_probe_size_retry_unconfirmed_end_is_ok(self):
        """page_size=1 被拒后按配置页大小重试、重试仍"无法确证翻完"：按能连通处理。"""
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100})
        fetcher = self._fetcher(job)
        calls = {"n": 0}

        def fake_fetch_unit(unit, **kwargs):
            calls["n"] += 1
            if kwargs.get("page_size_override") is not None:
                raise utils.FatalApiError("HTTP 400: invalid page size")
            raise fetch_mod.UnconfirmedEndError("d 第 1 页返回为空，但无法确认已翻完（接口没有给总页数/总条数）")

        with mock.patch.object(fetcher, "fetch_unit", side_effect=fake_fetch_unit):
            label, count = fetcher.probe([date(2026, 9, 18)])
        self.assertEqual(count, 0)
        self.assertEqual(calls["n"], 2)

    def test_probe_empty_units_is_config_error(self):
        """体检时算出 0 个请求单元：按配置错报（ConfigError），不是裸 RuntimeError。"""
        fetcher = self._fetcher(minimal_job())
        with mock.patch.object(fetcher, "build_units", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                fetcher.probe([date(2026, 9, 18)])
        self.assertIn("请求单元", str(ctx.exception))

    def test_probe_limits_page_size(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 100,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 1, "data": {"list": [{"id": 1}, {"id": 2}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 2)
        self.assertEqual(calls[0]["n"], 1)

    def test_probe_stops_after_first_page_even_with_more_pages_left(self):
        """体检只发一次请求：接口说还有 99 页也不能继续翻（曾因循环里 continue 跳过返回而报错）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 100,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 99, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 1)  # 只请求一次，不跟着 total_pages 翻

    def test_probe_treats_unconfirmed_end_as_connected(self):
        """接口不给总数、首页又为空：体检按"能连通、当日无数据"处理（0 条），
        不能因为 strict 的"无法确认已翻完"把好源判成体检失败。"""
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100})
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: {"data": {"list": []}}):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 0)

    def test_fetch_all_renews_stop_each_round(self):
        """fetch_all 退出（成功/失败/中断）时置当轮停止信号；下一轮换新对象而不是复位——
        已 set 的旧对象保持 set，上一轮遗留的 worker 永远看到"已停止"、不会被复活。"""
        job = minimal_job()
        fetcher = self._fetcher(job)
        with mock.patch.object(fetcher, "fetch_unit", return_value=[]):
            fetcher.fetch_all([date(2026, 9, 18)])
        first_stop = fetcher._stop
        self.assertTrue(first_stop.is_set())
        with mock.patch.object(fetcher, "fetch_unit", return_value=[]) as fu:
            fetcher.fetch_all([date(2026, 9, 18)])
        self.assertEqual(fu.call_count, 1)
        self.assertTrue(first_stop.is_set())  # 旧对象不复位
        self.assertIsNot(fetcher._stop, first_stop)  # 新一轮换新对象

    def test_sleep_is_interruptible(self):
        """停止信号已置位时 _sleep 立即返回 True（不真等 10s/20s 的退避）。"""
        fetcher = self._fetcher(minimal_job())
        fetcher._stop.set()
        started = time.monotonic()
        self.assertTrue(fetcher._sleep(10))
        self.assertLess(time.monotonic() - started, 1.0)


class TestFetchAll(OfflineTestCase):
    def test_window_retry_then_success(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        fetcher = fetch_mod.Fetcher(job, Path("."))
        attempts = {"n": 0}
        collected: list = []

        def flaky(unit, **_kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("boom")
            return [{"id": attempts["n"]}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=1, on_records=collected.extend)
        self.assertEqual(failures, [])
        self.assertEqual(len(collected), 1)
        self.assertEqual(stats[0][1], 1)

    def test_failures_collected_and_order_kept(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 17), date(2026, 9, 18)]
        collected: list = []

        def flaky(unit, **_kw):
            if unit.day.day == 17:
                raise RuntimeError("bad day")
            return [{"id": 2}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all(days, window_retries=0, on_records=collected.extend)
        self.assertEqual(len(failures), 1)
        self.assertEqual([r["id"] for r in collected], [2])
        self.assertEqual(stats, [("2026-09-18", 1)])

    def test_fatal_error_not_retried_across_units(self):
        """4xx 是参数/权限问题：重试多少次都一样，立刻中止（别把同一错误重复几十遍）。

        修复前：每个单元各重试 3 次，30 天窗口 = 90 个必失败请求 + 十几分钟空等。
        """
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
        calls = {"n": 0}

        def unauthorized(unit, **_kw):
            calls["n"] += 1
            raise utils.FatalApiError("HTTP 401：Unauthorized")

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=unauthorized),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            with self.assertRaises(utils.FatalApiError):
                fetcher.fetch_all(days, window_retries=2)
        self.assertEqual(calls["n"], 1)  # 只试了第一个单元就中止

    def test_config_error_not_retried(self):
        """配置写错（如 skip_rows 为负）同样不该整窗重试。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        calls = {"n": 0}

        def bad_config(unit, **_kw):
            calls["n"] += 1
            raise utils.ConfigError("parse.skip_rows 不能为负：-1")

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=bad_config),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            with self.assertRaises(utils.ConfigError):
                fetcher.fetch_all([date(2026, 9, 16), date(2026, 9, 17)], window_retries=2)
        self.assertEqual(calls["n"], 1)

    def test_network_error_still_retried(self):
        """回归保护：网络抖动仍要整窗重试（只把不可重试的错误挑出去）。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        calls = {"n": 0}

        def flaky(unit, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return [{"id": 1}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=2)
        self.assertEqual(failures, [])
        self.assertEqual(calls["n"], 2)
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
            self.assertFalse(path.exists())  # 默认用完即删

    def test_iter_batches_splits_by_rows_and_bytes(self):
        """写入分批在读取侧完成：大记录不能攒成"1000 行一批"把内存吃满。"""
        spool = spool_mod.SpoolWriter()
        try:
            spool.write_records([{"i": i, "pad": "x" * 100} for i in range(6)])
            by_rows = list(spool.iter_batches(batch_size=2, max_bytes=10**6))
            self.assertEqual([len(b) for b in by_rows], [2, 2, 2])
            # 字节上限更小时按字节切：每条约 120 字节
            by_bytes = list(spool.iter_batches(batch_size=1000, max_bytes=250))
            self.assertEqual([len(b) for b in by_bytes], [2, 2, 2])
            # 行内容不变、也没有丢行
            self.assertEqual([json.loads(r)["i"] for b in by_bytes for r in b], [0, 1, 2, 3, 4, 5])
        finally:
            spool.close()

    def test_nan_in_record_fails_instead_of_writing_invalid_json(self):
        """NaN/Infinity 落盘后不是合法 JSON（MaxCompute get_json_object 取不到值，静默变 NULL）。"""
        spool = spool_mod.SpoolWriter()
        try:
            with self.assertRaises(RuntimeError):
                spool.write_records([{"fee": float("nan")}])
            with self.assertRaises(RuntimeError):
                spool.write_records([{"fee": float("inf")}])
            self.assertEqual(spool.count, 0)  # 失败的那条不能落盘
            spool.write_records([{"fee": 1.5}])  # 正常数据不受影响
            self.assertEqual(spool.count, 1)
        finally:
            spool.close()

    def test_keep_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1}])
            spool.close(keep=True)
            self.assertTrue(path.exists())

    def test_iter_rows_after_close_raises_readable_error(self):
        """关闭后读回：原来抛没有上下文的 FileNotFoundError（keep=False 已删文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1}])
            spool.close()
            with self.assertRaises(RuntimeError) as ctx:
                list(spool.iter_rows())
            self.assertIn("已关闭", str(ctx.exception))


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

    def test_secret_answers_not_stripped(self):
        """密钥类输入只去尾部换行：首尾空白可能是凭据的一部分，静默改写会让验签失败难以排查。"""
        self.assertEqual(init_wizard._ask_secret(lambda prompt="": " myPass \n", "密码"), " myPass ")
        self.assertEqual(init_wizard._ask_secret(lambda prompt="": "tok ", "Token"), "tok ")

    def test_generate_simple_token_page_job(self):
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "请求方法": "",  # 直接回车 = GET
            "Token 的值": "tok123",  # 鉴权选择 Bearer
            "记录列表在返回": "data.list",
            "总页数字段路径": "data.totalPages",
            "总条数字段路径": "",
            "每次回拉最近几天": "15",
            "项目名": "",  # my_project
            "表名": "",  # ods_demo_api_json_di
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
            "endpoint": "",  # 默认 us-west-1
        }
        # 选择题顺序：鉴权=1（Bearer）、翻页=1（页码）、窗口=1（按天）、返回=0（JSON）
        ask = self._answers(mapping, ["1", "1", "1", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(
                out_path=str(Path(tmp) / "demo_api.json"),
                ask=ask,
                ask_secret=ask,
                echo=lambda *a: None,
                workdir=Path(tmp),
            )
            self.assertEqual(code, 0)
            job = json.loads((Path(tmp) / "demo_api.json").read_text(encoding="utf-8"))
        self.assertEqual(job["job"], "demo_api")
        self.assertEqual(job["request"]["base_url"], "https://api.example.com")
        self.assertEqual(job["request"]["path"], "/v1/items")
        self.assertEqual(job["request"]["auth"], {"type": "bearer", "token": "tok123"})
        self.assertEqual(job["request"]["records_path"], "data.list")
        self.assertEqual(job["pagination"]["total_pages_path"], "data.totalPages")
        self.assertEqual(job["window"]["mode"], "per_day")
        self.assertEqual(job["target"]["table"], "ods_demo_api_json_di")
        self.assertEqual(job["maxcompute"]["access_key_id"], "AKID")
        normalized = config_mod.normalize_job(job)
        config_mod.validate_job(normalized)  # 生成的配置（补默认值后）必须能通过校验

    def test_interrupt_during_chmod_after_overwrite_reports_generated(self):
        """覆盖已存在的作业文件：replace 完成后收尾 chmod 阶段被 Ctrl+C，不能报
        "未生成任何文件"——旧配置其实已被整体替换。"""
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "请求方法": "",
            "Token 的值": "tok123",
            "记录列表在返回": "data.list",
            "总页数字段路径": "data.totalPages",
            "总条数字段路径": "",
            "每次回拉最近几天": "15",
            "项目名": "",
            "表名": "",
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
            "endpoint": "",
        }
        ask = self._answers(mapping, ["1", "1", "1", "0"])
        echoes: list = []
        fake_os = mock.Mock(wraps=init_wizard.os)
        fake_os.name = "posix"
        # 第一次 chmod 是"写入前收紧旧文件权限"（放行），第二次是写后的收尾（中断）
        fake_os.chmod.side_effect = [None, KeyboardInterrupt]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo_api.json"
            out.write_text('{"job": "old"}', encoding="utf-8")
            with mock.patch.object(init_wizard, "os", fake_os):
                code = init_wizard.run_init(
                    out_path=str(out),
                    ask=ask,
                    ask_secret=ask,
                    echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
                    workdir=Path(tmp),
                )
            self.assertEqual(code, 0)
            self.assertTrue(any("已生成" in line for line in echoes), echoes[-5:])
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["job"], "demo_api")

    def test_interrupt_at_write_start_with_existing_file_reports_cancelled(self):
        """旧文件存在、写盘刚开始（replace 未发生）就中断：必须报"已取消、未生成"并保留
        旧文件——不能把"旧文件还在"误判成"本次已生成"。"""
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "请求方法": "",
            "Token 的值": "tok123",
            "记录列表在返回": "data.list",
            "总页数字段路径": "data.totalPages",
            "总条数字段路径": "",
            "每次回拉最近几天": "15",
            "项目名": "",
            "表名": "",
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
            "endpoint": "",
        }
        ask = self._answers(mapping, ["1", "1", "1", "0"])
        echoes: list = []
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo_api.json"
            out.write_text('{"job": "old"}', encoding="utf-8")
            with mock.patch.object(init_wizard, "_atomic_write_job", side_effect=KeyboardInterrupt):
                code = init_wizard.run_init(
                    out_path=str(out),
                    ask=ask,
                    ask_secret=ask,
                    echo=lambda *a: echoes.append(" ".join(str(x) for x in a)),
                    workdir=Path(tmp),
                )
            self.assertEqual(code, 1)
            self.assertTrue(any("未生成" in line for line in echoes), echoes[-5:])
            self.assertEqual(out.read_text(encoding="utf-8"), '{"job": "old"}')

    def test_keyboard_interrupt_cleans_tmp_file(self):
        """Ctrl+C 落在写盘途中（fsync）：含明文密钥的临时文件必须清掉，
        向导的"已取消，未生成任何文件"才属实（清理原来只接 Exception，中断会留下 .tmp）。"""
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "请求方法": "",
            "Token 的值": "tok123",
            "记录列表在返回": "data.list",
            "总页数字段路径": "data.totalPages",
            "总条数字段路径": "",
            "每次回拉最近几天": "15",
            "项目名": "",
            "表名": "",
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
            "endpoint": "",
        }
        ask = self._answers(mapping, ["1", "1", "1", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "demo_api.json"
            with mock.patch.object(init_wizard.os, "fsync", side_effect=KeyboardInterrupt):
                code = init_wizard.run_init(
                    out_path=str(out), ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
                )
            self.assertIn(code, (1, 130))
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(tmp).glob(".demo_api.json.*.tmp")), [])

    def test_cancel_on_eof(self):
        def ask(_prompt=""):
            raise EOFError

        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(
                out_path=str(Path(tmp) / "x.json"), ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
            )
        self.assertEqual(code, 1)
        self.assertFalse((Path(tmp) / "x.json").exists())

    def _run(self, mapping, choices, name="w.json"):
        """跑一次向导，返回 (退出码, 生成的文件路径, 输出文本)。"""
        out = []
        ask = self._answers(mapping, choices)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            code = init_wizard.run_init(
                out_path=str(path),
                ask=ask,
                ask_secret=ask,
                echo=lambda *a: out.append(" ".join(str(x) for x in a)),
                workdir=Path(tmp),
            )
            content = path.read_text(encoding="utf-8") if path.exists() else None
        return code, content, "\n".join(out)

    def test_split_url_invalid_ipv6_returns_none(self):
        """方括号不配对的 IPv6 地址（从日志截断粘来）要按"格式不对"返回 None、
        走重试分支，而不是 urlsplit 的 ValueError 裸崩向导。"""
        self.assertIsNone(init_wizard._split_url("https://[10.0.0.1:8080/api"))

    def test_invalid_url_is_retried(self):
        """地址格式不对要重问，不能拿着坏地址生成配置。"""
        mapping = {"API 完整地址": "api.example.com", "AccessKeyId": "A", "AccessKeySecret": "S"}
        code, content, output = self._run(mapping, ["0", "0", "0"])
        self.assertEqual(code, 1)
        self.assertIsNone(content)
        self.assertIn("地址格式不对", output)

    def test_url_with_query_is_moved_into_path_with_hint(self):
        mapping = {
            "API 完整地址": "https://api.example.com/v1/items?app=demo",
            "记录列表在返回": "data.list",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, output = self._run(mapping, ["0", "0", "0"])
        self.assertEqual(code, 0)
        job = json.loads(content)
        self.assertEqual(job["request"]["base_url"], "https://api.example.com")
        self.assertIn("app=demo", job["request"]["path"])
        self.assertIn("建议稍后手工挪到 request.params", output)

    def test_sha256_auth_branch(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "商户密钥": "sk1",
            "签名字段名": "",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["6", "0", "0"])
        self.assertEqual(code, 0)
        auth = json.loads(content)["request"]["auth"]
        self.assertEqual(auth["type"], "sha256_concat")
        self.assertEqual(auth["secret_key"], "sk1")
        self.assertEqual(auth["sign_field"], "sign")  # 回车取默认

    def test_custom_auth_points_at_signers_py(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "signers.py 的函数名": "my_sign",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["7", "0", "0"])
        self.assertEqual(code, 0)
        auth = json.loads(content)["request"]["auth"]
        self.assertEqual(auth, {"type": "custom", "module": "signers.py", "func": "my_sign"})

    def test_query_and_basic_auth_branches(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "URL 参数名": "api_key",
            "Token 的值": "t1",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["3", "0", "0"])
        self.assertEqual(json.loads(content)["request"]["auth"], {"type": "query", "params": {"api_key": "t1"}})

        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "用户名": "u",
            "密码": "p",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["4", "0", "0"])
        self.assertEqual(json.loads(content)["request"]["auth"], {"type": "basic", "username": "u", "password": "p"})

    def test_page_pagination_defaults_endpoint_path_when_both_blank(self):
        """总页数/总条数两个都留空：给个默认值继续，而不是生成一份翻不了页的配置。"""
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "总页数字段路径": "",
            "总条数字段路径": "",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, output = self._run(mapping, ["0", "1", "1", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(content)["pagination"]["total_pages_path"], "data.totalPages")
        self.assertIn("两个都留空了", output)

    def test_page_pagination_total_items_only(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "总页数字段路径": "",
            "总条数字段路径": "data.totalCount",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["0", "1", "1", "0"])
        pagination = json.loads(content)["pagination"]
        self.assertEqual(pagination["type"], "page")
        self.assertEqual(pagination["total_items_path"], "data.totalCount")

    def test_cursor_pagination_requires_path(self):
        """游标路径连续留空 → 取消，而不是生成一份只会拉第一页的配置。"""
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        code, content, output = self._run(mapping, ["0", "2", "1", "0"])
        self.assertEqual(code, 1)
        self.assertIsNone(content)
        self.assertIn("游标路径连续三次为空", output)

    @unittest.skipUnless(os.name == "posix", "符号链接/chmod 语义按 POSIX 断言")
    def test_atomic_write_job_does_not_chmod_symlink_target(self):
        """目标是符号链接时跳过 chmod：os.chmod 会跟随链接改到真实文件权限，而 os.replace
        只替换链接本身——共享目录里的同名链接能让向导改任意文件的权限（且白改）。"""
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.txt"
            real.write_text("keep", encoding="utf-8")
            os.chmod(real, 0o644)
            link = Path(tmp) / "job.json"
            try:
                os.symlink(real, link)
            except OSError:
                self.skipTest("平台/权限不支持创建符号链接")
            init_wizard._atomic_write_job(link, {"job": "demo"})
            self.assertEqual(os.stat(real).st_mode & 0o777, 0o644)  # 真实文件权限未被动过
            self.assertFalse(link.is_symlink())  # 链接本身被替换成作业文件
            self.assertEqual(json.loads(link.read_text(encoding="utf-8")), {"job": "demo"})

    def test_invalid_choice_numbers_fall_back_with_hint(self):
        """编号填了非法值：提示后按默认走，不静默也不崩。"""
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        code, content, output = self._run(mapping, ["9", "9", "9", "0", "0", "0"])
        self.assertEqual(code, 0)
        self.assertIn("不是有效选项", output)

    def test_invalid_auth_choice_does_not_generate_custom_signers(self):
        """鉴权填非法编号不能落进 custom 分支（生成的配置跑起来必报"找不到 signers.py"）。"""
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "Token 的值": "tok",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        # 选择题顺序：鉴权=8（非法）、翻页=0、窗口=0、返回=0
        code, content, output = self._run(mapping, ["8", "0", "0", "0"])
        self.assertEqual(code, 0)
        auth = json.loads(content)["request"]["auth"]
        self.assertEqual(auth["type"], "bearer")
        self.assertIn("不是有效选项", output)

    def test_range_window_mode(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "每次覆盖最近几天": "7",
            "开始时间参数名": "from",
            "结束时间参数名": "to",
            "时间格式": "unix",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["0", "0", "2"])
        window = json.loads(content)["window"]
        self.assertEqual(window["mode"], "range")
        self.assertEqual(window["days"], 7)
        self.assertEqual(window["start_param"], "from")
        self.assertEqual(window["format"], "unix")

    def test_end_param_dash_means_no_end_param(self):
        """结束时间参数填 -：写成 end_param=null，而不是让用户手改文件。"""
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "每次回拉最近几天": "3",
            "结束时间参数名": "-",
            "时间格式": "unix",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["0", "0", "1"])
        self.assertIsNone(json.loads(content)["window"]["end_param"])

    def test_non_numeric_days_falls_back_to_default(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "每次回拉最近几天": "十五天",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, output = self._run(mapping, ["0", "0", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(content)["window"]["days"], 15)
        self.assertIn("不是整数", output)

    def test_file_response_with_zip(self):
        mapping = {
            "API 完整地址": "https://a.example.com/export",
            "文件格式": "csv",
            "是 ZIP 压缩包吗": "y",
            "只取文件名包含什么字的条目": "amount",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["0", "0", "0", "1"])
        job = json.loads(content)
        self.assertEqual(job["request"]["response_type"], "bytes")
        self.assertTrue(job["parse"]["unzip"])
        self.assertEqual(job["parse"]["entry_contains"], "amount")
        self.assertEqual(job["parse"]["entry_field"], "__file")

    def test_generated_job_passes_validation_in_every_branch(self):
        """生成的配置必须能过校验：向导的产物不能比手写的更容易被拒。"""
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "Token 的值": "t",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        for choices in (["1", "0", "0", "0"], ["1", "1", "1", "0"], ["1", "2", "2", "0"]):
            with self.subTest(choices=choices):
                if choices[1] == "2":
                    mapping["下一页游标在返回里的路径"] = "data.next"
                code, content, _ = self._run(mapping, choices)
                self.assertEqual(code, 0)
                config_mod.validate_job(config_mod.normalize_job(json.loads(content)))

    def test_unrelated_runtime_error_is_not_swallowed(self):
        """与 stdin 无关的 RuntimeError（向导自身的 bug）必须原样抛出，别伪装成"已取消"。"""

        def ask(_prompt=""):
            raise RuntimeError("向导内部 bug")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                init_wizard.run_init(
                    out_path=str(Path(tmp) / "w.json"), ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
                )
        self.assertIn("向导内部 bug", str(ctx.exception))

    def test_missing_stdin_reports_how_to_run(self):
        """没有标准输入（CI / 重定向）时 input() 抛的是 RuntimeError：要给人话 + 退出码 1。"""

        def ask(_prompt=""):
            raise RuntimeError("lost sys.stdin")

        out = []
        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(
                out_path=str(Path(tmp) / "w.json"),
                ask=ask,
                ask_secret=ask,
                echo=lambda *a: out.append(str(a[0])),
                workdir=Path(tmp),
            )
        self.assertEqual(code, 1)
        self.assertIn("需要在终端里交互运行", "\n".join(out))

    def test_generated_file_is_chmod_600_on_posix(self):
        """生成的配置里有明文密钥：类 Unix 上收紧到 600（Windows 忽略权限位），
        且临时文件走 tempfile.mkstemp（随机名 + O_EXCL、默认 0600）后原子替换；
        原来可预测的 `名字.pid.tmp + O_TRUNC` 会被同目录下的同名符号链接跟随。"""
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        ask = self._answers(mapping, ["0", "0", "0", "0"])
        # wraps=真 os 模块：os.fdopen/os.replace 走真实实现（文件真的写出来），只记录调用；
        # name 强制成 posix 以覆盖 chmod 分支（Windows 上生产代码会跳过它）
        fake_os = mock.Mock(wraps=init_wizard.os)
        fake_os.name = "posix"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            real_mkstemp = tempfile.mkstemp
            with (
                mock.patch.object(init_wizard, "os", fake_os),
                mock.patch.object(init_wizard.tempfile, "mkstemp", wraps=real_mkstemp) as mkstemp,
            ):
                code = init_wizard.run_init(
                    out_path=str(path), ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
                )
            self.assertEqual(code, 0)
            self.assertTrue(path.is_file())
            # 临时文件必须建在目标目录旁（保证 os.replace 同盘原子），且不留残渣
            self.assertEqual(Path(mkstemp.call_args.kwargs["dir"]), Path(tmp))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])
        self.assertTrue(fake_os.chmod.called)
        fake_os.chmod.assert_called_with(path, 0o600)

    def test_relative_out_path_is_resolved_under_workdir(self):
        """--init-out 给相对路径时按工作目录解析（否则会落到进程的当前目录）。"""
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        ask = self._answers(mapping, ["0", "0", "0", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            code = init_wizard.run_init(
                out_path="out/w.json", ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
            )
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "out" / "w.json").is_file())

    def test_custom_header_and_aliyun_rpc_auth_branches(self):
        mapping = {
            "API 完整地址": "https://a.example.com/x",
            "请求头名字": "X-Api-Key",
            "Token/Key 的值": "k1",
            "AccessKeyId": "A",
            "AccessKeySecret": "S",
        }
        code, content, _ = self._run(mapping, ["2", "0", "0", "0"])
        self.assertEqual(
            json.loads(content)["request"]["auth"], {"type": "token", "header": "X-Api-Key", "value": "k1"}
        )

        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "AKID2", "AccessKeySecret": "SECRET2"}
        code, content, _ = self._run(mapping, ["5", "0", "0", "0"])
        self.assertEqual(
            json.loads(content)["request"]["auth"],
            {"type": "aliyun_rpc", "access_key_id": "AKID2", "access_key_secret": "SECRET2"},
        )

    def test_empty_url_is_asked_again_with_hint(self):
        """地址回车留空：提示后重问，不能拿空地址继续（生成的配置必然跑不起来）。"""
        out = []
        urls = ["", "https://api.example.com/v1/x"]
        choices = ["0", "0", "0", "0"]

        def ask(prompt=""):
            if "请选择编号" in prompt:
                return choices.pop(0)
            if "API 完整地址" in prompt:
                return urls.pop(0)
            if "AccessKeyId" in prompt:
                return "A"
            if "AccessKeySecret" in prompt:
                return "S"
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            code = init_wizard.run_init(
                out_path=str(path),
                ask=ask,
                ask_secret=ask,
                echo=lambda *a: out.append(" ".join(str(x) for x in a)),
                workdir=Path(tmp),
            )
            job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        self.assertEqual(code, 0)
        self.assertEqual(job["request"]["base_url"], "https://api.example.com")
        self.assertEqual(job["request"]["auth"], {"type": "none"})
        self.assertIn("地址不能为空", "\n".join(out))

    def test_unsafe_job_name_is_sanitized_for_file_name(self):
        """作业名里的空格/路径分隔符会写到 jobs 目录之外：文件名要用替换后的安全名。"""
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        code, content, output = self._run({**mapping, "作业名": "my api"}, ["0", "0", "0", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(content)["job"], "my_api")
        self.assertIn("已替换为下划线", output)

    def test_secret_prompts_go_through_unhidden_entry(self):
        """token / 密码 / AccessKeySecret 等密钥类问答走 ask_secret（默认 getpass，不回显）；
        普通问答仍走 ask。原来一律走 input()，密钥明文回显在终端。"""
        plain, secret = [], []
        choices = iter(["1", "0", "0", "0"])  # 鉴权=Bearer、翻页=无、窗口=无、返回=JSON

        def ask(prompt=""):
            plain.append(prompt)
            if "请选择编号" in prompt:
                return next(choices, "")
            if "API 完整地址" in prompt:
                return "https://api.example.com/v1/items"
            if "AccessKeyId" in prompt:
                return "AKID"
            return ""

        def ask_secret(prompt=""):
            secret.append(prompt)
            return "hidden-value"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            code = init_wizard.run_init(
                out_path=str(path), ask=ask, ask_secret=ask_secret, echo=lambda *a: None, workdir=Path(tmp)
            )
            job = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        # 密钥类问题确实走了不回显入口
        self.assertTrue(any("Token 的值" in p for p in secret))
        self.assertTrue(any("AccessKeySecret" in p for p in secret))
        # 普通问题没有落到密钥入口
        self.assertFalse(any("AccessKeySecret" in p for p in plain))
        self.assertFalse(any("Token 的值" in p for p in plain))
        self.assertEqual(job["request"]["auth"]["token"], "hidden-value")
        self.assertEqual(job["maxcompute"]["access_key_secret"], "hidden-value")
        self.assertEqual(job["maxcompute"]["access_key_id"], "AKID")

    def test_default_secret_entry_uses_getpass(self):
        """不注入 ask_secret 时，默认的密钥入口是 getpass（不回显）。"""
        with mock.patch.object(init_wizard.getpass, "getpass", return_value="hidden") as gp:
            self.assertEqual(init_wizard._default_ask_secret("密码："), "hidden")
        gp.assert_called_once_with("密码：")


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
        self.writer_kwargs = []
        self.fail_next_write = False
        self._writer = FakeWriter()
        self.existing_partitions = set()

    def exist_partition(self, spec):
        return spec in self.existing_partitions

    def delete_partition(self, spec, if_exists=False):
        self.deleted.append((spec, if_exists))

    def create_partition(self, spec, if_not_exists=False):
        self.created.append((spec, if_not_exists))

    def open_writer(self, partition=None, **kwargs):
        self.writers.append(partition)
        self.writer_kwargs.append(kwargs)
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("tunnel boom")
        return self._writer


class FakeWriter:
    def __init__(self):
        self.records = []
        self.closed = False
        self.max_row_counts = []

    def write(self, records, max_row_count=None):
        self.records.extend(records)
        self.max_row_counts.append(max_row_count)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True


class DdlOdps:
    """假 ODPS 客户端：分区增删的 DDL 直接落到 FakeTable 上（生产已改走带超时的 DDL）。

    - run_sql 收到 `alter table ... drop/add ... partition (pt='X')` 时，按 pyodps 表 API 的
      语义更新 table.deleted / table.created（既有断言"确实先删过"继续有意义）；
    - 其余 SQL 返回"立即成功"的假实例（这些用例不校验 count/verify）。
    """

    def __init__(self, table=None):
        self.table = table
        self.sqls: list[str] = []

    def run_sql(self, sql):
        self.sqls.append(sql)
        match = re.search(
            r"(drop\s+if\s+exists|add\s+if\s+not\s+exists)\s+partition\s*\(\s*pt\s*=\s*'([^']*)'\s*\)",
            sql,
            re.I,
        )
        if match is not None and self.table is not None:
            spec = f"pt={match.group(2)}"
            if match.group(1).lower().startswith("drop"):
                self.table.deleted.append((spec, True))
            else:
                self.table.created.append((spec, True))
        return OkInstance()


class OkInstance:
    """立即成功、未终止的假 SQL 实例（run_sql_with_timeout 直接取用）。"""

    def is_successful(self):
        return True

    def is_terminated(self):
        return False


class TestNormalizeJob(OfflineTestCase):
    """配置瘦身：缺省字段自动补齐 / 推断（让新源配置尽量短）。"""

    def test_pagination_infers_page_with_defaults(self):
        job = minimal_job(pagination={"total_pages_path": "data.totalPages"})
        got = config_mod.normalize_job(job)
        self.assertEqual(got["pagination"]["type"], "page")
        self.assertEqual(got["pagination"]["page_param"], "page")
        self.assertEqual(got["pagination"]["size_param"], "size")
        self.assertEqual(got["pagination"]["page_size"], 100)
        config_mod.validate_job(got)

    def test_pagination_infers_cursor(self):
        got = config_mod.normalize_job(minimal_job(pagination={"cursor_path": "data.next"}))
        self.assertEqual(got["pagination"]["type"], "cursor")
        self.assertEqual(got["pagination"]["cursor_param"], "cursor")
        config_mod.validate_job(got)

    def test_pagination_delay_only_becomes_none(self):
        got = config_mod.normalize_job(minimal_job(pagination={"delay_seconds": 1}))
        self.assertEqual(got["pagination"]["type"], "none")

    def test_user_values_win(self):
        job = minimal_job(
            pagination={
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 300,
                "total_items_path": "Data.TotalCount",
            }
        )
        got = config_mod.normalize_job(job)
        self.assertEqual(got["pagination"]["page_param"], "PageNum")
        self.assertEqual(got["pagination"]["page_size"], 300)

    def test_window_defaults(self):
        got = config_mod.normalize_job(minimal_job(window={"days": 7}))
        self.assertEqual(got["window"]["start_param"], "startTime")
        self.assertEqual(got["window"]["end_param"], "endTime")

    def test_window_explicit_null_end_stays_null(self):
        got = config_mod.normalize_job(minimal_job(window={"days": 7, "start_param": "BillingDate", "end_param": None}))
        self.assertIsNone(got["window"]["end_param"])

    def test_window_custom_start_param_does_not_gain_end_param(self):
        """只自定义 start_param（v2.0.0 老写法）时不再补 end_param，请求参数保持原样。"""
        got = config_mod.normalize_job(minimal_job(window={"days": 21, "start_param": "date", "format": "%Y%m%d"}))
        self.assertEqual(got["window"]["start_param"], "date")
        self.assertNotIn("end_param", got["window"])
        self.assertEqual(dates_mod.window_param_sets(got, [date(2026, 9, 18)]), [{"date": "20260918"}])

    def test_window_custom_end_param_keeps_start_param_absent(self):
        """只自定义 end_param 时同理（不再凭空补一个 startTime）。"""
        got = config_mod.normalize_job(minimal_job(window={"days": 7, "end_param": "to"}))
        self.assertNotIn("start_param", got["window"])


class TestTargetTable(OfflineTestCase):
    def test_ddl_contains_structure_and_options(self):
        ddl = mc_mod.build_target_ddl(
            "demo_project", "ods_x_json_df", "json", "备注", stored_as="aliorc", lifecycle_days=36500
        )
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

        def batches():
            return iter([rows[0:2], rows[2:4], rows[4:5]])

        with mock.patch.object(utils.time, "sleep"):
            written = mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "20260918", batches, total=5
            )
        self.assertEqual(written, 5)
        self.assertEqual(table.deleted, [("pt=20260918", True)])
        self.assertEqual(table.created, [("pt=20260918", True)])
        flat = [batch[0] for batch in table._writer.records]
        self.assertEqual(flat, rows)

        # 第一次写失败后自动重试（写前会重新删/建分区，幂等）：
        # 重试必须从头重新读批次，且已写行数归零，不能把同一批数据写两遍
        table2 = FakeTable()
        table2.fail_next_write = True
        with mock.patch.object(utils.time, "sleep"):
            written = mc_mod.write_partition(
                DdlOdps(table2), table2, "demo_project", "ods_x", "20260918", batches, total=5, retries=2
            )
        self.assertEqual(written, 5)
        self.assertEqual(len(table2.deleted), 2)
        self.assertEqual([batch[0] for batch in table2._writer.records], rows)

    def test_write_partition_rejects_oversized_row_before_delete(self):
        """超限记录必失败：检查必须在删分区之前，否则旧数据被删、新数据又写不进去。"""
        table = FakeTable()

        def batches():
            return iter([["x" * (mc_mod.MAX_ROW_BYTES + 1)]])

        with self.assertRaises(SystemExit):
            mc_mod.write_partition(DdlOdps(table), table, "demo_project", "ods_x", "20260918", batches, total=1)
        self.assertEqual(table.deleted, [])
        self.assertEqual(table.created, [])

    def test_write_partition_opens_writer_with_reopen(self):
        """重试用全新 Tunnel 会话：复用上次失败留下的会话会把旧块一起提交（重复行）。"""
        table = FakeTable()
        batches = lambda: iter([['{"a":1}']])  # noqa: E731
        with mock.patch.object(utils.time, "sleep"):
            mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "20260918", batches, total=1, retries=1
            )
        self.assertEqual(table.writer_kwargs[0].get("reopen"), True)

    def test_write_partition_forwards_secrets_to_retry_call(self):
        """写库重试的日志/报错要带上作业的密钥值（Tunnel/SQL 报错里可能带签名 URL）。"""
        table = FakeTable()
        batches = lambda: iter([['{"a":1}']])  # noqa: E731
        with mock.patch.object(mc_mod, "retry_call", return_value=1) as retry:
            mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "20260918", batches, total=1, secrets=["sk-LIVE-XYZ"]
            )
        self.assertEqual(retry.call_args.kwargs["secrets"], ["sk-LIVE-XYZ"])

    def test_write_partition_count_mismatch(self):
        table = FakeTable()

        def batches():
            return iter([['{"a":1}']])

        with mock.patch.object(utils.time, "sleep"):
            with self.assertRaises(RuntimeError):
                mc_mod.write_partition(
                    DdlOdps(table), table, "demo_project", "ods_x", "20260918", batches, total=99, retries=1
                )

    def test_partition_ops_use_timeout_protected_ddl(self):
        """分区增删必须走带超时的 DDL（run_sql），不再调 pyodps 的 delete/create_partition。"""

        class NoPyodpsApiTable(FakeTable):
            def delete_partition(self, spec, if_exists=False):
                raise AssertionError("分区删除不该再走 pyodps API（同步、不受 --sql-timeout 保护）")

            def create_partition(self, spec, if_not_exists=False):
                raise AssertionError("分区创建不该再走 pyodps API（同步、不受 --sql-timeout 保护）")

        table = NoPyodpsApiTable()
        odps = DdlOdps(table)
        with mock.patch.object(utils.time, "sleep"):
            written = mc_mod.write_partition(
                odps, table, "demo_project", "ods_x", "20260918", lambda: iter([['{"a":1}']]), total=1, timeout=7
            )
        self.assertEqual(written, 1)
        sqls = "\n".join(odps.sqls)
        self.assertIn("drop if exists partition (pt='20260918')", sqls)
        self.assertIn("add if not exists partition (pt='20260918')", sqls)

    def test_drop_and_add_partition_sql_and_timeout(self):
        """删/建分区的 SQL 形态与 timeout 透传（README 承诺分区增删受 --sql-timeout 保护）。"""
        with mock.patch.object(mc_mod, "run_sql_with_timeout") as run:
            mc_mod.drop_partition(mock.Mock(), "p", "t", "pt=20260918", timeout=7)
            mc_mod.add_partition(mock.Mock(), "p", "t", "pt='20260918'", timeout=7)
        sqls = [call.args[1] for call in run.call_args_list]
        self.assertEqual(sqls[0], "alter table p.t drop if exists partition (pt='20260918')")
        self.assertEqual(sqls[1], "alter table p.t add if not exists partition (pt='20260918')")
        self.assertTrue(all(call.kwargs["timeout"] == 7 for call in run.call_args_list))

    def test_sql_spec_normalizes_quotes_and_rejects_bad_input(self):
        self.assertEqual(mc_mod._sql_spec("pt=20260918"), "pt='20260918'")
        self.assertEqual(mc_mod._sql_spec("pt='20260918'"), "pt='20260918'")
        self.assertEqual(mc_mod._sql_spec('pt="20260918"'), "pt='20260918'")
        for bad in ("pt=", "=20260918", "pt;drop=20260918"):
            with self.assertRaises(SystemExit):
                mc_mod._sql_spec(bad)


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

            def monotonic(self):  # 生产代码量经过时间用单调时钟
                return self.time()

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
            {"name": "teamA", "access_key_id": "id1", "access_key_secret": "s1"}, "作业文件"
        )
        self.assertEqual((ak, sk), ("id1", "s1"))
        self.assertIn("teamA", source)

    def test_load_credentials_env_fallback(self):
        with mock.patch.dict(
            os.environ, {"ALIYUN_ACCESS_KEY_ID": "env_id", "ALIYUN_ACCESS_KEY_SECRET": "env_sk"}, clear=False
        ):
            ak, sk, source = mc_mod.load_mc_credentials({}, "作业文件")
        self.assertEqual((ak, sk), ("env_id", "env_sk"))
        self.assertIn("环境变量", source)

    def test_connect_odps_uses_profile_endpoint(self):
        captured = {}

        class FakeODPS:
            def __init__(self, ak, sk, project, endpoint=None):
                captured.update(ak=ak, sk=sk, project=project, endpoint=endpoint)

        with mock.patch.object(mc_mod, "ODPS", FakeODPS):
            mc_mod.connect_odps(
                {}, "job.json", {"access_key_id": "a", "access_key_secret": "b", "endpoint": "http://e"}, "proj"
            )
        self.assertEqual(captured["endpoint"], "http://e")
        self.assertEqual(captured["project"], "proj")


class TestMcCredentialFallback(OfflineTestCase):
    """凭证查找链：作业文件 → 环境变量 → 本机 aliyun CLI 配置。"""

    def _fake_cli_config(self, tmp, payload) -> Path:
        home = Path(tmp) / "home"
        (home / ".aliyun").mkdir(parents=True)
        (home / ".aliyun" / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return home

    def test_falls_back_to_aliyun_cli_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._fake_cli_config(
                tmp,
                {
                    "current": "default",
                    "profiles": [
                        {"name": "default", "mode": "AK", "access_key_id": "cli_id", "access_key_secret": "cli_sk"}
                    ],
                },
            )
            with (
                mock.patch.dict(os.environ, {}, clear=False),
                mock.patch.object(Path, "home", staticmethod(lambda: home)),
            ):
                os.environ.pop("ALIYUN_ACCESS_KEY_ID", None)
                os.environ.pop("ALIYUN_ACCESS_KEY_SECRET", None)
                ak, sk, source = mc_mod.load_mc_credentials({}, "作业文件")
        self.assertEqual((ak, sk), ("cli_id", "cli_sk"))
        self.assertIn("aliyun CLI profile", source)
        self.assertNotIn("cli_sk", source)  # 来源说明里不能出现密钥本身

    def test_explicit_cli_profile_name_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self._fake_cli_config(
                tmp,
                {
                    "current": "default",
                    "profiles": [
                        {"name": "default", "mode": "AK", "access_key_id": "d_id", "access_key_secret": "d_sk"},
                        {"name": "teamB", "mode": "AK", "access_key_id": "b_id", "access_key_secret": "b_sk"},
                    ],
                },
            )
            with (
                mock.patch.dict(os.environ, {}, clear=False),
                mock.patch.object(Path, "home", staticmethod(lambda: home)),
            ):
                os.environ.pop("ALIYUN_ACCESS_KEY_ID", None)
                os.environ.pop("ALIYUN_ACCESS_KEY_SECRET", None)
                ak, _sk, source = mc_mod.load_mc_credentials({}, "作业文件", cli_profile="teamB")
        self.assertEqual(ak, "b_id")
        self.assertIn("teamB", source)

    def test_no_credentials_anywhere_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_home = Path(tmp) / "home"
            empty_home.mkdir()
            env = {k: v for k, v in os.environ.items() if k not in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET")}
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(Path, "home", staticmethod(lambda: empty_home)),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    mc_mod.load_mc_credentials({}, "作业文件")
        message = str(ctx.exception)
        self.assertIn("找不到阿里云 AccessKey", message)
        for hint in ("作业文件", "环境变量", "aliyun CLI"):
            self.assertIn(hint, message)

    def test_partial_credentials_is_config_error(self):
        """只填了 AK 没填 SK（典型：键名拼错）：报错、不回退——回退到 env/本机 CLI
        会用另一个身份写库（审计/计费全错），即使别的来源有完整凭证也不许用。"""
        env = {k: v for k, v in os.environ.items() if k not in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                mc_mod.load_mc_credentials({"access_key_id": "half"}, "作业文件")
        message = str(ctx.exception)
        self.assertIn("只填了一半", message)
        self.assertIn("access_key_secret", message)
        self.assertNotIn("half", message)  # 半对里的值也不回显

    def test_short_aliases_are_accepted(self):
        self.assertEqual(mc_mod._pick_aksk({"ak": "a", "sk": "b"}), ("a", "b"))
        self.assertEqual(mc_mod._pick_aksk({"ak_id": "a", "ak_secret": "b"}), ("a", "b"))
        self.assertEqual(mc_mod._pick_aksk({"access_key_id": "a"}), ("", ""))

    def test_named_cli_profile_missing_does_not_fall_back(self):
        """--mc-profile 指定了但不存在：报错，不许改用其它 profile 的 AK（换身份写库）。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = self._fake_cli_config(
                tmp,
                {
                    "current": "default",
                    "profiles": [
                        {"name": "default", "mode": "AK", "access_key_id": "cli_id", "access_key_secret": "cli_sk"}
                    ],
                },
            )
            env = {k: v for k, v in os.environ.items() if k not in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET")}
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(Path, "home", staticmethod(lambda: home)),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    mc_mod.load_mc_credentials({}, "作业文件", cli_profile="prod")
        message = str(ctx.exception)
        self.assertIn("prod", message)
        self.assertNotIn("cli_id", message)  # 不回显别的 profile 的 AK

    def test_broken_cli_config_gives_readable_error(self):
        """本机 aliyun 配置坏掉（空文件 / profiles 类型不对）不该抛裸 traceback。"""
        env = {k: v for k, v in os.environ.items() if k not in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET")}
        for payload in ("", '{"profiles": "oops"}', '{"profiles": ["not-a-dict"]}', "[1, 2]"):
            with tempfile.TemporaryDirectory() as tmp:
                home = self._fake_cli_config(tmp, {})  # 先建好目录
                (home / ".aliyun" / "config.json").write_text(payload, encoding="utf-8")
                with (
                    mock.patch.dict(os.environ, env, clear=True),
                    mock.patch.object(Path, "home", staticmethod(lambda: home)),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        mc_mod.load_mc_credentials({}, "作业文件")
            message = str(ctx.exception)
            self.assertIn("aliyun CLI 配置读不了", message)
            self.assertIn("ALIYUN_ACCESS_KEY_ID", message)  # 给出可用的替代方案

    def test_missing_pyodps_reports_install_hint(self):
        with mock.patch.object(mc_mod, "ODPS", None):
            with self.assertRaises(SystemExit) as ctx:
                mc_mod.connect_odps({}, "作业文件", {}, "proj")
        self.assertIn("pip install pyodps", str(ctx.exception))


# =============================================================================
# 目标解析 / profile / 其他
# =============================================================================


class TestResolveTarget(OfflineTestCase):
    def test_defaults(self):
        job, _ = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        project, table, column, pt = config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))
        self.assertEqual((project, table, column, pt), ("demo_project", "ods_demo_json_df", "json", "20260918"))

    def test_resolve_target_rechecks_table_and_column(self):
        """resolve_target 也是对外入口（库调用方不经 validate_job）：table/column 会拼进
        DDL/SQL，必须与 project 同口径过标识符白名单。"""
        job = minimal_job(target={"project": "p", "table": "t; drop table x"})
        with self.assertRaises(SystemExit):
            config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))
        job = minimal_job(target={"project": "p", "table": "t", "column": "c'; drop"})
        with self.assertRaises(SystemExit):
            config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))

    def test_project_from_profile(self):
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {"secrets": {}}, date(2026, 9, 18))
        project, _t, _c, _pt = config_mod.resolve_target(
            job, {"profiles": {"default": {"project": "p1"}}}, make_args(), date(2026, 9, 18)
        )
        self.assertEqual(project, "p1")

    def test_legacy_maxcompute_block(self):
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {"secrets": {}}, date(2026, 9, 18))
        project, _t, _c, _pt = config_mod.resolve_target(
            job, {"maxcompute": {"project": "legacy"}}, make_args(), date(2026, 9, 18)
        )
        self.assertEqual(project, "legacy")

    def test_pt_override_and_validation(self):
        job, _ = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        _p, _t, _c, pt = config_mod.resolve_target(job, {}, make_args(pt="20260918"), date(2026, 9, 18))
        self.assertEqual(pt, "20260918")
        # --pt 显式指定：允许测试/对比/补数用的特殊分区（默认的 target.pt 仍强制 8 位业务日）
        _p, _t, _c, pt = config_mod.resolve_target(job, {}, make_args(pt="test_20260921"), date(2026, 9, 18))
        self.assertEqual(pt, "test_20260921")
        # 但值本身必须是合法分区名（斜杠/空格/换行等一律拒）
        for bad in ("2026/09/18", "bad pt", "20260918" + chr(10)):
            with self.assertRaises(SystemExit):
                config_mod.resolve_target(job, {}, make_args(pt=bad), date(2026, 9, 18))

    def test_profile_value_wrong_type_reports_field(self):
        """profiles.<名> 写成字符串：给字段名报错，不是 dict.update 的裸 ValueError。"""
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {}, date(2026, 9, 18))
        with self.assertRaises(SystemExit) as ctx:
            config_mod.resolve_target(job, {"profiles": {"default": "my-project"}}, make_args(), date(2026, 9, 18))
        self.assertIn("profiles.default", str(ctx.exception))

    def test_profiles_not_object_reports_field(self):
        """--config 的 profiles 整体写成字符串：不能靠 `"default" in "oops"` 的子串判断静默当没配。"""
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {}, date(2026, 9, 18))
        with self.assertRaises(SystemExit) as ctx:
            config_mod.resolve_target(job, {"profiles": "oops"}, make_args(), date(2026, 9, 18))
        self.assertIn("profiles 必须是对象", str(ctx.exception))

    def test_maxcompute_block_wrong_type_reports_field(self):
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {}, date(2026, 9, 18))
        with self.assertRaises(SystemExit) as ctx:
            config_mod.resolve_target(job, {"maxcompute": ["ak", "sk"]}, make_args(), date(2026, 9, 18))
        self.assertIn("maxcompute 必须是对象", str(ctx.exception))

    def test_project_from_profile_is_still_whitelisted(self):
        """project 来自 profiles/<maxcompute> 时同样要过标识符白名单（它一样会拼进 DDL/SQL）。"""
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {}, date(2026, 9, 18))
        for config in (
            {"profiles": {"default": {"project": "bad-name;drop"}}},
            {"maxcompute": {"project": "has space"}},
        ):
            with self.assertRaises(SystemExit) as ctx:
                config_mod.resolve_target(job, config, make_args(), date(2026, 9, 18))
            self.assertIn("标识符", str(ctx.exception))

    def test_full_width_pt_rejected(self):
        """_PT_RE 只认 ASCII 数字：全角数字不能当业务日蒙混过关。"""
        self.assertIsNone(config_mod._PT_RE.match("２０２６０９２０"))
        self.assertIsNotNone(config_mod._PT_RE.match("20260920"))


class TestRunLock(OfflineTestCase):
    def test_lock_path_is_per_job(self):
        from api2ods.cli import _lock_path

        first = _lock_path(Path("jobs/onerway.json"))
        second = _lock_path(Path("jobs/aliyun.json"))
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("onerway-") and first.suffix == ".lock")

    def test_lock_path_normalizes_path_spelling(self):
        """同一作业的相对/绝对/带 .. 写法必须落到同一把锁：否则两个实例互斥静默失效
        （同删同写一个分区）。"""
        from api2ods.cli import _lock_path

        rel = Path("jobs/demo.json")
        self.assertEqual(_lock_path(rel), _lock_path(Path(os.path.abspath("jobs/demo.json"))))
        self.assertEqual(_lock_path(rel), _lock_path(Path("jobs/../jobs/demo.json")))

    def test_lock_path_is_redirected_to_temp_in_tests(self):
        """单测把运行锁根目录重定向到临时目录：跑测试不会在仓库 .run-locks/ 里
        无限累积锁文件（临时作业路径每次哈希都不同）。生产行为不变。"""
        repo_root = Path(cli_mod.__file__).resolve().parents[1]
        self.assertNotEqual(cli_mod.ROOT, repo_root)
        self.assertTrue(cli_mod.ROOT.is_relative_to(Path(tempfile.gettempdir())))
        self.assertTrue(cli_mod._lock_path(Path("jobs/demo.json")).is_relative_to(cli_mod.ROOT))

    def test_same_stem_in_different_dirs_gets_different_locks(self):
        """jobs/a/api.json 与 jobs/b/api.json 是两份不同作业，共用一个锁会互相阻塞。"""
        from api2ods.cli import _lock_path

        self.assertNotEqual(_lock_path(Path("jobs/a/api.json")), _lock_path(Path("jobs/b/api.json")))
        self.assertEqual(_lock_path(Path("jobs/a/api.json")), _lock_path(Path("jobs/a/api.json")))

    def test_lock_dir_env_override_pins_location(self):
        """API2ODS_LOCK_DIR 把锁钉在固定目录：不同身份/TMPDIR 跑同一作业也互斥。"""
        with tempfile.TemporaryDirectory() as tmp:
            pinned = Path(tmp) / "shared-locks"
            with mock.patch.dict(os.environ, {"API2ODS_LOCK_DIR": str(pinned)}):
                path = cli_mod._lock_path(Path("jobs/demo.json"))
            self.assertEqual(path.parent, pinned)
            self.assertTrue(path.name.endswith(".lock"))
            # 同一作业两次调用必须落到同一个锁文件（不因身份/临时目录差异漂移）
            with mock.patch.dict(os.environ, {"API2ODS_LOCK_DIR": str(pinned)}):
                self.assertEqual(path, cli_mod._lock_path(Path("jobs/demo.json")))

    def test_lock_dir_env_override_unusable_fails_loudly(self):
        """显式指定的锁目录不可用要立刻报错：静默换目录就等于互斥失效。"""
        with mock.patch.dict(os.environ, {"API2ODS_LOCK_DIR": "/nonexistent-root-only/locks"}):
            with mock.patch.object(Path, "mkdir", side_effect=OSError("只读")):
                with self.assertRaises(SystemExit) as ctx:
                    cli_mod._lock_path(Path("jobs/demo.json"))
        self.assertIn("API2ODS_LOCK_DIR", str(ctx.exception))

    @unittest.skipIf(utils.fcntl is None and utils.msvcrt is None, "本平台没有可用的文件锁")
    def test_same_lock_blocks_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path):
                with self.assertRaises(SystemExit):
                    with utils.RunLock(path):
                        pass

    @unittest.skipIf(utils.fcntl is None and utils.msvcrt is None, "本平台没有可用的文件锁")
    def test_lock_is_released_after_with_block(self):
        """出了 with 就该能重新拿到锁，否则第二次运行会被自己的残留锁挡住。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path):
                pass
            with utils.RunLock(path):
                pass

    @unittest.skipIf(utils.fcntl is None and utils.msvcrt is None, "本平台没有可用的文件锁")
    def test_lock_blocked_message_points_at_the_file(self):
        """抢占失败时要说清是哪个文件、以及怎么处理，不能只说"忙"。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path):
                with self.assertRaises(SystemExit) as ctx:
                    with utils.RunLock(path):
                        pass
        message = str(ctx.exception)
        self.assertIn("已有任务在运行", message)
        self.assertIn(str(path), message)

    @unittest.skipIf(utils.fcntl is None and utils.msvcrt is None, "本平台没有可用的文件锁")
    def test_lock_writes_pid_for_troubleshooting(self):
        """锁文件里写明进程号：调度报警时能据此判断是不是同一个实例还在跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path):
                pass
            # Windows 上持锁期间独占文件，出了 with 再读
            self.assertEqual(path.read_text(encoding="utf-8"), str(os.getpid()))

    def test_lock_degrades_to_noop_without_platform_support(self):
        """没有 flock/msvcrt 的平台（罕见）退化成不阻塞：不能因此让工具跑不起来。"""
        with mock.patch.object(utils, "fcntl", None), mock.patch.object(utils, "msvcrt", None):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "job.lock"
                with utils.RunLock(path):
                    pass
                self.assertFalse(path.exists())  # 没有加锁就不该建文件


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
            utils.log("中文日志 ❌ 测试")  # 不应抛异常
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

    def test_log_sink_write_happens_outside_lock(self):
        """慢 sink（NFS/满盘）只该拖慢这条日志，不该占住全局锁卡死其它线程。"""
        seen = {}

        class Probe:
            def write(self, *_a):
                seen["locked"] = utils._lock.locked()

            def flush(self):
                pass

            def close(self):
                pass

        probe = Probe()
        utils.add_log_sink(probe)
        try:
            utils.log("hello")
        finally:
            utils.remove_log_sink(probe)
        self.assertIs(seen["locked"], False)

    def test_remove_log_sink_detaches_and_closes(self):
        """句柄要同时完成两件事：不再接收日志 + 关闭文件（否则日志会一直占着句柄）。"""
        handle = io.StringIO()
        utils.add_log_sink(handle)
        utils.remove_log_sink(handle)
        self.assertNotIn(handle, utils._sinks)
        self.assertTrue(handle.closed)
        utils.log("句柄已摘掉，不该再写进去")  # 不应抛异常
        with self.assertRaises(ValueError):
            handle.getvalue()  # 已关闭：证明真的关了

    def test_remove_log_sink_is_idempotent(self):
        """重复摘要在正常路径上会发生（main 的 finally 与 _exit_now 都会调）：不能报错。"""
        handle = io.StringIO()
        utils.add_log_sink(handle)
        utils.remove_log_sink(handle)
        utils.remove_log_sink(handle)  # 第二次：句柄已关、也已不在列表里
        utils.remove_log_sink(None)  # 没挂日志文件时传 None

    def test_broken_sink_does_not_break_logging(self):
        """日志文件写失败（磁盘满/句柄被关）不能影响主流程，且要能被看见一次。"""

        class BrokenSink:
            def __init__(self):
                self.closed = False

            def write(self, _text):
                raise OSError("disk full")

            def flush(self):
                pass

            def close(self):
                self.closed = True

        sink = BrokenSink()
        utils.add_log_sink(sink)
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            utils.log("照常输出")
        # 写失败的 sink 被摘掉并关闭（句柄不再泄漏），stderr 上留一条可见的警告
        self.assertNotIn(sink, utils._sinks)
        self.assertTrue(sink.closed)
        self.assertIn("--log-file 写入失败", buf.getvalue())

    def test_progress_log_is_throttled(self):
        """进度日志每 1000 条才打一次，否则百万条作业会刷爆日志。"""
        logged = []
        with mock.patch.object(utils, "log", side_effect=logged.append):
            utils.progress_log("已落盘", 999)
            utils.progress_log("已落盘", 1000)
            utils.progress_log("已落盘", 1001)
            utils.progress_log("已落盘", 2000)
        self.assertEqual(len(logged), 2)
        self.assertIn("1,000", logged[0])
        self.assertIn("2,000", logged[1])


class TestAsBool(OfflineTestCase):
    def test_string_false_is_false(self):
        """JSON 里写 "false"（带引号）是常见笔误：按真值判断会把分区清空。"""
        for value in ("false", "FALSE", " False ", "0", "no", "off"):
            self.assertFalse(utils.as_bool(value, default=True), value)
        # 空白串按"没填"处理：原来 strip 后落进 falsy 列表返回 False，
        # `verify: " "` 会静默关掉 TLS 校验、`strict: " "` 会静默放宽丢数检查
        for blank in ("", " ", chr(9)):
            self.assertTrue(utils.as_bool(blank, default=True), repr(blank))
            self.assertFalse(utils.as_bool(blank, default=False), repr(blank))

    def test_string_true_is_true(self):
        for value in ("true", "TRUE", "yes", "1", "  yes  "):
            self.assertTrue(utils.as_bool(value, default=False), value)

    def test_none_uses_default(self):
        self.assertTrue(utils.as_bool(None, default=True))
        self.assertFalse(utils.as_bool(None, default=False))

    def test_native_types(self):
        self.assertTrue(utils.as_bool(True, default=False))
        self.assertFalse(utils.as_bool(False, default=True))
        self.assertTrue(utils.as_bool(1, default=False))
        self.assertFalse(utils.as_bool(0, default=True))


class TestRedact(OfflineTestCase):
    def test_query_string_secrets(self):
        self.assertEqual(utils.redact("?token=abc123&page=1"), "?token=***&page=1")
        self.assertEqual(utils.redact("?AccessKeyId=x&Signature=y"), "?AccessKeyId=***&Signature=***")

    def test_json_numeric_secret_is_masked(self):
        """{"password": 12345} 这类不带引号的数字/布尔值只有 query 规则能遮（JSON 规则只吃字符串）。"""
        out = utils.redact('{"password": 12345, "page": 2}')
        self.assertNotIn("12345", out)
        self.assertIn("***", out)

    def test_ordinary_params_are_untouched(self):
        """task/monkey/keywords 这类普通参数不能被误伤（按词判断而不是子串）。"""
        self.assertEqual(utils.redact("?task=1&monkey=2&keywords=3"), "?task=1&monkey=2&keywords=3")
        self.assertEqual(utils.redact("?page=1&size=100"), "?page=1&size=100")

    def test_json_body_secrets(self):
        self.assertEqual(utils.redact('{"secret_key": "sk1", "page": 1}'), '{"secret_key": "***", "page": 1}')

    def test_single_quoted_repr_secrets(self):
        """异常里插值的 dict / {exc!r} 是单引号形态：漏掉它等于把密钥原样打进日志。"""
        self.assertEqual(
            utils.redact("{'access_key_secret': 'sk1', 'page': 1}"), "{'access_key_secret': '***', 'page': 1}"
        )
        self.assertNotIn("sk1", utils.redact("KeyError: 'client_secret' -> {'client_secret': 'sk1'}"))

    def test_sig_field_name_is_sensitive(self):
        """sign_field 用户可配（向导会问），写成 sig 时同样要脱敏。"""
        self.assertEqual(utils.redact("?sig=abc123&page=1"), "?sig=***&page=1")
        self.assertNotIn("abc123", utils.redact('{"sig": "abc123"}'))

    def test_rule_order_stops_truncation_leaks(self):
        """规则顺序：Bearer/Basic 与配置片段必须先于 `key=value` 规则跑。

        query 规则按 `=` / `:` 截断值，先跑它的话 `Authorization=Bearer abc123`
        会被切成 `Authorization=`，后面的 Bearer 规则再也匹配不到——
        密钥原样留在日志里（本轮复审实测三种形态）。
        """
        self.assertNotIn("abc123def", utils.redact("header: 'Authorization=Bearer abc123def'"))
        self.assertNotIn("abc123", utils.redact("url=https://h/api?access_token=abc123&x=1"))
        self.assertNotIn("abc 123", utils.redact("access_token='abc 123'"))

    def test_deeply_nested_equals_does_not_recursion_error(self):
        """构造性文本（上千个等号连写）不能把脱敏本身打成 RecursionError。

        各回调会把匹配到的值再交给 redact 递归；`a=b=c=…` 每层只剥一个等号，
        没有深度上限时第三方响应体里的这种文本会直接打挂日志路径。
        """
        text = "a=" + "b=" * 5000 + "c"
        out = utils.redact(text)
        self.assertIsInstance(out, str)
        self.assertIn("***", out)  # 到上限按「宁可多脱敏」整段遮掉

    def test_nested_values_are_redacted(self):
        """键名不敏感时值里也可能藏着密钥：配置片段 / 头行 / 查询串都要递归脱一层。"""
        self.assertNotIn("abc123", utils.redact("{'url': 'https://h/api?token=abc123&t=1'}"))
        self.assertNotIn("abc123", utils.redact("{'auth': {'access_token': 'abc123'}}"))
        self.assertNotIn("abc123", utils.redact("{'header': 'X-Api-Key: abc123'}"))

    def test_authorization_headers(self):
        self.assertNotIn("SECRET", utils.redact("Authorization: Bearer SECRETTOKENVALUE"))
        self.assertNotIn("dXNlcjpwYXNz", utils.redact("Authorization: Basic dXNlcjpwYXNz"))

    def test_custom_header_lines(self):
        """自定义头（X-Api-Key: xxx）也要脱敏——requests 抛错时带的就是这种多行形态。"""
        self.assertEqual(utils.redact("X-Api-Key: abc123def456"), "X-Api-Key: ***")
        self.assertEqual(utils.redact("X-Api-Key=abc123def456"), "X-Api-Key=***")
        self.assertNotIn("abc123", utils.redact("  access-token: abc123\n  page: 1"))

    def test_ordinary_header_lines_untouched(self):
        self.assertEqual(utils.redact("Content-Type: application/json"), "Content-Type: application/json")
        self.assertEqual(utils.redact("Content-Length: 123"), "Content-Length: 123")

    def test_timestamps_and_counts_not_mangled(self):
        """头行规则不能把日志正文（时间戳/条数）当成 header 改掉。"""
        line = "2026-09-18 10:00:00 拉到 5 条"
        self.assertEqual(utils.redact(line), line)

    def test_empty_input(self):
        self.assertEqual(utils.redact(""), "")
        self.assertIsNone(utils.redact(None))


class TestRecordToJson(OfflineTestCase):
    def test_compact_and_unicode(self):
        row = record_to_json({"名称": "测试", "n": 1, "nested": {"a": [1, 2]}})
        self.assertIn("测试", row)
        self.assertNotIn(": ", row)
        self.assertEqual(json.loads(row)["nested"]["a"], [1, 2])


class TestVersion(OfflineTestCase):
    def test_version(self):
        self.assertTrue(api2ods.VERSION.startswith("2."))


# =============================================================================
# 补数分区护栏 / BOM / 数值配置 / 体检页大小（v2.1.2 复审修复）
# =============================================================================


class TestBackfillRequiresBizdate(OfflineTestCase):
    """补数只决定"拉哪些天"，不决定分区——不给 --bizdate 就报错，别静默覆盖正式分区。"""

    def _resolve(self, biz_date, **args):
        """按真实流程走一遍：先 render_job（替换 ${bizdate}）再解析目标。"""
        job, _ = config_mod.render_job(minimal_job(), {}, biz_date)
        return config_mod.resolve_target(job, {}, make_args(**args), biz_date)

    def test_backfill_without_bizdate_raises(self):
        for overrides in ({"start_date": "2026-01-01", "end_date": "2026-02-28"}, {"dates": "2026-09-01,2026-09-05"}):
            with self.assertRaises(SystemExit) as ctx:
                self._resolve(date(2026, 9, 20), **overrides)
            message = str(ctx.exception)
            self.assertIn("补数", message)
            self.assertIn("--bizdate", message)  # 报错要直接给出正确用法

    def test_backfill_with_bizdate_lands_in_that_partition(self):
        _p, _t, _c, pt = self._resolve(
            date(2026, 9, 20), start_date="2026-01-01", end_date="2026-02-28", bizdate="20260920"
        )
        self.assertEqual(pt, "20260920")

    def test_normal_schedule_unaffected(self):
        _p, _t, _c, pt = self._resolve(date(2026, 9, 20), bizdate="20260920")
        self.assertEqual(pt, "20260920")

    def test_env_bizdate_also_anchors(self):
        with mock.patch.object(config_mod, "env_bizdate", lambda: date(2026, 9, 20)):
            _p, _t, _c, pt = self._resolve(date(2026, 9, 20), dates="2026-09-01")
        self.assertEqual(pt, "20260920")

    def test_explicit_pt_does_not_read_malformed_env(self):
        """--pt 已经指明了写哪个分区，畸形 env 不该在这里二次抛错（原来会炸在 env_bizdate）。"""
        with mock.patch.dict(os.environ, {"bizdate": "oops"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            _p, _t, _c, pt = self._resolve(date(2026, 9, 20), dates="2026-09-01", pt="20260920")
        self.assertEqual(pt, "20260920")

    def test_malformed_env_without_explicit_still_raises(self):
        """没有显式 --pt/--bizdate 时，畸形 env 仍然要响亮报错（这条不能回退）。"""
        with mock.patch.dict(os.environ, {"bizdate": "oops"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            with self.assertRaises(SystemExit):
                self._resolve(date(2026, 9, 20), dates="2026-09-01")


class TestBomTolerance(OfflineTestCase):
    def test_job_file_with_bom_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text('{"job": "带BOM"}', encoding="utf-8-sig")
            self.assertEqual(config_mod.load_json_file(path, "作业配置文件")["job"], "带BOM")

    def test_response_with_bom_parses(self):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.content = '{"code": 0}'.encode("utf-8-sig")
        response.text = '{"code": 0}'
        with mock.patch.object(http_mod, "requests") as fake_requests:
            fake_requests.request.return_value = response
            got = http_mod.request_once("GET", "https://x", {}, {}, "json", 5, True, True, None)
        self.assertEqual(got, {"code": 0})

    def test_bytes_response_with_bom_json_error_body_detected(self):
        """文件接口返回带 BOM 的 JSON 错误体时，要报"返回了 JSON"而不是误导性的"期望 ZIP"。"""
        payload = '{"error": "no permission"}'.encode("utf-8-sig")
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(payload, {"format": "csv"}, "导出")
        self.assertIn("返回了 JSON", str(ctx.exception))


class TestNumericConfigGuards(OfflineTestCase):
    def test_page_size_not_a_number(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": "100条",
                "total_pages_path": "t",
            }
        )
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: {"t": 1, "data": {"list": []}}):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod.Fetcher(job, Path(".")).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("page_size", str(ctx.exception))

    def test_days_not_a_number(self):
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(), {"window": {"days": "15天"}})
        self.assertIn("days", str(ctx.exception))

    def test_pad_hours_not_a_number(self):
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.window_param_sets({"window": {"pad_hours": "两小时"}}, [date(2026, 9, 18)])
        self.assertIn("pad_hours", str(ctx.exception))

    def test_pad_hours_out_of_range_message_points_the_right_way(self):
        """越界文案方向要对：25 说的是"重合超过一整天"，-3 才是"缩小窗口漏数据"。

        原来两种情况共用一句"负值会让窗口比该拉的范围少一段"，
        填 25 的用户按提示去改负值只会更错。
        """
        for value, must_have in ((25, "超过 24"), (-3, "往里缩")):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.window_param_sets({"window": {"pad_hours": value}}, [date(2026, 9, 18)])
            self.assertIn(must_have, str(ctx.exception))

    def test_huge_integer_reports_config_error_not_traceback(self):
        """JSON 里的超长整数字面量：float() 抛 OverflowError，必须是明确报错而不是裸 traceback。"""
        with self.assertRaises(SystemExit) as ctx:
            config_mod._require_number(10**400, "window.days", exclusive_min=0, integer=True)
        self.assertIn("window.days", str(ctx.exception))

    def test_negative_retry_delay_reports_config_error(self):
        """retry_delay 配成负数会在退避 sleep 时抛裸 ValueError，配置阶段就要拦下。"""
        job = minimal_job(
            request={
                "base_url": "https://api.example.com",
                "path": "/v1/items",
                "method": "GET",
                "records_path": "data.list",
                "retry_delay": -5,
            }
        )
        with self.assertRaises(SystemExit) as ctx:
            fetch_mod.Fetcher(job, Path("."))
        self.assertIn("retry_delay", str(ctx.exception))

    def _job_with(self, **request_overrides):
        request = {
            "base_url": "https://api.example.com",
            "path": "/v1/items",
            "method": "GET",
            "records_path": "data.list",
        }
        request.update(request_overrides)
        return minimal_job(request=request)

    def test_negative_retry_times_reports_config_error(self):
        with self.assertRaises(SystemExit) as ctx:
            fetch_mod.Fetcher(self._job_with(retry_times=-1), Path("."))
        self.assertIn("retry_times", str(ctx.exception))

    def test_invalid_method_reports_config_error(self):
        """任意字符串会原样写进 request.method（含换行还会拼进请求行）：按 RFC token 形态先挡住。"""
        for bad in ("GET\nPOST", "GE T"):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod.Fetcher(self._job_with(method=bad), Path("."))
            self.assertIn("method", str(ctx.exception))
        # 空串仍按未配置处理（默认 GET）
        self.assertEqual(fetch_mod.Fetcher(self._job_with(method=""), Path(".")).method, "GET")

    def test_window_format_is_stripped_and_written_back(self):
        """ " %Y-%m-%d" 这类带空白的写法校验通过后要写回剥离值，运行时不能按含空白的字面量格式化。"""
        job = minimal_job(window={"mode": "per_day", "days": 1, "format": " %Y-%m-%d "})
        config_mod.validate_job(job)
        self.assertEqual(job["window"]["format"], "%Y-%m-%d")

    def test_placeholder_key_resolving_to_list_is_an_error(self):
        """整串占位符解析成列表后当键：str() 会静默产出 "['a', 'b']" 这种没人认得的键。"""
        with self.assertRaises(SystemExit) as ctx:
            config_mod.deep_substitute({"${secrets.lst}": 1}, {"secrets": {"lst": ["a", "b"]}})
        self.assertIn("无法作为 JSON 键", str(ctx.exception))

    def test_zero_count_does_not_underflow(self):
        """float("1e-400") 会下溢成 0.0：极小但非零的计数不能被当成"明确的 0 条"。"""
        self.assertFalse(fetch_mod._is_zero_count("1e-400"))
        self.assertTrue(fetch_mod._is_zero_count("0"))
        self.assertTrue(fetch_mod._is_zero_count(0))
        self.assertTrue(fetch_mod._is_zero_count("0.0"))

    def test_oversized_zip_entry_is_rejected(self):
        """条目解压上限：超限条目在解压前就报错（zip bomb / 源侧导出事故）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
        with mock.patch.object(parsers, "MAX_ZIP_ENTRY_BYTES", 4):
            with self.assertRaises(utils.ConfigError) as ctx:
                parsers.parse_bytes(buffer.getvalue(), {"format": "csv", "unzip": True}, "t")
        self.assertIn("超过上限", str(ctx.exception))


class TestProbePageSize(OfflineTestCase):
    def _fetcher(self, job):
        return fetch_mod.Fetcher(job, Path("."))

    def test_page_probe_uses_size_one(self):
        job = minimal_job(
            pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100, "total_pages_path": "t"}
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 1, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(calls[0]["n"], 1)

    def test_cursor_probe_keeps_configured_page_size(self):
        """游标接口常有最小页大小限制，体检压到 1 会被误杀（正式跑却正常）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "size_param": "n",
                "page_size": 50,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(calls[0]["n"], 50)


class TestUnfinishedReason(OfflineTestCase):
    def test_unparseable_endpoint_is_not_completion(self):
        """终点字段存在但读不出数字：不能当「翻完了」收尾（原来会静默少数据）。"""
        self.assertIsNotNone(fetch_mod._unfinished_reason(2, "abc", 5, None))
        self.assertIsNotNone(fetch_mod._unfinished_reason(2, None, 5, "xyz"))

    def test_genuine_completion_returns_none(self):
        self.assertIsNone(fetch_mod._unfinished_reason(3, 3, 5, None))
        self.assertIsNone(fetch_mod._unfinished_reason(2, None, 5, 5))


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


class TestPendingWarningsArePerJob(OfflineTestCase):
    def test_string_warnings_key_falls_through_to_unknown_warning(self):
        """用户误写的 "__warnings__"（非 list）不能被 extend 按字符拆成假告警，
        也不能被静默吞掉——要落到"不是已知配置项"的扫描。"""
        job = minimal_job()
        job["__warnings__"] = "oops"
        ws = config_mod.collect_warnings(job)
        self.assertTrue(any("__warnings__" in w and "未知" in w or "__warnings__" in w for w in ws), ws)
        self.assertNotIn("o", ws)

    def test_warnings_do_not_leak_between_jobs(self):
        """模块级缓存会让告警串到下一次调用；现在挂在 job 上、由 collect_warnings 读取。"""
        first = config_mod.normalize_job(
            minimal_job(pagination={"type": "none", "size_param": "size", "page_size": 100})
        )
        config_mod.validate_job(first)
        self.assertTrue(any("size_param" in w for w in config_mod.collect_warnings(first)))

        second = minimal_job()
        config_mod.validate_job(second)
        self.assertEqual([w for w in config_mod.collect_warnings(second) if "size_param" in w], [])
        # 同一份 job 反复收集要读到同样的告警（只读不取走）：第一次取走后第二次返回空，
        # 会让第二次调用者看不到"只会请求一次"这类关键告警
        again = [w for w in config_mod.collect_warnings(first) if "size_param" in w]
        self.assertEqual(len(again), 1, again)


class TestFourthPassReview(OfflineTestCase):
    """上一轮改动自身引出的问题。"""

    @staticmethod
    def _fetcher(job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_internal_warning_key_is_not_reported_as_typo(self):
        """job["__warnings__"] 是内部告警槽，不该再被未知键扫描报一条"拼写错误"。"""
        job = config_mod.normalize_job(minimal_job(pagination={"type": "none", "size_param": "size", "page_size": 100}))
        config_mod.validate_job(job)
        warnings = config_mod.collect_warnings(job)
        self.assertTrue(any("size_param" in w for w in warnings))
        self.assertEqual([w for w in warnings if "__warnings__" in w], [])

    def test_window_retries_non_numeric_reports_field(self):
        """cli 层原来直接 int()，配置写错给的是裸 ValueError traceback。"""
        from api2ods.cli import _as_count

        with self.assertRaises(SystemExit) as ctx:
            _as_count("abc", 2, "pagination.window_retries")
        self.assertIn("pagination.window_retries", str(ctx.exception))
        self.assertEqual(_as_count(None, 2, "x"), 2)
        self.assertEqual(_as_count("3", 2, "x"), 3)

    def test_parse_skip_rows_error_is_config_error(self):
        """配置写错要立即失败，不能被整窗重试当成"接口抖动"白等十几秒。"""
        with self.assertRaises(utils.ConfigError):
            parsers._parse_text("a,b\n1,2\n", {"format": "csv", "skip_rows": "abc"})

    def test_probe_falls_back_when_page_size_rejected(self):
        """页大小有下限（>=10）的接口：体检压到 1 被判 400 时，按配置页大小再试一次。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 20,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            if args[2].get("n") == 1:
                raise utils.FatalApiError("HTTP 400：page size must be >= 10")
            return {"t": 1, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual([c["n"] for c in calls], [1, 20])
        self.assertEqual(count, 1)

    def test_probe_does_not_mask_unrelated_400(self):
        """与页大小无关的 400（如密钥错）不能被回退吞掉重试。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 20,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            raise utils.FatalApiError("HTTP 403：invalid signature")

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(utils.FatalApiError):
                self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(len(calls), 1)

    def test_interrupted_run_exits_hard(self):
        """拉取阶段的 Ctrl+C 走 run_sync 的 return 130，main 必须把它转成 os._exit。

        只捕异常的话这道保险永远不触发：异常在 run_sync 里就被吃掉了，
        进程仍会被 atexit 的线程 join 拖到在飞请求跑完。
        """
        import api2ods.cli as cli_mod

        exits = []
        # 作业文件现写一份：CI 上只有被跟踪的 jobs/*.example.json，真实作业（已 gitignore）不存在
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=130),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit", side_effect=exits.append),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "1"])
        # 真的走了硬退出（真实 os._exit 不返回，这里被 mock 掉所以还能拿到返回值）
        self.assertEqual(exits, [130])
        self.assertEqual(rc, 130)

    def test_normal_run_returns_normally(self):
        """正常结束不能被硬退出打断（否则临时文件清理、下游读返回值都受影响）。"""
        import api2ods.cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=0),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit") as exited,
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "1"])
        self.assertEqual(rc, 0)
        exited.assert_not_called()

    def test_failed_parallel_run_exits_hard(self):
        """并发模式失败退出（rc=1）也要硬退出：主线程 return 后，在飞请求会把进程拖到最后。

        实测：一个 5s 的在飞请求能让 rc=1 的进程多活 6 秒（调度看到"报了错还赖着"）。
        """
        import api2ods.cli as cli_mod

        exits = []
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=1),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit", side_effect=exits.append),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "3", "--workers", "3"])
        self.assertEqual(exits, [1])
        self.assertEqual(rc, 1)

    def test_failed_serial_run_still_returns(self):
        """单进程（--workers=1）没有线程要等：保持普通返回，别让测试/嵌入调用方被 os._exit 打断。"""
        import api2ods.cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=1),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit") as exited,
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920"])
        self.assertEqual(rc, 1)
        exited.assert_not_called()

    def test_size_error_hints_are_specific(self):
        """关键词要够具体：无关的 400 不该被当成"页大小被拒"而多发一次请求。"""
        self.assertTrue(fetch_mod._looks_like_size_error("HTTP 400：page size must be >= 10"))
        self.assertTrue(fetch_mod._looks_like_size_error("HTTP 400：每页最少 20 条"))
        self.assertFalse(fetch_mod._looks_like_size_error("HTTP 400：Invalid page param"))
        self.assertFalse(fetch_mod._looks_like_size_error("HTTP 400：invalid count of parameters"))

    def test_job_summary_redacts_description(self):
        """作业名/description 是自由文本（常粘联调样例）：进日志前必须过 _redact_job。"""
        job = minimal_job()
        job["description"] = "样例 curl 'https://a.example.com/x?ak=AKLT123456&sk=SECRET987654' 已联调"
        text = "\n".join(cli_mod._job_summary(job))
        self.assertNotIn("SECRET987654", text)

    def test_check_is_not_blocked_by_backfill_guard(self):
        """--check 是只读体检，补数前先看连不连通是正常用法。"""
        args = argparse.Namespace(
            check=True, pt="", bizdate=None, dates=None, start_date="2026-07-01", end_date="2026-09-20"
        )
        self.assertFalse(config_mod._backfill_without_bizdate(args))
        args.check = False
        self.assertTrue(config_mod._backfill_without_bizdate(args))

    def test_wizard_invalid_window_choice_warns(self):
        """窗口选择题填非法编号：提示一句，再按"不传时间"生成（原来静默）。"""
        answers = iter(
            [
                "demo",
                "https://api.example.com/v1/x",
                "GET",
                "0",
                "",
                "0",
                "9",
                "1",
                "",
                "",
                "",
                "proj",
                "tbl",
                "ak",
                "sk",
                "",
            ]
        )
        output: list[str] = []
        path = Path(tempfile.mkdtemp()) / "wizard.json"
        try:
            code = init_wizard.run_init(
                out_path=str(path),
                ask=lambda prompt="": next(answers, ""),
                ask_secret=lambda prompt="": next(answers, ""),
                echo=output.append,
            )
            self.assertEqual(code, 0)
            self.assertTrue(any("不是有效选项" in line for line in output))
        finally:
            # mkdtemp 建的是独立目录，用 shutil 递归清理：目录非空时 rmdir 会抛 OSError，
            # 掩盖真实的断言失败（残渣清理失败不该变成第二个错误）
            shutil.rmtree(path.parent, ignore_errors=True)


class TestThirdPassReview(OfflineTestCase):
    """全文复审第三轮修掉的边界问题。"""

    def test_parse_switches_are_known_keys(self):
        """strict_encoding / allow_multi_entry 是实现了的开关，不该被当成"未知配置项"告警。"""
        job = minimal_job(parse={"format": "csv", "strict_encoding": True, "allow_multi_entry": True})
        warnings = config_mod.collect_warnings(job)
        self.assertEqual([w for w in warnings if "strict_encoding" in w or "allow_multi_entry" in w], [])

    def test_retry_after_negative_seconds_clamped(self):
        """负秒数传给 time.sleep 会抛 ValueError（被当成网络抖动白退避）。"""
        response = mock.Mock()
        response.headers = {"Retry-After": "-1"}
        self.assertEqual(http_mod._retry_after_seconds(response), 0.0)

    def test_jsonl_rejects_non_object_lines(self):
        """每行必须是 JSON 对象：数组/数字行落进 ODS 后 get_json_object 取不到值。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text('{"a":1}\n[1,2]\n', {"format": "jsonl"})
        self.assertIn("第 2 行", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            parsers._parse_text("123\n", {"format": "jsonl"})

    def test_retryable_business_error_logs_message(self):
        """fail_if + retry=true 的报错要能被识别成可重试异常（日志才不会打接口原文）。"""
        payload = {"code": "Throttling", "message": "qps 超限"}
        with self.assertRaises(http_mod.RetryLater):
            http_mod.check_fail_if(payload, [{"path": "code", "not_equals": "0", "retry": True}])

    def test_wizard_repeats_on_non_numeric_days(self):
        """--init 的"最近几天"填 abc 原来抛裸 ValueError。"""
        answers = iter(
            [
                "demo",
                "https://api.example.com/v1/x",
                "GET",
                "0",
                "",
                "0",
                "1",
                "abc",
                "30",
                "",
                "",
                "",
                "proj",
                "tbl",
                "ak",
                "sk",
                "",
            ]
        )
        output: list[str] = []
        path = Path(tempfile.mkdtemp()) / "wizard.json"
        try:
            code = init_wizard.run_init(
                out_path=str(path),
                ask=lambda prompt="": next(answers, ""),
                ask_secret=lambda prompt="": next(answers, ""),
                echo=output.append,
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["window"]["days"], 30)
            self.assertTrue(any("不是整数" in line for line in output))
        finally:
            # mkdtemp 建的是独立目录，用 shutil 递归清理：目录非空时 rmdir 会抛 OSError，
            # 掩盖真实的断言失败（残渣清理失败不该变成第二个错误）
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_wizard_repeats_on_zero_or_negative_days(self):
        """window.days 必须是正整数：向导填 0/-3 不能生成 --check 都过不了的配置。"""
        answers = iter(
            [
                "demo",
                "https://api.example.com/v1/x",
                "GET",
                "0",
                "",
                "0",
                "1",
                "0",
                "-3",
                "7",
                "",
                "",
                "",
                "proj",
                "tbl",
                "ak",
                "sk",
                "",
            ]
        )
        output: list[str] = []
        path = Path(tempfile.mkdtemp()) / "wizard.json"
        try:
            code = init_wizard.run_init(
                out_path=str(path),
                ask=lambda prompt="": next(answers, ""),
                ask_secret=lambda prompt="": next(answers, ""),
                echo=output.append,
            )
            self.assertEqual(code, 0)
            job = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(job["window"]["days"], 7)
            config_mod.validate_job(config_mod.normalize_job(job))
            self.assertGreaterEqual(sum("必须不小于 1" in line for line in output), 2)
        finally:
            # mkdtemp 建的是独立目录，用 shutil 递归清理：目录非空时 rmdir 会抛 OSError，
            # 掩盖真实的断言失败（残渣清理失败不该变成第二个错误）
            shutil.rmtree(path.parent, ignore_errors=True)


class TestSkipRowsKeepsLineEndings(OfflineTestCase):
    def test_multiline_quoted_field_survives_skip_rows(self):
        text = '表头行\ncol1,col2\n"带\n换行的值",x\n'
        records = parsers._parse_text(text, {"format": "csv", "skip_rows": 1})
        self.assertEqual(records[0]["col1"], "带\n换行的值")

    def test_skip_until_keeps_line_endings(self):
        # 标记行本身当表头，明细从下一行开始
        text = '说明段\ncol1,col2\n"a\nb",x\n'
        records = parsers._parse_text(text, {"format": "csv", "skip_until": "col1"})
        self.assertEqual(records[0]["col1"], "a\nb")

    def test_negative_skip_rows_rejected(self):
        # ConfigError：配置写错，不该被当成网络抖动去做整窗重试
        with self.assertRaises(utils.ConfigError):
            parsers._parse_text("a,b\n1,2\n", {"format": "csv", "skip_rows": -1})

    def test_skip_rows_and_skip_until_conflict_reported(self):
        """标记行落在被跳过的前几行里时，报错要说"两个配置冲突"，不能只说"结构变了"。"""
        text = "汇总\n标记行\n表头\n1,2\n"
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers._parse_text(text, {"format": "csv", "skip_rows": 2, "skip_until": "标记行"})
        self.assertIn("不要同时配", str(ctx.exception))

    def test_skip_rows_swallowing_everything_is_empty_result(self):
        """skip_rows 正好把内容全跳空：当天真的没出账，按空结果处理，不是"两配置冲突"。"""
        text = "仅有的前置说明\n第二行\n"
        records = parsers._parse_text(text, {"format": "csv", "skip_rows": 2, "skip_until": "表头"})
        self.assertEqual(records, [])


# =============================================================================
# 主流程（run_sync / run_check）：写库的每道保护都必须真的拦得住
# =============================================================================


class SyncFlowTestCase(OfflineTestCase):
    """run_sync / run_check 的公共夹具：把网络与数仓两侧都换成假的。"""

    def setUp(self):
        super().setUp()
        import api2ods.cli as cli_mod

        self.cli = cli_mod
        # 主流程每步都打日志：这里的用例关心的是分支与返回值，把日志静音
        patcher = mock.patch.object(cli_mod, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        # 走和 main 一样的顺序：先替换 ${bizdate} 之类占位符，再补默认值
        self.job = config_mod.normalize_job(config_mod.render_job(minimal_job(), {}, date(2026, 9, 20))[0])
        config_mod.validate_job(self.job)
        # 每用例一个独立目录：字段快照（.field-state/）会落在 job_path 旁边，
        # 用系统 temp 共享路径会让快照串到别的用例（还要人工清理）
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_path = Path(self._tmp.name) / "demo.json"
        self.config_path = Path(self._tmp.name) / "config.json"

        # fetch_all 默认成功但不产生记录，各用例按需覆盖
        patcher = mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=lambda *a, **kw: ({}, []))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.odps = mock.Mock()
        patcher = mock.patch.object(self.cli, "connect_odps", return_value=self.odps)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.table = mock.Mock()
        patcher = mock.patch.object(self.cli, "ensure_target_table", return_value=self.table)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.written = mock.Mock(return_value=0)
        patcher = mock.patch.object(self.cli, "write_partition", self.written)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.counted = mock.Mock(return_value=0)
        patcher = mock.patch.object(self.cli, "count_partition", self.counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_sync(self, records=(), **arg_overrides) -> int:
        """跑一次 run_sync；records 会经 on_records 写进 spool（模拟拉到数据）。

        跑完删掉本次的临时 spool：run_sync 只在成功路径上 close（删文件），
        失败分支（失败退出、--keep-spool、异常）会把文件留在系统 temp 里，
        测试反复跑就会攒一地。这里按"本次新建的文件"清，别的用例的文件不动。
        """

        def fake_fetch_all(days, workers=1, window_retries=2, on_records=None):
            for record in records:
                on_records([record])
            return {}, []

        made = []
        real_init = spool_mod.SpoolWriter.__init__

        def spy_init(instance, path=None, **kwargs):
            real_init(instance, path, **kwargs)
            if path is None:
                made.append(instance.path)

        with (
            mock.patch.object(spool_mod.SpoolWriter, "__init__", spy_init),
            mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=fake_fetch_all),
        ):
            try:
                return self.cli.run_sync(
                    self.job, {}, self.config_path, make_args(**arg_overrides), date(2026, 9, 20), self.job_path
                )
            finally:
                for path in made:
                    path.unlink(missing_ok=True)


class TestRunSyncFetchFailures(SyncFlowTestCase):
    def test_fetch_failure_does_not_touch_warehouse(self):
        """任一请求单元失败就放弃写库：旧分区保持原样，绝不写半个分区。"""

        def failing(days, workers=1, window_retries=2, on_records=None):
            return {}, [("2026-09-20", "接口超时")]

        with mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=failing):
            rc = self.cli.run_sync(self.job, {}, self.config_path, make_args(), date(2026, 9, 20), self.job_path)
        self.assertEqual(rc, 1)
        self.odps.assert_not_called()
        self.written.assert_not_called()

    def test_keyboard_interrupt_returns_130(self):
        """拉取阶段 Ctrl+C：不写库、返回 130（main 会把它转成硬退出）。"""

        def interrupted(days, workers=1, window_retries=2, on_records=None):
            raise KeyboardInterrupt

        with mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=interrupted):
            rc = self.cli.run_sync(self.job, {}, self.config_path, make_args(), date(2026, 9, 20), self.job_path)
        self.assertEqual(rc, 130)
        self.odps.assert_not_called()

    def test_keep_spool_keeps_file_on_failure(self):
        """--keep-spool 是用户明确要求，失败分支也要生效（否则拿不到现场数据）。"""

        def failing(days, workers=1, window_retries=2, on_records=None):
            on_records([{"id": 1}])
            return {}, [("2026-09-20", "接口超时")]

        kept = []
        real_close = spool_mod.SpoolWriter.close

        def spy_close(self, keep=False):
            kept.append((self.path, keep))
            real_close(self, keep=keep)

        with (
            mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=failing),
            mock.patch.object(spool_mod.SpoolWriter, "close", spy_close),
        ):
            self.cli.run_sync(
                self.job, {}, self.config_path, make_args(keep_spool=True), date(2026, 9, 20), self.job_path
            )
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0][1], "失败 + --keep-spool 时应保留临时文件")
        # 该保留的是线上排障的现场文件，不是测试留下的垃圾：断言完自己收拾干净
        kept[0][0].unlink(missing_ok=True)


class TestRunSyncEmptyGuard(SyncFlowTestCase):
    def test_zero_rows_refuses_to_write(self):
        """0 行默认不写库：接口异常返回空时，不能把已有分区清掉。"""
        rc = self.run_sync(records=[])
        self.assertEqual(rc, 1)
        self.written.assert_not_called()
        self.odps.assert_not_called()

    def test_zero_rows_with_flag_writes_empty_partition(self):
        rc = self.run_sync(records=[], allow_empty=True)
        self.assertEqual(rc, 0)
        self.written.assert_called_once()

    def test_zero_rows_with_target_config_allows(self):
        """target.allow_empty=true 与命令行 --allow-empty 等效。"""
        self.job["target"]["allow_empty"] = True
        rc = self.run_sync(records=[])
        self.assertEqual(rc, 0)
        self.written.assert_called_once()


class TestRunSyncDryRun(SyncFlowTestCase):
    def test_dry_run_reports_without_writing(self):
        rc = self.run_sync(records=[{"id": 1}, {"id": 2}], dry_run=True)
        self.assertEqual(rc, 0)
        self.odps.assert_not_called()
        self.written.assert_not_called()


class TestRunSyncWriteStage(SyncFlowTestCase):
    def test_success_verifies_row_count(self):
        self.counted.return_value = 2
        rc = self.run_sync(records=[{"id": 1}, {"id": 2}])
        self.assertEqual(rc, 0)
        self.written.assert_called_once()
        # 写后必须用 count(*) 再核对一遍：Tunnel 的写入数不能自己证明自己
        self.counted.assert_called_once()

    def test_count_mismatch_returns_failure(self):
        """写后行数对不上 → 失败退出，让调度看到（而不是假装成功）。"""
        self.counted.return_value = 1
        rc = self.run_sync(records=[{"id": 1}, {"id": 2}])
        self.assertEqual(rc, 1)

    def test_write_exception_returns_failure(self):
        self.written.side_effect = RuntimeError("Tunnel 连接被重置")
        rc = self.run_sync(records=[{"id": 1}])
        self.assertEqual(rc, 1)

    def test_write_exception_keeps_spool_when_asked(self):
        kept = []
        real_close = spool_mod.SpoolWriter.close

        def spy_close(self, keep=False):
            kept.append(keep)
            real_close(self, keep=keep)

        self.written.side_effect = RuntimeError("Tunnel 连接被重置")
        with mock.patch.object(spool_mod.SpoolWriter, "close", spy_close):
            rc = self.run_sync(records=[{"id": 1}], keep_spool=True)
        self.assertEqual(rc, 1)
        self.assertEqual(kept, [True])

    def test_bad_lifecycle_days_is_config_error(self):
        """lifecycle_days 写错要给字段名，不能是裸 ValueError；且必须发生在建表之前。"""
        self.job["target"]["lifecycle_days"] = "三十天"
        with self.assertRaises(SystemExit) as ctx:
            self.run_sync(records=[{"id": 1}])
        self.assertIn("lifecycle_days", str(ctx.exception))
        self.written.assert_not_called()


class TestRunCheck(SyncFlowTestCase):
    def setUp(self):
        super().setUp()
        # 体检默认打一次真实请求：这里换成假的，用例只关心分支走向
        patcher = mock.patch.object(self.cli.Fetcher, "probe", return_value=("2026-09-20", 1))
        patcher.start()
        self.addCleanup(patcher.stop)
        # 默认目标表还没建：不存在的表走“运行时自动创建”，不调 get_table
        self.odps.exist_table.return_value = False

    def run_check(self, **arg_overrides) -> int:
        return self.cli.run_check(
            self.job, {}, self.config_path, make_args(check=True, **arg_overrides), date(2026, 9, 20), self.job_path
        )

    def test_probe_failure_returns_1(self):
        with mock.patch.object(self.cli.Fetcher, "probe", side_effect=RuntimeError("HTTP 401：签名错误")):
            self.assertEqual(self.run_check(), 1)

    def test_missing_table_is_not_an_error(self):
        """表不存在不算失败：正式跑的时候会自动建。"""
        self.odps.exist_table.return_value = False
        self.assertEqual(self.run_check(), 0)

    def test_existing_table_verifies_schema(self):
        """表已存在 → 校验结构 + 报告分区在不在；两种分区状态都不算失败。"""
        self.odps.exist_table.return_value = True
        table = FakeTable()
        self.odps.get_table.return_value = table
        self.assertEqual(self.run_check(), 0)  # 分区不存在：运行时创建

        table.existing_partitions.add("pt=20260920")
        self.assertEqual(self.run_check(), 0)  # 分区已存在

    def test_view_as_target_is_rejected(self):
        """视图不能当写入目标：体检就要拦住（先删再填会把视图搞坏）。"""
        self.odps.exist_table.return_value = True
        self.odps.get_table.return_value = FakeTable(view=True)
        self.assertEqual(self.run_check(), 1)

    def test_wrong_schema_is_rejected(self):
        """宽表/事务表结构与要求不符：必须在体检阶段就拒绝，别等到写库。"""
        self.odps.exist_table.return_value = True
        self.odps.get_table.return_value = FakeTable(columns=[Col("raw_json"), Col("pt")])
        self.assertEqual(self.run_check(), 1)

    def test_mc_connect_failure_returns_1(self):
        with mock.patch.object(self.cli, "connect_odps", side_effect=RuntimeError("网络不通")):
            self.assertEqual(self.run_check(), 1)

    def test_backfill_guard_does_not_block_check(self):
        """--check 是只读体检，补数护栏不该拦它（补数前先验证连通性是正常用法）。"""
        rc = self.cli.run_check(
            self.job,
            {},
            self.config_path,
            make_args(check=True, bizdate=None, start_date="2026-07-01", end_date="2026-09-20"),
            date(2026, 9, 20),
            self.job_path,
        )
        self.assertEqual(rc, 0)


class TestRunSyncBackfillGuard(SyncFlowTestCase):
    def test_backfill_without_bizdate_refuses_to_write(self):
        """补数不跟 --bizdate 会整段覆盖调度分区：必须在建表/删分区之前就拦住。"""
        with self.assertRaises(SystemExit) as ctx:
            self.cli.run_sync(
                self.job,
                {},
                self.config_path,
                make_args(start_date="2026-07-01", end_date="2026-09-20"),
                date(2026, 9, 20),
                self.job_path,
            )
        self.assertIn("--bizdate", str(ctx.exception))
        self.odps.assert_not_called()
        self.written.assert_not_called()

    def test_backfill_guard_does_not_block_dry_run(self):
        """--dry-run 同样不写库，补数前先试跑看条数正是它的用途（有数据时返回 0）。"""
        rc = self.run_sync(records=[{"id": 1}], start_date="2026-07-01", end_date="2026-09-20", dry_run=True)
        self.assertEqual(rc, 0)
        self.odps.assert_not_called()
        self.written.assert_not_called()

    def test_dry_run_zero_rows_returns_nonzero(self):
        """dry-run 因 0 行同样退出非 0：当预检用时能提前发现坏配置，与 main 的退出码承诺一致。"""
        rc = self.run_sync(records=[], dry_run=True)
        self.assertEqual(rc, 1)
        self.odps.assert_not_called()
        self.written.assert_not_called()


class TestMainEntry(OfflineTestCase):
    """main() 的参数校验与命令分发。"""

    def _main(self, argv):
        import api2ods.cli as cli_mod

        with mock.patch.object(cli_mod, "_open_log_file", lambda p: None):
            return cli_mod.main(argv)

    def _main_logging(self, argv):
        """跑一次 main 并收集日志行（cli 用的是 `from .utils import log`，
        patch utils.log 抓不到，得 patch cli 模块里的那个名字）。"""
        import api2ods.cli as cli_mod

        with mock.patch.object(cli_mod, "log") as spy, mock.patch.object(cli_mod, "_open_log_file", lambda p: None):
            rc = cli_mod.main(argv)
        return rc, [str(c.args[0]) for c in spy.call_args_list]

    def test_missing_job_returns_2(self):
        self.assertEqual(self._main([]), 2)

    def test_sql_timeout_negative_is_arg_error(self):
        """负数 --sql-timeout：在 argparse 阶段就报错（退出码 2），一个请求都不发起。"""
        import api2ods.cli as cli_mod

        with self.assertRaises(SystemExit) as ctx:
            cli_mod.build_parser().parse_args(["--sql-timeout", "-5"])
        self.assertEqual(ctx.exception.code, 2)

    def test_sql_timeout_zero_and_positive_ok(self):
        import api2ods.cli as cli_mod

        self.assertEqual(cli_mod.build_parser().parse_args(["--sql-timeout", "0"]).sql_timeout, 0)
        self.assertEqual(cli_mod.build_parser().parse_args(["--sql-timeout", "30"]).sql_timeout, 30)

    def test_sql_timeout_default(self):
        import api2ods.cli as cli_mod
        from api2ods.mc import SQL_TIMEOUT_SECONDS

        self.assertEqual(cli_mod.build_parser().parse_args([]).sql_timeout, SQL_TIMEOUT_SECONDS)

    def test_bad_bizdate_format_reports_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            rc, lines = self._main_logging(["--job", str(job_file), "--bizdate", "2026/09/20"])
        self.assertEqual(rc, 1)
        # 准备阶段的配置错也要带时间戳进日志（--log-file 里原来一个字都没有）
        self.assertIn("2026/09/20", chr(10).join(lines))

    def test_ctrl_c_during_check_returns_130(self):
        """体检要发真实请求（可能卡在超时里），这段在所有 try 之外：
        Ctrl+C 不接住的话会以裸 traceback 结束，退出码也不是 130。"""
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            import api2ods.cli as cli_mod
            import api2ods.fetch as fetch_mod

            with mock.patch.object(cli_mod.Fetcher, "probe", side_effect=KeyboardInterrupt):
                with mock.patch.object(fetch_mod.Fetcher, "probe", side_effect=KeyboardInterrupt):
                    self.assertEqual(self._main(["--job", str(job_file), "--check", "--bizdate", "20260920"]), 130)

    def test_missing_job_file_reports_path(self):
        rc, lines = self._main_logging(["--job", str(Path(tempfile.gettempdir()) / "no_such_job.json")])
        self.assertEqual(rc, 1)
        self.assertIn("no_such_job.json", chr(10).join(lines))

    def test_version_flag_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self._main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_log_file_pointing_at_directory_reports_clearly(self):
        """--log-file 给成目录：要一句人话，而不是 IsADirectoryError 的裸 traceback。"""
        import api2ods.cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                cli_mod._open_log_file(tmp)
        self.assertIn("指向的是目录", str(ctx.exception))

    def test_block_wrong_type_reports_through_main(self):
        """走真实入口：类型检查必须在 date_tz_of 之前生效，否则是裸 AttributeError。

        这条守的是"检查放在哪一步"——单测直接调 check_block_types 测不出顺序问题。
        """
        for block in ("window", "pagination", "request", "parse", "target"):
            with tempfile.TemporaryDirectory() as tmp:
                job_file = Path(tmp) / "demo.json"
                payload = minimal_job()
                payload[block] = "per_day"
                job_file.write_text(json.dumps(payload), encoding="utf-8")
                # 准备阶段的报错现在由 main 统一记日志并返回 1（原来冒泡成裸 SystemExit，
                # 控制台那行没有时间戳、--log-file 里什么都没有）
                rc, lines = self._main_logging(["--job", str(job_file), "--bizdate", "20260918"])
            text = chr(10).join(lines)
            self.assertEqual(rc, 1)
            self.assertIn(f"{block} 必须是对象", text)
            self.assertNotIn("has no attribute", text)

    def test_secrets_wrong_type_reports_field(self):
        """secrets 写成列表/字符串/数字：报错要说清是 secrets 字段，不是 dict.update 的裸异常。"""
        for bad in (["a"], "abc", 123):
            with tempfile.TemporaryDirectory() as tmp:
                job_file = Path(tmp) / "demo.json"
                payload = minimal_job()
                payload["secrets"] = bad
                job_file.write_text(json.dumps(payload), encoding="utf-8")
                rc, lines = self._main_logging(["--job", str(job_file), "--bizdate", "20260920"])
            self.assertEqual(rc, 1)
            self.assertIn("secrets 必须是对象", chr(10).join(lines))

    def test_log_file_handle_is_closed_after_run(self):
        """main 每次都要摘掉日志句柄：同一进程里反复调用不能向已关闭的文件写日志。"""
        import api2ods.cli as cli_mod

        handle = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=0),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: handle),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920"])
        self.assertEqual(rc, 0)
        self.assertTrue(handle.closed)
        self.assertNotIn(handle, utils._sinks)


# =============================================================================
# 覆盖率补齐：错误分支 / 并发路径 / 交互式向导（同样不碰网络与 MaxCompute）
# =============================================================================


class TestUtilsDefensiveBranches(OfflineTestCase):
    """utils 的防御分支：控制台兼容、日志句柄、平台锁、重试出口。"""

    def test_setup_console_tolerates_streams_without_reconfigure(self):
        """重定向流（CI 捕获、自定义流）没有 reconfigure：跳过就行，不能因此打不出日志。"""

        class PlainStream:
            def __init__(self):
                self.written = []

            def write(self, text):
                self.written.append(text)

            def flush(self):
                pass

        out, err = PlainStream(), PlainStream()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            utils.setup_console()
            utils.log("照常输出")
        self.assertTrue(any("照常输出" in chunk for chunk in out.written))

    def test_log_patches_console_only_once(self):
        """第一次 log 顺手把控制台切 UTF-8；之后每行日志不再重复尝试（热路径）。"""
        calls = []
        with (
            mock.patch.object(utils, "_console_patched", False),
            mock.patch.object(utils, "setup_console", side_effect=lambda: calls.append(1)),
        ):
            utils.log("第一次")
            utils.log("第二次")
        self.assertEqual(calls, [1])
        self.assertTrue(utils._console_patched)

    def test_remove_log_sink_survives_close_errors(self):
        """摘日志句柄时两种关闭错误都要吞掉：重复关（ValueError）和真关不动（如磁盘掉线）。

        这是收尾动作，不能把已经跑完的任务带崩——同一进程里 main 会被调用多次。
        """

        class ValueErrorOnClose:
            def close(self):
                raise ValueError("I/O operation on closed file")

        class BrokenOnClose:
            def close(self):
                raise OSError("句柄所在磁盘掉线")

        first = ValueErrorOnClose()
        utils.add_log_sink(first)
        utils.remove_log_sink(first)
        self.assertNotIn(first, utils._sinks)

        second = BrokenOnClose()
        utils.add_log_sink(second)
        utils.remove_log_sink(second)  # 关闭抛 OSError 同样只吞不报
        self.assertNotIn(second, utils._sinks)

    def test_runlock_ignores_pid_write_failure(self):
        """锁文件里写进程号只是排障标记：写不进去（磁盘满）不该让加锁失败。"""

        class ReadOnlyLockFile:
            def write(self, _text):
                raise OSError("no space left on device")

            def flush(self):
                pass

            def close(self):
                pass

            def seek(self, _pos):
                pass

            def truncate(self):
                # 新增的"拿到锁后再截断"步骤：写 pid 前先清旧标记（写失败仍只吞不报）
                pass

        with (
            mock.patch("builtins.open", return_value=ReadOnlyLockFile()),
            mock.patch.object(utils, "_try_lock", return_value=True),
            mock.patch.object(utils, "_unlock"),
        ):
            with utils.RunLock(Path("x.lock")) as lock:
                self.assertIsInstance(lock.fh, ReadOnlyLockFile)

    def test_try_lock_with_flock_reports_busy(self):
        """POSIX 分支：flock 拿得到返回 True，别人持锁（OSError）返回 False（不阻塞等）。"""
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB = 2, 4
        with mock.patch.object(utils, "fcntl", fake_fcntl):
            self.assertTrue(utils._try_lock("fh"))
            fake_fcntl.flock.assert_called_once_with("fh", 6)
            fake_fcntl.flock.side_effect = OSError(errno.EAGAIN, "Resource temporarily unavailable")
            self.assertFalse(utils._try_lock("fh"))

    def test_unlock_releases_flock_and_swallows_oserror(self):
        """解锁失败交给内核兜底（进程退出自动释放），不能让任务因为"放锁失败"报错。"""
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_UN = 8
        with mock.patch.object(utils, "fcntl", fake_fcntl):
            utils._unlock("fh")
            fake_fcntl.flock.assert_called_once_with("fh", 8)
            fake_fcntl.flock.side_effect = OSError("锁已随进程释放")
            utils._unlock("fh")

    def test_unlock_swallows_msvcrt_oserror(self):
        """Windows 分支同理：句柄已关时 msvcrt 解锁抛 OSError，只吞不报。"""
        fake_msvcrt = mock.Mock()
        fake_msvcrt.LK_UNLCK = 0
        fake_msvcrt.locking.side_effect = OSError("句柄已关闭")
        with mock.patch.object(utils, "fcntl", None), mock.patch.object(utils, "msvcrt", fake_msvcrt):
            utils._unlock(mock.Mock())  # 不应抛异常

    def test_try_lock_without_platform_support_always_true(self):
        """两种锁都没有的平台：_try_lock 恒真，退化成"不阻塞"而不是挡住运行。"""
        with mock.patch.object(utils, "fcntl", None), mock.patch.object(utils, "msvcrt", None):
            self.assertTrue(utils._try_lock("fh"))

    def test_retry_call_reraises_fatal_without_retrying(self):
        """FatalApiError（4xx/权限）：一次都不多试，也不等退避——重试多少次都一样。"""
        calls, sleeps = [], []

        def fn():
            calls.append(1)
            raise utils.FatalApiError("HTTP 401：invalid token")

        with mock.patch.object(utils.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(utils.FatalApiError):
                utils.retry_call(fn, attempts=3, base_delay=15, desc="写入")
        self.assertEqual(calls, [1])
        self.assertEqual(sleeps, [])

    def test_retry_call_exhausts_and_redacts(self):
        """重试到最后一轮仍失败：报"重试 N-1 次"（与真实请求数对得上），重试日志与异常都脱敏。"""
        calls, sleeps, logged = [], [], []

        def fn():
            calls.append(1)
            raise RuntimeError("接口超时 url=https://h/x?token=abc123")

        with (
            mock.patch.object(utils.time, "sleep", side_effect=sleeps.append),
            mock.patch.object(utils, "log", side_effect=logged.append),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                utils.retry_call(fn, attempts=3, base_delay=15, desc="拉取 2026-09-18")
        message = str(ctx.exception)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [15, 30])  # 指数退避，最后一轮不再等
        self.assertIn("重试 2 次仍失败", message)  # 一共发了 3 次请求
        self.assertIn("拉取 2026-09-18", message)
        self.assertNotIn("abc123", message)
        self.assertEqual(len(logged), 2)  # 每轮失败打一条
        self.assertTrue(all("abc123" not in line for line in logged))

    def test_retry_call_masks_configured_secret_values(self):
        """secrets 给定时按值精确遮蔽：底层 SDK 把凭证写进自由文本报错时形态规则盖不住。"""
        logged: list = []

        def fn():
            raise RuntimeError("tunnel failed: signature=ZmFrZTpub3RhcmVhbHNpZ25hdHVyZQ== raw sk-LIVE-XYZ")

        with (
            mock.patch.object(utils.time, "sleep"),
            mock.patch.object(utils, "log", side_effect=logged.append),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                utils.retry_call(
                    fn,
                    attempts=2,
                    base_delay=0,
                    desc="写入 t",
                    secrets=["sk-LIVE-XYZ"],
                )
        message = str(ctx.exception)
        self.assertNotIn("sk-LIVE-XYZ", message)
        self.assertIn("***", message)
        self.assertTrue(all("sk-LIVE-XYZ" not in line for line in logged))


# =============================================================================
# 第四轮复审（v2.1.4）修复的回归用例
# =============================================================================


class TestFourthPassParsers(OfflineTestCase):
    """parsers.py：行边界、空表头、JSON 防呆、空对象、空白行、空列名。"""

    def test_split_lines_ignores_non_newline_breaks(self):
        """\x0b/\x0c/  等不是 csv/JSONL 的行边界（splitlines 会多认 6 种）。"""
        got = parsers._parse_text("报表\x0c\n生成时间,今天\na,b\n1,x\n", {"format": "csv", "skip_rows": 2}, label="t")
        self.assertEqual(got, [{"a": "1", "b": "x"}])

    def test_jsonl_empty_object_line_is_skipped(self):
        """JSONL 里的空对象行（{}）与 Items:{} / [{}] 同义：跳过，不写全 NULL 假记录。"""
        body = '{"a": 1}' + chr(10) + "{}" + chr(10)
        self.assertEqual(parsers.parse_bytes(body.encode(), {"format": "jsonl"}, "t"), [{"a": 1}])

    def test_jsonl_empty_object_skipped_before_entry_field(self):
        """entry_field 合并前先判空：ZIP 里 {} 行不能变成只含条目名的假记录。"""
        buf = io.BytesIO()
        line = chr(10)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.jsonl", '{"a": 1}' + line + "{}" + line)
        got = parsers.parse_bytes(buf.getvalue(), {"format": "jsonl", "unzip": True, "entry_field": "src_file"}, "t")
        self.assertEqual(got, [{"a": 1, "src_file": "x.jsonl"}])

    def test_jsonl_line_with_u2028_kept(self):
        """U+2028 是合法 JSON 字符（工具自己 dump 的记录就有），不能被劈成两行。"""
        first = spool_mod.dump_record({"a": "x y"})
        second = spool_mod.dump_record({"b": 1})
        # 两条记录、最后一条不带换行：既验证 U+2028 不参与切行，也验证 JSONL 规范允许最后一行无换行符。
        self.assertEqual(
            parsers.parse_bytes((first + "\n" + second).encode(), {"format": "jsonl"}, "t"), [{"a": "x y"}, {"b": 1}]
        )

    def test_csv_blank_first_line_raises(self):
        """有内容但表头是空行 → 报错（原来静默 0 行，allow_empty 时会先删再填清空分区）。"""
        for payload in (b"\na,b\n1,2\n", b"\r\na,b\r\n1,2\r\n"):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(payload, {"format": "csv"}, "结算")
            self.assertIn("空行", str(ctx.exception))

    def test_jsonl_single_object_requires_explicit_opt_in(self):
        """整个响应只有一个 JSON 对象时与错误体无法区分：默认拦下，显式开关才放行。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"id":1}\n', {"format": "jsonl"}, "t")
        self.assertIn("allow_single_record", str(ctx.exception))
        self.assertEqual(
            parsers.parse_bytes(b'{"id":1}\n', {"format": "jsonl", "allow_single_record": True}, "t"),
            [{"id": 1}],
        )

    def test_error_body_detection_covers_array_and_broken_json(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'[{"code":500,"msg":"err"}]', {"format": "csv"}, "t")
        self.assertIn("JSON", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b'{"code": NaN}', {"format": "csv"}, "t")

    def test_csv_header_starting_with_bracket_not_mistaken(self):
        """合法 CSV 表头以 [ 开头时不能误报成 JSON 错误体。"""
        self.assertEqual(
            parsers.parse_bytes("[日期],[金额]\n1,2\n".encode(), {"format": "csv"}, "t"),
            [{"[日期]": "1", "[金额]": "2"}],
        )

    def test_records_path_empty_object_is_zero_rows(self):
        """Items: {} 表示"没有数据"，不能包成一条全 NULL 的假记录。"""
        self.assertEqual(parsers.extract_json_records({"data": {"list": {}}}, {"records_path": "data.list"}, "t"), [])

    def test_single_column_blank_value_kept(self):
        """单列文件里"值为空白"是合法记录；多列的全空白行仍按排版垃圾跳过。"""
        self.assertEqual(parsers.parse_bytes(b"v\n \nx\n", {"format": "csv"}, "t"), [{"v": " "}, {"v": "x"}])
        self.assertEqual(parsers.parse_bytes(b"a,b\n \n1,2\n", {"format": "csv"}, "t"), [{"a": "1", "b": "2"}])

    def test_empty_column_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,b,\n1,2,3\n", {"format": "csv"}, "t")
        self.assertIn("空列名", str(ctx.exception))


class TestFourthPassFetchHttp(OfflineTestCase):
    """fetch/http：并发内存、timeout 校验、JSON 编码、Retry-After NaN、fail_if 数值比较。"""

    def test_timeout_must_be_positive(self):
        for bad in (0, -5):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod.Fetcher({"request": {"base_url": "http://x", "timeout_seconds": bad}}, Path("."))
            self.assertIn("timeout_seconds", str(ctx.exception))

    def test_retry_after_nan_is_ignored(self):
        """NaN 夹取不掉、传给 time.sleep 会抛错，让整条 429 重试链失效。"""
        response = mock.Mock()
        response.headers = {"Retry-After": "nan"}
        self.assertIsNone(http_mod._retry_after_seconds(response))

    def test_fail_if_compares_numbers_by_value(self):
        # 0.0 与 0 等价：not_equals 不误杀
        http_mod.check_fail_if({"code": 0.0}, [{"path": "code", "not_equals": 0}])
        # equals 方向不漏判
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"code": 200}, [{"path": "code", "equals": 200.0}])
        # 字符串数字与数字等价（原行为保留）
        http_mod.check_fail_if({"code": "20000"}, [{"path": "code", "not_equals": 20000}])

    def test_gbk_json_body_requires_explicit_encoding(self):
        """GBK 的 JSON 不能静默按 UTF-8 replace 成乱码写库：要么显式配 json_encoding，要么报错。"""

        class Resp:
            status_code, headers, encoding = 200, {}, None
            content = '{"name": "张三"}'.encode("gbk")

            def raise_for_status(self):
                pass

        with mock.patch.object(http_mod, "requests") as rq:
            rq.request.return_value = Resp()
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 30, True, True, None)
            self.assertIn("json_encoding", str(ctx.exception))
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 30, True, True, None, "gbk")
        self.assertEqual(got, {"name": "张三"})

    def test_worker_records_released_after_spool(self):
        """--workers>1 时已交付单元的 records 要在落盘后立即释放（future 会持有到函数返回）。"""

        class RecList(list):
            pass

        job = {
            "request": {"base_url": "http://x", "records_path": "data.list"},
            "window": {"mode": "per_day", "days": 4, "format": "%Y-%m-%d %H:%M:%S"},
        }
        days = [date(2026, 9, 10 + i) for i in range(4)]
        refs, still_alive = [], []

        def fake_fetch_unit(self, unit, **kwargs):
            return RecList([{"x": 1}])

        def on_records(records):
            gc.collect()
            alive = 0
            for w in refs:
                # 只解引用一次：w() 的临时强引用在表达式结束后消失，第二次 w() 可能是 None，
                # 原写法 len(w()) 会随机 TypeError（用例时好时坏）
                obj = w()
                if obj is not None and len(obj) > 0:
                    alive += 1
            still_alive.append(alive)
            refs.append(weakref.ref(records))

        with mock.patch.object(fetch_mod.Fetcher, "fetch_unit", fake_fetch_unit):
            fetcher = fetch_mod.Fetcher(job, Path("."))
            fetcher.fetch_all(days, workers=2, window_retries=0, on_records=on_records)
        self.assertEqual(still_alive, [0, 0, 0, 0])


class TestFourthPassConfigDates(OfflineTestCase):
    """config/dates：校验补漏、报错脱敏、占位符、时区格式、业务日传递。"""

    def test_auth_written_as_string_gets_chinese_error(self):
        job = minimal_job()
        job["request"]["auth"] = "bearer"
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("request.auth", str(ctx.exception))

    def test_number_error_redacts_secret_value(self):
        """_require_number 的报错回显要走 _show（值可能来自 ${secrets.x} 替换后的密钥字面量，
        形态脱敏要能把它遮掉——直接 {value!r} 会原样进日志）。"""
        job = minimal_job()
        job["pagination"] = {"type": "page", "page_size": "access_token=SECRETVALUE123456"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertNotIn("SECRETVALUE123456", str(ctx.exception))

    def test_validate_job_infers_pagination_type_without_normalize(self):
        """validate_job 单独调用（不先 normalize）也要能推断分页类型：cursor 的
        「没配 total_items_path」告警不能因误判 none 而跳过。"""
        job = minimal_job(pagination={"cursor_path": "data.next"})
        config_mod.validate_job(job)
        warnings = [w for w in config_mod.collect_warnings(job) if "total_items_path" in w]
        self.assertTrue(warnings, warnings)

    def test_extra_params_type_checked(self):
        job = minimal_job()
        job["window"] = {"extra_params": "BillingCycle"}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)
        job["window"] = {"extra_params": {"BillingCycle": None}}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_extra_params_value_written_back_stripped(self):
        """校验的是 strip 后的值就该写回：否则运行时仍按带空白的字面量格式化（参数带空格）。"""
        job = minimal_job()
        job["window"] = {"extra_params": {"BillingCycle": " %Y-%m "}}
        config_mod.validate_job(job)
        self.assertEqual(job["window"]["extra_params"]["BillingCycle"], "%Y-%m")

    def test_window_format_typo_rejected(self):
        """format 写错（unixms）不能把字面量发给接口：立即报错并列出可用写法。"""
        job = minimal_job()
        job["window"] = {"format": "unixms"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("unix_ms", str(ctx.exception))

    def test_profile_error_does_not_echo_secrets(self):
        job = minimal_job()
        job["target"]["profile"] = {"project": "p", "access_key_id": "AKID", "access_key_secret": "SECRET456"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.get_mc_profile_meta({}, job, make_args())
        self.assertNotIn("SECRET456", str(ctx.exception))
        self.assertNotIn("AKID", str(ctx.exception))

    def test_unclosed_placeholder_rejected(self):
        with self.assertRaises(SystemExit):
            config_mod.deep_substitute("Bearer ${secrets.token", {"secrets": {"token": "t"}})

    def test_placeholder_in_dict_key_substituted(self):
        got = config_mod.deep_substitute({"${secrets.param}": "v"}, {"secrets": {"param": "pn"}})
        self.assertEqual(got, {"pn": "v"})

    def test_config_file_maxcompute_rendered(self):
        """--config 文件自己的 maxcompute 也支持 ${secrets.*}（secrets 就在同一份文件里）。"""
        config = {"secrets": {"ak": "REAL_AK"}, "maxcompute": {"access_key_id": "${secrets.ak}"}}
        _job, rendered_config = config_mod.render_job(minimal_job(), config, date(2026, 9, 20))
        self.assertEqual(rendered_config["maxcompute"]["access_key_id"], "REAL_AK")
        # 不改写入参：同一份 config 在同一进程里第二次渲染时，不会拿着上一次已替换的密钥
        self.assertEqual(config["maxcompute"]["access_key_id"], "${secrets.ak}")

    def test_percent_r_counts_as_time_format(self):
        """%R = %H:%M：不能被当成纯日期格式（窗口会塌成 00:00~00:00）。"""
        self.assertFalse(dates_mod.is_date_only_format("%Y-%m-%d %R"))
        got = dates_mod.window_param_sets(
            {
                "window": {
                    "mode": "per_day",
                    "format": "%Y-%m-%d %R",
                    "date_tz": "America/New_York",
                    "api_tz": "+08:00",
                    "start_param": "startTime",
                    "end_param": "endTime",
                }
            },
            [date(2026, 9, 18)],
        )
        self.assertEqual(got, [{"startTime": "2026-09-18 12:00", "endTime": "2026-09-19 12:00"}])

    def test_unix_format_case_insensitive(self):
        moment = datetime(2026, 9, 18, 12, 0, 0)
        self.assertEqual(dates_mod.format_time(moment, "UNIX"), int(moment.timestamp()))
        self.assertEqual(dates_mod.format_time(moment, "Unix_MS"), int(moment.timestamp() * 1000))

    def test_pad_hours_nan_rejected(self):
        with self.assertRaises(utils.ConfigError):
            dates_mod.window_param_sets(
                {"window": {"pad_hours": float("nan"), "format": "%Y-%m-%d %H:%M:%S", "start_param": "s"}},
                [date(2026, 9, 18)],
            )

    def test_window_days_zero_rejected_with_field_name(self):
        """window.days=0 是有意写错，不能静默变 1 天；报错要指向配置字段。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(bizdate="20260920"), {"window": {"days": 0}})
        self.assertIn("window.days", str(ctx.exception))

    def test_resolve_days_uses_caller_bizdate(self):
        """main 算好的业务日要透传：两边各读一次时钟会在跨零点错分区。"""
        with mock.patch.object(dates_mod, "env_bizdate", lambda strict: None):
            got = dates_mod.resolve_days(make_args(days=2), {"window": {}}, date(2026, 9, 15))
        self.assertEqual(got, [date(2026, 9, 14), date(2026, 9, 15)])

    def test_backfill_ignores_days_with_notice(self):
        """补数模式下 --days 不生效：原来的静默忽略要留痕。"""
        logged = []
        with mock.patch.object(dates_mod, "log_once", side_effect=logged.append):
            got = dates_mod.resolve_days(
                make_args(start_date="2026-09-01", end_date="2026-09-03", days=1), {"window": {}}
            )
        self.assertEqual(len(got), 3)
        self.assertTrue(any("--days" in line for line in logged))


class TestFourthPassUtilsCliWizard(OfflineTestCase):
    """utils/cli/wizard：脱敏补漏、锁探测、日志句柄、硬退出、向导自洽。"""

    def test_camel_case_secret_keys_redacted(self):
        for name in ("signStr", "authKey", "signBody"):
            out = utils.redact({name: "topsecret"})
            self.assertNotIn("topsecret", out, name)
        self.assertIn("1", utils.redact({"task": "1"}))  # 普通参数不误伤

    def test_authorization_two_part_value_fully_redacted(self):
        for header, secret in (
            ("Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b", "9944b0"),
            ("Authorization: Bearer abc", "abc"),
            ("Authorization: ApiKey SECRETKEY123456", "SECRETKEY"),
        ):
            out = utils.redact(header)
            self.assertNotIn(secret, out.replace("Authorization", ""), header)

    def test_url_encoded_secret_redacted(self):
        out = utils.redact("https://h/x?target=https%3A%2F%2Fhook%2Fcb%3Ftoken%3DSUPERSECRET&a=1")
        self.assertNotIn("SUPERSECRET", out)

    def test_lock_path_stable_under_concurrency(self):
        """探测文件用唯一名：并发调用不能漂移到不同目录（互斥会静默失效）。"""
        import api2ods.cli as cli_mod

        job_path = Path(tempfile.gettempdir()) / "api2ods-lock-probe-test.json"
        results = []
        threads = [threading.Thread(target=lambda: results.append(str(cli_mod._lock_path(job_path)))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 1)

    def test_missing_job_detaches_log_sink(self):
        """提前 return 2 也要摘日志 sink（否则同进程再跑 main 会继续写旧文件）。"""
        import api2ods.cli as cli_mod

        before = len(getattr(utils, "_sinks", []))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_path = Path(tmp.name) / "run.log"
        rc = cli_mod.main(["--log-file", str(log_path)])
        self.assertEqual(rc, 2)
        self.assertEqual(len(getattr(utils, "_sinks", [])), before)

    def test_config_error_from_run_sync_exits_hard(self):
        """并发模式下配置错也要硬退出（否则解释器退出阶段会 join 在飞请求）。"""
        import api2ods.cli as cli_mod

        exits = []
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", side_effect=utils.ConfigError("配置炸了")),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit", side_effect=exits.append),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "1", "--workers", "3"])
        self.assertEqual(exits, [1])
        self.assertEqual(rc, 1)


class TestConfigErrorBranches(OfflineTestCase):
    """config 的报错分支：文件坏、类型写错、profile 找不到。"""

    def test_job_file_with_broken_json_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text('{"job": "x",}', encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                config_mod.load_json_file(path, "作业配置文件")
        self.assertIn("不是合法 JSON", str(ctx.exception))
        self.assertIn("broken.json", str(ctx.exception))

    def test_job_file_top_level_must_be_object(self):
        """顶层是数组时 dict() 之后取键会全崩：这里直接报清楚。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text('[{"job": "x"}]', encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                config_mod.load_json_file(path, "作业配置文件")
        self.assertIn("顶层必须是 JSON 对象", str(ctx.exception))

    def test_unknown_non_secret_placeholder_lists_available(self):
        """普通占位符写错时列出可用清单：别让用户去猜（secrets.x 另有更具体的提示）。"""
        with self.assertRaises(SystemExit) as ctx:
            config_mod.deep_substitute("${today_isoo}", {"secrets": {}, "today": "20260920"})
        message = str(ctx.exception)
        self.assertIn("today_isoo", message)
        self.assertIn("today_iso", message)

    def test_secrets_must_be_object(self):
        for bad in (["k"], "abc", 3):
            with self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(minimal_job(secrets=bad))
            self.assertIn("secrets 必须是对象", str(ctx.exception))

    def test_maxcompute_and_profiles_must_be_objects(self):
        for block in ("maxcompute", "profiles"):
            with self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(minimal_job(**{block: "oops"}))
            self.assertIn(f"{block} 必须是对象", str(ctx.exception))

    def test_profile_entry_must_be_object(self):
        """profiles.prod 写成字符串：取用它的一刻才抛裸 ValueError，必须在校验阶段拦住。"""
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(minimal_job(profiles={"prod": "my-project"}))
        message = str(ctx.exception)
        self.assertIn("profiles.prod", message)
        self.assertIn("str", message)

    def test_add_fields_must_be_object(self):
        """add_fields 写成数组时 .setdefault 会崩：报错要说清字段名。"""
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(
                minimal_job(
                    request={
                        "base_url": "https://x",
                        "path": "/p",
                        "method": "GET",
                        "records_path": "d",
                        "add_fields": ["source_account"],
                    },
                    target={"project": "p", "table": "t"},
                )
            )
        self.assertIn("add_fields", str(ctx.exception))

    def test_bad_auth_type_lists_choices(self):
        job = minimal_job()
        job["request"]["auth"] = {"type": "oauth2"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("oauth2", str(ctx.exception))
        self.assertIn("bearer", str(ctx.exception))

    def test_bad_response_type(self):
        job = minimal_job()
        job["request"]["response_type"] = "xml"
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("json/bytes", str(ctx.exception))

    def test_bad_window_mode(self):
        job = minimal_job(window={"mode": "hourly"})
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("per_day/range", str(ctx.exception))

    def test_bad_pagination_type(self):
        job = minimal_job(pagination={"type": "offset"})
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("none/page/cursor", str(ctx.exception))

    def test_bad_parse_format_on_json_response(self):
        """JSON 响应也可以配 parse.format（整包解析），写错同样要拦。"""
        job = minimal_job(parse={"format": "xml"})
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("csv/tsv/jsonl", str(ctx.exception))

    def test_fail_if_must_be_array(self):
        job = minimal_job()
        job["request"]["fail_if"] = {"path": "Code"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("必须是数组", str(ctx.exception))

    def test_fail_if_item_needs_path(self):
        job = minimal_job()
        job["request"]["fail_if"] = [{"not_equals": "Success"}, "Code"]
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("必须包含 path", str(ctx.exception))

    def test_no_project_anywhere_reports_where_to_put_it(self):
        """project 缺失时要说清三个可写位置，而不是让运行到一半崩在别处。"""
        job, _ = config_mod.render_job(minimal_job(target={"table": "t"}), {}, date(2026, 9, 18))
        with self.assertRaises(SystemExit) as ctx:
            config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))
        self.assertIn("maxcompute.project", str(ctx.exception))

    def test_unknown_profile_lists_available_profiles(self):
        job, _ = config_mod.render_job(minimal_job(), {}, date(2026, 9, 18))
        config = {"profiles": {"prod": {"project": "p1"}}}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.get_mc_profile_meta(config, job, make_args(mc_profile="staging"))
        message = str(ctx.exception)
        self.assertIn("staging", message)
        self.assertIn("prod", message)


class TestDatesErrorBranches(OfflineTestCase):
    """dates 的解析报错与 unix_ms 分支。"""

    def test_parse_offset_rejects_garbage(self):
        for text in ("oops", "08:00", ""):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.parse_offset(text)
            self.assertIn("无法解析时区偏移", str(ctx.exception))

    def test_parse_offset_rejects_out_of_range(self):
        """'+080' 不能被贪心拆成 0 小时 80 分（静默变成 +01:20），越界要报错。"""
        for text in ("+15:00", "+08:80"):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.parse_offset(text)
            self.assertIn("超出范围", str(ctx.exception))

    def test_parse_offset_negative_sign(self):
        self.assertEqual(dates_mod.parse_offset("-05:30").utcoffset(None), timedelta(hours=-5, minutes=-30))

    def test_load_api_zone_accepts_offsets_and_names(self):
        self.assertEqual(dates_mod.load_api_zone("-05:00").utcoffset(None), timedelta(hours=-5))
        self.assertEqual(str(dates_mod.load_api_zone("UTC")), "UTC")

    def test_unknown_zone_reports_tzdata_hint(self):
        """时区名写错：报错带上"Windows 要装 tzdata"，这是最常见的踩坑点。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.load_zone("Nowhere/Nope")
        self.assertIn("tzdata", str(ctx.exception))

    def test_dates_list_with_only_blank_entries_is_an_error(self):
        """--dates 里全是空值（逗号/空格）：报"为空"，不能当成"拉 0 天"静默成功。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(dates=" , "), {})
        self.assertIn("--dates 为空", str(ctx.exception))

    def test_reversed_start_end_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(start_date="2026-09-05", end_date="2026-09-01"), {})
        self.assertIn("不能早于", str(ctx.exception))

    def test_days_below_one_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(bizdate="20260918", days=0), {})
        self.assertIn("--days 必须 >= 1", str(ctx.exception))

    def test_backfill_range_too_large_is_error(self):
        """年份打错（--end-date 写成下个世纪）会展开成几十万个日期：先 OOM 再无休止
        按天打接口——区间上限 3660 天，超了直接报错。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(start_date="2026-01-01", end_date="9999-12-31"), {})
        self.assertIn("区间过大", str(ctx.exception))

    def test_format_time_unix_ms(self):
        value = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(dates_mod.format_time(value, "unix_ms"), dates_mod.format_time(value, "unix") * 1000)

    def test_unix_ms_has_no_float_truncation(self):
        """亚秒边界不少 1ms：int(timestamp()*1000) 对 +0.36s 会截成 359（end 偏小 = 丢数据）。"""
        tz = timezone(timedelta(hours=8))
        base = datetime(2026, 9, 20, tzinfo=tz)
        value = datetime(2026, 9, 20, 0, 0, 0, 360000, tzinfo=tz)
        self.assertEqual(dates_mod.format_time(value, "unix_ms") - dates_mod.format_time(base, "unix_ms"), 360)

    def test_unix_seconds_floors_before_epoch(self):
        """1970 前的时刻按 floor 取整：int(timestamp()) 是向零截断，会大 1 秒。"""
        value = datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
        self.assertEqual(dates_mod.format_time(value, "unix"), -1)
        self.assertEqual(dates_mod.format_time(value, "unix_ms"), -1)

    def test_format_without_directives_is_error(self):
        """ "unixtime"/空白这类没有任何 % 指令的格式串：strftime 会原样输出成参数值，
        请求带着字面量发出去（0 数据还退出 0）——配置预检之外，库调用方也要拦。"""
        value = datetime(2026, 9, 18, tzinfo=timezone.utc)
        for bad in ("unixtime", "epoch", "  "):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.format_time(value, bad)
            self.assertIn("没有任何时间指令", str(ctx.exception))

    def test_pad_hours_bool_is_error(self):
        """YAML 里把 pad 当开关写 true：float(True)=1.0 会静默多拉前后各 1 小时。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.window_param_sets({"window": {"pad_hours": True}}, [date(2026, 9, 18)])
        self.assertIn("pad_hours", str(ctx.exception))


class TestParserErrorBranches(OfflineTestCase):
    """parsers 的解析报错分支：坏路径、坏表头、坏 JSONL、坏 ZIP。"""

    def test_malformed_path_returns_default(self):
        """路径片段里有非法字符（a[b]）：返回默认值，不能崩。"""
        self.assertEqual(parsers.get_path({"a": 1}, "a[b]", default="d"), "d")
        self.assertIsNone(parsers.get_path({"a": 1}, "a[1"))

    def test_records_must_be_array_or_object(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.ensure_object_records(5, "单次请求")
        message = str(ctx.exception)
        self.assertIn("不是数组/对象", message)
        self.assertIn("int", message)

    def test_empty_object_inside_array_is_normalized(self):
        """数组里的空对象（Items: [{}]）与 Items: {} 同义：归一成空列表，不写全 NULL 假记录。"""
        self.assertEqual(parsers.ensure_object_records([{}], "单次请求"), [])
        self.assertEqual(parsers.ensure_object_records([{"a": 1}, {}], "单次请求"), [{"a": 1}])

    def test_large_single_line_json_rejected_without_full_decode(self):
        """>1MB 的单行 JSON：不整包 decode（OOM 源），按首/尾字节判定拦下。"""
        big = b"{" + b'"k": 1,' * 200000 + b'"z": 2}'
        self.assertGreater(len(big), 1024 * 1024)
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(big, {"format": "csv"}, "t")
        self.assertIn("首尾字符与 JSON 一致", str(ctx.exception))

    def test_cr_only_file_not_treated_as_text_error_body(self):
        """CR-only 换行文件不会被当"单行文本错误体"（那是"无任何行界"的判据）：
        交给 csv 层按真实结构报错（csv 不认裸 CR，报 new-line 提示），而不是误导性的
        "疑似纯文本错误体"。"""
        CR = bytes([13])
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"amount" + CR + b"100" + CR + b"200", {"format": "csv"}, "t")
        message = str(ctx.exception)
        self.assertNotIn("单行文本", message)
        self.assertIn("表头解析失败", message)

    def test_small_zip_not_treated_as_text_error_body(self):
        """二进制（小 ZIP）的字节里恰好没有换行/分隔符时，不能被当成"单行文本错误体"。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.csv", "id,amount")
        data = buf.getvalue()
        self.assertIsNone(parsers._reject_json_error_body(data, {"format": "csv"}, "t"))

    def test_skip_rows_conflict_only_when_marker_in_skipped_part(self):
        """skip_rows 与 skip_until 同配：标记确实在被跳部分才报配置错；否则按可重试处理。"""
        NL = chr(10)
        with self.assertRaises(config_mod.ConfigError):
            parsers._parse_text(
                "标题段" + NL + "a,b" + NL + "1,2",
                {"format": "csv", "skip_rows": 1, "skip_until": "标题段"},
                "t",
            )
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text(
                "<html>err</html>" + NL + "x,y",
                {"format": "csv", "skip_rows": 1, "skip_until": "明细"},
                "t",
            )
        self.assertIn("找不到明细段标记", str(ctx.exception))

    def test_plain_text_error_body_rejected_on_csv_path(self):
        """单行纯文本错误体（非 JSON，如 503 Service Unavailable）：同样拦下
        （当 CSV 会静默产出 0 行、调度按零数据收尾）。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"503 Service Unavailable", {"format": "csv"}, "结算")
        self.assertIn("单行文本", str(ctx.exception))

    def test_large_multiline_json_rejected_by_tail_probe(self):
        """大且多行的 JSON（pretty-printed）：不整包解码，但按尾部采样照样拦下
        （不拦会把 JSON 当 CSV 解析成垃圾记录写进 ODS）。"""
        NL = bytes([10])
        big = NL.join([b"{"] + [b'  "k": 1,'] * 260000 + [b"}"])  # >1MB
        self.assertGreater(len(big), 1024 * 1024)
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(big, {"format": "csv"}, "结算")
        self.assertIn("尾部采样", str(ctx.exception))

    def test_scalar_error_body_rejected_on_csv_path(self):
        """HTTP 200 + JSON 标量错误体（"rate limit exceeded" / 500 / null）：当 CSV 会静默
        产出 0 行（唯一“列名”就是错误消息本身），必须拦下而不是按空数据收尾。"""
        for payload in (b'"rate limit exceeded"', b"500", b"null", b"true", b"502\n", b'"rate limit exceeded"\n'):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(payload, {"format": "csv"}, "结算")
            self.assertIn("JSON 标量", str(ctx.exception))

    def test_single_line_text_with_separator_is_not_scalar_error_body(self):
        """单行但带列分隔符（或本来就多行）的文本不按标量错误体处理，仍走正常解析。"""
        self.assertEqual(parsers.parse_bytes(b"a,b", {"format": "csv"}, "结算"), [])
        self.assertEqual(parsers.parse_bytes(b"a,b\n", {"format": "csv"}, "结算"), [])
        self.assertEqual(parsers.parse_bytes(b"a,b\n1,2\n", {"format": "csv"}, "结算"), [{"a": "1", "b": "2"}])

    def test_header_with_surrounding_spaces_is_error(self):
        """表头列名带首尾空格（" amount"，导出常见）：json 键原样带空格、下游
        get_json_object 取不到——与空列名同属静默丢列，直接报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"id, amount\n1,2\n", {"format": "csv"}, "结算")
        self.assertIn("空白", str(ctx.exception))

    def test_oversized_header_reports_csv_error(self):
        """表头行超过 csv 字段上限（解析层把上限调到 7MB，这里压小来模拟）：给可读报错。"""
        old_limit = csv.field_size_limit(10)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                parsers._parse_text("a" * 50 + ",b\n1,2\n", {"format": "csv"}, label="报表")
        finally:
            csv.field_size_limit(old_limit)
        self.assertIn("表头解析失败", str(ctx.exception))
        self.assertIn("报表", str(ctx.exception))

    def test_empty_text_returns_no_records(self):
        """空文件（当天真的没出账）：返回空列表，不是报错。"""
        self.assertEqual(parsers._parse_text("", {"format": "csv"}), [])

    def test_blank_rows_are_skipped(self):
        """报表里的空行（只有分隔符）不能变成一条全空记录写进 ODS。"""
        records = parsers._parse_text("a,b\n1,2\n,\n3,4\n", {"format": "csv"})
        self.assertEqual(records, [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])

    def test_jsonl_with_nan_reports_line_snippet(self):
        """NaN 不是合法 JSON（落进 ODS 后下游取不到值）：报错要带行内容。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text('{"a": 1}\n{"b": NaN}\n', {"format": "jsonl"})
        message = str(ctx.exception)
        self.assertIn("JSONL 解析失败", message)
        self.assertIn("NaN", message)

    def test_jsonl_entry_field_is_stamped(self):
        """ZIP 多条目合并时用 entry_field 标记来源文件（否则分不清数据出自哪个包）。"""
        records = parsers._parse_text('{"a": 1}\n', {"format": "jsonl", "entry_field": "__file"}, entry="part1.jsonl")
        self.assertEqual(records, [{"a": 1, "__file": "part1.jsonl"}])

    def test_unknown_format_is_a_config_error(self):
        """格式写错是配置问题：抛 ConfigError（SystemExit 子类），别被当成网络抖动重试。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers._parse_text("x", {"format": "xml"})
        self.assertIn("xml", str(ctx.exception))

    def test_corrupted_zip_entry_reports_name(self):
        """条目损坏（截断包/CRC 对不上）：报出是哪个条目，方便让源方重导。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("amount.csv", "user,amount\nu1,5\n")
        data = bytearray(buffer.getvalue())
        index = data.index(b"user,amount")  # 改一个字节让 CRC-32 对不上
        data[index] = ord("X")
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(bytes(data), {"format": "csv", "unzip": True}, "导出")
        message = str(ctx.exception)
        self.assertIn("amount.csv", message)
        self.assertIn("读取失败", message)

    def test_bytes_response_type_requires_binary_payload(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_payload({"code": 0}, {"response_type": "bytes"}, {"format": "csv"}, "导出")
        message = str(ctx.exception)
        self.assertIn("期望二进制响应", message)
        self.assertIn("dict", message)


class TestSpoolDefensiveBranches(OfflineTestCase):
    """spool 的清理动作与上下文管理器。"""

    def test_loads_json_rejects_nan_and_infinity(self):
        for text, name in (('{"a": NaN}', "NaN"), ('{"a": Infinity}', "Infinity")):
            with self.assertRaises(ValueError) as ctx:
                spool_mod.loads_json(text)
            self.assertIn(name, str(ctx.exception))

    def test_close_swallows_unlink_failure(self):
        """临时文件删不掉（被占用/权限）：任务已经写完了，不能因此报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1}])
            with mock.patch.object(Path, "unlink", side_effect=OSError("被占用")):
                spool.close()
            path.unlink(missing_ok=True)

    def test_context_manager_keeps_file_on_exception(self):
        """with 块里出错保留现场文件（排障），正常退出按默认删掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            with self.assertRaises(ValueError):
                with spool_mod.SpoolWriter(path) as spool:
                    self.assertIsInstance(spool, spool_mod.SpoolWriter)
                    spool.write_records([{"a": 1}])
                    raise ValueError("解析中途失败")
            self.assertTrue(path.exists())

            with spool_mod.SpoolWriter(path):
                pass
            self.assertFalse(path.exists())


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


class TestFetchDefensiveBranches(OfflineTestCase):
    """fetch 的防御分支：空计划、非对象记录、空页收尾、体检限量、整窗重试参数。"""

    @staticmethod
    def _fetcher(job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_zero_count_only_accepts_numbers(self):
        """零数据判定只认数字 0：读不出数字时按"无法确认"处理（宁可重试也别静默收尾）。"""
        self.assertTrue(fetch_mod._is_zero_count(0))
        self.assertTrue(fetch_mod._is_zero_count("0"))
        self.assertFalse(fetch_mod._is_zero_count([]))
        self.assertFalse(fetch_mod._is_zero_count("很多"))
        self.assertFalse(fetch_mod._is_zero_count(float("nan")))
        self.assertFalse(fetch_mod._is_zero_count(float("inf")))
        self.assertFalse(fetch_mod._is_zero_count(10**10000))

    def test_probe_with_no_units_reports_clearly(self):
        """日期列表为空（如 --dates 全是空值）时窗口展开不出任何单元：要明确报错。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        with self.assertRaises(utils.ConfigError) as ctx:
            self._fetcher(job).probe([])
        self.assertIn("days 为空", str(ctx.exception))

    def test_decorate_keeps_non_object_records_untouched(self):
        """add_fields 只给对象追加字段；非对象记录原样保留（上游解析已拦住这类数据）。"""
        job = minimal_job()
        job["request"]["add_fields"] = {"source_account": "账号A"}
        fetcher = self._fetcher(job)
        self.assertEqual(fetcher._decorate([{"id": 1}, "raw"]), [{"id": 1, "source_account": "账号A"}, "raw"])

    def test_window_params_are_merged_with_fixed_params(self):
        """窗口参数（startTime/endTime）要并进固定参数，而不是覆盖掉它们。"""
        job = minimal_job(
            window={
                "mode": "per_day",
                "date_tz": "UTC",
                "api_tz": "+08:00",
                "start_param": "startTime",
                "end_param": "endTime",
            }
        )
        job["request"]["params"] = {"app": "demo"}
        calls = []

        def fake(*args, **_kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        fetcher = self._fetcher(job)
        unit = fetcher.build_units([date(2026, 9, 18)])[0]
        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = fetcher.fetch_unit(unit)
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(calls[0]["app"], "demo")
        self.assertIn("startTime", calls[0])
        self.assertIn("endTime", calls[0])

    def test_page_without_records_path_reports_diagnostics(self):
        """翻页中途 records_path 取不到：必须报错（静默当空页 = 少拉数据还显示成功）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        with mock.patch.object(http_mod, "request_once", return_value={"data": {}}):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("找不到 records_path", str(ctx.exception))

    def test_empty_page_after_total_reached_stops_cleanly(self):
        """总数已达、接口又回一页空：按"已翻完"正常收尾，不能报"无法确认"整窗失败。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}]}},  # 首页：2 条，没带总数
            {"data": {"list": [], "totalCount": 2}},  # 末页之后：空页 + 总数 2
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual(responses, [])

    def test_stop_after_first_page_sends_one_request(self):
        """体检限量参数：只发一页就返回（数据量大的源不能因为体检被全量拉一遍）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **_kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}], "totalPages": 9}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(
                fetch_mod.FetchUnit("d", date(2026, 9, 18), None), stop_after_first_page=True
            )
        self.assertEqual([r["id"] for r in records], [1])
        self.assertEqual(len(calls), 1)

    def test_cursor_respects_param_as_string(self):
        """param_as_string 对游标同样生效（原来只有 page/size 字符串化）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "param_as_string": True,
                "cursor_start": 1000,
                "page_size": 5,
            }
        )
        fetcher = self._fetcher(job)
        seen = []

        def fake_request(params, *a, **k):
            seen.append(dict(params))
            return {"next": None, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(fetcher, "_do_request", side_effect=fake_request):
            fetcher.fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(seen[0]["c"], "1000")
        self.assertEqual(seen[0]["size"], "5")

    def test_page_delay_sleeps_between_pages(self):
        """pagination.delay_seconds 是给限速接口留的间隔，翻页时必须真的等。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 1,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0.5,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 2}},
            {"data": {"list": [{"id": 2}], "totalPages": 2}},
        ]
        with (
            mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False) as sleep,
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        sleep.assert_called_once()
        self.assertEqual(sleep.call_args.args[0], 0.5)  # 间隔秒数；第二参是本轮停止信号

    def test_cursor_not_advancing_is_error(self):
        """游标原样回显（cursor_path 配到恒定字段）：继续翻会把同一页拉满 max_pages 次、
        重复数据无上限累积——直接按配置错中止，而不是翻 2000 页。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "page_size": 2,
                "delay_seconds": 0,
            }
        )
        fetcher = self._fetcher(job)
        calls = []

        def same_cursor(*_a, **_k):
            calls.append(1)
            return {"next": "same", "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=same_cursor):
            with self.assertRaises(SystemExit) as ctx:
                fetcher.fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("游标未推进", str(ctx.exception))
        self.assertEqual(len(calls), 2)  # 第二页发现游标没动就中止，没有翻满 max_pages

    def test_window_retries_must_be_integer(self):
        """pagination.window_retries 写错：配置错直接退出，别在整窗重试里空等。"""
        job = minimal_job()
        with self.assertRaises(SystemExit) as ctx:
            self._fetcher(job).fetch_all([date(2026, 9, 18)], window_retries="两")
        self.assertIn("window_retries", str(ctx.exception))


class TestFetchAllConcurrent(OfflineTestCase):
    """并发模式（--workers>1）：单元进线程池、逐个收尾、失败只影响自己那个单元。"""

    @staticmethod
    def _fetcher_and_days():
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        days = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
        return fetch_mod.Fetcher(job, Path(".")), days

    def test_units_run_in_threads_and_records_are_handed_over(self):
        """并发模式确实把单元放进线程池，且每个单元拉完立刻回调（边拉边落盘）。"""
        fetcher, days = self._fetcher_and_days()
        in_main_thread, collected = [], []

        def fake(unit, **_kw):
            in_main_thread.append(threading.current_thread() is threading.main_thread())
            return [{"id": unit.label}]

        with mock.patch.object(fetcher, "fetch_unit", side_effect=fake):
            stats, failures = fetcher.fetch_all(days, workers=2, window_retries=0, on_records=collected.extend)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(label for label, _ in stats), sorted(str(day) for day in days))
        self.assertEqual(len(collected), len(days))
        self.assertEqual(in_main_thread, [False] * len(days))

    def test_concurrent_failure_only_fails_its_own_unit(self):
        fetcher, days = self._fetcher_and_days()

        def fake(unit, **_kw):
            if unit.day.day == 17:
                raise ConnectionError("connection reset")
            return [{"id": 1}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=fake),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all(days, workers=2, window_retries=0)
        self.assertEqual([label for label, _ in failures], ["2026-09-17"])
        self.assertIn("connection reset", failures[0][1])
        self.assertEqual(sorted(label for label, _ in stats), ["2026-09-16", "2026-09-18"])

    def test_concurrent_fatal_error_aborts_run(self):
        """并发模式下 4xx 同样要立刻中止：不能变成"每个单元各失败一次"继续跑。"""
        fetcher, days = self._fetcher_and_days()

        def unauthorized(_unit, **_kw):
            raise utils.FatalApiError("HTTP 401：Unauthorized")

        with mock.patch.object(fetcher, "fetch_unit", side_effect=unauthorized):
            with self.assertRaises(utils.FatalApiError):
                fetcher.fetch_all(days, workers=2, window_retries=2)


class TestMcDefensiveBranches(OfflineTestCase):
    """mc 的边角：建表封装、SQL 心跳、取消失败、空结果计数。"""

    class Clock:
        """可编程时钟：按给定序列前进，用尽后停在最后一个值上。

        生产代码用 time.monotonic() 量经过时间（墙钟会被 NTP 校时拨动），
        假钟两个入口（time/monotonic）走同一序列。
        """

        def __init__(self, values):
            self.values = list(values)
            self.last = self.values[0] if self.values else 0.0

        def time(self):
            if self.values:
                self.last = self.values.pop(0)
            return self.last

        def monotonic(self):
            return self.time()

        def sleep(self, _seconds):
            pass

    @staticmethod
    def _odps(instance):
        class FakeODPS:
            def run_sql(self, sql):
                self.sql = sql
                return instance

        o = FakeODPS()
        return o

    def test_ensure_target_table_creates_then_verifies(self):
        """建表封装：先跑 create if not exists，再取回表对象校验结构并返回。"""
        table = FakeTable()
        o = mock.Mock()
        o.get_table.return_value = table
        instance = mock.Mock()
        instance.is_successful.return_value = True
        o.run_sql.return_value = instance
        got = mc_mod.ensure_target_table(
            o, "demo_project", "ods_x_json_df", "json", comment="备注", stored_as="aliorc", lifecycle_days=30
        )
        self.assertIs(got, table)
        ddl = o.run_sql.call_args.args[0]
        self.assertIn("create table if not exists demo_project.ods_x_json_df", ddl)
        self.assertIn("lifecycle 30", ddl)

    def test_ensure_target_table_rejects_mismatched_schema(self):
        """建表是一次性的，真正保命的是"取回后校验"：结构不符必须拒绝。"""
        o = mock.Mock()
        o.get_table.return_value = FakeTable(columns=[Col("raw"), Col("pt")])
        instance = mock.Mock()
        instance.is_successful.return_value = True
        o.run_sql.return_value = instance
        with self.assertRaises(SystemExit) as ctx:
            mc_mod.ensure_target_table(o, "demo_project", "ods_x", "json")
        self.assertIn("拒绝写入", str(ctx.exception))

    def test_count_partition_with_no_rows_returns_zero(self):
        """reader 一行都没读到（分区刚建好就查 count）：返回 0，不能抛 StopIteration，
        但要留一条警告——静默返回会把"没读到结果"与"分区确实 0 行"混为一谈。"""
        reader = mock.MagicMock()
        reader.__enter__.return_value = iter([])
        instance = mock.Mock()
        instance.is_successful.return_value = True
        instance.open_reader.return_value = reader
        logged: list = []
        with mock.patch.object(mc_mod, "log", logged.append):
            got = mc_mod.count_partition(self._odps(instance), "p", "t", "20260918")
        self.assertEqual(got, 0)
        self.assertTrue(any("没读到结果" in str(line) for line in logged), logged)

    def test_terminated_but_failed_raises_even_if_wait_does_not(self):
        """已结束但不是成功态：先 wait 一次（真实环境会带出 SQL 错误信息），再显式判一次成功性。

        不能只依赖 wait_for_success 抛错：它万一没抛（超时语义/实现差异），终止但失败的
        实例会被当成成功，后续 DDL 就在"其实没执行"的前提下继续跑（与 sftp2ods 同口径）。
        """
        instance = mock.Mock()
        instance.is_successful.return_value = False
        instance.is_terminated.return_value = True
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.run_sql_with_timeout(self._odps(instance), "select 1", timeout=5, desc="建表 t")
        self.assertIn("已终止但未成功", str(ctx.exception))
        instance.wait_for_success.assert_called_once_with(timeout=1)

    def test_timeout_reports_even_when_stop_fails(self):
        """取消（stop）自身失败也要照常报超时：卡住的查询不能因为取消失败就被放过。"""
        instance = mock.Mock()
        instance.is_successful.return_value = False
        instance.is_terminated.return_value = False
        instance.stop.side_effect = RuntimeError("stop 也失败了")
        with mock.patch.object(mc_mod, "time", self.Clock([0.0, 2.0])):
            with self.assertRaises(TimeoutError) as ctx:
                mc_mod.run_sql_with_timeout(self._odps(instance), "select 1", timeout=1, desc="校验行数")
        self.assertIn("校验行数 执行超过 1 秒", str(ctx.exception))
        instance.stop.assert_called_once()

    def test_heartbeat_logs_during_long_sql(self):
        """长时间执行的 SQL 要定期打心跳：调度里否则只看到"卡住不动"。"""
        instance = mock.Mock()
        instance.is_successful.side_effect = [False, True]
        instance.is_terminated.return_value = False
        with mock.patch.object(mc_mod, "time", self.Clock([0.0, 40.0])), mock.patch.object(mc_mod, "log") as log:
            mc_mod.run_sql_with_timeout(self._odps(instance), "select 1", timeout=600, desc="建表 ods_x")
        self.assertTrue(any("还在执行" in str(call) for call in log.call_args_list))


class TestCliDefensiveBranches(OfflineTestCase):
    """cli 的小工具函数：日志文件、凭证来源说明、运行锁路径。"""

    def test_open_log_file_creates_parents_and_none_when_absent(self):
        self.assertIsNone(cli_mod._open_log_file(""))  # 没给 --log-file：不打文件日志
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "run.log"
            handle = cli_mod._open_log_file(str(path))
            handle.write("x\n")
            handle.close()
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), "x\n")

    def test_open_log_file_reports_unwritable_parent(self):
        """父路径是个文件（日志目录名写重了）：给一句人话，而不是裸 OSError traceback。"""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "logs"
            blocker.write_text("占位", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                cli_mod._open_log_file(str(blocker / "run.log"))
        self.assertIn("--log-file 打不开", str(ctx.exception))

    def test_cred_source_label_names_both_files(self):
        """凭证来源说明只写位置、不含密钥；--config 存在时两个文件名都要出现。"""
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text("{}", encoding="utf-8")
            job_file = Path(tmp) / "demo.json"
            job_file.write_text("{}", encoding="utf-8")
            self.assertEqual(cli_mod._cred_source_label(config_file, job_file), "demo.json / config.json")
            self.assertEqual(cli_mod._cred_source_label(Path(tmp) / "无此文件.json", job_file), "demo.json")

    def test_lock_path_falls_back_to_tempdir(self):
        """工具目录与临时目录都建不了（只读安装）：退回系统临时目录，别让任务跑不起来。"""
        with mock.patch.object(Path, "mkdir", side_effect=OSError("只读文件系统")):
            path = cli_mod._lock_path(Path("jobs/demo.json"))
        self.assertEqual(path.parent, Path(tempfile.gettempdir()))
        self.assertTrue(path.name.startswith("api2ods-"))
        self.assertTrue(path.name.endswith(".lock"))


class TestRunSyncSetupAndFatalErrors(SyncFlowTestCase):
    """run_sync 的两侧出口：Fetcher 构造失败（配置错）与接口 4xx（不可重试）。"""

    def test_fetcher_setup_failure_returns_1_with_message(self):
        """signers.py 缺失/写错在构造 Fetcher 时就抛 SystemExit：要给人话日志 + 退出码 1。"""
        with mock.patch.object(self.cli, "Fetcher", side_effect=SystemExit("找不到自定义签名文件：signers.py")):
            rc = self.cli.run_sync(self.job, {}, self.config_path, make_args(), date(2026, 9, 20), self.job_path)
        self.assertEqual(rc, 1)
        self.cli.log.assert_any_call("❌ 找不到自定义签名文件：signers.py")
        self.odps.assert_not_called()
        self.written.assert_not_called()

    def test_fatal_api_error_skips_write_and_redacts(self):
        """4xx（密钥错/参数错）：不写库、不整窗重试，把接口给的原因脱敏后打出来。"""
        with mock.patch.object(
            self.cli.Fetcher, "fetch_all", side_effect=utils.FatalApiError("HTTP 401：token=abcdef123456")
        ):
            rc = self.cli.run_sync(self.job, {}, self.config_path, make_args(), date(2026, 9, 20), self.job_path)
        self.assertEqual(rc, 1)
        self.odps.assert_not_called()
        messages = " ".join(str(call) for call in self.cli.log.call_args_list)
        self.assertIn("不可重试", messages)
        self.assertNotIn("abcdef123456", messages)


class TestMainBranches(OfflineTestCase):
    """main() 的入口分支：--config、业务日来源、未知字段告警、--init 分发、Ctrl+C。"""

    @staticmethod
    def _write_job(tmp, payload=None) -> Path:
        path = Path(tmp) / "demo.json"
        path.write_text(json.dumps(payload or minimal_job()), encoding="utf-8")
        return path

    def test_config_file_is_loaded_and_passed_to_sync(self):
        """--config 指定的凭证文件要读进来并传给主流程（作业内没有凭证时的补充来源）。"""
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp)
            config_file = Path(tmp) / "creds.json"
            config_file.write_text(json.dumps({"profiles": {"default": {"project": "from_config"}}}), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=0) as sync,
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260918", "--config", str(config_file)])
        self.assertEqual(rc, 0)
        self.assertEqual(sync.call_args.args[1], {"profiles": {"default": {"project": "from_config"}}})

    def test_config_notify_merged_into_job_for_value_redaction(self):
        """共享 --config 文件的 notify.webhook 合并进 job：值级脱敏按 job 收集，
        不合并的话 config 里的 webhook（裸 hook id）在自由文本报错时会漏遮。"""
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp)
            config_file = Path(tmp) / "creds.json"
            config_file.write_text(
                json.dumps({"notify": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/shared-hook-id"}}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=0) as sync,
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260918", "--config", str(config_file)])
            self.assertEqual(rc, 0)
            merged = sync.call_args.args[0].get("notify") or {}
            self.assertIn("shared-hook-id", str(merged.get("webhook") or ""))
            # 值级脱敏能遮住 config 里的 webhook（模拟 notify 模块对完整 URL 脱敏）
            secret_values = utils.collect_secret_values(sync.call_args.args[0])
            self.assertIn("shared-hook-id", secret_values)

    def test_bizdate_comes_from_env_when_flag_absent(self):
        """调度里不给 --bizdate 时按环境变量 bizdate 走（DataWorks 的标准用法）。"""
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp)
            with (
                mock.patch.object(cli_mod, "run_sync", return_value=0) as sync,
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.dict(os.environ, {"bizdate": "20260918", "SKYNET_BIZDATE": ""}),
            ):
                rc = cli_mod.main(["--job", str(job_file)])
        self.assertEqual(rc, 0)
        self.assertEqual(sync.call_args.args[4], date(2026, 9, 18))

    def test_check_tolerates_dirty_env_bizdate(self):
        """--check 只读体检：调度环境变量脏了也要能看一眼（打警告后按默认业务日继续）。"""
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp)
            with (
                mock.patch.object(cli_mod, "run_check", return_value=0) as check,
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(dates_mod, "log") as warnings,
                mock.patch.dict(os.environ, {"bizdate": "20-09-18", "SKYNET_BIZDATE": ""}),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--check"])
        self.assertEqual(rc, 0)
        self.assertTrue(any("不是合法日期" in str(call) for call in warnings.call_args_list))
        # 容差断言：被测代码与断言各自读时钟，跨本地午夜（或时区用例改写 date_tz）时会差一天，
        # 精确相等在边界上是偶发 flaky
        expected = datetime.now(dates_mod.date_tz_of(minimal_job())).date() - timedelta(days=1)
        self.assertIn(check.call_args.args[4], (expected, expected - timedelta(days=1)))

    def test_unknown_job_key_is_warned_through_main(self):
        """拼错的配置项要有一条告警：JSON 没有注释，用户看不出这个字段没生效。"""
        payload = minimal_job()
        payload["request"]["timeout_second"] = 30  # 少了一个 s
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp, payload)
            with (
                mock.patch.object(cli_mod, "run_check", return_value=0),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "log") as log,
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260918", "--check"])
        self.assertEqual(rc, 0)
        messages = [str(call) for call in log.call_args_list]
        self.assertTrue(any("timeout_second" in m and "不是已知配置项" in m for m in messages))

    def test_init_flag_runs_wizard_and_logs_questions_only(self):
        """--init 的提问也走日志（--log-file 里能看到整套问答），但回答不落盘（可能是密钥）。"""
        captured = {}

        def fake_run_init(out, ask=None, echo=None, ask_secret=None):
            # 在 main 内部（仍在 with 块里）调一次真实的提问回调：验证问题进日志、回答不落盘
            captured.update(out=out, answer=ask("① 作业名"))
            return 0

        class Recorder:
            def __init__(self):
                self.lines = []

            def write(self, text):
                self.lines.append(text)

            def flush(self):
                pass

            def close(self):
                pass

        handle = Recorder()
        with (
            mock.patch.object(cli_mod, "_open_log_file", lambda p: handle),
            mock.patch.object(init_wizard, "run_init", side_effect=fake_run_init),
            mock.patch("builtins.input", return_value="demo"),
        ):
            rc = cli_mod.main(["--init", "--init-out", "jobs/demo.json"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["out"], "jobs/demo.json")
        self.assertEqual(captured["answer"], "demo")
        logged = "".join(handle.lines)
        self.assertIn("① 作业名", logged)  # 问题进日志：没有日志时像卡死
        self.assertNotIn("demo", logged)  # 回答不进日志：答案里可能是密钥
        self.assertNotIn(handle, utils._sinks)  # 句柄已摘掉，同一进程重复调用不串

    def test_init_stdin_closed_is_cancelled_not_traceback(self):
        """stdin 关闭（`--init < /dev/null`）时提问回调把 EOFError 翻译成取消，不冒裸 traceback
        ——与 sftp2ods / feishu2ods 的向导同口径。"""
        seen = {}

        def fake_run_init(out, ask=None, echo=None, ask_secret=None):
            try:
                ask("① 作业名")
            except EOFError as exc:
                seen["cancelled_by"] = str(exc)
                return 1
            return 0

        class Recorder:
            def write(self, text):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        with (
            mock.patch.object(cli_mod, "_open_log_file", lambda p: Recorder()),
            mock.patch.object(init_wizard, "run_init", side_effect=fake_run_init),
            mock.patch("builtins.input", side_effect=EOFError),
        ):
            rc = cli_mod.main(["--init", "--init-out", "jobs/demo.json"])
        self.assertEqual(rc, 1)
        self.assertIn("标准输入", seen["cancelled_by"])

    def test_init_secret_prompt_goes_through_getpass(self):
        """--init 的密钥提问走 getpass（不回显）：回答不落日志，问题仍留痕。"""
        captured = {}

        def fake_run_init(out, ask=None, echo=None, ask_secret=None):
            captured["answer"] = ask_secret("   Token 的值（输入不回显）")
            return 0

        class Recorder:
            def __init__(self):
                self.lines = []

            def write(self, text):
                self.lines.append(text)

            def flush(self):
                pass

            def close(self):
                pass

        handle = Recorder()
        with (
            mock.patch.object(cli_mod, "_open_log_file", lambda p: handle),
            mock.patch.object(init_wizard, "run_init", side_effect=fake_run_init),
            mock.patch("getpass.getpass", return_value="hidden-token") as gp,
        ):
            rc = cli_mod.main(["--init", "--init-out", "jobs/demo.json"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["answer"], "hidden-token")
        gp.assert_called_once_with("")  # 空 prompt：问题已经由 log() 打过，getpass 不再重复回显
        logged = "".join(handle.lines)
        self.assertIn("Token 的值", logged)  # 问题进日志
        self.assertNotIn("hidden-token", logged)  # 密钥不进日志

    def test_init_wizard_config_error_goes_through_log(self):
        """向导抛的配置错（如 --init-out 指到目录）也要带时间戳进日志、退出码 1。

        这一段在 main 的其它 try 之外：不接住的话消息只落到 stderr、--log-file 里一个字都没有。
        """

        def fake_run_init(out, ask=None, echo=None, ask_secret=None):
            raise SystemExit(f"--init-out 指向的是目录，需要给文件名：{out}")

        class Recorder:
            def __init__(self):
                self.lines = []

            def write(self, text):
                self.lines.append(text)

            def flush(self):
                pass

            def close(self):
                pass

        handle = Recorder()
        with (
            mock.patch.object(cli_mod, "_open_log_file", lambda p: handle),
            mock.patch.object(init_wizard, "run_init", side_effect=fake_run_init),
        ):
            rc = cli_mod.main(["--init", "--init-out", "jobs", "--log-file", "run.log"])
        self.assertEqual(rc, 1)  # 退出码不变（原来 SystemExit 冒泡出去也是 1）
        logged = "".join(handle.lines)
        self.assertIn("--init-out 指向的是目录", logged)  # 报错进了 --log-file
        self.assertNotIn(handle, utils._sinks)  # 句柄已摘掉

    def test_ctrl_c_before_sync_exits_130(self):
        """准备阶段（还没进 run_sync）被 Ctrl+C：走同一个硬退出出口，退出码 130。"""
        exits = []
        with tempfile.TemporaryDirectory() as tmp:
            job_file = self._write_job(tmp)
            with (
                mock.patch.object(cli_mod, "run_sync", side_effect=KeyboardInterrupt),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit", side_effect=exits.append),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920"])
        self.assertEqual(rc, 130)
        self.assertEqual(exits, [130])

    def test_wizard_file_stream_clears_pagination(self):
        """先选页码分页、再选文件流：分页要被清掉（否则生成的配置过不了自己的校验）。"""
        answers = iter(
            [
                "demo",
                "https://api.example.com/v1/export",
                "GET",
                "0",
                "",  # ①-⑤
                "1",
                "data.totalPages",
                "",  # ⑥ 分页=页码
                "0",  # ⑦ 窗口=不传时间
                "1",
                "csv",
                "n",  # ⑧ 文件流
                "proj",
                "tbl",
                "ak",
                "sk",
                "",  # ⑨ 目标与凭证
            ]
        )
        output: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            code = init_wizard.run_init(
                out_path=str(path),
                ask=lambda prompt="": next(answers, ""),
                ask_secret=lambda prompt="": next(answers, ""),
                echo=lambda *a: output.append(" ".join(str(x) for x in a)),
                workdir=Path(tmp),
            )
            self.assertEqual(code, 0)
            job = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("pagination", job)
        self.assertTrue(any("文件流不支持分页" in line for line in output))
        config_mod.validate_job(config_mod.normalize_job(job))  # 生成的配置必须能通过校验


# =============================================================================
# å¤å®¡ç¬¬ä¸è½®ï¼æ¬è½®ä¿®å¤çåå½ç¨ä¾


# =============================================================================
# 复审第七轮：本轮修复的回归用例
# =============================================================================


class TestSeventhPassDataPath(OfflineTestCase):
    """数据通路：空对象、双终点冲突、skip_until 引号、编码名。"""

    def _fetcher(self, job):
        return fetch_mod.Fetcher(job, Path("."))

    def _run(self, job, responses):
        it = iter(responses)
        with (
            mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: next(it)),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            return self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_empty_object_records_on_paginated_path(self):
        """分页路径上 records_path 落在空对象：是"这次没数据"，不是"一条空记录"。

        原来 {} 被包成 [{}] 写进 ODS（下游全取不到字段、行数校验还自洽），
        而且它让"零数据日"豁免失效，会一路空翻到 max_pages 才报错。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        self.assertEqual(self._run(job, [{"data": {"list": {}, "totalPages": 1}}]), [])

    def test_empty_object_on_later_page_keeps_real_records(self):
        """第 2 页回空对象：真记录保留，假记录不写进去。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        records = self._run(
            job, [{"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}}, {"data": {"list": {}, "totalPages": 2}}]
        )
        self.assertEqual(records, [{"id": 1}, {"id": 2}])

    def test_empty_object_with_zero_total_does_not_page_to_max(self):
        """空对象 + 总数 0：立刻按零数据日收尾，而不是空翻 max_pages 页。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.total",
                "max_pages": 5,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(1)
            return {"data": {"list": {}, "total": 0}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])
        self.assertEqual(len(calls), 1)

    def test_page_count_break_defers_to_total_items(self):
        """两个终点都配时，页数说翻完、条数说还差，要继续翻（否则静默少拉）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 100,
                "total_pages_path": "data.totalPages",
                "total_items_path": "data.totalItems",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": i} for i in range(50)], "totalPages": 2, "totalItems": 120}},
            {"data": {"list": [{"id": 100 + i} for i in range(50)], "totalPages": 2, "totalItems": 120}},
            {"data": {"list": [{"id": 200 + i} for i in range(20)], "totalPages": 2, "totalItems": 120}},
        ]
        self.assertEqual(len(self._run(job, responses)), 120)

    def test_page_count_break_still_applies_without_total_items(self):
        """只配页数终点时行为不变：到末页就收尾，不会多翻一页。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "max_pages": 5,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[2]["page"])
            return {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(records), 4)
        self.assertEqual(calls, [1, 2])

    def test_skip_until_inside_quoted_multiline_field(self):
        """skip_until 命中"引号里跨行字段"的内容时不算命中：从那里解析会把半行当表头。"""
        text = '汇总\n"明细里也出现了 序号 两个字\n继续",2\n序号,金额\n1,100\n2,200\n'
        rows = parsers._parse_text(text, {"format": "csv", "skip_until": "序号"}, label="t")
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0].keys()), ["序号", "金额"])

    def test_skip_until_still_matches_normal_marker(self):
        """正常形态不受影响：标记行真在表头位置时照常从它开始解析。"""
        text = "汇总\n序号,金额\n1,100\n"
        rows = parsers._parse_text(text, {"format": "csv", "skip_until": "序号"}, label="t")
        self.assertEqual(rows, [{"序号": "1", "金额": "100"}])

    def test_bad_encoding_name_is_config_error(self):
        """parse.encoding 写错（utf8sig）要给配置错，不能当网络抖动整窗重试。"""
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["parse"] = {"format": "csv", "encoding": "utf8sig"}
        calls = []

        def fake(*args, **kwargs):
            calls.append(1)
            return b"a,b\n1,2\n"

        with (
            mock.patch.object(http_mod, "request_once", side_effect=fake),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            with self.assertRaises(utils.ConfigError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("encoding", str(ctx.exception))
        self.assertEqual(len(calls), 1)  # 没有再发请求


class TestSeventhPassGuardrails(OfflineTestCase):
    """护栏：分页告警、pt 口径、lifecycle、cursor 终点。"""

    def _warnings(self, pagination):
        job = config_mod.normalize_job(minimal_job(pagination=pagination))
        config_mod.validate_job(job)
        return config_mod.collect_warnings(job)

    def test_size_only_pagination_warns(self):
        """只配 page_size 或只配 size_param 时同样要告警（原来要两者同时出现才告警）。"""
        for pagination in ({"page_size": 500}, {"size_param": "PageSize"}, {"size_param": "size", "page_size": 100}):
            self.assertTrue(self._warnings(pagination), f"{pagination} 应当告警（只会请求一次）")

    def test_cursor_without_total_path_warns(self):
        """cursor 模式没配 total_items_path 要告警：游标字段写错/接口中途不回游标时，
        "取不到游标"会被当成"翻完了"只拉第一页（静默少数据）。配了就真的会兜底校验。"""
        warnings = self._warnings({"type": "cursor", "cursor_param": "c", "cursor_path": "next"})
        self.assertTrue(any("total_items_path" in w for w in warnings), warnings)
        self.assertFalse(
            self._warnings(
                {"type": "cursor", "cursor_param": "c", "cursor_path": "next", "total_items_path": "data.total"}
            ),
            "配了 total_items_path 就不该再告警（它现在是 cursor 模式的兜底校验）",
        )

    def test_pt_must_be_business_day(self):
        """默认路径（target.pt / 业务日）只认 yyyyMMdd：别的形态写进去调度与 DWD 都读不到
        （等于静默丢数）；--pt 显式指定时放宽为合法分区名，测试/对比/补数用。"""
        job, _ = config_mod.render_job(minimal_job(), {"secrets": {}}, date(2026, 9, 18))
        for good in ("20260918", "test_20260921", "cmp_20260920", "backfill_20260101"):
            _p, _t, _c, pt = config_mod.resolve_target(job, {}, make_args(pt=good), date(2026, 9, 18))
            self.assertEqual(pt, good)
        # --pt 的值本身仍必须是合法分区名（斜杠/空格/换行一律拒）
        for bad in ("2026/09/18", "bad pt", "20260918\n"):
            with self.assertRaises(SystemExit):
                config_mod.resolve_target(job, {}, make_args(pt=bad), date(2026, 9, 18))
        # 不显式给 --pt 时，target.pt 写死成别的形态（含 ${bizdate_iso} 渲染结果）必须拒绝
        for bad_pt in ("2026-09-18", "2026-W36-1", "${bizdate_iso}"):
            bad_job, _ = config_mod.render_job(
                minimal_job(target={"project": "demo_project", "table": "t", "pt": bad_pt}),
                {"secrets": {}},
                date(2026, 9, 18),
            )
            with self.assertRaises(SystemExit):
                config_mod.resolve_target(bad_job, {}, make_args(), date(2026, 9, 18))

    def test_iso_week_bizdate_is_rejected(self):
        """--bizdate 不接受 ISO 周日期（3.11+ 的 fromisoformat 会静默解析成别的日子）。"""
        with self.assertRaises(SystemExit):
            dates_mod.parse_day_arg("2026-W36-1")
        self.assertEqual(dates_mod.parse_day_arg("20260918"), date(2026, 9, 18))
        self.assertEqual(dates_mod.parse_day_arg("2026-09-18"), date(2026, 9, 18))
        with self.assertRaises(SystemExit):
            dates_mod.parse_day_arg("2026-02-30")
        # 紧凑写法但日期不存在：不能抛裸 ValueError，要和 ISO 写法一样给"日期不存在"
        for bad in ("20261301", "20260230"):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.parse_day_arg(bad)
            self.assertIn("日期不存在", str(ctx.exception))

    def test_as_bool_blank_means_unset(self):
        """纯空白串按"没填"处理：原来会让 verify/strict 静默变成 False。"""
        for blank in ("", " ", "\t"):
            self.assertTrue(utils.as_bool(blank, default=True), repr(blank))
            self.assertFalse(utils.as_bool(blank, default=False), repr(blank))


class TestSeventhPassRedaction(OfflineTestCase):
    def test_url_userinfo_redacted(self):
        self.assertNotIn("SUPERSECRET", utils.redact("https://ak:SUPERSECRET@api.example.com/v1/x"))
        self.assertNotIn("SUPERSECRET", utils.redact("http://user:SUPERSECRET@proxy.example.com:8080"))

    def test_long_text_without_url_auth_skips_costly_scan(self):
        """URL userinfo 规则没有长度上限，长文本上会 O(n²) 贪婪回扫（40KB hex dump 实测 50 秒）。

        预判（同时含 "://" 与 "@" 才执行该规则）后，不含这两种字符的长文本应近线性。
        用 200KB 文本做冒烟：无预判时这会卡几十秒，预判后应在几秒内完成（断言只保下限）。
        """
        long_text = "abcdef0123456789" * 12000  # ~192KB，无 :// 无 @
        started = time.time()
        result = utils.redact(long_text)
        elapsed = time.time() - started
        self.assertEqual(result, long_text)
        self.assertLess(elapsed, 5.0, f"长文本脱敏耗时 {elapsed:.1f}s，疑似退化了（预判失效？）")

    def test_quote_inside_value_redacted(self):
        """值里含引号时 repr 会换一种引号包裹：按"遇到任意引号就停"会漏掉引号之后的内容。"""
        self.assertNotIn("SUPERSECRET", utils.redact("""{'password': "ab'SUPERSECRET"}"""))
        self.assertNotIn("SUPERSECRET", utils.redact("""{'password': 'ab"SUPERSECRET'}"""))

    def test_json_encoded_string_redacted(self):
        """接口把内层 JSON 当字符串返回时（异常里是 \\" 转义形态）同样要脱敏。"""
        text = '{"data": "{\\"token\\": \\"SUPERSECRET\\"}"}'
        self.assertNotIn("SUPERSECRET", utils.redact(text))

    def test_short_secret_param_names(self):
        for text in ("?pwd=SUPERSECRET", "?pw=SUPERSECRET", "?pass=SUPERSECRET", "bearer: SUPERSECRET"):
            self.assertNotIn("SUPERSECRET", utils.redact(text), text)

    def test_spaced_sensitive_value_masked_to_eol(self):
        """口令短语含空格（password=my secret）不能被第一个词截断：敏感键的值遮到行尾。"""
        out = utils.redact("login failed: password=my secret and more")
        self.assertNotIn("secret", out)
        self.assertNotIn("more", out)
        self.assertEqual(utils.redact("Invalid token: *** / ***"), "Invalid token: *** / ***")
        # 未闭合引号（日志截断）：KV/JSON 要收尾引号、常规 QUERY 不吃引号，必须走
        # 这条兜底，否则三套规则全绕过、明文泄露
        out = utils.redact('password="abc123456')
        self.assertNotIn("abc123456", out)
        out = utils.redact("password='abc123456")
        self.assertNotIn("abc123456", out)
        # 「带引号的键」+ 不带引号的值：KV/JSON 都不收，必须走 SPACE 兜底
        out = utils.redact('"password": my secret')
        self.assertNotIn("secret", out)

    def test_url_userinfo_password_with_at_sign(self):
        """userinfo 口令含 @（proxy 场景）要按最后一个 @ 切分：余段不能明文留下。"""
        out = utils.redact("HTTPS_PROXY=https://user:p@ss@proxy:8080")
        self.assertNotIn("p@ss", out)
        self.assertNotIn("ss@proxy", out)
        self.assertIn("user:***@", out)

    def test_quoted_value_after_key_redacted(self):
        """!r 插值/repr 形态（access_token='xxx'，行中）必须遮：query 规则的值部分不吃引号。"""
        out = utils.redact("请求失败 access_token='t-g1045SUPERSECRET' url=https://x")
        self.assertNotIn("t-g1045SUPERSECRET", out)
        self.assertIn("access_token='***'", out)
        out = utils.redact('fail: code=1, token: "SUPERSECRET123"')
        self.assertNotIn("SUPERSECRET123", out)
        # 键名不敏感、值里再嵌 k=v 的也要递归兜住
        out = utils.redact("note: 'access_token=SUPERSECRET123'")
        self.assertNotIn("SUPERSECRET123", out)

    def test_normal_content_still_untouched(self):
        for text in (
            "Content-Type: application/json",
            "?page=1&size=100",
            '{"Amount": 12.5}',
            "https://example.invalid/api/bill",
        ):
            self.assertEqual(utils.redact(text), text)

    def test_auth_error_does_not_echo_secret(self):
        """auth.params 写成数组时不能把里面的密钥回显出来（list 的 repr 任何规则都盖不住）。"""
        applier = auth_mod.AuthApplier({"auth": {"type": "query", "params": ["tok", "SUPERSECRET"]}}, Path("."))
        with self.assertRaises(utils.ConfigError) as ctx:
            applier.apply({}, {})
        self.assertNotIn("SUPERSECRET", str(ctx.exception))

    def test_custom_signer_error_is_redacted(self):
        """自定义签名函数抛出的异常文本要过脱敏（里面常带签名/密钥）。"""
        with tempfile.TemporaryDirectory() as tmp:
            signer = Path(tmp) / "signers.py"
            signer.write_text("def sign(params):\n    raise ValueError('token=SUPERSECRET')\n", encoding="utf-8")
            applier = auth_mod.AuthApplier(
                {"auth": {"type": "custom", "module": str(signer), "func": "sign"}}, Path(tmp)
            )
            with self.assertRaises(utils.ConfigError) as ctx:
                applier.apply({"a": 1}, {})
        self.assertNotIn("SUPERSECRET", str(ctx.exception))

    def test_check_header_values_rejects_bad_values(self):
        """头值首尾空白/换行/非 latin-1 要提前给配置错，且不回显值。"""
        for headers in ({"X-Api-Key": "SECRET\n"}, {"X-Api-Key": " SECRET"}, {"X-Api-Key": "中文"}):
            with self.assertRaises(utils.ConfigError) as ctx:
                utils.check_header_values(headers)
            self.assertIn("X-Api-Key", str(ctx.exception))
            self.assertNotIn("SECRET", str(ctx.exception))
        utils.check_header_values({"X-Api-Key": "ok-token-123"})
        utils.check_header_values({"X-Api-Key": b"ok-token-123"})
        with self.assertRaises(utils.ConfigError) as ctx:
            utils.check_header_values({"X-Api-Key": ["SECRET", "list"]})
        self.assertIn("X-Api-Key", str(ctx.exception))
        self.assertIn("字符串", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))


class TestSeventhPassRetryAndPlatform(OfflineTestCase):
    @unittest.skipUnless(_REQUESTS_AVAILABLE, "未安装 requests")
    def test_request_construction_error_is_config_error(self):
        """地址没写 scheme 这类"一个字节都没发出去"的错要给配置错，不能退避 23 分钟。"""
        job = minimal_job()
        job["request"]["base_url"] = "api.example.com"
        job["request"]["path"] = "/v1/bill"
        sleeps = []
        with mock.patch.object(http_mod.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            with self.assertRaises(utils.ConfigError) as ctx:
                fetch_mod.Fetcher(job, Path(".")).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("base_url", str(ctx.exception))
        self.assertEqual(sleeps, [])  # 一次退避都没有

    def test_illegal_strftime_is_config_error(self):
        """非法 strftime 指令要给中文配置错，而不是"Linux 原样输出、Windows 裸崩溃"。

        glibc 对不认识的指令是原样输出（会把 "%Q" 当参数值发给接口），MSVC 直接抛
        ValueError；白名单按两边都认的交集取，行为才能真正一致。
        """
        for fmt in ("%Q", "%-d", "%Y-%m-%d %", "%k"):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.format_time(datetime(2026, 9, 18, 12, 0, 0), fmt)
            self.assertIn("format", str(ctx.exception))

    def test_common_formats_survive_whitelist(self):
        """ "两个平台都认"的常见写法不能被白名单误伤。"""
        value = datetime(2026, 9, 18, 15, 30, 5)
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%b/%Y", "%e"):
            self.assertIsInstance(dates_mod.format_time(value, fmt), str)

    def test_pure_percent_escape_format_is_error(self):
        """纯 %% 转义构成的格式（"%%Y-%%m-%%d"）：参数恒等于字面量 "%Y-%m-%d"、
        静默查不到数据——一个真实时间指令都没有就报错。"""
        value = datetime(2026, 9, 18, tzinfo=timezone.utc)
        for bad in ("%%Y", "%%Y-%%m-%%d", "%%P"):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.format_time(value, bad)
            self.assertIn("没有任何时间指令", str(ctx.exception))

    def test_empty_extra_param_format_falls_back_like_format_time(self):
        """空格式串在 _moment 判定"只到日"、在 format_time 却回退成带时刻的默认格式：
        两边口径必须一致，否则跳过 api_tz 换算却输出完整时间戳（窗口整体错时差）。"""
        api_tz = dates_mod.parse_offset("+08:00")
        value = datetime(2026, 9, 18, 0, 0, tzinfo=dates_mod.load_zone("UTC"))
        got = dates_mod._moment(value, "", api_tz, field="window.extra_params['t']")
        self.assertEqual(got, "2026-09-18 08:00:00")  # 换算过：UTC 00:00 -> +08:00 08:00

    def test_is_date_only_format_handles_percent_escape(self):
        """%%H 是字面量不是时刻指令：不能被当成"带时刻格式"而多走一次时区换算。"""
        self.assertTrue(dates_mod.is_date_only_format("%Y-%m-%d%%H"))
        self.assertFalse(dates_mod.is_date_only_format("%Y-%m-%d %H:%M"))

    def test_literal_time_format_is_not_date_only(self):
        """把时间写成字面量（%Y-%m-%d 00:00:00）也要算"带时刻"：否则 pad_hours 被静默
        忽略、也不做 api_tz 换算。"""
        self.assertFalse(dates_mod.is_date_only_format("%Y-%m-%d 00:00:00"))
        self.assertTrue(dates_mod.is_date_only_format("%Y-%m-%d"))
        self.assertTrue(dates_mod.is_date_only_format("%Y-%m-%d-%H".replace("%H", "H")))

    def test_percent_s_and_P_work_cross_platform(self):
        """%s / %P 是 glibc 扩展，MSVC 不认：自己实现，跨平台一致。"""
        value = datetime(2026, 9, 18, 15, 30, 0)
        self.assertEqual(dates_mod.format_time(value, "%s"), int(value.timestamp()))
        self.assertEqual(dates_mod.format_time(value, "%P"), "pm")
        self.assertEqual(dates_mod.format_time(datetime(2026, 9, 18, 9, 0, 0), "%P"), "am")
        # %%P（字面量）与 %P 混用：只替换未转义的那个（str.replace 会把 %%P 也换掉、
        # 剩下 "%\x01" 让 strftime 抛 ValueError）。纯转义的 "%%P" 全无真实指令，
        # 见 test_pure_percent_escape_format_is_error：这里只测混用形态
        self.assertEqual(dates_mod.format_time(value, "%Y-%%P-%P"), "2026-%P-pm")
        self.assertEqual(dates_mod.format_time(value, "%%P-%P"), "%P-pm")

    def test_h_is_locale_free_like_b(self):
        """%h 与 %b 同义：平台 strftime 会按 LC_TIME 展开（"9月" vs "Sep"），必须自己接管。"""
        value = datetime(2026, 9, 18, 15, 30, 0)
        self.assertEqual(dates_mod.format_time(value, "%d-%h-%Y"), "18-Sep-2026")

    @unittest.skipUnless(_REQUESTS_AVAILABLE, "未安装 requests")
    def test_json_encoding_latin1_rejected(self):
        """json_encoding 配成 latin-1 会把任何字节解成乱码却解析成功——必须拒绝。"""
        response = mock.Mock()
        response.content = '{"名称": "交易"}'.encode()
        response.status_code = 200
        response.encoding = ""
        with mock.patch.object(http_mod.requests, "request", return_value=response):
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once(
                    "GET", "https://h/api", {}, {}, "json", 5, True, True, None, json_encoding="latin-1"
                )
        self.assertIn("json_encoding", str(ctx.exception))


class TestSeventhPassWriteStage(SyncFlowTestCase):
    def test_lifecycle_days_must_be_positive_int(self):
        """lifecycle_days 写 true/浮点/负数都要报错（int(True)==1 会让新表当天被回收）。"""
        for bad in (True, 365.9, -1, "365"):
            self.job["target"]["lifecycle_days"] = bad
            # 写库阶段的配置错以 SystemExit 冒泡（main 会把它转成"记日志 + 退出码 1"），
            # 关键是别带着错值去建表
            with self.assertRaises(SystemExit) as ctx:
                self.run_sync(records=[{"id": 1}])
            self.assertIn("lifecycle_days", str(ctx.exception))
            self.written.assert_not_called()


class TestSeventhPassInitAndLock(OfflineTestCase):
    def test_init_out_pointing_to_directory(self):
        """--init-out 指到目录时给人话，而不是 PermissionError 裸 traceback。"""
        mapping = {
            "作业名": "demo_api",
            "API 完整地址": "https://api.example.com/v1/items",
            "Token 的值": "tok123",
            "记录列表在返回": "data.list",
            "总页数字段路径": "data.totalPages",
            "每次回拉最近几天": "15",
            "AccessKeyId": "AKID",
            "AccessKeySecret": "SECRET",
        }
        ask = TestInitWizard._answers(mapping, ["1", "1", "1", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                init_wizard.run_init(out_path=tmp, ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp))
        self.assertIn("--init-out", str(ctx.exception))

    def test_lock_path_unwritable_is_friendly(self):
        """锁文件所在目录不在时给人话，而不是 FileNotFoundError 裸 traceback。"""
        with self.assertRaises(SystemExit) as ctx:
            with utils.RunLock(Path(tempfile.gettempdir()) / "no_such_dir_xyz" / "t.lock"):
                pass
        self.assertIn("锁文件", str(ctx.exception))


class TestEighthPassReview(OfflineTestCase):
    """第八轮复审：%s 混用、失败链路脱敏、latin-1 别名、锁文件 pid、TSV 裸引号、entry_field。"""

    def test_mixed_percent_s_and_P_are_cross_platform(self):
        """%s / %P 混在其它指令或文字里也要自己实现（原来只有整串 == "%s" 才自己算）。"""
        value = datetime(2026, 9, 18, 15, 30, 0)
        stamp = int(value.timestamp())
        self.assertEqual(dates_mod.format_time(value, "%Y-%s"), f"2026-{stamp}")
        # 只拿 ASCII 文字包夹：Windows 的 3.9-3.11 上 strftime 格式串要过 locale 编码，
        # 非 ASCII 会先抛 UnicodeEncodeError（3.12+ 才改），与本修复要验证的点无关
        self.assertEqual(dates_mod.format_time(value, "pre-%s-post"), f"pre-{stamp}-post")
        self.assertEqual(dates_mod.format_time(value, "%s%P"), f"{stamp}pm")
        self.assertEqual(dates_mod.format_time(value, "%s-%P"), f"{stamp}-pm")
        # 修复前：%Y-%s 在 Windows 抛 ValueError（裸 traceback、日志一个字没有），
        # glibc 则把 %s 展开成 epoch 秒——同一份配置两个平台两种行为
        am = datetime(2026, 9, 18, 9, 0, 0)
        self.assertEqual(dates_mod.format_time(am, "%H%P"), "09am")

    def test_percent_escape_pairing_matches_strftime(self):
        """%% 成对转义：%%%s 里第三个 % 开头的才是真指令（后行断言 (?<!%) 会漏掉）。"""
        value = datetime(2026, 9, 18, 15, 30, 0)
        stamp = int(value.timestamp())
        self.assertEqual(dates_mod.format_time(value, "%%%s"), f"%{stamp}")
        self.assertEqual(dates_mod.format_time(value, "%%%P"), "%pm")
        self.assertEqual(dates_mod.format_time(value, "%%%%%s"), f"%%{stamp}")
        self.assertEqual(dates_mod.format_time(value, "%Y-%%s-%%%s"), f"2026-%s-%{stamp}")
        # 扫描器自身的边界：结尾孤立 %（check_format_string 已拦，这里只保证不崩）
        self.assertEqual(dates_mod._protect_extension("%", "s", "<S>"), "%")
        self.assertEqual(dates_mod._protect_extension("%Y-%s%%", "s", "<S>"), "%Y-<S>%%")

    def test_window_params_accept_mixed_percent_s(self):
        """复审复现：window.format=%Y-%s 时构造窗口参数不能再抛 ValueError。"""
        win = {
            "mode": "per_day",
            "date_tz": "UTC",
            "api_tz": "UTC",
            "pad_hours": 0,
            "start_param": "s",
            "end_param": "e",
            "format": "%Y-%s",
        }
        got = dates_mod.window_param_sets({"window": win}, [date(2026, 9, 18)])
        self.assertEqual(len(got), 1)
        prefix, _, stamp = got[0]["s"].partition("-")
        self.assertEqual(prefix, "2026")
        self.assertTrue(stamp.isdigit())

    def test_dump_failure_message_is_redacted(self):
        """落盘报错里可能带着记录原文：先脱敏再拼进异常/日志。"""
        with self.assertRaises(RuntimeError) as ctx:
            spool_mod.dump_record({"access_token": "SECRET-abc123", "amount": float("inf")})
        message = str(ctx.exception)
        self.assertNotIn("SECRET-abc123", message)
        self.assertIn("***", message)

    def test_fetch_failure_log_and_list_are_redacted(self):
        """on_records（落盘）抛出的异常不经 run_unit，失败列表与日志都要脱敏。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        lines: list = []

        def spill(records):
            for record in records:
                spool_mod.dump_record(record)

        with (
            mock.patch.object(
                fetcher, "fetch_unit", return_value=[{"access_token": "SECRET-abc123", "amount": float("inf")}]
            ),
            mock.patch.object(fetch_mod, "log", lines.append),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=0, on_records=spill)
        self.assertEqual(stats, [])
        self.assertEqual(len(failures), 1)
        self.assertNotIn("SECRET-abc123", failures[0][1])
        self.assertTrue(lines)
        self.assertTrue(all("SECRET-abc123" not in line for line in lines), lines)

    @unittest.skipUnless(_REQUESTS_AVAILABLE, "未安装 requests")
    def test_latin1_aliases_all_rejected(self):
        """latin-1 的别名（latin_1 / iso_8859_1 / cp819 / L1…）行为一样，一个都不能漏。"""
        self.assertTrue(http_mod._is_latin1_alias("ISO_8859-1"))
        self.assertFalse(http_mod._is_latin1_alias("utf-8"))
        self.assertFalse(http_mod._is_latin1_alias("not-a-codec"))
        for alias in ("latin-1", "latin_1", "iso8859-1", "iso_8859_1", "cp819", "L1", "8859"):
            response = mock.Mock()
            response.content = b'{"x": 1}'
            response.status_code = 200
            response.encoding = ""
            with self.subTest(alias=alias), mock.patch.object(http_mod.requests, "request", return_value=response):
                with self.assertRaises(utils.ConfigError) as ctx:
                    http_mod.request_once(
                        "GET", "https://h/api", {}, {}, "json", 5, True, True, None, json_encoding=alias
                    )
            self.assertIn("json_encoding", str(ctx.exception))

    def test_invalid_window_format_rejected_at_config_time(self):
        """%Q / %-m 这类写法要在 validate_job 就报，不能拖到 --check 里伪装成请求失败。"""
        job = minimal_job()
        job["window"] = {"mode": "per_day", "format": "%Q"}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("window.format", str(ctx.exception))
        job["window"] = {"mode": "per_day", "format": "%Y-%m-%d", "extra_params": {"cycle": "%-m"}}
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("extra_params", str(ctx.exception))

    def test_entry_field_without_unzip_warns(self):
        """entry_field 只在 unzip 多条目合并时有来源：漏开 unzip 时告警而不是静默多写空字段。"""
        job = config_mod.normalize_job(minimal_job(parse={"format": "csv", "entry_field": "__file"}))
        config_mod.validate_job(job)
        warnings = config_mod.collect_warnings(job)
        self.assertTrue([w for w in warnings if "entry_field" in w], warnings)
        job = config_mod.normalize_job(minimal_job(parse={"format": "csv", "entry_field": "__file", "unzip": True}))
        config_mod.validate_job(job)
        self.assertEqual([w for w in config_mod.collect_warnings(job) if "entry_field" in w], [])

    @unittest.skipIf(utils.fcntl is None and utils.msvcrt is None, "本平台没有可用的文件锁")
    def test_blocked_lock_keeps_holders_pid(self):
        """被挡下的第二次启动不能把持锁进程的 pid 标记截没（"w" 模式会）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.lock"
            with utils.RunLock(path) as lock:
                ours = str(os.getpid())
                with self.assertRaises(SystemExit):
                    with utils.RunLock(path):
                        pass
                # Windows 上文件区域锁是强制性的，只能从持锁句柄读
                lock.fh.seek(0)
                self.assertEqual(lock.fh.read(), ours)

    def test_tsv_bare_quote_does_not_hide_marker(self):
        """TSV 字段中间的裸引号不是引号段的开始，不能让标记被误判成"在引号里"。"""
        text = 'a\tb\n1\t他说"好\n序号\t金额\n1\t100\n'
        rows = parsers._parse_text(text, {"format": "tsv", "skip_until": "序号"}, label="t")
        self.assertEqual(rows, [{"序号": "1", "金额": "100"}])

    def test_quote_state_matches_csv_module(self):
        """引号段状态与 csv 模块对齐：`"a"x` 里引号已收尾，后面的标记照样能找到。

        这条是收尾条件放宽的回归：原来要求 `"` 后必须紧跟分隔符/行尾，`"a"x,b`
        会一直卡在"引号内"，后面几行的 skip_until 全被无视。
        """
        for line, want_quoted in [
            ('"a"x,b', False),
            ('"a",b', False),
            ('"a""b",c', False),
            ('"a', True),
            ('a"b,c', False),
            ('"""', True),
        ]:
            _, quoted = parsers._marker_outside_quotes(line, "\u0000", False)
            self.assertEqual(quoted, want_quoted, line)
        text = '"a"x,b\n序号,金额\n1,100\n'
        rows = parsers._parse_text(text, {"format": "csv", "skip_until": "序号"}, label="t")
        self.assertEqual(rows, [{"序号": "1", "金额": "100"}])

    def test_collect_secret_values_covers_credentials_only(self):
        """值级脱敏的收集面：secrets / auth 凭证字段 / 密钥头 / MC key，不含结构性字段。"""
        job = {
            "secrets": {"tok": "SECRET-abc123", "ids": ["ID-987654"], "merchant": 809758},
            "request": {
                "auth": {
                    "type": "token",
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "value": "SECRET-abc123",
                    "sign_field": "sign",
                },
                "params": {"apiKey": "RPARAM-2222", "appId": "app-1"},
                "headers": {
                    "Authorization": "Bearer SECRET-abc123",
                    "X-Api-Key": "KEY-xyz987",
                    "Content-Type": "application/json",
                },
            },
            "maxcompute": {"access_key_secret": "MC-SECRET-0001", "project": "p"},
        }
        values = utils.collect_secret_values(job)
        self.assertIn("SECRET-abc123", values)
        self.assertIn("ID-987654", values)
        self.assertIn("KEY-xyz987", values)
        self.assertIn("MC-SECRET-0001", values)
        # request.params 里按参数名判断：apiKey 收、appId 不收
        self.assertIn("RPARAM-2222", values)
        # 数字型密钥按十进制字符串收（商户号类）
        self.assertIn("809758", values)
        self.assertNotIn("app-1", values)
        # Bearer 头拆出裸 token：接口常常只回显后半截
        self.assertIn("Bearer SECRET-abc123", values)
        # 结构性字段的值不进收集（sign_field="sign"、Content-Type、project）
        self.assertNotIn("sign", values)
        self.assertNotIn("application/json", values)
        self.assertNotIn("p", values)
        # query 型鉴权的凭证载体是 params（auth.py 读的就是这个名字）
        query_job = {"request": {"auth": {"type": "query", "params": {"token": "QTOK-000111"}}}}
        self.assertIn("QTOK-000111", utils.collect_secret_values(query_job))

    def test_redact_secrets_masks_free_text_echo(self):
        """接口把凭证写进自由文本（形态规则盖不住）时要按值遮掉，长的先替。"""
        values = ["SECRET-abc123", "SECRET-abc"]
        got = utils.redact_secrets(values, "Invalid token: SECRET-abc123 / SECRET-abc")
        self.assertNotIn("SECRET-abc", got)
        self.assertEqual(got, "Invalid token: *** / ***")
        # 形态级是另一道：没配置过的 Bearer 值照样被遮
        self.assertEqual(utils.redact_secrets([], "Authorization: Bearer sk-unknown"), "Authorization: ***")
        # 短值（<4）不参与值级替换：否则 "SEC" 会把别的密钥切成 "***RET-…"，
        # 既没遮住又搅乱报错信息；收集入口也会先滤掉
        self.assertEqual(utils.redact_secrets(["SEC"], "dup SECRET-abc123"), "dup SECRET-abc123")

    def test_redact_secrets_accepts_plain_string_and_non_str_values(self):
        """宽容度契约：values 传单个字符串不能被拆成单字符，传数字不能抛 TypeError。"""
        out = utils.redact_secrets("SECRET-abc123", "Invalid token: SECRET-abc123")
        self.assertNotIn("SECRET-abc123", out)
        self.assertTrue(utils.redact_secrets([123456789], "code=123456789").endswith("***"))

    def test_redact_secrets_masks_url_encoded_forms(self):
        """凭证以 URL 编码形态落进自由文本时也要遮：明文 / quote / quote_plus 三种形态一起替。

        接口/工具把凭证写进没有键名的自由文本时，形态规则挡不住；只替明文会漏，
        编码后的凭证（token 里的空格/斜杠被转义）会原样进日志。
        """
        secret = "SECRET value/with+plus"
        encoded = urllib.parse.quote(secret, safe="")
        encoded_plus = urllib.parse.quote_plus(secret)
        # 三种形态确实互不相同，用例才有意义
        self.assertNotIn(secret, (encoded, encoded_plus))
        self.assertNotEqual(encoded, encoded_plus)
        text = f"plain={secret} quote={encoded} plus={encoded_plus}"
        out = utils.redact_secrets([secret], text)
        self.assertNotIn(secret, out)
        self.assertNotIn(encoded, out)
        self.assertNotIn(encoded_plus, out)
        self.assertEqual(out.count("***"), 3)

    def test_redact_secrets_masks_aggressively_encoded_form(self):
        """部分编码器把 "-" 这类字符也编码成 %2D：该形态（旧注释里的例子）同样要遮。"""
        out = utils.redact_secrets(["t-abc123"], "url?data=t%2Dabc123 end")
        self.assertNotIn("t%2Dabc123", out)
        self.assertIn("***", out)

    def test_redact_secrets_masks_aggressively_encoded_non_ascii(self):
        """含中文的口令：激进编码变体按字节编码（%E5%AF%86，而不是 Latin-1 的 å…）。"""
        secret = "p@ss-密码"
        encoded = "p%40ss%2D%E5%AF%86%E7%A0%81"
        out = utils.redact_secrets([secret], f"url?data={encoded} end")
        self.assertNotIn(encoded, out)
        self.assertIn("***", out)

    def test_redact_secrets_tolerates_non_str_values(self):
        """数字密钥能遮；None/bool 跳过（str 化会把文本里的 None/True 误替成 ***）。"""
        out = utils.redact_secrets([12345, None, True], "count=None flag=True id=12345")
        self.assertIn("id=***", out)
        self.assertIn("count=None", out)
        self.assertIn("flag=True", out)

    def test_redact_survives_surrogate_secret(self):
        """含孤立代理字符的密钥（surrogateescape 路径名）：脱敏不能抛 UnicodeEncodeError。"""
        secret = "sk-abc" + chr(0xDCE9) + "xyz"
        out = utils.redact_secrets([secret], "err: " + secret + " end")
        self.assertIsInstance(out, str)
        self.assertNotIn(secret, out)

    def test_redact_secrets_accepts_bare_scalar_values(self):
        """values 直接传裸标量（数字/字符串，没有列表壳）也不能炸：与单字符串同口径。"""
        out = utils.redact_secrets(123456, "charge failed id=123456")
        self.assertNotIn("123456", out)

    def test_cli_job_redaction_masks_free_text_secret(self):
        """CLI 出口（run_check / run_sync / 外层 SystemExit）用作业上下文脱敏。"""
        job = {"secrets": {"tok": "SECRET-abc123"}}
        self.assertEqual(cli_mod._redact_job(job, "bad token SECRET-abc123"), "bad token ***")

    def test_cli_job_redaction_includes_config_secrets(self):
        """--config 独有的凭据（如 maxcompute.access_key_secret，作业文件里是占位符）也要遮：
        SDK 异常回显的 AK/密码来自 config 那份明文，只收作业文件的密钥值会漏遮。"""
        job = {"secrets": {"tok": "SECRET-abc123"}}
        config = {"maxcompute": {"access_key_secret": "CFG-SECRET-987654"}}
        out = cli_mod._redact_job(job, "boom CFG-SECRET-987654", config)
        self.assertNotIn("CFG-SECRET-987654", out)
        self.assertIn("***", out)

    def test_fetch_retry_log_and_failures_mask_free_text_secret(self):
        """run_unit 的重试日志与失败列表也走值级脱敏（配置里的密钥值回显）。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        job["secrets"] = {"tok": "SECRET-abc123"}
        fetcher = fetch_mod.Fetcher(job, Path("."))
        lines: list = []
        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=RuntimeError("接口回显 bad token SECRET-abc123")),
            mock.patch.object(fetch_mod, "log", lines.append),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=0)
        self.assertEqual(stats, [])
        self.assertEqual(len(failures), 1)
        self.assertNotIn("SECRET-abc123", failures[0][1])
        self.assertTrue(any("***" in line for line in lines), lines)
        self.assertTrue(all("SECRET-abc123" not in line for line in lines), lines)

    def test_http_retry_log_masks_free_text_secret_with_redactor(self):
        """http 层的重试日志在 fetch/cli 脱敏之前落盘：调用方传的 redactor 必须生效。

        复现是 500/503 类可重试错误把凭证写进自由文本：401 那条走的是
        FatalApiError 直抛、由 CLI 兜住；能重试的错误会在 http 里先打一行日志。
        """
        lines: list = []

        def boom():
            raise RuntimeError("HTTP 503：bad token SECRET-abc123")

        with mock.patch.object(http_mod, "log", lines.append), mock.patch.object(http_mod.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                http_mod.request_with_retry(
                    "GET",
                    "http://h/x",
                    boom,
                    "json",
                    5,
                    retry_times=1,
                    retry_delay=0,
                    redactor=lambda text: utils.redact_secrets(["SECRET-abc123"], text),
                )
        self.assertTrue(any("***" in line for line in lines), lines)
        self.assertTrue(all("SECRET-abc123" not in line for line in lines), lines)
        self.assertNotIn("SECRET-abc123", str(ctx.exception))


# =============================================================================
# 第九轮复审（v2.1.7）修复的回归用例
# =============================================================================


class TestNinthPassReview(OfflineTestCase):
    """第九轮复审：jsonl 错误体、布尔笔误、json_encoding 冲突、ZIP 确定性错误、
    标识符注入、count_partition 行取值、覆盖写入失败的后果提示、locale 相关指令。
    """

    # ---------------------------------------------------------------- jsonl 错误体
    def test_jsonl_unframed_json_is_treated_as_error_body(self):
        """接口 200 + JSON 错误体（不带行尾换行）不能被当成一条记录写进 ODS。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"code":500,"message":"internal error"}', {"format": "jsonl"}, "对账单")
        self.assertIn("JSON", str(ctx.exception))

    def test_jsonl_single_record_ambiguous_requires_opt_in(self):
        """单条 JSONL 记录与 JSON 错误体无法区分：末尾有没有换行都要显式放行。"""
        for payload in (b'{"id":1}', b'{"id":1}\n'):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(payload, {"format": "jsonl"}, "t")
            self.assertIn("allow_single_record", str(ctx.exception))

    def test_jsonl_allow_single_record_opt_in(self):
        """低流量源"整个响应就是一条 JSON 记录"：显式开 allow_single_record 放行。"""
        for payload in (b'{"id":1}', b'{"id":1}\n'):
            with self.subTest(payload=payload):
                got = parsers.parse_bytes(payload, {"format": "jsonl", "allow_single_record": True}, "t")
                self.assertEqual(got, [{"id": 1}])

    def test_jsonl_multi_record_without_trailing_newline_parses(self):
        """JSONL 最后一行允许没有换行：多记录响应不能被整包 JSON 检测误杀。"""
        got = parsers.parse_bytes(b'{"a":1}\n{"b":2}', {"format": "jsonl"}, "t")
        self.assertEqual(got, [{"a": 1}, {"b": 2}])

    def test_jsonl_error_body_with_trailing_newline_rejected(self):
        """错误体后面常被框架补一个换行，不能因为"看起来像 JSONL 行"就放行。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"code":500,"message":"internal error"}\n', {"format": "jsonl"}, "对账单")
        self.assertIn("allow_single_record", str(ctx.exception))

    def test_allow_single_record_is_a_known_key(self):
        """新开关要进 PARSE_KEYS：否则 collect_warnings 会把它当拼写错误天天告警。"""
        job = minimal_job(parse={"format": "jsonl", "allow_single_record": True})
        self.assertEqual(config_mod.collect_warnings(job), [])

    # ---------------------------------------------------------------- 布尔笔误
    def test_as_bool_rejects_non_01_numbers(self):
        """数字写法只认 0/1：NaN / 2.5 这类笔误报错（allow_empty: NaN 被当 True 会清空分区）。"""
        for bad in (2, 2.5, -1, float("nan")):
            with self.assertRaises(SystemExit) as ctx:
                utils.as_bool(bad, default=False, field="flag")
            self.assertIn("布尔值", str(ctx.exception))
        self.assertFalse(utils.as_bool(0, default=True, field="flag"))
        self.assertTrue(utils.as_bool(1, default=False, field="flag"))

    def test_as_bool_rejects_container_types(self):
        """数组/对象不能 bool() 兜底（[] 会被静默当成 False，绕过 fail-closed 约定）。
        0/1 仍按数字真值处理。"""
        self.assertFalse(utils.as_bool(0, default=True, field="flag"))
        self.assertTrue(utils.as_bool(1, default=False, field="flag"))
        for bad in ([], {}, ["x"]):
            with self.assertRaises(SystemExit) as ctx:
                utils.as_bool(bad, default=False, field="flag")
            self.assertIn("类型不支持", str(ctx.exception))

    def test_as_bool_unknown_string_is_config_error(self):
        """布尔写错（flase/ture/否）不能"未知一律当真"：allow_empty 冤枉清空分区。"""
        for bad in ("flase", "ture", "否", "2"):
            with self.subTest(bad=bad), self.assertRaises(utils.ConfigError) as ctx:
                utils.as_bool(bad, False, "target.allow_empty")
            self.assertIn("target.allow_empty", str(ctx.exception))
        for text, expected in (
            ("true", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
        ):
            self.assertEqual(utils.as_bool(text, not expected, "x"), expected)
        # 没填（None/空白）走默认值；JSON 里真正的布尔照旧
        self.assertTrue(utils.as_bool(None, True, "x"))
        self.assertTrue(utils.as_bool("   ", True, "x"))
        self.assertFalse(utils.as_bool(False, True, "x"))

    # ---------------------------------------------------------------- 配置校验
    def test_body_type_typo_rejected_at_config_time(self):
        """body_type 写 "from" 不能被"非 form 即 JSON"静默当成 JSON 发出去。"""
        job = minimal_job(request={"base_url": "https://api.example.com", "path": "/x", "body_type": "from"})
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("body_type", str(ctx.exception))
        ok = minimal_job(request={"base_url": "https://api.example.com", "path": "/x", "body_type": "form"})
        config_mod.validate_job(ok)

    def test_auth_required_fields_are_checked(self):
        """认证块少字段要给"缺哪个字段"，不能让接口回一句含糊的 401。"""
        cases = [
            ({"type": "basic", "username": "u"}, "auth.password"),
            ({"type": "basic", "password": "p"}, "auth.username"),
            ({"type": "token"}, "auth.value"),
            ({"type": "query"}, "auth.params"),
            ({"type": "sha256_concat"}, "auth.secret_key"),
        ]
        for auth, field in cases:
            with self.subTest(auth=auth):
                job = minimal_job(request={"base_url": "https://api.example.com", "path": "/x", "auth": auth})
                with self.assertRaises(SystemExit) as ctx:
                    config_mod.validate_job(job)
                self.assertIn(field, str(ctx.exception))
        # 字段齐了要能过
        for auth in (
            {"type": "basic", "username": "u", "password": "p"},
            {"type": "token", "value": "v"},
            {"type": "query", "params": {"k": "v"}},
            {"type": "sha256_concat", "secret_key": "sk"},
        ):
            config_mod.validate_job(
                minimal_job(request={"base_url": "https://api.example.com", "path": "/x", "auth": auth})
            )

    def test_validate_job_rejects_non_object_blocks_without_traceback(self):
        """直接调 validate_job（不先 normalize_job）时，块写成字符串也不能抛裸 AttributeError。"""
        for block in ("request", "target", "window", "pagination", "parse"):
            with self.subTest(block=block), self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(minimal_job(**{block: "oops"}))
            self.assertIn(f"{block} 必须是对象", str(ctx.exception))

    def test_parse_bool_typos_rejected_at_config_time(self):
        """parse 的布尔开关在解析阶段才 as_bool：写错要在这里就报，别等到运行时静默当没开。

        allow_single_record 只在"整包恰好一条 JSON"那条分支上检查，多记录 JSONL 配成
        "flase" 会一路静默；unzip/strict_encoding/allow_multi_entry 同理要在配置阶段挡住。
        """
        for key in ("unzip", "strict_encoding", "allow_multi_entry", "allow_single_record"):
            with self.subTest(key=key), self.assertRaises(utils.ConfigError) as ctx:
                config_mod.validate_job(minimal_job(parse={"format": "jsonl", key: "flase"}))
            self.assertIn(f"parse.{key}", str(ctx.exception))
        # 正常写法照旧通过
        config_mod.validate_job(
            minimal_job(
                parse={
                    "format": "jsonl",
                    "unzip": True,
                    "strict_encoding": "false",
                    "allow_multi_entry": "1",
                    "allow_single_record": "yes",
                }
            )
        )

    def test_numeric_config_ranges_checked_at_config_time(self):
        """page_size=0/-1、负数 delay、window.days=0 这类要在配置阶段报，别拖到发请求。"""
        cases = [
            ({"request": {"base_url": "https://h", "path": "/x", "retry_times": 1.5}}, "request.retry_times"),
            ({"request": {"base_url": "https://h", "path": "/x", "retry_delay": -1}}, "request.retry_delay"),
            ({"window": {"mode": "per_day", "days": 0}}, "window.days"),
            (
                {"pagination": {"type": "page", "page_param": "page", "page_size": 0, "total_pages_path": "d.n"}},
                "pagination.page_size",
            ),
            (
                {"pagination": {"type": "page", "page_param": "page", "page_size": -1, "total_pages_path": "d.n"}},
                "pagination.page_size",
            ),
            (
                {"pagination": {"type": "page", "page_param": "page", "max_pages": 0, "total_pages_path": "d.n"}},
                "pagination.max_pages",
            ),
            (
                {"pagination": {"type": "page", "page_param": "page", "delay_seconds": -5, "total_pages_path": "d.n"}},
                "pagination.delay_seconds",
            ),
            ({"target": {"project": "demo_project", "table": "ods_x", "lifecycle_days": 0}}, "lifecycle_days"),
            ({"target": {"project": "demo_project", "table": "ods_x", "lifecycle_days": "365"}}, "lifecycle_days"),
        ]
        for overrides, field in cases:
            with self.subTest(field=field):
                job = minimal_job(**overrides)
                with self.assertRaises(SystemExit) as ctx:
                    config_mod.validate_job(config_mod.normalize_job(job))
                self.assertIn(field, str(ctx.exception))

    def test_target_identifiers_are_whitelisted(self):
        """表名/列名/存储格式是拼进 DDL 的标识符：注入与拼错要在配置阶段挡下。"""
        for key, bad in (
            ("table", "ods_x; drop table y"),
            ("table", "ods-x"),
            ("project", "p 1"),
            ("column", "1json"),
            ("stored_as", "aliorc; drop"),
        ):
            with self.subTest(key=key):
                target = {"project": "demo_project", "table": "ods_x"}
                target[key] = bad
                job = minimal_job(target=target)
                with self.assertRaises(SystemExit) as ctx:
                    config_mod.validate_job(job)
                self.assertIn("标识符", str(ctx.exception))
        config_mod.validate_job(
            minimal_job(target={"project": "demo_project", "table": "ods_x", "column": "json", "stored_as": "aliorc"})
        )

    def test_mc_build_target_ddl_revalidates_identifiers(self):
        """mc.py 可能被单独调用：自己再校验一道，别只依赖 config.validate_job。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            mc_mod.build_target_ddl("p", "ods_x; drop table y", "json", "")
        self.assertIn("标识符", str(ctx.exception))
        with self.assertRaises(utils.ConfigError):
            mc_mod.build_target_ddl("p", "t", "json; drop", "")
        with self.assertRaises(utils.ConfigError):
            mc_mod.build_target_ddl("p", "t", "json", "", stored_as="aliorc; drop table z")

    # ---------------------------------------------------------------- 编码
    def test_utf8_body_with_wrong_json_encoding_is_config_error(self):
        """UTF-8 接口错配 gbk：解出的乱码键名会让下游全取空，必须报错而不是只打警告。"""
        response = mock.Mock()
        body = '{"名称": "交易"}'
        response.content = body.encode("utf-8")
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(response, "gbk")
        self.assertIn("json_encoding", str(ctx.exception))

    def test_gbk_body_with_gbk_json_encoding_accepted(self):
        """真的非 UTF-8 源（GBK 解出来和 utf-8-sig 不一致、但 utf-8-sig 解不出）照常放行。"""
        response = mock.Mock()
        response.content = '{"名称": "交易"}'.encode("gbk")
        self.assertEqual(http_mod._decode_json_body(response, "gbk"), '{"名称": "交易"}')

    def test_gbk_body_that_is_also_valid_utf8_trusts_explicit_encoding(self):
        """GBK 双字节序列恰好也是合法 UTF-8：不能误杀显式配置的 GBK 源，但要告警留痕。"""
        response = mock.Mock()
        response.content = '{"name": "一"}'.encode("gbk")
        warnings = []
        with mock.patch.object(http_mod, "log_once", side_effect=warnings.append):
            got = http_mod._decode_json_body(response, "gbk")
        self.assertEqual(got, '{"name": "一"}')
        self.assertTrue(any("utf-8-sig" in line for line in warnings))

    def test_utf16_body_not_falsely_rejected_as_encoding_conflict(self):
        """UTF-16 也能被 utf-8-sig"解出来"（含 \\x00），不能被冲突检查误杀。"""
        response = mock.Mock()
        response.content = '{"name": "ok"}'.encode("utf-16")
        self.assertEqual(http_mod._decode_json_body(response, "utf-16"), '{"name": "ok"}')

    # ---------------------------------------------------------------- ZIP
    @staticmethod
    def _zip_bytes(name: str, content: str) -> bytes:
        """造一个普通 ZIP（单个条目）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(name, content)
        return buffer.getvalue()

    @staticmethod
    def _zip_with_flags(name: str, content: str, encrypted: bool = False, compress_type: int | None = None) -> bytes:
        """造一个"加密"或"压缩方式不认识"的 ZIP（直接改头里的标志位/压缩方式）。"""
        import struct

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(name, content)
        data = bytearray(buffer.getvalue())
        local = data.index(b"PK\x03\x04")  # 本地文件头
        central = data.index(b"PK\x01\x02")  # 中央目录
        if encrypted:
            struct.pack_into("<H", data, local + 6, struct.unpack_from("<H", data, local + 6)[0] | 1)
            struct.pack_into("<H", data, central + 8, struct.unpack_from("<H", data, central + 8)[0] | 1)
        if compress_type is not None:
            struct.pack_into("<H", data, local + 8, compress_type)
            struct.pack_into("<H", data, central + 10, compress_type)
        return bytes(data)

    def test_encrypted_zip_entry_is_config_error(self):
        """加密条目：重试不会变好（同一份包），要按不可重试的配置错立刻失败。"""
        data = self._zip_with_flags("amount.csv", "user,amount\nu1,5\n", encrypted=True)
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(data, {"format": "csv", "unzip": True}, "导出")
        message = str(ctx.exception)
        self.assertIn("amount.csv", message)
        self.assertIn("读取失败", message)

    def test_unsupported_zip_compression_is_config_error(self):
        """压缩方式不认识（deflate64/PPMd 等）：同样是源侧问题，不能当网络抖动重试。"""
        data = self._zip_with_flags("amount.csv", "user,amount\nu1,5\n", compress_type=99)
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(data, {"format": "csv", "unzip": True}, "导出")
        self.assertIn("amount.csv", str(ctx.exception))

    def test_zip_entry_json_error_body_rejected(self):
        """ZIP 每个条目也要过 JSON 错误体检测：整包以 PK 开头，不能把错误体当数据。"""
        data = self._zip_bytes("export.jsonl", '{"code":500,"message":"upstream failed"}\n')
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(data, {"format": "jsonl", "unzip": True}, "导出")
        message = str(ctx.exception)
        self.assertIn("export.jsonl", message)
        self.assertIn("allow_single_record", message)

    def test_zip_entry_single_record_opt_in(self):
        """确认是单条 JSONL 记录的低流量导出：ZIP 条目同样按开关放行。"""
        data = self._zip_bytes("export.jsonl", '{"id":1}\n')
        got = parsers.parse_bytes(data, {"format": "jsonl", "unzip": True, "allow_single_record": True}, "导出")
        self.assertEqual(got, [{"id": 1}])

    # ---------------------------------------------------------------- count_partition
    def test_count_partition_accepts_tuple_rows(self):
        """reader 给元组行时不能走 row["cnt"]（tuple 也有 __getitem__）→ TypeError。"""

        class Instance:
            def is_successful(self):
                return True

            def is_terminated(self):
                return False

            def open_reader(self):
                return Reader()

        class Reader:
            def __enter__(self):
                return iter([("7",)])

            def __exit__(self, *exc_info):
                return False

        class FakeODPS:
            def run_sql(self, sql):
                return Instance()

        self.assertEqual(mc_mod.count_partition(FakeODPS(), "p", "t", "20260918"), 7)

    def test_count_partition_rejects_bad_identifier(self):
        """表名同样是拼进 SQL 的标识符：注入要在拼串之前挡下。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            mc_mod.count_partition(mock.Mock(), "p", "t where 1=1--", "20260918")
        self.assertIn("标识符", str(ctx.exception))

    # ---------------------------------------------------------------- 覆盖写入失败
    def test_write_partition_failure_warns_partition_may_be_gone(self):
        """先删后填中途失败：错误信息必须点明"分区可能缺数、要重跑"（旧数据不会自己回来）。"""
        table = FakeTable()
        table.fail_next_write = True  # 删/建成功，Tunnel 写入失败
        rows = [json.dumps({"i": 0})]
        with self.assertRaises(RuntimeError) as ctx:
            mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "20260918", lambda: iter([rows]), total=1, retries=1
            )
        message = str(ctx.exception)
        self.assertIn("可能已被清空", message)
        self.assertIn("重跑", message)
        self.assertEqual(table.deleted, [("pt=20260918", True)])  # 确实先删过

    def test_delete_partition_error_still_warns_partition_may_be_gone(self):
        """删分区自己抛异常时，服务端可能已经删了：不能吞掉缺数提示。"""
        table = FakeTable()
        rows = [json.dumps({"i": 0})]
        with (
            mock.patch.object(mc_mod, "drop_partition", side_effect=RuntimeError("delete timeout")),
            mock.patch.object(utils.time, "sleep"),
            self.assertRaises(RuntimeError) as ctx,
        ):
            mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "20260918", lambda: iter([rows]), total=1, retries=1
            )
        message = str(ctx.exception)
        self.assertIn("可能已被清空", message)
        self.assertIn("delete timeout", message)

    # ---------------------------------------------------------------- 日期格式
    def test_locale_dependent_directives_are_c_locale_english(self):
        """%b/%a/%c/%x/%X/%r/%p 由 locale 决定：固定成 C locale 英文，跨机器一致。"""
        value = datetime(2026, 9, 18, 15, 30, 5)
        self.assertEqual(dates_mod.format_time(value, "%d/%b/%Y"), "18/Sep/2026")
        self.assertEqual(dates_mod.format_time(value, "%a %A"), "Fri Friday")
        self.assertEqual(dates_mod.format_time(value, "%b %B"), "Sep September")
        self.assertEqual(dates_mod.format_time(value, "%c"), "Fri Sep 18 15:30:05 2026")
        self.assertEqual(dates_mod.format_time(value, "%x"), "09/18/26")
        self.assertEqual(dates_mod.format_time(value, "%X"), "15:30:05")
        self.assertEqual(dates_mod.format_time(value, "%r"), "03:30:05 PM")
        self.assertEqual(dates_mod.format_time(value, "%p"), "PM")

    def test_locale_dependent_directives_bypass_strftime(self):
        """这些指令不交给 strftime：否则中文/法语机器上 %b 会变「9月」「sept.」。"""

        class LocalizedDatetime(datetime):
            def strftime(self, fmt):  # 模拟"locale 变了"的平台 strftime
                return super().strftime(fmt).replace("Sep", "9月")

        value = LocalizedDatetime(2026, 9, 18)
        self.assertEqual(dates_mod.format_time(value, "%b"), "Sep")
        self.assertEqual(dates_mod.format_time(value, "%Y-%m-%d"), "2026-09-18")

    # ---------------------------------------------------------------- 配置文件编码
    def test_non_utf8_config_file_reports_encoding(self):
        """作业文件存成 GBK：要给人话（含路径与"请存成 UTF-8"），不是裸 UnicodeDecodeError。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_bytes('{"job": "中文作业"}'.encode("gbk"))
            with self.assertRaises(SystemExit) as ctx:
                config_mod.load_json_file(path, "作业配置文件")
        message = str(ctx.exception)
        self.assertIn("UTF-8", message)
        self.assertIn("job.json", message)


class TestTenthPassReview(OfflineTestCase):
    """第十轮复审修复的回归用例：脱敏回溯、cursor 兜底、分页参数同名、range 派生参数、重定向、URL 凭证。"""

    def _fetcher(self, job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_json_regex_backslash_run_is_fast(self):
        """反斜杠串不能触发 _JSON_RE 的指数回溯（修复前 38 个反斜杠要 20 秒）。

        触发方是不受控的第三方接口（400 错误体、或 200 但 records_path 不匹配），
        而 http/parsers 拼错误信息都会先截断到 300 字符——里面能装 ~290 个反斜杠，
        等于永不返回，且纯 Python 正则期间 Ctrl+C 也打断不了。
        """
        # 键名用非敏感词（"key" 在敏感词表里，未闭合值会被 SPACE 规则遮掉，与本用例
        # 要测的"反斜杠不爆炸"无关）
        text = '{"note":"' + "\\" * 80 + "tail"
        started = time.perf_counter()
        out = utils.redact(text)
        elapsed = time.perf_counter() - started
        self.assertEqual(out, text)
        self.assertLess(elapsed, 1.0, f"脱敏耗时 {elapsed:.1f}s，JSON 规则可能又出现回溯爆炸")

    def test_cursor_stops_early_with_total_is_error(self):
        """cursor 模式配了 total_items_path 时，游标提前结束但总数没拉够 → 报错。

        游标字段写错 / 接口某页不回游标时，"取不到游标"会被当成"翻完了"，
        只拉第一页就收尾（静默少数据，写后条数校验还自洽）。
        """
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "data.next",
                "total_items_path": "data.total",
                "delay_seconds": 0,
            }
        )
        payload = {"data": {"list": [{"id": 1}], "total": 9}}
        with mock.patch.object(http_mod, "request_once", return_value=payload):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("游标已结束", str(ctx.exception))

    def test_page_and_size_param_same_name_rejected(self):
        """page_param 与 size_param 同名：页大小会覆盖页码，接口只回同一页（重复行静默入库）。"""
        job = config_mod.normalize_job(
            minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "p",
                    "size_param": "p",
                    "total_pages_path": "pages",
                    "page_size": 10,
                }
            )
        )
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("同名", str(ctx.exception))

    def test_page_and_size_param_same_name_skipped_when_either_empty(self):
        """两个参数名都空（没走 normalize）不是同名覆盖，不能误拦。"""
        job = minimal_job()
        job["pagination"] = {"type": "page", "total_pages_path": "pages"}
        config_mod.validate_job(job)
        job["pagination"] = {"type": "page", "page_param": "p", "total_pages_path": "pages"}
        config_mod.validate_job(job)

    def test_range_extra_params_boundary_rejected(self):
        """range 模式下 extra_params 跨月：整段只发一次请求、派生参数只能取首日的值，
        后半段数据会静默拉不到——配置阶段直接拦下。"""
        window = {
            "mode": "range",
            "start_param": "startDate",
            "end_param": "endDate",
            "format": "%Y-%m-%d",
            "extra_params": {"BillingCycle": "%Y-%m"},
        }
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.window_param_sets(minimal_job(window=window), [date(2026, 8, 28), date(2026, 9, 3)])
        self.assertIn("extra_params", str(ctx.exception))
        # 不跨界的区间、以及 per_day 模式都不受影响
        dates_mod.window_param_sets(minimal_job(window=window), [date(2026, 8, 28), date(2026, 8, 31)])
        dates_mod.window_param_sets(
            minimal_job(window=dict(window, mode="per_day")), [date(2026, 8, 28), date(2026, 9, 3)]
        )

    def test_redirect_not_followed(self):
        """3xx 必须失败：跟随重定向会把 POST 降级成无 body 的 GET（窗口参数全丢），
        自定义鉴权头也会被转发到重定向目标。"""
        fake_requests = mock.Mock()
        fake_requests.request.return_value = FakeResponse(
            status=302, headers={"Location": "https://elsewhere.example/x"}, text=""
        )
        with mock.patch.object(http_mod, "requests", fake_requests):
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once("POST", "http://x", {}, {}, "json", 5, True, True, None)
        self.assertIn("重定向", str(ctx.exception))
        self.assertFalse(
            fake_requests.request.call_args.kwargs.get("allow_redirects", True),
            "必须显式 allow_redirects=False，否则 requests 会跟随重定向",
        )

    def test_url_query_secret_collected(self):
        """URL query 里的凭证（?appkey=xxx）要纳入值级脱敏：requests 的异常消息带完整 URL。"""
        job = {"request": {"base_url": "https://x/api", "path": "/bill?appkey=LITERAL-APPKEY-1234&start=1"}}
        values = utils.collect_secret_values(job)
        self.assertIn("LITERAL-APPKEY-1234", values)
        out = utils.redact_secrets(values, "Max retries exceeded with url: /bill?appkey=LITERAL-APPKEY-1234&start=1")
        self.assertNotIn("LITERAL-APPKEY-1234", out)


class TestEleventhPassReview(OfflineTestCase):
    """第十一轮复审（线上巡检评审）修复的回归用例。"""

    # ---------------------------------------------------------------- utils

    def test_require_identifier_rejects_non_string(self):
        """None/True 经 str() 会变成 "None"/"True" 这种"合法"标识符，必须直接拒绝。"""
        for bad in (None, True, 123):
            with self.assertRaises(utils.ConfigError):
                utils.require_identifier(bad, "target.table")

    def test_retry_call_rejects_non_positive_attempts(self):
        with self.assertRaises(ValueError):
            utils.retry_call(lambda: None, attempts=0, base_delay=0, desc="测试")

    def test_retry_call_chains_original_error(self):
        def always_fail():
            raise ConnectionError("boom")

        with self.assertRaises(RuntimeError) as ctx:
            utils.retry_call(always_fail, attempts=1, base_delay=0, desc="测试")
        self.assertIsInstance(ctx.exception.__cause__, ConnectionError)

    def test_retry_backoff_delays_are_requested(self):
        """基类把 sleep 换成空操作（离线用例不能真的等分钟级退避）；退避时长本身
        用记录调用的方式验证，避免"静音后从未被验证"。"""

        def always_fail():
            raise ConnectionError("boom")

        with mock.patch.object(utils.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                utils.retry_call(always_fail, attempts=4, base_delay=15, desc="测试", max_delay=300)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [15, 30, 60])

    def test_url_auth_regex_stays_linear(self):
        """_URL_AUTH_RE 的 scheme 部分必须限长：无上限时在长小写字母数字串上会 O(n²)
        （修复前 20KB 要 10 秒以上）。用相对判据（规模放大 4 倍不应接近 16 倍）规避机器差异。"""

        def elapsed(kb: int) -> float:
            n = kb * 1024
            text = "a" * n + "://" + "b" * n + "@"
            started = time.perf_counter()
            out = utils.redact(text)
            self.assertEqual(out, text)
            return time.perf_counter() - started

        small, big = elapsed(4), elapsed(16)
        self.assertLess(big, max(small * 8, 0.5), f"脱敏耗时 {small:.3f}s → {big:.3f}s，疑似 O(n²)")

    # ---------------------------------------------------------------- config

    def test_render_job_rejects_non_object_window_with_clean_error(self):
        job = minimal_job()
        job["window"] = "per_day"
        with self.assertRaises(utils.ConfigError) as ctx:
            config_mod.render_job(job, {}, date(2026, 9, 18))
        self.assertIn("window", str(ctx.exception))

    def test_collect_warnings_tolerates_non_object_blocks(self):
        """`or {}` 挡不住真值非对象（如 "window": ["a"]）：收集告警不阻断运行的契约要成立。"""
        job = minimal_job()
        job["window"] = ["per_day"]
        job["target"] = 5
        self.assertIsInstance(config_mod.collect_warnings(job), list)

    def test_profile_and_target_resolution_reject_non_object_target(self):
        job = minimal_job()
        job["target"] = "ods_x"
        with self.assertRaises(utils.ConfigError) as ctx:
            config_mod.get_mc_profile_meta({}, job, make_args())
        self.assertIn("target", str(ctx.exception))
        with self.assertRaises(utils.ConfigError):
            config_mod.resolve_target(job, {}, make_args(), date(2026, 9, 18))

    def test_backfill_without_bizdate_returns_bool(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(config_mod._backfill_without_bizdate(make_args(dates="2026-09-01")), True)
            self.assertIs(config_mod._backfill_without_bizdate(make_args()), False)

    def test_lifecycle_nan_rejected(self):
        job = minimal_job()
        job["target"]["lifecycle_days"] = float("nan")
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("lifecycle_days", str(ctx.exception))

    # ---------------------------------------------------------------- signers.example.py

    def _load_signers_example(self):
        path = Path(__file__).resolve().parents[1] / "signers.example.py"
        spec = importlib.util.spec_from_file_location("signers_example_for_tests", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_signers_example_raises_plain_errors_on_missing_config(self):
        """示例签名函数缺配置要抛普通异常（框架会包装成配置错）；抛 SystemExit 会绕过
        except Exception 的包装（BaseException），别让示例教错写法。"""
        signers = self._load_signers_example()
        with self.assertRaises(ValueError):
            signers.onerway_sign({"request": {}})  # 缺 params
        with self.assertRaises(ValueError) as ctx:
            signers.onerway_sign({"params": {}})  # 缺 request
        self.assertIn("request", str(ctx.exception))
        with self.assertRaises(ValueError):
            signers.onerway_sign({"params": {}, "request": {}})  # 缺 secret_key
        with self.assertRaises(ValueError):
            signers.xmp_sign({"request": {}})  # 缺 client_id/secret
        with self.assertRaises(ValueError) as ctx:
            signers.xmp_sign({"params": {}})  # 缺 request
        self.assertIn("request", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            signers.xmp_sign({"request": {"client_id": "cid"}})
        self.assertIn("secret_key", str(ctx.exception))

    def test_signers_example_xmp_sign_works(self):
        signers = self._load_signers_example()
        result = signers.xmp_sign({"request": {"client_id": "cid", "secret": "sec"}})
        self.assertEqual(result["params"]["client_id"], "cid")
        self.assertEqual(len(result["params"]["sign"]), 32)  # md5 十六进制

    # ---------------------------------------------------------------- fetch

    def test_stop_when_short_rejected_with_size_param_null(self):
        """size_param:null（不发页大小）+ stop_when_short：请求里没有页大小，接口按自己的
        默认值返回——第一页就会被误判成末页、静默少数据，配置错要在发请求前拦下。"""
        job = config_mod.normalize_job(
            minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "current",
                    "size_param": None,
                    "stop_when_short": True,
                    "delay_seconds": 0,
                }
            )
        )
        with self.assertRaises(utils.ConfigError) as ctx:
            fetch_mod.Fetcher(job, Path(".")).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("stop_when_short", str(ctx.exception))

    def test_config_error_in_unit_is_not_swallowed(self):
        """配置类错误与具体某天无关：要整轮上抛，不能被逐单元吞掉重复 N 遍。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s", "end_param": "e"})
        days = [date(2026, 9, 16), date(2026, 9, 17)]
        fetcher = fetch_mod.Fetcher(job, Path("."))
        with mock.patch.object(fetcher, "fetch_unit", side_effect=utils.ConfigError("配置错")):
            with self.assertRaises(utils.ConfigError):
                fetcher.fetch_all(days, workers=1, window_retries=3)
            with self.assertRaises(utils.ConfigError):
                fetcher.fetch_all(days, workers=2, window_retries=3)

    def test_on_records_receives_copy_so_retained_refs_survive(self):
        """并发分支在回调后会清空 future 持有的列表；回调必须拿到副本——
        调用方保留引用时不能被静默清空（传原列表会造成这种数据丢失）。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s", "end_param": "e"})
        days = [date(2026, 9, 16), date(2026, 9, 17)]
        fetcher = fetch_mod.Fetcher(job, Path("."))
        retained: list = []

        def fake(unit, **_kw):
            return [{"id": unit.label}, {"id": unit.label + "b"}]

        with mock.patch.object(fetcher, "fetch_unit", side_effect=fake):
            fetcher.fetch_all(days, workers=2, window_retries=0, on_records=retained.append)
        self.assertEqual(len(retained), 2)
        for batch in retained:
            self.assertEqual(len(batch), 2, "回调拿到的列表被后续清理清空了")

    # ---------------------------------------------------------------- parsers

    def test_skip_rows_rejects_float(self):
        """2.9 静默截断成 2 会让 skip_rows 少跳一行、表头整体错位。"""
        with self.assertRaises(utils.ConfigError):
            parsers._as_int(2.9, 0, "parse.skip_rows")

    def test_padded_blank_rows_are_skipped(self):
        """报表导出常见的 ",,,," 末尾填充行：整行全空白时按排版垃圾跳过，
        不能因为多出的 restkey 就硬报"列数多于表头"、让整个窗口反复失败。"""
        text = "a,b,c\n1,2,3\n,,,\n"
        records = parsers._parse_text(text, {"format": "csv", "delimiter": ","})
        self.assertEqual(records, [{"a": "1", "b": "2", "c": "3"}])

    def test_short_rows_are_rejected(self):
        """列数少于表头（文件被截断/换行未转义）：静默补 NULL 会让下游取值全空，必须报错。"""
        text = "a,b,c\n1,2\n"
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text(text, {"format": "csv", "delimiter": ","})
        self.assertIn("少于表头", str(ctx.exception))

    def test_json_error_body_with_trailing_newline_detected(self):
        """JSON 错误体后面带一个换行（nginx/框架常补）：尾字符判断要先 rstrip，
        否则错误体会被当数据解析（条数校验还自洽）。"""
        body = b'{"code": NaN}\n'
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(body, {"format": "csv"}, "d")
        self.assertIn("无法解析的 JSON", str(ctx.exception))

    # ---------------------------------------------------------------- mc / cli

    def test_pick_aksk_rejects_non_object(self):
        with self.assertRaises(SystemExit):
            mc_mod._pick_aksk("ak")

    def test_half_env_credentials_warns_and_continues(self):
        """只设了一半 ALIYUN_ACCESS_KEY_*：原先静默忽略、最终只说"找不到 AccessKey"，
        用户不知道自己设漏了；现在要留一条明确的告警。"""
        with mock.patch.dict(os.environ, {"ALIYUN_ACCESS_KEY_ID": "EA"}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(mc_mod.Path, "home", return_value=Path(tmp)):
                    with mock.patch.object(mc_mod, "log") as log:
                        with self.assertRaises(SystemExit):
                            mc_mod.load_mc_credentials({}, "作业")
        self.assertTrue(any("只设了" in str(call) for call in log.call_args_list), log.call_args_list)

    def test_count_partition_partition_value_whitelist(self):
        """分区值拼进 SQL 前必须过白名单（注入形态一律拒绝），且不发起任何查询。"""
        o = mock.Mock()
        for bad in ("2026/09/18", "x'; drop table t --", "", "a" * 65):
            with self.assertRaises(utils.ConfigError):
                mc_mod.count_partition(o, "p", "t", bad)
        o.run_sql.assert_not_called()

    def test_row_size_check_runs_once_before_delete(self):
        """尺寸校验在进入重试前只做一次（源数据不变），且早于任何删分区动作。"""
        table = FakeTable()
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return iter([["{}"]])

        written = mc_mod.write_partition(
            DdlOdps(table), table, "demo_project", "t", "20260918", factory, total=1, retries=3
        )
        self.assertEqual(written, 1)
        self.assertEqual(calls["n"], 2)  # 1 次写前校验 + 1 次写入（不再每轮重试各通读一遍）
        self.assertEqual(table.deleted, [("pt=20260918", True)])

    def test_as_count_rejects_bool_and_float(self):
        """window_retries: true 会被 int(True) 静默当成 1；2.5/nan 会被静默截断。"""
        for bad in (True, 2.5, float("nan")):
            with self.assertRaises(SystemExit):
                cli_mod._as_count(bad, 2, "pagination.window_retries")
        self.assertEqual(cli_mod._as_count("3", 2, "x"), 3)

    # ---------------------------------------------------------------- dates / http / auth

    def test_days_arg_error_points_to_cli(self):
        """--days 传非法值时，报错要指向 --days，不能打印 window.days=None 误导运维。"""
        with self.assertRaises(SystemExit) as ctx:
            dates_mod.resolve_days(make_args(days="两"), minimal_job(), bizdate=date(2026, 9, 18))
        self.assertIn("--days", str(ctx.exception))

    def test_extra_params_error_names_the_field(self):
        window = {"mode": "per_day", "start_param": "s", "end_param": "e", "extra_params": {"Billing": "%Q"}}
        with self.assertRaises(utils.ConfigError) as ctx:
            dates_mod.window_param_sets(minimal_job(window=window), [date(2026, 9, 18)])
        self.assertIn("extra_params['Billing']", str(ctx.exception))

    def test_day_arg_rejects_non_ascii_digits(self):
        """\\d 会把全角/阿拉伯-印度数字当合法日期（int() 也认），必须用 [0-9] 白名单拒绝。"""
        for bad in ("２０２６０９１８", "٢٠٢٦٠٩١٨"):
            with self.assertRaises(SystemExit):
                dates_mod.parse_day_arg(bad)

    def test_invalid_json_encoding_name_is_config_error(self):
        """编码名拼错（LookupError 被候选循环吞掉）原来会静默忽略显式配置。"""
        fake = mock.Mock()
        fake.content = b'{"a": 1}'
        fake.encoding = None
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(fake, "utf8sig")
        self.assertIn("json_encoding", str(ctx.exception))

    def test_normalize_compare_keeps_large_ints_exact(self):
        """>2^53 的整数（19 位订单号/纳秒时间戳）不能折叠成同一个浮点值：equals 漏判、not_equals 误杀。"""
        big, smaller = 9007199254740993, 9007199254740992  # 2^53+1 与 2^53
        self.assertEqual(http_mod._normalize_compare(big), big)
        self.assertNotEqual(http_mod._normalize_compare(big), http_mod._normalize_compare(smaller))
        # 小整数与字符串形态仍按数值归一（原有修复保持有效）
        self.assertEqual(http_mod._normalize_compare("200"), http_mod._normalize_compare(200))
        self.assertEqual(http_mod._normalize_compare("200"), http_mod._normalize_compare(200.0))

    def test_fail_if_missing_path_does_not_key_error(self):
        """fail_if 条件漏写 path：命中时报"业务错误"，而不是裸 KeyError（库调用方可能没走校验）。"""
        with self.assertRaises(utils.FatalApiError) as ctx:
            http_mod.check_fail_if({"x": 1}, [{"not_equals": 0}])
        self.assertIn("业务错误", str(ctx.exception))

    def test_auth_empty_credentials_rejected(self):
        cases = [
            ({"type": "bearer"}, "token"),
            ({"type": "token"}, "value"),
            ({"type": "basic", "username": "u"}, "password"),
            ({"type": "aliyun_rpc", "access_key_id": "ak"}, "access_key_secret"),
        ]
        for auth_cfg, field in cases:
            applier = auth_mod.AuthApplier({"auth": auth_cfg}, Path("."))
            with self.assertRaises(utils.ConfigError) as ctx:
                applier.apply({}, {})
            self.assertIn(field, str(ctx.exception))

    # ---------------------------------------------------------------- fieldwatch / notify / spool / wizard

    def test_snapshot_non_object_is_handled_as_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "demo.json"
            path = fieldwatch_mod.snapshot_path(job_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('["not", "an", "object"]', encoding="utf-8")
            self.assertIsNone(fieldwatch_mod.load_snapshot(job_path))

    def test_snapshot_updated_at_is_timezone_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "demo.json"
            fieldwatch_mod.save_snapshot(job_path, {"a", "b"}, "demo")
            payload = json.loads(fieldwatch_mod.snapshot_path(job_path).read_text(encoding="utf-8"))
        self.assertIsNotNone(datetime.fromisoformat(payload["updated_at"]).tzinfo)

    def test_notify_non_object_json_is_failure_not_crash(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return ["not", "a", "dict"]

        fake = mock.Mock()
        fake.post.return_value = FakeResp()
        with mock.patch.object(notify_mod, "requests", fake), mock.patch.object(notify_mod, "log"):
            self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]))

    def test_dump_record_rejects_unserializable_with_clean_error(self):
        """不可序列化的对象（set/datetime）抛的是 TypeError：也要走脱敏+中文报错，
        不能变成裸 traceback。"""
        with self.assertRaises(RuntimeError) as ctx:
            spool_mod.dump_record({"when": {1, 2}})
        self.assertIn("无法序列化", str(ctx.exception))

    def test_split_url_keeps_query_in_path(self):
        base, path = init_wizard._split_url("https://a.example.com/api?app=1&x=2")
        self.assertEqual(base, "https://a.example.com")
        self.assertEqual(path, "/api?app=1&x=2")
        self.assertIsNone(init_wizard._split_url("not a url"))

    def test_wizard_unexpected_runtime_error_propagates(self):
        """只把明确的 "lost sys.stdin" 当输入不可用；其它 RuntimeError 是真实缺陷，必须上抛。"""

        def ask(_prompt=""):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                init_wizard.run_init(
                    out_path=str(Path(tmp) / "w.json"),
                    ask=ask,
                    ask_secret=ask,
                    echo=lambda *a: None,
                    workdir=Path(tmp),
                )


class TestSpoolCloseFailure(SyncFlowTestCase):
    def test_close_failure_does_not_change_rc(self):
        """finally 里的清理失败（磁盘满/句柄异常）不能把已经确定的返回值顶掉：调度只看退出码。"""
        self.counted.return_value = 1
        real_close = spool_mod.SpoolWriter.close

        def failing_close(instance, keep=False):
            # 模拟"句柄已关、删除临时文件失败"的真实形态：句柄真的收掉，
            # 否则 Windows 上测试自己清理临时文件时会因文件仍被打开而失败
            real_close(instance, keep=True)
            raise OSError("disk full")

        with mock.patch.object(spool_mod.SpoolWriter, "close", failing_close):
            rc = self.run_sync(records=[{"id": 1}])
        self.assertEqual(rc, 0)


@unittest.skipUnless(_REQUESTS_AVAILABLE, "没装 requests")
class TestNotifyModule(OfflineTestCase):
    """api2ods.notify：发卡片、禁用/缺 webhook 跳过、失败不抛且不透出 hook id。"""

    def test_skip_when_disabled_or_missing_webhook(self):
        self.assertFalse(notify_mod.notify("", "t", ["x"]))
        self.assertFalse(notify_mod.notify("https://x/hook/abc", "t", ["x"], enabled=False))

    def test_success_payload(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"code": 0}
        with mock.patch.object(notify_mod.requests, "post", return_value=resp) as post:
            ok = notify_mod.notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc1234", "标题", ["行1"], footer="脚注"
            )
        self.assertTrue(ok)
        card = post.call_args.kwargs["json"]
        self.assertEqual(card["msg_type"], "interactive")
        self.assertIn("标题", card["card"]["header"]["title"]["content"])
        self.assertIn("行1", card["card"]["elements"][0]["text"]["content"])

    def test_false_and_float_zero_are_not_success_codes(self):
        """`False == 0`、`0.0 == 0` 都是真：布尔 false / 浮点 0 的失败响应不能当成功码。"""
        for payload in ({"code": False, "message": "invalid token"}, {"code": 0.0}):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertFalse(notify_mod.notify("https://x/hook/abc", "t", ["x"]), payload)

    def test_failure_does_not_raise_and_redacts_hook_id(self):
        messages: list[str] = []
        hook = "https://open.feishu.cn/open-apis/bot/v2/hook/deadbeef00cafe"
        with (
            mock.patch.object(notify_mod.requests, "post", side_effect=RuntimeError(f"Max retries: {hook}")),
            mock.patch.object(notify_mod, "log", side_effect=lambda msg: messages.append(str(msg))),
        ):
            self.assertFalse(notify_mod.notify(hook, "t", ["x"]))
        joined = "\n".join(messages)
        self.assertIn("飞书通知发送失败", joined)
        self.assertNotIn("deadbeef00cafe", joined)

    def test_missing_code_is_failure(self):
        """缺 code/StatusCode 的 200 响应不能算「已发送」：webhook 误填成其它接口
        （回 {"msg": "ok"} 这类）时会静默失效；仅空 {} 保留按 HTTP 200 判定的宽容。"""
        for payload, expected in (({"msg": "ok"}, False), ({}, True), ({"code": "0"}, True)):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertEqual(
                    notify_mod.notify("https://open.feishu.cn/open-apis/bot/v2/hook/abc1234", "t", ["x"]),
                    expected,
                    payload,
                )


class TestFieldWatch(OfflineTestCase):
    """字段快照：观察并集、读写往返、同名不同作业互不串。"""

    def test_observer_union(self):
        observer = fieldwatch_mod.FieldObserver()
        observer.update([{"a": 1}, {"b": 2, "a": 3}])
        observer.update([{"c": 4}])
        self.assertEqual(observer.fields, {"a", "b", "c"})

    def test_snapshot_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo.json"
            self.assertIsNone(fieldwatch_mod.load_snapshot(job))
            fieldwatch_mod.save_snapshot(job, ["b", "a"], "demo")
            self.assertEqual(fieldwatch_mod.load_snapshot(job), {"a", "b"})
            fieldwatch_mod.snapshot_path(job).write_text("{broken", encoding="utf-8")
            self.assertIsNone(fieldwatch_mod.load_snapshot(job))

    def test_same_named_jobs_do_not_share_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a" / "task.json"
            second = Path(tmp) / "b" / "task.json"
            first.parent.mkdir()
            second.parent.mkdir()
            fieldwatch_mod.save_snapshot(first, ["x"], "task")
            self.assertEqual(fieldwatch_mod.load_snapshot(first), {"x"})
            self.assertIsNone(fieldwatch_mod.load_snapshot(second))


class TestNotifyRedaction(OfflineTestCase):
    """webhook 是凭证：裸 hook id 纳入值级脱敏，/hook/<id> 形态也要遮。"""

    def test_webhook_hook_id_collected(self):
        job = {"notify": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/0123abcd-0000-4ef0"}}
        self.assertIn("0123abcd-0000-4ef0", utils.collect_secret_values(job))

    def test_redact_masks_hook_id_shape(self):
        text = "Max retries exceeded with url: /open-apis/bot/v2/hook/deadbeefcafe (Caused by ...)"
        self.assertNotIn("deadbeefcafe", utils.redact(text))


class TestRunSyncFieldDrift(SyncFlowTestCase):
    """run_sync：新增字段不阻塞、发提醒一次、快照只在写库成功后更新。"""

    def _sync(self, records, **overrides):
        self.counted.return_value = len(records)
        with mock.patch.object(self.cli, "notify") as notifier:
            code = self.run_sync(records=records, **overrides)
        return code, notifier

    def test_snapshot_save_failure_keeps_success(self):
        """写库已校验通过、字段快照写不进去（目录只读/磁盘满）只警告：不能把成功报成失败。"""
        with mock.patch.object(cli_mod.fieldwatch, "save_snapshot", side_effect=OSError("read-only")):
            code, _notifier = self._sync([{"record_id": "r1", "amount": "$1"}])
        self.assertEqual(code, 0)

    def test_new_fields_card_does_not_claim_written_in_dry_run(self):
        """--dry-run 的新字段卡片不能声称"数据已照常写入 ODS"：本次没写库，下游会误判分区就绪。"""
        base = {"record_id": "r1"}
        self._sync([base])
        code, notifier = self._sync([{**base, "extra": "1"}], dry_run=True)
        self.assertEqual(code, 0)
        text = "\n".join(notifier.call_args.args[2])
        self.assertIn("未写库", text)
        self.assertNotIn("已照常写入", text)

    def test_new_fields_notify_and_snapshot_update(self):
        base = {"record_id": "r1", "amount": "$1"}
        code, notifier = self._sync([base])
        self.assertEqual(code, 0)
        notifier.assert_not_called()  # 首次运行没有基线，不提醒

        code, notifier = self._sync([{**base, "coupon": "x"}])
        self.assertEqual(code, 0)
        notifier.assert_called_once()
        title, lines = notifier.call_args.args[1], notifier.call_args.args[2]
        self.assertIn("新增字段", title)
        self.assertIn("coupon", "\n".join(lines))

        code, notifier = self._sync([{**base, "coupon": "x"}])  # 快照已更新，不再提醒
        self.assertEqual(code, 0)
        notifier.assert_not_called()

    def test_dry_run_notifies_but_keeps_snapshot(self):
        base = {"record_id": "r1"}
        self._sync([base])
        code, notifier = self._sync([{**base, "extra": "1"}], dry_run=True)
        self.assertEqual(code, 0)
        notifier.assert_called_once()
        # dry-run 不落盘快照：下次正式跑仍会提醒（人工确认前不消音）
        code, notifier = self._sync([{**base, "extra": "1"}])
        self.assertEqual(code, 0)
        notifier.assert_called_once()

    def test_no_notify_flag_suppresses_but_still_calls_disabled(self):
        base = {"record_id": "r1"}
        self._sync([base])
        code, notifier = self._sync([{**base, "extra": "1"}], no_notify=True)
        self.assertEqual(code, 0)
        notifier.assert_called_once()
        self.assertFalse(notifier.call_args.kwargs["enabled"])

    def test_fetch_failure_skips_notify_and_snapshot(self):
        base = {"record_id": "r1"}
        self._sync([base])

        def failing(days, workers=1, window_retries=2, on_records=None):
            on_records([{**base, "extra": "1"}])
            return {}, [("2026-09-20", "接口超时")]

        before = fieldwatch_mod.load_snapshot(self.job_path)
        with (
            mock.patch.object(self.cli.Fetcher, "fetch_all", side_effect=failing),
            mock.patch.object(self.cli, "notify") as notifier,
        ):
            code = self.cli.run_sync(self.job, {}, self.config_path, make_args(), date(2026, 9, 20), self.job_path)
        self.assertEqual(code, 1)
        notifier.assert_not_called()
        self.assertEqual(fieldwatch_mod.load_snapshot(self.job_path), before)


class TestHarnessReviewFixes(OfflineTestCase):
    """harness 复审点：空 days、Cookie 脱敏、Retry-After 0、鉴权 ConfigError、Spool 关闭。"""

    def test_run_sync_empty_days_returns_1(self):
        job = config_mod.normalize_job(config_mod.render_job(minimal_job(), {}, date(2026, 9, 20))[0])
        logs = []
        with (
            mock.patch.object(cli_mod, "resolve_days", return_value=[]),
            mock.patch.object(cli_mod, "log", side_effect=lambda msg: logs.append(str(msg))),
        ):
            code = cli_mod.run_sync(job, {}, Path("config.json"), make_args(), date(2026, 9, 20), Path("demo.json"))
        self.assertEqual(code, 1)
        self.assertTrue(any("days 为空" in line for line in logs))

    def test_cookie_header_is_redacted(self):
        for text in ("Cookie: session=abc123secret", "Set-Cookie: sid=abc123secret; Path=/"):
            redacted = utils.redact(text)
            self.assertNotIn("abc123secret", redacted, text)
            self.assertIn("***", redacted)

    def test_retry_after_zero_is_immediate_retry(self):
        """Retry-After: 0 表示立刻重试，不能当成假值而改走默认退避。"""
        sleeps = []

        def once_then_ok(*_args, **_kwargs):
            if not sleeps:
                raise http_mod.RetryLater(0.0, "HTTP 429")
            return {"ok": True}

        with (
            mock.patch.object(http_mod, "request_once", side_effect=once_then_ok),
            mock.patch.object(http_mod.time, "sleep", side_effect=sleeps.append),
        ):
            payload = http_mod.request_with_retry(
                "GET", "https://x", lambda: ({}, {}), "json", 5, retry_times=2, retry_delay=15, desc="拉取"
            )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(sleeps, [0.0])

    def test_unknown_auth_type_is_config_error(self):
        applier = auth_mod.AuthApplier({"auth": {"type": "magic"}}, Path("."))
        with self.assertRaises(utils.ConfigError) as ctx:
            applier.apply({}, {})
        self.assertIn("未知鉴权类型", str(ctx.exception))

    def test_aliyun_rpc_pops_previous_signature(self):
        params = {"Action": "X", "Signature": "OLD_SIGNATURE"}
        expected = reference_aliyun_sign({"Action": "X"}, "AKID", "SECRET", "nonce123", "2026-09-18T00:00:00Z")
        got = auth_mod.sign_aliyun_rpc(params, "AKID", "SECRET", nonce="nonce123", timestamp="2026-09-18T00:00:00Z")
        self.assertEqual(got, expected)
        self.assertEqual(params["Signature"], expected)

    def test_spool_write_after_close_errors_clearly(self):
        spool = spool_mod.SpoolWriter()
        try:
            spool.write_records([{"a": 1}])
            spool.close(keep=True)
            with self.assertRaises(RuntimeError) as ctx:
                spool.write_records([{"b": 2}])
            self.assertIn("已关闭", str(ctx.exception))
        finally:
            spool.close()

    def test_xmp_sign_accepts_secret_key_alias(self):
        path = Path(__file__).resolve().parents[1] / "signers.example.py"
        spec = importlib.util.spec_from_file_location("signers_example_secret_key", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.xmp_sign({"request": {"client_id": "cid", "secret_key": "sec"}})
        self.assertEqual(result["params"]["client_id"], "cid")
        self.assertEqual(len(result["params"]["sign"]), 32)

    def test_ddl_comment_escapes_backslash_then_quotes(self):
        ddl = mc_mod.build_target_ddl("p", "t", "json", r"it's a \path")
        self.assertIn(r"'it''s a \\path'", ddl)

    def test_write_partition_rejects_illegal_partition_value(self):
        table = FakeTable()
        with self.assertRaises(utils.ConfigError) as ctx:
            mc_mod.write_partition(
                DdlOdps(table), table, "demo_project", "ods_x", "pt'; drop table x", lambda: iter([]), total=0
            )
        self.assertIn("分区值", str(ctx.exception))
        self.assertEqual(table.deleted, [])


class TestRemainingProductBugs(OfflineTestCase):
    """剩余产品缺陷回归：JSON BOM、ZIP 表头、fail_if、锁、分页、重试、日期空列表、通知码。"""

    def test_utf16_bom_json_error_body_detected(self):
        payload = '{"error": "no permission"}'.encode("utf-16")
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(payload, {"format": "csv", "encoding": "utf-16"}, "导出")
        self.assertIn("返回了 JSON", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(payload, {"format": "csv"}, "导出")
        self.assertIn("返回了 JSON", str(ctx.exception))

    def test_fail_if_falsey_non_list_rejected(self):
        for bad in ("", 0, {}, False):
            job = minimal_job()
            job["request"]["fail_if"] = bad
            with self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(job)
            self.assertIn("必须是数组", str(ctx.exception))

    def test_try_lock_unsupported_fails_closed_by_default(self):
        """文件系统不支持锁时默认拒绝执行（fail-closed）；显式 API2ODS_ALLOW_NO_LOCK=1
        才接受无互斥风险继续。"""
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB = 2, 4
        if not utils._LOCK_UNSUPPORTED:
            self.skipTest("platform has no ENOLCK/ENOTSUP")
        code = next(iter(utils._LOCK_UNSUPPORTED))
        fake_fcntl.flock.side_effect = OSError(code, "Operation not supported")
        with mock.patch.object(utils, "fcntl", fake_fcntl), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                utils._try_lock("fh")
        self.assertIn("ALLOW_NO_LOCK", str(ctx.exception))
        with (
            mock.patch.object(utils, "fcntl", fake_fcntl),
            mock.patch.dict(os.environ, {"API2ODS_ALLOW_NO_LOCK": "1"}, clear=True),
            mock.patch.object(utils, "log"),
        ):
            self.assertTrue(utils._try_lock("fh"))

    def test_try_lock_other_oserror_raises(self):
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB = 2, 4
        fake_fcntl.flock.side_effect = OSError(errno.EBADF, "Bad file descriptor")
        with mock.patch.object(utils, "fcntl", fake_fcntl):
            with self.assertRaises(OSError):
                utils._try_lock("fh")

    def test_lock_path_probe_failure_does_not_silently_fallback(self):
        logged = []
        job_path = Path("jobs/demo.json")
        with (
            mock.patch.object(tempfile, "mkstemp", side_effect=OSError(errno.ENOSPC, "No space left")),
            mock.patch.object(cli_mod, "log", side_effect=lambda msg: logged.append(str(msg))),
        ):
            path = cli_mod._lock_path(job_path)
        self.assertTrue(logged)
        self.assertIn("探测", "".join(logged))
        self.assertEqual(path.parent, cli_mod.ROOT / ".run-locks")

    def test_empty_size_param_blocks_stop_when_short(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "",
                "page_size": 100,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        with self.assertRaises(utils.ConfigError) as ctx:
            fetch_mod.Fetcher(job, Path(".")).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("stop_when_short", str(ctx.exception))

    def test_http_retry_skips_programming_errors(self):
        for exc in (AttributeError("x"), TypeError("x"), KeyError("x")):
            calls = []

            def boom(*_a, _exc=exc, **_k):
                calls.append(1)
                raise _exc

            with (
                mock.patch.object(http_mod, "request_once", side_effect=boom),
                mock.patch.object(http_mod.time, "sleep") as slept,
            ):
                with self.assertRaises(type(exc)):
                    http_mod.request_with_retry(
                        "GET", "http://x", lambda: ({}, {}), "json", 5, retry_times=3, retry_delay=0
                    )
            self.assertEqual(len(calls), 1, msg=type(exc).__name__)
            slept.assert_not_called()

    def test_empty_days_is_config_error(self):
        with self.assertRaises(utils.ConfigError) as ctx:
            dates_mod.window_param_sets({"window": {"start_param": "s", "end_param": "e"}}, [])
        self.assertIn("日期列表为空", str(ctx.exception))

    def test_notify_explicit_zero_code_is_success_null_is_not(self):
        """显式成功码（0 / "0" / StatusCode=0）才算成功；code 缺失或为 null 不能当成功——
        webhook 误填成其它接口（回 {"msg": "ok"}）时会「已发送」而告警静默失效。"""
        if not _REQUESTS_AVAILABLE:
            self.skipTest("没装 requests")
        for payload in ({"code": "0"}, {"StatusCode": 0}):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertTrue(notify_mod.notify("https://hook", "t", ["x"]), msg=payload)
        for payload in ({"code": None, "msg": "ok"}, {"msg": "ok"}):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertFalse(notify_mod.notify("https://hook", "t", ["x"]), msg=payload)

    def test_wizard_value_error_from_dumps_is_not_cancel(self):
        mapping = {"API 完整地址": "https://a.example.com/x", "AccessKeyId": "A", "AccessKeySecret": "S"}
        ask = TestInitWizard._answers(mapping, ["0", "0", "0", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(init_wizard.json, "dumps", side_effect=ValueError("circular")):
                with self.assertRaises(ValueError):
                    init_wizard.run_init(
                        out_path=str(Path(tmp) / "w.json"),
                        ask=ask,
                        ask_secret=ask,
                        echo=lambda *a: None,
                        workdir=Path(tmp),
                    )

    def test_snapshot_uses_unique_tmp_name(self):
        names = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            handle, name = real_mkstemp(*args, **kwargs)
            names.append(name)
            return handle, name

        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo.json"
            with mock.patch.object(tempfile, "mkstemp", side_effect=spy_mkstemp):
                fieldwatch_mod.save_snapshot(job, ["a"], "demo")
                fieldwatch_mod.save_snapshot(job, ["a", "b"], "demo")
        self.assertEqual(len(names), 2)
        snap = fieldwatch_mod.snapshot_path(job).name
        self.assertTrue(all(snap in Path(item).name for item in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
