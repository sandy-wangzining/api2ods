# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.1.8] - 2026-09-24

### 修复（第十轮复审：接口能让进程卡死 + 几处静默少数据/写错数据）

- **脱敏正则的指数级回溯（ReDoS）**：`_JSON_RE` 的两个分支在反斜杠上重叠，而
  `http`/`parsers` 拼错误信息时都会先把响应体截断到 300 字符——里面塞 ~290 个反斜杠时
  匹配等于永不返回（实测 38 个就要 20 秒），纯 Python 正则期间 Ctrl+C 也打断不了。
  触发方是不受控的第三方接口（回一个 400 错误体、或 200 但 `records_path` 不匹配）。
- **cursor 分页补上"没翻完"校验**：游标字段写错、或接口某页不回游标时，"取不到游标"
  以前会被当成"翻完了"，只拉第一页就收尾（静默少数据，写后条数校验还自洽）。
  现在配了 `total_items_path` 就会核对已拉条数并报错，没配会告警提醒
  （此前文档说"cursor 时 `total_*` 不生效"，已同步改掉）。
- **`page_param` 与 `size_param` 同名直接报配置错**：同名时页大小会覆盖页码参数，
  接口永远只回同一页，重复行静默写进 ODS、写后校验还自洽。
- **`window.mode=range` + `extra_params` 跨月/跨年时报配置错**：整段只发一次请求、
  派生参数（如 `BillingCycle=%Y-%m`）只能取首日的值，后半段数据会静默拉不到；
  per_day 模式不受影响。
- **不跟随 HTTP 重定向**：requests 默认跟随，而 301/302/303 会把 POST 降级成不带 body
  的 GET（窗口/分页参数全丢，接口若回 200 就是一份无过滤数据被当成成功写库），
  自定义鉴权头（`X-Api-Key` 这类，requests 只清 `Authorization`）还会被转发到重定向
  目标。现在接口返回 3xx 直接报错并打印 Location，请把 `base_url` 改成最终地址。
- **URL query 里的凭证纳入值级脱敏**：`?appkey=xxx` 这类参数名不在密钥词表里时以前
  漏遮，而 requests 的连接异常消息带着完整 URL（`Max retries exceeded with url: ...`）。
- 失败路径的三处补齐：临时文件创建失败（磁盘满/临时目录不可写）给一句人话而不是裸
  traceback；`--keep-spool` 在拉取阶段的失败分支也生效（原来只覆盖写库段）；
  写入条数对不上时的报错补上"分区已被覆盖写清空重填，请重跑"（与重试耗尽那条同口径）。

### 文档

- README：`base_url` 写明不跟随重定向；`page_param` 不能与 `size_param` 同名；
  cursor 分页建议同时配 `total_items_path`。

## [2.1.7] - 2026-09-23

### 修复（第九轮复审：16 条，每条都有回归用例）

**会静默写坏 / 丢数据**

- **`parse.format=jsonl` 无条件豁免"整包 JSON 错误体"检测**：接口 HTTP 200 返回
  `{"code":500,...}`（带不带行尾换行都一样）时会被当成"一条正常记录"写进 ODS（下游
  取不到任何字段）。现在只要整个响应能解析为一个 JSON 对象/数组就按错误体拦下，ZIP
  内每个条目也各查一遍；多记录 JSONL（包括最后一行不带换行）照常解析。确实"整个响应
  就是一条 JSON 记录、和错误体无法区分"的低流量源，用新配置项
  `parse.allow_single_record: true` 显式放行
- **`as_bool` 把未知字符串一律当真**：`"flase"`/`"ture"`/`"否"` 这类笔误静默走开分支
  （`target.allow_empty: "flase"` 会在 0 行时清空已有分区）。现在只认
  `true/1/yes/on` 与 `false/0/no/off`，其它非空字符串直接报带字段名的配置错；`None`/
  纯空白仍按"没填"用默认值
- **`request.json_encoding` 的冲突检测会误杀合法 GBK**：GBK 双字节序列偶尔也恰好是合法
  UTF-8（例如 `'一'.encode("gbk")` 能被 `utf-8-sig` 解成 `'һ'`），原冲突检查会拒绝这类
  正常响应。现在先比较两边解出的 JSON 键名：键名不同才按配置错快速失败；键名一致、只有
  值不同时按显式 `json_encoding` 处理并告警留痕，避免误杀合法 GBK。UTF-16/UTF-32 这类含
  `\x00` 的定宽编码仍排除在冲突检查之外

