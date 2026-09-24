# -*- coding: utf-8 -*-
"""日期与时区：参数解析、业务日列表、窗口参数（起止时间 / unix 秒）。"""

from __future__ import annotations

import math
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
# 带时刻的格式指令：出现任意一个就说明接口要的是"时间点"，需要按时区换算。
# %R（= %H:%M）/%r（12 小时制）/ %s（epoch 秒）也是常见的"带时刻"写法，
# 漏掉它们会把这类 format 误判成"只到日期"：跳过时区换算、end 取闭区间，
# 窗口直接塌成零长度（start == end）而没有任何告警
_TIME_TOKENS = ("%H", "%I", "%M", "%S", "%f", "%p", "%P", "%X", "%T", "%R", "%r", "%s", "%c", "%z", "%Z")
_UNIX_FORMATS = ("unix", "unix_s", "unix_ms", "unix_millis")
# strftime 指令白名单 = Windows 与 glibc 都认的交集（外加我们自己实现的 %s / %P）。
# 实测 MSVC 不认的：k l N P q s v 与所有修饰符（%-d/%_d/%0d/%^B）；glibc 则是不认的
# 原样输出。取交集才能保证"同一份配置在 Linux 与 Windows 上行为一致"，
# 而不是一边崩、一边把 "%Q" 当参数值发给接口
_STRFTIME_CODES = set("aAbBcCdDeFgGhHIjmMnprRStTuUVWwWxXyYzZ%") | {"s", "P"}
# 下面这些指令的输出由 C 库的 locale / 平台时区库决定，同一份配置在不同机器上可能不一样：
# 中文/法语 Windows 上 %b 给「9月」「sept.」，英文 Linux 给 "Sep"；%c/%x/%X 是整体 locale
# 格式；%Z 取平台时区缩写（"CST"/"China Standard Time"）。同一个作业在开发机与调度机
# 拼出的参数值不同，接口按值匹配（如按日期字符串查账）时会静默查不到数据。
# 与 %s / %P 同样处理：由 format_time 自己实现，固定成 C locale 的英文写法。
_LOCALE_DEPENDENT_CODES = ("a", "A", "b", "B", "p", "c", "x", "X", "r", "Z")
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_FULL = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _locale_free_values(value: datetime) -> dict:
    """%a/%A/%b/%B/%p/%c/%x/%X/%r/%Z 的确定性取值（C locale + tzinfo 自带的时区名）。

    %c/%x/%r 按 C locale 的定义展开（%c = "%a %b %e %H:%M:%S %Y"、%x = "%m/%d/%y"、
    %r = "%I:%M:%S %p"）；%Z 用 tzinfo.tzname()，比平台 strftime 的缩写稳定。
    """
    hour12 = value.hour % 12 or 12
    ampm = "AM" if value.hour < 12 else "PM"
    weekday_abbr = _WEEKDAY_ABBR[value.weekday()]
    month_abbr = _MONTH_ABBR[value.month - 1]
    return {
        "a": weekday_abbr,
        "A": _WEEKDAY_FULL[value.weekday()],
        "b": month_abbr,
        "B": _MONTH_FULL[value.month - 1],
        "p": ampm,
        "c": (
            f"{weekday_abbr} {month_abbr} {value.day:2d} "
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d} {value.year:04d}"
        ),
        "x": f"{value.month:02d}/{value.day:02d}/{value.year % 100:02d}",
        "X": f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}",
        "r": f"{hour12:02d}:{value.minute:02d}:{value.second:02d} {ampm}",
        "Z": value.tzname() or "",
    }


_DIRECTIVE_RE = re.compile(r"%(.)", re.S)
_MODIFIERS = "-_0^#"
# 日期参数白名单：只认紧凑与 ISO 两种写法（见 parse_day_arg 的说明）
_DAY_COMPACT_RE = re.compile(r"\A\d{8}\Z")
_DAY_ISO_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def is_date_only_format(fmt: str) -> bool:
    """格式是否只到"日"（没有时分秒），如 %Y-%m-%d / %Y%m%d / %Y-%m。"""
    fmt = str(fmt or "")
    if fmt.lower() in _UNIX_FORMATS:
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
            f"无法识别时区：{name}（如 America/New_York / Asia/Shanghai / UTC；Windows 本机需 pip install tzdata）"
        )


