# -*- coding: utf-8 -*-
"""响应解析：JSON 路径取值、JSON 记录提取、文件类响应（ZIP/CSV/TSV/JSONL）。"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile

from .spool import loads_json
from .utils import ConfigError, as_bool, log

_PATH_TOKEN_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")

# 行边界只认 \r\n / \r / \n（与 csv / JSONL 的行定义一致）。不用 str.splitlines()：
# 它还会在 \x0b \x0c \x1c \x1d \x1e \x85     处分行——报表导出里的分页符
# \x0c 会让 skip_rows 与 csv.reader 的"第几行"错位（表头错位、列名全错还照样成功），
# JSON 字符串里的 U+2028（合法字符，工具自己 dump 的记录就可能有）会被劈成两半
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n|$)")


def _split_lines(text: str, keepends: bool = False) -> list[str]:
    """按 \\r\\n / \\r / \\n 切行；keepends=True 时保留行尾换行符（见上方 _LINE_RE 说明）。"""
    lines = []
    for match in _LINE_RE.finditer(text):
        segment = match.group(0)
        if not segment:
            continue
        lines.append(segment if keepends else segment.rstrip("\r\n"))
    return lines


# CSV 单字段默认只允许 128KB，报表类文件很容易超；调到接近 MaxCompute 单列上限
_CSV_FIELD_LIMIT = 7_000_000
try:
    csv.field_size_limit(_CSV_FIELD_LIMIT)
except OverflowError:  # pragma: no cover - 32 位平台上 C long 装不下
    csv.field_size_limit(10 ** 7)


def _as_int(value, fallback: int, field: str) -> int:
    """数值配置项转 int：JSON 里写成字符串（"100"）也认，写错给报错而不是 traceback。

    用 ConfigError（SystemExit 子类）：配置写错重试多少次都一样，
    抛 RuntimeError 会被整窗重试当成"接口抖动"白等十几秒。
    """
    if value is None or value == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} 必须是整数，实际 {value!r}")


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


def ensure_object_records(records, label: str) -> list[dict]:
    """把记录数组规整成"每个元素都是对象"的列表，不合规直接报错。

    JSON 单页路径与分页路径共用这一份检查：数组里混进数字/字符串会原样写进 json 列，
    下游 get_json_object 解出来全是 NULL 却显示成功（JSONL 路径对同样输入是报错的）。
    少任何一条路径都会让这种记录悄悄进 ODS。
    """
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        raise RuntimeError(f"{label} records_path 指向的不是数组/对象：{type(records).__name__}")
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            snippet = json.dumps(item, ensure_ascii=False)[:120] if not isinstance(item, str) else item[:120]
            raise RuntimeError(
                f"{label} records_path 指向的数组第 {index} 个元素不是对象"
                f"（是 {type(item).__name__}：{snippet!r}）；这种记录写进 ODS 后下游取不到任何字段"
            )
    return records


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
        snippet = (json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list))
                   else str(payload))[:300]
        hint = (f"找不到 records_path={records_path!r}（顶层键：{top_keys}）" if records_path
                else "records_path 为空且返回不是记录数组")
        raise RuntimeError(f"{label} 解析失败：{hint}；返回片段：{snippet}")
    if isinstance(records, dict) and not records:
        # 路径落在空对象上（Items: {}、data: {}）——接口用它表示"这次没有数据"。
        # 原样包成一条 {} 会往 ODS 写一条全 NULL 的假记录；分页时它还会让"零数据日"
        # 的豁免失效，一路翻到 max_pages 才报错
        return []
    return ensure_object_records(records, label)


def _parse_text(text: str, parse_cfg: dict, entry: str = "",
                label: str = "") -> list[dict]:
    """文本 → 记录列表：csv / tsv / jsonl。"""
    fmt = str(parse_cfg.get("format") or "csv").lower()
    delimiter = str(parse_cfg.get("delimiter") or ("\t" if fmt == "tsv" else ","))
    if fmt in ("csv", "tsv") and len(delimiter) != 1:
        # 写 "\t"（转义后的两个字面字符）而不是制表符是常见笔误：3.11 起
        # csv 会当"多字符分隔符"静默按它切分，3.10 及以前直接 TypeError，
        # 同一份配置跨 Python 版本行为完全不同——不如直接拒绝
        raise ConfigError(f"parse.delimiter 必须是单个字符，实际 {delimiter!r}"
                          f"（TSV 请用 \\t 表示制表符，或直接改用 format=tsv）")
    skip_rows = _as_int(parse_cfg.get("skip_rows"), 0, "parse.skip_rows")
    skip_until = str(parse_cfg.get("skip_until") or "")
    entry_field = str(parse_cfg.get("entry_field") or "")
    if skip_rows < 0:
        # ConfigError：配置写错了，整窗重试多少次都一样（重试只该留给"可能自己好起来"的错误）
        raise ConfigError(f"parse.skip_rows 不能为负：{skip_rows}")

    if skip_rows:
        before = text
        text = "".join(_split_lines(text, keepends=True)[skip_rows:])
        if before.strip() and not text.strip():
            # 文件本来有内容，被 skip_rows 一行不剩地跳空了：要么 skip_rows 配大了，
            # 要么源文件结构缩水（比如错误页只有两行）。空结果是静默的，得留个痕
            log(f"  警告：{label or '文件'} 按 skip_rows={skip_rows} 跳过之后没有任何内容"
                f"（原文件 {len(_split_lines(before))} 行）；请核对 parse.skip_rows 与文件结构")
    if skip_until and not text.strip():
        # 配置了两个跳过项、且 skip_rows 正好把内容全跳空了：按空结果处理。
        # 这条要放在冲突检测前面——"文件里有内容"这个前提不成立时，
        # 报"两者冲突"会把方向指错（那是文件本身就是空的/只有前置段）
        return []
    if skip_until and skip_rows and skip_until not in text:
        # 标记行本身就在被 skip_rows 跳掉的前几行里时，删完才去找必然找不到，
        # 而报错文案会指向"文件结构变了"这个错误方向。这里把两种配置的冲突说清楚
        raise ConfigError(
            f"parse.skip_until={skip_until!r} 在跳过前 {skip_rows} 行之后找不到了："
            f"标记行可能就在被跳过的那几行里，skip_rows 与 skip_until 不要同时配"
        )
    if skip_until:
        # 报表类文件常有前置说明/汇总段：从"包含标记的那一行"开始解析（该行即表头）。
        # 找不到标记有两种含义完全不同的情况，必须分开：文件为空 = 当天真的没出账（空结果），
        # 文件有内容却没有明细段 = 结构变了/拿到错误页，报错触发整窗重试（否则静默少拉一天）
        # keepends=True + 直接叠加：用 splitlines() 再 join 会把「引号里跨行的 CSV 字段」
        # 的换行重建掉，字段内容被悄悄改掉
        lines = _split_lines(text, keepends=True)
        for index, line in enumerate(lines):
            if skip_until in line:
                text = "".join(lines[index:])
                break
        else:
            if not any(line.strip() for line in lines):
                return []
            preview = " / ".join(line.strip()[:60] for line in lines[:3] if line.strip())
            raise RuntimeError(
                f"{label or '文件'} 里找不到明细段标记 {skip_until!r}，但文件有内容"
                f"（{len(lines)} 行，开头：{preview[:200]!r}）；"
                f"文件结构可能变了，或接口返回的不是预期文件"
            )

    records: list[dict] = []
    if fmt in ("csv", "tsv"):
        # strict=True：引号未闭合时模块默认会把后面几行吞进同一个字段，
        # 行数照常 > 0、校验也过，等于静默丢掉一大段数据
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter, strict=True)
        try:
            fieldnames = reader.fieldnames
        except csv.Error as exc:
            raise RuntimeError(f"{label or '文件'} 表头解析失败：{exc}（引号未闭合？）")
        if not fieldnames:
            # 首行是空行（表头为空）时，后面所有数据行都会被静默丢掉（0 行）。
            # 文件本身为空才是 0 行；"有内容却解析不出表头"说明文件结构变了
            if text.strip():
                raise RuntimeError(
                    f"{label or '文件'} 的第一行是空行（表头为空），无法解析；"
                    f"文件开头：{text.lstrip()[:120]!r}"
                )
            return []
        try:
            for line_no, row in enumerate(reader, start=2):
                if row is None:
                    continue
                if (all(v is None or str(v).strip() == "" for v in row.values())
                        and len(fieldnames) > 1):
                    # 全空白行：多列文件里是排版垃圾，跳过；单列文件里它就是"值为空"的
                    # 一条合法记录，丢掉等于静默少数
                    continue
                # 列数比表头多：多出来的字段会落进 restkey=None，静默丢字段等于写错数据
                extra = row.pop(None, None)
                if extra:
                    raise RuntimeError(
                        f"{label or '文件'} 第 {line_no} 行列数多于表头（多出 {len(extra)} 个值：{extra[:3]}），"
                        f"字段会被截断；请检查文件格式或分隔符"
                    )
                record = {str(k): (None if v is None else str(v)) for k, v in row.items()}
                if entry_field:
                    record[entry_field] = entry
                records.append(record)
        except csv.Error as exc:
            raise RuntimeError(
                f"{label or '文件'} 第 {reader.line_num} 行 CSV 解析失败：{exc}；"
                f"多半是引号未闭合/字段内含未转义的引号，整份文件的行数会因此对不上"
            )
        # 同名列会让前面的列被后面的覆盖、空列名的键是 ""（下游 get_json_object 取不到），
        # 都等于静默丢列（表头都是字符串时才可能出现）
        if fieldnames and any(str(name).strip() == "" for name in fieldnames):
            raise RuntimeError(
                f"{label or '文件'} 的 CSV 表头有空列名（行尾多了一个分隔符？）：{fieldnames}")
        if fieldnames and len(fieldnames) != len(set(fieldnames)):
            raise RuntimeError(f"{label or '文件'} 的 CSV 表头有重复列名，解析会丢列：{fieldnames}")
    elif fmt == "jsonl":
        for line_no, line in enumerate(_split_lines(text), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = loads_json(line)
            except ValueError as exc:      # 含 NaN/Infinity 的非法数值
                raise RuntimeError(f"JSONL 解析失败（{exc}）；行片段：{line[:120]!r}")
            # JSONL 的定义是「每行一个 JSON 对象」：数组/数字/字符串行会原样落进
            # ODS 的 json 列，下游 get_json_object 解不出来（静默变 NULL）
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"{label or '文件'} 第 {line_no} 行不是 JSON 对象"
                    f"（是 {type(item).__name__}：{line[:120]!r}）；jsonl 要求每行一个对象"
                )
            if entry_field:
                item = dict(item, **{entry_field: entry})
            records.append(item)
    else:
        raise ConfigError(f"不支持的文件解析格式：{fmt}（可用 csv / tsv / jsonl）")
    return records


def parse_bytes(data: bytes, parse_cfg: dict, label: str) -> list:
    """文件类响应 → 记录列表：整包 CSV/TSV/JSONL，或 ZIP（可筛条目、逐条解析）。

    流程：ZIP？→ 按 entry_contains 筛出要的文件 → 逐个按 encoding 解码 → 按 format 解析：
    - csv/tsv：首行当表头，每行 → 一个 {列名: 值} 字典（值保持字符串）；
    - jsonl：每行一个 JSON 对象。
    表头/格式不对会直接报错（宁失败勿写错），空行自动跳过。
    """
    encoding = str(parse_cfg.get("encoding") or "utf-8-sig")
    unzip = as_bool(parse_cfg.get("unzip"), default=False)
    strict_encoding = as_bool(parse_cfg.get("strict_encoding"), default=False)
    allow_multi_entry = as_bool(parse_cfg.get("allow_multi_entry"), default=False)
    entry_contains = str(parse_cfg.get("entry_contains") or "")

    # 防呆：接口出错时经常返回 JSON（HTTP 200），而不是文件流；这里直接给出可读报错。
    # lstrip b"\xef\xbb\xbf"：带 BOM 的 JSON 错误体前缀不是 "{"，漏掉会给出"期望 ZIP"的误导报错。
    # parse.format=jsonl 时跳过：JSONL 文件本身就是一行一个 JSON 对象，"只有 1 条记录的文件"
    # 与 JSON 错误体在内容上无法区分，拦下来会让低流量源（每天 1 条）永远跑不通
    probe = data.lstrip(b" \t\r\n\xef\xbb\xbf")
    if (probe[:1] in (b"{", b"[")
            and str(parse_cfg.get("format") or "").lower() != "jsonl"):
        payload = None
        try:
            payload = loads_json(data.decode("utf-8-sig", "replace"))
        except ValueError:
            # 解不出来又以 }/] 收尾：大概率是含 NaN/被截断的 JSON 错误体。
            # （CSV 表头以 { [ 开头虽罕见但合法，所以只在这个更窄的形态上报错）
            if probe[-1:] in (b"}", b"]"):
                raise RuntimeError(
                    f"{label} 期望文件流，但响应像一个无法解析的 JSON（含 NaN/Infinity 或被截断）："
                    f"{data[:200].decode('utf-8', 'replace')!r}"
                )
        if isinstance(payload, (dict, list)):
            raise RuntimeError(
                f"{label} 期望文件流，但接口返回了 JSON（多半是错误信息）："
                f"{json.dumps(payload, ensure_ascii=False)[:300]}"
            )

    def decode(raw: bytes, where: str) -> str:
        """按配置编码解码；strict_encoding=true 时拒绝"解出乱码还照写"。

        默认 replace：源方偶尔混入个别坏字节不该让整个任务失败。
        但整份文件编码不对（如 UTF-8 接口换成 GBK/UTF-16）时，replace 会把列名和值
        全变成 U+FFFD 且行数照常 > 0，静默写进 ODS——不确定编码的源建议打开这个开关。
        """
        if strict_encoding:
            try:
                # 这里也必须去 BOM：非严格分支有 lstrip，漏掉会让同一个开关下
                # 列名变成 "﻿date"，下游 get_json_object('$.date') 静默取空
                return raw.decode(encoding).lstrip("﻿")
            except UnicodeDecodeError as exc:
                raise RuntimeError(
                    f"{label} {where}按 {encoding} 解码失败（{exc}）；"
                    f"接口可能换了文件编码，请改 parse.encoding（如 gbk）"
                )
        text = raw.decode(encoding, "replace").lstrip("﻿")
        if "�" in text:
            log(f"  警告：{label} {where}按 {encoding} 解码出现替换字符（乱码），"
                f"建议核对 parse.encoding；需要「解码失败即报错」时设 parse.strict_encoding=true")
        return text

    if not unzip:
        return _parse_text(decode(data, ""), parse_cfg, label=label)

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
        # 不筛条目时，多文件（明细+汇总、多语言副本等）会被无脑串成一份记录：
        # 表头还可能各不相同。要么用 entry_contains 指定，要么明确接受全部。
        if len(names) > 1 and not entry_contains and not allow_multi_entry:
            raise RuntimeError(
                f"{label} ZIP 里有多个文件（表头可能不同，混在一起会写错数据）：{names[:10]}；"
                f"请用 parse.entry_contains 指定要取的文件名关键字，"
                f"确实要全部解析时设 parse.allow_multi_entry=true"
            )
        if len(names) > 1:
            # 指定了 entry_contains 却命中多个条目：常见用法（按地区/批次分包、表头一致）
            # 需要合并，所以不拦；但表头不一致时会拼出字段不齐的记录，得让用户看见
            log(f"  提示：{label} ZIP 里匹配 entry_contains={entry_contains!r} 的条目有 "
                f"{len(names)} 个，将按文件名顺序合并：{sorted(names)[:5]}")
        records: list[dict] = []
        for name in sorted(names):
            try:
                raw = archive.read(name)
            except zipfile.BadZipFile as exc:      # 条目损坏（截断包等）
                raise RuntimeError(f"{label} ZIP 条目 {name!r} 读取失败：{exc}")
            records.extend(_parse_text(decode(raw, f"条目 {name!r} "), parse_cfg,
                                       entry=name, label=label))
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
