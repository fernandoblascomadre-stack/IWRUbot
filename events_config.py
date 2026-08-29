import os
import re
import pathlib

from Crypto.Hash import keccak


def _safe_int_env(name: str, default: int) -> int:
    """Reads an integer env var with a safe fallback -- for a value that
    already has a sensible default, a single typo'd override should never
    crash the whole bot at import time (mirrors _resolve_event_window's
    reasoning below, applied generically)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[events_config] invalid {name}={raw!r}, falling back to {default}", flush=True)
        return default


def _safe_float_env(name: str, default: float) -> float:
    """Float sibling of _safe_int_env, same never-crash-at-import reasoning --
    used for the buybot's USD floor, which is naturally fractional."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[events_config] invalid {name}={raw!r}, falling back to {default}", flush=True)
        return default


def _bool_env(name: str) -> bool:
    """Reads a boolean env var, OFF unless explicitly enabled -- only "1" or
    "true" (any casing, surrounding whitespace ignored) turn it on. Any other
    value, including typos, stays False rather than surprise-enabling a
    feature -- mirrors _safe_int_env's never-crash-at-import reasoning."""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true")


# ══════════════════════════════════════════════════════════════════════════
#  BUYBOT ($IWRU buy alerts, see buybot.py)
# ══════════════════════════════════════════════════════════════════════════
# Off until switched on from Render: the poller hits a public RPC every few
# seconds, so it should never start by accident (e.g. on a local run someone
# left open, which would double-post every buy).
BUYBOT_ENABLED = _bool_env("BUYBOT_ENABLED")

# Buys smaller than this in USD are ignored. A bonding curve gets a lot of
# dust; without a floor the chat turns into a ticker.
BUYBOT_MIN_USD = _safe_float_env("BUYBOT_MIN_USD", 2.0)

# Seconds between polls. The public Monad RPC caps eth_getLogs at a 100-block
# range and Monad blocks land ~0.3s apart, so one call covers ~30s of chain:
# poll well inside that and the happy path is always a single request.
BUYBOT_POLL_SECONDS = _safe_int_env("BUYBOT_POLL_SECONDS", 20)

# How many 100-block pages one tick may walk while catching up after downtime.
# 20 pages = 2000 blocks ~ 10 minutes of chain per tick, so even a long Render
# redeploy is caught up within a couple of ticks without ever asking the RPC
# for a range it will refuse.
BUYBOT_MAX_PAGES_PER_TICK = _safe_int_env("BUYBOT_MAX_PAGES_PER_TICK", 20)

# $IWRU on Monad (chain 143) -- the same address bot.py already links as NAD_CA.
BUYBOT_TOKEN = "0xaCCD61772BCd3717546f141382b68b6D2EF17777"
BUYBOT_RPC_URLS = [
    u.strip()
    for u in (os.environ.get("MONAD_RPC_URLS") or "https://rpc.monad.xyz,https://monad.drpc.org").split(",")
    if u.strip()
]
BUYBOT_NADFUN_API = "https://api.nad.fun"
BUYBOT_EXPLORER = "https://monadscan.com"
# Banner sent with every buy alert. Resolved relative to this file, not the
# process cwd, same as ASSETS_DIR below. A missing file is not fatal: the
# alert falls back to text-only rather than being dropped.
BUYBOT_IMAGE = pathlib.Path(__file__).parent / "assets" / "buybot" / "buy.jpg"
# Where the buttons point. The chart is nad.fun for as long as $IWRU sits on
# the bonding curve: there is no DEX pair yet, so there is no Dexscreener or
# GeckoTerminal chart to link either.
BUYBOT_CHART_URL = os.environ.get("BUYBOT_CHART_URL") or f"https://nad.fun/tokens/{BUYBOT_TOKEN}"
BUYBOT_SITE_URL = os.environ.get("BUYBOT_SITE_URL") or "https://iwillrugu.com/"
# BUYBOT_CHAT_ID is defined below, next to EVENTS_CHAT_ID, since it defaults
# to it.

