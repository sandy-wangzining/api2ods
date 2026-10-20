# -*- coding: utf-8 -*-
"""业务日解析与取数窗口。"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
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
from api2ods import (  # noqa: E402
    utils,
)


class TestResolveDays(OfflineTestCase):
    def test_bizdate_with_days(self):
        days = dates_mod.resolve_days(make_args(bizdate="20260918", days=3), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-16", "2026-09-17", "2026-09-18"])

    def test_env_bizdate(self):
        with mock.patch.dict(os.environ, {"bizdate": "20260918"}, clear=False):
            os.environ.pop("SKYNET_BIZDATE", None)
            days = dates_mod.resolve_days(make_args(), {})
        self.assertEqual([d.isoformat() for d in days], ["2026-09-18"])

    def test_default_bizdate_is_us_eastern(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(dates_mod, "default_bizdate", return_value=date(2026, 9, 18)),
        ):
            days = dates_mod.resolve_days(make_args(), {})
        self.assertEqual(days, [date(2026, 9, 18)])

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


class TestDatesErrorBranches(OfflineTestCase):
    """dates 的解析报错与 unix_ms 分支。"""

    def test_parse_offset_rejects_garbage(self):
        for text in ("oops", "08:00", ""):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.parse_offset(text)
            self.assertIn("无法解析时区偏移", str(ctx.exception))

    def test_parse_offset_rejects_out_of_range(self):
        """'+080' 不能被贪心拆成 0 小时 80 分（静默变成 +01:20），越界要报错。"""
        for text in ("+15:00", "+08:80", "+14:59", "-12:30", "-13:00"):
            with self.assertRaises(SystemExit) as ctx:
                dates_mod.parse_offset(text)
            self.assertIn("超出范围", str(ctx.exception))

    def test_parse_offset_accepts_real_world_bounds(self):
        self.assertEqual(dates_mod.parse_offset("+14:00").utcoffset(None), timedelta(hours=14))
        self.assertEqual(dates_mod.parse_offset("-12:00").utcoffset(None), timedelta(hours=-12))

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
