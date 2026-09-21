# -*- coding: utf-8 -*-
"""配置：读取、占位符替换、校验（含未知键告警）、目标表解析。"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

ALLOWED_AUTH_TYPES = ("none", "basic", "token", "bearer", "query", "sha256_concat", "aliyun_rpc", "custom")
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
                   "total_pages_path", "total_items_path", "cursor_param", "cursor_path",
                   "cursor_start", "delay_seconds", "max_pages", "window_retries"}
PARSE_KEYS = {"format", "encoding", "delimiter", "skip_rows", "skip_until", "unzip",
              "entry_contains", "entry_field"}
TARGET_KEYS = {"project", "table", "column", "pt", "comment", "allow_empty", "profile",
               "stored_as", "lifecycle_days"}

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
_PT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


# =============================================================================
# 读取与占位符
# =============================================================================

def load_json_file(path: Path, desc: str) -> dict:
    """读一个 JSON 文件，失败时给出带路径的明确报错。"""
    if not path.is_file():
        raise SystemExit(f"找不到{desc}：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{desc}不是合法 JSON（{path}）：{exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{desc}顶层必须是 JSON 对象：{path}")
    return data


def build_context(config: dict, bizdate: date, job_secrets: dict | None = None) -> dict:
    """运行时上下文：secrets（作业内优先）+ 日期。"""
    secrets = dict(config.get("secrets") or {})
    secrets.update(job_secrets or {})
    return {
        "secrets": secrets,
        "bizdate": bizdate.strftime("%Y%m%d"),
        "bizdate_iso": bizdate.isoformat(),
        "today": date.today().strftime("%Y%m%d"),
        "today_iso": date.today().isoformat(),
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
        return str(resolve_placeholder(m.group(1), context))

    return _PLACEHOLDER_RE.sub(_replace, value)


def render_job(job_raw: dict, config: dict, bizdate: date) -> dict:
    """作业配置 → 替换占位符（secrets/日期）后的运行时配置。

    作业文件里的 secrets 原样保留（密钥本身不参与占位符替换），只用于 ${secrets.x}。
    """
    context = build_context(config, bizdate, job_raw.get("secrets"))
    job = {key: value for key, value in job_raw.items() if key != "secrets"}
    rendered = deep_substitute(job, context)
    rendered["secrets"] = dict(job_raw.get("secrets") or {})
    return rendered


def normalize_job(job: dict) -> dict:
    """把作业配置补齐成"带默认值"的完整形态（让用户配置尽量短）。

    规则（都只在字段缺失时生效，用户写了就以用户的为准）：
    - pagination：
        · type 缺失时自动推断：有 cursor_path → cursor；有 total_pages_path/total_items_path/page_param → page；否则 none
        · page 类型补默认 page_param=page、size_param=size、page_size=100
        · cursor 类型补默认 cursor_param=cursor
    - window：
        · start_param 默认 startTime；end_param 默认 endTime
        · 显式写 "end_param": null 表示"不要结束时间参数"（如阿里云按天+月份查询的接口）
    """
    job = dict(job)
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
        window.setdefault("start_param", "startTime")
        window.setdefault("end_param", "endTime")
        job["window"] = window
    return job


# =============================================================================
# 校验
# =============================================================================

def _check_unknown_keys(obj: dict, allowed: set, where: str, warnings: list) -> None:
    for key in obj:
        if key.startswith("//") or key.startswith("#"):
            continue
        if key not in allowed:
            warnings.append(f"{where}.{key} 不是已知配置项（拼写错误？）——已忽略")


def collect_warnings(job: dict) -> list[str]:
    """收集未知配置项告警（拼写错误提醒），不阻断运行。"""
    warnings: list[str] = []
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
    if request.get("add_fields") is not None and not isinstance(request["add_fields"], dict):
        raise SystemExit("request.add_fields 必须是对象（如 {\"source_account\": \"账号A\"}）")

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
            raise SystemExit("window.mode=range 时必须给 window.end_param（或删掉 end_param 让它用默认 endTime）")

    pagination = job.get("pagination") or {}
    page_type = str(pagination.get("type") or "none").lower()
    if page_type not in ALLOWED_PAGINATION_TYPES:
        raise SystemExit(f"pagination.type 不支持：{page_type}（可用 none/page/cursor）")
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
    pt = str(getattr(args, "pt", "") or target.get("pt") or bizdate.strftime("%Y%m%d"))
    if not _PT_RE.match(pt):
        raise SystemExit(f"pt 值不合法（应为字母数字下划线中划线）：{pt!r}，可用 --pt 覆盖")
    return project, table, column, pt


# =============================================================================
# MaxCompute 凭证 profile
# =============================================================================

def get_mc_profile_meta(config: dict, job: dict, args) -> dict:
    """取作业使用的 MaxCompute profile 元信息（project/endpoint/ak/sk）。

    查找顺序：作业文件的 profiles.<名> / maxcompute → --config 文件的 profiles.<名> / maxcompute。
    （作业内优先，方便一份作业自带全部凭证。）
    """
    name = str(getattr(args, "mc_profile", "") or (job.get("target") or {}).get("profile") or "default").strip()
    available: list[str] = []

    for source in (job, config):
        profiles = source.get("profiles") or {}
        available += [f"{key}" for key in profiles if key not in available]
        if name in profiles:
            return dict(profiles[name])
        if name == "default" and source.get("maxcompute"):
            return dict(source["maxcompute"])
    if name == "default":
        return {}
    raise SystemExit(f"找不到 MaxCompute profile「{name}」；已配置：{available or ['（无）']}")


def build_context_doc() -> str:
    """给 --help / --check 用的占位符说明。"""
    return "${secrets.xxx} / ${bizdate} / ${bizdate_iso} / ${today} / ${today_iso}"
