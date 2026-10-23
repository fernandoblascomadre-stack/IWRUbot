"""Dexscreener 🚀 votes (2026-10-23, draft): the reward rules in rocket_votes.RocketLedger, the 8-a-day schedule and the
cat lines. The ledger gets the clock and the counter as arguments, so every case here plays a real sequence of reads.
Run: python -m pytest test_rocket_votes.py -q"""
import os
import random
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import rocket_votes as rv

T0 = 1_800_000_000.0


def ledger(conn=None):
    n = iter(range(1, 10**6))
    return rv.RocketLedger(conn or sqlite3.connect(":memory:"), lambda tg: f"CODE-{tg}-{next(n)}")


def test_one_vote_pays_one_code_once():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(1, "ana", T0, 5)[0] == "turn"
    assert L.claim(1, T0 + 10, 5) == ("not_yet",)          # tapped "I voted" before voting
    st, code = L.claim(1, T0 + 20, 6)                       # the rocket landed
    assert st == "paid" and code.startswith("CODE-1-")
    assert L.start(1, "ana", T0 + 30, 6) == ("paid", code)  # same code again, never a second one
    assert L.claim(1, T0 + 40, 7) == ("paid", code)
    assert len(L.ranking()) == 1 and L.record() == 6


def test_two_people_at_once_never_take_each_others_rocket():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(1, "ana", T0, 5)[0] == "turn"
    assert L.start(2, "bob", T0 + 1, 5) == ("wait", 1)      # bob waits for ana's turn
    assert L.start(3, "cid", T0 + 2, 5) == ("wait", 2)
    assert L.claim(2, T0 + 5, 6) == ("no_turn",)            # bob can't claim the rocket that landed in ana's turn
    assert L.claim(1, T0 + 6, 6)[0] == "paid"
    assert L.next_in_queue(T0 + 7) == (2, "bob")            # the bot calls bob
    assert L.start(3, "cid", T0 + 8, 6) == ("wait", 2)      # cid can't jump the queue
    assert L.start(2, "bob", T0 + 9, 6)[0] == "turn"
    assert L.claim(2, T0 + 10, 6) == ("not_yet",)           # ana's rocket is not bob's
    assert L.claim(2, T0 + 20, 7)[0] == "paid"
    assert L.next_in_queue(T0 + 21) == (3, "cid")


def test_removing_and_putting_back_a_rocket_never_pays():
    L = ledger()
    L.observe(T0, 5)                                        # dan voted long ago, before the bot
    L.observe(T0 + 60, 4)                                   # dan removes his rocket
    assert L.start(4, "dan", T0 + 120, 4)[0] == "turn"
    assert L.claim(4, T0 + 130, 5) == ("not_yet",)          # puts it back: 5 is not a new record
    L.observe(T0 + 140, 4)                                  # removes it again inside his own turn
    assert L.claim(4, T0 + 150, 5) == ("not_yet",)
    assert L.record() == 5 and L.ranking() == []


def test_a_vote_made_outside_the_bot_pays_nobody():
    L = ledger()
    L.observe(T0, 5)
    L.observe(T0 + 60, 6)                                   # someone voted on the website, no turn open
    assert L.record() == 6
    assert L.start(5, "eva", T0 + 120, 6)[0] == "turn"
    assert L.claim(5, T0 + 130, 6) == ("not_yet",)          # eva can't cash that one


def test_rocket_between_tap_and_turn_start_is_not_yours():
    L = ledger()
    L.observe(T0, 5)
    L.observe(T0 + 1, 6)                                    # landed before eva's turn started
    assert L.start(5, "eva", T0 + 2, 6)[0] == "turn"
    assert L.claim(5, T0 + 3, 6) == ("not_yet",)


def test_no_data_never_pays():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(1, "ana", T0, None) == ("no_data",)
    assert L.start(1, "ana", T0 + 1, 5)[0] == "turn"
    assert L.claim(1, T0 + 2, None) == ("no_data",)
    assert L.ranking() == []


