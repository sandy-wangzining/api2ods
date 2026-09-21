# -*- coding: utf-8 -*-
"""日期与时区：参数解析、业务日列表、窗口参数（起止时间 / unix 秒）。"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

DEFAULT_DATE_TZ = "Asia/Shanghai"
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")


def parse_offset(text: str) -> timezone:
    """把 '+08:00' / '+0800' 解析成固定偏移时区；解析失败抛错。"""
    match = _OFFSET_RE.match((text or "").strip())
    if not match:
        raise SystemExit(f"无法解析时区偏移：{text!r}（应形如 +08:00）")
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    if sign == "-":
        delta = -delta
    return timezone(delta)


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


def env_bizdate() -> date | None:
    """DataWorks 环境变量 bizdate / SKYNET_BIZDATE（YYYYMMDD）。"""
    text = (os.environ.get("bizdate") or os.environ.get("SKYNET_BIZDATE") or "").strip()
    if not text:
        return None
    try:
        return parse_day_arg(text)
    except SystemExit:
        return None


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
        if getattr(args, "bizdate", ""):
            base = parse_day_arg(args.bizdate)
        elif env_bizdate() is not None:
            base = env_bizdate()
        else:
            base = datetime.now(tz).date() - timedelta(days=1)
        count = args.days if getattr(args, "days", None) is not None else int(window.get("days") or 1)
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


def _format_window(win: dict, start: datetime, end: datetime,
                   day: date, tz: ZoneInfo) -> dict:
    """按 api_tz + format 把起止时间转成请求参数字典。

    - start_param / end_param：窗口起止（end_param 可省略，如按天+月份查询的接口）；
    - extra_params：额外派生参数，值为 strftime 格式或 unix/unix_ms，
      基准时间 = 该窗口日 00:00（date_tz，不含 pad），再换算到 api_tz。
    """
    api_tz = parse_offset(str(win.get("api_tz") or "+08:00"))
    fmt = str(win.get("format") or DEFAULT_TIME_FORMAT)
    result = {str(win["start_param"]): format_time(start.astimezone(api_tz), fmt)}
    if win.get("end_param"):
        result[str(win["end_param"])] = format_time(end.astimezone(api_tz), fmt)
    if win.get("extra_params"):
        base = datetime.combine(day, dtime(0, 0), tzinfo=tz).astimezone(api_tz)
        for name, extra_fmt in dict(win["extra_params"]).items():
            result[str(name)] = format_time(base, str(extra_fmt))
    return result


def window_param_sets(job: dict, days: list[date]) -> list[dict | None]:
    """日期列表 → 每次请求要附加的窗口参数列表（顺序与 days 对应）。

    - per_day：每天一组（当天 00:00 前后各 pad_hours 小时，按 date_tz 解释后转 api_tz）；
    - range：整个区间一组（days[0] 00:00 到 days[-1] 次日 00:00，前后各 pad_hours）；
    - 未配置 window：返回 [None]（不做时间过滤，单次请求）。
    """
    win = job.get("window") or {}
    if not win:
        return [None]
    pad = timedelta(hours=float(win.get("pad_hours") or 0))
    tz = date_tz_of(job)
    mode = str(win.get("mode") or "per_day").lower()

    if mode == "range":
        start = datetime.combine(days[0], dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(days[-1] + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        return [_format_window(win, start, end, days[0], tz)]

    result = []
    for day in days:
        start = datetime.combine(day, dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(day + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        result.append(_format_window(win, start, end, day, tz))
    return result
