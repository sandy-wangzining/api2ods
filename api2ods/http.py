# -*- coding: utf-8 -*-
"""HTTP：单次请求（可测）+ 重试（含 Retry-After / 业务错误 fail_if）。"""

from __future__ import annotations

import time

from .parsers import get_path
from .utils import FatalApiError, log, redact

try:
    import requests
except ImportError:  # pragma: no cover - 单元测试/--dry-run 可以不装
    requests = None

MAX_RETRY_AFTER = 180


class RetryLater(RuntimeError):
    """服务端明确要求稍后重试（429/5xx + Retry-After）。"""

    def __init__(self, seconds: float | None, message: str):
        super().__init__(message)
        self.seconds = seconds


def _retry_after_seconds(response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return None


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
        return response.json()
    except ValueError:
        raise RuntimeError(f"接口返回不是 JSON：{redact(response.text[:300])}")


def _normalize_compare(value):
    """fail_if 比较用：布尔转 'true'/'false'，数字转字符串（接口返回 20000 与配置 "20000" 视为相等）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def check_fail_if(payload, fail_if: list | None) -> None:
    """业务错误检查：命中的条件按 retry 标记抛可重试异常或 FatalApiError。"""
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
            raise RuntimeError(message)
        raise FatalApiError(message)


def request_with_retry(method: str, url: str, params: dict, headers: dict, body_type: str,
                       timeout: float, *, expect_json: bool = True, fail_if: list | None = None,
                       retry_times: int = 5, retry_delay: float = 15,
                       verify: bool = True, proxies: dict | None = None, desc: str = "请求"):
    """带长冷却重试的请求；业务错误按 fail_if 判定。

    重试策略（和"失败快、成功稳"的调度需求对齐）：
    - FatalApiError（4xx 参数/权限类）：直接抛出，不浪费时间重试；
    - RetryLater（429/5xx，服务端给了 Retry-After）：按服务端要求等；
    - 其他网络/解析异常：指数退避（15s → 30s → …，封顶 300s）。
    """
    delay = retry_delay
    last_err = None
    for attempt in range(1, max(1, retry_times) + 1):
        try:
            payload = request_once(method, url, params, headers, body_type, timeout,
                                   expect_json, verify, proxies)
            check_fail_if(payload, fail_if)      # 200 也可能是业务错误（如 code != 0）
            return payload
        except FatalApiError:
            raise
        except RetryLater as exc:
            last_err = exc
            if attempt == retry_times:
                break
            wait = exc.seconds if exc.seconds else delay
            log(f"  [{desc}] {exc}（第 {attempt}/{retry_times - 1} 次），{wait:g}s 后重试")
            time.sleep(wait)
            delay = min(delay * 2, 300)
        except Exception as exc:  # noqa: BLE001 - 网络/解析类错误统一重试
            last_err = exc
            if attempt == retry_times:
                break
            log(f"  [{desc}] 第 {attempt}/{retry_times - 1} 次失败：{redact(str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, 300)
    raise RuntimeError(f"{desc} 重试 {retry_times} 次仍失败：{redact(str(last_err))}")
