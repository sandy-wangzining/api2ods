# -*- coding: utf-8 -*-
"""拉取：作业 → 请求单元（按天/按区间）→ 翻页/解析 → 交给调用方（落盘 Spool）。

数据流：
    Fetcher.build_units(days)      把"要拉哪几天"展开成请求单元（每天一个 / 整区间一个）
    Fetcher.fetch_unit(unit)       单个单元：拼参数 → 鉴权 → 翻页 → 取出记录列表
    Fetcher.fetch_all(...)         并发跑所有单元；每个单元一拉完就通过 on_records 回调交出去
                                   （调用方写 Spool 落盘，避免全量数据堆在内存里）

失败策略：单个单元失败会整窗重试 N 次（window_retries），最终仍失败则记入 failures；
只要还有失败单元，调用方就不写库（防止分区缺数）；重跑整条命令即可（写库是"先删再填"幂等）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .auth import AuthApplier
from .dates import window_param_sets
from .http import request_with_retry
from .parsers import extract_json_records, get_path, parse_payload
from .utils import log, redact


@dataclass
class FetchUnit:
    """一次逻辑请求：某天（per_day 模式）或某个区间（range 模式）。"""

    label: str              # 日志里显示的名字，如 "2026-09-18"
    day: date               # 该单元的基准日（决定窗口参数 / 用于日志）
    params: dict | None     # 窗口参数（startTime/endTime 等）；单次请求模式为 None


class Fetcher:
    """按作业配置请求 API：页码/游标分页、JSON/文件解析、整窗重试。"""

    def __init__(self, job: dict, job_dir: Path):
        request_cfg = job.get("request") or {}
        self.job = job
        self.request_cfg = request_cfg
        self.parse_cfg = job.get("parse") or {}
        self.pagination = job.get("pagination") or {}
        self.method = str(request_cfg.get("method") or "GET").upper()

        # 完整 URL = base_url + path（path 为空时直接用 base_url）
        base_url = str(request_cfg.get("base_url") or "").rstrip("/")
        path = str(request_cfg.get("path") or "")
        self.url = base_url + ("/" + path.lstrip("/") if path else "")

        self.body_type = str(request_cfg.get("body_type") or "json").lower()
        self.timeout = float(request_cfg.get("timeout_seconds") or 30)
        self.response_type = str(request_cfg.get("response_type") or "json").lower()
        self.fail_if = request_cfg.get("fail_if") or []
        self.retry_times = int(request_cfg.get("retry_times") or 5)
        self.retry_delay = float(request_cfg.get("retry_delay") or 15)
        self.verify = bool(request_cfg.get("verify", True))
        self.proxies = request_cfg.get("proxies") or None
        self.records_path = str(request_cfg.get("records_path") or "")
        # records_missing=empty：接口用"空对象"表示无数据（如阿里云 Items: {}）时不报错
        self.records_missing = str(request_cfg.get("records_missing") or "error").lower()
        # add_fields：给每条记录追加固定字段（多账号打来源标记）
        self.add_fields = dict(request_cfg.get("add_fields") or {})
        self.base_params = dict(request_cfg.get("params") or {})
        self.base_headers = {str(k): str(v) for k, v in (request_cfg.get("headers") or {}).items()}
        # params_in=headers：请求参数（含窗口参数）最终放进请求头而不是 URL（Onerway 结算文件接口）
        self.params_in = str(request_cfg.get("params_in") or "query").lower()
        self.auth = AuthApplier(request_cfg, job_dir)
        self._job_dir = job_dir

    # ------------------------------------------------------------------ 计划

    def build_units(self, days: list[date]) -> list[FetchUnit]:
        """日期列表 → 请求单元列表。

        - per_day（默认）：每天一个单元；
        - range：整个区间一个单元；
        - 没配 window：只有一个单元（不做时间过滤）。
        """
        window = self.job.get("window") or {}
        mode = str(window.get("mode") or "per_day").lower()
        param_sets = window_param_sets(self.job, days)
        if not window or mode == "range":
            label = f"{days[0]}~{days[-1]}" if window else "单次请求"
            return [FetchUnit(label=label, day=days[0], params=param_sets[0])]
        return [FetchUnit(label=str(day), day=day, params=params)
                for day, params in zip(days, param_sets)]

    def unit_count(self, days: list[date]) -> int:
        return len(self.build_units(days))

    def probe(self, days: list[date]) -> tuple[str, int]:
        """体检（--check）专用：页码分页只拉 1 条、最多 1 页，其余模式拉第一个单元。"""
        units = self.build_units(days)
        if not units:
            raise RuntimeError("没有可执行的请求单元")
        unit = units[0]
        page_type = str(self.pagination.get("type") or "none").lower()
        if page_type == "page":
            records = self.fetch_unit(unit, page_size_override=1, max_pages_override=1)
        else:
            records = self.fetch_unit(unit)
        return unit.label, len(records)

    # ------------------------------------------------------------------ 单次

    def _decorate(self, records: list) -> list:
        """给记录追加固定字段（add_fields）；API 已有同名键时保留 API 原值。"""
        if not self.add_fields:
            return records
        decorated = []
        for record in records:
            if isinstance(record, dict):
                merged = dict(record)
                for key, value in self.add_fields.items():
                    merged.setdefault(key, value)
                decorated.append(merged)
            else:
                decorated.append(record)
        return decorated

    def _do_request(self, params: dict, desc: str, expect_json: bool):
        """拼请求头 → 鉴权 → 带重试地发请求（所有请求的唯一出口，便于统一加保护）。

        params_in=headers 时：先让鉴权把签名写进 params/headers，再把所有参数搬到请求头、
        清空 URL 参数（有些接口要求所有参数都走 Header）。
        """
        headers = dict(self.base_headers)
        self.auth.apply(params, headers, self.method)
        if self.params_in == "headers":
            headers.update({str(key): str(value) for key, value in params.items()})
            params.clear()
        return request_with_retry(
            self.method, self.url, params, headers, self.body_type, self.timeout,
            expect_json=expect_json, fail_if=self.fail_if,
            retry_times=self.retry_times, retry_delay=self.retry_delay,
            verify=self.verify, proxies=self.proxies, desc=desc,
        )

    def fetch_unit(self, unit: FetchUnit, page_size_override: int | None = None,
                   max_pages_override: int | None = None) -> list[dict]:
        """执行一个请求单元，返回该单元的记录列表。

        page_size_override / max_pages_override 仅供 --check 限量使用（不用于正式同步）。
        """
        # 基础参数 + 该单元的窗口参数（如 startTime/endTime）
        params = dict(self.base_params)
        if unit.params:
            params.update(unit.params)

        page_type = str(self.pagination.get("type") or "none").lower()
        if page_type == "none":
            payload = self._do_request(params, unit.label, expect_json=self.response_type != "bytes")
            records = parse_payload(payload, self.request_cfg, self.parse_cfg, unit.label,
                                    missing_ok=self.records_missing == "empty")
            return self._decorate(records)
        return self._decorate(self._fetch_pages(unit, params, page_size_override, max_pages_override))

    def _fetch_pages(self, unit: FetchUnit, base_params: dict,
                     page_size_override: int | None = None,
                     max_pages_override: int | None = None) -> list[dict]:
        """页码 / 游标分页循环。

        终止条件（按优先级）：
        - page：翻到 total_pages_path 指示的末页，或 total_items_path 累计条数已够；
          接口回报的条数没拉完却返回空页 → 视为接口抖动，报错触发整窗重试；
        - cursor：返回里取不到下一页游标为止；
        - 任何模式：超过 max_pages 页主动中止（防死循环）。
        """
        page_cfg = self.pagination
        page_type = str(page_cfg.get("type") or "none").lower()
        page_param = str(page_cfg.get("page_param") or "page")
        size_param = str(page_cfg.get("size_param") or "size")
        page_size = int(page_size_override or page_cfg.get("page_size") or 100)
        param_as_string = bool(page_cfg.get("param_as_string"))   # 页码/页大小按字符串传（有些接口要求）
        total_pages_path = str(page_cfg.get("total_pages_path") or "")
        total_items_path = str(page_cfg.get("total_items_path") or "")
        cursor_param = str(page_cfg.get("cursor_param") or "cursor")
        cursor_path = str(page_cfg.get("cursor_path") or "")
        cursor_start = page_cfg.get("cursor_start")
        delay = float(page_cfg.get("delay_seconds") or 0)          # 翻页间隔，防限流
        max_pages = int(max_pages_override or page_cfg.get("max_pages") or 2000)

        records: list[dict] = []
        current = 1
        cursor = cursor_start
        for page_index in range(1, max_pages + 1):
            # ① 组装本页参数
            params = dict(base_params)
            if page_type == "page":
                params[page_param] = str(current) if param_as_string else current
                params[size_param] = str(page_size) if param_as_string else page_size
            elif page_type == "cursor" and cursor is not None:
                params[cursor_param] = cursor

            # ② 发请求 + 取记录数组
            payload = self._do_request(params, f"{unit.label} 第{page_index}页", expect_json=True)
            page_records = get_path(payload, self.records_path, default=None)
            if page_records is None:
                if self.records_missing == "empty":
                    log(f"  {unit.label}：records_path 未命中，按空结果处理（records_missing=empty）")
                    break
                if page_index == 1:
                    # 第一页就取不到记录数组 = 配置写错了，抛带诊断信息的错误
                    extract_json_records(payload, self.request_cfg, unit.label)
                break
            if isinstance(page_records, dict):
                page_records = [page_records]
            if not isinstance(page_records, list):
                raise RuntimeError(f"{unit.label} records_path 指向的不是数组/对象："
                                   f"{type(page_records).__name__}")
            records.extend(page_records)

            # ③ 判断是否翻完
            if page_type == "page":
                total_pages = get_path(payload, total_pages_path, default=None) if total_pages_path else None
                if total_pages is not None:
                    try:
                        if current >= int(total_pages):
                            break
                    except (TypeError, ValueError):
                        pass
                total_items = get_path(payload, total_items_path, default=None) if total_items_path else None
                if total_items is not None:
                    try:
                        reached_end = current * page_size >= int(total_items)
                    except (TypeError, ValueError):
                        reached_end = False
                    if not page_records and not reached_end:
                        raise RuntimeError(
                            f"{unit.label} 第 {current} 页返回为空，但仍有数据未拉完"
                            f"（TotalCount={total_items}），可能是接口抖动"
                        )
                    if reached_end:
                        break
                if not page_records:
                    break
                current += 1
            else:  # cursor
                next_cursor = get_path(payload, cursor_path, default=None)
                if next_cursor in (None, ""):
                    break
                cursor = next_cursor

            # ④ 翻页间隔 + 体检模式的提前返回
            if delay > 0:
                time.sleep(delay)
            if max_pages_override is not None and page_index >= max_pages_override:
                return records
        else:
            raise RuntimeError(
                f"{unit.label} 分页超过 max_pages={max_pages} 页，已中止（请检查接口返回或调大 max_pages）"
            )
        return records

    # ------------------------------------------------------------------ 全部

    def fetch_all(self, days: list[date], workers: int = 1, window_retries: int = 2,
                  on_records: Callable[[list], None] | None = None
                  ) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
        """按计划拉取全部单元。

        - on_records：每拉完一个单元立刻回调（调用方用来写 Spool，避免全量数据占内存）；
        - 返回 (每单元条数统计 [(label, 条数)], 失败明细 [(label, 错误)])；
        - 只要 failures 非空，调用方必须放弃写库（见 cli.run_sync）。
        """
        units = self.build_units(days)
        attempts = max(1, 1 + int(window_retries or 0))
        stats: list[tuple[str, int]] = []
        failures: list[tuple[str, str]] = []

        def run_unit(unit: FetchUnit) -> tuple[str, list[dict]]:
            """单个单元 + 整窗重试（接口偶发抖动时不用重跑整个任务）。"""
            delay = 10
            last_err = None
            for attempt in range(1, attempts + 1):
                try:
                    return unit.label, self.fetch_unit(unit)
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if attempt == attempts:
                        break
                    log(f"  [{unit.label}] 第 {attempt}/{attempts - 1} 次失败："
                        f"{redact(str(exc))}；{delay}s 后整窗重试")
                    time.sleep(delay)
                    delay *= 2
            raise RuntimeError(redact(str(last_err)))

        def handle(label: str, records: list[dict]) -> None:
            stats.append((label, len(records)))
            if on_records is not None:
                on_records(records)
            log(f"  {label}：拉取 {len(records):,} 条")

        if workers <= 1 or len(units) <= 1:
            # 串行：顺序稳定，日志简单
            for unit in units:
                try:
                    label, records = run_unit(unit)
                    handle(label, records)
                except Exception as exc:  # noqa: BLE001
                    failures.append((unit.label, str(exc)))
                    log(f"  ❌ {unit.label} 拉取失败：{exc}")
        else:
            # 并发：只并发网络等待；回调在主线程的 as_completed 循环里，天然串行安全
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(run_unit, unit): unit for unit in units}
                for future in as_completed(futures):
                    unit = futures[future]
                    try:
                        label, records = future.result()
                        handle(label, records)
                    except Exception as exc:  # noqa: BLE001
                        failures.append((unit.label, str(exc)))
                        log(f"  ❌ {unit.label} 拉取失败：{exc}")

        return stats, failures
