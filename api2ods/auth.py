# -*- coding: utf-8 -*-
"""鉴权：none / basic / token / query / sha256_concat / aliyun_rpc / custom。

aliyun_rpc 完全按阿里云官方规则实现（HMAC-SHA1），并与现有
aliyun_bill_sync_daily.py 的 call_bss 逻辑逐位一致（有单测对照）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import time
import urllib.parse
import uuid
from pathlib import Path

ALIYUN_RPC_SIGNATURE_VERSION = "1.0"


def aliyun_encode(value) -> str:
    """阿里云要求的 URL 编码：不转义 ~，其余按 RFC3986（空格 → %20）。"""
    return urllib.parse.quote(str(value), safe="~")


def sign_aliyun_rpc(params: dict, access_key_id: str, access_key_secret: str,
                    method: str = "GET", nonce: str | None = None,
                    timestamp: str | None = None) -> str:
    """给 params 原地补齐阿里云 RPC 公共参数并写入 Signature，返回签名值。

    nonce / timestamp 参数只为单测固定值用；生产走默认（随机 nonce + 当前 UTC 时间）。
    """
    params.setdefault("Format", "JSON")
    params.setdefault("SignatureMethod", "HMAC-SHA1")
    params.setdefault("SignatureVersion", ALIYUN_RPC_SIGNATURE_VERSION)
    params["SignatureNonce"] = nonce or uuid.uuid4().hex
    params["Timestamp"] = timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    params["AccessKeyId"] = access_key_id

    canonical = "&".join(
        f"{aliyun_encode(key)}={aliyun_encode(value)}" for key, value in sorted(params.items())
    )
    string_to_sign = f"{method.upper()}&{aliyun_encode('/')}&{aliyun_encode(canonical)}"
    digest = hmac.new((access_key_secret + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    params["Signature"] = signature
    return signature


class AuthApplier:
    """按配置给请求参数/请求头做鉴权（原地修改 params / headers）。"""

    def __init__(self, request_cfg: dict, job_dir: Path):
        self.cfg = request_cfg.get("auth") or {}
        self.type = str(self.cfg.get("type") or "none").lower()
        self.job_dir = job_dir
        self._custom_func = None
        if self.type == "custom":
            self._custom_func = self._load_custom_func()

    def _load_custom_func(self):
        """从 signers.py 里加载自定义签名函数（见 signers.example.py）。"""
        module_name = str(self.cfg.get("module") or "signers.py")
        func_name = str(self.cfg.get("func") or "")
        path = Path(module_name)
        if not path.is_absolute():
            path = self.job_dir / path
        if not path.is_file():
            raise SystemExit(f"找不到自定义签名文件：{path}")
        spec = importlib.util.spec_from_file_location("api2ods_signers", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        func = getattr(module, func_name, None)
        if not callable(func):
            raise SystemExit(f"{path} 里没有可调用的函数：{func_name}")
        return func

    def apply(self, params: dict, headers: dict, method: str = "GET") -> None:
        """给一次请求附加鉴权信息。"""
        if self.type in ("none", ""):
            return

        if self.type == "basic":
            username = str(self.cfg.get("username") or "")
            password = str(self.cfg.get("password") or "")
            token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
            return

        if self.type == "token":
            header = str(self.cfg.get("header") or "Authorization")
            value = str(self.cfg.get("value") or "")
            prefix = str(self.cfg.get("prefix") or "")
            headers[header] = f"{prefix}{value}"
            return

        if self.type == "bearer":
            # 最常见的写法：Authorization: Bearer <token>（配置只需 token 一个字段）
            token = str(self.cfg.get("token") or self.cfg.get("value") or "")
            headers["Authorization"] = f"Bearer {token}"
            return

        if self.type == "query":
            for key, value in (self.cfg.get("params") or {}).items():
                params[str(key)] = value
            return

        if self.type == "sha256_concat":
            self._apply_sha256_concat(params, headers)
            return

        if self.type == "aliyun_rpc":
            sign_aliyun_rpc(
                params,
                access_key_id=str(self.cfg.get("access_key_id") or ""),
                access_key_secret=str(self.cfg.get("access_key_secret") or ""),
                method=method,
            )
            return

        if self.type == "custom":
            context = {"params": params, "headers": headers, "request": self.cfg, "method": method}
            result = self._custom_func(context)
            if isinstance(result, dict):
                for key, value in (result.get("params") or {}).items():
                    params[str(key)] = value
                for key, value in (result.get("headers") or {}).items():
                    headers[str(key)] = value
            return

        raise SystemExit(f"未知鉴权类型：{self.type}")

    def _apply_sha256_concat(self, params: dict, headers: dict) -> None:
        """Onerway 式签名：非空参数按 key 排序拼接 + 密钥，sha256 十六进制。

        签名值与 onerway_sync.py 完全一致：参与排序的是「除签名字段外、值非空的
        所有参数」，startTime/endTime/current/size 等一起参与。
        """
        secret = str(self.cfg.get("secret_key") or "")
        if not secret:
            raise SystemExit("auth.type=sha256_concat 必须给 auth.secret_key")
        sign_field = str(self.cfg.get("sign_field") or "sign")
        sign_in = str(self.cfg.get("sign_in") or "body").lower()
        keys = sorted(k for k in params if k != sign_field and params[k] not in (None, ""))
        text = "".join(str(params[k]) for k in keys) + secret
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if sign_in == "header":
            headers[sign_field] = digest
        else:
            params[sign_field] = digest