def test_a_big_jump_freezes_payouts_until_the_owner_looks():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(1, "ana", T0, 5)[0] == "turn"
    assert L.claim(1, T0 + 5, 5 + rv.JUMP_FREEZE + 1) == ("frozen",)
    assert L.start(2, "bob", T0 + 6, 17) == ("frozen",)
    assert L.ranking() == []
    L.unfreeze()
    assert L.claim(1, T0 + 7, 17) == ("no_turn",)           # the jump ended ana's turn: it never pays
    assert L.record() == 17 and L.ranking() == []
    assert L.start(1, "ana", T0 + 8, 17)[0] == "turn"       # she can start again; only a new record pays
    assert L.claim(1, T0 + 9, 17) == ("not_yet",)
    assert L.claim(1, T0 + 10, 18)[0] == "paid"


def test_jump_after_unfreeze_is_absorbed_not_paid():
    L = ledger()
    L.observe(T0, 5)
    L.observe(T0 + 60, 30)                                  # frozen, no turn open
    L.unfreeze()
    L.observe(T0 + 120, 30)                                 # the Owner unfroze: 30 becomes the record
    assert L.record() == 30
    assert L.start(1, "ana", T0 + 130, 30)[0] == "turn"
    assert L.claim(1, T0 + 140, 30) == ("not_yet",)
    assert L.claim(1, T0 + 150, 31)[0] == "paid"


def test_expired_turn_frees_the_queue_and_cannot_be_claimed():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(1, "ana", T0, 5)[0] == "turn"
    assert L.start(2, "bob", T0 + 1, 5) == ("wait", 1)
    t = T0 + rv.LOCK_S + 1
    assert L.next_in_queue(t) == (2, "bob")
    assert L.claim(1, t, 6) == ("no_turn",)
    assert L.start(2, "bob", t + 1, 6)[0] == "turn"         # the late rocket became the record at bob's start
    assert L.claim(2, t + 2, 6) == ("not_yet",)


def test_dexscreener_lag_keeps_the_turn_while_rereading():
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    t = T0 + rv.LOCK_S - 5                                  # "I voted" 5 s before the turn ends
    L.extend_for_claim(1, t)
    assert L.claim(1, t, 5) == ("not_yet",)
    assert L.claim(1, t + rv.CLAIM_WAIT_S - 1, 6)[0] == "paid"   # Dexscreener showed it 2 min later


def test_two_rockets_in_one_turn_pay_one_code():
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    assert L.claim(1, T0 + 5, 7)[0] == "paid"
    assert L.record() == 7 and len(L.ranking()) == 1


def test_only_group_members():
    L = ledger()
    L.observe(T0, 5)
    assert L.start(9, "out", T0, 5, is_member=False) == ("not_member",)


def test_state_survives_a_restart():
    path = os.path.join(tempfile.mkdtemp(), "rv.db")
    L = ledger(sqlite3.connect(path))
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    L.start(2, "bob", T0 + 1, 5)
    L.c.close()
    L2 = ledger(sqlite3.connect(path))                      # Render restart
    assert L2.holder(T0 + 2) == (1, "ana") and L2.record() == 5
    assert L2.claim(2, T0 + 3, 6) == ("no_turn",)
    st, code = L2.claim(1, T0 + 4, 6)
    assert st == "paid"
    L2.c.close()
    L3 = ledger(sqlite3.connect(path))
    assert L3.start(1, "ana", T0 + 5, 6) == ("paid", code)
    assert L3.next_in_queue(T0 + 6) == (2, "bob")


def test_random_simulation_never_pays_more_than_real_new_records():
    """Thousands of mixed actions: real votes through the bot, votes outside it, removals and re-adds, fake "I voted",
    read failures, expired turns. Invariants: paid codes <= rockets above the starting record that were ever reached;
    nobody paid twice; every payout's level is a strictly new record."""
    rnd = random.Random(1234)
    for _round in range(30):
        L = ledger()
        count, now = 5, T0
        L.observe(now, count)
        start_record = count
        voted = set()        # users whose rocket is on Dexscreener now
        levels = []
        for _ in range(600):
            now += rnd.choice([1, 5, 20, 60, 200])
            user = rnd.randint(1, 25)
            act = rnd.random()
            read = None if rnd.random() < 0.05 else count
            if act < 0.30:
                L.start(user, f"u{user}", now, read)
            elif act < 0.55:                       # the user votes (only once per person on Dexscreener)
                if user not in voted:
                    voted.add(user)
                    count += 1
            elif act < 0.65:                       # removes their rocket
                if user in voted:
                    voted.discard(user)
                    count -= 1
            elif act < 0.95:                       # "I voted" (true or not)
                was_holder = L.holder(now)
                st = L.claim(user, now, read)
                if st[0] == "paid" and was_holder and was_holder[0] == user:
                    levels.append(L.record())
            else:
                L.observe(now, read)
        paid = L.ranking()
        names = [p[0] for p in paid]
        assert len(names) == len(set(names))
        assert levels == sorted(set(levels))                   # each payout a strictly higher record
        assert len(paid) <= max([start_record] + levels) - start_record
        rows = L.c.execute("SELECT level FROM rv_votes").fetchall()
        assert all(r[0] > start_record for r in rows)


