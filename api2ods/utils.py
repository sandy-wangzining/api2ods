# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import errno
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlparse

try:
    import fcntl  # Linux / macOS：进程级运行锁
except ImportError:  # pragma: no cover - Windows 没有 fcntl
    fcntl = None

try:
    import msvcrt  # Windows：用首字节锁实现同样的效果
except ImportError:  # pragma: no cover - Linux / macOS 没有 msvcrt
    msvcrt = None


class FatalApiError(RuntimeError):
    """参数/权限/业务类错误：重试没有意义，立刻失败（如 HTTP 401/400、签名错误）。"""


class ConfigError(SystemExit):
    """作业配置错误：重试多少次结果都一样，快速失败并让调度看到原因。

    继承 SystemExit 与项目里其它配置类报错（config.py / mc.py / cli.py）保持一致的退出行为，
    同时给重试循环一个可识别的类型——注意不能用 OSError，requests 的
    ConnectionError / Timeout / SSLError 全是它的子类，拿来当"本地错误"会误杀网络重试。
    """


_lock = threading.Lock()
PROGRESS_EVERY = 1000  # 进度日志节流：每 N 条记录打一次
_sinks: list = []
_console_patched = False


def setup_console() -> None:
    """stdout/stderr 切 UTF-8，避免 Windows 控制台中文乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 只吞"这个流不支持 reconfigure"（含 io.UnsupportedOperation，它是 OSError/ValueError
            # 的子类）；吞掉别的异常会把本函数自身的编程错误也一起静默，日后无从排查乱码
            pass


def progress_log(label: str, count: int, unit: str = "条") -> None:
    """每 PROGRESS_EVERY 个打一条进度，避免大作业刷爆日志。"""
    if count and count % PROGRESS_EVERY == 0:
        log(f"    {label}：已处理 {count:,} {unit}")


def add_log_sink(handle) -> None:
    """把日志再写一份到文件（--log-file），句柄由调用方负责关闭。"""
    with _lock:
        _sinks.append(handle)


def remove_log_sink(handle) -> None:
    """摘掉日志文件并关闭句柄（同一进程里多次调用 main 时，残留句柄会继续写已关闭的文件）。"""
    if handle is None:
        return
    with _lock:
        if handle in _sinks:
            _sinks.remove(handle)
    try:
        handle.close()
    except ValueError:
        # 句柄已经被关过（同一进程里 main 多次调用时 _detach 会跑两遍）：
        # 再关一次抛的是"对已关闭文件做 I/O"，不是真错误
        pass
    except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
        pass


_logged_once: set = set()
_sink_write_warned = False


def reset_log_once() -> None:
    """清空"已打过的告警"记录（每次运行开始时调，见 cli.main）。

    不重置的话，同一进程里第二次调用 main（测试、嵌入调用）会静默吞掉
    第一次已经打过的那条告警——用户看不到任何提示。
    """
    global _sink_write_warned
    with _lock:
        _logged_once.clear()
        _sink_write_warned = False


def _warn_log_sink_once(exc: BaseException) -> None:
    """日志文件写失败时往 stderr 打一条（同一次运行只提示一次），sink 已由 log() 摘掉关闭。"""
    global _sink_write_warned
    with _lock:
        if _sink_write_warned:
            return
        _sink_write_warned = True
    try:
        sys.stderr.write(
            f"警告：--log-file 写入失败（{type(exc).__name__}: {exc}），该文件后续不再写入；控制台日志不受影响\n"
        )
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - stderr 也坏了就放弃
        pass


def log_once(message: str) -> None:
    """同一次运行里内容相同的告警只打一次，之后静默。

    同一个配置问题常被多条代码路径各自发现（如 window_param_sets 会被
    unit_count / fetch_all / probe 依次调用），不去重的话一条命令里
    同样的警告会连打两三遍，把真正有用的信息淹掉。
    """
    with _lock:
        if message in _logged_once:
            return
        _logged_once.add(message)
    log(message)


def log(message: str) -> None:
    """线程安全的控制台输出；时间戳=运行机器本地时间，只标记执行时刻。

    防御：Windows CI/老控制台默认是 cp1252 之类编码，中文/符号会抛 UnicodeEncodeError；
    这里第一次调用时自动把控制台切到 UTF-8，切不了就用"可替换字符"降级输出，保证不中断业务。
    """
    global _console_patched
    if not _console_patched:
        # check-then-set 放进锁里：多线程首次调用时不会重复执行 setup_console
        # （TextIOWrapper.reconfigure 不是线程安全的）
        with _lock:
            if not _console_patched:
                setup_console()
                _console_patched = True

    import datetime as _dt

    # 带时区偏移的本地时间：跨时区/夏令时排障时能和调度系统、服务端日志对齐
    stamp = _dt.datetime.now(_dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    line = f"[{stamp}] {message}"
    # print 与 sink 写入都放在锁外：慢速目标（管道被压满、NFS/满盘上的 --log-file）
    # 只会拖慢这条日志本身，不该把全局 _lock 占住——否则其它线程的 log_once /
    # add_log_sink / remove_log_sink 会一起卡死，整个进程表现为停滞
    try:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = line.encode(encoding, "replace").decode(encoding, "replace")
            print(safe, flush=True)
    except (OSError, ValueError, RuntimeError, AttributeError):
        # stdout 断管/关闭（BrokenPipeError、`| head` 提前退出；sys.stdout 属性缺失等
        # 极端形态抛 RuntimeError/AttributeError）：日志函数不能反过来把业务打挂
        # （写文件的那一路下面还有自己的兜底）——与 sftp2ods / feishu2ods 同口径
        pass
    with _lock:
        sinks = list(_sinks)  # 快照：写的时候不持锁
    failed_exc = None
    broken: list = []
    for handle in sinks:
        try:
            handle.write(line + "\n")
            handle.flush()
        except Exception as exc:  # noqa: BLE001 - 日志文件问题不影响主流程，但必须可见一次
            failed_exc = exc
            broken.append(handle)
    if failed_exc is not None:
        with _lock:
            # 从当前列表里剔除写坏的（不用写前快照覆盖：期间新加的 sink 不能丢）
            _sinks[:] = [handle for handle in _sinks if handle not in broken]
        # 摘掉的同时把句柄关掉（写坏的文件句柄不可再用，留着只是泄漏）
        for handle in broken:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
        _warn_log_sink_once(failed_exc)


class RunLock:
    """进程级运行锁：避免定时任务与手动执行（或两个实例）同时跑。

    - Linux / macOS：flock 排它锁；
    - Windows：msvcrt 首字节锁（同样是排它、非阻塞）；
    - 两种锁都没有的平台：退化为"不阻塞"，不挡运行；
    - 锁随进程退出自动释放，进程被 kill 也由内核释放，不会残留死锁。

    注意：任意进程删除锁文件后，新进程会拿到一把"新锁"，与持锁者不再互斥——
    正常流程不会删锁文件（异常退出时删除只在拿不到锁的分支里）。
    """

    def __init__(self, path: Path):
        """path 由调用方按作业算好（同名作业在不同目录不会互相顶掉，见 cli._lock_path）。"""
        self.path = path
        self.fh = None

    def __enter__(self):
        """拿锁；已被别人持有就抛 SystemExit（不等待），拿不到直接让本次运行退出。"""
        if fcntl is None and msvcrt is None:
            # 平台完全没有文件锁模块：与"文件系统不支持锁"同口径留一次告警——
            # 静默退化成"无锁"会让并发启动同一作业时互斥形同虚设而无人察觉
            log_once("  警告：当前平台没有 fcntl/msvcrt，本次不加锁继续；并发启动同一作业将无法互斥")
            return self
        try:
            # "a+" 而不是 "w"：w 会在打开时把文件截断，持锁进程刚写进去的 pid 就被抹掉了
            # （锁本身是文件区域锁，与文件内容无关，互斥不受影响；丢的是排障用的"谁在跑"）
            # POSIX 上 O_NOFOLLOW：锁路径若是符号链接就拒绝跟随，新建按 0600
            # （与 sftp2ods / feishu2ods 的锁同口径）
            open_kwargs: dict = {"encoding": "utf-8", "errors": "replace"}
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                open_kwargs["opener"] = lambda path, flags, _nf=nofollow: os.open(path, flags | _nf, 0o600)
            self.fh = open(self.path, "a+", **open_kwargs)
        except (OSError, UnicodeError) as exc:
            # 父目录被删/路径过长（Windows MAX_PATH）时给一句人话，
            # 而不是让 FileNotFoundError 以裸 traceback 的形式糊在用户脸上
            raise SystemExit(
                f"无法创建运行锁文件 {self.path}（{exc}）；请检查该路径所在目录是否存在/可写，或用 --job 指定别处的作业"
            )
        try:
            locked = _try_lock(self.fh)
        except BaseException:
            # _lock_oserror_result 对未识别的 errno 会上抛：确保任何路径下句柄都被释放
            # （否则锁文件句柄只能等 GC，同一进程重复进入时可能累积）
            self.fh.close()
            self.fh = None
            raise
        if not locked:
            self.fh.close()
            self.fh = None
            raise SystemExit(f"已有任务在运行（锁文件 {self.path}），本次退出；确认没有任务在跑时可删除该文件后重试。")
        try:
            # 拿到锁之后才截断+写自己的 pid：拿不到锁时绝不能动内容，
            # 否则每一次被拦下的启动都会把持锁进程的标记清掉
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(str(os.getpid()))
            self.fh.flush()
        except OSError:  # 写进程号只是标记，失败不影响加锁
            pass
        return self

    def __exit__(self, *exc_info):
        """解锁并关句柄；锁文件本身保留（不删文件，避免削掉别人的锁）。"""
        if self.fh is not None:
            try:
                _unlock(self.fh)
            finally:
                self.fh.close()


# flock / msvcrt 忙：别人正持锁。只把这类错误当成"已有任务在运行"。
_LOCK_BUSY = {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}
if hasattr(errno, "EDEADLK"):
    _LOCK_BUSY.add(errno.EDEADLK)
if hasattr(errno, "EDEADLOCK"):
    _LOCK_BUSY.add(errno.EDEADLOCK)
# 文件系统不支持锁 / 锁资源耗尽：不能伪装成"已有任务在运行"
_LOCK_UNSUPPORTED = set()
for _name in ("ENOLCK", "ENOTSUP", "EOPNOTSUPP"):
    if hasattr(errno, _name):
        _LOCK_UNSUPPORTED.add(getattr(errno, _name))


def _try_lock(fh) -> bool:
    """对已打开的文件加排它锁；别人拿着锁时返回 False（不阻塞等待）。

    三类 OSError 必须分开：忙=False，文件系统不支持=告警后当没锁继续，其余原样抛出。
    """
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            return _lock_oserror_result(exc)
    if msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            return _lock_oserror_result(exc)
    return True  # 两种锁都没有：不阻塞（退回"无锁"行为）


def _lock_oserror_result(exc: OSError) -> bool:
    """busy → False；文件系统不支持锁 → 默认 fail-closed 拒绝执行（除非显式
    API2ODS_ALLOW_NO_LOCK=1 接受无互斥风险）；其它 OSError 上抛。"""
    code = exc.errno
    if code in _LOCK_BUSY:
        return False
    if code in _LOCK_UNSUPPORTED:
        if os.environ.get("API2ODS_ALLOW_NO_LOCK", "").strip() == "1":
            log(f"  警告：文件系统不支持运行锁（{exc}）；API2ODS_ALLOW_NO_LOCK=1 已显式接受无互斥风险，本次不加锁继续")
            return True
        # fail-closed：无锁继续会让两个实例并发写同一作业/表（purge/rename 互拆、数据被
        # 静默覆盖），宁可拒绝执行
        raise SystemExit(
            f"运行锁所在文件系统不支持加锁（{exc}）：拒绝无锁执行（并发实例会互相写坏数据）。"
            f"请把锁目录放到本地磁盘，或确认无人并发时设置 API2ODS_ALLOW_NO_LOCK=1"
        )
    raise exc


def _unlock(fh) -> None:
    """释放锁；释放失败也没关系——进程退出时内核会兜底释放，不该因此让任务报错。"""
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
    elif msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


# 布尔字符串的白名单：只认这些，其它字符串（如 "flase" 这种笔误）一律报配置错。
# 不能"未知一律当真"：`target.allow_empty: "flase"` 会被当成开，0 行时把已有分区
# 清空；`parse.allow_multi_entry: "flase"` 会把本不该合并的多文件串成一份。
_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def as_bool(value, default: bool, field: str = "") -> bool:
    """配置里的布尔值：JSON 写 true/false、字符串 "true"/"false"、0/1 都认。

    - 未填（None）/纯空白串 → 返回 default（"没填"就是走默认值）；
    - 认识的写法 → 对应真假；
    - 其它字符串（"flase" / "ture" / "否" 之类无法识别的）→ 报配置错。

    为什么不能"未知一律当真"：`target.allow_empty: "flase"` 会被当成开，本次 0 行时
    把已有分区清空；`parse.allow_multi_entry: "flase"` 会把本不该合并的多文件串成一份。
    宁失败勿写错——把笔误变成一次清晰的报错，而不是一次静默的错误分支。
    """
    where = f"{field} " if field else ""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "":
            return default
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ConfigError(
            f"{where}布尔值无法识别：{value!r}；请写 JSON 的 true/false，"
            f'或字符串 "true"/"false"（也认 0/1、yes/no、on/off）'
        )
    if isinstance(value, (int, float)):
        # 数字写法只认 0/1（文档承诺的范围）：NaN/2.5 这类笔误与 "flase" 同口径报错，
        # 不能 bool() 放行——allow_empty: NaN 被静默当 True 会在 0 行时清空已有分区
        if value in (0, 1):
            return bool(value)
        raise ConfigError(f"{where}布尔值无法识别：{value!r}；数字写法只认 0/1，其余请写 true/false")
    # 其它类型（数组/对象）不能 bool() 兜底：[] 会被静默当成 False、绕过 fail-closed 约定
    raise ConfigError(f"{where}布尔值类型不支持：{type(value).__name__}（{value!r}）；请写 true/false 或 0/1")


# MaxCompute 常规标识符：字母/下划线开头 + 字母/数字/下划线
_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def require_identifier(value, where: str) -> str:
    """校验一个会直接拼进 DDL / SQL 的标识符（project / table / column / stored_as）。

    这些值不是请求参数，而是**拼进语句的标识符**：带空格、连字符、分号的名字要么建表失败，
    要么成为 SQL 注入点（`ods_x; drop table ...`）。配置虽然是本机文件，但名字写错时给一句
    人话，远好过让 MaxCompute 抛一句看不出所以然的语法错。合法名只允许字母/数字/下划线，
    且不以数字开头（与 MaxCompute 的常规标识符规则一致）。

    返回 str，方便调用方直接拿去拼语句；不合法抛 ConfigError（SystemExit 子类，
    不可重试、快速失败）。
    """
    if not isinstance(value, str) or not value:
        # 不能先 str() 再校验：str(None) == "None"、str(True) == "True" 都能过标识符正则，
        # 配置漏填时会被静默拼出一个名叫 None / True 的表名——快速失败，别写错对象
        raise ConfigError(f"{where} 缺失或不是字符串：{value!r}")
    if not _IDENT_RE.match(value):
        raise ConfigError(f"{where} 不是合法的 MaxCompute 标识符：{value!r}；只允许字母/数字/下划线且不能以数字开头")
    return value


def check_header_values(headers: dict) -> None:
    """请求头的值必须是"能直接发出去"的字符串，否则提前给配置错。

    requests 对"值首尾有空白/含换行/含非 latin-1 字符"只会抛 InvalidHeader，
    而且**消息里带着头的原值**——密钥会跟着进日志与最终异常（实测一次运行漏十几行）。
    更糟的是它属于"一个字节都没发出去"的确定性错误，被当成网络抖动退避能白等 20 多分钟。
    所以在这里提前拦下，报错只给头名、不回显值。
    """
    if headers is not None and not isinstance(headers, dict):
        # 写错成列表/字符串时 .items() 是裸 AttributeError；与其余配置错同口径给中文报错
        raise ConfigError(f"request.headers 必须是对象（键值对），实际 {type(headers).__name__}")
    for name, value in (headers or {}).items():
        if isinstance(value, bytes):
            try:
                text = value.decode("latin-1")
            except UnicodeDecodeError:
                raise ConfigError(
                    f"请求头 {name} 的值含非 latin-1 字符（中文等），HTTP 头发不出去；值不回显以免泄漏密钥"
                )
        elif isinstance(value, str):
            text = value
        else:
            # 先拦再 str()：list/dict/int 转成字符串再发出去，接口行为未定义；
            # 报错只给类型、不回显值（值里可能就是 token）
            raise ConfigError(f"请求头 {name} 的值必须是字符串（实际 {type(value).__name__}）；值不回显以免泄漏密钥")
        if text != text.strip() or "\r" in text or "\n" in text:
            raise ConfigError(
                f"请求头 {name} 的值首尾有空白或含换行（配置或密钥里多半抄多了空格/换行）；值不回显以免泄漏密钥"
            )
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            raise ConfigError(f"请求头 {name} 的值含非 latin-1 字符（中文等），HTTP 头发不出去；值不回显以免泄漏密钥")


# =============================================================================
# 脱敏：日志/异常里不出现密钥、签名、token
# =============================================================================

# 密钥字段名按「词」判断：先按下划线/中划线/驼峰切开再看每个词，这样
# accessToken / client_secret / X-Api-Key 都能认出来，而 task=? 不会因为含 "sk" 被误伤
_SENSITIVE_WORDS = {
    "sign",
    "signature",
    "sig",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth",
    "apikey",
    "key",
    "accesskey",
    "sk",
    "ak",
    # 常见简写与 scheme 名：?pwd= / ?pw= / ?pass= / bearer: <token>
    "pwd",
    "pw",
    "pass",
    "bearer",
    # 连写形态：?appkey= / ?appsecret=（无下划线时词切分切不出 "key"，
    # 切不出就漏遮——CLink 这类接口的凭证就叫 appkey）
    "appkey",
    "appsecret",
}
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
# 参数名做左边界限制（不用 \b：下划线在正则里算词字符，client_secret 会被漏掉）。
# 分隔符同时认 = 与 :、且允许键后收尾引号与分隔符后空格（`{"password": 12345}` 这类
# 不带引号的数字/布尔值只有这条能遮，JSON 规则只吃带引号的字符串值）
_QUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})(['\"]?)([:=])([ 	]*)([^&\s\"']+)")
# 敏感键的值吃到行尾/`&`为止：`password=my secret` 原来只遮 "my"、"secret" 明文留下
# （口令短语很常见）。负向先行断言只挡「引号后紧跟 ***」的已遮罩文本（避免把
# `"secret_key": "***", "page": 2` 整行再吞一遍）；未闭合引号（password="abc 被日志
# 截断）或「带引号的键」+ 不带引号的值（\"password\": my secret）必须走这条兜底——
# 否则 KV/JSON 要收尾引号、常规 QUERY 的值类不吃引号，三套规则全绕过
_QUERY_SPACE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<key>[A-Za-z0-9_.\-]{1,64})(?P<q>[\"']?)(?P<sep>\s*[:=]\s*)(?!\s*[\"']?\s*\*\*\*)(?P<val>[^&\n]+)"
)


def _mask_spaced_values(text: str) -> str:
    """敏感键 + 无引号值遮到行尾/`&`（口令短语含空格不被第一个词截断）。

    用扫描器而不是一次 sub：非敏感键的贪婪值会吞掉其后的 k=v（sub 不重叠），
    这里非敏感只前移到"值起点"继续扫，后续键照常处理。
    """
    parts: list[str] = []
    pos = 0
    while True:
        match = _QUERY_SPACE_RE.search(text, pos)
        if not match:
            parts.append(text[pos:])
            break
        parts.append(text[pos : match.start()])
        head = f"{match.group('key')}{match.group('q')}{match.group('sep')}"
        if _is_sensitive_key(match.group("key")):
            parts.append(head + "***")
            pos = match.end()
        else:
            parts.append(head)
            pos = match.start("val")
    return "".join(parts)


# 同时认单引号：异常里直接插值的 dict（f"{cfg}"）和 repr（{exc!r}）都是单引号形态，
# 只认双引号会让含密钥的 KeyError/ValueError 消息把密钥原样带进日志。
# 值用「回引号」收尾而不是 [^"']*：repr 对「值里含单引号」的串会改用双引号包裹
# （{'password': "ab'SECRET"}），按"遇到任意引号就停"会在第一个单引号处截断，
# 引号之后的部分原样漏进日志
# 值体里 (?!\\.) 让两个分支互斥：否则 "\x" 既能走 \\.、也能走 [\s\S]，一串反斜杠会让
# 回溯指数爆炸（实测 38 个反斜杠要 20 秒；而 http/parsers 拼错误信息时会先截断到 300 字符，
# 里面能装 ~290 个反斜杠 ≈ 永不返回）。触发方是不受控的第三方接口（回一个 400、或 200 但
# records_path 不匹配），且纯 Python 正则期间 Ctrl+C 也打断不了——必须在正则层面消掉歧义。
_JSON_RE = re.compile(r"""(?i)(["']([^"']{1,64})["']\s*:\s*)(?P<q>["'])((?:\\.|(?!\\.)(?!(?P=q))[\s\S])*)(?P=q)""")
# 键不带引号、值带引号（access_token='t-xxx' / app_secret: "xx"）：f-string 的 !r 插值与
# repr 的输出正好是这种形态，而 _QUERY_RE 的值部分 [^&\s"']+ 不吃引号——行中出现的这类
# 取值会整段漏遮（行首的由 _HEADER_RE 兜底，行中不会）。值体与 _JSON_RE 同款互斥分支，
# 转义引号与「另一种引号出现在值里」（repr 会改用另一种引号包裹）都能认。
_KV_QUOTED_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})(\s*[:=]\s*)(?P<q>[\"'])((?:\\.|(?!\\.)(?!(?P=q))[^\n])*)(?P=q)"
)
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer)\s+)[A-Za-z0-9._~+/=-]{6,}")
_BASIC_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S{8,}")
# URL 里的 userinfo（https://user:pass@host）：代理/接口地址常把账号密码写在地址里，
# requests 自己的 ProxyError 不带密码，但配置报错与 --check 概要会原样回显整条地址。
# scheme 部分限长（{0,63}）：无上限时在长小写字母数字串上会在每个起始位置贪婪回扫
# （实测 20KB 要 10 秒、40KB 要 50 秒），限长后整条规则保持线性
_URL_AUTH_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]{0,63}://[^/\s:@]+):([^\s/]+)@")
# 请求头行：'X-Api-Key: xxx' / 'X-Api-Key=xxx'（requests 抛错时带的 headers 是这种形态）。
# 上一条 Authorization 规则只认 Basic/Bearer 两种值，其余自定义头名要靠这里兜。
# 值要吃到行尾：只吃第一个词的话，"Authorization: Token abc…" 会变成 "*** abc…"
# （凭证明文留下）；整行遮掉最安全，行边界由 (?m) 的 ^/$ 兜住
_HEADER_RE = re.compile(r"(?im)^(\s*([A-Za-z0-9_.\-]{1,64})\s*[:=]\s*)(.+)$")
# 飞书 webhook 形态：open.feishu.cn/open-apis/bot/v2/hook/<id>；scheme 部分可选——
# requests 的异常消息里只带 URL 的路径（"Max retries exceeded with url: /open-apis/..."），
# 这时靠这个规则兜底，别让 hook id 明文进日志
# 可选前缀限长（{0,1024}）：无上限的惰性展开在"超长且无空白、又没有 /hook/"的
# 文本上会二次回溯（每个 https:// 起点都要扫到 token 末尾）；限长后保持线性。
# 真实 webhook 的 URL 前缀远短于 1024 字符。
_WEBHOOK_RE = re.compile(r"(?i)((?:https?://[^\s\"']{0,256}?)?/hook/)[A-Za-z0-9\-_]{4,}")


