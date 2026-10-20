# -*- coding: utf-8 -*-
"""命令行主流程、补数、退出码与运行锁集成。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    Col,
    FakeTable,
    OfflineTestCase,
    SyncFlowTestCase,
    make_args,
    minimal_job,
)

from api2ods import cli as cli_mod  # noqa: E402
from api2ods import config as config_mod  # noqa: E402
from api2ods import dates as dates_mod  # noqa: E402
from api2ods import fieldwatch as fieldwatch_mod  # noqa: E402
from api2ods import (  # noqa: E402
    init_wizard,  # noqa: E402
    utils,
)
from api2ods import spool as spool_mod  # noqa: E402


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

    def test_workers_out_of_range_is_arg_error(self):
        """--workers 必须在 1~32：手误写 100 会创建大量线程、放大接口限流。"""
        import api2ods.cli as cli_mod

        for value in ("0", "-1", "33", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as ctx:
                    cli_mod.build_parser().parse_args(["--workers", value])
                self.assertEqual(ctx.exception.code, 2)

    def test_workers_range_ok(self):
        import api2ods.cli as cli_mod

        self.assertEqual(cli_mod.build_parser().parse_args(["--workers", "1"]).workers, 1)
        self.assertEqual(cli_mod.build_parser().parse_args(["--workers", "32"]).workers, 32)

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
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no_such_job.json"
            rc, lines = self._main_logging(["--job", str(missing)])
        self.assertEqual(rc, 1)
        self.assertIn("no_such_job.json", chr(10).join(lines))

    def test_version_flag_returns_zero(self):
        """--version 走正常退出语义；main 仍返回 int（嵌入调用不被 SystemExit 终止）。"""
        self.assertEqual(self._main(["--version"]), 0)

    def test_parse_error_returns_2(self):
        """argparse 阶段（未做任何操作）的参数错误按"参数问题"返回 2，不退化成 SystemExit。"""
        self.assertEqual(self._main(["--job", "x", "--sql-timeout", "-5"]), 2)

    def test_log_file_pointing_at_directory_reports_clearly(self):
        """--log-file 给成目录：要一句人话，而不是 IsADirectoryError 的裸 traceback。"""
        import api2ods.cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                cli_mod._open_log_file(tmp)
        self.assertIn("指向的是目录", str(ctx.exception))

    def test_log_file_open_failure_returns_2_not_systemexit(self):
        """--log-file 指向目录（打不开）属"参数问题"：main 必须返回 2，不能把 SystemExit
        冒泡出去——docstring 承诺返回 int，同进程多次调用 main 的宿主（测试/嵌入）会被
        SystemExit 直接终止（它继承 BaseException，宿主 except Exception 也拦不住）。"""
        import api2ods.cli as cli_mod

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cli_mod, "log") as spy:
                rc = cli_mod.main(["--job", str(Path(tmp) / "demo.json"), "--log-file", tmp])
        self.assertEqual(rc, 2)
        self.assertIn("指向的是目录", chr(10).join(str(c.args[0]) for c in spy.call_args_list))

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


class TestMainBranches(OfflineTestCase):
    """main() 的入口分支：--config、业务日来源、未知字段告警、--init 分发、Ctrl+C。"""

    @staticmethod
    def _write_job(tmp, payload=None) -> Path:
        path = Path(tmp) / "demo.json"
        path.write_text(json.dumps(payload if payload is not None else minimal_job()), encoding="utf-8")
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
                rc = cli_mod.main(["--job", str(job_file), "--bizdate", "20260920"], hard_exit=True)
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

    def test_lock_path_refuses_unsafe_last_resort(self):
        """两个目录都不可写时拒绝运行，不能把锁放到公共临时目录。"""
        with mock.patch.object(Path, "mkdir", side_effect=OSError("只读文件系统")):
            with self.assertRaisesRegex(SystemExit, "API2ODS_LOCK_DIR"):
                cli_mod._lock_path(Path("jobs/demo.json"))


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
