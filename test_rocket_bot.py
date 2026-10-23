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
        self.sent, self.deleted, self.members, self.n, self.blocked = [], [], {}, 100, set()

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
    check("the post has a cat line, the real count and how to vote",
          "🚀 rockets on Dexscreener right now: 5" in p["text"] and rv.HOW_LINE in p["text"], p["text"])
    check("one 🚀 VOTE button that opens the private chat with the bot",
          len(b) == 1 and b[0].text == "🚀 VOTE" and b[0].url == "https://t.me/IWRUcatBot?start=rocket", b)
    check("the slot is saved (a restart never posts it twice)", db.get_config("rv_slot_last") == "2026-10-24:3")
    first_id = p["id"]
    counter["n"] = 6  # someone voted on the website, outside the bot
    await rb.post_job(ctx("2026-10-24:4"))
    p = last_to(GROUP)
    check("the next post deletes the previous one", (GROUP, first_id) in bot.deleted, bot.deleted)
    check("and shows the real rise since the last post", "right now: 6 (+1 since my last nap" in p["text"], p["text"])

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
          [x["text"] for x in new_msgs][0] == "<sticker>" and buttons(new_msgs[1])[0].text == "🚀 VOTE", new_msgs)
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
    check("the rocket lands: a code for 150 Essence + the redeem link", "150 Essence" in paid and code.startswith("IWRU-5")
          and f"{cfg.PARADOX_REDEEM_URL}?code={code}" in paid, paid)
    check("the code is genuine (server's signature) and of the Essence kind",
          prize_code.verifies(cfg.CATCH_PRIZE_SECRET, code) and prize_code._normalize(code)[0] == "5", code)
    shout = last_to(GROUP)["text"]
    check("a public shout-out with the real rocket number", "ana" in shout and "#7" in shout and "+150 Essence" in shout, shout)
    await start(ana)
    check("tapping VOTE again resends the SAME code, never a second", code in last_to(1)["text"]
          and "already launched" in last_to(1)["text"])
    check("/rockets shows the real voter", "@ana" in await ranking_text() and "total: 1 rockets · 150 Essence" in await ranking_text())

    print("two at once")
    await start(bob)
    await start(cid)
    check("bob gets the turn, cid waits (#1)", "your turn" in last_to(2)["text"] and "#1 in line" in last_to(3)["text"],
          (last_to(2)["text"][:40], last_to(3)["text"]))
    counter["n"] = 8  # bob votes
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
    counter["n"] = 30  # a bot farm or bad data
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
    counter["n"] = 31
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
    check("/rockets: exactly the 3 people paid", text.count("· +150 Essence") == 3 and "total: 3 rockets · 450 Essence" in text, text)


asyncio.run(main())
print(f"rocket bot: {ok} ok, {fail} failed")
sys.exit(1 if fail else 0)