def _is_sensitive_key(name) -> bool:
    """字段名是否含密钥语义（按词切分，避免 "task=1" 这类含 sk 的普通参数被误伤）。"""
    original = str(name)
    # 切词要保留原大小写：_WORD_RE 的驼峰分支（[A-Z][a-z0-9]*）在已经 lower 的串上
    # 永远匹配不到——signStr / authKey 这类驼峰名会漏（而 snake_case 的孪生名却命中）
    words = _WORD_RE.findall(original)
    if any(word.lower() in _SENSITIVE_WORDS for word in words):
        return True
    lowered = original.lower()
    # 不用分隔符的写法：accesstoken / secretkey / accesskeyid
    # （"key" 不单独做子串规则，否则 monkey / keywords 这类普通参数会被误伤）
    return any(
        word in lowered
        for word in (
            "token",
            "secret",
            "password",
            "passwd",
            "signature",
            "apikey",
            "accesskey",
            "secretkey",
            "privatekey",
            "signkey",
            "keyid",
        )
    )


# 脱敏递归的深度上限：正常文本（嵌套 JSON、值里再嵌 k=v）深度 ≤3，
# 构造性文本（"a=b=c=…" 上千个等号）能把递归喂到 Python 上限（见 redact 的 _depth）。
# 注意：每一层的 query 规则与头行规则会各递归一次，深度上限同时决定最坏调用量
# （随深度指数增长），所以这个值要小——10 层已经远超正常文本所需。
_MAX_REDACT_DEPTH = 10


