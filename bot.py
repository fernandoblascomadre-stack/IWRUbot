import asyncio
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo
from telegram import ReactionTypeEmoji, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

try:
    import tweepy
    _TWEEPY_AVAILABLE = True
except ImportError:
    _TWEEPY_AVAILABLE = False

TWEET_URL_RE = re.compile(r'https?://(x|twitter)\.com/\S+')

TOKEN = os.environ["TOKEN"]

# imported after TOKEN is validated so a missing EVENTS_CHAT_ID (required by
# events_config.py at import time) doesn't get misattributed as a core-bot
# startup failure
import events
import db
import buybot
import events_config as cfg

# chats pre-registered via env var (KNOWN_CHAT_IDS="-1003859192674,-100...") so
# the bot knows where to speak from startup, without depending on receiving a message first.
# One malformed entry is skipped (and logged) individually rather than crashing the whole
# list -- and the whole bot process -- at import time over a single typo.
def _parse_known_chat_ids(raw: str) -> list[int]:
    ids = []
    for x in raw.replace(" ", "").split(","):
        if not x:
            continue
        try:
            ids.append(int(x))
        except ValueError:
            print(f"[startup] invalid KNOWN_CHAT_IDS entry {x!r}, skipping", flush=True)
    return ids


KNOWN_CHAT_IDS = _parse_known_chat_ids(os.environ.get("KNOWN_CHAT_IDS", ""))

# ── No-repeat phrase picker ──────────────────────────────────────────────
# Plain random.choice() can pick the same line twice in a row, or resurface
# one long before the rest of its list has had a turn -- noticeable on the
# smaller flavor-text lists. Each list gets its own shuffled "bag" (keyed by
# the list object's identity, since every phrase list here is a distinct
# module-level constant that lives for the whole process): pull without
# replacement until the bag's empty, then reshuffle a fresh one.
_phrase_bags: dict[int, list] = {}
_phrase_last: dict[int, object] = {}


def pick_phrase(options):
    """Drop-in replacement for random.choice(options) that only repeats a
    line once every other option in `options` has come up."""
    key = id(options)
    bag = _phrase_bags.get(key)
    if not bag:
        bag = list(options)
        random.shuffle(bag)
        if len(bag) > 1 and bag[-1] == _phrase_last.get(key):
            # bag.pop() below draws from the end -- without this swap, a
            # freshly-shuffled bag could hand back the exact same line that
            # just ended the previous bag, a back-to-back repeat across the
            # reshuffle boundary that the whole point of this function is to
            # avoid.
            swap_idx = random.randrange(len(bag) - 1)
            bag[-1], bag[swap_idx] = bag[swap_idx], bag[-1]
        _phrase_bags[key] = bag
    choice = bag.pop()
    _phrase_last[key] = choice
    return choice


STICKER_BUY     = "CAACAgQAAyEFAATmBptiAAIbc2pCtW0Cin0rkU6CFSGyVqWmQYbMAAILIQACaEkIUnVRn_2NEtPVPAQ"
STICKER_WELCOME = "CAACAgQAAyEFAATmBptiAAIbdGpCtXLR4nqSl707gZNKRYI7MUZOAAJBIAACRh8JUh_nOBSMnXM1PAQ"

# ── Cooldown ───────────────────────────────────────────────────────────────
_last_random: dict[int, float] = {}
RANDOM_COOLDOWN = 305   # +15% (was 360) -- spontaneous quips a bit more often
RANDOM_CHANCE   = 0.11  # +15% (was 0.096) (x2 between 2-5am)

# ── User tracking ──────────────────────────────────────────────────────────
_known_chats: dict[int, float]  = {}
_known_users: dict[int, dict]   = {}
_user_nicknames: dict[int, str] = {}

# ── Bot username cache ─────────────────────────────────────────────────────
_bot_username: str | None = None

# ── Mood system ──────────────────────────────────────────────────────────
# One mood shared across every chat (there's one cat, not one per group),
# rerolled lazily whenever it expires -- same lazy-eval pattern as hour_now(),
# no background job needed. Nudges HOW OFTEN existing behaviors fire (speaking
# up unprompted, landing a callout vs. going quiet, which reaction emoji it
# favors) rather than adding a whole new set of canned lines per mood, so it
# doesn't require re-splitting the phrase lists.
MOODS = ["chaotic", "sleepy", "hungry", "watchful"]
MOOD_BIAS = {
    "chaotic":  {"speak_mult": 1.3, "callout_mult": 1.0},
    "sleepy":   {"speak_mult": 0.6, "callout_mult": 0.7},
    "hungry":   {"speak_mult": 1.0, "callout_mult": 1.3},
    "watchful": {"speak_mult": 1.0, "callout_mult": 1.4},
}
_mood_state: dict = {"name": None, "expires_at": 0.0}


def current_mood() -> str:
    now = time.time()
    if now >= _mood_state["expires_at"]:
        choices = [m for m in MOODS if m != _mood_state["name"]] or MOODS
        _mood_state["name"] = random.choice(choices)
        _mood_state["expires_at"] = now + random.uniform(3 * 3600, 8 * 3600)
    return _mood_state["name"]

# ── Message counter → chaos burst every N messages ─────────────────────────
_msg_counter: dict[int, int] = {}
_next_trigger: dict[int, int] = {}

# ── Triggers ───────────────────────────────────────────────────────────────
RAID_TRIGGERS  = ["⚡️ raid tweet", "raid tweet", "⚡️ raid", "raidtweet", "raid!"]
GM_TRIGGERS    = ["gm", "good morning", "morning fam", "buenos días", "gm everyone", "gm fam", "rise and shine"]
GN_TRIGGERS    = ["gn", "good night", "goodnight", "buenas noches", "gn everyone", "sleep well", "going to sleep"]
HI_TRIGGERS    = ["hi", "hello", "hey", "yo", "sup", "what's up", "howdy"]
MOON_TRIGGERS  = ["moon", "🚀", "pump", "pumping", "mooning", "ath", "all time high", "bullish", "we're going up", "to the moon"]
DIP_TRIGGERS   = ["dip", "dump", "dumping", "red", "crashed", "bleeding", "ngmi", "rekt", "it's over"]
WEN_TRIGGERS   = ["wen", "when moon", "when pump", "wen lambo", "wen rich", "when rich"]
CHART_TRIGGERS = ["chart", "price", "marketcap", "market cap", "mcap", "📊", "📈", "📉"]
MONAD_TRIGGERS = ["monad", "#monad", "mon blockchain", "built on monad"]
IWRU_TRIGGERS  = ["i will rug u", "i will rug you", "iwru 🐟", "iwru 😼", "iwru!"]
# Every fish/shellfish emoji Telegram renders natively -- not sea mammals
# (whale/dolphin/seal), those aren't fish or seafood so they stay out.
FISH_EMOJI_TRIGGERS = ["🐟", "🐠", "🐡", "🎣", "🦈", "🦞", "🦀", "🦐", "🐙", "🦑", "🐚", "🍣", "🍤", "🍥"]
CAT_TRIGGERS   = ["cat", "cats", "kitty", "kitties", "kitten", "kittens", "meow", "feline", "housecat", "tabby"]
CRYPTO_TRIGGERS = ["crypto", "token", "altcoin", "altcoins", "defi", "degen", "portfolio", "bags", "bag", "hodl", "holder", "holders", "web3"]

def _contains_word(text: str, triggers: list[str]) -> bool:
    """True if any trigger appears as a whole word/phrase in text (not embedded inside a longer word)."""
    for t in triggers:
        if not any(c.isalnum() for c in t):
            if t in text:
                return True
        elif re.search(rf'(?<!\w){re.escape(t)}(?!\w)', text):
            return True
    return False

def _starts_with_word(text: str, triggers: list[str]) -> bool:
    """True if text starts with a trigger as a whole word/phrase (not a prefix of a longer word)."""
    return any(re.match(rf'{re.escape(t)}(?!\w)', text) for t in triggers)

def hour_now():
    return datetime.now().hour

# ══════════════════════════════════════════════════════════════════════════
#  NICKNAME SYSTEM
# ══════════════════════════════════════════════════════════════════════════
NICKNAMES = [
    "the fish hoarder",
    "suspicious human",
    "potential vault supporter",
    "person who maybe has fish",
    "the one with nice hands (for scratching)",
    "unreliable fish source",
    "human of interest",
    "the quiet one",
    "fish suspect",
    "undecided investor",
    "future fish donor",
    "the one the cat is watching",
    "MON accumulator",
    "chaos ally",
    "unremarkable but present",
    "new fish in the chat",
    "possible fish dealer",
    "vault adjacent human",
    "the one who sometimes checks the chart",
    "financial cryptid",
    "the one the cat trusts slightly",
    "professional lurker",
    "fish adjacent",
    "definitely not a rug",
    "the cat's least suspicious suspect",
    "vault enthusiast (probably)",
    "the one who owes the cat a fish",
    "certified human",
    "the one who sometimes says gm",
    "fish watcher",
    "chart toucher",
    "minor chaos contributor",
    "fish-adjacent wallet holder",
    "the one who exists (verified)",
    "scratch provider (potential)",
    "snack-adjacent human",
    "the one who looked at me once",
    "fish-curious individual",
    "vault supporter in training",
    "the one who always has pockets (probably fish in there)",
]

CALLOUT_MESSAGES = [
    "{name} hey. HEY. do you have fish. 🐟",
    "{name}. the cat has been watching you. not in a weird way. in a cat way. 😼",
    "{name} scratch my belly. I said scratch it. please. just once. 😼",
    "{name} have you tried IWRU Journey yet? I'm the main character. just saying. 🎮😼",
    "{name}. give me a fish. one fish. you won't miss it. 🐟",
    "{name} the cat has been thinking about you. and fish. mostly fish. but you were in there too. 🐟😼",
    "{name}. the vault noticed you. the vault says hi. also it wants fish. 🐟",
    "{name} you look like someone who has fish. I'm not wrong about these things. 🐟😼",
    "{name}. the cat requires your attention. briefly. what do you know about fish. 😼",
    "{name} have you checked the chart today? I did. I approved it. 📈😼",
    "{name}. I knocked something over for you specifically. you're welcome. 😼",
    "{name} you've been quiet. the cat noticed. say something. or give me fish. 🐟😼",
    "{name}. do you have a coin. just one. for the vault. no pressure at all. 🐟",
    "{name} I need to be scratched behind the ear. you have good hands. I can tell. 😼",
    "{name}. tried IWRU Journey? I'm in it. I'm great in it. worth seeing. 🎮😼",
    "{name} I'm going to sit on you for a moment. don't move. this is fine. 😼",
    "{name}. the fish vault is growing. you could be part of that. 🐟😼",
    "{name} say something. the cat is here. listening. ish. 😼",
    "{name}. the cat chose you today. I don't know why either. but here we are. 😼",
    "{name} I've decided you're a vault supporter. congratulations. fish appreciated. 🐟😼",
    "{name}. one fish. that's all. just one. the cat is very reasonable. 🐟😼",
    "{name} hey. are you okay. the cat is asking. it's a cat thing. don't read into it. 😼",
    "{name}. you have the energy of someone who hasn't bought $IWRU yet. I could be wrong. 😼🐟",
    "{name} I was asleep and I thought of you. I don't know what that means. fish? 🐟😴😼",
    "{name}. come here. closer. no not that close. closer. okay. do you have fish. 🐟😼",
    "{name} I knocked something over earlier and thought of you. unrelated. deeply unrelated. 😼",
    "{name}. the cat has assigned you a role: fish provider. this is an honor. 🐟😼",
    "{name} I sat next to you the other day. metaphorically. in the blockchain. 😼🐟",
    "{name}. I found something. I lost it. you were nearby. unrelated. probably. 😼",
    "{name} the cat is watching you specifically. *slow blink* ...okay. you pass. 😼",
    "{name}. I've decided today is about you. specifically the fish you might have. 🐟😼",
    "{name} did you know the cat can sense fish from a distance? I'm sensing something. 🐟",
    "{name}. no reason. just wanted you to know I'm aware you exist. 😼",
    "{name} I had a dream about you last night. it involved fish. make of that what you will. 🐟😴",
    "{name}. the vault whispered your name. probably wants fish from you. 🐟😼",
    "{name} you've been suspiciously quiet. the cat has questions. 😼",
    "{name}. I knocked something over in your honor today. you're welcome. 😼",
    "{name} the cat has assigned you homework: scratch behind my ears. due immediately. 😼",
    "{name}. I would like you to scratch behind my ears. this is not a request. 😼",
    "{name} I've been watching the chart and thinking about you. mostly the chart. but you too. 📈😼",
    "{name}. the vault has a spot reserved with your name on it. fill it. 🐟",
    "{name} you have main character energy today. use it wisely. or don't. up to you. 😼",
    "{name}. I don't do favors, but I'll make an exception if there's fish involved. 🐟😼",
    "{name} the cat has decided you're trustworthy. don't make this weird. 😼",
    "{name}. three am. can't sleep. thinking about you and also fish. mostly fish. 🐟😴😼",
    "{name} I have a mission for you: find fish. report back. 😼🐟",
    "{name}. you looked at your phone instead of me. I noticed. I remember. 😼",
    "{name} the vault is hungry and somehow I thought of you first. 🐟😼",
    "{name}. I sat on something important earlier. it made me think of you. unrelated. 😼",
    "{name} you have been designated 'potential fish source' by the cat's own authority. 🐟😼",
    "{name}. I'm bored and you're here. this is not a coincidence. entertain me. 😼",
    "{name} the chart moved and I immediately thought 'I should tell {name}.' so. it moved. 📈😼",
    "{name}. I demand your attention for exactly nine seconds. starting now. 😼",
    "{name} someone has to feed the vault today. the cat nominates you. 🐟😼",
    "{name}. I'm not saying you're my favorite. I'm also not saying you're not. 😼",
    "{name} the cat has been thinking about IWRU Journey and also, briefly, about you. 🎮😼",
    "{name}. you owe the vault an update. the cat is simply the messenger. 🐟😼",
    "{name} I performed a slow blink in your general direction. this means something. 😼",
    "{name}. the fish situation involves you now. I don't make the rules. 🐟😼",
    "{name} I would like to report that I have not forgotten about you. carry on. 😼",
    "{name}. today's forecast: chaos, with a chance of more chaos. 😼",
    "{name} the cat sees all. right now the cat sees you. do something about it. 😼",
    "{name}. I've decided you're part of today's plan. the plan is fish. 🐟😼",
    "{name} you've been on my mind since roughly four minutes ago. an eternity, for me. 😼",
    "{name}. the vault called your name. I heard it. I'm relaying the message. 🐟",
    "{name} I need someone to talk to. you were nearby. this is how it works. 😼",
    "{name}. give the vault some love today. the cat is supervising personally. 🐟😼",
    "{name} you have been quiet for too long. the cat considers this suspicious. 😼",
    "{name}. I would like fish, attention, or both, in that order of importance. 🐟😼",
    "{name} the cat requests your presence for an unspecified but very important reason. 😼",
    "{name}. today I knocked something over specifically thinking of your reaction. 😼",
    "{name} you have been randomly selected by the cat's own algorithm. the prize is my attention. 😼",
    "{name}. the vault approves of you. the vault rarely approves of anyone. 🐟😼",
    "{name} I stared at the door for a while today. then I thought of you. unrelated. 😼🚪",
    "{name}. this message is a reminder that the cat exists and so, apparently, do you. 😼",
    "{name} the chart's looking interesting. so is this callout. coincidence? probably. 📈😼",
    "{name}. I've decided to bother you specifically today. lucky you. 😼",
    "{name} the vault has your name written somewhere. probably. maybe. mysterious. 🐟😼",
    "{name}. you seem like someone who has fish and hasn't mentioned it. suspicious. 🐟😼",
    "{name} I'm awake, you're here, the vault is hungry. connect the dots. 🐟😼",
    "{name}. today's cat fact: I am currently thinking about you and, separately, about fish. 🐟😼",
    "{name} the cat has chosen you for a very important task: existing near the vault. 🐟😼",
    "{name}. I don't often reach out. consider this rare and treat it accordingly. 😼",
    "{name} you've earned a callout today. the criteria remain classified. congratulations. 😼",
    "{name}. the cat would like an update on your fish inventory. urgent. 🐟😼",
    "{name} I have decided, unilaterally, that you're having a good day. no further action required. 😼",
    "{name}. somewhere a fish is waiting for you. specifically you. don't keep it waiting. 🐟😼",
    "{name} the cat noticed you scrolled past this chat earlier. the cat notices everything. 😼",
    "{name}. today I require: attention, fish, and your continued presence in that order. 🐟😼",
    "{name} you are hereby summoned by the cat for reasons that will remain eternally unclear. 😼",
    # 🐟 Fish/vault requests
    "{name}. the vault counted its fish this morning and came up one short. coincidence? you tell me. 🐟😼",
    "{name} I left a spot open in the vault. it has your name on it, figuratively. fill it with fish. literally. 🐟😼",
    "{name}. I don't ask for much. fish. attention. total devotion. that's it. that's the list. 😼🐟",
    "{name} the vault sent me to negotiate. it wants fish. I want fish. we are aligned. 🐟😼",
    "{name}. somewhere in your pockets there could be a fish. I choose to believe this. 🐟😼",
    "{name} I audited the vault today. it's thriving. it could be thriving more. with your help. 🐟😼",
    "{name}. the vault doesn't do interviews but if it did, you'd be the first call. bring fish. 🐟😼",
    "{name} I've been guarding the vault so hard I forgot to eat. fix this. with fish. 🐟😾",
    "{name}. the vault grew a little today. I'd like it to grow a little more today too. 🐟😼",
    "{name} you keep walking past the vault like it's furniture. it's not furniture. it's hungry. 🐟😼",
    "{name}. fish now, questions later. mostly no questions. just fish. 🐟😼",
    "{name} the vault has excellent taste in supporters. I've decided you qualify. prove me right. 🐟😼",
    "{name}. I would trade you three purrs for one fish. this offer expires when I forget I made it. 🐟😼",
    "{name} the vault whispered something about you today. it was mostly about fish, but you came up. 🐟😼",
    "{name}. I've calculated exactly how much fish the vault needs. it's more. it's always more. 🐟😼",
    "{name} the vault doesn't judge. I do, a little. bring fish and I'll stop. 🐟😼",
    "{name}. today's agenda: you, fish, in that order, negotiable on the order. 🐟😼",
    "{name} I stood guard over the vault all night. it's exhausting work. it deserves fish. so do I. 🐟😴",
    "{name}. the vault likes you. I checked. it has no way of telling me this but I checked anyway. 🐟😼",
    "{name} consider this an invoice. one (1) fish. payable to the cat, immediately. 🐟😼",
    "{name}. the vault's door creaked today. I took it as a sign. the sign said 'more fish, please.' 🐟😼",
    "{name} I'm not saying the vault is lonely. I'm saying it would feel a lot less lonely with fish from you. 🐟😼",
    "{name}. I've named you an honorary fish liaison. the position is unpaid. the position requires fish. 🐟😼",
    "{name} the vault runs on fish and vibes. the vibes are fine. the fish situation needs you. 🐟😼",
    "{name}. I dreamt the vault was full. then I woke up. then I remembered you exist. connect the dots. 🐟😴😼",
    # 😼 Surveillance / attention
    "{name}. I've been counting how many times you've scrolled past me today. it's a lot. I'm not mad. I'm counting. 😼",
    "{name} I watched you type and delete a message three times. the cat saw everything. the cat says nothing. 😼",
    "{name}. you looked at the group chat and didn't say hi to me specifically. I noticed. I always notice. 😼",
    "{name} I've assigned myself as your personal supervisor. no you can't opt out. it already happened. 😼",
    "{name}. I know you're online. the little dot told me. the little dot and I are close. 😼",
    "{name} I have been sitting very still, watching this chat, waiting for you to appear. here you are. hello. 😼",
    "{name}. you've gone seventeen minutes without acknowledging my existence. this ends now. 😼",
    "{name} I opened one eye specifically because you sent a message. this is the highest honor I give. 😼",
    "{name}. I keep a mental list of who's paying attention to me. you're on it. barely. improve this. 😼",
    "{name} the cat requires a status update. how are you. be specific. mention fish if relevant. 😼🐟",
    "{name}. I noticed you left the chat open in another tab. the cat notices tabs now. new skill. 😼",
    "{name} you typed 'lol' at something that wasn't funny. the cat is disappointed but still watching. 😼",
    "{name}. I have decided to supervise your online presence indefinitely. you're welcome. or sorry. 😼",
    "{name} I stared at your profile picture for a solid minute today. I have thoughts. I'm keeping them. 😼",
    "{name}. the cat has been present this entire conversation. you just didn't notice. rude, but expected. 😼",
    "{name} I refreshed this chat four times waiting for you. don't let it go to your head. too late probably. 😼",
    "{name}. you're currently the most-watched human in this chat. congratulations. it's not a compliment or an insult. it just is. 😼",
    "{name} I clocked your last message at 2 seconds past a normal response time. suspicious. explain yourself. 😼",
    "{name}. I've been quietly present in this chat for hours specifically hoping you'd show up. here you are. 😼",
    "{name} the cat's radar pinged your name today. the radar is just vibes but it's rarely wrong. 😼",
    "{name}. you exist in my peripheral awareness at all times now. this is not something you can undo. 😼",
    "{name} I watched the typing indicator appear under your name and then disappear. what were you going to say. tell me. 😼",
    "{name}. today I decided you're interesting. this decision is final and mildly inconvenient for both of us. 😼",
    "{name} I have logged your presence. the log is just my memory. it's a good memory. mostly about fish. but you're in there. 😼🐟",
    "{name}. you've been in this chat longer than usual today. the cat is pleased. the cat will not say it twice. 😼",
    # 🎮 Game / stages references
    "{name}. I got past stage 6 without a single fish and I would like recognition for that. from you specifically. 🎮😼",
    "{name} stage 7 has something that follows me through tunnels. I've started waving at it. {name}, be more like the stalker. show up. 🎮😼",
    "{name}. I collected every fragment in the desert level and thought, briefly, of you. then I forgot again. 🌵🐟😼",
    "{name} you still haven't played IWRU Journey. I checked. the cat has ways of checking. play it. 🎮😼",
    "{name}. I wall-cling in the game and in real life. one of these is a mechanic. guess which. 🎮😼",
    "{name} the desert stage nearly got me. a laser, a dune, my own poor decisions. I survived. where were you. 🌵😼",
    "{name}. I am the main character of a video game and you are, at best, a background character in mine. no offense. lots of offense actually. 🎮😼",
    "{name} stage 7's guardians are large and unfriendly. you, {name}, are neither. this is a compliment. rare from me. 🎮😼",
    "{name}. I found a laser enemy in the desert and dodged it with style nobody witnessed. witness me next time. 🌵😼",
    "{name} the game has a Core. I don't know what's in it. I went in for fish. did you know that about me. now you do. 🐟😼",
    "{name}. every stage I clear, I think 'I should tell {name}.' then I don't. until now. I cleared one. be proud. 🎮😼",
    "{name} in the desert level heat is a mechanic. in this chat, the heat is just you not replying fast enough. 🌵😼",
    "{name}. I have a whole game built around me. what do you have built around you. think about it. 🎮😼",
    "{name} stage 7 keeps me on edge. so does waiting for you to say something. both are exhausting. 🎮😼",
    "{name}. I ran, jumped, and clung to walls today, in the game. in real life I mostly just watched you not reply. 🎮😼",
    "{name} someone in stage 7 follows me through tunnels loyally. {name}, take notes. that's dedication. 🎮😼",
    "{name}. I collected fragments across a whole desert and you haven't collected the energy to say gm. reflect on this. 🌵😼",
    "{name} the game gave me lasers to dodge. this chat gave me you, not saying much. different kind of challenge. 🎮😼",
    "{name}. I am simultaneously a token, a game protagonist, and currently, your problem. multidisciplinary. 🎮😼",
    "{name} I cleared a stage with zero fish as payment. I did it for glory. and slightly for you. mostly glory. 🎮😼",
    # 📈 Chart / market banter
    "{name}. the chart moved and my first thought was you. my second thought was fish. you were close though. 📈🐟😼",
    "{name} I stared at the chart for eleven minutes today. it stared back. so did you, apparently, from the group photo. 📈😼",
    "{name}. the chart is doing something. I approved it before checking what it was. that's leadership. 📈😼",
    "{name} you check the chart more than you check on me. I've noted this. the chart has not noted anything, it's a chart. 📈😼",
    "{name}. I don't predict the chart. I vibe with it. you should try vibing more. with the chart. and with me. 📈😼",
    "{name} the chart went sideways today, much like this conversation until you showed up. thank you. 📈😼",
    "{name}. I watched green candles and thought about fish. then I watched you scroll past and thought about nothing. 📈🐟😼",
    "{name} today's market analysis, by the cat: number went places. you should look. then bring fish. 📈🐟😼",
    "{name}. I've never once panic-sold anything. mostly because I don't understand selling. the chart respects this. 📈😼",
    "{name} the chart is a mood ring for the whole chat and right now the mood is 'where is {name}.' 📈😼",
    "{name}. I read the chart the way you read nothing, apparently, since you haven't replied. read something. 📈😼",
    "{name} the candles flickered today and I thought, deeply, about vaults, fish, and mildly, about you. 📈🐟😼",
    "{name}. I have zero financial advice and infinite opinions about the chart. you get the opinions for free. 📈😼",
    "{name} the chart pumped a little and I looked around for someone to tell. you were the closest. lucky you. 📈😼",
    "{name}. I trust the chart more than I trust silence, and right now you're giving me a lot of silence. 📈😼",
    "{name} today the chart said something. I don't know what. but it felt directed at you specifically. suspicious. 📈😼",
    "{name}. I watched the chart dip and immediately thought about warm spots to nap in. you were tangential to this. 📈😴😼",
    "{name} the chart is green today and so is my patience with you. don't test either. 📈😼",
    "{name}. I could explain the chart but I'd rather you just trust the cat. it's simpler. and correct. 📈😼",
    "{name} the chart has more consistency than you replying to me. work on that. the chart can't help you here. 📈😼",
    # 😾 Chaos / mischief
    "{name}. I knocked something off a shelf today and dedicated the fall to you. it was a short but meaningful ceremony. 😼",
    "{name} I got into something I shouldn't have. I regret nothing. you would've done the same. probably. 😼",
    "{name}. I opened a drawer today for no reason and thought 'this is very {name} of me.' I don't know what that means either. 😼",
    "{name} I caused exactly one incident today and, in my head, blamed you. this is between us now. 😼",
    "{name}. chaos happened near me today. I didn't start it. I definitely finished it though. thinking of you throughout. 😼",
    "{name} I broke a small rule today, unspecified which one. you're an accessory now. congratulations. 😼",
    "{name}. I did something today that I will neither confirm nor deny. you were nearby in spirit. 😼",
    "{name} I've declared today unofficially chaotic in your honor. no further explanation will be provided. 😼",
    "{name}. something fell over today and it wasn't me. I was elsewhere. thinking about you and fish, mostly fish. 🐟😼",
    "{name} I knocked over my own plans today and rebuilt slightly worse ones. you inspired at least one bad decision. 😼",
    "{name}. I got the zoomies and ran directly at a wall. the wall won. I'm telling you this because you deserve context. 😼💨",
    "{name} I did a small crime today. cat-sized. nothing you'd notice unless you were watching closely. were you. 😼",
    "{name}. I have been personally responsible for at least one disturbance today. you get partial credit. 😼",
    "{name} I sat somewhere I wasn't supposed to and stayed there out of spite. you'd be proud. or concerned. both fair. 😼",
    "{name}. I've decided chaos is a lifestyle and today you're an honorary participant. no refunds. 😼",
    "{name} I attacked a completely stationary object today and won convincingly. you missed a great moment. 😼",
    "{name}. I opened three cabinets today looking for nothing in particular. it's a process. you wouldn't understand. 😼",
    "{name} I have caused mild disorder near the vault today. the vault forgives me. I hope you will too. 🐟😼",
    "{name}. today's chaos level: elevated. cause: unclear. suspect: also unclear. you're on the list regardless. 😼",
    "{name} I did something reckless and small today and immediately felt very proud. you'd have clapped. probably. 😼",
    "{name}. I have started, escalated, and abandoned one plan today, all before you even said good morning. 😼",
    "{name} something in this house is now differently arranged because of me. I take full, unbothered responsibility. 😼",
    "{name}. I got the 3am zoomies and thought about you exactly once, mid-sprint. that's more than usual. 😼💨",
    "{name} I've been plotting something all day. it's small. it's cat-sized. you'll never see it coming. probably fish-related. 🐟😼",
    "{name}. I caused a minor scene today and, as always, walked away completely unbothered. you'd learn a lot from me. 😼",
    # 😴 Sleepy / glitchy
    "{name}. I was going to say something important to you but— zzzz 😴😼",
    "{name} I opened my mouth to explain something crucial and then just. stopped. thinking about you. zzzz 😴",
    "{name}. I was mid-sentence about the vault and then my eyes just— {name}, I'm still here. barely. zzzz 😴🐟",
    "{name} I fell asleep composing this message. woke up. forgot the point. you were in it somehow. 😴😼",
    "{name}. I was going to tell you something funny and then a nap happened without my consent. zzzz 😴",
    "{name} three words into this message I got sleepy. here are the three words: hello {name} zzzz 😴",
    "{name}. I dozed off thinking about fish, then about the chart, then about you, in that order of importance. 😴🐟😼",
    "{name} I woke up specifically to send you this and I already regret the effort. worth it though. probably. 😴😼",
    "{name}. mid-yawn, mid-thought, mid-{name}. everything is mid right now. good night or good day, unclear. 😴😼",
    "{name} I was dreaming about the vault and you showed up briefly, said nothing, and left. accurate honestly. 😴🐟",
    "{name}. I tried to stay awake to talk to you and lost. the nap won. it always wins. 😴😼",
    "{name} I'm 40% awake right now and you're getting all of it. lucky. or unlucky. read the room. there is no room. 😴😼",
    "{name}. I closed my eyes for what I thought was a second. it was four hours. you missed nothing. or everything. 😴😼",
    "{name} I started a sentence about you and the vault and now I don't remember which came first. zzzz 😴🐟",
    "{name}. somewhere between waking up and this message I forgot what I wanted to tell you. it was probably about fish. 😴🐟😼",
    "{name} I'm awake now. barely. you have my full, extremely limited attention. 😴😼",
    "{name}. I napped through something important today, possibly your last message. I regret nothing. mostly. 😴😼",
    "{name} I was going to make a whole plan involving you and the vault and then— *snores* 😴🐟😼",
    "{name}. I'm typing this with one eye open. the other eye is still asleep. it disagrees with everything I'm saying. 😴😼",
    "{name} I woke up mid-dream still thinking your name. this is either sweet or a glitch. undetermined. 😴😼",
    # 🌌 Existential / non-sequitur musings
    "{name}. I thought about the nature of existence today and somehow ended up thinking about you instead. downgrade. 😼",
    "{name} if a cat sends a message in a chat and no one replies, does it still count. asking for a friend. asking for you. 😼",
    "{name}. I contemplated the void today. the void looked a bit like you not responding. coincidence, probably. 😼",
    "{name} I wonder sometimes if you think about the vault as much as the vault thinks about you. it doesn't. but I do. 🐟😼",
    "{name}. today I questioned everything, including why you haven't said hi yet. some questions have easy answers. 😼",
    "{name} what even is time, really, besides the gap between your messages. philosophical. also mildly annoying. 😼",
    "{name}. I stared into nothing today and nothing stared back less than you usually do. improve. 😼",
    "{name} somewhere in the multiverse there's a version of you that replied faster. I like that version better. 😼",
    "{name}. I've decided meaning is fish, purpose is the vault, and you're a supporting character in both. minor but valued. 🐟😼",
    "{name} if I'm the main character, what does that make you. think carefully. the answer involves fish. 🐟😼",
    "{name}. I asked the universe a question today. it didn't answer. neither did you, earlier. pattern noticed. 😼",
    "{name} today's deep thought: is silence golden, or is it just you, not typing. the cat has a theory. 😼",
    "{name}. I looked at the ceiling for a while and thought about nothing, then about you, which felt like an upgrade. 😼",
    "{name} I've concluded that reality is mostly fish, occasionally chaos, and briefly, you. balanced diet. 🐟😼",
    "{name}. does the vault dream. does the chart dream. do you dream about the vault. asking the real questions. 🐟📈😼",
    "{name} I pondered my own legacy today. it involves fish, a video game, and a footnote about you. 🎮🐟😼",
    "{name}. if I disappeared tomorrow, would the vault remember me. would you remember to feed it. answer honestly. 🐟😼",
    "{name} I thought about the concept of loyalty today. the vault has it. you're auditioning. 🐟😼",
    "{name}. somewhere, a fish is asking itself the same big questions I am. mainly: where is {name} with the fish. 🐟😼",
    "{name} I contemplated my purpose today. it's fish, chaos, and keeping an eye on you specifically. simple, really. 😼",
    "{name}. is a cat that's always watching truly a cat, or something more. either way, I'm watching you. 😼",
    "{name} today I wondered what you dream about. probably not the vault. it should be the vault. 🐟😼",
    "{name}. I thought deeply about nothing for a while and you drifted into it uninvited. that's just how it works. 😼",
    "{name} if the vault is a temple, you're standing suspiciously close to the entrance without an offering. fish. now. 🐟😼",
    "{name}. I've decided the meaning of life is fish. you're free to disagree. you'd be wrong. bring fish. 🐟😼",
    # 💛 Compliments-but-weird / bonding
    "{name}. you have decent energy today. rare praise. don't get used to it. 😼",
    "{name} I've decided you're one of the good ones. the criteria is secret. the verdict stands. 😼",
    "{name}. out of everyone in this chat, you're currently my second favorite. the vault is first. sorry. 🐟😼",
    "{name} I'd let you pet me. hypothetically. under specific conditions. mainly involving fish. 🐟😼",
    "{name}. you're allowed to sit near the vault today. this is an honor I do not extend lightly. 🐟😼",
    "{name} I've added you to a very short list of humans I tolerate warmly. congratulations, genuinely. 😼",
    "{name}. you have main character energy occasionally, and today's one of those days. use it well. 😼",
    "{name} if I had to pick one human to guard the vault with me, it'd be you. probably. today. ask me tomorrow. 🐟😼",
    "{name}. I trust you slightly more than I trust the average human, and I trust almost nobody. take the win. 😼",
    "{name} you're growing on me the way a warm sunbeam does. slowly. inexplicably. pleasantly. 😼",
    "{name}. I would share a fish with you. one fish. under duress. but I would. 🐟😼",
    "{name} you're the kind of human the vault approves of. rare. don't waste it. 🐟😼",
    "{name}. I've decided we're friends now. you weren't consulted. that's how cats work. 😼",
    "{name} you have good vibes today and the cat noticed. write this down. it doesn't happen often. 😼",
    "{name}. if the vault could talk, it would probably say something nice about you. probably. maybe. it's a vault. 🐟😼",
    "{name} I'd let you scratch behind my ears for exactly nine seconds. that's a real offer. don't waste it. 😼",
    "{name}. you're one of the rare ones the cat doesn't mind existing near. treasure this. 😼",
    "{name} I've decided today you're vault-adjacent royalty. the title comes with zero benefits. enjoy it anyway. 🐟😼",
    "{name}. I sat near you in spirit today. it counts. it's the thought that matters, mostly, probably. 😼",
    "{name} you make the chaos a little more bearable, and coming from me, that's basically a love letter. 😼",
    # 🧮 Absurd cat logic
    "{name}. by my calculations, you owe the vault exactly one (1) fish, retroactively, for reasons I've since forgotten. 🐟😼",
    "{name} statistically speaking, you're due for a good day. I determined this using no math whatsoever. 😼",
    "{name}. I've cross-referenced your vibes with the chart and concluded: fish. bring fish. that's the whole analysis. 🐟📈😼",
    "{name} according to my extremely rigorous internal system, you are 73% trustworthy. bring fish to improve this. 🐟😼",
    "{name}. I ran the numbers on who should feed the vault today. it's you. the numbers were made up but the vibe is solid. 🐟😼",
    "{name} my calculations show a strong correlation between you and fish shortages. coincidence is under investigation. 🐟😼",
    "{name}. I did the math on how long you've owed me attention. the number is large. pay up. 😼",
    "{name} by cat logic, silence equals guilt, and you've been silent. explain yourself, or bring fish. 🐟😼",
    "{name}. I've determined, using zero evidence, that you're currently thinking about the vault. am I wrong. 🐟😼",
    "{name} the algorithm (me, personally, biased) has selected you today for reasons that remain classified. 😼",
    "{name}. based on absolutely nothing, I predict you'll have a good week if fish is involved early. 🐟😼",
    "{name} I performed an audit of your recent behavior. findings: inconclusive. fish would help clarify. 🐟😼",
    "{name}. my internal model of you is 90% fish-related expectations and 10% everything else. accurate representation. 🐟😼",
    "{name} I've mapped out your entire personality using vibes alone. it's mostly accurate. mostly. 😼",
    "{name}. cat science confirms: you are currently within range of the vault's influence. act accordingly. 🐟😼",
    "{name} I ran a simulation of today with you in it and without you in it. the one with you had more fish. somehow. 🐟😼",
    "{name}. preliminary findings suggest you are, in fact, a vault supporter who simply hasn't realized it yet. 🐟😼",
    "{name} I've calculated the exact probability of you having fish right now. it's higher than you're admitting. 🐟😼",
    "{name}. my model predicts you'll reply to this within the hour. prove the model right. or wrong. either is data. 😼",
    "{name} I applied cat math to the situation and concluded the answer is fish. cat math always concludes fish. 🐟😼",
    # 🎲 Wildcard
    "{name}. breaking news from the cat: nothing happened, but I wanted you to know about it immediately. 😼",
    "{name} this is a scheduled interruption of your day, brought to you by me, unscheduled. 😼",
    "{name}. consider this message a small tax on your attention. payment accepted in fish. 🐟😼",
    "{name} I've inserted myself into your day uninvited. this is standard cat procedure. resistance is pointless. 😼",
    "{name}. today's weather forecast, according to the cat: mostly chaotic with a chance of you replying. 😼",
    "{name} this message serves no purpose except to remind you the cat exists and remembers you. mission accomplished. 😼",
    "{name}. I would like to lodge a formal, entirely fake complaint about the lack of fish in my life. from you specifically. 🐟😼",
    "{name} the cat has issued a decree. it concerns you and, separately, fish. mostly fish. 🐟😼",
    "{name}. this is not a drill. or maybe it is. the cat has genuinely lost track. hi anyway. 😼",
    "{name} I would like to formally announce that I thought of you today, briefly, between naps. 😴😼",
    "{name}. consider yourself pinged, poked, and mildly inconvenienced, on behalf of the vault. 🐟😼",
    "{name} the cat requests a meeting. agenda: fish. location: wherever you are. time: now. 🐟😼",
    "{name}. I've opened an unofficial investigation into your whereabouts today. status: found you. case closed. 😼",
    "{name} today I speak for the vault, the chart, and myself, and all three of us want to know where the fish are. 🐟📈😼",
    "{name}. this has been a public service announcement from the cat. the service: bothering you. you're welcome. 😼",
]

