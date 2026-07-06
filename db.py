import os
import sqlite3
from datetime import datetime, timezone

import events_config as cfg

DB_PATH = os.environ.get("EVENTS_DB_PATH", "events.db")

# Logged at startup (visible in Render/any platform's logs) specifically so a
# misconfiguration -- EVENTS_DB_PATH left relative, or pointing outside a
# mounted persistent disk -- is obvious from the resolved absolute path,
# rather than silently writing to an ephemeral filesystem that gets wiped on
# every deploy/restart.
print(f"[db] using database file: {os.path.abspath(DB_PATH)}", flush=True)

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.row_factory = sqlite3.Row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display(username, name) -> str:
    if username:
        return f"@{username}"
    return name or "someone"


def init_db() -> None:
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            first_seen TEXT,
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            reward INTEGER NOT NULL,
            chat_id INTEGER,
            message_id INTEGER,
            has_image INTEGER NOT NULL DEFAULT 0,
            sticker_message_id INTEGER,
            owner_message_id INTEGER,
            display_text TEXT,
            status TEXT NOT NULL DEFAULT 'unclaimed',
            created_at TEXT NOT NULL,
            winner_id INTEGER,
            winner_username TEXT,
            winner_name TEXT,
            claimed_at TEXT,
            wallet TEXT,
            wallet_received_at TEXT,
            paid_at TEXT
        );
        """
    )
    _conn.commit()
    _migrate_schema()
    if get_config("events_enabled") is None:
        set_config("events_enabled", "0")


# (column_name, DDL fragment) for every column added to `events` after its
# original CREATE TABLE -- add a new entry here for any future column, rather
# than hand-rolling another repeated if-block in _migrate_schema.
_EVENTS_TABLE_MIGRATIONS = [
    ("has_image", "ALTER TABLE events ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0"),
    ("sticker_message_id", "ALTER TABLE events ADD COLUMN sticker_message_id INTEGER"),
]


def _migrate_schema() -> None:
    """CREATE TABLE IF NOT EXISTS is a no-op against a table that already
    exists -- it does NOT add newly-introduced columns to an existing events
    table from a prior run. Without this, any bot instance with an events.db
    predating a schema change would crash the instant that column is read,
    since sqlite3.Row raises on a missing key exactly like a real bug would,
    not a graceful None."""
    existing_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(events)")}
    for column_name, ddl in _EVENTS_TABLE_MIGRATIONS:
        if column_name in existing_columns:
            continue
        try:
            _conn.execute(ddl)
            _conn.commit()
        except sqlite3.OperationalError as e:
            # Two processes briefly overlapping against the same fresh
            # events.db (a deploy-time overlap, or two dev instances
            # launched close together before this column has ever existed)
            # could both pass the "column missing" check above before either
            # commits -- the loser's ALTER then raises "duplicate column
            # name" here. That means the column now exists (the winner added
            # it), which is exactly the end state this function is trying to
            # reach, so treat it as success rather than crashing startup.
            if "duplicate column" not in str(e).lower():
                raise


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG (key/value)
# ══════════════════════════════════════════════════════════════════════════
def get_config(key: str, default=None):
    row = _conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    _conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    _conn.commit()


def events_enabled() -> bool:
    return get_config("events_enabled") == "1"


# ══════════════════════════════════════════════════════════════════════════
#  USERS
# ══════════════════════════════════════════════════════════════════════════
def upsert_user(telegram_id: int, username, first_name) -> None:
    now = _now()
    _conn.execute(
        """
        INSERT INTO users (telegram_id, username, first_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
        """,
        (telegram_id, username, first_name, now, now),
    )
    _conn.commit()


# ══════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════
def create_event(
    event_key: str, token: str, reward: int, chat_id: int, message_id: int, display_text: str, has_image: bool,
    sticker_message_id: int = None,
) -> int:
    """One atomic INSERT covering everything known at creation time (event
    already posted to Telegram by the caller, so chat_id/message_id/
    display_text are all available up front). A single INSERT either fully
    succeeds or fully fails -- unlike separate follow-up UPDATEs, there is no
    window where a row exists with some columns populated and others NULL.

    message_id/chat_id always refer to the plain TEXT message carrying the
    Catch/Inspect keyboard -- the one this whole row tracks and edits going
    forward. sticker_message_id (nullable) is a separate, purely decorative
    message (the event's artwork, sent as a sticker so Telegram renders its
    transparency correctly) with no caption and no keyboard of its own;
    has_image just records whether one was sent, so a bump knows whether
    there's a sticker to also delete/repost alongside the text message."""
    cur = _conn.execute(
        "INSERT INTO events (event_key, token, reward, chat_id, message_id, display_text, has_image, sticker_message_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unclaimed', ?)",
        (event_key, token, reward, chat_id, message_id, display_text, int(has_image), sticker_message_id, _now()),
    )
    _conn.commit()
    return cur.lastrowid


def set_owner_message_id(event_id: int, message_id: int, expected_status: str = None) -> bool:
    """Atomic compare-and-set when expected_status is given -- on_catch's
    call site needs this: a concurrent JobQueue-triggered auto-expiry of
    THIS exact row (no minimum-age floor by design) can win a race during
    on_catch's own Owner-notification await, and this guard stops that from
    then pointing owner_message_id at a message describing a claim that's no
    longer valid. on_private_message's call site doesn't need it -- by the
    time it runs, the row is already 'ready_to_pay', which auto-expiry never
    touches, and manual Owner actions can't interleave with it (both are
    regular, serialized updates under this bot's default concurrency)."""
    if expected_status is not None:
        cur = _conn.execute(
            "UPDATE events SET owner_message_id = ? WHERE id = ? AND status = ?",
            (message_id, event_id, expected_status),
        )
    else:
        cur = _conn.execute(
            "UPDATE events SET owner_message_id = ? WHERE id = ?",
            (message_id, event_id),
        )
    _conn.commit()
    return cur.rowcount > 0


def rebump_event(
    event_id: int, chat_id: int, message_id: int, has_image: bool, *, expected_status: str,
    sticker_message_id: int = None,
) -> bool:
    """Repoints an existing event at a freshly-reposted message (the old one
    was deleted and a new copy sent at the bottom of the chat, so an
    unclaimed treasure doesn't stay buried under new chat activity forever).
    Same event/token/reward/history -- only WHERE it lives in the chat
    changes.

    Atomic compare-and-set against expected_status (mirrors cancel_event's
    guard): if the event was claimed in the moment between deciding to bump
    it and this write actually running, don't blindly relocate a now-claimed
    event out from under its own winner -- return False and let the caller
    skip the bump entirely."""
    cur = _conn.execute(
        "UPDATE events SET chat_id = ?, message_id = ?, has_image = ?, sticker_message_id = ? WHERE id = ? AND status = ?",
        (chat_id, message_id, int(has_image), sticker_message_id, event_id, expected_status),
    )
    _conn.commit()
    return cur.rowcount > 0


def set_display_text(event_id: int, text: str) -> None:
    """Persists exactly what's currently shown on the group announcement, so a
    later '⬅️ Back' press can restore it verbatim without recomputing/drifting."""
    _conn.execute(
        "UPDATE events SET display_text = ? WHERE id = ?",
        (text, event_id),
    )
    _conn.commit()


def get_event_by_token(token: str):
    return _conn.execute("SELECT * FROM events WHERE token = ?", (token,)).fetchone()


def get_event_by_message(chat_id: int, message_id: int):
    return _conn.execute(
        "SELECT * FROM events WHERE chat_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id, message_id),
    ).fetchone()


def get_active_event():
    """Most recent event that isn't fully closed yet (status not in ('paid',
    'cancelled')). Only for display purposes (Stats/Owner Status) -- NOT for
    routing a specific winner's wallet DM, since a newer event can outrank an
    older still-pending one here. Use get_pending_wallet_event() for that."""
    return _conn.execute(
        "SELECT * FROM events WHERE status NOT IN ('paid', 'cancelled') ORDER BY id DESC LIMIT 1"
    ).fetchone()


def get_pending_wallet_event(winner_id: int):
    """The event this specific user won that still needs their attention --
    either awaiting their first wallet submission ('claimed') or awaiting
    Owner payout after one was already submitted ('ready_to_pay', so a
    correction can still be accepted before payment goes out). Scoped by
    winner_id so it can't be shadowed by a later Generate Event call, and used
    both to route DMs and to block a user from catching a second event while
    an earlier claim is still unresolved."""
    return _conn.execute(
        "SELECT * FROM events WHERE winner_id = ? AND status IN ('claimed', 'ready_to_pay') "
        "ORDER BY id DESC LIMIT 1",
        (winner_id,),
    ).fetchone()


def get_stale_claimed_events():
    """Every event still awaiting its winner's FIRST wallet submission
    (status='claimed') -- used to auto-expire them the moment a new event is
    about to begin, since the game's random timing rules out a fixed timer.
    Deliberately excludes 'ready_to_pay': once a wallet has been submitted,
    the reward never expires on its own -- only the Owner can resolve it
    (pay or manually cancel), no matter how many new events start meanwhile."""
    return _conn.execute("SELECT * FROM events WHERE status = 'claimed' ORDER BY id ASC").fetchall()


def get_stale_unclaimed_events():
    """Every event nobody has ever caught at all (status='unclaimed') -- used
    alongside get_stale_claimed_events() so a treasure nobody found doesn't
    sit forever, silently hidden the moment a second event posts on top of it
    (get_active_event() only ever returns the newest non-terminal row)."""
    return _conn.execute("SELECT * FROM events WHERE status = 'unclaimed' ORDER BY id ASC").fetchall()


def mark_claimed(event_id: int, winner_id: int, winner_username, winner_name: str) -> bool:
    """Atomic compare-and-set against 'unclaimed' -- this is the single most
    contested write in the whole game (first-catch-wins). Currently safe
    without this guard too, since on_catch has zero await points between
    reading the row's status and calling this -- but matches the same
    defense-in-depth pattern already applied to cancel_event/set_wallet/
    rebump_event/mark_paid, so a future change adding any await in between
    (an async permission check, a swapped-in async DB driver) can't silently
    let two simultaneous Catch presses both succeed and double-award the
    same treasure."""
    cur = _conn.execute(
        "UPDATE events SET status = 'claimed', winner_id = ?, winner_username = ?, "
        "winner_name = ?, claimed_at = ? WHERE id = ? AND status = 'unclaimed'",
        (winner_id, winner_username, winner_name, _now(), event_id),
    )
    _conn.commit()
    return cur.rowcount > 0


def set_wallet(event_id: int, wallet: str) -> bool:
    """Atomic compare-and-set: only writes if the row is STILL in
    ('claimed', 'ready_to_pay') at the moment this runs (covers both the
    first submission and a correction). Defense-in-depth, matching
    cancel_event/rebump_event's guard pattern -- on the current call sites
    there's no await between the read and this write, so it isn't reachable
    today, but it closes the door on a future refactor (e.g. adding a
    lookup/await in between) silently reopening a resurrection race where a
    wallet DM overwrites a claim that a concurrent cancel/expiry just closed."""
    cur = _conn.execute(
        "UPDATE events SET status = 'ready_to_pay', wallet = ?, wallet_received_at = ? "
        "WHERE id = ? AND status IN ('claimed', 'ready_to_pay')",
        (wallet, _now(), event_id),
    )
    _conn.commit()
    return cur.rowcount > 0


def mark_paid(event_id: int, expected_status: str = None) -> bool:
    """Atomic compare-and-set when expected_status is given -- matches
    cancel_event/set_wallet/rebump_event's guard pattern. Without this, a
    fast double-tap on "Mark as Paid" (or Telegram redelivering the same
    callback, which does happen) could let two invocations both read
    status=='ready_to_pay' before either commits, then both independently DM
    the winner and post a duplicate public payout announcement."""
    if expected_status is not None:
        cur = _conn.execute(
            "UPDATE events SET status = 'paid', paid_at = ? WHERE id = ? AND status = ?",
            (_now(), event_id, expected_status),
        )
    else:
        cur = _conn.execute(
            "UPDATE events SET status = 'paid', paid_at = ? WHERE id = ?",
            (_now(), event_id),
        )
    _conn.commit()
    return cur.rowcount > 0


def cancel_event(event_id: int, expected_status: str = None) -> bool:
    """Releases a stuck claim (winner unreachable/unresponsive) so the game
    isn't permanently softlocked by one abandoned prize -- the Owner's only
    other option before this existed was editing SQLite by hand. Doesn't
    affect payout totals (total_distributed/stats only ever sum
    status='paid'), and history isn't deleted, just closed under a distinct
    status.

    expected_status, when given, makes this an atomic compare-and-set: only
    cancels if the row's CURRENT status still matches (returns False,
    changing nothing, otherwise). This closes a real race in batch auto-expiry
    (events.py's _expire_stale_events): PTB's JobQueue jobs run as independent
    asyncio tasks, not serialized with regular update processing, so a
    winner's wallet DM (a separate update, handled by on_private_message) can
    genuinely complete -- flipping a row to 'ready_to_pay' -- in the gap
    between two awaited Telegram calls inside the same expiry batch loop.
    Without this guard, cancelling based on a stale pre-fetched snapshot could
    silently destroy a wallet submitted moments earlier."""
    if expected_status is not None:
        cur = _conn.execute(
            "UPDATE events SET status = 'cancelled' WHERE id = ? AND status = ?",
            (event_id, expected_status),
        )
    else:
        cur = _conn.execute("UPDATE events SET status = 'cancelled' WHERE id = ?", (event_id,))
    _conn.commit()
    return cur.rowcount > 0


# ══════════════════════════════════════════════════════════════════════════
#  READ HELPERS — Hall of Fame / Stats / My Rewards
#  (all derived from `events`/`users`, no separate mutable stats table, so
#  numbers can never drift out of sync with the underlying history)
# ══════════════════════════════════════════════════════════════════════════
def stats_summary() -> dict:
    events_generated = _conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    # Cancelled claims are excluded -- they were released back by the Owner
    # (winner unreachable/unresponsive), so they shouldn't count as a
    # standing "claimed" event any more than a caught-but-voided catch should.
    events_claimed = _conn.execute(
        "SELECT COUNT(*) c FROM events WHERE winner_id IS NOT NULL AND status != 'cancelled'"
    ).fetchone()["c"]
    total_rewards_distributed = _conn.execute(
        "SELECT COALESCE(SUM(reward), 0) s FROM events WHERE status = 'paid'"
    ).fetchone()["s"]

    last = _conn.execute(
        "SELECT winner_username, winner_name FROM events "
        "WHERE winner_id IS NOT NULL AND status != 'cancelled' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_winner = _display(last["winner_username"], last["winner_name"]) if last else "None yet"

    common_row = _conn.execute(
        "SELECT event_key, COUNT(*) c FROM events WHERE winner_id IS NOT NULL AND status != 'cancelled' "
        "GROUP BY event_key ORDER BY c DESC LIMIT 1"
    ).fetchone()
    # .get(..., {}) fallbacks below: a historical event_key may no longer
    # exist in the live EVENTS config (e.g. an event type was renamed or
    # retired), and old rows referencing it must still render, not crash.
    most_common_treasure = (
        cfg.EVENTS.get(common_row["event_key"], {}).get("name", common_row["event_key"])
        if common_row
        else "None yet"
    )

    active = get_active_event()
    if active is None:
        current_status = "No active event"
    else:
        info = cfg.EVENTS.get(active["event_key"], {})
        name = info.get("name", active["event_key"])
        emoji = info.get("emoji", "")
        status_word = {
            "unclaimed": "unclaimed",
            "claimed": "pending wallet",
            "ready_to_pay": "pending payment",
        }.get(active["status"], active["status"])
        current_status = f"{emoji} {name} — {status_word}".strip()

    return {
        "events_generated": events_generated,
        "events_claimed": events_claimed,
        "current_status": current_status,
        "total_rewards_distributed": total_rewards_distributed,
        "last_winner": last_winner,
        "most_common_treasure": most_common_treasure,
    }


def top_hunters(limit: int = 5):
    # ORDER BY c DESC, winner_id ASC -- the second key is just for a stable,
    # reproducible tiebreak; without it, SQLite's order among tied counts is
    # unspecified and could visibly reshuffle between renders as unrelated
    # rows are inserted. Cancelled claims excluded -- a released claim
    # shouldn't credit the public leaderboard.
    #
    # winner_username/winner_name are deliberately NOT selected directly
    # alongside GROUP BY winner_id -- a bare (non-aggregated) column in a
    # GROUP BY query is SQLite's documented "arbitrary row" case, so a user
    # who changed their @username between two wins could display either the
    # old or new one, unpredictably. The subquery pins down each winner's
    # MOST RECENT row (MAX(id)) and the outer join reads the display columns
    # from exactly that row, so the result is deterministic.
    rows = _conn.execute(
        "SELECT e.winner_username, e.winner_name, t.c FROM events e "
        "JOIN ("
        "  SELECT winner_id, COUNT(*) c, MAX(id) latest_id FROM events "
        "  WHERE winner_id IS NOT NULL AND status != 'cancelled' GROUP BY winner_id"
        ") t ON t.latest_id = e.id "
        "ORDER BY t.c DESC, e.winner_id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(_display(r["winner_username"], r["winner_name"]), r["c"]) for r in rows]


def most_of(event_key: str, limit: int = 3):
    # Same deterministic-latest-row approach as top_hunters, see its comment.
    rows = _conn.execute(
        "SELECT e.winner_username, e.winner_name, t.c FROM events e "
        "JOIN ("
        "  SELECT winner_id, COUNT(*) c, MAX(id) latest_id FROM events "
        "  WHERE winner_id IS NOT NULL AND status != 'cancelled' AND event_key = ? GROUP BY winner_id"
        ") t ON t.latest_id = e.id "
        "ORDER BY t.c DESC, e.winner_id ASC LIMIT ?",
        (event_key, limit),
    ).fetchall()
    return [(_display(r["winner_username"], r["winner_name"]), r["c"]) for r in rows]


def crown_winners(limit: int = 10):
    rows = _conn.execute(
        "SELECT winner_username, winner_name FROM events "
        "WHERE winner_id IS NOT NULL AND status != 'cancelled' AND event_key = 'crown' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_display(r["winner_username"], r["winner_name"]) for r in rows]


def total_distributed() -> int:
    return _conn.execute(
        "SELECT COALESCE(SUM(reward), 0) s FROM events WHERE status = 'paid'"
    ).fetchone()["s"]


def user_rewards_summary(telegram_id: int) -> dict:
    # Cancelled claims excluded from "won"/"treasures_found" -- otherwise a
    # released claim would show up as a permanent phantom "pending" reward
    # that can never resolve, since it will never reach 'paid'.
    won = _conn.execute(
        "SELECT COALESCE(SUM(reward), 0) s FROM events WHERE winner_id = ? AND status != 'cancelled'",
        (telegram_id,),
    ).fetchone()["s"]
    paid = _conn.execute(
        "SELECT COALESCE(SUM(reward), 0) s FROM events WHERE winner_id = ? AND status = 'paid'",
        (telegram_id,),
    ).fetchone()["s"]
    treasures_found = _conn.execute(
        "SELECT COUNT(*) c FROM events WHERE winner_id = ? AND status != 'cancelled'",
        (telegram_id,),
    ).fetchone()["c"]
    return {
        "won": won,
        "paid": paid,
        "pending": won - paid,
        "treasures_found": treasures_found,
    }
