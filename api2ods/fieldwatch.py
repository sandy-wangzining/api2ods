# -*- coding: utf-8 -*-
"""字段快照：检测接口记录里的「新增字段」，只提醒、不阻塞入库。

api2ods 的 ODS 是裸 json 列：接口加字段不会写错数据、也不用改表，但下游 DWD 用
get_json_object 取字段，源侧结构变化最好有人知道。做法：按作业把每次**成功运行**观察到
的字段集合存成快照（作业同目录 .field-state/ 下），下次运行对比快照，出现新字段即发提醒。

口径：
- 快照只在"真实写库成功"之后更新（--dry-run 只对比、不落盘；失败不更新），
  与各工具"台账只在写成功后记账"的口径一致；
- 观察是逐批 union，内存只跟字段数有关、与数据量无关；
- 快照读写失败只记日志，绝不影响主流程与退出码。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .utils import log

STATE_DIR_NAME = ".field-state"


class FieldObserver:
    """收集一次运行观察到的记录字段名（fetch 阶段多线程并发回调，内部加锁）。"""

    def __init__(self) -> None:
        self.fields: set[str] = set()
        self._lock = threading.Lock()

    def update(self, records) -> None:
        """拿一批记录（逐条取 dict 的键）并进字段集合。"""
        names = {str(key) for record in records if isinstance(record, dict) for key in record}
        with self._lock:
            self.fields.update(names)


def snapshot_path(job_path: Path) -> Path:
    """快照文件路径：作业同目录 `.field-state/<作业名>-<路径哈希8>.json`。

    带哈希：`jobs/a/task.json` 与 `jobs/b/task.json` 同名不同作业，不能互相覆盖。
    """
    stem = job_path.stem or "job"
    digest = hashlib.sha1(str(job_path).encode("utf-8")).hexdigest()[:8]
    return job_path.parent / STATE_DIR_NAME / f"{stem}-{digest}.json"


def load_snapshot(job_path: Path) -> set[str] | None:
    """读上次的字段快照；没有快照返回 None；文件损坏时告警并按首次运行处理。"""
    path = snapshot_path(job_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            # 合法 JSON 但非对象（数组/字符串/null）时 data.get 抛的是 AttributeError，
            # 不在捕获范围内、会中断主流程；统一归一成 ValueError 走告警分支
            raise ValueError(f"顶层应是 JSON 对象，实际是 {type(data).__name__}")
        fields = data.get("fields")
        if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
            raise ValueError("fields 不是字符串数组")
        return set(fields)
    except (OSError, ValueError) as exc:  # JSONDecodeError 是 ValueError 的子类
        log(f"  警告：字段快照读取失败（{path}）：{exc}；本次按首次运行处理")
        return None


def save_snapshot(job_path: Path, fields, job_name: str) -> None:
    """写字段快照（先写 .tmp 再替换，避免半截 JSON）；失败只记日志。"""
    names = sorted(str(item) for item in fields)
    if not names:
        return
    path = snapshot_path(job_path)
    # updated_at 用带时区的 UTC：不同机器/时区下写出的快照可直接比较（本地朴素时间跨时区会误判新旧）
    payload = {"job": job_name, "fields": names, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            try:
                tmp = os.fdopen(handle, "w", encoding="utf-8")
            except Exception:
                # fdopen 失败时 fd 还没被接管：显式关闭，否则文件描述符泄漏到进程退出
                try:
                    os.close(handle)
                except OSError:
                    pass
                raise
            with tmp:
                tmp.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        log(f"  警告：字段快照写入失败（{path}）：{exc}（不影响本次数据）")
