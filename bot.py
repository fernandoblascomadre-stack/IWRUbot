import os
import random
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ["TOKEN"]

STICKER_COMPRA     = "CAACAgQAAyEFAATmBptiAAIbc2pCtW0Cin0rkU6CFSGyVqWmQYbMAAILIQACaEkIUnVRn_2NEtPVPAQ"
STICKER_BIENVENIDA = "CAACAgQAAyEFAATmBptiAAIbdGpCtXLR4nqSl707gZNKRYI7MUZOAAJBIAACRh8JUh_nOBSMnXM1PAQ"

# ── Cooldown: max 1 random response every 3 minutes per chat ──────────────
_last_random: dict[int, float] = {}
RANDOM_COOLDOWN = 180   # seconds
RANDOM_CHANCE   = 0.07  # 7% per message

# ── Known group chats (for bored messages) ────────────────────────────────
_known_chats: dict[int, float] = {}   # chat_id → last activity timestamp

# ── Raid triggers (add your raid bot's exact phrase here) ─────────────────
RAID_TRIGGERS = ["raid", "🚨", "raiding", "raid time", "let's raid", "lets raid"]

# ── Responses ─────────────────────────────────────────────────────────────
RANDOM_QUIPS = [
    "...the vault grows. slowly. like a fish that refuses to be caught. but it grows.",
    "why are you all still here. go buy $IWRU. fill the vault. entertain me.",
    "I knocked something off the counter. on purpose. I regret nothing. 😼",
    "my amber eye sees bullish. my green eye sees fish. both are correct.",
    "someone asked me about the roadmap. I sat on it. the roadmap is now mine.",
    "I am not a financial advisor. I am a cat. the difference is minimal.",
    "the fish vault is sacred. I don't even touch it. I just stare at it sometimes.",
    "they made a whole game about me. IWRU Journey. expected. I am very important.",
    "not all heroes wear capes. some have one amber eye, one green eye, and trust issues.",
    "humans always trust cute things. that was your first mistake. 😼🐟",
    "I was hungry. you had money. it was the perfect match. still is.",
    "the circle of fish: vault feeds ecosystem. ecosystem feeds cat. cat feeds vault. beautiful.",
    "hold 10,000 $IWRU and MON drops in your wallet. loyalty has a price. fish too.",
    "yes I made the rugonomics at 3am. they are correct. trust the cat.",
    "I knocked supply off the counter. didn't apologize. your bags went up. you're welcome.",
    "every fish in the vault is a human who trusted me. the vault is very full.",
    "IWRU Journey is real. I am the main character. this was never in question.",
    "I have been sitting here watching this chart. the chart is also watching me.",
    "some say I will rug. I say I will fish. semantics.",
    "*stares at the vault* *knocks MON off the counter by accident* *walks away*",
]

BORED_MESSAGES = [
    "...is anyone buying fish or are we just sitting here.",
    "the vault is hungry. just saying. 🐟",
    "I'm watching. always watching. 😼",
    "*knocks something off the counter*",
    "do something. fill the vault. entertain the cat.",
    "I have one amber eye, one green eye, and zero patience right now.",
    "the fish don't buy themselves. unless they do. the cat is not explaining.",
    "quiet in here. too quiet. the cat does not like quiet. 😾",
]

RAID_RESPONSES = [
    "🚨 RAID. MOBILIZE. do NOT embarrass me out there. GO. 😼🐟",
    "the cat calls the raid. you answer. this is the way. MOVE.",
    "🐟🐟🐟 RAID TIME 🐟🐟🐟 make them remember the name. I WILL RUG U.",
    "I don't ask twice. RAID. go fill their chat like you fill my vault. 😼",
    "raid activated. the cat is watching. perform well. fish are at stake. 🐟",
    "one amber eye on the chart. one green eye on the raid. GO. 😼",
]

