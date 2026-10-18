"""
paradox_cards.py -- the daily IWRU PARADOX post on X (2026-10-04, new format 2026-10-18).

Fernando (2026-10-18): ONE post a day, at a random moment between 17:30 and 19:00 Spain time, with a different hook
each day -- a card, a combo, a battle or an IWRU joke -- so it never repeats the same announcement and still invites
people to play. Mentions @opensea and @monad in the post; the first reply carries the OpenSea collection and the game
link (https://iwillrugu.com/paradox/#/). The very first post of the new format is his own text (INTRO_TEXT). All the
words live in paradox_post_lines.py.

- Rotation: ROTATION repeats card / combo / card / battle / card / joke. Card posts alternate a Common and a powerful
  card (Rare, Epic, Legendary or Mythic art), so the posts show different strategies, not an NFT catalogue.
- Card list: paradox_cards.json (the 222 file names in IWRU PARADOX/public/cards); names, factions and rarities in
  paradox_card_info.json (from IWRU PARADOX/src/data/paradox-cards.json -- regenerate both if cards are added).
  Images are fetched at post time from the live site (https://iwillrugu.com/paradox/cards/<file>), the same WebP the
  game serves, so the bot repo doesn't carry 44 MB of art.
- Every queue (cards per pool, combos, battles, jokes) is persisted in events.db's config table: each item once
  before any repeat, and a restart or redeploy never resets a cycle.
- Per-Madrid-date dedupe written BEFORE posting (same restart-safety idiom as bot.py's tweet jobs).
- Self-gated: register() does nothing unless the four TWITTER_* keys are set.
"""
import asyncio
import json
import os
import random
import re
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import paradox_post_lines as L

try:
    import db
except Exception:  # pragma: no cover -- tests import this module without the bot's env
    db = None

try:
    import tweepy
except ImportError:  # pragma: no cover
    tweepy = None

MADRID_TZ = ZoneInfo("Europe/Madrid")
POST_WINDOW_MADRID = ((17, 30), (19, 0))  # 17:30-19:00 Spain time, once a day

CARD_IMAGE_BASE = "https://iwillrugu.com/paradox/cards/"
OPENSEA_URL = "https://opensea.io/collection/iwru-paradox"
GAME_URL = "https://iwillrugu.com/paradox/#/"
_HERE = os.path.dirname(os.path.abspath(__file__))
_MANIFEST = os.path.join(_HERE, "paradox_cards.json")
_INFO = os.path.join(_HERE, "paradox_card_info.json")
_KEYS = ("TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")

ROTATION = ["card", "combo", "card", "battle", "card", "joke"]
RARITY_LABEL = {"common": "Common", "rare": "Rare", "epic": "Epic", "legendary": "Legendary"}
COMBO_QUESTIONS = ["Would you run this combo?", "What would you add to it?", "Have you tried it yet?"]
BATTLE_QUESTIONS = ["How would you play it?", "Would you have seen it coming?", "What's your answer to it?"]
TWEET_LIMIT = 280


# ── texts ────────────────────────────────────────────────────────────────

def card_id(file: str) -> str:
    return re.sub(r"(-mythic)?\.\w+$", "", file)


def card_info() -> dict:
    with open(_INFO, encoding="utf-8") as f:
        return json.load(f)


def card_heading(file: str, info: dict) -> str:
    c = info[card_id(file)]
    rarity = "Mythic" if "-mythic" in file else RARITY_LABEL[c["rarity"]]
    return f"{c['name']}\n{c['faction']} · {rarity}"


def build_card_text(file: str, info: dict, hook: str, question: str) -> str:
    return f"{card_heading(file, info)}\n\n{hook}\n\nIn battle: {L.UTILITY[card_id(file)]}\n\n{question}\n\n{L.CALL}"


def build_combo_text(combo: dict, question: str) -> str:
    return f"{combo['text']}\n\n{question}\n\n{L.CALL}"


def build_battle_text(battle: dict, question: str) -> str:
    return f"{battle['text']}\n\n{question}\n\n{L.CALL}"


def build_joke_text(joke: dict) -> str:
    return f"{joke['text']}\n\n{L.CALL}"


