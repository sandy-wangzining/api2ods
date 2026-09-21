# -*- coding: utf-8 -*-
"""通用工具：控制台、日志（可写文件副本）、运行锁、密钥脱敏、通用重试。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl  # Linux：进程级运行锁；Windows 无此模块，退化为不加锁
except ImportError:  # pragma: no cover
    fcntl = None


class FatalApiError(RuntimeError):
    """参数/权限/业务类错误：重试没有意义，立刻失败（如 HTTP 401/400、签名错误）。"""


_lock = threading.Lock()
_sinks: list = []
_console_patched = False


def setup_console() -> None:
    """stdout/stderr 切 UTF-8，避免 Windows 控制台中文乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 某些重定向流不支持 reconfigure（如 CI 捕获）
            pass


def add_log_sink(handle) -> None:
    """把日志再写一份到文件（--log-file），句柄由调用方负责关闭。"""
    with _lock:
        _sinks.append(handle)


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

    - Linux（正式环境）：对锁文件加 flock 排它锁；拿不到说明已有任务在跑；
    - Windows（本地开发）：无 fcntl，直接放行（本机不作为运行环境）；
    - 锁随进程退出自动释放，进程被 kill 也由内核释放，不会残留死锁。
    """

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise SystemExit(
                f"已有任务在运行（锁文件 {self.path}），本次退出；"
                f"确认没有任务在跑时可删除该文件后重试。"
            )
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc_info):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()


# =============================================================================
# 脱敏：日志/异常里不出现密钥、签名、token
# =============================================================================

_SENSITIVE_KEY = (
    r"sign|signature|token|access_token|refresh_token|secret|secret_key|password|"
    r"authorization|apikey|api_key|accesskeyid|access_key_id|access_key_secret|"
    r"ak_secret|sk|passwd"
)
_QUERY_RE = re.compile(rf"(?i)\b({_SENSITIVE_KEY})=([^&\s\"']+)")
_JSON_RE = re.compile(rf"(?i)(\"(?:{_SENSITIVE_KEY})\"\s*:\s*\")([^\"]+)(\")")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}")


def redact(text: str) -> str:
    """把文本里的密钥/签名/token 值替换成 ***，用于日志与异常信息。"""
    if not text:
        return text
    out = _QUERY_RE.sub(r"\1=***", str(text))
    out = _JSON_RE.sub(r"\1***\3", out)
    out = _BEARER_RE.sub(r"\1***", out)
    return out


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
    raise RuntimeError(f"{desc} 重试 {attempts} 次仍失败：{redact(str(last_err))}")
