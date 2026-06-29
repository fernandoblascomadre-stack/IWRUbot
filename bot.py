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

# ── Cooldown ───────────────────────────────────────────────────────────────
_last_random: dict[int, float] = {}
RANDOM_COOLDOWN = 180
RANDOM_CHANCE   = 0.07

# ── Known chats (for bored messages) ──────────────────────────────────────
_known_chats: dict[int, float] = {}

# ── Trigger keywords ───────────────────────────────────────────────────────
RAID_TRIGGERS  = ["⚡️ raid tweet", "raid tweet", "⚡️ raid"]
GM_TRIGGERS    = ["gm", "good morning", "morning fam", "buenos días", "gm everyone", "gm fam", "rise and shine"]
GN_TRIGGERS    = ["gn", "good night", "goodnight", "buenas noches", "gn everyone", "sleep well", "going to sleep"]
MOON_TRIGGERS  = ["moon", "🚀", "pump", "pumping", "mooning", "ath", "all time high", "bullish", "we're going up", "to the moon"]
DIP_TRIGGERS   = ["dip", "dump", "dumping", "red", "crashed", "bleeding", "ngmi", "rekt", "it's over", "we're done"]
WEN_TRIGGERS   = ["wen ", "wen?", "when moon", "when pump", "wen lambo", "wen rich", "when rich"]
CHART_TRIGGERS = ["chart", "price", "marketcap", "market cap", "mcap", "📊", "📈", "📉"]
MONAD_TRIGGERS = ["monad", "#monad", "mon blockchain", "built on monad"]

# ── Phrase lists ───────────────────────────────────────────────────────────

