# 贡献指南

欢迎提 Issue 和 PR。这个工具的目标很明确：**配置驱动、写入幂等、宁可失败也不静默写坏数据**。
提改动前请先读一下下面的"设计红线"，很多看似"顺手优化"的改法会踩到它们。

## 环境准备

```bash
git clone https://github.com/sandy-wangzining/api2ods.git
cd api2ods
python3 -m venv venv
./venv/bin/pip install -e ".[dev]"        # Windows: .\venv\Scripts\pip install -e ".[dev]"
```

只需要 Python 3.9+，没有系统级依赖（Windows 会自动装 `tzdata`）。

## 跑测试与代码检查

```bash
python -m unittest discover -s tests -v   # 全部用例：不访问网络、不连数仓
ruff check .                              # 代码检查（配置在 pyproject.toml，当前 0 告警）
```

- 测试**必须离线可跑**：不许依赖真实 API、真实 MaxCompute、本机特定的文件（CI 会在
  ubuntu / windows / macos × Python 3.9 / 3.12 六种组合上跑同一套用例）。
- 需要外部资源时用 `unittest.mock` 打桩；需要作业文件时在 `tempfile.TemporaryDirectory()`
  里现写一份，**不要引用 `jobs/*.json`**（真实作业文件含密钥，不入库）。
- 改动涉及数据完整性判定（分页终点、写前校验、行数核对）时，请补上"改之前会失败"的回归用例
  ——这类 bug 的共同点是**静默**：跑完显示成功，数据却少了。

## 设计红线（改代码前必读）

1. **写入是"先删再填"**：任何失败路径都不能让分区停在一半的状态。写前校验（如单行大小）
   必须在 `delete_partition` 之前完成；写入用全新的 Tunnel 会话（`reopen=True`），
   不复用上次失败的会话，否则会把残留块一起提交、产生重复行。
2. **宁可失败，不可静默丢数**：分页没翻完、`records_path` 中途取不到、空页而总数没拉够、
   写后行数对不上——一律抛错并以非 0 退出码结束（调度系统靠它告警）。
   确实需要放宽的情况用显式开关（`pagination.strict: false`），且**必须打警告日志留痕**。
3. **密钥不进日志**：任何进日志/异常的文本都要过 `utils.redact()`；测试里写假密钥。
   真实密钥只写在 `jobs/*.json` 或 `config.json`（都在 `.gitignore` 里）。
4. **不把全量数据放进内存**：拉取边拉边落盘（`spool.py`），写入按批（同时受行数与字节数限制）。
   新增功能时保持"峰值内存与总天数无关"。
5. **每个错误都要归位**：配置/鉴权错 → `ConfigError`（立即失败，不重试）；
   参数/权限类 4xx → `FatalApiError`（不重试）；429/5xx/网络抖动 → 重试。
   **别用 `OSError` 当自定义错误基类**——`requests` 的 `ConnectionError`/`Timeout`/`SSLError`
   都是它的子类，会把网络重试整个误杀。
6. **跨平台**：路径用 `pathlib`，编码一律显式 `utf-8`（读文件用 `utf-8-sig` 兼容 BOM），
   控制台输出走 `utils.log()`（它会自动处理 Windows 老控制台的编码问题）。
   Windows / macOS / Linux 三种行为都要能跑。

## 代码风格

- 中文注释与日志（目标用户是国内数仓同学），注释解释**为什么**，不复述代码在做什么。
- 每个模块头部写清职责；对外函数写 docstring，说明参数含义与失败行为。
- 行宽 120；`ruff` 配置只选"能发现真问题"的规则（`E4/E7/E9/F/W/I/UP`），
  风格类规则故意没开——不用为了对齐风格改代码。
- 不引入新依赖：核心只用 `requests` + `pyodps`。确有必要的依赖请先在 Issue 里讨论。

## 提交 PR

1. 从 `main` 切分支，一个 PR 做一件事；
2. 本地跑通 `python -m unittest discover -s tests` 与 `ruff check .`；
3. PR 描述里写清：**为什么改**（复现步骤 / 影响的作业）、**怎么验证的**；
4. 涉及行为变化或修 bug 的，同步更新 `CHANGELOG.md`（修 bug 说明"原来会怎样、现在怎样"）；
5. 涉及配置项/命令行参数的，同步更新 `README.md` 与 `jobs/_template_full.example.json`
   （三者是同一份文档的三个入口，容易漏）。

提交信息用 Conventional Commits 风格（`fix:` / `feat:` / `docs:` / `test:` / `refactor:`），
第一行说清做了什么，需要时在正文里补"为什么"。

## 想加一个新数据源？

多数情况写一份作业配置就够了，不用改代码——照 README 的「怎么接一个新源」走，
`api2ods --init` 可以交互式生成配置。配置里表达不了的接口形态，欢迎提 Issue 说明返回结构。

## 想加一种鉴权/分页/解析方式？

- **鉴权**：优先用 `auth.type: custom` + 作业同目录的 `signers.py`（见 `signers.example.py`），
  这是给"一次性、私有"的签名留的口子；
- 通用性够强的再往 `auth.py` 里加，并在 `README.md` 的鉴权表、`init_wizard.py` 的问答里同步登记；
- 加完请补单测：签名类一定要用**固定 nonce + 固定时间戳**对照官方示例或既有脚本的实际输出
  （`auth.py` 里阿里云签名就是按这个办法和线上脚本逐位对齐的）。

## License

贡献的代码按 MIT 许可（见 `LICENSE`）。
