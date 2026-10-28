"""Tests for fish_alert.py. Run: python -m unittest test_fish_alert -v"""
import hashlib
import hmac
import io
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("FISH_CHAT_ID", "-1001234567890")
os.environ.setdefault("TOOLS_API_URL", "http://tools.test")
os.environ.setdefault("TOOLS_INTERNAL_SECRET", "s3cret")
import fish_alert as fa  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SignedRequestTest(unittest.TestCase):
    def test_signature_is_hmac_of_raw_body(self):
        body, sig = fa.signed_request(42, -1001234567890, now=1000, secret="s3cret")
        self.assertEqual(json.loads(body), {"chat_id": "-1001234567890", "tg_id": 42, "ts": 1000})
        self.assertEqual(sig, hmac.new(b"s3cret", body.encode(), hashlib.sha256).hexdigest())

    def test_same_bytes_as_the_node_service_expects(self):
        # the Node side verifies the raw body bytes, so no spaces may sneak in
        body, _ = fa.signed_request(1, 2, now=3, secret="x")
        self.assertEqual(body, '{"chat_id":"2","tg_id":1,"ts":3}')


class AskToolsTest(unittest.TestCase):
    def test_approve(self):
        opener = mock.Mock(return_value=_Resp(b'{"approve": true}'))
        self.assertEqual(fa.ask_tools(1, -100, opener=opener), {"approve": True})
        req = opener.call_args[0][0]
        self.assertEqual(req.full_url, "http://tools.test/internal/join-request")
        self.assertIn("X-iwru-signature", req.headers)

    def test_decline(self):
        opener = mock.Mock(return_value=_Resp(b'{"approve": false, "reason": "not-linked"}'))
        self.assertEqual(fa.ask_tools(1, -100, opener=opener)["approve"], False)

    def test_service_down_means_no_decision(self):
        opener = mock.Mock(side_effect=OSError("connection refused"))
        self.assertIsNone(fa.ask_tools(1, -100, opener=opener))

    def test_garbage_means_no_decision(self):
        opener = mock.Mock(return_value=_Resp(b"<html>502</html>"))
        self.assertIsNone(fa.ask_tools(1, -100, opener=opener))
        opener = mock.Mock(return_value=_Resp(b'{"ok": true}'))
        self.assertIsNone(fa.ask_tools(1, -100, opener=opener))


class JoinRequestTest(unittest.IsolatedAsyncioTestCase):
    def _update(self, chat_id):
        req = mock.Mock()
        req.chat.id = chat_id
        req.from_user.id = 7
        req.approve = mock.AsyncMock()
        req.decline = mock.AsyncMock()
        upd = mock.Mock()
        upd.chat_join_request = req
        return upd, req

    async def test_approves_when_service_says_so(self):
        upd, req = self._update(int(fa.FISH_CHAT_ID))
        with mock.patch.object(fa, "ask_tools", return_value={"approve": True}):
            await fa.on_join_request(upd, None)
        req.approve.assert_awaited_once()
        req.decline.assert_not_called()

    async def test_declines_when_service_refuses(self):
        upd, req = self._update(int(fa.FISH_CHAT_ID))
        with mock.patch.object(fa, "ask_tools", return_value={"approve": False, "reason": "access-INACTIVE"}):
            await fa.on_join_request(upd, None)
        req.decline.assert_awaited_once()
        req.approve.assert_not_called()

    async def test_leaves_pending_when_service_down(self):
        upd, req = self._update(int(fa.FISH_CHAT_ID))
        with mock.patch.object(fa, "ask_tools", return_value=None):
            await fa.on_join_request(upd, None)
        req.approve.assert_not_called()
        req.decline.assert_not_called()

    async def test_other_chats_ignored(self):
        upd, req = self._update(-100999)
        with mock.patch.object(fa, "ask_tools") as ask:
            await fa.on_join_request(upd, None)
        ask.assert_not_called()

    async def test_truthy_but_not_true_is_not_approval(self):
        upd, req = self._update(int(fa.FISH_CHAT_ID))
        with mock.patch.object(fa, "ask_tools", return_value={"approve": "yes"}):
            await fa.on_join_request(upd, None)
        req.approve.assert_not_called()


class QuietTest(unittest.IsolatedAsyncioTestCase):
    async def test_updates_from_alert_group_stop_there(self):
        upd = mock.Mock()
        upd.effective_chat.id = int(fa.FISH_CHAT_ID)
        upd.chat_join_request = None
        with self.assertRaises(fa.ApplicationHandlerStop):
            await fa._quiet_other_updates(upd, None)

    async def test_other_chats_continue(self):
        upd = mock.Mock()
        upd.effective_chat.id = -100555
        upd.chat_join_request = None
        await fa._quiet_other_updates(upd, None)  # no exception


if __name__ == "__main__":
    unittest.main()