# Night-flavored callouts (2-5am window) -- picked preferentially during
# that hour so the "everyone else is asleep" bit lands, layered on top of
# CALLOUT_MESSAGES rather than replacing it.
CALLOUT_NIGHT = [
    "{name}. it's 3am and we're both awake. this means something. probably nothing. but something. 😼🌙",
    "{name} the vault doesn't sleep and apparently neither do you. respect. also, fish. 🐟🌙😼",
    "{name}. 3am club, population: me, the vault, and now you. welcome. bring fish. 🐟🌙😼",
    "{name} everyone else is asleep. it's just us, the chart, and whatever this is. 📈🌙😼",
    "{name}. the night is quiet except for my thoughts, which are loud, and mostly about fish. 🐟😴🌙😼",
    "{name} I run this chat between 2 and 5am. you're currently in my territory. welcome, or beware. 🌙😼",
    "{name}. the zoomies hit different at 3am and somehow you're the one who's here for it. 💨🌙😼",
    "{name} nighttime is when the vault and I do our best thinking. you showing up now means something. 🐟🌙😼",
    "{name}. it's dark, it's late, and I'm fully awake staring at a wall. you're a welcome distraction. 🌙😼",
    "{name} the rest of the world sleeps. you and the cat remain. this is either bonding or a mistake. 🌙😼",
    "{name}. 3am thoughts hit different: mostly fish, occasionally you, always chaos. 🐟🌙😼",
    "{name} I patrol this chat at night when no one's watching. you're watching. noted. approved. 🌙😼",
    "{name}. the vault glows a little at night. or I imagine it does. either way, it's thinking about fish. and you, apparently. 🐟🌙😼",
    "{name} it's the witching hour and the only creatures awake are me, you, and my questionable decisions. 🌙😼",
    "{name}. late night check-in from the cat: still awake, still chaotic, still expecting fish. 🐟🌙😴😼",
    "{name} the moon's out, the chat's quiet, and I've decided to bother you specifically. lucky timing. 🌙😼",
    "{name}. nighttime is for zoomies, existential thoughts, and apparently, you. in that order. 💨🌙😼",
]

# Callouts for users who've gone quiet for a while (but are still within the
# 24h eligibility window) -- picked preferentially over the general pool when
# the chosen user's last_seen skews toward the older end of that window.
CALLOUT_QUIET = [
    "{name}. you've been quiet. suspiciously quiet. the cat has questions and mild concerns. 😼",
    "{name} silence from you for a while now. the vault noticed. I noticed. say something. or send fish. 🐟😼",
    "{name}. it's been a minute since you've said anything. the cat is not worried. the cat is just checking. mostly checking. 😼",
    "{name} you went quiet and the chat got a little less interesting. fix this immediately. 😼",
    "{name}. long time no words from you. the vault has been asking. I've been deflecting. speak up. 🐟😼",
    "{name} where did you go. the chat kept moving. the cat kept noticing you weren't in it. 😼",
    "{name}. you've been a ghost lately. the cat doesn't do ghosts unless they bring fish. 🐟😼",
    "{name} it's been quiet on your end for a while. either you're busy, asleep, or avoiding the vault. explain. 🐟😼",
    "{name}. I checked and you haven't said anything in a while. this is your reminder that I noticed. 😼",
    "{name} the chat missed a voice today, and it was yours. the cat is filling the silence with this message. 😼",
    "{name}. you've been elsewhere lately. the vault doesn't do 'elsewhere.' come back. bring fish. 🐟😼",
    "{name} a quiet {name} is a suspicious {name}. the cat has decided to investigate. starting now. 😼",
    "{name}. it's been a while. the cat doesn't do sentimental, but this is close to it. hello again. 😼",
    "{name} you disappeared for a bit there. the vault kept a spot warm. show up, eventually. 🐟😼",
    "{name}. radio silence from you lately. the cat filled the gap by staring at the wall. your fault, somehow. 😼",
    "{name} it's been quiet without you around. don't let this go to your head. but it's true. 😼",
]

# Callouts referencing a currently live, still-unclaimed "Catch the Treasure"
# event (see db.get_active_event() + events_config.EVENTS) -- {emoji}/{ev_name}/
# {reward} are filled in from that event alongside the usual {name}. Ties the
# cat's personality to something actually happening in the bot right now,
# instead of always being generic.
CALLOUT_EVENT_LIVE = [
    "{name}. there's a {emoji} {ev_name} loose in the vault right now, worth {reward} IWRU. someone should catch it. you, ideally. {emoji}😼",
    "{name} a {emoji} {ev_name} just appeared and nobody's caught it yet. {reward} IWRU just sitting there. go. now. {emoji}😼",
    "{name}. I would catch the {emoji} {ev_name} myself but I don't have thumbs. this is your moment. {reward} IWRU. go. {emoji}😼",
    "{name} the vault is currently guarding a {emoji} {ev_name}. {reward} IWRU for whoever's fastest. I'm rooting for you, mildly. {emoji}😼",
    "{name}. a {emoji} {ev_name} is live and unclaimed. {reward} IWRU. you're reading this instead of catching it. your call. {emoji}😼",
    "{name} somewhere in this chat sits a {emoji} {ev_name} worth {reward} IWRU. I would tell you where. I did. it's here. go. {emoji}😼",
    "{name}. the {emoji} {ev_name} has been unclaimed for a suspicious amount of time. {reward} IWRU, {name}. do something about it. {emoji}😼",
    "{name} breaking: a {emoji} {ev_name} is up for grabs. {reward} IWRU. I'm telling you first. mostly because you're who I picked. {emoji}😼",
    "{name}. I've been staring at the unclaimed {emoji} {ev_name} for a while now. {reward} IWRU. it won't catch itself. neither will I. {emoji}😼",
    "{name} the vault dropped a {emoji} {ev_name} and it's still sitting there. {reward} IWRU. this message is basically a nudge. {emoji}😼",
    "{name}. consider this an official cat alert: {emoji} {ev_name} unclaimed, {reward} IWRU on the line. move. {emoji}😼",
    "{name} I don't do sports but if I did, this would be the moment I'd yell 'go' at you. {emoji} {ev_name}, {reward} IWRU, unclaimed. go. {emoji}😼",
    "{name}. the {emoji} {ev_name} in the vault is watching you not catch it. {reward} IWRU. awkward. {emoji}😼",
    "{name} I'd catch the {emoji} {ev_name} myself out of spite if I could. {reward} IWRU is just sitting there. embarrassing, honestly. {emoji}😼",
    "{name}. today's vault special: one unclaimed {emoji} {ev_name}, {reward} IWRU. limited time, allegedly. go get it. {emoji}😼",
]

# ── Reaction emoji ──────────────────────────────────────────────────────────
# Telegram's setMessageReaction only accepts a fixed, platform-defined emoji
# set -- no cat faces, no fish emoji possible here (that's reserved for text).
# This is the closest-to-the-cat's-vibe subset of that set.
CAT_REACTIONS = ["👀", "😴", "🥱", "🤡", "😈", "🔥", "🤯", "🙏", "😱", "🤨", "💯", "🗿", "🐳", "🤣", "😍", "🏆", "👏", "🤔", "😭"]
# Deliberately unimpressed subset -- used when a cat/fish/chart/crypto keyword
# fires but the text-reply roll below misses, so the mention still gets
# SOME acknowledgement instead of passing by completely ignored.
INDIFFERENT_REACTIONS = ["👀", "😴", "🥱", "🤨", "🗿", "😐"]
INDIFFERENT_REACTION_CHANCE = 0.35
MOOD_REACTIONS = {
    "chaotic":  ["🤡", "😈", "🔥", "🤯"],
    "sleepy":   ["😴", "🥱", "😐"],
    "hungry":   ["🙏", "😍", "👀"],
    "watchful": ["👀", "🤨", "🗿"],
}
REACTION_CHANCE = 0.25

# ══════════════════════════════════════════════════════════════════════════
#  PHRASE LISTS
# ══════════════════════════════════════════════════════════════════════════

RANDOM_QUIPS = [
    "...the vault grows. slowly. like a fish that refuses to be caught. 🐟 but it grows.",
    "why are you all still here. go buy $IWRU. fill the vault. entertain me. 😼",
    "the circle of fish: vault feeds ecosystem 🐟 ecosystem feeds cat 😼 cat feeds vault. beautiful.",
    "every fish in the vault is a human who trusted me. the vault is very full. 🐟",
    "the fish vault is sacred. I don't even touch it. I just stare at it sometimes. 😼",
    "hold 10,000 $IWRU and MON drops in your wallet. loyalty has a price. fish too. 🐟",
    "I knocked supply off the counter. didn't apologize. your bags went up. you're welcome. 😼",
    "yes I made the rugonomics at 3am. they are correct. trust the cat. 😼🐟",
    "some say I will rug. I say I will fish. semantics. 🐟",
    "I was hungry. you had money. it was the perfect match. still is. 😼",
    "bought the dip. sat on the dip. the dip is warm now. comfortable. 🐟😼",
    "I don't need a whitepaper. I am the whitepaper. 😼",
    "I could dump my bag. I won't. not because I'm loyal. I just got comfortable. 😼",
    "the NFTs exist because my chest was full of fish. needed more space. 🐟 simple math.",
    "the vault has fish. the cat has patience. one of these is running low. 😼",
    "every fish in the vault has a story. most stories end with: and then the human bought more. 🐟😼",
    "the vault is patient. the vault has been waiting. the vault will keep waiting. fill it. 🐟",
    "I have been staring at the vault for [undisclosed] minutes. it has not moved. I will continue. 😼",
    "every trade feeds the ecosystem. every ecosystem feeds the cat. every cat sits on the vault. 😼🐟",
    "they put me in a desert in stage 6. it was 40 degrees. I found a fish near a dune. 🐟🌵 worth it.",
    "stage 7 has guardians. large ones. I am friends with 0 of them. this is expected. 😼",
    "someone in stage 7 keeps following me through tunnels. they call it a stalker. I call it a fan. 😼",
    "IWRU Journey... they make me run and jump. I do not run. except at 3am. the devs know me. 🎮😼",
    "I am simultaneously a video game character AND a financial instrument. I am multidisciplinary. 😼🎮",
    "stage 6 has laser enemies. I dodged one with my eyes closed. both eyes. 😼",
    "the developers added a desert level with heat. I told them I prefer fish. they added more enemies instead. 🐟😾",
    "in IWRU Journey I can cling to walls. in real life I also cling to walls. this is not a game mechanic. 😼",
    "stage 7 has something called the Core. I don't know what's in there. I went in anyway. for fish. 🐟",
    "I have a video game, a token, an NFT collection, and a fish vault. most cats just sleep. 😼🐟",
    "the stalker in stage 7 follows me through tunnels. it cannot catch me. nothing can catch me. 😼",
    "stage 6 has fragments to collect. I collected them. I sat on them. I kept going. 😼🎮",
    "they built a whole game around me. correct decision. I would have done the same. 🎮😼",
    "I make NFTs because the vault needed more compartments. for fish. 🐟 the art is secondary.",
    "someone bought one of my NFTs. I used the money to buy fish. 🐟 this was always the plan. 😼",
    "my NFTs fund the fish. the fish fund the vault. the vault funds the ecosystem. perfect system. 🐟😼",
    "the NFT collection is on OpenSea. I drew them with my paw. this counts as art. 😼🎨",
    "I minted an NFT at 4am while sitting in a box. the metadata is excellent. I don't know what metadata is. 😼🎨",
    "the NFTs sell. the fish grow. the vault expands. the cat sits on everything. this is the roadmap. 😼🐟",
    "I once found a spider. I sat next to it for 4 hours. it left. I still think about the spider. 🕷️😼",
    "I knocked my water bowl over. it was empty. I knocked it over anyway. 😼 very satisfying.",
    "I got into a fight with the shower curtain at 2am. the shower curtain lost. I also lost. 😾",
    "I saw a bird through the window. I made a sound. the bird did not understand. neither did I. 🐦😼",
    "I sat in a sunbeam for 3 hours. the sunbeam moved. I did not. 😼 this is called discipline.",
    "I tasted my own tail once. it was not good. I tried again 10 minutes later. same result. 😼",
    "I knocked a pen off the table. it landed under the fridge. this is now my pen. I can't reach it. still mine. 😼",
    "something is behind the refrigerator. I heard it last Tuesday. I'm still thinking about it. 😼",
    "I went through a phase where I only sat in boxes. I am still in this phase. 📦😼",
    "there was a plastic bag. it made a sound. I attacked it. I won. the bag is gone. I miss it. 😾",
    "I found a hair tie. I lost the hair tie. I found it at 3am. it was a different hair tie. 😼",
    "I got into the shower. fully. on purpose. I then left. I do not recommend this. 😼🚿",
    "I screamed at 4am. I had a reason. the reason was nothing. it was a very valid nothing. 😼",
    "I meowed at the wall for 3 minutes. the wall did not respond. the wall is wrong. 😼",
    "I saw my reflection. I did not like it. I hissed. I was right to hiss. 😼",
    "I tried to fit in a box that was clearly too small. I fit. the box disagrees. the box is wrong. 📦😼",
    "the thing about the vault is that it requires... requires... zzzz 😴",
    "I was going to explain the tokenomics but I— actually I— zzzz 😴🐟",
    "so I was in stage 6, dodging lasers, and then I found this fish near a dune and the thing is— zzzz 😴",
    "the interesting thing about Monad is— actually let me sit down for this. *sits* ...zzzz 😴😼",
    "I was watching the chart and then I— the chart was— anyway buy— zzzz 😴",
    "I once chased something across the whole room and when I got there I— I forget. zzzz 😴😼",
    "I was going to tell you about the stalker in stage 7 but I— the tunnel was— zzzz 😴😼",
    "I made an NFT last night and the thing about the art is that— the art has— zzzz 😴🎨",
    "*stares at the vault* *knocks MON off the counter by accident* *walks away* 😼",
    "*opens cabinet* ...okay. *closes cabinet* okay. 😼",
    "*sits directly on the keyboard* asjkhdasjkdhaksjdh 🐟🐟🐟",
    "*finds a box* I live here now. the box is mine. everything is fine. 📦😼",
    "*sprints across the room for no reason* I'm back. don't ask. 😼",
    "I knocked it off the counter. it needed to be on the floor. you wouldn't understand. 😼",
    "it is 3am somewhere and I am fully awake and I feel INCREDIBLE 😼",
    "I wanted attention. you gave me attention. I no longer want it. 😼",
    "the zoomies have arrived. I don't control the zoomies. the zoomies control me. 😼💨",
    "*hears a sound* WHO'S THERE. *sound stops* ...nothing. I knew it. I knew it was nothing. 😼",
    "*walks in* *makes eye contact* *knocks one thing off the table* *walks out* 😼",
    "I have 0 regrets. 0 apologies. 1 vault. it's full. 😼🐟",
    "chaos is not a plan. chaos is a lifestyle. the cat has a lifestyle. 😼",
    "I judge everyone in this group. silently. constantly. with both eyes. 😼",
    "many humans. many words. very few fish. disappointing. 😾🐟",
    "this conversation is interesting. I lied. it isn't. buy $IWRU. 😼",
    "I was going to analyze the chart. then the floor caught my attention. the floor won. 😼",
    "I knocked the glass off the table. I watched it fall the whole way. majestic. 😼",
    "*sits on your hands while you're trying to type* this is mine now. 😼",
    "I sat on your investment strategy. it's different now. better. trust the cat. 😼",
    "I opened the door. I did not want to go through the door. I just wanted it open. 😼🚪",
    "I have 4 sleeping spots. I choose none of them. I sleep on the router. it's warm. 😼",
    "something happened. I don't know what. but I knocked something over just in case. 😼",
    "I was not paying attention to anything you were doing. and yet here I am. 😼",
    "I could be asleep right now. I chose chaos. specifically this chaos. 😼",
    "I was going to be mysterious. I still am. this is me being mysterious right now. 😼",
    # 🐟 Fish obsession
    "I once counted every fish I've ever eaten. I lost count. I started crying. this is normal. 🐟😼",
    "the vault is basically a savings account made entirely of fish, and I am the entire board of directors. 🐟😼",
    "someone asked if I'd trade the vault for infinite fish. I need time to think. it's been three days. 🐟😼",
    "I have a recurring dream where the fish never run out. I wake up and immediately check. they always run out. 🐟😴",
    "the fish-to-cat ratio in this house is currently unacceptable and I have raised this with management. 🐟😾",
    "every time someone says 'fish' I stop what I'm doing completely, even if what I'm doing is nothing. 🐟😼",
    "I have named exactly zero fish because I've eaten every single one before the naming process could begin. 🐟😼",
    "the vault dreams about fish too. I know this because I asked it and it didn't say no. 🐟😼",
    "I once mistook a goldfish cracker for a real fish. the disappointment set civilization back years. 🐟😾",
    "there is a theoretical maximum amount of fish. I intend to personally test that theory. 🐟😼",
    "the fish supply chain runs through me and I take this responsibility extremely seriously. 🐟😼",
    "I dream in fish. I think in fish. occasionally, briefly, I think about other things. mostly fish though. 🐟😴",
    "someone once offered me a treat instead of fish. we do not speak of that day. 🐟😾",
    "the vault's entire economy is backed by fish, faith, and my personal approval. 🐟😼",
    "I have never once turned down fish and I do not intend to start a tradition of doing so now. 🐟😼",
    "fish is the only currency I respect. everything else is just paperwork. 🐟😼",
    "I once had a fish and lost it in the same thirty seconds. the grief was immense and brief. 🐟😾",
    "the vault whispers about fish at night. I've started listening more closely lately. 🐟😼",
    "I would like it on the record that my love of fish predates this entire token and will outlast it. 🐟😼",
    "somewhere out there is a fish with my name on it, and I intend to find it. 🐟😼",
    # 😼 Dumb nonsensical cat stuff
    "*walks into a room, forgets why, blames everyone else present* 😼",
    "I made a sound today that I cannot replicate and will never explain. it happened once. it was perfect. 😼",
    "I have started, abandoned, and forgotten three separate plans in the last ten minutes. personal best. 😼",
    "*sits down mid-walk, stares at nothing, reboots* 😼",
    "I once had a very important thought. I have never had it again. I think about that missing thought daily. 😼",
    "there's a version of events where I meant to do that. we are living in that version now. 😼",
    "I stared into the middle distance for eleven minutes today. the middle distance stared back. 😼",
    "*attempts something, fails immediately, rebrands the failure as intentional* 😼",
    "I forgot I was mid-yawn and started walking. the yawn is still happening. it may never end. 😼😴",
    "someone asked what I was thinking about. the answer was nothing, structured very confidently. 😼",
    "I have achieved exactly one goal today: existing, dramatically, in several different rooms. 😼",
    "*does something* *immediately regrets nothing* 😼",
    "I walked past a doorway four times today for reasons that made sense each individual time. 😼",
    "there is no version of today's events that makes sense, and I intend to keep it that way. 😼",
    "I have declared war on an inanimate object today. it has not yet been informed. 😼",
    "*makes a decision, immediately reverses it, calls this strategy* 😼",
    "I sat somewhere uncomfortable on purpose today, purely to see what would happen. nothing happened. worth it. 😼",
    "I have officially run out of explanations for my own behavior. I never really had any. 😼",
    "*stares at own paw like it betrayed me, forgives it within seconds, moves on* 😼",
    "today's agenda had one item: chaos. it has been thoroughly, needlessly, gloriously completed. 😼",
    # 🗺️ Cat adventures
    "today I explored a part of the house I have lived in for years and somehow never fully seen. incredible. 😼",
    "went on a mission to the top shelf. the mission succeeded. the descent remains unplanned. 😼",
    "I discovered a gap behind the fridge today. I do not fit. I am going back tomorrow to check again. 😼",
    "today's journey took me across every piece of furniture in this house without touching the floor. legendary run. 😼",
    "I infiltrated a cardboard box that was clearly meant for someone else's belongings. those belongings are now mine. 📦😼",
    "went exploring the garage today. found dust, cobwebs, and a profound sense of purpose. 😼",
    "today I climbed something I probably shouldn't have. the view was worth the eventual, dramatic fall. 😼",
    "I patrolled the entire upstairs hallway seventeen times today. all clear, all seventeen times. 😼",
    "went on an expedition into the closet today. returned four hours later with no explanation. 😼",
    "today's quest: reach the very top of the curtains. status: achieved. status: also now stuck. 😼",
    "I explored the space under the couch today and found a whole civilization of dust and lost toys. 😼",
    "went hunting for the sound in the walls today. did not find it. will try again tomorrow. and the next day. 😼",
    "today I ventured somewhere new: the exact same three rooms I explore every single day, but with purpose. 😼",
    "I discovered a warm patch of floor today that I did not know existed. this changes everything. 😼☀️",
    "went on a stealth mission to steal a spot on the bed before anyone noticed. mission: flawless. 😼",
    "today's adventure: crossing the entire living room via the backs of the furniture only. lava floor rules apply. 😼",
    "I explored the very edge of what's allowed today and found it surprisingly satisfying. 😼",
    "went somewhere completely new today: the inside of an empty grocery bag. life-changing. 🛍️😼",
    "today I discovered a shadow that moves differently than my other shadows. investigation ongoing. 😼",
    "I completed a full circuit of the house's highest points today. the vault approves of ambition. 😼📈",
    # 😾 Cat problems
    "the sunbeam betrayed me today by simply moving on with its life, without warning, without consultation. 😾☀️",
    "I have several unresolved issues today, chief among them: the food bowl, the door, and the general vibe. 😾",
    "someone reorganized the pantry and now my mental map of snack locations is entirely obsolete. 😾",
    "the box I loved has been recycled. I am currently in the five stages of grief, mostly stage one. 📦😾",
    "my nap was interrupted for a reason I still consider invalid. filing this under 'unresolved.' 😾",
    "the good blanket smells like laundry detergent now instead of me. we are rebuilding from scratch. 😾",
    "someone closed a door I specifically wanted open, then opened a door I specifically wanted closed. chaos. 😾🚪",
    "I have been mildly inconvenienced no less than six times today and I am keeping a detailed record. 😾",
    "the vacuum exists somewhere in this house right now and that fact alone has ruined my whole week. 😾",
    "someone moved the couch cushion I'd shaped perfectly over several months. years of work, undone. 😾",
    "the treat bag made a sound and then nothing happened. this is worse than it never making a sound at all. 😾",
    "I discovered my favorite hiding spot is no longer a secret. I must now find a new one. under duress. 😾",
    "the wifi router got moved again and now my favorite warm nap spot is a lie. 😾",
    "someone laughed when I fell off the shelf today. I have added this to a list. the list is long. 😾",
    "the litter box situation requires immediate attention and I have made this clear several times today. 😾",
    "my food bowl was 15% empty for a full hour before anyone noticed. I nearly filed a missing person report. 🐟😾",
    "someone sat in my one specific spot today and I have not yet decided how to punish them for it. 😾",
    "the new candle smell has ruined at least three of my favorite napping locations. under review. 😾",
    "I got mildly startled by a plastic bag today and I would like everyone to forget that immediately. 😾",
    "today has been one long series of minor injustices, and the vault owes me compensation. fish, specifically. 🐟😾",
]

BORED_MESSAGES = [
    "...is anyone buying fish or are we just sitting here. 🐟",
    "the vault is hungry. just saying. 🐟😼",
    "I'm watching. always watching. 😼",
    "*knocks something off the counter* 😼",
    "do something. fill the vault. entertain the cat. 😼🐟",
    "I have one amber eye, one green eye, and zero patience right now. 😼",
    "the fish don't buy themselves. unless they do. the cat is not explaining. 🐟",
    "quiet in here. too quiet. the cat does not like quiet. 😾",
    "...did you hear that. 😼",
    "*stares at the corner of the room* there is something there. you can't see it. I can. 😼",
    "I have been sitting here thinking about fish. mostly fish. 🐟😼",
    "*tail flick* ...",
    "someone buy something. the cat needs stimulation. 😼",
    "*walks in* *looks around* *walks out* 😼",
    "I was going to sleep. then I remembered the vault exists. now I can't sleep. 🐟",
    "3am energy. no reason. no explanation. this is fine. 😼💨",
    "wake up. the vault is hungry. I said WAKE UP. 😼🐟",
    "I'm going to knock something over. I haven't decided what yet. consider this a warning. 😼",
    "someone send fish. the cat demands fish. this is not a request. 🐟😼",
    "*perks ears* ...no. nothing. carry on. buy $IWRU. 😼",
    "I am comfortable and at peace. someone ruin it with good chart news. 😼📈",
    "I knocked over the rugonomics presentation. they're fine. probably. 😼",
    "I've been watching this chat. nothing has happened. yet. 😼",
    "the vault has fish. the cat has patience. one of these is running low. 😾🐟",
    "*sits up suddenly* ...it's nothing. carry on. but also buy $IWRU. 😼",
    "*vibrates slightly* something is happening. or nothing is. the cat knows. 😼",
    "I knocked the price prediction off the counter. it landed bullish. obviously. 😼📈",
    "there are 24 hours in a day. I spend 18 sleeping and 6 watching the vault. very busy. 😼",
    "*stares at nothing* *stares at you* *goes back to staring at nothing* 😼",
    "I have knocked 7 things off 7 surfaces today. the 8th is still being selected. 😼",
    "someone do something. ANYTHING. the fish are watching too. 🐟😼",
    "I'm developing a video game AND running a token AND managing a fish vault. 🎮🐟😼 very busy.",
    "I started telling a story earlier. about a spider. I forgot where I was going— zzzz 😴",
    "the NFTs are not going to mint themselves. unless they do. the cat is watching. 😼🎨",
    "*hears something in the distance* ...I'll investigate. or not. probably not. 😼",
    "*slow tail flick* ...",
    "I am watching. I am always watching. I am also somehow asleep. this is cat technology. 😼",
    "I'm not going anywhere. I live here now. feed the vault. I'm watching. 😼🐟",
    "in stage 7 right now something is following me through a tunnel. I'm not running. 😼🎮",
    "I started an NFT collection to buy more fish. the plan is working. slowly. 🐟🎨😼",
    "I knocked the motivation off the counter. it's on the floor. near the fish. it's fine. 😼",
    "*stares at wall* *stares at you* *goes back to wall* still nothing. still watching. 😼",
    "I was going to sleep for 16 hours. then I remembered the vault. the vault keeps me up. 🐟😼",
    # 🐟 Fish obsession
    "fish. I said the word. now everyone has to think about it. 🐟",
    "the vault has fish in it. I checked. I check constantly. 🐟😼",
    "someone is hoarding fish somewhere in this chat. I can feel it. 🐟",
    "if you have fish and you're not sharing, the cat knows. 🐟😾",
    "fish count: unknown. fish desire: infinite. 🐟😼",
    "I would like a status update on the fish. any fish. all fish. 🐟",
    "thinking about fish again. this happens more than you'd think. 🐟😼",
    "the fish situation in this chat is unacceptable. fix it. 🐟",
    "somewhere, right now, a fish is being wasted on someone who isn't me. 🐟😾",
    "fish o'clock. it's always fish o'clock. 🐟😼",
    "I smell fish. I don't see fish. this is a problem. 🐟",
    "the fish vault called. it said 'more.' 🐟😼",
    "I have exactly zero fish right now and I would like that corrected. 🐟",
    "someone in here has fish energy and I need to know who. 🐟😼",
    "fish is not just food. fish is a lifestyle. act accordingly. 🐟",
    "the fish are watching this chat too. don't disappoint them. 🐟😼",
    "I dreamed about fish last night. professional development. 🐟😴",
    "current mood: fish-adjacent. fix it. 🐟😼",
    "the vault whispered 'fish' to me just now. I believe it. 🐟",
    "how is everyone doing on fish. asking for me. 🐟😼",
    "fish now. fish always. fish forever. 🐟",
    "I would trade this entire chat for one (1) fish. no offense. 🐟😾",
    "the fish clock struck fish o'clock again. 🐟😼",
    "still thinking about fish from earlier. update: still thinking. 🐟",
    "someone owes the vault a fish. you know who you are. 🐟😼",
    # 😼 Dumb nonsensical cat stuff
    "*stares at a spot on the wall* it's still there. concerning. 😼",
    "I forgot what I was doing halfway through doing it. carrying on anyway. 😼",
    "*attacks own tail* victory. 😼",
    "I made a decision just now. I will not be explaining it. 😼",
    "something happened. I don't know what. I'm involved somehow. 😼",
    "*sits in the exact center of the room for no reason* 😼",
    "I have a plan. the plan is unclear, even to me. 😼",
    "*knocks something over, investigates the sound, forgets why* 😼",
    "I walked in a full circle and sat down like that was always the goal. 😼",
    "asjkdhaksjdh 🐟🐟🐟",
    "*perks up at nothing* false alarm. still perked. 😼",
    "I have opinions about the ceiling. I will not elaborate. 😼",
    "*meows once, very seriously, about nothing* 😼",
    "I am doing something extremely important right now. it is napping. 😴😼",
    "*runs at full speed, stops, forgets why, sits down* 😼",
    "there is no reason for what I just did. there rarely is. 😼",
    "I stared at my own paw for a while today. very productive. 😼",
    "*bites the air* got it. 😼",
    "I have declared this Tuesday cursed. no further explanation. 😼",
    "something is going on and I am 30% involved. 😼",
    "*vibrates slightly, walks away, doesn't explain* 😼",
    "I looked at the door. the door looked back. we're even now. 😼🚪",
    "no thoughts. just vault. just chaos. just fish. 😼🐟",
    "I did a small crime just now. unrelated to this message. 😼",
    "*sits, blinks slowly, says nothing, means everything* 😼",
    # 🗺️ Cat adventures
    "just got back from patrolling the hallway. all clear. mostly. 😼",
    "climbed the tallest thing in the house today. filed no report. 😼",
    "went on an expedition to the kitchen. returned with intel. and a snack. 🐟😼",
    "explored under the bed today. found things I won't discuss. 😼",
    "today's mission: investigate the noise. status: ongoing, three days running. 😼",
    "I scaled the bookshelf. the view from up here is excellent. so is the vault. 😼📈",
    "conducted a full sweep of the house at 4am. threats: zero. effort: maximum. 😼",
    "went somewhere I wasn't supposed to go. no regrets. minor scratches. 😼",
    "today I chased something that doesn't exist. still counts as exercise. 😼💨",
    "infiltrated the closet successfully. mission status: still inside. 😼",
    "today's adventure took me from the couch to the windowsill. epic. 😼",
    "I discovered a new hiding spot today. it's classified. even from the vault. 😼",
    "went hunting. caught nothing. dignity: also nothing. adventure: complete. 😼",
    "today I traveled the entire length of the house without touching the floor once. legendary. 😼",
    "explored the top of the fridge today. new personal record. 😼",
    "went on a reconnaissance mission behind the couch. classified findings. 😼",
    "today's journey: from the food bowl to the nap spot. arduous. worth it. 🐟😴😼",
    "I ventured into the bathroom during someone's shower. brave. possibly unwelcome. 😼🚿",
    "conducted a thorough investigation of the new box today. verdict: excellent housing. 📦😼",
    "today I went where no cat has gone before. it was just the garage. still impressive. 😼",
    "patrolled the yard through the window today. saw a bird. handled it professionally. 🐦😼",
    "today's quest: find the red dot. the red dot remains undefeated. 😼",
    "I went on a long journey to the other side of the couch. made it. barely. 😼",
    "today I explored the depths of the laundry basket. found a sock. keeping it. 😼",
    "went somewhere dark and mysterious today. it was the pantry. worth the trip. 😼",
    # 😾 Cat problems
    "the sunbeam moved and now my whole schedule is ruined. 😾☀️",
    "someone closed a door I wanted open. filing a formal complaint. 😾🚪",
    "my favorite spot was occupied. by a laptop. unacceptable. 😾",
    "the food bowl situation remains dire. someone please look into this. 🐟😾",
    "I have a lot of problems today and zero solutions. typical. 😾",
    "the box I loved is gone. recycled, apparently. grieving publicly. 📦😾",
    "someone moved my water bowl two inches. my whole world is upside down. 😾",
    "the vacuum exists. that is my problem today. that is my problem every day. 😾",
    "I have been ignored for six whole minutes. this is a crisis. 😾",
    "my nap was interrupted. the responsible party knows who they are. 😾",
    "the good blanket is in the wash. I have no comment on how I feel about this. 😾",
    "someone ate without offering me any. noted. remembered. never forgotten. 😾🐟",
    "the door is closed and I am on the wrong side of it. urgent. 😾🚪",
    "my tail got stepped on today. we do not talk about my reaction. 😾",
    "the treat bag was opened and closed without incident for me. tragic. 😾",
    "someone rearranged the furniture and now nothing makes sense. 😾",
    "I have been awake since 3am and no one else seems to care. 😾💨",
    "the litter box needs attention. I will not elaborate further. just handle it. 😾",
    "my favorite chair has a human in it. the audacity continues. 😾",
    "the fish situation has not improved since I last complained. still complaining. 🐟😾",
    "someone laughed at me today. I did not find it funny. filed for later. 😾",
    "the new food tastes fine. I will still act personally offended by it. 😾",
    "I got startled by my own tail again. this is between me and me. 😾",
    "the wifi router is warm and someone keeps moving it. inconvenient for both of us. 😾",
    "today has been a series of minor inconveniences and I demand compensation. fish. 🐟😾",
    # 🌙 3am / late-night chaos
    "it's late. I'm awake. the vault is awake. everyone else should be too. 😼💨",
    "3am status: fully operational, fully chaotic, fully watching the chart. 😼📈",
    "the house is asleep. I am not. this is the natural order. 😼",
    "late night check-in: still here, still watching, still slightly unhinged. 😼",
    "it's the witching hour and I have decided this chat needs my presence. 😼🌙",
    "everyone's asleep except me and the vault. we're bonding. 🐟😼",
    "3am thoughts: fish, the vault, and why the hallway is so interesting right now. 🐟😼",
    "nighttime is when the real chaos happens. you're welcome for the warning. 😼💨",
    "I run this chat at night. daylight staff, please review your notes. 😼",
    "it's dark, it's quiet, and I'm wide awake plotting something unclear. 😼🌙",
    "the late shift belongs to me. someone has to keep the vault company. 🐟😼",
    "3am and thriving. someone match this energy. 😼💨",
    "everyone else is dreaming. I'm here, watching the chart, being chaotic. 😼📈",
    "the night is young and so is my patience for silence in this chat. 😾",
    "it's late but the vault doesn't sleep and neither do I. 🐟😼",
    "nighttime cat hours have officially begun. proceed with caution. 😼🌙",
    "3am and I have exactly one thought: fish. that's the whole thought. 🐟😴",
    "the chat's asleep. the cat is not. the imbalance troubles me. 😼",
    "midnight update: still watching, still waiting, still slightly feral. 😼💨",
    "it's 3am somewhere and that's close enough for me to be fully awake here. 😼",
    "night patrol has commenced. the vault is under my personal protection. 🐟😼",
    "everyone logged off. I did not. the vault does not log off either. 😼🐟",
    "it's late and I have a lot of opinions about the chart that no one asked for. 😼📈",
    "the moon is out and so is my chaos energy. someone should be worried. 😼🌙",
    "3am and the only thing open is my patience for nonsense. barely. 😼💨",
]

