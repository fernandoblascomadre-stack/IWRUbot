"""
paradox_cards.py -- "card of the moment" posts on X for IWRU PARADOX (2026-10-04).

Fernando: two IWRU PARADOX cards a day on X, at 00:00 and 13:00 Spain time,
starting today; every card from IWRU PARADOX/public/cards goes out once before
any repeats. The post is always titled "IWRU PARADOX" with a tagline in the line
of "Choose your faction. Build your deck. Unleash the chaos." and
"More info in the first reply". The first reply carries
"Think you can handle this card? Come prove it. 😼", a witty cat line inviting
people to play/collect/progress, and the site link. 20+ variants of each text.

- Card list: paradox_cards.json (the 222 file names in IWRU PARADOX/public/cards,
  regenerate it if cards are added). Images are fetched at post time from the
  live site (https://iwillrugu.com/paradox/cards/<file>), the same WebP the game
  serves, so the bot repo doesn't carry 44 MB of art.
- Rotation persisted in events.db's config table (survives Render restarts and
  redeploys, the disk is persistent): a shuffled queue, refilled only when empty.
- Exact times in Europe/Madrid, DST-aware. Per-slot, per-Madrid-date dedupe
  written BEFORE posting (same restart-safety idiom as bot.py's tweet jobs).
- Self-gated: register() does nothing unless the four TWITTER_* keys are set.
"""
import asyncio
import json
import os
import random
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db

try:
    import tweepy
except ImportError:  # pragma: no cover
    tweepy = None

MADRID_TZ = ZoneInfo("Europe/Madrid")
CARD_SLOTS_MADRID = [(0, 0), (13, 0)]  # 00:00 and 13:00 Spain time
CARD_IMAGE_BASE = "https://iwillrugu.com/paradox/cards/"
SITE_URL = "https://iwillrugu.com/"
_MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paradox_cards.json")
_KEYS = ("TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")

TITLE = "IWRU PARADOX"
MORE_INFO = "More info in the first reply 👇"
REPLY_HOOK = "Think you can handle this card? Come prove it. 😼"

TAGLINES = [
    "Choose your faction. Build your deck. Unleash the chaos.",
    "Pick a side. Stack your deck. Let the chaos do the rest.",
    "One faction. One deck. Zero mercy.",
    "Choose your faction. Sharpen your deck. Break the board.",
    "Build the deck. Bend the rules. Unleash the chaos.",
    "Every faction has a secret. Every deck has a plan. Every match is chaos.",
    "Choose wisely. Build boldly. Play dirty (within the rules).",
    "Your faction. Your deck. Your chaos.",
    "Draw. Plan. Unleash. Repeat.",
    "Pick your colors. Forge your deck. Rule the paradox.",
    "Factions clash. Decks evolve. Chaos wins.",
    "Choose your faction. Build your deck. Bring the chaos home.",
    "New card on the table. Old rivalries in the deck. Fresh chaos incoming.",
    "Collect them. Combine them. Unleash them.",
    "The deck is yours. The faction is yours. The chaos is everyone's.",
    "Choose a faction you can live with. Build a deck they can't beat.",
    "Shuffle up. Pick a side. Let the paradox begin.",
    "Twelve fragments. Endless decks. One glorious mess.",
    "Build smart. Play fearless. Unleash the chaos.",
    "Every card tells a story. Your deck decides the ending.",
    "Choose your faction. Trust your deck. Embrace the chaos.",
]