# ══════════════════════════════════════════════════════════════════════════
#  OWNER
# ══════════════════════════════════════════════════════════════════════════
# Fixed numeric Telegram user ID. Every permission check compares against
# this constant directly -- no username resolution, no discovery, nothing
# persisted. The env var override exists only so a local test run can point
# at a different test-owner account without editing this file.
BOT_OWNER_ID = _safe_int_env("BOT_OWNER_ID", 5612550615)

# ══════════════════════════════════════════════════════════════════════════
#  GROUP / SCHEDULING
# ══════════════════════════════════════════════════════════════════════════
EVENTS_CHAT_ID = int(os.environ["EVENTS_CHAT_ID"])

# Buy alerts land in the same chat (and, with no thread id, the same General
# topic) the bot already posts in. Overridable so alerts can be split out into
# a dedicated chat later without touching code.
BUYBOT_CHAT_ID = _safe_int_env("BUYBOT_CHAT_ID", 0) or EVENTS_CHAT_ID

def _resolve_event_window() -> tuple:
    """Daily random event fires at a random second inside this UTC hour
    window. Validates env-var input (both must be valid 0-24 hours, with
    end strictly after start) and falls back to the safe default rather than
    letting a misconfiguration crash the whole bot at import time."""
    try:
        start = int(os.environ.get("EVENT_WINDOW_START_UTC", "12"))
        end = int(os.environ.get("EVENT_WINDOW_END_UTC", "23"))
        if 0 <= start < 24 and 0 < end <= 24 and end > start:
            return start, end
        raise ValueError(f"out of range or end <= start ({start}-{end})")
    except ValueError as e:
        print(
            f"[events_config] invalid EVENT_WINDOW_START_UTC/END_UTC ({e}), falling back to 12-23 UTC",
            flush=True,
        )
        return 12, 23


EVENT_WINDOW_UTC = _resolve_event_window()

# How many group messages of chat activity pass before an UNCLAIMED event
# gets reposted at the bottom of the chat, so it can't stay buried forever
# under new conversation. Only applies while the event is genuinely
# unclaimed -- once someone's caught it, there's nothing left to surface.
EVENT_BUMP_MESSAGE_THRESHOLD = _safe_int_env("EVENT_BUMP_MESSAGE_THRESHOLD", 1)

# Minimum seconds between two automatic bumps, regardless of message count --
# with the threshold above at 1, the message-count check alone would fire on
# literally every group message. This cooldown keeps "the treasure reappears
# near-instantly" while capping how often a fast burst of chat activity can
# trigger a full repost (2 Telegram sends + 2 deletes) in a row.
EVENT_BUMP_COOLDOWN_SECONDS = _safe_int_env("EVENT_BUMP_COOLDOWN_SECONDS", 20)

# ══════════════════════════════════════════════════════════════════════════
#  WALLET VALIDATION
# ══════════════════════════════════════════════════════════════════════════
WALLET_RE = re.compile(r'^0x[a-fA-F0-9]{40}$', re.IGNORECASE)


def is_valid_wallet(address: str) -> bool:
    """Shape check (WALLET_RE) first, then -- only when the address is
    mixed-case -- verifies its EIP-55 checksum too.

    Wallet apps (MetaMask etc.) normally display addresses with a specific
    mix of upper/lowercase letters that encodes a checksum of the address
    itself. An all-lowercase or all-uppercase address carries no checksum
    (both are equally valid on-chain, unchanged from before this function
    existed) and is accepted on shape alone, same as always. But a MIXED-case
    address is claiming to carry that checksum, so verifying it catches a
    real class of typos/bad copy-pastes automatically -- a single altered
    character almost always breaks the checksum -- before real funds are
    ever sent to a wrong address. This never adds a step for a winner who
    just copy-pastes their real address correctly; it only ever rejects
    something that was already wrong."""
    if not WALLET_RE.fullmatch(address):
        return False
    hex_part = address[2:]
    if hex_part == hex_part.lower() or hex_part == hex_part.upper():
        return True
    hash_hex = keccak.new(digest_bits=256, data=hex_part.lower().encode("ascii")).hexdigest()
    for i, c in enumerate(hex_part):
        if c.isalpha() and c.isupper() != (int(hash_hex[i], 16) >= 8):
            return False
    return True