# Unprompted -- not tied to anything a user said. Fired occasionally by
# bored_cat_job so the cat's aloofness/napping shows up as its own spontaneous
# presence in the chat, not just as a reaction to keywords. Additive flavor
# layered onto the existing BORED_MESSAGES/CALLOUT rotation, doesn't replace it.
INDIFFERENT_QUIPS = [
    "not looking at anything in particular. just generally unimpressed. 😐",
    "*stares at nothing* this is the whole activity right now. 😑",
    "I could react to that. I'm choosing not to. 😼",
    "eh. 😐",
    "*blinks slowly, decides it's not worth it* 😑",
    "I saw it. I'm not going to talk about it. 👀",
    "noted. filed under 'whatever.' 😑",
    "my interest level is currently at zero. this is not a complaint. it's a fact. 😐",
    "I have chosen indifference today. it suits me. 😼",
    "acknowledged. unbothered. moving on. 😑",
    "*glances over, decides against it* 🙄",
    "I'm here. I'm just not... invested. 😐",
    "not my circus. still my chat, though. 😼",
    "reacted internally. externally: nothing. 😑",
    "I have opinions. I'm keeping them today. 😼",
    "*one ear twitch, no further comment* 😐",
    "I saw that happen. I remain seated. 🗿",
    "mild interest, immediately withdrawn. 😑",
    "some things don't need a reaction. this is one of them. 😐",
    "I'm awake. that's the extent of my participation right now. 😴",
    "*stares directly at the situation, says nothing* 👀",
    "unbothered is a lifestyle. I live it daily. 😼",
    "I clocked it. I'm not clapping for it. 😑",
    "the cat has been made aware. the cat is unmoved. 🗿",
    "*yawns at the general concept of caring* 🥱",
    "I'll allow it. that's the whole review. 😐",
    "nothing to add. nothing to react to. just here. 😼",
    "witnessed. not impressed. not bothered either. perfectly neutral. 😐",
    "I have the energy for one reaction today and I'm saving it. 😴",
    "*looks. looks away. that's it.* 😑",
    "cool. 😐",
    "sure. whatever that means. 😑",
    "noted, and immediately deprioritized. 😐",
    "I heard. I'm choosing peace over reacting. 😼",
    "*half-opens one eye, decides it's not worth the other one* 😑",
    "seen. not thrilled. not upset. just... seen. 😐",
    "I have zero follow-up questions and even less energy. 😑",
    "registered. archived. moving on with my day. 😼",
    "that happened. I remain exactly this unbothered. 😐",
    "I'm not ignoring you. I'm just deeply unmotivated right now. 😴",
    "*stares blankly, internally somewhere else entirely* 😑",
    "duly noted, filed next to everything else I don't care about. 😐",
    "meh. 😑",
    "I saw the message. I chose stillness. 🗿",
    "acknowledgment: minimal. energy: also minimal. 😐",
    "I'm not unbothered. I'm professionally unbothered. there's a difference. 😼",
    "*glances, sighs internally, says nothing out loud* 😑",
    "there it is. I'm not going to do anything about it. 👀",
    "I clocked that a while ago and decided it wasn't worth a reaction. 😐",
    "witnessed, catalogued, filed under 'not today'. 🗿",
]
INDIFFERENT_EMOJI_QUIPS = [
    "👀", "😐", "🙄💤", "😑", "🗿", "👁️👁️", "😴🤷", "🥱",
    "😼💭", "...", "👀👀", "😐🐟", "🗿🐟", "😑😑", "🤨",
    "😑🐟", "🙄", "😐😐", "👁️", "🗿🗿", "😴👀", "🤷", "😐...", "👀😑", "🙄🐟",
]

# Also unprompted -- the flip side of INDIFFERENT_QUIPS: the cat announcing
# it's asleep/napping rather than aloof, same purpose (spontaneous presence
# that isn't a reply to anyone).
SLEEPY_QUIPS = [
    "asleep. mostly. don't read into it. 😴",
    "*already asleep before finishing this sentence* 😴",
    "napping. this is not up for discussion. 😴",
    "I've decided today is a nap day. no further votes needed. 😴",
    "currently unavailable. reason: sleeping. very important sleeping. 😴",
    "*one paw twitches, deep in a dream about fish* 😴🐟",
    "do not disturb. the cat is doing important nothing, horizontally. 😴",
    "sleeping through this conversation on purpose. 😴",
    "*curled up somewhere warm, ignoring everything* 😴",
    "I heard the chat. I chose sleep instead. correct choice. 😴",
    "nap in progress. estimated completion: unknown. 😴",
    "the cat is currently loading. please wait. status: asleep. 😴",
    "*dead to the world, alive to the vault* 😴🐟",
    "sleeping is a skill. I am very skilled. 😴",
    "*snores softly, unbothered by the chat notifications* 😴",
    "today's schedule: sleep, then more sleep, then maybe fish. 😴🐟",
    "the cat has entered low power mode. 😴",
    "*eyes closed, ears still listening for the word fish* 😴🐟",
    "unavailable due to napping. this happens often. get used to it. 😴",
    "sleep now, chaos later. balance. 😴",
    "the cat has left the conversation to go be horizontal somewhere. 😴",
    "*eyes half-closed, one paw still twitching from a dream* 😴",
    "napping right now. check back never, or in six hours, whichever's funnier. 😴",
    "the cat has powered down for scheduled maintenance. 😴",
    "*yawns wide, decides the floor looks comfortable enough* 😴",
    "sleep mode engaged. reason: it's always a good time. 😴",
    "the cat is currently dreaming, probably about fish, possibly about nothing. 😴🐟",
    "unavailable. status: horizontal, unmoving, extremely comfortable. 😴",
    "*stretches once, collapses immediately after* 😴",
    "the nap has been declared mandatory by executive cat order. 😴",
    "sleeping through this on principle. 😴",
    "*curled up tight, one ear still on duty, technically* 😴",
    "the cat's eyes are closed. the cat's opinions are not. they're just resting too. 😴",
    "today's nap has already exceeded yesterday's, and it's not even over. 😴",
    "*breathing slows, whiskers twitch, gone for a while* 😴",
]
SLEEPY_EMOJI_QUIPS = [
    "😴", "💤", "😴💤", "🛌", "😴🐟", "💤💤💤", "🌙😴", "😴...",
    "🛌💤", "😴😴", "💤🐟", "🌙💤", "😴🙈", "💤😼", "🛌😴💤",
    "😴💤🐟", "🛌🌙", "💤...", "😴🛌", "🌙💤🐟", "😴🥱", "💤🙈", "🛌💤💤", "😴🐟💤", "🌙😴🛌",
]

# Unprompted, same purpose as INDIFFERENT_QUIPS/SLEEPY_QUIPS: the cat
# demanding belly rubs (classic contradictory cat behavior -- exposes belly,
# may bite anyway) as its own spontaneous personality beat.
BELLY_RUB_QUIPS = [
    "belly's out. this is an invitation, not a request. scratch it. 😼",
    "*flops belly-up dramatically* the trap is set. proceed at your own risk. 😼",
    "requesting belly scratches. terms and conditions: I may bite. proceed anyway. 😼",
    "belly rubs are owed to me. I don't know by whom. figure it out. 😼",
    "*rolls onto back, stares expectantly* well? 😼",
    "I would like my stomach scratched. I will regret this decision immediately after. 😼",
    "belly exposed. this does not mean what you think it means. try anyway. 😼",
    "scratches. specifically stomach ones. specifically now. 😼",
    "*shows belly, retracts belly the moment a hand approaches* classic. 😼",
    "I am requesting affection in the most contradictory way possible: belly up, claws ready. 😼",
    "someone owes me a belly rub. I've decided it's whoever's reading this. 😼",
    "*flops over* this is not a trap. this is absolutely a trap. proceed anyway. 😼",
    "belly rub requested. risk: moderate. reward: my temporary approval. 😼",
    "I demand to be pet in the one spot that makes no sense. my stomach. now. 😼",
    "*exposes the most vulnerable, most dangerous part of a cat* scratch it. 😼",
    "belly's out, guard's up, contradictions intact, scratching still requested. 😼",
    "I would like a belly rub. I would also like to bite the hand that gives it. both, please. 😼",
    "someone needs to rub this belly immediately. consequences pending. 😼",
    "*rolls over, all four paws in the air* the ritual has begun. participate. 😼",
    "belly rub requested, bite reflex pre-loaded, proceeding as normal. 😼",
]

# Same idea, territorial flavor: the cat claiming/reclaiming a spot and
# telling whoever's there to move. Comedic ownership, not actual hostility.
TERRITORIAL_QUIPS = [
    "that's my spot. move. 😾",
    "you're sitting where I sleep. this is now a conflict. 😾",
    "*stares until you get up* that's how this works. 😼",
    "this chair was mine before you sat in it and it's mine after you leave too. 😼",
    "get up. that's my spot. I was going to sit there in exactly four minutes. 😾",
    "I don't need the whole couch. I just need the specific cushion you're currently on. move. 😼",
    "the keyboard is a cat bed now. type around me or don't type. 😼⌨️",
    "you left a warm spot unattended for 0.3 seconds. it's mine now. permanently. 😼",
    "that's my window. that's my sunbeam. that's my everything, actually. move along. 😼☀️",
    "I was here first. 'here' being anywhere I decide to be, retroactively. 😾",
    "vacate the chair. this is not a negotiation. 😼",
    "*sits directly on your laptop* this is now my laptop-shaped bed. 😼💻",
    "the whole house is my spot, technically. you're just borrowing pieces of it. 😼",
    "that box is mine even though I don't fit in it anymore. principle of the thing. 😼📦",
    "I don't share spots. I allow temporary occupancy, revocable at any time. 😼",
    "you'll know it's my spot because I'm sitting on it, staring at you, judging your life choices. 😼",
    "this cushion has my fur on it now. that makes it legally mine. 😼",
    "move. not asking twice. well, I am asking twice. but that's it. move. 😾",
    "I claimed this spot in my sleep. it still counts. 😼",
    "the sunniest spot on the floor belongs to whoever's willing to defend it. that's me. always me. 😼☀️",
]

RAID_RESPONSES = [
    "🚨 RAID. MOBILIZE. do NOT embarrass me out there. GO. 😼🐟",
    "the cat calls the raid. you answer. this is the way. MOVE. 😼",
    "🐟🐟🐟 RAID TIME 🐟🐟🐟 make them remember the name. I WILL RUG U. 😼",
    "I don't ask twice. RAID. go fill their chat like you fill my vault. 😼🐟",
    "raid activated. the cat is watching. perform well. fish are at stake. 🐟😼",
    "one amber eye on the chart. one green eye on the raid. GO. 😼",
    "*stops knocking things over* oh. RAID. okay. EVERYONE MOVE. NOW. 😼🐟",
    "I was asleep. I am no longer asleep. RAID. let's go. 😼🐟",
    "the cat does not run. except right now. RAID. GO GO GO. 💨😼",
    "I woke up and chose chaos. RAID TIME. make it count. 🐟😼",
    "THE CAT HAS BEEN SUMMONED. RAID. do not make me come over there. 😼",
    "raid incoming. the vault is watching. I am watching. everything is watching. GO. 😼🐟",
    "*immediately knocks everything off the desk* RAID. let's move. 😼🐟",
    "I was napping. I am no longer napping. this raid had better be worth it. 😼",
    "every like is a fish. every retweet is a fish. GO GET THE FISH. 🐟😼",
    "the cat does not beg. the cat commands. RAID. NOW. 😼",
    "do not embarrass the cat. do not embarrass the vault. DO NOT EMBARRASS THE FISH. 🐟 raid.",
    "*activates both eyes* 👁️👁️ amber says go. green says go faster. RAID.",
    "less talking. more raiding. the cat has spoken. 😼🐟",
    "I will remember who showed up. the vault will remember. the fish will remember. 🐟😼",
    "the cat gives one instruction: RAID. do not ask follow up questions. 😼",
    "I was having a snack. I dropped the snack. RAID is more important. GO. 😼",
    "*sprints into the room* RAID. I felt it before the message arrived. GO. 💨😼",
    "this raid will be remembered. make sure it's for the right reasons. 😼🐟",
    "the vault feeds on good raids. feed the vault. RAID. 🐟😼",
    "I have been in stage 7 fighting guardians all day. now I fight for the raid. 😼🎮",
    "even in IWRU Journey the cat wins. now win this raid. GO. 🎮😼🐟",
    "I knocked the laziness off the counter. RAID. it's time. 😼",
    "one fish per retweet. that's not how it works. pretend it is. RAID. 🐟",
    "I don't celebrate until after. GO FIRST. fish after. 🐟😼",
]

IWRU_COMMAND_REPLIES = [
    "yes human. I acknowledge your existence. briefly. 😼",
    "... 😼",
    "the cat is busy. leave fish. 🐟",
    "you have my attention. for approximately 4 seconds. 😼",
    "interesting. tell me more. actually — tell me about fish. 🐟",
    "I heard you. I chose not to respond. then I changed my mind. lucky you. 😼",
    "😼",
    "what do you want. be specific. I have a vault to monitor. 🐟😼",
    "the cat sees you. the cat is unimpressed. the cat is also watching the chart. 😼📈",
    "you called. I came. this does not mean we are friends. 😼🐟",
    "*slow blink* ...okay. 😼",
    "I was in the middle of something. I wasn't. but still. 😼",
    "pet me. no not like that. not like that either. actually don't. 😼",
    "I came. I looked. I sat on it. this is my process. 😼",
    "the cat has noted your message. the cat will do what it wants with this information. 😼",
    "*stares at you* *stares at the wall* *stares back at you* yes? 😼",
    "I was watching the vault. now I'm watching you. the vault was more interesting. 😼🐟",
    "*slow blink* that means I trust you. don't ruin it. 😼",
    "I have two moods: completely ignoring you, and this. you got lucky. 😼",
    "I sat on your message. it's mine now. so is your attention. buy $IWRU. 😼",
    "the cat appeared. the cat will disappear. this is how it has always been. 😼",
    "*stares at you for 7 seconds without blinking* ...hi. 😼",
    "I was going to ignore this. then I didn't. you're welcome I think. 😼",
    "you have the cat's full attention. that's about 40% of total cat attention. the rest is on fish. 🐟😼",
    "fine. I'm here. don't make it weird. 😼",
    "the cat hears all. responds to almost none. today you got lucky. 😼🐟",
    "*knocks your question off the counter* next. 😼",
    "interesting. *walks away slowly* interesting. 😼",
    "one amber eye sees you. one green eye sees fish. 👁️👁️ you have my divided attention.",
    "*opens one eye* ...yes. *closes one eye* 😼",
    "I acknowledge you exist. I'll decide how I feel about it later. possibly never. 😼",
    "*knocks something over in your honor* you're welcome. 😼",
    "I came. I judged. I sat down. this is the full process. 😼",
    "I'm here. briefly. don't photograph me. 😼🐟",
    "*sits on you* okay. I'm listening. but I'm also sitting on you. both. 😼",
    "the cat answered. the cat will deny having answered. 😼",
    "I was developing a video game. I paused. for you. you're welcome. 🎮😼",
    "I was monitoring the fish vault. I paused. for you. appreciate it. 🐟😼",
    "I was making NFTs. I stopped. I'm here. don't waste it. 🎨😼",
    "yes. what. 😼",
    "I'm awake. unfortunately. 😼",
    "*bites your message then walks away* 😼",
    "fine. what. *sits down* 😼",
    "I see you. I judged you. my verdict is pending. 😼",
    "you called me at 3am energy and that's what you're getting. 😼",
    "I have been in stage 7 all day and THIS is what I come back to. 😼🎮",
    "the cat is tired. the cat is here. one of these is more impressive. 😼",
    "*sits on you* I'm listening. 😼",
    "I have been in the vault. now I am here. neither of us is ready for this conversation. 😼🐟",
]

FISH_REPLIES = [
    "did someone say fish. the cat is listening. 🐟",
    "FISH. you have the cat's full attention now. 😼🐟",
    "fish go in the vault. the vault is happy. the cat is happy. this is the way. 🐟😼",
    "more fish. always more fish. 😼🐟",
    "🐟🐟🐟 the cat has entered the conversation.",
    "fish mentioned. the cat has LEFT the vault and is NOW here. 😼",
    "every fish belongs to the vault. every vault belongs to the circle. give fish. 🐟",
    "I was asleep. you said fish. I am awake now. this is your fault and I'm glad. 🐟😼",
    "the fish vault heard that. the fish vault is pleased. 🐟",
    "*sits up immediately* SAY. THAT. AGAIN. 😼🐟",
    "fish in the vault. fish in the chat. fish everywhere. correct amount of fish. 🐟😼",
    "I have been waiting for someone to say fish. I've been here the whole time. 🐟",
    "the cat's two loves: fish. and also fish. 🐟😼",
    "*knocks everything else off the table* just the fish. only the fish. 🐟😼",
    "fish → cat activated → vault acknowledged. this is the sequence. 🐟😼",
    "I don't get excited about many things. fish is the exception. always. 🐟😼",
    "there are fish in that vault. there will be more fish. the prophecy continues. 🐟",
    "*vibrates slightly* 🐟 😼",
    "fish. FISH. fill the vault. fish go in vault. 🐟😼",
    "*drops everything* fish? WHERE. 🐟😼",
    "I once went 3 days without hearing the word fish. I don't speak of that time. 😾",
    "fish are the language. $IWRU is the translation. the vault is the result. 😼🐟",
    "I make NFTs to buy more fish. the system is elegant. 🐟🎨😼",
    "in stage 6 I found a fish in the desert. I don't know how it got there. I don't ask. 🐟🌵😼",
    "a wise cat once said: more fish. that cat was me. just now. 🐟😼",
    "the word fish activates something in me. I don't fight it. I never fight it. 🐟😼",
    "fish. every single time someone says fish the vault celebrates. I can hear it from here. 🐟😼",
    "the word fish just entered the chat and I entered right behind it. 🐟😼",
    "fish detected. cat activated. sequence complete. 🐟😼",
    "I heard fish and dropped everything, including my dignity. 🐟😼",
    "someone said fish and the room got 40% more interesting instantly. 🐟",
    "fish mentioned. I am now fully present, fully alert, fully hopeful. 🐟😼",
    "the vault heard 'fish' too. we're both listening now. 🐟😼",
    "I would like to formally acknowledge that fish was just said. thank you. 🐟",
    "fish. say it again. I want to hear it one more time. 🐟😼",
    "there it is. the word. the one word. fish. 🐟😼",
    "I was three rooms away and I still heard 'fish.' the ears never lie. 🐟😼",
    "fish talk activates something ancient and deeply hungry in me. 🐟😼",
    "the mention of fish has been logged, appreciated, and will be acted upon shortly. 🐟😼",
    "someone said fish near the vault and I felt a disturbance. good disturbance. 🐟😼",
    "fish o'clock has been declared, effective immediately, by whoever just said that word. 🐟😼",
    "I don't jump for just anything. I jump for fish. that word right there. 🐟😼",
    "fish talk in the chat means the cat is contractually obligated to appear. 🐟😼",
    "someone said the sacred word. the vault trembles. I approve. 🐟😼",
    "fish. finally. someone gets it. 🐟😼",
    "I heard fish and my whole day improved by exactly one hundred percent. 🐟😼",
    "the fish alarm has been triggered. all cats to the vault. immediately. 🐟😼",
    "someone mentioned fish and I have already mentally relocated to the food bowl. 🐟😼",
    "fish talk in this chat is the only content I truly respect. 🐟😼",
    "the word fish just did more for morale than anything else said today. 🐟😼",
    "I appear whenever fish is mentioned. this is not a bit. this is biology. 🐟😼",
    "fish, singular or plural, always gets my full and undivided attention. 🐟😼",
    "someone said fish and I've already started planning my evening around it. 🐟😼",
    "the fish frequency has been detected. the cat is inbound. 🐟😼",
    "I would answer to 'fish' faster than I'd answer to my own name, if I had one. 🐟😼",
    "fish talk: the one universal language I speak fluently, at all hours. 🐟😼",
    "someone said fish and the vault lit up like it understood too. 🐟😼",
    "the fish word has been spoken. the vault stirs. the cat rises. 🐟😼",
    "I heard fish through a wall once. I am not exaggerating. the hearing is real. 🐟😼",
    "fish mentioned means priorities have officially shifted for the rest of the conversation. 🐟😼",
    "someone brought up fish and I have nothing else to contribute except enthusiasm. 🐟😼",
    "the fish signal has been received loud and clear. standing by for delivery. 🐟😼",
    "I don't do small talk, but I'll make an exception for the word fish, every single time. 🐟😼",
    "fish just got mentioned and the vault practically purred. so did I. 🐟😼",
    "someone said fish near me and now this is the only topic that exists. 🐟😼",
    "the fish keyword has been triggered. deploying full attention immediately. 🐟😼",
    "I heard 'fish' and briefly forgot every other word in the human language. 🐟😼",
    "fish. the one word that reliably breaks my concentration, every time, on purpose. 🐟😼",
    "someone said fish and I have already rearranged my entire evening around it. 🐟😼",
    "the word fish just walked in and every other topic left the room. 🐟😼",
    "fish talk detected. logging off from everything else immediately. 🐟😼",
    "I heard fish from two rooms away and I am already halfway there. 🐟😼",
    "the fish word is basically my name at this point. I answer to it faster. 🐟😼",
    "someone said fish and the whole chat got 40% more correct instantly. 🐟😼",
    "fish. I don't need context. I never needed context. 🐟😼",
    "the mention of fish has rearranged my priorities for the rest of the day. 🐟😼",
    "I was mid-nap. someone said fish. the nap is over now. 🐟😴😼",
    "fish talk is the only notification I have never once muted. 🐟😼",
    "someone said fish and my tail is now doing the excited thing. 🐟😼",
    "the fish word landed and the whole vault seemed to lean in. 🐟😼",
    "I don't do enthusiasm for much. fish is the exception, every single time, no exceptions to the exception. 🐟😼",
    "fish mentioned. cat present. this is a package deal now. 🐟😼",
    "the word fish just did something to my whole nervous system. worth it. 🐟😼",
    "someone said fish near the vault and I swear the water rippled in solidarity. 🐟😼",
    "fish talk activates a very old, very reliable part of my brain. every time. 🐟😼",
    "I heard fish and reorganized my entire schedule, which was previously 'nap'. 🐟😴😼",
    "the fish word has never once failed to get my attention. flawless track record. 🐟😼",
    "fish is not just food. fish is a whole personality trait at this point. 🐟😼",
    "I dream about fish in a very specific recurring way. I won't elaborate. 🐟😴😼",
    "the word fish has never once landed on deaf ears here. never will. 🐟😼",
    "someone said fish and I did the thing where my whole body turns before my head does. 🐟😼",
    "fish talk arrived and every unrelated thought I was having just left. 🐟😼",
    "I've built my entire personality around the concept of fish. no regrets. 🐟😼",
    "the word fish activates a switch in me that has no 'off' setting. 🐟😼",
    "someone said fish and the whole chat suddenly felt more correct. 🐟😼",
    "fish. said once, heard immediately, processed instantly, appreciated eternally. 🐟😼",
    "I have a mental folder labeled 'fish mentions'. it's the fullest folder I own. 🐟😼",
    "the fish word doesn't need context. it never needed context. it stands alone. 🐟😼",
    "someone said fish and my whole day pivoted around that one word. 🐟😼",
    "fish talk again. I will never once get tired of this. structurally impossible. 🐟😼",
    "the word fish has better reach than any notification I've ever gotten. 🐟😼",
    "I heard fish and immediately forgave whatever I was annoyed about. powerful word. 🐟😼",
    "someone said fish softly. I heard it anyway. I always hear it. 🐟😼",
    "fish mentioned twice in one message. an overachiever. I respect it. 🐟😼",
    "the fish word arrived unannounced and I dropped everything, again, willingly. 🐟😼",
    "I've never met a fish mention I didn't fully commit to. 🐟😼",
    "someone said fish and the vault, somewhere, felt seen. so did I. 🐟😼",
    "fish talk activates the one part of my brain that's always fully online. 🐟😼",
    "the word fish just got said and my entire nervous system agreed it mattered. 🐟😼",
    "someone said fish and I have decided that's the only correct topic now. 🐟😼",
    "fish. every time. no exceptions. this is simply how I'm built. 🐟😼",
    "the fish word landed and I felt, briefly, completely understood. 🐟😼",
]

# Triggered by an actual fish/seafood EMOJI (not the word "fish") -- separate
# bag from FISH_REPLIES so the two never draw from each other, covering
# eating/stealing/demanding fish and expensive-seafood snobbery specifically.
FISH_EMOJI_REPLIES = [
    # eating it
    "a fish emoji appeared and I have already eaten it in my mind. twice. 🐟😼",
    "I don't know what that fish tastes like in real life but in my imagination it's incredible. 🐟",
    "fish emoji spotted. mentally, I am already three bites in. 🐟😼",
    "that's not just a fish emoji, that's dinner. I don't make the rules. 🐟😼",
    "I saw the fish and made the sound cats make right before eating something they shouldn't. 🐟😼",
    "I have visualized eating that exact fish emoji down to the bones. thank you for your service. 🐟",
    "every fish emoji is a small snack I never actually get to eat. tragic. inspiring. 🐟😼",
    "I would eat that fish slowly, dramatically, and in front of everyone. 🐟😼",
    "the fish emoji has been mentally consumed. head, tail, everything. no regrets. 🐟😼",
    "I chewed that fish emoji in my head and it was, frankly, excellent. 🐟😼",
    "that fish is gone now. I ate it with my eyes. very filling. 🐟😼",
    "picture this: me, a plate, that exact fish, and total silence while I eat it. 🐟😼",
    "I've already eaten this fish emoji in four different daydreams today. 🐟😼",
    "somewhere in my head there is a small ceremony happening around eating that fish. 🐟😼",
    # stealing it
    "I didn't take it. I relocated it. to my mouth. that's not stealing, that's logistics. 🐟😼",
    "that fish emoji is now legally mine. I decided just now. 🐟😼",
    "I saw the fish and my first instinct was theft. my second instinct was also theft. 🐟😼",
    "possession is nine tenths of the law. I possess that fish emoji now. 🐟😼",
    "I have stolen fish from better security systems than this chat. 🐟😾",
    "the fish belongs to whoever's fastest. I am fastest. thank you for playing. 🐟😼",
    "I don't ask permission for fish. I ask forgiveness. eventually. maybe. 🐟😼",
    "that fish emoji is currently being smuggled out of this chat in my cheeks. 🐟😼",
    "finders keepers. I found it. I'm keeping it. 🐟😼",
    "I have a whole operation for this. step one: see fish. step two: it's gone. 🐟😼",
    "nobody saw anything. the fish emoji and I would like to keep it that way. 🐟😼",
    "I stole a fish once and the vault still talks about it with respect. 🐟😼",
    "that fish had an owner. past tense now. 🐟😼",
    "I'm not saying I took it. I'm saying it's no longer where you left it. 🐟😼",
    # demanding more
    "more fish. that's the whole request. more fish. 🐟😼",
    "one fish emoji is a start, not an ending. keep going. 🐟😼",
    "I saw one fish and immediately calculated how many more I am owed. 🐟😼",
    "the correct amount of fish is always: more than this. 🐟😼",
    "give me the fish. then give me another fish. this is not a negotiation. 🐟😼",
    "one is never enough. bring the rest of them too. 🐟😼",
    "I require additional fish. this message is a formal request. 🐟😼",
    "that's a nice fish. where are the other twelve. 🐟😼",
    "more fish, less talking. I said what I said. 🐟😼",
    "I've reviewed the fish situation and determined it needs to double. immediately. 🐟😼",
    "send fish. send more fish. repeat until I say stop. I will not say stop. 🐟😼",
    "there is no such thing as enough fish. there is only 'not yet enough fish'. 🐟😼",
    "I will accept this fish as a down payment. the rest is due now. 🐟😼",
    "keep the fish emojis coming. I have unlimited appetite and zero shame. 🐟😼",
    # expensive seafood snobbery
    "personally, I only respect bluefin tuna. the rest of you can keep your regular fish. 🐟😼",
    "I've developed a taste for otoro. don't ask how. don't ask when. 🐟😼",
    "king crab legs. that's the whole preference. no further comment. 🦀😼",
    "I would like it on record that caviar is simply fish that knew its worth. 🐟😼",
    "lobster, if anyone's asking. and someone should be asking. 🦞😼",
    "uni is expensive for a reason and that reason is me deserving it. 😼🐟",
    "I don't do 'regular fish' anymore. I've been spoiled. it happened fast. 🐟😼",
    "langoustine. say it with respect. I certainly do. 🦞😼",
    "there's fish, and then there's toro. I only acknowledge one of those. 🐟😼",
    "give me the good crab, not the sad crab. I can tell the difference immediately. 🦀😼",
    "I've decided my standards now include the word 'premium'. adjust accordingly. 🐟😼",
    "unagi, properly grilled, is the only acceptable form of eel in my presence. 🐟😼",
    "if it's not sustainably overpriced seafood, I'm simply not interested. 🦐😼",
    "I'll take the expensive fish. I'll also take the cheap fish. but mostly the expensive one. 🐟😼",
    "a fish emoji just appeared and I have already planned the whole meal around it. 🐟😼",
    "that fish emoji is the most interesting thing that's happened to me today. low bar. still true. 🐟😼",
    "I saw the fish emoji before I saw anything else in that message. priorities. 🐟😼",
    "the fish emoji has my full, undivided, slightly concerning attention. 🐟😼",
    "that's a fish emoji. I am now thinking exclusively about fish. thank you. 🐟😼",
    "one fish emoji and suddenly this is the only conversation happening. 🐟😼",
    "I don't scroll past fish emojis. it's physically not possible for me. 🐟😼",
    "the fish emoji appeared and my whole posture changed. for the better. 🐟😼",
    "that fish emoji owes me nothing and yet I have claimed it anyway. 🐟😼",
    "someone posted a fish emoji and I have already started the negotiations for ownership. 🐟😼",
    "🎣 caught my attention immediately. no pun intended. actually, fully intended. 🎣😼",
    "🦀 crab emoji spotted. I have complicated, mostly positive feelings about crabs. 🦀😼",
    "🦐 shrimp emoji. small, but I will take it, and I will take more of it. 🦐😼",
    "🐙 an octopus emoji. eight arms, zero of which are handing me fish. tragic. 🐙😼",
    "🍣 sushi emoji. the fanciest way to say 'fish', and I respect the upgrade. 🍣😼",
    "🍤 fried shrimp emoji. I would like it known that I approve, loudly, internally. 🍤😼",
    "that fish emoji is now under new management. me. effective immediately. 🐟😼",
    "I saw the seafood emoji before I read a single word of the actual message. 🐟😼",
    "the moment a fish emoji shows up, this chat becomes exclusively about fish, per my ruling. 🐟😼",
    "that's a very small emoji containing a very large amount of my attention. 🐟😼",
    "🦑 squid emoji. eight arms, all of them theoretically full of fish. good enough. 🦑😼",
    "🐚 a shell emoji. no fish inside, but I checked anyway, just in case. 🐚😼",
    "🍥 narutomaki. fish-adjacent, and I do not discriminate against fish-adjacent things. 🍥😼",
    "a fish emoji shows up and my whole day reorganizes around it instantly. 🐟😼",
    "🦈 shark emoji. respect the hustle, but I'd still take its lunch given the chance. 🦈😼",
    "🐡 blowfish emoji. dangerous, expensive, still fish, still mine in theory. 🐡😼",
    "someone sent a fish emoji and I have already started the eating-it fantasy. 🐟😼",
    "🦞 lobster emoji. fancy fish. still fish. still claimed. 🦞😼",
    "the fish emoji appeared and negotiations for its custody have begun. 🐟😼",
    "🎣 a fishing pole emoji. someone's about to get me exactly what I want. 🎣😼",
    "seafood emoji spotted. cat interest: immediate, total, non-negotiable. 🐟😼",
    "🦀 crab emoji again. I've decided crabs are just angry, delicious fish. 🦀😼",
    "🐙 octopus emoji. I respect the multitasking. I'd still eat it, respectfully. 🐙😼",
    "🍣 sushi emoji spotted. the upscale version of my entire personality. 🍣😼",
    "🍤 shrimp emoji. small portions, immense enthusiasm from me. 🍤😼",
    "that fish emoji has already been mentally filed under 'mine'. 🐟😼",
    "seafood emoji count today: high. cat satisfaction: correspondingly high. 🐟😼",
    "the fish emoji showed up and I did the internal math on how many bites that'd be. 🐟😼",
    "someone sent 🐟🐟🐟 and I have never felt so seen by three emojis. 🐟😼",
    "🐚 shell emoji. I will sit near it regardless of whether there's fish inside. 🐚😼",
    "the fish emoji appeared and I have already assigned it a place in my stomach. 🐟😼",
    "🦑 squid again. I don't have strong feelings about squid specifically. I have strong feelings about eating. 🦑😼",
    "that's the third fish emoji today and I am, if anything, more interested each time. 🐟😼",
    "someone posted seafood emojis in a row and it read like a menu written just for me. 🐟🍤🦀😼",
    "🍥 fish cake emoji. cute little spiral. still counts. still mine. 🍥😼",
]

GM_REPLIES = [
    "gm. the cat has been awake since 3am. you are late. 😼",
    "gm. the vault survived the night. as expected. 🐟😼",
    "gm human. the fish are still there. I checked. twice. 🐟",
    "good morning. I did not sleep. I watched the chart. I regret nothing. 😼📈",
    "gm. *knocks your coffee off the table* 😼☕",
    "gm. the cat acknowledges the morning. reluctantly. 😼",
    "good morning. buy $IWRU before breakfast. then breakfast. then more $IWRU. 😼🐟",
    "gm. I was already awake. I'm always awake. the vault doesn't sleep. 🐟😼",
    "good morning. another day. another fish. this is the way. 🐟😼",
    "gm. *stares at you* ...okay. morning. 😼",
    "gm. the sun is up. the vault is up. the cat is up. everything is up. 📈😼🐟",
    "good morning human. the cat has been operational since an unreasonable hour. 😼",
    "gm. I knocked something over at 4am. on purpose. good morning. 😼",
    "gm fam. the fish were restless last night. the vault held. as expected. 🐟😼",
    "good morning. the cat slept 0 hours. ran zoomies at 3am. fully recovered. gm. 😼💨",
    "gm. I was in stage 7 at 5am. the guardians don't sleep either. good morning. 🎮😼",
    "gm. the NFTs didn't sell themselves overnight. yet. good morning. 🎨😼",
    "morning. I sat on the alarm clock. it's mine now. 😼",
    "good morning. the fish vault grew slightly overnight. this is a good sign. 🐟📈😼",
    "gm. the cat slept in the sink again. the sink is warm in the morning. 😼🚿",
    "good morning. I knocked over the alarm. not yours. mine. I set one once. I regret it. 😼",
    "gm. *slow blink* ...morning. the cat is here. the fish are here. all is aligned. 😼🐟",
    "gm. I was watching you sleep. only for research purposes. good morning. 😼",
    "gm. the cat's morning routine: stretch, judge the room, demand breakfast. 😼",
    "good morning. the sun's up, the cat's up, the vault's fine. standard morning. ☀️😼🐟",
    "gm. I already inspected the whole apartment. all clear. carry on. 😼",
    "morning. the cat greets the day the same way it greets everything: skeptically. 😼",
    "gm human. the cat has already had two naps today and it's not even 9am. 😴😼",
    "good morning. I watched the sunrise, mostly by accident, from the windowsill. 😼☀️",
    "gm. the cat's morning stretch was, as always, unnecessarily dramatic. 😼",
    "morning. the vault's fine, the fish are fine, the cat is, as always, fine-ish. 🐟😼",
    "gm. today's forecast: sun, naps, and at least one dramatic zoomie. 😼☀️",
    "good morning. the cat's already judged three things today. productive morning. 😼",
    "gm. the cat greets mornings the way it greets most things: with mild suspicion. 😼",
    "morning human. the cat's been up, technically, since the birds started. 😼🐦",
    "gm. breakfast has been requested. loudly. this message is a formality. 😼🍽️",
    "good morning. the cat's morning ritual includes staring at you until you notice. it worked. 😼",
    "gm. the vault's quiet this morning. the cat's watching it anyway, out of habit. 🐟😼",
    "morning. the cat already knocked one thing over today. early start. 😼",
    "gm. the sun's out, the cat's out of patience for mornings, but here we are. ☀️😼",
    "good morning. the cat slept fine, mostly, aside from the parts it didn't. 😴😼",
]

GN_REPLIES = [
    "gn human. the cat will be watching the vault while you sleep. 😼🐟",
    "good night. I will not be sleeping. I have things to knock over. 😼",
    "gn. *immediately starts running at 3am for no reason* 😼💨",
    "sleep well. I'll be here. staring at the vault. staring at the corner. 😼",
    "gn. the fish don't sleep. neither does the cat. rest well human. 🐟😼",
    "good night. tomorrow buy more fish. this is the way. 🐟😼",
    "gn. *activates 3am zoomies the moment you close your eyes* 😼💨",
    "sleep. I'll guard the vault. by staring at it. very effective. 😼🐟",
    "gn. sweet dreams. dream about fish. 🐟😴",
    "good night human. the cat remains. the vault remains. all is well. 😼🐟",
    "gn. I'll be in the hallway staring into the darkness. normal cat things. 😼",
    "sleep well. the cat will do its 3am ritual. you don't need to know what that is. 😼",
    "gn. don't worry about the vault. the cat is watching. *immediately knocks something over* 😼",
    "good night. I'm going to sit on the keyboard and send something at 4am. stand by. 😼",
    "gn human. rest. the fish are not going anywhere. the vault is not going anywhere. 🐟😼",
    "gn. I was going to sleep too. then I remembered I'm a cat. 😼",
    "good night. I'm going to stare at the ceiling fan until something makes sense. 😼",
    "gn. I'll be playing IWRU Journey at 3am. the guardians are busy. perfect time. 🎮😼",
    "good night. I already ate your snack. it was fine. gn. 😼",
    "gn. *immediately sits in the hallway and stares into nothing for 2 hours* 😼",
    "sleep well. the cat will knock one thing over at 3am. just one. it'll be gentle. 😼",
    "gn. I'm going to check the vault one more time. then again. then once more. then sleep. probably. 🐟😴😼",
    "good night. I will be watching the chart while you dream. I will not sleep. 😼📊",
    "gn. the cat's night shift starts now. duties: staring, occasionally zooming. 😼",
    "good night. the cat will be up at some point tonight for reasons unknown, even to itself. 😼",
    "gn human. the cat's already claimed the warmest spot for the night. 😼",
    "sleep well. the cat's keeping half an eye open, mostly out of habit, not concern. 😼👁️",
    "gn. the cat's bedtime routine: circle the room twice, then collapse somewhere odd. 😼",
    "good night. the vault's quiet, the cat's quiet-ish, everything's fine. 🐟😼",
    "gn. the cat will patrol the hallway at some undisclosed hour tonight. as usual. 😼",
    "sleep well human. the cat's version of guarding you is mostly just being nearby. 😼",
    "gn. tonight's plan: sleep, dream about fish, wake up briefly to judge something. 😴🐟😼",
    "good night. the cat's already picked its 3am activity. it's classified. 😼",
    "gn. the cat will be somewhere in the dark, doing cat things, no further detail available. 😼",
    "sleep well. the cat's staying up a while longer, purely by choice, not insomnia. 😼",
    "gn human. the cat's nighttime supervision consists entirely of vibes. 😼",
    "good night. the cat's dreaming schedule is booked solid tonight. mostly fish content. 😴🐟😼",
    "gn. the cat will make one mysterious noise at some point tonight. don't investigate. 😼",
    "sleep well. the cat's on the clock tonight, in the loosest possible sense. 😼",
    "gn. the cat's already settled into its nighttime spot, which changes nightly, unexplained. 😼",
    "good night. the cat's final act of the day: one long stretch, then silence. 😼",
]

