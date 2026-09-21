# api2ods

**通用 REST API → MaxCompute ODS 同步工具**：把任意 HTTP 接口返回的 JSON / CSV / ZIP 记录
**原样**写进 MaxCompute ODS（每条记录一行 JSON + `pt` 业务日分区），DWD 层再解 JSON、
按主键取最新。接一个新数据源 ≈ 写一份配置文件。

- 配置驱动：地址、参数、鉴权、分页、时间窗口、目标表都在一个 JSON 里
- 写入幂等：每次按窗口拉数，**先删再填** `pt` 分区，重复跑/补数不会重复
- 自动建表：目标表不存在时按 `json string + pt string` 建表，存在则校验结构
- 少见即用的健壮性：请求重试+限流退避、业务错误判定、大数据量流式落盘（不占内存）、
  写后行数校验、密钥脱敏、运行锁，失败退出码非 0（方便调度系统告警）
- 跨平台：Windows / macOS / Linux（Python 3.9+），无系统级依赖

支持的真实接口形态（仓库内 `jobs/` 有完整样例）：

| 形态 | 例子 |
|---|---|
| Header Token + 页码分页 + JSON | 大多数 SaaS API |
| 自定义 sha256 签名 + POST JSON | Onerway |
| 参数走 Header + 返回 CSV 文件 | Onerway 结算明细 |
| 阿里云 RPC 签名（HMAC-SHA1）+ 总条数分页 | 阿里云账单 `QueryInstanceBill` |
| Bearer Token + ZIP 内含 CSV | DeepSeek 用量导出 |

## 安装

```bash
# 方式一：pip（建议放虚拟环境）
python3 -m venv venv && ./venv/bin/pip install api2ods    # 发布到 PyPI 后
./venv/bin/pip install .                                    # 或在源码目录本地安装
api2ods --version

# 方式二：pipx（全局命令行工具，隔离环境）
pipx install .

# 方式三：直接从 GitHub 安装
pipx install "git+https://github.com/sandy-wangzining/api2ods.git"

# 方式四：源码直接跑（不安装）
python -m api2ods --job jobs/xxx.json --check
```

Windows / macOS / Linux 通用；Windows 会自动带上 `tzdata` 依赖（时区数据）。

## 三分钟上手

```bash
# 1) 生成一份作业配置（交互式问答，密钥直接写进文件；也可复制 jobs/_template_simple.example.json 手工改）
api2ods --init

# 2) 体检：配置 + 真实请求一次 + 目标表结构（新接源第一步，报错会给线索）
api2ods --job jobs/my_api.json --check

# 3) 试跑：真实拉取统计，不写库
api2ods --job jobs/my_api.json --days 1 --dry-run

# 4) 正式同步：把最近 N 天写进 pt=<业务日>（N 取配置里的 window.days）
api2ods --job jobs/my_api.json --bizdate 20260918

# 常用补充：
api2ods --job jobs/my_api.json --start-date 2026-07-01 --end-date 2026-09-20   # 补数（闭区间）
api2ods --job jobs/my_api.json --dates 2026-09-01,2026-09-05                    # 补零散日期
api2ods --job jobs/my_api.json --bizdate ${bizdate} --workers 3                 # 调度（并发拉）
```

## 它是怎么工作的

```
接口（JSON/CSV/ZIP）
  │  ① 按 window 生成请求单元（每天一次 / 整区间一次），并发拉取
  │  ② 每个单元翻页拉全（页码/游标），记录逐条落本地临时文件（Spool，不占内存）
  │  ③ 任一单元三次重试仍失败 → 本次放弃写库（旧分区保持原样，重跑即可）
  ▼
MaxCompute ODS：<json 列> + pt 分区（普通表）
  │  ④ 自动建表/校验结构 → delete_partition → Tunnel 分批写入（先删再填）
  │  ⑤ count(*) 与拉取条数核对，不一致按失败退出
  ▼
DWD 层：解 JSON、按主键取最新一条（跨 pt 去重）
```

## 配置参考

作业文件（`jobs/*.json`）分六块，**只有 `request.base_url`、`request`、`target.table` 是必填**，
其余按需。字段默认值和逐行注释见 `jobs/_template_full.example.json`，最小示例见
`jobs/_template_simple.example.json`。示例里的 `<项目名>`、`<AccessKeyId>` 等占位符
换成你自己的值即可。

### 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `job` | 否 | 作业名（日志用） |
| `description` | 否 | 一句话描述（日志用） |
| `secrets` | 否 | 密钥键值对；`request` 里用 `${secrets.键名}` 引用。**也可以直接把密钥写在用到的地方** |
| `maxcompute` | 是* | 目标项目与凭证：`project` / `endpoint` / `access_key_id` / `access_key_secret`（* 或走环境变量/aliyun CLI） |
| `profiles` | 否 | 多套 MaxCompute 凭证，配合 `target.profile` 切换 |

