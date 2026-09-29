# -*- coding: utf-8 -*-
"""HTTP：单次请求（可测）+ 重试（含 Retry-After / 业务错误 fail_if）。"""

from __future__ import annotations

import codecs
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .parsers import get_path
from .spool import loads_json
from .utils import ConfigError, FatalApiError, log, log_once, redact

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
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        if not math.isfinite(seconds):
            # NaN/inf 过得了 float() 却过不了 min/max 夹取（NaN 跟谁比都是 False），
            # 原样传给 time.sleep 会抛 ValueError，让整条 429 重试链一个请求都没重试就崩；
            # 按"没给"处理，走默认退避
            return None
        # 下界也要夹住：服务端写个 "-1" 原样传给 time.sleep 会抛
        # "sleep length must be non-negative"，被上层当成网络抖动白退避好几轮
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # 个别服务端给不带时区的日期，按 GMT 解释
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        delay = (parsed - datetime.now(timezone.utc)).total_seconds()
    except (OverflowError, OSError, ValueError):
        return None
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def _is_latin1_alias(encoding) -> bool:
    """编码名是不是 latin-1（含 latin_1 / iso8859-1 / iso_8859_1 / cp819 / 8859 / L1 等别名）。

    只比较字面量会漏掉别名：上面这些名字 codecs.lookup 全都归一到 iso8859-1，
    解码行为一样——"任何字节序列都能解成看起来成功的乱码"，正是要挡掉的东西。
    认不出的编码名返回 False（交给解码循环去报，不在这里提升为配置错）。
    """
    try:
        return codecs.lookup(str(encoding or "").strip()).name == "iso8859-1"
    except (LookupError, TypeError, ValueError):
        return False


def _json_key_set(text: str) -> set[str] | None:
    """解析 JSON 并收集所有对象键；解析失败返回 None。

    只用于"显式编码与 UTF-8 都能解开、但内容不同"时的冲突判断：如果两边的键名
    完全不同，说明按显式编码很可能把 UTF-8 的键名解成了乱码；如果键名一致、只有
    值不同，则可能是 GBK 字节恰好也能被 UTF-8 解成合法字符，不能据此武断报错。
    """
    try:
        payload = loads_json(text)
    except ValueError:
        return None
    keys: set[str] = set()
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    keys.add(key)
                stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
    return keys


