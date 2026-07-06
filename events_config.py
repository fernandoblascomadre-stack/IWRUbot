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
EVENT_BUMP_MESSAGE_THRESHOLD = _safe_int_env("EVENT_BUMP_MESSAGE_THRESHOLD", 5)

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
    "Please send your Monad wallet address to receive your reward."
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
    "👉 {deep_link}"
)

# Used instead of DEEPLINK_LINE_TEMPLATE on the rare occasion the bot can't
# even generate its own deep link (a transient get_me() failure) -- the
# winner still needs SOME path forward instead of a dead end.
DEEPLINK_UNAVAILABLE_LINE = (
    "\n\n🐈‍⬛ I couldn't reach you privately. Please message me directly to claim your reward."
)

# {winner} is a precomputed display string ("@username" or the first name
# when no username is set) -- never a raw username field, so it never
# breaks on users without one.
CAUGHT_TEMPLATE = (
    "🎉 {winner} caught the {name}!\n\n"
    "Reward: {reward} IWRU\n\n"
    "🐈‍⬛ Check your DMs with me to claim it!"
)

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