def test_schedule_eight_a_day_spread_and_restart_safe():
    random.seed(7)
    now = datetime(2026, 10, 24, 0, 0, tzinfo=rv.MADRID_TZ)
    last, times = None, []
    t = now
    for _ in range(16):                                      # two days
        delay, key = rv.next_post(last, t)
        t = (t.astimezone(timezone.utc) + timedelta(seconds=delay)).astimezone(rv.MADRID_TZ)
        times.append(t)
        last = key
    by_day = {}
    for x in times:
        by_day.setdefault(x.astimezone(rv.MADRID_TZ).date(), []).append(x)
    assert all(len(v) == 8 for v in by_day.values()), {k: len(v) for k, v in by_day.items()}
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert min(gaps) >= 3600 and max(gaps) <= 5 * 3600 + 60
    # restart in the middle of a slot that was already posted: never the same slot again
    delay, key = rv.next_post(last, t + timedelta(minutes=1))
    assert key > last


def test_restart_after_a_missed_slot_posts_soon():
    now = datetime(2026, 10, 24, 11, 45, tzinfo=rv.MADRID_TZ)  # slot 3 (09-12) not posted, past its latest point (11:30)
    delay, key = rv.next_post("2026-10-24:2", now)
    assert key == "2026-10-24:3" and 60 <= delay <= 300


def test_post_text_uses_only_the_real_count():
    line = rv.ROCKET_LINES[0]
    assert "rockets" not in rv.compose(line, None, 5).split("\n\n")[1]   # no read -> no number at all
    assert rv.compose(line, 7, 5).split("\n\n")[1] == "🚀 rockets on Dexscreener right now: 7 (+2 since my last nap 🐾)"
    assert rv.compose(line, 7, 7).split("\n\n")[1] == "🚀 rockets on Dexscreener right now: 7"
    assert rv.compose(line, 7, None).split("\n\n")[1] == "🚀 rockets on Dexscreener right now: 7"


def test_cat_lines_policy():
    lines = rv.ROCKET_LINES
    assert len(lines) >= 60 and len(set(lines)) == len(lines)
    for line in lines:
        assert re.search(r"🚀|rocket|vot", line) and len(line) <= 100, line   # short and punchy (Fernando)
        assert not re.search(r"\bbuy\b|price|moon|100x|profit|guarantee|pump", line, re.I), line
    assert len(rv.SHOUTOUT_LINES) >= 5
    for line in rv.SHOUTOUT_LINES:
        assert "{name}" in line and "{n}" in line and "+150 Essence" in line, line


def test_schedule_many_seeds_across_both_dst_changes():
    """Found 2026-10-23: on the spring DST night slot 0 lasts 2 real hours and two posts landed 48 min apart."""
    for start_day in (datetime(2026, 3, 28, tzinfo=rv.MADRID_TZ), datetime(2026, 10, 24, tzinfo=rv.MADRID_TZ)):
        for seed in range(300):
            random.seed(seed)
            t, last, times = start_day, None, []
            for _ in range(24):
                d, k = rv.next_post(last, t)
                t = (t.astimezone(timezone.utc) + timedelta(seconds=d)).astimezone(rv.MADRID_TZ)  # real time, not wall-clock
                times.append(t)
                last = k
            per_day = {}
            for x in times:
                per_day[x.astimezone(rv.MADRID_TZ).date()] = per_day.get(x.astimezone(rv.MADRID_TZ).date(), 0) + 1
            gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
            assert all(v == 8 for v in per_day.values()), (start_day, seed, per_day)
            assert min(gaps) >= 3600, (start_day, seed, min(gaps))


