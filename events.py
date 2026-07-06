import random
import uuid
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import events_config as cfg
import db

# ══════════════════════════════════════════════════════════════════════════
#  OWNER CHECK — trivial, no discovery/resolution needed
# ══════════════════════════════════════════════════════════════════════════
def _is_owner(user_id: int) -> bool:
    return user_id == cfg.BOT_OWNER_ID


def _display_name(user) -> str:
    return f"@{user.username}" if user.username else (user.first_name or "someone")


def _display(username, name) -> str:
    return f"@{username}" if username else (name or "someone")


def _event_info(event_key: str) -> dict:
    """Safe lookup that survives a historical event_key no longer existing in
    the live EVENTS config (e.g. an event type was renamed/retired) -- a DB
    row referencing it must still render instead of crashing the handler."""
    return cfg.EVENTS.get(event_key, {"emoji": "", "name": event_key})


_bot_username_cache = None


async def _get_bot_username(context: ContextTypes.DEFAULT_TYPE):
    """Returns None on failure instead of raising -- a transient get_me()
    failure must not abort whatever caller is mid-flow (e.g. on_catch still
    has to update the group message and notify the Owner either way)."""
    global _bot_username_cache
    if _bot_username_cache is None:
        try:
            me = await context.bot.get_me()
            _bot_username_cache = me.username
        except TelegramError as e:
            print(f"[events] get_me() failed: {e}", flush=True)
            return None
    return _bot_username_cache