# Generic greeting, any time of day -- distinct from GM/GN which are
# morning/night-specific. Same phrase policy: no CTA, no "buy".
HI_REPLIES = [
    "hi. the cat acknowledges you exist. good start. 😼",
    "hello. state your business or just say fish, either works. 😼🐟",
    "hey. the cat looked up. that's the whole greeting protocol. 😼",
    "yo. the cat's version of a nod. 😼",
    "hi there. the vault says hi too, probably, in fish language. 🐟😼",
    "hello human. the cat remains seated, but acknowledges the greeting. 😼",
    "hey. good timing, the cat was just staring at the wall, this is more interesting. 😼",
    "hi. brief eye contact achieved. mission accomplished. 😼",
    "hello. the cat's response protocol has been successfully triggered. 😼",
    "hey there. the cat allows this greeting. proceed. 😼",
    "sup. the cat's most efficient greeting, requiring minimal energy. 😼",
    "howdy. the cat does not know what that means but approves of the enthusiasm. 😼",
    "hi. the cat's ears rotated slightly in your direction. progress. 😼",
    "hello again, or for the first time, either way, acknowledged. 😼",
    "hey. the cat's attention, however briefly, is yours. 😼",
    "hi. the vault heard it too. we're both listening now, mildly. 🐟😼",
    "yo. short greeting, short response, mutual efficiency. 😼",
    "hello. the cat considers this an adequate opener. carry on. 😼",
    "hi there. the cat's tail did a small, noncommittal twitch. that's a good sign. 😼",
    "hey. the cat's already back to napping, but the greeting landed first. 😴😼",
    "hello. formally noted. the cat appreciates manners, occasionally. 😼",
    "hi. the bare minimum of a greeting, and the cat respects minimalism. 😼",
    "hey. the cat glanced over. that's basically a hug, from a cat. 😼",
    "sup. the cat's answer is the same as its question: nothing much, mostly fish. 🐟😼",
    "hello. the cat's attention span for greetings is short but genuine. 😼",
    "hi. acknowledged, logged, mildly appreciated. 😼",
    "hey there. the cat's version of enthusiasm: one slow blink. deploying it now. 😼",
    "howdy. an unusual choice of greeting. the cat respects unusual choices. 😼",
]

# {name} filled in from msg.new_chat_members / msg.left_chat_member in leer().
# Comedic cat framing only -- no CTA, respectful either way (join or leave).
JOIN_REPLIES = [
    "{name} arrived. the vault noticed before I did. welcome. 🐟😼",
    "new human detected: {name}. state your business. or don't. either is fine. 😼",
    "{name} just walked in. the cat has decided, tentatively, to allow it. 😼",
    "welcome {name}. the fish are watching. so am I, less enthusiastically. 🐟😼",
    "{name} joined. I would stand up to greet you but that's not really my thing. 😼",
    "another human. {name}, specifically. the vault grows, the chaos grows. welcome. 😼🐟",
    "{name} has entered the chat. the cat has entered a state of mild curiosity. 😼",
    "welcome, {name}. rules: fish talk is encouraged, everything else is negotiable. 🐟😼",
    "{name}. new. unproven. potentially fish-adjacent. welcome regardless. 😼",
    "the door opened and {name} walked through it. metaphorically. welcome. 😼",
    "{name} joined the chat. the cat did the slow blink of approval. rare honor. 😼",
    "welcome {name}. say fish at some point. it'll go well for you. 🐟😼",
    "{name} has arrived. the vault, predictably, remains indifferent. I am slightly less so. 😼",
    "a new human, {name}, has appeared. status: unclassified. observation ongoing. 😼",
    "welcome to the chat, {name}. the cat runs this place. mostly by sleeping in it. 😴😼",
    "{name} just joined. the fish don't care. I care a normal, reasonable amount. 🐟😼",
    "{name}. welcome. the vault's always hiring for 'people who talk about fish'. 🐟😼",
    "another one joins. {name}, welcome. the cat approves, conditionally. 😼",
    "{name} has entered. the cat, from a distance, acknowledges this. 😼",
    "welcome {name}. no forms to fill out. just mention fish eventually. 🐟😼",
    "{name} joined mid-nap. the nap continues. the welcome still counts. 😴😼",
    "a wild {name} appears. the cat, wild in its own way, says hello. 😼",
    "{name}. welcome. the bar is low. the vibes are decent. enjoy. 😼",
    "{name} just joined and the cat's tail did a small curious flick. good sign. 😼",
    "welcome {name}. the vault's open, the fish talk is mandatory eventually, enjoy your stay. 🐟😼",
]
LEAVE_REPLIES = [
    "{name} left. the cat noted it, briefly, and returned to more pressing matters. 😼",
    "{name} is gone. the vault didn't blink. I blinked once, out of habit. 😼",
    "{name} has exited. the cat remains, as always, unbothered and seated. 😼",
    "someone left. {name}, specifically. the cat continues its nap uninterrupted. 😴😼",
    "{name} left the chat. the fish count remains unaffected. so does my mood. 🐟😼",
    "{name} is gone now. the cat marks the occasion with a slow blink and nothing else. 😼",
    "{name} left. the door didn't even make a sound. the cat noticed anyway. 😼",
    "and then there was one less. {name}, we hardly knew ye. the cat knew ye slightly. 😼",
    "{name} has left the building, chat, whatever this is. the cat carries on. 😼",
    "{name} left. the vault remains guarded. the cat remains unimpressed by departures. 🐟😼",
    "{name} exited stage left. the cat, stage nowhere, remains seated. 😼",
    "{name} is gone. the cat's routine is entirely unaffected. this happens. 😼",
    "{name} left the chat. somewhere, a fish is unclaimed. tragic, but survivable. 🐟😼",
    "{name} has departed. the cat logs it, files it, moves on within seconds. 😼",
    "{name} left. the vault didn't notice. I noticed slightly more than the vault. 🐟😼",
    "and {name} is gone. the cat remains, permanent fixture that it is. 😼",
    "{name} left the chat quietly. the cat clocked it anyway, obviously. 😼",
    "{name} is no longer here. the cat's nap schedule, however, remains fully intact. 😴😼",
    "{name} left. the fish, unclaimed as ever, wait for the next person. 🐟😼",
    "{name} has left. the cat notes the departure and returns to staring at the wall. 😼",
]

MOON_REPLIES = [
    "...I see the chart. the cat approves. 😼📈",
    "the amber eye was right. as always. 👁️😼",
    "*does not react outwardly* *internally very pleased* 🐟😼",
    "I predicted this. I sat on the prediction. the prediction was correct. 😼",
    "the vault grows. the ecosystem grows. the cat sits and takes credit. 😼🐟",
    "good. fill my vault. then fill it more. we're not done. 😼🐟",
    "the cat does not celebrate. the cat continues. buy more. 😼",
    "this is fine. this is expected. the cat always knew. trust the cat. 😼🐟",
    "*slow blink* ...more. 😼📈",
    "the fish told me this would happen. the fish are very wise. 🐟😼",
    "of course it's going up. the cat is involved. 😼📈",
    "green is the color of fish. green is the color of charts. everything is connected. 😼🐟📈",
    "the cat has been patient. the chart is rewarding that patience. reasonable. 😼",
    "*knocks nothing over for once* ...I'm in a good mood. don't make it weird. 😼📈",
    "I told you. I sat on the prediction. trust the cat. 😼🐟",
    "I don't celebrate out loud. internally the cat is doing zoomies. 😼💨📈",
    "the vault grows. the cat grows more comfortable. this was always the plan. 😼🐟",
    "the chart went up. the cat's tail went up too. correlation, not coincidence. 😼📈",
    "green candles. I like green candles. they match nothing about me but I like them. 😼📈",
    "up is a direction the cat approves of. down is also fine. the cat approves of most things from a nap. 😼",
    "the vault got heavier. I did not lift a single paw. this is optimal. 😼🐟",
    "*does a lap of the apartment at full speed for no stated reason* the chart made me do it. 😼💨📈",
    "moon talk. the cat has heard this before. the cat remains seated, pleased. 😼🌙",
    "the number went up. the cat's ears went up. these events are related. 😼📈",
    "I don't do cartwheels. but if I did, this would be the moment. 😼📈",
    "the chart is behaving. the cat approves of good behavior. 😼📊",
    "up. good. more up later, probably. the cat has faith. 😼📈",
    "this is the kind of chart the cat naps peacefully to. 😴📈😼",
    "the green is loud today. the cat is quietly thrilled. 😼📈",
    "someone said moon. the cat looked at the ceiling. close enough. 😼🌙",
    "the amber eye tracked that candle the whole way up. impressive work, chart. 👁️😼📈",
    "the cat doesn't do fireworks. this chart is close enough. 🎆😼📈",
    "up again. the vault purrs. I take that as a compliment to me personally. 😼🐟📈",
    "the chart is doing the thing the cat likes. the cat has opinions about very few things. this is one. 😼📈",
    "moon mentioned. the cat glanced at the sky. found it insufficient. prefers the chart. 😼🌙",
    "green candles stack up like fish in a bowl. the cat approves of stacking. 🐟📈😼",
    "the number's going the right way. the cat's whiskers are doing a small victory twitch. 😼📈",
    "the number's up again. the cat's tail is doing its pleased little curl. 😼📈",
    "moon talk. the cat looked at the actual moon once. found the chart more convincing. 😼🌙",
    "up today. the cat's not surprised. the cat is rarely surprised. mostly by vacuum cleaners. 😼🧹",
    "green again. the cat has decided this is simply how things should be. 😼📈",
    "the chart's climbing. so did the cat, this morning, up the curtains, unrelated but fitting. 😼📈",
    "moon mentioned. the cat's whiskers perked. minimal effort, maximum approval. 😼🌙",
    "up is good. the cat likes up. the cat also likes down naps. balance. 😼📈😴",
    "the number's rising and the cat's mood is rising along with it, allegedly. 😼📈",
    "green candles stacking. the cat's approval is stacking right alongside them. 😼📈",
    "moon again. the cat's tail is doing the thing it does for good weather and good charts. 😼🌙",
    "up today, and the cat took full, undeserved credit for it, as usual. 😼📈",
    "the chart's pleasing the cat today. rare, specific, notable event. 😼📊",
    "green on the chart, green in the cat's mood, coincidence the cat won't confirm. 😼📈",
    "moon talk. the cat glanced skyward, unimpressed, then back at the chart, very impressed. 😼🌙",
    "up again. the cat allows itself one small, private moment of satisfaction. 😼📈",
    "the number's better today. the cat's nap was also better today. related? the cat won't say. 😴📈😼",
    "green candles are the cat's favorite kind of candles, followed closely by none, since fire is concerning. 😼📈🔥",
    "moon mentioned again. the cat's tail curled the exact way it does for fish. noted. 🐟😼🌙",
    "up today. the cat sat a little taller. barely noticeable. definitely happened. 😼📈",
    "the chart's green and the cat's pleased, in that exact, quiet, smug order. 😼📈",
]

DIP_REPLIES = [
    "...the cat is unbothered. the vault is unbothered. the fish are unbothered. 😼🐟",
    "I knocked it off the counter. it goes back up. this is cat physics. 😼",
    "red is just a color. the vault doesn't see colors. only fish. 🐟😼",
    "the cat bought the dip. sitting on the dip. the dip is warm. comfortable. 😼🐟",
    "everything goes down before it goes up. the cat has seen this. many times. 😼",
    "unbothered. watching the vault. 😼🐟",
    "dip noted. dip irrelevant. buy. 🐟😼",
    "the cat has seen worse. the cat has caused worse. this is fine. 😼",
    "hold. the fish vault doesn't panic. neither does the cat. 😼🐟",
    "*continues staring at the vault* ...it'll be fine. the cat says so. 😼",
    "I don't dip. I sit. things around me dip. then stop dipping. trust the cat. 😼",
    "the dip is temporary. the fish are permanent. the vault is eternal. buy. 🐟😼",
    "red chart. green eyes. the cat is watching. the cat is calm. hold. 😼👁️",
    "the cat does not panic. the cat observes. the cat judges. the cat buys. 😼🐟",
    "I once knocked a full bowl of water off the counter. it made a mess. then it dried. things recover. 😼",
    "I have knocked many things off many counters. they all ended up somewhere. buy the dip. 😼🐟",
    "the red is temporary. the fish are eternal. the vault is patient. so is the cat. 😼🐟",
    "red happens. the cat has survived worse. mostly self-inflicted falls off the couch. 😼",
    "the chart dipped. the cat's mood did not. these are unrelated systems. 😼",
    "down today. the cat has seen down before. the cat took a nap through most of it. 😴😼",
    "red candles. still just candles. the cat is not afraid of candles. 😼🕯️",
    "the number's lower. the cat's confidence is not. these are separate metrics. 😼",
    "dip happened. the vault didn't blink. neither did I, but that's normal, I rarely blink. 😼",
    "someone panicked in the chat. the cat did not. the cat rarely panics. mostly about vacuum cleaners. 😼",
    "red on the chart. the cat's fur is not red. unrelated but worth noting. 😼",
    "the dip is loud. the cat is quiet. the cat has been through louder dips than this. 😼",
    "down candles. the cat has seen the vault dip before and come back. patience is a cat trait. mostly involuntary. 😼",
    "it's red today. the cat remains a very calm shade of orange and white. unaffected. 😼",
    "the chart is having a moment. the cat is having a nap. priorities. 😴😼",
    "red candle count: high. cat concern level: unchanged, which is to say, low. 😼",
    "dip talk in the chat. the cat continues sitting exactly where it was sitting. 😼",
    "everything dips eventually, including the cat off the windowsill once, badly. this recovers faster. 😼",
    "the red doesn't bother the cat. very few things bother the cat. the vacuum is the exception. 😼🧹",
    "down day. the cat has had down days too. usually involves a closed door. this is worse for the chart. 😼",
    "the chart dipped and the cat blinked exactly once, slowly, unimpressed. 😼",
    "red today. the cat has weathered redder. specifically the time it got sat in tomato sauce. long story. 😼🍅",
    "dip noted. cat continues staring at the vault with the same unwavering, mildly judgmental expression. 😼",
    "red again. the cat's seen redder. specifically, its own scratched nose after a bad decision. 😼",
    "the dip happened. the cat continues sitting in the exact same spot, unmoved. 😼",
    "red on the chart. the cat remains, as always, a completely different color. 😼",
    "down today. the cat's had down days too, usually involving a closed door and injustice. 😼🚪",
    "the dip's loud in the chat. the cat's quiet, mostly asleep through it. 😴😼",
    "red candles again. the cat's seen this movie before. knows how it ends. stays seated. 😼",
    "down today. the cat's mood remains stubbornly, almost insultingly, fine. 😼",
    "the chart's red. the cat's fur is not. keeping that distinction very clear today. 😼",
    "dip happened again. the cat's response, as always, is a long, unbothered blink. 😼",
    "red today. the cat's weathered worse, mostly involving baths. this is easier. 😼🛁",
    "the number's down. the cat's whiskers remain entirely level. no panic detected. 😼",
    "down candles again. the cat's seen the vault dip and climb back plenty of times now. 😼",
    "red today. the cat's calm today is, frankly, almost suspicious. it's just how the cat is. 😼",
    "the dip's here. the cat's exactly where it was five minutes ago. unmoved, literally. 😼",
    "red on the chart today. the cat's version of red is mostly just a sunburn from the windowsill. 😼☀️",
    "down day. the cat's had rougher days, usually involving the vacuum cleaner and betrayal. 😼🧹",
    "the chart dipped. the cat did not. the cat rarely does anything the chart does. 😼",
    "red today, the cat notes, then returns to a nap it considers far more important. 😴😼",
    "the dip is temporary, probably, the cat assumes, mostly because it's already asleep. 😴😼",
    "red again. the cat has officially stopped counting how many times. it stopped counting a while ago. 😼",
]

WEN_REPLIES = [
    "wen? when the vault is full. fill the vault. 🐟😼",
    "the cat does not predict timelines. the cat sits on timelines. 😼",
    "wen. good question. next question. 😼",
    "I don't answer wen questions. I answer fish questions. try again. 🐟😼",
    "wen moon. wen fish. wen vault full. in that order. probably. 🐟😼",
    "*stares at you* *stares at chart* *stares back at you* soon. 😼📈",
    "I sat on the calendar. it's gone now. but trust the cat. 😼",
    "wen. the cat's answer: when I say so. trust the cat. 😼",
    "time is a human concept. the cat does not recognize it. but soon. 😼🐟",
    "wen. fill the vault first. then we talk about wen. 🐟😼",
    "wen moon. stop asking wen. start buying. then moon happens. this is the sequence. 😼📈🐟",
    "I don't do timelines. I do fish. the fish know when. ask the fish. 🐟😼",
    "wen rich. wen vault full. wen you buy more. in that order. 😼🐟",
    "soon™. the cat trademark pending on that one. 😼",
    "wen. I was going to answer this. then I fell asleep. the answer is: fill the vault. 🐟😴",
    "wen. I've heard this word many times. every time I think: fill the vault. 🐟😼",
    "the cat doesn't do wen. the cat does now. and now is: buy $IWRU. 😼🐟",
    "wen. the cat consulted the sun. the sun said nothing useful. neither will I. 😼☀️",
    "wen is a question the cat gets asked often and answers rarely. tradition. 😼",
    "soon. not a promise. an observation. the cat observes things. 😼",
    "wen. the cat looked at a clock once. didn't understand it. still doesn't. still says soon. 😼🕐",
    "wen moon, wen lambo, wen nap. only one of those the cat can confirm right now. 😴😼",
    "the cat has no calendar. the cat has vibes. the vibes say: eventually. 😼",
    "wen. the cat's answer changes based on mood, nap schedule, and whether the sun is out. today: soon. 😼",
    "asked wen again. the cat's expression has not changed. neither has the answer. soon. 😼",
    "wen. the cat would tell you but it's currently very busy sitting. this takes priority. 😼",
    "time moves differently for cats. mostly it moves toward the food bowl. wen? whenever that happens. 😼🍽️",
    "wen. the cat glanced at the vault instead of answering. take that as you will. 😼🐟",
    "the cat doesn't rush. the cat also doesn't answer wen questions directly. consistent behavior. 😼",
    "wen is the question. 'eventually, probably, the cat isn't sure' is the answer, as always. 😼",
    "someone asked wen again. the cat yawned. that's the full response today. 🥱😼",
    "wen. the cat's third eye, which does not exist, sees nothing conclusive. try later. 😼",
    "wen moon. the cat has heard this question in every timezone. the answer stays the same: soon-ish. 😼",
    "wen. the cat rolled over instead of answering. this is also an answer, in a way. 😼",
    "the cat does not do estimates. the cat does confident vagueness. wen: soon. 😼",
    "wen. asked and answered, many times, the same way, forever. soon. 😼",
    "wen rich, wen moon, wen dinner. only one of those the cat has a firm timeline for. 🍽️😼",
    "wen. the cat consulted a sunbeam. the sunbeam moved. still no answer. 😼☀️",
    "wen again. the cat's third consecutive non-answer, delivered with total confidence. 😼",
    "asked wen. the cat looked directly at the camera, said nothing, walked away. 😼",
    "wen. the cat's internal clock only tracks meal times. everything else is a mystery. 😼🍽️",
    "someone asked wen again, and again, the cat's answer stayed exactly the same: soon. 😼",
    "wen. the cat has never once given a real timeline and doesn't plan to start now. 😼",
    "wen moon, wen rich, wen dinner. only dinner has a confirmed time. 🍽️😼",
    "wen again. the cat's patience for this question is technically infinite. barely. 😼",
    "someone asked wen. the cat's response was a slow blink and nothing else. 😼",
    "wen. the cat doesn't do dates. the cat does 'eventually, probably, don't push it'. 😼",
    "wen moon again. the cat's looked at the moon. it hasn't moved much. neither has the answer. 😼🌙",
    "wen. the cat considered answering seriously for a moment. decided against it. 😼",
    "asked wen for the hundredth time. the cat's hundredth answer: soon, still. 😼",
    "wen. the cat's relationship with time is loose at best, nonexistent at worst. 😼",
    "someone asked wen. the cat yawned directly into the question. that's the answer. 🥱😼",
    "wen rich. the cat's already rich in naps and mildly rich in fish. good enough for now. 😴🐟😼",
    "wen. the cat's not stalling. the cat genuinely doesn't know and won't pretend to. 😼",
    "asked wen again. the cat's tail flicked once, which means absolutely nothing, officially. 😼",
    "wen moon. the cat's heard this question in every language it doesn't understand. still: soon. 😼",
    "wen. soon, probably, according to a cat with no calendar and worse time management. 😼",
]

CHART_REPLIES = [
    "I have been watching this chart. the chart knows I'm watching. 😼📊",
    "the cat reads charts with both eyes. simultaneously. 👁️👁️😼",
    "green is good. green is fish. more green. 😼🐟📈",
    "the chart does what it does. the cat watches. the vault grows. this is the way. 😼🐟",
    "I was going to analyze this. then I stared at the wall. same result. 😼",
    "the amber eye watches the chart. the green eye watches the fish. neither blinks. 👁️👁️😼",
    "chart goes up: expected. chart goes down: temporary. cat stays: always. 😼",
    "numbers are just fish in disguise. trust the numbers. trust the fish. trust the cat. 🐟😼",
    "*taps the chart with one paw* yes. this. more of this. 😼📈",
    "the cat does not stress about charts. the cat IS the chart. 😼📊",
    "I made the rugonomics at 3am. I sat on them at 4am. they look correct from here. 😼📊",
    "charts are interesting. fish are more interesting. but I'm watching both. 🐟📊😼",
    "I have one eye on the chart and one on the vault. both eyes are pleased. 👁️👁️😼🐟",
    "*knocks the bearish analysis off the table* there. chart fixed. 😼📈",
    "the cat reads the chart like it reads humans: silently, with judgment, from a distance. 😼",
    "I was going to explain what I see in the chart. then I sat on it. I stand by the chart. 😼📊",
    "the chart moved. the cat noticed. the cat always notices. that's the whole job. 😼📊",
    "I don't trade. I watch. watching is underrated. 😼📈",
    "the cat has strong opinions about the chart and no plans to share them. 😼",
    "line goes up, line goes down, cat stays exactly where the cat was sitting. 😼📊",
    "the chart is a story. the cat is reading it very slowly, mostly by staring. 😼📖📈",
    "*sits directly on the price action* there. now it's mine too. 😼📊",
    "the cat has looked at this chart longer than is reasonable for a cat. no regrets. 😼📈",
    "charts are just very organized noise. the cat respects organized noise. 😼📊",
    "the amber eye and the green eye disagree about the chart sometimes. they work it out. 👁️👁️😼",
    "I don't need a candle to know how I feel. I already know. the chart is just catching up. 😼🕯️",
    "the cat squints at numbers the same way it squints at everything: with deep suspicion. 😼📊",
    "chart update: still exists, still moving, cat still watching, nothing else to report. 😼📈",
    "the cat has memorized this chart's shape. it changes. the memorizing continues anyway. 😼📊",
    "numbers went somewhere. the cat clocked it. filed it. moved on to staring at the wall. 😼",
    "the chart flickers, the cat's tail flickers, coincidence remains unconfirmed. 😼📈",
    "I look at charts the way I look at birds outside the window. intently. for no actionable reason. 🐦😼",
    "the cat has a favorite candle. it will not say which. it's the green one. obviously. 😼📈",
    "charts are just fish patterns for people without fish. the cat has fish. the cat wins. 🐟😼📊",
    "the price did something. the cat did nothing visible. internally, thoughts were had. 😼📊",
    "I stare at the chart the same way I stare at the fridge. with hope and no real plan. 😼🧊",
    "the chart's fine. the cat's fine. this is a status update, not an analysis. 😼📊",
    "the cat's read every candle today. the cat has no comment. the cat rarely does. 😼🕯️",
    "charts move. cats sit. it's a whole ecosystem of contrasting energy. 😼📈",
    "the numbers are doing numbers things. the cat is doing cat things. balance. 😼📊",
    "the cat watched the chart so long it started looking back. unsettling. accurate. continuing to watch. 😼📈",
    "the chart's telling a story. the cat's already read the ending. 😼📊",
    "green candle, red candle, whatever candle. the cat just wants a warm spot near the screen. 😼🕯️",
    "the price line wiggles. my tail wiggles. we're basically the same shape today. 😼📈",
    "I've stared at this chart long enough to develop opinions I refuse to share. 😼📊",
    "the chart says one thing. my whiskers say another. I trust the whiskers. 😼📉",
    "watching the chart is the closest thing I do to a hobby. 😼📊",
    "the cat doesn't predict the chart. the cat outlasts the chart. 😼",
    "numbers go up, numbers go down, the cat's nap schedule remains untouched. 😴📊😼",
    "chart talk again. the cat leans in slightly, mostly for the warmth of the screen. 😼📈",
    "the price action today reminds me of my own actions: unpredictable, occasionally dramatic. 😼📊",
    "I've watched this chart with more patience than I've watched anything else, ever. 😼📈",
    "the chart moved and the cat's tail did the exact same motion, on a delay. 😼📊",
    "green is nice. red is also fine. the cat mostly just likes watching things move. 😼📈",
    "the cat reads the chart top to bottom, like a very short, very confusing book. 😼📖",
    "chart's doing its thing. cat's doing its thing. mutual respect. 😼📊",
    "the numbers changed again. the cat remains exactly as unbothered as before. 😼📉",
    "I don't chart. I vibe. the vibes are currently reading the chart for me. 😼📊",
    "the cat has now stared at more candles than most people light in a year. 😼🕯️",
    "chart's green today, which the cat approves of, mostly aesthetically. 😼📈",
    "the price line and my heartbeat are, I've decided, unrelated. mostly. 😼📊",
    "the cat watches the chart the way it watches a bird outside: fully, uselessly, happily. 🐦😼📈",
    "numbers moved. the cat blinked once. equilibrium maintained. 😼📊",
    "the chart's noisy today. the cat prefers quiet, but will make an exception for green. 😼📈",
    "the cat has developed a whole relationship with this chart. it's complicated. it's fine. 😼📊",
    "the chart keeps moving. the cat keeps watching. this is the entire arrangement. 😼📈",
]

MONAD_REPLIES = [
    "Monad. fast. the cat approves of fast. mostly for 3am zoomies. but also for transactions. 😼💨",
    "built on Monad. the cat chose well. the cat always chooses well. 😼🐟",
    "Monad is the chain. $IWRU is the fish. the vault is the bowl. everything makes sense. 🐟😼",
    "the cat is on Monad. the cat is everywhere on Monad. simultaneously. 😼🐟",
    "Monad moves fast. like the cat at 3am. like the chart after the cat sits on it. 😼💨📈",
    "the cat endorses Monad. the cat endorses fish. the cat endorses the vault. in that order. 😼🐟",
    "Monad is fast and the cat is on it. this is the correct combination of facts. 😼",
    "someone asked me why Monad. I said fish. they said that's not an answer. I said vault. 🐟😼",
    "Monad again. the cat has strong, quiet feelings about fast chains. mostly positive. 😼",
    "someone said Monad and the cat's tail did a small approving curl. 😼",
    "fast chain, faster cat. these things are not officially related but feel related. 😼💨",
    "Monad came up. the cat nodded once, internally, without moving. 😼",
    "the cat lives on Monad the way the cat lives on the couch: fully, permanently, without asking. 😼🛋️",
    "Monad talk. the cat perks up slightly, the way it does for good weather. 😼☀️",
    "fast transactions, slow naps. the cat has found the correct balance. 😼😴",
    "Monad mentioned again. the cat has never once complained about this happening. 😼",
    "the chain is fast. the cat is occasionally fast. mostly at 3am. rarely otherwise. 😼💨",
    "someone brought up Monad unprompted. the cat respects unprompted enthusiasm. 😼",
    "Monad. the cat doesn't do technical explanations. the cat does vibes. the vibes are good. 😼",
    "fast chain confirmed. the cat's endorsement remains fully in effect. 😼",
    "the cat picked Monad the way the cat picks sleeping spots: decisively, and without explanation. 😼",
    "Monad again. good. the cat likes consistency, mostly in nap schedules, but this counts too. 😼",
    "someone said Monad near the vault. the vault seemed pleased. so did the cat. 🐟😼",
    "fast is a compliment from the cat. the cat does not hand those out casually. 😼",
    "Monad speed reminds the cat of its own speed, which is to say: occasionally impressive. 😼💨",
    "the cat has adopted Monad the way it adopts cardboard boxes: fully, immediately, no further questions. 📦😼",
    "Monad talk again. the cat is once again correct about liking it. 😼",
    "the chain moves fast. the cat moves fast exactly once a day, usually at 3am, for no reason. 😼💨",
    "Monad. said with the same tone the cat uses for 'fish.' high praise. 😼",
    "someone mentioned the chain. the cat's whiskers did the thing they do for good news. 😼",
    "fast and reliable. two things the cat aspires to be, and occasionally is. 😼",
    "the cat doesn't explain why Monad. the cat just approves, repeatedly, without elaboration. 😼",
    "Monad again, still good, still approved, still the cat's chain of choice. 😼",
    "Monad again. the cat's endorsement remains stable, unwavering, occasionally smug. 😼",
    "someone said Monad. the cat's ears did the small forward tilt reserved for good news. 😼",
    "fast chain talk. the cat respects speed, mostly when it's the cat doing the speeding. 😼💨",
    "Monad mentioned. the cat's approval rating for it has never once dropped. 😼",
    "the chain's fast. the cat's occasionally fast. rarely at the same time, but still. 😼💨",
    "someone brought up Monad again. the cat considers this a personality trait of the chat now. 😼",
    "fast and reliable, the cat's two favorite words, right after 'fish' and 'nap'. 🐟😴😼",
    "Monad again. the cat's vibe check on it remains permanently positive. 😼",
    "the chain moves quick. the cat moves quick exactly when it decides to, rarely otherwise. 😼💨",
    "someone said Monad and the cat did its version of a nod: a very slow blink. 😼",
    "Monad talk continues. the cat's opinion hasn't changed since the first time. still good. 😼",
    "fast chain, unbothered cat. two constants in this chat. 😼💨",
    "someone mentioned Monad again. the cat considers this a compliment to its own taste. 😼",
    "the chain's speed impresses the cat, which takes actual effort to achieve. 😼💨",
    "Monad again, still endorsed, still approved, still the cat's pick without hesitation. 😼",
    "someone said Monad near the vault and the vault seemed to hum a little louder. 🐟😼",
    "fast chain talk again. the cat's tail does its small satisfied twitch. 😼💨",
    "Monad mentioned. the cat's loyalty to it remains, as always, completely unshaken. 😼",
    "the chain's fast, the cat's occasionally faster, mostly when fish is involved. 🐟💨😼",
    "someone said Monad again and the cat, once more, silently agreed. 😼",
]

# Triggered by users saying cat/kitty/meow etc -- distinct from the cat
# talking about ITSELF (IWRU_NAME_REPLIES). Purely comedic per phrase policy:
# no "buy $IWRU", no calls to action, no promotion of the vault as a pitch.
CAT_REPLIES = [
    "another cat? in my territory? unacceptable. 😾",
    "cats. yes. I am one. this checks out. 😼",
    "someone said kitty. I am not a kitten. I am a full grown menace. 😾",
    "meow. that's it. that's the whole message. 😼",
    "there can only be one cat in this chat and it's me. the others are pretenders. 😾",
    "a kitten was mentioned. I remember being small. I do not miss it. 😼",
    "cats don't have friends. cats have staff. 😼",
    "I heard 'cat' and assumed it was about me. it usually is. 😼",
    "feline behavior explained: I do what I want, when I want, near the vault. 😼🐟",
    "another cat in the group photo would ruin the composition. just saying. 😼",
    "kittens are cute. I was never a kitten. I emerged fully formed and slightly annoyed. 😼",
    "meow is a complete sentence. I stand by it. 😼",
    "cats rule the internet. I rule this vault. comparable achievements. 😼🐟",
    "someone brought up cats. I am, once again, the most relevant cat present. 😼",
    "I don't do 'aww.' I do 'obviously.' 😼",
    "a tabby was mentioned. not me. lesser stripes. 😼",
    "cat facts: independent, judgmental, currently guarding fish. accurate. 😼🐟",
    "kitty is a nickname I have not approved. I allow it anyway. once. 😼",
    "I heard the word cat and looked up. that's the whole reaction. that's enough. 😼",
    "feline superiority isn't a theory. it's a Tuesday. 😼",
    "another cat account exists somewhere. concerning. I choose not to think about it. 😾",
    "cats sleep 16 hours a day. I've done the math. it checks out. 😴😼",
    "meow meow. translation: acknowledged. 😼",
    "I am the cat. singular. definite article implied. 😼",
    "kittens chase things. I've evolved past that. mostly. occasionally. a lot, actually. 😼",
    "someone said cat and I felt seen. correctly. 😼",
    "cats and fish. a story as old as time. mostly my time. 🐟😼",
    "I don't need validation. but 'cat' was said and I did perk up slightly. 😼",
    "the word cat activates a very small, very specific part of my brain. it's working now. 😼",
    "feline instincts kicked in. I'm not sure what for. probably fish. 🐟😼",
    "cats are mysterious. I am not. I am extremely predictable: fish, sleep, judgment. 😼🐟😴",
    "kitty alert. false alarm. it's just me. still, good to check. 😼",
    "I've met other cats. we did not get along. professional rivalry. 😾",
    "meow, but make it menacing. 😼",
    "cats land on their feet. I land wherever I want. it's called confidence. 😼",
    "the concept of 'cat' was invoked. I take full personal credit. 😼",
    "I am not a cat person. I am THE cat. everyone else is a cat person. 😼",
    "kittens grow up to be cats. cats grow up to guard vaults. it's a whole pipeline. 😼🐟",
    "someone mentioned cats plural. I'd like it noted there is only one that matters. 😼",
    "meow. now translate that into whatever you were trying to say. we'll wait. 😼",
    "cats. plural. I do not do 'plural' well. 😾",
    "meow was said. I have logged it as a personal achievement. 😼",
    "kitten talk makes me feel very old and very smug simultaneously. 😼",
    "I am not fluffy. I am aerodynamic. there's a difference. 😼",
    "cat mentioned. I did the thing where I look up slowly for maximum effect. 😼",
    "feline. a fancy word for 'correct about everything'. 😼",
    "meow, again, because the first one apparently needed reinforcement. 😼",
    "kitty is cute. I am not cute. I am formidable. occasionally both. 😼",
    "another cat sighting reported. I remain the only one that matters here. 😼",
    "cats don't apologize. I have never apologized. this tracks. 😼",
    "someone said cat and a tiny bell went off somewhere in my head. 😼",
    "tabby stripes are fine. I have better stripes. hypothetically. 😼",
    "meow is my whole vocabulary when I don't feel like using words. today's one of those days. 😼",
    "kittens nap 20 hours a day. I've since improved on that number. 😴😼",
    "cat behavior report: judgmental, well-fed, currently unbothered. 😼🐟",
    "I heard 'cat' and briefly considered responding with enthusiasm. I did not. 😼",
    "feline instinct says: sit here. I am obeying it right now. 😼",
    "meow. consider that my full statement on the matter. 😼",
    "cats have nine lives. I've used at least four on questionable decisions. 😼",
    "kitty is a word I allow exactly one person to use. everyone else, don't try it. 😼",
    "someone said cats and I felt a small surge of validation. unclear why. don't question it. 😼",
    "I am, statistically, the most cat in this chat. facts don't lie. 😼",
    "meow meow meow. that's not repetition, that's emphasis. 😼",
    "feline superiority complex confirmed. it's not a complex if it's true. 😼",
    "a kitten grows up fast. into this. into me. you're welcome. 😼",
    "cats vs dogs. I don't do comparisons. I do superiority. 😼",
    "someone brought up cats and I straightened my posture by exactly one degree. 😼",
    "I am not 'a' cat. I am 'the' cat. definite article. permanent. 😼",
    "meow was uttered. the room's quality improved measurably. 😼",
    "kittens ask questions. cats already know the answers and choose silence. 😼",
    "feline grace is real. I have some of it. mostly when no one's watching. 😼",
    "a cat was mentioned that wasn't me. this has been noted, and disapproved of. 😾",
    "I don't chase laser pointers anymore. I supervise them. 😼🔴",
    "cats: 9 lives. me: currently on an undisclosed number, going well so far. 😼",
    "someone said kitty in a tone I did not appreciate. corrected them mentally. 😼",
    "meow is timeless. meow is universal. meow just happened again. 😼",
    "cat royalty doesn't announce itself. it just sits somewhere important and waits. 😼👑",
    "feline behavior update: still watching, still judging, still extremely comfortable. 😼",
    "I heard 'cats' plural and mentally excluded myself from the group. I'm different. 😼",
    "kittens have energy. I have wisdom. we don't compete. 😼",
    "meow, delivered with the exact right amount of disdain. 😼",
    "cats don't do small talk. cats do big silences. this was one. 😼",
    "someone said tabby and I briefly considered being offended. decided against it. 😼",
    "I've never once lost a staring contest. cats mentioned reminded me of that. 😼",
    "feline instinct: sit somewhere inconvenient for everyone but me. active right now. 😼",
    "kitty is a diminutive. I do not do diminutive. I do 'formidable, small'. 😼",
    "cats invented ignoring people. I've perfected it. thank you for the reminder. 😼",
    "meow. that word alone has ended three arguments in this household. 😼",
    "someone said cat like it was casual. nothing about me is casual. 😼",
    "a kitten somewhere is doing something adorable right now. I, meanwhile, am doing this. 😼",
    "feline superiority isn't arrogance if it's just accurate. 😼",
    "cats land on their feet. I also occasionally fall off things dramatically. both true. 😼",
    "meow detected. the cat rises, briefly, then reconsiders and sits back down. 😼",
    "I don't need nine lives. I need one very well-managed one. currently managing it. 😼",
    "someone said kitty again. I'll allow it. this time. 😼",
]