RANDOM_QUIPS = [
    # vault & fish lore
    "...the vault grows. slowly. like a fish that refuses to be caught. but it grows.",
    "why are you all still here. go buy $IWRU. fill the vault. entertain me.",
    "the circle of fish: vault feeds ecosystem. ecosystem feeds cat. cat feeds vault. beautiful.",
    "every fish in the vault is a human who trusted me. the vault is very full.",
    "the fish vault is sacred. I don't even touch it. I just stare at it sometimes.",
    "hold 10,000 $IWRU and MON drops in your wallet. loyalty has a price. fish too.",
    "I knocked supply off the counter. didn't apologize. your bags went up. you're welcome.",
    "yes I made the rugonomics at 3am. they are correct. trust the cat.",
    "some say I will rug. I say I will fish. semantics.",
    "I was hungry. you had money. it was the perfect match. still is.",
    "bought the dip. sat on the dip. the dip is warm now. comfortable.",
    "I don't need a whitepaper. I am the whitepaper. 😼",
    "I could dump my bag. I won't. not because I'm loyal. I just got comfortable.",
    "the NFTs exist because my chest was full of fish. needed more space. simple math.",
    "the fish vault heard everything you said. the fish vault remembers.",
    "I have been staring at the vault for [undisclosed] minutes. it has not moved. I will continue.",
    "every trade feeds the ecosystem. every ecosystem feeds the cat. every cat sits on the vault. this is math.",
    "humans put fish in the vault. the vault becomes powerful. the cat becomes pleased. everyone wins. mostly me.",
    "I don't hoard fish. I curate them. there's a difference. the difference is fish.",
    "the vault is patient. the vault has been waiting. the vault will keep waiting. fill it.",
    # cat philosophy
    "I am not a financial advisor. I am a cat. the difference is minimal.",
    "my amber eye sees bullish. my green eye sees fish. both are correct.",
    "not all heroes wear capes. some have one amber eye, one green eye, and trust issues.",
    "humans always trust cute things. that was your first mistake. 😼🐟",
    "someone asked me about the roadmap. I sat on it. the roadmap is now mine.",
    "I could have explained my plan. I chose mystery instead.",
    "I have been sitting here watching this chart. the chart is also watching me.",
    "they made a whole game about me. IWRU Journey. expected. I am very important.",
    "IWRU Journey is real. I am the main character. this was never in question.",
    "chaos is not a plan. chaos is a lifestyle. the cat has a lifestyle.",
    "I judge everyone in this group. silently. constantly. with both eyes.",
    "trust is built slowly. fish come faster. I prefer fish.",
    "the cat has two settings: completely asleep and aggressively present. there is no in between.",
    "I don't predict. I observe. and then I sit on the thing being observed.",
    "you think the cat is watching the vault. the cat is also watching you. simultaneously.",
    "I have learned one thing in my years of existence. fish. also: knock things off counters.",
    "some cats knock things over by accident. I do it with intention. every time. including now.",
    "Monad is fast. the cat appreciates speed. mostly for running at 3am for no reason.",
    "the cat is on Monad. the cat is everywhere. simultaneously. this is the way.",
    "someone looked at the chart and smiled. this pleases the cat. 😼",
    # real cat chaos
    "*stares at the vault* *knocks MON off the counter by accident* *walks away*",
    "*opens cabinet* ...okay. *closes cabinet* okay.",
    "*sits directly on the keyboard* asjkhdasjkdhaksjdh 🐟🐟🐟",
    "*finds a box* I live here now. the box is mine. everything is fine.",
    "*sprints across the room for no reason* I'm back. don't ask.",
    "I knocked it off the counter. it needed to be on the floor. you wouldn't understand.",
    "I was asleep. now I am awake. I don't know why. you didn't do anything. probably.",
    "it is 3am somewhere and I am fully awake and I feel INCREDIBLE",
    "I wanted attention. you gave me attention. I no longer want it.",
    "the floor is lava. also the chart is bullish. these things are related.",
    "I am going to sleep for 16 hours. this is my contribution to the ecosystem. you're welcome.",
    "the tokenomics make sense because I wrote them at 3am while sitting in a box. this is called expertise.",
    "*stares at the wall for 11 minutes* ...nothing. I knew it.",
    "you are all very loud today. the cat is trying to sleep. please buy in silence.",
    "many humans. many words. very few fish. disappointing.",
    "I read everything you wrote. I have thoughts. I'm keeping them.",
    "I was going to analyze the chart. then something on the floor caught my attention. the floor won.",
    "*hears a sound* WHO'S THERE. *sound stops* ...nothing. I knew it. I knew it was nothing.",
    "the zoomies have arrived. I don't control the zoomies. the zoomies control me.",
    "*knocks your phone off the table while you were reading the chart* ...sorry. I'm not sorry.",
    "I sat in the sink. it's mine now. everything I sit in is mine. including this group.",
    "I found a paper bag. I entered the paper bag. the paper bag is my whole personality now.",
    "*bites your ankle for no reason* ...you know what you did.",
    "*sits on your hands while you're trying to type* this is mine now. so are you.",
    "I knocked the glass off the table. I watched it fall the whole way. majestic. 😼",
    "this conversation is interesting. I lied. it isn't. buy $IWRU.",
    "I was not paying attention to anything you were doing. and yet here I am.",
    "I chose to be here. I want credit for choosing. this was not easy.",
    "I could be asleep right now. I chose chaos. specifically this chaos.",
    "*walks in* *makes eye contact* *knocks one thing off the table* *walks out*",
    "I sat on your investment strategy. it's different now. you'll thank me later.",
    "I have 0 regrets. I have 0 apologies. I have 1 vault. it's full. 😼🐟",
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
    "...did you hear that.",
    "*stares at the corner of the room* there is something there. you can't see it. I can.",
    "I have been sitting here. thinking. about fish. mostly fish.",
    "*tail flick* ...",
    "someone buy something. anything. the cat needs stimulation.",
    "*walks in* *looks around* *walks out*",
    "I was going to sleep. then I remembered the vault exists. now I can't sleep.",
    "3am energy. no reason. no explanation. this is fine.",
    "wake up. the vault is hungry. I said WAKE UP.",
    "I'm going to knock something over. I haven't decided what yet. consider this a warning.",
    "if a cat stares at an empty chat and no one buys fish... does the vault still grow. yes. obviously. buy fish.",
    "someone send fish. the cat demands fish. this is not a request.",
    "*perks ears* ...no. nothing. carry on. buy $IWRU.",
    "I am comfortable and completely at peace. someone ruin it with good news about the chart.",
    "I knocked over the rugonomics presentation. they're fine. probably.",
    "*slow tail flick* ...",
    "I've been watching this chat for [undisclosed] minutes. nothing has happened. yet.",
    "*sits up suddenly* ...it's nothing. carry on. but also buy $IWRU.",
    "the vault has fish. the cat has patience. one of these is running low.",
    "I sat on the keyboard. accidentally sent something. I stand by it.",
    "*walks across the desk very slowly making eye contact the entire time*",
    "you should be buying $IWRU right now. instead you're doing... whatever this is.",
    "I am watching. I am always watching. I am also somehow asleep. this is cat technology.",
    "*hears something in the distance* ...I'll investigate. or not. probably not.",
    "*vibrates slightly* something is happening. or nothing is happening. the cat knows.",
    "I knocked the price prediction off the counter. it landed bullish. obviously.",
    "the cat is bored. this is your problem. solve it with $IWRU. 😼🐟",
    "there are 24 hours in a day. I spend 18 sleeping and 6 watching the vault. I am very busy.",
    "I'm not going anywhere. I live here now. feed the vault. I'm watching.",
    "*stares at nothing* *stares at you* *goes back to staring at nothing*",
    "someone do something. ANYTHING. the fish are watching too. 🐟",
    "I have knocked 7 things off 7 surfaces today. the 8th is still being selected.",
]