CAT_LINES = [
    "I've already picked my faction. It's the one that wins. Your move.",
    "Collect the cards, build the deck, climb the ranks. I'll be watching from the top shelf.",
    "I knocked this card off the table on purpose. Now it's your problem. Come play.",
    "Free to play, impossible to put down. Like a laser dot, but with strategy.",
    "Every card you collect makes your deck stronger. Every match you win makes me slightly less smug.",
    "I napped through the tutorial and still won. Let's see what you can do.",
    "Build a deck. Find your style. Bring snacks, it's going to be a long game.",
    "Some collect stamps. Some collect cards. I collect wins. Join the collectors.",
    "The board is set, the cards are shuffled, my tail is twitching. Ready?",
    "Progress is simple: play, win, upgrade, repeat. Naps between rounds are optional.",
    "I'd tell you my strategy, but then I'd have to sit on your keyboard.",
    "This card has been waiting for a worthy player. I volunteered. It said no. Your turn.",
    "Your deck, your rules, your bragging rights. The cat only judges a little.",
    "Collect them all, or just the shiny ones. I won't tell anyone.",
    "Every great player started by losing to a cat. Come get it over with.",
    "Climb the ranks, unlock the cards, earn the right to look smug. I'll show you how.",
    "Games are short, rivalries are long, and the cat never forgets a good play.",
    "Your next favorite card is waiting. So is your next favorite rival. It's me.",
    "Shuffle in, play a round, see why I stopped knocking things off tables. Mostly.",
    "Bring your best deck. I'll bring my best side-eye.",
    "No pressure. Just you, a deck, and a cat who has already calculated your next move.",
]

_bags = {}


def _pick(options):
    """No repeat until every option has come up (and never the same line twice
    across a reshuffle) -- same contract as bot.py's pick_phrase."""
    key = id(options)
    bag = _bags.get(key)
    if not bag:
        bag = list(options)
        random.shuffle(bag)
        last = _bags.get((key, "last"))
        if len(bag) > 1 and bag[-1] == last:
            i = random.randrange(len(bag) - 1)
            bag[-1], bag[i] = bag[i], bag[-1]
        _bags[key] = bag
    choice = bag.pop()
    _bags[(key, "last")] = choice
    return choice


def build_post_text(tagline: str) -> str:
    return f"{TITLE}\n\n{tagline}\n\n{MORE_INFO}"


def build_reply_text(cat_line: str) -> str:
    return f"{REPLY_HOOK}\n\n{cat_line}\n\n{SITE_URL}"


def card_files() -> list:
    with open(_MANIFEST, encoding="utf-8") as f:
        return [c for c in json.load(f) if isinstance(c, str) and c.endswith((".webp", ".png", ".jpg"))]


def next_card() -> str:
    """Every card once, in random order, before any repeat; persisted so a
    restart never resets the cycle. The new cycle never starts with the card
    that ended the previous one."""
    files = card_files()
    raw = db.get_config("paradox_card_queue")
    try:
        queue = [c for c in json.loads(raw) if c in files] if raw else []
    except (ValueError, TypeError) as e:
        print(f"[paradox-cards] queue corrupted ({e}), reshuffling", flush=True)
        queue = []
    if not queue:
        queue = files[:]
        random.shuffle(queue)
        last = db.get_config("paradox_card_last")
        if len(queue) > 1 and queue[0] == last:
            queue.append(queue.pop(0))
    chosen = queue.pop(0)
    db.set_config("paradox_card_queue", json.dumps(queue))
    db.set_config("paradox_card_last", chosen)
    return chosen


