"""rocket_votes.py -- the cat asks for 🚀 votes on the $IWRU Dexscreener page (2026-10-23).

Fernando: cat phrases, at least 8 a day, spread over the whole day to cover several time zones. A Dexscreener rocket is
one vote per person, forever, so the lines also talk to people who already voted (bring a human friend).

- One post in each 3h window of the Europe/Madrid day (SLOTS_MADRID, 8 a day), at a random minute 30-150 into the window,
  so two posts are always at least an hour apart. The last posted slot ("YYYY-MM-DD:slot", Madrid date) is persisted in
  events.db by the caller, so a Render restart never posts the same slot twice.
- Each post carries the REAL rocket count, read from the endpoint the Dexscreener page itself uses (ROCKETS_API), and how
  many were added since the previous post. If the read fails, the post goes out without the count -- never a made-up one.
- Rewards (RocketLedger, below; DRAFT, not wired into bot.py yet): Dexscreener only says HOW MANY rockets, never WHO,
  so a vote is paid only under rules that make the counter itself the proof:
    1. ONE voter at a time (an exclusive turn, LOCK_S): every rocket that lands during your turn is yours, so two people
       voting at once can't take each other's rocket. Whoever taps Vote meanwhile waits in a queue and is called next.
    2. RECORDS ONLY: a vote is paid only when the count beats the highest count ever seen (the record, which never
       goes down). Removing a rocket and putting it back never beats the record, so it never pays.
    3. One code per Telegram account, forever (a Dexscreener rocket is once per person), and only group members.
    4. No data = no pay: if Dexscreener can't be read nothing is paid; a jump of more than JUMP_FREEZE rockets between
       two reads freezes payouts until the Owner looks (bot farm or bad data).
    5. Dexscreener lag: after "I voted" the bot keeps re-reading for up to CLAIM_WAIT_S and answers by itself.
- Phrase policy as every other pool: funny/chaotic cat, never a price promise, "buy" or "moon".
"""
import json
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")
PAIR = "monad:0x0caaF82370766e76E150A5610c16A306E254b8A7"
DEXSCREENER_LINK = "https://dexscreener.com/monad/0x0caaf82370766e76e150a5610c16a306e254b8a7"
ROCKETS_API = "https://io.dexscreener.com/hype/reactions/dexPair/" + PAIR

SLOTS_MADRID = [(h, h + 3) for h in range(0, 24, 3)]  # (start hour, end hour), 8 windows
SLOT_OFFSET_MIN = (30, 150)                            # minutes into the window

ROCKET_LINES = [
    "I tried to press 🚀 with my nose. still unpressed. help.",
    "one 🚀 = one happy cat. do the maths.",
    "my paws are too fluffy for the 🚀 button. yours aren't.",
    "the 🚀 button is lonely. go keep it company.",
    "fewer 🚀 than naps I took today. unacceptable. 😾",
    "I want a fish, a box and your 🚀. in that order. no, 🚀 first.",
    "press 🚀 or I sit on your keyboard. your choice. 😼",
    "rocket inspection done. verdict: not enough 🚀.",
    "I trained a pigeon to press 🚀. then I ate it. your turn. 🐦",
    "2 seconds. one 🚀. I spend longer staring at walls.",
    "the 💩 button is not for my litter report. press 🚀. 🚀",
    "every 🚀 lifts my tail. it's at half mast. 🐈",
    "already voted 🚀? legend. now drag a friend here. 😼",
    "one 🚀 per human, ever. don't waste a once-in-a-lifetime button.",
    "I meowed at the 🚀 for an hour. it didn't press itself. 🙀",
    "rockets are just fast cats without fur. give us one. 🚀",
    "one tap, one 🚀, one smug cat. 😼",
    "voted 🚀? purr twice. didn't? 4am face-sit incoming.",
    "I saw you scroll past. so did the 🚀. 👀",
    "cats can't vote. I'm lending you mine. tap 🚀.",
    "to-do: nap, knock cup, get 🚀, nap. step 3 is you.",
    "press 🚀 and I'll pretend to like you. 5 whole minutes. 😼",
    "the only button I won't knock off the table: 🚀.",
    "wherever you are, whatever time: 🚀, please. 🌍",
    "dreamt the 🚀 count doubled. make it real. 💭",
    "breaking: cat finds 🚀 button, can't use it, blames you. 📰",
    "a 🚀 is like a box: free, and the cat loves it. 📦",
    "I can smell who hasn't voted 🚀 yet. 👃",
    "ran out of toes counting 🚀. that's bad. need more toes. 🐾",
    "tiny 🚀 for tiny cats. many tiny cats waiting.",
    "🔥 is nice. 🚀 is nicer. 💩 is my enemy.",
    "you have one 🚀 to give. it's burning your pocket. 🔥",
    "I knocked your phone off the table so you'd vote 🚀. 📱",
    "rockets go up, cats land on feet. tap 🚀. 🐾",
    "staring at the 🚀 without blinking. it's getting weird. 👁️",
    "a 🚀 costs zero fish. I checked every fish. 🐟",
    "voted? slow blink from me. that's cat for love. 😌",
    "going to sleep. want more 🚀 when I wake. 😴",
    "democracy, but with whiskers: tap 🚀. 🗳️",
    "hi. cat again. still about the 🚀. always the 🚀.",
    "I hid a fish behind the 🚀. there's no fish. press it anyway.",
    "the cat never sleeps. (lie.) tap 🚀. 🌙",
    "🚀 are like treats: never too many. we have too few.",
    "I don't get charts. I get buttons. 🚀 is a good button.",
    "this is a cat-alyst. tap 🚀. I'll see myself out. 🚪",
    "🚀 🐟 😼 — you can only give me the first one. do it.",
    "every unpressed 🚀 = one glass on the floor. it's wet here. 💧",
    "too busy being a loaf to vote. you do it. 🍞",
    "the button you keep ignoring? 🚀. the cat knows. 👁️",
    "put the 🚀 in my food bowl so you'd notice. nobody notices my bowl. 🥣",
    "tap 🚀, get a free cat stare. no extra charge. 👀",
    "9 lives, 1 vote, 0 excuses. 🚀",
    "if the 🚀 count goes up I'll stop meowing at 5am. (I won't.)",
    "😼 → 🚀 → 😸. you're the arrow.",
    "my tail is a 🚀 antenna. it detects you haven't voted.",
    "lunch break? coffee? bathroom? perfect time for 🚀.",
    "I'd vote 🚀 myself but I bit the phone. long story.",
    "new rule: every human taps 🚀 once. rule by: the cat.",
    "rocket count low, cat mood low. coincidence? no. 😾",
    "one 🚀 from you, one purr from me. fair trade.",
]

