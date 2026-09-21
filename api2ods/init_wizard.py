# -*- coding: utf-8 -*-
"""交互式建配置向导（`api2ods --init`）。

目的：让不熟悉配置字段的人也能接新数据源——按提示回答十来个问题，生成一份能直接
`--check` 的作业 JSON，密钥直接写进文件（作业文件已 gitignore）。

设计：所有提问都允许回车用默认值；answers 通过 ask 注入，便于单元测试。
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

# 常见默认值（都可在提问时直接回车采纳）
DEFAULT_METHOD = "GET"
DEFAULT_DATE_TZ = "Asia/Shanghai"
DEFAULT_API_TZ = "+08:00"
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_ENDPOINT = "http://service.us-west-1.maxcompute.aliyun.com/api"
DEFAULT_START_PARAM = "startTime"
DEFAULT_END_PARAM = "endTime"

AUTH_CHOICES = (
    "0 = 不需要鉴权",
    "1 = Authorization: Bearer <token>（最常用）",
    "2 = 自定义请求头里放 Token（如 X-Api-Key: xxx）",
    "3 = URL 参数里放 Token（如 ?token=xxx）",
    "4 = Basic 用户名密码",
    "5 = 阿里云 RPC 签名（AK/SK，账单/OpenAPI 用）",
    "6 = Onerway 式 sha256 签名（参数排序拼接 + 密钥）",
    "7 = 自定义 signers.py（高级，需自己写函数）",
)


def _ask(ask, prompt: str, default: str = "") -> str:
    """问一个问题；空输入用默认值。"""
    hint = f"（默认 {default}）" if default else ""
    answer = str(ask(f"{prompt}{hint}：") or "").strip()
    return answer or default


def _ask_choice(ask, prompt: str, choices: tuple, default: str = "0", echo=print) -> str:
    """让用户从编号选项里选一个，返回选中的编号字符串。"""
    echo(prompt)
    for line in choices:
        echo(f"    {line}")
    return _ask(ask, "请选择编号", default)


def _split_url(raw: str) -> tuple[str, str] | None:
    """把用户粘贴的完整 URL 拆成 (base_url, path)；格式不对返回 None。"""
    parsed = urllib.parse.urlsplit(raw.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or ""
    if parsed.query:
        # URL 里的固定 query 参数（如 ?app=xx）：提示用户改成 params（这里先拼回 path 会错）
        return base, path + ("?" + parsed.query if path else "?" + parsed.query)
    return base, path


def run_init(out_path: str = "", ask=input, echo=print, workdir: Path | None = None) -> int:
    """交互式生成作业配置，返回退出码（0 成功 / 1 取消）。

    默认输出到「当前目录/jobs/<作业名>.json」——无论是在源码目录还是 pip 安装后运行都合理。
    """
    root = Path(workdir) if workdir else Path.cwd()
    try:
        echo("=== api2ods 配置向导（直接回车用默认值；随时 Ctrl+C 取消）===")
        echo("")

        # ---------------------------------------------------------- ① 作业与接口
        job_name = _ask(ask, "① 作业名（英文/数字/下划线，用于文件名和日志）", "my_api")

        raw_url = ""
        for _ in range(3):
            raw_url = _ask(ask, "② API 完整地址（直接粘贴，含 https://）")
            if not raw_url:
                echo("   地址不能为空。")
                continue
            split = _split_url(raw_url)
            if split is None:
                echo("   地址格式不对，示例：https://api.example.com/v1/orders")
                continue
            base_url, path = split
            if "?" in path:
                echo(f"   提示：地址里带了 query 参数，已放进 path；建议稍后手工挪到 request.params：{path}")
            break
        else:
            echo("❌ 地址连续三次无效，已取消。")
            return 1

        method = _ask(ask, "③ 请求方法（GET/POST）", DEFAULT_METHOD).upper()

        # ---------------------------------------------------------- ② 鉴权
        auth_choice = _ask_choice(ask, "④ 接口怎么鉴权？", AUTH_CHOICES, "1", echo)
        auth: dict = {}
        if auth_choice == "0":
            auth = {"type": "none"}
        elif auth_choice == "1":
            token = _ask(ask, "   Token 的值（直接粘贴）")
            auth = {"type": "bearer", "token": token}
        elif auth_choice == "2":
            header = _ask(ask, "   请求头名字", "X-Api-Key")
            value = _ask(ask, "   Token/Key 的值")
            auth = {"type": "token", "header": header, "value": value}
        elif auth_choice == "3":
            name = _ask(ask, "   URL 参数名", "token")
            value = _ask(ask, "   Token 的值")
            auth = {"type": "query", "params": {name: value}}
        elif auth_choice == "4":
            username = _ask(ask, "   用户名")
            password = _ask(ask, "   密码")
            auth = {"type": "basic", "username": username, "password": password}
        elif auth_choice == "5":
            ak = _ask(ask, "   AccessKeyId")
            sk = _ask(ask, "   AccessKeySecret")
            auth = {"type": "aliyun_rpc", "access_key_id": ak, "access_key_secret": sk}
        elif auth_choice == "6":
            secret = _ask(ask, "   商户密钥（Secret key）")
            sign_field = _ask(ask, "   签名字段名", "sign")
            auth = {"type": "sha256_concat", "secret_key": secret,
                    "sign_field": sign_field, "sign_in": "body"}
        else:
            module = _ask(ask, "   signers.py 的函数名（文件放作业同目录）", "my_sign")
            auth = {"type": "custom", "module": "signers.py", "func": module}

        # ---------------------------------------------------------- ③ 记录路径
        records_path = _ask(ask, "⑤ 记录列表在返回 JSON 里的路径（如 data.list；整个返回就是数组则留空）")
        echo("   （不确定可以先留空，跑 --check 时脚本会打印返回的顶层字段帮你判断）")

        # ---------------------------------------------------------- ④ 分页
        page_choice = _ask_choice(
            ask, "⑥ 接口怎么翻页？",
            ("0 = 不翻页（一次返回全部）",
             "1 = 页码分页（第几页/每页多少条）",
             "2 = 游标分页（返回里带下一页游标）"),
            "0",
            echo,
        )
        pagination: dict = {"type": "none"}
        if page_choice == "1":
            echo("   翻页终点至少要给一个（总页数字段 或 总条数字段，形如 data.totalPages / data.totalCount）")
            total_pages_path = _ask(ask, "   总页数字段路径")
            total_items_path = _ask(ask, "   总条数字段路径")
            if not total_pages_path and not total_items_path:
                total_pages_path = "data.totalPages"
                echo(f"   两个都留空了，先按 {total_pages_path} 写（跑 --check 报错后再改）")
            pagination = {"total_pages_path": total_pages_path} if total_pages_path else {}
            if total_items_path:
                pagination["total_items_path"] = total_items_path
            pagination["delay_seconds"] = 0.5
            # page/size 等参数用默认值（page/size/100），接口不一样时再手工补
        elif page_choice == "2":
            cursor_path = _ask(ask, "   下一页游标在返回里的路径（如 data.nextCursor）")
            pagination = {"cursor_path": cursor_path}

        # ---------------------------------------------------------- ⑤ 取数窗口
        window_choice = _ask_choice(
            ask, "⑦ 要不要按时间窗口取数？",
            ("0 = 不传时间（每次全量或接口自带默认范围）",
             "1 = 按天窗口（推荐：每次回拉最近 N 天，时间参数由脚本生成）",
             "2 = 整区间窗口（一次请求覆盖 N 天）"),
            "1",
            echo,
        )
        window: dict = {}
        if window_choice == "1":
            days = _ask(ask, "   每次回拉最近几天", "15")
            start_param = _ask(ask, "   开始时间参数名", DEFAULT_START_PARAM)
            end_param = _ask(ask, "   结束时间参数名（不需要就填 -）", DEFAULT_END_PARAM)
            window = {
                "mode": "per_day",
                "days": int(days or "15"),
                "start_param": start_param,
                "end_param": None if end_param == "-" else end_param,
                "format": _ask(ask, "   时间格式（unix=秒，或 strftime 格式）", DEFAULT_TIME_FORMAT),
            }
            echo("   （时区默认 Asia/Shanghai、+08:00；要改就编辑文件里的 date_tz/api_tz）")
        elif window_choice == "2":
            days = _ask(ask, "   每次覆盖最近几天", "7")
            window = {
                "mode": "range",
                "days": int(days or "7"),
                "start_param": _ask(ask, "   开始时间参数名", DEFAULT_START_PARAM),
                "end_param": _ask(ask, "   结束时间参数名", DEFAULT_END_PARAM),
                "format": _ask(ask, "   时间格式", DEFAULT_TIME_FORMAT),
            }

        # ---------------------------------------------------------- ⑥ 响应类型
        response_choice = _ask_choice(
            ask, "⑧ 接口返回什么？",
            ("0 = JSON（绝大多数接口）",
             "1 = 文件流（ZIP/CSV/JSONL，如导出接口）"),
            "0",
            echo,
        )
        parse: dict = {}
        response_type = "json"
        if response_choice == "1":
            response_type = "bytes"
            fmt = _ask(ask, "   文件格式（csv/tsv/jsonl）", "csv")
            parse = {"format": fmt, "encoding": "utf-8-sig"}
            if _ask(ask, "   是 ZIP 压缩包吗（y/n）", "n").lower().startswith("y"):
                parse["unzip"] = True
                entry = _ask(ask, "   只取文件名包含什么字的条目（如 amount；全部则留空）")
                if entry:
                    parse["entry_contains"] = entry
                parse["entry_field"] = "__file"

        # ---------------------------------------------------------- ⑦ 目标表与凭证
        echo("")
        echo("=== MaxCompute 目标 ===")
        project = _ask(ask, "⑨ 项目名", "my_project")
        table = _ask(ask, "   表名（建议 <层级>_<业务域>_<过程>_json_di）", f"ods_{job_name}_json_di")
        ak = _ask(ask, "   阿里云 AccessKeyId")
        sk = _ask(ask, "   阿里云 AccessKeySecret")
        endpoint = _ask(ask, "   endpoint", DEFAULT_ENDPOINT)

        # ---------------------------------------------------------- ⑧ 组装并写出
        job = {
            "job": job_name,
            "maxcompute": {"project": project, "endpoint": endpoint,
                           "access_key_id": ak, "access_key_secret": sk},
            "request": {
                "base_url": base_url,
                "path": path,
                "method": method,
                "auth": auth,
                "records_path": records_path,
            },
            "target": {"project": project, "table": table, "pt": "${bizdate}", "column": "json"},
        }
        if pagination.get("type") != "none":     # page/cursor 分支不带 type，交给 normalize 推断
            job["pagination"] = pagination
        if window:
            job["window"] = window
        if response_type == "bytes":
            job["request"]["response_type"] = "bytes"
            job["parse"] = parse

        target_path = Path(out_path) if out_path else root / "jobs" / f"{job_name}.json"
        if not target_path.is_absolute():
            target_path = root / target_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(target_path, 0o600)   # 含密钥，收紧权限（Windows 忽略）

        echo("")
        echo(f"✅ 已生成：{target_path}")
        echo("下一步：")
        echo("  1) 打开文件核对字段（尤其 auth、records_path、window 参数名）")
        echo(f"  2) 体检：  api2ods --job {target_path.name} --check   （报错会提示缺什么/返回长什么样）")
        echo(f"  3) 试跑：  api2ods --job {target_path.name} --days 1 --dry-run")
        echo(f"  4) 正式：  api2ods --job {target_path.name} --bizdate ${{bizdate}}")
        return 0
    except (KeyboardInterrupt, EOFError):
        echo("")
        echo("已取消，未生成任何文件。")
        return 1
