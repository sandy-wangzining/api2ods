# -*- coding: utf-8 -*-
"""响应解析：JSON 路径取值、JSON 记录提取、文件类响应（ZIP/CSV/TSV/JSONL）。"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile

_PATH_TOKEN_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def get_path(data, path: str, default=None):
    """按 'a.b[0].c' 形式的路径取值；任一段缺失返回 default。空路径返回 data 本身。"""
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        match = _PATH_TOKEN_RE.match(part)
        if not match:
            return default
        name, indexes = match.groups()
        if name:
            if not isinstance(current, dict) or name not in current:
                return default
            current = current[name]
        for index in _INDEX_RE.findall(indexes):
            if not isinstance(current, (list, tuple)) or int(index) >= len(current):
                return default
            current = current[int(index)]
    return current


def extract_json_records(payload, request_cfg: dict, label: str, missing_ok: bool = False) -> list:
    """从 JSON 响应里取记录列表（records_path）；取不到时抛出带诊断信息的错误。

    missing_ok=True（request.records_missing=empty）：路径不存在时按空列表处理，
    适用于"零数据日返回空对象"的接口（如阿里云账单 Items: {}）。
    """
    records_path = str(request_cfg.get("records_path") or "")
    records = get_path(payload, records_path, default=None)
    if records is None:
        if missing_ok:
            return []
        top_keys = list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__
        snippet = json.dumps(payload, ensure_ascii=False)[:300] if isinstance(payload, (dict, list)) else str(payload)[:300]
        hint = (f"找不到 records_path={records_path!r}（顶层键：{top_keys}）" if records_path
                else "records_path 为空且返回不是记录数组")
        raise RuntimeError(f"{label} 解析失败：{hint}；返回片段：{snippet}")
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise RuntimeError(f"{label} records_path 指向的不是数组/对象：{type(records).__name__}")
    return records


def _parse_text(text: str, parse_cfg: dict, entry: str = "") -> list[dict]:
    """文本 → 记录列表：csv / tsv / jsonl。"""
    fmt = str(parse_cfg.get("format") or "csv").lower()
    delimiter = str(parse_cfg.get("delimiter") or ("\t" if fmt == "tsv" else ","))
    skip_rows = int(parse_cfg.get("skip_rows") or 0)
    skip_until = str(parse_cfg.get("skip_until") or "")
    entry_field = str(parse_cfg.get("entry_field") or "")

    if skip_rows:
        text = "\n".join(text.splitlines()[skip_rows:])
    if skip_until:
        # 报表类文件常有前置说明/汇总段：从"包含标记的那一行"开始解析（该行即表头）
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if skip_until in line:
                text = "\n".join(lines[index:])
                break
        else:
            return []          # 整份文件都没有明细段（未出账等），按空结果处理

    records: list[dict] = []
    if fmt in ("csv", "tsv"):
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if not reader.fieldnames:
            return []
        for row in reader:
            if row is None or all(v is None or str(v).strip() == "" for v in row.values()):
                continue
            record = {str(k): (None if v is None else str(v)) for k, v in row.items() if k is not None}
            if entry_field:
                record[entry_field] = entry
            records.append(record)
    elif fmt == "jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"JSONL 解析失败（{exc}）；行片段：{line[:120]!r}")
            if isinstance(item, dict) and entry_field:
                item = dict(item, **{entry_field: entry})
            records.append(item)
    else:
        raise RuntimeError(f"不支持的文件解析格式：{fmt}")
    return records


def parse_bytes(data: bytes, parse_cfg: dict, label: str) -> list:
    """文件类响应 → 记录列表：整包 CSV/TSV/JSONL，或 ZIP（可筛条目、逐条解析）。

    流程：ZIP？→ 按 entry_contains 筛出要的文件 → 逐个按 encoding 解码 → 按 format 解析：
    - csv/tsv：首行当表头，每行 → 一个 {列名: 值} 字典（值保持字符串）；
    - jsonl：每行一个 JSON 对象。
    表头/格式不对会直接报错（宁失败勿写错），空行自动跳过。
    """
    encoding = str(parse_cfg.get("encoding") or "utf-8-sig")
    unzip = bool(parse_cfg.get("unzip"))
    entry_contains = str(parse_cfg.get("entry_contains") or "")

    # 防呆：接口出错时经常返回 JSON（HTTP 200），而不是文件流；这里直接给出可读报错
    if data.lstrip()[:1] == b"{":
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raise RuntimeError(
                f"{label} 期望文件流，但接口返回了 JSON（多半是错误信息）："
                f"{json.dumps(payload, ensure_ascii=False)[:300]}"
            )

    if not unzip:
        return _parse_text(data.decode(encoding, "replace"), parse_cfg)

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        snippet = data[:200].decode("utf-8", "replace")
        raise RuntimeError(f"{label} 期望 ZIP 文件但返回不是 ZIP；片段：{snippet!r}")
    with archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if entry_contains:
            names = [n for n in names if entry_contains in n]
        if not names:
            raise RuntimeError(f"{label} ZIP 里没有匹配 entry_contains={entry_contains!r} 的文件："
                               f"{archive.namelist()[:10]}")
        records: list[dict] = []
        for name in sorted(names):
            records.extend(_parse_text(archive.read(name).decode(encoding, "replace"), parse_cfg, entry=name))
        return records


def parse_payload(payload, request_cfg: dict, parse_cfg: dict, label: str,
                  missing_ok: bool = False) -> list:
    """按 response_type 分派：json → records_path；bytes → 文件解析。"""
    response_type = str(request_cfg.get("response_type") or "json").lower()
    if response_type == "bytes":
        if not isinstance(payload, (bytes, bytearray)):
            raise RuntimeError(f"{label} 期望二进制响应，实际拿到 {type(payload).__name__}")
        return parse_bytes(bytes(payload), parse_cfg or {}, label)
    return extract_json_records(payload, request_cfg, label, missing_ok=missing_ok)