def redact(text: str, _depth: int = 0) -> str:
    """把文本里的密钥/签名/token 值替换成 ***，用于日志与异常信息。

    覆盖：URL query（?token=…）、请求体/配置片段（"secret_key": "…"）、
    请求头行（X-Api-Key: …，含 Authorization 的 Bearer/Basic）。
    密钥一旦进日志就等于泄露，宁可多脱敏。

    规则顺序按"认得出的形态"从严到宽：Bearer/Basic 与配置片段先处理——query 规则会
    按 `=` / `:` 把值截断，先跑它的话 `header: 'Authorization=Bearer abc123def'` 会被
    切成 `Authorization=`，后面的 Bearer 规则就再也匹配不到了（密钥原样留在日志里）。

    _depth：内部递归深度，调用方不要传。各回调会把匹配到的值再交给 redact 递归处理
    （嵌套 JSON、"值里还有 k=v 链"），正常文本深度 ≤3；构造性文本（如上千个等号的
    `a=b=c=…`、第三方响应体里的任意内容）能把深度喂到 Python 递归上限，
    把"脱敏"本身打成 RecursionError。到上限按"宁可多脱敏"整段遮掉。
    """
    if not text:
        return text
    if _depth >= _MAX_REDACT_DEPTH:
        return "***"

    def _bearer(match: re.Match) -> str:
        """Bearer / Basic 形态：scheme 保留，值换掉。"""
        return match.group(1) + "***"

    def _url_auth(match: re.Match) -> str:
        """URL 里的 userinfo：只留账号，密码换掉（scheme://user:***@host）。"""
        return f"{match.group(1)}:***@"

    def _webhook(match: re.Match) -> str:
        """飞书 webhook：保留 /hook/ 路径，hook id（凭证）换掉。"""
        return f"{match.group(1)}***"

    def _json(match: re.Match) -> str:
        """JSON/配置片段里的 "key": "value"：只吃字符串值，保留引号结构。"""
        quote = match.group("q")
        prefix, value = match.group(1), match.group(4)
        if _is_sensitive_key(match.group(2)):
            return f"{prefix}{quote}***{quote}"
        # 值本身可能是"被 JSON 编码成字符串的一整段 JSON"（接口把内层 JSON 当字符串返回，
        # 异常里就是 {"data": "{\"token\": \"xxx\"}"} 这种形态），这时内层的引号是 \"、
        # 任何按引号认边界的规则都匹配不到。反转义 → 脱敏 → 再转义回去
        if '\\"' in value:
            try:
                decoded = json.loads(f'"{value}"')
            except (ValueError, RecursionError):
                # RecursionError：值里含超深嵌套（构造性 payload）时 json.loads 会递归爆栈；
                # 脱敏流程不能反过来把进程打崩，按"反转义失败"处理
                decoded = None
            if decoded is not None:
                redacted = redact(decoded, _depth + 1)
                if redacted != decoded:
                    return f"{prefix}{quote}{json.dumps(redacted, ensure_ascii=False)[1:-1]}{quote}"
        # 键名不敏感时值里也可能藏着密钥（'X-Api-Key: xxx' 这种头行、查询串、
        # 嵌套的 {"auth": {"token": "…"}}），递归脱敏一次再放回去
        return f"{prefix}{quote}{redact(value, _depth + 1)}{quote}"

    def _kv_quoted(match: re.Match) -> str:
        """`key='value'` / `key: "value"`（键无引号、值有引号）：命中密钥词才遮值。"""
        key, gap, qchar, value = (match.group(1), match.group(2), match.group(3), match.group(4))
        head = f"{key}{gap}{qchar}"
        if _is_sensitive_key(key):
            return f"{head}***{qchar}"
        # 键名不敏感时值里也可能藏着密钥（'note=access_token=abc'）：递归一次兜底
        redacted = redact(value, _depth + 1)
        if redacted != value:
            return f"{head}{redacted}{qchar}"
        return match.group(0)

    def _query(match: re.Match) -> str:
        """URL 查询串里的 key=value / "key": 12345：命中密钥词才替换值，其余原样返回。

        没命中的值再看两层：① 递归脱敏（值里可能嵌着 'Authorization=Bearer xxx'）；
        ② 值是 URL 编码的整串（target=https%3A%2F%2F…%3Ftoken%3Dx）时，编码后的
        'token%3D…' 任何规则都匹配不到——解码后能识别出密钥就整段遮掉（宁可多脱敏）。
        """
        key, quote, sep, gap, value = (match.group(1), match.group(2), match.group(3), match.group(4), match.group(5))
        head = f"{key}{quote}{sep}{gap}"  # 原样保留引号/分隔符/空白，只换值
        if _is_sensitive_key(key):
            return f"{head}***"
        if "%" in value:
            try:
                decoded = unquote(value)
            except Exception:  # noqa: BLE001 - 解码失败按原文处理
                decoded = value
            if decoded != value and redact(decoded, _depth + 1) != decoded:
                return f"{head}***"
        return f"{head}{redact(value, _depth + 1)}"

    def _header(match: re.Match) -> str:
        """多行文本里的一行 "Header: value"：只吃头名命中密钥词 / 敏感头名的行。

        Cookie / Set-Cookie 不含 token/secret 这类词，但整行都是会话凭证，必须整值遮掉。
        """
        header_name = match.group(2)
        if header_name.lower() in _SECRET_HEADER_KEYS or _is_sensitive_key(header_name):
            return f"{match.group(1)}***"
        return f"{match.group(1)}{redact(match.group(3), _depth + 1)}"

    out = str(text)
    out = _BEARER_RE.sub(_bearer, out)
    out = _BASIC_RE.sub(_bearer, out)
    # URL userinfo 规则必须同时出现 "://" 与 "@" 才可能匹配，先做一次 O(n) 预判省掉
    # 一次无谓的全量扫描。该规则的 scheme 部分已限长（见 _URL_AUTH_RE），配合预判在
    # 长文本（整段十六进制转储、超长 token）上保持线性；没有预判 + 无上限的旧写法
    # 实测 20KB 要 10 秒、40KB 要 50 秒，且 C 层正则期间 Ctrl+C 也打断不了。
    # （与 sftp2ods 的同款修复保持一致，见其 utils.redact）
    if "://" in out and "@" in out:
        out = _URL_AUTH_RE.sub(_url_auth, out)
    out = _WEBHOOK_RE.sub(_webhook, out)
    # JSON 片段规则至少要出现引号才可能匹配：没引号的长文本直接跳过，省一遍全量扫描
    if '"' in out or "'" in out:
        out = _JSON_RE.sub(_json, out)
        out = _KV_QUOTED_RE.sub(_kv_quoted, out)
    # 敏感键 + 无引号值先整体遮到行尾（password=my secret），再走常规 query 扫描
    out = _mask_spaced_values(out)
    out = _QUERY_RE.sub(_query, out)
    # 头行规则放最后：它最宽松（只要求行首是 name: value），前面几条先处理过更精确的形态
    return _HEADER_RE.sub(_header, out)