def date_tz_of(job: dict) -> ZoneInfo:
    """窗口/业务日按配置时区解释（默认 Asia/Shanghai）。"""
    return load_zone(str((job.get("window") or {}).get("date_tz") or DEFAULT_DATE_TZ))


def parse_day_arg(text: str) -> date:
    """解析日期参数：YYYYMMDD 或 YYYY-MM-DD。

    两条正则白名单而不是 date.fromisoformat：3.11 起 fromisoformat 还认 ISO 周日期
    （"2026-W36-1" 会静默解析成 2026-08-31）与紧凑写法，同一个 --bizdate 在不同
    Python 版本上行为不同，写错的日子会静默写进错分区。
    """
    value = str(text or "").strip()
    if _DAY_COMPACT_RE.match(value):
        try:
            return date(int(value[:4]), int(value[4:6]), int(value[6:]))
        except ValueError as exc:
            # 形态对但日期不存在（20261301、20260230）：与 ISO 写法给同样的提示，
            # 不能让裸 ValueError 冒到用户脸上
            raise SystemExit(f"日期不存在：{text!r}（{exc}）")
    if _DAY_ISO_RE.match(value):
        try:
            return date(int(value[:4]), int(value[5:7]), int(value[8:10]))
        except ValueError as exc:
            raise SystemExit(f"日期不存在：{text!r}（{exc}）")
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
            log(
                f"  警告：环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}；"
                f"只读体检（--check）不写库，按默认业务日继续"
            )
            return None
        raise SystemExit(
            f"环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期：{raw!r}"
            f"（应为 YYYYMMDD 或 YYYY-MM-DD）；不打算用它请先 unset，或用 --bizdate 显式指定业务日"
        ) from exc


def resolve_days(args, job: dict, bizdate: date | None = None) -> list[date]:
    """命令行参数 → 待拉取的日期列表（升序）。

    优先级：--dates > --start-date/--end-date > --bizdate（或环境变量）> 默认（时区昨天）。
    回拉天数：--days 覆盖配置 window.days（默认 1），语义 = 含基准日的最近 N 天。

    bizdate：调用方（main）已经定好的业务日；给了它就别再自己读时钟——原来这里和
    main 各读一次 datetime.now()，跨零点时 pt 与"拉哪几天"会错开一天（数据写进错的
    pt 且先删后填）。不传时保持旧行为（独立可用、测试友好）。
    """
    window = job.get("window") or {}
    tz = date_tz_of(job)

    if getattr(args, "days", None) is not None and (
        getattr(args, "dates", "") or getattr(args, "start_date", "") or getattr(args, "end_date", "")
    ):
        # 补数模式下拉取范围由 --dates / --start-date+--end-date 决定，--days 不参与；
        # 静默忽略会让"--start-date X --end-date Y --days 1"看起来像只拉一天
        log_once("  提示：补数模式（--dates / --start-date+--end-date）下 --days 不生效")

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
        elif bizdate is not None:
            # 调用方已经算过业务日（--bizdate / 环境变量 / 默认昨天），直接用——
            # 在这里二次读时钟会与 pt 的取值错开（见 docstring）
            base = bizdate
        else:
            from_env = env_bizdate(strict=not getattr(args, "check", False))
            base = from_env if from_env is not None else datetime.now(tz).date() - timedelta(days=1)
        from_cli = getattr(args, "days", None) is not None
        try:
            if from_cli:
                count = int(args.days)
            else:
                configured = window.get("days")
                # 不用 `or 1`：0 会被静默当成默认值 1（window.days=0 是笔误，该报错）
                count = 1 if configured is None or configured == "" else int(configured)
        except (TypeError, ValueError):
            raise SystemExit(f"window.days 必须是整数，实际 {window.get('days')!r}")
        if count < 1:
            raise SystemExit(f"{'--days' if from_cli else 'window.days'} 必须 >= 1，实际 {count}")
        days = [base - timedelta(days=count - 1 - i) for i in range(count)]
    return sorted(set(days))


