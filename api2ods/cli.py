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
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import VERSION
from .config import (
    build_context_doc,
    collect_warnings,
    get_mc_profile_meta,
    load_json_file,
    render_job,
    resolve_target,
    validate_job,
)
from .dates import date_tz_of, env_bizdate, parse_day_arg, resolve_days
from .fetch import Fetcher
from .mc import (
    PARTITION_COLUMN,
    SQL_TIMEOUT_SECONDS,
    connect_odps,
    count_partition,
    ensure_target_table,
    verify_target_schema,
    write_partition,
)
from .spool import SpoolWriter
from .utils import RunLock, log, setup_console

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.json"


def record_to_json(record) -> str:
    """一条记录 → 单行 JSON（紧凑、中文不转义，键顺序保持 API 返回顺序）。"""
    import json
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api2ods",
        description="通用 REST API → MaxCompute ODS（裸 json 列 + pt 分区，先删再填）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例：\n"
            "  api2ods --init                                  # 交互式生成一份作业配置（新手推荐）\n"
            "  api2ods --job jobs/demo.json --check            # 体检：配置 + API 连通 + 目标表\n"
            "  api2ods --job jobs/demo.json --bizdate 20260918 --days 1 --dry-run\n"
            "  api2ods --job jobs/demo.json --bizdate ${bizdate}          # 正式同步\n"
            "  api2ods --job jobs/demo.json --start-date 2026-07-01 --end-date 2026-09-20  # 补数\n"
            "\n"
            f"占位符：{build_context_doc()}\n"
        ),
    )
    parser.add_argument("--job", default="", help="作业配置文件（jobs/*.json）")
    parser.add_argument("--init", action="store_true",
                        help="交互式生成作业配置（生成后自己填密钥，再 --check）")
    parser.add_argument("--init-out", default="", help="--init 的输出路径（默认 jobs/<作业名>.json）")
    parser.add_argument("--config", default="", help=f"可选的共享凭证文件（默认 {DEFAULT_CONFIG_PATH}，没有就不读）")
    parser.add_argument("--check", action="store_true", help="只体检：配置 + API 连通 + 目标表结构")
    parser.add_argument("--bizdate", default="", help="业务日期 yyyyMMdd 或 yyyy-MM-dd（默认时区昨天）")
    parser.add_argument("--days", type=int, default=None, help="回拉天数（含基准日），覆盖作业配置 window.days")
    parser.add_argument("--dates", default="", help="逗号分隔的日期列表（补零散几天），指定后忽略 --bizdate/--days")
    parser.add_argument("--start-date", default="", help="补数起始日期（含），与 --end-date 成对使用")
    parser.add_argument("--end-date", default="", help="补数结束日期（含），与 --start-date 成对使用")
    parser.add_argument("--pt", default="", help="覆盖分区值（默认取作业 target.pt，即 ${bizdate}）")
    parser.add_argument("--workers", type=int, default=1, help="并按天/按区间并发拉取，默认 1；回补历史可用 2~4")
    parser.add_argument("--dry-run", action="store_true", help="只拉取统计，不写数仓")
    parser.add_argument("--allow-empty", action="store_true", help="本次 0 行时也清空并写空分区（默认拒绝）")
    parser.add_argument("--keep-spool", action="store_true",
                        help="失败时保留本次落盘的临时 JSONL（排查/手工重传用；默认失败也清理）")
    parser.add_argument("--endpoint", default="", help="MaxCompute endpoint（覆盖作业里的配置）")
    parser.add_argument("--mc-profile", default="", help="作业 maxcompute/profiles 里的 profile 名（默认 default）")
    parser.add_argument("--cli-profile", default="", help="aliyun CLI profile 名（本机调试凭证兜底，默认 current）")
    parser.add_argument("--sql-timeout", type=int, default=SQL_TIMEOUT_SECONDS,
                        help=f"单条 MaxCompute SQL 最长等待秒数，默认 {SQL_TIMEOUT_SECONDS}；0 表示不限制")
    parser.add_argument("--log-file", default="", help="日志同时写一份到该文件（追加，UTF-8）")
    parser.add_argument("--version", action="version", version=f"api2ods {VERSION}")
    return parser


def _open_log_file(path_text: str):
    if not path_text:
        return None
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8")


def _cred_source_label(config_path: Path, job_path: Path) -> str:
    """凭证来源说明（只写位置，不含密钥）。"""
    if config_path.is_file():
        return f"{job_path.name} / {config_path.name}"
    return job_path.name