def _decode_json_body(response, json_encoding: str | None) -> str:
    """JSON 响应体 → 文本：显式配置 > UTF-8(sig) > 响应头声明的 charset，全都解不出就报错。

    不能用 errors="replace" 兜底：GBK 等非 UTF-8 接口的中文会静默变成 U+FFFD 写进 ODS，
    条数与写后校验全过（违反"宁可失败不可静默写坏数据"）。原实现固定 utf-8+replace，
    连响应头里声明的编码都不看。
    """
    raw = response.content
    candidates: list[str] = []
    if json_encoding:
        # latin-1 能把任何字节序列解成"看起来成功"的乱码，和响应头声明一样要挡掉：
        # 显式配置成它时，UTF-8 的接口会被解出乱码键名，JSON 解析照样成功、写库也照样
        # 校验通过，下游 get_json_object 全取空——属于静默写坏数据
        if _is_latin1_alias(json_encoding):
            raise ConfigError(
                f"request.json_encoding 不能是 {json_encoding!r}：它能把任何字节序列解成乱码"
                f'而不报错；源是 GBK 之类请写具体编码（如 "gbk"），是 UTF-8 就删掉这一项'
            )
        candidates.append(str(json_encoding))
    # utf-8-sig：有些源会在 JSON 最前面带 BOM，按 utf-8 解出来是 "﻿{"，
    # loads_json 会一直解析失败并重试到耗尽
    candidates.append("utf-8-sig")
    declared = str(getattr(response, "encoding", "") or "").strip()
    # requests 对 text/* 默认给 ISO-8859-1（HTTP 规范默认值，不是服务端声明）：
    # 它能把任何字节序列解成"看起来成功"的乱码，绝不能进候选
    if declared and not _is_latin1_alias(declared):
        candidates.append(declared)
    seen: set[str] = set()
    last_exc: Exception | None = None
    resolved: str | None = None
    for encoding in candidates:
        if encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            resolved = raw.decode(encoding).lstrip("﻿")
            break
        except (LookupError, UnicodeDecodeError) as exc:
            last_exc = exc
    if resolved is None:
        raise ConfigError(
            f"接口 JSON 响应按 {'/'.join(candidates)} 都解不出来（{last_exc}）；"
            f'源是 GBK 等非 UTF-8 编码时，请给 request 加 json_encoding（如 "gbk"）'
        )
    if json_encoding:
        # 显式配置与 utf-8-sig 都能解出来、但内容不一致：多半是把 UTF-8 接口错配成了
        # gbk 之类——解出来的是乱码键名，JSON 照样解析成功、写库也照样校验通过，下游
        # get_json_object 全取空。原来只打一条警告就照写，属于"静默写坏数据"，这里改成
        # 直接报配置错（ConfigError 不可重试，快速暴露）。
        #
        # 但 UTF-16/UTF-32 这类定宽编码会产出含 \x00 的字节串，它同样能被 utf-8-sig
        # "解出来"（内容是一堆 \x00 夹杂）——那不是"源是 UTF-8"的证据，要排除掉，
        # 否则合法的 UTF-16 源会被这条冲突检查误杀。
        try:
            fallback = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            fallback = None
        if fallback is not None and "\x00" in fallback:
            fallback = None
        if fallback is not None and fallback != resolved:
            configured_keys = _json_key_set(resolved)
            utf8_keys = _json_key_set(fallback)
            if configured_keys is not None and utf8_keys is not None and configured_keys != utf8_keys:
                # 键名完全不同是很强的信号：UTF-8 的键名被按 GBK 解成了另一组汉字，
                # 下游 get_json_object('$.原键名') 会全取空。
                raise ConfigError(
                    f"request.json_encoding={json_encoding!r} 解出的 JSON 键名与 utf-8-sig "
                    f"不一致（{sorted(configured_keys)[:5]} vs {sorted(utf8_keys)[:5]}），"
                    f"无法确定接口真实编码；按 json_encoding 写库可能是乱码键名。若接口是 UTF-8，"
                    f"请删掉 request.json_encoding；若接口确实是 {json_encoding!r}，请核对源编码"
                )
            # 键名一致、只有值不同：GBK 双字节序列恰好也是合法 UTF-8 的情况真实存在
            # （例如 '一'.encode('gbk') 能被 utf-8-sig 解成 'һ'）。显式 json_encoding
            # 是用户最强的信号，不能因为这种歧义直接拒绝合法 GBK 数据；打一条告警留痕。
            log_once(
                f"  警告：request.json_encoding={json_encoding!r} 解出的内容与 utf-8-sig "
                f"不一致，但两者都是合法 JSON；已优先按显式 json_encoding 处理。若接口其实是 "
                f"UTF-8，请删掉 request.json_encoding，否则下游可能取不到字段"
            )
    return resolved


