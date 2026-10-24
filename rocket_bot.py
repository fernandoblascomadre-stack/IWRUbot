"""rocket_bot.py -- the Telegram side of the Dexscreener 🚀 votes (2026-10-23, Fernando). The rules live in
rocket_votes.py (RocketLedger); this file only talks to Telegram.

OFF unless ROCKET_VOTES_ENABLED=1 (and CATCH_PRIZE_SECRET, which signs the Essence codes, is set).

Flow:
- 8 posts a day in the group (rocket_votes.SLOTS_MADRID): Fernando's image as a sticker (assets/rocket_vote.webp,
  transparent background; skipped while it is missing) and right under it a short rotating cat line with two buttons:
  "🚀 VOTE · <real rocket count>" (kept up to date by refresh_button) and "📊 Statistics" (a pop-up with this month's
  voters, restarting on day 1, 00:00 Madrid). VOTE opens a private chat with the bot (deep link ?start=rocket -- a
  plain link button can't tell the bot who tapped it). Each post deletes the previous one (both messages).
- Private chat: only group members; the bot gives the voter an exclusive turn (or a place in the queue) with two
  buttons, "Open Dexscreener" and "I voted". After "I voted" it re-reads the counter every CHECK_EVERY_S for up to
  rocket_votes.CLAIM_WAIT_S: a new record -> a one-time code for 20 Essence (prize_code.new_essence_code), redeemed in
  PARADOX where the Catch card codes are, and a public shout-out in the group.
- Every POLL_S: one read of the counter (rocket_votes.observe: votes outside the bot become the record, a big jump
  freezes payouts and DMs the Owner), and the next one in the queue is called when the turn is free.
- Writing "vote" (or /vote) in the group or in a private chat brings up the vote post (VOTE_WORD_COOLDOWN_S per chat).
- /rockets: the real voters (name, date, Essence). /rocketsunfreeze (Owner): resume payouts after a freeze.
"""
import asyncio
import os
import random
import re
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

import db
import events_config as cfg
import prize_code
import rocket_votes as rv

ENABLED = os.environ.get("ROCKET_VOTES_ENABLED", "").strip() == "1"
# Fernando's image with a transparent background, sent as a STICKER (a photo would lose the transparency). Make it from
# the PNG with: python make_rocket_sticker.py <image.png>
STICKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "rocket_vote.webp")
GROUP_LINK = "https://t.me/iwillrug_u"
PARADOX_HOME = "https://iwillrugu.com/paradox/#/"
POLL_S = 60
CHECK_EVERY_S = 10
VOTE_WORD = re.compile(r"\W*/?vote(@\w+)?\W*", re.IGNORECASE)
VOTE_WORD_COOLDOWN_S = 60  # per chat: "vote" written 10 times in a row brings up one post
MEMBER_STATUSES = {"member", "administrator", "creator", "owner"}

DM_TURN = (
    "🚀 your turn to launch a rocket!\n\n"
    "1. tap «🚀 Open Dexscreener» and press the 🚀 on the IWRU page\n"
    "2. come back here: I'll spot your rocket by myself (if it takes long, tap «✅ I voted» and I'll keep looking)\n\n"
    "when your rocket shows up on the counter, you get a code for 20 Essence to redeem here: {redeem}\n"
    "it's added to your account.\n\n"
    "⚠️ IMPORTANT: you need an account at {home}\n\n"
    "your turn lasts {minutes} minutes. 🐾"
)
DM_WAIT = ("🚀 someone is launching a rocket right now. you're #{pos} in line: I'll message you the moment it's your turn.\n\n"
           "⚠️ DON'T vote on Dexscreener yet: a rocket only counts for you during YOUR turn. 🐾")
DM_CHECKING = "🔍 checking the rocket counter… (up to {minutes} minutes)"
DM_PAID = "🎉 your rocket is in! here's your code for 20 Essence:\n\n{code}\n\nredeem it here (logged in to your account): {link}"
DM_ALREADY = ("you already launched today's rocket 🚀 (one a day). today's code:\n\n{code}\n\nredeem it here: {link}\n\n"
              "come back tomorrow for another one. 🐾")
DM_NOT_MEMBER = "join the IWRU group first, then tap 🚀 VOTE there again: {group} 🐾"
DM_NO_DATA = "I can't read the rocket counter right now. 😿 try again in a few minutes."
DM_FROZEN = "rocket rewards are paused for a moment while the cat checks something. try again later. 🐾"
DM_NOT_YET = ("I can't see your rocket yet 👀 did you tap 🚀 on the IWRU page? if you just did, tap «✅ I voted» once more. "
              "if your turn ran out, tap 🚀 VOTE in the group again.")
DM_NO_TURN = ("your turn ran out ⏰ tap 🚀 VOTE in the group to get a new one. "
              "(only new rockets pay: if you voted outside your turn, the cat can't tell it was you.)")
