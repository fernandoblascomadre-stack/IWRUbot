"""Checks the daily IWRU PARADOX X post (paradox_cards.py + paradox_post_lines.py) without touching X or events.db.
Run: python test_paradox_cards.py"""
import json
import random
from datetime import datetime, timedelta, timezone

import paradox_cards as pc
import paradox_post_lines as L

fails = 0


def check(label, ok):
    global fails
    print(("  ok  " if ok else "FAIL  ") + label)
    fails += 0 if ok else 1


class FakeDb:
    def __init__(self):
        self.d = {}

    def get_config(self, k):
        return self.d.get(k)

    def set_config(self, k, v):
        self.d[k] = v


info = pc.card_info()
files = pc.card_files()
check("every card in the game has a combat line", set(info) == set(L.UTILITY))
check("every image maps to a known card", all(pc.card_id(f) in info for f in files))
named = [c for x in L.COMBOS for c in x["cards"]] + [x["card"] for x in L.BATTLES + L.JOKES]
check("every combo/battle/joke card has an image", all(f"{c}.webp" in files for c in named) and L.INTRO_CARD in files)

# every card fits with at least its shortest hook + question (the post picks a hook that fits)
worst = max(pc.tweet_len(pc.build_card_text(f, info, min(L.HOOKS, key=len), min(L.QUESTIONS, key=len))) for f in files)
check(f"every card can fit in 280 (tightest {worst})", worst <= pc.TWEET_LIMIT)
worst = max(pc.tweet_len(pc.build_combo_text(c, q)) for c in L.COMBOS for q in pc.COMBO_QUESTIONS)
check(f"combo posts fit in 280 (worst {worst})", worst <= pc.TWEET_LIMIT)
worst = max(pc.tweet_len(pc.build_battle_text(b, q)) for b in L.BATTLES for q in pc.BATTLE_QUESTIONS)
check(f"battle posts fit in 280 (worst {worst})", worst <= pc.TWEET_LIMIT)
worst = max(pc.tweet_len(pc.build_joke_text(j)) for j in L.JOKES)
check(f"joke posts fit in 280 (worst {worst})", worst <= pc.TWEET_LIMIT)
worst = max(pc.tweet_len(pc.build_reply_text(o)) for o in L.REPLY_OPENERS)
check(f"replies fit in 280 (worst {worst})", worst <= pc.TWEET_LIMIT)
check(f"intro fits ({pc.tweet_len(L.INTRO_TEXT)} / reply {pc.tweet_len(L.INTRO_REPLY)})",
      pc.tweet_len(L.INTRO_TEXT) <= 280 and pc.tweet_len(L.INTRO_REPLY) <= 280)
check("every card post mentions @opensea and @monad", "@opensea" in L.CALL and "@monad" in L.CALL)
check("replies carry OpenSea and the game link", pc.OPENSEA_URL in pc.build_reply_text("x") and "https://iwillrugu.com/paradox/#/" in pc.build_reply_text("x"))

# rotation over a simulated year
pc.db = FakeDb()
kinds, cards, last_text = [], [], None
for day in range(400):
    kind, text, imgs, reply = pc.next_post()
    kinds.append(kind)
    if pc.tweet_len(text) > pc.TWEET_LIMIT or pc.tweet_len(reply) > pc.TWEET_LIMIT:
        check(f"day {day} {kind} post fits in 280 ({pc.tweet_len(text)})", False)
    if kind == "card":
        cards.append(imgs[0])
    if day == 0:
        check("first post is Fernando's intro, verbatim", kind == "intro" and text == L.INTRO_TEXT and reply == L.INTRO_REPLY)
    if text == last_text:
        check(f"no identical posts on consecutive days (day {day})", False)
    last_text = text
check("rotation is card/combo/card/battle/card/joke", kinds[1:7] == pc.ROTATION)
commons = [info[pc.card_id(c)]["rarity"] == "common" and "-mythic" not in c for c in cards]
check("card posts alternate common and powerful", all(commons[i] != commons[i + 1] for i in range(len(commons) - 1)))
pools = pc.card_pools()
check("no card repeats before its whole pool has been posted",
      len(set(cards[0::2][:len(pools["common"])])) == min(len(pools["common"]), len(cards[0::2])))

# timing: always inside 17:30-19:00 Madrid
ok = True
for _ in range(2000):
    now = datetime(2026, random.choice([3, 10, 12]), random.randint(1, 28), random.randint(0, 23), random.randint(0, 59),
                   tzinfo=pc.MADRID_TZ)
    at = (now.astimezone(timezone.utc) + timedelta(seconds=pc.seconds_until_post(now=now, posted_today=random.random() < .3))).astimezone(pc.MADRID_TZ)
    if not ((17, 30) <= (at.hour, at.minute) <= (19, 0)) or at < now:
        ok = False
        print("   bad:", now, "->", at)
        break
check("every scheduled post lands inside 17:30-19:00 Spain time (DST included)", ok)
now = datetime(2026, 10, 18, 18, 0, tzinfo=pc.MADRID_TZ)
at = now + timedelta(seconds=pc.seconds_until_post(now=now, posted_today=True))
check("already posted today -> tomorrow", at.date().isoformat() == "2026-10-19")

print("ALL OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)