def request_once(
    method: str,
    url: str,
    params: dict,
    headers: dict,
    body_type: str,
    timeout: float,
    expect_json: bool,
    verify: bool,
    proxies: dict | None,
    json_encoding: str | None = None,
):
    """发一次 HTTP 请求（不含重试；单元测试会替换本函数）。

    - 429 / 5xx：抛 RetryLater（带 Retry-After 秒数），由上层退避重试；
    - 其余 4xx：抛 FatalApiError（参数/权限问题，重试无意义）；
    - JSON 解析失败：抛 RuntimeError（可能是接口异常，重试）；
    - JSON 响应解不出编码：抛 ConfigError（确定性错误，重试无意义）。
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

    try:
        # allow_redirects=False：requests 默认跟随重定向，而 301/302/303 会把 POST 降级成
        # **不带 body 的 GET**（窗口/分页参数全丢，接口若回 200 就是一份没有过滤条件的数据
        # 被当成成功写进当天分区），且自定义鉴权头（X-Api-Key 这类，requests 只清
        # Authorization）会被原样转发到重定向目标。这两种后果都比"直接失败"危险得多
        response = requests.request(method, url, allow_redirects=False, **kwargs)
    except (
        requests.exceptions.MissingSchema,
        requests.exceptions.InvalidSchema,
        requests.exceptions.InvalidURL,
        requests.exceptions.InvalidHeader,
        requests.exceptions.URLRequired,
        UnicodeError,
    ) as exc:
        # 这几类异常在"一个字节都没发出去"时就抛了（地址没写 https://、头值带换行/中文），
        # 是确定性的配置错。当成网络抖动去退避，一次能白等 20 多分钟（实测 17 轮 1425 秒）。
        # 不转发原始消息：InvalidHeader 的消息里带着请求头原值，密钥会原样进日志
        raise ConfigError(
            f"请求无法发出（{type(exc).__name__}）：请检查 request.base_url 是否带 http(s)://、"
            f"request.headers / 鉴权配置的值有没有首尾空白、换行或非 latin-1 字符"
        )
    status = response.status_code
    if 300 <= status < 400:
        # 不跟随重定向（见上面的说明）：把 Location 报出来让用户直接改成最终地址。
        # 这里抛 ConfigError（确定性错误、不重试），别让它掉进"网络抖动"的退避里
        location = redact(str(response.headers.get("Location") or ""))
        raise ConfigError(
            f"接口返回重定向 HTTP {status}（Location: {location}）：本工具不跟随重定向——"
            f"301/302/303 会把 POST 降级成不带 body 的 GET（窗口参数全丢），"
            f"自定义鉴权头也可能被转发到别的地址。请把 request.base_url 改成最终地址"
        )
    if status == 429 or 500 <= status < 600:
        raise RetryLater(_retry_after_seconds(response), f"HTTP {status}")
    if 400 <= status < 500:
        raise FatalApiError(f"HTTP {status}：{redact(response.text[:300])}")
    response.raise_for_status()

    if not expect_json:
        return response.content
    text = _decode_json_body(response, json_encoding)
    try:
        # 返回值带 NaN/Infinity 时在 loads_json 里挡住
        return loads_json(text)
    except ValueError as exc:
        raise RuntimeError(f"接口返回不是 JSON 或含非法数值（{exc}）：{redact(text[:300])}")


def _normalize_compare(value):
    """fail_if 比较用：布尔转 'true'/'false'；数字统一按 float，数字样字符串也按 float。

    原实现把数字 str() 后比：接口返回 0.0（浮点序列化）与配置 0 会判成"不相等"——
    equals 方向漏判（把业务错误当成功继续写库）、not_equals 方向误杀；
    反过来 str(2e2)="200.0" 与 "200" 也对不上。统一按数值比后这些形态一致。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return value
        return number if math.isfinite(number) else value
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
        bad = ("equals" in cond and value == _normalize_compare(cond["equals"])) or (
            "not_equals" in cond and value != _normalize_compare(cond["not_equals"])
        )
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


def request_with_retry(
    method: str,
    url: str,
    build_request,
    body_type: str,
    timeout: float,
    *,
    expect_json: bool = True,
    fail_if: list | None = None,
    retry_times: int = 5,
    retry_delay: float = 15,
    verify: bool = True,
    proxies: dict | None = None,
    desc: str = "请求",
    json_encoding: str | None = None,
    redactor=None,
):
    """带长冷却重试的请求；业务错误按 fail_if 判定。

    build_request：无参函数，每次尝试前调用一次，返回 (params, headers)。
    做成回调是为了让每次重试都重新鉴权——一次性签名（阿里云 RPC 的 SignatureNonce）
    复用同一个 nonce 会被服务端判 400。

    redactor：可选的掩码函数。重试日志在这里就打完了，上游（Fetcher / CLI）的
    值级脱敏还没机会出手；调用方传自己的 redactor 才能盖住接口把凭证写进自由文本
    的报错（如 500 的 `bad token sk-xxx`）。不传时退化为形态级 redact。

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
    mask = redactor or redact
    delay = retry_delay
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            params, headers = build_request()
            payload = request_once(
                method, url, params, headers, body_type, timeout, expect_json, verify, proxies, json_encoding
            )
            check_fail_if(payload, fail_if)  # 200 也可能是业务错误（如 code != 0）
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
            log(f"  [{desc}] {mask(str(exc))}（第 {attempt}/{attempts - 1} 次），{wait:g}s 后重试")
            time.sleep(wait)
            delay = min(delay * 2, 300)
        except Exception as exc:  # noqa: BLE001 - 网络/解析类错误统一重试
            last_err = exc
            if attempt >= attempts:
                break
            log(f"  [{desc}] 第 {attempt}/{attempts - 1} 次失败：{mask(str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, 300)
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{mask(str(last_err))}")
