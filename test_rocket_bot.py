"""Dexscreener 🚀 votes, the Telegram flow end to end (2026-10-23, draft): a fake Telegram (messages, buttons, member
checks, the job queue) and a rocket counter the test moves by hand; everything else is the real code (rocket_bot.py,
rocket_votes.RocketLedger on a real sqlite file, prize_code). Run: python test_rocket_bot.py"""
import asyncio
import os
import sys
import tempfile
import types

TMP = tempfile.mkdtemp()
os.environ.update({
    "EVENTS_DB_PATH": os.path.join(TMP, "events.db"), "EVENTS_CHAT_ID": "-1001111", "BOT_OWNER_ID": "42",
    "CATCH_PRIZE_SECRET": "rocket-test-secret", "ROCKET_VOTES_ENABLED": "1",
})
import db  # noqa: E402
db.init_db()
import events_config as cfg  # noqa: E402
import prize_code  # noqa: E402
import rocket_bot as rb  # noqa: E402
import rocket_votes as rv  # noqa: E402

GROUP = cfg.EVENTS_CHAT_ID


class Clock:
    t = 1_800_000_000.0

    def time(self):
        return self.t


clock = Clock()
rb.time = clock
counter = {"n": 5}


async def fake_read():
    return counter["n"]


rb.read_count = fake_read


class Msg:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    username = "IWRUcatBot"

    def __init__(self):
        self.sent, self.deleted, self.members, self.n, self.blocked, self.edits = [], [], {}, 100, set(), []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            from telegram.error import Forbidden
            raise Forbidden("bot was blocked by the user")
        self.n += 1
        self.sent.append({"chat": chat_id, "text": text, "kb": reply_markup, "id": self.n})
        return Msg(self.n)

    async def send_sticker(self, chat_id, sticker, **kw):
        data = sticker.read()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", "a sticker must be a WEBP file"
        return await self.send_message(chat_id, "<sticker>")

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.edits.append((chat_id, message_id, reply_markup))
        for m in self.sent:
            if m["chat"] == chat_id and m["id"] == message_id:
                m["kb"] = reply_markup

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def get_chat_member(self, chat_id, user_id):
        return types.SimpleNamespace(status=self.members.get(user_id, "left"))


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, cb, when, data=None, name=None):
        self.jobs.append((clock.t + when, cb, data))

    def run_repeating(self, cb, interval, first=0):
        pass


bot, jq = FakeBot(), FakeJobQueue()
app = types.SimpleNamespace(job_queue=jq)


def ctx(data=None, args=None):
    return types.SimpleNamespace(bot=bot, application=app, job=types.SimpleNamespace(data=data), args=args or [])


async def run_jobs(until):
    """Runs every queued job due by `until`, in time order (jobs may queue more)."""
    while True:
        due = sorted([j for j in jq.jobs if j[0] <= until], key=lambda j: j[0])
        if not due:
            clock.t = max(clock.t, until)
            return
        j = due[0]
        jq.jobs.remove(j)
        clock.t = max(clock.t, j[0])
        await j[1](ctx(j[2]))


def user(uid, username):
    return types.SimpleNamespace(id=uid, username=username, first_name=username)


async def start(u):
    upd = types.SimpleNamespace(effective_user=u, effective_chat=types.SimpleNamespace(id=u.id))
    await rb.on_start(upd, ctx(args=["rocket"]))


async def voted(u):
    async def answer():
        return None
    upd = types.SimpleNamespace(callback_query=types.SimpleNamespace(from_user=u, answer=answer))
    await rb.on_voted(upd, ctx())


def last_to(chat):
    return [m for m in bot.sent if m["chat"] == chat][-1]


def buttons(m):
    return [b for row in m["kb"].inline_keyboard for b in row] if m["kb"] else []


