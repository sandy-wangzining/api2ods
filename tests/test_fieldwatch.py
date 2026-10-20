# -*- coding: utf-8 -*-
"""字段漂移快照。"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
)

from api2ods import cli as cli_mod  # noqa: E402
from api2ods import fieldwatch as fieldwatch_mod  # noqa: E402


class TestFieldWatch(OfflineTestCase):
    """字段快照：观察并集、读写往返、同名不同作业互不串。"""

    @unittest.skipUnless(os.name == "posix", "POSIX surrogateescape 文件名")
    def test_non_utf8_job_path_roundtrips_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / os.fsdecode(b"job-\xff.json")
            try:
                job.write_bytes(b"{}")
            except OSError as exc:
                if exc.errno != errno.EILSEQ:
                    raise
                self.skipTest("当前文件系统不支持非 UTF-8 文件名（如 macOS APFS）")
            fieldwatch_mod.save_snapshot(job, ["a"], job.stem)
            self.assertEqual(fieldwatch_mod.load_snapshot(job), {"a"})

    @unittest.skipUnless(os.name == "posix", "POSIX surrogateescape 文件名")
    def test_non_utf8_job_path_can_acquire_lock_location(self):
        job = Path(os.fsdecode(b"job-\xff.json"))
        self.assertEqual(cli_mod._lock_path(job), cli_mod._lock_path(job))

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

    def test_surrogate_field_name_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo.json"
            fieldwatch_mod.save_snapshot(job, ["\ud800"], "demo")
            self.assertEqual(fieldwatch_mod.load_snapshot(job), {"\ud800"})

    def test_snapshot_serialization_failure_does_not_fail_job(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fieldwatch_mod, "log") as log:
            job = Path(tmp) / "demo.json"
            fieldwatch_mod.save_snapshot(job, ["a"], object())
            log.assert_called_once()
            self.assertFalse(fieldwatch_mod.snapshot_path(job).exists())
            self.assertEqual(list((Path(tmp) / ".field-state").glob("*.tmp")), [])

    def test_same_named_jobs_do_not_share_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a" / "task.json"
            second = Path(tmp) / "b" / "task.json"
            first.parent.mkdir()
            second.parent.mkdir()
            fieldwatch_mod.save_snapshot(first, ["x"], "task")
            self.assertEqual(fieldwatch_mod.load_snapshot(first), {"x"})
            self.assertIsNone(fieldwatch_mod.load_snapshot(second))