def build_reply_text(opener: str) -> str:
    return (f"{opener}\n\nCollect your cards:\n{OPENSEA_URL}\nPlay IWRU PARADOX:\n{GAME_URL}\n"
            "Link the wallet holding your card NFTs to your game account to use them in battle.")


def tweet_len(text: str) -> int:
    """X's weighted length, conservatively: every URL counts 23, Latin text 1 per char, anything else (emoji, symbols
    outside the light ranges) 2 per code point -- slightly over-counts emoji sequences, never under-counts."""
    text = re.sub(r"https?://\S+", "x" * 23, text)
    n = 0
    for ch in text:
        o = ord(ch)
        n += 1 if (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037) else 2
    return n


# ── persisted queues ─────────────────────────────────────────────────────

def card_files() -> list:
    with open(_MANIFEST, encoding="utf-8") as f:
        return [c for c in json.load(f) if isinstance(c, str) and c.endswith((".webp", ".png", ".jpg"))]


def card_pools() -> dict:
    """'common' = the Common cards, 'power' = Rare, Epic, Legendary and every Mythic-art file."""
    info = card_info()
    pools = {"common": [], "power": []}
    for f in card_files():
        c = info.get(card_id(f))
        if c and card_id(f) in L.UTILITY:
            pools["common" if c["rarity"] == "common" and "-mythic" not in f else "power"].append(f)
    return pools


def _next_from(key: str, items: list, fits=None) -> str:
    """Every item once, in random order, before any repeat; persisted so a restart never resets the cycle. A new cycle
    never starts with the item that ended the previous one. With `fits`, takes the first queued item that fits (the
    others keep their place); None when no item fits at all."""
    if fits is not None and not any(fits(x) for x in items):
        return None
    raw = db.get_config(key)
    try:
        queue = [x for x in json.loads(raw) if x in items] if raw else []
    except (ValueError, TypeError) as e:
        print(f"[paradox-cards] {key} corrupted ({e}), reshuffling", flush=True)
        queue = []
    if not queue:
        queue = items[:]
        random.shuffle(queue)
        last = db.get_config(key + "_last")
        if len(queue) > 1 and queue[0] == last:
            queue.append(queue.pop(0))
    if fits is not None and not any(fits(x) for x in queue):
        queue += [x for x in items if x not in queue]  # nothing left that fits: let the next cycle's items in early
    chosen = queue.pop(next(i for i, x in enumerate(queue) if fits is None or fits(x)))
    db.set_config(key, json.dumps(queue))
    db.set_config(key + "_last", chosen)
    return chosen


def _pick_index(key: str, options: list, fits=None):
    i = _next_from(key, [str(i) for i in range(len(options))], fits and (lambda x: fits(options[int(x)])))
    return None if i is None else options[int(i)]


def card_post_text(file: str, info: dict) -> str:
    """A card post whose hook and question fit in TWEET_LIMIT: the question first, then the first queued hook that
    fits with it (the shortest question if no hook fits with the one drawn)."""
    question = _pick_index("paradox_post_question_q", L.QUESTIONS)
    fits = lambda h, q=None: tweet_len(build_card_text(file, info, h, q or question)) <= TWEET_LIMIT  # noqa: E731
    hook = _pick_index("paradox_post_hook_q", L.HOOKS, fits)
    if hook is None:
        question = min(L.QUESTIONS, key=len)
        hook = _pick_index("paradox_post_hook_q", L.HOOKS, lambda h: fits(h, question)) or min(L.HOOKS, key=len)
    return build_card_text(file, info, hook, question)


