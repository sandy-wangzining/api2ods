# -*- coding: utf-8 -*-
"""HTTP：单次请求（可测）+ 重试（含 Retry-After / 业务错误 fail_if）。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .parsers import get_path
from .spool import loads_json
from .utils import ConfigError, FatalApiError, log, redact

try:
    import requests
except ImportError:  # pragma: no cover - 单元测试/--dry-run 可以不装
    requests = None

MAX_RETRY_AFTER = 180


class RetryLater(RuntimeError):
    """服务端明确要求稍后重试（429/5xx + Retry-After）。"""

    def __init__(self, seconds: float | None, message: str):
        """seconds 为服务端要求的等待秒数；没给（如 fail_if 判定的业务错误）则 None，走默认退避。"""
        super().__init__(message)
        self.seconds = seconds


def _retry_after_seconds(response) -> float | None:
    """Retry-After 既可能是秒数，也可能是 HTTP-date（RFC 7231 两种写法都合法）。"""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        # 下界也要夹住：服务端写个 "-1" 原样传给 time.sleep 会抛
        # "sleep length must be non-negative"，被上层当成网络抖动白退避好几轮
        return min(max(float(value), 0.0), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:      # 个别服务端给不带时区的日期，按 GMT 解释
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        delay = (parsed - datetime.now(timezone.utc)).total_seconds()
    except (OverflowError, OSError, ValueError):
        return None
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def request_once(method: str, url: str, params: dict, headers: dict, body_type: str,
                 timeout: float, expect_json: bool, verify: bool, proxies: dict | None):
    """发一次 HTTP 请求（不含重试；单元测试会替换本函数）。

    - 429 / 5xx：抛 RetryLater（带 Retry-After 秒数），由上层退避重试；
    - 其余 4xx：抛 FatalApiError（参数/权限问题，重试无意义）；
    - JSON 解析失败：抛 RuntimeError（可能是接口异常，重试）。
    """
    if requests is None:
        raise FatalApiError("缺少 requests，请先 pip install requests")
    method = method.upper()
    kwargs = {"headers": headers, "timeout": timeout, "verify": verify}
    if proxies:
        kwargs["proxies"] = proxies
    if method == "GET":
        kwargs["params"] = params
    elif str(body_type).lower() == "form":
        kwargs["data"] = params
    else:
        kwargs["json"] = params

    response = requests.request(method, url, **kwargs)
    status = response.status_code
    if status == 429 or 500 <= status < 600:
        raise RetryLater(_retry_after_seconds(response), f"HTTP {status}")
    if 400 <= status < 500:
        raise FatalApiError(f"HTTP {status}：{redact(response.text[:300])}")
    response.raise_for_status()

    if not expect_json:
        return response.content
    try:
        # 显式解码：requests 默认会用响应头里声明的编码，返回值带 NaN 时也在这里挡住。
        # utf-8-sig：有些源会在 JSON 最前面带 BOM，按 utf-8 解出来是 "﻿{"，
        # loads_json 会一直解析失败并重试到耗尽
        return loads_json(response.content.decode("utf-8-sig", "replace"))
    except ValueError as exc:
        raise RuntimeError(f"接口返回不是 JSON 或含非法数值（{exc}）：{redact(response.text[:300])}")


def _normalize_compare(value):
    """fail_if 比较用：布尔转 'true'/'false'，数字转字符串（接口返回 20000 与配置 "20000" 视为相等）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def check_fail_if(payload, fail_if: list | None) -> None:
    """业务错误检查：命中的条件按 retry 标记抛可重试异常或 FatalApiError。

    二进制响应（response_type=bytes）没有字段可查：文件类接口的作业若从模板抄了
    fail_if，路径永远取不到值，not_equals 会命中，任务每天必失败。这里直接跳过，
    文件类接口的错误由解析阶段兜（拿到 JSON 错误体时会报"期望文件流"）。
    """
    if isinstance(payload, (bytes, bytearray)):
        return
    for cond in fail_if or []:
        value = _normalize_compare(get_path(payload, cond.get("path", ""), default=None))
        bad = ("equals" in cond and value == _normalize_compare(cond["equals"])) or \
              ("not_equals" in cond and value != _normalize_compare(cond["not_equals"]))
        if not bad:
            continue
        message = f"接口返回业务错误：{cond['path']}={value!r}"
        if cond.get("message_path"):
            message += f"，{cond['message_path']}={get_path(payload, cond['message_path'], default=None)!r}"
        if cond.get("retry"):
            # 用 RetryLater 而不是裸 RuntimeError：两者的重试行为一样，但裸异常会被
            # 当成网络抖动，把这条业务错误（常含接口返回的数据）脱敏后打进日志；
            # RetryLater 只记一行"限流中，N 秒后重试"
            raise RetryLater(None, message)
        raise FatalApiError(message)


def request_with_retry(method: str, url: str, build_request, body_type: str,
                       timeout: float, *, expect_json: bool = True, fail_if: list | None = None,
                       retry_times: int = 5, retry_delay: float = 15,
                       verify: bool = True, proxies: dict | None = None, desc: str = "请求"):
    """带长冷却重试的请求；业务错误按 fail_if 判定。

    build_request：无参函数，每次尝试前调用一次，返回 (params, headers)。
    做成回调是为了让每次重试都重新鉴权——一次性签名（阿里云 RPC 的 SignatureNonce）
    复用同一个 nonce 会被服务端判 400。

    重试策略（和"失败快、成功稳"的调度需求对齐）：
    - FatalApiError（4xx 参数/权限类）：直接抛出，不浪费时间重试；
    - ConfigError（鉴权配置错、签名函数自身报错）：重试多少次都一样，直接抛出；
    - RetryLater（429/5xx，服务端给了 Retry-After）：按服务端要求等；
    - 其他网络/解析异常（ConnectionError / Timeout / SSLError 等）：指数退避
      （15s → 30s → …，封顶 300s）。
    """
    # retry_times 是"失败后最多再试几次"，不是"总共几次"：0 就是只发一次请求、不做退避。
    # 原来的 range(1, max(1, 0)+1) 会把 0 当成 1，发完还白睡一轮再报"重试 0 次仍失败"
    attempts = max(1, int(retry_times) + 1) if retry_times else 1
    delay = retry_delay
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            params, headers = build_request()
            payload = request_once(method, url, params, headers, body_type, timeout,
                                   expect_json, verify, proxies)
            check_fail_if(payload, fail_if)      # 200 也可能是业务错误（如 code != 0）
            return payload
        except (FatalApiError, ConfigError):
            # 鉴权/配置类错误：重试不会变好，快速失败让调度看到真实原因。
            # 注意别在这里捕 OSError：requests 的 ConnectionError / Timeout / SSLError
            # 都是 OSError 子类，捕了会把连接抖动的请求级重试整个误杀
            raise
        except RetryLater as exc:
            last_err = exc
            if attempt >= attempts:
                break
            wait = exc.seconds if exc.seconds else delay
            # 业务错误（fail_if）的文案可能带接口返回的原文，一样过脱敏
            log(f"  [{desc}] {redact(str(exc))}（第 {attempt}/{attempts - 1} 次），{wait:g}s 后重试")
            time.sleep(wait)
            delay = min(delay * 2, 300)
        except Exception as exc:  # noqa: BLE001 - 网络/解析类错误统一重试
            last_err = exc
            if attempt >= attempts:
                break
            log(f"  [{desc}] 第 {attempt}/{attempts - 1} 次失败：{redact(str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, 300)
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{redact(str(last_err))}")