# ══════════════════════════════════════════════════════════════════════════
#  ASSETS
# ══════════════════════════════════════════════════════════════════════════
# Resolved relative to this file, not the process cwd. Drop PNGs here later;
# missing files fall back to text-only events automatically.
ASSETS_DIR = pathlib.Path(__file__).parent / "assets" / "events"

# ══════════════════════════════════════════════════════════════════════════
#  EVENT BUTTONS (fixed, shared across all event types)
# ══════════════════════════════════════════════════════════════════════════
CATCH_BUTTON_TEXT = "🐾 CATCH TREASURE"
INSPECT_BUTTON_TEXT = "👀 INSPECT"

# ══════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════
# Adding a new event later = adding one entry here. Nothing else needs to
# change. `stars`/`rarity_label` are purely cosmetic and independent of the
# real `weight`.
EVENTS = {
    "mouse": {
        "emoji": "🐭",
        "name": "Mouse",
        "weight": 50,
        "reward": 250,
        "stars": "⭐☆☆☆☆",
        "rarity_label": "Common",
        "sticker_filename": "mouse.webp",
        "catch_text": "First human to catch it wins.",
    },
    "fish": {
        "emoji": "🐟",
        "name": "Purple Fish",
        "weight": 30,
        "reward": 1000,
        "stars": "⭐⭐☆☆☆",
        "rarity_label": "Uncommon",
        "sticker_filename": "purple_fish.webp",
        "catch_text": "First human to catch it wins.",
    },
    "box": {
        "emoji": "📦",
        "name": "Mystery Box",
        "weight": 15,
        "reward": 2500,
        "stars": "⭐⭐⭐☆☆",
        "rarity_label": "Rare",
        "sticker_filename": "mystery_box.webp",
        "catch_text": "First human to catch it wins.",
    },
    "crown": {
        "emoji": "👑",
        "name": "Golden Crown",
        "weight": 5,
        "reward": 10000,
        "stars": "⭐⭐⭐⭐⭐",
        "rarity_label": "Legendary",
        "sticker_filename": "golden_crown.webp",
        "catch_text": "First human to catch it wins.",
    },
}

# ══════════════════════════════════════════════════════════════════════════
#  INSPECT POPUPS
# ══════════════════════════════════════════════════════════════════════════
# Common pool (~99% of presses). Flavor only -- never affects the game.
INSPECT_POPUPS = [
    "It smells like fish...",
    "Maybe it is a trap.",
    "IWRU is watching you.",
    "This treasure looks suspicious.",
    "Touch it if you dare.",
    "You notice tiny paw prints...",
    "There is a strange smell...",
    "The treasure looks real.",
    "😼 Nothing to see here. Move along.",
    "🐾 Something rustled nearby.",
    "The treasure hums quietly. Or maybe that's just you.",
    "IWRU sniffs it approvingly.",
    "You feel like you're being watched. You are.",
    "It's warmer than it should be.",
    "😼 Curiosity is a virtue. Mostly.",
]

# Rare pool (~1% of presses, see INSPECT_RARE_CHANCE). Pure easter egg --
# mechanically does nothing, just makes people wonder.
INSPECT_RARE_POPUPS = [
    "👀 Wait... I think I hear another mouse... 😼",
]

INSPECT_RARE_CHANCE = 0.01

# ══════════════════════════════════════════════════════════════════════════
#  LATE-CLICK POPUPS
# ══════════════════════════════════════════════════════════════════════════
LATE_POPUPS = [
    "Too late, human. Try again another day.",
    "😼 Someone was faster.",
    "🐾 Better luck tomorrow.",
    "😹 I already stole it.",
    "🌙 Come back tomorrow.",
    "😼 Not today.",
    "🐟 Gone. Just like that.",
    "The vault says no.",
]

