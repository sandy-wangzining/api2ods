# -*- coding: utf-8 -*-
"""配置：读取、占位符替换、校验（含未知键告警）、目标表解析。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .dates import DEFAULT_DATE_TZ, date_tz_of, env_bizdate
from .utils import ConfigError

ALLOWED_AUTH_TYPES = ("none", "basic", "token", "bearer", "query", "sha256_concat", "aliyun_rpc",
                      "custom")
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ALLOWED_RESPONSE_TYPES = ("json", "bytes")
ALLOWED_PARSE_FORMATS = ("csv", "tsv", "jsonl")
ALLOWED_WINDOW_MODES = ("per_day", "range")
ALLOWED_PAGINATION_TYPES = ("none", "page", "cursor")

JOB_KEYS = {"job", "description", "secrets", "maxcompute", "profiles",
            "request", "window", "pagination", "parse", "target"}
REQUEST_KEYS = {"base_url", "path", "method", "body_type", "timeout_seconds", "headers",
                "params", "params_in", "auth", "records_path", "records_missing", "fail_if",
                "response_type", "verify", "proxies", "retry_times", "retry_delay", "add_fields"}
WINDOW_KEYS = {"mode", "days", "date_tz", "api_tz", "pad_hours", "start_param", "end_param",
               "extra_params", "format"}
PAGINATION_KEYS = {"type", "page_param", "size_param", "page_size", "param_as_string",
                   "total_pages_path", "total_items_path", "strict", "cursor_param", "cursor_path",
                   "cursor_start", "delay_seconds", "max_pages", "window_retries"}
PARSE_KEYS = {"format", "encoding", "delimiter", "skip_rows", "skip_until", "unzip",
              "entry_contains", "entry_field", "strict_encoding", "allow_multi_entry"}
TARGET_KEYS = {"project", "table", "column", "pt", "comment", "allow_empty", "profile",
               "stored_as", "lifecycle_days"}

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
# 校验阶段产生的告警（如 pagination 推断）挂在 job 上，由 collect_warnings 取走并清空。
# 不挂在模块上：模块级缓存会让告警串到下一次调用（测试里、库被复用时的下一次运行都会串）
_WARNINGS_KEY = "__warnings__"
# \A...\Z 而不是 ^...$：$ 会放过结尾的换行（"20260920\n" 静默通过校验）
_PT_RE = re.compile(r"\A[A-Za-z0-9_\-]+\Z")


# =============================================================================
# 读取与占位符
# =============================================================================

def load_json_file(path: Path, desc: str) -> dict:
    """读一个 JSON 文件，失败时给出带路径的明确报错。

    用 utf-8-sig 读：Windows 上记事本 / VSCode 很容易存成「UTF-8 with BOM」，
    按 utf-8 读会在首字符处报 JSONDecodeError，而错误信息完全看不出是 BOM 导致。
    """
    if not path.is_file():
        raise SystemExit(f"找不到{desc}：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{desc}不是合法 JSON（{path}）：{exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{desc}顶层必须是 JSON 对象：{path}")
    return data


def _as_secrets(value, where: str) -> dict:
    """secrets 必须是键值对；写成列表/字符串/数字时给出人话报错。

    不在这里拦住的话，dict.update 会抛出 "dictionary update sequence element #0 has
    length 1; 2 is required" 这种裸 traceback——同样的错误 validate_job 里有一句
    清楚的提示，但 render_job 在它之前就跑完了，那句提示成了永远到不了的死代码。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} 必须是对象（键值对），实际 {type(value).__name__}：{value!r}")
    return dict(value)


def build_context(config: dict, bizdate: date, job_secrets: dict | None = None,
                  tz: ZoneInfo | None = None) -> dict:
    """运行时上下文：secrets（作业内优先）+ 日期。

    ${today} 与 ${bizdate} 用同一个时区口径（window.date_tz，默认 Asia/Shanghai）：
    按运行机器的本地日期算，服务器时区是 UTC 时会比北京时间早一天。
    """
    secrets = _as_secrets(config.get("secrets"), "--config 文件里的 secrets")
    secrets.update(_as_secrets(job_secrets, "作业配置的 secrets"))
    today = datetime.now(tz or ZoneInfo(DEFAULT_DATE_TZ)).date()
    return {
        "secrets": secrets,
        "bizdate": bizdate.strftime("%Y%m%d"),
        "bizdate_iso": bizdate.isoformat(),
        "today": today.strftime("%Y%m%d"),
        "today_iso": today.isoformat(),
    }