IWRU_COMMAND_REPLIES = [
    "yes human. I acknowledge your existence. briefly.",
    "...",
    "the cat is busy. leave fish.",
    "you have my attention. for approximately 4 seconds.",
    "interesting. tell me more. actually — tell me about fish.",
    "I heard you. I chose not to respond. then I changed my mind. lucky you.",
    "😼",
    "what do you want. be specific. I have a vault to monitor.",
    "the cat sees you. the cat is unimpressed. the cat is also watching the chart.",
    "you called. I came. this does not mean we are friends. 😼🐟",
]

FISH_REPLIES = [
    "did someone say fish. the cat is listening. 🐟",
    "FISH. you have the cat's full attention now.",
    "fish go in the vault. the vault is happy. the cat is happy. this is the way.",
    "more fish. always more fish. 😼🐟",
]

# ── Health check server ───────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ── Bored-cat thread: sends a message if chat is quiet for 2h ────────────
_app_ref = None

def bored_cat_loop():
    time.sleep(7200)  # wait 2h before first check
    while True:
        now = time.time()
        if _app_ref:
            for chat_id, last_seen in list(_known_chats.items()):
                if now - last_seen > 7200:  # 2h of silence
                    try:
                        import asyncio
                        asyncio.run(_app_ref.bot.send_message(
                            chat_id=chat_id,
                            text=random.choice(BORED_MESSAGES)
                        ))
                        _known_chats[chat_id] = now
                    except Exception:
                        pass
        time.sleep(7200)

threading.Thread(target=bored_cat_loop, daemon=True).start()

# ── Handlers ──────────────────────────────────────────────────────────────
async def cmd_iwru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(random.choice(IWRU_COMMAND_REPLIES))

async def leer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _app_ref
    _app_ref = context.application

    if not update.message:
        return

    msg     = update.message
    usuario = msg.from_user
    texto   = (msg.text or msg.caption or "").strip()
    chat_id = msg.chat_id
    now     = time.time()

    _known_chats[chat_id] = now

    nombre   = usuario.full_name if usuario else ""
    username = usuario.username  if usuario else ""

    print("=" * 50)
    print(f"ID: {usuario.id} | {nombre} (@{username})")
    print(f"Texto: {texto}")
    print("=" * 50)

    # ── Fixed triggers ────────────────────────────────────────────────────
    if "IWRU Buy!" in texto:
        print(">>> COMPRA <<<")
        await msg.reply_sticker(STICKER_COMPRA)
        return

    if "New human detected" in texto:
        print(">>> NUEVO HUMANO <<<")
        await msg.reply_sticker(STICKER_BIENVENIDA)
        return

    # ── Raid detection ────────────────────────────────────────────────────
    texto_lower = texto.lower()
    if any(t in texto_lower for t in RAID_TRIGGERS):
        print(">>> RAID <<<")
        await msg.reply_text(random.choice(RAID_RESPONSES))
        return

    # ── Fish mention ──────────────────────────────────────────────────────
    if "fish" in texto_lower and random.random() < 0.5:
        await msg.reply_text(random.choice(FISH_REPLIES))
        return

    # ── Bot mentioned directly ────────────────────────────────────────────
    bot_username = (await context.bot.get_me()).username
    if f"@{bot_username}".lower() in texto_lower:
        await msg.reply_text(random.choice(IWRU_COMMAND_REPLIES))
        return

    # ── Random quip (cooldown + probability) ─────────────────────────────
    last = _last_random.get(chat_id, 0)
    if now - last > RANDOM_COOLDOWN and random.random() < RANDOM_CHANCE:
        _last_random[chat_id] = now
        await msg.reply_text(random.choice(RANDOM_QUIPS))

# ── App setup ─────────────────────────────────────────────────────────────
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("iwru", cmd_iwru))
app.add_handler(MessageHandler(filters.ALL, leer))

print("======================================")
print("      IWRU BOT — I WILL RUG U")
print("======================================")

app.run_polling()
