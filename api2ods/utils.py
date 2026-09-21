# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

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
PROGRESS_EVERY = 1000      # 进度日志节流：每 N 条记录打一次
_sinks: list = []
_console_patched = False


def setup_console() -> None:
    """stdout/stderr 切 UTF-8，避免 Windows 控制台中文乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 某些重定向流不支持 reconfigure（如 CI 捕获）
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


def reset_log_once() -> None:
    """清空"已打过的告警"记录（每次运行开始时调，见 cli.main）。

    不重置的话，同一进程里第二次调用 main（测试、嵌入调用）会静默吞掉
    第一次已经打过的那条告警——用户看不到任何提示。
    """
    with _lock:
        _logged_once.clear()


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
        setup_console()
        _console_patched = True

    import datetime as _dt

    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with _lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = (getattr(sys.stdout, "encoding", None) or "utf-8")
            safe = line.encode(encoding, "replace").decode(encoding, "replace")
            print(safe, flush=True)
        for handle in _sinks:
            try:
                handle.write(line + "\n")
                handle.flush()
            except Exception:  # noqa: BLE001 - 日志文件问题不影响主流程
                pass


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
            return self
        self.fh = open(self.path, "w")
        if not _try_lock(self.fh):
            self.fh.close()
            self.fh = None
            raise SystemExit(
                f"已有任务在运行（锁文件 {self.path}），本次退出；"
                f"确认没有任务在跑时可删除该文件后重试。"
            )
        try:
            self.fh.write(str(os.getpid()))
            self.fh.flush()
        except OSError:          # 写进程号只是标记，失败不影响加锁
            pass
        return self

    def __exit__(self, *exc_info):
        """解锁并关句柄；锁文件本身保留（不删文件，避免削掉别人的锁）。"""
        if self.fh is not None:
            try:
                _unlock(self.fh)
            finally:
                self.fh.close()


def _try_lock(fh) -> bool:
    """对已打开的文件加排它锁；别人拿着锁时返回 False（不阻塞等待）。"""
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    return True          # 两种锁都没有：不阻塞（退回"无锁"行为）


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


def as_bool(value, default: bool) -> bool:
    """配置里的布尔值：JSON 写 true/false、字符串 "true"/"false"、0/1 都认。

    JSON 里写 "false"（带引号）是很常见的笔误，直接按真值判断会当成开，静默走错分支
    ——对 allow_empty 这类开关来说，走错的代价是"把已有分区清空"。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


# =============================================================================
# 脱敏：日志/异常里不出现密钥、签名、token
# =============================================================================

# 密钥字段名按「词」判断：先按下划线/中划线/驼峰切开再看每个词，这样
# accessToken / client_secret / X-Api-Key 都能认出来，而 task=? 不会因为含 "sk" 被误伤
_SENSITIVE_WORDS = {
    "sign", "signature", "sig", "token", "secret", "password", "passwd", "authorization",
    "auth", "apikey", "key", "accesskey", "sk", "ak",
}
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
# 参数名做左边界限制（不用 \b：下划线在正则里算词字符，client_secret 会被漏掉）
_QUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})=([^&\s\"']+)")
# 同时认单引号：异常里直接插值的 dict（f"{cfg}"）和 repr（{exc!r}）都是单引号形态，
# 只认双引号会让含密钥的 KeyError/ValueError 消息把密钥原样带进日志
_JSON_RE = re.compile(r"""(?i)(["']([^"']{1,64})["']\s*:\s*["'])([^"']*)(["'])""")
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer)\s+)[A-Za-z0-9._~+/=-]{6,}")
_BASIC_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S{8,}")
# 请求头行：'X-Api-Key: xxx' / 'X-Api-Key=xxx'（requests 抛错时带的 headers 是这种形态）。
# 上一条 Authorization 规则只认 Basic/Bearer 两种值，其余自定义头名要靠这里兜。
# 值要吃到行尾：只吃第一个词的话，"Authorization: Token abc…" 会变成 "*** abc…"
#（凭证明文留下）；整行遮掉最安全，行边界由 (?m) 的 ^/$ 兜住
_HEADER_RE = re.compile(r"(?im)^(\s*([A-Za-z0-9_.\-]{1,64})\s*[:=]\s*)(.+)$")


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
    return any(word in lowered for word in
               ("token", "secret", "password", "passwd", "signature",
                "apikey", "accesskey", "secretkey", "privatekey", "signkey", "keyid"))