**配置错没能提前拦下（白跑 / 白等）**

- **`request.body_type` 笔误被当 JSON**：原来"非 form 即 JSON"，`"from"`/`"FROM"` 静默
  按 JSON body 发出，接口行为可能完全不同；现在只认 `json`/`form`，其它报配置错
- **鉴权必填字段漏检**：`basic` 缺 `username`/`password`、`token` 缺 `value`/`token`、
  `query` 缺 `params`、`sha256_concat` 缺 `secret_key`，原来要到发请求时才拼出空凭证
  （接口回 401，用户看不出漏了哪个字段）；现在在配置阶段逐项报出
- **配置文件不是 UTF-8**：GBK/UTF-16 记事本另存的文件原来抛裸 `UnicodeDecodeError`；
  现在给一句"不是 UTF-8 编码，请另存为 UTF-8"的提示，读取失败（`OSError`）也一并兜住
- **数值配置不在配置阶段校验**：`page_size`/`max_pages`/`retry_times`/`retry_delay`/
  `window.days`/`pad_hours`/`pagination.delay_seconds`/`window_retries` 等原来或拖到运行时
  才炸、或被 `or 默认值` 悄悄吞掉（`page_size: 0` 被换成 100）；现在在 `validate_job`
  统一校验正数/非负/整数/有限，报错带字段名。`parse` 的布尔开关
  （`unzip`/`strict_encoding`/`allow_multi_entry`/`allow_single_record`）同样提前校验：
  多记录 JSONL 配成 `"flase"` 原来不会走到那条分支、静默当没开，现在在配置阶段就报错
- **`target.lifecycle_days` 校验太晚**：原来要"拉完所有数据、准备写库"时才校验，配置写错
  先白跑一整轮 API；现在提前到配置阶段（运行期那道检查保留作兜底）
- **`--init` 向导能生成非法 `window.days`**：向导对"最近几天"填 0/负数照单全收，生成的
  配置随后被自己的 `validate_job` 拒绝；现在提示并重问，超过三次才用正整数默认值兜底
- **直接调用 `validate_job` 时非对象块抛裸异常**：库调用方不先走 `normalize_job` 时，
  `request`/`target`/`window`/`pagination`/`parse` 写成字符串会得到 `AttributeError`；现在
  统一报"必须是对象（键值对）"的配置错

**跨平台一致性**

- **`window.format` 的 locale/平台相关指令**：`%a`/`%A`/`%b`/`%B`/`%c`/`%x`/`%X`/`%r`/
  `%p`/`%Z` 的输出由 C 库 locale 与平台时区库决定（中文/法语 Windows 上 `%b` 给「9月」
  「sept.」，`%Z` 给平台时区缩写），同一份配置在开发机与调度机上算出不同参数值，接口按值
  匹配时会静默查不到数据。现在这些指令与 `%s`/`%P` 一样由自己实现，固定成 C locale 的
  英文写法（`%c=%a %b %e %H:%M:%S %Y`、`%x=%m/%d/%y`、`%r=%I:%M:%S %p`，`%Z` 取
  `tzinfo.tzname()`）

**错误归位 / 写库安全**

- **ZIP 加密或不支持的压缩方式被判成"可重试"**：重试多少次都是同一份包、同一个结果，
  原来白等十几分钟退避；现在归为 `ConfigError`（不可重试）快速失败，并提示"在源侧换一种
  导出方式"（条目损坏 `BadZipFile` 仍按可重试）
- **`count_partition` 用 `hasattr(row, "__getitem__")` 判断取值方式**：元组也有
  `__getitem__`，于是对元组行走 `row["cnt"]` 直接 `TypeError`；改成先按列名、失败再按位置取
- **目标标识符裸拼进 DDL / SQL**：`target.project`/`table`/`column`/`stored_as` 带空格、
  连字符是建表失败，带分号是 SQL 注入点；现在在 `config` 与 `mc` 两处按
  `[A-Za-z_][A-Za-z0-9_]*`（字母/下划线开头）白名单校验，报一句明确的配置错
