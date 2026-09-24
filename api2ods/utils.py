# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

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
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
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
        try:
            # "a+" 而不是 "w"：w 会在打开时把文件截断，持锁进程刚写进去的 pid 就被抹掉了
            # （锁本身是文件区域锁，与文件内容无关，互斥不受影响；丢的是排障用的"谁在跑"）
            self.fh = open(self.path, "a+")
        except OSError as exc:
            # 父目录被删/路径过长（Windows MAX_PATH）时给一句人话，
            # 而不是让 FileNotFoundError 以裸 traceback 的形式糊在用户脸上
            raise SystemExit(
                f"无法创建运行锁文件 {self.path}（{exc}）；请检查该路径所在目录是否存在/可写，或用 --job 指定别处的作业"
            )
        if not _try_lock(self.fh):
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
    return True  # 两种锁都没有：不阻塞（退回"无锁"行为）


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
    return bool(value)


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
    text = str(value)
    if not _IDENT_RE.match(text):
        raise ConfigError(f"{where} 不是合法的 MaxCompute 标识符：{text!r}；只允许字母/数字/下划线且不能以数字开头")
    return text


def check_header_values(headers: dict) -> None:
    """请求头的值必须是"能直接发出去"的字符串，否则提前给配置错。

    requests 对"值首尾有空白/含换行/含非 latin-1 字符"只会抛 InvalidHeader，
    而且**消息里带着头的原值**——密钥会跟着进日志与最终异常（实测一次运行漏十几行）。
    更糟的是它属于"一个字节都没发出去"的确定性错误，被当成网络抖动退避能白等 20 多分钟。
    所以在这里提前拦下，报错只给头名、不回显值。
    """
    for name, value in (headers or {}).items():
        text = str(value)
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
# 参数名做左边界限制（不用 \b：下划线在正则里算词字符，client_secret 会被漏掉）
_QUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_.\-]{1,64})=([^&\s\"']+)")
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
_BEARER_RE = re.compile(r"(?i)(\b(?:bearer)\s+)[A-Za-z0-9._~+/=-]{6,}")
_BASIC_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S{8,}")
# URL 里的 userinfo（https://user:pass@host）：代理/接口地址常把账号密码写在地址里，
# requests 自己的 ProxyError 不带密码，但配置报错与 --check 概要会原样回显整条地址
_URL_AUTH_RE = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^/\s:@]+):([^/\s@]+)@")
# 请求头行：'X-Api-Key: xxx' / 'X-Api-Key=xxx'（requests 抛错时带的 headers 是这种形态）。
# 上一条 Authorization 规则只认 Basic/Bearer 两种值，其余自定义头名要靠这里兜。
# 值要吃到行尾：只吃第一个词的话，"Authorization: Token abc…" 会变成 "*** abc…"
# （凭证明文留下）；整行遮掉最安全，行边界由 (?m) 的 ^/$ 兜住
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

    def _url_auth(match: re.Match) -> str:
        """URL 里的 userinfo：只留账号，密码换掉（scheme://user:***@host）。"""
        return f"{match.group(1)}:***@"

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
            except ValueError:
                decoded = None
            if decoded is not None:
                redacted = redact(decoded)
                if redacted != decoded:
                    return f"{prefix}{quote}{json.dumps(redacted, ensure_ascii=False)[1:-1]}{quote}"
        # 键名不敏感时值里也可能藏着密钥（'X-Api-Key: xxx' 这种头行、查询串、
        # 嵌套的 {"auth": {"token": "…"}}），递归脱敏一次再放回去
        return f"{prefix}{quote}{redact(value)}{quote}"

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
    out = _URL_AUTH_RE.sub(_url_auth, out)
    # JSON 片段规则至少要出现引号才可能匹配：没引号的长文本直接跳过，省一遍全量扫描
    if '"' in out or "'" in out:
        out = _JSON_RE.sub(_json, out)
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


def collect_secret_values(job: dict) -> list[str]:
    """收集作业配置里"可能被接口回显"的密钥字面量，按值脱敏的输入。

    形态识别（redact）盖不住接口把凭证写进**自由文本**的报错（如
    `Invalid token: sk-xxx`），但这类回显的内容必然是请求里用过的凭证，而凭证就
    躺在这几处配置里：job.secrets、request.auth 的凭证字段、request.headers 的
    密钥头、maxcompute 的 access key。收集出来按值替换即可。
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
    text = str(text)  # 与 redact 同样的宽容度：调用方直接传异常对象/数字也不会炸
    for value in sorted(set(values or ()), key=len, reverse=True):
        # 短值（< _SECRET_MIN_LEN）连值级替换也要挡：否则 "SEC" 会把别的密钥切成
        # "***RET-…"——既没遮住，还把报错信息搅乱。值从 collect_secret_values 来时
        # 已经过滤过，这里再守一道是为了直接调用本函数的入口（防以后新调用方）。
        if len(value) >= _SECRET_MIN_LEN and value in text:
            text = text.replace(value, "***")
    return redact(text)


# =============================================================================
# 通用重试
# =============================================================================


def retry_call(
    fn, attempts: int = 5, base_delay: float = 15, desc: str = "", fatal=(FatalApiError,), max_delay: float = 300
):
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