def redact(text: str) -> str:
    """把文本里的密钥/签名/token 值替换成 ***，用于日志与异常信息。

    覆盖：URL query（?token=…）、请求体/配置片段（"secret_key": "…"）、
    请求头行（X-Api-Key: …，含 Authorization 的 Bearer/Basic）。
    密钥一旦进日志就等于泄露，宁可多脱敏。

    规则顺序按"认得出的形态"从严到宽：Bearer/Basic 与配置片段先处理——query 规则会
    按 `=` / `:` 把值截断，先跑它的话 `header: 'Authorization=Bearer abc123def'` 会被
    切成 `Authorization=`，后面的 Bearer 规则就再也匹配不到了（密钥原样留在日志里）。
    """
    if not text:
        return text

    def _bearer(match: re.Match) -> str:
        """Bearer / Basic 形态：scheme 保留，值换掉。"""
        return match.group(1) + "***"

    def _json(match: re.Match) -> str:
        """JSON/配置片段里的 "key": "value"：只吃字符串值，保留引号结构。"""
        if _is_sensitive_key(match.group(2)):
            return f"{match.group(1)}***{match.group(4)}"
        # 键名不敏感时值里也可能藏着密钥（'X-Api-Key: xxx' 这种头行、查询串、
        # 嵌套的 {"auth": {"token": "…"}}），递归脱敏一次再放回去
        return f"{match.group(1)}{redact(match.group(3))}{match.group(4)}"

    def _query(match: re.Match) -> str:
        """URL 查询串里的 key=value：命中密钥词才替换值，其余原样返回。

        没命中的值再看两层：① 递归脱敏（值里可能嵌着 'Authorization=Bearer xxx'）；
        ② 值是 URL 编码的整串（target=https%3A%2F%2F…%3Ftoken%3Dx）时，编码后的
        'token%3D…' 任何规则都匹配不到——解码后能识别出密钥就整段遮掉（宁可多脱敏）。
        """
        if _is_sensitive_key(match.group(1)):
            return f"{match.group(1)}=***"
        value = match.group(2)
        if "%" in value:
            try:
                decoded = unquote(value)
            except Exception:  # noqa: BLE001 - 解码失败按原文处理
                decoded = value
            if decoded != value and redact(decoded) != decoded:
                return f"{match.group(1)}=***"
        return f"{match.group(1)}={redact(value)}"

    def _header(match: re.Match) -> str:
        """多行文本里的一行 "Header: value"：只吃头名命中密钥词的行。"""
        if _is_sensitive_key(match.group(2)):
            return f"{match.group(1)}***"
        return f"{match.group(1)}{redact(match.group(3))}"

    out = str(text)
    out = _BEARER_RE.sub(_bearer, out)
    out = _BASIC_RE.sub(_bearer, out)
    out = _JSON_RE.sub(_json, out)
    out = _QUERY_RE.sub(_query, out)
    # 头行规则放最后：它最宽松（只要求行首是 name: value），前面几条先处理过更精确的形态
    return _HEADER_RE.sub(_header, out)


# =============================================================================
# 通用重试
# =============================================================================

def retry_call(fn, attempts: int = 5, base_delay: float = 15, desc: str = "",
               fatal=(FatalApiError,), max_delay: float = 300):
    """执行 fn，瞬时错误指数退避重试；FatalApiError 与调用方声明的不重试异常直接抛出。

    重试日志与最终异常都会做脱敏，避免把 URL 里的签名/token 打进日志。
    """
    delay = base_delay
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except fatal:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/服务端类错误统一重试
            last_err = exc
            if attempt == attempts:
                break
            log(f"  [{desc} 第 {attempt}/{attempts - 1} 次失败] {redact(str(exc))}；{delay:g}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    # 报"重试 N-1 次"（成功那次之外又试了几次），和 http.py 的口径一致：
    # 写 attempts 会让人以为总共发了 attempts+1 个请求，对不上实际请求数
    raise RuntimeError(f"{desc} 重试 {attempts - 1} 次仍失败：{redact(str(last_err))}")
