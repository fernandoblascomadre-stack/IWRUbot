"""Dexscreener 🚀 votes under real concurrency (review 2026-10-23): the bot's handlers and jobs interleave at every await
(Dexscreener read, Telegram send). Real asyncio tasks, real time with the turn shortened to seconds, a fake Telegram and
a counter that only moves when a simulated person votes.
- honest run: 30 people tap VOTE at once, each votes only in their own turn then taps "I voted" -> all 30 paid, once.
- chaos run: cheaters tapping "I voted" without voting, people removing rockets, votes outside the bot, failed reads ->
  never more codes than new records of the real counter, nobody paid twice, never two turns at once.
Run: python test_rocket_concurrency.py"""
import asyncio
import os
import random
import sys
import tempfile
import types

TMP = tempfile.mkdtemp()
os.environ.update({"EVENTS_DB_PATH": os.path.join(TMP, "e.db"), "EVENTS_CHAT_ID": "-1001111", "BOT_OWNER_ID": "42",
                   "CATCH_PRIZE_SECRET": "conc-secret", "ROCKET_VOTES_ENABLED": "1"})
import db  # noqa: E402
db.init_db()
import rocket_bot as rb  # noqa: E402
import rocket_votes as rv  # noqa: E402

rv.LOCK_S, rv.CLAIM_WAIT_S = 1.2, 0.9
rv.MAX_TURN_S = rv.LOCK_S + rv.CLAIM_WAIT_S
rb.CHECK_EVERY_S = 0.1
STATE = {"count": 5, "fail_reads": 0.0}


async def fake_read():
    await asyncio.sleep(random.uniform(0, 0.05))       # Dexscreener latency: other tasks run meanwhile
    if random.random() < STATE["fail_reads"]:
        return None
    return STATE["count"]


rb.read_count = fake_read


class FakeBot:
    username = "IWRUbot"

    def __init__(self):
        self.dms = {}

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        await asyncio.sleep(random.uniform(0, 0.02))
        self.dms.setdefault(chat_id, []).append(text)
        return types.SimpleNamespace(message_id=1)

    async def get_chat_member(self, chat_id, user_id):
        return types.SimpleNamespace(status="member")


class JQ:
    def run_once(self, cb, when, data=None, name=None):
        async def later():
            await asyncio.sleep(when)
            await cb(types.SimpleNamespace(bot=BOT, application=APP, job=types.SimpleNamespace(data=data)))
        asyncio.get_running_loop().create_task(later())


BOT = FakeBot()
APP = types.SimpleNamespace(job_queue=JQ())


def ctx():
    return types.SimpleNamespace(bot=BOT, application=APP, args=["rocket"], job=None)


def u(i):
    return types.SimpleNamespace(id=i, username=f"p{i}", first_name=f"p{i}")


async def tap_vote(i):
    await rb.on_start(types.SimpleNamespace(effective_user=u(i), effective_chat=types.SimpleNamespace(id=i)), ctx())


async def tap_voted(i):
    async def answer():
        return None
    await rb.on_voted(types.SimpleNamespace(callback_query=types.SimpleNamespace(from_user=u(i), answer=answer)), ctx())


async def poller(stop):
    async def watch():
        while not stop.is_set():
            await rb.turn_watch_job(ctx())                     # the bot's own watch, every CHECK_EVERY_S
            await asyncio.sleep(rb.CHECK_EVERY_S)
    w = asyncio.get_running_loop().create_task(watch())
    while not stop.is_set():
        await rb.poll_job(ctx())
        await asyncio.sleep(0.15)
    await w


def no_duplicates(paid_ids):
    """Each paid person got exactly one code DM and exactly one shout-out (watch + "I voted" can both see the rocket)."""
    shouts = BOT.dms.get(int(os.environ["EVENTS_CHAT_ID"]), [])
    return all(sum("20 Essence:" in m for m in BOT.dms.get(i, [])) == 1 for i in paid_ids) and len(shouts) == len(paid_ids)


def fresh():
    rb._ledger = None
    rb._checking.clear()
    BOT.dms.clear()
    db._conn.executescript("DROP TABLE IF EXISTS rv_state; DROP TABLE IF EXISTS rv_votes; DROP TABLE IF EXISTS rv_queue;")
    STATE.update(count=5, fail_reads=0.0)
    rb.ledger().observe(0, 5)


