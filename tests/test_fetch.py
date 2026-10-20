# -*- coding: utf-8 -*-
"""分页拉取、并发单元与体检探针。"""

from __future__ import annotations

import gc
import importlib.util
import io
import sys
import threading
import time
import weakref
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
    minimal_job,
)

from api2ods import fetch as fetch_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)


class TestFetcher(OfflineTestCase):
    def test_max_response_bytes_is_passed_to_http(self):
        job = minimal_job()
        job["request"]["max_response_bytes"] = 5
        fetcher = fetch_mod.Fetcher(job, Path("."))
        unit = fetch_mod.FetchUnit(label="x", day=date(2026, 9, 18), params=None)
        with mock.patch.object(fetch_mod, "request_with_retry", return_value={"data": {"list": [{"a": 1}]}}) as retry:
            records = fetcher.fetch_unit(unit)
        self.assertEqual(records, [{"a": 1}])
        self.assertEqual(retry.call_args.kwargs["max_response_bytes"], 5)

    def test_single_unit_memory_cap(self):
        """单个请求单元累积过多记录时 fail-closed，不能一路堆到 OOM。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_size": 2,
                "total_pages_path": "data.pages",
                "delay_seconds": 0,
            }
        )
        payloads = [
            {"data": {"list": [{"a": 1}, {"a": 2}], "pages": 3}},
            {"data": {"list": [{"a": 3}, {"a": 4}], "pages": 3}},
        ]
        unit = fetch_mod.FetchUnit("range", date(2026, 9, 18), None)
        with (
            mock.patch.object(http_mod, "request_once", side_effect=payloads),
            mock.patch.object(fetch_mod, "MAX_UNIT_RECORDS", 3),
        ):
            with self.assertRaises(utils.ConfigError) as ctx:
                self._fetcher(job).fetch_unit(unit)
        self.assertIn("内存保护上限", str(ctx.exception))

    def test_add_fields_must_be_mapping(self):
        """库调用方绕过 validate_job 时，非对象 add_fields 也要给干净配置错。"""
        job = minimal_job()
        job["request"]["add_fields"] = ["x"]
        with self.assertRaises(utils.ConfigError):
            fetch_mod.Fetcher(job, Path("."))

    def test_zero_total_cannot_override_positive_end_marker(self):
        for pages, items in ((0, 100), (3, 0)):
            with self.subTest(pages=pages, items=items):
                job = minimal_job(
                    pagination={
                        "type": "page",
                        "total_pages_path": "data.pages",
                        "total_items_path": "data.total",
                    }
                )
                payload = {"data": {"list": [], "pages": pages, "total": items}}
                with mock.patch.object(http_mod, "request_once", return_value=payload):
                    with self.assertRaises(fetch_mod.UnconfirmedEndError):
                        self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def _fetcher(self, job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_page_pagination_with_total_pages(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "param_as_string": True,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(dict(params))
            if params["current"] == "1":
                return {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}}
            return {"data": {"list": [{"id": 3}], "totalPages": 2}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(calls[0]["current"], "1")

    def test_page_stop_when_short_stops_at_short_page(self):
        """接口不返回总数（如 XMP）：本页条数 < page_size 即判末页，多页照常翻满。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            page = args[2].get("page")
            calls.append(page)
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}
            return {"data": {"list": pages.get(page, []), "page": page}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(calls, [1, 2, 3])

    def test_page_stop_when_short_exact_multiple_ends_on_empty_page(self):
        """条数恰好是 page_size 整数倍：末页之后会多一页空页，按短页规则收尾而不是报错。

        默认严格模式没有总数时会把空页视为"无法确认翻完"报错；stop_when_short
        下空页（0 条 < page_size）就是"没有下一页"，应正常收尾。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            page = args[2].get("page")
            calls.append(page)
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}]}
            return {"data": {"list": pages.get(page, [])}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(records), 4)
        self.assertEqual(calls, [1, 2, 3])

    def test_page_stop_when_short_empty_first_page_is_empty_day(self):
        """首屏空页 = 该窗口确实没数据：0 条成功收尾，不发第二页、不触发严格报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "page_size",
                "page_size": 2,
                "stop_when_short": True,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[2].get("page"))
            return {"data": {"list": []}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])
        self.assertEqual(calls, [1])

    def test_boolean_total_items_path_does_not_end_pagination_early(self):
        """终点字段指到布尔字段（data.ok: true）时不能当成"总共 1 条"提前收尾。

        float(True) == 1.0 能过数字检查，原来会只拉第一页就停、还显示成功——
        静默少数据正是本工具最不能接受的失败。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.ok",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(params.get("page"))
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}, {"id": 4}], 3: [{"id": 5}]}
            return {"data": {"list": pages.get(params.get("page"), []), "ok": True}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        # 关键是它不再停在第 1 页：第 1、2 页照常拉满，空页 + 终点字段读不出数字 ->
        # 默认 strict 视为"无法确认翻完"，报错让调度看见（而不是只拉 2 条还显示成功）
        self.assertEqual(calls[:3], [1, 2, 3])

    def test_boolean_total_pages_does_not_end_pagination_early(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.ok",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(params.get("page"))
            pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}
            return {"data": {"list": pages.get(params.get("page"), []), "ok": True}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(calls, [1, 2, 3])

    def test_aliyun_nonce_regenerated_on_retry(self):
        """请求重试必须重新签名：复用 SignatureNonce 会被阿里云判 400 SignatureNonceUsed（生产实测）。"""
        job = minimal_job()
        job["request"]["auth"] = {"type": "aliyun_rpc", "access_key_id": "ak", "access_key_secret": "sk"}
        seen = []

        def fake(*args, **kwargs):
            seen.append(args[2].get("SignatureNonce"))
            if len(seen) == 1:
                raise RuntimeError("boom")  # 网络抖动 -> 触发重试
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake), mock.patch.object(http_mod.time, "sleep"):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0] and seen[0] != seen[1])

    def test_page_pagination_with_total_items_and_defensive_check(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 5}},  # page 1
            {"Data": {"Items": {"Item": []}, "TotalCount": 5}},  # 抖动：空页但没拉完 -> 报错
        ]

        def fake(*args, **kwargs):
            return responses.pop(0)

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("TotalCount", str(ctx.exception))

    def test_strict_false_tolerates_stale_total_count(self):
        """TotalCount 不准的接口（翻页期间数据在变）：strict=false 按已拉到的收尾，但打警告。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 9}},
            {"Data": {"Items": {"Item": []}, "TotalCount": 9}},  # 空页但总数没够
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_strict_accepts_string_false(self):
        """JSON 里写 "false"（带引号）也应按 false 处理，不能因为不是布尔就当成开。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "strict": "false",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 9}},
            {"Data": {"Items": {"Item": []}, "TotalCount": 9}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_total_pages_empty_page_midway_raises(self):
        """只配 total_pages_path 时，中间的抖动空页不能提前收尾（原来会静默少数据且显示成功）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 3}},  # 第 2 页空，但总页数说还有第 3 页
            {"data": {"list": [{"id": 3}], "totalPages": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))
        self.assertEqual(len(responses), 1)  # 没有继续翻第 3 页

    def test_total_pages_empty_first_page_raises(self):
        """第 1 页就是空、总页数却说有数据：同样是接口抖动，不能静默当成"今天没数据"。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [{"data": {"list": [], "totalPages": 2}}]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_total_pages_empty_page_on_last_page_is_normal(self):
        """最后一页为空是正常的（接口按"第 N 页 / 共 N 页"给终点），必须照常收尾。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 2}},
            {"data": {"list": [], "totalPages": 2}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])

    def test_total_pages_only_on_first_page(self):
        """终点字段只在第一页返回（很常见）：后续页取不到时要用最后一次读到的值判断。

        修复前：末页之后那一页空页取不到 totalPages，被判"无法确认已翻完"，
        默认 strict 下整窗失败——数据明明已经拉全了。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},  # 只有第一页带终点
            {"data": {"list": [{"id": 3}, {"id": 4}]}},
            {"data": {"list": [{"id": 5}]}},
            {"data": {"list": []}},  # 末页之后的空页
        ]
        calls = []

        def fake(*args, **kwargs):
            calls.append(args[2]["current"])
            return responses.pop(0)

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5])
        self.assertEqual(calls, [1, 2, 3])  # 读到第 3 页（共 3 页）就收尾，没多翻

    def test_total_items_only_on_first_page(self):
        """TotalCount 只在首页返回时同样要用缓存值收尾。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalCount": 3}},
            {"data": {"list": [{"id": 3}]}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])

    def test_nonpositive_total_pages_is_not_an_end_marker(self):
        """totalPages = -1 / 0（"未知"哨兵、字段占位）不能当终点：否则第 1 页就 break，
        数据只拉到一页却校验通过，是静默少数据。这里只有 totalCount 可信，按它翻完。"""
        for sentinel in (-1, 0):
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "current",
                    "size_param": "size",
                    "page_size": 2,
                    "total_pages_path": "data.totalPages",
                    "total_items_path": "data.totalCount",
                    "delay_seconds": 0,
                }
            )
            responses = [
                {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": sentinel, "totalCount": 6}},
                {"data": {"list": [{"id": 3}, {"id": 4}], "totalPages": sentinel, "totalCount": 6}},
                {"data": {"list": [{"id": 5}, {"id": 6}], "totalPages": sentinel, "totalCount": 6}},
            ]
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6], f"totalPages={sentinel} 时不该提前收尾")

    def test_total_items_shrinking_midway_does_not_end_early(self):
        """TotalCount 中途变小（窗口期数据被删、或某页只回本页条数）时终点不能跟着缩水。

        修复前：取"最近一次读到的值"，第 2 页回 2 时 len(records)=4 >= 2 直接收尾，
        少拉一整页却不报错。
        """
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalCount": 6}},
            {"data": {"list": [{"id": 3}, {"id": 4}], "totalCount": 2}},  # 回落到"本页条数"
            {"data": {"list": [{"id": 5}, {"id": 6}], "totalCount": 6}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6])

    def test_no_endpoint_field_anywhere_still_raises(self):
        """缓存不等于放宽：全程没拿到过终点字段时，空页仍要报错（不能静默少数据）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "max_pages": 4,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}]}},
            {"data": {"list": []}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("无法确认已翻完", str(ctx.exception))

    def test_cached_total_pages_still_catches_shrinking_page(self):
        """缓存的是终点值，不是"翻完了"这个结论：缓存说还有数据就得继续报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 5}},  # 首页说共 5 页
            {"data": {"list": []}},  # 第 2 页就空了
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))

    def test_total_pages_empty_page_strict_false_stops_with_warning(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "strict": False,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])

    def test_fail_if_skipped_for_binary_response(self):
        """文件类响应没有字段可查：fail_if 不能把 bytes 判成业务错误（照抄模板会每天必失败）。"""
        http_mod.check_fail_if(b"PK\x03\x04binary", [{"path": "code", "not_equals": "0"}])
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["request"]["fail_if"] = [{"path": "code", "not_equals": "0", "retry": True}]
        job["parse"] = {"format": "csv"}
        with (
            mock.patch.object(http_mod, "request_once", return_value=b"id\n1\n"),
            mock.patch.object(http_mod, "check_fail_if", wraps=http_mod.check_fail_if) as spy,
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": "1"}])
        self.assertIsInstance(spy.call_args.args[0], bytes)  # 走的是真实请求路径，且命中的是二进制分支

    def test_cursor_can_disable_size_param(self):
        """size_param 显式写 null 时不带页大小参数（有些游标接口不认 size）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "cursor",
                "cursor_path": "data.next",
                "size_param": None,
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertNotIn("size", calls[0])

    def test_page_pagination_total_items_complete(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 3}},
            {"Data": {"Items": {"Item": [{"id": 3}]}, "TotalCount": 3}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3])
        self.assertEqual(responses, [])

    def test_page_size_capped_by_server_still_pulls_all(self):
        """接口把 PageSize 压小（返回条数 < 请求的 page_size）时不能提前判定拉完（静默丢数回归）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 5,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"Items": {"Item": [{"id": 1}, {"id": 2}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 3}, {"id": 4}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 5}, {"id": 6}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 7}, {"id": 8}]}, "TotalCount": 10}},
            {"Data": {"Items": {"Item": [{"id": 9}, {"id": 10}]}, "TotalCount": 10}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(responses, [])

    def test_missing_records_path_after_first_page_raises(self):
        """翻到第 2 页 records_path 取不到 → 报错，不能当“拉完了”静默截断。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 2,
                "total_pages_path": "Data.TotalPages",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        responses = [
            {"Data": {"TotalPages": 3, "Items": {"Item": [1, 2]}}},
            {"Data": {"TotalPages": 3, "Items": {}}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("records_path", str(ctx.exception))

    def test_records_missing_empty_only_applies_to_first_page(self):
        """records_missing=empty 只认第一页：翻到一半缺字段是接口/配置问题，必须报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        job["request"]["records_missing"] = "empty"
        responses = [
            {"Data": {"Items": {"Item": [1, 2]}, "TotalCount": 6}},
            {"Data": {"Items": {}}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_cursor_pagination(self):
        job = minimal_job(
            pagination={"type": "cursor", "cursor_param": "cursor", "cursor_path": "data.next", "delay_seconds": 0}
        )
        calls = []

        def fake(*args, **kwargs):
            params = args[2]
            calls.append(dict(params))
            if "cursor" not in params:
                return {"data": {"list": [{"id": 1}], "next": "c2"}}
            return {"data": {"list": [{"id": 2}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual(calls[1]["cursor"], "c2")
        # 游标模式同样要带页大小，否则接口用默认值（可能很小）
        self.assertEqual(calls[0]["size"], 100)

    def test_window_retries_infinity_is_config_error(self):
        """window_retries=1e999（json 解析成 inf）时 int(inf) 抛 OverflowError：
        要变成"必须是整数"的配置错，而不是裸 traceback。"""
        job = minimal_job(pagination={"type": "page", "total_pages_path": "d.p", "window_retries": float("inf")})
        fetcher = self._fetcher(job)
        with self.assertRaises(SystemExit) as ctx:
            fetcher.fetch_all([date(2026, 9, 18)], window_retries=float("inf"))
        self.assertIn("window_retries", str(ctx.exception))

    def test_cursor_missing_path_rejected_before_request(self):
        """库调用方绕过 validate_job：type=cursor 而 cursor_path 为空时必须在发请求前报错——
        "取不到游标"会被当成"翻完了"，只拉第一页还报成功（静默截断）。"""
        job = minimal_job(pagination={"type": "cursor", "cursor_param": "cursor", "delay_seconds": 0})
        with mock.patch.object(http_mod, "request_once") as request_mock:
            with self.assertRaises(utils.ConfigError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        request_mock.assert_not_called()

    def test_unknown_pagination_type_rejected(self):
        """拼错/带空格的 type 原来落到 else 被当成 cursor、只拉第一页还报成功——
        必须在发请求前白名单拦截（先 strip 再比，大小写/空白归一）。"""
        for bad in ("pages", "offset", "cursorx", "nonee"):
            job = minimal_job(pagination={"type": bad, "cursor_path": "data.next", "delay_seconds": 0})
            with mock.patch.object(http_mod, "request_once") as request_mock:
                with self.assertRaises(utils.ConfigError):
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            request_mock.assert_not_called()
        job = minimal_job(pagination={"type": " NONE ", "delay_seconds": 0})
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": []}}):
            self.assertEqual(self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None)), [])

    def test_none_pagination_single_call(self):
        job = minimal_job()
        calls = []
        with mock.patch.object(
            http_mod, "request_once", side_effect=lambda *a, **k: (calls.append(a), {"data": {"list": [{"id": 1}]}})[1]
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(records, [{"id": 1}])

    def test_scalar_records_rejected(self):
        """记录数组里混进数字/字符串时直接报错：写进 json 列下游全取不到，却显示成功。"""
        job = minimal_job()
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": [1, "x", {"a": 1}]}}):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("不是对象", str(ctx.exception))

    def test_scalar_records_rejected_on_paginated_path(self):
        """分页路径同样要拦标量元素：原来只有单页路径有这道检查，
        分页接口（很常见的形态）会把数字/字符串原样写进 json 列且显示成功。"""
        for bad_records, page_no in (
            ([{"id": 1}, 42], 1),  # 首页就混进数字
            ([{"id": 1}], 2),  # 首页正常、第 2 页混进字符串
        ):
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
            responses = [{"data": {"list": [{"id": 1}], "totalPages": 2}}]
            if page_no == 2:
                responses.append({"data": {"list": ["oops"], "totalPages": 2}})
            else:
                responses[0] = {"data": {"list": bad_records, "totalPages": 2}}
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertIn("不是对象", str(ctx.exception))

    def test_scalar_records_rejected_on_cursor_path(self):
        """游标分页走的是同一段取数代码，同样要拦。"""
        job = minimal_job(
            pagination={"type": "cursor", "cursor_param": "cursor", "cursor_path": "data.next", "delay_seconds": 0}
        )
        with mock.patch.object(
            http_mod, "request_once", return_value={"data": {"list": [{"id": 1}, None], "next": None}}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("不是对象", str(ctx.exception))

    def test_zero_data_day_empty_page_with_zero_total_succeeds(self):
        """零数据日：空数组 + 总数 0 = 接口明说"这个窗口没有数据"，成功写 0 行。

        原来只认正数终点，把 0 一律当"字段没填"忽略，于是零数据日被判成"接口抖动"，
        整窗重试几次后失败——正常没数据的一天变成天天报警。
        """
        for marker in ({"data.total": 0}, {"data.totalPages": 0}, {"data.total": "0"}):
            path, value = next(iter(marker.items()))
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "page",
                    "size_param": "size",
                    "page_size": 100,
                    "total_items_path": path,
                    "delay_seconds": 0,
                }
            )
            with mock.patch.object(
                http_mod, "request_once", return_value={"data": {"list": [], "total": value, "totalPages": value}}
            ):
                records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertEqual(records, [], f"{path}=0 时应按零数据日成功返回")

    def test_zero_data_day_only_applies_to_first_page(self):
        """中途翻出空页却报 0 条：与前几页的记录自相矛盾，仍按未翻完报错。

        零数据日的豁免只认"一条都没拉到过"的情况，否则接口某页异常返回 0
        会把前面的天数/页数悄悄截掉。
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
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}},
            {"data": {"list": [], "totalPages": 0}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("总页数显示还有数据", str(ctx.exception))

    def test_negative_total_is_still_not_zero_count(self):
        """total=-1（"未知"哨兵）不是"零数据日"：第一页有空记录时才收尾，否则照旧报错。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "page",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.total",
                "max_pages": 3,
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}], "total": -1}},
            {"data": {"list": [], "total": -1}},
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_weird_end_markers_do_not_raise_raw_exceptions(self):
        """终点字段是 inf / "inf" / NaN / 10**400 时不能抛裸异常。

        原来 int() 在 try 之外：float("inf") 过得了 _is_number，随后 int(inf) 抛
        OverflowError、int(nan) 抛 ValueError，都被当成"接口抖动"整窗重试白等。
        """
        for value in ("inf", float("inf"), float("nan"), 10**400, "-inf", "abc"):
            job = minimal_job(
                pagination={
                    "type": "page",
                    "page_param": "page",
                    "size_param": "size",
                    "page_size": 2,
                    "total_items_path": "data.total",
                    "max_pages": 3,
                    "delay_seconds": 0,
                }
            )
            responses = [
                {"data": {"list": [{"id": 1}, {"id": 2}], "total": value}},
                {"data": {"list": [], "total": value}},
            ]
            with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
            self.assertIn("无法确认已翻完", str(ctx.exception))

    def test_nan_in_numeric_config_is_rejected(self):
        """NaN/inf 过得了 float()，却会让所有范围判断失效（NaN 跟谁比都是 False）。"""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod._as_number(value, 0.0, "pagination.delay_seconds")
            self.assertIn("有限数字", str(ctx.exception))

    def test_missing_records_path_raises(self):
        job = minimal_job()
        with mock.patch.object(http_mod, "request_once", return_value={"nope": 1}):
            with self.assertRaises(RuntimeError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_missing_records_path_empty_mode(self):
        job = minimal_job()
        job["request"]["records_missing"] = "empty"
        with mock.patch.object(http_mod, "request_once", return_value={"Data": {"Items": {}}}):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])

    def test_missing_records_path_empty_mode_with_pagination(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "PageNum",
                "size_param": "PageSize",
                "page_size": 2,
                "total_items_path": "Data.TotalCount",
                "delay_seconds": 0,
            }
        )
        job["request"]["records_path"] = "Data.Items.Item"
        job["request"]["records_missing"] = "empty"
        with mock.patch.object(http_mod, "request_once", return_value={"Data": {"Items": {}, "TotalCount": 0}}):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [])

    def test_max_pages_guard(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 1,
                "max_pages": 3,
                "delay_seconds": 0,
                "total_pages_path": "t",
            }
        )
        with mock.patch.object(http_mod, "request_once", return_value={"data": {"list": [{"id": 1}], "t": 99}}):
            with self.assertRaises(utils.ConfigError):
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))

    def test_bytes_mode_zip_csv(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("amount.csv", "user,amount\nu1,5\n")
        job = minimal_job()
        job["request"]["response_type"] = "bytes"
        job["parse"] = {"format": "csv", "unzip": True, "entry_contains": "amount"}
        job["pagination"] = {"type": "none"}
        with mock.patch.object(http_mod, "request_once", return_value=buffer.getvalue()):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"user": "u1", "amount": "5"}])

    def test_add_fields(self):
        job = minimal_job()
        job["request"]["add_fields"] = {"source_account": "acct1"}
        payload = {"data": {"list": [{"id": 1}, {"id": 2, "source_account": "api"}]}}
        with mock.patch.object(http_mod, "request_once", return_value=payload):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records[0]["source_account"], "acct1")
        self.assertEqual(records[1]["source_account"], "api")  # API 原值优先

    def test_params_in_headers(self):
        job = minimal_job()
        job["request"]["params_in"] = "headers"
        job["request"]["auth"] = {"type": "sha256_concat", "secret_key": "sec", "sign_field": "sign"}
        captured = {}

        def fake(*args, **kwargs):
            captured.update(method=args[0], params=dict(args[2]), headers=dict(args[3]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(captured["params"], {})  # URL 参数被清空
        self.assertEqual(captured["headers"]["app"], "demo")  # 参数搬到请求头
        self.assertIn("sign", captured["headers"])  # 签名也在请求头

    def test_build_units_per_day_and_range(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        units = self._fetcher(job).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual([u.label for u in units], ["2026-09-17", "2026-09-18"])
        job_range = minimal_job(
            window={"mode": "range", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        units = self._fetcher(job_range).build_units([date(2026, 9, 17), date(2026, 9, 18)])
        self.assertEqual(len(units), 1)

    def test_example_signer_rejects_non_scalar_params(self):
        """示例签名器对非标量参数显式拒绝（str() 与框架实际发送形态不一致会恒定 401）。"""

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("signers_example", root / "signers.example.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for bad_params in ({"ids": [1, 2]}, {"raw": b"x"}):
            with self.assertRaises(ValueError) as err:
                module.onerway_sign({"params": bad_params, "request": {"secret_key": "s"}})
            self.assertIn("非标量", str(err.exception))
        ok = module.onerway_sign({"params": {"a": "1", "b": 2}, "request": {"secret_key": "s"}})
        self.assertIn("sign", ok["params"])

    def test_window_retries_fractional_or_negative_rejected(self):
        """window_retries 先校验再取整：2.5/-1 不能被 int() 截断/被 max(1,…) 吞掉。"""
        for bad in (2.5, -1, float("inf")):
            with self.assertRaises(SystemExit) as ctx:
                self._fetcher(minimal_job()).fetch_all([date(2026, 9, 18)], window_retries=bad)
            self.assertIn("window_retries", str(ctx.exception))

    def test_params_in_typo_is_config_error(self):
        """params_in 拼错（header）不能静默退回 query（签名/密钥参数进 URL 明文）。"""
        with self.assertRaises(SystemExit) as ctx:
            self._fetcher(minimal_job(request={"params_in": "header"}))
        self.assertIn("params_in", str(ctx.exception))

    def test_retry_times_fractional_or_negative_rejected(self):
        """retry_times 先校验再取整：-0.5/2.5 不能被 int() 截断后绕过校验。"""
        for bad in (-0.5, 2.5, -1):
            with self.assertRaises(SystemExit) as ctx:
                self._fetcher(minimal_job(request={"retry_times": bad}))
            self.assertIn("retry_times", str(ctx.exception))

    def test_probe_size_retry_unconfirmed_end_is_ok(self):
        """page_size=1 被拒后按配置页大小重试、重试仍"无法确证翻完"：按能连通处理。"""
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100})
        fetcher = self._fetcher(job)
        calls = {"n": 0}

        def fake_fetch_unit(unit, **kwargs):
            calls["n"] += 1
            if kwargs.get("page_size_override") is not None:
                raise utils.FatalApiError("HTTP 400: invalid page size")
            raise fetch_mod.UnconfirmedEndError("d 第 1 页返回为空，但无法确认已翻完（接口没有给总页数/总条数）")

        with mock.patch.object(fetcher, "fetch_unit", side_effect=fake_fetch_unit):
            label, count = fetcher.probe([date(2026, 9, 18)])
        self.assertEqual(count, 0)
        self.assertEqual(calls["n"], 2)

    def test_probe_empty_units_is_config_error(self):
        """体检时算出 0 个请求单元：按配置错报（ConfigError），不是裸 RuntimeError。"""
        fetcher = self._fetcher(minimal_job())
        with mock.patch.object(fetcher, "build_units", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                fetcher.probe([date(2026, 9, 18)])
        self.assertIn("请求单元", str(ctx.exception))

    def test_probe_limits_page_size(self):
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 100,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 1, "data": {"list": [{"id": 1}, {"id": 2}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 2)
        self.assertEqual(calls[0]["n"], 1)

    def test_probe_stops_after_first_page_even_with_more_pages_left(self):
        """体检只发一次请求：接口说还有 99 页也不能继续翻（曾因循环里 continue 跳过返回而报错）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "p",
                "size_param": "n",
                "page_size": 100,
                "total_pages_path": "t",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 99, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 1)  # 只请求一次，不跟着 total_pages 翻

    def test_probe_treats_unconfirmed_end_as_connected(self):
        """接口不给总数、首页又为空：体检按"能连通、当日无数据"处理（0 条），
        不能因为 strict 的"无法确认已翻完"把好源判成体检失败。"""
        job = minimal_job(pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100})
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: {"data": {"list": []}}):
            label, count = self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(count, 0)

    def test_fetch_all_renews_stop_each_round(self):
        """fetch_all 退出（成功/失败/中断）时置当轮停止信号；下一轮换新对象而不是复位——
        已 set 的旧对象保持 set，上一轮遗留的 worker 永远看到"已停止"、不会被复活。"""
        job = minimal_job()
        fetcher = self._fetcher(job)
        with mock.patch.object(fetcher, "fetch_unit", return_value=[]):
            fetcher.fetch_all([date(2026, 9, 18)])
        first_stop = fetcher._stop
        self.assertTrue(first_stop.is_set())
        with mock.patch.object(fetcher, "fetch_unit", return_value=[]) as fu:
            fetcher.fetch_all([date(2026, 9, 18)])
        self.assertEqual(fu.call_count, 1)
        self.assertTrue(first_stop.is_set())  # 旧对象不复位
        self.assertIsNot(fetcher._stop, first_stop)  # 新一轮换新对象

    def test_sleep_is_interruptible(self):
        """停止信号已置位时 _sleep 立即返回 True（不真等 10s/20s 的退避）。"""
        fetcher = self._fetcher(minimal_job())
        fetcher._stop.set()
        started = time.monotonic()
        self.assertTrue(fetcher._sleep(10))
        self.assertLess(time.monotonic() - started, 1.0)


class TestFetchAll(OfflineTestCase):
    def test_window_retry_then_success(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        fetcher = fetch_mod.Fetcher(job, Path("."))
        attempts = {"n": 0}
        collected: list = []

        def flaky(unit, **_kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("boom")
            return [{"id": attempts["n"]}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=1, on_records=collected.extend)
        self.assertEqual(failures, [])
        self.assertEqual(len(collected), 1)
        self.assertEqual(stats[0][1], 1)

    def test_failures_collected_and_order_kept(self):
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 17), date(2026, 9, 18)]
        collected: list = []

        def flaky(unit, **_kw):
            if unit.day.day == 17:
                raise RuntimeError("bad day")
            return [{"id": 2}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all(days, window_retries=0, on_records=collected.extend)
        self.assertEqual(len(failures), 1)
        self.assertEqual([r["id"] for r in collected], [2])
        self.assertEqual(stats, [("2026-09-18", 1)])

    def test_fatal_error_not_retried_across_units(self):
        """4xx 是参数/权限问题：重试多少次都一样，立刻中止（别把同一错误重复几十遍）。

        修复前：每个单元各重试 3 次，30 天窗口 = 90 个必失败请求 + 十几分钟空等。
        """
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
        calls = {"n": 0}

        def unauthorized(unit, **_kw):
            calls["n"] += 1
            raise utils.FatalApiError("HTTP 401：Unauthorized")

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=unauthorized),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            with self.assertRaises(utils.FatalApiError):
                fetcher.fetch_all(days, window_retries=2)
        self.assertEqual(calls["n"], 1)  # 只试了第一个单元就中止

    def test_config_error_not_retried(self):
        """配置写错（如 skip_rows 为负）同样不该整窗重试。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        calls = {"n": 0}

        def bad_config(unit, **_kw):
            calls["n"] += 1
            raise utils.ConfigError("parse.skip_rows 不能为负：-1")

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=bad_config),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            with self.assertRaises(utils.ConfigError):
                fetcher.fetch_all([date(2026, 9, 16), date(2026, 9, 17)], window_retries=2)
        self.assertEqual(calls["n"], 1)

    def test_network_error_still_retried(self):
        """回归保护：网络抖动仍要整窗重试（只把不可重试的错误挑出去）。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        calls = {"n": 0}

        def flaky(unit, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return [{"id": 1}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=flaky),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all([date(2026, 9, 18)], window_retries=2)
        self.assertEqual(failures, [])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(stats, [("2026-09-18", 1)])


class TestFetchAllConcurrent(OfflineTestCase):
    """并发模式（--workers>1）：单元进线程池、逐个收尾、失败只影响自己那个单元。"""

    def test_fetch_unit_after_fetch_all_has_its_own_stop_signal(self):
        job = minimal_job(pagination={"type": "page", "total_pages_path": "data.pages"})
        fetcher = fetch_mod.Fetcher(job, Path("."))
        days = [date(2026, 9, 18)]
        pages = [{"data": {"list": [{"id": i}], "pages": 2}} for i in (1, 2)]
        with mock.patch.object(fetcher, "_do_request", side_effect=pages + pages):
            stats, failures = fetcher.fetch_all(days, window_retries=0)
            self.assertEqual(failures, [])
            self.assertEqual(sum(count for _label, count in stats), 2)
            self.assertTrue(fetcher._stop.is_set())
            records = fetcher.fetch_unit(fetcher.build_units(days)[0])
        self.assertEqual([record["id"] for record in records], [1, 2])

    def test_pending_results_stay_bounded_when_spool_is_slow(self):
        fetcher, _ = self._fetcher_and_days()
        days = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
        submitted = consumed = 0
        outstanding = []
        pool_class = fetch_mod.ThreadPoolExecutor

        class ObservedPool(pool_class):
            def submit(self, *args, **kwargs):
                nonlocal submitted
                submitted += 1
                return super().submit(*args, **kwargs)

        def consume(_records):
            nonlocal consumed
            outstanding.append(submitted - consumed)
            consumed += 1

        with (
            mock.patch.object(fetch_mod, "ThreadPoolExecutor", ObservedPool),
            mock.patch.object(fetcher, "fetch_unit", side_effect=lambda *_args, **_kwargs: [{"id": 1}]),
        ):
            stats, failures = fetcher.fetch_all(days, workers=2, window_retries=0, on_records=consume)
        self.assertEqual(failures, [])
        self.assertEqual(len(stats), len(days))
        self.assertEqual(sum(count for _label, count in stats), len(days))
        self.assertEqual(consumed, len(days))
        self.assertLessEqual(max(outstanding), 2)

    @staticmethod
    def _fetcher_and_days():
        job = minimal_job(
            window={"mode": "per_day", "date_tz": "UTC", "api_tz": "+08:00", "start_param": "s", "end_param": "e"}
        )
        days = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
        return fetch_mod.Fetcher(job, Path(".")), days

    def test_units_run_in_threads_and_records_are_handed_over(self):
        """并发模式确实把单元放进线程池，且每个单元拉完立刻回调（边拉边落盘）。"""
        fetcher, days = self._fetcher_and_days()
        in_main_thread, collected = [], []

        def fake(unit, **_kw):
            in_main_thread.append(threading.current_thread() is threading.main_thread())
            return [{"id": unit.label}]

        with mock.patch.object(fetcher, "fetch_unit", side_effect=fake):
            stats, failures = fetcher.fetch_all(days, workers=2, window_retries=0, on_records=collected.extend)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(label for label, _ in stats), sorted(str(day) for day in days))
        self.assertEqual(len(collected), len(days))
        self.assertEqual(in_main_thread, [False] * len(days))

    def test_concurrent_failure_only_fails_its_own_unit(self):
        fetcher, days = self._fetcher_and_days()

        def fake(unit, **_kw):
            if unit.day.day == 17:
                raise ConnectionError("connection reset")
            return [{"id": 1}]

        with (
            mock.patch.object(fetcher, "fetch_unit", side_effect=fake),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False),
        ):
            stats, failures = fetcher.fetch_all(days, workers=2, window_retries=0)
        self.assertEqual([label for label, _ in failures], ["2026-09-17"])
        self.assertIn("connection reset", failures[0][1])
        self.assertEqual(sorted(label for label, _ in stats), ["2026-09-16", "2026-09-18"])

    def test_concurrent_fatal_error_aborts_run(self):
        """并发模式下 4xx 同样要立刻中止：不能变成"每个单元各失败一次"继续跑。"""
        fetcher, days = self._fetcher_and_days()

        def unauthorized(_unit, **_kw):
            raise utils.FatalApiError("HTTP 401：Unauthorized")

        with mock.patch.object(fetcher, "fetch_unit", side_effect=unauthorized):
            with self.assertRaises(utils.FatalApiError):
                fetcher.fetch_all(days, workers=2, window_retries=2)


class TestProbePageSize(OfflineTestCase):
    def _fetcher(self, job):
        return fetch_mod.Fetcher(job, Path("."))

    def test_page_probe_uses_size_one(self):
        job = minimal_job(
            pagination={"type": "page", "page_param": "p", "size_param": "n", "page_size": 100, "total_pages_path": "t"}
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"t": 1, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(calls[0]["n"], 1)

    def test_cursor_probe_keeps_configured_page_size(self):
        """游标接口常有最小页大小限制，体检压到 1 会被误杀（正式跑却正常）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "size_param": "n",
                "page_size": 50,
            }
        )
        calls = []

        def fake(*args, **kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            self._fetcher(job).probe([date(2026, 9, 18)])
        self.assertEqual(calls[0]["n"], 50)


class TestUnfinishedReason(OfflineTestCase):
    def test_unparseable_endpoint_is_not_completion(self):
        """终点字段存在但读不出数字：不能当「翻完了」收尾（原来会静默少数据）。"""
        self.assertIsNotNone(fetch_mod._unfinished_reason(2, "abc", 5, None))
        self.assertIsNotNone(fetch_mod._unfinished_reason(2, None, 5, "xyz"))

    def test_genuine_completion_returns_none(self):
        self.assertIsNone(fetch_mod._unfinished_reason(3, 3, 5, None))
        self.assertIsNone(fetch_mod._unfinished_reason(2, None, 5, 5))


class TestFetchDefensiveBranches(OfflineTestCase):
    """fetch 的防御分支：空计划、非对象记录、空页收尾、体检限量、整窗重试参数。"""

    @staticmethod
    def _fetcher(job: dict) -> fetch_mod.Fetcher:
        return fetch_mod.Fetcher(job, Path("."))

    def test_zero_count_only_accepts_numbers(self):
        """零数据判定只认数字 0：读不出数字时按"无法确认"处理（宁可重试也别静默收尾）。"""
        self.assertTrue(fetch_mod._is_zero_count(0))
        self.assertTrue(fetch_mod._is_zero_count("0"))
        self.assertFalse(fetch_mod._is_zero_count([]))
        self.assertFalse(fetch_mod._is_zero_count("很多"))
        self.assertFalse(fetch_mod._is_zero_count(float("nan")))
        self.assertFalse(fetch_mod._is_zero_count(float("inf")))
        self.assertFalse(fetch_mod._is_zero_count(10**10000))

    def test_probe_with_no_units_reports_clearly(self):
        """日期列表为空（如 --dates 全是空值）时窗口展开不出任何单元：要明确报错。"""
        job = minimal_job(window={"mode": "per_day", "date_tz": "UTC", "start_param": "s"})
        with self.assertRaises(utils.ConfigError) as ctx:
            self._fetcher(job).probe([])
        self.assertIn("days 为空", str(ctx.exception))

    def test_decorate_keeps_non_object_records_untouched(self):
        """add_fields 只给对象追加字段；非对象记录原样保留（上游解析已拦住这类数据）。"""
        job = minimal_job()
        job["request"]["add_fields"] = {"source_account": "账号A"}
        fetcher = self._fetcher(job)
        self.assertEqual(fetcher._decorate([{"id": 1}, "raw"]), [{"id": 1, "source_account": "账号A"}, "raw"])

    def test_window_params_are_merged_with_fixed_params(self):
        """窗口参数（startTime/endTime）要并进固定参数，而不是覆盖掉它们。"""
        job = minimal_job(
            window={
                "mode": "per_day",
                "date_tz": "UTC",
                "api_tz": "+08:00",
                "start_param": "startTime",
                "end_param": "endTime",
            }
        )
        job["request"]["params"] = {"app": "demo"}
        calls = []

        def fake(*args, **_kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}]}}

        fetcher = self._fetcher(job)
        unit = fetcher.build_units([date(2026, 9, 18)])[0]
        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = fetcher.fetch_unit(unit)
        self.assertEqual(records, [{"id": 1}])
        self.assertEqual(calls[0]["app"], "demo")
        self.assertIn("startTime", calls[0])
        self.assertIn("endTime", calls[0])

    def test_page_without_records_path_reports_diagnostics(self):
        """翻页中途 records_path 取不到：必须报错（静默当空页 = 少拉数据还显示成功）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        with mock.patch.object(http_mod, "request_once", return_value={"data": {}}):
            with self.assertRaises(RuntimeError) as ctx:
                self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("找不到 records_path", str(ctx.exception))

    def test_empty_page_after_total_reached_stops_cleanly(self):
        """总数已达、接口又回一页空：按"已翻完"正常收尾，不能报"无法确认"整窗失败。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_items_path": "data.totalCount",
                "delay_seconds": 0,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}, {"id": 2}]}},  # 首页：2 条，没带总数
            {"data": {"list": [], "totalCount": 2}},  # 末页之后：空页 + 总数 2
        ]
        with mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        self.assertEqual(responses, [])

    def test_stop_after_first_page_sends_one_request(self):
        """体检限量参数：只发一页就返回（数据量大的源不能因为体检被全量拉一遍）。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 2,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0,
            }
        )
        calls = []

        def fake(*args, **_kwargs):
            calls.append(dict(args[2]))
            return {"data": {"list": [{"id": 1}], "totalPages": 9}}

        with mock.patch.object(http_mod, "request_once", side_effect=fake):
            records = self._fetcher(job).fetch_unit(
                fetch_mod.FetchUnit("d", date(2026, 9, 18), None), stop_after_first_page=True
            )
        self.assertEqual([r["id"] for r in records], [1])
        self.assertEqual(len(calls), 1)

    def test_cursor_respects_param_as_string(self):
        """param_as_string 对游标同样生效（原来只有 page/size 字符串化）。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "param_as_string": True,
                "cursor_start": 1000,
                "page_size": 5,
            }
        )
        fetcher = self._fetcher(job)
        seen = []

        def fake_request(params, *a, **k):
            seen.append(dict(params))
            return {"next": None, "data": {"list": [{"id": 1}]}}

        with mock.patch.object(fetcher, "_do_request", side_effect=fake_request):
            fetcher.fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual(seen[0]["c"], "1000")
        self.assertEqual(seen[0]["size"], "5")

    def test_page_delay_sleeps_between_pages(self):
        """pagination.delay_seconds 是给限速接口留的间隔，翻页时必须真的等。"""
        job = minimal_job(
            pagination={
                "type": "page",
                "page_param": "current",
                "size_param": "size",
                "page_size": 1,
                "total_pages_path": "data.totalPages",
                "delay_seconds": 0.5,
            }
        )
        responses = [
            {"data": {"list": [{"id": 1}], "totalPages": 2}},
            {"data": {"list": [{"id": 2}], "totalPages": 2}},
        ]
        with (
            mock.patch.object(http_mod, "request_once", side_effect=lambda *a, **k: responses.pop(0)),
            mock.patch.object(fetch_mod.Fetcher, "_sleep", return_value=False) as sleep,
        ):
            records = self._fetcher(job).fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertEqual([r["id"] for r in records], [1, 2])
        sleep.assert_called_once()
        self.assertEqual(sleep.call_args.args[0], 0.5)  # 间隔秒数；第二参是本轮停止信号

    def test_cursor_not_advancing_is_error(self):
        """游标原样回显（cursor_path 配到恒定字段）：继续翻会把同一页拉满 max_pages 次、
        重复数据无上限累积——直接按配置错中止，而不是翻 2000 页。"""
        job = minimal_job(
            pagination={
                "type": "cursor",
                "cursor_param": "c",
                "cursor_path": "next",
                "page_size": 2,
                "delay_seconds": 0,
            }
        )
        fetcher = self._fetcher(job)
        calls = []

        def same_cursor(*_a, **_k):
            calls.append(1)
            return {"next": "same", "data": {"list": [{"id": 1}]}}

        with mock.patch.object(http_mod, "request_once", side_effect=same_cursor):
            with self.assertRaises(SystemExit) as ctx:
                fetcher.fetch_unit(fetch_mod.FetchUnit("d", date(2026, 9, 18), None))
        self.assertIn("游标未推进", str(ctx.exception))
        self.assertEqual(len(calls), 2)  # 第二页发现游标没动就中止，没有翻满 max_pages

    def test_window_retries_must_be_integer(self):
        """pagination.window_retries 写错：配置错直接退出，别在整窗重试里空等。"""
        job = minimal_job()
        with self.assertRaises(SystemExit) as ctx:
            self._fetcher(job).fetch_all([date(2026, 9, 18)], window_retries="两")
        self.assertIn("window_retries", str(ctx.exception))