async def _edit_current(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Edits whatever message this callback is attached to, whether it's a
    plain-text message or a photo with a caption. Uses getattr(..., None)
    instead of direct attribute access because Telegram represents messages
    it can no longer fully access (old enough, or deleted) as an
    InaccessibleMessage stub that lacks .photo entirely -- a plain attribute
    access there would raise AttributeError instead of failing gracefully.
    Catches the whole TelegramError family (not just BadRequest) so a
    transient network error here can never abort a caller mid-flow (e.g.
    on_owner_paid/on_owner_cancel still have further steps to run even if
    one of their edits through here fails)."""
    try:
        if getattr(query.message, "photo", None):
            # Captions cap at 1024 UTF-16 code units (Telegram's own count,
            # not Python's len()) -- most emoji used throughout this file are
            # astral-plane (2 UTF-16 units each but 1 Python codepoint), so a
            # check against 1024/1000 using len() could still let an
            # emoji-heavy render sail past Telegram's real limit. Using a much
            # lower Python-length threshold (500/450) stays safely under 1024
            # UTF-16 units even in the absolute worst case where every single
            # character is an astral-plane emoji.
            caption = text if len(text) <= 500 else text[:450] + "…"
            await query.edit_message_caption(caption=caption, reply_markup=keyboard)
        else:
            await query.edit_message_text(text=text, reply_markup=keyboard)
    except (TelegramError, AttributeError):
        pass


# ══════════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════
_BACK_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:back")]])


def _iwru_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏆 Hall of Fame", callback_data="menu:hof"),
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton("🎒 My Rewards", callback_data="menu:rewards"),
                InlineKeyboardButton("🍀 Today's Luck", callback_data="menu:luck"),
            ],
            [InlineKeyboardButton("🐾 How to Play", callback_data="menu:howto")],
        ]
    )


def _event_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(cfg.CATCH_BUTTON_TEXT, callback_data=f"catch:{token}"),
                InlineKeyboardButton(cfg.INSPECT_BUTTON_TEXT, callback_data=f"inspect:{token}"),
            ],
            [
                InlineKeyboardButton("🏆 Hall of Fame", callback_data="menu:hof"),
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton("🎒 My Rewards", callback_data="menu:rewards"),
                InlineKeyboardButton("🐾 How to Play", callback_data="menu:howto"),
            ],
        ]
    )


def _owner_claim_keyboard(token: str, include_pay: bool) -> InlineKeyboardMarkup:
    """The keyboard attached to the Owner's per-claim message. include_pay is
    False while still 'claimed' (nothing to pay yet) and True once
    'ready_to_pay'. Cancel is always available so a stuck/unresponsive winner
    can never permanently softlock the game."""
    rows = []
    if include_pay:
        rows.append([InlineKeyboardButton("✅ Mark as Paid", callback_data=f"paid:{token}")])
    rows.append([InlineKeyboardButton("❌ Cancel Claim", callback_data=f"cancel:{token}")])
    return InlineKeyboardMarkup(rows)


_OWNER_PANEL_TEXT = "Owner Panel"

_GENERATE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("🐭 Mouse", callback_data="owner:gen:mouse"),
            InlineKeyboardButton("🐟 Purple Fish", callback_data="owner:gen:fish"),
        ],
        [
            InlineKeyboardButton("📦 Mystery Box", callback_data="owner:gen:box"),
            InlineKeyboardButton("👑 Golden Crown", callback_data="owner:gen:crown"),
        ],
        [InlineKeyboardButton("🎲 Random", callback_data="owner:gen:random")],
        [InlineKeyboardButton("⬅️ Back", callback_data="owner:back")],
    ]
)


def _owner_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 Enable Events", callback_data="owner:enable"),
                InlineKeyboardButton("🔴 Disable Events", callback_data="owner:disable"),
            ],
            [InlineKeyboardButton("📊 Status", callback_data="owner:status")],
            [InlineKeyboardButton("🎁 Generate Event", callback_data="owner:generate")],
            [InlineKeyboardButton("❌ Close", callback_data="owner:close")],
        ]
    )


# ══════════════════════════════════════════════════════════════════════════
#  DAILY EVENT
# ══════════════════════════════════════════════════════════════════════════
def _pick_event_id() -> str:
    keys = list(cfg.EVENTS.keys())
    weights = [cfg.EVENTS[k]["weight"] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def _build_unclaimed_text(event_key: str) -> str:
    info = cfg.EVENTS[event_key]
    return (
        f"{info['emoji']} {info['name']}\n"
        f"{info['stars']}\n"
        f"{info['rarity_label']}\n\n"
        f"Reward:\n{info['reward']} $IWRU\n\n"
        f"{info['catch_text']}"
    )


async def _expire_stale_events(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-retires two kinds of stale event right before a new one begins --
    since events fire at random times, "a new treasure just appeared" is the
    natural trigger instead of a fixed timer:

    1. A claim still awaiting its winner's FIRST wallet submission
       (status='claimed').
    2. A treasure nobody ever caught at all (status='unclaimed'). Without
       this, get_active_event() (which only ever returns the newest
       non-terminal row) would silently hide an older still-live, still-
       catchable treasure the moment a second one posts on top of it --
       letting two events be simultaneously live in the same chat with the
       Owner only ever seeing the newer one.

    Applies equally whether the new event comes from the daily scheduler or
    a manual Owner Generate Event, since both funnel through _post_new_event.

    Deliberately does NOT touch 'ready_to_pay' claims (wallet already
    submitted) -- those never expire on their own, no matter how many new
    events start meanwhile; only the Owner's Pay/Cancel buttons resolve them.

    Wrapped by the caller so a failure here can never block a new event from
    posting -- expiring old events is a courtesy cleanup, not a precondition.

    Each row (in both loops) is processed independently (its own try/except)
    so one row's failure -- Telegram error, template mismatch, anything --
    can never skip expiry of the OTHER stale rows in the same batch."""
    for row in db.get_stale_claimed_events():
        try:
            # Atomic compare-and-cancel, not a blind write: PTB's JobQueue
            # jobs run as independent asyncio tasks, not serialized with
            # regular update processing, so a winner's wallet DM can complete
            # (flipping this exact row to 'ready_to_pay' via a concurrently-
            # running on_private_message) in the gap between two awaited
            # Telegram calls elsewhere in this same loop. If that already
            # happened, expected_status='claimed' fails to match and this
            # cancel is a no-op -- skip the row entirely rather than
            # overwriting a just-submitted wallet's status.
            if not db.cancel_event(row["id"], expected_status="claimed"):
                continue

            info = _event_info(row["event_key"])
            winner_display = _display(row["winner_username"], row["winner_name"])

            try:
                await context.bot.send_message(chat_id=row["winner_id"], text=cfg.CLAIM_EXPIRED_WINNER_MSG)
            except TelegramError as e:
                print(f"[events] failed to notify winner of expiry (event {row['id']}): {e}", flush=True)

            if row["chat_id"] is not None and row["message_id"] is not None:
                expired_text = cfg.EXPIRED_GROUP_TEMPLATE.format(
                    winner=winner_display, emoji=info["emoji"], name=info["name"]
                )
                try:
                    if row["has_image"]:
                        await context.bot.edit_message_caption(
                            chat_id=row["chat_id"], message_id=row["message_id"],
                            caption=expired_text, reply_markup=InlineKeyboardMarkup([]),
                        )
                    else:
                        await context.bot.edit_message_text(
                            chat_id=row["chat_id"], message_id=row["message_id"],
                            text=expired_text, reply_markup=InlineKeyboardMarkup([]),
                        )
                    # Keep display_text in sync with what's actually shown now --
                    # otherwise a later Hall of Fame/Stats/How-to-Play shortcut
                    # pressed on this same (now-closed) message, followed by
                    # Back, would restore the stale pre-expiry "caught!" text
                    # instead of this expiry notice. Only updated on a
                    # successful edit -- if the edit above failed, the group
                    # message still shows the OLD text, so display_text must
                    # keep matching that reality, not this one.
                    db.set_display_text(row["id"], expired_text)
                except TelegramError as e:
                    print(f"[events] failed to edit expired group message (event {row['id']}): {e}", flush=True)

            owner_text = cfg.OWNER_EXPIRED_TEMPLATE.format(
                winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"]
            )
            owner_notified = False
            if row["owner_message_id"] is not None:
                try:
                    await context.bot.edit_message_text(
                        chat_id=cfg.BOT_OWNER_ID, message_id=row["owner_message_id"],
                        text=owner_text, reply_markup=InlineKeyboardMarkup([]),
                    )
                    owner_notified = True
                except TelegramError as e:
                    if "message is not modified" in str(e).lower():
                        # Content already matched -- treat as success, not a
                        # failure (mirrors on_private_message's same guard),
                        # so this doesn't fall through to sending a
                        # duplicate standalone Owner DM below.
                        owner_notified = True
                    else:
                        print(f"[events] failed to notify Owner of expiry (event {row['id']}): {e}", flush=True)
            if not owner_notified:
                # Either owner_message_id was never set (the initial claim
                # DM to the Owner failed) or editing it just failed above --
                # either way, without this fallback the Owner could get ZERO
                # signal this claim ever existed, was pending, or expired. A
                # fresh standalone message is the best we can do when there's
                # no earlier message to (successfully) edit.
                try:
                    await context.bot.send_message(chat_id=cfg.BOT_OWNER_ID, text=owner_text)
                except TelegramError as e:
                    print(f"[events] failed to send fallback Owner expiry notice (event {row['id']}): {e}", flush=True)
        except Exception as e:
            print(f"[events] failed to expire stale claim (event {row['id']}): {e}", flush=True)

    for row in db.get_stale_unclaimed_events():
        try:
            # Same atomic compare-and-cancel discipline as above, but against
            # 'unclaimed': closes a separate, previously-open gap where a
            # treasure nobody ever caught would sit forever, letting a new
            # daily/manual event stack a SECOND simultaneously-live treasure
            # on top of it (get_active_event() only ever surfaces the newest
            # one, so the older one would silently vanish from the Owner's
            # view while still catchable by anyone scrolling back to it).
            if not db.cancel_event(row["id"], expected_status="unclaimed"):
                # Claimed (or already bumped-and-claimed) in the interim --
                # no longer stale, leave it alone.
                continue

            # Re-fetch rather than trust the pre-loop snapshot: _bump_event
            # runs from a regular (serialized) update while this runs from
            # _post_new_event, which can itself be triggered by the
            # JobQueue's daily_event_job -- an independent asyncio task that
            # can interleave with a concurrently-running bump. If a bump won
            # the race and relocated this row before this cancel committed,
            # the row's chat_id/message_id here would point at a message
            # that's about to be (or already was) deleted -- re-reading picks
            # up wherever the treasure actually lives right now.
            fresh = db.get_event_by_token(row["token"]) or row
            info = _event_info(fresh["event_key"])
            if fresh["chat_id"] is not None and fresh["message_id"] is not None:
                expired_text = cfg.UNCLAIMED_EXPIRED_TEMPLATE.format(emoji=info["emoji"], name=info["name"])
                try:
                    if fresh["has_image"]:
                        await context.bot.edit_message_caption(
                            chat_id=fresh["chat_id"], message_id=fresh["message_id"],
                            caption=expired_text, reply_markup=InlineKeyboardMarkup([]),
                        )
                    else:
                        await context.bot.edit_message_text(
                            chat_id=fresh["chat_id"], message_id=fresh["message_id"],
                            text=expired_text, reply_markup=InlineKeyboardMarkup([]),
                        )
                    db.set_display_text(fresh["id"], expired_text)
                except TelegramError as e:
                    print(f"[events] failed to edit unclaimed-expiry group message (event {row['id']}): {e}", flush=True)
        except Exception as e:
            print(f"[events] failed to expire stale unclaimed event (event {row['id']}): {e}", flush=True)


async def _send_event_message(context: ContextTypes.DEFAULT_TYPE, event_id: str, text: str, keyboard: InlineKeyboardMarkup):
    """Sends one event announcement to the group -- as a photo if the event's
    image file exists on disk, falling back to plain text otherwise. Shared by
    _post_new_event (brand-new event) and _bump_event (reposting an existing
    unclaimed one), so both stay in sync automatically if this logic ever
    changes."""
    info = cfg.EVENTS[event_id]
    image_path = cfg.ASSETS_DIR / info["image_filename"]
    if image_path.is_file():
        try:
            with open(image_path, "rb") as f:
                return await context.bot.send_photo(
                    chat_id=cfg.EVENTS_CHAT_ID, photo=f, caption=text, reply_markup=keyboard
                )
        except Exception as e:
            print(f"[events] failed to send image {image_path}: {e}", flush=True)
    # Let this raise on failure -- the caller has nothing to clean up if
    # nothing was ever persisted/updated yet.
    return await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=text, reply_markup=keyboard)


