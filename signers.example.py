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
    params = ctx["params"]
    secret = str(ctx["request"].get("secret_key") or "")
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
        raise SystemExit("auth.client_id / auth.secret 未配置（XMP Open API 的 Client ID/Secret）")
    timestamp = int(time.time())
    sign = hashlib.md5(f"{secret}{timestamp}".encode()).hexdigest()
    return {"params": {"client_id": client_id, "timestamp": timestamp, "sign": sign}}