# =============================================================================
# 值级脱敏：配置里的密钥值本身
# =============================================================================

_SECRET_MIN_LEN = 4
"""短于该长度的密钥值不做值级替换：`1` / `ok` 这种在普通文本里出现概率太高，
替换只会把报错信息搅乱，而真实凭证不会这么短。"""

# auth 配置里承载凭证的字段名（auth.py 各类型的取值字段：token 的 value、bearer 的
# token、basic 的 password、sha256_concat 的 secret_key、aliyun_rpc 的 access_key_*）。
# 结构性字段（type/header/prefix/sign_field…）不在此列。
_AUTH_SECRET_KEYS = frozenset(
    {
        "password",
        "value",
        "token",
        "secret_key",
        "secret",
        "access_key_id",
        "access_key_secret",
        "client_secret",
        "private_key",
    }
)
_SECRET_HEADER_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
    }
)
_AUTH_SCHEMES = frozenset({"basic", "bearer", "token", "digest"})
# 结构性字段后缀：值为配置项名/位置（sign_field="sign"、sign_in="body"），
# 不是凭证；不排除的话 "sign"、"body" 会被当密钥值把报错文本里这些常见词遮掉
_AUTH_STRUCT_SUFFIXES = ("_field", "_in", "_name", "_param")


def _auth_carrier(name: str) -> bool:
    """auth 配置里的键是否承载凭证：显式字段名、query 型的 params，
    或其余含密钥语义的键（自定义签名函数的字段名），但结构字段除外。"""
    if name in _AUTH_SECRET_KEYS or name == "params":
        return True
    return _is_sensitive_key(name) and not name.endswith(_AUTH_STRUCT_SUFFIXES)