async def _post_new_event(context: ContextTypes.DEFAULT_TYPE, event_id=None) -> dict:
    """Unconditionally posts one event -- no events_enabled check here, so the
    Owner panel's 'Generate Event' always works regardless of the daily
    scheduler's on/off state.

    Sends to Telegram FIRST and only persists to SQLite once that succeeds --
    this removes an entire class of "phantom orphaned row" bugs by
    construction (no partially-created event can ever exist, including if the
    process is killed/cancelled mid-send), rather than needing to detect and
    clean up a failed send after the fact."""
    try:
        await _expire_stale_events(context)
    except Exception as e:
        print(f"[events] _expire_stale_events failed: {e}", flush=True)

    if event_id is None:
        event_id = _pick_event_id()
    info = cfg.EVENTS[event_id]
    token = uuid.uuid4().hex[:10]
    text = _build_unclaimed_text(event_id)
    keyboard = _event_keyboard(token)

    msg = await _send_event_message(context, event_id, text, keyboard)

    try:
        # One atomic INSERT (chat_id/message_id/display_text all included) --
        # either the row is fully created or not created at all, never
        # partially populated. has_image is stored (not re-derived later) so
        # a future edit with no live Message object to inspect -- e.g.
        # auto-expiring a stale claim -- knows which edit method to use.
        db.create_event(
            event_id, token, info["reward"], msg.chat_id, msg.message_id, text,
            has_image=bool(getattr(msg, "photo", None)),
        )
        # A brand-new event starts its own fresh bump countdown -- without
        # this, a count left over from a PREVIOUS event (claimed/expired
        # right as it neared the bump threshold) would carry straight into
        # this one, bumping it far sooner than EVENT_BUMP_MESSAGE_THRESHOLD
        # messages of its own actual activity.
        _msgs_since_bump[cfg.EVENTS_CHAT_ID] = 0
    except Exception as e:
        # The message is already live in the group at this point. If we can't
        # persist it, don't leave what looks like a normal, catchable event
        # with working buttons -- get_event_by_token would find no row for it,
        # so every press would silently show "too late" forever with no
        # indication anything is wrong. Best-effort: mark it broken instead.
        print(f"[events] failed to persist newly-posted event: {e}", flush=True)
        try:
            # Same conservative truncation _edit_current uses, for the same
            # reason (Telegram's caption cap is in UTF-16 units, not Python
            # codepoints, and astral-plane emoji count double) -- but the
            # budget here is tighter than _edit_current's, since a fixed
            # suffix is appended AFTER this truncation, unlike there. Leaving
            # ~100 fewer characters of headroom keeps base + suffix safely
            # under the 1024 UTF-16 unit cap even in the worst case where
            # every character (in both base and the suffix) is astral-plane.
            base = text if len(text) <= 400 else text[:350] + "…"
            error_text = f"{base}\n\n⚠️ Registration failed -- contact the Owner."
            if msg.photo:
                await context.bot.edit_message_caption(
                    chat_id=msg.chat_id, message_id=msg.message_id,
                    caption=error_text, reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=msg.chat_id, message_id=msg.message_id,
                    text=error_text, reply_markup=InlineKeyboardMarkup([]),
                )
        except Exception as edit_error:
            print(f"[events] also failed to mark the broken event: {edit_error}", flush=True)
        raise
    return info


# ══════════════════════════════════════════════════════════════════════════
#  BUMP  (repost an unclaimed event so chat activity can't bury it)
# ══════════════════════════════════════════════════════════════════════════
_msgs_since_bump: dict[int, int] = {}


