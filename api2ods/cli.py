# -*- coding: utf-8 -*-
"""命令行入口：体检（--check）、试跑（--dry-run）、正式同步、交互式建配置（--init）。

一次运行的完整流程（run_sync）：
    1) 解析参数 → 计算要拉哪几天（resolve_days）
    2) 拉取：Fetcher 并发跑所有请求单元，记录边拉边写本地 Spool 临时文件（不占内存）
    3) 拉取有任何失败 → 不写库直接退出（防止分区缺数；重跑即可）
    4) 写库：自动建表 → 删分区 → Tunnel 分批写入 → count(*) 核对行数
    5) 全程日志带时间戳，密钥/签名/token 已脱敏
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import VERSION, fieldwatch
from .config import (
    build_context_doc,
    check_block_types,
    collect_warnings,
    get_mc_profile_meta,
    load_json_file,
    normalize_job,
    render_job,
    resolve_notify,
    resolve_target,
    validate_job,
)
from .dates import date_tz_of, env_bizdate, parse_day_arg, resolve_days
from .fetch import Fetcher
from .mc import (
    MAX_BATCH_BYTES,
    PARTITION_COLUMN,
    SQL_TIMEOUT_SECONDS,
    WRITE_BATCH_SIZE,
    connect_odps,
    count_partition,
    ensure_target_table,
    verify_target_schema,
    write_partition,
)
from .notify import notify
from .spool import SpoolWriter, dump_record
from .utils import (
    FatalApiError,
    RunLock,
    as_bool,
    collect_secret_values,
    log,
    redact,
    redact_secrets,
    reset_log_once,
    setup_console,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.json"


def record_to_json(record) -> str:
    """一条记录 → 单行 JSON（落盘与写入用同一个序列化出口）。"""
    return dump_record(record)


def _sql_timeout_arg(value: str) -> int:
    """--sql-timeout 参数校验：非负整数。

    负数在 run_sql_with_timeout 里会被当成"0=不限制"，与用户直觉相反（想调小却等成无限）；
    与 sftp2ods 的口径一致——负数属于命令行参数问题，在 argparse 阶段就报错（退出码 2，
    一个请求都不发起），而不是把未定义的值原样交给 pyodps。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"必须是整数（0 表示不限制），实际 {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"不能为负（0 表示不限制），实际 {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义（help 文案就是用户文档的第一入口，改参数时同步改 README）。"""
    parser = argparse.ArgumentParser(
        prog="api2ods",
        description="通用 REST API → MaxCompute ODS（裸 json 列 + pt 分区，先删再填）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  api2ods --init                                 # 交互式生成一份作业配置（新手推荐）\n"
            "  api2ods --job jobs/demo.json --check           # 体检：配置 + API 连通 + 目标表\n"
            "\n"
            "  # 试跑：先拉 1 天看条数对不对，不写库\n"
            "  api2ods --job jobs/demo.json --bizdate 20260918 --days 1 --dry-run\n"
            "\n"
            "  # 正式同步：天数取配置里的 window.days；--days 可覆盖（如回拉最近 30 天）\n"
            "  api2ods --job jobs/demo.json --bizdate ${bizdate}\n"
            "  api2ods --job jobs/demo.json --bizdate ${bizdate} --days 30\n"
            "\n"
            "  # 补数：必须跟着 --bizdate，整段数据写进 pt=<bizdate>\n"
            "  api2ods --job jobs/demo.json --bizdate 20260920 --dates 2026-09-01,2026-09-05\n"
            "  api2ods --job jobs/demo.json --bizdate 20260920 --start-date 2026-07-01"
            " --end-date 2026-09-20\n"
            "\n"
            f"占位符：{build_context_doc()}\n"
        ),
    )
    parser.add_argument("--job", default="", help="作业配置文件（jobs/*.json）")
    parser.add_argument("--init", action="store_true", help="交互式生成作业配置（密钥类输入不回显，生成后再 --check）")
    parser.add_argument("--init-out", default="", help="--init 的输出路径（默认 jobs/<作业名>.json）")
    parser.add_argument("--config", default="", help=f"可选的共享凭证文件（默认 {DEFAULT_CONFIG_PATH}，没有就不读）")
    parser.add_argument("--check", action="store_true", help="只体检：配置 + API 连通 + 目标表结构")
    parser.add_argument(
        "--bizdate",
        default=None,
        # 默认必须是 None 而不是 ""：空串无法区分"没传 --bizdate"与"显式传了空值"
        # （调度脚本 `--bizdate "$pt"` 且 $pt 未定义）——后者要报错，不能静默回退昨天
        help="业务日期 yyyyMMdd 或 yyyy-MM-dd（默认时区昨天）",
    )
    parser.add_argument("--days", type=int, default=None, help="回拉天数（含基准日），覆盖作业配置 window.days")
    parser.add_argument(
        "--dates",
        default=None,
        help="逗号分隔的日期列表（补零散几天）：指定后 --days 不生效，写入哪个分区仍由 --bizdate/--pt 决定",
    )
    parser.add_argument("--start-date", default="", help="补数起始日期（含），与 --end-date 成对使用")
    parser.add_argument("--end-date", default="", help="补数结束日期（含），与 --start-date 成对使用")
    parser.add_argument(
        "--pt",
        default="",
        help="覆盖分区值；不指定时 target.pt 必须是 8 位业务日 yyyyMMdd，"
        "显式指定时可写特殊分区（如测试用 test_20260921——注意调度与 DWD 只自动读 yyyyMMdd 分区）",
    )
    parser.add_argument("--workers", type=int, default=1, help="并发按天/按区间拉取，默认 1；回补历史可用 2~4")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写数仓")
    parser.add_argument("--allow-empty", action="store_true", help="本次 0 行时也清空并写空分区（默认拒绝）")
    parser.add_argument(
        "--keep-spool",
        action="store_true",
        help="保留本次落盘的临时 JSONL（排查/手工重传用，成功失败都保留）；不加时运行结束自动清理",
    )
    parser.add_argument("--endpoint", default="", help="MaxCompute endpoint（覆盖作业里的配置）")
    parser.add_argument("--mc-profile", default="", help="作业 maxcompute/profiles 里的 profile 名（默认 default）")
    parser.add_argument("--cli-profile", default="", help="aliyun CLI profile 名（本机调试凭证兜底，默认 current）")
    parser.add_argument(
        "--sql-timeout",
        type=_sql_timeout_arg,
        default=SQL_TIMEOUT_SECONDS,
        help=f"单条 MaxCompute SQL 最长等待秒数，默认 {SQL_TIMEOUT_SECONDS}；0 表示不限制，负数在参数解析阶段报错退出码 2",
    )
    parser.add_argument("--log-file", default="", help="日志同时写一份到该文件（追加，UTF-8）")
    parser.add_argument("--no-notify", action="store_true", help="不发任何飞书通知（如新增字段提醒）")
    parser.add_argument("--version", action="version", version=f"api2ods {VERSION}")
    return parser


def _as_count(value, fallback: int, field: str) -> int:
    """次数类配置项 → int：写错时直接报错退出（附字段名），不给裸 traceback。

    在进 fetch_all 之前转换：真转到里面再抛，SystemExit 不是 Exception、
    照样会一路传播出去，但错误信息会混在"这一窗失败"的重试日志里，不如这里干净。
    """
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        # int(True) == 1：window_retries: true 曾被静默当成 1，与实际预期不符
        raise SystemExit(f"{field} 必须是整数，实际 {value!r}")
    if isinstance(value, int):
        number = value
    else:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise SystemExit(f"{field} 必须是整数，实际 {value!r}")
        # NaN/Infinity 与小数都要拦：原来是 int(value) 静默截断（2.9 → 2）
        if not math.isfinite(number) or number != int(number):
            raise SystemExit(f"{field} 必须是整数，实际 {value!r}")
    if number < 0:
        # 负数会被 max(1, ...) 吞成"不重试"，与 timeout/retry_delay 的校验口径对齐
        raise SystemExit(f"{field} 不能为负数，实际 {value!r}")
    return int(number)


def _open_log_file(path_text: str):
    """打开 --log-file 指定的日志文件（追加、UTF-8、父目录自动创建）；没指定返回 None。

    用户给成目录名（--log-file logs）时给一句人话，而不是 IsADirectoryError 的裸 traceback。
    """
    if not path_text:
        return None
    # 展开 ~：调度平台把参数写成 "~/logs/x.log"（带引号时 shell 不展开）时，
    # 字面量 "~" 会在 CWD 下建目录、日志落错位置（排障时以为进程没跑）
    path = Path(path_text).expanduser()
    if path.is_dir():
        raise SystemExit(f"--log-file 指向的是目录，需要给文件名：{path}（如 {path / 'run.log'}）")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"--log-file 打不开：{path}（{exc}）") from exc


def _cred_source_label(config_path: Path, job_path: Path) -> str:
    """凭证来源说明（只写位置，不含密钥）。"""
    if config_path.is_file():
        return f"{job_path.name} / {config_path.name}"
    return job_path.name


def _detach_log_sink(handle) -> None:
    """摘掉日志文件并关闭（同一进程里 main 可能被调用多次，残留的 handle 会写坏日志）。"""
    if handle is None:
        return
    from .utils import remove_log_sink

    remove_log_sink(handle)


def _lock_path(job_path: Path) -> Path:
    """每个作业一把运行锁（不同作业可并行，同一作业不会重复跑）。

    锁目录优先级：环境变量 API2ODS_LOCK_DIR > 工具目录下 .run-locks/ > 系统临时目录
    （工具目录不可写，如 pip 装在只读位置时）。用 API2ODS_LOCK_DIR 可以把锁钉在与运行者
    身份/环境无关的同一目录上——否则"root 能写工具目录、普通用户退回 TMPDIR"这类差异会让
    同一作业的两个实例锁在不同文件上，互斥静默失效；指到共享存储（如 NFS）时多机也能互斥
    （文件系统不支持锁会告警并降级）。
    锁名带路径哈希：jobs/a/api.json 与 jobs/b/api.json 同名不同作业，只按文件名会互相阻塞。
    """
    stem = job_path.stem or "job"
    # sha256 截 16 位十六进制：sha1 只取 8 位（32 位）时不同作业有可观的碰撞概率，
    # 撞了会互相阻塞（解锁时还可能删错对方的锁）；与 sftp2ods 的锁同口径。
    # 摘要先 resolve（绝对化/展开 ~/消解 .. 与软链接）：同一作业用相对/绝对路径两种写法
    # 原来会落到两把锁上、互斥静默失效（两个进程同删同写一个分区）
    digest = hashlib.sha256(os.fsencode(str(Path(job_path).expanduser().resolve()).encode("utf-8"))).hexdigest()[:16]
    name = f"{stem}-{digest}"
    override = os.environ.get("API2ODS_LOCK_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 显式指定的目录不可用要立刻失败：静默换目录等于互斥失效，正是这个开关要防的事
            raise SystemExit(f"API2ODS_LOCK_DIR 指定的锁目录不可用（{exc}）：{base}") from exc
        return base / f"{name}.lock"
    candidates = [ROOT / ".run-locks", Path(tempfile.gettempdir()) / "api2ods-locks"]
    for index, base in enumerate(candidates):
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        try:
            # 探测文件名必须唯一（mkstemp）：原来用所有作业共享的 ".probe"，并发启动时
            # 别的进程先 unlink 会让本进程抛 FileNotFoundError，于是"静默"落到下一个候选
            # 目录——同一作业的两个实例锁在不同路径上，互斥失效
            handle, probe = tempfile.mkstemp(prefix=".probe-", dir=str(base))
            os.close(handle)
            os.unlink(probe)
        except OSError as exc:
            # 目录已经建出来：探测失败（EMFILE/ENOSPC 等）不能再换一把锁路径，
            # 否则两个实例各拿各的锁；至少留一条日志，并仍用这个目录
            log(f"  警告：运行锁目录 {base} 探测写入失败（{exc}），仍使用该目录，避免互斥落到另一路径")
        if index > 0:
            # 退回目录按用户/环境解析（TMPDIR、macOS /var/folders、systemd PrivateTmp）：
            # 不同身份/环境跑同一作业可能拿到不同目录、互斥静默失效——至少把事实说出来
            log(
                f"  提示：工具目录不可写，运行锁放在 {base}；"
                f"若存在多用户/多环境混跑，请用 API2ODS_LOCK_DIR 固定同一锁目录"
            )
        return base / f"{name}.lock"
    fallback = Path(tempfile.gettempdir()) / f"api2ods-{name}.lock"
    log(
        f"  警告：工具目录与系统临时目录都不可写，运行锁临时退回 {fallback}；"
        f"请用 API2ODS_LOCK_DIR 指定一个可写的固定锁目录，否则并发保护可能失效"
    )
    return fallback


def _redact_job(job: dict, text) -> str:
    """作业上下文下的脱敏：先按配置里的密钥值遮（形态规则盖不住的自由文本回显），
    再走形态兜底。错误只在真出错时走这里，每次重收密钥值的开销可忽略。"""
    return redact_secrets(collect_secret_values(job), str(text))


def _notifier(job: dict, args):
    """构造飞书通知函数：读合并后的 job.notify（见 main 的 resolve_notify），--no-notify 关闭。

    告警发送失败不影响退出码（notify 模块只记日志）；未配 webhook 时静默跳过。
    """
    cfg = job.get("notify") or {}
    webhook = str(cfg.get("webhook") or "")
    enabled = as_bool(cfg.get("enabled"), default=True, field="notify.enabled") and not args.no_notify
    return lambda title, lines, footer="": notify(webhook, title, lines, footer, enabled=enabled)


def _job_summary(job: dict) -> list[str]:
    """体检时打印的作业概要（让用户一眼确认配置理解正确）。"""
    request = job.get("request") or {}
    window = job.get("window") or {}
    pagination = job.get("pagination") or {}
    parse = job.get("parse") or {}
    endpoint = (
        f"{str(request.get('method') or 'GET').upper()} "
        f"{str(request.get('base_url') or '').rstrip('/')}{request.get('path') or ''}"
    )
    window_text = (
        (
            f"{window.get('mode', 'per_day')} × {window.get('days', 1)} 天，"
            f"date_tz={window.get('date_tz') or 'Asia/Shanghai'}"
        )
        if window
        else "无（单次请求）"
    )
    response_text = f"{request.get('response_type') or 'json'}" + (
        f" / records_path={request.get('records_path')}"
        if (request.get("response_type") or "json") == "json"
        else f" / parse={parse.get('format')}{'+zip' if parse.get('unzip') else ''}"
    )
    return [
        # 作业名/description 是自由文本（description 常粘联调样例，可能带 ak/sk 明文）：
        # 与下一行的 endpoint 同口径过 _redact_job（本项目的 log() 不做全局脱敏）
        _redact_job(
            job,
            f"  作业      : {job.get('job') or '(未命名)'}"
            + (f" —— {job['description']}" if job.get("description") else ""),
        ),
        # path 允许带查询串（--init 就是这么把签名类 URL 存下来的），
        # 概要会落进 --check 日志和 --log-file，必须过一遍脱敏
        "  接口      : " + _redact_job(job, endpoint),
        f"  响应      : {response_text}",
        f"  窗口      : {window_text}",
        f"  分页      : {pagination.get('type') or 'none'}"
        + (f"（page_size={pagination.get('page_size')}）" if pagination.get("type") == "page" else ""),
        f"  鉴权      : {(request.get('auth') or {}).get('type') or 'none'}",
    ]


def run_check(job: dict, config: dict, config_path: Path, args, bizdate, job_path: Path) -> int:
    """体检：配置概要 + API 真实请求一页 + 目标表结构。新接一个源时先跑这个。"""
    job_dir = job_path.parent
    project, table_name, column, pt = resolve_target(job, config, args, bizdate)

    log("== 作业概要 ==")
    for line in _job_summary(job):
        log(line)
    log(f"  目标      : {project}.{table_name}（列 {column} + 分区 {PARTITION_COLUMN}，pt={pt}）")

    log("")
    log("== API 连通性（真实请求） ==")
    try:
        fetcher = Fetcher(job, job_dir)
        label, count = fetcher.probe([bizdate])
        # label 里可能带 URL/签名（与下面失败分支、run_sync 的失败列表同口径脱敏）
        log(f"  ✅ {_redact_job(job, label)} 请求成功，拿到 {count:,} 条记录")
    except Exception as exc:  # noqa: BLE001
        # 统一走 redact：接口的错误体常回显 token/签名（fail_if 的 message_path
        # 往往是整段错误消息），run_sync 的同类分支一直是脱敏的，这里不能搞两套标准
        log(f"  ❌ 请求失败：{_redact_job(job, exc)}")
        return 1

    log("")
    log("== MaxCompute 目标表 ==")
    try:
        profile = get_mc_profile_meta(config, job, args)
        o = connect_odps(
            config,
            _cred_source_label(config_path, job_path),
            profile,
            project,
            endpoint=str(args.endpoint or ""),
            cli_profile=args.cli_profile,
        )
        if not o.exist_table(table_name):
            log(f"  ⭕ 表不存在（运行同步时自动创建）：{project}.{table_name}")
        else:
            table = o.get_table(table_name)
            verify_target_schema(table, table_name, column)
            has_pt = table.exist_partition(f"{PARTITION_COLUMN}={pt}")
            log(f"  ✅ {table_name} 结构符合；pt={pt} 分区{'已存在' if has_pt else '不存在（运行时创建）'}")
    except SystemExit as exc:
        log(f"  ❌ {_redact_job(job, exc)}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"  ❌ 连接/校验失败：{_redact_job(job, exc)}")
        return 1

    log("")
    log("检查通过。")
    return 0


def run_sync(job: dict, config: dict, config_path: Path, args, bizdate, job_path: Path) -> int:
    """正式同步：拉取（落盘 Spool）→ 建表 → 先删再填 pt 分区 → 行数校验。"""
    job_dir = job_path.parent
    project, table_name, column, pt = resolve_target(job, config, args, bizdate)
    target_cfg = job.get("target") or {}
    days = resolve_days(args, job, bizdate)
    if not days:
        # 后面日志/通知会取 days[0]/days[-1]；空列表是裸 IndexError，调度侧看不到原因
        log("❌ 没有可执行的日期（days 为空），请检查 --days/--dates/--start-date 等参数与 window 配置")
        return 1
    pagination = job.get("pagination") or {}
    job_name = job.get("job") or job_path.stem
    notifier = _notifier(job, args)
    footer = _redact_job(job, f"作业 {job_name} · 目标 {project}.{table_name} · pt={pt}")

    # 构造 Fetcher 会读 signers.py（自定义签名）：文件缺失/写错时抛 SystemExit，
    # 不要让它变成裸 traceback（--check 走的是同一条路，所以那边一直是干净的）
    try:
        fetcher = Fetcher(job, job_dir)
    except SystemExit as exc:
        log(f"❌ {_redact_job(job, exc)}")
        return 1
    unit_count = fetcher.unit_count(days)
    log(
        _redact_job(
            job,
            f"{job.get('job') or '作业'} 启动：{days[0]} ~ {days[-1]}（{len(days)} 天，{unit_count} 次请求计划），"
            f"目标 {project}.{table_name} pt={pt}",
        )
    )
    if len(days) > 30:
        log("提示：回补天数较多，耗时较长（受接口限速影响）；可 Ctrl+C 中断后重跑（先删再填，重复跑幂等）。")

    started = time.time()
    try:
        spool = SpoolWriter()  # 记录边拉边落盘（大数据量不占内存）
    except OSError as exc:
        # 临时目录不可写/磁盘满：给一句人话，而不是裸 traceback（否则 --log-file 里一个字都没有）
        log(f"❌ 无法创建落盘临时文件（检查系统临时目录是否可写/磁盘是否已满）：{exc}")
        return 1
    # --keep-spool 是用户明确要求：本次运行内是常量。下面各失败分支里的重复赋值只是显式标注
    # （便于逐条分支阅读），真正生效的是 finally 里的 close(keep=...)——成败统一由它处理。
    keep_spool = bool(args.keep_spool)
    observer = fieldwatch.FieldObserver()  # 字段漂移检测：逐批并集，内存只跟字段数有关

    def _on_records(records):
        # workers>1 时本回调由多个工作线程并发执行：SpoolWriter.write_records 与
        # FieldObserver.update 内部都自带锁（各自的类注释有说明），这里直接调用即可
        observer.update(records)
        spool.write_records(records)

    try:
        stats, failures = fetcher.fetch_all(
            days,
            workers=max(1, args.workers),
            window_retries=_as_count(pagination.get("window_retries"), 2, "pagination.window_retries"),
            on_records=_on_records,
        )

        # ① 拉取阶段：任一单元失败 → 放弃写库（旧分区保持原样，重跑即可）
        if failures:
            keep_spool = args.keep_spool
            log("")
            log("以下请求失败，本次不写库（避免分区缺数，重跑即可）：")
            for label, err in failures:
                # fetch_all 已脱敏，这里再走一遍兜底：除 fetch 之外的失败源也要进同一道口。
                # label 里可能带 URL/签名（如带查询串的接口地址），与同一行的 err 同口径脱敏
                log(f"  - {_redact_job(job, label)}: {_redact_job(job, err)}")
            return 1

        log(
            f"拉取完成：{spool.count:,} 条记录（{len(stats)} 个请求单元），约 {spool.bytes / 1024 / 1024:.2f} MB，"
            f"耗时 {(time.time() - started) / 60:.1f} 分钟"
        )

        # 字段漂移检测：与上次成功运行的快照对比，出现新字段只提醒、不阻塞（json 列原样落库）。
        # 失败单元已提前 return（此时并集可能不完整）；--dry-run 也提醒，但不落盘快照。
        snapshot = fieldwatch.load_snapshot(job_path)
        new_fields = sorted(observer.fields - snapshot) if snapshot is not None else []
        if new_fields:
            # 字段名来自接口键名（外部、数量不受控）：展示截断，避免日志行/飞书卡片超长被拒
            # （完整清单不影响入库，json 列原样保留）
            limit = 50
            shown = "、".join(f"`{name}`" for name in new_fields[:limit])
            if len(new_fields) > limit:
                shown += f"…（共 {len(new_fields)} 个，仅显示前 {limit} 个）"
            log(
                f"⚠️ 接口记录出现 {len(new_fields)} 个新增字段：{shown}"
                + (
                    "（已照常入库，如需使用请更新 DWD 提取口径）"
                    if not args.dry_run
                    else "（本次为 --dry-run，未写库；正式运行会自动入库）"
                )
            )
            notifier(
                # 标题同样过 _redact_job：job 名是自由文本（可能含联调样例的 ak/sk），
                # 卡片会发到外部 webhook，泄露面比日志更大
                _redact_job(job, f"{job_name}：接口出现新增字段"),
                [
                    f"**新增字段**：{shown}（共 {len(new_fields)} 个）",
                    f"**本次窗口**：{days[0]} ~ {days[-1]}",
                    # dry-run 未写库：不能照抄"数据已写入"的结论，否则下游按卡片以为分区已就绪
                    (
                        "数据已照常写入 ODS（json 原样保留、新字段自动包含，无需改表）。请人工确认："
                        if not args.dry_run
                        else "（试跑）本次未写库；正式运行时新字段会自动入库（json 原样保留、无需改表）。请人工确认："
                    ),
                    "① 需要的话更新 DWD 的 `get_json_object` 提取逻辑；",
                    "② 若字段来自接口异常/拼写变化，核对上游或调整作业配置。",
                ],
                footer,
            )

        allow_empty = bool(args.allow_empty) or as_bool(
            target_cfg.get("allow_empty"), default=False, field="target.allow_empty"
        )
        if args.dry_run:
            log(f"--dry-run：不写库。将写入 {project}.{table_name} pt={pt}（{spool.count:,} 行）")
            if not spool.count and not allow_empty:
                # dry-run 常被当调度/CI 预检（epilog 就这么推荐）：0 行时静默返回 0 会让
                # 坏配置一路"正常"到正式跑才暴露；退出码语义与 main 文档承诺对齐
                log("❌ 本次拉取 0 行（--dry-run 不写库；正式运行会因 0 行保护退出非 0）")
                return 1
            return 0

        # ② 0 行保护：默认不写空分区（避免接口异常时把已有数据清掉）
        if not spool.count and not allow_empty:
            keep_spool = args.keep_spool
            log(
                f"❌ 本次拉取 0 行，为避免清空 pt={pt} 分区，未写库。"
                f"确认要写空分区时加 --allow-empty（或配置 target.allow_empty=true）"
            )
            return 1

        # ③ 写库：自动建表 → 先删再填 → Tunnel 写入 → 行数与 count(*) 双重校验
        try:
            lifecycle_days = None
            raw_lifecycle = target_cfg.get("lifecycle_days")
            if raw_lifecycle is not None and raw_lifecycle != "":
                # 布尔要单独挡：JSON 里写 true 时 int(True) == 1，新表会拿到 lifecycle 1，
                # 建表当天数据就被生命周期回收；NaN/Infinity（json.load 默认接受）会让
                # float(raw) != int(raw) 直接抛 ValueError（裸 traceback），一并挡掉
                if (
                    isinstance(raw_lifecycle, bool)
                    or not isinstance(raw_lifecycle, (int, float))
                    or (
                        isinstance(raw_lifecycle, float)
                        and (not math.isfinite(raw_lifecycle) or not raw_lifecycle.is_integer())
                    )
                    or raw_lifecycle <= 0
                ):
                    raise SystemExit(f"target.lifecycle_days 必须是正整数（天），实际 {raw_lifecycle!r}")
                lifecycle_days = int(raw_lifecycle)
            profile = get_mc_profile_meta(config, job, args)
            o = connect_odps(
                config,
                _cred_source_label(config_path, job_path),
                profile,
                project,
                endpoint=str(args.endpoint or ""),
                cli_profile=args.cli_profile,
            )
            table = ensure_target_table(
                o,
                project,
                table_name,
                column,
                str(target_cfg.get("comment") or ""),
                stored_as=str(target_cfg.get("stored_as") or ""),
                lifecycle_days=lifecycle_days,
                timeout=args.sql_timeout,
            )
            log(f"表就绪：{project}.{table_name}（{column} + {PARTITION_COLUMN}）")

            started_write = time.time()
            write_partition(
                o,
                table,
                project,
                table_name,
                pt,
                lambda: spool.iter_batches(WRITE_BATCH_SIZE, MAX_BATCH_BYTES),
                total=spool.count,
                timeout=args.sql_timeout,
                # 写库重试的报错也要按配置里的密钥值遮一道（Tunnel/SQL 报错里可能带签名 URL）
                secrets=collect_secret_values(job),
            )
            log(f"已写入 pt={pt}：{spool.count:,} 行，耗时 {(time.time() - started_write) / 60:.1f} 分钟")

            actual = count_partition(o, project, table_name, pt, timeout=args.sql_timeout)
            if actual != spool.count:
                keep_spool = args.keep_spool
                log(f"❌ 写后校验不一致：期望 {spool.count:,} 行，实际 {actual:,} 行（pt={pt}）")
                return 1
        except SystemExit:
            # 结构校验之类的配置错也走这里：--keep-spool 是用户明确要求，任何失败分支都要生效
            keep_spool = args.keep_spool
            raise
        except Exception as exc:  # noqa: BLE001 - SQL/Tunnel 失败统一按失败退出（调度可告警）
            keep_spool = args.keep_spool
            log(f"❌ 写库失败：{_redact_job(job, exc)}")
            return 1

        # 写库成功后才更新字段快照（与"台账只在写成功后记账"一致；失败/中断不更新，下次重报）。
        # 快照只是"少提醒一次"的辅助文件：写不进去（目录只读/磁盘满）不能把已经校验通过的
        # 写库结果报成失败——最坏是下次运行再提醒一次新字段
        try:
            fieldwatch.save_snapshot(job_path, observer.fields, job_name)
        except OSError as exc:
            log(f"  警告：字段快照未更新（{exc}），不影响本次入库；下次运行会重复提醒新增字段")

        log(f"校验通过：pt={pt} 共 {actual:,} 行")
        log(f"全部完成（总耗时 {(time.time() - started) / 60:.1f} 分钟）。")
        return 0
    except FatalApiError as exc:
        # 4xx（密钥错/参数错/没权限）：不写库、不打整窗重试，直接把接口给的原因打出来。
        # 这类错误在 fetch_all 里就已经跳过重试了，这里只是把出口做得干净些
        keep_spool = args.keep_spool
        log(f"❌ 接口返回不可重试的错误，本次不写库：{_redact_job(job, exc)}")
        return 1
    except KeyboardInterrupt:
        keep_spool = args.keep_spool
        log("已手动中断（本次未写库；若在写入阶段中断，分区可能不完整，重跑同一命令即可）")
        return 130
    finally:
        if keep_spool:
            log(f"已保留本次数据文件（--keep-spool）：{spool.path}")
        try:
            spool.close(keep=keep_spool)
        except OSError as exc:
            # 清理失败（磁盘满/句柄异常）不能把已经确定的返回值/原始异常顶掉：
            # 调度只看退出码，多一条告警比换个错误码安全
            log(f"  警告：清理落盘临时文件失败（{exc}）；不影响本次运行结果")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回退出码：0 成功 / 1 运行失败 / 2 参数问题 / 130 用户中断。

    调度系统按退出码判断成败，所以"拉取到 0 行""写后行数对不上"这类情况都返回非 0。
    """
    setup_console()
    # 告警去重记录按"每次运行"清空：同一进程里 main 被调用多次（测试、嵌入）时，
    # 上一轮的告警会把这一轮同名的那条吞掉，用户看不到任何提示
    reset_log_once()
    args = build_parser().parse_args(argv)

    # 日志文件要先挂上：--init 的交互问答也值得留痕（原来它 return 在新挂载点之前）
    log_handle = _open_log_file(args.log_file)
    if log_handle is not None:
        from .utils import add_log_sink

        add_log_sink(log_handle)

    if args.init:  # 交互式建配置：不需要 --job
        from .init_wizard import run_init

        def _wizard_ask(prompt: str = "") -> str:
            """向导的提问也走 log：这样 --log-file 里能看到整套问答（原来文件始终是空的）。

            只记问题、不记回答：向导要填 token / AK / SK，记进日志等于把密钥抄一份到磁盘。
            """
            log(prompt)
            try:
                return input()
            except (EOFError, ValueError, RuntimeError) as exc:
                # stdin 关闭/无输入源：统一翻译成 EOFError（run_init 顶层按"取消"处理），
                # 别让裸 traceback 打断向导——与 sftp2ods 的 _wizard_ask/prompt_secret 同口径
                raise EOFError("标准输入不可用") from exc

        def _wizard_ask_secret(prompt: str = "") -> str:
            """密钥类提问：问题同样走 log（留痕），回答走 getpass 不回显。

            不回显是为了密钥不进终端 scrollback，也不被 `script` / 录屏抄走——原来走
            input() 时密钥明文回显在终端上。无 tty 等读不到隐藏输入的场景退回 input()，
            但显式提示"会明文回显"（提示语里写着不回显，不能名不符实）。
            """
            log(prompt)
            try:
                return getpass.getpass("")
            except EOFError:
                # stdin 已关闭：按用户中断处理，走向导的取消出口，而不是冒成裸 traceback
                raise KeyboardInterrupt from None
            except OSError as exc:
                # 只接"读不到隐藏输入"这类预期错误；其余异常上抛，别把真实缺陷掩盖成输入问题
                log(f"  警告：当前环境无法隐藏输入（{exc}），退回明文回显的普通输入")
                try:
                    return input()
                except EOFError:
                    raise KeyboardInterrupt from None

        try:
            # echo 也走 log()：带时间戳、写完即 flush（原来用 print，提示语会被输入缓冲
            # 压住、看着像卡死），且切不到 UTF-8 的控制台会降级成可替换字符而不是崩掉
            return run_init(args.init_out, ask=_wizard_ask, echo=log, ask_secret=_wizard_ask_secret)
        except SystemExit as exc:
            # 向导自己抛的配置错（如 --init-out 指到目录）：这一步在下面那个大 try 之外，
            # 不接住的话消息只落到 stderr、没有时间戳、--log-file 里一个字都没有，
            # 与"准备阶段的配置错也要留痕"的口径不一致（退出码仍是 1）
            log(f"❌ {redact(str(exc))}")
            return 1
        finally:
            _detach_log_sink(log_handle)

    if not args.job:
        log("请用 --job 指定作业配置文件（第一次接新源可以先用 `api2ods --init` 生成）")
        # 提前返回同样要摘掉日志 sink：不然同进程再次调用 main 时，日志会继续写进上一轮的文件
        _detach_log_sink(log_handle)
        return 2

    job_raw: dict = {}  # 供统一异常出口做值级脱敏（读到作业文件后就有内容）
    config: dict = {}  # 同上：--config 里的密钥值（如 maxcompute.secrets）也要参与脱敏
    try:
        # 凭证来源：作业文件自带；--config/默认 config.json 只在存在时作为补充（可选）
        if args.config:
            config_path = Path(args.config)
            config = load_json_file(config_path, "凭证/密钥文件")
        else:
            config_path = DEFAULT_CONFIG_PATH
            config = load_json_file(config_path, "凭证/密钥文件") if config_path.is_file() else {}

        job_path = Path(args.job)
        job_raw = load_json_file(job_path, "作业配置文件")
        # 类型检查提到最前面：下面第一句 date_tz_of 就要取 window.date_tz，
        # 而 window 写成字符串时那是 'str' object has no attribute 'get' 的裸 traceback
        check_block_types(job_raw)

        # 业务日：--bizdate > 环境变量（DataWorks） > 时区昨天
        # 顺序要紧：先看显式 --bizdate，没有才读环境变量。反过来写的话，调度环境变量
        # 写坏时运维连"--bizdate 强制指定业务日重跑"这条自救路都走不了。
        # 环境变量畸形本身必须报错（静默回退昨天会写错分区还显示成功）——
        # 唯一的例外是只读体检 --check：它不写库，脏环境变量不该连"看一眼"都挡掉
        tz = date_tz_of(job_raw)
        if args.bizdate is not None:
            bizdate = parse_day_arg(args.bizdate)
        else:
            from_env = env_bizdate(strict=not args.check)
            bizdate = from_env if from_env is not None else datetime.now(tz).date() - timedelta(days=1)

        # 替换 ${secrets.x}/${bizdate} 等占位符；--config 的 maxcompute/profiles 用返回的副本
        # （render_job 不改写入参，同一进程复用同一份 config 时不会串上一次替换过的密钥）
        job, config = render_job(job_raw, config, bizdate)
        job = normalize_job(job)  # 补齐默认值（翻页方式/参数名等），让配置尽量短
        validate_job(job)
        for warning in collect_warnings(job):  # 未知字段告警（拼写错误提示）
            log(f"⚠️ {_redact_job(job, warning)}")  # job 已渲染：与其它日志同口径值级脱敏

        # notify 合并进 job（作业优先、--config 兜底）：共享 --config 文件里的 webhook
        # 也要被 collect_secret_values 的值级脱敏收集（它按 job 收集），否则 webhook 裸
        # hook id 出现在自由文本报错时会漏遮。合并发生在校验之后，空 dict 无副作用
        job["notify"] = resolve_notify(job, config)

        if args.check:
            try:
                return run_check(job, config, config_path, args, bizdate, job_path.resolve())
            except KeyboardInterrupt:
                # 体检要发真实请求（可能卡在超时里），这一段在所有 try 之外：
                # 不接的话 Ctrl+C 会以裸 traceback 结束，退出码也不是 130
                log("已中断（体检未完成），退出")
                return 130

        def _exit_now(code: int) -> int:
            """立即结束进程，不等在飞的请求。

            不能只用 return / SystemExit：解释器退出前会 join 所有 ThreadPoolExecutor 的工作
            线程（threading._register_atexit），并发拉取时那些在飞请求（最长 180s 超时）会把
            进程拖住最长几分钟——调度侧看到的就是"Ctrl+C 之后赖着不走"，4xx 快速失败也只快在
            函数层面、进程仍要等（实测 rc=1 仍被一个 5s 的在飞请求拖了 6 秒）。
            这里跳过退出钩子直接 os._exit：走到这一步时 run_sync 已经清过临时文件、放过运行锁
            （写库是先删再填、幂等），日志每条都 flush，没有需要收尾的东西。

            返回 code 只是为了让"被测试 mock 掉的 os._exit"还能拿到退出码；
            真实运行时这一行不会返回。
            """
            _detach_log_sink(log_handle)
            # 显式 flush：os._exit 不跑解释器退出流程，没 flush 的 stdout/stderr 缓冲
            # （重定向到文件时是块缓冲）会丢——日志每条自行 flush，但非 log() 的输出未必
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (OSError, ValueError):
                    pass
            os._exit(code)
            return code

        try:
            with RunLock(_lock_path(job_path.resolve())):  # 同机同一作业互斥；不同作业可并行
                rc = run_sync(job, config, config_path, args, bizdate, job_path.resolve())
            if rc == 130:
                # 拉取/写库阶段的 Ctrl+C 走的是 run_sync 的 return 130（它要先清临时文件、
                # 放运行锁），异常不会传到这里；只看异常的话这道保险等于不存在
                return _exit_now(130)
            if rc == 1 and max(1, args.workers) > 1:
                # 失败退出（4xx / 配置错 / 拉取失败）在并发模式下同样有在飞请求要等：
                # 主线程一句 return 1 之后，解释器退出会 join 它们，调度看到的就是"报了错还赖着"
                return _exit_now(1)
            return rc
        except SystemExit as exc:
            # 配置类错误（ConfigError 是 SystemExit 子类）从 run_sync 里冒泡时同样带着
            # 在飞请求：不走 _exit_now 的话，解释器退出阶段仍要 join 它们（实测进程耗时
            # 随在飞请求线性增长）。消息可能是配置片段，log 前统一过脱敏
            log(f"❌ {_redact_job(job, exc)}")
            return _exit_now(1)
        except KeyboardInterrupt:
            # 准备阶段（还没进 run_sync）被 Ctrl+C：没有线程要等，但走同一条出口更省心
            log("已中断（尚未开始运行），退出")
            return _exit_now(130)
    except SystemExit as exc:
        # 准备阶段的配置错（bizdate 畸形、缺 request.base_url、占位符写错、validate_job
        # 的各类报错、运行锁拿不到）原来直接冒泡出 main：退出码虽然是对的，但控制台那句
        # 没有时间戳、--log-file 里一个字都没有，调度侧翻日志文件看不到任何原因。
        # 这里统一按运行期错误的格式记一笔，并且同样过脱敏——按**作业文件 + --config
        # 两份配置**里的密钥值做值级脱敏（只收 job_raw 的话，凭证写在 --config 的
        # maxcompute/secrets 里时该值漏遮），拿不到任何配置时退回形态级
        if job_raw or config:
            secrets = collect_secret_values(job_raw) + collect_secret_values(config)
            log(f"❌ {redact_secrets(secrets, str(exc))}")
        else:
            log(f"❌ {redact(str(exc))}")
        return 1
    finally:
        _detach_log_sink(log_handle)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