# ══════════════════════════════════════════════════════════════════════════
#  TODAY'S LUCK  (pure roleplay, no gameplay effect, no correlation to odds)
# ══════════════════════════════════════════════════════════════════════════
TODAYS_LUCK_MESSAGES = [
    ("⭐⭐⭐☆☆", "Maybe today you'll find something..."),
    ("⭐☆☆☆☆", "Stay in the group... I have a feeling..."),
    ("⭐⭐☆☆☆", "Nothing today. Probably. I don't actually know."),
    ("⭐⭐⭐⭐☆", "The vault feels generous today. Or hungry. Hard to tell."),
    ("⭐☆☆☆☆", "I wouldn't get my hopes up, human."),
    ("⭐⭐⭐⭐⭐", "Something big is coming. Or I'm just excited about fish."),
    ("⭐⭐☆☆☆", "Keep watching. That's all I'll say."),
]

# ══════════════════════════════════════════════════════════════════════════
#  EVENT TEASER  (posted some time before the daily event, pure atmosphere)
# ══════════════════════════════════════════════════════════════════════════
# The daily event itself fires at a random second inside EVENT_WINDOW_UTC, so
# this can only ever be an approximate heads-up, never a countdown -- these
# lines are deliberately vague and never promise a specific time. Skipped
# entirely if a treasure is already sitting there unclaimed (that's a bigger
# hint than any of these).
EVENT_TEASER_MESSAGES = [
    "😼 I smell something in the vault. Could be nothing.",
    "🐾 The vault's been rattling. Keep an eye on the chat.",
    "👀 Something's stirring in the Fish Vault today.",
    "😼 I have a feeling. Don't ask me to explain it.",
    "🐟 The vault feels heavier than usual today...",
    "😼 Stay close. I think today's the day.",
    "🧶 I keep looking at the vault. It's looking back.",
    "👑 Something shiny might turn up. Might.",
]

# Randomized each time the daily event is (re)scheduled, so the lead time
# isn't a fixed, learnable interval. Skipped entirely if the window is too
# tight to fit a meaningful lead (see events.py's _schedule_daily_event).
EVENT_TEASER_LEAD_SECONDS_MIN = _safe_int_env("EVENT_TEASER_LEAD_SECONDS_MIN", 900)   # 15 min
EVENT_TEASER_LEAD_SECONDS_MAX = _safe_int_env("EVENT_TEASER_LEAD_SECONDS_MAX", 2700)  # 45 min

# ══════════════════════════════════════════════════════════════════════════
#  /iwru FLAVOR LINES
# ══════════════════════════════════════════════════════════════════════════
IWRU_MENU_FLAVOR_LINES = [
    "😼 I'm not sleeping... I'm just watching.",
    "🐟 Have you seen any fish today?",
    "🧶 Don't touch my yarn.",
    "👑 One day I'll steal a crown.",
    "🐭 I can smell a mouse...",
    "😼 Stay a while. The vault likes company.",
    "🐾 Something's out there. I can feel it.",
    "😼 Ask me anything. I'll probably ignore you.",
]

# ══════════════════════════════════════════════════════════════════════════
#  MESSAGE TEMPLATES
# ══════════════════════════════════════════════════════════════════════════
PENDING_WALLET_MSG = (
    "Congratulations! 🎉\n\n"
    "Please send your Monad wallet address to receive your reward.\n\n"
    "⚠️ If you don't send it before the next treasure event starts, this prize expires and goes back to the Fish Vault."
)

# Sent by the Owner's "🔔 Remind Winner" button -- a manual nudge, not tied
# to any automatic timer, so it can be re-sent as many times as the Owner
# likes while the claim is still 'claimed'.
WALLET_REMINDER_MSG = (
    "⏰ Reminder: you still have a prize waiting! 🎉\n\n"
    "Please send your Monad wallet address to receive your reward.\n\n"
    "⚠️ If you don't send it before the next treasure event starts, this prize expires and goes back to the Fish Vault."
)

WALLET_INVALID_MSG = (
    "😼 That doesn't look like a valid Monad wallet address.\n\n"
    "It should look like: 0x followed by 40 hex characters. Try again."
)

WALLET_RECEIVED_MSG = "Wallet received. Reward pending."

WALLET_UPDATED_MSG = "Wallet updated. Reward still pending."

NOT_YOUR_PRIZE_MSG = "😼 There's no prize waiting for you right now."

PENDING_PAYMENT_MSG = "😼 Your wallet is on file. The Owner is sending your reward -- sit tight."

ALREADY_PENDING_CLAIM_MSG = "😼 You already have a prize waiting on your wallet. Send that one first."

