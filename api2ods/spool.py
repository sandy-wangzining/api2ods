# -*- coding: utf-8 -*-
"""Spool：把拉到的记录逐行写入本地临时 JSONL 文件。

为什么需要它：一次运行可能拉到几十万~几百万条记录（多天窗口），全量放内存既慢又危险。
拉取阶段边拉边写盘（顺序写，很便宜），写入 MaxCompute 时再一行行读回来送进 Tunnel，
跨请求单元不累积——峰值内存只跟"单个请求单元（一天 / range 模式的整个区间）的数据量"
有关，跟一共拉了多少天无关。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .utils import progress_log, redact

# JSON 序列化参数：紧凑（不产生多余空格）、中文不转义（和后端存储口径一致）、
# allow_nan=False：NaN/Infinity 不是合法 JSON，落盘后 MaxCompute 侧取不到值
# （下游静默变 NULL），宁可在这里失败
_JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":"), "allow_nan": False}


def _reject_constant(name: str):
    """json 解析时遇到 NaN/Infinity 直接报错：它们不是合法 JSON。

    默认 allow_nan=True 会把它们解析成 float('nan')，落盘再写成 NaN（非法 JSON），
    MaxCompute 侧 get_json_object 取不到值、下游静默变 NULL。宁可拉取阶段就失败。
    """
    raise ValueError(f"接口返回的 JSON 里有非法数值 {name}")


def loads_json(text: str):
    """解析接口返回的 JSON（拒绝 NaN/Infinity）。"""
    return json.loads(text, parse_constant=_reject_constant)


def dump_record(record) -> str:
    """一条记录 → 单行 JSON（全工具唯一的序列化出口）。"""
    try:
        return json.dumps(record, **_JSON_KWARGS)
    except (TypeError, ValueError) as exc:
        # TypeError 与 ValueError 都要接：不可序列化的对象（datetime/set/自定义对象）抛的是
        # TypeError，只接 ValueError 会让它绕过本处的脱敏与中文报错、变成裸 traceback
        # 先过 redact 再截断：这条报错的正文会被上层原样打进控制台 / --log-file /
        # 调度告警，前 200 字符里完全可能带着 token / 手机号这类密钥与 PII
        # （dump_record 是唯一序列化出口，在这里堵住就断掉了整条泄漏链）
        snippet = redact(str(record))[:200]
        raise RuntimeError(
            f"记录里有无法序列化的值（{exc}）：{snippet}；"
            f"源接口返回了 NaN/Infinity 之类的非法 JSON，写进 ODS 后下游取不到值，已中止"
        )


class SpoolWriter:
    """线程安全的 JSONL 落盘器（fetch 阶段多个并发单元会同时往里写）。"""

    def __init__(self, path: Path | None = None):
        """path 为 None 时在系统临时目录建文件（mkstemp 占位后 fdopen，避免关 fd 再按路径重开）。"""
        if path is None:
            handle, name = tempfile.mkstemp(prefix="api2ods-", suffix=".jsonl")
            try:
                self.path = Path(name)
                self._handle = os.fdopen(handle, "w", encoding="utf-8", newline="\n")
            except Exception:
                try:
                    os.close(handle)
                except OSError:
                    pass
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
        else:
            self.path = Path(path)
            self._handle = open(self.path, "w", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._closed = False
        self.count = 0  # 已写入记录数
        self.bytes = 0  # 已写入字节数（近似值，含换行）

    def write_records(self, records: list) -> int:
        """把一批记录序列化后写入文件（返回本批条数）。"""
        with self._lock:
            if self._closed:
                raise RuntimeError(f"落盘文件已关闭，不能再写入：{self.path}")
            for record in records:
                line = dump_record(record)
                self._handle.write(line + "\n")
                self.bytes += len(line.encode("utf-8")) + 1
                self.count += 1
                progress_log("已落盘", self.count)
            # 每批落完 flush 一次：日志说"已落盘"而数据还在缓冲里时，
            # 进程被 kill 会让日志行数与磁盘实际内容对不上（逐条 flush 又太贵）
            self._handle.flush()
            return len(records)

    def iter_rows(self):
        """重新从头逐行读出（可多次调用：写库失败重试时会重新读一遍）。

        读回必须在"全部写入结束"之后：fetch 全部完成后才进入写库阶段。
        **不在 yield 期间持锁**：重试路径会把上一趟失败的 traceback 留在引用里，
        被 traceback 引用住的生成器不会关闭——若它在 yield 时持锁，下一趟 iter_rows
        会永远等在这把锁上（写库重试直接死锁）。flush 在锁内做（避免读到半行），
        读出本身要求调用方遵守"不再并发写"的契约。
        """
        with self._lock:
            if self._closed:
                # 关闭后文件可能已被删除（keep=False）：原来会抛没有上下文的
                # FileNotFoundError；明确报错，与 write_records 的关闭口径一致
                raise RuntimeError(f"落盘文件已关闭，不能再读回：{self.path}")
            self._handle.flush()
        with open(self.path, "r", encoding="utf-8", newline="") as handle:
            for line in handle:  # 逐行读：对超长行（大 JSON）也安全
                line = line.rstrip("\n")
                if line:
                    yield line

    def iter_batches(self, batch_size: int = 1000, max_bytes: int = 8_000_000):
        """按批读回，每批 ≤ batch_size 行且 ≤ max_bytes 字节。

        写入 MaxCompute 用：单条记录很大时，逐行读（iter_rows）+ 客户端攒批会让内存
        随批次膨胀；这里在读取侧就切好批。
        """
        batch: list[str] = []
        size = 0
        for row in self.iter_rows():
            row_bytes = len(row.encode("utf-8"))
            if batch and (len(batch) >= batch_size or size + row_bytes > max_bytes):
                yield batch
                batch, size = [], 0
            batch.append(row)
            size += row_bytes
        if batch:
            yield batch

    def close(self, keep: bool = False) -> None:
        """关闭并（默认）删除临时文件；keep=True 时保留（排障/手工重传用）。

        与 write_records 共用一把锁：并发单元还在写时不能先关句柄；关完置位，
        之后再写入给出明确错误，而不是 "I/O operation on closed file"。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.close()
            finally:
                if not keep:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass

    def __enter__(self):
        """支持 with 写法（目前调用方是按需 close，见 __exit__ 注释）。"""
        return self

    def __exit__(self, *exc_info):
        """退出 with 块：有异常保留文件，否则删除。"""
        # 出任何异常都保留文件，方便排查（正常退出则删掉）。
        # 目前调用方（cli.run_sync）不用 with，是按需调 close(keep=...)
        self.close(keep=bool(exc_info and exc_info[0]))
