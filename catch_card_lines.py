"""catch_card_lines.py -- what the cat says when someone catches a PARADOX card in the Catch Event (2026-10-13).

Fernando: the Catch Event no longer pays a mouse, a fish, a mystery box or a crown, it pays PARADOX cards, so the old
item reactions no longer fit. When a player catches a card the cat answers with one punchy, very-cat line about THAT
card -- never disrespectful, the joke is always the cat's own greed, vanity or chaos (same rule as CATCH_CONGRATS and
the phrase policy: funny/chaotic only, never promotional, never at the winner's expense).

- CARD_CATCH_LINES: one line per card in the event pool. The event pays one Common card, chosen by the server from all
  115 Commons (iwillrugu _paradox-packs-core.mjs buildPools -> 'common'), so every Common has its own line.
- GENERIC_CARD_CATCH_LINES: used when the card is unknown (server lookup failed) or a new card is added before it gets
  its own line.
{name} = the winner's plain first name (never an @-mention), {card} = the card's name.
"""
import random

CARD_CATCH_LINES = {
    # ── Abyss ──────────────────────────────────────────────────────────────
    "A-002": "{name} caught the Grave Keeper Initiate. it's learning where the dead are buried. I'd like it to learn where my fish are buried next. 🪦😼",
    "A-003": "{name} got the Shadow Beast Whelp. it stands between you and danger. I stand between you and the snack drawer. same energy. 🌑😼",
    "A-004": "{name} caught the Eclipse Priest Acolyte. eternal dusk is my favourite time of day. it's when I plan crimes. 🌘😼",
    "A-005": "{name} grabbed the Rift Serpent Hatchling. it grows sharper every time something falls. I grow hungrier every time something falls. off the table. 🐍😼",
    "A-006": "{name} caught the Void Walker Acolyte. it reminds enemies they're smaller than they think. I do that to dogs. 🌀😼",
    "A-007": "{name} got the Eclipse Priest. he brings things back from the graveyard. I would like him to bring back the fish I dropped behind the sofa. 🕯️😼",
    "A-008": "{name} caught the Grave Keeper. it has buried enough to know how most fights end. I have buried enough toys to know where they all are. 🪦😼",
    "A-009": "{name} got the Shadow Beast. it has guarded worse things than an attacker. I have guarded worse things than a laptop keyboard. barely. 🌑😼",
    "A-010": "{name} caught the Rift Serpent. it makes sure the defender never shows up. I do the same with your alarm clock. 🐍😼",
    "A-011": "{name} got the Void Walker. it corrects what things believe they are. it told me I'm not a lion. we are no longer speaking. 🌀😾",
    "A-012": "{name} caught the Night Oracle. she sees the top three cards. I see the top three fish. we are both prophets. 🔮😼",
    "A-013": "{name} got the Drowned Noble. it makes opponents discard. I make cups discard. off the table. one at a time. 👑😼",
    "A-014": "{name} caught the Void Walker Corruptor. its correction never fades. neither does my grudge against the vacuum cleaner. 🌀😾",
    "A-026": "{name} got Wither. -2/-2 to a creature. that's exactly what my face does when the bowl is empty. 🥀😼",
    "A-027": "{name} caught Grave Pact. it returns small things from the graveyard. I return small things to your bed. you're welcome. 🪦😼",
    "A-028": "{name} got Toll of the Drowned. the toll grows the longer you wait. like my breakfast demands. pay up. 💀😼",
    "A-033": "{name} caught Grim Reprisal. prevents destruction and makes the attacker regret it. this is also my policy on bath time. ⚔️😾",
    "A-034": "{name} got Void Interference. it counters spells unless they pay up. I counter keyboards unless you pay in treats. ⛓️😼",
    "A-035": "{name} caught Echo of Loss. the attacker remembers everything it lost. I remember every fish I didn't get. every single one. 🌫️😿",
    "A-037": "{name} got Sealed Tomb. it doesn't open often. neither does the treat cupboard, which is why I'm staring at it. 🪦😼",
    "A-038": "{name} caught Famine's Toll. the Drowned Empire remembers famine. I remember famine too. it was 20 minutes ago. 🍽️😿",
    "A-039": "{name} got Grave Ambush. it prevents damage at the last line. I am also an excellent last line. mostly lying on it. 🗡️😼",
    "A-040": "{name} caught the Void-Forged Blade. +2 attack. my claws are +2 attack too and I didn't need a forge, just a sofa. 🗡️😼",
    "A-041": "{name} got the Cursed Shroud. +3 defense and Guard. I achieve the same by hiding under a blanket. 🧥😼",
    "A-043": "{name} caught the Ashen Necropolis. it simply makes room. I simply make room too. by knocking things off the shelf. 🏚️😼",
    "A-044": "{name} got the Black Cathedral. nothing returns for free there. same rule in my kitchen. tax: one fish. ⛪😼",
    # ── Machine ────────────────────────────────────────────────────────────
    "M-002": "{name} caught Automaton Unit, unit 1 of an order that won't stop. I am unit 1 of an order that won't stop meowing. 🤖😼",
    "M-003": "{name} got the Clockwork Sentinel. it never tires of standing watch. I never tire of lying down while calling it standing watch. ⚙️😼",
    "M-004": "{name} caught the Arc Reactor Drone. it makes sure no power goes unaccounted. I make sure no fish goes unaccounted. 🔋😼",
    "M-005": "{name} got the Survey Drone. built to map the Foundries. I have mapped every warm spot in this house. no machine needed. 🛰️😼",
    "M-006": "{name} caught the Automaton Line Worker. it doesn't need instructions twice. I don't need instructions at all. I simply ignore them. 🤖😼",
    "M-007": "{name} got the Engineer Apprentice. she hasn't built anything important yet. neither have I. I've destroyed plenty though. 🔧😼",
    "M-008": "{name} caught the Clockwork Repair Drone. it asks what still needs to hold. my answer is always: the fish. hold the fish. ⚙️😼",
    "M-009": "{name} got the Sentinel Watcher. flagged the same anomaly for 600 cycles. I've flagged the empty bowl for 600 seconds. same dedication. 👁️😼",
    "M-010": "{name} caught the Steel Colossus Frame. enters exhausted. relatable. I also enter every room exhausted. 🦾😴",
    "M-011": "{name} got the Sentinel Disruptor. it silences enemies. I have tried this on the vacuum cleaner. no results yet. 🔇😼",
    "M-012": "{name} caught the Engineer Technician. she goes and finds the right part. I go and find the warmest laptop. 🔧😼",
    "M-013": "{name} got the Arc Reactor Core. every field is logged. every nap of mine is also logged. by me. in my head. 🔋😼",
    "M-014": "{name} caught the Automaton Disabler. switches enemies off for a moment. I switch your work off by sitting on the keyboard. 🤖😼",
    "M-026": "{name} got Efficient Calibration. downtime is a design flaw, it says. I disagree. downtime is my entire design. 😴😼",
    "M-027": "{name} caught Systematic Repair. +3 defense, permanently. my defense against baths is already permanent. 🛠️😾",
    "M-028": "{name} got Resource Reallocation. nothing is spent without a system. my system: spend everything on fish. 📊😼",
    "M-033": "{name} caught Emergency Backup. nothing fails permanently on the first attempt. that's also how I approach jumping onto the fridge. 🔁😼",
    "M-034": "{name} got Priority Override. this one always wins the priority queue. so do I. at 6am. at the food bowl. 🥇😼",
    "M-035": "{name} caught Automated Response. it only needed permission to respond. I never ask permission. that's the difference between us. ⚡😼",
    "M-036": "{name} got Failsafe Array. every failure is logged and quietly improved upon. I fell off the shelf once. nobody saw. nobody. 📋😼",
    "M-037": "{name} caught Prepared Defense. it was already calculated. so was my jump. the landing, less so. 🧮😼",
    "M-038": "{name} got Efficiency Audit. the Foundries do not forgive waste. neither do I. who left fish in the bowl. explain yourself. 📑😾",
    "M-039": "{name} caught Containment Field. scale is not a threat if it was accounted for. I account for the dog. the dog is not a threat. 🧲😼",
    "M-040": "{name} got the Precision Blade. every millimeter was justified. every scratch on your sofa was also justified. 🗡️😼",
    "M-041": "{name} caught Reinforced Plating. +3 defense and Guard. I'd wear it too if it came in fish scent. 🛡️😼",
    "M-043": "{name} got the Iron Metropolis. an industrial megacity. I'd still sit in the cardboard box it came in. 🏙️😼",
    "M-044": "{name} caught the Assembly District. builds gear faster and cheaper. can it build me a bigger food bowl. asking for me. 🏭😼",
    # ── Neutral ────────────────────────────────────────────────────────────
    "N-001": "{name} got Crossroads Barter. everyone trades something at the Crossroads. I offer one hairball for one fish. fair deal. 🔄😼",
    "N-002": "{name} caught the Wandering Scavenger. belongs to no domain, holds ground in all of them. I also belong to no one and own every chair. 🎒😼",
    "N-003": "{name} got Sundering Blow. destroys something small. I do this to glasses on tables. professionally. 💥😼",
    "N-004": "{name} caught the Traveler's Pack. +1/+1. every domain sells one. none agree what goes inside. I vote fish. 🎒🐟",
    "N-005": "{name} got the Forgotten Crossroads. no faction claims it. I have claimed it. it's mine now. sorry. 🗺️😼",
    "N-006": "{name} caught Dismantle. nothing is above being undone. especially your neatly folded laundry. 🔨😼",
    # ── Ocean ──────────────────────────────────────────────────────────────
    "O-002": "{name} caught the Reef Scout, the one who watches before the wall. I am also the one who watches. mostly the aquarium. 🐠😼",
    "O-003": "{name} got the Reef Guardian. the tide won't take this line. I won't take this line either. too much water. 🌊😼",
    "O-004": "{name} caught the Coral Acolyte. she hums old songs and the water listens. I hum at 4am and nobody listens. rude. 🪸😼",
    "O-005": "{name} got the Sea Spirit Wisp. it only remembers being alive. I only remember being fed. once. long ago. 👻😿",
    "O-006": "{name} caught the Tide Hunter. strikes from the shallows and is gone before the wave breaks. that's how I steal fish from the counter. 🌊😼",
    "O-007": "{name} got the Pearl Knight. a pearl is a wound the oyster refused to let fester. I prefer the oyster. with lemon. 🦪😼",
    "O-008": "{name} caught the Coral Sage. she chooses what to remember. I chose to forget I already ate. 🪸😼",
    "O-009": "{name} got the Ancient Turtle. it was old when the Tide Courts were young. I'm told I'm also old. I'm told this rudely. 🐢😼",
    "O-010": "{name} caught the Tide Hunter Strider. can't attack without the right weather. I can't attack without the right nap first. 🌊😴",
    "O-011": "{name} got the Jelly Sage. it freezes enemies just by drifting by. I freeze humans just by staring at the treat bag. 🪼😼",
    "O-012": "{name} caught the Reef Captain. never lost her line. I've never lost a staring contest. same discipline. 🌊😼",
    "O-013": "{name} got the Current Strider. sends creatures back to your hand. I send your hand back to the treat bag. 🌊😼",
    "O-014": "{name} caught the Reef Sentinel. it decides who reaches the line. I decide who reaches the sofa. you don't. 🌊😼",
    "O-015": "{name} got Leviathan's Herald. before the deep sends its champion, it sends word. before I send my champion, I send a meow at 5am. 🐋😼",
    "O-019": "{name} caught the Elder Jelly. where the current turns cold, the young follow. where the fridge door opens, I follow. 🪼😼",
    "O-026": "{name} got Tide's Grasp. the shallows reclaim what stands in them. I reclaim whatever is on the table. by gravity. 🌊😼",
    "O-027": "{name} caught Coral Ward. for one breath, anything can hold the line. I can hold the line for 14 hours. horizontally. 🪸😴",
    "O-028": "{name} got Ebb and Flow. draw two, discard one. that's how I treat two treats. kidding. I eat both. 🌊😼",
    "O-033": "{name} caught Tidal Rescue. the sea doesn't let its own sink. neither do I. but please keep me away from the sea. 🛟😾",
    "O-034": "{name} got Counter Current. every current can be answered by a stronger one. every 'no' can be answered by a louder meow. 🌊😼",
    "O-035": "{name} caught Guarded Response. by the time the strike lands, the line has changed. by the time you reach for me, I've moved. 🛡️😼",
    "O-036": "{name} got Backwash. arrive against the current and you arrive tired. I arrive tired regardless. 🌊😴",
    "O-037": "{name} caught Undertow Snare. the reef looks empty only to those who can't read it. the bowl looks empty because IT IS. 🕸️😾",
    "O-039": "{name} got Coral Deadfall. the reef decides for you. I also decide for you. it's usually 'feed the cat'. 🪸😼",
    "O-040": "{name} caught the Coral Trident. forged from a Leviathan's spine-plate. I'd use it exclusively to spear sardines. 🔱🐟",
    "O-041": "{name} got the Pearl Aegis. a pearl worn openly tells every attacker where to strike. I wear my belly openly. it is a trap. 🐚😼",
    "O-043": "{name} caught the Crown of Tides. the jewel of the depths. technically it's my crown. you may borrow it. 👑😼",
    "O-044": "{name} got the Coral Archive. it never runs out of memory. I never run out of reasons to be fed again. 📚😼",
    # ── Wildlands ──────────────────────────────────────────────────────────
    "W-002": "{name} caught the Forest Stalker. Rush. it doesn't wait to learn the forest. I also don't wait. 3am zoomies included. 🌲😼",
    "W-003": "{name} got the Spirit Wolf Pup. alone it's nothing, beside the pack it's fearless. I'm fearless alone. it's called being a cat. 🐺😼",
    "W-004": "{name} caught the Thorn Shaman Initiate. she asks the forest to lean forward. I ask the plant to fall over. it always agrees. 🌿😼",
    "W-005": "{name} got the Stonehorn Calf. doesn't move because something else wants it to. that's my entire personality. 🦬😼",
    "W-006": "{name} caught the Forest Stalker Ambusher. by the time you see the leaves move, it's decided. same with my ankle attacks. 🍃😼",
    "W-007": "{name} got the Tusked Charger. Pierce. it rarely stops at the second thing in its way either. me at the kitchen counter. 🐗😼",
    "W-008": "{name} caught the Spirit Wolf Hunter. a pack hunts to win. a cat hunts to bring you something horrible at 6am. 🐺😼",
    "W-009": "{name} got the Bramble Skirmisher. the first strike is a warning, this one is not. I also give exactly one warning tail flick. 🌿😾",
    "W-010": "{name} caught the Stonehorn Bull. the tribes just ask a bull to stand still. you can ask me to stand still. I won't. 🦬😼",
    "W-011": "{name} got the Treant Sapling. plant it today, it's taller by morning. like my demands. 🌱😼",
    "W-012": "{name} caught the Bloom Keeper. she reminds things that they can grow. she reminded me I can grow. sideways. 🌸😼",
    "W-013": "{name} got the Sky Raptor Scout. it doesn't need the pack, only the sky. I don't need the pack, only the top of the wardrobe. 🦅😼",
    "W-014": "{name} caught the Thorn Shaman Elder. the forest remembers who fought for it. I remember who fed me late. I remember forever. 🌿😾",
    "W-015": "{name} got the Great Beast Yearling. cannot block. has never once considered standing still. that's me in the hallway at midnight. 🐾😼",
    "W-026": "{name} caught Feral Surge. for one breath, it forgets it was ever tired. that's the exact effect of the treat bag sound. 🐾⚡",
    "W-027": "{name} got Savage Charge. wait for the right moment and the moment is gone. so I never wait. especially for dinner. 🏃😼",
    "W-028": "{name} caught Root Tribes' Call. they don't draft strangers, they call for kin. I'm kin. call me. with fish. 📣😼",
    "W-033": "{name} got Thicket Guard. it doesn't stop the blow, it refuses to let it matter. that's how I handle the word 'no'. 🌿😼",
    "W-034": "{name} caught Pack Instinct. it remembers it is not alone. I remember I'm not alone too. there's a fish somewhere. 🐺😼",
    "W-035": "{name} got Opportunist's Strike. the first blow is a distraction, the second is the point. classic cat strategy for stealing toast. 🍞😼",
    "W-036": "{name} caught Last Stand. it doesn't ask to be spared, only answered. I don't ask to be fed. I demand. there's a difference. 🛡️😾",
    "W-037": "{name} got Hidden Den. sometimes the tribes don't meet an attack with a line. sometimes they hide. under the bed. relatable. 🛏️😼",
    "W-039": "{name} caught Ambush Roots. it never sees the roots until it's stopped moving. you never see me in the dark hallway either. 🌳😼",
    "W-040": "{name} got Bone Claws. +3 attack, carved from something that used to argue. my claws were also carved from arguments. 🦴😼",
    "W-041": "{name} caught Bark Hide. wear the forest's skin and its patience comes with it. I'd take the skin. patience sold separately. 🌳😼",
    "W-042": "{name} got Alpha's Mark. wherever IWRU's mark lands, the pack follows. correct. that's my paw print. follow it to the fish. 🐾👑",
    "W-043": "{name} caught Root Valley. wide enough for a stampede. I've made a stampede alone. at 3am. down the stairs. 🏞️😼",
    "W-044": "{name} got the Emerald Forest. the forest grows to fit newcomers. the bed does not. I take the middle anyway. 🌳😼",
}