- **"先删再填"重试全失败时不说明后果**：`write_partition` 原样抛"重试 N 次仍失败"，
  调度侧看不出分区已被清空或只写入一半、旧数据不会自动恢复；现在错误信息写明后果与补救
  动作（请重跑本作业、重跑会从头覆盖不会叠加）。`delete_partition` 自身抛错时也保守地
  认为"分区可能已被删掉"，不会因为异常发生在一行赋值之前而吞掉缺数提示。`except Exception`
  不含 `KeyboardInterrupt`/`SystemExit`，Ctrl+C 的退出行为不受影响

### 测试

- 用例 497 → 527（上述每处修复都有回归用例）；`tests` 说明更新为"没装 `requests` 时，
  少数借用真实 `requests` 异常类型的用例会 `skip`"

## [2.1.6] - 2026-09-22

### 修复（第八轮复审：对 2.1.5 逐条复测又查出的 8 处，每条都有回归用例）

**跨平台一致性（2.1.5 声称已修，但只修了最窄的形态）**

- **`%s`/`%P` 混在格式串里仍然跨平台不一致**：2.1.5 只处理了"整串恰好是 `%s`"和 `%P`，
  `window.format` 写成 `%Y-%s`（把时间戳拼进自定义串）时，Windows 上 `strftime` 直接抛
  `ValueError`——裸 traceback、退出码 1、`--log-file` 一个字都没有，`--check` 还把它
  报成"请求失败"（其实一个字节都没发出去）；glibc 则把 `%s` 展开成 epoch 秒，
  同一份配置两个平台两种行为。现在 `%s`/`%P` 在格式串的**任何位置**都由自己实现
  （`%Y-%s` → `2026-1789716600`，`%s` 单用仍返回 int）
- **`%%%s`/`%%%P`（转义符后接真指令）被后行断言漏掉**：`(?<!%)` 只看前一个字符，
  第二个 `%` 明明是转义符，第三个 `%` 开头的真指令却被当成"已转义"——Windows 抛
  `ValueError`、glibc 输出 `%<epoch>`。改成按 `%%` 成对扫描，奇偶天然正确
  （`%%%s` → `%<epoch>`，`%%%P` → `%pm`，`%%s`/`%%P` 仍是字面量）
- **`%Q`/`%-d` 这类指令拖到运行时才炸**：白名单校验现在进 `validate_job`，
  `--check` 与正式跑都在准备阶段给出带字段名的配置错（`window.format`、
  `window.extra_params['x']`），不再伪装成网络失败或裸崩溃

**密钥泄漏**

- **落盘报错链路**：`on_records`（`spool.dump_record`）抛出的异常不经 `run_unit` 的
  脱敏包装，`fetch.py` 的失败列表/日志、`cli.py` 的失败重打与写库失败会原样输出记录
  片段（含 token/手机号这类密钥与 PII）；现在 `dump_record` 先 `redact` 再截断，
  `fetch.py` / `cli.py` 的六处日志统一过 `redact`
- **`page_size=1` 体检被拒**的日志同样过 `redact`（错误体里可能有签名串）
- **接口把凭证写进自由文本报错时，形态规则盖不住**（如 `HTTP 401：{"error": "bad token sk-xxx"}`
  或 fail_if 的 `message_path` 原文）：`redact` 只认 `token=…`/`Bearer …`/`"key": "value"`
  这类写法，裸 token 会原样进日志。现在增加一层**值级脱敏**：从
  `job.secrets`、`request.auth`（`value`/`token`/`params`/`secret_key`/`access_key_*`…）、
  `request.params` 与 `request.headers` 的敏感键、`maxcompute` 的 access key
  收集密钥字面量，出现即换 `***`（长度 <4 的值不参与，避免搅乱报错；长值先替、
  带 `Bearer `/`Basic ` 前缀的值再拆出裸 token）。`Fetcher`、`--check`、
  `run_sync` 与 CLI 错误出口全走这一层；`request_with_retry` 新增 `redactor` 参数
  ——重试日志在它内部就落盘，不传的话值级脱敏追不上（可重试业务错误那条路径）

**护栏与观感**

- **latin-1 别名**：原来只挡 `iso-8859-1`/`latin-1`/`latin1` 三个字面量，
  `latin_1`/`iso_8859_1`/`cp819`/`L1`/`8859` 等写法能绕过——`codecs.lookup` 把它们全部
  归一成 `iso8859-1`，照样把任何字节解成"看起来成功"的乱码；现在按归一化编码名判断
  （`json_encoding` 配置项与响应头声明的 charset 两处）
