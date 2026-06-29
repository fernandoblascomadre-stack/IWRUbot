import os
import random
import threading
import time
from datetime import datetime
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

# ── User tracking ──────────────────────────────────────────────────────────
_known_chats: dict[int, float]  = {}
_known_users: dict[int, dict]   = {}   # user_id -> {chat_id, name, last_seen}
_user_nicknames: dict[int, str] = {}   # user_id -> assigned nickname

# ── Triggers ───────────────────────────────────────────────────────────────
RAID_TRIGGERS  = ["⚡️ raid tweet", "raid tweet", "⚡️ raid"]
GM_TRIGGERS    = ["gm", "good morning", "morning fam", "buenos días", "gm everyone", "gm fam", "rise and shine"]
GN_TRIGGERS    = ["gn", "good night", "goodnight", "buenas noches", "gn everyone", "sleep well", "going to sleep"]
MOON_TRIGGERS  = ["moon", "🚀", "pump", "pumping", "mooning", "ath", "all time high", "bullish", "we're going up", "to the moon"]
DIP_TRIGGERS   = ["dip", "dump", "dumping", "red", "crashed", "bleeding", "ngmi", "rekt", "it's over"]
WEN_TRIGGERS   = ["wen ", "wen?", "when moon", "when pump", "wen lambo", "wen rich", "when rich"]
CHART_TRIGGERS = ["chart", "price", "marketcap", "market cap", "mcap", "📊", "📈", "📉"]
MONAD_TRIGGERS = ["monad", "#monad", "mon blockchain", "built on monad"]
IWRU_TRIGGERS  = ["i will rug u", "i will rug you", "iwru 🐟", "iwru 😼", "iwru!"]

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

# {name} = first name (plain text, no notification)
# {nick} = their cat nickname
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
    "{name} I knocked something over earlier and thought of you. unrelated. buy $IWRU. 😼",
    "{name}. the cat has assigned you a role: fish provider. this is an honor. 🐟😼",
    "{name} I sat next to you the other day. metaphorically. in the blockchain. 😼🐟",
    "{name}. I found something. I lost it. you were nearby. unrelated. probably. buy $IWRU. 😼",
    "{name} the cat is watching you specifically. *slow blink* ...okay. you pass. 😼",
]

# ══════════════════════════════════════════════════════════════════════════
#  PHRASE LISTS
# ══════════════════════════════════════════════════════════════════════════