> 占位符：`${secrets.键名}`、`${bizdate}`（YYYYMMDD）、`${bizdate_iso}`、`${today}`、`${today_iso}`。

### request（接口）

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_url` | - | 根地址（必填） |
| `path` | 空 | 路径；留空表示 base_url 就是完整地址 |
| `method` | GET | GET / POST / PUT / PATCH / DELETE |
| `body_type` | json | POST 请求体格式：`json` / `form` |
| `timeout_seconds` | 30 | 单次请求超时 |
| `headers` / `params` | {} | 静态请求头 / 参数 |
| `params_in` | query | `query`（默认）/ `headers`（参数全放请求头，部分文件接口要求） |
| `auth` | none | 见下表 |
| `records_path` | 空 | 记录数组路径（如 `data.list`）；留空=整个返回就是数组 |
| `records_missing` | error | `empty`：路径取不到时按空数据（零数据日接口） |
| `add_fields` | {} | 每条记录追加固定字段（多账号打来源标记） |
| `response_type` | json | `bytes`：文件流响应（配 `parse`，不支持分页） |
| `fail_if` | [] | 业务错误判定，如 `[{"path":"code","not_equals":"0","retry":true}]` |
| `retry_times` / `retry_delay` | 5 / 15 | 请求重试次数 / 首次冷却秒数（指数退避） |
| `verify` / `proxies` | true / - | HTTPS 证书校验 / 代理 |

### auth（鉴权，七选一）

| type | 关键字段 | 适用 |
|---|---|---|
| `none` | - | 公开接口 |
| `token` | `header` / `prefix` / `value` | 最常见：`Authorization: Bearer xxx` |
| `query` | `params` | 密钥放 URL 参数 |
| `basic` | `username` / `password` | HTTP Basic |
| `sha256_concat` | `secret_key` / `sign_field` / `sign_in` | 参数按 key 排序拼接+密钥做 sha256（Onerway） |
| `aliyun_rpc` | `access_key_id` / `access_key_secret` | 阿里云 OpenAPI RPC 签名（HMAC-SHA1） |
| `custom` | `module` / `func` | 自定义函数（放作业同目录 `signers.py`，见 `signers.example.py`） |

### window（取数窗口，不写=不传时间单次请求）

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | per_day | `per_day` 每天一次请求（推荐）/ `range` 整区间一次 |
| `days` | 1 | 回拉天数（含基准日）；`--days` 可覆盖 |
| `date_tz` | Asia/Shanghai | “最近 N 天”按哪个时区切 |
| `api_tz` | +08:00 | 传给接口的时间时区 |
| `pad_hours` | 0 | 窗口前后多拉几小时（防边界丢数） |
| `start_param` / `end_param` | - | 起止时间参数名；`end_param` 可省略 |
| `format` | %Y-%m-%d %H:%M:%S | 时间格式；`unix`=秒、`unix_ms`=毫秒 |
| `extra_params` | - | 额外派生参数，如 `{"BillingCycle": "%Y-%m"}`（基于窗口日起算） |

### pagination（分页，不写=单页）

| 字段 | 说明 |
|---|---|
| `type` | `none`（默认）/ `page` / `cursor` |
| `page_param` / `size_param` / `page_size` / `param_as_string` | 页码分页四件套 |
| `total_pages_path` / `total_items_path` | page 分页至少给一个（翻页终点）；两个都给时先满足者停 |
| `cursor_param` / `cursor_path` / `cursor_start` | 游标分页 |
| `delay_seconds` / `max_pages` / `window_retries` | 翻页间隔 / 最大页数保护 / 单窗口失败重试次数 |

### parse（`response_type=bytes` 时的文件解析）

| 字段 | 说明 |
|---|---|
| `format` | `csv` / `tsv` / `jsonl` |
| `encoding` | 默认 `utf-8-sig`（兼容 BOM）；乱码可试 `gbk` |
| `delimiter` / `skip_rows` | 分隔符 / 跳过开头 N 行 |
| `skip_until` | 从包含该文字的行开始解析（报表有汇总段时用；找不到按空数据处理） |
| `unzip` / `entry_contains` | 解 ZIP / 只取文件名含指定串的条目 |
| `entry_field` | 给每条记录加一列“来源文件名” |

### target（目标表）

| 字段 | 默认 | 说明 |
|---|---|---|
| `project` / `table` | - / 必填 | 目标项目（缺省用 maxcompute.project）/ 表名 |
| `pt` | ${bizdate} | 分区值模板；`--pt` 可覆盖 |
| `column` | json | json 列名 |
| `comment` / `stored_as` / `lifecycle_days` | - | 建表注释 / 存储格式 / 生命周期 |
| `allow_empty` | false | 本次 0 行时是否允许写空分区 |
| `profile` | default | 使用 `profiles.<名>` |

## 怎么接一个新源（照着抄）

1. **看接口文档**，回答四个问题：怎么鉴权？怎么翻页？记录在返回的哪个字段？有没有时间参数？
2. **挑模板**：

   | 你的接口 | 抄这个 |
   |---|---|
   | Header Token + 页码分页 | `jobs/_template_simple.example.json` |
   | 什么都不用改太多、想看所有字段 | `jobs/_template_full.example.json` |
   | 阿里云 OpenAPI（RPC 签名） | `jobs/aliyun_bill.example.json` |
   | 自定义签名（参数拼接/加盐） | `jobs/onerway_transactions.example.json` |
   | 参数走 Header + 返回 CSV 文件 | `jobs/onerway_settlement_details.example.json` |
   | 返回 ZIP/CSV 文件 | `jobs/deepseek_usage.example.json` |
   | 不想研究字段 | 直接 `api2ods --init`，按问答生成 |

3. **改三处**：`maxcompute`（AK/SK）、`request`（地址/鉴权/records_path）、`target.table`；
4. **跑 `--check`**：它会真实请求一次并打印返回顶层字段，帮你确认 `records_path`；
5. **跑 `--days 1 --dry-run`** 看条数对不对，然后就可以配调度了。

## 行为与保护（为什么可以放心跑调度）

1. 拉取有任何失败 → 不写库（旧分区保持原样），退出码 1；
2. 写库先删分区再写、整段失败自动重试，重跑幂等；
3. 写后 `count(*)` 与拉取条数双重核对；
4. 默认拒绝写 0 行（防接口异常时清空分区），要写空分区显式 `--allow-empty`；
5. 单行超过约 7MB 提前报错（MaxCompute 单列上限 8MB）；
6. 建表/删分区/校验 SQL 有超时保护（默认 600 秒，超时主动取消）；
7. 日志与异常里的 token/sign/secret 一律脱敏；
8. 429/5xx 按 `Retry-After` 退避重试，4xx 直接失败（参数/密钥问题快速暴露）；
9. **每个作业一把 flock 运行锁**（同机同一作业互斥、不同作业可并行）；Ctrl+C 不写库（退出码 130）；
10. 大数据量流式落盘（本地临时 JSONL，默认系统 temp，可用 `TMPDIR` 指定），内存占用与数据总量无关；失败时可 `--keep-spool` 保留数据文件排查。

## 常见问题

| 现象 | 处理 |
|---|---|
| `解析失败：找不到 records_path` | 路径写错；`--check` 会打印返回的顶层字段；接口“空对象=无数据”时加 `records_missing: "empty"` |
| `HTTP 401/403/400` | 密钥/参数问题（不重试）；检查 auth 与 params |
| `期望文件流，但接口返回了 JSON` | 文件接口的权限/参数错误（如未开通下载权限）；按提示里的 JSON 内容排查 |
| `接口返回业务错误：code=...` | 命中 `fail_if`；限流类错误给 `"retry": true` 先重试几次 |
| 大量“重试”日志 | 撞限流：降 `--workers`、调大 `pagination.delay_seconds` |
| `本次拉取 0 行...未写库` | 确认接口当天确实无数据；要写空分区加 `--allow-empty` |
| `表结构与框架要求不一致` | 目标表是宽表/事务表：换表名（建议 `ods_xxx_json_di`）或先 drop 重建 |
| 时区不对/跨天差一小时 | 检查 `window.date_tz`（怎么切天）与 `api_tz`（怎么传给接口），夏令时用 IANA 名称（如 `America/New_York`） |
| Windows 报找不到时区 | `pip install tzdata` |

## 开发与测试

```bash
python -m unittest discover -s tests -v    # 109 个离线用例：不访问网络、不连数仓
```

代码检查（可选，开发用）：`pip install ruff && ruff check .`（配置在 `pyproject.toml`，
只选能发现真问题的规则；当前 0 告警）。

模块结构（都在 `api2ods/` 包内，每个文件头部有职责说明）：

| 模块 | 职责 |
|---|---|
| `cli.py` | 命令行入口 / 体检 / 同步主流程 |
| `init_wizard.py` | `--init` 交互式配置生成 |
| `config.py` | 配置加载/占位符/校验/目标解析 |
| `dates.py` | 日期与时区、窗口参数生成 |
| `auth.py` | 七种鉴权（含阿里云 RPC 签名） |
| `http.py` | 请求、重试、Retry-After、业务错误判定 |
| `parsers.py` | JSON 路径 / CSV / ZIP / JSONL 解析 |
| `fetch.py` | 请求单元、翻页、并发、整窗重试 |
| `spool.py` | 流式落盘（大数据量不占内存） |
| `mc.py` | MaxCompute：建表校验 / 先删再填 / 行数核对 |
| `utils.py` | 日志、运行锁、脱敏、重试工具 |

## License

MIT（见 `LICENSE`）。
