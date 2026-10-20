# -*- coding: utf-8 -*-
"""--init 向导。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    wizard_answers,
)

from api2ods import config as config_mod  # noqa: E402
from api2ods import dates as dates_mod  # noqa: E402
from api2ods import (  # noqa: E402
    init_wizard,  # noqa: E402
)


class TestInitWizard(OfflineTestCase):
    _answers = staticmethod(wizard_answers)

    def test_generated_default_table_name_is_valid_identifier(self):
        """作业名带 "-" 时默认表名要替换成 "_"，否则生成一份过不了校验的配置。"""
        mapping = {
            "作业名": "demo-api",
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
            code = init_wizard.run_init(
                out_path=str(Path(tmp) / "demo-api.json"),
                ask=ask,
                ask_secret=ask,
                echo=lambda *a: None,
                workdir=Path(tmp),
            )
            self.assertEqual(code, 0)
            job = json.loads((Path(tmp) / "demo-api.json").read_text(encoding="utf-8"))
        self.assertEqual(job["target"]["table"], "ods_demo_api_json_di")
        config_mod.validate_job(config_mod.normalize_job(job))  # 默认表名必须是合法标识符

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
            out = Path(tmp) / "x.json"
            code = init_wizard.run_init(
                out_path=str(out), ask=ask, ask_secret=ask, echo=lambda *a: None, workdir=Path(tmp)
            )
            self.assertFalse(out.exists())  # 目录已随 with 退出删除，必须在块内断言
        self.assertEqual(code, 1)

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
        # 向导默认值必须跟运行时 dates 模块同源，避免两处常量漂移
        self.assertEqual(window["date_tz"], dates_mod.DEFAULT_DATE_TZ)
        self.assertEqual(window["api_tz"], dates_mod.DEFAULT_API_TZ)

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
