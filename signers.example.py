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