def _leaf_strings(value) -> list[str]:
    """递归收集 dict/list/tuple/set 里的字符串叶子（数字也按 str 收：
    商户号/ID 类密钥写起来就是数字，报错里回显的是它的十进制形式）。"""
    if isinstance(value, dict):
        return [item for sub in value.values() for item in _leaf_strings(sub)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for sub in value for item in _leaf_strings(sub)]
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _with_scheme_bare(value: str) -> list[str]:
    """`Bearer sk-xxx` 除整串外再收集裸 token：接口常常只回显后半截。"""
    head, sep, tail = value.partition(" ")
    if sep and head.lower() in _AUTH_SCHEMES and tail.strip():
        return [value, tail.strip()]
    return [value]


# 飞书 webhook 里的 hook id（/hook/<id>）：报错/日志里常只出现后半截
_WEBHOOK_ID_RE = re.compile(r"/hook/([A-Za-z0-9\-_]{4,})")


def _webhook_values(value) -> list[str]:
    """webhook 除整条 URL 外，再收集裸 hook id（形态脱敏认 URL，裸 id 要靠值级遮）。"""
    result: list[str] = []
    for part in _leaf_strings(value):
        result.append(part)
        match = _WEBHOOK_ID_RE.search(part)
        if match:
            result.append(match.group(1))
    return result