def resolve_placeholder(name: str, context: dict):
    """把 ${a.b} 解析成 context 里的值；找不到直接报错（不静默留空）。"""
    node = context
    for part in name.split("."):
        if not isinstance(node, dict) or part not in node:
            if name.startswith("secrets."):
                hint = (f"请在作业文件的 secrets（或 --config 文件）里补上"
                        f"「{name.split('.', 1)[1]}」")
            else:
                hint = "可选：secrets.<键名> / bizdate / bizdate_iso / today / today_iso"
            raise SystemExit(f"配置里引用了不存在的占位符：${{{name}}}；{hint}")
        node = node[part]
    return node


def deep_substitute(value, context: dict):
    """递归替换配置里的占位符（字符串里可混写，如 'Bearer ${secrets.x}'）。

    例子：
        "Bearer ${secrets.token}"      →  "Bearer abc123"
        "${secrets.ids}"（整串占位）    →  直接返回原始对象（列表/数字等，不强制转字符串）
        "${bizdate}"                    →  "20260918"
    找不到的占位符直接报错（不静默留空，避免密钥没配却悄悄请求失败）。
    """
    if isinstance(value, dict):
        return {key: deep_substitute(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [deep_substitute(item, context) for item in value]
    if not isinstance(value, str):
        return value

    match = _PLACEHOLDER_RE.fullmatch(value)
    if match:
        return resolve_placeholder(match.group(1), context)

    def _replace(m: re.Match) -> str:
        """字符串里混着占位符（如 "Bearer ${secrets.t}"）时的替换回调，结果一律转成字符串。"""
        return str(resolve_placeholder(m.group(1), context))

    return _PLACEHOLDER_RE.sub(_replace, value)


def render_job(job_raw: dict, config: dict, bizdate: date) -> dict:
    """作业配置 → 替换占位符（secrets/日期）后的运行时配置。

    作业文件里的 secrets 原样保留（密钥本身不参与占位符替换），只用于 ${secrets.x}。
    """
    context = build_context(config, bizdate, job_raw.get("secrets"), tz=date_tz_of(job_raw))
    job = {key: value for key, value in job_raw.items() if key != "secrets"}
    rendered = deep_substitute(job, context)
    rendered["secrets"] = _as_secrets(job_raw.get("secrets"), "作业配置的 secrets")
    return rendered


def check_block_types(job: dict) -> None:
    """作业文件里几个大块必须是对象，否则给中文报错。

    必须在**读文件之后、任何取值之前**调用：`date_tz_of` 会取 `job["window"]["date_tz"]`，
    `normalize_job` 会 `dict(job["pagination"])`——两者拿到字符串都是
    `'str' object has no attribute 'get'` / "dictionary update sequence" 这种裸 traceback。
    """
    for block in ("window", "pagination", "parse", "target", "request"):
        value = job.get(block)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(
                f"作业配置的 {block} 必须是对象（键值对），实际 {type(value).__name__}：{value!r}"
            )


def normalize_job(job: dict) -> dict:
    """把作业配置补齐成"带默认值"的完整形态（让用户配置尽量短）。

    规则（都只在字段缺失时生效，用户写了就以用户的为准）：
    - pagination：
        · type 缺失时自动推断：有 cursor_path → cursor；有 total_pages_path/total_items_path/page_param → page；否则 none
        · page 类型补默认 page_param=page、size_param=size、page_size=100；cursor 类型补默认 cursor_param=cursor
        · 只写了 size_param/page_size（没写 page_param/终点路径/游标路径）时：不加页码参数、
          只请求一次，等于 none——由 validate_job 给出告警提醒
    - window：
        · start_param / end_param 当成一对：两个都没写才补默认 startTime / endTime
        · 自定义了 start_param 就不再补 end_param（老作业升级后请求参数不变）；
          只要开始时间就只写 start_param，不要结束时间写 "end_param": null
    """
    job = dict(job)
    check_block_types(job)
    pagination = dict(job.get("pagination") or {})
    if pagination:
        page_type = str(pagination.get("type") or "").lower()
        if not page_type:
            if pagination.get("cursor_path"):
                page_type = "cursor"
            elif (pagination.get("total_pages_path") or pagination.get("total_items_path")
                  or pagination.get("page_param")):
                page_type = "page"
            else:
                page_type = "none"
        pagination["type"] = page_type
        if page_type == "page":
            pagination.setdefault("page_param", "page")
            pagination.setdefault("size_param", "size")
            pagination.setdefault("page_size", 100)
        elif page_type == "cursor":
            pagination.setdefault("cursor_param", "cursor")
        job["pagination"] = pagination

    window = dict(job.get("window") or {})
    if window:
        if "start_param" not in window and "end_param" not in window:
            window["start_param"] = "startTime"
            window["end_param"] = "endTime"
        job["window"] = window
    return job


# =============================================================================
# 校验
# =============================================================================

def _check_unknown_keys(obj: dict, allowed: set, where: str, warnings: list) -> None:
    """逐个键比对白名单，命中就追加一条告警（JSON 没有注释，以 // 或 # 开头的键当注释放过）。"""
    for key in obj:
        if key.startswith("//") or key.startswith("#"):
            continue
        if key not in allowed:
            warnings.append(f"{where}.{key} 不是已知配置项（拼写错误？）——已忽略")


def collect_warnings(job: dict) -> list[str]:
    """收集未知配置项告警（拼写错误提醒），不阻断运行。"""
    warnings: list[str] = []
    # 先取走校验阶段挂上来的告警：_WARNINGS_KEY 不是用户配置项，
    # 留在 job 里会被下面的未知键扫描当成拼写错误再报一条
    warnings.extend(job.pop(_WARNINGS_KEY, None) or [])
    _check_unknown_keys(job, JOB_KEYS, "作业", warnings)
    _check_unknown_keys(job.get("request") or {}, REQUEST_KEYS, "request", warnings)
    _check_unknown_keys(job.get("window") or {}, WINDOW_KEYS, "window", warnings)
    _check_unknown_keys(job.get("pagination") or {}, PAGINATION_KEYS, "pagination", warnings)
    _check_unknown_keys(job.get("parse") or {}, PARSE_KEYS, "parse", warnings)
    _check_unknown_keys(job.get("target") or {}, TARGET_KEYS, "target", warnings)
    return warnings


def validate_job(job: dict) -> None:
    """校验作业配置的必填项与枚举值（错误信息带字段路径，方便直接改）。"""
    request = job.get("request") or {}
    target = job.get("target") or {}
    if not request.get("base_url"):
        raise SystemExit("作业配置缺少 request.base_url（API 根地址）")
    if not target.get("table"):
        raise SystemExit("作业配置缺少 target.table（MaxCompute 目标表名）")
    if "secrets" in job and not isinstance(job["secrets"], dict):
        raise SystemExit("作业配置的 secrets 必须是对象（键值对）")
    for block in ("maxcompute", "profiles"):
        if block in job and not isinstance(job[block], dict):
            raise SystemExit(f"作业配置的 {block} 必须是对象")
    # 子项也要查：profiles.prod 写成字符串时，取用它的一刻才抛裸 ValueError
    # （get_mc_profile_meta 里还有一道兜底，这里让报错在"校验配置"阶段就出现）
    for key, value in (job.get("profiles") or {}).items():
        if not isinstance(value, dict):
            raise SystemExit(f"作业配置的 profiles.{key} 必须是对象（键值对），"
                             f"实际 {type(value).__name__}：{value!r}")
    if request.get("add_fields") is not None and not isinstance(request["add_fields"], dict):
        raise SystemExit("request.add_fields 必须是对象（如 {\"source_account\": \"账号A\"}）")
    # 这两处写错类型会在发请求时才崩：headers 写成数组是 AttributeError，
    # params 写成数组则被 requests 当"参数名列表"静默丢掉全部固定参数——
    # 拿不到参数接口常常照样回 200，于是"成功"写进一份缺过滤条件的数据
    for block, where in (("headers", "request.headers"), ("params", "request.params"),
                         ("proxies", "request.proxies")):
        value = request.get(block)
        if value is not None and not isinstance(value, dict):
            raise SystemExit(f"{where} 必须是对象（键值对），实际 {type(value).__name__}：{value!r}")

    method = str(request.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise SystemExit(f"request.method 不支持：{method}（可用 {'/'.join(ALLOWED_METHODS)}）")

    auth = request.get("auth") or {}
    auth_type = str(auth.get("type") or "none").lower()
    if auth_type not in ALLOWED_AUTH_TYPES:
        raise SystemExit(f"request.auth.type 不支持：{auth_type}（可用 {'/'.join(ALLOWED_AUTH_TYPES)}）")
    if auth_type == "custom" and not auth.get("func"):
        raise SystemExit("auth.type=custom 必须给 auth.func（函数名）")
    if auth_type == "bearer" and not (auth.get("token") or auth.get("value")):
        raise SystemExit("auth.type=bearer 必须给 auth.token（如 {\"type\": \"bearer\", \"token\": \"xxx\"}）")
    if auth_type == "aliyun_rpc":
        for field in ("access_key_id", "access_key_secret"):
            if not auth.get(field):
                raise SystemExit(f"auth.type=aliyun_rpc 必须给 auth.{field}")

    response_type = str(request.get("response_type") or "json").lower()
    if response_type not in ALLOWED_RESPONSE_TYPES:
        raise SystemExit(f"request.response_type 不支持：{response_type}（可用 json/bytes）")

    records_missing = str(request.get("records_missing") or "error").lower()
    if records_missing not in ("error", "empty"):
        raise SystemExit(f"request.records_missing 不支持：{records_missing}（可用 error/empty）")

    params_in = str(request.get("params_in") or "query").lower()
    if params_in not in ("query", "headers"):
        raise SystemExit(f"request.params_in 不支持：{params_in}（可用 query/headers）")

    window = job.get("window") or {}
    mode = str(window.get("mode") or "per_day").lower()
    if window:
        if mode not in ALLOWED_WINDOW_MODES:
            raise SystemExit(f"window.mode 不支持：{mode}（可用 per_day/range）")
        if mode == "range" and not window.get("end_param"):
            raise SystemExit("window.mode=range 时必须给 window.end_param"
                             "（或把 start_param/end_param 都删掉，用默认 startTime/endTime）")

    pagination = job.get("pagination") or {}
    page_type = str(pagination.get("type") or "none").lower()
    if page_type not in ALLOWED_PAGINATION_TYPES:
        raise SystemExit(f"pagination.type 不支持：{page_type}（可用 none/page/cursor）")
    if (page_type == "none" and "page_size" in pagination
            and pagination.get("size_param") is not None):
        # 只配了"页大小"没有页码/终点：请求里会带上 size 但永远只拉一页，
        # 数据量一大就静默少拿（看着像配了分页）
        job.setdefault(_WARNINGS_KEY, []).append(
            "pagination 里只配了 size_param/page_size，没有页码或翻页终点，"
            "本次只会请求一次（等于 type=none）；确实要分页请补 page_param + "
            "total_pages_path/total_items_path，或补 cursor_path 用游标分页"
        )
    if page_type == "page":
        if not (pagination.get("total_pages_path") or pagination.get("total_items_path")):
            raise SystemExit(
                "分页类型 page 必须给 pagination.total_pages_path 或 pagination.total_items_path"
                "（否则无法判断何时翻完）"
            )
    if page_type == "cursor" and not pagination.get("cursor_path"):
        raise SystemExit("分页类型 cursor 必须给 pagination.cursor_path（从返回里取下一页游标的路径）")
    if response_type == "bytes" and page_type != "none":
        raise SystemExit("response_type=bytes（文件类响应）不支持分页，请把 pagination.type 设为 none")

    parse = job.get("parse") or {}
    if response_type == "bytes":
        parse_format = str(parse.get("format") or "").lower()
        if parse_format not in ALLOWED_PARSE_FORMATS:
            raise SystemExit(
                f"response_type=bytes 时必须在 parse.format 指定解析方式：{'/'.join(ALLOWED_PARSE_FORMATS)}"
            )
    elif parse.get("format"):
        parse_format = str(parse.get("format")).lower()
        if parse_format not in ALLOWED_PARSE_FORMATS:
            raise SystemExit(f"parse.format 不支持：{parse_format}（可用 {'/'.join(ALLOWED_PARSE_FORMATS)}）")

    fail_if = request.get("fail_if") or []
    if not isinstance(fail_if, list):
        raise SystemExit("request.fail_if 必须是数组（每个元素形如 {\"path\": \"Code\", \"not_equals\": \"Success\"}）")
    for cond in fail_if:
        if not isinstance(cond, dict) or not cond.get("path"):
            raise SystemExit("request.fail_if 每个元素必须包含 path")
        if "equals" not in cond and "not_equals" not in cond:
            raise SystemExit("request.fail_if 每个元素必须包含 equals 或 not_equals")


def _backfill_without_bizdate(args) -> bool:
    """补数（--dates / --start-date+--end-date）却没给业务日时为 True。

    两者都只指定"拉哪些天"，不指定"写进哪个分区"——一次运行只写一个 pt，
    所以补数会把整段窗口的数据全部写进默认业务日分区，并把该分区先删再填。
    默认业务日又恰好是调度天天在写的那个分区，等于把最新数据换成历史数据。

    --check 是只读体检（不写库），不拦——补数前先 --check 看连不连通是正常用法。
    --dry-run 同样不写库，也放行：补数前先试跑看条数正是它存在的意义。
    """
    if getattr(args, "check", False) or getattr(args, "dry_run", False):
        return False
    explicit_pt = bool(str(getattr(args, "pt", "") or "").strip())
    explicit_bizdate = bool(str(getattr(args, "bizdate", "") or "").strip())
    # 前两项是"短路"：有了显式值就不该再读环境变量——畸形 env 会在这里抛错，
    # 而用户明明已经用 --bizdate/--pt 指定了分区。环境变量本身仍然算锚定：
    # 调度里 env 正常时补数跟着它走是合理用法（pt 就是那个业务日）
    anchored = explicit_bizdate or explicit_pt or env_bizdate() is not None
    dates = str(getattr(args, "dates", "") or "").strip()
    window = (str(getattr(args, "start_date", "") or "").strip()
              or str(getattr(args, "end_date", "") or "").strip())
    return (dates or window) and not explicit_pt and not anchored


def resolve_target(job: dict, config: dict, args, bizdate: date) -> tuple[str, str, str, str]:
    """解析目标表信息 → (project, table, column, pt值)。"""
    target = job.get("target") or {}
    profile = get_mc_profile_meta(config, job, args)
    project = str(target.get("project") or profile.get("project") or "")
    if not project:
        raise SystemExit(
            "没有目标项目：请在作业文件的 maxcompute.project（或 profiles.<名>.project）"
            "或 target.project 里指定"
        )
    table = str(target.get("table") or "")
    column = str(target.get("column") or "json")
    if _backfill_without_bizdate(args):
        raise SystemExit(
            "补数（--dates / --start-date+--end-date）必须跟着 --bizdate 一起用，"
            "否则整段数据会全部落进默认业务日分区，并先删后填覆盖掉调度刚写的数据。\n"
            "  用法：--bizdate 20260920 --start-date 2026-07-01 --end-date 2026-09-20\n"
            "  含义：整段补数数据写进 pt=20260920（一个分区装一次运行，与调度口径一致）"
        )
    pt = str(getattr(args, "pt", "") or target.get("pt") or bizdate.strftime("%Y%m%d"))
    if not _PT_RE.match(pt):
        raise SystemExit(f"pt 值不合法（应为字母数字下划线中划线）：{pt!r}，可用 --pt 覆盖")
    return project, table, column, pt


# =============================================================================
# MaxCompute 凭证 profile
# =============================================================================

def _profile_source(source: dict, where: str) -> dict:
    """取一份配置里的 profiles 映射；写成非对象时给出人话报错。

    profiles 整体写成字符串时更危险：`"default" in "oops"` 是子串判断，会静默通过，
    最后返回 {}（"没配凭证"），用户以为凭证生效了，实际连的是环境默认身份。
    """
    profiles = source.get("profiles")
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise SystemExit(f"{where} 的 profiles 必须是对象（形如 {{\"default\": {{...}}}}），"
                         f"实际 {type(profiles).__name__}：{profiles!r}")
    return profiles


def get_mc_profile_meta(config: dict, job: dict, args) -> dict:
    """取作业使用的 MaxCompute profile 元信息（project/endpoint/ak/sk）。

    查找顺序：作业文件的 profiles.<名> / maxcompute → --config 文件的 profiles.<名> / maxcompute。
    （作业内优先，方便一份作业自带全部凭证。）
    """
    name = str(getattr(args, "mc_profile", "") or (job.get("target") or {}).get("profile") or "default").strip()
    available: list[str] = []

    for source, where in ((job, "作业配置"), (config, "--config 文件")):
        profiles = _profile_source(source, where)
        available += [f"{key}" for key in profiles if key not in available]
        if name in profiles:
            entry = profiles[name]
            # 值必须是对象：写成字符串时 dict() 会抛 "dictionary update sequence element
            # #0 has length 1; 2 is required" 这种裸异常，看不出是哪个字段写错了
            if not isinstance(entry, dict):
                raise SystemExit(f"{where} 的 profiles.{name} 必须是对象（键值对），"
                                 f"实际 {type(entry).__name__}：{entry!r}")
            return dict(entry)
        if name == "default" and source.get("maxcompute"):
            block = source["maxcompute"]
            if not isinstance(block, dict):
                raise SystemExit(f"{where} 的 maxcompute 必须是对象（键值对），"
                                 f"实际 {type(block).__name__}：{block!r}")
            return dict(block)
    if name == "default":
        return {}
    raise SystemExit(f"找不到 MaxCompute profile「{name}」；已配置：{available or ['（无）']}")


def build_context_doc() -> str:
    """给 --help / --check 用的占位符说明。"""
    return "${secrets.xxx} / ${bizdate} / ${bizdate_iso} / ${today} / ${today_iso}"