def check_format_string(fmt: str, field: str = "window.format") -> None:
    """按白名单校验 strftime 格式串里的指令（跨平台一致）。

    各平台 C 库对"不认识的指令"反应不同：MSVC 抛 ValueError（裸 traceback），
    glibc 原样输出 `%Q`——于是同一个配置在 Linux 上会把"%Q"当成参数值发给接口，
    在 Windows 上直接崩。与其两边行为都对不上，不如在配置阶段就挡掉。

    field 只用于报错文案：同一个白名单也要校验 window.extra_params 的值。
    """

    def _check(match: re.Match) -> str:
        char = match.group(1)
        if char in _MODIFIERS:
            raise ConfigError(
                f"{field} 不支持带修饰符的指令 %{char}…：{fmt!r}；（如 %-d 去零填充只有 glibc 认，Windows 会直接报错）"
            )
        if char not in _STRFTIME_CODES:
            raise ConfigError(f"{field} 里有不支持的格式指令 %{char}：{fmt!r}；epoch 秒请写 unix 或 %s，毫秒写 unix_ms")
        return ""

    rest = _DIRECTIVE_RE.sub(_check, str(fmt))
    if "%" in rest:
        raise ConfigError(f"{field} 里有写坏的格式指令（孤立的 %）：{fmt!r}")


def _protect_extension(fmt: str, code: str, sentinel: str) -> str:
    """把「未被 %% 转义掉的 %<code>」替换成哨兵，`%%` 原样保留。

    不能用 `(?<!%)%P` 这类后行断言：它只看前一个字符，`%%%P` 里第二个 `%` 是转义符、
    第三个 `%` 开头的才是真指令，却被当成"前面有 % 所以已转义"漏掉（Windows 抛
    ValueError、Linux 输出 `%pm`——同一份配置的跨平台差异正是这么来的）。这里按 `%%`
    成对消费逐段扫描，奇偶天然正确：`%%s`/`%%P` 是字面量，`%s`/`%P` 才替换。
    """
    out: list[str] = []
    index = 0
    length = len(fmt)
    while index < length:
        char = fmt[index]
        if char != "%":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:  # 结尾孤立 %（check_format_string 已拦，兜底不崩）
            out.append(char)
            index += 1
            continue
        nxt = fmt[index + 1]
        if nxt == "%":  # %% → 字面量 %
            out.append("%%")
        elif nxt == code:  # 未转义的目标指令 → 哨兵
            out.append(sentinel)
        else:  # 其它指令原样保留（两字符）
            out.append("%" + nxt)
        index += 2
    return "".join(out)


def format_time(value: datetime, fmt: str):
    """按配置格式化时间：支持 unix（秒）/ unix_ms（毫秒），并自己实现 %s / %P。

    `%s`（epoch 秒）与 `%P`（小写 am/pm）是 glibc/BSD 扩展，MSVC 的 strftime 不认：
    同一份配置在 Linux 能跑、到 Windows 直接抛 ValueError（裸 traceback，退出码 1，
    --log-file 里什么都没有）；反过来 glibc 会把混在格式串里的 %s 展开成 epoch 秒
    （看起来就是个普通大整数）。这两个指令在格式串的**任何位置**都由这里自己实现，
    跨平台一致；其余认不出的指令由 check_format_string 挡掉。

    fmt 恰好是 "%s" 时返回 int（保持"时间戳"语义，_moment 直接当参数发出去）；
    与其它文本混用时返回 str（如 "%Y-%s" → "2026-1789889400"）。
    """
    fmt = str(fmt or DEFAULT_TIME_FORMAT)
    lowered = fmt.lower()
    if lowered in ("unix", "unix_s"):
        return int(value.timestamp())
    if lowered in ("unix_ms", "unix_millis"):
        return int(value.timestamp() * 1000)
    if fmt == "%s":
        return int(value.timestamp())
    check_format_string(fmt)
    # %s / %P 先换成哨兵再交给 strftime（不这么绕的话，Windows 上 strftime 会直接抛错）。
    # 哨兵不能用 \x00：strftime 收的是 C 字符串，NUL 会把它后面的内容整段截掉（Linux 实测输出空串）。
    # 替换按 %% 转义规则逐段扫描：只换真正是"指令"的那个，%%s / %%P 是字面量。
    s_sentinel = "\x01s\x01"
    p_sentinel = "\x01P\x01"
    protected = _protect_extension(fmt, "s", s_sentinel)
    protected = _protect_extension(protected, "P", p_sentinel)
    # locale/平台相关指令同样先换成哨兵：交给 strftime 的话，中文/法语环境下 %b 会
    # 拼出「9月」「sept.」，同一份配置在开发机与调度机上算出不同的参数值
    locale_free = _locale_free_values(value)
    substitutions = {s_sentinel: str(int(value.timestamp())), p_sentinel: "am" if value.hour < 12 else "pm"}
    for code in _LOCALE_DEPENDENT_CODES:
        sentinel = f"\x01{code}\x01"
        substitutions[sentinel] = locale_free[code]
        protected = _protect_extension(protected, code, sentinel)
    out = value.strftime(protected)
    for sentinel, text in substitutions.items():
        if sentinel in out:
            out = out.replace(sentinel, text)
    return out