def collect_secret_values(job: dict) -> list[str]:
    """收集作业配置里"可能被接口回显"的密钥字面量，按值脱敏的输入。

    形态识别（redact）盖不住接口把凭证写进**自由文本**的报错（如
    `Invalid token: sk-xxx`），但这类回显的内容必然是请求里用过的凭证，而凭证就
    躺在这几处配置里：job.secrets、request.auth 的凭证字段、request.headers 的
    密钥头、maxcompute 的 access key、notify.webhook（hook id 即凭证）。
    返回值已去重并按从长到短排序：短值先替会把长密钥切成半截、留下可辨认的碎片。
    """
    if not isinstance(job, dict):
        return []
    values: list[str] = _leaf_strings(job.get("secrets"))
    request_cfg = job.get("request")
    if isinstance(request_cfg, dict):
        auth_cfg = request_cfg.get("auth")
        if isinstance(auth_cfg, dict):
            for key, val in auth_cfg.items():
                if _auth_carrier(str(key)):
                    values += [part for value in _leaf_strings(val) for part in _with_scheme_bare(value)]
        # 固定请求参数里也可能直接夹着凭证（不经 auth 块的 API），按参数名判断
        params = request_cfg.get("params")
        if isinstance(params, dict):
            for key, val in params.items():
                if _is_sensitive_key(str(key)):
                    values += _leaf_strings(val)
        headers = request_cfg.get("headers")
        if isinstance(headers, dict):
            for key, val in headers.items():
                name = str(key).lower()
                if name in _SECRET_HEADER_KEYS or _is_sensitive_key(name):
                    values += [part for value in _leaf_strings(val) for part in _with_scheme_bare(value)]
        # URL 里的凭证：--init 会把带 query 的地址原样存进 path，而 requests 的连接类异常
        # 消息带着完整 URL（实测 "Max retries exceeded with url: /open/api/v2/bill?appkey=..."），
        # 参数名不在密钥词表里就漏遮了——把 URL query 里命中密钥词的参数值也纳入值级脱敏
        for key in ("base_url", "path"):
            query = urlparse(str(request_cfg.get(key) or "")).query
            if not query:
                continue
            for name, value in parse_qsl(query, keep_blank_values=False):
                if value and _is_sensitive_key(name):
                    values.append(value)
    maxcompute = job.get("maxcompute")
    if isinstance(maxcompute, dict):
        for key, val in maxcompute.items():
            if _is_sensitive_key(str(key)):
                values += _leaf_strings(val)
    notify_cfg = job.get("notify")
    if isinstance(notify_cfg, dict):
        values += _webhook_values(notify_cfg.get("webhook"))
    cleaned = (value.strip() for value in values)
    return sorted({value for value in cleaned if len(value) >= _SECRET_MIN_LEN}, key=len, reverse=True)


