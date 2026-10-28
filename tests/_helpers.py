# -*- coding: utf-8 -*-
"""api2ods 测试公共设施：离线基类、夹具与替身。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api2ods import cli as cli_mod  # noqa: E402
from api2ods import config as config_mod  # noqa: E402
from api2ods import (  # noqa: E402
    http as http_mod,
)
from api2ods import spool as spool_mod  # noqa: E402
from api2ods import (
    utils,
)

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
        # 调度环境可能带着业务日/时区变量：不隔离会让默认业务日用例吃到脏值。
        env_patcher = mock.patch.dict(os.environ, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for name in ("bizdate", "SKYNET_BIZDATE", "ODS_BIZDATE_TZ"):
            os.environ.pop(name, None)


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


def wizard_answers(mapping: dict, choice_answers: list):
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
        self.written = {}

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
        self._writer = FakeWriter(self, partition)
        return self._writer


class FakeWriter:
    def __init__(self, table=None, partition=None):
        self.table = table
        self.partition = partition
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
        if exc_info[0] is None and self.table is not None:
            self.table.written[self.partition] = list(self.records)
            self.table.existing_partitions.add(self.partition)


class DdlOdps:
    """假数仓：暂存与正式表隔离，发布成功时原子替换。"""

    def __init__(self, table=None):
        self.table = table
        self.sqls: list[str] = []
        self.stages = {}
        self.stage = None

    def get_table(self, name):
        short = str(name).rsplit(".", 1)[-1]
        if short in self.stages:
            return self.stages[short]
        return self.table

    def run_sql(self, sql):
        self.sqls.append(sql)
        create = re.match(r"create table if not exists ([\w.]+)", sql, re.I)
        if create and create.group(1).rsplit(".", 1)[-1].startswith("tmp_api2ods_"):
            name = create.group(1).rsplit(".", 1)[-1]
            self.stage = FakeTable(
                columns=list(self.table.table_schema.columns), partitions=list(self.table.table_schema.partitions)
            )
            self.stages[name] = self.stage
            return OkInstance()
        drop = re.match(r"drop table if exists ([\w.]+)", sql, re.I)
        if drop:
            self.stages.pop(drop.group(1).rsplit(".", 1)[-1], None)
            return OkInstance()
        publication = re.match(
            r"insert overwrite table ([\w.]+) partition \(pt='([^']+)'\) select (.+?) from ([\w.]+) where",
            sql,
            re.I,
        )
        if publication:
            target = self.get_table(publication.group(1))
            stage = self.get_table(publication.group(4))
            spec = f"pt={publication.group(2)}"
            target.written[spec] = [list(row) for row in stage.written.get(spec, [])]
            target.existing_partitions.add(spec)
            return OkInstance()
        if "count(" in sql.lower().replace(" ", ""):
            partition = re.search(r"pt\s*=\s*'([^']*)'", sql, re.I)
            source = re.search(r"\bfrom\s+([\w.]+)", sql, re.I)
            if partition is None or source is None:
                raise AssertionError(f"fake 无法解析 count：{sql!r}")
            table = self.get_table(source.group(1))
            return OkInstance([{"cnt": len(table.written.get(f"pt={partition.group(1)}", []))}])
        partition = re.search(
            r"alter table ([\w.]+) (drop\s+if\s+exists|add\s+if\s+not\s+exists)\s+partition\s*\(\s*pt\s*=\s*'([^']*)'\s*\)",
            sql,
            re.I,
        )
        if partition and self.table is not None:
            table = self.get_table(partition.group(1))
            spec = f"pt={partition.group(3)}"
            if partition.group(2).lower().startswith("drop"):
                table.deleted.append((spec, True))
                table.written.pop(spec, None)
                table.existing_partitions.discard(spec)
            else:
                table.created.append((spec, True))
        return OkInstance()


class OkInstance:
    """立即成功、未终止的假 SQL 实例（run_sql_with_timeout 直接取用）。"""

    def __init__(self, rows=None):
        self.rows = rows or []

    def open_reader(self):
        return nullcontext(self.rows)

    def is_successful(self):
        return True

    def is_terminated(self):
        return False


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
