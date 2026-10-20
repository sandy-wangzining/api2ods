# -*- coding: utf-8 -*-
"""JSON 路径、CSV/ZIP/JSONL 解析与错误体拦截。"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
)

from api2ods import config as config_mod  # noqa: E402
from api2ods import http as http_mod  # noqa: E402
from api2ods import (  # noqa: E402
    parsers,
    utils,
)
from api2ods import spool as spool_mod  # noqa: E402


class TestGetPath(OfflineTestCase):
    data = {"data": {"list": [{"id": 1}, {"id": 2}], "totalPages": 3}, "ok": True}

    def test_simple_and_nested(self):
        self.assertTrue(parsers.get_path(self.data, "ok"))
        self.assertEqual(parsers.get_path(self.data, "data.totalPages"), 3)

    def test_index(self):
        self.assertEqual(parsers.get_path(self.data, "data.list[1].id"), 2)

    def test_missing_returns_default(self):
        self.assertIsNone(parsers.get_path(self.data, "data.nope.x"))
        self.assertEqual(parsers.get_path(self.data, "data.list[9]", default="d"), "d")

    def test_empty_path_returns_input(self):
        self.assertIs(parsers.get_path(self.data, ""), self.data)


class TestParsers(OfflineTestCase):
    def test_extract_json_records_ok(self):
        payload = {"data": {"list": [{"id": 1}]}}
        self.assertEqual(parsers.extract_json_records(payload, {"records_path": "data.list"}, "t"), [{"id": 1}])

    def test_extract_json_records_single_object(self):
        payload = {"data": {"item": {"id": 1}}}
        self.assertEqual(parsers.extract_json_records(payload, {"records_path": "data.item"}, "t"), [{"id": 1}])

    def test_extract_json_records_missing_raises_with_snippet(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.extract_json_records({"code": 1}, {"records_path": "data.list"}, "单次请求")
        message = str(ctx.exception)
        self.assertIn("records_path", message)
        self.assertIn("code", message)

    def test_extract_json_records_missing_ok(self):
        self.assertEqual(
            parsers.extract_json_records({"code": 1}, {"records_path": "data.list"}, "t", missing_ok=True), []
        )

    def test_csv_text(self):
        text = "a,b\n1,x\n2,y\n"
        records = parsers.parse_bytes(text.encode("utf-8"), {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])

    def test_skip_rows_bool_is_config_error(self):
        """skip_rows: true（笔误）不能 int(True)=1 静默切掉真表头。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(b"a,b\n1,2\n", {"format": "csv", "skip_rows": True}, "t")
        self.assertIn("skip_rows", str(ctx.exception))

    def test_csv_skip_rows_and_entry_field(self):
        text = "报表\n生成时间,今天\na,b\n1,x\n"
        records = parsers.parse_bytes(
            text.encode("utf-8"), {"format": "csv", "skip_rows": 2, "entry_field": "__file"}, "t"
        )
        # 非 ZIP 来源没有条目名：不能写入空串——CSV 本来就有同名列时会被整列清空
        self.assertEqual(records, [{"a": "1", "b": "x"}])

    def test_csv_entry_field_does_not_clobber_same_named_column(self):
        """CSV 自带 entry_field 同名列（非 ZIP 响应）时保留原值，不被空串覆盖。"""
        text = "__file,a\ncrash_2026.csv,1\n"
        records = parsers.parse_bytes(text.encode("utf-8"), {"format": "csv", "entry_field": "__file"}, "t")
        self.assertEqual(records, [{"__file": "crash_2026.csv", "a": "1"}])

    def test_jsonl(self):
        data = b'{"a": 1}\n\n{"a": 2}\n'
        records = parsers.parse_bytes(data, {"format": "jsonl"}, "t")
        self.assertEqual([r["a"] for r in records], [1, 2])

    def test_zip_total_uncompressed_cap(self):
        """单条目各自合规的"多条目炸弹"要被累计上限拦住（256MB 单条上限可被绕开）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "user,amount\nu2,6\n")
        with mock.patch.object(parsers, "MAX_ZIP_TOTAL_BYTES", 5):
            with self.assertRaises(utils.ConfigError) as ctx:
                parsers.parse_bytes(
                    buffer.getvalue(),
                    {"format": "csv", "unzip": True, "allow_multi_entry": True},
                    "t",
                )
        self.assertIn("累计", str(ctx.exception))

    def test_zip_truncated_entry_is_retryable_runtimeerror(self):
        """压缩流被截断时 zipfile 抛的是 EOFError（不是 BadZipFile）：要按"条目损坏、
        可能重拉就好"的可重试语义包成 RuntimeError，不能裸异常冒出。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
        boom = EOFError("Compressed file ended before the end-of-stream marker was reached")
        with mock.patch.object(zipfile.ZipFile, "read", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(buffer.getvalue(), {"format": "csv", "unzip": True}, "t")
        self.assertIn("读取失败", str(ctx.exception))

    def test_small_multiline_json_body_still_rejected(self):
        """小响应保持原语义：以 { 开头、解不成整包 JSON 的内容在非 jsonl 格式下按错误体拦下。"""
        data = b'{"code": 500}\n{"x": 1}'
        with self.assertRaises(RuntimeError):
            parsers._reject_json_error_body(data, {"format": "csv"}, "t")

    def test_wide_multiline_jsonl_not_whole_decoded(self):
        """单行宽于嗅探前缀的 JSONL 也要判成多行（扫整包字节，不看前 4KB）：
        不做整包解码，也就不再走"整包是一个 JSON 值"的错误体拦截。"""
        wide = '{"a": "' + "x" * 300 + '"}\n'
        data = (wide * 3).rstrip("\n").encode()  # 以 } 收尾：旧实现会按"截断的 JSON"报错
        with mock.patch.object(parsers, "_SNIFF_MAX_BYTES", 100):
            # jsonl 格式不做整包解码、也不做尾部采样（本来就是逐行 JSON）
            self.assertIsNone(parsers._reject_json_error_body(data, {"format": "jsonl"}, "t"))

    def test_big_multiline_response_not_whole_decoded(self):
        """超过嗅探阈值、开头 4KB 就有换行的内容不再整包解码（百 MB 级 JSONL 不该为看首字符
        付整包 str 的内存）：表现为不走"整包是一个 JSON 值"的判定。"""
        data = b'{"code": 500}\n{"x": 1}'
        with mock.patch.object(parsers, "_SNIFF_MAX_BYTES", 4):
            # jsonl 格式不做整包解码、也不做尾部采样（本来就是逐行 JSON）
            self.assertIsNone(parsers._reject_json_error_body(data, {"format": "jsonl"}, "t"))

    def _zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("amount-2026.csv", "user,amount\nu1,5\n")
            archive.writestr("cost-2026.csv", "user,cost\nu1,1\n")
        return buffer.getvalue()

    def test_zip_filter_entries(self):
        records = parsers.parse_bytes(
            self._zip_bytes(),
            {"format": "csv", "unzip": True, "entry_contains": "amount", "entry_field": "__file"},
            "t",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["user"], "u1")
        self.assertEqual(records[0]["__file"], "amount-2026.csv")

    def test_zip_duplicate_entry_names_all_parsed(self):
        """ZIP 允许重名条目：按名字读会读同一条目两遍、另一条静默丢数据——必须按条目分别解析。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("2024-06.csv", "user,amount\nu1,5\n")
            archive.writestr("2024-06.csv", "user,amount\nu2,6\n")
        records = parsers.parse_bytes(
            buffer.getvalue(),
            {"format": "csv", "unzip": True, "allow_multi_entry": True},
            "t",
        )
        self.assertEqual([(r["user"], r["amount"]) for r in records], [("u1", "5"), ("u2", "6")])

    def test_entry_field_collision_rejected(self):
        """entry_field 与源文件自带同名列冲突时会静默覆盖真实列值——必须报错（宁失败勿写错）。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "file,amount\nreal-1,5\n")
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(buffer.getvalue(), {"format": "csv", "unzip": True, "entry_field": "file"}, "t")
        self.assertIn("重名", str(ctx.exception))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            # 两行才不会被"整包是单个 JSON = 疑似错误体"的先检拦下（那是另一条防线）
            archive.writestr("a.jsonl", '{"file": "real-1", "amount": 5}\n{"file": "real-2", "amount": 6}\n')
        with self.assertRaises(utils.ConfigError):
            parsers.parse_bytes(buffer.getvalue(), {"format": "jsonl", "unzip": True, "entry_field": "file"}, "t")

    def test_zip_no_match_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True, "entry_contains": "nope"}, "t")

    def test_bad_zip_raises(self):
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b"not a zip", {"format": "csv", "unzip": True}, "t")

    def test_zip_multiple_entries_without_filter_raises(self):
        """没筛条目却有多份文件（表头还可能不同）：不能无脑串成一份记录。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True}, "t")
        self.assertIn("entry_contains", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(self._zip_bytes(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t")
        self.assertIn("表头不一致", str(ctx.exception))

    def test_zip_multi_entry_same_columns_different_order_merges(self):
        """同构条目（列集合相同、列序不同）要能合并：列序变化不该被当成"表头不一致"。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "amount,user\n6,u2\n")
        records = parsers.parse_bytes(
            buffer.getvalue(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t"
        )
        self.assertEqual(records[0]["amount"], "5")
        self.assertEqual(records[1]["amount"], "6")

    def test_zip_multi_entry_same_headers_merge(self):
        """表头一致的多条目才允许合并。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a.csv", "user,amount\nu1,5\n")
            archive.writestr("b.csv", "user,amount\nu2,6\n")
        records = parsers.parse_bytes(
            buffer.getvalue(), {"format": "csv", "unzip": True, "allow_multi_entry": True}, "t"
        )
        self.assertEqual(records, [{"user": "u1", "amount": "5"}, {"user": "u2", "amount": "6"}])

    def test_encoding_mismatch_warns_and_strict_raises(self):
        """GBK 字节按默认编码解出乱码：默认告警（行数仍 >0），strict_encoding=true 直接报错。"""
        gbk = "用户名,金额\nu1,5\n".encode("gbk")
        records = parsers.parse_bytes(gbk, {"format": "csv"}, "t")
        self.assertEqual(len(records), 1)  # 默认仍按容错处理
        self.assertIn("�", list(records[0])[0])  # 列名是乱码（所以会有警告日志）
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(gbk, {"format": "csv", "strict_encoding": True}, "t")
        self.assertIn("gbk", str(ctx.exception))
        self.assertEqual(list(parsers.parse_bytes(gbk, {"format": "csv", "encoding": "gbk"}, "t")[0])[0], "用户名")

    def test_parse_payload_dispatch(self):
        records = parsers.parse_payload(b"a\n1\n", {"response_type": "bytes"}, {"format": "csv"}, "t")
        self.assertEqual(records, [{"a": "1"}])

    def test_csv_skip_until_summary_section(self):
        text = (
            "Settlement Summary\nBatch,Total\nB1,10\n"
            "Settlement Date,Settlement Batch ID,Transaction ID\n"
            "2026-09-18,B1,T1\n2026-09-18,B1,T2\n"
        )
        records = parsers.parse_bytes(
            text.encode("utf-8"), {"format": "csv", "skip_until": "Settlement Date,Settlement Batch ID"}, "t"
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["Transaction ID"], "T1")

    def test_csv_skip_until_not_found_returns_empty(self):
        """空文件/纯空白 = 当天真的没出账：按空结果处理，不算失败。"""
        records = parsers.parse_bytes(b"", {"format": "csv", "skip_until": "明细"}, "t")
        self.assertEqual(records, [])
        self.assertEqual(parsers.parse_bytes(b"\n  \n", {"format": "csv", "skip_until": "明细"}, "t"), [])

    def test_csv_skip_until_not_found_with_content_raises(self):
        """有内容却找不到明细段 = 结构变了/拿到错误页，必须报错（否则静默少拉一天）。"""
        NL = bytes([10])
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(
                b"left,right" + NL + b"1,2" + NL,
                {"format": "csv", "skip_until": "Settlement Date"},
                "settlement",
            )
        self.assertIn("Settlement Date", str(ctx.exception))

    def test_csv_unterminated_quote_raises(self):
        """引号未闭合时 csv 会把后续行吞进同一个字段，行数照常>0——必须报错。"""
        bad = b'a,b,c\n1,"x\n2,3,4\n5,6,7\n'
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(bad, {"format": "csv"}, "报表")
        self.assertIn("CSV 解析失败", str(ctx.exception))

    def test_bom_stripped_in_strict_encoding_mode(self):
        """strict_encoding=true 也要去 BOM，否则列名变成 \\ufeffdate，下游静默取空。"""
        data = "date,amount\n2026-09-18,10\n".encode("utf-8-sig")
        for strict in (False, True):
            records = parsers.parse_bytes(data, {"format": "csv", "encoding": "utf-8", "strict_encoding": strict}, "t")
            self.assertEqual(list(records[0]), ["date", "amount"], f"strict={strict}")

    def test_multi_char_delimiter_rejected(self):
        """delimiter 写 "\\t"（两个字面字符）是常见笔误，跨 Python 版本行为还不一致。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers.parse_bytes(b"a||b\n1||2\n", {"format": "csv", "delimiter": "||"}, "t")
        self.assertIn("单个字符", str(ctx.exception))

    def test_csv_extra_columns_raise(self):
        """数据行列数多于表头：多出来的字段会被 csv 静默丢掉，宁可报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,b\n1,2,3,4\n", {"format": "csv"}, "t")
        self.assertIn("列数多于表头", str(ctx.exception))

    def test_csv_duplicate_headers_raise(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,a\n1,2\n", {"format": "csv"}, "t")
        self.assertIn("重复列名", str(ctx.exception))

    def test_json_error_body_is_reported(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"respCode":"50217","respMsg":"No api query permissions"}', {"format": "csv"}, "d")
        self.assertIn("50217", str(ctx.exception))


class TestBomTolerance(OfflineTestCase):
    def test_job_file_with_bom_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text('{"job": "带BOM"}', encoding="utf-8-sig")
            self.assertEqual(config_mod.load_json_file(path, "作业配置文件")["job"], "带BOM")

    def test_response_with_bom_parses(self):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.content = '{"code": 0}'.encode("utf-8-sig")
        response.text = '{"code": 0}'
        with mock.patch.object(http_mod, "requests") as fake_requests:
            fake_requests.request.return_value = response
            got = http_mod.request_once("GET", "https://x", {}, {}, "json", 5, True, True, None)
        self.assertEqual(got, {"code": 0})

    def test_bytes_response_with_bom_json_error_body_detected(self):
        """文件接口返回带 BOM 的 JSON 错误体时，要报"返回了 JSON"而不是误导性的"期望 ZIP"。"""
        payload = '{"error": "no permission"}'.encode("utf-8-sig")
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(payload, {"format": "csv"}, "导出")
        self.assertIn("返回了 JSON", str(ctx.exception))


class TestSkipRowsKeepsLineEndings(OfflineTestCase):
    def test_multiline_quoted_field_survives_skip_rows(self):
        text = '表头行\ncol1,col2\n"带\n换行的值",x\n'
        records = parsers._parse_text(text, {"format": "csv", "skip_rows": 1})
        self.assertEqual(records[0]["col1"], "带\n换行的值")

    def test_skip_until_keeps_line_endings(self):
        # 标记行本身当表头，明细从下一行开始
        text = '说明段\ncol1,col2\n"a\nb",x\n'
        records = parsers._parse_text(text, {"format": "csv", "skip_until": "col1"})
        self.assertEqual(records[0]["col1"], "a\nb")

    def test_negative_skip_rows_rejected(self):
        # ConfigError：配置写错，不该被当成网络抖动去做整窗重试
        with self.assertRaises(utils.ConfigError):
            parsers._parse_text("a,b\n1,2\n", {"format": "csv", "skip_rows": -1})

    def test_skip_rows_and_skip_until_conflict_reported(self):
        """标记行落在被跳过的前几行里时，报错要说"两个配置冲突"，不能只说"结构变了"。"""
        text = "汇总\n标记行\n表头\n1,2\n"
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers._parse_text(text, {"format": "csv", "skip_rows": 2, "skip_until": "标记行"})
        self.assertIn("不要同时配", str(ctx.exception))

    def test_skip_rows_swallowing_everything_is_empty_result(self):
        """skip_rows 正好把内容全跳空：当天真的没出账，按空结果处理，不是"两配置冲突"。"""
        text = "仅有的前置说明\n第二行\n"
        records = parsers._parse_text(text, {"format": "csv", "skip_rows": 2, "skip_until": "表头"})
        self.assertEqual(records, [])


class TestFourthPassParsers(OfflineTestCase):
    """parsers.py：行边界、空表头、JSON 防呆、空对象、空白行、空列名。"""

    def test_split_lines_ignores_non_newline_breaks(self):
        """\x0b/\x0c/  等不是 csv/JSONL 的行边界（splitlines 会多认 6 种）。"""
        got = parsers._parse_text("报表\x0c\n生成时间,今天\na,b\n1,x\n", {"format": "csv", "skip_rows": 2}, label="t")
        self.assertEqual(got, [{"a": "1", "b": "x"}])

    def test_jsonl_empty_object_line_is_skipped(self):
        """JSONL 里的空对象行（{}）与 Items:{} / [{}] 同义：跳过，不写全 NULL 假记录。"""
        body = '{"a": 1}' + chr(10) + "{}" + chr(10)
        self.assertEqual(parsers.parse_bytes(body.encode(), {"format": "jsonl"}, "t"), [{"a": 1}])

    def test_jsonl_empty_object_skipped_before_entry_field(self):
        """entry_field 合并前先判空：ZIP 里 {} 行不能变成只含条目名的假记录。"""
        buf = io.BytesIO()
        line = chr(10)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x.jsonl", '{"a": 1}' + line + "{}" + line)
        got = parsers.parse_bytes(buf.getvalue(), {"format": "jsonl", "unzip": True, "entry_field": "src_file"}, "t")
        self.assertEqual(got, [{"a": 1, "src_file": "x.jsonl"}])

    def test_jsonl_line_with_u2028_kept(self):
        """U+2028 是合法 JSON 字符（工具自己 dump 的记录就有），不能被劈成两行。"""
        first = spool_mod.dump_record({"a": "x y"})
        second = spool_mod.dump_record({"b": 1})
        # 两条记录、最后一条不带换行：既验证 U+2028 不参与切行，也验证 JSONL 规范允许最后一行无换行符。
        self.assertEqual(
            parsers.parse_bytes((first + "\n" + second).encode(), {"format": "jsonl"}, "t"), [{"a": "x y"}, {"b": 1}]
        )

    def test_csv_blank_first_line_raises(self):
        """有内容但表头是空行 → 报错（原来静默 0 行，allow_empty 时会先删再填清空分区）。"""
        for payload in (b"\na,b\n1,2\n", b"\r\na,b\r\n1,2\r\n"):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(payload, {"format": "csv"}, "结算")
            self.assertIn("空行", str(ctx.exception))

    def test_jsonl_single_object_requires_explicit_opt_in(self):
        """整个响应只有一个 JSON 对象时与错误体无法区分：默认拦下，显式开关才放行。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'{"id":1}\n', {"format": "jsonl"}, "t")
        self.assertIn("allow_single_record", str(ctx.exception))
        self.assertEqual(
            parsers.parse_bytes(b'{"id":1}\n', {"format": "jsonl", "allow_single_record": True}, "t"),
            [{"id": 1}],
        )

    def test_error_body_detection_covers_array_and_broken_json(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b'[{"code":500,"msg":"err"}]', {"format": "csv"}, "t")
        self.assertIn("JSON", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b'{"code": NaN}', {"format": "csv"}, "t")

    def test_csv_header_starting_with_bracket_not_mistaken(self):
        """合法 CSV 表头以 [ 开头时不能误报成 JSON 错误体。"""
        self.assertEqual(
            parsers.parse_bytes("[日期],[金额]\n1,2\n".encode(), {"format": "csv"}, "t"),
            [{"[日期]": "1", "[金额]": "2"}],
        )

    def test_records_path_empty_object_is_zero_rows(self):
        """Items: {} 表示"没有数据"，不能包成一条全 NULL 的假记录。"""
        self.assertEqual(parsers.extract_json_records({"data": {"list": {}}}, {"records_path": "data.list"}, "t"), [])

    def test_single_column_blank_value_kept(self):
        """单列文件里"值为空白"是合法记录；多列的全空白行仍按排版垃圾跳过。"""
        self.assertEqual(parsers.parse_bytes(b"v\n \nx\n", {"format": "csv"}, "t"), [{"v": " "}, {"v": "x"}])
        self.assertEqual(parsers.parse_bytes(b"a,b\n \n1,2\n", {"format": "csv"}, "t"), [{"a": "1", "b": "2"}])

    def test_empty_column_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"a,b,\n1,2,3\n", {"format": "csv"}, "t")
        self.assertIn("空列名", str(ctx.exception))