def _moment(value: datetime, fmt: str, api_tz) -> str | int:
    """按 format 输出一个时间点；纯日期格式不做时区换算。

    format 只到"日"（如 %Y-%m-%d）时，接口要的是"date_tz 里的哪一天"，
    再按 api_tz 换算只会把日期顶到前一天/后一天（如 UTC 的 00:00 在 +08:00 是 08:00，
    但反过来 -05:00 的 00:00 在 +08:00 就跑到下一天）。
    """
    if not is_date_only_format(fmt):
        value = value.astimezone(api_tz)
    return format_time(value, fmt)


def _format_window(win: dict, start: datetime, end: datetime, day: date, last_day: date, tz: ZoneInfo) -> dict:
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
            result[str(win["end_param"])] = format_time(datetime.combine(last_day, dtime(0, 0), tzinfo=tz), fmt)
        else:
            result[str(win["end_param"])] = _moment(end, fmt, api_tz)
    if win.get("extra_params"):
        base = datetime.combine(day, dtime(0, 0), tzinfo=tz)
        for name, extra_fmt in dict(win["extra_params"]).items():
            result[str(name)] = _moment(base, str(extra_fmt), api_tz)
    return result


def _check_range_extra_params(win: dict, days: list[date], tz) -> None:
    """range 模式下 extra_params 只按区间首日派生：跨月/跨年会静默少拉数据，直接报配置错。

    per_day 每天各自派生、没有这个问题；range 整段只发一次请求，像 BillingCycle=%Y-%m
    这样的派生参数只能取首日的值。接口一旦拿它当过滤条件，第一个周期之后的数据就静默
    拉不到（请求有效、条数 > 0、写后校验还自洽），属于"宁可失败"要拦下的那类。
    """
    extra = win.get("extra_params") or {}
    if not extra or days[0] == days[-1]:
        return
    api_tz = load_api_zone(win.get("api_tz"))
    base_first = datetime.combine(days[0], dtime(0, 0), tzinfo=tz)
    base_last = datetime.combine(days[-1], dtime(0, 0), tzinfo=tz)
    for name, extra_fmt in dict(extra).items():
        first_value = _moment(base_first, str(extra_fmt), api_tz)
        last_value = _moment(base_last, str(extra_fmt), api_tz)
        if first_value != last_value:
            raise ConfigError(
                f"window.extra_params 的 {name}（{extra_fmt}）在区间 {days[0]} ~ {days[-1]} 上会变"
                f"（{first_value} → {last_value}）：range 模式整段只发一次请求、派生参数只能取首日的值，"
                f"后半段数据会静默拉不到。请改用 window.mode=per_day（每天一个请求），"
                f"或把区间收窄到该参数不跨界的范围"
            )


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
    if not math.isfinite(pad_hours):
        # NaN 跟谁比都是 False，会绕过下面的 0~24 校验，最后在 timedelta 里抛裸 ValueError
        raise ConfigError(f"window.pad_hours 必须是有限数字，实际 {win.get('pad_hours')!r}")
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
        log_once(
            f"  警告：window.format（{fmt}）只到日期，已忽略 pad_hours={pad_hours:g}"
            f"（日期参数减 pad 会把日期整体顶到前一天）；"
            f"需要跨天余量请把 format 改成带时分的（如 %Y-%m-%d %H:%M:%S）"
        )
        pad_hours = 0.0
    pad = timedelta(hours=pad_hours)

    if mode == "range":
        start = datetime.combine(days[0], dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(days[-1] + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        _check_range_extra_params(win, days, tz)
        return [_format_window(win, start, end, days[0], days[-1], tz)]

    result = []
    for day in days:
        start = datetime.combine(day, dtime(0, 0), tzinfo=tz) - pad
        end = datetime.combine(day + timedelta(days=1), dtime(0, 0), tzinfo=tz) + pad
        result.append(_format_window(win, start, end, day, day, tz))
    return result
