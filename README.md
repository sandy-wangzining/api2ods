# api2ods

[![tests](https://github.com/sandy-wangzining/api2ods/actions/workflows/tests.yml/badge.svg)](https://github.com/sandy-wangzining/api2ods/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://github.com/sandy-wangzining/api2ods/blob/main/pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/sandy-wangzining/api2ods/blob/main/LICENSE)

**通用 REST API → MaxCompute ODS 同步工具**：把任意 HTTP 接口返回的 JSON / CSV / ZIP 记录
**原样**写进 MaxCompute ODS（每条记录一行 JSON + `pt` 业务日分区），DWD 层再解 JSON、
按主键取最新。接一个新数据源 ≈ 写一份配置文件。

> REST API → MaxCompute ODS sync tool. 同系列：[feishu2ods](https://github.com/sandy-wangzining/feishu2ods)（飞书多维表格） · [sftp2ods](https://github.com/sandy-wangzining/sftp2ods)（SFTP 文件）

**最短路径：** 安装 → `--init` 生成配置 → `--check` 体检 → `--dry-run` 试跑 → 正式同步。

**导航：** [快速开始](#快速开始) · [它是怎么工作的](#它是怎么工作的) · [配置参考](#配置参考) · [行为与保护](#行为与保护) · [常见问题](#常见问题)

适合把有 REST API 的数据源按天/按窗口原样落到 MaxCompute ODS；不适合增量 CDC、实时同步或非 MaxCompute 数仓。

- 配置驱动：地址、参数、鉴权、分页、时间窗口、目标表都在一个 JSON 里
- 写入幂等：每次按窗口拉数，**暂存校验后原子发布** `pt` 分区，重复跑/补数不会重复
- 自动建表：目标表不存在时按 `json string + pt string` 建表，存在则校验结构
- 少见即用的健壮性：请求重试+限流退避、业务错误判定、大数据量分单元落盘、
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
| 自定义 md5 签名 + POST JSON + 无总数短页分页 | XMP（Mobvista）Open API |

## 独立使用与部署前准备

本项目可独立从 GitHub 下载并安装，不需要其它 ODS 项目或团队内部服务。支持 Windows、macOS、Linux；使用前需要源端读取权限和目标 MaxCompute 项目的凭证与读写权限。

- Python 3.9+；依赖由 `pip install .` 自动安装。建议为工具创建虚拟环境，或使用 pipx。
- 请按目标项目的真实地域设置 `maxcompute.endpoint`（可用 `--endpoint` 覆盖）。默认 us-west-1 仅适用于该地域，不能用于任意项目。
- 业务日默认按 `America/New_York` 的昨天计算；可用 `ODS_BIZDATE_TZ=Asia/Shanghai`、`UTC` 等 IANA 时区名覆盖，也可显式传 `--bizdate`。数据窗口/源文件时区按各自配置设置，不必采用同一地域。
- 从 `jobs/*.example.json` 复制配置并填入自己的源地址、项目和凭证；真实配置不要提交到 Git。首次接入先运行 `--check` 和 `--dry-run`。

```bash
git clone https://github.com/sandy-wangzining/api2ods.git
cd api2ods
python -m venv .venv
# Windows PowerShell：.\.venv\Scripts\python.exe -m pip install .
.venv/bin/python -m pip install .
```

## 安装

```bash
# 方式一：pip（建议放虚拟环境）
python3 -m venv venv
./venv/bin/pip install .                                    # 在源码目录本地安装
./venv/bin/pip install api2ods                              # 发布到 PyPI 后
api2ods --version

# 方式二：pipx（全局命令行工具，隔离环境）
pipx install .

# 方式三：直接从 GitHub 安装
pipx install "git+https://github.com/sandy-wangzining/api2ods.git"

# 方式四：源码直接跑（不安装）
python -m api2ods --job jobs/xxx.json --check
```

Windows / macOS / Linux 通用；Windows 会自动带上 `tzdata` 依赖（时区数据）。
没准备好 MaxCompute / 飞书凭证时，先跑 `python -m unittest discover -s tests -v` 验证安装（离线，不连任何外部服务）。

## 快速开始

```bash
# 1) 生成一份作业配置（交互式问答，密钥直接写进文件、输入时不回显；也可复制 jobs/_template_simple.example.json 手工改）
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
api2ods --job jobs/my_api.json --bizdate ${bizdate} --no-notify                 # 不发飞书通知（新增字段提醒等）

# 补数（闭区间）：必须跟着 --bizdate（整段写进 pt=<bizdate>），见下方「一次运行只写一个 pt」
api2ods --job jobs/my_api.json --bizdate 20260920 --start-date 2026-07-01 --end-date 2026-09-20
api2ods --job jobs/my_api.json --bizdate 20260920 --dates 2026-09-01,2026-09-05   # 补零散日期

# 补数月份多的时候并发拉（--workers 只并行网络等待，写库仍是单线程，按各单元完成顺序落盘）
api2ods --job jobs/my_api.json --bizdate 20260920 --start-date 2026-07-01 --end-date 2026-09-20 --workers 3
```

> `--workers` 范围 1~32（默认 1）；回补历史建议 2~4。并发只并行网络等待，写库仍是单线程。

> **一次运行只写一个 `pt`**（这是设计：一个分区＝一次运行的一份快照，DWD 再按源数据里的
> 日期字段重新分区）。`--dates` / `--start-date`+`--end-date` 只决定"拉哪些天"，
> **不决定"写进哪个分区"**，所以补数时不给业务日就会把整段窗口全写进默认业务日分区
> （`pt=<默认业务日，默认美东昨天>`）、并先删后填覆盖掉调度刚写进去的数据。为此补数**必须**跟着 `--bizdate`：
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
  │     （按页写入单元 Spool，完整成功后分批并入主 Spool）
  │  ③ 任一单元三次重试仍失败 → 本次放弃写库（旧分区保持原样，重跑即可）
  ▼
MaxCompute ODS：<json 列> + pt 分区（普通表）
  │  ④ 自动建表/校验结构 → 独立暂存表 → Tunnel 写入与核对 → INSERT OVERWRITE 发布
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
| `maxcompute` | 是* | 目标项目与凭证：`project` / `endpoint`（默认 us-west-1，https）/ `access_key_id`+`access_key_secret`（可简写 `ak`/`sk`） |
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
| `max_response_bytes` | 1073741824 | 单次响应体上限（解压后字节，默认 1GiB）；导出大文件时按需调大，超过直接报错 |
| `headers` / `params` | {} | 静态请求头 / 参数 |
| `params_in` | query | `query`（默认）/ `headers`（参数全放请求头，部分文件接口要求） |
| `auth` | none | 见下表（`bearer` 最常用） |
| `records_path` | 空 | 记录数组路径（如 `data.list`）；留空=整个返回就是数组 |
| `response_type` | json | `json` / `bytes`（文件流，配 `parse`，不支持分页） |
| `json_encoding` | 空 | JSON 接口不是 UTF-8（如 GBK）时显式指定编码；不写则按 响应头声明的 charset → UTF-8(sig) 严格解码，解不出直接报错（不静默变乱码写库）。声明编码解不出 JSON、UTF-8 能解出时自动改用 UTF-8；两者都能解出但内容不同会拒绝猜测（报配置错，可用 `json_encoding` 明确指定）；显式编码与 UTF-8 键名不同会报配置错，键名一致、只有值不同时按显式编码处理并告警 |
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
| `mode` | per_day | `per_day` 每天一次请求（推荐）/ `range` 整区间一次。会先去掉首尾空白再匹配、只认这两个值——拼错或带空格（`"range "`）直接报错，不会静默按 `per_day` 跑 |
| `days` | 1 | 回拉天数（含基准日）；`--days` 可覆盖 |
| `date_tz` | America/New_York | 窗口起止（“最近 N 天”）和 `${today}` 占位符按哪个时区切；不传业务日时的默认业务日取 `ODS_BIZDATE_TZ` 时区的昨天（默认美东），不受此字段影响 |
| `api_tz` | +08:00 | 传给接口的时间按哪个时区算：固定偏移（`+08:00` / `+0800` / `+08`）或时区名（`America/New_York`，自动处理夏令时） |
| `pad_hours` | 0 | 窗口前后多拉几小时（防边界丢数），取值 0~24。**只要 >0，相邻两天的窗口就会重合 2×pad_hours 小时**（同一笔数据拉两遍，靠 DWD 按主键去重兜底）。`format` 只到日期时本项**被忽略并告警**：日期参数减 pad 不是多拉一段，而是把日期整体顶到前一天。写布尔（YAML 里当开关写 `true`）与 NaN/超范围都会报错，不会被当成 1 小时 |
| `start_param` / `end_param` | startTime / endTime | 起止时间参数名。**两个都不写**才启用默认；自定义了 `start_param` 就不补 `end_param`（只要开始时间只写 `start_param`，不要结束时间写 `"end_param": null`） |
| `format` | %Y-%m-%d %H:%M:%S | 时间格式；`unix`=秒、`unix_ms`=毫秒；`%s`（epoch 秒）与 `%P`（am/pm）可在格式串任意位置用（自己实现，跨平台一致；`%s` 单用返回纯数字）。**只到日期（无时分秒）时按 `date_tz` 直接输出那一天，不做时区换算**。格式串至少要含一个真实时间指令：`"unixtime"`（unix 拼错）、纯 `%%` 转义（`"%%Y-%%m-%%d"`）都会在配置阶段报错——它们会被 strftime 原样输出成参数值、静默查不到任何数据 |
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
> - `date_tz` 没按源方时区配置（默认美东）时，装进 `pt=20260920` 的会是「美东 9/19 半天 + 9/20 半天」，
>   数据不缺但跟源方的账期对不上；DWD 层按源数据里的日期字段重新分区可以规避这个影响；
> - `format` 只到日期时（`%Y-%m-%d` / `%Y%m%d`）不参与时区换算，直接就是那一天的日期。
>
> **统一口径**：不带 `--bizdate` 且没有环境变量时，默认业务日统一取业务日时区的昨天
> （默认 `America/New_York`，可用 `ODS_BIZDATE_TZ` 覆盖；三个 ODS 工具共用公共运行层实现）；`date_tz` 只负责窗口起止怎么切成“一天”，
> 不改变默认业务日。`pt` 永远是业务日 `bizdate` 本身。`format` 只到日期时（阿里云 `BillingDate`），
> `date_tz` **完全不影响**发给接口的参数；`format` 带时分秒 / `unix` 时（DeepSeek），`date_tz`
> 决定窗口的实际起止，换时区会改变每个 `pt` 里装的是哪段时间（同一 `pt` 的内容整体平移，
> 不影响 `count` 校验）。本仓库作业按源方时区显式配置 `date_tz`
> （Onerway 美东、阿里云 UTC、DeepSeek/XMP 上海）。

### pagination（分页，不写=单页）

| 字段 | 默认 | 说明 |
|---|---|---|
| `type` | 自动推断 | `none` / `page` / `cursor`；不写时：有 `cursor_path`→cursor、有 `total_*` 或 `stop_when_short` 或 `page_param`→page。**只配了 `size_param`/`page_size` 会告警**（没有翻页终点，只会请求一次） |
| `page_param` / `size_param` / `page_size` / `param_as_string` | page / size / 100 / false | 页码/游标分页用；`size_param` 写 `null` 表示不带页大小参数（接口不认 `size` 时）。**`page_param` 不能与 `size_param` 同名**：同名时页大小会覆盖页码，接口只回同一页、重复行会静默入库 |
| `total_pages_path` / `total_items_path` | - | page 分页至少给一个（翻页终点）；两个都给时以条数终点为准（页数说翻完、条数没拉够就继续翻，宁可多翻一页也不静默少拉）。**只认正数**（`-1`/`0` 这类"未知"哨兵会被忽略），且终点值单调不减（某页只回本页条数也不会提前收尾）。cursor 分页也可以用 `total_items_path` 做兜底：游标提前结束但条数没拉够时会报错（防"游标字段写错 → 只拉第一页"） |
| `stop_when_short` | false | 接口不返回总数时用：**本页条数 < `page_size` 即判末页**（含首屏空页=窗口无数据、整页后空页=没有下一页）。与 `total_*` **互斥**、只对 `type=page` 生效，配置阶段报错兜底。假设接口除末页外按请求的 `page_size` 返回 |
| `strict` | true | 严格模式：空页但 `TotalCount` 没拉够 → 判失败（防静默截断）。接口总数不准才设 `false`，此时按已拉到的收尾并打警告。终点字段只在第一页返回时会被记住并沿用（末页之后的空页不再误判为"无法确认翻完"） |
| `cursor_param` / `cursor_path` / `cursor_start` | 游标分页；`cursor_start` 写 `""` 表示首页就带上空的游标参数（默认首页不带）。**建议同时配 `total_items_path`**：没配时游标字段写错会被当成"翻完了"只拉第一页（会告警提示）。**游标未推进检测**：接口原样回显游标（或 `cursor_path` 指到了恒定字段）时第二页直接报错，不会翻满 `max_pages` 把同一页重复拉爆 |
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

### notify（可选，飞书通知）

| 字段 | 默认 | 说明 |
|---|---|---|
| `webhook` | - | 飞书群机器人地址（发「接口出现新增字段」提醒）；支持**字符串或字符串数组**（多个群同时告警）；也可写在共享 `--config` 文件（默认工具目录 `config.json`）的 `notify` 块里，作业级同键覆盖 |
| `enabled` | true | false 时关闭该作业的通知（`--no-notify` 全局关闭） |

> **字段漂移检测**：每次成功运行观察到的记录字段会存成快照（作业同目录 `.field-state/` 下），
> 下次运行发现**新增字段**时发一条飞书提醒（只提醒、不阻塞：ODS 是裸 json 列，新字段原样包含、
> 无需改表）。快照只在**真实写库成功后**更新；`--dry-run` 只对比不落盘。想使用新字段时，
> 更新 DWD 的 `get_json_object` 提取逻辑即可。

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
   | 自定义 md5 签名 + 无总数短页分页 | `jobs/xmp.example.json` |
   | 不想研究字段 | 直接 `api2ods --init`，按问答生成 |

3. **改三处**：`maxcompute`（AK/SK）、`request`（地址/鉴权/records_path）、`target.table`；
4. **跑 `--check`**：它会真实请求一次并打印返回顶层字段，帮你确认 `records_path`；
5. **跑 `--days 1 --dry-run`** 看条数对不对，然后就可以配调度了。

## 行为与保护

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
7. 建表 / 校验 / **分区增删** SQL 都有超时保护（默认 600 秒，可用 `--sql-timeout` 调整，0=不限制、
   负数在参数解析阶段报错退出码 2；超时主动取消）。分区增删走 `alter table … drop/add if [not]
   exists partition (pt='…')` 这条带超时的 DDL，不再用同步不限时的 pyodps 表 API——后者在云端
   元数据操作卡住时会让任务一直挂起、**占着运行锁**把后续调度顶掉（与 sftp2ods / feishu2ods 同口径）；
8. 日志与异常里的 token/sign/secret 一律脱敏：形态规则 + 按配置里的密钥值精确遮蔽
   （凭证的明文与其 URL 编码形态 `quote` / `quote_plus`、以及把 `-` 也编码的激进编码器形态都替；含中文的密钥同样覆盖），接口把凭证写进自由文本报错时也不会漏；
9. 429/5xx 按 `Retry-After` 退避重试，4xx 直接失败（参数/密钥问题快速暴露）；
   每次重试都重新鉴权（一次性签名如阿里云 `SignatureNonce` 不会因复用被判 400）；
   连接抖动（ConnectionError / Timeout / SSL 错误）走请求级退避重试；
   而配置/签名错误（`ConfigError`）立即失败，不做无意义的空等重试；
   4xx 与配置错误也**不会**再被 `window_retries` 整窗放大（重试多少次结果都一样）；
10. **每个作业一把运行锁**（Linux/macOS 用 flock、Windows 用 msvcrt；锁名＝文件名＋路径哈希，
    同机同一作业互斥、不同作业可并行，同名不同目录的作业文件不会互相阻塞）；
    锁目录默认在工具目录 `.run-locks/`（不可写时退回当前用户的私有临时目录）；同一作业会以不同身份或
    不同 `TMPDIR` 跑（root 手动补数与普通用户调度混用）时，用环境变量 `API2ODS_LOCK_DIR`
    把锁目录钉在固定位置，并按[多用户权限说明](docs/shared-runtime.md#多用户共享锁目录)配置共用组。
    跨主机互斥取决于共享文件系统的锁支持；不支持文件锁时默认拒绝运行；
    Ctrl+C 不写库（退出码 130），并发拉取时进程立即结束、不等在飞的请求跑完
    （拉取阶段返回的 130 会在 `main` 里转成 `os._exit`——光靠 `shutdown(wait=False)`，
    解释器退出时仍会 join 工作线程；代价是本次的临时 spool 文件可能留在系统 temp）；
11. 大数据量流式落盘（本地临时 JSONL，默认系统 temp，可用 `TMPDIR` 指定）：**每个请求单元拉完就落盘，
    不跨单元累积**；分页结果先逐页写入单元私有暂存文件，整单元拉取成功后按行数/字节预算
    分批并入主 Spool。失败尝试的文件会清理，重试不会混入半成品；取消或退出也会回收未交付结果。
    峰值内存与 workers、单页响应和回调批次有关，不再随单元全部页数线性增长；单次 HTTP 响应
    仍受 max_response_bytes 限制，超大单页仍需调小 page_size 或拆分请求窗口；
    拉取进度每 1000 条打一次日志；写入分批同时受行数与字节数限制（默认 1000 行 / 8MB），
    单条超限记录会在写库前拒绝；`--keep-spool` 可保留本次落盘的临时文件排查（成功失败都保留，
    不加时运行结束自动清理）。
12. **接口新增字段只提醒、不阻塞**：与上次成功运行观察到的字段快照对比，出现新字段时发一条
    飞书提醒（列名 + 处理建议；一次运行最多一条，`--no-notify` / `notify.enabled:false` 可关）。
    数据本身不受影响——json 列原样落库、新字段自动包含；要分析新字段时更新 DWD 提取逻辑即可。
    快照更新只在写库成功后（与"写后核对"同一口径），失败/中断/`--dry-run` 都不会推进快照，
    保证提醒不丢。快照文件在作业同目录 `.field-state/`（读写失败只记日志，不影响主流程与退出码）。

13. **JSON 标量错误体不会当成"空数据"**：HTTP 200 下返回 `"rate limit exceeded"` / `500` /
    `null` 这类单行响应（文件流路径）时直接报错——原来会被当"只有表头的 CSV"解析出 0 行、
    按零数据静默收尾（宁失败勿写错）；
14. **凭证不全不会静默换身份**：`maxcompute` 的 AK/SK 只填一半（典型：键名拼错成
    `access_key_secect`）直接报错、不会警告后回退到环境变量/本机 aliyun CLI；显式指定的
    `--mc-profile` / `maxcompute.profile` 找不到时同样报错、不会改用其它 profile 的 AK；
    确实想用环境变量/CLI 时把 AK/SK 整对留空即可；
15. **运行锁按规范化路径计算**：同一作业用相对/绝对/带 `..` 的写法落到同一把锁（否则两份
    调度各拿一把锁、互斥静默失效、同删同写一个分区）。

### 退出码（调度侧判断成败）

| 码 | 含义 |
|---|---|
| 0 | 成功（`--dry-run` 拉到数据时同样返回 0；0 行但 `--allow-empty` 时也会写空分区并返回 0） |
| 1 | 运行失败：请求失败、写库失败、写后行数对不上、0 行保护触发（`--dry-run` 按同一判定，0 行即退出非 0）、`--check` 未通过；作业文件不存在、业务日格式不对等配置类问题也归这里 |
| 2 | 命令行参数问题（缺 `--job`、`--days` 不是整数、`--sql-timeout` 为负等 argparse 层），**没发过请求** |
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
| `--dry-run` 显示 0 行并退出 1 | 有意的：dry-run 与正式跑同一套判定（0 行且未 `--allow-empty` 即非 0），当预检用时能提前发现坏配置 |
| `游标未推进（接口回显了同一个游标）` | `cursor_path` 指到了恒定字段（如 `total`/`page_size`）：改成真正的下一页游标字段。接口确实原样回显游标时，这个接口不适合游标翻页，改用 page + `total_*` |
| `指定的 aliyun CLI profile「x」不存在或没有 AK/SK` | 拼写核对 `~/.aliyun/config.json` 里的 profile 名；有意换来源就清空 `--mc-profile`/`profile`——工具不会自动改用别的 profile |
| `--start-date/--end-date 区间过大` | 上限 3660 天（≈10 年）：日期大概率打错了（如 end 写成下个世纪）；确实要超长补数请分段执行 |
| `响应是一个 JSON 标量（疑似错误体）` | 接口在 200 状态下返回了 `"错误消息"`/数字/null（常见于限流/网关异常）：先确认接口行为；确属数据再调整 `response_type` / `parse.format` |
| `补数（--dates / --start-date+--end-date）必须跟着 --bizdate 一起用` | 加 `--bizdate 20260920`，整段数据写进 `pt=20260920`；见上方「一次运行只写一个 pt」 |
| 配置文件报 `不是合法 JSON` 但内容看着没问题 | 多半是文件存成了「UTF-8 with BOM」；新版已自动兼容，升级后仍报错再查逗号/引号 |
| `pagination.page_size 必须是数字` / `window.days 必须是整数` | 配置里写成了带引号的字符串或带了单位，改成纯数字 |
| `表结构与框架要求不一致` | 目标表是宽表/事务表：换表名（建议 `ods_xxx_json_di`）或先 drop 重建 |
| 时区不对/跨天差一小时 | 检查 `window.date_tz`（怎么切天）与 `api_tz`（怎么传给接口）。两者都可以写 IANA 名称（如 `America/New_York`），`api_tz` 写时区名会自动处理夏令时（见上方「时区怎么配」）。请按数据源的业务日口径设置 `date_tz`，例如 `America/New_York`、`Asia/Shanghai` 或 `UTC` |
| `pagination.strict=false` 的警告日志 | 接口给的 `TotalCount` 不准（翻页期间数据还在涨）；警告里会写明"已拉到多少条就收尾了" |
| `记录里有无法序列化的值 ... NaN` | 接口返回了 `NaN`/`Infinity`（非法 JSON），落库后下游取不到值，已在拉取阶段中止 |
| `环境变量 bizdate/SKYNET_BIZDATE 的值不是合法日期` | 调度传下来的业务日格式不对（应为 `YYYYMMDD`）。**不会**回退成"昨天"——那会暂存校验后原子发布写错分区、退出码还是 0；先 `unset bizdate` 或显式 `--bizdate` |
| Windows 报找不到时区 | `pip install tzdata` |

## 开发与测试

```bash
python -m pip install -e ".[dev]"          # 开发依赖（ruff）
python -m unittest discover -s tests -v    # 离线用例：不访问网络、不连数仓
ruff check .                               # 代码检查
ruff format --check .                      # 格式检查（需要时先跑 ruff format .）
```

`review_ignore.json` 是代码审查工具的误报抑制名单，不参与运行。测试按模块拆分（`tests/test_*.py`），公共替身在 `tests/_helpers.py`。

CI 在 ubuntu / windows / macos × Python 3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14
十八种组合上跑同一套用例（见 `.github/workflows/tests.yml`）。想改代码或加数据源，先看 `CONTRIBUTING.md`
——里面写了这个工具的几条"设计红线"（暂存校验后原子发布、宁可失败不可静默丢数、密钥不进日志……），
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
| `spool.py` | 按单元落盘与写入分批 |
| `mc.py` | MaxCompute：建表校验 / 暂存校验后原子发布 / 行数核对 |
| `notify.py` | 飞书群卡片通知（新增字段提醒等，发送失败不改退出码） |
| `fieldwatch.py` | 字段漂移检测（快照对比，只提醒不阻塞入库） |
| `utils.py` | 日志、运行锁、脱敏、重试工具 |

## License

MIT（见 [LICENSE](LICENSE)）。

## 公共源码维护

默认临时锁目录在 POSIX 上校验属主和写权限，拒绝软链或公共可写目录；无安全可写目录时停止运行。
多身份或多环境运行同一作业时，请用对应的 `*_LOCK_DIR` 固定同一目录，保持互斥。

三个项目保持独立安装；SQL 等待、告警和部分脱敏工具维护一份源码并同步，详见
[公共源码说明](docs/shared-runtime.md)。贡献前请运行 `python tools/sync_common.py --check`。

## 数值精度、分页与并发保护

JSON 和 JSONL 中的十进制数字使用 Decimal 解析，并通过 simplejson 保留为 JSON 数字写入；大整数、高精度金额和极小非零数不会先转换成二进制浮点数。输出的空白、指数大小写等文本形式可以变化，数字的值和类型保持不变。`simplejson>=4.2` 已列入安装依赖，源码运行时也需安装 requirements.txt。

页码分页默认检测整页重复；即使累计条数达到 total，也不能靠重复第一页冒充拉取完整。发现重复立即失败，本次不写库。没有唯一标识、且源数据确实允许完全相同页面的接口，可以显式配置：

```json
"pagination": {
  "type": "page",
  "total_items_path": "data.totalCount",
  "allow_repeated_pages": true
}
```

该开关只用于已确认的源端语义，不会替接口补齐缺失数据。检测比较整页内容并忽略对象键顺序；它不代替业务主键去重，也不保证上游在分页期间被编辑时仍能获得一致快照。

正式运行在作业锁之外，还会取得按 `project.table` 归一化的目标表锁。同一用户的三个 ODS 工具、不同虚拟环境和不同配置文件写同一张表时互斥；不同表可并行，争用锁的任务以失败退出而非无限等待。它仍不能消除提交响应丢失后仍需核对云端实例与正式分区的要求。

默认目标锁位于系统临时目录的 `ods-target-locks[-UID]`。多用户、不同 TMPDIR 或容器运行同一目标时，用 `ODS_TARGET_LOCK_DIR` 指向同一固定目录，并确认文件系统提供有效的文件锁。仅设置共享路径不能保证跨主机互斥；推荐固定调度机器。升级前停止旧进程，并更新所有可能写同一目标的工具。原有 `*_LOCK_DIR` 继续只控制各工具的作业锁。

POSIX 跨用户部署需将共享目录设为共用组所有的 `2770` setgid 目录，运行锁会继承组并以 `0660` 供组内账号复用；默认私有目录仍使用 `0600`。Windows 需配置继承的读写 ACL。完整示例和旧锁权限迁移步骤见[多用户共享锁目录](docs/shared-runtime.md#多用户共享锁目录)。


## 原子发布与升级

正式分区通过单条 `INSERT OVERWRITE ... SELECT <数据列>` 发布。在暂存上传和行数核对完成前，
旧快照保持可读；Tunnel、暂存 DDL 或校验失败不会提前删除正式分区。发布请求失败时可能已经提交，
应核对云端实例和正式分区；重跑会幂等覆盖，不会叠加数据。

每次写分区会创建独立的 `tmp_api2ods_<随机 ID>` 暂存表，配置 `lifecycle 1` 和
`lifecycle.deletemeta=true`。成功后尽力删除暂存表；失败或清理异常时保留供排查，再由
MaxCompute 生命周期任务异步回收分区和空表元数据。

升级账号需具备暂存表创建、读写、查询和删除权限，以及正式分区的发布权限。暂存存储与发布 SQL
会产生费用，请按项目授权和预算部署。升级前停止旧写入进程，更新所有写同一目标的工具，
并继续使用统一目标锁目录；该锁不提供跨主机的分布式提交保证。
