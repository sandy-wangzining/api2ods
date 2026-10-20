# -*- coding: utf-8 -*-
"""配置校验、占位符、字段与数值保护。"""

from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    make_args,
    minimal_job,
)

from api2ods import config as config_mod  # noqa: E402
from api2ods import dates as dates_mod  # noqa: E402
from api2ods import fetch as fetch_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    parsers,
    utils,
)


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

    def test_max_response_bytes_must_be_positive_int(self):
        for bad in (0, -1, 1.5, True, "abc"):
            with self.subTest(bad=bad):
                job = minimal_job()
                job["request"]["max_response_bytes"] = bad
                with self.assertRaises(SystemExit):
                    config_mod.validate_job(job)

    def test_allow_empty_type_checked_at_validate(self):
        """布尔笔误在配置阶段就报，不能等整轮 API 拉完、写库前才失败。"""
        job = minimal_job()
        job["target"]["allow_empty"] = "flase"
        with self.assertRaises(SystemExit) as ctx:
            config_mod.validate_job(job)
        self.assertIn("target.allow_empty", str(ctx.exception))

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

    def test_notify_webhook_array(self):
        job = minimal_job()
        job["notify"] = {"webhook": [" https://hook/a ", "https://hook/b", "https://hook/a"]}
        got = config_mod.normalize_job(job)
        config_mod.validate_job(got)
        self.assertEqual(got["notify"]["webhook"], ["https://hook/a", "https://hook/b"])

    def test_notify_webhook_array_bad_item(self):
        job = minimal_job()
        job["notify"] = {"webhook": ["https://hook/a", 123]}
        with self.assertRaises(SystemExit):
            config_mod.validate_job(job)

    def test_notify_webhook_array_placeholders_render(self):
        """数组里的 ${secrets.x} 占位符逐项渲染（多群地址可放 secrets）。"""
        job = minimal_job()
        job["secrets"] = {"hook_a": "https://a/hook/1", "hook_b": "https://b/hook/2"}
        job["notify"] = {"webhook": ["${secrets.hook_a}", "${secrets.hook_b}"]}
        rendered, _ = config_mod.render_job(job, {}, date(2026, 9, 18))
        config_mod.validate_job(rendered)
        self.assertEqual(rendered["notify"]["webhook"], ["https://a/hook/1", "https://b/hook/2"])

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

    def test_window_param_name_cannot_be_blank(self):
        """空串会被运行期当成"未配置"，只发一半时间边界、可能拉回全部历史。"""
        for key in ("start_param", "end_param"):
            job = minimal_job()
            job["window"] = {"mode": "per_day", "start_param": "startTime", "end_param": "endTime"}
            job["window"][key] = "   "
            with self.subTest(key=key), self.assertRaises(SystemExit) as ctx:
                config_mod.validate_job(config_mod.normalize_job(job))
            self.assertIn(key, str(ctx.exception))

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

    def test_decimal_string_is_rejected_as_integer(self):
        """'2.0'/'1e3' 能通过 float() 的整值判断，但运行期 int() 会崩：配置阶段必须拦下。"""
        for value in ("2.0", "1e3", " 3.5 "):
            with self.assertRaises(SystemExit) as ctx:
                config_mod._require_number(value, "pagination.page_size", exclusive_min=0, integer=True)
            self.assertIn("pagination.page_size", str(ctx.exception))

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
