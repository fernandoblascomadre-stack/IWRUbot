"""cat_posts.py -- twice-a-day cat posts in the Telegram groups (2026-10-15).

Fernando asked for two of them, with the same rules: at least 20 different funny cat lines, a random moment twice a
day, at least 10 hours between the two posts, always in English. Same phrase policy as every other pool: funny/chaotic,
the joke is the cat's own greed and chaos, never a price promise or "buy now".

- DEXSCREENER_LINES: $IWRU is on Dexscreener ("look at the chart, I swear I saw fish in the graph"); + DEXSCREENER_LINK.
  Each one also carries a GEMPUMP_VOTE_LINES line + GEMPUMP_LINK (2026-10-22, Fernando): IWRU is on Gempump too, vote for
  it and the cat shares fish with the humans.
- DIVIDENDS_LINES: holders of 10K $IWRU can claim their dividends ("their fish") before the cat eats them; + FEES_LINK
  (iwillrugu.com/fees, which has the claim button; minBal = 10000 there). Replaces the old once-a-day nad.fun reminder.
- next_post_delay(): seconds until the next post, from the last post time persisted in events.db, so a Render restart
  or redeploy never makes the cat post early.
"""
import random
from datetime import datetime, timedelta, timezone

DEXSCREENER_LINK = "https://dexscreener.com/monad/0x0caaf82370766e76e150a5610c16a306e254b8a7"
FEES_LINK = "https://iwillrugu.com/fees/"
GEMPUMP_LINK = "https://www.gempump.io/en/token/iwillrugu"

MIN_GAP_S = 10 * 3600   # never two posts closer than 10h
MAX_GAP_S = 13 * 3600   # 10-13h after the last one -> about two a day
MAX_PER_UTC_DAY = 2
STARTUP_MIN_S = 20 * 60  # first post after a fresh install (no history): 20 min - 3h
STARTUP_MAX_S = 3 * 3600

DEXSCREENER_LINES = [
    "look at the chart. I swear I saw fish in the graph. 🐟📊😼",
    "$IWRU is on Dexscreener now. I've been staring at the candles for 4 hours. some of them are definitely sardines. 🕯️🐟",
    "I put my paw on the Dexscreener chart and now there's a paw print on the screen. technical analysis complete. 🐾📊",
    "the chart is up on Dexscreener. I don't read charts. I knock them off the table. 📊😼",
    "zoomed into the $IWRU chart on Dexscreener. found a fish. ate the fish. the chart is still there. 🐟",
    "humans keep saying 'check Dexscreener'. I checked. there were no snacks. 0/10. still looking though. 📊😾",
    "every green candle on Dexscreener is a fish that hasn't been caught yet. I'm just saying. 🟩🐟😼",
    "I tried to sit on the Dexscreener chart. it was a laptop. now it's my laptop. 💻😼",
    "breaking: cat finds own token on Dexscreener. cat is unimpressed. cat refreshes 47 times anyway. 🔄😼",
    "the $IWRU chart lives on Dexscreener. it moves like a laser pointer. I must chase it. 🔴📊",
    "someone said 'wick'. I thought they said 'fish stick'. anyway, chart's on Dexscreener. 🐟📊",
    "I don't know what a liquidity pool is but if it has fish in it I'm jumping in. Dexscreener says it's there. 🏊‍♂️🐟😼",
    "went to Dexscreener. saw a line going sideways. it reminded me of a sleeping cat. respect. 😴📊",
    "the cat has officially been spotted on Dexscreener. witnesses report fish-shaped candles. 👀🐟",
    "I hiss at red candles and purr at green ones. all of them are on Dexscreener. 😾🟥 😺🟩",
    "pretty sure that candle on Dexscreener is a fish bone. I'm investigating. with my face. 🦴📊😼",
    "Dexscreener chart open, paw on the keyboard, 'asdfghjkl' sent to the group chat. I am a trader now. ⌨️😼",
    "they told me to 'DYOR'. I did my own research. the research is: there's a chart on Dexscreener and it's mine. 📊😼",
    "the chart has more ups and downs than me at 3am zooming through the house. Dexscreener has it all. 🌙📊",
    "if you stare at the $IWRU chart on Dexscreener long enough, the chart stares back. it's me. I'm the chart. 👁️😼",
    "Dexscreener added a cat to the blockchain. the blockchain did not consent. 🐈⛓️",
    "I knocked a glass of water off the desk while checking Dexscreener. no regrets. chart's still there. 💧📊😼",
    "I sniffed the chart on Dexscreener. smells like tuna and chaos. my two favourite things. 🐟🌀",
    "the cat would like to report a fish sighting at the following coordinates: Dexscreener, $IWRU chart. 📍🐟",
    "chart update from the cat: there is a chart. it is on Dexscreener. I have looked at it. I am now going to sleep. 😴",
    "I asked the Dexscreener chart where my dinner is. it just drew a line. useless. beautiful, but useless. 📊😾",
    "candles on Dexscreener remind me of the candles I knock off shelves. same energy, less fire. 🕯️😼",
    "today's forecast from the cat: 80% chance of me looking at the Dexscreener chart and 20% chance of a nap on it. 🌤️📊",
    "the fish are in the chart. I've seen them. they're on Dexscreener. nobody believes me. 🐟📊🙀",
    "a human in this chat asked 'where chart'. the cat answers: Dexscreener. the cat also wants a treat for answering. 📊🐟",
]

