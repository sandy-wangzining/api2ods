# -*- coding: utf-8 -*-
"""日志、脱敏、布尔工具与字段快照。"""

from __future__ import annotations

import errno
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    minimal_job,
)

import api2ods  # noqa: E402
from api2ods import cli as cli_mod  # noqa: E402
from api2ods import config as config_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)
from api2ods.cli import record_to_json  # noqa: E402


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

    def test_lock_refuses_without_platform_support(self):
        """没有 flock/msvcrt 的平台（罕见）默认拒绝无锁执行；显式 ALLOW_NO_LOCK=1 才继续。"""
        with mock.patch.object(utils, "fcntl", None), mock.patch.object(utils, "msvcrt", None):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "job.lock"
                with mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(SystemExit):
                        with utils.RunLock(path):
                            pass
                with (
                    mock.patch.dict(os.environ, {"API2ODS_ALLOW_NO_LOCK": "1"}, clear=True),
                    mock.patch.object(utils, "log"),
                ):
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

    def test_removed_sink_race_is_not_reported_as_broken(self):
        """快照取完后句柄被正常摘除并关闭：不能误报"写入失败"（并发收尾场景）。"""
        utils.reset_log_once()

        class ClosingSink:
            def write(self, _text):
                utils._sinks.remove(self)  # 模拟 remove_log_sink 在快照之后摘掉
                raise ValueError("I/O operation on closed file")

            def flush(self):
                pass

            def close(self):
                pass

        handle = ClosingSink()
        utils.add_log_sink(handle)
        utils.log("并发收尾")
        self.assertNotIn(handle, utils._sinks)
        self.assertFalse(utils._sink_write_warned)

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
            self.assertTrue(utils._console_patched)
        self.assertEqual(calls, [1])

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


class TestPendingWarningsArePerJob(OfflineTestCase):
    def test_string_warnings_key_falls_through_to_unknown_warning(self):
        """用户误写的 "__warnings__"（非 list）不能被 extend 按字符拆成假告警，
        也不能被静默吞掉——要落到"不是已知配置项"的扫描。"""
        job = minimal_job()
        job["__warnings__"] = "oops"
        ws = config_mod.collect_warnings(job)
        self.assertTrue(any("__warnings__" in w and "不是已知配置项" in w for w in ws), ws)
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


class TestTimezoneSecretRendering(unittest.TestCase):
    def test_secret_timezone_is_resolved_before_today(self):
        job = {"window": {"date_tz": "${secrets.tz}"}, "secrets": {"tz": "UTC"}, "job": "${today}"}
        shared = {"secrets": {"tz": "Asia/Shanghai"}}
        with mock.patch.object(config_mod, "build_context", wraps=config_mod.build_context) as build:
            rendered, _ = config_mod.render_job(job, shared, date(2026, 9, 18))
        self.assertEqual(rendered["window"]["date_tz"], "UTC")
        self.assertEqual(job["window"]["date_tz"], "${secrets.tz}")
        self.assertEqual(shared["secrets"]["tz"], "Asia/Shanghai")
        self.assertEqual(build.call_args.kwargs["tz"].key, "UTC")