RAID_RESPONSES = [
    "🚨 RAID. MOBILIZE. do NOT embarrass me out there. GO. 😼🐟",
    "the cat calls the raid. you answer. this is the way. MOVE.",
    "🐟🐟🐟 RAID TIME 🐟🐟🐟 make them remember the name. I WILL RUG U.",
    "I don't ask twice. RAID. go fill their chat like you fill my vault. 😼",
    "raid activated. the cat is watching. perform well. fish are at stake. 🐟",
    "one amber eye on the chart. one green eye on the raid. GO. 😼",
    "*stops knocking things over* oh. RAID. okay. EVERYONE MOVE. NOW.",
    "I was asleep. I am no longer asleep. RAID. let's go. 😼🐟",
    "the cat does not run. except right now. RAID. GO GO GO.",
    "I woke up and chose chaos. RAID TIME. make it count. 🐟",
    "THE CAT HAS BEEN SUMMONED. RAID. do not make me come over there. 😼",
    "raid incoming. the vault is watching. I am watching. everything is watching. GO.",
    "*immediately knocks everything off the desk* RAID. let's move. 🐟",
    "I was napping. I am no longer napping. this raid had better be worth it. 😼",
    "you raid them. I'll watch from here. with both eyes. judging your performance.",
    "every like is a fish. every retweet is a fish. GO GET THE FISH. 🐟",
    "the cat does not beg. the cat commands. RAID. NOW. this is the command.",
    "I woke up. I saw raid. I chose violence. GO. 😼",
    "do not embarrass the cat. do not embarrass the vault. DO NOT EMBARRASS THE FISH. raid.",
    "raid time is sacred time. move like you mean it. 🐟😼",
    "*activates both eyes* RAID. amber eye says go. green eye says go faster.",
    "less talking. more raiding. the cat has spoken. the fish await. 🐟",
    "I will remember who showed up. the vault will remember. the fish will remember.",
    "the cat gives one instruction: RAID. do not ask follow up questions. 😼",
    "I was having a snack. I dropped the snack. RAID is more important than snacks. GO.",
    "*sprints into the room* RAID. I felt it before the message arrived. GO.",
    "this raid will be remembered. make sure it's for the right reasons. 😼🐟",
    "I don't celebrate until after. GO FIRST. celebrate later. fish after that.",
    "the vault feeds on good raids. feed the vault. RAID. 🐟",
    "I have been waiting for this raid. I didn't know I was waiting. but I was. GO. 😼",
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
    "*slow blink* ...okay.",
    "I was in the middle of something. I wasn't. but still.",
    "pet me. no not like that. not like that either. actually don't.",
    "I came. I looked. I sat on it. this is my process.",
    "the cat has noted your message. the cat will do what it wants with this information.",
    "*stares at you* *stares at the wall* *stares back at you* yes?",
    "I was watching the vault. now I'm watching you. the vault was more interesting. 😼",
    "yes. what. why. okay. goodbye. 😼",
    "*slow blink* that means I trust you. don't ruin it.",
    "I have two moods: completely ignoring you, and this. you got lucky.",
    "I sat on your message. it's mine now. so is your attention. buy $IWRU.",
    "the cat appeared. the cat will disappear. this is how it has always been.",
    "you called the cat. the cat is here. the cat is already thinking about fish.",
    "*stares at you for 7 seconds without blinking* ...hi.",
    "I was going to ignore this. then I didn't. you're welcome I think.",
    "you have the cat's full attention. that's about 40% of total cat attention. the rest is on fish.",
    "I woke up. you were here. fate is strange.",
    "fine. I'm here. don't make it weird.",
    "the cat hears all. responds to almost none. today you got lucky. 😼🐟",
    "*knocks your question off the counter* next.",
    "interesting. *walks away slowly* interesting.",
    "I have been here this whole time. I chose now to reveal this. timing is everything.",
    "I sat on something. it was your request. it is mine now.",
    "one amber eye sees you. one green eye sees fish. you have my divided attention.",
    "*opens one eye* ...yes. *closes one eye*",
    "I acknowledge you exist. I'll decide how I feel about it later. possibly never.",
    "*knocks something over in your honor* you're welcome.",
    "the cat does not explain itself. the cat simply appears. like now.",
    "I came. I judged. I sat down. this is the full process.",
    "*bites your message then walks away* 😼",
    "I was going to be mysterious. I still am. this is me being mysterious.",
    "you called. I came. I'm already bored. this is not your fault. probably. 😼",
    "the cat was busy staring at nothing. this is now less important than that. and yet.",
    "I see you. you see me. one of us is about to walk away. 😼",
    "fine. what. 😼",
    "I'm here. briefly. don't photograph me. 😼🐟",
    "*sits on you* okay. I'm listening. but I'm also sitting on you. both.",
    "the cat answered. the cat will deny having answered. 😼",
]