- **`parse.entry_field` 没开 `unzip`**：字段没有来源，每条记录会多写一个恒为空的列；
  现在 `validate_job` 告警（不报错：字段恒空不影响正确性，但多半是漏开了 unzip）
- **运行锁文件被第二次启动截断**：`open(..., "w")` 在拿锁**之前**就把持锁进程写进去的
  pid 清掉（互斥不受影响，丢的是排障用的"谁在跑"）；改成 `a+`，拿到锁之后才截断写入
- **TSV/CSV 字段中间的裸引号**：被当成 csv 引号段的开始，`skip_until` 标记落在后面时会被
  误判成"在引号里"，文件有内容却报"找不到明细段"；现在只在字段开头的 `"` 开启引号段，
  段内任意非双写 `"` 即收尾（与 Python `csv` 模块逐行交叉验证：`"a"x,b` 视为已收尾，
  `""` 转义与跨行字段行为不变）

### 测试

- 用例 481 → 497（上述每处修复都有回归用例；含与 Python `csv` 模块对齐的
  引号状态交叉验证）

## [2.1.5] - 2026-09-22

### 修复（第五轮全文复审：3 个审查面共 22 条，每条都先实测复现再改）

**会静默写坏 / 丢数据**

- **分页路径上 `records_path` 落在空对象 `{}`**：接口用空对象表示"这次没有数据"
  （`Items: {}`、`data: {}`），单页路径会归一成 0 条，分页路径却把它包成一条 `{}` 写进 ODS
  ——一条全 NULL 的假记录，下游取不到任何字段、行数校验还自洽；它同时让"零数据日"豁免
  失效，一路空翻到 `max_pages`（默认 2000 页）才报错。现在两条路径共用同一处归一
- **两个终点都配时以页数优先 break**：接口按"请求的 page_size"算总页数、实际每页给得更少
  （或 `totalPages` 中途变小）时，会停在"页数够了"而少拉数据，退出码 0。
  现在条数终点说没拉完就继续翻（真翻过头会拿到空页，由既有检查报出来）
- **`skip_until` 命中"引号里跨行字段"的内容**：从半行开始解析、真表头行被当数据行，
  列名全错却照样写库成功。现在逐字符判断标记是否落在引号外
- **`json_encoding` 配成 `latin-1`/`iso-8859-1`**：任何字节都能解成"看起来成功"的乱码、
  键名全错却写库成功；现在直接拒绝，并在显式编码与 utf-8-sig 解出的内容不一致时告警
- **`pt` 值形态不校验**：`--pt 2026-09-20` / `target.pt` 写成非 `yyyyMMdd` 时数据写进
  `pt=2026-09-20`，调度与 DWD 都按 `pt=20260920` 读——"写进去了没人读"却退出码 0；
  现在 `target.pt`（及默认业务日）只认 8 位业务日；`--pt` 显式指定时放宽为合法分区名
  （测试/对比/补数用，如 `test_20260921`、`cmp_*`），报错文案与 README/模板/CLI help 同步
- **`--bizdate 2026-W36-1`（ISO 周日期）**：Python 3.11+ 的 `date.fromisoformat` 会把它
  静默解析成 2026-08-31（3.9/3.10 上则报错），同一个参数跨版本行为不同；现在只认
  `YYYYMMDD` / `YYYY-MM-DD`

**配置错被当成网络抖动（白等）**

- **请求构造期错误**：`base_url` 少写 `https://`、请求头值带换行/中文——这些在
  一个字节都没发出去时就抛，属于确定性配置错，原来会被退避重试 17 轮
  **累计 1425 秒（23.8 分钟）**。现在转成配置错立即失败；并在发请求前校验请求头
  （首尾空白/换行/非 latin-1），报错只给头名
- **`parse.encoding` 名字写错**（如 `utf8sig`）：`LookupError` 落到"统一重试"分支，
  3 天窗口要发 9 次请求、空等 90 秒；现在报配置错、每个单元只发一次
- **`window.format` 的平台差异**：`%s`/`%P` 是 glibc 扩展、MSVC 不认（Windows 上直接抛裸
  `ValueError`），而 glibc 对不认识的指令是**原样输出**（把 `%Q` 当参数值发给接口）。
  现在 `%s`/`%P` 自己实现，指令按「Windows ∩ glibc」的白名单校验（`%Q`/`%-d` 等一律
  配置错），同一份配置在两个平台上行为一致