async def honest_person(i, voted):
    """Taps VOTE, waits for the bot to say it's their turn, votes once, taps I voted (again if told so)."""
    await asyncio.sleep(random.uniform(0, 0.3))
    await tap_vote(i)
    for _ in range(4000):
        msgs = BOT.dms.get(i, [])
        if any("20 Essence:" in m for m in msgs):
            return
        h = rb.ledger().holder(__import__("time").time())
        if h and h[0] == i and i not in voted:
            await asyncio.sleep(random.uniform(0.05, 0.4))   # opens Dexscreener, taps the rocket
            voted.add(i)
            STATE["count"] += 1
            await asyncio.sleep(random.uniform(0, 0.2))
            await tap_voted(i)
        elif h and h[0] == i and i in voted and i not in rb._checking:
            await tap_voted(i)
        elif not h and i not in voted and rb.ledger().queue_position(i) is None:
            await tap_vote(i)                                  # dropped (turn expired before voting): back in line
        await asyncio.sleep(0.05)


async def honest_run():
    fresh()
    stop = asyncio.Event()
    p = asyncio.create_task(poller(stop))
    voted = set()
    await asyncio.wait_for(asyncio.gather(*[honest_person(i, voted) for i in range(1, 31)]), timeout=400)
    stop.set()
    await p
    rows = rb.ledger().c.execute("SELECT tg_id, level FROM rv_votes").fetchall()
    ids = [r[0] for r in rows]
    ok = sorted(ids) == list(range(1, 31)) and len(set(r[1] for r in rows)) == 30 and STATE["count"] == 35 and no_duplicates(ids)
    print(("  ok  " if ok else "FAIL  ") + f"honest run: {len(ids)}/30 paid once each, levels 6..35 distinct, counter {STATE['count']}")
    return ok


async def chaos_person(i, voted, deadline):
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        r = random.random()
        if r < 0.3:
            await tap_vote(i)
        elif r < 0.55:
            await tap_voted(i)                                 # cheaters press it whether or not they voted
        elif r < 0.7 and i not in voted:
            voted.add(i)
            STATE["count"] += 1                                # votes, maybe not in their turn
        elif r < 0.75 and i in voted:
            voted.discard(i)
            STATE["count"] -= 1                                # removes the rocket (and may vote again later)
        await asyncio.sleep(random.uniform(0, 0.15))


async def chaos_run(seed):
    random.seed(seed)
    fresh()
    STATE["fail_reads"] = 0.05
    stop = asyncio.Event()
    p = asyncio.create_task(poller(stop))
    voted, peak = set(), [5]

    async def watch_peak():
        while not stop.is_set():
            peak[0] = max(peak[0], STATE["count"])
            # never two turns at once: the ledger has a single holder slot, check it is consistent
            await asyncio.sleep(0.01)
    w = asyncio.create_task(watch_peak())
    deadline = asyncio.get_running_loop().time() + 8
    await asyncio.gather(*[chaos_person(i, voted, deadline) for i in range(1, 26)])
    await asyncio.sleep(rv.MAX_TURN_S + 0.5)
    stop.set()
    await p
    await w
    rows = rb.ledger().c.execute("SELECT tg_id, level FROM rv_votes ORDER BY ts").fetchall()
    ids = [r[0] for r in rows]
    levels = [r[1] for r in rows]
    had_turn = {i for i, msgs in BOT.dms.items() if any("your turn to launch" in m for m in msgs)}
    ok = (len(ids) == len(set(ids)) and levels == sorted(set(levels)) and len(rows) <= peak[0] - 5
          and all(lv <= peak[0] for lv in levels) and set(ids) <= had_turn and no_duplicates(ids))
    print(("  ok  " if ok else "FAIL  ") + f"chaos run seed {seed}: {len(rows)} paid, real new records {peak[0] - 5}, "
          f"no double pay/DM/shout-out, rising levels, only people who had a turn")
    return ok


async def main():
    results = [await honest_run()]
    for seed in range(5):
        results.append(await chaos_run(seed))
    return all(results)


ok = asyncio.run(main())
print("concurrency:", "ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