OWNER_FROZEN = "⚠️ rocket votes: the 🚀 count jumped from {old} to {new} between two reads. payouts are frozen. /rocketsunfreeze to resume."

_ledger = None
_vote_word_last = {}
_checking = set()
_bags = {}


def ledger():
    global _ledger
    if _ledger is None:
        _ledger = rv.RocketLedger(db._conn, lambda tg_id: prize_code.new_essence_code(cfg.CATCH_PRIZE_SECRET))
    return _ledger


def _pick(lines):
    """Every line once before any repeats (same idea as bot.pick_phrase, without importing bot.py)."""
    bag = _bags.get(id(lines))
    if not bag:
        bag = list(lines)
        random.shuffle(bag)
        _bags[id(lines)] = bag
    return bag.pop()


def _name(user):
    return "@" + user.username if user.username else (user.first_name or "someone")


def _redeem_link(code=None):
    return f"{cfg.PARADOX_REDEEM_URL}?code={code}" if code else cfg.PARADOX_REDEEM_URL


async def read_count():
    return await asyncio.to_thread(rv.fetch_rocket_count)


def _turn_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Open Dexscreener", url=rv.DEXSCREENER_LINK)],
        [InlineKeyboardButton("✅ I voted", callback_data="rv:voted")],
    ])


def _vote_keyboard(bot_username, count):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(rv.vote_button_text(count), url=f"https://t.me/{bot_username}?start=rocket")],
        [InlineKeyboardButton(rv.STATS_BUTTON, callback_data="rv:stats")],
    ])


async def refresh_button(bot, count):
    """Keeps the 🚀 total on the last post's VOTE button in step with the real counter (one edit per change)."""
    mid = db.get_config("rv_post_msg")
    if count is None or not (mid or "").isdigit() or db.get_config("rv_button_count") == str(count):
        return
    try:
        await bot.edit_message_reply_markup(chat_id=cfg.EVENTS_CHAT_ID, message_id=int(mid),
                                            reply_markup=_vote_keyboard(bot.username, count))
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            print(f"[rocket] button count not updated: {e}", flush=True)
            return
    db.set_config("rv_button_count", str(count))


async def on_stats(update, context):
    """📊 Statistics: a pop-up with this month's voters (restarts on day 1, 00:00 Madrid), only for whoever tapped."""
    now = time.time()
    await update.callback_query.answer(rv.stats_text(ledger().month_stats(now), now), show_alert=True)


async def is_member(bot, user_id):
    try:
        m = await bot.get_chat_member(cfg.EVENTS_CHAT_ID, user_id)
    except TelegramError:
        return False
    return m.status in MEMBER_STATUSES or (m.status == "restricted" and bool(getattr(m, "is_member", False)))