def redact_secrets(values, text: str) -> str:
    """值级 + 形态级双重脱敏：配置里的密钥值原样出现时也遮掉。

    形态规则认的是 `token=…` / `Bearer …` / `"key": "value"` 这类写法；接口若把
    凭证写进自由文本（`Invalid token: sk-xxx`），只有按配置值精确替换才挡得住。
    两条路互不替代，值级先遮、再走形态兜底。
    """
    if not text:
        return text
    if not isinstance(values, (list, tuple, set, frozenset)):
        # 单个字符串会被 set() 拆成单字符（值级脱敏静默失效、凭证明文进日志）；
        # int/None 等标量（secrets 配置直接写成数字）会让下面的 for 抛 TypeError、
        # 把真正的失败原因顶掉。一律按"只有一个密钥"包一层
        values = [values]
    text = str(text)  # 与 redact 同样的宽容度：调用方直接传异常对象/数字也不会炸
    # 非字符串的密钥值（数字等）先 str()：key=len 对 int 会抛 TypeError，
    # 与"宽容度"的契约不符（直接调用方传 [123] 也该能脱敏）；None/bool 不是密钥
    # （str 化后会把文本里的 "None"/"True" 误替成 ***），跳过
    secrets = {str(v) for v in (values or ()) if v is not None and not isinstance(v, bool)}
    for secret in sorted(secrets, key=len, reverse=True):
        # 短值（< _SECRET_MIN_LEN）连值级替换也要挡：否则 "SEC" 会把别的密钥切成
        # "***RET-…"——既没遮住，还把报错信息搅乱。值从 collect_secret_values 来时
        # 已经过滤过，这里再守一道是为了直接调用本函数的入口（防以后新调用方）。
        if len(secret) < _SECRET_MIN_LEN:
            continue
        # 凭证可能以 URL 编码形态出现在自由文本里（`+`/`/`/`=` 会被 quote 编码；
        # 部分编码器更激进，连 `-` 这类字符也编码成 %2D），而自由文本没有可识别的
        # 键名，形态规则挡不住；只替明文会漏。明文、quote、quote_plus 与"非字母数字
        # 全编码"四种形态一起替换（长值优先的排序不变）。
        # 按字节（不是 chr(b) 的 Latin-1 字符）判断：>=0x80 的字节在 Latin-1 里常恰好是
        # "字母"（0xE5='å'），原样保留会让含中文的密钥生成错误的编码变体、漏遮
        aggressive = "".join(
            f"%{b:02X}" if not (b < 128 and chr(b).isalnum()) else chr(b)
            for b in secret.encode("utf-8", "surrogatepass")
        )
        try:
            encoded_forms = (quote(secret, safe=""), quote_plus(secret))
        except UnicodeError:
            # 含孤立代理字符的密钥（surrogateescape 解出的路径名被登记为敏感值）：
            # quote 内部 strict 编码会抛——跳过编码变体，绝不让脱敏反过来打崩业务
            encoded_forms = ()
        for variant in (secret, *encoded_forms, aggressive):
            if variant:
                text = text.replace(variant, "***")
    return redact(text)