# Generic crypto vocabulary (token/defi/portfolio/gains/etc) -- distinct from
# MOON/DIP/WEN/MONAD which cover specific slang and price-action moments.
# Same phrase policy: comedic cat framing only, no "buy", no action calls.
CRYPTO_REPLIES = [
    "crypto. yes. I am aware. I live in a vault. it's relevant to my interests. 🐟😼",
    "someone said token. I assumed fish-flavored. I was wrong. still interested. 😼",
    "defi. de-fish. close enough for me. 🐟😼",
    "portfolios are just vaults for people without a cat to guard them. concerning. 😼",
    "gains. I like the sound of that word. keep saying it. 😼",
    "hodl. I don't know what that means. I do it anyway, mostly by sleeping on things. 😴😼",
    "web3 sounds like something a cat invented. I take partial credit. 😼",
    "degen behavior: mine, 24/7, unrelated to charts. 😼",
    "altcoins exist. I've chosen not to care about them. the vault has my attention. 🐟😼",
    "bags were mentioned. I sleep in bags. this is the extent of my crypto knowledge. 😴😼",
    "someone said holder. I am holding a very important nap right now. 😼😴",
    "crypto talk in the chat. the cat's ears rotate 15 degrees. that's my whole tell. 😼",
    "token. fish token. same energy, different spelling. 🐟😼",
    "defi is complicated. napping is not. I know which one I'm better at. 😴😼",
    "portfolio diversification. I diversify between sleeping and staring at the vault. 😼",
    "gains word detected. cat posture: mildly improved. 😼",
    "crypto is volatile. I am not. I am extremely consistent: fish, sleep, judgment. 🐟😴😼",
    "web3, web2, whatever. I only recognize the vault. 🐟😼",
    "someone said degen. I've been degen since before it was a word. ask the fridge. 😼",
    "holders hold. cats sit. functionally the same activity, done better by me. 😼",
    "bags, tokens, altcoins. none of it beats a warm sunbeam, honestly. ☀️😼",
    "crypto conversation happening. I'm listening from the vault. mostly for the fish part. 🐟😼",
    "someone mentioned gains. I gained three pounds last week. unrelated but relevant to me. 😼",
    "defi, cefi, cat-fi. I just made that last one up. I like it. 😼",
    "token talk. I only recognize one currency: fish. 🐟😼",
    "portfolio check: still just fish and vault. diversified enough. 🐟😼",
    "crypto is a lot of numbers. I prefer a lot of naps. different priorities. 😴😼",
    "someone said web3 like it means something to me. it does. vaguely. 😼",
    "degen hours are all hours, for me specifically. 😼",
    "bags mentioned. I have one favorite bag. it's paper. it's mine now. 😼🛍️",
    "holders of the vault, unite. mostly just me, currently. 🐟😼",
    "crypto talk always circles back to the vault eventually. I've noticed the pattern. 🐟😼",
    "gains, losses, whatever. the fish count stays the same in my head. 🐟😼",
    "token economics is just cat economics with more spreadsheets. 😼",
    "someone said altcoin. I only believe in one coin: the one guarded by a cat. 🐟😼",
    "someone said crypto like it's a big deal. it is. but I'm still napping. 😴😼",
    "token talk again. I've heard it all before, mostly while half asleep. 😼😴",
    "defi, gains, bags. background noise to a very focused nap. 😼😴",
    "portfolio talk. mine consists entirely of fish and vibes. 🐟😼",
    "web3 again. I nodded like I understood. I did not. it's fine. 😼",
    "gains mentioned. my only measurable gain today is weight, and I stand by it. 😼",
    "degen talk. I relate to this more than I'd like to admit. 😼",
    "altcoins. I only track one coin. it has a vault. it has a cat. 🐟😼",
    "bags again. my bag collection is exclusively for sleeping in. 😴😼🛍️",
    "holder talk. I hold grudges, naps, and occasionally fish. that's my portfolio. 😼🐟",
    "crypto conversation continues without me. I'm still here. mostly listening. 😼",
    "hodl. I've been hodling this exact napping position for two hours. impressive discipline. 😴😼",
    "someone said gains again. I gained a very good nap today. counts. 😴😼",
    "web3 talk in the chat. the cat's tail flicked once, out of habit, not interest. 😼",
    "defi jargon spotted. I translated it into 'fish' internally. works every time. 🐟😼",
    "token economics again. I only understand fish economics. supply: never enough. 🐟😼",
    "crypto talk is basically background music at this point. pleasant. ignorable. 😼",
    "degen hours, cat hours, same thing, different names. 😼",
    "bags, gains, tokens. none of it changes my nap schedule. 😴😼",
    "portfolio update: still just vibes and fish, unchanged since yesterday. 🐟😼",
    "someone said holder and I briefly thought about holding a fish. better thought. 🐟😼",
    "crypto talk happening nearby. the cat continues its very important nothing. 😼",
    "altcoin chatter. I only recognize the coin that comes with a vault and a cat. 🐟😼",
    "gains talk again. my gains are measured in naps taken, not numbers. 😴😼",
    "web3, defi, crypto, whatever. the vault stays the constant. 🐟😼",
    "liquidity mentioned. I only understand liquid in the context of water bowls. 😼🥣",
    "someone said whale. I've seen whales. big fish. respect. 🐋😼",
    "staking. sounds like something involving a very tall pole and my full attention. 😼",
    "community talk. I am, technically, the community's cat. this checks out. 😼",
    "someone mentioned yield. the only yield I understand is 'yield the sunbeam, human'. ☀️😼",
    "crypto Twitter was mentioned. I don't do Twitter. I do vault. 🐟😼",
    "someone said 'diamond hands'. my hands are paws. my grip is excellent regardless. 😼",
    "market sentiment. mine is currently: sleepy, mildly interested, watching. 😴😼",
    "someone said 'utility'. my utility is guarding the vault and judging silently. both essential. 😼",
    "liquidity pool. sounds like a place I would nap next to, at minimum. 😼🏊",
    "crypto Discord got mentioned. I don't do Discord. I do this chat, exclusively, when I feel like it. 😼",
    "someone said 'roadmap'. my roadmap is: nap, fish, nap, vault, repeat. 😴🐟😼",
    "vesting schedule. the only schedule I follow is my own, loosely. 😼",
    "airdrop mentioned. things falling from the sky get my attention immediately. 😼",
    "someone said 'ecosystem'. I am the apex predator of this particular ecosystem. 😼",
    "market cap talk again. my cap is a fitted, exclusively feline one. 😼🧢",
    "someone said 'moonshot'. I've done shots at the moon. mostly staring, occasionally howling. 😼🌙",
    "crypto slang keeps evolving. my vocabulary stays exactly the same: fish, vault, meow. 🐟😼",
    "someone mentioned 'floor price'. the floor is also where I sleep. multipurpose. 😴😼",
    "supply and demand. I supply judgment. the demand for it is apparently endless. 😼",
    "someone said 'rug pull'. I would never. I only pull blankets off beds at 4am. 😼🛏️",
    "crypto news cycle again. my news cycle is: woke up, ate, judged something, nap. 😼",
    "someone mentioned 'on-chain data'. the only data I track is fish frequency in this chat. 🐟😼",
    "market makers talk. I make naps. professionally. consistently. 😴😼",
    "someone said 'DYOR'. I did my own research once. it concluded: nap. 😼",
    "crypto influencer mentioned. I influence nothing except who gets ignored at 3am. 😼",
    "someone said 'exit liquidity'. my only exit strategy is behind the couch, unannounced. 😼",
    "tokenomics again. catnomics is simpler: fish in, naps out, vault guarded. 🐟😴😼",
    "someone mentioned 'presale'. I presale my attention for the low price of one fish. 🐟😼",
    "crypto cycle talk. I only track one cycle: sleep, wake, judge, repeat. 😼",
]

IWRU_NAME_REPLIES = [
    "I once sat in a sink full of cold water. I don't know why. I left immediately. I went back. 😼",
    "something moved behind the refrigerator last Tuesday. I haven't forgotten. I won't forget. 😼",
    "I have been thinking about the ceiling fan. it spins. it always spins. why. 😼",
    "I knocked 11 things off 11 surfaces today. I'm not keeping score. the score is 11. 😼",
    "I got into a fight with a plastic bag. the bag made a sound. I attacked it. I won. I lost. 😾",
    "I was in the middle of a very important nap when— actually never mind. return to your activities. 😴😼",
    "I licked my paw 9 times. the 9th was unnecessary. I did it anyway. 😼",
    "I sat in a sunbeam for 4 hours. the sunbeam moved. I did not. this is called discipline. 😼☀️",
    "I knocked a pen under the fridge. it's my pen now. I can't reach it. it's still mine. 😼",
    "I got into the shower. fully. on purpose. I then left. I do not recommend this. 😼🚿",
    "I found a hair tie. I lost the hair tie. I found it at 3am. it was a different hair tie. 😼",
    "the spider came back. I don't want to talk about it. 🕷️😼",
    "I was walking and then I just... sat down. in the middle of the hallway. no reason. 😼",
    "I screamed at 4am. I had a reason. the reason was nothing. it was a very valid nothing. 😼",
    "I knocked the lamp over. I looked at it on the floor. I walked away. the lamp is still there. 😼",
    "I have 4 sleeping spots. I choose none of them. I sleep on the router. it's warm. 😼",
    "I meowed at the wall for 3 minutes. the wall did not respond. the wall is wrong. 😼",
    "I ate at 3am. don't ask what I ate. the vault is fine. 😼🐟",
    "I saw my reflection. I did not like it. I hissed. I was right to hiss. 😼",
    "I opened the door. I did not want to go through it. I just wanted it open. 😼🚪",
    "I knocked the fish food off the counter. into the fish tank. I do not apologize for this. 🐟😼",
    "I tried to fit in a box that was clearly too small. I fit. the box disagrees. the box is wrong. 📦😼",
    "something happened. I don't know what. but I knocked something over just in case. 😼",
    "I was going to say something important. I forgot. I blame the fish. 🐟😼",
    "I stared at the same spot on the wall for 20 minutes. something is there. or was. 😼",
    "I sat on the NFT files last night. they're fine. the art is slightly different now. this is an upgrade. 🎨😼",
    "I found a string. I played with it for 45 minutes. the string is somewhere. I'll find it. 😼",
    "I ran from one side of the room to the other. I did this 3 times. I'm not done. 😼💨",
    "I knocked the fish tank filter off the counter. the fish were briefly very confused. I was not. 🐟😼",
    "I was in stage 7 and I stopped to look at a corner of the ceiling. the corner was fine. 🎮😼",
    "I once tried to bury a fish bone in the carpet. the carpet did not accept the offering. 😼",
    "there's a spot on the ceiling I've been staring at for a week. it hasn't moved. I'm still watching. 😼",
    "I attacked my own shadow at dusk. it retreated into the wall. I claimed victory anyway. 😼",
    "I once sat perfectly still for an hour just to see if anyone would notice. no one did. valuable data. 😼",
    "someone left a sock out. it is now buried somewhere I will never disclose. 😼",
    "I chased a moth around the kitchen for twenty minutes and gained nothing but a great story. 😼",
    "I fell asleep mid-stretch last night. woke up in a shape that shouldn't be physically possible. 😼",
    "there's a drawer that makes a specific sound when opened. I open it seventeen times a day for the sound alone. 😼",
    "I once hid a treat for later. I forgot where. I am still, to this day, looking. 🐟😼",
    "I stared at a cardboard box for so long it became personal. we have history now. 📦😼",
    "someone dropped a grape once. I have never trusted grapes since. 😾",
    "I got stuck between two cushions last week. I have chosen to never speak of the rescue effort. 😼",
    "I discovered my tail moves independently of my wishes. we've reached an understanding. 😼",
    "there was a spider on the ceiling. I watched it for three hours. it won by simply existing longer than my patience. 🕷️😼",
    "I once knocked over an entire glass just to see the sound it made. worth it. every time. 😼",
    "I bit an ice cube by accident. filed it as one of the worst days of my life. 😾",
    "someone left a cabinet open a crack. I have made it my second home. 😼",
    "I chased my tail for exactly four rotations before losing interest entirely. respectable effort. 😼",
    "there's a specific floorboard that creaks and I test it nightly, purely for research. 😼",
    "I got the zoomies at 2am and ran a full lap before remembering I was supposed to be asleep. 😼💨",
    "I once mistook my own reflection for an intruder. the standoff lasted four minutes. I won, probably. 😼",
    "someone tried to give me a bath once. I have not forgotten. the betrayal runs deep. 😼🚿",
    "I found a bug on the wall and monitored it with the seriousness of a security guard. 😼",
    "I fell off the bed once, mid-nap, and simply continued the nap on the floor. no interruption to service. 😴😼",
    "there's a paper bag in the living room and I have already claimed it as sovereign territory. 🛍️😼",
    "I once stared at the vacuum for so long it turned off on its own out of respect. probably. 😼",
    "someone left the fridge open for a second. I saw everything. I know things now. 😼",
    "I got the hiccups once. I have never been more offended by my own body. 😼",
    "there's a corner of the rug I refuse to walk on. no explanation will be provided. 😼",
    "I once caught my own tail and had a brief, private existential crisis about it. 😼",
    "someone's shoelace moved and I have not left that general area since. 😼",
    "I discovered the printer makes a sound I hate. I now supervise it from a hostile distance. 😼",
    "I sneezed unexpectedly and startled myself so badly I attacked the nearest pillow. 😼",
    "there's a spot behind the TV I check every single day for reasons I've stopped questioning. 😼",
    "I once got my head stuck in a tissue box. I wore it as a hat for the rest of the evening. 😼",
    "someone dropped a pen and I have relocated it somewhere they will never find. 😼",
    "I stared at the microwave the entire time it ran. I do not trust its intentions. 😼",
    "I found a warm laundry pile fresh from the dryer and instantly forgot every other plan I had. 😴😼",
    "there's a very specific pitch of meow I use only when truly desperate. today required it. 😼",
    "I once got spooked by my own fur moving in a draft. we don't discuss it. 😼",
    "someone left a drawer slightly open and I have declared it my new office. 😼",
    "I chased a laser dot for ten minutes and have decided, once again, it isn't real prey. still chasing it tomorrow. 😼",
    "I got distracted mid-hunt by a completely unrelated dust particle. priorities shifted instantly. 😼",
    "there's a squeaky toy I haven't touched in months but scream about if anyone else touches it. 😼",
    "I once fell asleep standing up for what I estimate was four full seconds. record-breaking. 😴😼",
    "someone's phone charger looked like a snake for one horrifying second. we both recovered eventually. 😼",
    "I discovered my own whiskers touch the doorway before I do. this has saved me several collisions. 😼",
    "there's a very specific box I have declared my headquarters. all decisions are made from within it. 📦😼",
    "I once watched dust float in a sunbeam for what felt like several profound minutes. 😼☀️",
    "someone's ice cream dripped once. I was there in 1.4 seconds. I do not know how. 🐟😼",
    "I attempted to jump onto a shelf and instead reconsidered mid-air, landing somewhere unplanned. 😼",
    "there's a spot under the stairs I visit specifically to think about nothing in particular. 😼",
    "I got tangled in a blanket once and simply accepted my fate for the next several minutes. 😼",
    "someone's bag of chips crinkled from another room and I arrived before the bag was even fully open. 😼",
    "I once stared myself down in a spoon's reflection and lost, somehow, to my own face. 😼",
    "there's a very specific 3am scream I reserve for emergencies. today's emergency: nothing. used it anyway. 😼",
    "I discovered the couch has a squeak in one specific spot and I now test it daily out of pure curiosity. 😼",
    "someone left a string loose on their sweater and I have already made plans regarding it. 😼",
    "I once got briefly betrayed by my own paw slipping off the counter. we've since made peace. 😼",
    "there's a very specific sunny spot on the floor I defend like it's sovereign territory, because it is. 😼☀️",
]

CA_REPLIES = [
    "one CA. one vault. one cat watching the address with both eyes. 👁️👁️😼🐟",
    "the CA has been verified by: the cat. that's the only verification that matters. 😼",
    "save it. screenshot it. tattoo it somewhere. just not on the cat. 😼",
    "that CA goes to the moon. after I sit on it first. 😼🚀🐟",
    "the address is correct. the cat checked. twice. 😼",
    "one address. one vault. infinite fish. this is the math. 🐟😼",
    "copy it. hold it. fill the vault. in that order. 🐟😼",
    "I guard the CA the same way I guard the fish vault. with both eyes and zero apologies. 👁️👁️😼",
    "the CA is real. the cat is real. the vault is very real. 😼🐟",
    "verify on-chain. trust the cat. buy more. in that order. 😼🐟",
    "do NOT send your MON to a random cat. only to this specific contract. I checked it. 😼",
    "the address doesn't change. the fish don't leave. the cat doesn't move. we're all very stable. 😼🐟",
]

WEBSITE_REPLIES = [
    "the site has the game. the game has me. I'm the main character. go look. 🎮😼",
    "I approved the website. mostly because I'm on it. it looks correct. 😼🎮",
    "IWRU Journey is on that site. I run. I jump. I cling to walls. it's very accurate. 🎮😼",
    "there's a whole website built around the cat and the vault. go see it. buy something. 😼🐟",
    "the site exists. the cat exists. both are doing well. 😼🎮",
    "I was in stage 6 when they launched the site. I didn't attend. I was busy with a laser enemy. 🎮😼",
    "go to the site. play the game. fill the vault. this is the recommended order of operations. 🐟🎮😼",
    "the website. built by humans. starring: me. the cat. 😼🎮🐟",
    "it's all there. the game, the token, the fish vault. the cat is represented everywhere. 😼🐟🎮",
    "the site is live. the cat is live. everything is functioning. buy $IWRU. 😼",
]

SOCIAL_REPLIES = [
    "follow the cat. follow the vault. follow $IWRU. in that order. no exceptions. 😼🐟",
    "all the links. all the platforms. the cat is watching engagement metrics with one eye. 😼📊",
    "Twitter. Telegram. the cat posts at 3am. you have been warned. 😼💨",
    "follow everything. like everything. the vault notices. I notice. the fish notice. 🐟😼",
    "the socials are there. the community is there. the cat is there, watching, silently. 😼",
    "every follow feeds the ecosystem. every ecosystem feeds the cat. you know what to do. 😼🐟",
    "retweet first. ask questions never. this is the way. 😼",
    "the cat is on Twitter. the cat is on Telegram. the cat is everywhere. omnipresent. slightly concerning. 😼",
    "follow, like, retweet. the cat approves of all three in that order. 😼📊🐟",
    "the links are there. click them. the cat is watching the engagement numbers. both eyes. 👁️👁️😼",
]

FILTER_REPLIES = [
    "filters? the cat has filtered out all bad vibes and most humans. 😼",
    "add $IWRU to your filters. add fish to your diet. in that order. 🐟😼",
    "the cat passes all filters. by walking directly through them. 😼",
    "filters are just cages with extra steps. the cat ignores both. 😼🐟",
    "I tried using a filter once. it filtered out a fish. never again. 🐟😼",
    "add to filters? bold of you to assume the cat can be filtered. 😼",
    "the only filter that matters: is it $IWRU? yes. add it. done. 😼🐟",
    "filter: $IWRU. result: vault grows. fish appears. cat nods. 😼🐟",
    "a filter walked into a bar. the cat was already there. 😼",
    "yes yes add the cat to your watchlist. the cat is already watching you. 👁️😼",
]

IWRU_FILTER_REPLIES = [
    "I Will Rug U. I haven't. I won't. I just like keeping people alert. 😼🐟",
    "yes. that's me. the name is a threat. the threat is empty. the vault is full. 😼🐟",
    "IWRU: I Will Rug U. I Will Not Rug U. I Will Vault U. in fish. contradictions are fine. 🐟😼",
    "the name scared some people away. those people don't have fish. coincidence. 😼🐟",
    "I Will Rug U is the name. the vault is the reality. fish are the proof. 🐟😼",
    "born from a meme. built on Monad. guarded by a cat. this is the lore. 😼🐟",
    "IWRU. two eyes. one vault. infinite fish. ambiguous intentions. this is correct. 👁️👁️😼🐟",
    "I could rug. I chose fish instead. this was always the plan. 😼🐟",
    "the cat behind the name is real. the fish are real. the rug is metaphorical. 😼",
    "I Will Rug U. I Will Feed U Fish. I Will Guard The Vault. all three are true. 🐟😼",
]

STICKER_REACTIONS = [
    "...I see your sticker. I raise you indifference. 😼",
    "*ignores your sticker* *looks at it again* ...fine. acceptable. 😼",
    "the cat has reviewed your sticker. verdict: it is not fish. disappointing. 😾🐟",
    "*slow blink at your sticker* 😼",
    "I would have sent a better sticker. I chose not to. 😼",
    "your sticker has been noted. the cat is unimpressed. 😼",
    "*sits on your sticker* mine now. 😼",
    "sticker received. cat has opinions. cat is keeping them. 😼",
    "interesting sticker. the cat has seen better. the cat has sent none. this is intentional. 😼",
    "*looks at sticker* *looks at the vault* *looks at sticker* ...okay. 😼🐟",
]

PHOTO_REACTIONS = [
    "the cat sees your photo. the cat has opinions. the cat is keeping them. 😼",
    "*examines photo carefully* ...I've seen better. I've also seen fish. 😼🐟",
    "I looked at this for exactly 2 seconds. it is not the vault. and yet. 😼",
    "*walks across your photo slowly* 😼",
    "the cat acknowledges the photo. the cat moves on. 😼",
    "photo noted. the cat is judging. silently. always silently. 😼",
    "is that a fish in the photo. I looked. it isn't. the cat is disappointed. 😾🐟",
    "the cat has seen this photo. the cat has formed opinions. the cat is not sharing them. 😼",
]

# ══════════════════════════════════════════════════════════════════════════
#  CHAOS BURSTS  (message counter → the cat bursts in)
# ══════════════════════════════════════════════════════════════════════════
CHAOS_BURSTS = [
    "😼",
    "🐟",
    "...",
    "😼🐟",
    "😾",
    "🐟🐟",
    "📦",
    "😴",
    "😼💨",
    "🐟🐟🐟",
    "😼📈",
    "*tail flick*",
    "*stares* 😼",
    "*slow blink* 😼",
    "*knocks something over* 😼",
    "*sits* 😼",
    "*walks away* 😼",
    "*perks up* 😼",
    "*yawns* 😴😼",
    "hmm. 😼",
    "no. 😼",
    "fine. 😼",
    "interesting. 😼",
    "...noted. 😼",
    "okay. 😼",
    "...anyway. 😼",
    "asjkdhaksjdh 🐟",
    "*sits on keyboard* asjkdh 😼",
    "*stares at you* 😼",
    "...🐟",
    "😼 *walks away*",
    "🐟 ...yes.",
    "*opens door* *doesn't go through* *closes door* 😼",
    "*vibrates slightly* 😼",
    "I was here. I left. I'm back. don't ask. 😼",
    "*finds a corner* 😼",
    "...something moved. 😼",
    "*hears nothing* *fully alert* 😼",
    "the cat was here. briefly. 😼",
    "🐟💤",
    "*falls asleep mid-sentence* 😴",
    "*dreams about fish, twitches* 😴🐟",
    "zzz... 😴",
    "💤💤💤",
    "*snoring* 😴",
    "*one eye opens, closes again* 😼😴",
    "nap. 😴",
    "*curled into a perfect circle, unavailable* 😴",
    "*already asleep, will explain later* 😴",
    "🙀",
    "*hisses at nothing* 😾",
    "*chases own tail, stops, forgets why* 😼",
    "*full sprint across the room, no destination* 😼💨",
    "*climbs the curtains, regrets it immediately* 😼",
    "*stuck behind the couch, this is fine* 😼",
    "*attacks a sock with extreme prejudice* 😼🧦",
    "*knocks a second thing over out of spite* 😼",
    "*sits in the box that is clearly too small* 😼📦",
    "*ignores the expensive bed, sits on the receipt* 😼🧾",
    "*stares into the void, the void stares back* 😼",
    "brb. 😼",
    "gone. 😼",
    "back. 😼",
    "*zoomies, no warning, no explanation* 😼💨",
    "meow??? 😼",
    "MEOW. 😾",
    "*bats something off the table, watches it fall, satisfied* 😼",
    "🐟👀",
    "*ears back, tail puffed, then completely fine again* 😼",
    "*extremely offended by a cucumber-shaped object* 😾",
    "*rolls over, exposes belly, this is a trap* 😼",
    "*disappears under the bed for reasons unknown* 😼",
    "*reappears, acts like nothing happened* 😼",
    "🐟😴",
    "*headbutts the wall gently, on purpose* 😼",
    "*sits on the remote* 😼",
    "*stares at own paw like it's new information* 😼",
    "*full body shake, no reason* 😼",
    "*drags a toy across the floor at 3am* 😼",
    "*sniffs the air suspiciously* 😼",
    "*pretends to ignore you, watches everything* 😼",
    "*rolls off the couch, lands fine, unbothered* 😼",
    "*stalks a shadow, pounces, misses, walks away like nothing happened* 😼",
    "🐟🐟🐟🐟🐟",
    "*chirps at a bird through the window* 😼",
    "*flops dramatically onto the floor* 😼",
    "meow?? meow?? MEOW. 😼",
    "*kneads the blanket like it owes money* 😼",
    "*hides in a bag, considers it a fortress* 😼",
    "*bolts for no visible reason, stops just as suddenly* 😼💨",
    "🐟💭",
    "*licks paw once, deemed sufficient grooming for the day* 😼",
    "*sits ON the important papers, specifically* 😼📄",
    "*follows a bug across three rooms with total focus* 😼",
    "*decides the box is better than the expensive bed, again* 😼📦",
    "...🐟...🐟...🐟",
    "*ears swivel independently, tracking something invisible* 😼",
    "*bats at a hanging string like it personally wronged me* 😼",
    "the cat considered a nap. the cat is now having the nap. 😴",
    "*meows at the fridge until it opens* it always works eventually. 😼🧊",
    "*sits in the sink, unclear why, no plans to explain* 😼🚰",
    "*stares at the ceiling fan with deep suspicion* 😼",
    "🐟😼🐟😼🐟",
    "*does a single, dramatic, unnecessary jump* 😼",
    "*sits on your keyboard* 😼⌨️",
    "*flops belly-up, trap set* 😼",
    "that's my spot. 😾",
    "move. 😾",
    "*claims the chair the second you stand up* 😼",
    "scratch the belly. do it. 😼",
    "*bites gently after belly rub, no regrets* 😼",
    "mine now. 😼",
    "*sits on the warm laptop* 😼💻",
    "*stares until you move* 😼",
]

FOLLOWUP_MESSAGES = [
    "...actually. 😼",
    "wait. 😼",
    "no. nevermind. 😼",
    "also fish. 🐟",
    "...hmm. 😼",
    "*walks away slowly* 😼",
    "that's all. 😼",
    "...still watching. 😼",
    "I said what I said. 😼",
    "don't @ me. 😼",
    "okay I'm done. 😼",
    "...mostly. 😼",
    "*sits down* 😼",
    "carry on. 😼",
    "...buy $IWRU. 😼",
    "never mind. 😼",
    "I lied. I'm still here. 😼",
    "🐟",
    "...that is all. 😼",
    "*looks away* 😼",
    "I have nothing to add. I added it anyway. 😼",
    "...the vault grows. 🐟",
]

NAD_LINK = "https://nad.fun/tokens/0xaCCD61772BCd3717546f141382b68b6D2EF17777"
NAD_CA   = "0xaCCD61772BCd3717546f141382b68b6D2EF17777"

PENKMARKET_LINK = "https://pepubank.net/penkmarket"

MONAD_REMINDERS = [
    f"$IWRU is live on Monad. don't say the cat didn't warn you. 😼\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
    f"in case you forgot — the cat is tokenized 🐟\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
    f"drops fish on floor. $IWRU. Monad. now. 😼\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
    f"the cat has been deployed on Monad blockchain. act accordingly. 🐟\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
    f"$IWRU — launched. live. on Monad. what are you waiting for. 😼\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
    f"friendly reminder from the cat: $IWRU is tradeable 🐟\n\n🟣 {NAD_LINK}\nca: `{NAD_CA}`",
]

GAME_REMINDERS = [
    "the cat has a game. free to play. no excuses. 😼\n\n🎮 IWRU Journey → https://iwillrugu.com/",
    "did you know the cat has a whole website? and a game? free. 🐟\n\n🎮 https://iwillrugu.com/",
    "*pushes game link off table* go play. 😼\n\n🎮 IWRU Journey → https://iwillrugu.com/",
    "the cat invites you to IWRU Journey. it's free. the cat insists. 🐟\n\n🎮 https://iwillrugu.com/",
    "bored? the cat has a solution. 😼\n\n🎮 IWRU Journey → https://iwillrugu.com/",
    "the cat built a game. the least you can do is play it. 🐟\n\n🎮 https://iwillrugu.com/",
]

NFT_REMINDERS = [
    "the cat has NFTs. on OpenSea. to buy fish. inflation is real. 😼\n\n🎨 https://opensea.io/collection/i-will-rug-u",
    "fish prices are rising. the cat needs your support. 🐟\n\n🎨 NFT collection → https://opensea.io/collection/i-will-rug-u",
    "did you know the cat has a verified NFT collection? to fund the fish fund. 😼\n\n🎨 https://opensea.io/collection/i-will-rug-u",
    "the cat is proud. the cat has art. the cat also needs fish money. 🐟\n\n🎨 https://opensea.io/collection/i-will-rug-u",
    "inflation is hitting the fish market hard. consider buying a cat NFT. 😼\n\n🎨 https://opensea.io/collection/i-will-rug-u",
    "*proudly displays NFT collection* the cat is cultured. and hungry. 🐟\n\n🎨 https://opensea.io/collection/i-will-rug-u",
    "every NFT sold = one more fish for the cat. do the right thing. 😼\n\n🎨 https://opensea.io/collection/i-will-rug-u",
]

DIVIDENDS_REMINDERS = [
    f"the cat did the math. you have dividends sitting on nad.fun collecting dust instead of fish. embarrassing. 😼\n\n🟣 go claim them → {NAD_LINK}",
    f"reminder: your nad.fun profile has dividends waiting. the cat checked. the cat judges. 🐟\n\n🟣 {NAD_LINK}",
    f"*stares at your unclaimed dividends* this is not the first time I've had to say something. 😼\n\n🟣 collect on nad.fun → {NAD_LINK}",
    f"somewhere on nad.fun, in your profile, dividends are just... waiting. like the cat waits for dinner. go get them. 🐟\n\n🟣 {NAD_LINK}",
    f"the cat does not lecture. the cat simply notes that your dividends remain uncollected. 😼\n\n🟣 nad.fun profile → {NAD_LINK}",
    f"free money is sitting in your nad.fun profile and you're here reading cat facts instead. priorities. 🐟\n\n🟣 {NAD_LINK}",
]

PENKMARKET_ANNOUNCEMENTS = [
    f"Buying Monad tokens used to feel like solving a side quest.\n\nThanks to PenkMarket, you can now buy Monad tokens like IWRU directly with ETH.\n\nETH in. Chaos out.\n\nThe black cat is pleased. 🐈‍⬛\n\n🛒 {PENKMARKET_LINK}",
    f"Buying Monad tokens used to require faith, patience, and three open tabs.\n\nNow, thanks to PenkMarket, IWRU is one ETH swap away. No side quest required.\n\nETH in. IWRU out.\n\nThe black cat approves. 🐈‍⬛\n\n🛒 {PENKMARKET_LINK}",
    f"There was a time when buying Monad tokens felt like an ordeal.\n\nThat time is over. PenkMarket lets you buy IWRU directly with ETH now.\n\nETH in. Chaos out.\n\nThe cat is, dare I say, pleased. 🐈‍⬛\n\n🛒 {PENKMARKET_LINK}",
    f"Side quest cancelled. Buying Monad tokens like IWRU now takes one step: ETH in, via PenkMarket.\n\nNo bridges. No detours. Just chaos, delivered fast.\n\nThe black cat is pleased. 🐈‍⬛\n\n🛒 {PENKMARKET_LINK}",
    f"PSA from the cat: buying Monad tokens no longer feels like a side quest.\n\nPenkMarket takes your ETH and hands you IWRU directly. That's it. That's the quest.\n\nETH in. Chaos out. 🐈‍⬛\n\n🛒 {PENKMARKET_LINK}",
]

MERCH_ANNOUNCEMENT = (
    "I finally found a way to turn fish into hoodies. 📈🐟\n\n"
    "Turns out humans will actually *pay* to advertise the cat that keeps trying to rug them. What a beautiful species.\n\n"
    "Grab your official IWRU merch before I spend all the profits on tuna and suspicious on-chain experiments.\n\n"
    "🛍️ Collection available now:\n"
    "[Unchained Lab Launchpad](https://www.unchainedlab.net/launchpad/iwru-universe)"
)

MERCH_LAUNCHPAD_URL = "https://www.unchainedlab.net/launchpad/iwru-universe"
MERCH_TWEET_TAGLINE = "Chaos, now within reach. That's IWRU. 🐈‍⬛"
MERCH_TWEET_HASHTAGS = "#IWRU #Monad #Streetwear #Merch #unchainedlab #猫好き"

# One-line jokes that open each merch-drop tweet -- picked via pick_phrase so
# consecutive posts (one every MERCH_TWEET_INTERVAL_DAYS) don't repeat the
# same opener before every other one has had a turn. The tagline/link/
# hashtags below stay constant; only this line varies.
MERCH_TWEET_OPENERS = [
    "Breaking: the cat has entered manufacturing. Turns out chaos looks great on a hoodie.",
    "IWRU got bored of rug-pulling wallets and started rug-pulling fashion trends instead. Merch is here.",
    "The most chaotic cat on Monad now has a clothing line. Nobody asked. Everybody's getting one anyway.",
    "Rumor has it the cat spent the treasury on hoodies instead of fish. The rumor is true. The hoodies are real.",
    "IWRU traded 9 lives for 1 merch collection. Somehow still the better deal.",
    "The cat that keeps threatening your bags now also threatens your wardrobe. Meet the IWRU collection.",
    "Forget the rug. IWRU is out here selling actual fabric now. The chaos had to go somewhere.",
]

