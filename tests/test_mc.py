# -*- coding: utf-8 -*-
"""目标表、SQL 超时、写入与凭证。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    Col,
    DdlOdps,
    FakeTable,
    OfflineTestCase,
    make_args,
    minimal_job,
)

from api2ods import config as config_mod  # noqa: E402
from api2ods import mc as mc_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)


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
        self.assertTrue(table.writer_kwargs and all(kw.get("reopen") for kw in table.writer_kwargs))

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
        instance = self.FakeInstance(success=True)
        o = self.FakeODPS(instance)
        self.assertIs(mc_mod.run_sql_with_timeout(o, "select 1", timeout=5, desc="测试"), instance)

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
        o.get_table.assert_called_once_with("demo_project.ods_x_json_df")
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

    def test_count_partition_with_no_rows_fails(self):
        """空结果不能冒充零行，否则写空保护会放行并清掉已有数据。"""
        reader = mock.MagicMock()
        reader.__enter__.return_value = iter([])
        instance = mock.Mock()
        instance.is_successful.return_value = True
        instance.open_reader.return_value = reader
        with self.assertRaisesRegex(RuntimeError, "未返回行"):
            mc_mod.count_partition(self._odps(instance), "p", "t", "20260918")

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