class TestParserErrorBranches(OfflineTestCase):
    """parsers 的解析报错分支：坏路径、坏表头、坏 JSONL、坏 ZIP。"""

    def test_malformed_path_returns_default(self):
        """路径片段里有非法字符（a[b]）：返回默认值，不能崩。"""
        self.assertEqual(parsers.get_path({"a": 1}, "a[b]", default="d"), "d")
        self.assertIsNone(parsers.get_path({"a": 1}, "a[1"))

    def test_records_must_be_array_or_object(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.ensure_object_records(5, "单次请求")
        message = str(ctx.exception)
        self.assertIn("不是数组/对象", message)
        self.assertIn("int", message)

    def test_empty_object_inside_array_is_normalized(self):
        """数组里的空对象（Items: [{}]）与 Items: {} 同义：归一成空列表，不写全 NULL 假记录。"""
        self.assertEqual(parsers.ensure_object_records([{}], "单次请求"), [])
        self.assertEqual(parsers.ensure_object_records([{"a": 1}, {}], "单次请求"), [{"a": 1}])

    def test_large_single_line_json_rejected_without_full_decode(self):
        """>1MB 的单行 JSON：不整包 decode（OOM 源），按首/尾字节判定拦下。"""
        big = b"{" + b'"k": 1,' * 200000 + b'"z": 2}'
        self.assertGreater(len(big), 1024 * 1024)
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(big, {"format": "csv"}, "t")
        self.assertIn("首尾字符与 JSON 一致", str(ctx.exception))

    def test_cr_only_file_not_treated_as_text_error_body(self):
        """CR-only 换行文件不会被当"单行文本错误体"（那是"无任何行界"的判据）：
        交给 csv 层按真实结构报错（csv 不认裸 CR，报 new-line 提示），而不是误导性的
        "疑似纯文本错误体"。"""
        CR = bytes([13])
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"amount" + CR + b"100" + CR + b"200", {"format": "csv"}, "t")
        message = str(ctx.exception)
        self.assertNotIn("单行文本", message)
        self.assertIn("表头解析失败", message)

    def test_small_zip_not_treated_as_text_error_body(self):
        """二进制（小 ZIP）的字节里恰好没有换行/分隔符时，不能被当成"单行文本错误体"。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.csv", "id,amount")
        data = buf.getvalue()
        self.assertIsNone(parsers._reject_json_error_body(data, {"format": "csv"}, "t"))

    def test_skip_rows_conflict_only_when_marker_in_skipped_part(self):
        """skip_rows 与 skip_until 同配：标记确实在被跳部分才报配置错；否则按可重试处理。"""
        NL = chr(10)
        with self.assertRaises(config_mod.ConfigError):
            parsers._parse_text(
                "标题段" + NL + "a,b" + NL + "1,2",
                {"format": "csv", "skip_rows": 1, "skip_until": "标题段"},
                "t",
            )
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text(
                "<html>err</html>" + NL + "x,y",
                {"format": "csv", "skip_rows": 1, "skip_until": "明细"},
                "t",
            )
        self.assertIn("找不到明细段标记", str(ctx.exception))

    def test_plain_text_error_body_rejected_on_csv_path(self):
        """单行纯文本错误体（非 JSON，如 503 Service Unavailable）：同样拦下
        （当 CSV 会静默产出 0 行、调度按零数据收尾）。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"503 Service Unavailable", {"format": "csv"}, "结算")
        self.assertIn("单行文本", str(ctx.exception))

    def test_single_column_header_only_csv_requires_opt_in(self):
        """单列 CSV 只有表头时，需要 parse.allow_single_line 显式放行（默认仍拦纯文本错误体）。"""
        with self.assertRaises(RuntimeError):
            parsers.parse_bytes(b"id\n", {"format": "csv"}, "单列")
        self.assertEqual(parsers.parse_bytes(b"id\n", {"format": "csv", "allow_single_line": True}, "单列"), [])

    def test_large_multiline_json_rejected_by_tail_probe(self):
        """大且多行的 JSON（pretty-printed）：不整包解码，但按尾部采样照样拦下
        （不拦会把 JSON 当 CSV 解析成垃圾记录写进 ODS）。"""
        NL = bytes([10])
        big = NL.join([b"{"] + [b'  "k": 1,'] * 260000 + [b"}"])  # >1MB
        self.assertGreater(len(big), 1024 * 1024)
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(big, {"format": "csv"}, "结算")
        self.assertIn("尾部采样", str(ctx.exception))

    def test_scalar_error_body_rejected_on_csv_path(self):
        """HTTP 200 + JSON 标量错误体（"rate limit exceeded" / 500 / null）：当 CSV 会静默
        产出 0 行（唯一“列名”就是错误消息本身），必须拦下而不是按空数据收尾。"""
        for payload in (b'"rate limit exceeded"', b"500", b"null", b"true", b"502\n", b'"rate limit exceeded"\n'):
            with self.assertRaises(RuntimeError) as ctx:
                parsers.parse_bytes(payload, {"format": "csv"}, "结算")
            self.assertIn("JSON 标量", str(ctx.exception))

    def test_single_line_text_with_separator_is_not_scalar_error_body(self):
        """单行但带列分隔符（或本来就多行）的文本不按标量错误体处理，仍走正常解析。"""
        self.assertEqual(parsers.parse_bytes(b"a,b", {"format": "csv"}, "结算"), [])
        self.assertEqual(parsers.parse_bytes(b"a,b\n", {"format": "csv"}, "结算"), [])
        self.assertEqual(parsers.parse_bytes(b"a,b\n1,2\n", {"format": "csv"}, "结算"), [{"a": "1", "b": "2"}])

    def test_header_with_surrounding_spaces_is_error(self):
        """表头列名带首尾空格（" amount"，导出常见）：json 键原样带空格、下游
        get_json_object 取不到——与空列名同属静默丢列，直接报错。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(b"id, amount\n1,2\n", {"format": "csv"}, "结算")
        self.assertIn("空白", str(ctx.exception))

    def test_oversized_header_reports_csv_error(self):
        """表头行超过 csv 字段上限（解析层把上限调到 7MB，这里压小来模拟）：给可读报错。"""
        old_limit = csv.field_size_limit(10)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                parsers._parse_text("a" * 50 + ",b\n1,2\n", {"format": "csv"}, label="报表")
        finally:
            csv.field_size_limit(old_limit)
        self.assertIn("表头解析失败", str(ctx.exception))
        self.assertIn("报表", str(ctx.exception))

    def test_empty_text_returns_no_records(self):
        """空文件（当天真的没出账）：返回空列表，不是报错。"""
        self.assertEqual(parsers._parse_text("", {"format": "csv"}), [])

    def test_blank_rows_are_skipped(self):
        """报表里的空行（只有分隔符）不能变成一条全空记录写进 ODS。"""
        records = parsers._parse_text("a,b\n1,2\n,\n3,4\n", {"format": "csv"})
        self.assertEqual(records, [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])

    def test_jsonl_with_nan_reports_line_snippet(self):
        """NaN 不是合法 JSON（落进 ODS 后下游取不到值）：报错要带行内容。"""
        with self.assertRaises(RuntimeError) as ctx:
            parsers._parse_text('{"a": 1}\n{"b": NaN}\n', {"format": "jsonl"})
        message = str(ctx.exception)
        self.assertIn("JSONL 解析失败", message)
        self.assertIn("NaN", message)

    def test_jsonl_entry_field_is_stamped(self):
        """ZIP 多条目合并时用 entry_field 标记来源文件（否则分不清数据出自哪个包）。"""
        records = parsers._parse_text('{"a": 1}\n', {"format": "jsonl", "entry_field": "__file"}, entry="part1.jsonl")
        self.assertEqual(records, [{"a": 1, "__file": "part1.jsonl"}])

    def test_unknown_format_is_a_config_error(self):
        """格式写错是配置问题：抛 ConfigError（SystemExit 子类），别被当成网络抖动重试。"""
        with self.assertRaises(utils.ConfigError) as ctx:
            parsers._parse_text("x", {"format": "xml"})
        self.assertIn("xml", str(ctx.exception))

    def test_corrupted_zip_entry_reports_name(self):
        """条目损坏（截断包/CRC 对不上）：报出是哪个条目，方便让源方重导。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("amount.csv", "user,amount\nu1,5\n")
        data = bytearray(buffer.getvalue())
        index = data.index(b"user,amount")  # 改一个字节让 CRC-32 对不上
        data[index] = ord("X")
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_bytes(bytes(data), {"format": "csv", "unzip": True}, "导出")
        message = str(ctx.exception)
        self.assertIn("amount.csv", message)
        self.assertIn("读取失败", message)

    def test_bytes_response_type_requires_binary_payload(self):
        with self.assertRaises(RuntimeError) as ctx:
            parsers.parse_payload({"code": 0}, {"response_type": "bytes"}, {"format": "csv"}, "导出")
        message = str(ctx.exception)
        self.assertIn("期望二进制响应", message)
        self.assertIn("dict", message)