GENERIC_CARD_CATCH_LINES = [
    "{name} caught {card}. a fine card. I'd have caught it first if I hadn't been napping on purpose. 🃏😼",
    "{name} got {card}! I inspected it personally. it smells like a winner. and a little like fish. 🃏🐟",
    "congratulations {name}, {card} is yours. I'd like visiting rights on weekends. 🃏😼",
    "{name} caught {card}. I let you have this one. I'm generous like that. very occasionally. 🃏😼",
    "{name} grabbed {card} before I could sit on it. respect. I usually sit on everything. 🃏😼",
    "{card} goes to {name}. the deck grows, the chaos grows, my pride in you grows. slightly. 🃏😼",
    "{name}, {card} chose you. cards are like cats. they choose. you just get to feed them. 🃏😼",
    "well played {name}. {card} is in your hands. keep it away from the edge of the table. I'm just saying. 🃏😼",
    "{name} caught {card}. I've decided this was my plan all along. I'm very good at plans after they happen. 🃏😼",
    "{name} got {card}! put it in a deck. put it in a frame. put a fish next to it, for luck. mine. 🃏🐟",
    "{card} for {name}. a fair catch. a fast catch. not as fast as me, but I was busy. 🃏😼",
    "{name} caught {card}. the cat approves. the cat also wants to know where the fish is. 🃏😼",
    "{name}, you caught {card}. I knocked three cards off the table this morning. you caught one. we both had a productive day. 🃏😼",
    "{card} belongs to {name} now. I'll be watching it. not suspiciously. very suspiciously. 🃏👀",
    "{name} got {card}. go build something chaotic with it. that's the only rule in my house. 🃏😼",
]


def catch_line(def_id, card_name, winner):
    """The cat's line for `winner` catching the card `def_id`/`card_name`. Falls back to a generic card line, and
    to a fully generic one if even the card name is unknown."""
    winner = winner or "human"
    line = CARD_CATCH_LINES.get(def_id or "")
    if line is None:
        line = random.choice(GENERIC_CARD_CATCH_LINES)
    return line.replace("{name}", winner).replace("{card}", card_name or "a PARADOX card")