**密钥泄漏**

- **请求头非法值把明文密钥打进日志**：`requests` 的 `InvalidHeader` 消息里带着头的原值，
  一次运行实测泄漏 15 行（控制台 + `--log-file`），最终异常里也带。现在提前校验、只报头名
- **`auth.py` 三处报错没过脱敏**：`auth.params` 写成数组时会把 `['tok', '密钥']` 整个回显
  （Python list 的 repr 任何脱敏规则都盖不住），自定义签名文件/函数的异常文本也是原样插入。
  现在前者不回显内容，后者过 `redact`
- **`redact` 补三类形态**：URL 里的 userinfo（`https://user:pass@host`，`--check` 概要会打）、
  值里含引号时被引号截断（`{'password': "ab'SEC"}` 漏掉引号之后的内容）、
  内层 JSON 被编码成字符串（`{"data": "{\"token\": \"x\"}"}`）；另补 `pwd`/`pw`/`pass`/`bearer`
  几个常见简写字段名
- **`--check` 的失败分支不脱敏**：接口错误体常回显 token，`run_sync` 的同类分支一直是脱敏的

**护栏与观感**

- **只配 `page_size` 或只配 `size_param` 时不再静默**：原来要两者同时出现才告警，
  单写 `page_size`（最像"我配好分页了"的写法）会静默只发一次请求、条数校验拿这"一页"自比
- **`type=cursor` 时配了 `total_*` 现在会告警**：该字段只在 page 分页生效，原来配了等于没配
- **准备阶段的配置错现在也进 `--log-file`**：`--bizdate` 畸形、缺 `base_url`、占位符写错、
  `validate_job` 的报错原来直接冒泡出 `main`，控制台那行没有时间戳、日志文件里一个字都没有
- **`--init-out` 指向目录**给人话提示（原来 Windows 上是裸 `PermissionError`）
- **`--init` 选"文件流 + ZIP + 条目留空"**会补上 `allow_multi_entry`（原来生成一份
  跑不通的配置：解析到第二个文件就报错）
- **`target.lifecycle_days`** 拒布尔/浮点/负数（`int(True)==1` 会让新表当天被生命周期回收）
- **运行锁文件建不出来**（父目录被删/路径过长）给人话，而不是裸 `FileNotFoundError`
- **`as_bool` 的纯空白串按"没填"处理**：原来 `verify: " "` 会静默关掉 TLS 校验、
  `strict: " "` 会静默放宽丢数检查
- **大 payload 的错误信息**不再先 `json.dumps` 整份响应（百 MB 级响应可能先 MemoryError），
  改用增量编码器边编边停

**复审补充修复（第六轮，`pt` 校验两档化 + 两处边界）**

- **`pt` 校验改成两档**：默认路径（`target.pt` / 业务日）仍强制 8 位 `yyyyMMdd`；
  `--pt` 显式指定时只要求是合法分区名——测试写入（`test_*`）、新旧对比（`cmp_*`）、
  补数（`backfill_*`）不再被一刀切拒掉。原报错"需要写别的分区请用 --pt 显式指定"
  本身就是错的（`--pt` 走同一条校验）；文案已改准，README / 模板 / `--pt` help 同步
- **`--bizdate 20261301`（紧凑写法但日期不存在）**：`date()` 构造抛裸 `ValueError`；
  现在与 ISO 写法一致报"日期不存在"（`--dates` / `--start-date` / 环境变量同路径）
- **`window.format` 里 `%%P`（字面量）与 `%P` 混用**：替换用了 `str.replace`，
  会把 `%%P` 里的 `%P` 也换掉、剩下 `%\x01` 让 strftime 抛裸 `ValueError`；
  现在只替换未转义的 `%P`（`%Y-%%P-%P` → `2026-%P-pm`）

### 测试与文档

- 用例 452 → 481（含本轮每条修复的回归用例）；覆盖率 99%
- CI 矩阵扩到 Python 3.9–3.14 × ubuntu/windows/macos
- 把 `fix/ci-ruff-tests` 分支上新增的 82 个防错分支用例合并进来（该分支基于 2.1.3，
  直接合并会丢掉 2.1.4 的 34 个用例，故按"main + 分支新增"逐个并入）

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
