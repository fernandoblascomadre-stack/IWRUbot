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


TOK = "0x" + "ab" * 20


class FollowTest(unittest.IsolatedAsyncioTestCase):
    def test_signed_follow_body(self):
        body, sig = fa.signed_follow(42, TOK, True, now=7, secret="k")
        self.assertEqual(body, '{"follow":true,"tg_id":42,"token":"%s","ts":7}' % TOK)
        self.assertEqual(sig, hmac.new(b"k", body.encode(), hashlib.sha256).hexdigest())

    def test_ask_follow_posts_to_internal_follow(self):
        opener = mock.Mock(return_value=_Resp(b'{"ok": true, "reason": "following", "symbol": "FISH"}'))
        self.assertEqual(fa.ask_follow(1, TOK, True, opener=opener)["reason"], "following")
        req = opener.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/internal/follow"))
        self.assertEqual(req.get_header("X-iwru-signature"), hmac.new(fa.TOOLS_INTERNAL_SECRET.encode(), req.data, hashlib.sha256).hexdigest())

    def test_ask_follow_unreachable_is_none(self):
        self.assertIsNone(fa.ask_follow(1, TOK, True, opener=mock.Mock(side_effect=OSError("down"))))

    def test_popups(self):
        self.assertIn("You now follow FISH", fa.follow_popup({"reason": "following", "symbol": "FISH"})[0])
        self.assertIn("Unfollowed FISH", fa.follow_popup({"reason": "unfollowed", "symbol": "FISH"})[0])
        t, alert = fa.follow_popup({"reason": "cannot-dm"}, "IWRUbot")
        self.assertIn("@IWRUbot", t)
        self.assertTrue(alert)
        self.assertTrue(fa.follow_popup({"reason": "no-access"})[1])
        self.assertTrue(fa.follow_popup(None)[1])
        for t, _ in [fa.follow_popup({"reason": r, "symbol": "X"}) for r in ("following", "already-following", "unfollowed", "no-access", "cannot-dm", "too-many", "bad-request")] + [fa.follow_popup(None)]:
            self.assertLessEqual(len(t), 200, "Telegram pop-up limit")
        self.assertIn("5 tokens", fa.follow_popup({"reason": "too-many"})[0])

    def test_pattern(self):
        self.assertRegex("fw:" + TOK, fa.FOLLOW_PATTERN)
        self.assertRegex("uf:" + TOK, fa.FOLLOW_PATTERN)
        self.assertNotRegex("catch:1", fa.FOLLOW_PATTERN)
        self.assertNotRegex("fw:0x12", fa.FOLLOW_PATTERN)

    def _press(self, data, chat_type, markup=None):
        q = mock.Mock()
        q.data = data
        q.from_user.id = 42
        q.answer = mock.AsyncMock()
        q.message.chat.type = chat_type
        q.message.reply_markup = markup
        q.message.edit_reply_markup = mock.AsyncMock()
        upd = mock.Mock()
        upd.callback_query = q
        ctx = mock.Mock()
        ctx.bot.username = "IWRUbot"
        return upd, ctx, q

    def _markup(self, first):
        return fa.InlineKeyboardMarkup([[first, fa.InlineKeyboardButton("Buy IWRU", url="https://nad.fun/x"),
                                         fa.InlineKeyboardButton("🚀 Vote IWRU", url="https://dexscreener.com/x")]])

    async def test_follow_in_group_popup_and_stop(self):
        upd, ctx, q = self._press("fw:" + TOK, "supergroup")
        with mock.patch.object(fa, "ask_follow", return_value={"ok": True, "reason": "following", "symbol": "FISH"}) as ask:
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_follow_button(upd, ctx)
        ask.assert_called_once_with(42, TOK, True)
        self.assertIn("FISH", q.answer.call_args.kwargs["text"])
        q.message.edit_reply_markup.assert_not_called()  # the group's buttons are the same for everyone

    async def test_unfollow_in_private_flips_button_keeps_others(self):
        markup = self._markup(fa.InlineKeyboardButton("Unfollow", callback_data="uf:" + TOK))
        upd, ctx, q = self._press("uf:" + TOK, "private", markup)
        with mock.patch.object(fa, "ask_follow", return_value={"ok": True, "reason": "unfollowed", "symbol": "FISH"}) as ask:
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_follow_button(upd, ctx)
        ask.assert_called_once_with(42, TOK, False)
        new = q.message.edit_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual([b.text for b in new], ["Follow", "Buy IWRU", "🚀 Vote IWRU"])
        self.assertEqual(new[0].callback_data, "fw:" + TOK)
        self.assertEqual(new[1].url, "https://nad.fun/x")

    async def test_service_down_no_flip(self):
        markup = self._markup(fa.InlineKeyboardButton("Unfollow", callback_data="uf:" + TOK))
        upd, ctx, q = self._press("uf:" + TOK, "private", markup)
        with mock.patch.object(fa, "ask_follow", return_value=None):
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_follow_button(upd, ctx)
        q.message.edit_reply_markup.assert_not_called()
        self.assertTrue(q.answer.call_args.kwargs["show_alert"])


