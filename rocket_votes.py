"""rocket_votes.py -- the cat asks for 🚀 votes on the IWRU Dexscreener page (2026-10-23).

Fernando: cat phrases, at least 8 a day, spread over the whole day to cover several time zones. A Dexscreener rocket is
one vote per person PER DAY (Fernando 2026-10-24), so the lines also remind people to come back tomorrow.

- One post every 3 hours (SLOTS_MADRID, 8 a day: 01:30, 04:30 ... 22:30 Madrid, SLOT_OFFSET_MIN),
  never less than an hour apart (DST nights). The last posted slot ("YYYY-MM-DD:slot", Madrid date) is persisted in
  events.db by the caller, so a Render restart never posts the same slot twice.
- Each post carries the REAL rocket count on its VOTE button, read from the endpoint the Dexscreener page itself uses
  (ROCKETS_API). If the read fails, the button says just VOTE -- never a made-up number.
- Rewards (RocketLedger, below; DRAFT, not wired into bot.py yet): Dexscreener only says HOW MANY rockets, never WHO,
  so a vote is paid only under rules that make the counter itself the proof:
    1. ONE voter at a time (an exclusive turn, LOCK_S): every rocket that lands during your turn is yours, so two people
       voting at once can't take each other's rocket. Whoever taps Vote meanwhile waits in a queue and is called next.
    2. RECORDS ONLY: a vote is paid only when the count beats the highest count ever seen (the record, which never
       goes down). Removing a rocket and putting it back never beats the record, so it never pays.
    3. One code per Telegram account per day (DAY = Europe/Madrid calendar day, like the monthly statistics; a
       Dexscreener rocket is once per person per day), enforced by the database (UNIQUE(tg_id, day)); only group members.
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
# Fernando 2026-10-24: the vote reminder every 3 hours, so people keep seeing it: always 90 min into each window
# (01:30, 04:30, ... 22:30 Madrid), exactly 3h apart (2h/4h only on the two DST nights). Was a random minute 30-150.
SLOT_OFFSET_MIN = (90, 90)                             # minutes into the window

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
    "voted 🚀 today? legend. same time tomorrow. 😼",
    "one 🚀 per human per day. today's is still waiting. ⏰",
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
    "new rule: every human taps 🚀 once a day. rule by: the cat.",
    "rocket count low, cat mood low. coincidence? no. 😾",
    "one 🚀 from you, one purr from me. fair trade.",
]

SHOUTOUT_LINES = [
    "{name} launched rocket #{n} 🚀 +20 Essence. the rest of you: thin ice. 🐾",
    "🚀 #{n} by {name}! +20 Essence. the cat approves. slightly. 😼",
    "{name} pressed the 🚀 (#{n}). +20 Essence. the cat purrs. briefly.",
    "rocket #{n} 🚀 from {name}. +20 Essence. who's next? 👀",
    "{name} did the thing. 🚀 #{n}, +20 Essence. hero of the day. 🐾",
    "liftoff! 🚀 #{n} by {name}, +20 Essence. the cat salutes with one paw.",
]

# 2026-10-24 (Fernando): the group post is the image + one short cat line + two buttons: "🚀 VOTE · <real count>" and,
# under it, "📊 Statistics" (this month's voters, counted from day 1, 00:00 Madrid). The count lives on the button.
STATS_BUTTON = "📊 Statistics"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]
ALERT_MAX = 200  # Telegram's limit for the pop-up that answers a button tap


def vote_button_text(count):
    """The VOTE button with the real 🚀 total; without a read, just VOTE (never a made-up number)."""
    return "🚀 VOTE" if count is None else f"🚀 VOTE · {count} rockets"


def day_key(ts):
    """The Madrid calendar day of a unix time, "YYYY-MM-DD": one paid vote per person per day."""
    return datetime.fromtimestamp(ts, tz=MADRID_TZ).strftime("%Y-%m-%d")


def month_start(now):
    """Unix time of day 1, 00:00 Madrid, of the month `now` falls in: the monthly statistics restart there."""
    local = datetime.fromtimestamp(now, tz=MADRID_TZ)
    return datetime(local.year, local.month, 1, tzinfo=MADRID_TZ).timestamp()


def stats_text(rows, now):
    """The 📊 Statistics pop-up: this month's paid votes per person ([(name, votes)], most first), cut to ALERT_MAX."""
    month = MONTHS[datetime.fromtimestamp(now, tz=MADRID_TZ).month - 1]
    if not rows:
        return f"📊 {month}: no rockets yet. be the first: tap 🚀 VOTE. 🐾"
    total = sum(n for _, n in rows)
    lines = [f"📊 {month}: {total} rocket{'s' if total != 1 else ''}"]
    for i, (name, n) in enumerate(rows):
        line = f"{i + 1}. {name} 🚀{n}"
        left = len(rows) - i - 1                      # rows still to show after this one
        tail = f"\n+{left} more" if left else ""
        if len("\n".join(lines + [line]) + tail) > ALERT_MAX:
            lines.append(f"+{left + 1} more")
            break
        lines.append(line)
    return "\n".join(lines)


