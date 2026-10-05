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

import decimal
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .auth import AuthApplier
from .dates import window_param_sets
from .http import request_with_retry
from .parsers import ensure_object_records, extract_json_records, get_path, parse_payload
from .utils import ConfigError, FatalApiError, as_bool, check_header_values, collect_secret_values, log, redact_secrets

# HTTP 方法按 RFC 7230 的 token 形态（字母数字与 !#$%&'*+-.^_`|~）：任意字符串原样写进
# request.method 会把换行等拼进请求行，或者让 requests 报难以归类的底层错误
_METHOD_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9!#$%&'*+\-.^_`|~]{0,31}\Z")

# 体检用 page_size=1 被拒时的判据：宁缺毋滥，命中不了的接口最多少回退一次。
# 不收录光秃秃的 "limit"：报错说"时段超限/调用次数 limit"的接口会被误判成拒绝页大小，
# 白白多发一次请求（要表达页大小限制的接口会带上 page_size/每页 这类更具体的词）
_SIZE_ERROR_HINTS = (
    "page size",
    "page_size",
    "pagesize",
    "per page",
    "perpage",
    "每页",
    "页大小",
    "页长",
    "size must",
    "size should",
    "limit must",
    "limit should",
    "limit range",
)


def _looks_like_size_error(message: str) -> bool:
    """报错是否像是在拒绝"页大小"参数。

    体检只发了这一条请求、页大小是唯一被改过的参数，所以 400 多半是它引起的；
    但关键词要够具体——只写 "page"/"count" 会命中 "Invalid page param"、
    "invalid count of parameters" 这类无关报错，白白多发一次请求。
    """
    lowered = message.lower()
    return any(hint in lowered for hint in _SIZE_ERROR_HINTS)


def _as_number(value, fallback: float, field: str) -> float:
    """数值配置项转 float（int 也走这里）：写错给报错，而不是抛裸的 ValueError traceback。"""
    if value is None or value == "":
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError：JSON 里的超长整数字面量 float() 会溢出
        raise SystemExit(f"{field} 必须是数字，实际 {value!r}")
    # NaN / inf 过得了 float()，却会让后面的范围判断全部失效（NaN 跟谁比都是 False），
    # 于是"超限值"被当成合法配置一路带下去；这类值一律按写错处理
    if not math.isfinite(number):
        raise SystemExit(f"{field} 必须是有限数字（不能是 NaN/Infinity），实际 {value!r}")
    return number


