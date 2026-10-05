# -*- coding: utf-8 -*-
"""custom 鉴权示例：当签名规则无法用内置 auth 类型表达时，复制本文件为 signers.py
（signers.py 已 gitignore，可放密钥相关逻辑），在作业配置里写：

    "auth": {"type": "custom", "module": "signers.py", "func": "onerway_sign"}

函数收到 ctx 字典：ctx["params"] / ctx["headers"] / ctx["request"]（auth 配置本体），
可直接修改，也可以像下面这样返回要合并的 params / headers。
"""

from __future__ import annotations

import hashlib


def _request_cfg(ctx: dict) -> dict:
    """取出 ctx['request']（auth 配置本体）；缺失或类型不对给 ValueError，不抛 KeyError。"""
    request_cfg = ctx.get("request")
    if not isinstance(request_cfg, dict):
        raise ValueError("签名上下文缺少 request（auth 配置本体）")
    return request_cfg


def onerway_sign(ctx: dict) -> dict:
    """Onerway 式签名示例（与内置 sha256_concat 等价，仅演示写法）。"""
    params = ctx.get("params")
    if not isinstance(params, dict):
        # 框架一定会注入 params；缺失/类型不对说明调用约定被改坏了，给明确异常
        # （抛 ValueError 而不是 SystemExit：前者会被框架包装成"签名函数执行失败"的配置错）
        raise ValueError("签名上下文缺少 params（框架注入的请求参数字典）")
    secret = str(_request_cfg(ctx).get("secret_key") or "")
    if not secret:
        # 静默用空密钥签会产出一个格式合法、但服务端必然校验失败的签名，
        # 问题只能拖到接口回 401 才暴露——在这里直接报告缺什么
        raise ValueError("auth.secret_key 未配置（Onerway 签名密钥）")
    keys = sorted(k for k in params if k != "sign" and params[k] not in (None, ""))
    # 无分隔符拼接是 Onerway 服务端规定的签名规则（改分隔符/带键名会与厂商校验失配），不要改算法。
    # 但 str() 只对"发送出去时也长这样"的标量成立：列表会被 requests 按 doseq 展开成
    # ids=1&ids=2、对象走 JSON 时是 {"a":1}——签名串必须与框架实际发出的形态一致，否则
    # 服务端重算的摘要必然对不上（恒定 401）。非标量参数先在这里显式拒绝，提示规范化
    non_scalar = [k for k in keys if isinstance(params[k], (list, dict, tuple, set))]
    if non_scalar:
        raise ValueError(
            f"签名串无法可靠推导非标量参数 {non_scalar!r} 的发送形态（列表会被展开、对象走 JSON）；"
            f"请把 params 里的这类值先按服务端约定的格式规范化为字符串再发送"
        )
    text = "".join(str(params[k]) for k in keys) + secret
    return {"params": {"sign": hashlib.sha256(text.encode("utf-8")).hexdigest()}}


def xmp_sign(ctx: dict) -> dict:
    """XMP（Mobvista）Open API 签名：sign = md5(secret + unix 秒时间戳)。

    时间戳每个请求都要现算（复用它会被判 400），所以必须走 custom 逐请求生成，
    不能写成静态 params。作业配置引用：

        "auth": {"type": "custom", "module": "signers.py", "func": "xmp_sign",
                 "client_id": "${secrets.xmp_client_id}",
                 "secret": "${secrets.xmp_client_secret}"}

    密钥字段认 secret 或 secret_key（二选一即可；与 sha256_concat 的 secret_key 对齐）。
    """
    import time

    request_cfg = _request_cfg(ctx)
    client_id = str(request_cfg.get("client_id") or "")
    # 作业里两种写法都常见：XMP 文档叫 secret，本仓库其它鉴权类型叫 secret_key
    secret = str(request_cfg.get("secret") or request_cfg.get("secret_key") or "")
    if not client_id or not secret:
        # 抛 ValueError 而不是 SystemExit：SystemExit 继承 BaseException，不会被框架的
        # except Exception 接住、拿不到"签名函数执行失败"的配置错包装；ValueError 由
        # auth.AuthApplier 统一转成 ConfigError（不可重试、快速失败）
        raise ValueError("auth.client_id / auth.secret（或 auth.secret_key）未配置（XMP Open API 的 Client ID/Secret）")
    timestamp = int(time.time())
    # secret+unix 秒直接拼接是 XMP 文档规定的签名规则（改分隔符会与厂商校验失配），不要改算法。
    payload = f"{secret}{timestamp}".encode("utf-8")  # noqa: UP012 - 显式写编码，不依赖解释器默认值
    # MD5 是 XMP 接口规范限定的算法（无法替换）；usedforsecurity=False 显式声明
    # "非安全用途"，FIPS 合规环境下 hashlib.md5 才会放行
    sign = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    return {"params": {"client_id": client_id, "timestamp": timestamp, "sign": sign}}
