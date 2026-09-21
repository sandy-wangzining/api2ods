# -*- coding: utf-8 -*-
"""Spool：把拉到的记录逐行写入本地临时 JSONL 文件。

为什么需要它：一次运行可能拉到几十万~几百万条记录（多天窗口），全量放内存既慢又危险。
拉取阶段边拉边写盘（顺序写，很便宜），写入 MaxCompute 时再一行行读回来送进 Tunnel，
整个过程内存占用只跟"批大小"有关，跟数据总量无关。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

# JSON 序列化参数：紧凑（不产生多余空格）、中文不转义（和后端存储口径一致）
_JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":")}


class SpoolWriter:
    """线程安全的 JSONL 落盘器（fetch 阶段多个并发单元会同时往里写）。"""

    def __init__(self, path: Path | None = None):
        if path is None:
            handle, name = tempfile.mkstemp(prefix="api2ods-", suffix=".jsonl")
            os.close(handle)
            path = Path(name)
        self.path = Path(path)
        self._handle = open(self.path, "w", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self.count = 0          # 已写入记录数
        self.bytes = 0          # 已写入字节数（近似值，含换行）

    def write_records(self, records: list) -> int:
        """把一批记录序列化后写入文件（返回本批条数）。"""
        with self._lock:
            for record in records:
                line = json.dumps(record, **_JSON_KWARGS)
                self._handle.write(line + "\n")
                self.bytes += len(line.encode("utf-8")) + 1
                self.count += 1
            return len(records)

    def iter_rows(self):
        """重新从头逐行读出（可多次调用：写库失败重试时会重新读一遍）。"""
        self._handle.flush()
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if line:
                    yield line

    def close(self, keep: bool = False) -> None:
        """关闭并（默认）删除临时文件；keep=True 时保留（排障/手工重传用）。"""
        try:
            self._handle.close()
        finally:
            if not keep:
                try:
                    self.path.unlink()
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        # 异常退出时保留文件，方便排查（调用方也可以显式 close(keep=...)）
        self.close(keep=bool(exc_info and exc_info[0]))