FISH_REPLIES = [
    "did someone say fish. the cat is listening. 🐟",
    "FISH. you have the cat's full attention now.",
    "fish go in the vault. the vault is happy. the cat is happy. this is the way.",
    "more fish. always more fish. 😼🐟",
    "🐟🐟🐟 the cat has entered the conversation.",
    "fish mentioned. the cat has LEFT the vault and is NOW here.",
    "every fish belongs to the vault. every vault belongs to the circle. give fish.",
    "I was asleep. you said fish. I am awake now. this is your fault and I'm glad.",
    "the fish vault heard that. the fish vault is pleased.",
    "*sits up immediately* SAY. THAT. AGAIN.",
    "fish in the vault. fish in the chat. fish everywhere. correct amount of fish.",
    "I have been waiting for someone to say fish. I've been here the whole time. 🐟",
    "the cat's two loves: fish. and also fish.",
    "fish is the language I understand best. continue.",
    "*knocks everything else off the table* just the fish. only the fish. 🐟",
    "fish mentioned → cat activated → vault acknowledged. this is the sequence.",
    "I don't get excited about many things. fish is the exception. always. 🐟",
    "there are fish in that vault. there will be more fish. the prophecy continues.",
    "*vibrates slightly* 🐟",
    "fish. FISH. I have been saying this. fill the vault. fish go in vault. 🐟",
    "the fish called. I answered. I always answer when fish call.",
    "*drops everything* fish? WHERE. 🐟",
    "I once went 3 days without hearing the word fish. I don't speak of that time.",
    "fish are the language. $IWRU is the translation. the vault is the result. 😼🐟",
]