def test_a_glitch_read_of_zero_neither_pays_nor_freezes():
    """Dexscreener answers 0 (not an error) for a pair it doesn't know (checked live 2026-10-23)."""
    L = ledger()
    L.observe(T0, 40)
    L.observe(T0 + 60, 0)                                   # glitch
    assert not L.frozen() and L.record() == 40
    L.observe(T0 + 120, 40)                                 # back to the real count: not a jump
    assert not L.frozen()
    assert L.start(1, "ana", T0 + 130, 0)[0] == "turn"      # a glitch read at the start of a turn
    assert L.claim(1, T0 + 140, 40) == ("not_yet",)         # back to 40 is not a new record
    assert L.claim(1, T0 + 150, 41)[0] == "paid"


def test_essence_and_card_codes_never_mix():
    import prize_code
    s = "mix-test-secret"
    cards = [prize_code.new_code(s, r) for r in ("common", "rare", "epic", "legendary") for _ in range(500)]
    essence = [prize_code.new_essence_code(s) for _ in range(2000)]
    assert all(prize_code._normalize(c)[0] in "1234" for c in cards)
    assert all(prize_code._normalize(c)[0] == "5" for c in essence)
    assert all(prize_code.verifies(s, c) for c in cards + essence)
    assert not any(prize_code.verifies("other-secret", c) for c in essence[:50])
    assert len(set(essence)) == len(essence)


# ── review 2026-10-23 (second pass): attacks on the turn and the queue ──────────────────────────────────────────────
def test_tapping_i_voted_again_and_again_never_holds_the_turn_forever():
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    t = T0
    for _ in range(50):                                     # taps "I voted" every 90 s
        t += 90
        L.extend_for_claim(1, t)
    assert L.holder(T0 + rv.MAX_TURN_S - 1) is None or L.holder(T0 + rv.MAX_TURN_S) is None
    assert L.holder(T0 + rv.MAX_TURN_S + 1) is None
    assert L.start(2, "bob", T0 + rv.MAX_TURN_S + 2, 5)[0] == "turn"


def test_a_place_in_line_expires_unless_the_person_is_still_there():
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    assert L.start(2, "bob", T0 + 1, 5) == ("wait", 1)
    assert L.start(3, "cid", T0 + 2, 5) == ("wait", 2)
    assert L.start(3, "cid", T0 + 20 * 60, 5) == ("wait", 2)        # cid says "still here", keeps place 2
    t = T0 + rv.QUEUE_TTL_S + 60                                     # bob is gone
    assert L.next_in_queue(t) == (3, "cid")
    assert L.queue_position(2) is None


def test_release_frees_the_turn_for_the_next():
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    L.start(2, "bob", T0 + 1, 5)
    L.release(2)                                            # not the holder: nothing happens
    assert L.holder(T0 + 2) == (1, "ana")
    L.release(1)
    assert L.holder(T0 + 3) is None and L.next_in_queue(T0 + 3) == (2, "bob")


def test_the_texts_tell_people_to_wait_for_their_turn():
    import rocket_bot as rb
    assert "tap VOTE first" in rv.HOW_LINE and "open the page and tap" not in rv.HOW_LINE
    assert "DON'T vote on Dexscreener yet" in rb.DM_WAIT


def test_voting_while_waiting_in_line_pays_the_holder_never_twice():
    """The known limit, pinned: a rocket that lands in someone else's turn is theirs; the impatient voter can't be paid
    later for it (their vote is spent) -- which is why DM_WAIT says not to vote yet."""
    L = ledger()
    L.observe(T0, 5)
    L.start(1, "ana", T0, 5)
    L.start(2, "bob", T0 + 1, 5)
    assert L.claim(1, T0 + 5, 6)[0] == "paid"              # bob voted early: ana's turn pays ana
    assert L.start(2, "bob", T0 + 10, 6)[0] == "turn"
    assert L.claim(2, T0 + 20, 6) == ("not_yet",)          # bob has no second vote to give
    assert len(L.ranking()) == 1