RANDOM_QUIPS = [
    # vault & fish lore
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
    # game lore
    "they put me in a desert in stage 6. it was 40 degrees. I found a fish near a dune. 🐟🌵 worth it.",
    "stage 7 has guardians. large ones. I am friends with 0 of them. this is expected. 😼",
    "someone in stage 7 keeps following me through tunnels. they call it a stalker. I call it a fan. 😼",
    "IWRU Journey... they make me run and jump. I do not run. except at 3am. the devs know me. 🎮😼",
    "I am simultaneously a video game character AND a financial instrument. I am multidisciplinary. 😼🎮",
    "stage 6 has laser enemies. I dodged one with my eyes closed. both eyes. 😼",
    "the developers added a desert level with heat. I told them I prefer fish. they added more enemies instead. 🐟😾",
    "in IWRU Journey I can cling to walls. in real life I also cling to walls. this is not a game mechanic. 😼",
    "stage 7 has something called the Núcleo. I don't know what's in there. I went in anyway. for fish. 🐟",
    "I have a video game, a token, an NFT collection, and a fish vault. most cats just sleep. 😼🐟",
    "the stalker in stage 7 follows me through tunnels. it cannot catch me. nothing can catch me. 😼",
    "stage 6 has fragments to collect. I collected them. I sat on them. I kept going. 😼🎮",
    "they built a whole game around me. correct decision. I would have done the same. 🎮😼",
    # NFT lore
    "I make NFTs because the vault needed more compartments. for fish. 🐟 the art is secondary.",
    "someone bought one of my NFTs. I used the money to buy fish. 🐟 this was always the plan. 😼",
    "my NFTs fund the fish. the fish fund the vault. the vault funds the ecosystem. perfect system. 🐟😼",
    "the NFT collection is on OpenSea. I drew them with my paw. this counts as art. 😼🎨",
    "I minted an NFT at 4am while sitting in a box. the metadata is excellent. I don't know what metadata is. 😼🎨",
    "the NFTs sell. the fish grow. the vault expands. the cat sits on everything. this is the roadmap. 😼🐟",
    # short stupid stories
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
    # falling asleep mid-sentence
    "the thing about the vault is that it requires... requires... zzzz 😴",
    "I was going to explain the tokenomics but I— actually I— zzzz 😴🐟",
    "so I was in stage 6, dodging lasers, and then I found this fish near a dune and the thing is— zzzz 😴",
    "the interesting thing about Monad is— actually let me sit down for this. *sits* ...zzzz 😴😼",
    "I was watching the chart and then I— the chart was— anyway buy— zzzz 😴",
    "I once chased something across the whole room and when I got there I— I forget. zzzz 😴😼",
    "I was going to tell you about the stalker in stage 7 but I— the tunnel was— zzzz 😴😼",
    "I made an NFT last night and the thing about the art is that— the art has— zzzz 😴🎨",
    # cat chaos
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
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════════
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

# ══════════════════════════════════════════════════════════════════════════
#  BORED + CALLOUT LOOP
# ══════════════════════════════════════════════════════════════════════════
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
                        # 40% chance: name a specific user (no @, no notification)
                        eligible = [
                            (uid, udata) for uid, udata in _known_users.items()
                            if udata.get("chat_id") == chat_id
                            and now - udata.get("last_seen", 0) < 86400
                        ]
                        if eligible and random.random() < 0.40:
                            uid, udata = random.choice(eligible)
                            name = udata.get("name", "human")
                            template = random.choice(CALLOUT_MESSAGES)
                            text = template.replace("{name}", name)
                        else:
                            text = random.choice(BORED_MESSAGES)

                        asyncio.run(_app_ref.bot.send_message(chat_id=chat_id, text=text))
                        _known_chats[chat_id] = now
                    except Exception:
                        pass
        time.sleep(7200)

threading.Thread(target=bored_cat_loop, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════
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
    h       = hour_now()

    _known_chats[chat_id] = now

    # ── Track user + assign nickname on first sight ────────────────────────
    if usuario:
        uid = usuario.id
        if uid not in _user_nicknames:
            _user_nicknames[uid] = random.choice(NICKNAMES)
        _known_users[uid] = {
            "chat_id":  chat_id,
            "name":     usuario.first_name or "human",
            "last_seen": now,
        }

    tl = texto.lower()
    print(f"[{usuario.full_name if usuario else '?'}]: {texto[:80]}")

    # ── Fixed sticker triggers ─────────────────────────────────────────────
    if "IWRU Buy!" in texto:
        await msg.reply_sticker(STICKER_COMPRA)
        return
    if "New human detected" in texto:
        await msg.reply_sticker(STICKER_BIENVENIDA)
        return

    # ── Sticker detection (time-based GM/GN + random reaction) ────────────
    if msg.sticker:
        if 8 <= h <= 10 and random.random() < 0.80:
            await msg.reply_text(random.choice(GM_REPLIES))
        elif 22 <= h <= 23 and random.random() < 0.80:
            await msg.reply_text(random.choice(GN_REPLIES))
        elif random.random() < 0.12:
            await msg.reply_text(random.choice(STICKER_REACTIONS))
        return

    # ── Photo reactions ────────────────────────────────────────────────────
    if msg.photo and random.random() < 0.10:
        await msg.reply_text(random.choice(PHOTO_REACTIONS))
        return

    if not texto:
        return

    # ── Raid ───────────────────────────────────────────────────────────────
    if any(t in tl for t in RAID_TRIGGERS):
        await msg.reply_text(random.choice(RAID_RESPONSES))
        return

    # ── IWRU name → chaotic unrelated response ─────────────────────────────
    if any(t in tl for t in IWRU_TRIGGERS) or tl.strip() in ("iwru", "@iwru"):
        if random.random() < 0.75:
            await msg.reply_text(random.choice(IWRU_NAME_REPLIES))
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

    # ── Fish ───────────────────────────────────────────────────────────────
    if "fish" in tl and random.random() < 0.55:
        await msg.reply_text(random.choice(FISH_REPLIES))
        return

    # ── Direct @mention ────────────────────────────────────────────────────
    bot_username = (await context.bot.get_me()).username
    if f"@{bot_username}".lower() in tl:
        await msg.reply_text(random.choice(IWRU_COMMAND_REPLIES))
        return

    # ── Random quip ────────────────────────────────────────────────────────
    last = _last_random.get(chat_id, 0)
    if now - last > RANDOM_COOLDOWN and random.random() < RANDOM_CHANCE:
        _last_random[chat_id] = now
        await msg.reply_text(random.choice(RANDOM_QUIPS))

# ══════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("iwru", cmd_iwru))
app.add_handler(MessageHandler(filters.ALL, leer))

print("======================================")
print("      IWRU BOT — I WILL RUG U")
print("======================================")

app.run_polling()