def next_post() -> tuple:
    """(kind, text, image files, reply text) for today's post, advancing the persisted rotation."""
    if db.get_config("paradox_post_intro_done") != "1":
        db.set_config("paradox_post_intro_done", "1")
        return "intro", L.INTRO_TEXT, [L.INTRO_CARD], L.INTRO_REPLY
    reply = build_reply_text(_pick_index("paradox_post_reply_q", L.REPLY_OPENERS))
    i = int(db.get_config("paradox_post_rotation") or 0)
    db.set_config("paradox_post_rotation", str((i + 1) % len(ROTATION)))
    kind = ROTATION[i % len(ROTATION)]
    if kind == "combo":
        combo = _pick_index("paradox_post_combo_q", L.COMBOS)
        files = [f"{c}.webp" for c in combo["cards"]]
        return kind, build_combo_text(combo, random.choice(COMBO_QUESTIONS)), files, reply
    if kind == "battle":
        battle = _pick_index("paradox_post_battle_q", L.BATTLES)
        return kind, build_battle_text(battle, random.choice(BATTLE_QUESTIONS)), [f"{battle['card']}.webp"], reply
    if kind == "joke":
        joke = _pick_index("paradox_post_joke_q", L.JOKES)
        return kind, build_joke_text(joke), [f"{joke['card']}.webp"], reply
    pool = "power" if db.get_config("paradox_post_next_pool") == "power" else "common"
    db.set_config("paradox_post_next_pool", "common" if pool == "power" else "power")
    file = _next_from(f"paradox_post_{pool}_q", card_pools()[pool])
    return kind, card_post_text(file, card_info()), [file], reply


# ── timing ───────────────────────────────────────────────────────────────

def seconds_until_post(*, now=None, posted_today=False) -> float:
    """Seconds until a random moment in today's POST_WINDOW_MADRID (from now on, if the window is already open), or in
    tomorrow's once today's has passed or already posted. Spain wall-clock, DST-aware; subtracted in UTC."""
    now = now or datetime.now(MADRID_TZ)
    (h1, m1), (h2, m2) = POST_WINDOW_MADRID
    d = now.date()
    if posted_today or now >= datetime(d.year, d.month, d.day, h2, m2, tzinfo=MADRID_TZ) - timedelta(minutes=1):
        d = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, h1, m1, tzinfo=MADRID_TZ)
    end = datetime(d.year, d.month, d.day, h2, m2, tzinfo=MADRID_TZ)
    start = max(start, now)
    at = start + (end - start) * random.random()
    return max(0.0, (at.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


# ── posting ──────────────────────────────────────────────────────────────

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


def publish(text: str, files: list, reply_text: str) -> tuple:
    """Blocking: card image(s) + main post, then the first reply. Returns (post_id, reply_id)."""
    api, client = _clients()
    media_ids = []
    for card in files:
        path = _download(card)
        try:
            media_ids.append(api.media_upload(filename=path).media_id)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    resp = client.create_tweet(text=text, media_ids=media_ids)
    post_id = resp.data["id"]
    print(f"[paradox-cards] posted id={post_id} cards={files}", flush=True)
    reply_id, last_err = None, None
    for attempt in range(3):  # the post is already public: try hard to attach its reply
        try:
            r = client.create_tweet(text=reply_text, in_reply_to_tweet_id=post_id)
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


async def daily_post_job(context):
    """Posts today's post once per Madrid date, then schedules tomorrow's. try/finally so nothing can ever skip the
    reschedule."""
    try:
        today = datetime.now(MADRID_TZ).date().isoformat()
        if db.get_config("paradox_post_date") == today:
            print("[paradox-cards] already posted today, skipping", flush=True)
            return
        db.set_config("paradox_post_date", today)  # BEFORE posting: a restart mid-post must not double-post
        kind, text, files, reply = next_post()
        try:
            await asyncio.get_event_loop().run_in_executor(None, publish, text, files, reply)
        except Exception as e:  # noqa: BLE001
            print(f"[paradox-cards] post error ({kind}, {files}): {e}", flush=True)
    finally:
        context.application.job_queue.run_once(daily_post_job, seconds_until_post(posted_today=True))


def register(application) -> bool:
    if tweepy is None or not all(os.environ.get(k) for k in _KEYS):
        print("[paradox-cards] disabled (tweepy or TWITTER_* keys missing)", flush=True)
        return False
    posted = db.get_config("paradox_post_date") == datetime.now(MADRID_TZ).date().isoformat()
    delay = seconds_until_post(posted_today=posted)
    application.job_queue.run_once(daily_post_job, delay)
    print(f"[paradox-cards] daily post (17:30-19:00 Madrid) scheduled in {delay/3600:.2f}h", flush=True)
    return True