OWNER_CANNOT_CLAIM_MSG = "😼 You are the Owner. You can't steal from your own Fish Vault."

DEEPLINK_LINE_TEMPLATE = (
    "\n\n🐈‍⬛ To receive your reward, click here and press Start:\n"
    "👉 {deep_link}\n\n"
    "⚠️ If you don't do this before the next treasure event starts, this prize expires and goes back to the Fish Vault."
)

# Used instead of DEEPLINK_LINE_TEMPLATE on the rare occasion the bot can't
# even generate its own deep link (a transient get_me() failure) -- the
# winner still needs SOME path forward instead of a dead end.
DEEPLINK_UNAVAILABLE_LINE = (
    "\n\n🐈‍⬛ I couldn't reach you privately. Please message me directly to claim your reward.\n\n"
    "⚠️ If you don't do this before the next treasure event starts, this prize expires and goes back to the Fish Vault."
)

# {winner} is a precomputed display string ("@username" or the first name
# when no username is set) -- never a raw username field, so it never
# breaks on users without one.
CAUGHT_TEMPLATE = (
    "🎉 {winner} caught the {name}!\n\n"
    "Reward: {reward} IWRU\n\n"
    "🐈‍⬛ Check your DMs with me to claim it!"
)

# ══════════════════════════════════════════════════════════════════════════
#  CATCH CONGRATULATIONS  (cat-voice, per item -- separate from CAUGHT_TEMPLATE)
# ══════════════════════════════════════════════════════════════════════════
# Sent by events.py's on_catch as its OWN follow-up chat message, right after
# the functional CAUGHT_TEMPLATE edit (reward amount + DM instructions) --
# pure personality, never touches the claim/reward flow itself, so a failure
# sending this is only ever a missed joke. {name} is always the winner's
# plain first name, never an @-mention (per the user, 2026-08-29). Every
# joke's punchline is the CAT's own absurd, greedy, self-important behavior
# -- never at the winner's expense, per the user's explicit instruction ("el
# loco es el gato", "jamas atacamos a nuestro usuarios"). 10 lines per item,
# each tuned to that item specifically per the user's own examples: the cat
# wants the mouse/fish back for itself, "graciously" allows the crown-winner
# to look at the crown it still considers its own throne, and flip-flops on
# loving/hating surprises for the mystery box.
CATCH_CONGRATS = {
    "mouse": [
        "{name} caught the mouse?? that was MY mouse. I was building up to that mouse. respect, but also, give it back. 🐭😼",
        "{name} you caught a mouse. I would like to formally request custody. joint custody. mostly custody. 🐭😾",
        "{name}, the mouse is yours, legally. emotionally, it's still mine. I'm not over this. 🐭😼",
        "congratulations {name}. a mouse. do you know how long I've been stalking that exact mouse? asking for a friend. the friend is me. 🐭🕵️",
        "{name} snagged the mouse before I could. I'm proud of you. I'm also filing a complaint. both are true. 🐭😼",
        "well played, {name}. the mouse chose you. I would like to speak to the mouse's manager. 🐭📋",
        "{name}, a mouse! incredible! now hand it over, slowly, no sudden movements. 🐭😼",
        "{name} you win the mouse. the cat congratulates you through gritted teeth. mostly gritted. 🐭😬",
        "the mouse is yours now, {name}. I hope you know what that means. it means I'm coming over. 🐭😼🚪",
        "{name} caught it fair and square. the cat is happy for you. the cat is ALSO available to 'help' you keep it safe. 🐭😼",
    ],
    "fish": [
        "{name} caught the fish. beautiful. stunning. now, as one professional to another, can I have it. 🐟😼",
        "congratulations {name}! a fish! the cat would like to negotiate terms. terms being: give me the fish. 🐟🤝",
        "{name} you caught a fish and I have never respected anyone more. also I would like the fish. 🐟😼",
        "the fish is yours, {name}. legally binding. spiritually, I feel it belongs to whoever is currently the most charming. that's me. 🐟😼",
        "{name} caught a fish!! do you know what I would do for that fish. actually don't answer that. just hand it over. 🐟😼",
        "well caught, {name}. truly inspiring. now, about that fish. I have a proposal. it involves you giving it to me. 🐟📜",
        "{name} you're now the proud owner of a fish. the cat would like to be considered for adoption. of the fish. 🐟🐈‍⬛",
        "a fish, {name}? incredible work. I'm not crying, I'm just very interested in your fish specifically. 🐟😼",
        "{name} caught the fish fair and square. the cat salutes you and also very quietly eyes the fish. 🐟👀",
        "congratulations on the fish, {name}. purely out of curiosity, hypothetically, what would it take for you to give it to me. 🐟😼",
    ],
    "box": [
        "{name} caught the mystery box!! I love surprises. I also hate surprises. mostly I just need to know what's inside RIGHT NOW. 📦😼",
        "congratulations {name}, a mystery box! don't open it. actually open it. actually let me open it. 📦😼",
        "{name} you won a mystery box. the suspense is delightful. the suspense is also unbearable. open it immediately. 📦😅",
        "a mystery box, {name}? the cat respects mystery in theory and finds it deeply stressful in practice. what's inside. tell me. 📦😼",
        "{name} caught the box! I've always said surprises are the spice of life. I've also always been lying. what's in it. 📦😼",
        "well done {name}. a mystery box. I would like to state, for the record, that I am extremely calm about not knowing what's in it. 📦😤",
        "{name} you got the mystery box. genuinely thrilled for you. genuinely need to know the contents within the next ten seconds. 📦⏱️",
        "congratulations on the box, {name}. the cat's philosophy is 'embrace the unknown,' followed immediately by 'no wait, tell me everything.' 📦😼",
        "{name} caught a mystery box and honestly? bold. brave. now open it before I do it for you. 📦😼",
        "a mystery box for {name}! I love that we'll never know what's inside. also I will absolutely find out. 📦🕵️",
    ],
    "crown": [
        "{name} caught the crown. cute. anyway, the king is me, obviously, but you can hold it for a bit. carefully. 👑😼",
        "congratulations {name}, a crown! I'll allow you to look at it. admire it, even. it does not leave my jurisdiction. 👑😼",
        "{name} you caught the crown. impressive. meaningless, since royalty is determined by whoever naps the most, but impressive. 👑😴",
        "a crown, {name}? charming. decorative. purely ceremonial. the real throne is my spot on the windowsill. 👑😼",
        "{name} caught the crown!! you may wear it. briefly. under supervision. mine. 👑😼",
        "congratulations on the crown, {name}. I've decided you're now a duke. maybe a baron. I'm the only king here. 👑🎓",
        "{name} you win the crown. historic moment. doesn't change the chain of command, but historic nonetheless. 👑😼",
        "the crown is yours, {name}, in the sense that you're holding it. ownership is more of a cat concept. 👑😼",
        "{name} caught the golden crown! truly legendary. I remain, as always, the actual monarch here. you may bow. 👑👋",
        "congratulations {name}, a crown! I'll allow this. temporarily. the cat retains all executive power regardless. 👑😼",
    ],
}

