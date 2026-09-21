# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] - 2026-09-21

首个可公开发布的版本。

### 功能

- 配置驱动的 REST API → MaxCompute ODS 同步：一根 `jobs/*.json` 接一个源
- 响应形态：JSON（`records_path`）、文件流（ZIP / CSV / TSV / JSONL）
- 鉴权：`none` / `token` / `query` / `basic` / `sha256_concat` / `aliyun_rpc`（HMAC-SHA1）/ `custom`
- 取数窗口：`per_day`（按天）/ `range`（整区间），支持 `unix` / `unix_ms` / 各种 strftime 格式与派生参数
- 分页：页码（总页数/总条数两种终点判定，含"空页但未拉完"防御）/ 游标 / 单页
- 业务错误判定 `fail_if`、整窗重试、请求重试与 `Retry-After` 退避
- 写入：自动建表、结构校验、先删再填分区、Tunnel 分批写入、写后行数双重校验
- 大数据量流式落盘（Spool），内存与数据总量无关
- 运行锁（每作业一把）、密钥脱敏、`--log-file`、`--keep-spool`
- `api2ods --init` 交互式配置向导；`--check` / `--dry-run` / 补数（区间、零散日期）
- 跨平台：Windows / macOS / Linux，Python 3.9+

### 已验证的数据源形态

- Onerway 交易流水（自定义 sha256 签名 + 页码分页）
- Onerway 结算明细（参数走 Header + CSV 分段文件）
- 阿里云账单 `QueryInstanceBill`（RPC 签名 + 总条数分页 + 零数据日空结果）
- DeepSeek 用量导出（Bearer token + ZIP 内含 CSV）

## [1.0.0] - 2026-09-21（内部）

- 内部试用版：json + pt 落地、先删再填、单文件实现。