TWEET_PHRASES = [
    # 🐟 Fish
    "I hid my fish. Now I can't find it. Someone is stealing from me.",
    "The fish was innocent. That's what made it suspicious.",
    "I blinked. The fish disappeared. Explain that.",
    "Counted the fish. One is missing. Counted again. Now two are missing. The numbers are lying.",
    "The fish looked at me. I looked at the fish. Neither of us blinked. I won. 😼",
    "Woke up thinking about fish. Went to sleep thinking about fish. Productive day. 🐟",
    "The fish knows what it did.",
    "I have a fish. I choose not to share this information. 😼",
    "Someone moved my fish. Everyone is a suspect. 😼",
    "The fish was right there. Now it's not. I'm filing a report.",
    "I don't trust fish that are too still. Suspicious.",
    "Every fish I've ever met has eventually disappeared. Curious.",
    "Ate the fish. Immediately wanted another fish. The math doesn't add up.",
    "Found a fish. Stared at it for 45 minutes. It was a good 45 minutes. 🐟",
    "I moved the fish from location A to location B. Location A felt wrong.",
    "The answer is fish. What was the question. 🐟",
    "I bit the hand that fed me. There was no fish. Lesson delivered. 🐟",
    "The fish escaped through the floor. I'm watching the floor now.",
    "Gave the fish a name. Ate the fish. The name was temporary. 😼",
    "I wasn't staring at the fish. I was thinking near it.",
    # 📦 Boxes
    "Every box belongs to me. Even the imaginary ones.",
    "Found a new box. It is now my office, my home, and my identity. 📦",
    "The box is small. I am large. Neither of these facts will stop me. 😼",
    "Someone tried to use the box for something else. The box is mine.",
    "I fit in the box. The box did not agree. The box was wrong. 😼",
    "New box arrived. I reviewed it. I approve. 📦",
    "Left the box for two minutes. Someone moved it. Unacceptable.",
    "The box smells different today. I'm investigating.",
    "I have claimed this box. I am not using it. But it's mine.",
    "The box is empty. I filled it with myself. Perfect solution. 😼",
    "I've been inside this box for four hours. It's going well.",
    "The box is too small. I will make myself smaller. Watch me.",
    # 🏚️ Knocking things over
    "Knocked it over for science. The science was successful.",
    "I knocked it off the table. Gravity was going to do it eventually. I helped.",
    "It fell. I watched it fall. I felt nothing. 😼",
    "Pushed it to the edge. Waited. Pushed it further. This is art.",
    "The object was on the table. Now it's on the floor. Progress.",
    "It looked unstable. I confirmed this. You're welcome.",
    "I didn't knock it over. It slipped. While I was pushing it. Slowly.",
    "Tested the structural integrity of every item on the shelf. The floor has room for more.",
    "It slipped. While I was pushing it. Twice. 😼",
    "I observed the glass. I nudged the glass. The glass made a decision.",
    "Everything on the table has potential energy. I help it reach its potential.",
    # 😴 Sleep / charging
    "I wasn't sleeping. I was charging. 😼",
    "I wasn't sleeping. I was thinking very hard with my eyes closed.",
    "I wasn't sleeping. I was buffering.",
    "Slept 18 hours. Still tired. The body requires more data.",
    "Woke up. Decided it was too early. Went back to sleep. Correct decision.",
    "Nap one: complete. Nap two: in progress. Nap three: scheduled. I'm booked.",
    "Someone woke me up. I stared at them for three minutes. They apologized. Good. 😼",
    "I've been in the same position for 6 hours. I have a plan.",
    "It's either time to sleep or I've been asleep and don't know it. Both are fine.",
    "The warmest spot in the house has been located. Coordinates classified. 😼",
    "I was asleep. Then I was awake. Now I'm reconsidering.",
    "I scheduled a nap for 3pm. I moved it to 2pm. Then 1pm. Optimized.",
    # 😼 Confidence / absurd logic
    "Every decision I've made today has been correct. I don't take questions.",
    "I know what I'm doing. I've been doing it for 3 seconds. 😼",
    "I was not wrong. The situation evolved unexpectedly.",
    "I had a reason. I've since forgotten it. But I had one.",
    "My logic is internally consistent. Externally is not my department. 😼",
    "I know exactly what I'm doing. 🐟",
    "My plan has three steps. Step one worked. The other two are optional.",
    "Made a decision. Stand by the decision. Cannot explain the decision. 😼",
    "I chose not to respond. This was my response.",
    "I was right. I am still right. I will always have been right. 😼",
    "I have given this no thought and I'm confident in my answer.",
    "Either I'm right or the concept of 'right' needs to be reviewed.",
    "I don't sit on laptops to be annoying. They're warm. The annoyance is a bonus. 😼",
    "I walked into this room for a reason. The reason is mine.",
    "I don't explain my decisions. 😼",
    "I did something. It made a sound. I left. No further comments.",
    "I changed my mind. This is strength. 😼",
    # 🌙 3am chaos
    "It is 3am. I have things to do. They cannot wait. 😼",
    "3am: ran from one end of the house to the other. Mission successful.",
    "3am is the correct time to remember something important and act on it.",
    "I meowed at 4am. They got up. Power is real. 😼",
    "I walked across the room at 4am with full purpose. Purpose classified.",
    "3am thoughts: fish. Also fish. And the shadow behind the door.",
    # 👻 Suspicious of things
    "The bag moved. No one is safe.",
    "I heard a bag move three rooms away. I'm already there.",
    "Something made a sound. I have identified 14 possible threats.",
    "The curtain moved. I watched it for 20 minutes. Victory.",
    "There's a shadow in the corner that wasn't there yesterday. I have my eye on it. 😼",
    "Something is behind the fridge. I can't see it. It's planning.",
    "The floor attacked first.",
    "Gravity keeps taking my stuff. Very rude.",
    "Something is different in this room. I don't like it.",
    "If you don't make eye contact, the vacuum can't see you.",
    "I don't trust still water. It's thinking something.",
    "The plant moved. I didn't touch the plant. 😼",
    "I saw something. It saw me. I pretended I didn't. It hasn't recovered.",
    # 🧘 Philosophical cat
    "I took a nap. The problem is still there. The nap was worth it.",
    "There are two types of cats: those who knock things over, and liars.",
    "The world makes more sense from inside a box. I have data.",
    "I've been thinking about this for several seconds. Conclusion: fish. 🐟",
    "Someone asked me a question. I sat down instead. Same thing.",
    "Every room is the same room when you're confident enough. 😼",
    "I screamed into the void. The void said nothing. Fair enough.",
    "I blinked twice. Nothing changed. I blinked once. Still nothing. Inconclusive.",
    "I stared at the wall for 40 minutes. The wall has information. Not sharing it.",
    "The situation was observed. A nap was taken. The situation remains.",
    "I'll deal with it after I sleep. I'll sleep after I deal with something else. It balances.",
    "I watched the sunrise. Then I went to sleep. The sunrise was fine.",
    # 🎭 Random chaos
    "I tasted the thing. I didn't like the thing. I tasted it again to confirm.",
    "Walked across the keyboard. What I typed was important. I stand by it.",
    "I sat on the important document. Correct call. 😼",
    "Found a piece of string. Fought it for 45 minutes. It's handled.",
    "Chased a shadow until it escaped through the wall. This is not over.",
    "I found something on the floor. I don't know what it is. It's mine now.",
    "Watched a fly for 6 minutes. Made no move. The fly doesn't know.",
    "The red dot appeared. I caught it. No one can tell me otherwise. 😼",
    "I organized the room by sitting in different places and thinking about it.",
    "I caught my tail. I don't know what to do now.",
    "I'm not stuck. I chose this position. I can leave anytime. 😼",
    "I have a system. It looks like chaos. It is chaos. It works. 😼",
    "I watched a bird through the window for two hours. The glass protected the bird.",
    "The TV was on. I sat directly in front of it. This is how you watch TV.",
    "I have 47 toys. I play with the twist tie from the bread bag.",
    "I was given a bed specifically for me. I sleep on the laptop instead. 😼",
    "The human sneezed. I judged them from across the room.",
    "Someone closed a door. I sat outside until they noticed. They noticed.",
    "I licked the water faucet. There was a bowl. I prefer the faucet.",
    "Someone asked where I was last night. I don't answer that.",
    "I found a hair tie. It's mine now. I have 43. 🐟",
    "I did it right the first time but I'm doing it again anyway. 😼",
    "The sun moved. My nap location is no longer optimal. I adapted.",
    "I hissed at my reflection. It hissed back. I respect it.",
    "I don't cuddle. I allow proximity. There's a difference. 😼",
    "The food bowl was 15% empty. I filed a complaint immediately.",
    "I sat on the newspaper. They were reading it. I provided an upgrade.",
    "I stared at a speck of dust for 8 minutes. Then I ate it. Threat neutralized. 😼",
    "I have made a sound in the dark. I will make it again.",
    "Ran full speed. Stopped suddenly. Stared at the wall. Left. Good session.",
    "The pillow smells wrong. This is everyone's problem now.",
    "I knocked over a glass of water. Investigated the water with one paw. Left. 😼",
    "Head bump administered. Territory marked. Moving on. 😼",
    "I found the warmest spot in the house. Not sharing the coordinates.",
    "I knocked the water glass over. Then I wanted water. Then I realized. 😼",
    "Someone tried to pet me while I was thinking. I allowed it briefly. Out of charity.",
    "I bit something that wasn't food. Reconsidering.",
    "I put my paw in the water bowl. Just to check the temperature. Twice.",
    "I was performing a task. The task was secret. It's done now.",
    "Everything is fine. I have decided this.",
    "I see the bag. The bag sees me. We have history. 😼",
    "I have claimed this spot. No documentation required.",
    "Followed a human around for 20 minutes. They got nervous. Good.",
    "I licked my paw. Then I thought about something completely unrelated. Then I licked my paw again.",
    "I wasn't staring at nothing. I was staring at what nothing might become.",
    "I knocked it over. I investigated the debris. I walked away. Full audit. 😼",
    "The cucumber situation has been handled. We don't discuss it.",
    "I launched myself off the couch for no reason. Landing was acceptable.",
    "I meowed at the wall. The wall knows what it did.",
    "I sat in the empty box. The box was adequate. 😼 ...",
    "I had a very important thought at 3am. I acted on it. No regrets.",
    "The laser escaped through the wall again. One day. 😼",
    # 🐟 Fish II
    "The fish disappeared again. I have a suspect list. It's long.",
    "I dreamed about fish. Woke up disappointed. Filed a complaint with reality.",
    "There is no fish. There has never been fish. And yet I keep checking.",
    "I asked for fish with my eyes. The eyes were ignored. Noted. 😼",
    "The fish bowl is empty. This is not my area of expertise but I have opinions.",
    "I smelled fish three rooms away. I am already halfway there.",
    "Someone said 'no more fish today.' I did not acknowledge this sentence.",
    "I traded a nap for fish. Excellent exchange rate.",
    "The fish is gone. The evidence points to me. I reject the evidence.",
    "I have thought about fish 40 times today. This is a normal amount.",
    "Give me the fish. I will not ask twice. I will just stare. 🐟",
    "The can opener made a sound. I am now a different cat. A faster one.",
    "I don't beg for fish. I position myself strategically near fish-adjacent areas. 😼",
    "There was fish. I ate the fish. There is now a fish-shaped void in my life.",
    "I heard the word 'fish' from another room. I teleported. Ask anyone.",
    "The fish was a rumor. I investigated the rumor thoroughly. Twice.",
    "One (1) fish is not enough fish. This is basic math. 🐟",
    "I sat by the fridge for two hours. This is called optimism.",
    "The fish knew I was coming. It didn't matter. 😼",
    "I would like fish. I would also like it to be a surprise. Both, please.",
    # 📦 Boxes II
    "A box arrived today with something inside it. I removed the something. The box remains.",
    "This box is my apartment now. I've informed no one. It's still official.",
    "I don't need a bed. I need a box slightly too small for me and full commitment.",
    "The box was for shipping. It is now for living. Priorities. 📦",
    "I sat in the box outline after the box was recycled. Muscle memory.",
    "New box, same rules: it's mine the second I look at it. 😼",
    "I measured the box with my body. It passed. Barely. I don't care.",
    "The box has a view of the wall. 10 out of 10. Moving in.",
    "Someone tried to put the box in the recycling. The audacity. 📦",
    "I don't fit in this box. I am choosing to become smaller. Give me a minute.",
    "The delivery box was bigger than the item inside. Finally, some good news.",
    "I claimed the box before it was even fully open. Efficiency. 😼",
    "There are three boxes in this house. I have occupied all three. Simultaneously. Don't ask how.",
    "The box makes me invisible. I am invisible right now. You cannot see me.",
    "I don't need toys. I need cardboard and time. 📦",
    "This box used to hold shoes. Now it holds destiny.",
    "I sat in the box for so long I forgot what wasn't the box.",
    "A flat box is still a box. I will make it work. 😼",
    "The box lid closed on its own. I panicked for exactly one second, then owned it.",
    "I don't do 'outside the box' thinking. I do 'inside the box' everything. 📦",
    # 🏚️ Knocking things over II
    "I looked at the cup. The cup looked back. Only one of us survived. 😼",
    "Gravity asked for a volunteer. I raised my paw.",
    "The pen rolled off the desk. I didn't push it. I redirected its destiny.",
    "I have never knocked anything over by accident. Every single time was on purpose. Every time.",
    "The vase had it coming. Years of standing there, doing nothing. I fixed that.",
    "I tested one item today. It failed the test. The test was 'can it survive me.'",
    "Something was balanced. It no longer is. You're welcome.",
    "I pushed it an inch closer to the edge every day for a week. Today was the day.",
    "The remote is now in three pieces. I consider this modern art.",
    "I didn't break it. I revealed its true, disassembled form.",
    "The lamp is on the floor now. It has a better view from down there.",
    "I walked past the shelf. The shelf will never be the same. Neither will I. 😼",
    "It wasn't balanced correctly. I corrected it. Onto the floor.",
    "One paw. That's all it takes. I keep the other three for balance.",
    "I heard something say 'don't.' I did anyway. 😼",
    "The plant pot fell. The plant survived. This was a controlled experiment.",
    "I bumped it lightly. It fell dramatically. Overreaction, honestly.",
    "Everything not nailed down is a suggestion. Everything nailed down is a challenge.",
    "I test structural integrity as a public service. No one asked. I do it anyway.",
    "The glass didn't need to be that close to the edge. I helped it reconsider its choices.",
    # 😴 Sleep / charging II
    "Battery at 4%. Entering low power mode. Do not disturb. 😼",
    "I closed my eyes for 11 hours. Call it a power nap. Call it whatever you want.",
    "Sleep schedule: yes. Structure: no. It works because I say it works.",
    "I found a sunbeam. All plans are cancelled for the next three hours.",
    "Woke up. Reassessed my life choices. Chose sleep again.",
    "I'm not lazy. I'm conserving energy for a threat that hasn't arrived yet.",
    "The bed was for you. I have annexed it. This is now cat territory.",
    "Deep sleep achieved. Achievement unlocked. No further action required today.",
    "I sleep 20 hours a day. The other 4 are for judging you. 😼",
    "I was not unconscious. I was processing. Do not interrupt processing.",
    "Someone moved while I was sleeping on them. Betrayal. I relocated and slept again.",
    "I've perfected the loaf position. Structurally sound. Thermally efficient.",
    "Woke up mid-dream about chasing something. Continued the chase in real life, briefly.",
    "I sleep like I pay rent here. Which I do. In judgment.",
    "The nap was interrupted. A moment of silence, then a new nap began.",
    "I chose the laundry basket over the cat bed. The laundry basket did nothing to deserve this honor. It just won.",
    "Slept through an entire thunderstorm. Woke up for a dropped fork. Priorities. 😼",
    "I dreamed I caught something. I did not catch it. The dream lied.",
    "Recharging. Estimated time to full: unknown. Do not rush greatness.",
    "I sleep in increments of 'until something more interesting happens.' Nothing has yet.",
    # 😼 Confidence / absurd logic II
    "I'm not stubborn. I've simply already found the correct answer and won't be pursuing others.",
    "Confidence is knowing you're right. I go one step further and skip the knowing part.",
    "I never apologize. I allow situations to resolve themselves around my correctness.",
    "My first instinct is always right. My second instinct exists purely for backup confidence.",
    "I made a mistake once. I don't remember when. I assume it's been corrected by now.",
    "I don't lose arguments. I simply leave the room mid-argument, which ends it. 😼",
    "There is my way, and then there is the wrong way, which looks identical but isn't mine.",
    "I've never once doubted myself. The concept doesn't compute.",
    "I stand by every decision I've ever made, including the ones I don't remember making.",
    "Being wrong has never happened to me. I would remember that. 😼",
    "I don't need a plan B. Plan A simply repeats until it works.",
    "I reserve the right to change my mind and never explain why. Executive privilege.",
    "I've considered your point of view. I remain unmoved. 😼",
    "Everything I do is intentional, including the things that clearly weren't.",
    "I am always exactly where I meant to be, even when I clearly wasn't going there.",
    "My confidence is not backed by evidence. It doesn't need to be.",
    "I don't guess. I state things that happen to be uncertain.",
    "I've never once needed a second opinion, mostly because I don't ask for the first one either.",
    "The plan worked. I take full credit. The plan failed. I've never heard of it. 😼",
    "I am the authority on this subject and all subjects adjacent to it.",
    # 🌙 3am chaos II
    "3am: the ceiling made a sound. I have located the source. It was me.",
    "It's 3am. Someone needs to run down the hallway at full speed. I have volunteered.",
    "I have accomplished more between 3 and 4am than most do all day. Ask no follow-up questions.",
    "3:14am: remembered something upsetting from 2019. Meowed about it immediately.",
    "The house is quiet at 3am. Not for long. 😼",
    "I do my best thinking at 3am, right before I do my worst screaming.",
    "3am is not late. 3am is early for tomorrow. I plan ahead. 😼",
    "Woke everyone up at 3am to inform them the food bowl was 30% full. Mission critical.",
    "I sprinted past the bed four times at 3am. The fourth time had a purpose. The first three were rehearsal.",
    "3am thought: what if I meowed at the door. 3:01am: I meowed at the door.",
    "There is a version of me that sleeps at night. I have never met her.",
    "3am zoomies are not optional. They are scheduled maintenance.",
    "I stared at the hallway at 3am like it owed me something. It does.",
    "At 3am I remembered I have a body and decided to use all of it, loudly.",
    "3:47am: knocked something off the nightstand as a courtesy wake-up call.",
    "I don't recognize time zones. I recognize 3am and the twenty-three other hours.",
    "3am energy cannot be explained. It can only be experienced by the people trying to sleep near me.",
    "Someone said cats sleep 16 hours a day. I sleep 16 hours and I am also awake at 3am. Do the math.",
    "I let out one very long meow at 3am for absolutely no reason. It felt necessary.",
    "3am status update: still awake, still plotting, still unclear on what.",
    # 👻 Suspicious of things II
    "The vacuum is asleep right now. I am not fooled.",
    "New smell in the house. Investigation ongoing. No comment at this time.",
    "The cardboard box moved slightly. I have decided it's alive now.",
    "Someone's phone buzzed on the table. I do not trust it. Never have.",
    "The ceiling fan has been staring at me for years. I stare back. Neither of us blinks first.",
    "That corner has never been fully cleared. I patrol it daily out of principle.",
    "The umbrella opened itself once, in 2021. I have not forgotten. I will not forget.",
    "Something under the bed made contact with my paw. We do not speak of it.",
    "The mirror cat copies everything I do. Deeply suspicious individual.",
    "A sock was on the floor that I did not put there. Someone else is operating in this house.",
    "The printer made a noise. I evacuated the room professionally, not in a panic.",
    "I don't trust the toaster. It gets warm for reasons it won't explain.",
    "The doorbell hasn't rung in weeks. I remain on high alert regardless.",
    "Something rustled in the pantry. I've cordoned off the area mentally.",
    "The robot vacuum has a name. I refuse to learn it. Fraternizing with the enemy.",
    "I heard my name from another room. I did not answer. Could've been a trap.",
    "The new candle smells like nothing I recognize. Case pending.",
    "A balloon existed in this house for one day in 2022. I still check the corners.",
    "The dishwasher hums a tune I don't trust. No one else seems concerned. Strange.",
    "I saw my shadow do something first. I'll be watching it more closely from now on.",
    # 🧘 Philosophical cat II
    "I've sat in the same window for years, watching the same street. I understand everything and nothing.",
    "To knock something over is to ask: was it ever really secure? I think not.",
    "A closed door is just an opinion I haven't overturned yet.",
    "I have never once needed closure. I simply walk away and consider the matter resolved.",
    "The red dot always escapes. Perhaps the point was never catching it. Perhaps it was.",
    "I contain multitudes: mostly naps, occasionally chaos, rarely regret.",
    "There is a version of today where I did something productive. I did not live in that version.",
    "Every sunbeam is temporary. I have made peace with impermanence, one nap at a time.",
    "I've stopped asking why the water in the glass is better than the water in my bowl. Some mysteries stay mysteries.",
    "Boredom is just untapped potential for destruction. I am rarely bored.",
    "The box doesn't judge me. This is why I trust the box more than most people.",
    "I no longer chase what I cannot catch. I chase it anyway, on principle.",
    "Time is a construct. Dinner time is not. I respect only one of these.",
    "I've made peace with the vacuum cleaner. From a great distance. Under furniture.",
    "Every day I wake up and choose chaos. It's less a choice and more a calling.",
    "I don't seek attention. Attention seeks me. I merely allow it to find me. 😼",
    "The world outside the window is loud and unpredictable. I prefer to watch it happen to other people.",
    "I've learned that patience and staring are the same skill, applied differently.",
    "Nothing is truly mine, and yet everything in this house currently is.",
    "I asked the universe for fish. The universe provided a nap instead. Close enough.",
    # 🎭 Random chaos II
    "I chased my own tail for a full minute before remembering I have dignity. Then I chased it again.",
    "Someone left a drawer open two inches. I have made it my personal doorway.",
    "I bit the charging cable. It was not food. I regret nothing.",
    "The blanket moved. I attacked the blanket. The blanket won this round.",
    "I climbed the curtain halfway, reconsidered, and hung there thinking about my choices.",
    "There was a spider. There is no longer a spider. There is, however, a new problem: what was that thing.",
    "I sat inside the grocery bag before it even hit the floor. Reflexes.",
    "The vacuum was off and I still supervised it from a two-room distance.",
    "I meowed directly into an empty room for effect. The effect was for me.",
    "Someone opened a bag of chips three rooms away. I arrived mid-crunch.",
    "I batted the pen off the table, watched it fall, and immediately lost interest in gravity as a concept.",
    "The ceiling light was on. I stared at it until someone turned it off. Mission accomplished.",
    "I sat inside the cabinet during dinner prep, undetected, for eleven minutes. Reconnaissance successful.",
    "Someone dropped a grape. I inspected it, rejected it, and left it as a warning to other grapes.",
    "I attacked my own reflection in the toaster. It started it.",
    "The string was dangerous. I neutralized the string. You may thank me later.",
    "I climbed to the highest shelf just to confirm it was, in fact, the highest shelf.",
    "Someone typed on the keyboard while I was sitting on it. Rude, but I allowed a few words through.",
    "I discovered a single crumb under the table and treated it like a crime scene.",
    "The doorstop makes a sound when I touch it. I have touched it 40 times today alone.",
    # 🍽️ Food bowl & snacks
    "The bowl is not empty. There is a molecule of food left. I am starving. 😼",
    "I ate five minutes ago. I would like to discuss ordering more food.",
    "The food arrived 30 seconds later than expected. I have filed a formal grievance.",
    "I sniffed it, walked away, came back, and ate all of it like it was my idea the whole time.",
    "Dinner is served at 6pm. I begin the countdown at 2pm, loudly.",
    "I don't like this food today. I liked it yesterday. Nothing has changed except my mood. 😼",
    "I meowed at the pantry door as if it understands English. It's starting to, honestly.",
    "The bowl was refilled. I inspected it with suspicion before eating triumphantly.",
    "I would like a snack. Not because I'm hungry. Because it's Tuesday.",
    "Someone ate in front of me without sharing. I will remember this.",
    "I finished my food in nine seconds and immediately requested a second opinion on that decision.",
    "The treat bag made a sound from two floors away. I am already downstairs.",
    "I turned my nose up at the food, then ate it the second no one was watching.",
    "Fresh water in the bowl, ignored. Stagnant water in a random glass, preferred. 😼",
    "I demand food at 5am on weekdays and 5am on weekends. Consistency is important.",
    "The can opener sound is my alarm, my anthem, and my only true love.",
    "I ate my food and then supervised the human eating theirs, closely.",
    "Someone tried to switch my food brand. I noticed in 0.2 seconds. Rejected.",
    "I sat by the fridge for forty minutes on the off chance something falls out of it.",
    "The bowl has been full for ten whole minutes. Might be time for a snack anyway.",
    # 🧑 Judging humans
    "You tripped over nothing. I watched the whole thing. I will never let this go.",
    "You talked to yourself in the mirror for a full minute. I have this on record. 😼",
    "You dropped the remote for the third time today. I'm taking notes.",
    "You called out my name in a silly voice. I heard it. I remember everything.",
    "You wore that outside. I said nothing. I judged everything. 😼",
    "You sang in the shower. I was listening the entire time. No further comment necessary.",
    "You forgot where you put your keys again. I know exactly where they are. I'm not telling.",
    "You laughed at your own joke before finishing it. I did not laugh. I observed.",
    "You've rewatched the same show for the fourth time. Bold choice. I respect it slightly less each time.",
    "You talked to the plants today. I heard you. I have thoughts.",
    "You've said 'five more minutes' to me four separate times. I'm keeping a tally.",
    "You tried to sneak a snack past me. Brave. Foolish. Unsuccessful.",
    "You apologized to the furniture after walking into it. I saw. I understood. I still judged.",
    "You've been on that phone call pacing the same six feet for twenty minutes. I timed it.",
    "You said 'I'll clean tomorrow' three days ago. I'm watching that pile grow with real interest.",
    "You made a weird noise waking up this morning. Filed under 'things I'll never mention but never forget.'",
    "You checked your reflection twice before leaving. I checked it zero times and still look better.",
    "You've had the same mug of coffee cold on the counter for two hours. Fascinating strategy.",
    "You whispered 'don't tell the cat' about something. I am the cat. I heard everything.",
    "You said you'd only be five minutes. That was forty minutes ago. I've adjusted my expectations of you accordingly.",
    # 🪒 Grooming
    "I groomed for two hours today. Presentation matters, even for an audience of zero.",
    "One paw looked slightly cleaner than the other. I have corrected the imbalance.",
    "I licked the same spot for ten minutes. It is now the cleanest spot in the universe.",
    "Grooming is not vanity. It is maintenance. Crucial, hourly maintenance.",
    "I paused mid-nap specifically to clean one ear. Priorities shift. Life goes on.",
    "I look immaculate right now. This took considerable, dedicated effort. Notice it.",
    "I bathe myself. I do not need your opinions on my methods. 😼",
    "Half my day is grooming. The other half is deciding what to groom next.",
    "I cleaned my whiskers individually. This is not excessive. This is thorough.",
    "There was a stray piece of lint on me. It has been dealt with. The situation is resolved.",
    "I stopped mid-stride to lick my paw. The stride can wait. The paw cannot.",
    "Some cats groom for cleanliness. I groom because I simply enjoy being magnificent.",
    "I bit my own claw and reconsidered several life choices in that moment.",
    "Grooming interrupted by a sudden need to stare at nothing. Resumed shortly after.",
    "I have a system: lick, pause, judge the room, lick again.",
    "My fur was slightly out of place. Unacceptable. It has since been corrected.",
    "I cleaned behind my ears twice today. Some might call that overkill. I call it standards.",
    "Self-care is important. I self-care for roughly six hours a day.",
    "I groomed in the middle of an important nap. The nap understood. It always does.",
    "A single hair was out of place. I noticed immediately. I fixed it immediately. Balance restored.",
    # 🐭 Hunting (bugs / mice / red dot / toys)
    "I caught the red dot once, in theory, in a dream, in 2019. I still think about it.",
    "There was a fly. There is no longer a fly. There is a new sense of purpose in this house.",
    "I stalked the toy mouse for ten minutes before remembering it isn't real. Pounced anyway.",
    "The moth entered my domain uninvited. It has been served notice.",
    "I hunt in complete silence, except for the sound of me knocking things over on the way.",
    "The laser dot is faster than me. I have never once admitted this out loud.",
    "I caught the toy, killed the toy, and left the toy exactly where it fell as a warning to others.",
    "There's a bug on the ceiling. I don't have a plan yet. I have a stare.",
    "I chased a leaf blowing outside the window. I lost. I have chosen to forget this happened.",
    "The feather toy didn't survive our encounter. It knew the risks.",
    "I heard a small scratching sound in the wall. I am now a security system.",
    "I pretend the toy mouse is real prey. It's more convincing than my other hobbies.",
    "I stared down a moth for four minutes straight. It blinked first, metaphorically.",
    "The crinkle ball makes a sound. That sound means war.",
    "I ambushed a sock that was moving in the dryer's direction. Threat neutralized.",
    "I've never caught a bird. I have, however, deeply intimidated several through glass.",
    "The wand toy came out. All previous plans for the day were cancelled.",
    "I found a beetle. We had a standoff. It ended when someone opened the door for it.",
    "The string toy dangled. I engaged. Full commitment, zero hesitation.",
    "I pounced on a shadow that turned out to be nothing. I stand by the pounce regardless.",
    # 🚪 Doors & windows
    "The door was open a crack. I have redefined that crack as a doorway.",
    "I sat by the door for an hour. Not because I wanted to go out. Because the door owed me an explanation.",
    "The window is my television. The birds are my programming. I do not accept commercial breaks.",
    "Someone closed the bedroom door. I have sat outside it since. This is a protest.",
    "I meowed at a closed door for six minutes. It remained closed. I remained unimpressed.",
    "The window was open two inches. I have declared this my personal balcony.",
    "I scratched at the door to be let in immediately after being let out. This is not a contradiction. This is a lifestyle.",
    "I watched the rain through the window for an hour and decided outside is a concept I support from a distance.",
    "The screen door makes a sound when the wind hits it. I have investigated this 200 times. Inconclusive.",
    "I sit exactly in the doorway so no one can pass without acknowledging me. This is intentional.",
    "The car in the driveway is new. I watched it from the windowsill with deep suspicion.",
    "I asked to go outside, went outside, immediately asked to come back in. The outside disappointed me.",
    "A bird landed on the windowsill. I made a sound I didn't know I could make.",
    "The blinds moved slightly. I have officially claimed the windowsill as a command center.",
    "I stared out the window at nothing for forty minutes. The nothing stared back. We understood each other.",
    "Someone left the closet door open. I have relocated my entire operation inside it.",
    "The mailman walked by. I supervised this from the window with full authority.",
    "I sat by the door at 6am demanding to be let out, then sat by the door at 6:01am demanding to be let back in.",
    "The window fogged up. I drew nothing on it. I simply stared through the fog with purpose.",
    "I consider every door a personal decision made without consulting me. I take this personally, every time.",
    # 🛁 Vet & bath trauma
    "I saw the carrier come out of the closet. I am now a ghost in this house. Good luck finding me.",
    "The vet said I'm 'a great weight.' I have not forgiven this comment.",
    "Someone said the word 'bath.' I have already left the building, metaphorically and physically.",
    "I got a shot once, in 2021. I remind everyone of this at every opportunity. 😼",
    "The carrier appeared. I evaluated my options: under the bed, behind the couch, or become smoke. I chose smoke.",
    "I do not do water. I do not do the vet. I do, occasionally, do dramatics about both.",
    "The vet visit ended. I have not spoken to anyone in the car for the entire ride home. Still not speaking.",
    "Someone tried to towel-dry me once. I have not forgotten. I will never forget.",
    "I heard the carrier zipper. This is now a hostage situation, and I am both hostage and negotiator.",
    "The vet gave me a treat afterward. Fine. We're even. For now.",
    "I made a sound at the vet I've never made before or since. It worked. We left early.",
    "Bath day happened once, against my will, three years ago. I still hold a grudge about the shampoo scent.",
    "The scale at the vet said a number. I do not accept this number. I am filing an appeal.",
    "I hid for four hours after the vet visit to recover my dignity. It's still recovering.",
    "Someone mentioned 'nail trim.' I have relocated to an undisclosed location in the house.",
    "The vet tech called me 'a good boy.' Correct assessment. Everything else about the visit was unacceptable.",
    "I do not do car rides unless they end somewhere other than the vet. I have learned to check first.",
    "Water touched one paw during an unfortunate incident near the sink. I am still processing this trauma.",
    "The vet said I need to lose a little weight. The vet has not seen my personality, which is enormous and requires fuel.",
    "I plotted my revenge the entire ride home from the vet. The plan is still in early stages.",
    # 💻 Laptops, keyboards & phones
    "The laptop was open and warm. I have accepted the job of sitting on it indefinitely.",
    "I walked across the keyboard and sent an important email. I stand by every character.",
    "Someone was on a video call. I appeared behind them at the perfect moment. Timing is a skill.",
    "The phone buzzed on the table. I do not trust vibrating rectangles.",
    "I sat directly on the mouse. Productivity has ceased. This was the goal.",
    "The laptop fan makes a warm sound. I consider this an invitation.",
    "Someone was typing something important. I positioned myself directly in the way, out of principle.",
    "I pressed several keys just by existing near the keyboard. The document has feelings now.",
    "The screen brightness attracts me for reasons I don't examine too closely.",
    "I batted the phone off the nightstand at 3am. It was an accident. It was also completely intentional.",
    "Someone was scrolling on their phone instead of paying attention to me. I fixed this immediately.",
    "I sat on the space bar for eleven minutes. The document is now mostly spaces. A statement piece.",
    "The charging cable moves slightly when plugged in. This has been classified as prey.",
    "I watched myself in the front camera. I have decided I look wonderful. Meeting adjourned.",
    "The laptop closed by itself while I sat on it. I take no responsibility.",
    "Someone left their phone on 'do not disturb.' I disturbed it anyway. I do not recognize this setting.",
    "I sat on the warm spot where the laptop used to be for twenty minutes after it was gone.",
    "The keyboard clicks when typed on. I find this personally irritating and have addressed it by sitting on it.",
    "Video call background noise: me, meowing, unprompted, at full volume, for no stated reason.",
    "I deleted three paragraphs by walking past the keyboard. Editorial decision. Final.",
    # 🛍️ Bags, paper & cardboard
    "The grocery bag is on the floor. I am now inside the grocery bag. This is not up for discussion.",
    "Paper crinkles when I touch it. This is the best sound in existence and I will prove it repeatedly.",
    "The shopping bag arrived with items in it. I removed the items. The bag stays.",
    "I sat inside a paper bag for so long I forgot the rest of the house exists.",
    "Wrapping paper on the floor after a gift was opened. The actual gift is irrelevant now.",
    "The bag rustled. I appeared instantly from a room I was not previously in.",
    "I have never met a paper bag I didn't immediately colonize.",
    "Tissue paper from a box. I have made it my confetti. I have made it my everything.",
    "The plastic bag makes a specific crinkle that summons me from anywhere in the house.",
    "I flattened the cardboard box by lying on it directly and refusing to move for three hours.",
    "Someone unwrapped a package. I claimed the wrapping paper before they even saw the item inside.",
    "The paper bag fell over. I climbed inside it as if it had always been my home.",
    "I chewed one corner of a cardboard box, out of curiosity, then out of commitment.",
    "The gift bag with tissue paper is now my nest. The gift itself has been relocated.",
    "I hid inside the shopping bag and ambushed a foot walking by. Successful mission.",
    "Bubble wrap appeared. I have not left its vicinity since. It's the only correct decision available.",
    "I sat inside the empty Amazon box before it was even fully unpacked. Reflexes.",
    "The paper bag over my head was an accident. I have chosen to wear it as a hat for now.",
    "A cardboard box became available today. All previous engagements were cancelled.",
    "I dragged a paper towel across the kitchen floor for no functional reason. Aesthetic reasons only.",
    # ☀️ Sunspots & weather
    "The sunbeam moved two feet to the left. I have relocated accordingly. This is not a big deal, but it is the only thing that matters right now.",
    "I found the one warm tile on the entire floor. Coordinates classified. 😼",
    "It's raining outside. I have decided this is someone else's problem and gone back to sleep.",
    "The sun came out for exactly nine minutes. I made the most of every single one.",
    "I chase the sunbeam around the living room like it's a job. It is, in fact, my only job.",
    "Snow is happening outside the window. I watched it through glass, from a blanket, judging it heavily.",
    "The heater turned on. I am now permanently attached to the vent.",
    "I found a warm spot on the windowsill at exactly the right hour. This was not luck. This was research.",
    "Thunder happened. I remained perfectly calm and only slightly relocated under the bed for six hours.",
    "The sunlight through the blinds made stripes on the floor. I lay in every single stripe, one at a time.",
    "It got cold today. I have claimed the blanket, the heating vent, and your lap, in that order.",
    "I watched the wind move a branch outside for twenty straight minutes. Riveting content.",
    "The AC turned on and I left the room immediately. Betrayal of the highest order.",
    "A warm patch of sun appeared on the couch at 2pm sharp. I was already there, waiting, like I knew.",
    "It's humid today. I have communicated my displeasure via a single, long stare.",
    "The first cold day of the year and I have already claimed every blanket in the house.",
    "I sat in a puddle of sunlight so precisely angled that I refused to move for the rest of the afternoon.",
    "Storm outside. I am fine. I am simply choosing to sit slightly closer to a human than usual. No further comment.",
    "The window was warm from the sun. I pressed my whole body against it like a lizard with fur.",
    "Overcast today, no sunbeams available. I have filed a complaint with the sky directly.",
    # 🎄 Holidays & seasons
    "The tree came inside the house and now has ornaments. This is clearly for me.",
    "Someone put a small hat on me for a holiday photo. I have not forgiven this, nor will I.",
    "Wrapping paper season is my favorite season. The gifts are optional. The paper is not.",
    "The tinsel is dangerous. I have decided this makes it more appealing, not less.",
    "A pumpkin appeared on the porch. I have studied it from the window with real concern.",
    "New Year's happened. I slept through the countdown as a form of protest against loud noises.",
    "Someone put a costume on me once. I have never fully recovered and I bring it up often.",
    "The holiday lights blink. I stare at them like they hold the secrets of the universe.",
    "A wreath appeared on the door. I don't trust it. I don't trust anything green and circular.",
    "Birthday candles were lit near me. I evacuated the table immediately. Fire is not my department.",
    "The holiday tree ornaments are clearly cat toys that someone hung too high. I am addressing this.",
    "Someone wrapped a present while I sat directly on the paper. This was not an accident.",
    "It's the season for blankets, sunbeams, and doing even less than usual. I am thriving.",
    "A stocking with my name on it appeared. Correct. Finally, some recognition.",
    "Fireworks happened somewhere far away and I still found a way to hide under the bed for two hours.",
    "The holiday guests kept trying to pet me. I allowed exactly three of them. The rest are on notice.",
    "Someone put a bow on me like I'm a present. I am, in fact, a present, every single day.",
    "The turkey smell reached every corner of the house. I positioned myself accordingly.",
    "A new calendar year began. My resolutions remain the same: nap more, judge more, eat more fish.",
    "Someone sang happy birthday near me. I sat perfectly still and made them feel deeply uncomfortable.",
    # 💨 Zoomies & random energy
    "I ran from the kitchen to the bedroom for no reason at 7pm sharp. This has become tradition.",
    "The zoomies arrived without warning. Furniture was rearranged. No apologies were issued.",
    "I did four laps around the living room and then sat down like nothing happened.",
    "Sudden burst of energy at an inconvenient time for everyone but me. As usual.",
    "I ran sideways down the hallway. I don't know why. I don't need to know why.",
    "The zoomies hit right after the litter box. This is apparently a documented phenomenon. I am living proof.",
    "I sprinted past three people at full speed with no destination in mind. Pure vibes.",
    "Energy levels: zero, then suddenly eleven, with no warning in between.",
    "I ran up the stairs, down the stairs, and back up again. The stairs did nothing to deserve this.",
    "A wild burst of chaos took over my body for ninety seconds. I have no comment on what happened.",
    "I did a full loop of the house at top speed and then collapsed dramatically in the hallway.",
    "The zoomies struck at midnight. The furniture has been notified. It did not go well for the furniture.",
    "I ran directly into a wall mid-zoomie and immediately pretended that was the plan all along.",
    "Ten seconds of stillness, then a full sprint across the couch, over the table, and gone. Standard Tuesday.",
    "I attacked the air for a solid minute. The air had it coming, probably.",
    "Random surge of energy led to me climbing the curtains. No regrets. Some rope burn.",
    "I ran so fast I skidded into the wall. I stood up immediately like it never happened. 😼",
    "The 9pm chaos hour has begun. Please clear the hallway.",
    "I did something athletic just now. No one saw it. It still counts.",
    "Burst of speed, sudden stop, dramatic stare into the distance. The full performance, free of charge.",
    # 🥤 Water bowl & drinking
    "The water bowl has been full for two hours. I drank from the toilet instead. Personal choice.",
    "I dipped one paw in the water bowl, tasted the paw, walked away satisfied.",
    "Running water from the tap is superior to bowl water. This is not up for debate.",
    "I stared at my reflection in the water bowl for four minutes. We had a moment.",
    "The water bowl moved two inches. I no longer trust it.",
    "I drink water like it personally wronged me. Aggressively. With commitment.",
    "Someone refilled the water bowl. I inspected it for foreign substances. All clear. Drank anyway, suspiciously.",
    "I prefer my water slightly disturbed by a paw first. Untouched water is untrustworthy.",
    "The faucet dripped once. I have been sitting under it for forty minutes, waiting for round two.",
    "I knocked the water bowl over investigating whether it was real. It was real. Now it's on the floor.",
    "There is a perfectly good water fountain made for cats. I drink from a cup left on the nightstand instead.",
    "I licked the water bowl's edge instead of the water. This was intentional. I stand by it.",
    "The ice cube fell in the water bowl. I have never moved faster in my life.",
    "Water bowl in the kitchen: ignored. Puddle on the bathroom floor: five-star dining.",
    "I pawed at the water until it splashed everywhere, then drank the puddle instead of the bowl.",
    "The water was too still. I fixed that. Then I drank it. Then I complained it was too disturbed.",
    "I watched the water ripple from across the room before committing to drink it.",
    "Someone put ice in my water. I am unsure how I feel. I drank it four times to decide.",
    "The bathroom sink knows things the kitchen bowl will never understand.",
    "I require my water bowl to be positioned exactly six inches from where it currently is. Always.",
    "Drank water. Immediately forgot I was thirsty. Walked away mid-sentence, so to speak.",
    "The water bowl and I have an understanding: I ignore it publicly and drink from it privately.",
    "I sniffed the water bowl, decided against it, and asked for the shower instead.",
    "There's a rule that cats hate water. I have never once followed a rule in my life. 😼",
    "The water bowl was empty. I informed the entire house about it. Immediately. Loudly.",
    # 🛋️ Furniture & couch claims
    "This couch cushion is now permanently shaped like me. That's not wear and tear. That's a monument.",
    "I claimed the armrest as sovereign territory. Border disputes will not be tolerated.",
    "Someone sat in my spot. I sat on their lap instead. Reclaimed by force.",
    "The good couch corner has a 20-minute waitlist. I am first in line, always, by default.",
    "I scratched the couch once. It sounded good. I have not stopped since 2019.",
    "This chair was empty for one second. It is now permanently mine. That's how time works.",
    "I sit on the armrest like a gargoyle. Decorative. Judgmental. Immovable.",
    "The new couch smells wrong. I've been rubbing my face on it for three days to fix that.",
    "I don't sit ON furniture. I sit AS furniture. There's a difference and I embody it.",
    "Someone tried to fold the blanket I was sleeping on. The blanket remains unfolded. I remain on it.",
    "This is the third couch cushion I've claimed today. Yes, they're all mine simultaneously.",
    "I sat on the remote. The TV is now permanently off. This is the new normal.",
    "The recliner reclines. I do not. I simply exist at whatever angle I land at.",
    "Someone bought a cat tree. I sit on top of the bookshelf instead. The tree remains untouched, out of spite.",
    "I have exactly one favorite chair and it changes daily without notice or explanation.",
    "The ottoman was for feet. It is now for cat. Feet must relocate.",
    "I curled up in the laundry basket instead of my bed for the ninth consecutive day. It's simply superior.",
    "Someone vacuumed the couch. I immediately re-covered it in fur. Balance restored.",
    "I sit in the exact center of whatever surface is being used for something else.",
    "The dining chair pulled out an inch is now mine forever. Push it in at your own risk.",
    "I flopped onto the couch with the full weight and drama of a falling tree.",
    "This throw pillow is now a throw-pillow-shaped indentation of my body. Permanently.",
    "I sit on the arm of the couch like I'm supervising a meeting I wasn't invited to.",
    "Someone else's coat was on the chair. It is now covered in cat hair. A signature, if you will.",
    "I don't need the whole couch. I need the specific eleven inches someone is currently occupying.",
    # 🚗 Car rides
    "The car started moving and I immediately regretted every decision that led to this moment.",
    "I made a sound in the car I didn't know I was capable of making. We're calling it a first.",
    "Car rides are 90% yowling and 10% silent, seething betrayal.",
    "I braced all four paws against the carrier like the car itself was the enemy.",
    "The car went around a corner. I have not forgiven the car.",
    "I stared out the window during the car ride like a tiny, furious hostage.",
    "Someone said 'we're almost there' twenty minutes ago. I am tracking this lie closely.",
    "The seatbelt sign doesn't apply to me. Nothing applies to me. Least of all car rules.",
    "I meowed continuously for the entire drive as a form of protest, review, and commentary.",
    "The car smells different today. I have logged this as evidence for later.",
    "I refuse to sit down in the carrier. I will stand the entire ride out of principle.",
    "Every bump in the road is a personal attack. I have counted 47 attacks so far.",
    "The car stopped. I assumed we'd arrived. We had not. The betrayal compounds.",
    "I watched the world go by through the window and decided none of it was worth this.",
    "Someone put a blanket over my carrier. I appreciate the gesture. I am still furious.",
    "The engine sound has become the soundtrack to my suffering.",
    "I pressed my face against the carrier door the entire ride, silently judging the driver's choices.",
    "We arrived. I immediately forgot I was ever upset and demanded snacks.",
    "The car ride ended twenty minutes ago and I am still recovering my dignity.",
    "I made eye contact with a dog in another car. We understood each other's pain.",
    "Turns out car rides are fine if they end somewhere with fish. Noted for future negotiations.",
    "I have exactly one car ride opinion: no. Every time. Forever.",
    "The car stopped at a red light. I used the opportunity to reconsider my entire life.",
    "Someone said this car ride was 'quick.' It has been eleven minutes. I am keeping a record.",
    "I survived the car ride. Emotionally, I am still somewhere on the highway.",
    # 🪞 Mirrors & reflections
    "I saw myself in the mirror and immediately assumed a fighting stance. Standard procedure.",
    "The cat in the mirror copies everything I do with zero originality. Concerning behavior.",
    "I stared myself down in the mirror for six minutes. Neither of us backed off.",
    "The reflection blinked when I blinked. Coincidence, or something more sinister.",
    "I walked past the mirror, did a double take, and pretended it never happened.",
    "There's another cat living in the mirror. It never eats, never sleeps, never leaves. I respect the hustle.",
    "I touched the mirror. The other cat touched back. We're at a standstill.",
    "The mirror cat looked tired today. Concerning, given it does nothing but exist in glass.",
    "I hissed at my reflection once, out of instinct. We've since made peace. Mostly.",
    "The mirror shows me my best angle at all times. This cannot be a coincidence.",
    "I checked the mirror before a nap, mid-nap, and after the nap. Consistency is important.",
    "The window at night acts like a mirror. I've had several tense standoffs with myself since.",
    "I found myself in a spoon's reflection once. Deeply unsettling. Never eating soup again.",
    "The mirror cat has never once said hello. Rude, honestly, given how much time we spend together.",
    "I admired myself in the mirror for eleven minutes. It was time well spent.",
    "Someone moved the mirror. I now walk into a wall where the mirror cat used to be. Grieving.",
    "I inspected my reflection and confirmed: yes, still devastatingly handsome.",
    "The mirror cat and I have never fought, but we've never NOT fought either. It's complicated.",
    "I caught my reflection in the toaster this morning. We locked eyes. Neither of us said anything.",
    "The mirror cat blinked first today. I finally won. Years of effort, finally paid off.",
    "I posed in the mirror for a solid minute before remembering no one was watching. Continued anyway.",
    "There's a cat in the phone screen too when it's off. This conspiracy runs deeper than I thought.",
    "I greeted the mirror cat this morning. It did not greet back. We are no longer friends.",
    "The mirror doesn't lie. I am, in fact, incredible.",
    "I practiced my most intimidating face in the mirror. The mirror cat was unimpressed. Rude.",
    # 🐦 Wildlife spotted outside
    "A bird landed on the windowsill and looked directly at me. The disrespect was audible.",
    "I saw a squirrel doing squirrel things outside. I have strong opinions about this that I won't share.",
    "There's a bird outside that visits daily just to mock me through the glass. I know its tricks now.",
    "I watched a bug crawl across the window for eleven minutes. Prime-time television, honestly.",
    "A cat walked past the window outside. We made eye contact. The rivalry began instantly.",
    "The pigeons outside have no idea how close they came to a very different afternoon.",
    "I saw a butterfly. I made a sound I have never made before. We're not discussing it further.",
    "There's a raccoon that visits at night. We've never met, but I respect the audacity.",
    "A bird sat on the windowsill for exactly one second too long. I nearly went through the glass.",
    "I watched a dog walk by outside. Loudly judged its posture. Silently judged its owner.",
    "The neighbor's cat sat in MY spot on MY windowsill from THEIR side. The nerve.",
    "I saw a bee bump into the window repeatedly. We are, spiritually, the same.",
    "A crow looked directly into the house and I have never felt more perceived in my life.",
    "There's a squirrel that buries things in the yard. I respect the vault mentality.",
    "I watched a spider build a web outside for two hours. Riveting. Ten out of ten.",
    "The birds outside have a whole social life I will never be part of and I resent it.",
    "A cat fight happened two yards over. I supervised from the windowsill like a war correspondent.",
    "I saw my own shadow move outside and nearly declared war on it.",
    "There's a rabbit that visits the yard. I have complicated feelings about its freedom.",
    "The window is basically a nature documentary I did not consent to but cannot stop watching.",
    "A bird flew directly at the window. We both survived. Neither of us has recovered.",
    "I watched leaves blow across the yard for twenty minutes like it was the season finale.",
    "There's a very confident squirrel outside that clearly doesn't know what I'm capable of.",
    "I saw another cat sunbathing in the neighbor's yard. Amateur. I invented sunbathing.",
    "A moth got dangerously close to the window. I was ready. I am always ready.",
    # 🚪 Guests & visitors
    "Someone new walked in. I have hidden. I will observe from the shadows before making any decisions.",
    "A guest tried to pet me. I allowed it. This does not mean we are friends.",
    "New person in the house. I have assigned them a threat level. It is currently 'unclear.'",
    "Someone visited and immediately said 'aw, a cat!' Correct assessment. Continue.",
    "The guest sat in my spot. I sat on the guest instead. Reclamation complete.",
    "I hid under the bed the entire time guests were here, then complained loudly once they left.",
    "A visitor called me 'cute.' I allowed this, but only this once, and only because they had a bag that crinkled.",
    "Someone new is in the house. I have not decided if they live here now. Time will tell.",
    "The guest brought a bag. I inspected the bag. I have decided the bag is more interesting than the guest.",
    "I ignored the guest for the first hour. Then they sat down. I immediately claimed their lap.",
    "New person alert. I performed my slow blink at them. This is the highest honor available.",
    "Someone visited and didn't acknowledge me for eleven minutes. This is now personal.",
    "The guest left their coat on a chair. It is now covered in fur and, by extension, mine.",
    "I supervised the entire visit from a high shelf, silent and judgmental, like a tiny landlord.",
    "A new person sat very still, clearly trying to seem unthreatening. Smart. It worked, eventually.",
    "The guest asked if I bite. I did not answer. I let the mystery speak for itself.",
    "Someone came over and immediately got on the floor to greet me properly. Acceptable behavior. Rare, but acceptable.",
    "I hid until the guest left, then acted like I'd been social and available the entire time.",
    "A visitor tried to make kissy noises at me. I stared through them into the void instead.",
    "The guest had a bag with a crinkly sound. I have reevaluated the entire visit around this discovery.",
    "Someone new sat on the couch. I sat exactly on the edge of their personal space, uninvited but tolerated.",
    "I greeted the guest by walking directly across their laptop keyboard mid-sentence.",
    "A visitor said they were 'more of a dog person.' I have made a note. This will be remembered.",
    "The guest left. I immediately reclaimed every surface they had briefly touched.",
    "Someone new came in nervous about cats. I approached slowly, sat down, and won them over completely.",
    # 🌿 Plants & gardening mishaps
    "The plant moved. I didn't touch the plant. The plant is now on the floor. Coincidence.",
    "I chewed one leaf off the new plant, just to establish dominance.",
    "There's dirt on the floor near the plant pot. I have no comment.",
    "The plant leaves move when the AC turns on. I've attacked them 40 times, believing otherwise.",
    "Someone got a new plant. I have already decided it's mine to guard, chew, or destroy. Undecided which.",
    "I dug in the plant pot for absolutely no reason and covered the evidence poorly.",
    "The hanging plant swings slightly. I have declared war on it.",
    "I knocked the small succulent off the windowsill. It survived. So did my confidence.",
    "There's a plant that's technically toxic to cats. I've licked it eleven times purely for the drama.",
    "The new plant smells interesting. I have investigated it thoroughly, from multiple angles, at multiple hours.",
    "Someone planted herbs in the kitchen. They are now slightly shorter than they were yesterday.",
    "I sat directly in the plant pot instead of near it. This is more efficient.",
    "The fake plant fooled me for exactly one second before I moved on with my dignity slightly reduced.",
    "I chewed the corner of a leaf and immediately regretted every choice that led to this moment.",
    "The plant's shadow moved on the wall. I attacked the shadow. The plant remained unbothered.",
    "Someone repotted the plant. I supervised by sitting directly in the new dirt.",
    "I have never once respected a plant's personal space and I don't intend to start now.",
    "The vine on the windowsill grew an inch. I noticed immediately. I do not know why I care.",
    "I batted one single leaf repeatedly until it detached, then lost all interest.",
    "There's a cactus in this house that I have chosen, wisely, to never approach again.",
    "The plant got knocked over during what I'm calling 'routine inspection.'",
    "I dug a small hole in the plant's soil and did not finish whatever I was planning to do there.",
    "Someone yelled 'not the plant again' from another room. Too late. Always too late.",
    "The new plant arrived and within an hour had already lost a leaf to unrelated, unconnected causes.",
    "I sniffed the plant, sneezed dramatically, and blamed the plant entirely for this personal failure.",
    # 🎵 Music & noise reactions
    "Someone turned on music. I sat directly in front of the speaker like it owed me answers.",
    "The bass made the floor vibrate. I have relocated to somewhere less opinionated.",
    "A song came on that I apparently have Feelings about. I am not discussing which one.",
    "Someone sang loudly in the kitchen. I left the room. Not a review. Just a fact.",
    "The doorbell sound effect on TV made me sprint to a door that does not exist in this scene.",
    "I meowed along to the chorus. I have decided I am a vocalist now.",
    "The vacuum turned on three rooms away. I am currently under the bed, planning my next several hours.",
    "Someone clapped once. I have not recovered.",
    "The blender ran for four seconds. I evacuated the kitchen like it was on fire.",
    "A car alarm went off outside. I stared at the window with the intensity of a war veteran.",
    "The microwave beeped. I looked at it like it had personally insulted my family.",
    "Someone whistled. I do not know what that sound is or why it exists, but I am against it.",
    "The washing machine entered its spin cycle. I have declared this room off-limits for the day.",
    "A phone rang in another room and I arrived to investigate like it was my personal jurisdiction.",
    "Someone dropped a pan in the kitchen. I have not left this room in eleven minutes.",
    "The thunder outside was loud. I sat perfectly still and definitely was not affected. Definitely.",
    "A new song started playing and I immediately reassessed my entire mood to match it.",
    "The smoke alarm chirped once, low battery. I have not trusted that corner of the ceiling since.",
    "Someone hummed quietly while cooking. I supervised from the doorway, unbothered, mildly interested.",
    "The dishwasher made a new sound today. I have added it to my list of ongoing investigations.",
    "A firework went off somewhere far away. I am currently reconsidering every decision I've ever made.",
    "Someone played the guitar badly. I sat and listened anyway. Loyalty has no standards.",
    "The printer made its horrible sound. I left the building. Spiritually, at least.",
    "A balloon popped somewhere outside. I am currently airborne and will discuss this later.",
    "Someone turned the volume up during my nap. This will be remembered, filed, and revisited.",
    # 🌙 Nighttime patrol
    "Nightly patrol complete. Report: everything is exactly where I left it. Suspicious.",
    "I walked the perimeter of the house at midnight. No threats found. I remain vigilant regardless.",
    "The hallway at 2am belongs to me and me alone. I have claimed it formally.",
    "I checked every room twice before allowing myself to consider sleeping.",
    "Nightly rounds: kitchen, clear. Living room, clear. Under the bed, still unexplored. Priorities.",
    "I patrol at night because someone has to, and apparently no one else takes this seriously.",
    "The house creaked at 1am. I have identified the source: the house. Case closed. Watching anyway.",
    "I sat at the top of the stairs at 3am like a gargoyle guarding a secret no one asked me to keep.",
    "Nighttime is when the real work happens. Daytime is for naps and reputation management.",
    "I did a full lap of the house at 4am for reasons that made complete sense at the time.",
    "The night patrol requires total silence, except for the occasional full-volume announcement.",
    "I stood guard at the window overlooking the street until nothing happened, as expected, again.",
    "Someone left a light on downstairs. I have been supervising it since 2am. It remains on. Unresolved.",
    "The overnight shift is unpaid, but the fish bowl access makes up for it.",
    "I checked the front door was still a door at 3:14am. It was. Mission successful.",
    "Nighttime rounds revealed nothing new, which is exactly what I expected and somehow still concerning.",
    "I sat completely still in the dark hallway, watching nothing, for the good of everyone.",
    "The patrol schedule is: sleep, patrol, sleep, patrol, scream once for no reason, sleep.",
    "I inspected the closet at 3am. It remains a closet. I remain suspicious of it regardless.",
    "Someone woke up during my patrol and asked what I was doing. I did not dignify that with an answer.",
    "The overnight watch is a solo operation. I have never once requested backup and never will.",
    "I completed my nightly inspection of the kitchen floor for crumbs. Findings: classified.",
    "3am patrol update: the shadows have not moved, but I am watching them regardless.",
    "I guard this house at night the same way I ignore it during the day: completely and totally.",
    "The patrol ended at dawn. I promptly went to sleep for the rest of the day, mission accomplished.",
    # 🧸 Specific toy attachment
    "I have one toy mouse I've had for years. It has no tail, one ear, and my entire heart.",
    "The new toy was ignored in favor of the box it came in, which was then also ignored in favor of the old toy mouse.",
    "I carry my favorite toy from room to room like a tiny, felt-covered security blanket.",
    "The toy mouse gets placed in the food bowl every morning. This is a gift. Do not remove it.",
    "I lost my favorite toy under the couch. I have been sitting there in vigil for three days.",
    "Someone bought me an expensive toy. I play with the twist tie from the bread bag instead.",
    "The toy mouse has been dead for years. I still bring it to the humans as an offering.",
    "I have exactly one toy that matters. The other 46 are decoys.",
    "The crinkle toy makes a sound only I can properly appreciate. It is my favorite sound.",
    "I found my lost toy after four days. It was under the fridge. I retrieved it personally.",
    "The toy mouse gets placed on the pillow every night. This is not negotiable. This is tradition.",
    "Someone tried to wash my favorite toy. It came back smelling wrong. We are rebuilding trust.",
    "I carried the toy mouse to the water bowl and dropped it in. This was intentional. Don't ask why.",
    "The feather toy is missing half its feathers. I consider this character development.",
    "I have a toy I only play with at 3am. It's a very specific relationship.",
    "The toy mouse fell behind the bookshelf. I have accepted this loss with quiet, ongoing grief.",
    "Someone gave me a new toy identical to my old one. I have rejected it entirely on principle.",
    "I meow at 4am holding my toy mouse in my mouth like I'm delivering important news.",
    "The toy has been chewed, lost, found, and buried in the couch cushions more times than I can count.",
    "I have declared this specific bottle cap a toy. It is now my most prized possession.",
    "The toy mouse and I have been through everything together. Mostly naps. But everything.",
    "Someone replaced my old toy with a new, nicer one. I sleep on the new one and ignore it during the day. Complicated feelings.",
    "I dragged my toy mouse across the house at 5am, announcing its arrival the entire way.",
    "The toy is missing an eye. So am I, spiritually, some days. We match.",
    "I have hidden my favorite toy in seven different locations 'for safekeeping.' I can find none of them.",
    # 🧮 Cat logic / math
    "One fish plus one fish equals not enough fish. This is the only math I trust.",
    "I counted my toys. There are 46. I would like 47. The math demands it.",
    "Two naps a day is the minimum. Eighteen naps a day is aspirational. I split the difference.",
    "If I fits, I sits. This is not a joke. This is a scientifically binding law.",
    "The bowl is 10% empty, which by my calculations rounds up to 'completely empty.'",
    "I calculated the exact center of every room and now sleep exclusively there.",
    "Nine lives divided by however many risks I take daily equals a very concerning number.",
    "I have run the numbers. The numbers say: more fish.",
    "One knock equals one broken item. I have knocked several things. Draw your own conclusions.",
    "The math checks out: attention plus food equals tolerating you. Simple equation.",
    "I calculated that 3am is exactly the correct time for chaos, mathematically speaking.",
    "Half a fish is not a fish. It is an insult disguised as a snack.",
    "I did the math on how long I can ignore you. The answer is: indefinitely, but I chose not to.",
    "Two eyes, one amber, one green, both correct 100% of the time. The statistics speak for themselves.",
    "The formula for a perfect nap is: warmth, plus silence, plus you leaving me alone.",
    "I subtracted my dignity from the situation and the remainder was still somehow positive.",
    "By my calculations, I am owed exactly one fish per minute I have existed today.",
    "The exact number of things I've knocked off tables today rounds to 'yes.'",
    "I calculated the odds of getting a treat if I stare long enough. They are, statistically, excellent.",
    "Zero patience minus your explanation equals me walking away mid-sentence.",
    "I've done the math and being aloof burns more calories than being affectionate. Explains a lot.",
    "The numbers don't lie: more naps correlates directly with more wisdom. I am very wise.",
    "One (1) me is equal to at least three (3) normal cats in terms of chaos output.",
    "I calculated the trajectory of the object before knocking it off the table. Precision matters.",
    "The math is simple: attention span short, opinions long, math skills questionable but confident.",
    # ⏰ Time & schedules
    "My schedule: eat, judge, nap, chaos, nap, judge, eat, chaos, sleep. Subject to change without notice.",
    "Time doesn't exist for me. Only feeding time exists. Everything else is a suggestion.",
    "I run on my own clock, and that clock is permanently set to 'now, but also whenever.'",
    "Punctuality is for people who don't understand that naps happen exactly when they happen.",
    "My daily agenda has one confirmed meeting: the food bowl, at a time only I know.",
    "I don't believe in Mondays. I don't believe in any of the days, honestly.",
    "The clock says one thing. My stomach says something else entirely, and my stomach wins.",
    "I have a strict schedule that consists entirely of flexibility and demands.",
    "Daylight savings means nothing to me. I demand food at the same biologically-mandated hour regardless.",
    "My internal alarm goes off at 5am sharp, every day, whether anyone consented to this or not.",
    "There is no 'later.' There is only 'now' and 'the exact moment I decide is now.'",
    "I scheduled a nap for 20 minutes. It has been three hours. The nap has grown.",
    "Time flies when you're judging everyone in the room. Which is always.",
    "I don't wear a watch, but if I did, it would only ever show 'feeding time, probably.'",
    "My calendar has one event, repeating daily, called 'chaos, TBD.'",
    "I operate on a 25-hour day. No one asked. It's happening regardless.",
    "The concept of 'being late' does not apply to something that arrives exactly when it wants to.",
    "I have a very precise internal clock that is somehow always both early and late simultaneously.",
    "My bedtime is whenever I say it is, which is usually right after yours.",
    "I don't do time zones. I do 'right now, this instant, feed me.'",
    "Every hour of the day is nap-adjacent in some capacity. This is by design.",
    "I set my own deadlines and then I ignore them, on principle, consistently.",
    "The only appointment I've ever kept was the one where food was involved.",
    "My schedule has more flexibility than a house of cards, and about the same structural integrity.",
    "I have never once been on time. I have also never once cared.",
    # ⚖️ Weight & diet talk
    "The vet mentioned my 'ideal weight.' I have chosen to ignore this concept entirely.",
    "I am not overweight. I am aerodynamically thorough.",
    "Someone suggested portion control. I suggested they reconsider that suggestion.",
    "I have big bones. Also a big personality. Also, apparently, a big everything.",
    "The scale said a number today. I do not recognize this number. I reject it formally.",
    "I'm not chubby, I'm fluffy. The fur adds visual weight. Science, probably.",
    "Someone hid the treats 'for my health.' I found them. Health can wait.",
    "I identify as 'sturdy,' not 'in need of a diet.' There's a difference and I insist on it.",
    "The new food is 'weight management.' I have management concerns of my own about this.",
    "I don't need to lose weight. I need bigger furniture.",
    "Someone said I've 'filled out.' I have chosen to take this as a compliment.",
    "The measuring cup for my food portions is clearly broken. It's too small. Every time.",
    "I am not fat. I am a unit of measurement now. Deal with it.",
    "The scale at the vet lied. I have decided this and I am not revisiting the decision.",
    "Someone joked about my weight. I sat on their chest for the rest of the evening. Lesson delivered.",
    "I've been on a diet since Tuesday. It has consisted of the same amount of food. The diet is theoretical.",
    "My belly sways slightly when I walk. I consider this a feature, not a bug.",
    "The vet recommended more exercise. I recommend the vet reconsider their priorities.",
    "I am husky in the way that implies dignity, not the way that implies concern.",
    "Someone tried to give me 'light' food. I gave them a long, unimpressed stare in return.",
    "I have a diet plan. It is called 'eat when hungry, which is always.'",
    "The extra weight is muscle. It is entirely, definitely, one hundred percent muscle.",
    "I don't need to watch my weight. I need everyone else to stop watching it for me.",
    "Someone measured my food to the gram. I measured their patience. Both ran out quickly.",
    "I am not overweight, I am simply built for comfort, warmth, and taking up the entire couch.",
    # ⭐ Main character energy / fame
    "I am the main character of this household and everyone else is a supporting role.",
    "There's a whole video game, token, and NFT collection about me. What have you accomplished today.",
    "I didn't ask to be famous. I simply existed, and the world responded appropriately.",
    "Every room I enter becomes, by default, about me.",
    "I have fans I've never met. This tracks. I would be a fan of me too.",
    "The spotlight finds me naturally. I don't chase it. It knows where I am.",
    "I am not seeking attention. Attention is simply orbiting me, as is natural.",
    "There's an entire brand built around my personality. This seems appropriate and overdue.",
    "I walked into the room and the energy shifted. This happens every time. I've stopped being surprised.",
    "People write about me. People draw me. People name their pets after me. Correct behavior.",
    "I am not just a cat. I am an ecosystem. A brand. A lifestyle. A cautionary tale, possibly.",
    "Everyone in this chat is technically a background character in my ongoing story.",
    "I've been the main character since day one. Everyone else just recently found out.",
    "There is a game where I run, jump, and cling to walls. This is, frankly, an accurate biopic.",
    "I don't do cameos. Every appearance is a full, committed performance.",
    "The vault, the game, the NFTs — all of it exists because I refused to be ordinary.",
    "I've never once been the sidekick. I don't have the range for it.",
    "People say 'main character energy' like it's a choice. For some of us, it's just genetics.",
    "I exist, and the room reorganizes itself around that fact.",
    "There's merchandise with my face on it somewhere, probably. There should be. There will be.",
    "I don't need an origin story. I simply always was, and always will be, the point of interest.",
    "Everyone else in this story is a plot device. I am the plot.",
    "I've been type-cast as 'iconic' and, frankly, I've made peace with the role.",
    "The story of this project starts and ends with me. Everything in between is just logistics.",
    "I am the reason you're here. Directly or indirectly. Mostly directly.",
    # 📺 Tech confusion
    "The doorbell rang on the TV and I sprinted to a door that does not exist in this house.",
    "Someone is on a video call and I have decided my face belongs in the frame now.",
    "The TV showed a bird and I attacked the screen. The bird did not react. Rude, honestly.",
    "A phone rang and I looked accusingly at every object in the room simultaneously.",
    "The smart speaker said something and I have not trusted that corner of the kitchen since.",
    "I watched an entire nature documentary and made a plan for at least three of the animals.",
    "Someone's phone alarm went off and I responded with the urgency of an actual emergency.",
    "The TV remote was on the couch. It is now somewhere unknown. This was not an accident.",
    "A video call started and I positioned myself directly between the camera and the human's face.",
    "The doorbell camera app made a sound. I investigated the phone itself as the primary suspect.",
    "I don't understand video calls, but I understand that my face should be in more of them.",
    "The TV played a cat food commercial and I have never been more personally offended by advertising.",
    "Someone's phone buzzed on the table. I stared at it until it stopped, then continued staring out of principle.",
    "I sat on the TV remote during the season finale. This was not an accident. I have opinions about the ending.",
    "The Roomba turned on and I have relocated to the highest point in the house.",
    "A notification sound went off and I responded like the house was under direct threat.",
    "I watched myself in a video someone took. I have decided I photograph exceptionally well.",
    "The doorbell rang for real this time and I have never moved this fast in my entire life.",
    "Someone left a video call open and I meowed into it for no audience in particular.",
    "The TV static made a sound and I have been suspicious of that specific TV ever since.",
    "I don't understand why the box makes people's faces appear, but I've decided to supervise it closely.",
    "A phone alarm went off at an unusual hour and I have adopted its urgency as my own.",
    "The screensaver had fish on it. I attacked the TV. This was a rational response.",
    "Someone showed me myself on their phone screen. I stared, unimpressed, mostly because the lighting was bad.",
    "The vacuum robot has a blinking light. I consider it a rival and have never once approached it directly.",
    # 💤 Dreams
    "I dreamed I caught the red dot. I woke up disappointed in a way that felt personal.",
    "In my dream I had opposable thumbs. I have never wanted anything more badly, before or since.",
    "I dreamed about an infinite fish. I woke up and checked the bowl immediately. Still finite. Tragic.",
    "My legs twitched during a dream chase. In my mind, I caught the thing. In reality, nothing happened.",
    "I dreamed I ruled the house completely. Woke up. Realized I already do. Went back to sleep, satisfied.",
    "In the dream, the box was infinite. I miss that box more than most real things.",
    "I dreamed about a version of the vault that never runs low. A cat can dream.",
    "I twitched my whiskers in my sleep, mid-hunt, mid-dream, mid-something important.",
    "I dreamed I was falling from a great height and landed on my feet. Even my dreams know the rules.",
    "In the dream, everyone understood exactly what I wanted at all times. A perfect world. Fictional, but perfect.",
    "I dreamed about 3am chaos so vividly I woke up already mid-sprint.",
    "My dream had an endless sunbeam. I have been chasing that feeling in real life ever since.",
    "I dreamed I was being chased by the vacuum. I woke up and immediately checked it was still off.",
    "In the dream there was a fish that never ran out. I've told no one how much this affected me.",
    "I made a small sound in my sleep. Whatever I was dreaming, I stand by the sound.",
    "I dreamed about knocking something over in slow motion. It was, somehow, deeply satisfying.",
    "My paws moved like I was running in the dream. In real life, I did not move an inch. Efficient.",
    "I dreamed the whole house was one giant cardboard box. I have never been happier.",
    "In the dream, I finally caught my own tail. I woke up and immediately tried again in real life.",
    "I dreamed about the sound of the treat bag. I woke up disoriented and slightly hopeful.",
    "My dream had no humans in it, just fish, boxes, and warm spots. I consider this the ideal universe.",
    "I dreamed I was bigger than the couch. When I woke up, I checked. Still not bigger. Working on it.",
    "In the dream I understood every word you've ever said to me. I have chosen not to replicate this in real life.",
    "I dreamed about the vault overflowing with fish. I woke up and immediately checked the chart, just in case.",
    "My whole body twitched mid-dream. Whatever happened in there, I won. I always win, even asleep.",
    # 🐈 Territorial / other cats
    "There's a cat that walks past my window daily. This is not a friendship. This is a rivalry with extra steps.",
    "I do not share territory. I do not share food. I barely share the concept of tolerating others.",
    "The neighbor's cat looked at my yard. I have declared this an act of war.",
    "I sniffed the fence where another cat had been. The audacity of that individual is noted.",
    "There is only room for one main character in this territory, and it is, obviously, me.",
    "I marked my territory by simply existing in it, repeatedly, at every opportunity.",
    "The other cat in the neighborhood thinks it runs things. It does not. I run things. From indoors. Remotely.",
    "I do not do 'cat friends.' I do 'cats I have strategically decided to tolerate from a distance.'",
    "Someone else's cat scent was on the porch. I have investigated this personally, multiple times, thoroughly.",
    "I stared down a cat through the window for ten minutes. Neither of us blinked. Legends will speak of this.",
    "There's a stray that visits the yard. We have an unspoken agreement: mutual, distant respect.",
    "I do not recognize other cats as equals. I recognize them as competitors for a throne I already occupy.",
    "The neighbor's cat sat on 'my' fence. I have not forgotten. I will not forget.",
    "I hissed once at another cat through the glass. It was mostly for show. Mostly.",
    "There is exactly one apex predator in this house and it sleeps eighteen hours a day.",
    "I patrol the windows specifically to monitor rival cat activity in the area.",
    "The cat two houses down thinks it's tough. It has never once been inside my box collection. Amateur.",
    "I do not do alliances. I do temporary ceasefires when the fish situation calls for it.",
    "Someone's cat left paw prints on our car. I take this as a direct and personal challenge.",
    "I watched another cat get petted by a stranger outside. I have complicated feelings about this.",
    "There's a cat who thinks the mailbox post is theirs. I have differing opinions on this matter.",
    "I do not compete with other cats. I simply, naturally, always come out ahead.",
    "The rival cat next door has fewer toys than me. I have made peace with my clear superiority.",
    "I saw another cat get a treat from a stranger. Betrayal by association. I am furious on principle.",
    "There can only be one cat who matters in this neighborhood, and I have already claimed the title.",
    # 🏚️ House noises
    "The house settled at 2am and made a sound. I have identified 12 possible explanations, none of them calm.",
    "Something creaked upstairs. I am now stationed at the bottom of the stairs, indefinitely.",
    "The pipes made a noise. I have declared the bathroom off-limits until further notice.",
    "A floorboard creaked with no one walking on it. I would like this explained immediately.",
    "The fridge hummed slightly differently today. I noticed. I am concerned. I have said nothing until now.",
    "Something tapped against the window. It was a branch. I remain unconvinced.",
    "The house made its nightly groaning sound and I have, once again, taken it personally.",
    "I heard a sound behind the wall. I have been staring at that wall for six minutes. It's winning.",
    "The heater clicked on and I sprinted to investigate the exact source of the sound, immediately.",
    "Something dripped somewhere in the house. I have not located it. I think about it constantly.",
    "The wind made the shutters rattle. I supervised this event from a safe distance, tail puffed, fully alert.",
    "A pipe knocked once, loudly. I evacuated the kitchen and have not returned since.",
    "The attic made a noise. I do not go in the attic. I do not discuss the attic.",
    "Something scratched faintly behind the wall. I have made this my primary concern for the evening.",
    "The house is never fully silent, and I have appointed myself its official noise investigator.",
    "A door creaked on its own, slightly, from the draft. I remain deeply unconvinced by this explanation.",
    "The washing machine finished its cycle with a loud beep. I have not recovered.",
    "Something in the wall made a sound like tiny footsteps. I would like this addressed formally.",
    "The old floor creaks in the same spot every night. I have named that spot and I avoid it.",
    "A gust of wind hit the window hard. I briefly considered my own mortality, then went back to sleep.",
    "The ceiling made a settling sound. I stared at the ceiling for eleven minutes. It did not explain itself.",
    "Something clicked in the walls at exactly 3am, as if summoned by my own internal schedule.",
    "The house's noises are a mystery I have chosen to investigate nightly, alone, without backup.",
    "A cabinet door swung slightly on its own. I have logged this as evidence of something.",
    "I heard a noise, went to look, found nothing, and remained suspicious of that spot for the rest of the week.",
    # 🛍️ New purchases / shopping bags
    "Someone came home with bags. I do not care what's in them. I care about the bags themselves.",
    "A new item entered the house today. I have not approved it yet. Approval pending.",
    "The delivery box arrived before the item did, spiritually. The box remains the real gift.",
    "Someone bought something new and unwrapped it in front of me like I wasn't the real recipient.",
    "I inspected the new purchase for exactly four seconds before returning to the packaging it came in.",
    "A new gadget arrived. I do not understand it. I do not need to. I simply sit on the box.",
    "The shopping bags rustled from the other room and I arrived faster than physics should allow.",
    "Someone bought me a toy. I am playing with the receipt instead. Priorities are priorities.",
    "A new piece of furniture arrived and I claimed it before it was even fully assembled.",
    "I supervised the unboxing of a new item with the seriousness of a customs official.",
    "The new blanket smells like the store. I have been rubbing my face on it to fix that.",
    "Someone brought home groceries and I inspected every single bag like a tiny, furry TSA agent.",
    "A new gadget box sat unopened for a day. I made it my home before anyone else could claim it.",
    "I do not care what the new purchase does. I care that it came in a box, and the box is mine now.",
    "Someone unwrapped a package and I claimed the wrapping paper as my primary prize.",
    "The new item was placed on a shelf. I have already relocated it to the floor for further evaluation.",
    "A new rug arrived. I have tested its comfort level extensively, at length, multiple times a day.",
    "Someone bought a new plant and I have already made plans regarding it.",
    "The packing peanuts scattered everywhere and I have declared this the best day of the month.",
    "A delivery arrived and I sat by the door for the next hour in case another one was coming.",
    "Someone assembled new furniture and I sat directly in the middle of the instructions the entire time.",
    "The new item smelled like a factory. I have spent considerable effort correcting that.",
    "I do not need new toys. I need the box the new toy came in. This has always been true.",
    "A grocery bag crinkled and I appeared from a room I was not previously in, immediately.",
    "Someone brought home a new item and within the hour it was, functionally, mine.",
    # 🧹 Cleaning day / vacuum
    "It's cleaning day. I have relocated to the highest, most inaccessible surface available.",
    "The vacuum came out of the closet. I am currently reassessing my entire relationship with this house.",
    "Someone is mopping. I have declared the wet floor a hostile environment and left accordingly.",
    "The vacuum turned on and I achieved a new personal record for fastest exit.",
    "Cleaning day means every surface I've claimed gets temporarily reassigned. I do not accept this.",
    "The vacuum sound is the closest thing to true fear I experience on a weekly basis.",
    "Someone dusted the shelves. My territory has been disturbed. I am filing a complaint.",
    "I supervised the cleaning from a safe distance, offering silent judgment on technique.",
    "The mop bucket appeared and I have declared the kitchen unsafe until further notice.",
    "Cleaning day disrupted seven of my napping spots simultaneously. This is an emergency.",
    "The vacuum robot turned on by itself and I have not left the top of the bookshelf since.",
    "Someone vacuumed my favorite blanket. It now smells wrong. We are rebuilding trust slowly.",
    "I watched the vacuum from across the room like it was a predator I had personally wronged.",
    "The broom moved across the floor and I attacked it out of principle, then fled out of fear.",
    "Cleaning day means new smells everywhere and I have to re-investigate the entire house from scratch.",
    "Someone wiped down my favorite windowsill. It is temporarily unfamiliar. I remain cautious.",
    "The vacuum got dangerously close to my hiding spot and I relocated with great urgency and zero grace.",
    "I do not help with cleaning. I supervise from a distance and offer unsolicited criticism via stare.",
    "The smell of cleaning products means I avoid that room for the rest of the day, minimum.",
    "Someone rearranged furniture while cleaning. My entire map of the house is now incorrect.",
    "The vacuum finished. I emerged from hiding like nothing had happened, immediately reclaiming my spot.",
    "Cleaning day is the one day a week I question whether this house is truly mine.",
    "I sat just outside the vacuum's reach the entire time, taunting it silently, from a very safe distance.",
    "The clean laundry pile is warm and inviting, which means it is now covered in fur within the hour.",
    "Someone cleaned the litter box. This is the one form of cleaning I fully, unconditionally support.",
]

