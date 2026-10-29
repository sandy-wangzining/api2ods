# -*- coding: utf-8 -*-
"""MaxCompute：凭证、建表/结构校验、SQL 超时控制、分区覆盖写入、行数校验。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from . import _common
from .utils import ConfigError, log, progress_log, redact_secrets, require_identifier, retry_call

try:
    from odps import ODPS
except ImportError:  # pragma: no cover
    ODPS = None

PARTITION_COLUMN = "pt"
SQL_TIMEOUT_SECONDS = 600
WRITE_BATCH_SIZE = 1000  # 写入分批的行数上限（实际分批在 SpoolWriter.iter_batches 里做）
MAX_BATCH_BYTES = 8_000_000  # 写入分批的字节上限：单条记录很大时防止攒批吃内存
MAX_ROW_BYTES = 7_000_000  # MaxCompute string 上限 8MB，留余量提前报错
SQL_HEARTBEAT_SECONDS = 30
# https：作业没写 endpoint 时 AK/SK 签名与查询结果不能走明文 HTTP
DEFAULT_ENDPOINT = "https://service.us-west-1.maxcompute.aliyun.com/api"
# 分区值白名单：与 config 的 --pt/target.pt 口径一致（字母/数字/下划线/中划线）
_PARTITION_VALUE_RE = re.compile(r"\A[A-Za-z0-9_\-]{1,64}\Z")


# =============================================================================
# 凭证与连接
# =============================================================================


def _pick_aksk(item: dict) -> tuple[str, str]:
    """从配置里取 AK/SK：兼容 access_key_id/ak/_id 与 access_key_secret/ak_secret/sk 几种写法。"""
    if not isinstance(item, dict):
        # maxcompute 块写成字符串/数组时，get 会抛 'str' object has no attribute 'get' 的裸 AttributeError；
        # 与同函数对 aliyun CLI 配置的校验口径一致，这里给明确的中文报错
        raise SystemExit(f"maxcompute 配置必须是对象（键值对），实际 {type(item).__name__}")
    ak_id = next((str(item[key]) for key in ("access_key_id", "ak_id", "ak") if item.get(key)), "")
    secret = next((str(item[key]) for key in ("access_key_secret", "ak_secret", "sk") if item.get(key)), "")
    return (ak_id, secret) if ak_id and secret else ("", "")


def load_mc_credentials(profile: dict, source_label: str = "作业文件", cli_profile: str = "") -> tuple[str, str, str]:
    """按优先级查找阿里云 AccessKey，返回 (ak_id, ak_secret, 来源说明)；只说明位置，不含密钥。

    优先级：作业/配置文件里的 profile → 环境变量 → 本机 aliyun CLI。
    """
    ak_id, secret = _pick_aksk(profile or {})
    if ak_id and secret:
        name = str((profile or {}).get("name") or "default")
        return ak_id, secret, f"{source_label}（{name}）"
    if isinstance(profile, dict):
        has_id = any(profile.get(k) for k in ("access_key_id", "ak_id", "ak"))
        has_sk = any(profile.get(k) for k in ("access_key_secret", "ak_secret", "sk"))
        if has_id != has_sk:
            # 只填一半（典型：secret 键名拼错）必须报错，不能警告后回退：回退到环境变量/
            # 本机 aliyun CLI 会用另一个身份写库（审计/计费/归属全错），与「--mc-profile
            # 找不到不回退」「非映射报错」同口径
            missing = "access_key_secret（或 ak_secret/sk）" if has_id else "access_key_id（或 ak_id/ak）"
            raise ConfigError(
                f"{source_label}的 maxcompute 配置里 AK/SK 只填了一半、缺 {missing}；"
                f"补齐这对凭证，或整对留空以使用环境变量/本机 aliyun CLI"
            )

    env_id, env_secret = os.environ.get("ALIYUN_ACCESS_KEY_ID"), os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
    if env_id and env_secret:
        return env_id, env_secret, "环境变量 ALIYUN_ACCESS_KEY_ID/SECRET"
    if env_id or env_secret:
        # 只设了一半：原来静默忽略，最终报错只说"找不到 AccessKey"，用户不知道自己设漏了；
        # 继续往下找其它来源（CLI 配置），但把这条线索留出来
        log(
            f"  警告：环境变量只设了 {'ALIYUN_ACCESS_KEY_ID' if env_id else 'ALIYUN_ACCESS_KEY_SECRET'}、"
            f"缺 {'ALIYUN_ACCESS_KEY_SECRET' if env_id else 'ALIYUN_ACCESS_KEY_ID'}；"
            f"本次忽略环境变量，继续查找其它凭证来源"
        )

    cli_config_file = Path.home() / ".aliyun" / "config.json"
    if cli_config_file.is_file():
        # 这个文件是"锦上添花"的凭证来源，坏了不该抛裸 traceback（空文件 = JSONDecodeError，
        # profiles 写成对象 = 'str' object has no attribute 'get'），说明白文件名和原因就行
        try:
            cli_config = json.loads(cli_config_file.read_text(encoding="utf-8"))
            if not isinstance(cli_config, dict):
                raise ValueError(f"顶层应是 JSON 对象，实际是 {type(cli_config).__name__}")
            raw_profiles = cli_config.get("profiles") or []
            if not isinstance(raw_profiles, list):
                raise ValueError(f"profiles 应是数组，实际是 {type(raw_profiles).__name__}")
            unknown = [p for p in raw_profiles if not isinstance(p, dict)]
            if unknown:
                raise ValueError(f"profiles 的每个元素应是对象，实际有 {type(unknown[0]).__name__}")
            profiles = {p.get("name"): p for p in raw_profiles}
        except (OSError, ValueError) as exc:
            raise SystemExit(
                f"本机 aliyun CLI 配置读不了（{cli_config_file}）：{exc}\n"
                f"  可以不修它——改用作业文件的 maxcompute.access_key_id/access_key_secret，"
                f"或设环境变量 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET"
            ) from exc
        if cli_profile:
            # 显式指定的 profile 必须自食其力：找不到/没 AK 就直接报错，不能落进下面
            # "任意 AK profile"的回退——那会用另一个身份写库（与 sftp2ods 同口径）
            item = profiles.get(cli_profile) or {}
            if not (item.get("access_key_id") and item.get("access_key_secret")):
                raise SystemExit(
                    f"指定的 aliyun CLI profile「{cli_profile}」不存在或没有 AK/SK；"
                    f"不会自动回退到其它 profile（避免用另一个身份写库），请检查 --mc-profile"
                )
            return item["access_key_id"], item["access_key_secret"], f"aliyun CLI profile [{cli_profile}]"
        names = [cli_config.get("current")]
        names += [
            name
            for name, item in profiles.items()
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


def connect_odps(
    config: dict, source_label: str, profile: dict, project: str, endpoint: str = "", cli_profile: str = ""
):
    """建立 MaxCompute 连接；缺 pyodps / 缺凭证时给出明确报错。"""
    if ODPS is None:
        raise SystemExit("缺少 pyodps：pip install pyodps")
    ak_id, secret, source = load_mc_credentials(profile, source_label, cli_profile)
    log(f"MaxCompute 凭证来源：{source}")
    endpoint = endpoint or str((profile or {}).get("endpoint") or DEFAULT_ENDPOINT)
    if str(endpoint).lower().startswith("http://"):
        log("  警告：maxcompute.endpoint 是 http:// 明文地址：AK/SK 签名与查询结果会明文传输，建议改 https")
    return ODPS(ak_id, secret, project, endpoint=endpoint)


# =============================================================================
# SQL 执行（带超时：pyodps 默认不限时，云端卡住会一直干等）
# =============================================================================


def run_sql_with_timeout(
    o, sql: str, timeout: int = SQL_TIMEOUT_SECONDS, desc: str = "SQL", *, publication=None, secrets=()
):
    """通过公共实现等待 SQL，保留项目自身的时钟、脱敏日志与超时配置。"""
    return _common.run_sql_with_timeout(
        o,
        sql,
        timeout,
        desc,
        clock=time,
        log=lambda message: log(redact_secrets(secrets, str(message))),
        heartbeat=SQL_HEARTBEAT_SECONDS,
        publication=publication,
    )


# =============================================================================
# 目标表：DDL / 结构校验 / 自动建表
# =============================================================================


def build_target_ddl(
    project: str,
    table: str,
    column: str,
    comment: str,
    stored_as: str = "",
    lifecycle_days: int | None = None,
    *,
    staging: bool = False,
) -> str:
    """目标表 DDL：单 json 列 + pt 分区（普通表，暂存后原子发布）。

    project/table/column/stored_as 都是直接拼进 DDL 的**标识符**：这里再做一道防御性校验
    （config.validate_job 也会校验，但 mc.py 可能被单独调用/被别的入口调），挡住
    `ods_x; drop table ...` 这类注入与拼错导致的天书语法错。
    """
    project = require_identifier(project, "target.project")
    table = require_identifier(table, "target.table")
    column = require_identifier(column, "target.column")
    if stored_as:
        stored_as = require_identifier(stored_as, "target.stored_as")
    table_comment = (comment or "API 原始 JSON 原样落库").replace("\\", "\\\\").replace("'", "''")
    lines = [
        f"create table if not exists {project}.{table} (",
        f"    {column} string comment 'API 原始 JSON 文本（整条记录原样）'",
        ")",
        f"partitioned by ({PARTITION_COLUMN} string comment '业务日期 yyyyMMdd')",
    ]
    if stored_as:
        lines.append(f"stored as {stored_as}")
    stage_properties = ", 'lifecycle.deletemeta'='true'" if staging else ""
    lines.append(f"tblproperties ('comment' = '{table_comment}'{stage_properties})")
    if lifecycle_days:
        lines.append(f"lifecycle {int(lifecycle_days)}")
    return "\n".join(lines)


def verify_target_schema(table, table_name: str, column: str) -> None:
    """校验已存在的表结构与框架要求一致；不一致直接报错拒绝写入。

    检查顺序：不是视图 → 不是事务表 → 非分区列只有 json 列 → 分区列只有 pt →
    所有列都是 string。任何一条不满足都给出"要求 vs 实际"的对比，拒绝写入，
    避免把 JSON 写进宽表/事务表把线上数据搞乱。

    列名比较忽略大小写：MaxCompute 标识符大小写不敏感，别人建表写成 JSON/PT 时
    结构与要求一致，按大小写敏感比较会把能写的表拒掉。
    """
    if getattr(table, "is_virtual_view", False) or getattr(table, "is_materialized_view", False):
        raise SystemExit(f"{table_name} 是视图，不能作为写入目标")
    if getattr(table, "is_transactional", False):
        raise SystemExit(f"{table_name} 是事务表，本框架按「普通分区表暂存后原子发布」写入；请换表名或先 drop 后重建")
    schema = table.table_schema
    partitions = [str(col.name) for col in schema.partitions]
    columns = [str(col.name) for col in schema.columns if str(col.name) not in partitions]
    if [name.lower() for name in columns] != [column.lower()] or [name.lower() for name in partitions] != [
        PARTITION_COLUMN.lower()
    ]:
        raise SystemExit(
            f"{table_name} 表结构与框架要求不一致，拒绝写入。\n"
            f"  要求：列 [{column}] + 分区 [{PARTITION_COLUMN}]\n"
            f"  实际：列 {columns} + 分区 {partitions}"
        )
    for col in list(schema.partitions) + [c for c in schema.columns if str(c.name) not in partitions]:
        if str(col.type).lower() != "string":
            raise SystemExit(f"{table_name}.{col.name} 类型是 {col.type}，框架要求 string")


def ensure_target_table(
    o,
    project: str,
    table_name: str,
    column: str,
    comment: str = "",
    stored_as: str = "",
    lifecycle_days: int | None = None,
    timeout: int = SQL_TIMEOUT_SECONDS,
):
    """表不存在则自动按固定结构建表；存在则校验结构，返回 table 对象。"""
    ddl = build_target_ddl(project, table_name, column, comment, stored_as, lifecycle_days)
    run_sql_with_timeout(o, ddl, timeout=timeout, desc=f"建表 {table_name}")  # create if not exists
    table = o.get_table(f"{project}.{table_name}")
    verify_target_schema(table, table_name, column)
    return table


# =============================================================================
# 分区覆盖写入 / 行数校验
# =============================================================================


def _sql_spec(spec: str) -> str:
    """把 pyodps 风格分区串（pt=20260918）转成 DDL 里的带引号写法（pt='20260918'）。

    pyodps 的 partition.name 可能已带引号（pt='20260918'），统一先去掉再补引号，
    避免生成 pt=''20260918'' 这种非法写法；分区字段名同样过标识符白名单。

    边界：本工具的表结构被 verify_target_schema 强制为单级 pt 分区，spec 恒由内部按
    pt=<值> 构造，多级分区（pt=x,region=y）不可达（会被字段名白名单挡下）。
    """
    key, _, value = str(spec).partition("=")
    key = require_identifier(key.strip(), "分区字段名")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not value:
        raise ConfigError(f"分区值不能为空：{spec!r}")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"{key}='{escaped}'"


def drop_partition(
    o, project: str, table_name: str, spec: str, timeout: int = SQL_TIMEOUT_SECONDS, *, secrets=()
) -> None:
    """删除分区（DDL，走超时保护；分区不存在也不报错，与旧口径的 if_exists=True 一致）。

    走 DDL 而不是 pyodps 的 `table.delete_partition`（同步、不限时）：云端分区元数据操作
    卡住时同样会无限挂起、一直占着运行锁，把后续调度全顶掉。`IF EXISTS` 提供与
    `delete_partition(if_exists=True)` 一致的幂等语义。
    """
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    run_sql_with_timeout(
        o,
        f"alter table {project}.{table_name} drop if exists partition ({_sql_spec(spec)})",
        timeout=timeout,
        desc=f"删分区 {table_name}",
        secrets=secrets,
    )


def add_partition(
    o, project: str, table_name: str, spec: str, timeout: int = SQL_TIMEOUT_SECONDS, *, secrets=()
) -> None:
    """新增分区（DDL，走超时保护；分区已存在不报错，与旧口径的 if_not_exists=True 一致）。"""
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    run_sql_with_timeout(
        o,
        f"alter table {project}.{table_name} add if not exists partition ({_sql_spec(spec)})",
        timeout=timeout,
        desc=f"建分区 {table_name}",
        secrets=secrets,
    )


def write_partition(
    o,
    table,
    project: str,
    table_name: str,
    partition_value: str,
    batches_factory,
    total: int | None = None,
    retries: int = 3,
    timeout: int = SQL_TIMEOUT_SECONDS,
    secrets=(),
) -> int:
    """先写独立暂存表并核对，再以单条 INSERT OVERWRITE 发布；正式分区不提前删除。

    batches_factory 必须可重复读取；写前大小校验保留。暂存写入失败使用新 Tunnel 会话重试，
    发布重试复用已核对的数据；发布响应丢失时幂等覆盖，失败残留由生命周期兜底回收。
    """
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    _common.ensure_no_pending_publication(o, project, table_name)
    partition_value = _partition_literal(partition_value)
    spec = f"{PARTITION_COLUMN}={partition_value}"
    columns = [col for col in table.table_schema.columns if str(col.name).lower() != PARTITION_COLUMN]
    if len(columns) != 1:
        raise ConfigError("API 目标表必须只有一个非分区列")
    verify_target_schema(table, table_name, str(columns[0].name))
    select_columns = ", ".join(require_identifier(col.name, "目标列名") for col in columns)

    def check_sizes() -> None:
        index = 0
        for rows in batches_factory():
            for row in rows:
                index += 1
                size = len(row.encode("utf-8"))
                if size > MAX_ROW_BYTES:
                    raise SystemExit(
                        f"第 {index:,} 条记录 JSON 大小 {size:,} 字节，超过单列上限（约 {MAX_ROW_BYTES:,} 字节）；"
                        "请检查 records_path 是否指到了大对象、或该接口记录过大"
                    )

    check_sizes()
    stage_name = f"tmp_api2ods_{uuid.uuid4().hex}"
    stage_ddl = build_target_ddl(
        project, stage_name, str(columns[0].name), "API 暂存数据，校验后发布", lifecycle_days=1, staging=True
    )
    verified = published = False
    written = 0
    try:
        run_sql_with_timeout(o, stage_ddl, timeout=timeout, desc=f"建暂存表 {stage_name}", secrets=secrets)
        stage = o.get_table(f"{project}.{stage_name}")
        verify_target_schema(stage, stage_name, str(columns[0].name))

        def publish() -> int:
            nonlocal verified, published, written
            if not verified:
                drop_partition(o, project, stage_name, spec, timeout=timeout, secrets=secrets)
                add_partition(o, project, stage_name, spec, timeout=timeout, secrets=secrets)
                written = 0
                with stage.open_writer(partition=spec, reopen=True) as writer:
                    for rows in batches_factory():
                        writer.write([[row] for row in rows])
                        written += len(rows)
                        progress_log(f"{stage_name} pt={partition_value} 暂存", written, "行")
                if total is not None and written != total:
                    raise RuntimeError(f"暂存写入行数异常：计划 {total:,}，实际 {written:,}")
                actual = count_partition(o, project, stage_name, partition_value, timeout=timeout, secrets=secrets)
                if actual != written:
                    raise RuntimeError(f"暂存校验行数不一致：写入 {written:,}，查询 {actual:,}")
                verified = True
            run_sql_with_timeout(
                o,
                f"insert overwrite table {project}.{table_name} partition ({_sql_spec(spec)}) "
                f"select {select_columns} from {project}.{stage_name} "
                f"where {PARTITION_COLUMN} = '{partition_value}'",
                timeout=timeout,
                desc=f"发布分区 {table_name} pt={partition_value}",
                publication=(project, table_name),
                secrets=secrets,
            )
            published = True
            return written

        try:
            return retry_call(
                publish,
                attempts=max(1, retries),
                base_delay=10,
                desc=f"{table_name} pt={partition_value} 暂存/发布",
                secrets=secrets,
            )
        except Exception as exc:
            raise RuntimeError(
                f"{table_name} pt={partition_value} 发布失败；未提前删除正式分区。"
                f"暂存表 {project}.{stage_name} 已保留（生命周期 1 天）；"
                f"发布可能已提交，请核对云端实例及正式分区后重跑：{exc}"
            ) from exc
    finally:
        if published:
            try:
                run_sql_with_timeout(
                    o,
                    f"drop table if exists {project}.{stage_name}",
                    timeout=timeout,
                    desc="清理暂存表",
                    secrets=secrets,
                )
            except (Exception, _common.UnresolvedSqlError) as exc:
                log(
                    f"  警告：暂存表 {project}.{stage_name} 清理失败，将由生命周期回收：{redact_secrets(secrets, str(exc))}"
                )
        else:
            log(f"  提示：暂存表 {project}.{stage_name}（如已创建）已保留；正式分区未提前删除")


def _partition_literal(partition_value) -> str:
    """拼进 SQL 的分区值：白名单 + 引号转义。

    MaxCompute 没有绑定参数，分区值只能拼进语句；白名单把"能拼什么"锁死
    （分区名只允许字母/数字/下划线/中划线），反斜杠/引号/空格等注入面直接归零。
    """
    text = str(partition_value)
    if not _PARTITION_VALUE_RE.match(text):
        raise ConfigError(f"分区值不合法（只允许字母/数字/下划线/中划线，1~64 字符）：{text!r}")
    return text.replace("'", "''")


def count_partition(
    o, project: str, table_name: str, partition_value: str, timeout: int = SQL_TIMEOUT_SECONDS, *, secrets=()
) -> int:
    """SELECT COUNT(*) 校验分区行数（用于写后核对）。"""
    # 表名同样是拼进 SQL 的标识符：再校验一道，挡住注入与拼错的表名
    project = require_identifier(project, "target.project")
    table_name = require_identifier(table_name, "target.table")
    literal = _partition_literal(partition_value)
    sql = f"select count(*) as cnt from {project}.{table_name} where {PARTITION_COLUMN} = '{literal}'"
    instance = run_sql_with_timeout(o, sql, timeout=timeout, desc=f"校验 {table_name} 行数", secrets=secrets)
    with instance.open_reader() as reader:
        for row in reader:
            # reader 的行既可能是"按列名取值"的对象（pyodps 的 Record），也可能是元组
            # （部分实现/测试替身直接给 tuple）。原来用 hasattr(row, "__getitem__") 判断，
            # 而 tuple 也有 __getitem__，于是走 row["cnt"] 直接 TypeError。改成先按列名、
            # 失败再按位置取。
            try:
                return int(row["cnt"])
            except (TypeError, KeyError, IndexError):
                return int(row[0])
    # COUNT 必须返回一行；未知不能冒充零行，否则写空保护会允许清空已有数据。
    raise RuntimeError(f"校验 SQL 未返回行，无法确认分区 {table_name} pt={partition_value} 的行数")