async def on_group_activity(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Called once per real (human, non-command) group message -- bot.py's
    leer() is the only caller. Counts messages per chat and, once enough have
    passed, reposts the currently-unclaimed event (if there is one) at the
    bottom of the chat so it doesn't stay buried under new conversation
    forever. Deliberately message-driven, not time-based: a quiet chat can't
    bury anything, so there's nothing to bump regardless of how much time
    passes."""
    if chat_id != cfg.EVENTS_CHAT_ID:
        return  # only the events chat's treasure can ever need bumping -- no point tracking counts elsewhere
    try:
        count = _msgs_since_bump.get(chat_id, 0) + 1
        if count < cfg.EVENT_BUMP_MESSAGE_THRESHOLD:
            _msgs_since_bump[chat_id] = count
            return
        _msgs_since_bump[chat_id] = 0

        row = db.get_active_event()
        if row is not None and row["status"] == "unclaimed":
            await _bump_event(context, row)
    except Exception as e:
        print(f"[events] on_group_activity error: {e}", flush=True)


async def _bump_event(context: ContextTypes.DEFAULT_TYPE, row) -> None:
    """Deletes the old (buried) event message and reposts a fresh copy at the
    bottom of the chat, repointing the SAME event/token/history at the new
    message -- nothing about the event itself changes, only where it lives in
    the chat."""
    text = _build_unclaimed_text(row["event_key"])
    keyboard = _event_keyboard(row["token"])

    msg = await _send_event_message(context, row["event_key"], text, keyboard)

    # Atomic compare-and-set: if this event was claimed OR expired in the
    # tiny window between the caller deciding to bump it and this write
    # actually running (e.g. a concurrently-running daily_event_job's
    # unclaimed-expiry beating this to the punch), don't repoint an event
    # that's no longer unclaimed out from under its real current state.
    moved = db.rebump_event(
        row["id"], msg.chat_id, msg.message_id, bool(getattr(msg, "photo", None)),
        expected_status="unclaimed",
    )
    if not moved:
        # The fresh copy above was already sent and is now completely
        # untracked (never written into the DB) -- without cleaning it up
        # here, it would sit in the chat forever as a permanently-orphaned
        # duplicate with a live-looking Catch/Inspect keyboard, since nothing
        # else ever learns this message exists. Best-effort: delete it, or
        # failing that, at least strip its keyboard.
        try:
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
        except TelegramError as e:
            print(f"[events] failed to delete orphaned bump copy (event {row['id']}): {e}", flush=True)
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=msg.chat_id, message_id=msg.message_id,
                    reply_markup=InlineKeyboardMarkup([]),
                )
            except TelegramError as e2:
                print(f"[events] also failed to clear orphaned bump copy's keyboard (event {row['id']}): {e2}", flush=True)
        return

    try:
        await context.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
    except TelegramError as e:
        print(f"[events] failed to delete old bumped message (event {row['id']}): {e}", flush=True)
        # Couldn't remove it outright (already deleted by someone else,
        # missing permissions, network blip) -- at minimum, strip its
        # Catch/Inspect keyboard so it doesn't sit there looking clickable
        # forever once the real treasure has moved on. Uses the dedicated
        # reply-markup-only edit endpoint (not edit_message_text/caption) so
        # the message's existing text/photo is left completely untouched --
        # only the keyboard changes. Best-effort; if this also fails there's
        # nothing further to do.
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=row["chat_id"], message_id=row["message_id"],
                reply_markup=InlineKeyboardMarkup([]),
            )
        except TelegramError as e2:
            print(f"[events] also failed to clear old bumped message's keyboard (event {row['id']}): {e2}", flush=True)


def _seconds_until_window(start_hour_utc: int, end_hour_utc: int, *, force_next_day: bool = False) -> float:
    """Seconds until a random moment inside [start_hour_utc, end_hour_utc) today
    (or tomorrow). Self-contained copy of bot.py's helper of the same shape --
    events.py must stay import-safe and never import from bot.py."""
    now = datetime.utcnow()
    window_minutes = (end_hour_utc - start_hour_utc) * 60
    offset_minutes = random.randint(0, window_minutes - 1)
    target = now.replace(hour=start_hour_utc, minute=0, second=0, microsecond=0) + timedelta(minutes=offset_minutes)
    if force_next_day or target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _schedule_daily_event(job_queue, *, force_next_day: bool = False) -> None:
    """Schedules daily_event_job, and -- if the window leaves room for a
    meaningful heads-up -- a companion event_teaser_job some random minutes
    earlier. Both use the SAME computed delay for today's/tomorrow's window,
    so the teaser's lead time is relative to the real scheduled moment
    without either job needing to know the other's schedule.

    The critical daily_event_job scheduling is not wrapped in its own
    try/except here -- if job_queue.run_once itself is fundamentally broken,
    there's nothing left to fall back to -- but the caller (daily_event_job's
    finally block) wraps this whole call anyway. The optional teaser
    scheduling below IS separately guarded so a failure there can never be
    mistaken for -- or cause -- a failure to reschedule the actual event."""
    delay = _seconds_until_window(*cfg.EVENT_WINDOW_UTC, force_next_day=force_next_day)
    job_queue.run_once(daily_event_job, delay)

    try:
        lead = random.uniform(cfg.EVENT_TEASER_LEAD_SECONDS_MIN, cfg.EVENT_TEASER_LEAD_SECONDS_MAX)
        teaser_delay = delay - lead
        if teaser_delay > 60:  # not worth teasing something under a minute away
            job_queue.run_once(event_teaser_job, teaser_delay)
    except Exception as e:
        print(f"[events] failed to schedule event_teaser_job: {e}", flush=True)


async def event_teaser_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Posts one atmospheric hint ahead of the daily event. Pure flavor --
    the real event's exact second is independent of this and was already
    decided when it was scheduled, so this never promises or reveals timing.
    Skipped if events are disabled, or if a treasure is already sitting there
    unclaimed (redundant -- that's already the biggest hint there is).

    Deduped per UTC calendar day (mirrors daily_event_job's own dedupe) --
    without it, a process restart within the window could re-schedule and
    fire a second teaser the same day."""
    try:
        if db.events_enabled():
            today = datetime.utcnow().date().isoformat()
            if db.get_config("last_teaser_date") != today:
                active = db.get_active_event()
                if active is None or active["status"] != "unclaimed":
                    # Flag set BEFORE sending, matching daily_event_job's same
                    # fix -- a restart landing between send and flag-write
                    # would otherwise let a second teaser post the same day.
                    db.set_config("last_teaser_date", today)
                    text = random.choice(cfg.EVENT_TEASER_MESSAGES)
                    await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=text)
    except Exception as e:
        print(f"[events] event_teaser_job error: {e}", flush=True)


