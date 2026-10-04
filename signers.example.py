# -*- coding: utf-8 -*-
"""custom 鉴权示例：当签名规则无法用内置 auth 类型表达时，复制本文件为 signers.py
（signers.py 已 gitignore，可放密钥相关逻辑），在作业配置里写：

    "auth": {"type": "custom", "module": "signers.py", "func": "onerway_sign"}

函数收到 ctx 字典：ctx["params"] / ctx["headers"] / ctx["request"]（auth 配置本体），
可直接修改，也可以像下面这样返回要合并的 params / headers。
"""

from __future__ import annotations

import hashlib


def onerway_sign(ctx: dict) -> dict:
    """Onerway 式签名示例（与内置 sha256_concat 等价，仅演示写法）。"""
    params = ctx.get("params")
    if not isinstance(params, dict):
        # 框架一定会注入 params；缺失/类型不对说明调用约定被改坏了，给明确异常
        # （抛 ValueError 而不是 SystemExit：前者会被框架包装成"签名函数执行失败"的配置错）
        raise ValueError("签名上下文缺少 params（框架注入的请求参数字典）")
    secret = str(ctx["request"].get("secret_key") or "")
    if not secret:
        # 静默用空密钥签会产出一个格式合法、但服务端必然校验失败的签名，
        # 问题只能拖到接口回 401 才暴露——在这里直接报告缺什么
        raise ValueError("auth.secret_key 未配置（Onerway 签名密钥）")
    keys = sorted(k for k in params if k != "sign" and params[k] not in (None, ""))
    text = "".join(str(params[k]) for k in keys) + secret
    return {"params": {"sign": hashlib.sha256(text.encode("utf-8")).hexdigest()}}


def xmp_sign(ctx: dict) -> dict:
    """XMP（Mobvista）Open API 签名：sign = md5(secret + unix 秒时间戳)。

    时间戳每个请求都要现算（复用它会被判 400），所以必须走 custom 逐请求生成，
    不能写成静态 params。作业配置引用：

        "auth": {"type": "custom", "module": "signers.py", "func": "xmp_sign",
                 "client_id": "${secrets.xmp_client_id}",
                 "secret": "${secrets.xmp_client_secret}"}
    """
    import time

    request_cfg = ctx["request"]
    client_id = str(request_cfg.get("client_id") or "")
    secret = str(request_cfg.get("secret") or "")
    if not client_id or not secret:
        # 抛 ValueError 而不是 SystemExit：SystemExit 继承 BaseException，不会被框架的
        # except Exception 接住、拿不到"签名函数执行失败"的配置错包装；ValueError 由
        # auth.AuthApplier 统一转成 ConfigError（不可重试、快速失败）
        raise ValueError("auth.client_id / auth.secret 未配置（XMP Open API 的 Client ID/Secret）")
    timestamp = int(time.time())
    payload = f"{secret}{timestamp}".encode("utf-8")  # noqa: UP012 - 显式写编码，不依赖解释器默认值
    # MD5 是 XMP 接口规范限定的算法（无法替换）；usedforsecurity=False 显式声明
    # "非安全用途"，FIPS 合规环境下 hashlib.md5 才会放行
    sign = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    return {"params": {"client_id": client_id, "timestamp": timestamp, "sign": sign}}