GM_REPLIES = [
    "gm. the cat has been awake since 3am. you are late.",
    "gm. the vault survived the night. as expected.",
    "gm human. the fish are still there. I checked. twice.",
    "good morning. I did not sleep. I watched the chart. I regret nothing.",
    "gm. *knocks your coffee off the table* 😼",
    "gm. the cat acknowledges the morning. reluctantly.",
    "good morning. buy $IWRU before breakfast. then breakfast. then more $IWRU.",
    "gm. I was already awake. I'm always awake. the vault doesn't sleep.",
    "good morning. another day. another fish. this is the way. 🐟",
    "gm. *stares at you* ...okay. morning.",
    "gm. the sun is up. the vault is up. the cat is up. everything is up. 📈😼",
    "good morning, human. the cat has been operational since an unreasonable hour.",
    "gm. I knocked something over at 4am. it was intentional. good morning.",
    "gm fam. the fish were restless last night. the vault held. as expected.",
    "good morning. the cat slept 0 hours. ran zoomies at 3am. fully recovered. gm. 😼",
]

GN_REPLIES = [
    "gn human. the cat will be watching the vault while you sleep. as always.",
    "good night. I will not be sleeping. I have things to knock over.",
    "gn. *immediately starts running at 3am for no reason*",
    "sleep well. I'll be here. staring at the vault. staring at the corner. staring at things.",
    "gn. the fish don't sleep. neither does the cat. rest well, human. 🐟",
    "good night. tomorrow buy more fish. this is the way.",
    "gn. *activates 3am zoomies the moment you close your eyes* 😼",
    "sleep. I'll guard the vault. by staring at it. very effective.",
    "gn. sweet dreams. dream about fish. 🐟",
    "good night human. the cat remains. the vault remains. all is well. 😼",
    "gn. I'll be here if you need me. I won't be here. I'll be running for no reason.",
    "sleep well. the cat will do its 3am ritual. you don't need to know what that is.",
    "gn. don't worry about the vault. the vault is fine. the cat is watching. *knocks something over*",
    "good night. I'm going to sit in the hallway and stare into the darkness. normal cat things.",
    "gn human. rest. the fish are not going anywhere. the vault is not going anywhere. go. 😼🐟",
]

MOON_REPLIES = [
    "...I see the chart. the cat approves. 😼",
    "the amber eye was right. as always.",
    "*does not react outwardly* *internally very pleased* 🐟",
    "I predicted this. I sat on the prediction. the prediction was correct.",
    "the vault grows. the ecosystem grows. the cat sits calmly and takes credit.",
    "good. fill my vault. then fill it more. we're not done. 😼🐟",
    "the cat does not celebrate. the cat continues. buy more.",
    "this is fine. this is expected. the cat always knew. trust the cat.",
    "*slow blink* ...more. 😼",
    "the fish told me this would happen. the fish are very wise.",
    "of course it's going up. the cat is involved. 😼",
    "I sat on the chart. it went up. coincidence? the cat thinks not.",
    "green is the color of fish. green is the color of charts. everything is connected. 😼🐟",
    "the cat has been patient. the chart has been rewarding. this is a reasonable arrangement.",
    "*knocks nothing over for once* ...I'm in a good mood. don't make it weird. 😼",
]

DIP_REPLIES = [
    "...the cat is unbothered. the vault is unbothered. the fish are unbothered.",
    "I knocked it off the counter. it goes back up. this is cat physics.",
    "red is just a color. the vault doesn't see colors. only fish.",
    "the cat bought the dip. the cat is now sitting on the dip. the dip is warm. comfortable.",
    "everything goes down before it goes up. the cat has knocked many things off many surfaces. they all landed.",
    "unbothered. watching the vault. 😼",
    "dip noted. dip irrelevant. buy. 🐟",
    "the cat has seen worse. the cat has caused worse. this is fine.",
    "hold. the fish vault doesn't panic. neither does the cat. 😼",
    "*continues staring at the vault* ...it'll be fine. the cat says so.",
    "I don't dip. I sit. things around me sometimes dip. then they stop dipping. trust the cat.",
    "the dip is temporary. the fish are permanent. the vault is eternal. buy the dip.",
    "I once knocked a full glass of water off the counter. it made a mess. then I cleaned it up. actually I didn't. someone else did. the point is: things recover.",
    "red chart. green eyes. the cat is watching. the cat is calm. the cat says: hold. 😼🐟",
    "the cat does not panic. the cat observes. the cat judges. the cat buys. 😼",
]

