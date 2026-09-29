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
import hashlib
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import VERSION
from .config import (
    build_context_doc,
    check_block_types,
    collect_warnings,
    get_mc_profile_meta,
    load_json_file,
    normalize_job,
    render_job,
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
    parser.add_argument("--init", action="store_true", help="交互式生成作业配置（生成后自己填密钥，再 --check）")
    parser.add_argument("--init-out", default="", help="--init 的输出路径（默认 jobs/<作业名>.json）")
    parser.add_argument("--config", default="", help=f"可选的共享凭证文件（默认 {DEFAULT_CONFIG_PATH}，没有就不读）")
    parser.add_argument("--check", action="store_true", help="只体检：配置 + API 连通 + 目标表结构")
    parser.add_argument("--bizdate", default="", help="业务日期 yyyyMMdd 或 yyyy-MM-dd（默认时区昨天）")
    parser.add_argument("--days", type=int, default=None, help="回拉天数（含基准日），覆盖作业配置 window.days")
    parser.add_argument("--dates", default="", help="逗号分隔的日期列表（补零散几天），指定后忽略 --bizdate/--days")
    parser.add_argument("--start-date", default="", help="补数起始日期（含），与 --end-date 成对使用")
    parser.add_argument("--end-date", default="", help="补数结束日期（含），与 --start-date 成对使用")
    parser.add_argument(
        "--pt",
        default="",
        help="覆盖分区值；不指定时 target.pt 必须是 8 位业务日 yyyyMMdd，"
        "显式指定时可写特殊分区（如测试用 test_20260921——注意调度与 DWD 只自动读 yyyyMMdd 分区）",
    )
    parser.add_argument("--workers", type=int, default=1, help="并按天/按区间并发拉取，默认 1；回补历史可用 2~4")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写数仓")
    parser.add_argument("--allow-empty", action="store_true", help="本次 0 行时也清空并写空分区（默认拒绝）")
    parser.add_argument(
        "--keep-spool", action="store_true", help="失败时保留本次落盘的临时 JSONL（排查/手工重传用；默认失败也清理）"
    )
    parser.add_argument("--endpoint", default="", help="MaxCompute endpoint（覆盖作业里的配置）")
    parser.add_argument("--mc-profile", default="", help="作业 maxcompute/profiles 里的 profile 名（默认 default）")
    parser.add_argument("--cli-profile", default="", help="aliyun CLI profile 名（本机调试凭证兜底，默认 current）")
    parser.add_argument(
        "--sql-timeout",
        type=int,
        default=SQL_TIMEOUT_SECONDS,
        help=f"单条 MaxCompute SQL 最长等待秒数，默认 {SQL_TIMEOUT_SECONDS}；0 表示不限制",
    )
    parser.add_argument("--log-file", default="", help="日志同时写一份到该文件（追加，UTF-8）")
    parser.add_argument("--version", action="version", version=f"api2ods {VERSION}")
    return parser


def _as_count(value, fallback: int, field: str) -> int:
    """次数类配置项 → int：写错时直接报错退出（附字段名），不给裸 traceback。

    在进 fetch_all 之前转换：真转到里面再抛，SystemExit 不是 Exception、
    照样会一路传播出去，但错误信息会混在"这一窗失败"的重试日志里，不如这里干净。
    """
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{field} 必须是整数，实际 {value!r}")


def _open_log_file(path_text: str):
    """打开 --log-file 指定的日志文件（追加、UTF-8、父目录自动创建）；没指定返回 None。

    用户给成目录名（--log-file logs）时给一句人话，而不是 IsADirectoryError 的裸 traceback。
    """
    if not path_text:
        return None
    path = Path(path_text)
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

    优先放工具目录下 .run-locks/；工具目录不可写（如 pip 装在只读位置）时退回系统临时目录。
    锁名带路径哈希：jobs/a/api.json 与 jobs/b/api.json 同名不同作业，只按文件名会互相阻塞。
    """
    stem = job_path.stem or "job"
    digest = hashlib.sha1(str(job_path).encode("utf-8")).hexdigest()[:8]
    name = f"{stem}-{digest}"
    candidates = [ROOT / ".run-locks", Path(tempfile.gettempdir()) / "api2ods-locks"]
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            # 探测文件名必须唯一（mkstemp）：原来用所有作业共享的 ".probe"，并发启动时
            # 别的进程先 unlink 会让本进程抛 FileNotFoundError，于是"静默"落到下一个候选
            # 目录——同一作业的两个实例锁在不同路径上，互斥失效
            handle, probe = tempfile.mkstemp(prefix=".probe-", dir=str(base))
            os.close(handle)
            os.unlink(probe)
            return base / f"{name}.lock"
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / f"api2ods-{name}.lock"


def _redact_job(job: dict, text) -> str:
    """作业上下文下的脱敏：先按配置里的密钥值遮（形态规则盖不住的自由文本回显），
    再走形态输助。错误只在真出错时走这里，每次重收密钥值的开销可忽略。"""
    return redact_secrets(collect_secret_values(job), str(text))


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
        f"  作业      : {job.get('job') or '(未命名)'}"
        + (f" —— {job['description']}" if job.get("description") else ""),
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
        log(f"  ✅ {label} 请求成功，拿到 {count:,} 条记录")
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
    pagination = job.get("pagination") or {}

    # 构造 Fetcher 会读 signers.py（自定义签名）：文件缺失/写错时抛 SystemExit，
    # 不要让它变成裸 traceback（--check 走的是同一条路，所以那边一直是干净的）
    try:
        fetcher = Fetcher(job, job_dir)
    except SystemExit as exc:
        log(f"❌ {_redact_job(job, exc)}")
        return 1
    unit_count = fetcher.unit_count(days)
    log(
        f"{job.get('job') or '作业'} 启动：{days[0]} ~ {days[-1]}（{len(days)} 天，{unit_count} 次请求计划），"
        f"目标 {project}.{table_name} pt={pt}"
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
    keep_spool = bool(args.keep_spool)  # --keep-spool 是用户明确要求：任何失败分支都要生效
    try:
        stats, failures = fetcher.fetch_all(
            days,
            workers=max(1, args.workers),
            window_retries=_as_count(pagination.get("window_retries"), 2, "pagination.window_retries"),
            on_records=spool.write_records,
        )

        # ① 拉取阶段：任一单元失败 → 放弃写库（旧分区保持原样，重跑即可）
        if failures:
            keep_spool = args.keep_spool
            log("")
            log("以下请求失败，本次不写库（避免分区缺数，重跑即可）：")
            for label, err in failures:
                # fetch_all 已脱敏，这里再走一遍兜底：除 fetch 之外的失败源也要进同一道口
                log(f"  - {label}: {_redact_job(job, err)}")
            return 1

        log(
            f"拉取完成：{spool.count:,} 条记录，约 {spool.bytes / 1024 / 1024:.2f} MB，"
            f"耗时 {(time.time() - started) / 60:.1f} 分钟"
        )

        if args.dry_run:
            log(f"--dry-run：不写库。将写入 {project}.{table_name} pt={pt}（{spool.count:,} 行）")
            return 0

        # ② 0 行保护：默认不写空分区（避免接口异常时把已有数据清掉）
        allow_empty = bool(args.allow_empty) or as_bool(
            target_cfg.get("allow_empty"), default=False, field="target.allow_empty"
        )
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
                # 建表当天数据就被生命周期回收；浮点静默截断、负数原样写进 DDL 同理
                if isinstance(raw_lifecycle, bool) or not isinstance(raw_lifecycle, (int, float)):
                    raise SystemExit(f"target.lifecycle_days 必须是正整数（天），实际 {raw_lifecycle!r}")
                if float(raw_lifecycle) != int(raw_lifecycle) or int(raw_lifecycle) <= 0:
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
                table,
                table_name,
                pt,
                lambda: spool.iter_batches(WRITE_BATCH_SIZE, MAX_BATCH_BYTES),
                total=spool.count,
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
        spool.close(keep=keep_spool)


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
            return input()

        try:
            # echo 也走 log()：带时间戳、写完即 flush（原来用 print，提示语会被输入缓冲
            # 压住、看着像卡死），且切不到 UTF-8 的控制台会降级成可替换字符而不是崩掉
            return run_init(args.init_out, ask=_wizard_ask, echo=log)
        finally:
            _detach_log_sink(log_handle)

    if not args.job:
        log("请用 --job 指定作业配置文件（第一次接新源可以先用 `api2ods --init` 生成）")
        # 提前返回同样要摘掉日志 sink：不然同进程再次调用 main 时，日志会继续写进上一轮的文件
        _detach_log_sink(log_handle)
        return 2

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
        if args.bizdate:
            bizdate = parse_day_arg(args.bizdate)
        else:
            from_env = env_bizdate(strict=not args.check)
            bizdate = from_env if from_env is not None else datetime.now(tz).date() - timedelta(days=1)

        job = render_job(job_raw, config, bizdate)  # 替换 ${secrets.x}/${bizdate} 等占位符
        job = normalize_job(job)  # 补齐默认值（翻页方式/参数名等），让配置尽量短
        validate_job(job)
        for warning in collect_warnings(job):  # 未知字段告警（拼写错误提示）
            log(f"⚠️ {warning}")

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
        # 这里统一按运行期错误的格式记一笔，并且同样过脱敏
        log(f"❌ {redact(str(exc))}")
        return 1
    finally:
        _detach_log_sink(log_handle)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