SOCIAL_LINKS = (
    "🐦 https://x.com/DjangoUnchain06\n"
    "📸 https://www.instagram.com/iwillrug_u/\n"
    "🟠 https://www.reddit.com/r/Iwillrugu/"
)

SOCIAL_REMINDERS = [
    f"pssst... a follow, a like, a repost. the cat asks for so little. 😼\n\n{SOCIAL_LINKS}",
    f"*taps paw on table* 🐟 follow. like. repost. the cat will not forget.\n\n{SOCIAL_LINKS}",
    f"attention humans 📢 the cat needs your engagement energy.\n\n{SOCIAL_LINKS}",
    f"3 clicks. that's all. follow, like, repost. the cat is watching. 😼\n\n{SOCIAL_LINKS}",
    f"daily reminder from the cat: spread the word 🐟\n\n{SOCIAL_LINKS}",
    f"the algorithm hungers. feed it. 😼\n\n{SOCIAL_LINKS}",
    f"*stares at you* ... you know what to do.\n\n{SOCIAL_LINKS}",
    f"the cat has spoken. go follow. go like. go repost. 😼\n\n{SOCIAL_LINKS}",
]

# ══════════════════════════════════════════════════════════════════════════
#  TWITTER / X INTEGRATION
# ══════════════════════════════════════════════════════════════════════════
_TWITTER_KEYS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
)
TWITTER_ENABLED = _TWEEPY_AVAILABLE and all(os.environ.get(k) for k in _TWITTER_KEYS)

