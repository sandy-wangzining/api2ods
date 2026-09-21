# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.1.4] - 2026-09-22

### 修复（第四轮全文复审；每条都有"改之前会失败"的回归用例）

**会静默写坏 / 丢数据**

- **CSV 行边界**：`skip_rows` / `skip_until` 用 `str.splitlines()` 切行，比 `csv.reader` 多认
  `\x0b \x0c \x1c \x1d \x1e \x85` 等 6 种字符——报表导出里的分页符会让"第几行"两边错位，
  表头错位、列名全错还照样成功；JSONL 里合法的 U+2028（工具自己 dump 的记录就有）会被劈成两半
- **CSV 首行是空行**：原来静默按 0 行处理（配合 `target.allow_empty` 会先删再填清空分区）；
  现在"有内容却解析不出表头"直接报错
- **`records_path` 命中空对象**（`Items: {}`）：原来包成一条全 NULL 的假记录写进 ODS、
  分页时还会翻到 `max_pages`；现在按空结果处理
- **JSON 错误体防呆**：JSON 数组错误体与含 NaN/被截断的错误体现在会报错；同时
  `parse.format=jsonl` 的单条记录文件不再被误判成错误体（低流量源原来每天必失败）
- **`--workers` 并发内存**：future 一直持有已落盘单元的 records 直到 fetch_all 返回，
  回补时全窗口数据驻留内存（打破"峰值内存=单个请求单元"的口径）；现在落盘后立即释放
- **GBK 等非 UTF-8 的 JSON 接口**：原来固定 UTF-8 + `errors="replace"`，中文静默变 U+FFFD
  写库；现在按 `request.json_encoding` → UTF-8 → 响应头声明的 charset 严格解码，
  解不出直接报错（新增配置项 `request.json_encoding`）
- **`%R` / `%r` / `%s` 格式误判**：`format: "%Y-%m-%d %R"` 被当成纯日期格式，窗口塌成
  零长度（start == end）且跳过时区换算；`_TIME_TOKENS` 补全
- **默认业务日算两遍**：main 与 `resolve_days` 各读一次时钟，跨零点时 `pt` 与"拉哪几天"
  错开一天（先删再填写错分区）；现在 main 算好的业务日透传给 `resolve_days`

**密钥不进日志（红线 3）**

- **驼峰字段名漏网**：切词正则的驼峰分支在 `lower()` 之后是死代码——`signStr` / `authKey`
  明文进日志，而 `sign_str` 却被脱敏；现在按原大小写切词
- **`Authorization: <scheme> <凭证>` 只遮 scheme**：`Token` / `ApiKey` 与短 Bearer 值的
  凭证明文留下；敏感头现在整行遮掉
- **URL 编码后的密钥不脱敏**（docstring 声称覆盖的形态）：解码后能识别出密钥就整段替换
- **profile 报错回显明文 AK/SK**：`target.profile` 写成对象时报错把整段密钥打进日志；
  所有配置回显统一过 `redact()`
- **`--init` 粘贴的 URL 带 token**：提示行原文进日志文件；现在过 `redact`

**配置校验与错误归位**

- `request.auth` 写成字符串/数组 → 裸 `AttributeError`；现在给中文报错（唯一漏网的块）
- `window.extra_params` 写成字符串 → 裸 `ValueError`；值写 `null` → 静默发出 `"None"`
- `window.format` 写错（如 `unixms`）→ 字面量直发接口、`pad_hours` 被静默忽略；
  现在报错并列出可用写法（unix 家族大小写归一）
- `window.pad_hours: NaN` 绕过 0~24 校验 → 裸 `ValueError`
- `window.days: 0` 原来静默变 1 天；补数模式下 `--days` 被静默忽略（现在打提示）
- 占位符：未闭合/空键（`${secrets.token`、`${}`）原样发出；字典的**键**不替换；
  `--config` 文件自己的 `maxcompute` 不参与 `${secrets.*}` 替换——三处都修
