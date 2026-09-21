# -*- coding: utf-8 -*-
"""日期与时区：参数解析、业务日列表、窗口参数（起止时间 / unix 秒）。"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

from .utils import ConfigError, log, log_once

DEFAULT_DATE_TZ = "Asia/Shanghai"
DEFAULT_API_TZ = "+08:00"
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")
# 带时刻的格式指令：出现任意一个就说明接口要的是"时间点"，需要按时区换算
_TIME_TOKENS = ("%H", "%I", "%M", "%S", "%f", "%p", "%X", "%T", "%c", "%z", "%Z")
_UNIX_FORMATS = ("unix", "unix_s", "unix_ms", "unix_millis")


def is_date_only_format(fmt: str) -> bool:
    """格式是否只到"日"（没有时分秒），如 %Y-%m-%d / %Y%m%d / %Y-%m。"""
    fmt = str(fmt or "")
    if fmt in _UNIX_FORMATS:
        return False
    return not any(token in fmt for token in _TIME_TOKENS)


def parse_offset(text: str) -> timezone:
    """把 '+08:00' / '+0800' / '+08' 解析成固定偏移时区；解析失败抛错。

    分钟数会校验（避免 '+080' 被贪心拆成 0 小时 80 分、静默变成 +01:20）。
    """
    match = _OFFSET_RE.match((text or "").strip())
    if not match:
        raise SystemExit(f"无法解析时区偏移：{text!r}（应形如 +08:00 或 -05:00）")
    sign, hours, minutes = match.groups()
    hour_value, minute_value = int(hours), int(minutes or 0)
    if hour_value > 14 or minute_value > 59:
        raise SystemExit(f"时区偏移超出范围：{text!r}（应形如 +08:00 或 -05:00）")
    delta = timedelta(hours=hour_value, minutes=minute_value)
    if sign == "-":
        delta = -delta
    return timezone(delta)


def load_api_zone(value) -> timezone | ZoneInfo:
    """接口时区：既认 '+08:00' 这类固定偏移，也认 'Asia/Shanghai' 这类时区名。

    时区名会走夏令时规则（美东 3~11 月是 -04:00、其余月份是 -05:00）。写死成 -05:00
    会在夏令时的半年里把每天窗口整体挪错一小时。
    """
    text = str(value or DEFAULT_API_TZ).strip()
    if text[:1] in ("+", "-"):
        return parse_offset(text)
    return load_zone(text)


def load_zone(name: str) -> ZoneInfo:
    """按名称加载时区（如 America/New_York、UTC、Asia/Shanghai）。"""
    try:
        return ZoneInfo(str(name))
    except Exception:
        raise SystemExit(
            f"无法识别时区：{name}（如 America/New_York / Asia/Shanghai / UTC；"
            f"Windows 本机需 pip install tzdata）"
        )


def date_tz_of(job: dict) -> ZoneInfo:
    """窗口/业务日按配置时区解释（默认 Asia/Shanghai）。"""
    return load_zone(str((job.get("window") or {}).get("date_tz") or DEFAULT_DATE_TZ))


def parse_day_arg(text: str) -> date:
    """解析日期参数：YYYYMMDD 或 YYYY-MM-DD。"""
    value = str(text or "").strip()
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"日期格式应为 YYYYMMDD 或 YYYY-MM-DD：{text!r}")


def env_bizdate(strict: bool = True) -> date | None:
    """DataWorks 环境变量 bizdate / SKYNET_BIZDATE（YYYYMMDD）；没设置返回 None。

    设置了却解析不出来时必须报错，**不能**默默回退"昨天"：那会把数据写进错的分区，
    还是先删再填（把对的分区覆盖掉），而退出码是 0，调度侧完全看不出来。
    同一个错值 --bizdate 会立刻报错，环境变量走静默回退属于两套标准。

    strict=False 只给"只读体检"（--check）用：它不写库，落哪个 pt 只是看一眼，
    没必要因为调度环境变量脏了就连看都看不了（那时按默认业务日继续并打警告）。
    调用方只在**没有显式 --bizdate** 时才该读环境变量——显式参数优先，
    否则运维无法用 --bizdate 强制指定业务日重跑。
    """
    raw = os.environ.get("bizdate") or os.environ.get("SKYNET_BIZDATE") or ""
    text = raw.strip()
    if not text:
        return None
    try:
        return parse_day_arg(text)
    except SystemExit as exc:
        if not strict:
            log(f"  警告：环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}；"
                f"只读体检（--check）不写库，按默认业务日继续")
            return None
        raise SystemExit(
            f"环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}"
            f"（应为 YYYYMMDD 或 YYYY-MM-DD）；不打算用它请先 unset，或用 --bizdate 显式指定业务日"
        ) from exc


def resolve_days(args, job: dict) -> list[date]:
    """命令行参数 → 待拉取的日期列表（升序）。

    优先级：--dates > --start-date/--end-date > --bizdate（或环境变量）> 默认（时区昨天）。
    回拉天数：--days 覆盖配置 window.days（默认 1），语义 = 含基准日的最近 N 天。
    """
    window = job.get("window") or {}
    tz = date_tz_of(job)

    if getattr(args, "dates", ""):
        days = [parse_day_arg(x) for x in args.dates.split(",") if x.strip()]
        if not days:
            raise SystemExit("--dates 为空")
    elif getattr(args, "start_date", "") or getattr(args, "end_date", ""):
        if not (args.start_date and args.end_date):
            raise SystemExit("--start-date 与 --end-date 必须成对出现")
        start, end = parse_day_arg(args.start_date), parse_day_arg(args.end_date)
        if end < start:
            raise SystemExit("--end-date 不能早于 --start-date")
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        # 顺序要紧：先看显式 --bizdate，没有才读环境变量。反过来写的话，
        # env 畸形会在这里二次抛错——即使调用方已经用 --bizdate 拿到了正确业务日
        # （--check 用 strict=False 时这里就真的会二次抛），显式参数优先这条规则必须一致
        if getattr(args, "bizdate", ""):
            base = parse_day_arg(args.bizdate)
        else:
            from_env = env_bizdate(strict=not getattr(args, "check", False))
            base = (from_env if from_env is not None
                    else datetime.now(tz).date() - timedelta(days=1))
        try:
            count = (args.days if getattr(args, "days", None) is not None
                     else int(window.get("days") or 1))
        except (TypeError, ValueError):
            raise SystemExit(f"window.days 必须是整数，实际 {window.get('days')!r}")
        if count < 1:
            raise SystemExit("--days 必须 >= 1")
        days = [base - timedelta(days=count - 1 - i) for i in range(count)]
    return sorted(set(days))


def format_time(value: datetime, fmt: str):
    """按配置格式化时间：支持 unix（秒）/ unix_ms（毫秒）两种特殊格式。"""
    fmt = str(fmt or DEFAULT_TIME_FORMAT)
    if fmt in ("unix", "unix_s"):
        return int(value.timestamp())
    if fmt in ("unix_ms", "unix_millis"):
        return int(value.timestamp() * 1000)
    return value.strftime(fmt)


def _moment(value: datetime, fmt: str, api_tz) -> str | int:
    """按 format 输出一个时间点；纯日期格式不做时区换算。

    format 只到"日"（如 %Y-%m-%d）时，接口要的是"date_tz 里的哪一天"，
    再按 api_tz 换算只会把日期顶到前一天/后一天（如 UTC 的 00:00 在 +08:00 是 08:00，
    但反过来 -05:00 的 00:00 在 +08:00 就跑到下一天）。
    """
    if not is_date_only_format(fmt):
        value = value.astimezone(api_tz)
    return format_time(value, fmt)


def _format_window(win: dict, start: datetime, end: datetime,
                   day: date, last_day: date, tz: ZoneInfo) -> dict:
    """按 api_tz + format 把起止时间转成请求参数字典。

    - start_param / end_param：窗口起止（end_param 可省略，如按天+月份查询的接口）；
    - 纯日期格式走闭区间：end 输出 last_day 本身，不输出"次日"（见下）；
    - extra_params：额外派生参数，值为 strftime 格式或 unix/unix_ms，
      基准时间 = 该窗口日 00:00（date_tz，不含 pad）。
    """
    api_tz = load_api_zone(win.get("api_tz"))
    fmt = str(win.get("format") or DEFAULT_TIME_FORMAT)
    date_only = is_date_only_format(fmt)
    result = {}
    if win.get("start_param"):
        result[str(win["start_param"])] = _moment(start, fmt, api_tz)
    if win.get("end_param"):
        if date_only:
            # 日期参数是**闭区间**（startDate/endDate 这种问"哪几天"的接口）：
            # end 取次日 00:00 的日期会多查一天，落进 pt=业务日 就是"9/18 的分区里
            # 混着 9/19 的数据"——和 pt 口径打架（数据不会少，但口径错了）
            result[str(win["end_param"])] = format_time(
                datetime.combine(last_day, dtime(0, 0), tzinfo=tz), fmt)
        else:
            result[str(win["end_param"])] = _moment(end, fmt, api_tz)
    if win.get("extra_params"):
        base = datetime.combine(day, dtime(0, 0), tzinfo=tz)
        for name, extra_fmt in dict(win["extra_params"]).items():
            result[str(name)] = _moment(base, str(extra_fmt), api_tz)
    return result


def window_param_sets(job: dict, days: list[date]) -> list[dict | None]:
    """日期列表 → 每次请求要附加的窗口参数列表（顺序与 days 对应）。

    - per_day：每天一组（当天 00:00 前后各 pad_hours 小时，按 date_tz 解释后转 api_tz）；
    - range：整个区间一组（days[0] 00:00 到 days[-1] 次日 00:00，前后各 pad_hours）；
    - 未配置 window：返回 [None]（不做时间过滤，单次请求）。
    - format 只到日期时按"日期闭区间"算（见下），pad_hours 不参与计算。
    """
    win = job.get("window") or {}
    if not win:
        return [None]
    try:
        pad_hours = float(win.get("pad_hours") or 0)
    except (TypeError, ValueError):
        raise ConfigError(f"window.pad_hours 必须是数字（小时），实际 {win.get('pad_hours')!r}")
    if pad_hours < 0 or pad_hours > 24:
        raise ConfigError(
            f"window.pad_hours 应在 0~24 之间，实际 {pad_hours:g}："
            f"pad 是「每天前后各扩 pad 小时」（相邻天重合 2×pad 小时，靠 DWD 按主键去重兜底），"
            f"{'传负数相当于把窗口两头往里缩，会漏掉边界数据' if pad_hours < 0 else '超过 24 会让相邻两天重合超过一整天'}"
        )
    tz = date_tz_of(job)
    mode = str(win.get("mode") or "per_day").lower()
    # 纯日期格式的接口要的是"哪一天"，只输出年月日、没有时刻能承载余量：
    # 减 pad 不会"多拉一段"，而是把日期整体顶到前一天（业务日 9/18 发出 9/17），
    # 落进 pt=业务日 就是整表错一天。所以这里直接按天级边界算，忽略 pad。
    fmt = str(win.get("format") or DEFAULT_TIME_FORMAT)
    if pad_hours > 0 and is_date_only_format(fmt):
        log_once(f"  警告：window.format（{fmt}）只到日期，已忽略 pad_hours={pad_hours:g}"
                 f"（日期参数减 pad 会把日期整体顶到前一天）；"
                 f"需要跨天余量请把 format 改成带时分的（如 %Y-%m-%d %H:%M:%S）")
        pad_hours = 0.0
    pad = timedelta(hours=pad_hours)

    if mode == "range":
        start = datetime.combine(days[0], dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(days[-1] + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        return [_format_window(win, start, end, days[0], days[-1], tz)]

    result = []
    for day in days:
        start = datetime.combine(day, dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(day + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        result.append(_format_window(win, start, end, day, day, tz))
    return result