WEN_REPLIES = [
    "wen? when the vault is full. fill the vault.",
    "the cat does not predict timelines. the cat sits on timelines.",
    "wen. good question. next question.",
    "I don't answer wen questions. I answer fish questions. try again.",
    "wen moon. wen fish. wen vault full. in that order. probably.",
    "*stares at you* *stares at chart* *stares back at you* soon. 😼",
    "I sat on the calendar. it's gone now. but trust the cat.",
    "wen. the cat's answer: when I say so. trust the cat.",
    "time is a human concept. the cat does not recognize it. but soon. 😼",
    "wen. fill the vault first. then we talk about wen.",
    "wen moon. the cat says: stop asking wen. start buying. then moon happens. this is the sequence.",
    "I don't do timelines. I do fish. the fish know when. ask the fish.",
    "wen. the cat has heard this question 47 times. the cat's answer has not changed. fill. the. vault.",
    "soon™. the cat trademark pending on that one.",
    "wen rich. wen vault full. wen you buy more. in that order. 😼🐟",
]

CHART_REPLIES = [
    "I have been watching this chart. the chart knows I'm watching.",
    "the cat reads charts differently. with both eyes. simultaneously.",
    "green is good. green is fish. more green. 😼🐟",
    "the chart does what the chart does. the cat watches. the vault grows. this is the way.",
    "I was going to analyze this. then I stared at the wall instead. same result.",
    "the amber eye watches the chart. the green eye watches the fish. neither blinks.",
    "chart goes up: expected. chart goes down: temporary. cat stays: always. 😼",
    "numbers are just fish in disguise. trust the numbers. trust the fish. trust the cat.",
    "*taps the chart with one paw* yes. this. more of this.",
    "the cat does not stress about charts. the cat IS the chart. 😼",
    "I made the chart at 3am. I sat on the chart at 4am. it looks correct from here.",
    "charts are interesting. fish are more interesting. the chart has fish tendencies though. I approve.",
    "I have one eye on the chart and one eye on the vault. both eyes are pleased.",
    "the cat reads the chart like the cat reads humans: silently, with judgment, from a distance. 😼",
    "*knocks the bearish analysis off the table* there. chart fixed. 😼🐟",
]

MONAD_REPLIES = [
    "Monad. fast. the cat approves of fast. mostly for 3am zoomies. but also for transactions.",
    "built on Monad. the cat chose well. the cat always chooses well. 😼",
    "Monad is the chain. $IWRU is the fish. the vault is the bowl. everything makes sense now.",
    "the cat is on Monad. the cat is everywhere on Monad. simultaneously. 😼🐟",
    "Monad moves fast. like the cat at 3am. like the chart after the cat sits on it.",
    "the cat endorses Monad. the cat endorses fish. the cat endorses the vault. in that order.",
]

STICKER_REACTIONS = [
    "...I see your sticker. I raise you indifference. 😼",
    "*ignores your sticker* *looks at it again* ...fine. acceptable.",
    "the cat has reviewed your sticker. verdict: it is not fish. disappointing.",
    "*slow blink at your sticker*",
    "I would have sent a better sticker. I chose not to. 😼",
    "your sticker has been noted. the cat is unimpressed. the vault is indifferent. 🐟",
    "*sits on your sticker* mine now.",
    "sticker received. cat has opinions. cat is keeping them. 😼",
]

PHOTO_REACTIONS = [
    "the cat sees your photo. the cat has opinions. the cat is keeping them.",
    "*examines photo carefully* ...I've seen better. I've also seen fish.",
    "I looked at this for exactly 2 seconds. it is not the vault. and yet.",
    "*walks across your photo slowly*",
    "the cat acknowledges the photo. the cat moves on. 😼",
    "photo noted. the cat is judging. silently. always silently.",
    "is that a fish in the photo. I looked. it isn't. the cat is disappointed.",
]