# =============================================================================
# 通用重试
# =============================================================================


# requests 的「确定性」异常：请求根本没发出去（缺 scheme、URL/请求头非法），重试多少次
# 都是同一结果。都是 ValueError 子类，直接加 ValueError 会把"响应体解析失败"这类
# 可能重试成功的错误也卷进来，所以按类型点名；requests 未安装时为空
try:  # pragma: no cover - requests 未安装的离线环境走空元组
    from requests import exceptions as _requests_exceptions

    DETERMINISTIC_HTTP_ERRORS: tuple[type[BaseException], ...] = tuple(
        exc_type
        for name in ("MissingSchema", "InvalidSchema", "InvalidURL", "InvalidHeader", "URLRequired")
        if isinstance(exc_type := getattr(_requests_exceptions, name, None), type)
    )
except ImportError:  # pragma: no cover
    DETERMINISTIC_HTTP_ERRORS = ()


def retry_call(
    fn,
    attempts: int = 5,
    base_delay: float = 15,
    desc: str = "",
    fatal=(FatalApiError,),
    max_delay: float = 300,
    secrets=(),
):
    """执行 fn，瞬时错误指数退避重试；FatalApiError 与调用方声明的不重试异常直接抛出。

    重试日志与最终异常都会做脱敏，避免把 URL 里的签名/token 打进日志。
    secrets 给定时（如 collect_secret_values 的结果）：除了形态规则，再按配置里的密钥值
    精确遮蔽——底层 SDK/Tunnel 把凭证写进自由文本报错时，形态规则盖不住（与 sftp2ods 同口径）。
    """
    if attempts < 1:
        # attempts<=0 时循环体一次都不执行，last_err 保持 None，最终报错会变成
        # "重试 -1 次仍失败：None"（丢失失败原因）——提前给一句明确的参数错误
        raise ValueError(f"retry_call 的 attempts 必须 >= 1，当前 {attempts}")
    delay = min(base_delay, max_delay)  # 首次退避同样受 max_delay 约束
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except fatal:
            raise
        except (
            TypeError,
            AttributeError,
            KeyError,
            NameError,
            ImportError,
            *DETERMINISTIC_HTTP_ERRORS,
        ) as exc:
            # 确定性编程错误：重试多少次都是同一个结果，退避只会白等几分钟、
            # 还把原始错误类型包成 RuntimeError 掩盖掉（与 sftp2ods 同口径）。
            # DETERMINISTIC_HTTP_ERRORS：requests 的 MissingSchema/InvalidURL/InvalidHeader
            # 等（都是 ValueError 子类）——请求根本没发出去，重试无意义
            raise RuntimeError(
                f"{desc} 出现确定性错误（不重试）：{type(exc).__name__}: {redact_secrets(secrets, str(exc))}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 网络/服务端类错误统一重试
            last_err = exc
            if attempt == attempts:
                break
            # 分子/分母都按"总尝试次数"口径，避免写成 第 x/(n-1) 次 这种对不上的读法
            log(f"  [{desc} 第 {attempt}/{attempts} 次尝试失败] {redact_secrets(secrets, str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # 报"重试 N-1 次"（成功那次之外又试了几次），和 http.py 的口径一致：
    # 写 attempts 会让人以为总共发了 attempts+1 个请求，对不上实际请求数；
    # from last_err 保住原始异常链（调用方按异常类型分流、看底层 SDK 栈都需要它）
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{redact_secrets(secrets, str(last_err))}") from last_err