def _positive_int(value) -> int | None:
    """把翻页终点字段转成正整数；不是正数（或读不出来）返回 None。

    布尔必须排除：float(True) == 1.0 能过数字检查，而"total_items_path 指错到布尔字段"
    （如 data.ok: true）会让翻页在第一页就判定拉完，静默少数据还显示成功——
    这正是本工具最不能接受的那类失败。
    也不能让 float() / int() 的异常漏出去：NaN、inf、"inf"、10**400 这类值原来会抛
    ValueError / OverflowError，被上层当成"接口抖动"整窗重试，白等十几分钟。
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    except OverflowError:
        # 天文数字：JSON 里出现 1e999 或 10**400 时 float() 直接放弃（>0 才可能走到这里）
        return None
    # inf（JSON 字面量 Infinity 合法）与天文数字都当成"终点读不出来"：按未翻完处理，
    # 由空页逻辑报出"无法确认已翻完"，而不是编一个假的上限数字写进报错信息
    if not math.isfinite(number):
        return None
    number = int(number)
    return number if number > 0 else None


def _is_zero_count(value) -> bool:
    """终点字段是否明确写着"一条都没有"（数字 0）。

    零数据日接口常回"空数组 + 总数 0"：这是接口在说"这个窗口确实没数据"，
    与"字段缺失/-1/读不出来"（无法确认翻完，按抖动处理）必须区分开，
    否则每个零数据日都会整窗重试到失败。只认数字 0，字符串 "0" 也认（很多接口回字符串）。
    """
    if value is None or value == "" or isinstance(value, bool):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        # NaN == 0 为 False，但 inf 也不能当"明确的 0 条"去收尾
        return False
    try:
        # Decimal 而不是 float：float("1e-400") 会下溢成 0.0，把"极小但非零"误判成
        # "明确的 0 条"（接口写 0 才是零数据日的收尾信号，误判会少拉数据）
        return decimal.Decimal(str(value).strip()) == 0
    except (decimal.InvalidOperation, TypeError, ValueError):
        # 解析不出来（超大整数/奇怪文本）：不是 0，按"读不出来"处理
        return False


class UnconfirmedEndError(RuntimeError):
    """翻页中途空页、且接口没给总页数/总条数：无法证明"确实翻完了"。

    正常拉取（strict）按失败上报——宁可让调度看见，也别静默少数据；
    体检（probe）只拉一页、对这类接口必然遇到，按"能连通、空结果"处理。
    """


def _unfinished_reason(current: int, total_pages, count: int, total_items) -> str | None:
    """翻页中途返回空页时，判断是否"还没翻完"；没翻完返回原因，翻完了返回 None。

    两个终点路径至少要有一个能证明"确实翻完了"：总页数（当前页 < 总页数）或
    总条数（已累计 < 总数）。两者都取不到时就无法判断——按未拉完处理，让上层
    决定是报错还是打警告收尾（宁可让调度看见，也别静默少数据）。

    布尔不算数字：True 在 int() 下是 1，会让"翻到第 2 页、共 1 页"直接判定翻完。
    """
    known_end = False
    pages = _positive_int(total_pages)
    if pages is not None:
        if current < pages:
            return f"总页数显示还有数据（第 {current} 页 / 共 {total_pages} 页）"
        known_end = True
    items = _positive_int(total_items)
    if items is not None:
        if count < items:
            return f"仍有数据未拉完（TotalCount={total_items}，已拉 {count} 条）"
        known_end = True
    if known_end:
        return None
    if total_pages is None and total_items is None:
        return "无法确认已翻完（接口没有给总页数/总条数）"
    # 终点字段存在但值读不出来（类型不对 / 不是数字）：无法证明翻完了。
    # 这里绝不能按"翻完"收尾——字段在却解析不了，本身就是接口结构变了或返回异常
    return "翻页终点字段无法解析（总页数/总条数不是数字）"


@dataclass
class FetchUnit:
    """一次逻辑请求：某天（per_day 模式）或某个区间（range 模式）。"""

    label: str  # 日志里显示的名字，如 "2026-09-18"
    day: date  # 该单元的基准日（决定窗口参数 / 用于日志）
    params: dict | None  # 窗口参数（startTime/endTime 等）；单次请求模式为 None


class Fetcher:
    """按作业配置请求 API：页码/游标分页、JSON/文件解析、整窗重试。"""

    def __init__(self, job: dict, job_dir: Path):
        """把作业配置里跟请求相关的字段摊平成实例属性（构造一次，多个单元复用）。

        job_dir 用于定位自定义签名文件（见 auth.AuthApplier）。
        """
        request_cfg = job.get("request") or {}
        self.job = job
        # 停止信号：某个单元致命失败（401/配置错）或 fetch_all 退出时置位，
        # 其余在飞 worker 据此尽快收手（不再发新请求、不再空等退避 sleep）
        self._stop = threading.Event()
        self.request_cfg = request_cfg
        self.parse_cfg = job.get("parse") or {}
        self.pagination = job.get("pagination") or {}
        self.method = str(request_cfg.get("method") or "GET").upper().strip()
        if not _METHOD_RE.match(self.method):
            # 任意字符串会原样写进 request.method：requests 报的是难以归类的底层错误，
            # 还有把换行拼进请求行的空间；这里按 RFC 的 token 形态先挡住
            raise SystemExit(f"request.method 不是合法的 HTTP 方法：{self.method!r}（如 GET / POST）")

        # 完整 URL = base_url + path（path 为空时直接用 base_url）
        base_url = str(request_cfg.get("base_url") or "").rstrip("/")
        path = str(request_cfg.get("path") or "")
        self.url = base_url + ("/" + path.lstrip("/") if path else "")

        self.body_type = str(request_cfg.get("body_type") or "json").lower()
        self.timeout = _as_number(request_cfg.get("timeout_seconds"), 30.0, "request.timeout_seconds")
        if self.timeout <= 0:
            # requests 对 timeout<=0 会在连接前抛裸 ValueError（"Attempted to set connect
            # timeout to 0"），落到重试分类里被当成网络抖动白退避十几分钟；配置错要立刻报
            raise SystemExit(f"request.timeout_seconds 必须大于 0（秒），实际 {self.timeout:g}")
        # 非 UTF-8（如 GBK）的 JSON 接口用显式编码；不配则按 UTF-8/响应头声明，解不出直接报错
        self.json_encoding = str(request_cfg.get("json_encoding") or "").strip() or None
        self.response_type = str(request_cfg.get("response_type") or "json").lower()
        self.fail_if = request_cfg.get("fail_if") or []
        self.retry_times = int(_as_number(request_cfg.get("retry_times"), 5, "request.retry_times"))
        if self.retry_times < 0:
            # 负数会被 max(1, ...) 吞成 0 次重试（配置意图不明），与 timeout/retry_delay 同口径
            raise SystemExit(f"request.retry_times 不能为负数，实际 {self.retry_times}")
        self.retry_delay = _as_number(request_cfg.get("retry_delay"), 15.0, "request.retry_delay")
        if self.retry_delay < 0:
            # 负值会在 time.sleep() 里抛裸 ValueError，被当成网络抖动静默重试，
            # 掩盖真正的配置错（与上面的 timeout_seconds 校验同口径）
            raise SystemExit(f"request.retry_delay 不能为负数（秒），实际 {self.retry_delay:g}")
        self.verify = as_bool(request_cfg.get("verify"), default=True, field="request.verify")
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
        # 值级脱敏的输入：接口把凭证写进自由文本报错时，形态规则（redact）盖不住，
        # 按配置里出现过的密钥值再遮一道（见 self.redact）
        self._secret_values = collect_secret_values(job)

    def redact(self, text) -> str:
        """值级 + 形态级两道脱敏：请求/响应/落盘错误进日志前统一走这里。"""
        return redact_secrets(self._secret_values, str(text))

    # ------------------------------------------------------------------ 计划

    def build_units(self, days: list[date]) -> list[FetchUnit]:
        """日期列表 → 请求单元列表。

        - per_day（默认）：每天一个单元；
        - range：整个区间一个单元；
        - 没配 window：只有一个单元（不做时间过滤）。
        """
        if not days:
            # 空列表原来会掉进 days[0]/param_sets[0] 的裸 IndexError（报错毫无指引）；
            # 也不返回空列表——那会让"没拉到任何数据"看起来像一次成功运行
            raise ConfigError("没有可执行的日期（days 为空），请检查 --days/--dates/--start-date 等参数与 window 配置")
        window = self.job.get("window") or {}
        mode = str(window.get("mode") or "per_day").lower()
        param_sets = window_param_sets(self.job, days)
        if not window or mode == "range":
            label = f"{days[0]}~{days[-1]}" if window else "单次请求"
            return [FetchUnit(label=label, day=days[0], params=param_sets[0])]
        return [FetchUnit(label=str(day), day=day, params=params) for day, params in zip(days, param_sets)]

    def unit_count(self, days: list[date]) -> int:
        """这个窗口会拆成几个请求单元（只用于日志/提示，不真的发请求）。"""
        return len(self.build_units(days))

    def probe(self, days: list[date]) -> tuple[str, int]:
        """体检（--check）专用：只证明"配置能连通、能解析"。

        任何分页模式都只发一次请求（页码分页顺带把页大小压到 1、游标分页连游标参数
        都不带），避免在数据量大的源上体检把全量拉一遍。
        唯一的例外：页大小被接口拒掉时会按配置值重试一次（见下）。
        """
        units = self.build_units(days)
        if not units:
            # 配置错（窗口/分页参数算出 0 个请求单元）走 ConfigError：体检顶层按统一的
            # 配置错路径报中文提示，而不是裸 RuntimeError traceback（与 window_retries 同口径）
            raise ConfigError("没有可执行的请求单元：检查 window.days 与 pagination 的起止参数")
        unit = units[0]
        # 页码分页时把页大小压到 1：体检只需要"能连通、能解析"，拉一条就够。
        # 游标分页不动页大小——游标接口常从首页游标里推下一页，改 size 可能改变返回结构，
        # 而体检本来也只发一次请求，不需要靠 size 来限流
        page_type = str(self.pagination.get("type") or "none").strip().lower()
        size_override = 1 if page_type == "page" else None
        try:
            records = self.fetch_unit(
                unit, page_size_override=size_override, max_pages_override=1, stop_after_first_page=True
            )
        except FatalApiError as exc:
            # 有些接口要求页大小不低于 10/20，体检压到 1 会被判 400：
            # 这不是"接口不通"，按配置里的页大小再试一次，别把能用的源拒之门外
            if size_override is None or not _looks_like_size_error(str(exc)):
                raise
            log(f"  体检用 page_size=1 被接口拒绝（{self.redact(exc)}），改回配置的页大小重试一次")
            records = self.fetch_unit(unit, max_pages_override=1, stop_after_first_page=True)
        except UnconfirmedEndError as exc:
            # 体检只拉一页，对"接口不给总数"的源永远无法确证翻完——按"能连通、空结果"
            # 处理，不能把这类好源判成体检失败（空数据日尤其常见）
            log(f"  体检提示：单页体检无法确证翻完（{self.redact(exc)}），按“能连通、当日无数据”处理")
            return unit.label, 0
        return unit.label, len(records)

    # ------------------------------------------------------------------ 单次

    def _sleep(self, delay: float, stop: threading.Event | None = None) -> bool:
        """可中断等待：返回 True 表示等待期间收到了停止信号（其它单元已失败/中断）。

        stop 由并发路径注入「本轮」的信号（默认用实例当前的）；封装成方法也便于
        测试替换（单测不想真等 10s/20s 的整窗重试间隔）。
        """
        return (stop or self._stop).wait(delay)

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

        def build():
            """每次尝试都重新算一遍参数和签名，返回 (params, headers) 给 http 层。"""
            # 每次尝试都重新鉴权：阿里云 RPC 的 SignatureNonce 一次性有效，
            # 重试复用同一个 nonce 会被判 400（SignatureNonceUsed）
            attempt_params = dict(params)
            headers = dict(self.base_headers)
            self.auth.apply(attempt_params, headers, self.method)
            if self.params_in == "headers":
                headers.update({str(key): str(value) for key, value in attempt_params.items()})
                attempt_params.clear()
            # 头值首尾空白/换行/非 latin-1 是"发不出去"的确定性配置错：
            # 提前拦下，别让 requests 抛的 InvalidHeader 把头的原值（密钥）带进日志
            check_header_values(headers)
            return attempt_params, headers

        return request_with_retry(
            self.method,
            self.url,
            build,
            self.body_type,
            self.timeout,
            expect_json=expect_json,
            fail_if=self.fail_if,
            retry_times=self.retry_times,
            retry_delay=self.retry_delay,
            verify=self.verify,
            proxies=self.proxies,
            desc=desc,
            json_encoding=self.json_encoding,
            redactor=self.redact,
        )

    def fetch_unit(
        self,
        unit: FetchUnit,
        page_size_override: int | None = None,
        max_pages_override: int | None = None,
        stop_after_first_page: bool = False,
        _stop: threading.Event | None = None,
    ) -> list[dict]:
        """执行一个请求单元，返回该单元的记录列表。

        三个 override 参数仅供 --check 限量使用（正式同步不传），确保体检只发一次请求。
        _stop 仅供并发路径注入「本轮」的停止信号（默认用实例当前的 self._stop）：
        必须按轮注入——下一轮会换新对象，遗留 worker 只认自己那轮、不会被复活。
        """
        # 基础参数 + 该单元的窗口参数（如 startTime/endTime）
        params = dict(self.base_params)
        if unit.params:
            params.update(unit.params)

        page_type = str(self.pagination.get("type") or "none").strip().lower()
        if page_type == "none":
            payload = self._do_request(params, unit.label, expect_json=self.response_type != "bytes")
            records = parse_payload(
                payload, self.request_cfg, self.parse_cfg, unit.label, missing_ok=self.records_missing == "empty"
            )
            return self._decorate(records)
        return self._decorate(
            self._fetch_pages(unit, params, page_size_override, max_pages_override, stop_after_first_page, stop=_stop)
        )

    def _fetch_pages(
        self,
        unit: FetchUnit,
        base_params: dict,
        page_size_override: int | None = None,
        max_pages_override: int | None = None,
        stop_after_first_page: bool = False,
        stop: threading.Event | None = None,
    ) -> list[dict]:
        """页码 / 游标分页循环。

        终止条件（按优先级）：
        - page：翻到 total_pages_path 指示的末页，或「已累计条数」达到 total_items_path 指示的总数；
          接口回报的条数没拉完却返回空页 → 视为接口抖动，报错触发整窗重试；
        - page + stop_when_short：接口不返回总数时，本页条数 < 请求的 page_size 即判末页
          （短页 = 最后一页，含空页；不走"空页但无法确认翻完"的严格判罚）；
        - cursor：返回里取不到下一页游标为止；
        - 任何模式：超过 max_pages 页主动中止（防死循环）。
        """
        page_cfg = self.pagination
        stop = stop or self._stop
        page_type = str(page_cfg.get("type") or "none").strip().lower()
        if page_type not in ("none", "page", "cursor"):
            # 拼错（pages/offset）或尾随空格的值原来会落到下面的 else 分支、被当成 cursor：
            # cursor_path 为空 → "取不到游标 = 翻完了"，只拉第一页还报成功。
            # validate_job 会拦；库调用方没走校验时在这里兜一道（先 strip 再比）
            raise ConfigError(f"pagination.type 不支持：{page_type!r}（可用 none / page / cursor）")
        page_param = str(page_cfg.get("page_param") or "page")
        # size_param 显式写 null = 不带页大小参数（游标接口不认 size 时用）；
        # 没写才用默认名 "size"
        size_param_value = page_cfg.get("size_param", "size")
        # 显式 null 与空串都当成"不带页大小"：空串原来会绕过下面的 stop_when_short 守卫
        # （`if size_param:` 为假、不发参数），却过得了 `is None` 检查
        if size_param_value is None or size_param_value == "":
            size_param = None
        else:
            size_param = str(size_param_value)
        # 不能写成 `page_size_override or page_cfg.get("page_size") or 100`：
        # 配了 page_size=0（想表达"别带 size"却漏了 size_param:null）会被静默换成 100，
        # 页大小翻 100 倍、条数校验还拿这一页自比。这里显式区分"没填"和"填了 0/负数"
        raw_page_size = page_size_override if page_size_override is not None else page_cfg.get("page_size")
        if raw_page_size is None or raw_page_size == "":
            raw_page_size = 100
        param_as_string = as_bool(
            page_cfg.get("param_as_string"), default=False, field="pagination.param_as_string"
        )  # 有些接口要求字符串
        total_pages_path = str(page_cfg.get("total_pages_path") or "")
        total_items_path = str(page_cfg.get("total_items_path") or "")
        cursor_param = str(page_cfg.get("cursor_param") or "cursor")
        cursor_path = str(page_cfg.get("cursor_path") or "")
        if page_type == "cursor" and not cursor_path:
            # validate_job 会拦；库调用方没走校验时在这里兜一道：cursor_path 为空时
            # get_path 取不到游标 → 被当成"翻完了"，只拉第一页还报成功（静默截断比报错危险）
            raise ConfigError(
                "pagination.type=cursor 需要 cursor_path（响应里下一页游标的字段路径），"
                "否则第一页之后取不到游标会被当成已翻完、静默只拉第一页"
            )
        cursor_start = page_cfg.get("cursor_start")
        delay = _as_number(page_cfg.get("delay_seconds"), 0.0, "pagination.delay_seconds")
        if delay < 0:
            raise ConfigError(f"pagination.delay_seconds 不能是负数，实际 {delay:g}")
        raw_max_pages = max_pages_override if max_pages_override is not None else page_cfg.get("max_pages")
        if raw_max_pages is None or raw_max_pages == "":
            raw_max_pages = 2000
        max_pages = int(_as_number(raw_max_pages, 2000, "pagination.max_pages"))
        if max_pages <= 0:
            # max_pages=0/-1 时 range(1, max_pages+1) 为空，会直接落到 for-else 报
            # "超过 max_pages=0 页"这种误导文案。这里给一句明确的配置错
            raise ConfigError(f"pagination.max_pages 必须是正整数，实际 {max_pages}")
        # 默认严格：空页但总数没够 = 接口有问题，宁可整窗失败。确实遇到 TotalCount 不准的
        # 接口（数据翻页期间仍在增长）才关掉，关掉后仍会打警告日志留痕
        strict = as_bool(page_cfg.get("strict"), default=True, field="pagination.strict")
        # 接口不返回总数时的翻页终点：本页条数 < 请求的 page_size 即判末页（短页=最后一页）。
        # 与 total_pages_path/total_items_path 互斥（validate_job 已拦），这里默认关闭
        stop_when_short = False
        if "stop_when_short" in page_cfg:
            stop_when_short = as_bool(
                page_cfg.get("stop_when_short"), default=False, field="pagination.stop_when_short"
            )
        if stop_when_short and size_param is None:
            # 请求里根本没有页大小参数：接口按自己的默认值返回（如 20 条），
            # "本页条数 < effective_page_size(默认 100)" 会立刻成立——第一页就被当末页收尾，
            # 静默少数据、写后校验还自洽。配置错要在这里快速失败
            raise ConfigError(
                "pagination.stop_when_short 不能与「size_param: null（不发页大小参数）」同时用："
                "请求里没有页大小，无法用「本页条数 < 页大小」判断末页（接口默认页大小比配置值小时，"
                "第一页就会被误判成最后一页）。请改用 total_pages_path/total_items_path，"
                "或恢复 size_param 并在接口文档确认页大小"
            )
        if stop_when_short and (page_cfg.get("total_pages_path") or page_cfg.get("total_items_path")):
            # validate_job 会拦这一对；库调用方没走校验时在这里兜一道（与 cursor_path 同口径）：
            # 短页判定会先 break，总数终点永远走不到——只拉第一页还"自洽"，静默截断比报错危险
            raise ConfigError(
                "pagination.stop_when_short 与 total_pages_path/total_items_path 互斥"
                "（前者按“本页条数 < page_size”判断翻完，后者按接口给的总数判断，只能二选一）"
            )

        effective_page_size = int(_as_number(raw_page_size, 100, "pagination.page_size"))
        if effective_page_size <= 0:
            raise ConfigError(f"pagination.page_size 必须是正整数，实际 {effective_page_size}")
        records: list[dict] = []
        current = 1
        cursor = cursor_start
        first_page = True
        # 最近一次读到的终点值（见 ③ 里的说明：有些接口只在第一页回）
        last_total_pages = None
        last_total_items = None
        for page_index in range(1, max_pages + 1):
            # ① 组装本页参数
            params = dict(base_params)
            if page_type == "page":
                # break 路径（total_pages / total_items / 空页）先于 current += 1，页码不会多翻
                params[page_param] = str(current) if param_as_string else current
            elif page_type == "cursor":
                # 首页 cursor 为空（不带游标参数），但页大小每页都要带，
                # 否则接口按自己的默认值返回，可能每页只有几条。
                # cursor_start 显式写 "" 也算"要带上这个空参数"，别静默丢掉。
                if cursor is not None:
                    params[cursor_param] = cursor
            if size_param:
                params[size_param] = str(effective_page_size) if param_as_string else effective_page_size

            # ② 发请求 + 取记录数组
            payload = self._do_request(params, f"{unit.label} 第{page_index}页", expect_json=True)
            page_records = get_path(payload, self.records_path, default=None)
            if page_records is None:
                # records_missing=empty 只认第一页（零数据日常见写法：直接返回空对象）；
                # 翻到一半记录路径取不到，是接口结构变了/返回不完整，宁可整窗失败也别静默截断
                if first_page and self.records_missing == "empty":
                    log(f"  {unit.label}：records_path 未命中，按空结果处理（records_missing=empty）")
                    return []
                extract_json_records(payload, self.request_cfg, unit.label)  # 抛带诊断信息的错误
            first_page = False
            # 分页路径同样要校验元素类型：records_path 指向的数组里混进数字/字符串，
            # 只在单页路径拦得住的话，分页接口会把它原样写进 json 列
            page_records = ensure_object_records(page_records, unit.label)
            records.extend(page_records)

            # ③ 判断是否翻完
            if page_type == "page":
                if stop_when_short and len(page_records) < effective_page_size:
                    # 短页即末页：接口不返回总数（没有 total_pages_path/total_items_path）时，
                    # 用「本页条数 < 请求的 page_size」判断翻完。本页为空（0 条 < page_size）
                    # 同样按己翻完收尾：首屏空页 = 该窗口确实没数据，整页之后的空页 = 确实
                    # 没有下一页——这正是 stop_when_short 相对 total_* 的语义差异，
                    # 不走下面那套"空页但无法确认翻完"的 strict 判罚（当页取不到总数可对）
                    break
                raw_pages = get_path(payload, total_pages_path, default=None) if total_pages_path else None
                raw_items = get_path(payload, total_items_path, default=None) if total_items_path else None
                # 零数据日：接口回"空数组 + 总数 0"，这是在说"这个窗口确实没有数据"，
                # 直接收尾、成功写 0 行。按"接口抖动"处理会整窗重试到失败，把正常的
                # 零数据日变成天天报警。限第一页（还没拉到任何记录）才这么判：中途翻出
                # 空页却报 0 条，与前几页的记录自相矛盾，那种情况照旧按未翻完处理
                if not page_records and not records and (_is_zero_count(raw_pages) or _is_zero_count(raw_items)):
                    break
                # 很多接口只在第一页回 totalPages / TotalCount，后续页不带。读到就记下来，
                # 翻到空页时用最后一次读到的值判断——否则"数据已拉全、末页之后又空翻一页"
                # 会被判成"无法确认已翻完"，默认 strict 下整窗失败（数据明明是全的）
                # 只认正数：-1（"未知"的常见哨兵）、0（字段存在但没填）当终点会让第一页
                # 就 break——数据只拉到第一页、写库还校验通过，是静默少数据。
                # 取 max 而不是"最后一次读到的值"：往后翻时 TotalCount 可能变小
                # （窗口期数据被删、或某页只回本页条数），终点跟着缩水会提前收尾
                marker_pages = _positive_int(raw_pages)
                if marker_pages is not None:
                    last_total_pages = max(last_total_pages or 0, marker_pages)
                total_pages = last_total_pages
                marker_items = _positive_int(raw_items)
                if marker_items is not None:
                    last_total_items = max(last_total_items or 0, marker_items)
                total_items = last_total_items
                # 用「已累计条数」而不是 页码×page_size 估算：接口把 PageSize 压小
                # （返回条数少于请求的 page_size）时，估算会提前判定拉完，静默丢数
                reached_end = total_items is not None and len(records) >= total_items
                if total_pages is not None and current >= total_pages:
                    # 两个终点都配时，接口的 totalPages 可能按「请求的 page_size」算
                    # （实际每页给得更少），或者中途变小——这时页数说翻完了、条数说还差，
                    # 以条数为准继续翻：宁可多翻一页（真翻过头会拿到空页，由下面的检查
                    # 报出来），也不能静默少拉。只配了页数终点时行为不变
                    if total_items is None or reached_end:
                        break
                if page_records:
                    if reached_end:
                        break
                    current += 1
                else:
                    # 空页：只在"确实翻完了"时才收尾。只给了 total_pages 的作业同样要判
                    # （第 N 页空但 N < totalPages = 接口抖动，静默收尾会整天少数据），
                    # 并把条件写进报错信息，否则 strict=false 的收尾日志会变成一句空话
                    unfinished = _unfinished_reason(current, total_pages, len(records), total_items)
                    if unfinished is None:
                        break
                    message = f"{unit.label} 第 {current} 页返回为空，但{unfinished}，可能是接口抖动"
                    if strict:
                        # 专用类型：体检（probe）只拉一页、对"不给总数"的接口必然遇到这种
                        # 情况，需要按"能连通、空结果"处理；正常拉取仍按失败上报
                        raise UnconfirmedEndError(message)
                    # 非严格模式：接口给的终点不准时不该让整个调度永久失败，
                    # 但必须留痕——静默收尾正是"少拉数据却显示成功"的成因
                    log(f"  警告：{message}；pagination.strict=false，按已拉到的 {len(records)} 条收尾")
                    break
            else:  # cursor
                # 游标模式下也用 total_items_path 兜底：cursor_path 写错、或接口中途不返回游标时，
                # "取不到游标"会被当成"翻完了"，只拉第一页就收尾——静默少数据、写后校验还自洽
                # （page 模式有 _unfinished_reason 兜底，cursor 不能没有）
                raw_items = get_path(payload, total_items_path, default=None) if total_items_path else None
                marker_items = _positive_int(raw_items)
                if marker_items is not None:
                    last_total_items = max(last_total_items or 0, marker_items)
                next_cursor = get_path(payload, cursor_path, default=None)
                # 只认 None / 空串为"没有下一页"：0、false、[] 是合法游标值，
                # 当成结束会提前收尾、静默少数据
                if next_cursor is None or next_cursor == "":
                    total_items = last_total_items
                    if total_items is not None and len(records) < total_items:
                        message = (
                            f"{unit.label} 游标已结束，但仍有数据未拉完"
                            f"（TotalCount={total_items}，已拉 {len(records)} 条）——cursor_path 是否写错？"
                        )
                        if strict:
                            raise RuntimeError(message)
                        log(f"  警告：{message}；pagination.strict=false，按已拉到的 {len(records)} 条收尾")
                    break
                if next_cursor == cursor:
                    # 游标原样回显（cursor_path 配到了恒定字段）：继续翻只会把同一页拉满
                    # max_pages 次——重复数据无上限累积、还向接口打几千次重复请求
                    raise ConfigError(
                        f"{unit.label} 游标未推进（接口回显了同一个游标）：请检查 cursor_path / cursor_param 配置"
                    )
                cursor = next_cursor

            # ④ 翻页间隔 + 体检模式的提前返回
            if max_pages_override is not None and page_index >= max_pages_override:
                return records
            if stop_after_first_page:
                return records
            if stop.is_set():
                # 其它单元已致命失败（或用户中断）：不再发新请求；raise 让本 worker 尽早
                # 结束，异常由 future 承载（此时无人接收，无害）
                raise FatalApiError(f"{unit.label}：其它单元已失败，停止翻页")
            if delay > 0 and self._sleep(delay, stop):
                raise FatalApiError(f"{unit.label}：其它单元已失败，停止翻页")
        else:
            # 用 ConfigError（不可重试）而不是 RuntimeError：整窗重试会把同一份数据
            # 再翻满 max_pages 页（默认 2000 页）才失败，白白多打几千个请求
            raise ConfigError(
                f"{unit.label} 分页超过 max_pages={max_pages} 页，已中止"
                f"（请检查接口返回的终点字段，或调大 pagination.max_pages）"
            )
        return records

    # ------------------------------------------------------------------ 全部

    def fetch_all(
        self,
        days: list[date],
        workers: int = 1,
        window_retries: int = 2,
        on_records: Callable[[list], None] | None = None,
    ) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
        """按计划拉取全部单元。

        - on_records：每拉完一个单元立刻回调（调用方用来写 Spool，避免全量数据占内存）；
        - 返回 (每单元条数统计 [(label, 条数)], 失败明细 [(label, 错误)])；
        - 只要 failures 非空，调用方必须放弃写库（见 cli.run_sync）。
        """
        # 每轮换一个全新的停止信号（不是 clear 复用）：并发分支的 shutdown(wait=False)
        # 可能留下还在退避/请求中的旧 worker——它们只认自己那轮的 event（闭包/参数传入），
        # 换新对象后旧 worker 永远看到"已停止"，不会被新一轮的清位动作复活
        stop = threading.Event()
        self._stop = stop
        units = self.build_units(days)
        try:
            attempts = max(1, 1 + int(window_retries or 0))
        except (TypeError, ValueError, OverflowError):
            # OverflowError：JSON 里 1e999 解析成 float('inf')，int(inf) 会溢出——
            # 漏掉它这句"必须是整数"的配置错就变成裸 traceback
            raise SystemExit(f"pagination.window_retries 必须是整数，实际 {window_retries!r}")
        stats: list[tuple[str, int]] = []
        failures: list[tuple[str, str]] = []

        def run_unit(unit: FetchUnit) -> tuple[str, list[dict]]:
            """单个单元 + 整窗重试（接口偶发抖动时不用重跑整个任务）。

            重试只对"可能自己好起来"的错误有意义。密钥错（401）、配置写错这类问题重试
            多少次都一样，而且会翻倍放大：30 天窗口 × 3 次尝试 = 90 个必失败请求
            + 十几分钟空等。所以 FatalApiError / ConfigError 直接往上抛，由调用方立刻失败。
            """
            delay = 10
            last_err = None
            for attempt in range(1, attempts + 1):
                if stop.is_set():
                    # worker 还没起跑（或刚醒来）时其它单元已失败：直接收手，不发任何请求
                    raise FatalApiError(f"{unit.label}：其它单元已失败，不再尝试")
                try:
                    return unit.label, self.fetch_unit(unit, _stop=stop)
                except (FatalApiError, ConfigError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if attempt == attempts:
                        break
                    log(f"  [{unit.label}] 第 {attempt}/{attempts - 1} 次失败：{self.redact(exc)}；{delay}s 后整窗重试")
                    # 可中断的退避：其它单元已致命失败时立刻收手，不再空等 10s/20s
                    if self._sleep(delay, stop):
                        raise FatalApiError(f"{unit.label}：其它单元已失败，停止重试")
                    delay *= 2
            # 走得到这里的只有可重试的异常（FatalApiError / ConfigError 在循环里就抛了），
            # 统一按普通失败上报；消息要脱敏，重试日志里也不该出现密钥
            raise RuntimeError(self.redact(last_err)) from last_err

        def handle(label: str, records: list[dict]) -> None:
            """单元拉完后的统一收尾：先落盘、再统计条数、最后打日志。"""
            # 先落盘再统计：落盘抛错时这条不该算进"成功条数"（磁盘满就是这样，
            # 原来日志会显示拉取成功、实际一条没落盘）
            if on_records is not None:
                # 交付副本：下面的并发分支在回调后会清空原列表以释放 future 持有的内存，
                # 调用方即使保留了引用也不会被这次清理影响（传原列表会让"保留引用"静默丢数据）
                on_records(list(records))
            stats.append((label, len(records)))
            log(f"  {label}：拉取 {len(records):,} 条")

        if workers <= 1 or len(units) <= 1:
            # 串行：顺序稳定，日志简单
            try:
                for unit in units:
                    try:
                        label, records = run_unit(unit)
                        handle(label, records)
                    except (FatalApiError, ConfigError):
                        # 同上：不接住就会被下面的 except Exception 吃掉，同一份配置下后面的单元
                        # 必然同样失败，继续跑只是把错误重复几十遍（ConfigError 与 run_unit 里的
                        # 放行口径一致：配置类错误与具体某天数据无关）
                        raise
                    except Exception as exc:  # noqa: BLE001
                        # 统一 redact：on_records（落盘）的报错里可能带着记录原文/密钥，
                        # 它没走 run_unit 的脱敏包装，这是最后一道口
                        failures.append((unit.label, self.redact(exc)))
                        log(f"  ❌ {unit.label} 拉取失败：{self.redact(exc)}")
            finally:
                # 与并发分支同口径：fetch_all 退出（成功/失败/中断）就置停止信号，
                # 保持"方法结束即当轮 _stop 已置位"的不变式；下一轮会换新对象（不复位这个）
                stop.set()
        else:
            # 并发：只并发网络等待；回调在主线程的 as_completed 循环里，天然串行安全
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = {pool.submit(run_unit, unit): unit for unit in units}
                for future in as_completed(futures):
                    unit = futures.pop(future)
                    try:
                        label, records = future.result()
                        handle(label, records)
                        # future 会一直持有返回值（as_completed 内部的集合要到 fetch_all 返回
                        # 才释放）：不做处理的话，--workers 回补时全窗口数据都驻留内存，打破
                        # "峰值内存=单个请求单元"的承诺。落盘完成后立即清空（数据已进 spool；
                        # on_records 拿到的是副本，不受这次清空影响）
                        if isinstance(records, list):
                            records.clear()
                    except (FatalApiError, ConfigError):
                        # 不能省：下面的 except Exception 会把它吃掉，变成"每个单元各失败一次"，
                        # 而 401/配置错对所有单元都成立，继续跑只是把同一个错误重复几十遍
                        raise
                    except Exception as exc:  # noqa: BLE001
                        # 同上：on_records（落盘）抛出的报错不经 run_unit，必须自己脱敏
                        failures.append((unit.label, self.redact(exc)))
                        log(f"  ❌ {unit.label} 拉取失败：{self.redact(exc)}")
            finally:
                # 置当轮停止信号：本方法要退出了（成功/失败/中断都一样），在飞 worker 据此
                # 不再发新请求、不再空等退避 sleep——否则它们会带着整窗重试继续打接口，
                # 且 ThreadPoolExecutor 的非守护线程会让解释器退出/下一次用例启动时被拖住。
                # 置的是当轮对象：下一轮换新 event，这些旧 worker 永远看到"已停止"、不会复活
                stop.set()
                # 不用 with：它的 __exit__ 是 shutdown(wait=True)，Ctrl+C 之后还要等
                # 所有在飞的请求（可能正卡在 180s 超时或整窗重试的 sleep 里）跑完才退出。
                # cancel_futures 取消没开始的，在飞的请求收到停止信号后尽快结束，进程不再被拖住
                pool.shutdown(wait=False, cancel_futures=True)

        return stats, failures