async def daily_event_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The only place events_enabled is checked -- disabling only silences the
    automatic daily post, it never touches manual Generate Event.

    Also dedupes against posting more than one automatic event on the same
    UTC calendar day: a process restart within the window (this bot already
    restarts itself on Telegram conflicts) re-schedules a fresh job, which
    would otherwise fire again the same day. Manual Owner-triggered
    Generate Event is untouched by this -- it's tracked separately."""
    # Everything above the reschedule is wrapped so that ANY failure here
    # (including in events_enabled()/get_config() themselves, not just
    # _post_new_event) can never skip rescheduling tomorrow's job -- without
    # this, one transient SQLite hiccup would permanently kill the daily
    # scheduler until the whole process restarts.
    try:
        if db.events_enabled():
            today = datetime.utcnow().date().isoformat()
            if db.get_config("last_auto_event_date") != today:
                # Flag set BEFORE posting, not after: if the process is
                # killed/restarted in the gap between posting and marking the
                # day done, the OLD ordering would let this job fire AGAIN on
                # restart -- and _post_new_event's own _expire_stale_events
                # has no minimum-age floor by design, so it would wrongly
                # auto-expire the still-fresh event just posted (falsely
                # announcing "nobody found it in time") and double-post. The
                # worst case with THIS ordering is the opposite, gentler
                # failure: if posting itself fails right after this write,
                # that day's auto-event is silently skipped (resumes normally
                # tomorrow) rather than corrupting a real, valid event.
                db.set_config("last_auto_event_date", today)
                await _post_new_event(context)
    except Exception as e:
        print(f"[events] daily_event_job error: {e}", flush=True)
    finally:
        try:
            _schedule_daily_event(context.application.job_queue, force_next_day=True)
        except Exception as e:
            print(f"[events] failed to reschedule daily_event_job: {e}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
#  CATCH / INSPECT
# ══════════════════════════════════════════════════════════════════════════
async def on_catch(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    token = query.data.split(":", 1)[1]
    user = query.from_user

    if _is_owner(user.id):
        await query.answer(cfg.OWNER_CANNOT_CLAIM_MSG, show_alert=True)
        return

    row = db.get_event_by_token(token)
    if row is None or row["status"] != "unclaimed":
        await query.answer(random.choice(cfg.LATE_POPUPS), show_alert=True)
        return

    # A user can only have one unresolved claim at a time (covers both
    # 'claimed' -- awaiting their wallet -- and 'ready_to_pay' -- awaiting
    # Owner payout). Without this, a user who catches a second event before
    # their first is fully paid out would silently orphan the first claim
    # forever: wallet routing (get_pending_wallet_event) always resolves to
    # the newest matching row for that user, so the older claim could never
    # be reached again.
    if db.get_pending_wallet_event(user.id) is not None:
        await query.answer(cfg.ALREADY_PENDING_CLAIM_MSG, show_alert=True)
        return

    # Atomic compare-and-set against 'unclaimed' -- defense-in-depth (see
    # db.mark_claimed's docstring): not reachable today since nothing awaits
    # between the status check above and this call, but guards the single
    # most contested write in the game against ever double-awarding the same
    # treasure if that ever changes.
    if not db.mark_claimed(row["id"], user.id, user.username, user.first_name or "human"):
        await query.answer(random.choice(cfg.LATE_POPUPS), show_alert=True)
        return
    db.upsert_user(user.id, user.username, user.first_name)
    await query.answer()

    winner_display = _display_name(user)
    info = _event_info(row["event_key"])
    caught_text = cfg.CAUGHT_TEMPLATE.format(winner=winner_display, name=info["name"], reward=row["reward"])

    dm_ok = True
    try:
        await context.bot.send_message(chat_id=user.id, text=cfg.PENDING_WALLET_MSG)
    except TelegramError:
        # Any failure here (Forbidden, BadRequest, TimedOut, NetworkError, ...)
        # means "assume we couldn't reach them" -- the display/group-message
        # update and Owner notification below must still happen regardless,
        # or the event would be stuck 'claimed' forever with no recovery.
        dm_ok = False

    if not dm_ok:
        bot_username = await _get_bot_username(context)
        if bot_username:
            deep_link = f"https://t.me/{bot_username}?start=claim"
            caught_text += cfg.DEEPLINK_LINE_TEMPLATE.format(deep_link=deep_link)
        else:
            # Even generating our own deep link failed (transient get_me()
            # error) -- the winner still needs SOME path forward instead of a
            # silent dead end with no way to ever submit a wallet.
            caught_text += cfg.DEEPLINK_UNAVAILABLE_LINE

    # Re-check before touching the group message / Owner: db.get_stale_claimed_events
    # has no minimum-age floor by design ("the next event starting" is the
    # trigger, whenever that happens), so a JobQueue-triggered daily_event_job
    # (an independent asyncio task that can interleave with this very await
    # above) could have already auto-expired THIS exact row while we were mid-
    # flight sending the winner's DM. That process already correctly notified
    # everyone -- without this check, we'd blindly overwrite its work back to
    # "caught!" with a live keyboard and send a contradictory second Owner DM.
    fresh_check = db.get_event_by_token(token)
    if fresh_check is None or fresh_check["status"] != "claimed" or fresh_check["winner_id"] != user.id:
        return

    db.set_display_text(row["id"], caught_text)
    # Edit the row's CURRENT canonical location (fresh_check), not
    # query.message (wherever the pressed button physically lived) -- a bump
    # can relocate a live event to a new message between when this button was
    # rendered and when it was pressed (there's an awaited gap in
    # _bump_event between committing the new location and cleaning up the
    # old one). Editing query.message directly in that narrow window would
    # update the soon-to-be-deleted old message instead of the real,
    # current group post, leaving the actual live message stuck showing
    # "unclaimed" with a live-looking keyboard even though it's already won.
    try:
        if fresh_check["has_image"]:
            # Same conservative UTF-16 truncation _edit_current applies --
            # calling context.bot directly here (rather than through
            # _edit_current) means that safety margin has to be reapplied by
            # hand, not inherited for free.
            caption = caught_text if len(caught_text) <= 500 else caught_text[:450] + "…"
            await context.bot.edit_message_caption(
                chat_id=fresh_check["chat_id"], message_id=fresh_check["message_id"],
                caption=caption, reply_markup=_event_keyboard(token),
            )
        else:
            await context.bot.edit_message_text(
                chat_id=fresh_check["chat_id"], message_id=fresh_check["message_id"],
                text=caught_text, reply_markup=_event_keyboard(token),
            )
    except TelegramError as e:
        print(f"[events] failed to edit group message after catch (event {row['id']}): {e}", flush=True)

    # Re-check AGAIN right before notifying the Owner: the group-message
    # edit's own await just above is a SECOND interleaving opportunity for
    # the same concurrent-expiry race the first re-check (above) only closed
    # up to that point -- without this, a race landing during that specific
    # await would still let a "🟡 Waiting for Wallet" DM go out to the Owner
    # right after they'd already received a contradictory "Expired" one.
    fresh_check2 = db.get_event_by_token(token)
    if fresh_check2 is None or fresh_check2["status"] != "claimed" or fresh_check2["winner_id"] != user.id:
        return

    owner_text = cfg.OWNER_WAITING_TEMPLATE.format(
        winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"]
    )
    try:
        owner_msg = await context.bot.send_message(
            chat_id=cfg.BOT_OWNER_ID, text=owner_text, reply_markup=_owner_claim_keyboard(token, include_pay=False)
        )
        # CAS-guarded: if a concurrent expiry won the race during the send
        # above, don't let this stale write point owner_message_id at a
        # message about a claim that's no longer valid.
        db.set_owner_message_id(row["id"], owner_msg.message_id, expected_status="claimed")
    except TelegramError as e:
        print(f"[events] failed to notify Owner of claim (event {row['id']}): {e}", flush=True)


async def on_inspect(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if random.random() < cfg.INSPECT_RARE_CHANCE:
        text = random.choice(cfg.INSPECT_RARE_POPUPS)
    else:
        text = random.choice(cfg.INSPECT_POPUPS)
    await query.answer(text, show_alert=True)


# ══════════════════════════════════════════════════════════════════════════
#  WINNER DM FLOW
# ══════════════════════════════════════════════════════════════════════════
async def cmd_start(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.effective_chat.type != "private":
        # /start (and its deep-link payload) is only meaningful in a private
        # chat with the bot. Without this guard, typing /start in the group
        # would publicly leak whether the sender has a prize pending.
        return
    user = update.effective_user
    pending = db.get_pending_wallet_event(user.id)
    if pending is None:
        await update.message.reply_text(cfg.NOT_YOUR_PRIZE_MSG)
    elif pending["status"] == "claimed":
        await update.message.reply_text(cfg.PENDING_WALLET_MSG)
    else:  # ready_to_pay -- wallet already on file, awaiting Owner payout
        await update.message.reply_text(cfg.PENDING_PAYMENT_MSG)


async def on_private_message(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # Falls back to caption too -- a winner sending their wallet as a photo
    # caption (a plausible real way to share a wallet screenshot) would
    # otherwise be silently ignored entirely (this handler's own filter now
    # matches CAPTION as well as TEXT).
    text = (update.message.text or update.message.caption or "").strip()
    if text.startswith("/"):
        # ~filters.COMMAND (the handler's own registration filter) only
        # inspects message.entities, which is populated for .text but never
        # for a photo's .caption -- so a captioned photo whose caption
        # literally starts with "/" would otherwise slip past that filter
        # and get treated as literal (invalid) wallet text instead of being
        # ignored like a real command would be.
        return

    # Scoped by winner_id, not "the most recent event" -- so a newer event
    # generated by the Owner while this user's win is still pending can never
    # shadow their wallet submission.
    active = db.get_pending_wallet_event(user.id)
    if active is None:
        if cfg.WALLET_RE.fullmatch(text):
            # Wallet-shaped input with nothing to attach it to -- most likely
            # a race (e.g. the Owner cancelled the claim while this was being
            # typed) rather than ordinary chat, which we deliberately ignore
            # in every other case. Worth a reply instead of total silence.
            await update.message.reply_text(cfg.NOT_YOUR_PRIZE_MSG)
        return  # not a pending wallet submission from this user -- ignore

    if not cfg.WALLET_RE.fullmatch(text):
        await update.message.reply_text(cfg.WALLET_INVALID_MSG)
        return

    # If they already submitted a wallet (status == 'ready_to_pay') and the
    # Owner hasn't paid yet, treat this as a CORRECTION -- overwrite it rather
    # than silently ignoring it, so a typo doesn't send funds to an address
    # the winner doesn't control.
    is_correction = active["status"] == "ready_to_pay"
    if not db.set_wallet(active["id"], text):
        # Lost a race against a concurrent cancel/expiry (defense-in-depth --
        # not reachable today since nothing awaits between the read above and
        # this write, but guards against a future refactor changing that).
        await update.message.reply_text(cfg.NOT_YOUR_PRIZE_MSG)
        return
    await update.message.reply_text(cfg.WALLET_UPDATED_MSG if is_correction else cfg.WALLET_RECEIVED_MSG)

    info = _event_info(active["event_key"])
    winner_display = _display_name(user)
    owner_text = cfg.OWNER_READY_TEMPLATE.format(
        winner=winner_display, emoji=info["emoji"], name=info["name"], reward=active["reward"], wallet=text
    )
    keyboard = _owner_claim_keyboard(active["token"], include_pay=True)

    if active["owner_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=cfg.BOT_OWNER_ID,
                message_id=active["owner_message_id"],
                text=owner_text,
                reply_markup=keyboard,
            )
            return
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                # A "correction" that resubmits the EXACT same wallet text
                # (re-pasting to double-check, retrying after an unrelated
                # hiccup) hits this, not a real failure -- the Owner's
                # message already correctly shows this content, so treat it
                # as success. Falling through to the send_message fallback
                # below would otherwise create a genuine duplicate Owner
                # message for the same claim on every such resubmission.
                return

    try:
        msg = await context.bot.send_message(chat_id=cfg.BOT_OWNER_ID, text=owner_text, reply_markup=keyboard)
        db.set_owner_message_id(active["id"], msg.message_id)
    except TelegramError as e:
        print(f"[events] failed to notify Owner of wallet (event {active['id']}): {e}", flush=True)


async def on_owner_paid(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_owner(query.from_user.id):
        await query.answer()
        return

    token = query.data.split(":", 1)[1]
    row = db.get_event_by_token(token)
    if row is None:
        await query.answer("Nothing to pay here.", show_alert=True)
        return
    if row["status"] != "ready_to_pay":
        # Already resolved (paid/claimed/cancelled) -- refresh the message to
        # reflect reality instead of leaving it stuck on stale text. Re-fetch
        # AFTER the answer() await rather than reusing the pre-await snapshot:
        # unlike on_owner_cancel's equivalent branch (whose non-matching
        # statuses are always terminal), this one can see 'claimed' -- which
        # CAN still change to 'ready_to_pay' via a concurrent wallet
        # submission during the very await just below.
        await query.answer("This claim has already been resolved.", show_alert=True)
        fresh_row = db.get_event_by_token(token)
        text, keyboard = _owner_status_render(fresh_row)
        await _edit_current(query, text, keyboard)
        return

    # Atomic compare-and-set: a fast double-tap on this button, or Telegram
    # redelivering the same callback (which does happen), could otherwise let
    # two invocations both pass the status check above before either commits,
    # then both independently DM the winner and post a duplicate public
    # announcement.
    if not db.mark_paid(row["id"], expected_status="ready_to_pay"):
        await query.answer("This claim just changed -- refreshed.", show_alert=True)
        fresh_row = db.get_event_by_token(token)
        text, keyboard = _owner_status_render(fresh_row)
        await _edit_current(query, text, keyboard)
        return
    await query.answer()

    info = _event_info(row["event_key"])
    winner_display = _display(row["winner_username"], row["winner_name"])

    owner_text = cfg.OWNER_PAID_TEMPLATE.format(
        winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"], wallet=row["wallet"]
    )
    # Explicit empty keyboard (not None) so the "✅ Mark as Paid" button is
    # actually removed -- Telegram keeps the existing keyboard when
    # reply_markup is omitted/None, it only clears it on an explicit empty one.
    await _edit_current(query, owner_text, InlineKeyboardMarkup([]))

    try:
        await context.bot.send_message(chat_id=row["winner_id"], text=cfg.WINNER_PAID_MSG)
    except TelegramError as e:
        # Don't let a DM hiccup skip the public group announcement below --
        # the payment is already recorded regardless of whether this DM lands.
        print(f"[events] failed to DM winner of payout (event {row['id']}): {e}", flush=True)

    group_text = cfg.GROUP_PAID_ANNOUNCEMENT_TEMPLATE.format(winner=winner_display, reward=row["reward"])
    try:
        await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=group_text)
    except TelegramError as e:
        # Payment is already durably recorded (mark_paid ran above) -- a
        # failure to post the public announcement shouldn't raise out of the
        # handler.
        print(f"[events] failed to post payout announcement (event {row['id']}): {e}", flush=True)

    # The ORIGINAL group announcement is a separate message from the fresh
    # celebration post just above -- without this, it would be left forever
    # showing "caught!" with what still looks like a live, working
    # Catch/Inspect keyboard (the exact gap just fixed for on_owner_cancel,
    # reproduced here on the far more common successful-payment path).
    if row["chat_id"] is not None and row["message_id"] is not None:
        try:
            if row["has_image"]:
                await context.bot.edit_message_caption(
                    chat_id=row["chat_id"], message_id=row["message_id"],
                    caption=group_text, reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=row["chat_id"], message_id=row["message_id"],
                    text=group_text, reply_markup=InlineKeyboardMarkup([]),
                )
            db.set_display_text(row["id"], group_text)
        except TelegramError as e:
            print(f"[events] failed to edit original group message after payout (event {row['id']}): {e}", flush=True)


def _owner_status_render(row):
    """Rebuilds the (text, keyboard) for the Owner's per-claim message that
    matches row's ACTUAL current status. Used to self-heal a stale message --
    e.g. one that never got its final edit around a restart -- whenever the
    Owner interacts with it again instead of leaving it permanently wrong."""
    info = _event_info(row["event_key"])
    winner_display = _display(row["winner_username"], row["winner_name"])
    status = row["status"]
    if status == "claimed":
        text = cfg.OWNER_WAITING_TEMPLATE.format(
            winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"]
        )
        return text, _owner_claim_keyboard(row["token"], include_pay=False)
    if status == "ready_to_pay":
        text = cfg.OWNER_READY_TEMPLATE.format(
            winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"], wallet=row["wallet"]
        )
        return text, _owner_claim_keyboard(row["token"], include_pay=True)
    if status == "paid":
        text = cfg.OWNER_PAID_TEMPLATE.format(
            winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"], wallet=row["wallet"]
        )
        return text, InlineKeyboardMarkup([])
    # cancelled (or any other terminal status)
    # Include the wallet if one was ever submitted -- without this, cancelling
    # a claim that already reached ready_to_pay would erase the only place
    # the bot ever surfaced that wallet (e.g. for the Owner's own bookkeeping
    # after manually sending funds before releasing the claim).
    wallet_line = f"Wallet: {row['wallet']}\n" if row["wallet"] else ""
    text = cfg.OWNER_CANCELLED_TEMPLATE.format(
        winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"], wallet_line=wallet_line
    )
    return text, InlineKeyboardMarkup([])


async def on_owner_cancel(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lets the Owner release a claim that's stuck because the winner is
    unreachable or unresponsive (blocked the bot, went silent, etc.) -- without
    this, one abandoned claim could softlock that user out of ever catching
    again (the double-claim guard in on_catch) with no recourse but manually
    editing the database."""
    query = update.callback_query
    if not _is_owner(query.from_user.id):
        await query.answer()
        return

    token = query.data.split(":", 1)[1]
    row = db.get_event_by_token(token)
    if row is None:
        await query.answer("Nothing to cancel here.", show_alert=True)
        return
    if row["status"] not in ("claimed", "ready_to_pay"):
        # Already resolved (paid/cancelled) -- refresh the message to reflect
        # reality instead of leaving it stuck on stale text.
        await query.answer("This claim has already been resolved.", show_alert=True)
        text, keyboard = _owner_status_render(row)
        await _edit_current(query, text, keyboard)
        return

    # Atomic compare-and-cancel against the status we just read: if it
    # changed in the tiny window since (e.g. the winner's wallet arrived, or
    # auto-expiry got there first), don't blindly overwrite -- refresh the
    # message to show what's actually true now instead.
    if not db.cancel_event(row["id"], expected_status=row["status"]):
        await query.answer("This claim just changed -- refreshed.", show_alert=True)
        fresh_row = db.get_event_by_token(token)
        text, keyboard = _owner_status_render(fresh_row)
        await _edit_current(query, text, keyboard)
        return
    await query.answer("Claim cancelled.")

    info = _event_info(row["event_key"])
    winner_display = _display(row["winner_username"], row["winner_name"])
    # Include the wallet if one was ever submitted (row["wallet"] survives
    # regardless of status) -- without this, cancelling an already
    # ready_to_pay claim erases the only place the bot ever surfaced that
    # wallet, e.g. for the Owner's own bookkeeping after manually sending
    # funds before releasing the claim.
    wallet_line = f"Wallet: {row['wallet']}\n" if row["wallet"] else ""
    cancelled_text = cfg.OWNER_CANCELLED_TEMPLATE.format(
        winner=winner_display, emoji=info["emoji"], name=info["name"], reward=row["reward"], wallet_line=wallet_line
    )
    await _edit_current(query, cancelled_text, InlineKeyboardMarkup([]))

    # The GROUP's original announcement message is a separate message from
    # the Owner's own DM edited just above -- without this, it would be left
    # forever showing "caught!" with what still looks like a live, working
    # Catch/Inspect keyboard, with no indication to the community the prize
    # was ever released. Mirrors what auto-expiry already does for its own
    # group message.
    if row["chat_id"] is not None and row["message_id"] is not None:
        group_text = cfg.CANCELLED_GROUP_TEMPLATE.format(winner=winner_display, emoji=info["emoji"], name=info["name"])
        try:
            if row["has_image"]:
                await context.bot.edit_message_caption(
                    chat_id=row["chat_id"], message_id=row["message_id"],
                    caption=group_text, reply_markup=InlineKeyboardMarkup([]),
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=row["chat_id"], message_id=row["message_id"],
                    text=group_text, reply_markup=InlineKeyboardMarkup([]),
                )
            db.set_display_text(row["id"], group_text)
        except TelegramError as e:
            print(f"[events] failed to edit cancelled group message (event {row['id']}): {e}", flush=True)

    try:
        await context.bot.send_message(chat_id=row["winner_id"], text=cfg.CLAIM_CANCELLED_WINNER_MSG)
    except TelegramError as e:
        print(f"[events] failed to notify winner of cancellation (event {row['id']}): {e}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
#  OWNER PANEL  (/events -- always DMed to the Owner, never group-visible)
# ══════════════════════════════════════════════════════════════════════════
async def cmd_owner_panel(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_owner(user.id):
        return  # no reply anywhere -- don't reveal the command exists
    try:
        await context.bot.send_message(
            chat_id=cfg.BOT_OWNER_ID, text=_OWNER_PANEL_TEXT, reply_markup=_owner_panel_keyboard()
        )
    except TelegramError as e:
        # Nothing else to fall back to here (the whole point is this only
        # ever goes to the Owner's DM) -- at least log it instead of a
        # silent, unexplained no-op if the Owner never started the bot.
        print(f"[events] failed to send Owner panel: {e}", flush=True)


async def on_owner_panel_button(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_owner(query.from_user.id):
        await query.answer()
        return

    action = query.data.split(":", 1)[1]

    if action == "enable":
        db.set_config("events_enabled", "1")
        await query.answer("Events enabled.")
        await _edit_current(
            query, f"{_OWNER_PANEL_TEXT}\n\n🟢 Events enabled.", _owner_panel_keyboard()
        )

    elif action == "disable":
        db.set_config("events_enabled", "0")
        await query.answer("Events disabled.")
        await _edit_current(
            query, f"{_OWNER_PANEL_TEXT}\n\n🔴 Events disabled.", _owner_panel_keyboard()
        )

    elif action == "status":
        await query.answer()
        s = db.stats_summary()
        text = (
            f"{_OWNER_PANEL_TEXT}\n\n"
            f"Events Enabled: {'Yes' if db.events_enabled() else 'No'}\n"
            f"Active Event: {s['current_status']}\n"
            f"Last Winner: {s['last_winner']}\n"
            f"Total Events: {s['events_generated']}\n"
            f"Total Distributed: {s['total_rewards_distributed']} $IWRU"
        )
        await _edit_current(query, text, _owner_panel_keyboard())

    elif action == "generate":
        await query.answer()
        await _edit_current(
            query, f"{_OWNER_PANEL_TEXT}\n\n🎁 Pick an event to generate:", _GENERATE_KEYBOARD
        )

    elif action == "back":
        await query.answer()
        await _edit_current(query, _OWNER_PANEL_TEXT, _owner_panel_keyboard())

    elif action == "close":
        await query.answer()
        # Explicit empty keyboard (not None) -- Telegram keeps the existing
        # keyboard when reply_markup is omitted/None, only an explicit empty
        # one actually clears it.
        await _edit_current(query, "😼 Closed.", InlineKeyboardMarkup([]))

    elif action.startswith("gen:"):
        key = action.split(":", 1)[1]
        event_id = None if key == "random" else key
        await query.answer()
        try:
            info = await _post_new_event(context, event_id=event_id)
            text = f"{_OWNER_PANEL_TEXT}\n\n✅ Event generated: {info['emoji']} {info['name']}"
        except Exception as e:
            print(f"[events] Generate Event failed: {e}", flush=True)
            text = f"{_OWNER_PANEL_TEXT}\n\n⚠️ Failed to generate event -- check the logs."
        await _edit_current(query, text, _owner_panel_keyboard())


# ══════════════════════════════════════════════════════════════════════════
#  /iwru MENU  (permanent, always available, regardless of event state)
# ══════════════════════════════════════════════════════════════════════════
def _is_other_topic(msg) -> bool:
    """True if msg belongs to a forum topic other than General ('The Bowl').
    Local copy of bot.py's helper of the same shape -- events.py must stay
    import-safe and never import from bot.py."""
    return bool(getattr(msg, "is_topic_message", False))


async def cmd_iwru_menu(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or _is_other_topic(update.message):
        return
    text = random.choice(cfg.IWRU_MENU_FLAVOR_LINES)
    await update.message.reply_text(text, reply_markup=_iwru_menu_keyboard())


def _render_hall_of_fame() -> str:
    hunters = db.top_hunters(5)
    hunters_lines = "\n".join(f"{i + 1}. {name} — {count}" for i, (name, count) in enumerate(hunters))
    hunters_lines = hunters_lines or "No hunters yet."

    crowns = db.crown_winners(10)
    crowns_lines = "\n".join(f"👑 {name}" for name in crowns) or "No Golden Crown winners yet."

    fish = db.most_of("fish", 3)
    fish_lines = "\n".join(f"{i + 1}. {name} — {count}" for i, (name, count) in enumerate(fish))
    fish_lines = fish_lines or "No Purple Fish caught yet."

    total = db.total_distributed()

    return (
        "🏆 Hall of Fame\n\n"
        "Top Hunters:\n" + hunters_lines + "\n\n"
        "Golden Crown Winners:\n" + crowns_lines + "\n\n"
        "Most Purple Fish Found:\n" + fish_lines + "\n\n"
        f"Total Distributed: {total} $IWRU"
    )


def _render_stats() -> str:
    s = db.stats_summary()
    return (
        "📊 Stats\n\n"
        f"Events Generated: {s['events_generated']}\n"
        f"Events Claimed: {s['events_claimed']}\n"
        f"Current Status: {s['current_status']}\n"
        f"Total Rewards: {s['total_rewards_distributed']} $IWRU\n"
        f"Last Winner: {s['last_winner']}\n"
        f"Most Common Treasure: {s['most_common_treasure']}"
    )


async def _safe_answer(query, *args, **kwargs) -> None:
    """query.answer() wrapped defensively like every other Telegram call in
    this file -- a transient flood-control/network error here must never
    abort the handler."""
    try:
        await query.answer(*args, **kwargs)
    except TelegramError:
        pass


async def on_menu_button(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    suffix = query.data.split(":", 1)[1]

    if suffix in ("hof", "stats", "howto"):
        await _safe_answer(query)
        if suffix == "hof":
            content = _render_hall_of_fame()
        elif suffix == "stats":
            content = _render_stats()
        else:
            content = cfg.HOW_TO_PLAY_TEXT

        chat_id = getattr(query.message, "chat_id", None)
        message_id = getattr(query.message, "message_id", None)
        row = db.get_event_by_message(chat_id, message_id) if chat_id is not None and message_id is not None else None
        is_private = getattr(getattr(query.message, "chat", None), "type", None) == "private"
        if row is not None and row["status"] not in ("paid", "cancelled"):
            # This message is the live daily-event post, shared by the whole
            # group. Editing it in place would replace its Catch/Inspect
            # keyboard with just a Back button for EVERYONE until someone
            # happens to press Back -- hiding the treasure from every other
            # player and handing whoever restores it an unearned advantage.
            # Post a fresh, independent message instead; the event's own
            # keyboard is never touched. 'paid' and 'cancelled' are both
            # terminal/closed -- no live keyboard left to protect, so those
            # fall through to editing in place below (and correctly restore
            # via display_text on Back, instead of losing the historical
            # "who won" record).
            try:
                await context.bot.send_message(chat_id=chat_id, text=content)
            except TelegramError:
                pass
        elif row is None and not is_private:
            # The generic /iwru menu message itself, but posted in a GROUP --
            # still shared, visible, and interactable by everyone in that
            # chat. Editing it in place would let one member's button press
            # silently change what every other member sees on that same
            # message. Post a standalone reply instead (no Back button --
            # nothing further to navigate to from a one-shot info post).
            try:
                await context.bot.send_message(chat_id=chat_id, text=content)
            except TelegramError:
                pass
        else:
            await _edit_current(query, content, _BACK_KEYBOARD)

    elif suffix == "rewards":
        # Always a private popup -- answer_callback_query works for the
        # pressing user even if they've never started the bot privately,
        # so nothing sensitive is ever posted into the group.
        summary = db.user_rewards_summary(query.from_user.id)
        text = (
            "🎒 My Rewards\n\n"
            f"Rewards Won: {summary['won']} $IWRU\n"
            f"Rewards Paid: {summary['paid']} $IWRU\n"
            f"Rewards Pending: {summary['pending']} $IWRU\n"
            f"Treasures Found: {summary['treasures_found']}"
        )
        # Telegram's callback-alert text is capped well under this, but the
        # numbers here are unbounded in principle -- truncate defensively.
        if len(text) > 190:
            text = text[:187] + "…"
        await _safe_answer(query, text=text, show_alert=True)

    elif suffix == "luck":
        stars, line = random.choice(cfg.TODAYS_LUCK_MESSAGES)
        await _safe_answer(query, text=f"{stars}\n{line}", show_alert=True)

    elif suffix == "back":
        await _safe_answer(query)
        chat_id = getattr(query.message, "chat_id", None)
        message_id = getattr(query.message, "message_id", None)
        row = db.get_event_by_message(chat_id, message_id) if chat_id is not None and message_id is not None else None
        if row is not None and row["status"] not in ("paid", "cancelled"):
            await _edit_current(query, row["display_text"], _event_keyboard(row["token"]))
        elif row is not None:
            # Closed event (paid or cancelled) -- restore its historical
            # display_text (the permanent public record of what happened),
            # but WITHOUT the Catch/Inspect keyboard: that event is resolved,
            # so re-attaching working-looking buttons would falsely suggest
            # it's still catchable.
            await _edit_current(query, row["display_text"], InlineKeyboardMarkup([]))
        else:
            text = random.choice(cfg.IWRU_MENU_FLAVOR_LINES)
            await _edit_current(query, text, _iwru_menu_keyboard())

    else:
        # Unknown/future suffix -- always answer so the client never shows a
        # perpetual loading spinner on the tap.
        await _safe_answer(query)


# ══════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ══════════════════════════════════════════════════════════════════════════
def register(app: Application) -> None:
    db.init_db()

    app.add_handler(CommandHandler("iwru", cmd_iwru_menu))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("events", cmd_owner_panel))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_private_message)
    )

    app.add_handler(CallbackQueryHandler(on_catch, pattern=r"^catch:"))
    app.add_handler(CallbackQueryHandler(on_inspect, pattern=r"^inspect:"))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_owner_paid, pattern=r"^paid:"))
    app.add_handler(CallbackQueryHandler(on_owner_cancel, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(on_owner_panel_button, pattern=r"^owner:"))

    _schedule_daily_event(app.job_queue)
