# -*- coding: utf-8 -*-
"""落盘文件与批次读回。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    SyncFlowTestCase,
)

from api2ods import spool as spool_mod  # noqa: E402


class TestSpool(OfflineTestCase):
    def test_write_read_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            self.addCleanup(spool.close)  # 断言失败时也收口句柄/临时文件
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


class TestSpoolDefensiveBranches(OfflineTestCase):
    """spool 的清理动作与上下文管理器。"""

    def test_loads_json_rejects_nan_and_infinity(self):
        for text, name in (('{"a": NaN}', "NaN"), ('{"a": Infinity}', "Infinity")):
            with self.assertRaises(ValueError) as ctx:
                spool_mod.loads_json(text)
            self.assertIn(name, str(ctx.exception))

    def test_close_swallows_unlink_failure(self):
        """临时文件删不掉（被占用/权限）：任务已经写完了，不能因此报错，但要留告警。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spool.jsonl"
            spool = spool_mod.SpoolWriter(path)
            spool.write_records([{"a": 1}])
            with (
                mock.patch.object(Path, "unlink", side_effect=OSError("被占用")),
                mock.patch.object(spool_mod, "log") as logger,
            ):
                spool.close()
            self.assertTrue(any("清理失败" in str(call) for call in logger.call_args_list))
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
