# -*- coding: utf-8 -*-
"""历次代码审查回归用例。"""

from __future__ import annotations

import argparse
import errno
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
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    _REQUESTS_AVAILABLE,
    DdlOdps,
    FakeResponse,
    FakeTable,
    OfflineTestCase,
    SyncFlowTestCase,
    make_args,
    minimal_job,
    reference_aliyun_sign,
    wizard_answers,
)

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
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "1"], hard_exit=True)
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
                rc = cli_mod.main(
                    ["--job", str(job_file), "--bizdate", "20260920", "--days", "3", "--workers", "3"], hard_exit=True
                )
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

    def test_embedded_call_returns_code_without_hard_exit(self):
        """库/嵌入调用（默认 hard_exit=False）：即使并发失败也只返回 int，不终止宿主进程。"""
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
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--days", "3", "--workers", "3"])
        self.assertEqual(rc, 1)
        exited.assert_not_called()

    def test_hard_exit_survives_missing_standard_streams(self):
        """sys.stdout/stderr 为 None（pythonw 等）时 flush 抛 AttributeError，硬退出仍要生效。"""
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
                mock.patch.object(cli_mod.sys, "stdout", None),
                mock.patch.object(cli_mod.sys, "stderr", None),
            ):
                rc = cli_mod.main(
                    ["--job", str(job_file), "--bizdate", "20260920", "--days", "3", "--workers", "3"], hard_exit=True
                )
        self.assertEqual(exits, [1])
        self.assertEqual(rc, 1)

    def test_unexpected_error_from_run_sync_exits_hard(self):
        """run_sync 抛未预期异常时也要走脱敏日志 + 硬退出，不能裸 traceback 冒泡。"""
        import api2ods.cli as cli_mod

        exits = []
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "demo.json"
            job_file.write_text(json.dumps(minimal_job()), encoding="utf-8")
            with (
                mock.patch.object(cli_mod, "run_sync", side_effect=RuntimeError("boom")),
                mock.patch.object(cli_mod, "_open_log_file", lambda p: None),
                mock.patch.object(cli_mod, "_lock_path", lambda p: Path(tmp) / "t.lock"),
                mock.patch.object(cli_mod.os, "_exit", side_effect=exits.append),
                mock.patch.object(cli_mod, "log", side_effect=lambda msg: messages.append(str(msg))),
            ):
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920", "--workers", "3"], hard_exit=True)
        self.assertEqual(exits, [1])
        self.assertEqual(rc, 1)
        self.assertTrue(any("未预期错误" in msg for msg in messages))

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
        ask = wizard_answers(mapping, ["1", "1", "1", "0"])
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
            "profiles": {"prod": {"access_key_id": "PROF-AK-0001", "access_key_secret": "PROF-SK-0002"}},
        }
        values = utils.collect_secret_values(job)
        self.assertIn("SECRET-abc123", values)
        self.assertIn("ID-987654", values)
        self.assertIn("KEY-xyz987", values)
        self.assertIn("MC-SECRET-0001", values)
        self.assertIn("PROF-AK-0001", values)
        self.assertIn("PROF-SK-0002", values)
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
        # 小写十六进制的百分号编码（%2f）同样要遮
        lower_encoded = re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), encoded)
        self.assertNotEqual(lower_encoded, encoded)
        self.assertNotIn(lower_encoded, utils.redact_secrets([secret], f"lower={lower_encoded}"))
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
        """UTF-8 接口（响应头明确声明 charset=utf-8）错配 gbk：解出的乱码键名会让下游全取空，
        必须报错而不是只打警告。"""
        response = mock.Mock()
        body = '{"名称": "交易"}'
        response.content = body.encode("utf-8")
        response.headers = {"Content-Type": "application/json; charset=utf-8"}
        with self.assertRaises(utils.ConfigError) as ctx:
            http_mod._decode_json_body(response, "gbk")
        self.assertIn("json_encoding", str(ctx.exception))

    def test_explicit_encoding_without_header_charset_is_trusted(self):
        """响应头没声明 charset（requests 的 encoding 只是默认值）：显式配置是用户最强信号。"""
        response = mock.Mock()
        response.content = '{"一": 1}'.encode("gbk")
        response.headers = {"Content-Type": "application/json"}
        with mock.patch.object(http_mod, "log_once"):
            self.assertEqual(http_mod._decode_json_body(response, "gbk"), '{"一": 1}')

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

    def test_cursor_cycle_fails_before_accumulating_repeated_pages(self):
        job = minimal_job(
            pagination={"type": "cursor", "cursor_param": "c", "cursor_path": "data.next", "delay_seconds": 0}
        )
        pages = [{"data": {"list": [{"id": i}], "next": cursor}} for i, cursor in enumerate(("a", "b", "a"))]
        with mock.patch.object(http_mod, "request_once", side_effect=pages) as request:
            with self.assertRaisesRegex(utils.ConfigError, "循环"):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(request.call_count, 3)

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
        self.assertIsInstance(ctx.exception, fetch_mod.UnconfirmedEndError)
        with mock.patch.object(http_mod, "request_once", return_value=payload):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertTrue(label)
        self.assertEqual(count, 0)

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
        errors = []

        def run():
            try:
                results.append(str(cli_mod._lock_path(job_path)))
            except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须回传主线程
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)  # 线程异常时 results 会变少，不能只比 set
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
                rc = cli_mod.main(
                    ["--job", str(job_file), "--bizdate", "20260920", "--days", "1", "--workers", "3"], hard_exit=True
                )
        self.assertEqual(exits, [1])
        self.assertEqual(rc, 1)


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

    def test_missing_lock_module_fails_closed(self):
        """平台没有 fcntl/msvcrt 时默认拒绝无锁执行；显式 API2ODS_ALLOW_NO_LOCK=1 才放行。"""
        with mock.patch.object(utils, "fcntl", None), mock.patch.object(utils, "msvcrt", None):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    utils.RunLock(Path("x.lock")).__enter__()
                self.assertIn("ALLOW_NO_LOCK", str(ctx.exception))
            with (
                mock.patch.dict(os.environ, {"API2ODS_ALLOW_NO_LOCK": "1"}, clear=True),
                mock.patch.object(utils, "log"),
            ):
                lock = utils.RunLock(Path("x.lock"))
                self.assertIs(lock.__enter__(), lock)
                lock.__exit__(None, None, None)

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
        ask = wizard_answers(mapping, ["0", "0", "0", "0"])
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