def fetch_rocket_count(timeout=8):
    """The real 🚀 total for the IWRU pair, or None if Dexscreener can't be read right now."""
    req = urllib.request.Request(ROCKETS_API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = json.loads(resp.read().decode("utf-8"))["reactions"]["rocket"]["total"]
        return int(total) if isinstance(total, (int, float)) and total >= 0 else None
    except Exception:
        return None


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
# 2026-10-23 (Fernando: "igual hay que reducir la ventana del turno"): 2 min to vote (open the page, tap, come back:
# 30-60 s), the bot spots the rocket by itself during the turn (rocket_bot.turn_watch_job) and ends it at once; "I voted"
# only stretches the watch for a slow Dexscreener counter. Was 4 min + 2 min.
LOCK_S = 2 * 60        # one voter's exclusive turn
CLAIM_WAIT_S = 90      # how long the bot keeps re-reading after "I voted"
MAX_TURN_S = LOCK_S + CLAIM_WAIT_S  # "I voted" can stretch a turn up to here, never further (no holding the turn forever)
QUEUE_TTL_S = 30 * 60  # a place in line older than this is dropped (the person is gone; they can tap VOTE again)
JUMP_FREEZE = 10       # more than this many new rockets between two reads -> freeze payouts
ESSENCE_PER_VOTE = 20


class RocketLedger:
    """Who may vote now, which rocket pays whom, and the record of paid votes. Every call gets the clock (`now`, unix
    seconds) and the count just read from Dexscreener (None = could not read), so it is testable without the network.
    Persistence: the sqlite connection passed in (events.db in the bot)."""

    def __init__(self, conn, new_code):
        self.c = conn
        self.new_code = new_code  # tg_id -> one-time Essence code
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS rv_state (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS rv_paid (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, name TEXT, ts REAL,
                                                day TEXT, level INTEGER, code TEXT, UNIQUE(tg_id, day));
            CREATE TABLE IF NOT EXISTS rv_queue (tg_id INTEGER PRIMARY KEY, name TEXT, ts REAL, alive REAL, seq INTEGER);
        """)
        # 2026-10-24: rv_votes (one row per person, forever) -> rv_paid (one per person per day); old rows are kept
        if self.c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rv_votes'").fetchone():
            for tg, name, ts, level, code in self.c.execute("SELECT tg_id, name, ts, level, code FROM rv_votes").fetchall():
                self.c.execute("INSERT OR IGNORE INTO rv_paid(tg_id,name,ts,day,level,code) VALUES(?,?,?,?,?,?)",
                               (tg, name, ts, day_key(ts), level, code))
            self.c.execute("DROP TABLE rv_votes")
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

    def paid_code(self, tg_id, now):
        """Today's code (Madrid day) if tg_id was already paid today, else None."""
        row = self.c.execute("SELECT code FROM rv_paid WHERE tg_id=? AND day=?", (tg_id, day_key(now))).fetchone()
        return row[0] if row else None

    def _just_paid(self, tg_id, now):
        """The code of a payment in this turn or today: a claim right after midnight must not mistake a turn the watch
        paid at 23:59 for an expired one (that voter would be told "your turn ran out" just after getting the code)."""
        row = self.c.execute("SELECT code FROM rv_paid WHERE tg_id=? AND (day=? OR ts >= ?) ORDER BY ts DESC LIMIT 1",
                             (tg_id, day_key(now), now - MAX_TURN_S)).fetchone()
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
        ('paid', code)    already paid today (today's code is re-sent; tomorrow they can vote again)
        ('not_member',)   not in the IWRU group
        ('no_data',)      Dexscreener can't be read now
        ('frozen',)       payouts frozen
        ('wait', pos)     someone else's turn (or someone queued first): queued at position pos
        ('turn', until)   their turn, until `until`"""
        code = self.paid_code(tg_id, now)
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
        """A read during tg_id's turn (the bot's own watch, or "I voted"). Returns:
        ('paid', code)     a new record landed during their turn: one code, the turn ends -- THIS call paid it
        ('already', code)  they were paid before (by an earlier call): send nothing new
        ('not_yet',)    no new record yet (keep re-reading)
        ('no_turn',)    not their turn (expired, or never started)
        ('no_data',) / ('frozen',)"""
        code = self._just_paid(tg_id, now)
        if code:
            return ("already", code)
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
        self.c.execute("INSERT INTO rv_paid(tg_id,name,ts,day,level,code) VALUES(?,?,?,?,?,?)",
                       (tg_id, h[1], now, day_key(now), count, code))
        self._set("record", count)  # one code per turn, even if two rockets landed in it
        self._set("lock_tg", None)
        self._set("lock_until", None)
        self.c.commit()
        return ("paid", code)

    def month_stats(self, now):
        """This month's paid votes per Telegram account (from day 1, 00:00 Madrid): [(name, votes)], most first, then
        whoever got there first (at most one a day each)."""
        return [(r[0], r[1]) for r in self.c.execute(
            "SELECT (SELECT name FROM rv_paid p2 WHERE p2.tg_id=p.tg_id ORDER BY ts DESC LIMIT 1), COUNT(*), MIN(ts) "
            "FROM rv_paid p WHERE ts >= ? AND ts <= ? GROUP BY tg_id "
            "ORDER BY COUNT(*) DESC, MIN(ts)", (month_start(now), now))]

    def ranking(self):
        """Real voters, first to last: (name, unix time of the vote, Essence earned)."""
        return [(r[0], r[1], ESSENCE_PER_VOTE) for r in self.c.execute("SELECT name, ts FROM rv_paid ORDER BY ts")]