- `request.timeout_seconds <= 0` 无校验：被当网络抖动白退避（单次 fetch_all 约 24 分钟）
- `Retry-After: NaN` 夹取失效 → `time.sleep(nan)` 抛错、429 重试链一个请求都没重试
- `fail_if` 数值比较：`0.0` 与 `0`、`200.0` 与 `200` 被当成不等（equals 方向漏判会放行
  错误数据、not_equals 方向误杀）；现在数字/数字样字符串统一按 float 比
- 并发模式下配置类错误（`ConfigError` / `SystemExit`）不走 `os._exit`：解释器退出阶段仍要
  join 在飞请求（实测进程耗时随在飞请求线性增长）；现在同样硬退出
- 运行锁探测文件用共享名 `.probe`：并发启动可能因 `FileNotFoundError` "静默"漂移到备用
  目录，同一作业两个实例拿到两把锁（互斥失效）；改用 `mkstemp` 唯一名
- 缺 `--job` 提前 `return 2` 时日志 sink 不摘：同进程再次调用 `main` 会继续写旧日志文件
- `--init`：选"页码分页"后再选"文件流"会生成过不了自身校验的配置（文件流不支持分页）

**文档**

- README 退出码表修正：作业文件不存在/业务日格式不对实际是 1（原来写 2）
- 新增 `request.json_encoding`（README 配置表 + 完整模板同步）
- 用例数勘误（283 → 370）；`ruff check .` 恢复 0 告警

## [2.1.3] - 2026-09-21

### 修复（全文复审，三轮）

**写库与数据完整性**（前两条会静默写坏数据）

- **补数覆盖正式分区**：`--dates` / `--start-date`+`--end-date` 只决定"拉哪些天"、不决定
  "写进哪个分区"，整段数据原本会落进默认业务日分区并先删后填。现在补数必须跟着
  `--bizdate 20260920`（整段写进 `pt=20260920`，与调度口径一致），否则直接报错退出
- **分区重复行**：写入失败重试复用了上一次的 Tunnel 上传会话，`close()` 把「上次残留的块 +
  本轮全量」一起提交，而行数校验照样通过。现在每次写入都用全新会话（`reopen=True`）
- **超限记录先删分区再失败**：单条 > 7MB 永远写不进去，检查却在 `delete_partition` 之后，
  等于白丢一天数据；现在检查前置，失败时分区原样不动
- **分页静默丢数**：翻页终点改用「已累计条数」判定（原来按 `页码×page_size` 估算，接口把
  PageSize 压小时会提前收尾）；翻页中途 `records_path` 取不到、或空页而总数没拉够，都直接报错
  （接口总数确实不准时用 `pagination.strict: false` 放宽，仍会打警告留痕）
- **终点字段读不出数字被当成"翻完了"**：改为按"无法确认翻完"处理，不静默少数据
- **`NaN` / `Infinity`**：不是合法 JSON，落库后下游 `get_json_object` 取不到值；解析与落盘阶段都报错
- **解析阶段不再静默丢字段**：JSONL 非对象行、CSV 列数多于表头、表头重复列名、
  ZIP 多文件又不写 `entry_contains`，一律报错（`allow_multi_entry: true` 可显式放开）；
  CSV 单字段上限从 128KB 放宽到 7MB（报表类文件的长文本列会被卡住）
- 写入重试时行数计数不清零，会拿累加值去核对、报"行数异常"假失败
- 拉取统计改为先落盘再计数（磁盘满时不再显示"拉取成功"）；失败分支的 `--keep-spool` 生效

**请求与鉴权**

- **重试复用签名 nonce**：阿里云 RPC 重试必判 400（`SignatureNonceUsed`），一次网络抖动就击穿
  整个请求级重试；现在每次尝试都重新鉴权
- **鉴权/配置类错误不再当网络抖动重试**（新增 `ConfigError`）：原来要白等约 225 秒
- **`fail_if` 命中可重试业务错误**：走 `RetryLater`（重试行为不变，但不再把接口返回原文打进日志）
- **`Retry-After`**：HTTP-date（RFC 7231）也认；负值夹到 0（原来会让 `time.sleep` 抛错、白退避几轮）
- **游标分页把 `0` / `false` 当成"没有下一页"**：只认 `None` 与空串为结束，避免提前收尾丢数据
- **UTF-8 BOM**：作业文件、接口 JSON、文件接口的"返回了 JSON 错误体"检测，三处都兼容
- `count_partition` 的 pt 值拼进 SQL 前转义；目标表列名比较忽略大小写；`pt` 校验不再放过结尾换行