GEMPUMP_VOTE_LINES = [
    "we're on Gempump too. go vote for IWRU and I'll share my fish with the humans. the small ones. ok, the medium ones. 🗳️🐟",
    "IWRU is on Gempump. every vote = one fish I personally hand to a human. I have never shared fish before. this is huge. 🗳️😼",
    "vote for IWRU on Gempump and I'll throw a fish party. you bring the votes, I bring the fish, nobody touches my tuna. 🎉🐟",
    "Gempump has a vote button for IWRU. I can't press it, my paws are too fluffy. you press it. I'll pay you in sardines. 🗳️🐾",
    "humans who vote for IWRU on Gempump get fish. humans who don't get stared at. for a long time. 👀🐟",
    "I promised the humans fish if IWRU wins votes on Gempump. I don't have fish. I will find fish. vote anyway. 🗳️😼🐟",
    "go to Gempump, vote IWRU, come back, collect one (1) imaginary fish from the cat. very generous cat. 🐟✨",
    "every Gempump vote for IWRU makes the fish bowl bigger. the cat is now legally obligated to share it. 🐠🗳️",
    "vote IWRU on Gempump. I'll knock a fish off the table for each of you. that's how cats say thank you. 🐟😼",
    "Gempump voting is open. I already voted for IWRU 47 times with my paw on the keyboard. your turn, humans. fish after. 🗳️⌨️🐟",
    "if IWRU climbs on Gempump I'm sharing the fish stash. yes, the secret one under the couch. vote. 🛋️🐟",
    "dear humans: one vote for IWRU on Gempump = one fish from me. this offer is valid until I get hungry. 🗳️🐟😼",
]