# How long after a CATCH_CONGRATS message a reply to it, or a native emoji
# reaction on it, can still trigger a CATCH_FOLLOWUP_QUIPS follow-up in
# bot.py (per the user: keep the cat "alive" and responsive right after a
# win, not forever).
CATCH_ENGAGEMENT_WINDOW_SECONDS = 20 * 60  # 20 minutes

OWNER_WAITING_TEMPLATE = (
    "🟡 Waiting for Wallet\n\n"
    "Winner: {winner}\n"
    "Event: {emoji} {name}\n"
    "Reward: {reward} IWRU\n\n"
    "Waiting for the winner to submit their Monad wallet."
)

OWNER_READY_TEMPLATE = (
    "🟠 Ready to Pay\n\n"
    "Winner: {winner}\n"
    "Event: {emoji} {name}\n"
    "Reward: {reward} IWRU\n"
    "Wallet: {wallet}\n\n"
    "Send the reward from the Fish Vault, then confirm below."
)

OWNER_PAID_TEMPLATE = (
    "🟢 Paid\n\n"
    "Winner: {winner}\n"
    "Event: {emoji} {name}\n"
    "Reward: {reward} IWRU\n"
    "Wallet: {wallet}"
)

OWNER_CANCELLED_TEMPLATE = (
    "❌ Cancelled\n\n"
    "Winner: {winner}\n"
    "Event: {emoji} {name}\n"
    "Reward: {reward} IWRU\n"
    "{wallet_line}\n"
    "This claim was cancelled and released."
)