class NewFishTest(unittest.IsolatedAsyncioTestCase):
    def test_signed_body(self):
        body, sig = fa.signed_new_fish(42, True, now=7, secret="k")
        self.assertEqual(body, '{"on":true,"tg_id":42,"ts":7}')
        self.assertEqual(sig, hmac.new(b"k", body.encode(), hashlib.sha256).hexdigest())

    def test_ask_posts_to_internal_new_fish(self):
        opener = mock.Mock(return_value=_Resp(b'{"ok": true, "reason": "activated"}'))
        self.assertEqual(fa.ask_new_fish(1, True, opener=opener)["reason"], "activated")
        self.assertTrue(opener.call_args[0][0].full_url.endswith("/internal/new-fish"))
        self.assertIsNone(fa.ask_new_fish(1, True, opener=mock.Mock(side_effect=OSError("down"))))

    def test_pattern_and_popups(self):
        self.assertRegex("nf:on", fa.NEW_FISH_PATTERN)
        self.assertRegex("nf:off", fa.NEW_FISH_PATTERN)
        self.assertNotRegex("nf:maybe", fa.NEW_FISH_PATTERN)
        self.assertIn("ON", fa.new_fish_popup({"reason": "activated"})[0])
        self.assertIn("OFF", fa.new_fish_popup({"reason": "deactivated"})[0])
        self.assertIn("@IWRUbot", fa.new_fish_popup({"reason": "cannot-dm"}, "IWRUbot")[0])
        for r in ("activated", "already-active", "deactivated", "no-access", "cannot-dm", "x"):
            self.assertLessEqual(len(fa.new_fish_popup({"reason": r}, "IWRUbot")[0]), 200)

    def _press(self, data, chat_type, markup=None):
        q = mock.Mock()
        q.data = data
        q.from_user.id = 42
        q.answer = mock.AsyncMock()
        q.message.chat.type = chat_type
        q.message.reply_markup = markup
        q.message.edit_reply_markup = mock.AsyncMock()
        upd = mock.Mock()
        upd.callback_query = q
        ctx = mock.Mock()
        ctx.bot.username = "IWRUbot"
        return upd, ctx, q

    async def test_activate_in_group_keeps_the_sticker_button(self):
        upd, ctx, q = self._press("nf:on", "supergroup")
        with mock.patch.object(fa, "ask_new_fish", return_value={"ok": True, "reason": "activated"}) as ask:
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_new_fish_button(upd, ctx)
        ask.assert_called_once_with(42, True)
        q.message.edit_reply_markup.assert_not_called()

    async def test_deactivate_in_private_flips_only_that_button(self):
        markup = fa.InlineKeyboardMarkup([
            [fa.InlineKeyboardButton("Follow", callback_data="fw:0x" + "ab" * 20), fa.InlineKeyboardButton("Buy IWRU", url="https://nad.fun/x")],
            [fa.InlineKeyboardButton("DEACTIVATE", callback_data="nf:off")],
        ])
        upd, ctx, q = self._press("nf:off", "private", markup)
        with mock.patch.object(fa, "ask_new_fish", return_value={"ok": True, "reason": "deactivated"}) as ask:
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_new_fish_button(upd, ctx)
        ask.assert_called_once_with(42, False)
        rows = q.message.edit_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([[b.text for b in r] for r in rows], [["Follow", "Buy IWRU"], ["ACTIVATE"]])
        self.assertEqual(rows[1][0].callback_data, "nf:on")

    async def test_the_sticker_pair_never_changes(self):
        pair = fa.InlineKeyboardMarkup([[fa.InlineKeyboardButton("ACTIVATE", callback_data="nf:on"),
                                         fa.InlineKeyboardButton("DEACTIVATE", callback_data="nf:off")]])
        self.assertIsNone(fa.flipped_new_fish_markup(pair, True))
        self.assertIsNone(fa.flipped_new_fish_markup(pair, False))

    async def test_service_down_nothing_flips(self):
        markup = fa.InlineKeyboardMarkup([[fa.InlineKeyboardButton("DEACTIVATE", callback_data="nf:off")]])
        upd, ctx, q = self._press("nf:off", "private", markup)
        with mock.patch.object(fa, "ask_new_fish", return_value=None):
            with self.assertRaises(fa.ApplicationHandlerStop):
                await fa.on_new_fish_button(upd, ctx)
        q.message.edit_reply_markup.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class PersonalInviteTest(unittest.IsolatedAsyncioTestCase):
    """2026-10-31 (Fernando, option A): the personal 1-minute link says which wallet; no Telegram login on the web."""

    def test_body_carries_the_link_used_and_the_name(self):
        body, sig = fa.signed_request(7, -100, now=5, secret="k", invite_link="https://t.me/+abc", tg_name="@fish")
        self.assertEqual(json.loads(body), {"chat_id": "-100", "tg_id": 7, "ts": 5, "invite_link": "https://t.me/+abc", "tg_name": "@fish"})
        self.assertEqual(sig, hmac.new(b"k", body.encode(), hashlib.sha256).hexdigest())

    def _update(self, link="https://t.me/+abc", username="fish"):
        req = mock.Mock()
        req.chat.id = int(fa.FISH_CHAT_ID)
        req.from_user.id = 7
        req.from_user.username = username
        req.invite_link.invite_link = link
        req.user_chat_id = 7
        order = []
        req.approve = mock.AsyncMock(side_effect=lambda: order.append("approve"))
        req.decline = mock.AsyncMock()
        upd = mock.Mock()
        upd.chat_join_request = req
        ctx = mock.Mock()
        ctx.bot.username = "IWRUbot"
        ctx.bot.send_message = mock.AsyncMock(side_effect=lambda *a, **k: order.append("welcome"))
        return upd, req, ctx, order

    async def test_link_reaches_the_service_and_welcome_goes_before_approval(self):
        upd, req, ctx, order = self._update()
        with mock.patch.object(fa, "ask_tools", return_value={"approve": True}) as ask:
            await fa.on_join_request(upd, ctx)
        self.assertEqual(ask.call_args.kwargs["invite_link"], "https://t.me/+abc")
        self.assertEqual(ask.call_args.kwargs["tg_name"], "@fish")
        self.assertEqual(order, ["welcome", "approve"], "the bot may only write while the request is open")
        args, kwargs = ctx.bot.send_message.call_args
        self.assertEqual(args[0], 7)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].url, "https://t.me/IWRUbot?start=fish")

    async def test_no_welcome_when_refused_and_a_failed_welcome_never_blocks_approval(self):
        upd, req, ctx, order = self._update()
        with mock.patch.object(fa, "ask_tools", return_value={"approve": False, "reason": "invite-used"}):
            await fa.on_join_request(upd, ctx)
        ctx.bot.send_message.assert_not_called()
        req.decline.assert_awaited_once()
        upd, req, ctx, order = self._update()
        ctx.bot.send_message = mock.AsyncMock(side_effect=RuntimeError("Forbidden"))
        with mock.patch.object(fa, "ask_tools", return_value={"approve": True}):
            await fa.on_join_request(upd, ctx)
        req.approve.assert_awaited_once()

    async def test_start_fish_confirms(self):
        upd = mock.Mock()
        upd.effective_message.reply_text = mock.AsyncMock()
        with self.assertRaises(fa.ApplicationHandlerStop):
            await fa.on_start_fish(upd, None)
        self.assertIn("Done", upd.effective_message.reply_text.call_args[0][0])