SHOUTOUT_LINES = [
    "{name} launched rocket #{n} 🚀 +150 Essence. the rest of you: thin ice. 🐾",
    "🚀 #{n} by {name}! +150 Essence. the cat approves. slightly. 😼",
    "{name} pressed the 🚀 (#{n}). +150 Essence. the cat purrs. briefly.",
    "rocket #{n} 🚀 from {name}. +150 Essence. who's next? 👀",
    "{name} did the thing. 🚀 #{n}, +150 Essence. hero of the day. 🐾",
    "liftoff! 🚀 #{n} by {name}, +150 Essence. the cat salutes with one paw.",
]

COUNT_LINE = "🚀 rockets on Dexscreener right now: {n}"
COUNT_UP_LINE = "🚀 rockets on Dexscreener right now: {n} (+{d} since my last nap 🐾)"
HOW_LINE = "👉 tap VOTE first: the cat gives you your turn, then you vote and get 150 Essence (one vote per person, forever)"
BUTTON_TEXT = "🚀 Vote on Dexscreener"


def fetch_rocket_count(timeout=8):
    """The real 🚀 total for the $IWRU pair, or None if Dexscreener can't be read right now."""
    req = urllib.request.Request(ROCKETS_API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = json.loads(resp.read().decode("utf-8"))["reactions"]["rocket"]["total"]
        return int(total) if isinstance(total, (int, float)) and total >= 0 else None
    except Exception:
        return None


def compose(line, count, last_count):
    """Post text: the cat line, the real count (with the rise since the last post, if any) and how to vote."""
    parts = [line]
    if count is not None:
        if isinstance(last_count, int) and count > last_count:
            parts.append(COUNT_UP_LINE.format(n=count, d=count - last_count))
        else:
            parts.append(COUNT_LINE.format(n=count))
    parts.append(HOW_LINE)
    return "\n\n".join(parts)


def slot_key(dt):
    """'YYYY-MM-DD:slot' of the Madrid window that contains `dt`."""
    local = dt.astimezone(MADRID_TZ)
    return f"{local.date().isoformat()}:{local.hour // 3}"


def _wall(day, hour):
    """That Madrid wall-clock hour on `day`, as an aware datetime (a nonexistent hour on the spring DST night resolves
    forward, which is what we want: the window starts when the clock shows it)."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=MADRID_TZ)


def next_post(last_slot_key, now=None):
    """(seconds until the next post, its slot key): the first window, from now on, that has not been posted yet, at a
    random minute SLOT_OFFSET_MIN into it -- or right away (1-5 min) if we are already past that point of a window
    that was not posted yet (e.g. after a restart)."""
    now = (now or datetime.now(timezone.utc)).astimezone(MADRID_TZ)
    day = now.date()
    for _ in range(3):
        for i, (start, end) in enumerate(SLOTS_MADRID):
            key = f"{day.isoformat()}:{i}"
            if last_slot_key is not None and key <= last_slot_key:
                continue
            # real (UTC) times: on the DST change night a window is 2h or 4h long, and wall-clock arithmetic there
            # put two posts 48 min apart (found by the many-seeds test). Post 30 min after the window opens at the
            # earliest and 30 min before it closes at the latest, so two posts are always >= 1h apart.
            base = _wall(day, start).astimezone(timezone.utc)
            window_end = _wall(day + timedelta(days=1), 0).astimezone(timezone.utc) if end == 24 else _wall(day, end).astimezone(timezone.utc)
            latest = min(SLOT_OFFSET_MIN[1], int((window_end - base).total_seconds() // 60) - 30)
            target = base + timedelta(minutes=random.randint(SLOT_OFFSET_MIN[0], max(SLOT_OFFSET_MIN[0], latest)))
            if window_end <= now:
                continue
            if target <= now:
                target = now + timedelta(minutes=random.randint(1, 5))
            # UTC subtraction: same-ZoneInfo datetimes subtract as naive wall-clock times (DST ignored)
            return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds(), key
        day += timedelta(days=1)
    raise RuntimeError("no rocket slot found")


# ── rewards ledger (draft) ──────────────────────────────────────────────────────────────────────────────────────────
LOCK_S = 4 * 60        # one voter's exclusive turn
CLAIM_WAIT_S = 2 * 60  # how long the bot keeps re-reading after "I voted"
MAX_TURN_S = LOCK_S + CLAIM_WAIT_S  # "I voted" can stretch a turn up to here, never further (no holding the turn forever)
QUEUE_TTL_S = 30 * 60  # a place in line older than this is dropped (the person is gone; they can tap VOTE again)
JUMP_FREEZE = 10       # more than this many new rockets between two reads -> freeze payouts
ESSENCE_PER_VOTE = 150


class RocketLedger:
    """Who may vote now, which rocket pays whom, and the record of paid votes. Every call gets the clock (`now`, unix
    seconds) and the count just read from Dexscreener (None = could not read), so it is testable without the network.
    Persistence: the sqlite connection passed in (events.db in the bot)."""

    def __init__(self, conn, new_code):
        self.c = conn
        self.new_code = new_code  # tg_id -> one-time Essence code
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS rv_state (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS rv_votes (tg_id INTEGER PRIMARY KEY, name TEXT, ts REAL, level INTEGER, code TEXT);
            CREATE TABLE IF NOT EXISTS rv_queue (tg_id INTEGER PRIMARY KEY, name TEXT, ts REAL, alive REAL, seq INTEGER);
        """)
        self.c.commit()

    def _get(self, k, cast=None):
        row = self.c.execute("SELECT v FROM rv_state WHERE k=?", (k,)).fetchone()
        if row is None or row[0] is None:
            return None
        return cast(row[0]) if cast else row[0]

    def _set(self, k, v):
        self.c.execute("INSERT INTO rv_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       (k, None if v is None else str(v)))

    def record(self):
        return self._get("record", int)

    def frozen(self):
        return self._get("frozen") == "1"

    def unfreeze(self):
        self._set("frozen", "0")
        self.c.commit()

    def holder(self, now):
        """(tg_id, name) of the voter whose turn it is, or None (an expired turn counts as free)."""
        tg = self._get("lock_tg", int)
        if tg is None or now >= (self._get("lock_until", float) or 0):
            return None
        return tg, self._get("lock_name")

    def paid_code(self, tg_id):
        row = self.c.execute("SELECT code FROM rv_votes WHERE tg_id=?", (tg_id,)).fetchone()
        return row[0] if row else None

    def observe(self, now, count):
        """Every read of the counter goes through here. Outside a turn, a higher count just becomes the record (a vote
        made outside the bot: real, paid to nobody). Returns 'frozen' when this read froze payouts."""
        if count is None:
            return None
        last = self._get("last_count", int)
        self._set("last_count", count)
        # measured from the record too, not only from the last read: Dexscreener answers 0 (not an error) for a pair it
        # doesn't know, so a glitch read of 0 followed by the real count must not look like a jump
        base = max(x for x in (last, self.record()) if x is not None) if (last is not None or self.record() is not None) else None
        if base is not None and count - base > JUMP_FREEZE and not self.frozen():
            # the jump pays nobody, even after the Owner unfreezes: it becomes the record and the open turn ends
            self._set("frozen", "1")
            self._set("record", max(self.record() or count, count))
            self._set("lock_tg", None)
            self._set("lock_until", None)
            self.c.commit()
            return "frozen"
        rec = self.record()
        if rec is None or (self.holder(now) is None and count > rec):
            self._set("record", count)
        self.c.commit()
        return None

    def queue_position(self, tg_id):
        """1-based place in line, or None if not waiting."""
        rows = [r[0] for r in self.c.execute("SELECT tg_id FROM rv_queue ORDER BY seq")]
        return rows.index(tg_id) + 1 if tg_id in rows else None

    def _prune_queue(self, now):
        self.c.execute("DELETE FROM rv_queue WHERE alive < ?", (now - QUEUE_TTL_S,))

    def release(self, tg_id):
        """Ends tg_id's turn now (the bot could not reach them), so the next one in line is called."""
        if self._get("lock_tg", int) == tg_id:
            self._set("lock_tg", None)
            self._set("lock_until", None)
            self.c.commit()

    def _enqueue(self, tg_id, name, now):
        self._prune_queue(now)
        # tapping VOTE again keeps the place in line (ts) and only says "still here" (alive)
        # seq = arrival order (two taps in the same second: the first one goes first -- found by the oracle test)
        self.c.execute("INSERT INTO rv_queue(tg_id,name,ts,alive,seq) "
                       "VALUES(?,?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM rv_queue)) "
                       "ON CONFLICT(tg_id) DO UPDATE SET alive=excluded.alive", (tg_id, name, now, now))
        self.c.commit()
        return self.queue_position(tg_id)

    def next_in_queue(self, now):
        """The first one waiting, when the turn is free: (tg_id, name) for the bot to call, or None."""
        if self.holder(now) is not None:
            return None
        self._prune_queue(now)
        self.c.commit()
        row = self.c.execute("SELECT tg_id, name FROM rv_queue ORDER BY seq LIMIT 1").fetchone()
        return (row[0], row[1]) if row else None

    def start(self, tg_id, name, now, count, is_member=True):
        """The voter tapped Vote. Returns one of:
        ('paid', code)    already got their code (it is re-sent)
        ('not_member',)   not in the IWRU group
        ('no_data',)      Dexscreener can't be read now
        ('frozen',)       payouts frozen
        ('wait', pos)     someone else's turn (or someone queued first): queued at position pos
        ('turn', until)   their turn, until `until`"""
        code = self.paid_code(tg_id)
        if code:
            return ("paid", code)
        if not is_member:
            return ("not_member",)
        if count is None:
            return ("no_data",)
        if self.observe(now, count) == "frozen" or self.frozen():
            return ("frozen",)
        h = self.holder(now)
        if h is not None and h[0] != tg_id:
            return ("wait", self._enqueue(tg_id, name, now))
        first = self.next_in_queue(now)
        if h is None and first is not None and first[0] != tg_id:
            return ("wait", self._enqueue(tg_id, name, now))
        if h is None:
            # a new turn: the record absorbs anything that landed before it (it was not this voter's)
            self._set("record", max(self.record() or count, count))
            self._set("lock_tg", tg_id)
            self._set("lock_name", name)
            self._set("lock_until", now + LOCK_S)
            self._set("lock_start", now)
            self.c.execute("DELETE FROM rv_queue WHERE tg_id=?", (tg_id,))
            self.c.commit()
        return ("turn", self._get("lock_until", float))

    def extend_for_claim(self, tg_id, now):
        """"I voted" tapped near the end of the turn: the turn lasts the whole CLAIM_WAIT_S re-read."""
        h = self.holder(now)
        if h is not None and h[0] == tg_id:
            cap = (self._get("lock_start", float) or now) + MAX_TURN_S
            self._set("lock_until", min(cap, max(self._get("lock_until", float), now + CLAIM_WAIT_S)))
            self.c.commit()

    def claim(self, tg_id, now, count):
        """The voter tapped "I voted" (the bot re-reads every few seconds for CLAIM_WAIT_S). Returns:
        ('paid', code)  a new record landed during their turn: one code, the turn ends
        ('not_yet',)    no new record yet (keep re-reading)
        ('no_turn',)    not their turn (expired, or never started)
        ('no_data',) / ('frozen',)"""
        code = self.paid_code(tg_id)
        if code:
            return ("paid", code)
        h = self.holder(now)
        if h is None or h[0] != tg_id:
            return ("no_turn",)
        if count is None:
            return ("no_data",)
        if self.observe(now, count) == "frozen" or self.frozen():
            return ("frozen",)
        if count <= self.record():
            return ("not_yet",)
        code = self.new_code(tg_id)
        self.c.execute("INSERT INTO rv_votes(tg_id,name,ts,level,code) VALUES(?,?,?,?,?)",
                       (tg_id, h[1], now, count, code))
        self._set("record", count)  # one code per turn, even if two rockets landed in it
        self._set("lock_tg", None)
        self._set("lock_until", None)
        self.c.commit()
        return ("paid", code)

    def ranking(self):
        """Real voters, first to last: (name, unix time of the vote, Essence earned)."""
        return [(r[0], r[1], ESSENCE_PER_VOTE) for r in self.c.execute("SELECT name, ts FROM rv_votes ORDER BY ts")]