**配置与命令行**

- **数值配置项写错给字段名报错**（原来是裸 traceback，或被当成"接口抖动"整窗重试）：
  `page_size` / `days` / `max_pages` / `pad_hours` / `lifecycle_days` / `retry_times` /
  `delay_seconds` / `window_retries` / `skip_rows`
- **`parse.strict_encoding` / `parse.allow_multi_entry` 不在配置白名单里**：功能早已实现、
  文档也写了，一用却告警"不是已知配置项"，容易被当成写错删掉
- **Ctrl+C 退不掉**（`--workers>1`）：线程池退出要 join 在飞请求，最长等到单请求超时；
  现在拉取/写库阶段的 `return 130` 在 `main` 里转成 `os._exit(130)`，进程立即结束
  （只剩临时 spool 文件可能留在系统 temp，正常退出不受影响）
- **`--init`**：游标路径留空 / 非法编号给提示（原来静默退化成只拉一页）；"回拉几天"填非数字
  不再崩；向导输出写进 `--log-file`（回答不入日志，避免 token/AK/SK 被抄一份到磁盘）；
  GBK 控制台不再抛 `UnicodeEncodeError`
- **运行锁按文件名命名**：`jobs/a/api.json` 与 `jobs/b/api.json` 会互相阻塞，现在锁名带路径哈希
- **`--check`**：不再改游标分页的页大小、页码分页被"最小页大小"拒掉时按配置值重试一次；
  校验阶段的告警改为挂在 job 上（原来是模块级缓存，会串到下一次运行）；只读体检不受补数护栏限制
- 日志句柄的关闭收进 `remove_log_sink`（同一进程里多次调用 `main` 会向已关闭的文件写日志）

**时间窗口与文档**

- `window.api_tz` 支持 IANA 时区名（如 `America/New_York`，自动处理夏令时）；固定偏移支持
  `+08` / `+0800` / `+08:00`，越界值报错
- 纯日期格式（`format` 不含时分秒）不再做时区换算，避免日期被顶到前一天/后一天
- `pad_hours` 限定 0~24；`${today}` / `${today_iso}` 改用 `window.date_tz` 口径
- 作业配置统一 `date_tz: America/New_York`（阿里云账单的 `format` 只到日期，实测不影响请求
  参数；DeepSeek 是 unix 窗口，同一 `pt` 里装的是美东那一天的 24 小时）
- 删除无引用的 `signed_query` 白名单项与 `ALLOWED_SIGN_METHODS`
- 文档：补数用法与「一次运行只写一个 pt」、内存口径改为"峰值＝单个请求单元的数据量"、
  时区示例的夏令时时间修正、"分六块"的块数修正

### 工程化（开源就绪）

- 测试从 187 个补到 283 个，覆盖率 79% → 91%（`cli.py` 主流程、`http.py` 重试分支、
  `init_wizard.py` 问答、写入/校验阶段都补了用例）；修掉两个引用 gitignored 作业文件、
  在 CI（checkout 后没有 `jobs/*.json`）上必挂的用例
- 新增 `CONTRIBUTING.md`（含"设计红线"：先删再填、宁可失败不可静默丢数、密钥不进日志、
  内存口径、错误归类、跨平台）与 GitHub Issue 模板（问题反馈要求附脱敏后的配置与日志）

### 修复（复审第四轮）

**写库与数据完整性**

- **`pad_hours` 与纯日期 `format` 组合把日期整体顶到前一天**：日期参数只到年月日，
  减 pad 不是"多拉一段"而是退回一天（业务日 9/18 发出 9/17），落进 `pt=业务日` 就是整表错一天；
  现在这种组合直接忽略 `pad_hours` 并打警告（需要跨天余量请把 `format` 改成带时分的）。
  原警告文案也说反了（写成"已按日期格式处理"），一并改对