class TestFourthPassFetchHttp(OfflineTestCase):
    """fetch/http：并发内存、timeout 校验、JSON 编码、Retry-After NaN、fail_if 数值比较。"""

    def test_timeout_must_be_positive(self):
        for bad in (0, -5):
            with self.assertRaises(SystemExit) as ctx:
                fetch_mod.Fetcher({"request": {"base_url": "http://x", "timeout_seconds": bad}}, Path("."))
            self.assertIn("timeout_seconds", str(ctx.exception))

    def test_retry_after_nan_is_ignored(self):
        """NaN 夹取不掉、传给 time.sleep 会抛错，让整条 429 重试链失效。"""
        response = mock.Mock()
        response.headers = {"Retry-After": "nan"}
        self.assertIsNone(http_mod._retry_after_seconds(response))

    def test_fail_if_compares_numbers_by_value(self):
        # 0.0 与 0 等价：not_equals 不误杀
        http_mod.check_fail_if({"code": 0.0}, [{"path": "code", "not_equals": 0}])
        # equals 方向不漏判
        with self.assertRaises(utils.FatalApiError):
            http_mod.check_fail_if({"code": 200}, [{"path": "code", "equals": 200.0}])
        # 字符串数字与数字等价（原行为保留）
        http_mod.check_fail_if({"code": "20000"}, [{"path": "code", "not_equals": 20000}])

    def test_gbk_json_body_requires_explicit_encoding(self):
        """GBK 的 JSON 不能静默按 UTF-8 replace 成乱码写库：要么显式配 json_encoding，要么报错。"""

        class Resp:
            status_code, headers, encoding = 200, {}, None
            content = '{"name": "张三"}'.encode("gbk")

            def raise_for_status(self):
                pass

        with mock.patch.object(http_mod, "requests") as rq:
            rq.request.return_value = Resp()
            with self.assertRaises(utils.ConfigError) as ctx:
                http_mod.request_once("GET", "http://x", {}, {}, "json", 30, True, True, None)
            self.assertIn("json_encoding", str(ctx.exception))
            got = http_mod.request_once("GET", "http://x", {}, {}, "json", 30, True, True, None, "gbk")
        self.assertEqual(got, {"name": "张三"})

    def test_worker_records_released_after_spool(self):
        """--workers>1 时已交付单元的 records 要在落盘后立即释放（future 会持有到函数返回）。"""

        class RecList(list):
            pass

        job = {
            "request": {"base_url": "http://x", "records_path": "data.list"},
            "window": {"mode": "per_day", "days": 4, "format": "%Y-%m-%d %H:%M:%S"},
        }
        days = [date(2026, 9, 10 + i) for i in range(4)]
        refs, still_alive = [], []

        def fake_fetch_unit(self, unit, **kwargs):
            return RecList([{"x": 1}])

        def on_records(records):
            gc.collect()
            alive = 0
            for w in refs:
                # 只解引用一次：w() 的临时强引用在表达式结束后消失，第二次 w() 可能是 None，
                # 原写法 len(w()) 会随机 TypeError（用例时好时坏）
                obj = w()
                if obj is not None and len(obj) > 0:
                    alive += 1
            still_alive.append(alive)
            refs.append(weakref.ref(records))

        with mock.patch.object(fetch_mod.Fetcher, "fetch_unit", fake_fetch_unit):
            fetcher = fetch_mod.Fetcher(job, Path("."))
            fetcher.fetch_all(days, workers=2, window_retries=0, on_records=on_records)
        self.assertEqual(still_alive, [0, 0, 0, 0])