def seconds_until_madrid(hour: int, minute: int, *, now=None) -> float:
    """Seconds until the next hh:mm wall-clock time in Spain (DST-aware)."""
    now = now or datetime.now(MADRID_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        # next calendar day at the same WALL-CLOCK time (not +24h, which drifts
        # by an hour across a DST change)
        d = (now + timedelta(days=1)).date()
        target = datetime(d.year, d.month, d.day, hour, minute, tzinfo=MADRID_TZ)
    # Subtract in UTC: two datetimes sharing the same ZoneInfo subtract as naive
    # wall-clock times in Python, ignoring the DST jump (the 13:00 post would land
    # at 12:00 on the day the clocks go back).
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _clients():
    auth = tweepy.OAuth1UserHandler(*(os.environ[k] for k in _KEYS))
    api = tweepy.API(auth)
    client = tweepy.Client(
        consumer_key=os.environ[_KEYS[0]], consumer_secret=os.environ[_KEYS[1]],
        access_token=os.environ[_KEYS[2]], access_token_secret=os.environ[_KEYS[3]],
    )
    return api, client


def _download(card: str) -> str:
    url = CARD_IMAGE_BASE + card
    req = urllib.request.Request(url, headers={"User-Agent": "IWRUbot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"{url} -> HTTP {r.status}")
        data = r.read()
    if len(data) < 1024:
        raise RuntimeError(f"{url} -> suspiciously small ({len(data)} bytes)")
    # Re-encoded as PNG when Pillow is there: PNG is the format every X upload path
    # accepts without question (the merch posts already use it). WebP as-is otherwise.
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        img.load()
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(path, "PNG", optimize=True)
        return path
    except ImportError:
        pass
    fd, path = tempfile.mkstemp(suffix=os.path.splitext(card)[1])
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def post_card(card: str, tagline: str, cat_line: str) -> tuple:
    """Blocking: card image + main post, then the first reply. Returns (post_id, reply_id)."""
    api, client = _clients()
    path = _download(card)
    try:
        media = api.media_upload(filename=path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    resp = client.create_tweet(text=build_post_text(tagline), media_ids=[media.media_id])
    post_id = resp.data["id"]
    print(f"[paradox-cards] posted id={post_id} card={card}", flush=True)
    reply_id, last_err = None, None
    for attempt in range(3):  # the post is already public: try hard to attach its reply
        try:
            r = client.create_tweet(text=build_reply_text(cat_line), in_reply_to_tweet_id=post_id)
            reply_id = r.data["id"]
            print(f"[paradox-cards] reply id={reply_id} -> {post_id}", flush=True)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            import time as _t
            _t.sleep(5 * (attempt + 1))
    if reply_id is None:
        print(f"[paradox-cards] reply FAILED for post {post_id}: {last_err}", flush=True)
    return post_id, reply_id


async def card_slot_job(context):
    """Posts one card at its exact Madrid slot, then reschedules the same slot for
    the next day. try/finally so nothing can ever skip the reschedule."""
    hour, minute = context.job.data
    try:
        today = datetime.now(MADRID_TZ).date().isoformat()
        key = f"paradox_card_posted_{hour:02d}{minute:02d}"
        if db.get_config(key) == today:
            print(f"[paradox-cards] slot {hour:02d}:{minute:02d} already posted today, skipping", flush=True)
            return
        db.set_config(key, today)  # BEFORE posting: a restart mid-post must not double-post
        card = next_card()
        try:
            await asyncio.get_event_loop().run_in_executor(None, post_card, card, _pick(TAGLINES), _pick(CAT_LINES))
        except Exception as e:  # noqa: BLE001
            print(f"[paradox-cards] post error (slot {hour:02d}:{minute:02d}, card {card}): {e}", flush=True)
    finally:
        # +90s so "now" is safely past this slot's minute when computing the next one
        delay = seconds_until_madrid(hour, minute, now=datetime.now(MADRID_TZ) + timedelta(seconds=90)) + 90
        context.application.job_queue.run_once(card_slot_job, delay, data=(hour, minute))


def register(application) -> bool:
    if tweepy is None or not all(os.environ.get(k) for k in _KEYS):
        print("[paradox-cards] disabled (tweepy or TWITTER_* keys missing)", flush=True)
        return False
    for hour, minute in CARD_SLOTS_MADRID:
        delay = seconds_until_madrid(hour, minute)
        application.job_queue.run_once(card_slot_job, delay, data=(hour, minute))
        print(f"[paradox-cards] slot {hour:02d}:{minute:02d} Madrid scheduled in {delay/3600:.2f}h", flush=True)
    return True