def _lock_path(job_path: Path) -> Path:
    """每个作业一把运行锁（不同作业可并行，同一作业不会重复跑）。

    优先放工具目录下 .run-locks/；工具目录不可写（如 pip 装在只读位置）时退回系统临时目录。
    """
    name = job_path.stem or "job"
    candidates = [ROOT / ".run-locks", Path(tempfile.gettempdir()) / "api2ods-locks"]
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return base / f"{name}.lock"
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / f"api2ods-{name}.lock"


def _job_summary(job: dict) -> list[str]:
    """体检时打印的作业概要（让用户一眼确认配置理解正确）。"""
    request = job.get("request") or {}
    window = job.get("window") or {}
    pagination = job.get("pagination") or {}
    parse = job.get("parse") or {}
    window_text = (f"{window.get('mode', 'per_day')} × {window.get('days', 1)} 天，"
                   f"date_tz={window.get('date_tz') or 'Asia/Shanghai'}") if window else "无（单次请求）"
    response_text = (f"{request.get('response_type') or 'json'}"
                     + (f" / records_path={request.get('records_path')}"
                        if (request.get("response_type") or "json") == "json"
                        else f" / parse={parse.get('format')}{'+zip' if parse.get('unzip') else ''}"))
    return [
        f"  作业      : {job.get('job') or '(未命名)'}"
        + (f" —— {job['description']}" if job.get("description") else ""),
        f"  接口      : {str(request.get('method') or 'GET').upper()} "
        f"{str(request.get('base_url') or '').rstrip('/')}{request.get('path') or ''}",
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
        log(f"  ❌ 请求失败：{exc}")
        return 1

    log("")
    log("== MaxCompute 目标表 ==")
    try:
        profile = get_mc_profile_meta(config, job, args)
        o = connect_odps(config, _cred_source_label(config_path, job_path),
                         profile, project, endpoint=str(args.endpoint or ""),
                         cli_profile=args.cli_profile)
        if not o.exist_table(table_name):
            log(f"  ⭕ 表不存在（运行同步时自动创建）：{project}.{table_name}")
        else:
            table = o.get_table(table_name)
            verify_target_schema(table, table_name, column)
            has_pt = table.exist_partition(f"{PARTITION_COLUMN}={pt}")
            log(f"  ✅ {table_name} 结构符合；pt={pt} 分区{'已存在' if has_pt else '不存在（运行时创建）'}")
    except SystemExit as exc:
        log(f"  ❌ {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"  ❌ 连接/校验失败：{exc}")
        return 1

    log("")
    log("检查通过。")
    return 0


def run_sync(job: dict, config: dict, config_path: Path, args, bizdate, job_path: Path) -> int:
    """正式同步：拉取（落盘 Spool）→ 建表 → 先删再填 pt 分区 → 行数校验。"""
    job_dir = job_path.parent
    project, table_name, column, pt = resolve_target(job, config, args, bizdate)
    target_cfg = job.get("target") or {}
    days = resolve_days(args, job)
    pagination = job.get("pagination") or {}

    fetcher = Fetcher(job, job_dir)
    unit_count = fetcher.unit_count(days)
    log(f"{job.get('job') or '作业'} 启动：{days[0]} ~ {days[-1]}（{len(days)} 天，{unit_count} 次请求计划），"
        f"目标 {project}.{table_name} pt={pt}")
    if len(days) > 30:
        log("提示：回补天数较多，耗时较长（受接口限速影响）；可 Ctrl+C 中断后重跑（先删再填，重复跑幂等）。")

    started = time.time()
    spool = SpoolWriter()          # 记录边拉边落盘（大数据量不占内存）
    keep_spool = False             # 失败且 --keep-spool 时保留临时文件排障
    try:
        stats, failures = fetcher.fetch_all(
            days, workers=max(1, args.workers),
            window_retries=int(pagination.get("window_retries")
                               if pagination.get("window_retries") is not None else 2),
            on_records=spool.write_records,
        )

        # ① 拉取阶段：任一单元失败 → 放弃写库（旧分区保持原样，重跑即可）
        if failures:
            keep_spool = args.keep_spool
            log("")
            log("以下请求失败，本次不写库（避免分区缺数，重跑即可）：")
            for label, err in failures:
                log(f"  - {label}: {err}")
            return 1

        log(f"拉取完成：{spool.count:,} 条记录，约 {spool.bytes / 1024 / 1024:.2f} MB，"
            f"耗时 {(time.time() - started) / 60:.1f} 分钟")

        if args.dry_run:
            log(f"--dry-run：不写库。将写入 {project}.{table_name} pt={pt}（{spool.count:,} 行）")
            return 0

        # ② 0 行保护：默认不写空分区（避免接口异常时把已有数据清掉）
        allow_empty = bool(args.allow_empty or target_cfg.get("allow_empty"))
        if not spool.count and not allow_empty:
            keep_spool = args.keep_spool
            log(f"❌ 本次拉取 0 行，为避免清空 pt={pt} 分区，未写库。"
                f"确认要写空分区时加 --allow-empty（或配置 target.allow_empty=true）")
            return 1

        # ③ 写库：自动建表 → 先删再填 → Tunnel 写入 → 行数与 count(*) 双重校验
        try:
            profile = get_mc_profile_meta(config, job, args)
            o = connect_odps(config, _cred_source_label(config_path, job_path), profile, project,
                             endpoint=str(args.endpoint or ""), cli_profile=args.cli_profile)
            table = ensure_target_table(
                o, project, table_name, column, str(target_cfg.get("comment") or ""),
                stored_as=str(target_cfg.get("stored_as") or ""),
                lifecycle_days=(int(target_cfg["lifecycle_days"]) if target_cfg.get("lifecycle_days") else None),
                timeout=args.sql_timeout,
            )
            log(f"表就绪：{project}.{table_name}（{column} + {PARTITION_COLUMN}）")

            started_write = time.time()
            write_partition(table, table_name, pt, spool.iter_rows, total=spool.count)
            log(f"已写入 pt={pt}：{spool.count:,} 行，耗时 {(time.time() - started_write) / 60:.1f} 分钟")

            actual = count_partition(o, project, table_name, pt, timeout=args.sql_timeout)
            if actual != spool.count:
                keep_spool = args.keep_spool
                log(f"❌ 写后校验不一致：期望 {spool.count:,} 行，实际 {actual:,} 行（pt={pt}）")
                return 1
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - SQL/Tunnel 失败统一按失败退出（调度可告警）
            keep_spool = args.keep_spool
            log(f"❌ 写库失败：{exc}")
            return 1

        log(f"校验通过：pt={pt} 共 {actual:,} 行")
        log(f"全部完成（总耗时 {(time.time() - started) / 60:.1f} 分钟）。")
        return 0
    except KeyboardInterrupt:
        keep_spool = args.keep_spool
        log("已手动中断（本次未写库；若在写入阶段中断，分区可能不完整，重跑同一命令即可）")
        return 130
    finally:
        if keep_spool:
            log(f"已保留本次数据文件（--keep-spool）：{spool.path}")
        spool.close(keep=keep_spool)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)

    if args.init:                                    # 交互式建配置：不需要 --job
        from .init_wizard import run_init
        return run_init(args.init_out)

    if not args.job:
        log("请用 --job 指定作业配置文件（第一次接新源可以先用 `api2ods --init` 生成）")
        return 2

    log_handle = _open_log_file(args.log_file)
    if log_handle is not None:
        from .utils import add_log_sink
        add_log_sink(log_handle)

    try:
        # 凭证来源：作业文件自带；--config/默认 config.json 只在存在时作为补充（可选）
        if args.config:
            config_path = Path(args.config)
            config = load_json_file(config_path, "凭证/密钥文件")
        else:
            config_path = DEFAULT_CONFIG_PATH
            config = (load_json_file(config_path, "凭证/密钥文件")
                      if config_path.is_file() else {})

        job_path = Path(args.job)
        job_raw = load_json_file(job_path, "作业配置文件")

        # 业务日：--bizdate > 环境变量（DataWorks） > 时区昨天
        tz = date_tz_of(job_raw)
        if args.bizdate:
            bizdate = parse_day_arg(args.bizdate)
        elif env_bizdate() is not None:
            bizdate = env_bizdate()
        else:
            bizdate = datetime.now(tz).date() - timedelta(days=1)

        job = render_job(job_raw, config, bizdate)   # 替换 ${secrets.x}/${bizdate} 等占位符
        validate_job(job)
        for warning in collect_warnings(job):        # 未知字段告警（拼写错误提示）
            log(f"⚠️ {warning}")

        if args.check:
            return run_check(job, config, config_path, args, bizdate, job_path.resolve())

        with RunLock(_lock_path(job_path.resolve())):   # 同机同一作业互斥；不同作业可并行
            return run_sync(job, config, config_path, args, bizdate, job_path.resolve())
    finally:
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