async def stats(u):
    got = {}

    async def answer(text=None, show_alert=False, **kw):
        got.update(text=text, show_alert=show_alert)
    upd = types.SimpleNamespace(callback_query=types.SimpleNamespace(from_user=u, answer=answer))
    await rb.on_stats(upd, ctx())
    return got


async def ranking_text():
    out = []

    async def reply(t):
        out.append(t)
    upd = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=reply))
    await rb.cmd_rockets(upd, ctx())
    return out[0]


ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok  " + label)
    else:
        fail += 1
        print("FAIL  " + label + "  " + str(detail)[:400])


async def main():
    ana, bob, cid, out = user(1, "ana"), user(2, "bob"), user(3, "cid"), user(9, "outsider")
    bot.members.update({1: "member", 2: "member", 3: "administrator"})

    print("group post")
    await rb.post_job(ctx("2026-10-24:3"))
    p = last_to(GROUP)
    b = buttons(p)
    check("the post text is just one short cat line", p["text"] in rv.ROCKET_LINES and len(p["text"]) <= 100, p["text"])
    check("VOTE button with the real count, opening the private chat with the bot",
          b[0].text == "🚀 VOTE · 5 rockets" and b[0].url == "https://t.me/IWRUcatBot?start=rocket", b)
    check("under it, the 📊 Statistics button", len(b) == 2 and b[1].text == "📊 Statistics"
          and b[1].callback_data == "rv:stats" and len(p["kb"].inline_keyboard) == 2, b)
    check("the slot is saved (a restart never posts it twice)", db.get_config("rv_slot_last") == "2026-10-24:3")
    first_id = p["id"]
    counter["n"] = 6  # someone voted on the website, outside the bot
    await rb.post_job(ctx("2026-10-24:4"))
    p = last_to(GROUP)
    check("the next post deletes the previous one", (GROUP, first_id) in bot.deleted, bot.deleted)
    check("and its button shows the new real count", buttons(p)[0].text == "🚀 VOTE · 6 rockets", buttons(p))
    texts = []
    rb._bags.clear()
    for i in range(len(rv.ROCKET_LINES)):
        await rb.post_job(ctx(f"2026-10-24:x{i}"))
        texts.append(last_to(GROUP)["text"])
    check("the cat line changes: every line once before any repeats", sorted(texts) == sorted(rv.ROCKET_LINES))
    saved = rb.read_count

    async def no_read():
        return None
    rb.read_count = no_read
    await rb.post_job(ctx("2026-10-24:nodata"))
    check("Dexscreener can't be read: the button says just VOTE, never a made-up number",
          buttons(last_to(GROUP))[0].text == "🚀 VOTE", buttons(last_to(GROUP)))
    rb.read_count = saved
    await rb.post_job(ctx("2026-10-24:back"))

    print("sticker (Fernando's transparent image)")
    from PIL import Image, ImageDraw
    import make_rocket_sticker
    png = os.path.join(TMP, "rocket.png")
    im = Image.new("RGBA", (900, 700), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((200, 100, 700, 600), fill=(255, 120, 0, 255))
    im.save(png)
    rb.STICKER = make_rocket_sticker.make(png, os.path.join(TMP, "rocket_vote.webp"))
    w = Image.open(rb.STICKER)
    check("the converter keeps the transparency and makes it 512 px", w.mode == "RGBA" and max(w.size) == 512
          and w.getpixel((0, 0))[3] == 0 and os.path.getsize(rb.STICKER) <= 512 * 1024, (w.mode, w.size))
    before = len(bot.sent)
    await rb.post_job(ctx("2026-10-24:5"))
    new_msgs = bot.sent[before:]
    check("with the image: the sticker, then the text with the VOTE button right under it",
          [x["text"] for x in new_msgs][0] == "<sticker>" and buttons(new_msgs[1])[0].text.startswith("🚀 VOTE"), new_msgs)
    ids = [x["id"] for x in new_msgs]
    await rb.post_job(ctx("2026-10-24:6"))
    check("the next post deletes both (sticker and text)", all((GROUP, i) in bot.deleted for i in ids), (ids, bot.deleted[-3:]))

    print("one vote, start to code")
    await start(ana)
    m = last_to(1)
    b = buttons(m)
    check("private chat: the turn, the two buttons and the account warning",
          "your turn" in m["text"] and "IMPORTANT: you need an account at https://iwillrugu.com/paradox/#/" in m["text"]
          and [x.text for x in b] == ["🚀 Open Dexscreener", "✅ I voted"] and b[0].url == rv.DEXSCREENER_LINK
          and b[1].callback_data == "rv:voted", m)
    await voted(ana)  # tapped before voting
    check("“I voted” says the bot is checking", "checking the rocket counter" in last_to(1)["text"])
    await run_jobs(clock.t + 30)
    check("no new rocket yet: still checking, no code", not any("IWRU-" in x["text"] for x in bot.sent if x["chat"] == 1))
    counter["n"] = 7  # ana's rocket lands, Dexscreener shows it 40 s later
    await run_jobs(clock.t + 15)
    paid = last_to(1)["text"]
    code = paid.split("\n\n")[1] if "IWRU-" in paid else ""
    check("the rocket lands: a code for 20 Essence + the redeem link", "20 Essence" in paid and code.startswith("IWRU-5")
          and f"{cfg.PARADOX_REDEEM_URL}?code={code}" in paid, paid)
    check("the code is genuine (server's signature) and of the Essence kind",
          prize_code.verifies(cfg.CATCH_PRIZE_SECRET, code) and prize_code._normalize(code)[0] == "5", code)
    shout = last_to(GROUP)["text"]
    check("a public shout-out with the real rocket number", "ana" in shout and "#7" in shout and "+20 Essence" in shout, shout)
    await start(ana)
    check("tapping VOTE again resends the SAME code, never a second", code in last_to(1)["text"]
          and "already launched" in last_to(1)["text"])
    post = [m for m in bot.sent if m["chat"] == GROUP and m["kb"]][-1]
    check("the post's VOTE button now shows 7 (edited, not reposted)", buttons(post)[0].text == "🚀 VOTE · 7 rockets"
          and bot.edits and bot.edits[-1][1] == post["id"], buttons(post))
    n_edits = len(bot.edits)
    await rb.poll_job(ctx())
    check("no change in the count: no extra edit", len(bot.edits) == n_edits, bot.edits[n_edits:])
    pop = await stats(bob)
    check("📊 Statistics: a pop-up (only for whoever tapped) with this month's voters", pop["show_alert"] is True
          and pop["text"].startswith("📊 ") and "1 rocket" in pop["text"] and "1. @ana 🚀1" in pop["text"], pop)
    check("/rockets shows the real voter", "@ana" in await ranking_text() and "total: 1 rockets · 20 Essence" in await ranking_text())

    print("the bot spots the rocket by itself")
    hal = user(8, "hal")
    bot.members[8] = "member"
    await start(hal)
    check("hal's turn: the text says the bot spots the rocket by itself", "spot your rocket by myself" in last_to(8)["text"]
          and "your turn lasts 2 minutes" in last_to(8)["text"], last_to(8)["text"])
    shouts_before = sum("hal" in x["text"] for x in bot.sent if x["chat"] == GROUP)
    await rb.turn_watch_job(ctx())
    check("no rocket yet: the watch sends nothing", "IWRU-" not in last_to(8)["text"])
    counter["n"] += 1                                       # hal votes and never taps "I voted"
    await rb.turn_watch_job(ctx())
    check("the watch pays without 'I voted' and ends the turn", "IWRU-5" in last_to(8)["text"]
          and rb.ledger().holder(clock.t) is None, last_to(8)["text"])
    codes_to_hal = sum("IWRU-5" in x["text"] for x in bot.sent if x["chat"] == 8)
    await voted(hal)                                        # taps "I voted" afterwards anyway
    await run_jobs(clock.t + 5)
    await rb.turn_watch_job(ctx())
    check("tapping 'I voted' after the watch paid: no second code, no second shout-out",
          sum("IWRU-5" in x["text"] for x in bot.sent if x["chat"] == 8) == codes_to_hal
          and sum("hal" in x["text"] for x in bot.sent if x["chat"] == GROUP) == shouts_before + 1)

    print("two at once")
    await start(bob)
    await start(cid)
    check("bob gets the turn, cid waits (#1)", "your turn" in last_to(2)["text"] and "#1 in line" in last_to(3)["text"],
          (last_to(2)["text"][:40], last_to(3)["text"]))
    counter["n"] += 1  # bob votes
    await voted(cid)  # cid taps "I voted" on an old message: not his turn
    await run_jobs(clock.t + 1)
    check("cid can't take bob's rocket (he's told he's still in line)", "#1 in line" in last_to(3)["text"]
          and "IWRU-" not in last_to(3)["text"], last_to(3))
    await voted(bob)
    await run_jobs(clock.t + 1)
    check("bob is paid", "IWRU-5" in last_to(2)["text"])
    check("and cid is called right away with his own turn", "your turn" in last_to(3)["text"], last_to(3)["text"])
    await voted(cid)
    await run_jobs(clock.t + rv.CLAIM_WAIT_S + 20)  # cid never actually votes
    check("cid never voted: after 2 minutes of re-reading, no code", "can't see your rocket yet" in last_to(3)["text"]
          and "IWRU-" not in last_to(3)["text"], last_to(3)["text"])

    print("rules")
    await start(out)
    check("not in the group: no turn", "join the IWRU group first" in last_to(9)["text"])
    counter["n"] += 22  # a bot farm or bad data
    await rb.poll_job(ctx())
    check("a big jump freezes payouts and tells the Owner", "payouts are frozen" in last_to(42)["text"], last_to(42))
    dave = user(4, "dave")
    bot.members[4] = "member"
    await start(dave)
    check("while frozen nobody gets a turn", "paused" in last_to(4)["text"])
    replies = []

    async def reply(t):
        replies.append(t)
    await rb.cmd_unfreeze(types.SimpleNamespace(effective_user=user(1, "ana"), message=types.SimpleNamespace(reply_text=reply)), ctx())
    check("only the Owner can unfreeze", replies == [] and rb.ledger().frozen())
    await rb.cmd_unfreeze(types.SimpleNamespace(effective_user=user(42, "owner"), message=types.SimpleNamespace(reply_text=reply)), ctx())
    clock.t += rv.LOCK_S + 1
    await start(dave)
    await voted(dave)
    await run_jobs(clock.t + 15)
    check("after unfreezing, the jump itself paid nobody", "IWRU-" not in last_to(4)["text"])
    counter["n"] += 1
    await run_jobs(clock.t + 15)
    check("and a new real rocket pays again", "IWRU-5" in last_to(4)["text"], last_to(4)["text"])
    print("someone in line blocked the bot")
    eve, fay, gus = user(5, "eve"), user(6, "fay"), user(7, "gus")
    bot.members.update({5: "member", 6: "member", 7: "member"})
    clock.t += rv.MAX_TURN_S + 1
    await start(eve)
    await start(fay)
    await start(gus)
    bot.blocked.add(6)                                      # fay blocks the bot while waiting
    rb.ledger().release(5)                                  # eve's turn ends
    await rb.poll_job(ctx())
    check("fay can't be reached: her turn is released at once and gus is called",
          rb.ledger().holder(clock.t) == (7, "@gus") and "your turn" in last_to(7)["text"], rb.ledger().holder(clock.t))
    text = await ranking_text()
    counter["n"] += 1                                       # a vote on the website: the button follows within a poll
    await rb.poll_job(ctx())
    post = [m for m in bot.sent if m["chat"] == GROUP and m["kb"]][-1]
    check("a vote outside the bot also shows on the button after the next poll",
          buttons(post)[0].text == f"🚀 VOTE · {counter['n']} rockets", buttons(post))
    pop = (await stats(ana))["text"]
    check("Statistics: the 4 paid people of this month, in the order they voted", pop.count("🚀1") == 4
          and "4 rockets" in pop and pop.index("@ana") < pop.index("@hal") < pop.index("@bob"), pop)
    clock.t = rv.month_start(clock.t) + 40 * 86400         # into next month
    clock.t = rv.month_start(clock.t) + 60                  # 1 minute past day 1, 00:00 Madrid
    check("on day 1 the statistics start again from zero", "no rockets yet" in (await stats(ana))["text"],
          (await stats(ana))["text"])
    check("/rockets: exactly the 4 people paid", text.count("· +20 Essence") == 4 and "total: 4 rockets · 80 Essence" in text, text)


    print("next day: vote again")
    clock.t = rv.month_start(clock.t) + 3 * 86400 + 10 * 3600           # day 4 of the month, 10:00 Madrid
    await start(ana)
    check("a new day: ana gets a new turn", "your turn" in last_to(1)["text"], last_to(1)["text"])
    counter["n"] += 1
    await rb.turn_watch_job(ctx())
    codes = [x["text"].split("\n\n")[1] for x in bot.sent if x["chat"] == 1 and "20 Essence:" in x["text"]]
    check("and a second, different code", len(codes) == 2 and codes[0] != codes[1], codes)
    await start(ana)
    check("tapping VOTE again the same day: today's code and 'come back tomorrow'", codes[1] in last_to(1)["text"]
          and "tomorrow" in last_to(1)["text"], last_to(1)["text"])
    check("Statistics count her votes of this month", "@ana 🚀1" in (await stats(ana))["text"])

    print("writing vote in the chat")

    async def say(chat_id, kind, text):
        upd = types.SimpleNamespace(effective_message=types.SimpleNamespace(text=text),
                                    effective_chat=types.SimpleNamespace(id=chat_id, type=kind))
        before = len(bot.sent)
        await rb.on_vote_word(upd, ctx())
        return bot.sent[before:]
    rb._vote_word_last.clear()
    old = db.get_config("rv_last_msg").split(",")
    out = await say(GROUP, "supergroup", "vote")
    check("'vote' in the group: the whole post (sticker, line, VOTE · count, Statistics)",
          [m["text"] for m in out][0] == "<sticker>" and out[1]["text"] in rv.ROCKET_LINES
          and [b.text for b in buttons(out[1])] == [f"🚀 VOTE · {counter['n']} rockets", "📊 Statistics"], out)
    check("and it replaces the previous post: one block in the chat", all((GROUP, int(i)) in bot.deleted for i in old)
          and db.get_config("rv_post_msg") == str(out[1]["id"]), old)
    check("written again right away: no second post (60 s)", await say(GROUP, "supergroup", "VOTE!") == [])
    clock.t += rb.VOTE_WORD_COOLDOWN_S + 1
    check("a minute later it works again, also as /vote@bot", len(await say(GROUP, "supergroup", "/vote@IWRUcatBot")) == 2)
    clock.t += rb.VOTE_WORD_COOLDOWN_S + 1
    check("a sentence with the word does nothing", await say(GROUP, "supergroup", "I'll vote later") == [])
    dm = await say(77, "private", "vote")
    check("in a private chat too (and that one doesn't replace the group post)", len(dm) == 2
          and db.get_config("rv_post_msg") != str(dm[1]["id"]))
    check("another group the bot is in: nothing", await say(-100999, "group", "vote") == [])


asyncio.run(main())
print(f"rocket bot: {ok} ok, {fail} failed")
sys.exit(1 if fail else 0)