- **环境变量 `bizdate` / `SKYNET_BIZDATE` 值畸形时静默回退"昨天"**：写错分区还得先删再填
  （把对的分区覆盖掉），退出码却是 0，调度侧完全看不出来。现在同样报错退出，
  与 `--bizdate` 传给错值时的行为一致；不想用它请先 unset

**请求与重试**

- **4xx / 配置类错误被整窗重试放大**：`FatalApiError`（密钥错、参数错、无权限）与
  `ConfigError` 现在直接冒泡到 `main` 结束本次运行，不再按 `window_retries` 反复打
  （与 README "4xx 快速失败"一致）；多单元并发时一个单元撞上 4xx 也会立刻停下，
  不再让其余单元接着失败一遍
- **`retry_times: 0` 仍白睡一轮**：语义是"失败后最多再试 N 次"（总请求数 N+1），
  0 现在就是只发一次请求、不做退避；日志里的"第 x/y 次失败"分母也改成实际重试次数
- `secrets` 写成列表/字符串/数字时不再抛 `dict.update` 的裸 traceback
  （`validate_job` 里那句人话提示原来是永远到不了的死代码）
- `signers.py` 语法/导入错误在正式运行时不再裸 traceback（原来只有 `--check` 拦得住）
- `--check` 的"页大小被拒"提示不再把通用词 `limit` 当成页大小关键词，避免多打一次请求

**翻页与解析**

- **终点字段只在第一页返回**（`totalPages` / `TotalCount` 只有首页带）导致末页后的空页被判成
  "无法确认已翻完"、默认 `strict` 整窗失败：现在缓存最后一次读到的终点值再判断
- `parse.skip_rows` 与 `parse.skip_until` 冲突时报错说清是两者同时配的问题，
  不再指向"文件结构变了"这个错误方向
- `parse.skip_rows` 为负、`parse.format` 写错，都改报配置错误（原来会当接口抖动重试）

**其它**

- 本机 `~/.aliyun/config.json` 结构不对时给可操作提示（改用作业文件的 AK/SK 或环境变量），
  不再裸 traceback
- `--log-file` 指向目录时给出建议文件名，不再 `IsADirectoryError`
- `--init` 向导里鉴权方式填了非法编号时回退到 Bearer Token 并提示，
  不再静默生成一份跑起来必报"找不到自定义签名文件"的配置
- `redact` 补上请求头行形态（`X-Api-Key: xxx`），密钥不再从 `requests` 异常里的 headers 漏进日志

### 修复（复审第五轮：全文逐模块复查 + 分页终点加固）

**会静默少数据 / 写坏数据**

- **非正数终点被当成"翻完了"**：`totalPages` / `TotalCount` 回 `-1`（"未知"的常见哨兵）、
  `0`（占位）时会在第 1 页就收尾——只拉到一页、写库校验还通过。现在只认正数
- **终点值中途变小导致提前收尾**：终点改为"取历史最大值"（单调不减），某页只回本页条数
  或窗口期数据被删时不再跟着缩水
- **`format` 只到日期时 `end_param` 多输出一天**：`dateFormat` 这类闭区间接口会多查一天，
  落进 `pt=业务日` 就是口径打架；现在 end 取当天（带时分的 `format` 语义不变）
- **`strict_encoding: true` 漏去 BOM**：非严格分支有 `lstrip`，strict 分支没有，同一个开关
  下列名变成 `﻿date`，下游 `get_json_object('$.date')` 静默全 NULL
- **CSV 引号未闭合时静默吞行**：`DictReader` 默认会把后续几行并进同一个字段，行数照常 > 0；
  现在 `strict=True` 并包成带行号的中文报错
- **记录数组里混进数字/字符串**：原样写进 json 列、下游取不到任何字段却显示成功；
  现在与 JSONL 路径一致直接报错

**会被当成网络抖动白等（配置错走重试）**

