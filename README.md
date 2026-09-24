# api2ods

[![tests](https://github.com/sandy-wangzining/api2ods/actions/workflows/tests.yml/badge.svg)](https://github.com/sandy-wangzining/api2ods/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://github.com/sandy-wangzining/api2ods/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sandy-wangzining/api2ods/blob/main/LICENSE)

**通用 REST API → MaxCompute ODS 同步工具**：把任意 HTTP 接口返回的 JSON / CSV / ZIP 记录
**原样**写进 MaxCompute ODS（每条记录一行 JSON + `pt` 业务日分区），DWD 层再解 JSON、
按主键取最新。接一个新数据源 ≈ 写一份配置文件。

- 配置驱动：地址、参数、鉴权、分页、时间窗口、目标表都在一个 JSON 里
- 写入幂等：每次按窗口拉数，**先删再填** `pt` 分区，重复跑/补数不会重复
- 自动建表：目标表不存在时按 `json string + pt string` 建表，存在则校验结构
- 少见即用的健壮性：请求重试+限流退避、业务错误判定、大数据量流式落盘（不占内存）、
  写后行数校验、密钥脱敏、运行锁，失败退出码非 0（方便调度系统告警）
- 写入不改数据：每条记录原样一行 JSON（只追加 `add_fields` 里的固定字段），
  列名/空值/编码都保持接口返回的样子，怎么解留给下游 DWD
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

# 3) 试跑：真实拉取统计，不写库（--days 1 只拉一天，先看条数对不对）
api2ods --job jobs/my_api.json --bizdate 20260918 --days 1 --dry-run

# 4) 正式同步：把最近 N 天写进 pt=<业务日>（N 取配置里的 window.days）
api2ods --job jobs/my_api.json --bizdate 20260918

# 常用补充：
api2ods --job jobs/my_api.json --bizdate ${bizdate} --workers 3                 # 调度（并发拉）
api2ods --job jobs/my_api.json --bizdate ${bizdate} --days 30                   # 回拉最近 30 天（覆盖配置里的 window.days）

# 补数（闭区间）：必须跟着 --bizdate（整段写进 pt=<bizdate>），见下方「一次运行只写一个 pt」
api2ods --job jobs/my_api.json --bizdate 20260920 --start-date 2026-07-01 --end-date 2026-09-20
api2ods --job jobs/my_api.json --bizdate 20260920 --dates 2026-09-01,2026-09-05   # 补零散日期

# 补数月份多的时候并发拉（--workers 只并行网络等待，写库仍是单线程、顺序不变）
api2ods --job jobs/my_api.json --bizdate 20260920 --start-date 2026-07-01 --end-date 2026-09-20 --workers 3
```

> **一次运行只写一个 `pt`**（这是设计：一个分区＝一次运行的一份快照，DWD 再按源数据里的
> 日期字段重新分区）。`--dates` / `--start-date`+`--end-date` 只决定"拉哪些天"，
> **不决定"写进哪个分区"**，所以补数时不给业务日就会把整段窗口全写进默认业务日分区
> （`pt=<昨天>`）、并先删后填覆盖掉调度刚写进去的数据。为此补数**必须**跟着 `--bizdate`：
> `--bizdate 20260920` 表示整段补数数据写进 `pt=20260920` —— 分区口径和日常调度完全一致，
> DWD 不用区分"正式"和"补数"两种分区。缺了 `--bizdate` 会直接报错退出，不会静默写错分区。

一个最简单的作业配置长这样（**没写的字段都有默认值**：翻页方式自动推断、page/size/100、
startTime/endTime、时区、`pt=${bizdate}`……）：

```json
{
  "job": "demo_api",
  "maxcompute": { "project": "<项目名>", "ak": "<AK>", "sk": "<SK>" },
  "request": {
    "base_url": "https://api.example.com",
    "path": "/v1/items",
    "auth": { "type": "bearer", "token": "<token>" },
    "records_path": "data.list"
  },
  "window": { "days": 15 },
  "pagination": { "total_pages_path": "data.totalPages" },
  "target": { "table": "ods_demo_api_json_di" }
}
```

## 它是怎么工作的

```
接口（JSON/CSV/ZIP）
  │  ① 按 window 生成请求单元（每天一次 / 整区间一次），并发拉取
  │  ② 每个单元翻页拉全（页码/游标），拉完一个单元就逐条落本地临时文件
  │     （Spool：不跨单元累积，峰值内存 = 单个单元的数据量）
  │  ③ 任一单元三次重试仍失败 → 本次放弃写库（旧分区保持原样，重跑即可）
  ▼
MaxCompute ODS：<json 列> + pt 分区（普通表）
  │  ④ 自动建表/校验结构 → delete_partition → Tunnel 分批写入（先删再填）
  │  ⑤ count(*) 与拉取条数核对，不一致按失败退出
  ▼
DWD 层：解 JSON、按主键取最新一条（跨 pt 去重）
```

## 配置参考

作业文件（`jobs/*.json`）按下面几张表分块，**只有 `request.base_url` 和 `target.table`
是必填**，其余按需。字段默认值和逐行注释见 `jobs/_template_full.example.json`，最小示例见
`jobs/_template_simple.example.json`。示例里的 `<项目名>`、`<AccessKeyId>` 等占位符
换成你自己的值即可。

### 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `job` | 否 | 作业名（日志用） |
| `description` | 否 | 一句话描述（日志用） |
| `secrets` | 否 | 密钥键值对；`request` 里用 `${secrets.键名}` 引用。**也可以直接把密钥写在用到的地方** |
| `maxcompute` | 是* | 目标项目与凭证：`project` / `endpoint`（默认 us-west-1）/ `access_key_id`+`access_key_secret`（可简写 `ak`/`sk`） |
| `profiles` | 否 | 多套 MaxCompute 凭证，配合 `target.profile` 切换 |

> 占位符：`${secrets.键名}`、`${bizdate}`（YYYYMMDD）、`${bizdate_iso}`、`${today}`、`${today_iso}`。

### request（接口）

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_url` | - | 根地址（必填）；**不跟随重定向**——301/302/303 会把 POST 降级成不带 body 的 GET（窗口参数全丢），自定义鉴权头也可能被转发到别的地址；接口返回 3xx 会直接报错并打印 Location，请把地址改成最终地址 |
| `path` | 空 | 路径；留空表示 base_url 就是完整地址 |
| `method` | GET | GET / POST / PUT / PATCH / DELETE |
| `body_type` | json | POST 请求体格式：`json` / `form` |
| `timeout_seconds` | 30 | 单次请求超时 |
| `headers` / `params` | {} | 静态请求头 / 参数 |
| `params_in` | query | `query`（默认）/ `headers`（参数全放请求头，部分文件接口要求） |
| `auth` | none | 见下表（`bearer` 最常用） |
| `records_path` | 空 | 记录数组路径（如 `data.list`）；留空=整个返回就是数组 |
| `response_type` | json | `json` / `bytes`（文件流，配 `parse`，不支持分页） |
| `json_encoding` | 空 | JSON 接口不是 UTF-8（如 GBK）时显式指定编码；不写则按 UTF-8 → 响应头声明的 charset 严格解码，解不出直接报错（不静默变乱码写库）。若显式编码与 UTF-8 都能解出但键名不同，会直接报配置错；键名一致、只有值不同（GBK 字节恰好也是合法 UTF-8）时按显式编码处理并告警 |
| `records_missing` | error | `empty`：路径取不到时按空数据（零数据日接口） |
| `add_fields` | {} | 每条记录追加固定字段（多账号打来源标记） |
| `fail_if` | [] | 业务错误判定，如 `[{"path":"code","not_equals":"0","retry":true}]`（`response_type=bytes` 时自动跳过） |
| `retry_times` / `retry_delay` | 5 / 15 | 失败后最多**再试**几次 / 首次冷却秒数（指数退避）；即总请求数 = `retry_times`+1，写 `0` 就是只发一次 |
| `verify` / `proxies` | true / - | HTTPS 证书校验 / 代理 |

### auth（鉴权，八选一）

| type | 关键字段 | 适用 |
|---|---|---|
| `none` | - | 公开接口 |
| `bearer` | `token` | **最常用**：`Authorization: Bearer xxx`（只写一个字段） |
| `token` | `header` / `prefix` / `value` | 自定义头，如 `X-Api-Key: xxx` |
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
| `date_tz` | Asia/Shanghai | “最近 N 天”按哪个时区切（写时区名，如 `America/New_York`） |
| `api_tz` | +08:00 | 传给接口的时间按哪个时区算：固定偏移（`+08:00` / `+0800` / `+08`）或时区名（`America/New_York`，自动处理夏令时） |
| `pad_hours` | 0 | 窗口前后多拉几小时（防边界丢数），取值 0~24。**只要 >0，相邻两天的窗口就会重合 2×pad_hours 小时**（同一笔数据拉两遍，靠 DWD 按主键去重兜底）。`format` 只到日期时本项**被忽略并告警**：日期参数减 pad 不是多拉一段，而是把日期整体顶到前一天 |
| `start_param` / `end_param` | startTime / endTime | 起止时间参数名。**两个都不写**才启用默认；自定义了 `start_param` 就不补 `end_param`（只要开始时间只写 `start_param`，不要结束时间写 `"end_param": null`） |
| `format` | %Y-%m-%d %H:%M:%S | 时间格式；`unix`=秒、`unix_ms`=毫秒；`%s`（epoch 秒）与 `%P`（am/pm）可在格式串任意位置用（自己实现，跨平台一致；`%s` 单用返回纯数字）。**只到日期（无时分秒）时按 `date_tz` 直接输出那一天，不做时区换算** |
| `extra_params` | - | 额外派生参数，如 `{"BillingCycle": "%Y-%m"}`（基于窗口日起算） |

> **时区怎么配**（美东源这种最容易错，先看这段）：
> `date_tz` 决定“20260920 是哪一天的 20260920”，`api_tz` 决定“接口认哪个时区的时间”。
> 两个都填对就不会错位。例：美东业务日 + 接口要北京时间字符串 →
> `"date_tz": "America/New_York", "api_tz": "+08:00"` 时，`bizdate=20260920` 那一天
> 发出去的是 `startTime=2026-09-20 12:00:00` / `endTime=2026-09-21 12:00:00`，
> 也就是美东 9/20 00:00 ～ 9/21 00:00（9 月美东在夏令时，是 UTC-4）。
>
> - 接口认美东时间时写 **`"api_tz": "America/New_York"`（时区名）**，
>   不要写死 `-05:00`——夏令时期间美东是 `-04:00`，写死会在半年里每天错一小时；
> - `date_tz` 忘了改（留着默认上海）时，装进 `pt=20260920` 的会是「美东 9/19 半天 + 9/20 半天」，
>   数据不缺但跟源方的账期对不上；DWD 层按源数据里的日期字段重新分区可以规避这个影响；
> - `format` 只到日期时（`%Y-%m-%d` / `%Y%m%d`）不参与时区换算，直接就是那一天的日期。
>
> **统一口径**：本仓库现有作业一律 `date_tz: America/New_York`（数仓按美东）。
> `date_tz` 只管两件事——不带 `--bizdate` 时的默认业务日、以及窗口起止怎么切成“一天”；
> `pt` 永远是业务日 `bizdate` 本身。`format` 只到日期时（阿里云 `BillingDate`）
> 它**完全不影响**发给接口的参数，改它只是让配置口径统一；
> `format` 带时分秒 / `unix` 时（DeepSeek）它决定窗口的实际起止，换时区会改变
> 每个 `pt` 里装的是哪段时间（同一 `pt` 的内容整体平移，不影响 `count` 校验）。

### pagination（分页，不写=单页）

| 字段 | 默认 | 说明 |
|---|---|---|
| `type` | 自动推断 | `none` / `page` / `cursor`；不写时：有 `cursor_path`→cursor、有 `total_*` 或 `page_param`→page。**只配了 `size_param`/`page_size` 会告警**（没有翻页终点，只会请求一次） |
| `page_param` / `size_param` / `page_size` / `param_as_string` | page / size / 100 / false | 页码/游标分页用；`size_param` 写 `null` 表示不带页大小参数（接口不认 `size` 时）。**`page_param` 不能与 `size_param` 同名**：同名时页大小会覆盖页码，接口只回同一页、重复行会静默入库 |
| `total_pages_path` / `total_items_path` | - | page 分页至少给一个（翻页终点）；两个都给时先满足者停。**只认正数**（`-1`/`0` 这类"未知"哨兵会被忽略），且终点值单调不减（某页只回本页条数也不会提前收尾）。cursor 分页也可以用 `total_items_path` 做兜底：游标提前结束但条数没拉够时会报错（防"游标字段写错 → 只拉第一页"） |
| `strict` | true | 严格模式：空页但 `TotalCount` 没拉够 → 判失败（防静默截断）。接口总数不准才设 `false`，此时按已拉到的收尾并打警告。终点字段只在第一页返回时会被记住并沿用（末页之后的空页不再误判为"无法确认翻完"） |
| `cursor_param` / `cursor_path` / `cursor_start` | 游标分页；`cursor_start` 写 `""` 表示首页就带上空的游标参数（默认首页不带）。**建议同时配 `total_items_path`**：没配时游标字段写错会被当成"翻完了"只拉第一页（会告警提示） |
| `delay_seconds` / `max_pages` / `window_retries` | 翻页间隔 / 最大页数保护 / 单窗口失败重试次数 |

### parse（`response_type=bytes` 时的文件解析）

| 字段 | 说明 |
|---|---|
| `format` | `csv` / `tsv` / `jsonl` |
| `encoding` | 默认 `utf-8-sig`（兼容 BOM）；乱码可试 `gbk` |
| `delimiter` / `skip_rows` | 分隔符 / 跳过开头 N 行 |
| `skip_until` | 从包含该文字的行开始解析（报表有汇总段时用）。文件为空=当天没数据（按空处理）；文件有内容却没有该标记=报错（文件结构可能变了）。与 `skip_rows` 不要同时配：标记行若是被跳掉的那几行，会直接报冲突 |
| `unzip` / `entry_contains` | 解 ZIP / 只取文件名含指定串的条目。ZIP 里有多个文件又没写 `entry_contains` 时报错，确实要全解析时加 `allow_multi_entry: true` |
| `strict_encoding` | `true` 时按 `encoding` 解码失败直接报错；默认 `false`（解出乱码会打警告，仍按替换字符解析）——源方文件编码可能变过时建议打开 |
| `allow_multi_entry` | 见 `unzip`：ZIP 多文件时是否允许全部解析 |
| `entry_field` | 给每条记录加一列“来源文件名”（仅 `unzip: true` 时生效；没开 `unzip` 会告警） |
| `allow_single_record` | 仅 `format: jsonl` 用：整个响应恰好只有一条 JSON 记录时，它和接口 200 返回的 `{"code":500,...}` 错误体无法区分，默认按错误体拦下（多记录 JSONL、最后一行不带换行都正常解析）。低流量源确实“整个响应就是一条 JSON 记录”时才设 `true` |

### target（目标表）

| 字段 | 默认 | 说明 |
|---|---|---|
| `project` / `table` | - / 必填 | 目标项目（缺省用 maxcompute.project）/ 表名 |
| `pt` | ${bizdate} | 分区值模板，必须是 8 位业务日 `yyyyMMdd`（调度与 DWD 按它读取）；`--pt` 可显式覆盖成特殊分区（测试/对比/补数用，如 `test_20260921`——注意特殊分区不会被调度与 DWD 自动读到） |
| `column` | json | json 列名 |
| `comment` / `stored_as` / `lifecycle_days` | - | 建表注释 / 存储格式 / 生命周期 |
| `allow_empty` | false | 本次 0 行时是否允许写空分区 |
| `profile` | default | 使用 `profiles.<名>` |

`project` / `table` / `column` / `stored_as` 会直接拼进建表 DDL 与校验 SQL，只允许**字母/数字/下划线且不以数字开头**（与 MaxCompute 标识符规则一致）。带空格、连字符、分号的名字会拿到一条明确的配置错，而不是建表失败或注入风险。

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
2. 写库先删分区再写、整段失败自动重试（每次重试用**全新的 Tunnel 会话**，不会把上次残留的块
   一起提交），重跑幂等；单条超限记录在**删分区之前**就检查，失败时分区原样不动；
3. 分页翻完才收尾：接口把 `PageSize` 压小也能拉全，翻页中途记录路径取不到、或空页而总数没拉够
   都直接报错（不静默截断；接口总数确实不准时用 `pagination.strict: false` 放宽，仍会打警告）；
   终点字段只有第一页返回时会被记住并沿用，末页之后的空页不会误判成"翻不完"；
   终点值取历史最大值（中途变小不缩水），`-1`/`0` 这类"未知"值不当终点；
4. 写后 `count(*)` 与拉取条数双重核对；
5. 默认拒绝写 0 行（防接口异常时清空分区），要写空分区显式 `--allow-empty`；
6. 单行超过约 7MB 提前报错（MaxCompute 单列上限 8MB）；
7. 建表/删分区/校验 SQL 有超时保护（默认 600 秒，超时主动取消）；
8. 日志与异常里的 token/sign/secret 一律脱敏（形态规则 + 按配置里的密钥值精确遮蔽，
   接口把凭证写进自由文本报错时也不会漏）；
9. 429/5xx 按 `Retry-After` 退避重试，4xx 直接失败（参数/密钥问题快速暴露）；
   每次重试都重新鉴权（一次性签名如阿里云 `SignatureNonce` 不会因复用被判 400）；
   连接抖动（ConnectionError / Timeout / SSL 错误）走请求级退避重试；
   而配置/签名错误（`ConfigError`）立即失败，不做无意义的空等重试；
   4xx 与配置错误也**不会**再被 `window_retries` 整窗放大（重试多少次结果都一样）；
10. **每个作业一把运行锁**（Linux/macOS 用 flock、Windows 用 msvcrt；锁名＝文件名＋路径哈希，
    同机同一作业互斥、不同作业可并行，同名不同目录的作业文件不会互相阻塞）；
    Ctrl+C 不写库（退出码 130），并发拉取时进程立即结束、不等在飞的请求跑完
    （拉取阶段返回的 130 会在 `main` 里转成 `os._exit`——光靠 `shutdown(wait=False)`，
    解释器退出时仍会 join 工作线程；代价是本次的临时 spool 文件可能留在系统 temp）；
11. 大数据量流式落盘（本地临时 JSONL，默认系统 temp，可用 `TMPDIR` 指定）：**每个请求单元拉完就落盘，
    不跨单元累积**；单次请求单元（一天 / range 模式整个区间）的记录会先在内存里攒齐再落盘，
    峰值内存与"单个请求单元的数据量"成正比、与总天数无关（百万条级别的单日大源建议改用 range 或调大页大小）；
    拉取进度每 1000 条打一次日志；写入分批同时受行数与字节数限制（默认 1000 行 / 8MB），
    单条记录很大时也不会把内存吃满；失败时可 `--keep-spool` 保留数据文件排查。

### 退出码（调度侧判断成败）

| 码 | 含义 |
|---|---|
| 0 | 成功（含 `--dry-run`；0 行但 `--allow-empty` 时也会写空分区并返回 0） |
| 1 | 运行失败：请求失败、写库失败、写后行数对不上、0 行保护触发、`--check` 未通过；作业文件不存在、业务日格式不对等配置类问题也归这里 |
| 2 | 命令行参数问题（缺 `--job`、`--days` 不是整数等 argparse 层），**没发过请求** |
| 130 | 用户中断（Ctrl+C） |

配置类错误（作业 JSON 写错、`ConfigError`）走的是 `SystemExit`，退出码同样是 1：
凡是"重跑结果一样"的问题都归到 1，调度直接告警即可，不必按码分流重试。

## 常见问题

| 现象 | 处理 |
|---|---|
| `解析失败：找不到 records_path` | 路径写错；`--check` 会打印返回的顶层字段；接口“空对象=无数据”时加 `records_missing: "empty"` |
| JSON 接口中文变乱码 / 报“解不出来” | 源不是 UTF-8（如 GBK）：给 `request` 加 `json_encoding: "gbk"`（不配时工具不静默替换，直接报错）。若日志提示显式编码与 utf-8-sig 不一致，先核对接口真实编码；键名不同会直接报错，只有值不同则按显式编码继续并留告警 |
| `HTTP 401/403/400` | 密钥/参数问题（不重试）；检查 auth 与 params |
| `期望文件流，但接口返回了 JSON` | 文件接口的权限/参数错误（如未开通下载权限）；按提示里的 JSON 内容排查 |
| `接口返回业务错误：code=...` | 命中 `fail_if`；限流类错误给 `"retry": true` 先重试几次 |
| 大量“重试”日志 | 撞限流：降 `--workers`、调大 `pagination.delay_seconds` |
| `本次拉取 0 行...未写库` | 确认接口当天确实无数据；要写空分区加 `--allow-empty` |
| `补数（--dates / --start-date+--end-date）必须跟着 --bizdate 一起用` | 加 `--bizdate 20260920`，整段数据写进 `pt=20260920`；见上方「一次运行只写一个 pt」 |
| 配置文件报 `不是合法 JSON` 但内容看着没问题 | 多半是文件存成了「UTF-8 with BOM」；新版已自动兼容，升级后仍报错再查逗号/引号 |
| `pagination.page_size 必须是数字` / `window.days 必须是整数` | 配置里写成了带引号的字符串或带了单位，改成纯数字 |
| `表结构与框架要求不一致` | 目标表是宽表/事务表：换表名（建议 `ods_xxx_json_di`）或先 drop 重建 |
| 时区不对/跨天差一小时 | 检查 `window.date_tz`（怎么切天）与 `api_tz`（怎么传给接口）。两者都可以写 IANA 名称（如 `America/New_York`），`api_tz` 写时区名会自动处理夏令时（见上方「时区怎么配」）。数仓按美东，现有作业一律 `date_tz: America/New_York` |
| `pagination.strict=false` 的警告日志 | 接口给的 `TotalCount` 不准（翻页期间数据还在涨）；警告里会写明"已拉到多少条就收尾了" |
| `记录里有无法序列化的值 ... NaN` | 接口返回了 `NaN`/`Infinity`（非法 JSON），落库后下游取不到值，已在拉取阶段中止 |
| `环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期` | 调度传下来的业务日格式不对（应为 `YYYYMMDD`）。**不会**回退成"昨天"——那会先删再填写错分区、退出码还是 0；先 `unset bizdate` 或显式 `--bizdate` |
| Windows 报找不到时区 | `pip install tzdata` |

## 开发与测试

```bash
python -m unittest discover -s tests -v    # 481 个离线用例：不访问网络、不连数仓
pip install -e ".[dev]" && ruff check .    # 代码检查（配置在 pyproject.toml，当前 0 告警）
```

CI 在 ubuntu / windows / macos × Python 3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14
十八种组合上跑同一套用例（见 `.github/workflows/tests.yml`）。想改代码或加数据源，先看 `CONTRIBUTING.md`
——里面写了这个工具的几条"设计红线"（先删再填、宁可失败不可静默丢数、密钥不进日志……），
不少看着顺手的改法会踩到它们。

模块结构（都在 `api2ods/` 包内，每个文件头部有职责说明）：

| 模块 | 职责 |
|---|---|
| `cli.py` | 命令行入口 / 体检 / 同步主流程 |
| `init_wizard.py` | `--init` 交互式配置生成 |
| `config.py` | 配置加载/占位符/校验/目标解析 |
| `dates.py` | 日期与时区、窗口参数生成 |
| `auth.py` | 八种鉴权（含阿里云 RPC 签名） |
| `http.py` | 请求、重试、Retry-After、业务错误判定 |
| `parsers.py` | JSON 路径 / CSV / ZIP / JSONL 解析 |
| `fetch.py` | 请求单元、翻页、并发、整窗重试 |
| `spool.py` | 流式落盘（大数据量不占内存） |
| `mc.py` | MaxCompute：建表校验 / 先删再填 / 行数核对 |
| `utils.py` | 日志、运行锁、脱敏、重试工具 |

## License

MIT（见 `LICENSE`）。