class TestLargeJsonResponse(OfflineTestCase):
    def test_skip_until_accepts_quoted_header_fields(self):
        for fmt, separator in (("csv", ","), ("tsv", "\t")):
            text = '"report"\n"序号"' + separator + '"金额"\n"1"' + separator + '"100"\n'
            with self.subTest(fmt=fmt):
                rows = parsers._parse_text(text, {"format": fmt, "skip_until": "序号"}, label="test")
                self.assertEqual(rows, [{"序号": "1", "金额": "100"}])

    def test_large_jsonl_single_record_honors_explicit_opt_in(self):
        body = {"value": "x" * (parsers._SNIFF_MAX_BYTES + 10) + "\u2028\u0a00中"}
        text = json.dumps(body, ensure_ascii=False)
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            for suffix in ("", "\n", "\r", "\r\n", "\n \r\n\t"):
                with self.subTest(encoding=encoding, suffix=suffix):
                    data = (text + suffix).encode(encoding)
                    cfg = {"format": "jsonl", "encoding": encoding, "allow_single_record": True}
                    self.assertEqual(parsers.parse_bytes(data, cfg, "test"), [body])
                    cfg["allow_single_record"] = False
                    with self.assertRaisesRegex(RuntimeError, "JSON"):
                        parsers.parse_bytes(data, cfg, "test")

    def test_large_jsonl_multiple_records_remain_supported(self):
        rows = [{"value": "x" * (parsers._SNIFF_MAX_BYTES + 10)}, {"value": "second"}]
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            for newline in ("\n", "\r", "\r\n"):
                with self.subTest(encoding=encoding, newline=newline):
                    text = newline.join(json.dumps(row) for row in rows) + newline
                    cfg = {"format": "jsonl", "encoding": encoding}
                    self.assertEqual(parsers.parse_bytes(text.encode(encoding), cfg, "test"), rows)

    def test_single_record_sniff_decodes_in_bounded_chunks(self):
        sizes = []
        real_factory = parsers.codecs.getincrementaldecoder

        def factory(encoding):
            def decoder(**kwargs):
                actual = real_factory(encoding)(**kwargs)

                def decode(chunk, **options):
                    sizes.append(len(chunk))
                    return actual.decode(chunk, **options)

                return type("Decoder", (), {"decode": staticmethod(decode)})()

            return decoder

        data = ('{"value":"' + "x" * (parsers._SNIFF_MAX_BYTES + 10) + '"}\r\n').encode()
        with mock.patch.object(parsers.codecs, "getincrementaldecoder", side_effect=factory):
            self.assertFalse(parsers._has_multiple_content_lines(data, "utf-8"))
        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), 65536)

    def test_large_json_with_bom_or_whitespace_is_rejected(self):
        text = '{"value":"' + "x" * (parsers._SNIFF_MAX_BYTES + 10) + '"}'
        payloads = [("  " + text).encode(), b"\xef\xbb\xbf" + text.encode()]
        payloads.extend(text.encode(encoding) for encoding in ("utf-16", "utf-32"))
        for payload in payloads:
            with self.subTest(prefix=payload[:4]), self.assertRaisesRegex(RuntimeError, "JSON"):
                parsers._reject_json_error_body(payload, {"format": "csv"}, "test")