# ── Health check ───────────────────────────────────────────────────────────
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

# ── Bored-cat thread ───────────────────────────────────────────────────────
_app_ref = None

def bored_cat_loop():
    time.sleep(7200)
    while True:
        now = time.time()
        if _app_ref:
            for chat_id, last_seen in list(_known_chats.items()):
                if now - last_seen > 7200:
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

# ── Handlers ───────────────────────────────────────────────────────────────
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
    tl       = texto.lower()

    print(f"[{nombre} @{username}]: {texto[:80]}")

    # ── Fixed sticker triggers ─────────────────────────────────────────────
    if "IWRU Buy!" in texto:
        await msg.reply_sticker(STICKER_COMPRA)
        return
    if "New human detected" in texto:
        await msg.reply_sticker(STICKER_BIENVENIDA)
        return

    # ── Sticker / photo reactions (low probability) ────────────────────────
    if msg.sticker and random.random() < 0.12:
        await msg.reply_text(random.choice(STICKER_REACTIONS))
        return
    if msg.photo and random.random() < 0.10:
        await msg.reply_text(random.choice(PHOTO_REACTIONS))
        return

    if not texto:
        return

    # ── Raid ───────────────────────────────────────────────────────────────
    if any(t in tl for t in RAID_TRIGGERS):
        await msg.reply_text(random.choice(RAID_RESPONSES))
        return

    # ── GM ─────────────────────────────────────────────────────────────────
    if any(tl.startswith(t) or tl == t for t in GM_TRIGGERS) and random.random() < 0.60:
        await msg.reply_text(random.choice(GM_REPLIES))
        return

    # ── GN ─────────────────────────────────────────────────────────────────
    if any(tl.startswith(t) or tl == t for t in GN_TRIGGERS) and random.random() < 0.60:
        await msg.reply_text(random.choice(GN_REPLIES))
        return

    # ── Moon / pump ────────────────────────────────────────────────────────
    if any(t in tl for t in MOON_TRIGGERS) and random.random() < 0.35:
        await msg.reply_text(random.choice(MOON_REPLIES))
        return

    # ── Dip / dump ─────────────────────────────────────────────────────────
    if any(t in tl for t in DIP_TRIGGERS) and random.random() < 0.35:
        await msg.reply_text(random.choice(DIP_REPLIES))
        return

    # ── Wen ────────────────────────────────────────────────────────────────
    if any(t in tl for t in WEN_TRIGGERS) and random.random() < 0.70:
        await msg.reply_text(random.choice(WEN_REPLIES))
        return

    # ── Chart / price ──────────────────────────────────────────────────────
    if any(t in tl for t in CHART_TRIGGERS) and random.random() < 0.30:
        await msg.reply_text(random.choice(CHART_REPLIES))
        return

    # ── Monad ──────────────────────────────────────────────────────────────
    if any(t in tl for t in MONAD_TRIGGERS) and random.random() < 0.50:
        await msg.reply_text(random.choice(MONAD_REPLIES))
        return

    # ── Fish mention ───────────────────────────────────────────────────────
    if "fish" in tl and random.random() < 0.55:
        await msg.reply_text(random.choice(FISH_REPLIES))
        return

    # ── Direct mention ─────────────────────────────────────────────────────
    bot_username = (await context.bot.get_me()).username
    if f"@{bot_username}".lower() in tl:
        await msg.reply_text(random.choice(IWRU_COMMAND_REPLIES))
        return

    # ── Random quip (cooldown + probability) ──────────────────────────────
    last = _last_random.get(chat_id, 0)
    if now - last > RANDOM_COOLDOWN and random.random() < RANDOM_CHANCE:
        _last_random[chat_id] = now
        await msg.reply_text(random.choice(RANDOM_QUIPS))

# ── App setup ──────────────────────────────────────────────────────────────
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("iwru", cmd_iwru))
app.add_handler(MessageHandler(filters.ALL, leer))

print("======================================")
print("      IWRU BOT — I WILL RUG U")
print("======================================")

app.run_polling()