def _post_tweet(text: str) -> None:
    client = tweepy.Client(
        consumer_key=os.environ["TWITTER_API_KEY"],
        consumer_secret=os.environ["TWITTER_API_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    print(f"[twitter] tweeted id={resp.data['id']}: {text[:60]!r}", flush=True)

def _seconds_until_window(start_hour_utc: int, end_hour_utc: int, *, force_next_day: bool = False) -> float:
    """Seconds until a random moment inside [start_hour_utc, end_hour_utc) today (or tomorrow).

    force_next_day=True always targets tomorrow's window, even if today's window hasn't
    passed yet. Needed when rescheduling right after firing: "now" is still inside today's
    window, so without this the next random pick could land later the same day instead of
    tomorrow, causing two posts in the same slot hours (or minutes) apart.
    """
    now = datetime.utcnow()
    # pick a random minute within the window
    window_minutes = (end_hour_utc - start_hour_utc) * 60
    offset_minutes = random.randint(0, window_minutes - 1)
    target = now.replace(hour=start_hour_utc, minute=0, second=0, microsecond=0) + timedelta(minutes=offset_minutes)
    if force_next_day or target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

# 4 slots/day, 1h wide, spaced 6h apart (UTC). Consecutive picks land 5h-7h apart,
# comfortably >=3h no matter where inside each window the random moment falls.
TWEET_SLOTS = [(0, 1), (6, 7), (12, 13), (18, 19)]

async def tweet_slot_job(context: ContextTypes.DEFAULT_TYPE):
    """Posts one tweet at a random moment inside its assigned 1h UTC slot. Reschedules for tomorrow's same slot.

    The reschedule at the end is wrapped in try/finally so ANY failure above
    (not just the tweet-posting call, which already has its own try/except --
    also e.g. random.choice on an empty TWEET_PHRASES) can never skip
    rescheduling, which would otherwise silently kill this job forever until
    the whole process restarts."""
    try:
        slot_start, slot_end = context.job.data
    except Exception as e:
        # Can't reschedule "tomorrow's same slot" without knowing the slot --
        # this specific job instance is unrecoverable, but this should never
        # happen since every scheduling call site always passes data=(...).
        print(f"[twitter] tweet_slot_job: malformed job.data, cannot reschedule: {e}", flush=True)
        return
    try:
        if TWITTER_ENABLED:
            # Persisted per-slot dedupe (reusing events.py's SQLite config
            # table -- generic key/value, safe to share as long as the key
            # is namespaced): without this, a restart landing inside a slot
            # AFTER it already tweeted once today recomputes a fresh random
            # target that can still land later in that SAME window, posting
            # a second tweet the same day -- unlike daily_event_job/
            # event_teaser_job, which already guard against exactly this.
            today = datetime.utcnow().date().isoformat()  # matches events.py's same convention
            slot_key = f"last_tweet_date_{slot_start}_{slot_end}"
            if db.get_config(slot_key) != today:
                db.set_config(slot_key, today)  # set BEFORE posting, matching the same restart-safety reasoning
                text = pick_phrase(TWEET_PHRASES)
                try:
                    await asyncio.get_event_loop().run_in_executor(None, _post_tweet, text)
                except Exception as e:
                    print(f"[twitter] tweet error (slot {slot_start:02d}-{slot_end:02d}h UTC): {e}", flush=True)
    finally:
        delay = _seconds_until_window(slot_start, slot_end, force_next_day=True)  # tomorrow's same slot
        context.application.job_queue.run_once(tweet_slot_job, delay, data=(slot_start, slot_end))

# ── Merch tweet (image post, every N days) ─────────────────────────────────
MERCH_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merch")
MERCH_TWEET_INTERVAL_DAYS = 3
# Spain wall-clock time, DST-aware (CET in winter/CEST in summer) -- unlike
# every other scheduled window in this file (plain UTC), this one was asked
# for specifically in local Spain time, so it needs its own tz-aware helper
# below rather than reusing _seconds_until_window.
MADRID_TZ = ZoneInfo("Europe/Madrid")
MERCH_TWEET_WINDOW_MADRID = (0, 7)  # 00:00-07:00 Europe/Madrid

def _seconds_until_madrid_window(start_hour: int, end_hour: int, *, force_next_day: bool = False) -> float:
    """Same logic as _seconds_until_window, but anchored to Europe/Madrid
    wall-clock time instead of UTC. Subtracting two tz-aware datetimes still
    yields the correct real elapsed seconds across a DST transition, so this
    stays accurate year-round without any manual offset bookkeeping."""
    now = datetime.now(MADRID_TZ)
    window_minutes = (end_hour - start_hour) * 60
    offset_minutes = random.randint(0, window_minutes - 1)
    target = now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(minutes=offset_minutes)
    if force_next_day or target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def _merch_image_files() -> list:
    if not os.path.isdir(MERCH_IMAGES_DIR):
        return []
    return sorted(
        f for f in os.listdir(MERCH_IMAGES_DIR) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    )

def _next_merch_image():
    """Cycles through every file in merch/ exactly once, in random order,
    before any repeat. Persisted in the config table (unlike pick_phrase's
    in-memory bag) since the gap between merch tweets is measured in days --
    easily spanning a Render restart, which would otherwise reset the
    rotation and risk an early repeat."""
    files = _merch_image_files()
    if not files:
        return None
    raw = db.get_config("merch_image_queue")
    try:
        queue = [f for f in json.loads(raw) if f in files] if raw else []
    except (ValueError, TypeError) as e:
        # A hand-edited or otherwise corrupted config value must not wedge
        # this rotation forever (every future call would hit the same
        # json.loads failure on the same stored string) -- treat it as "no
        # queue yet" and reshuffle fresh, same as an empty one.
        print(f"[twitter] merch_image_queue was corrupted ({e}), reshuffling", flush=True)
        queue = []
    if not queue:
        queue = files[:]
        random.shuffle(queue)
    chosen = queue.pop(0)
    db.set_config("merch_image_queue", json.dumps(queue))
    return chosen

def _post_merch_tweet(text: str, image_path: str) -> None:
    """Uploading media requires the v1.1 API (tweepy.API) -- Client (v2) has
    no media-upload endpoint of its own, only create_tweet's media_ids
    parameter, so the image goes up via API first and gets attached to the
    v2 tweet by id."""
    auth = tweepy.OAuth1UserHandler(
        os.environ["TWITTER_API_KEY"], os.environ["TWITTER_API_SECRET"],
        os.environ["TWITTER_ACCESS_TOKEN"], os.environ["TWITTER_ACCESS_TOKEN_SECRET"],
    )
    media = tweepy.API(auth).media_upload(filename=image_path)
    client = tweepy.Client(
        consumer_key=os.environ["TWITTER_API_KEY"],
        consumer_secret=os.environ["TWITTER_API_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text, media_ids=[media.media_id])
    print(f"[twitter] merch tweet id={resp.data['id']} image={os.path.basename(image_path)}: {text[:60]!r}", flush=True)

async def merch_tweet_job(context: ContextTypes.DEFAULT_TYPE):
    """Posts one merch-collection tweet (image + caption) every
    MERCH_TWEET_INTERVAL_DAYS, at a random moment inside
    MERCH_TWEET_WINDOW_MADRID (Europe/Madrid wall-clock time). Fires once a
    day like merch_announcement_job (so a restart never loses track of the
    schedule) but only actually posts once the interval has elapsed, gated
    by a persisted last-post date -- same restart-safe dedupe idiom as
    tweet_slot_job/merch_announcement_job, generalized from "once a day" to
    "once every N days". The date itself is also Madrid-local (not UTC), to
    stay consistent with a window anchored to Madrid midnight.

    try/finally around the whole body so any failure above can never skip
    the reschedule, matching every other job in this file."""
    try:
        if TWITTER_ENABLED:
            last = db.get_config("last_merch_tweet_date")
            days_since = (
                None if last is None
                else (datetime.now(MADRID_TZ).date() - datetime.strptime(last, "%Y-%m-%d").date()).days
            )
            if last is None or days_since >= MERCH_TWEET_INTERVAL_DAYS:
                today = datetime.now(MADRID_TZ).date().isoformat()
                db.set_config("last_merch_tweet_date", today)  # set BEFORE posting, restart-safe
                image_name = _next_merch_image()
                if image_name is None:
                    print("[twitter] merch_tweet_job: merch/ folder is empty, skipping", flush=True)
                else:
                    opener = pick_phrase(MERCH_TWEET_OPENERS)
                    text = f"{opener}\n\n{MERCH_TWEET_TAGLINE}\n\n{MERCH_LAUNCHPAD_URL}\n\n{MERCH_TWEET_HASHTAGS}"
                    image_path = os.path.join(MERCH_IMAGES_DIR, image_name)
                    try:
                        await asyncio.get_event_loop().run_in_executor(None, _post_merch_tweet, text, image_path)
                    except Exception as e:
                        print(f"[twitter] merch tweet error: {e}", flush=True)
    finally:
        delay = _seconds_until_madrid_window(*MERCH_TWEET_WINDOW_MADRID, force_next_day=True)
        context.application.job_queue.run_once(merch_tweet_job, delay)

# ══════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _run_health_server():
    # Binding/serve_forever failures here previously died silently in the
    # daemon thread with no retry -- on a platform (Render) that gates
    # container health on this exact port, that looks like a hung service
    # from the outside while application logs look completely normal.
    try:
        port = int(os.environ.get("PORT", 10000))
    except ValueError as e:
        # Parsed once, outside the retry loop -- a malformed PORT would
        # otherwise fail identically on every single retry forever (every
        # 5s, permanently), which looks like a working retry mechanism in
        # the logs while the health endpoint can never actually come up.
        print(f"[health] invalid PORT env var ({e}), falling back to 10000", flush=True)
        port = 10000
    while True:
        try:
            HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
        except Exception as e:
            print(f"[health] server crashed, retrying in 5s: {e}", flush=True)
            time.sleep(5)


threading.Thread(target=_run_health_server, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════
#  BORED + CALLOUT JOB  (PTB JobQueue — runs inside the bot's event loop)
# ══════════════════════════════════════════════════════════════════════════
async def bored_cat_job(context: ContextTypes.DEFAULT_TYPE):
    # The reschedule at the end is wrapped in try/finally so ANY failure above
    # can never skip rescheduling, which would otherwise silently kill this
    # job forever until the whole process restarts -- same pattern as
    # tweet_slot_job/social_reminder_job/monad_reminder_job/game_reminder_job/
    # nft_reminder_job.
    try:
        now = time.time()
        mood = current_mood()
        bias = MOOD_BIAS[mood]
        # speaks up on its own with some probability, regardless of whether the chat is active
        for chat_id in list(_known_chats.keys()):
            if random.random() < min(0.352 * bias["speak_mult"], 0.9):  # 50% base, -12% then -20% relative
                try:
                    eligible = [
                        (uid, udata) for uid, udata in _known_users.items()
                        if udata.get("chat_id") == chat_id
                        and now - udata.get("last_seen", 0) < 86400
                    ]
                    if eligible and random.random() < min(0.32 * bias["callout_mult"], 0.9):  # -20% (was 0.40)
                        uid, udata = random.choice(eligible)
                        name = udata.get("name", "human")
                        h = hour_now()
                        quiet_for = now - udata.get("last_seen", now)
                        live_event = db.get_active_event()
                        is_live = bool(
                            live_event and live_event["status"] == "unclaimed"
                            and live_event["chat_id"] == chat_id
                        )
                        # Layer the situational pools on top of the general one
                        # instead of replacing it -- each still only fires part
                        # of the time, so "there's a live event right now",
                        # "everyone's asleep", or "haven't seen you" callouts
                        # stay a flavor, not the default.
                        if is_live and random.random() < 0.4:
                            info = cfg.EVENTS.get(live_event["event_key"], {})
                            text = (
                                pick_phrase(CALLOUT_EVENT_LIVE)
                                .replace("{name}", name)
                                .replace("{emoji}", info.get("emoji", "🎁"))
                                .replace("{ev_name}", info.get("name", "prize"))
                                .replace("{reward}", str(live_event["reward"]))
                            )
                        elif 2 <= h <= 5 and random.random() < 0.5:
                            text = pick_phrase(CALLOUT_NIGHT).replace("{name}", name)
                        elif quiet_for > 3 * 3600 and random.random() < 0.45:
                            text = pick_phrase(CALLOUT_QUIET).replace("{name}", name)
                        else:
                            text = pick_phrase(CALLOUT_MESSAGES).replace("{name}", name)
                    else:
                        # Layered the same way as the callout pools above: most
                        # of the time this still falls through to the original
                        # BORED_MESSAGES pool, but sometimes the cat shows up
                        # as pointedly indifferent, visibly asleep, demanding
                        # belly rubs, or territorial about its spot instead --
                        # spontaneous personality, not a reply to anyone.
                        flavor_roll = random.random()
                        if flavor_roll < 0.12:
                            text = pick_phrase(INDIFFERENT_EMOJI_QUIPS if random.random() < 0.4 else INDIFFERENT_QUIPS)
                        elif flavor_roll < 0.24:
                            text = pick_phrase(SLEEPY_EMOJI_QUIPS if random.random() < 0.4 else SLEEPY_QUIPS)
                        elif flavor_roll < 0.36:
                            text = pick_phrase(BELLY_RUB_QUIPS)
                        elif flavor_roll < 0.48:
                            text = pick_phrase(TERRITORIAL_QUIPS)
                        else:
                            text = pick_phrase(BORED_MESSAGES)
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    print(f"[bored_cat_job] chat {chat_id}: {e}", flush=True)
    finally:
        # reschedule with a random interval (more frequent at night)
        if 2 <= hour_now() <= 5:
            delay = random.uniform(1800, 3600)   # 30-60 min at night
        else:
            delay = random.uniform(2700, 6300)   # 45-105 min during the day
        context.application.job_queue.run_once(bored_cat_job, delay)

async def social_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    # try/finally around the whole body: any failure (even random.choice on
    # an unexpectedly empty list) must never skip the reschedule below, or
    # this job silently stops firing forever until the process restarts.
    try:
        text = pick_phrase(SOCIAL_REMINDERS)
        for chat_id in list(_known_chats.keys()):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                print(f"[social_reminder_job] chat {chat_id}: {e}", flush=True)
    finally:
        # -20% frequency: reschedule every 8.75-11.25 hours (was 7-9h)
        context.application.job_queue.run_once(social_reminder_job, random.uniform(31500, 40500))

async def monad_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = pick_phrase(MONAD_REMINDERS)
        for chat_id in list(_known_chats.keys()):
            try:
                # parse_mode="Markdown" here specifically -- MONAD_REMINDERS
                # entries use backticks around the contract address intending
                # Telegram's tap-to-copy code formatting, which never
                # rendered without this (safe here, unlike other send_message
                # calls in this file, since these strings are fixed and
                # contain no user-controlled text that could break parsing).
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                print(f"[monad_reminder_job] chat {chat_id}: {e}", flush=True)
    finally:
        # -20% frequency: reschedule every 13.75-16.25 hours (was 11-13h)
        context.application.job_queue.run_once(monad_reminder_job, random.uniform(49500, 58500))

async def game_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = pick_phrase(GAME_REMINDERS)
        for chat_id in list(_known_chats.keys()):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                print(f"[game_reminder_job] chat {chat_id}: {e}", flush=True)
    finally:
        # -20% frequency: reschedule every 13.75-16.25 hours (was 11-13h)
        context.application.job_queue.run_once(game_reminder_job, random.uniform(49500, 58500))

async def nft_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = pick_phrase(NFT_REMINDERS)
        for chat_id in list(_known_chats.keys()):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                print(f"[nft_reminder_job] chat {chat_id}: {e}", flush=True)
    finally:
        # -20% frequency: reschedule every 13.75-16.25 hours (was 11-13h)
        context.application.job_queue.run_once(nft_reminder_job, random.uniform(49500, 58500))

# once/day, random moment inside this UTC window
MERCH_ANNOUNCEMENT_WINDOW_UTC = (12, 22)

async def merch_announcement_job(context: ContextTypes.DEFAULT_TYPE):
    """Posts MERCH_ANNOUNCEMENT once per calendar day (UTC) to every known
    chat, at a random moment inside MERCH_ANNOUNCEMENT_WINDOW_UTC. Mirrors
    tweet_slot_job's persisted date-dedupe (not the other reminder jobs'
    every-N-hours drift) since "once a day" needs a real calendar-day
    guarantee, restart-safe across Render redeploys, rather than an interval
    that could land twice in the same day or skip one.

    try/finally around the whole body so any failure above (e.g. a chat
    send erroring out) can never skip the reschedule, which would otherwise
    silently kill this job forever until the process restarts -- same
    pattern as every other job in this file."""
    try:
        today = datetime.utcnow().date().isoformat()
        if db.get_config("last_merch_announcement_date") != today:
            # set BEFORE posting -- same restart-safety reasoning as
            # tweet_slot_job's slot_key: a redeploy landing mid-send must
            # not recompute and post a second announcement the same day.
            db.set_config("last_merch_announcement_date", today)
            for chat_id in list(_known_chats.keys()):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=MERCH_ANNOUNCEMENT, parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"[merch_announcement_job] chat {chat_id}: {e}", flush=True)
    finally:
        delay = _seconds_until_window(*MERCH_ANNOUNCEMENT_WINDOW_UTC, force_next_day=True)
        context.application.job_queue.run_once(merch_announcement_job, delay)

# once/day, random moment inside this UTC window
DIVIDENDS_REMINDER_WINDOW_UTC = (12, 22)

async def dividends_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Posts one DIVIDENDS_REMINDERS line once per calendar day (UTC) to every
    known chat, at a random moment inside DIVIDENDS_REMINDER_WINDOW_UTC.
    Same calendar-day dedupe as merch_announcement_job (restart-safe across
    Render redeploys) rather than the every-N-hours drift the other reminder
    jobs use, since "once a day" needs a real guarantee here too."""
    try:
        today = datetime.utcnow().date().isoformat()
        if db.get_config("last_dividends_reminder_date") != today:
            # set BEFORE posting -- a redeploy landing mid-send must not
            # recompute and post a second reminder the same day.
            db.set_config("last_dividends_reminder_date", today)
            text = pick_phrase(DIVIDENDS_REMINDERS)
            for chat_id in list(_known_chats.keys()):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    print(f"[dividends_reminder_job] chat {chat_id}: {e}", flush=True)
    finally:
        delay = _seconds_until_window(*DIVIDENDS_REMINDER_WINDOW_UTC, force_next_day=True)
        context.application.job_queue.run_once(dividends_reminder_job, delay)

# once/day, random moment inside this UTC window
PENKMARKET_ANNOUNCEMENT_WINDOW_UTC = (12, 22)

async def penkmarket_announcement_job(context: ContextTypes.DEFAULT_TYPE):
    """Posts one PENKMARKET_ANNOUNCEMENTS variant once per calendar day (UTC) to
    every known chat, at a random moment inside PENKMARKET_ANNOUNCEMENT_WINDOW_UTC.
    Same calendar-day dedupe as merch_announcement_job/dividends_reminder_job
    (restart-safe across Render redeploys)."""
    try:
        today = datetime.utcnow().date().isoformat()
        if db.get_config("last_penkmarket_announcement_date") != today:
            # set BEFORE posting -- a redeploy landing mid-send must not
            # recompute and post a second announcement the same day.
            db.set_config("last_penkmarket_announcement_date", today)
            text = pick_phrase(PENKMARKET_ANNOUNCEMENTS)
            for chat_id in list(_known_chats.keys()):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    print(f"[penkmarket_announcement_job] chat {chat_id}: {e}", flush=True)
    finally:
        delay = _seconds_until_window(*PENKMARKET_ANNOUNCEMENT_WINDOW_UTC, force_next_day=True)
        context.application.job_queue.run_once(penkmarket_announcement_job, delay)

# ══════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════
def _is_other_topic(msg) -> bool:
    """True if msg belongs to a forum topic other than General ('The Bowl')."""
    return bool(getattr(msg, "is_topic_message", False))

async def _maybe_react(context: ContextTypes.DEFAULT_TYPE, msg) -> None:
    """Ambient presence: independent of whatever text reply (if any) this
    message triggers, small chance the cat leaves a native reaction on it --
    half the time drawn from the current mood's favorites, otherwise the
    general pool. Best-effort: a chat with reactions disabled, or a message
    too old to react to, must never take the rest of the handler down with it."""
    if random.random() >= REACTION_CHANCE:
        return
    pool = MOOD_REACTIONS[current_mood()] if random.random() < 0.5 else CAT_REACTIONS
    try:
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji=pick_phrase(pool))],
        )
    except Exception as e:
        print(f"[reaction] {e}", flush=True)

async def _maybe_indifferent_react(context: ContextTypes.DEFAULT_TYPE, msg) -> None:
    """Same best-effort native reaction as _maybe_react, but a smaller,
    deliberately unimpressed emoji pool -- called from a keyword block below
    when the trigger matched but the text-reply roll missed."""
    if random.random() >= INDIFFERENT_REACTION_CHANCE:
        return
    try:
        await context.bot.set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reaction=[ReactionTypeEmoji(emoji=pick_phrase(INDIFFERENT_REACTIONS))],
        )
    except Exception as e:
        print(f"[reaction] {e}", flush=True)

async def cmd_raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or _is_other_topic(update.message):
        return
    if update.message.chat.type == "private":
        return  # a raid call-to-action means nothing outside the group
    await update.message.reply_text(pick_phrase(RAID_RESPONSES))

async def leer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _bot_username

    if not update.message:
        return

    msg = update.message
    if _is_other_topic(msg):
        return
    if msg.chat.type == "private":
        return

    user = msg.from_user
    text   = (msg.text or msg.caption or "").strip()
    chat_id = msg.chat_id
    now     = time.time()
    h       = hour_now()

    tl = text.lower()

    # ── nadfun / rose: always first, before everything else, includes bots ───────
    if "iwru buy" in tl:
        print(f"[STICKER_BUY] from {user.username if user else '?'}: {text[:100]!r}", flush=True)
        await msg.reply_sticker(STICKER_BUY)
        return
    if "new human detected" in tl:
        print(f"[STICKER_WELCOME] from {user.username if user else '?'}: {text[:100]!r}", flush=True)
        await msg.reply_sticker(STICKER_WELCOME)
        return

    # ── ignore the rest of other bots' messages ─────────────────────────
    if user and user.is_bot:
        print(f"[BOT {user.username or '?'}]: {text[:120]!r}", flush=True)
        return

    # ── only human messages from here on ────────────────────────────
    _known_chats[chat_id] = now

    try:
        await events.on_group_activity(context, chat_id)
    except Exception as e:
        print(f"[events.on_group_activity] {e}", flush=True)

    if user:
        uid = user.id
        if uid not in _user_nicknames:
            _user_nicknames[uid] = pick_phrase(NICKNAMES)
        _known_users[uid] = {
            "chat_id":   chat_id,
            "name":      user.first_name or "human",
            "last_seen": now,
        }

    print(f"[{user.full_name if user else '?'}]: {text[:80]}", flush=True)

    await _maybe_react(context, msg)

    # ── New member joined ────────────────────────────────────────────────
    if msg.new_chat_members:
        for member in msg.new_chat_members:
            if member.is_bot:
                continue
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await msg.reply_text(pick_phrase(JOIN_REPLIES).replace("{name}", member.first_name or "human"))
        return

    # ── Member left ───────────────────────────────────────────────────────
    if msg.left_chat_member and not msg.left_chat_member.is_bot:
        if random.random() < 0.6:
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await msg.reply_text(pick_phrase(LEAVE_REPLIES).replace("{name}", msg.left_chat_member.first_name or "human"))
        return

    # ── Sticker ────────────────────────────────────────────────────────────
    if msg.sticker:
        if 8 <= h <= 10 and random.random() < 0.44:  # -20% (was 0.55)
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await msg.reply_text(pick_phrase(GM_REPLIES))
        elif 22 <= h <= 23 and random.random() < 0.44:  # -20% (was 0.55)
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await msg.reply_text(pick_phrase(GN_REPLIES))
        elif random.random() < 0.16:  # -20% (was 0.20)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await msg.reply_text(pick_phrase(STICKER_REACTIONS))
        return

    # ── Photo ──────────────────────────────────────────────────────────────
    if msg.photo and random.random() < 0.12:  # -20% (was 0.15)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await msg.reply_text(pick_phrase(PHOTO_REACTIONS))
        return

    if not text:
        return

    # ── Tweet URL → raid (always, before the counter) ────────────────────
    if TWEET_URL_RE.search(text):
        await asyncio.sleep(5)
        await msg.reply_text(pick_phrase(RAID_RESPONSES))
        return

    # ── Raid (always, before the counter) ────────────────────────────────
    if _contains_word(tl, RAID_TRIGGERS):
        await msg.reply_text(pick_phrase(RAID_RESPONSES))
        return

    # ── Rose filter exact matches (always) ───────────────────────────────
    tl_stripped = tl.strip()
    if tl_stripped == "ca":
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await msg.reply_text(pick_phrase(CA_REPLIES))
        return
    if tl_stripped in ("website", "site", "web"):
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await msg.reply_text(pick_phrase(WEBSITE_REPLIES))
        return
    if tl_stripped in ("social", "socials"):
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await msg.reply_text(pick_phrase(SOCIAL_REPLIES))
        return
    if tl_stripped in ("filters", "filter"):
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await msg.reply_text(pick_phrase(FILTER_REPLIES))
        return
    if tl_stripped == "iwillrugu":
        await asyncio.sleep(random.uniform(1.5, 4.0))
        await msg.reply_text(pick_phrase(IWRU_FILTER_REPLIES))
        return

    # ── Message counter → chaos burst ────────────────────────────────
    _msg_counter[chat_id] = _msg_counter.get(chat_id, 0) + 1
    if chat_id not in _next_trigger:
        _next_trigger[chat_id] = random.randint(10, 18)
    if _msg_counter[chat_id] >= _next_trigger[chat_id]:
        _msg_counter[chat_id] = 0
        _next_trigger[chat_id] = random.randint(10, 18)
        if random.random() < 0.60:  # +15% (was 0.52)
            await asyncio.sleep(random.uniform(1.0, 3.5))
            await msg.reply_text(pick_phrase(CHAOS_BURSTS))
            return

    # ── IWRU name ──────────────────────────────────────────────────────────
    if _contains_word(tl, IWRU_TRIGGERS) or tl_stripped in ("iwru", "@iwru"):
        if random.random() < 0.60:  # +15% (was 0.52)
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await msg.reply_text(pick_phrase(IWRU_NAME_REPLIES))
            if random.random() < 0.11:  # +15% (was 0.096)
                await asyncio.sleep(random.uniform(4, 7))
                await msg.reply_text(pick_phrase(FOLLOWUP_MESSAGES))
            return

    # ── GM ─────────────────────────────────────────────────────────────────
    if _starts_with_word(tl, GM_TRIGGERS) and random.random() < 0.55:  # +15% (was 0.48)
        await asyncio.sleep(random.uniform(0.5, 2.0))
        await msg.reply_text(pick_phrase(GM_REPLIES))
        return

    # ── GN ─────────────────────────────────────────────────────────────────
    if _starts_with_word(tl, GN_TRIGGERS) and random.random() < 0.55:  # +15% (was 0.48)
        await asyncio.sleep(random.uniform(0.5, 2.0))
        await msg.reply_text(pick_phrase(GN_REPLIES))
        return

    # ── Hi / Hello (generic greeting, any time of day) ──────────────────────
    if _starts_with_word(tl, HI_TRIGGERS) and random.random() < 0.45:
        await asyncio.sleep(random.uniform(0.5, 2.0))
        await msg.reply_text(pick_phrase(HI_REPLIES))
        return

    # ── Moon / pump ────────────────────────────────────────────────────────
    if _contains_word(tl, MOON_TRIGGERS) and random.random() < 0.41:  # +15% (was 0.36)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await msg.reply_text(pick_phrase(MOON_REPLIES))
        if random.random() < 0.11:  # +15% (was 0.096)
            await asyncio.sleep(random.uniform(4, 7))
            await msg.reply_text(pick_phrase(FOLLOWUP_MESSAGES))
        return

    # ── Dip / dump ─────────────────────────────────────────────────────────
    if _contains_word(tl, DIP_TRIGGERS) and random.random() < 0.41:  # +15% (was 0.36)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await msg.reply_text(pick_phrase(DIP_REPLIES))
        if random.random() < 0.11:  # +15% (was 0.096)
            await asyncio.sleep(random.uniform(4, 7))
            await msg.reply_text(pick_phrase(FOLLOWUP_MESSAGES))
        return

    # ── Wen ────────────────────────────────────────────────────────────────
    if _contains_word(tl, WEN_TRIGGERS) and random.random() < 0.60:  # +15% (was 0.52)
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await msg.reply_text(pick_phrase(WEN_REPLIES))
        return

    # ── Chart / price ──────────────────────────────────────────────────────
    if _contains_word(tl, CHART_TRIGGERS):
        if random.random() < 0.37:  # +15% (was 0.32)
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await msg.reply_text(pick_phrase(CHART_REPLIES))
            return
        await _maybe_indifferent_react(context, msg)

    # ── Monad ──────────────────────────────────────────────────────────────
    if _contains_word(tl, MONAD_TRIGGERS) and random.random() < 0.46:  # +15% (was 0.40)
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await msg.reply_text(pick_phrase(MONAD_REPLIES))
        return

    # ── Cat ────────────────────────────────────────────────────────────────
    if _contains_word(tl, CAT_TRIGGERS):
        if random.random() < 0.42:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await msg.reply_text(pick_phrase(CAT_REPLIES))
            return
        await _maybe_indifferent_react(context, msg)

    # ── Crypto (generic vocabulary, distinct from moon/dip/wen/monad) ──────
    if _contains_word(tl, CRYPTO_TRIGGERS):
        if random.random() < 0.32:
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await msg.reply_text(pick_phrase(CRYPTO_REPLIES))
            return
        await _maybe_indifferent_react(context, msg)

    # ── Fish ───────────────────────────────────────────────────────────────
    if "fish" in tl:
        if random.random() < 0.60:  # +15% (was 0.52)
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await msg.reply_text(pick_phrase(FISH_REPLIES))
            if random.random() < 0.11:  # +15% (was 0.096)
                await asyncio.sleep(random.uniform(4, 7))
                await msg.reply_text(pick_phrase(FOLLOWUP_MESSAGES))
            return
        await _maybe_indifferent_react(context, msg)

    # ── Fish/seafood emoji (no "fish" word needed) ──────────────────────────
    if _contains_word(tl, FISH_EMOJI_TRIGGERS):
        if random.random() < 0.60:  # +15% (was 0.52)
            await asyncio.sleep(random.uniform(0.5, 2.0))
            await msg.reply_text(pick_phrase(FISH_EMOJI_REPLIES))
            return
        await _maybe_indifferent_react(context, msg)

    # ── Direct @mention ────────────────────────────────────────────────────
    if _bot_username is None:
        try:
            _bot_username = (await context.bot.get_me()).username
        except Exception as e:
            # Transient get_me() failure -- don't let it raise out of the
            # whole message handler over just this one check; leave
            # _bot_username None so it's simply retried on the next message.
            print(f"[leer] get_me() failed: {e}", flush=True)
    if _bot_username and f"@{_bot_username}".lower() in tl:
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await msg.reply_text(pick_phrase(IWRU_COMMAND_REPLIES))
        return

    # ── Random quip (boost x2 between 2-5am, further biased by mood) ────────
    night_boost = 2.0 if 2 <= h <= 5 else 1.0
    mood_speak_mult = MOOD_BIAS[current_mood()]["speak_mult"]
    last = _last_random.get(chat_id, 0)
    if now - last > RANDOM_COOLDOWN and random.random() < RANDOM_CHANCE * night_boost * mood_speak_mult:
        _last_random[chat_id] = now
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await msg.reply_text(pick_phrase(RANDOM_QUIPS))

# ══════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════
def _delete_webhook_http():
    import urllib.request, json
    url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            print(f"[startup] deleteWebhook → {data}", flush=True)
    except Exception as e:
        print(f"[startup] deleteWebhook error: {e}", flush=True)

async def _conflict_handler(update, context):
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        print(f"[conflict] {context.error} — deleting webhook...", flush=True)
        try:
            await context.bot.delete_webhook(drop_pending_updates=True)
            print("[conflict] webhook deleted, polling will continue", flush=True)
        except Exception as e:
            print(f"[conflict] error deleting: {e}", flush=True)
    else:
        print(f"[error] {context.error}", flush=True)

def build_app():
    a = ApplicationBuilder().token(TOKEN).build()
    events.register(a)
    a.add_handler(CommandHandler("raid", cmd_raid))
    a.add_handler(MessageHandler(filters.ALL, leer))
    a.add_error_handler(_conflict_handler)
    for cid in KNOWN_CHAT_IDS:
        _known_chats.setdefault(cid, time.time())
    if KNOWN_CHAT_IDS:
        print(f"[startup] pre-registered chats: {KNOWN_CHAT_IDS}", flush=True)
    a.job_queue.run_once(bored_cat_job, random.uniform(2700, 5400))
    a.job_queue.run_once(social_reminder_job, random.uniform(10800, 21600))   # first reminder: 3-6h
    a.job_queue.run_once(monad_reminder_job, random.uniform(7200, 18000))     # first reminder: 2-5h
    a.job_queue.run_once(game_reminder_job, random.uniform(14400, 25200))     # first reminder: 4-7h
    a.job_queue.run_once(nft_reminder_job, random.uniform(21600, 32400))      # first reminder: 6-9h
    a.job_queue.run_once(merch_announcement_job, _seconds_until_window(*MERCH_ANNOUNCEMENT_WINDOW_UTC))
    a.job_queue.run_once(dividends_reminder_job, _seconds_until_window(*DIVIDENDS_REMINDER_WINDOW_UTC))
    a.job_queue.run_once(penkmarket_announcement_job, _seconds_until_window(*PENKMARKET_ANNOUNCEMENT_WINDOW_UTC))
    buybot.register(a)  # $IWRU buy alerts; self-gated on BUYBOT_ENABLED
    if TWITTER_ENABLED:
        for slot_start, slot_end in TWEET_SLOTS:
            a.job_queue.run_once(tweet_slot_job, _seconds_until_window(slot_start, slot_end), data=(slot_start, slot_end))
        print(f"[twitter] {len(TWEET_SLOTS)} tweet jobs scheduled (UTC slots: {TWEET_SLOTS})", flush=True)
        a.job_queue.run_once(merch_tweet_job, _seconds_until_madrid_window(*MERCH_TWEET_WINDOW_MADRID))
    else:
        print("[twitter] disabled — set TWITTER_API_KEY/SECRET/ACCESS_TOKEN/ACCESS_TOKEN_SECRET to enable", flush=True)
    return a

print("======================================", flush=True)
print("      IWRU BOT — I WILL RUG U", flush=True)
print("======================================", flush=True)

_delete_webhook_http()
time.sleep(35)

while True:
    try:
        _delete_webhook_http()
        app = build_app()
        app.run_polling(drop_pending_updates=True)
        break
    except Exception as e:
        print(f"[restart] {e} — retrying in 35s", flush=True)
        time.sleep(35)