- `auth.params` 写成数组/字符串 → 裸 `AttributeError`；自定义签名函数返回 `{"params": [...]}`
  时并入阶段同样裸抛。两处都改报配置错（原来一次要空等 8 分钟 × 整窗重试）
- **分页超过 `max_pages`** 改报不可重试的配置错：原来每次整窗重试都会再翻满 2000 页
- `parse.delimiter` 写 `"\t"`（两个字面字符）现在是明确的配置错——3.11 起会被当多字符分隔符
  静默切分，3.10 及以前直接 `TypeError`，同一份配置跨版本行为不同

**健壮性与观感**

- 配置块类型写错（`"window": "per_day"`、`request.params: []`、`request.headers: []`）不再裸
  traceback；检查提到了读文件之后的第一步（`date_tz_of` 就要取 `window.date_tz`，晚一步就是
  `'str' object has no attribute 'get'`）。`params` 写成数组原本会被 `requests` 当"参数名列表"、
  固定参数静默全丢
- 脱敏补漏：单引号形态（`f"{cfg}"` / `{exc!r}`）与 `sig` 字段名（用户可配的 `sign_field`）；
  `--check` 概要里的接口 URL 也过 `redact`（`--init` 会把带 query 的地址存进 `path`）
- `--check` 期间 Ctrl+C 现在返回 130，不再裸 traceback
- `--dry-run` 不再被补数护栏拦住（它不写库，补数前先试跑看条数正是它的用途）
- `--init` 在 stdin 关闭时（`<&-`、CI）给一句人话；作业名里的路径字符会被替换，
  不再可能写到 `jobs` 目录之外
- `retry_call` 的"重试 N 次仍失败"与 `http.py` 口径对齐（都报实际重试次数，不含首发）
- `parse.skip_rows` 把非空文件一行不剩地跳空时打警告（原来静默返回 0 行）；
  `entry_contains` 命中多个 ZIP 条目时提示将要合并的文件名（原来无声串联）
- 告警去重记录按"每次运行"重置（同进程第二次 `main` 不再吞掉同名告警），并加了锁
- README 补退出码清单（0/1/2/130）与各码含义

## [2.1.0] - 2026-09-21

### 新增

- `auth.type: bearer`：最常见的 `Authorization: Bearer <token>`，配置只写一个字段
- 配置默认值与自动推断（让新作业配置明显变短，旧配置完全兼容）：
  - `pagination.type` 可省略，自动推断（`cursor_path`→cursor；`total_pages_path`/`total_items_path`/`page_param`→page）
  - page 分页默认 `page_param=page`、`size_param=size`、`page_size=100`；cursor 默认 `cursor_param=cursor`
  - `window.start_param`/`end_param` 默认 `startTime`/`endTime`；**显式写 `"end_param": null` 表示不要结束时间参数**（阿里云账单等）
- 交互式向导 `--init` 精简（Bearer 一步搞定；翻页/窗口只问必要问题）
- 写入分批同时受行数与字节数限制（默认 1000 行 / 8MB），批次在读取侧切好
  —— 原来纯按 1000 行攒批，单条记录很大（文件类源）时一个批次能吃下几 GB 内存
- 拉取/写入进度日志（每 1000 条一次）

### 修复

- Windows 非 UTF-8 控制台（cp1252 等）打印中文/符号日志会抛 `UnicodeEncodeError`：现在自动切
  UTF-8，切不了时按可替换字符降级输出（CI 在 windows-latest 上发现）
- 运行锁在 Windows 上原来是空操作；现在用 msvcrt 文件锁（Linux/macOS 仍是 flock）
- `--check` 体检时游标分页 / 单页模式会一路拉到底（大接口上很慢）：现在统一只发一次请求
- 游标分页补上页大小参数（接口不认 `size` 时可用 `"size_param": null` 关掉）
- `window.start_param` / `end_param` 改成**成对补齐**：只写 `start_param` 的作业不会被自动加上
  `endTime` —— **v2.0.0 及更早的作业升级后请求参数完全不变**

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
- 大数据量流式落盘（Spool）：峰值内存与"单个请求单元的数据量"成正比、与总天数无关
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