DIVIDENDS_LINES = [
    "holding 10K $IWRU? then you have fish waiting for you. claim them before the cat eats them. 🐟😼",
    "PSA from the cat: 10K $IWRU holders can claim their dividends. I'm guarding them. badly. I'm hungry. 🐟",
    "your dividends are sitting there like an unattended tuna. I'm not saying I'll eat it. I'm not NOT saying it. 🐟👀",
    "if you hold 10K $IWRU, there are fish with your name on them. I've licked some of them. they're still yours. mostly. 🐟😼",
    "claim your fish. I repeat. claim your fish. the cat cannot be held responsible for unclaimed fish. 🐟🚨",
    "10K $IWRU = you can claim dividends. 0 claims = the cat gets ideas. 💡🐟😼",
    "I counted your unclaimed dividends. then I counted my empty bowl. then I looked at you. 🥣😼",
    "reminder: your dividends don't claim themselves. I tried claiming them for you. turns out I don't have thumbs. 🐾",
    "the fish bowl is full of your dividends and the cat is sitting right next to it. tick tock. 🐟⏰",
    "holders with 10K $IWRU: your fish are ready. come get them before I mistake them for dinner. 🍽️🐟",
    "every unclaimed dividend is a fish swimming in circles waiting for you. I find this very stimulating. 🐟🌀😼",
    "breaking: local cat spotted near pile of unclaimed dividends. licking lips. claim yours now. 📰🐟",
    "I'm not a financial advisor. I'm a cat. and the cat says: go claim your fish if you hold 10K $IWRU. 😼🐟",
    "your dividends asked me to tell you they miss you. then I tried to eat them. please hurry. 🐟💔",
    "fish delivery for every wallet with 10K $IWRU! the delivery cat is getting hungry on the way. 🚚🐟😼",
    "I knocked a dividend off the table. it's fine, it's still there. but the next one I'm eating. claim yours. 🐟",
    "unclaimed dividends make the cat nervous. a nervous cat eats things. claim your fish. 😾🐟",
    "you hold 10K $IWRU and you haven't claimed your fish? the cat is disappointed. the cat is also drooling. 🐟🤤",
    "the cat sat on your dividends to keep them warm. they're getting a bit squished. come claim them. 🐟😼",
    "your fish are waiting. my patience is not. claim your $IWRU dividends. 🐟⏳",
    "10K $IWRU in your wallet = fish in your bowl. empty bowl = you forgot to claim. full cat = I didn't forget. 🐟😼",
    "I sniffed the dividends vault. it smells like fish. that means you should claim before I find the door. 🚪🐟",
    "a fish was left unclaimed for 3 days. nobody knows what happened to it. the cat has no comment. 🐟🤐",
    "dividends: claimable. cat: hungry. you: hopefully faster than the cat. 🏃🐟😼",
    "if you have 10K $IWRU, go claim your fish. if you don't, sit with me and watch other people claim theirs. 🍿😼",
    "the cat has opened the fish vault and is holding the door. for you. for now. hurry. 🚪🐟",
    "you know what's better than fish? free fish. you know what's better than free fish? claiming it before the cat does. 🐟😼",
]


def _parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def next_post_delay(last_sent_iso, sent_today, now=None):
    """Seconds until the next Dexscreener post.

    last_sent_iso: ISO UTC timestamp of the last post (None if never posted).
    sent_today: "YYYY-MM-DD:N" -- how many posts went out on that UTC day.
    The next post is 10-13h after the last one; if that lands on a UTC day that already had MAX_PER_UTC_DAY posts,
    it moves to a random moment in the first 3h of the next UTC day (still >= 10h after the last post).
    """
    now = now or datetime.now(timezone.utc)
    last = _parse(last_sent_iso)
    if last is None:
        return random.uniform(STARTUP_MIN_S, STARTUP_MAX_S)
    target = last + timedelta(seconds=random.uniform(MIN_GAP_S, MAX_GAP_S))
    day, _, count = (sent_today or "").partition(":")
    if day == target.strftime("%Y-%m-%d") and count.isdigit() and int(count) >= MAX_PER_UTC_DAY:
        next_day = datetime(target.year, target.month, target.day, tzinfo=timezone.utc) + timedelta(days=1)
        target = max(next_day + timedelta(seconds=random.uniform(0, 3 * 3600)), last + timedelta(seconds=MIN_GAP_S))
    return max((target - now).total_seconds(), 60.0)


def bump_day_count(sent_today, now=None):
    """New "YYYY-MM-DD:N" value after a post at `now`."""
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    day, _, count = (sent_today or "").partition(":")
    n = int(count) + 1 if day == today and count.isdigit() else 1
    return f"{today}:{n}"
