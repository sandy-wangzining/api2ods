# -*- coding: utf-8 -*-
"""MaxCompute：凭证、建表/结构校验、SQL 超时控制、分区覆盖写入、行数校验。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .utils import log, retry_call

try:
    from odps import ODPS
except ImportError:  # pragma: no cover
    ODPS = None

PARTITION_COLUMN = "pt"
SQL_TIMEOUT_SECONDS = 600
WRITE_BATCH_SIZE = 1000
MAX_ROW_BYTES = 7_000_000      # MaxCompute string 上限 8MB，留余量提前报错
SQL_HEARTBEAT_SECONDS = 30
DEFAULT_ENDPOINT = "http://service.us-west-1.maxcompute.aliyun.com/api"


# =============================================================================
# 凭证与连接
# =============================================================================

def _pick_aksk(item: dict) -> tuple[str, str]:
    """从配置里取 AK/SK：兼容 access_key_id/ak/_id 与 access_key_secret/ak_secret/sk 几种写法。"""
    ak_id = next((str(item[key]) for key in ("access_key_id", "ak_id", "ak")
                  if item.get(key)), "")
    secret = next((str(item[key]) for key in ("access_key_secret", "ak_secret", "sk")
                   if item.get(key)), "")
    return (ak_id, secret) if ak_id and secret else ("", "")


def load_mc_credentials(profile: dict, source_label: str = "作业文件", cli_profile: str = "") -> tuple[str, str, str]:
    """按优先级查找阿里云 AccessKey，返回 (ak_id, ak_secret, 来源说明)；只说明位置，不含密钥。

    优先级：作业/配置文件里的 profile → 环境变量 → 本机 aliyun CLI。
    """
    ak_id, secret = _pick_aksk(profile or {})
    if ak_id and secret:
        name = str((profile or {}).get("name") or "default")
        return ak_id, secret, f"{source_label}（{name}）"

    env_id, env_secret = os.environ.get("ALIYUN_ACCESS_KEY_ID"), os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
    if env_id and env_secret:
        return env_id, env_secret, "环境变量 ALIYUN_ACCESS_KEY_ID/SECRET"

    cli_config_file = Path.home() / ".aliyun" / "config.json"
    if cli_config_file.is_file():
        cli_config = json.loads(cli_config_file.read_text(encoding="utf-8"))
        profiles = {p.get("name"): p for p in cli_config.get("profiles", [])}
        names = [cli_profile] if cli_profile else [cli_config.get("current")]
        names += [
            name for name, item in profiles.items()
            if item.get("mode") == "AK" and item.get("access_key_id") and item.get("access_key_secret")
        ]
        for name in names:
            item = profiles.get(name) or {}
            if item.get("access_key_id") and item.get("access_key_secret"):
                return item["access_key_id"], item["access_key_secret"], f"aliyun CLI profile [{name}]"

    raise SystemExit(
        "找不到阿里云 AccessKey。任选一种方式：\n"
        "  1) 在作业文件的 maxcompute 块里填 access_key_id/access_key_secret（推荐）\n"
        "  2) 设置环境变量 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET\n"
        "  3) 配置本机 aliyun CLI（aliyun configure）"
    )


def connect_odps(config: dict, source_label: str, profile: dict, project: str,
                 endpoint: str = "", cli_profile: str = ""):
    """建立 MaxCompute 连接；缺 pyodps / 缺凭证时给出明确报错。"""
    if ODPS is None:
        raise SystemExit("缺少 pyodps：pip install pyodps")
    ak_id, secret, source = load_mc_credentials(profile, source_label, cli_profile)
    log(f"MaxCompute 凭证来源：{source}")
    endpoint = endpoint or str((profile or {}).get("endpoint") or DEFAULT_ENDPOINT)
    return ODPS(ak_id, secret, project, endpoint=endpoint)


# =============================================================================
# SQL 执行（带超时：pyodps 默认不限时，云端卡住会一直干等）
# =============================================================================

def run_sql_with_timeout(o, sql: str, timeout: int = SQL_TIMEOUT_SECONDS, desc: str = "SQL"):
    """提交 SQL 并等待完成：成功返回 / 失败抛错 / 超时主动 stop() 取消并抛 TimeoutError。"""
    instance = o.run_sql(sql)
    started = time.time()
    last_log = started
    while True:
        if instance.is_successful():
            return instance
        if instance.is_terminated():
            instance.wait_for_success(timeout=1)  # 触发一次，抛出带错误信息的异常
            return instance
        now = time.time()
        if timeout and timeout > 0 and now - started > timeout:
            try:
                instance.stop()
            except Exception:  # noqa: BLE001 - 取消失败不影响报错
                pass
            raise TimeoutError(f"{desc} 执行超过 {timeout} 秒，已主动停止")
        if timeout and timeout > 0 and now - last_log >= SQL_HEARTBEAT_SECONDS:
            log(f"    {desc} 还在执行（已等待 {int(now - started)} 秒，超时阈值 {timeout} 秒）...")
            last_log = now
        time.sleep(1)


# =============================================================================
# 目标表：DDL / 结构校验 / 自动建表
# =============================================================================

def build_target_ddl(project: str, table: str, column: str, comment: str,
                     stored_as: str = "", lifecycle_days: int | None = None) -> str:
    """目标表 DDL：单 json 列 + pt 分区（普通表，先删再填模式）。"""
    table_comment = (comment or "API 原始 JSON 原样落库").replace("'", "''")
    lines = [
        f"create table if not exists {project}.{table} (",
        f"    {column} string comment 'API 原始 JSON 文本（整条记录原样）'",
        ")",
        f"partitioned by ({PARTITION_COLUMN} string comment '业务日期 yyyyMMdd')",
    ]
    if stored_as:
        lines.append(f"stored as {stored_as}")
    lines.append(f"tblproperties ('comment' = '{table_comment}')")
    if lifecycle_days:
        lines.append(f"lifecycle {int(lifecycle_days)}")
    return "\n".join(lines)


def verify_target_schema(table, table_name: str, column: str) -> None:
    """校验已存在的表结构与框架要求一致；不一致直接报错拒绝写入。

    检查顺序：不是视图 → 不是事务表 → 非分区列只有 json 列 → 分区列只有 pt →
    所有列都是 string。任何一条不满足都给出"要求 vs 实际"的对比，拒绝写入，
    避免把 JSON 写进宽表/事务表把线上数据搞乱。
    """
    if getattr(table, "is_virtual_view", False) or getattr(table, "is_materialized_view", False):
        raise SystemExit(f"{table_name} 是视图，不能作为写入目标")
    if getattr(table, "is_transactional", False):
        raise SystemExit(
            f"{table_name} 是事务表，本框架按「先删再填普通分区表」写入；"
            f"请换表名或先 drop 后重建"
        )
    schema = table.table_schema
    partitions = [str(col.name) for col in schema.partitions]
    columns = [str(col.name) for col in schema.columns if str(col.name) not in partitions]
    if columns != [column] or partitions != [PARTITION_COLUMN]:
        raise SystemExit(
            f"{table_name} 表结构与框架要求不一致，拒绝写入。\n"
            f"  要求：列 [{column}] + 分区 [{PARTITION_COLUMN}]\n"
            f"  实际：列 {columns} + 分区 {partitions}"
        )
    for col in list(schema.partitions) + [c for c in schema.columns if str(c.name) not in partitions]:
        if str(col.type).lower() != "string":
            raise SystemExit(f"{table_name}.{col.name} 类型是 {col.type}，框架要求 string")


def ensure_target_table(o, project: str, table_name: str, column: str, comment: str = "",
                        stored_as: str = "", lifecycle_days: int | None = None,
                        timeout: int = SQL_TIMEOUT_SECONDS):
    """表不存在则自动按固定结构建表；存在则校验结构，返回 table 对象。"""
    ddl = build_target_ddl(project, table_name, column, comment, stored_as, lifecycle_days)
    run_sql_with_timeout(o, ddl, timeout=timeout, desc=f"建表 {table_name}")   # create if not exists
    table = o.get_table(table_name)
    verify_target_schema(table, table_name, column)
    return table


# =============================================================================
# 分区覆盖写入 / 行数校验
# =============================================================================

def write_partition(table, table_name: str, partition_value: str,
                    rows_factory, total: int | None = None,
                    retries: int = 3, batch_size: int = WRITE_BATCH_SIZE) -> int:
    """先删再填一个分区（delete → create → Tunnel 分批写入），整段失败自动重试。

    - rows_factory：一个"可重复调用"的函数，每次返回从头开始的迭代器（Spool 读回）；
      用工厂而不是列表，是为了支持失败重试时重新读一遍，同时内存里只留一个批次。
    - total：预期行数；写完后核对，不一致报错（调用方再与 count(*) 二次校验）。
    - 单行超过 MAX_ROW_BYTES 直接报错（MaxCompute 单列上限 8MB，提前失败比写一半好）。
    """
    spec = f"{PARTITION_COLUMN}={partition_value}"

    def _do() -> int:
        table.delete_partition(spec, if_exists=True)      # 先删：重复跑/补数不会叠加
        table.create_partition(spec, if_not_exists=True)
        written = 0
        batch: list[list[str]] = []
        with table.open_writer(partition=spec) as writer:  # Tunnel 写入
            for row in rows_factory():
                size = len(row.encode("utf-8"))
                if size > MAX_ROW_BYTES:
                    raise SystemExit(
                        f"第 {written + len(batch) + 1:,} 条记录 JSON 大小 {size:,} 字节，"
                        f"超过单列上限（约 {MAX_ROW_BYTES:,} 字节）；请检查 records_path "
                        f"是否指到了大对象、或该接口记录过大"
                    )
                batch.append([row])
                if len(batch) >= batch_size:
                    writer.write(batch)
                    written += len(batch)
                    batch = []
                    if written % (batch_size * 10) == 0:
                        log(f"    写入进度：{written:,} 行")
            if batch:
                writer.write(batch)
                written += len(batch)
        return written

    written = retry_call(_do, attempts=max(1, retries), base_delay=10,
                         desc=f"{table_name} pt={partition_value} 写入")
    if total is not None and written != total:
        raise RuntimeError(f"{table_name} 写入行数异常：计划 {total:,}，实际 {written:,}")
    return written


def count_partition(o, project: str, table_name: str, partition_value: str,
                    timeout: int = SQL_TIMEOUT_SECONDS) -> int:
    """SELECT COUNT(*) 校验分区行数（用于写后核对）。"""
    sql = (f"select count(*) as cnt from {project}.{table_name} "
           f"where {PARTITION_COLUMN} = '{partition_value}'")
    instance = run_sql_with_timeout(o, sql, timeout=timeout, desc=f"校验 {table_name} 行数")
    with instance.open_reader() as reader:
        for row in reader:
            return int(row["cnt"] if hasattr(row, "__getitem__") else row[0])
    return 0