async def _send_turn(bot, chat_id):
    await bot.send_message(chat_id=chat_id, text=DM_TURN.format(redeem=_redeem_link(), home=PARADOX_HOME,
                                                                minutes=rv.LOCK_S // 60),
                           reply_markup=_turn_keyboard(), disable_web_page_preview=True)


async def _answer_start(bot, chat_id, res):
    kind = res[0]
    if kind == "turn":
        await _send_turn(bot, chat_id)
    elif kind == "wait":
        await bot.send_message(chat_id=chat_id, text=DM_WAIT.format(pos=res[1]))
    elif kind == "paid":
        await bot.send_message(chat_id=chat_id, text=DM_ALREADY.format(code=res[1], link=_redeem_link(res[1])),
                               disable_web_page_preview=True)
    elif kind == "not_member":
        await bot.send_message(chat_id=chat_id, text=DM_NOT_MEMBER.format(group=GROUP_LINK))
    elif kind == "no_data":
        await bot.send_message(chat_id=chat_id, text=DM_NO_DATA)
    elif kind == "frozen":
        await bot.send_message(chat_id=chat_id, text=DM_FROZEN)


async def on_start(update, context):
    """/start rocket in the private chat (the group's 🚀 VOTE button). Called from events.cmd_start."""
    user = update.effective_user
    member = await is_member(context.bot, user.id)
    count = await read_count()
    res = ledger().start(user.id, _name(user), time.time(), count, is_member=member)
    await _answer_start(context.bot, update.effective_chat.id, res)


async def on_voted(update, context):
    q = update.callback_query
    uid = q.from_user.id
    try:
        await q.answer()
    except TelegramError:
        pass
    if uid in _checking:
        return
    ledger().extend_for_claim(uid, time.time())
    _checking.add(uid)
    await context.bot.send_message(chat_id=uid, text=DM_CHECKING.format(minutes=rv.CLAIM_WAIT_S // 60))
    context.application.job_queue.run_once(
        claim_check_job, 0, data={"uid": uid, "name": _name(q.from_user), "deadline": time.time() + rv.CLAIM_WAIT_S})


async def claim_check_job(context):
    d = context.job.data
    uid = d["uid"]
    try:
        count = await read_count()
        res = ledger().claim(uid, time.time(), count)
        kind = res[0]
        if kind in ("paid", "already"):
            _checking.discard(uid)
            if kind == "paid":  # "already": the turn watch paid it a moment ago and sent everything
                await deliver(context.bot, uid, d["name"], res[1])
                await call_next(context.bot)
            return
        if kind in ("not_yet", "no_data") and time.time() < d["deadline"]:
            context.application.job_queue.run_once(claim_check_job, CHECK_EVERY_S, data=d)
            return
        _checking.discard(uid)
        pos = ledger().queue_position(uid)
        if kind == "no_turn" and pos:
            text = DM_WAIT.format(pos=pos)  # still in line: it was never their turn
        else:
            text = {"not_yet": DM_NOT_YET, "no_data": DM_NO_DATA, "frozen": DM_FROZEN, "no_turn": DM_NO_TURN}[kind]
        await context.bot.send_message(chat_id=uid, text=text)
    except Exception as e:
        _checking.discard(uid)
        print(f"[rocket] claim check for {uid}: {e}", flush=True)


async def deliver(bot, uid, name, code):
    try:
        await bot.send_message(chat_id=uid, text=DM_PAID.format(code=code, link=_redeem_link(code)),
                               disable_web_page_preview=True)
    except TelegramError as e:
        print(f"[rocket] could not DM the code to {uid} (it stays in rv_votes, /start rocket resends it): {e}", flush=True)
    try:
        await bot.send_message(chat_id=cfg.EVENTS_CHAT_ID,
                               text=_pick(rv.SHOUTOUT_LINES).format(name=name, n=ledger().record()))
    except TelegramError as e:
        print(f"[rocket] shout-out failed: {e}", flush=True)
    await refresh_button(bot, ledger().record())  # the new rocket shows on the VOTE button at once


async def call_next(bot):
    """The turn is free: give it to the first one in the queue and DM them. Someone the bot can't reach (blocked it,
    deleted the chat) loses the turn at once and the next one is called."""
    L = ledger()
    for _ in range(20):
        nxt = L.next_in_queue(time.time())
        if nxt is None:
            return
        count = await read_count()
        res = L.start(nxt[0], nxt[1], time.time(), count)
        if res[0] != "turn":
            if res[0] in ("no_data", "frozen"):
                return  # stays first in line; the next poll tries again
            L.c.execute("DELETE FROM rv_queue WHERE tg_id=?", (nxt[0],))  # already paid: nothing to wait for
            L.c.commit()
        try:
            await _answer_start(bot, nxt[0], res)
            if res[0] == "turn":
                return
        except TelegramError as e:
            L.release(nxt[0])
            print(f"[rocket] could not call {nxt[0]}, turn released: {e}", flush=True)


async def turn_watch_job(context):
    """Every CHECK_EVERY_S while someone holds the turn: the rocket is spotted without waiting for "I voted", the code
    goes out and the next one in line is called at once (a turn lasts as long as voting takes, not the whole window)."""
    try:
        L = ledger()
        h = L.holder(time.time())
        if h is None:
            return
        count = await read_count()
        res = L.claim(h[0], time.time(), count)
        if res[0] == "paid":
            _checking.discard(h[0])
            await deliver(context.bot, h[0], h[1], res[1])
            await call_next(context.bot)
    except Exception as e:
        print(f"[rocket] turn watch: {e}", flush=True)


async def poll_job(context):
    try:
        L = ledger()
        old = L._get("last_count", int)
        count = await read_count()
        if L.observe(time.time(), count) == "frozen":
            try:
                await context.bot.send_message(chat_id=cfg.BOT_OWNER_ID, text=OWNER_FROZEN.format(old=old, new=count))
            except TelegramError:
                pass
        await refresh_button(context.bot, count)
        await call_next(context.bot)
    except Exception as e:
        print(f"[rocket] poll: {e}", flush=True)


def _schedule_post(job_queue):
    delay, key = rv.next_post(db.get_config("rv_slot_last"))
    print(f"[rocket] next vote post in {delay / 3600:.1f}h (slot {key})", flush=True)
    job_queue.run_once(post_job, delay, data=key, name="rocket_post")


async def send_post(bot, chat_id):
    """The vote post: sticker, one short cat line, VOTE · count + Statistics. In the group it replaces the previous
    post (both messages deleted) and becomes the one whose VOTE count refresh_button keeps up to date."""
    count = await read_count()
    text = _pick(rv.ROCKET_LINES)  # Fernando 2026-10-24: just the short cat line; the count is on the VOTE button
    if count is not None:
        ledger().observe(time.time(), count)
    group = chat_id == cfg.EVENTS_CHAT_ID
    if group:
        for prev in (db.get_config("rv_last_msg") or "").split(","):
            if prev.isdigit():
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(prev))
                except TelegramError:
                    pass  # too old (48h) or already gone
    ids = []
    if os.path.isfile(STICKER):
        try:
            with open(STICKER, "rb") as f:
                ids.append((await bot.send_sticker(chat_id=chat_id, sticker=f)).message_id)
        except TelegramError as e:
            print(f"[rocket] sticker failed, text only: {e}", flush=True)
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=_vote_keyboard(bot.username, count))
    ids.append(msg.message_id)
    if group:
        db.set_config("rv_last_msg", ",".join(str(i) for i in ids))
        db.set_config("rv_post_msg", str(msg.message_id))
        db.set_config("rv_button_count", "" if count is None else str(count))


async def post_job(context):
    key = context.job.data
    try:
        db.set_config("rv_slot_last", key)  # before sending: a restart mid-send never posts this slot twice
        await send_post(context.bot, cfg.EVENTS_CHAT_ID)
    except Exception as e:
        print(f"[rocket] post: {e}", flush=True)
    finally:
        _schedule_post(context.application.job_queue)


def is_vote_word(text):
    """Fernando 2026-10-24: writing "vote" in the chat brings up the vote post ("vote", "VOTE!", "/vote", "vote 🚀",
    "/vote@IWRUbot"); a sentence that only contains the word ("I'll vote later") does not."""
    return bool(VOTE_WORD.fullmatch((text or "").strip()))


async def on_vote_word(update, context):
    msg = update.effective_message
    if msg is None or not is_vote_word(msg.text):
        return
    chat_id = update.effective_chat.id
    if chat_id != cfg.EVENTS_CHAT_ID and update.effective_chat.type != "private":
        return  # only the IWRU group and private chats with the bot, never some other group
    now = time.time()
    if now - _vote_word_last.get(chat_id, 0) < VOTE_WORD_COOLDOWN_S:
        return  # the post from a moment ago is still right there: no flood of posts
    _vote_word_last[chat_id] = now
    try:
        await send_post(context.bot, chat_id)
    except Exception as e:
        print(f"[rocket] vote word: {e}", flush=True)


async def cmd_rockets(update, context):
    rows = ledger().ranking()
    if not rows:
        await update.message.reply_text("no rockets launched through the cat yet. be the first: tap 🚀 VOTE. 🐾")
        return
    out = ["🚀 rocket launchers (real votes, checked on the counter):", ""]
    for i, (name, ts, ess) in enumerate(rows[-30:], start=max(1, len(rows) - 29)):
        day = datetime.fromtimestamp(ts, tz=rv.MADRID_TZ).strftime("%d %b")
        out.append(f"{i}. {name} · {day} · +{ess} Essence")
    out += ["", f"total: {len(rows)} rockets · {len(rows) * rv.ESSENCE_PER_VOTE} Essence"]
    await update.message.reply_text("\n".join(out))


async def cmd_unfreeze(update, context):
    if update.effective_user.id != cfg.BOT_OWNER_ID:
        return
    ledger().unfreeze()
    await update.message.reply_text("rocket payouts resumed. 🚀")


def register(app):
    """Handlers + jobs, only with ROCKET_VOTES_ENABLED=1. Called from build_app() BEFORE the catch-all message handler,
    or /rockets would never reach cmd_rockets."""
    if not ENABLED or not cfg.CATCH_PRIZE_SECRET:
        print("[rocket] disabled -- set ROCKET_VOTES_ENABLED=1 (needs CATCH_PRIZE_SECRET)", flush=True)
        return
    app.add_handler(CallbackQueryHandler(on_voted, pattern=r"^rv:voted$"))
    app.add_handler(CallbackQueryHandler(on_stats, pattern=r"^rv:stats$"))
    app.add_handler(CommandHandler("rockets", cmd_rockets))
    app.add_handler(CommandHandler("rocketsunfreeze", cmd_unfreeze))
    # group 1: runs alongside the normal handlers (bot.py's catch-all still sees the message for its bookkeeping)
    app.add_handler(MessageHandler(filters.TEXT, on_vote_word), group=1)
    app.job_queue.run_repeating(poll_job, POLL_S, first=30)
    app.job_queue.run_repeating(turn_watch_job, CHECK_EVERY_S, first=CHECK_EVERY_S)
    _schedule_post(app.job_queue)
    print(f"[rocket] on: 8 vote posts a day -> chat {cfg.EVENTS_CHAT_ID}, sticker {'yes' if os.path.isfile(STICKER) else 'NO (text only)'}", flush=True)