WINNER_PAID_MSG = "Your reward has been sent.\n\nThank you for supporting IWRU."

CLAIM_CANCELLED_WINNER_MSG = "😼 Your pending prize was released by the Owner. If you think this is a mistake, reach out."

# Auto-expiry (NOT the same as a manual Owner cancel): fires only for a claim
# still waiting on the winner's FIRST wallet submission, the moment a new
# event begins -- since events fire at random times, "the next treasure
# appeared" is the natural point to say "you took too long," rather than a
# fixed timer. A wallet that's already been submitted (ready_to_pay) never
# expires this way -- only the Owner paying or cancelling resolves it.
CLAIM_EXPIRED_WINNER_MSG = (
    "😼 Too slow! A new treasure appeared before you sent your wallet, "
    "so this one slipped away. The Fish Vault keeps it."
)

EXPIRED_GROUP_TEMPLATE = (
    "⌛ {winner} didn't claim the {emoji} {name} reward in time.\n\n"
    "The Fish Vault keeps it."
)

OWNER_EXPIRED_TEMPLATE = (
    "⌛ Expired\n\n"
    "Winner: {winner}\n"
    "Event: {emoji} {name}\n"
    "Reward: {reward} IWRU\n\n"
    "This claim expired automatically -- the winner never submitted a wallet before the next treasure appeared."
)

# Shown on the GROUP's original announcement message when the Owner manually
# releases a claim (on_owner_cancel) -- without this, that message would be
# left forever showing "caught!" with what looks like a still-live keyboard,
# giving the community no indication the prize was ever released back to the
# vault.
CANCELLED_GROUP_TEMPLATE = (
    "❌ {winner}'s claim on the {emoji} {name} was released.\n\n"
    "The Fish Vault keeps it."
)

# Fires for a treasure NOBODY ever caught at all (status stayed 'unclaimed')
# once a new event is about to begin -- no winner, so no winner DM and no
# Owner message either (the Owner is only ever notified once someone catches
# something in the first place).
UNCLAIMED_EXPIRED_TEMPLATE = (
    "⌛ Nobody found the {emoji} {name} in time.\n\n"
    "The Fish Vault keeps it."
)

# Fires when the Owner manually withdraws a still-unclaimed event (e.g. a
# test generation) via the Owner panel's "Cancel Current Event" button --
# distinct wording from the auto-expiry template above, since this was an
# intentional Owner action, not a timeout.
OWNER_WITHDRAWN_GROUP_TEMPLATE = (
    "❌ The {emoji} {name} was withdrawn.\n\n"
    "The Fish Vault keeps it."
)

GROUP_PAID_ANNOUNCEMENT_TEMPLATE = "🎉 {winner} has received {reward} IWRU."

# ══════════════════════════════════════════════════════════════════════════
#  HOW TO PLAY
# ══════════════════════════════════════════════════════════════════════════
# Built from EVENTS itself (not hardcoded) so the reward numbers shown here
# can never drift out of sync with the real payouts if EVENTS is ever retuned.
_HOW_TO_PLAY_TREASURE_LINES = "\n".join(
    f"{info['emoji']} {info['name']} — {info['reward']} IWRU" for info in EVENTS.values()
)

HOW_TO_PLAY_TEXT = (
    "🐈‍⬛ How to Play\n\n"
    "Welcome to my little game, human.\n\n"
    "🐾 Once a day I may discover a treasure.\n\n"
    f"{_HOW_TO_PLAY_TREASURE_LINES}\n\n"
    "If you are the first to press Catch, the treasure is yours.\n\n"
    "I'll ask you for your Monad wallet in private.\n\n"
    "The rewards are manually sent from the Fish Vault.\n\n"
    "Think you're faster than me?\n\n"
    "Let's find out. 😼"
)
