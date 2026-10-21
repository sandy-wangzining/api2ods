# -*- coding: utf-8 -*-
"""飞书通知与脱敏。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _helpers import (
    OfflineTestCase,
)

from api2ods import notify as notify_mod  # noqa: E402
from api2ods import (  # noqa: E402
    utils,
)


class TestNotifyModule(OfflineTestCase):
    """api2ods.notify：发卡片、禁用/缺 webhook 跳过、失败不抛且不透出 hook id。"""

    def test_skip_when_disabled_or_missing_webhook(self):
        self.assertFalse(notify_mod.notify("", "t", ["x"]))
        self.assertFalse(notify_mod.notify("https://x/hook/abc", "t", ["x"], enabled=False))

    def test_success_payload(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"code": 0}
        with mock.patch.object(notify_mod.requests, "post", return_value=resp) as post:
            ok = notify_mod.notify(
                "https://open.feishu.cn/open-apis/bot/v2/hook/abc1234", "标题", ["行1"], footer="脚注"
            )
        self.assertTrue(ok)
        card = post.call_args.kwargs["json"]
        self.assertEqual(card["msg_type"], "interactive")
        self.assertIn("标题", card["card"]["header"]["title"]["content"])
        self.assertIn("行1", card["card"]["elements"][0]["text"]["content"])

    def test_false_and_float_zero_are_not_success_codes(self):
        """`False == 0`、`0.0 == 0` 都是真：布尔 false / 浮点 0 的失败响应不能当成功码。"""
        for payload in ({"code": False, "message": "invalid token"}, {"code": 0.0}):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertFalse(notify_mod.notify("https://x/hook/abc", "t", ["x"]), payload)

    def test_multiple_webhooks(self):
        """webhook 写成数组时逐个群发送（多个群同时告警）。"""
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"code": 0}
        hooks = ["https://open.feishu.cn/open-apis/bot/v2/hook/aaa", "https://open.feishu.cn/open-apis/bot/v2/hook/bbb"]
        with mock.patch.object(notify_mod.requests, "post", return_value=resp) as post:
            self.assertTrue(notify_mod.notify(hooks, "t", ["x"]))
        self.assertEqual([call.args[0] for call in post.call_args_list], hooks)

    def test_multiple_webhooks_partial_failure(self):
        """单个群失败不影响其它群，整体返回 False（告警失败不改主流程退出码）。"""
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"code": 0}

        def post(url, **kwargs):
            if url.endswith("/bad"):
                raise RuntimeError("down")
            return resp

        with mock.patch.object(notify_mod.requests, "post", side_effect=post) as fake_post:
            self.assertFalse(notify_mod.notify(["https://x/hook/bad", "https://x/hook/ok"], "t", ["x"]))
        self.assertEqual(fake_post.call_count, 2)

    def test_invalid_webhook_type_warns_and_skips(self):
        """webhook 写成对象等非法类型时不能静默跳过告警（配置错要能看见）。"""
        messages: list[str] = []
        with mock.patch.object(notify_mod, "log", side_effect=lambda msg: messages.append(str(msg))):
            self.assertFalse(notify_mod.notify({"url": "https://x/hook/abc"}, "t", ["x"]))
        self.assertTrue(any("没有可用地址" in msg for msg in messages))

    def test_failure_does_not_raise_and_redacts_hook_id(self):
        messages: list[str] = []
        hook = "https://open.feishu.cn/open-apis/bot/v2/hook/deadbeef00cafe"
        with (
            mock.patch.object(notify_mod.requests, "post", side_effect=RuntimeError(f"Max retries: {hook}")),
            mock.patch.object(notify_mod, "log", side_effect=lambda msg: messages.append(str(msg))),
        ):
            self.assertFalse(notify_mod.notify(hook, "t", ["x"]))
        joined = "\n".join(messages)
        self.assertIn("飞书通知发送失败", joined)
        self.assertNotIn("deadbeef00cafe", joined)

    def test_missing_code_is_failure(self):
        """缺 code/StatusCode 的 200 响应不能算「已发送」：webhook 误填成其它接口
        （回 {"msg": "ok"} 这类）时会静默失效；仅空 {} 保留按 HTTP 200 判定的宽容。"""
        for payload, expected in (({"msg": "ok"}, False), ({}, True), ({"code": "0"}, True)):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = payload
            with mock.patch.object(notify_mod.requests, "post", return_value=resp):
                self.assertEqual(
                    notify_mod.notify("https://open.feishu.cn/open-apis/bot/v2/hook/abc1234", "t", ["x"]),
                    expected,
                    payload,
                )


class TestNotifyRedaction(OfflineTestCase):
    """webhook 是凭证：裸 hook id 纳入值级脱敏，/hook/<id> 形态也要遮。"""

    def test_safe_label_escapes_control_and_backticks(self):
        """外部字段名进日志/飞书卡片前转义：换行/控制字符/反引号不能原样拼入。"""
        self.assertEqual(utils.safe_label("a`b\nc\x00d"), "a'b c d")
        long = "x" * 100
        shown = utils.safe_label(long)
        self.assertEqual(len(shown), 81)
        self.assertTrue(shown.endswith("…"))

    def test_webhook_hook_id_collected(self):
        job = {"notify": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/0123abcd-0000-4ef0"}}
        self.assertIn("0123abcd-0000-4ef0", utils.collect_secret_values(job))

    def test_webhook_array_hook_ids_collected(self):
        job = {
            "notify": {
                "webhook": [
                    "https://open.feishu.cn/open-apis/bot/v2/hook/hook-one-1111",
                    "https://open.feishu.cn/open-apis/bot/v2/hook/hook-two-2222",
                ]
            }
        }
        values = utils.collect_secret_values(job)
        self.assertIn("hook-one-1111", values)
        self.assertIn("hook-two-2222", values)

    def test_redact_masks_hook_id_shape(self):
        text = "Max retries exceeded with url: /open-apis/bot/v2/hook/deadbeefcafe (Caused by ...)"
        self.assertNotIn("deadbeefcafe", utils.redact(text))
