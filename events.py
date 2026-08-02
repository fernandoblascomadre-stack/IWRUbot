import random
import time
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


def _row_is_legacy_photo_message(row) -> bool:
    """True only for a row created BEFORE the sticker+text redesign, back
    when the tracked message itself was a photo with a caption (has_image
    meant exactly that). Every row created after that redesign always has
    sticker_message_id set together with has_image whenever an image was
    sent (the sticker is a separate message; the tracked message is always
    plain text) -- so has_image=True with sticker_message_id still NULL can
    only mean an old-style photo/caption row that predates this column.
    Needed so editing an old still-active event (created before a deploy of
    this change) doesn't try edit_message_text on what Telegram still
    considers a photo message, which would fail outright."""
    return bool(row["has_image"]) and row["sticker_message_id"] is None


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
            # query.message has neither .photo nor real text/caption to edit
            # (e.g. it's a sticker message -- the Catch/Inspect/menu keyboard
            # lives there now for most events) -- Telegram rejects this call
            # outright in that case, which is fine (nothing to do to a
            # sticker's content), but it's worth a log line rather than a
            # silent no-op like every other Telegram call site in this file.
            await query.edit_message_text(text=text, reply_markup=keyboard)
    except (TelegramError, AttributeError) as e:
        print(f"[events] _edit_current failed (message_id={getattr(query.message, 'message_id', '?')}): {e}", flush=True)


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
    # Catch gets its own full-width row -- sharing a row with Inspect split
    # the available width in half, which truncated CATCH_BUTTON_TEXT on
    # Telegram's clients. Every other button just shifts down one row,
    # keeping their existing pairings.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(cfg.CATCH_BUTTON_TEXT, callback_data=f"catch:{token}")],
            [
                InlineKeyboardButton(cfg.INSPECT_BUTTON_TEXT, callback_data=f"inspect:{token}"),
                InlineKeyboardButton("🏆 Hall of Fame", callback_data="menu:hof"),
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
                InlineKeyboardButton("🎒 My Rewards", callback_data="menu:rewards"),
            ],
            [InlineKeyboardButton("🐾 How to Play", callback_data="menu:howto")],
        ]
    )


def _owner_claim_keyboard(token: str, include_pay: bool) -> InlineKeyboardMarkup:
    """The keyboard attached to the Owner's per-claim message. include_pay is
    False while still 'claimed' (nothing to pay yet) and True once
    'ready_to_pay'. Cancel is always available so a stuck/unresponsive winner
    can never permanently softlock the game. Remind is offered instead of Pay
    while still 'claimed' -- a lower-friction nudge for a winner who probably
    just missed the DM/deep-link, before resorting to Cancel; once a wallet's
    been submitted (ready_to_pay) there's nothing left to remind them of."""
    rows = []
    if include_pay:
        rows.append([InlineKeyboardButton("✅ Mark as Paid", callback_data=f"paid:{token}")])
    else:
        rows.append([InlineKeyboardButton("🔔 Remind Winner", callback_data=f"remind:{token}")])
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


def _owner_panel_keyboard(show_cancel_current: bool = False) -> InlineKeyboardMarkup:
    """show_cancel_current adds a row to withdraw the currently-active
    UNCLAIMED event (e.g. a manual test generation) without touching
    events_enabled and without affecting whether today's random daily event
    still fires -- that dedupe is tracked separately and never touched here."""
    rows = [
        [
            InlineKeyboardButton("🟢 Enable Events", callback_data="owner:enable"),
            InlineKeyboardButton("🔴 Disable Events", callback_data="owner:disable"),
        ],
        [InlineKeyboardButton("📊 Status", callback_data="owner:status")],
        [InlineKeyboardButton("🎁 Generate Event", callback_data="owner:generate")],
    ]
    if show_cancel_current:
        rows.append([InlineKeyboardButton("❌ Cancel Current Event", callback_data="owner:cancel_current")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="owner:close")])
    return InlineKeyboardMarkup(rows)


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
        f"Reward:\n{info['reward']} IWRU\n\n"
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
                # Keep display_text in sync with what's actually shown now --
                # otherwise a later Hall of Fame/Stats/How-to-Play shortcut
                # pressed on this same (now-closed) message, followed by
                # Back, would restore the stale pre-expiry "caught!" text
                # instead of this expiry notice. Only updated on a
                # successful edit -- if the edit above failed, the group
                # message still shows the OLD text, so display_text must
                # keep matching that reality, not this one.
                if await _edit_group_message(context, row, expired_text, clear_keyboard=True):
                    db.set_display_text(row["id"], expired_text)

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
                if await _edit_group_message(context, fresh, expired_text, clear_keyboard=True):
                    db.set_display_text(fresh["id"], expired_text)
        except Exception as e:
            print(f"[events] failed to expire stale unclaimed event (event {row['id']}): {e}", flush=True)


async def _send_event_message(context: ContextTypes.DEFAULT_TYPE, event_id: str, text: str, keyboard: InlineKeyboardMarkup):
    """Sends one event announcement to the group: the artwork first, as a
    STICKER (not a photo) if the event's sticker file exists on disk, then
    the actual tracked message -- always plain text, carrying the caption
    content. Stickers are used specifically because Telegram reliably renders
    their transparent background; regular photo/caption messages do not
    (Telegram frequently flattens a photo's transparency onto a white
    canvas).

    The Catch/Inspect/menu keyboard is attached to the STICKER message
    (sendSticker supports reply_markup, just not a caption) whenever one was
    sent -- so the buttons live on the exact same message as the artwork and
    can never end up visually detached from it, even if an unrelated chat
    message lands between the two sends. The text message only gets the
    keyboard as a fallback, when there's no sticker to hold it (missing
    asset file). Callers must mirror this same sticker-first placement
    whenever they later edit/clear this event's keyboard -- see
    _keyboard_message.

    Returns (msg, sticker_message_id) -- msg is the text message (the one
    this whole event's row tracks and edits going forward via chat_id/
    message_id); sticker_message_id is None if no sticker was sent.

    Shared by _post_new_event (brand-new event) and _bump_event (reposting
    an existing unclaimed one), so both stay in sync automatically if this
    logic ever changes."""
    info = cfg.EVENTS[event_id]
    sticker_path = cfg.ASSETS_DIR / info["sticker_filename"]
    sticker_message_id = None
    if sticker_path.is_file():
        try:
            with open(sticker_path, "rb") as f:
                sticker_msg = await context.bot.send_sticker(chat_id=cfg.EVENTS_CHAT_ID, sticker=f, reply_markup=keyboard)
            sticker_message_id = sticker_msg.message_id
        except Exception as e:
            print(f"[events] failed to send sticker {sticker_path}: {e}", flush=True)
    text_keyboard = None if sticker_message_id is not None else keyboard
    # Let this raise on failure -- the caller has nothing to clean up if
    # nothing was ever persisted/updated yet. EXCEPT the sticker just sent
    # above, if any: that one already exists in the chat and is otherwise
    # completely untracked (its id was never returned anywhere), so a failure
    # here specifically must clean it up first or it's stuck as a permanent,
    # unexplained stray image, possibly still carrying a live keyboard, with
    # no text ever following it.
    try:
        msg = await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=text, reply_markup=text_keyboard)
    except Exception:
        if sticker_message_id is not None:
            try:
                await context.bot.delete_message(chat_id=cfg.EVENTS_CHAT_ID, message_id=sticker_message_id)
            except TelegramError as e:
                print(f"[events] failed to clean up orphaned sticker after text send failure: {e}", flush=True)
        raise
    return msg, sticker_message_id


def _keyboard_message(row) -> tuple:
    """Returns (chat_id, message_id) of whichever message currently carries
    this event's live interactive keyboard -- the sticker if one exists (see
    _send_event_message), otherwise the plain text message itself (a row
    with no sticker: missing artwork file, or a legacy pre-redesign row,
    which _row_is_legacy_photo_message already identifies by this same
    sticker_message_id-is-None condition)."""
    if row["sticker_message_id"] is not None:
        return row["chat_id"], row["sticker_message_id"]
    return row["chat_id"], row["message_id"]


async def _edit_group_message(context: ContextTypes.DEFAULT_TYPE, row, text: str, *, clear_keyboard: bool) -> bool:
    """Edits this event's plain text/caption message to `text`. When
    clear_keyboard is True, also strips whatever message currently carries
    the live keyboard (see _keyboard_message) -- the sticker, via a second
    edit, or the SAME text message in the same edit when there's no sticker
    to hold it instead. When clear_keyboard is False, the text message's own
    reply_markup is left untouched entirely (omitting reply_markup on an
    edit means "keep whatever's there", which is exactly right whether
    that's nothing -- sticker holds the keyboard -- or the still-live event
    keyboard on a no-sticker row).

    Returns True if the text/caption edit itself succeeded (failures are
    logged here so callers don't each need their own try/except). The
    sticker-keyboard clear (when applicable) is attempted independently of
    that outcome -- it targets a DIFFERENT message, so a failure editing the
    text must never suppress the attempt to also clear the sticker's
    keyboard (and vice versa); otherwise a merely-undeletable/edited-away
    text message would leave the sticker's Catch/Inspect keyboard live
    forever with no other code path left to clean it up."""
    has_sticker = row["sticker_message_id"] is not None
    text_keyboard = InlineKeyboardMarkup([]) if (clear_keyboard and not has_sticker) else None
    text_edit_ok = True
    try:
        if _row_is_legacy_photo_message(row):
            caption = text if len(text) <= 500 else text[:450] + "…"
            await context.bot.edit_message_caption(
                chat_id=row["chat_id"], message_id=row["message_id"],
                caption=caption, reply_markup=text_keyboard,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=row["chat_id"], message_id=row["message_id"],
                text=text, reply_markup=text_keyboard,
            )
    except TelegramError as e:
        print(f"[events] failed to edit group message (event {row['id']}): {e}", flush=True)
        text_edit_ok = False
    if clear_keyboard and has_sticker:
        kb_chat_id, kb_message_id = _keyboard_message(row)
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=kb_chat_id, message_id=kb_message_id,
                reply_markup=InlineKeyboardMarkup([]),
            )
        except TelegramError as e:
            print(f"[events] failed to clear sticker keyboard (event {row['id']}): {e}", flush=True)
    return text_edit_ok


async def _clear_group_keyboard(context: ContextTypes.DEFAULT_TYPE, row) -> None:
    """Clears this event's live Catch/Inspect keyboard in place WITHOUT
    touching its text -- for callers that don't want any marker text stamped
    onto the original post (e.g. on_owner_paid, where the payout is already
    announced via a separate fresh message). Targets whichever message
    _keyboard_message resolves to (sticker, or the text message itself when
    there's no sticker to hold it)."""
    kb_chat_id, kb_message_id = _keyboard_message(row)
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=kb_chat_id, message_id=kb_message_id,
            reply_markup=InlineKeyboardMarkup([]),
        )
    except TelegramError as e:
        print(f"[events] failed to clear group message keyboard (event {row['id']}): {e}", flush=True)


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

    msg, sticker_message_id = await _send_event_message(context, event_id, text, keyboard)

    try:
        # One atomic INSERT (chat_id/message_id/display_text all included) --
        # either the row is fully created or not created at all, never
        # partially populated. has_image records whether a sticker was sent
        # alongside, so a bump later knows whether there's one to also
        # delete/repost.
        db.create_event(
            event_id, token, info["reward"], msg.chat_id, msg.message_id, text,
            has_image=sticker_message_id is not None, sticker_message_id=sticker_message_id,
        )
        # A brand-new event starts its own fresh bump countdown -- without
        # this, a count left over from a PREVIOUS event (claimed/expired
        # right as it neared the bump threshold) would carry straight into
        # this one, bumping it far sooner than EVENT_BUMP_MESSAGE_THRESHOLD
        # messages of its own actual activity.
        _reset_bump_counter()
    except Exception as e:
        # The message is already live in the group at this point. If we can't
        # persist it, don't leave what looks like a normal, catchable event
        # with working buttons -- get_event_by_token would find no row for it,
        # so every press would silently show "too late" forever with no
        # indication anything is wrong. Best-effort: mark it broken instead.
        print(f"[events] failed to persist newly-posted event: {e}", flush=True)
        # msg is always the plain text message now -- Telegram's much higher
        # plain-text cap (4096 UTF-16 units) applies here, not the 1024-unit
        # caption cap, so no truncation budget is needed. The keyboard itself
        # lives on the sticker (if one was sent) rather than this text
        # message, so it needs its own separate clear -- attempted
        # independently of whether the text edit below succeeds, since they
        # target two different messages and either one failing must not
        # suppress the other.
        error_text = f"{text}\n\n⚠️ Registration failed -- contact the Owner."
        text_keyboard = None if sticker_message_id is not None else InlineKeyboardMarkup([])
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id, message_id=msg.message_id,
                text=error_text, reply_markup=text_keyboard,
            )
        except Exception as edit_error:
            print(f"[events] also failed to mark the broken event: {edit_error}", flush=True)
        if sticker_message_id is not None:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=msg.chat_id, message_id=sticker_message_id,
                    reply_markup=InlineKeyboardMarkup([]),
                )
            except TelegramError as e2:
                print(f"[events] also failed to clear broken event's sticker keyboard: {e2}", flush=True)
        raise
    return info


# ══════════════════════════════════════════════════════════════════════════
#  BUMP  (repost an unclaimed event so chat activity can't bury it)
# ══════════════════════════════════════════════════════════════════════════
# Persisted in the DB's config table (not a plain in-memory dict) because
# this process restarts often (Render redeploys, polling Conflict recovery --
# see build_app's retry loop): an in-memory counter resets to 0 on every one
# of those restarts, so on a busy-but-not-instant chat it could perpetually
# get wiped just before reaching EVENT_BUMP_MESSAGE_THRESHOLD, making the
# bump silently never fire even though the logic itself is correct. Only one
# chat (cfg.EVENTS_CHAT_ID) ever needs this, so a single scalar key is enough
# -- no need for a per-chat dict.
_BUMP_COUNTER_KEY = "msgs_since_bump"
_BUMP_LAST_AT_KEY = "last_bump_at"


def _reset_bump_counter() -> None:
    db.set_config(_BUMP_COUNTER_KEY, "0")
    # Also clears the cooldown timestamp -- called both when a brand-new
    # event starts (so it isn't held back by a cooldown left over from a
    # PREVIOUS event's last bump) and every time on_group_activity's gates
    # both clear (right before it either bumps, resetting this to "now", or
    # finds nothing to bump).
    db.set_config(_BUMP_LAST_AT_KEY, "0")


async def on_group_activity(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Called once per real (human, non-command) group message -- bot.py's
    leer() is the only caller. Counts messages per chat and, once enough have
    passed AND enough real time has elapsed since the last bump, reposts the
    currently-unclaimed event (if there is one) at the bottom of the chat so
    it doesn't stay buried under new conversation forever.

    Two independent gates, both required: EVENT_BUMP_MESSAGE_THRESHOLD (chat
    activity -- a quiet chat can't bury anything, so there's nothing to bump
    regardless of how much time passes) and EVENT_BUMP_COOLDOWN_SECONDS (wall
    clock -- with the threshold at 1, the message-count gate alone would fire
    on literally every message, hammering Telegram's API during a fast burst
    of chat activity)."""
    if chat_id != cfg.EVENTS_CHAT_ID:
        return  # only the events chat's treasure can ever need bumping -- no point tracking counts elsewhere
    try:
        count = int(db.get_config(_BUMP_COUNTER_KEY, "0")) + 1
        if count < cfg.EVENT_BUMP_MESSAGE_THRESHOLD:
            db.set_config(_BUMP_COUNTER_KEY, str(count))
            return

        last_bump_at = float(db.get_config(_BUMP_LAST_AT_KEY, "0"))
        now = time.time()
        if now - last_bump_at < cfg.EVENT_BUMP_COOLDOWN_SECONDS:
            # Message-count gate cleared but the cooldown hasn't -- keep the
            # counter climbing (harmless either way once threshold's already
            # met) rather than resetting it, so a config change raising the
            # threshold back up later doesn't silently start from a
            # stale-looking 0.
            db.set_config(_BUMP_COUNTER_KEY, str(count))
            return
        _reset_bump_counter()

        row = db.get_active_event()
        if row is not None and row["status"] == "unclaimed":
            db.set_config(_BUMP_LAST_AT_KEY, str(now))
            await _bump_event(context, row)
    except Exception as e:
        print(f"[events] on_group_activity error: {e}", flush=True)


async def _bump_event(context: ContextTypes.DEFAULT_TYPE, row) -> None:
    """Automatic bump: only ever fires for a still-UNCLAIMED event (chat
    activity threshold, see on_group_activity), so the CAS is always against
    'unclaimed' specifically."""
    text = _build_unclaimed_text(row["event_key"])
    keyboard = _event_keyboard(row["token"])
    await _reposition_event_messages(context, row, text, keyboard, expected_status="unclaimed")


async def _reposition_event_messages(
    context: ContextTypes.DEFAULT_TYPE, row, text: str, keyboard: InlineKeyboardMarkup, *, expected_status: str
) -> bool:
    """Deletes the old (buried) event message(s) and reposts a fresh copy at
    the bottom of the chat, repointing the SAME event/token/history there --
    nothing about the event itself changes, only where it lives in the chat.

    Shared by _bump_event (automatic, unclaimed-only, triggered by chat
    activity) and on_menu_button (triggered by someone pressing an info
    button on this event, REGARDLESS of its current status) -- without the
    latter, the sticker+text could be left behind, buried, while an info
    popup about them appears fresh at the bottom, which reads as
    disconnected/illogical. Repositioning on interaction keeps everything
    about this event visually together."""
    msg, sticker_message_id = await _send_event_message(context, row["event_key"], text, keyboard)

    # Atomic compare-and-set against expected_status: if this event's status
    # changed in the tiny window between the caller deciding to reposition it
    # and this write actually running (e.g. a concurrently-running
    # daily_event_job's unclaimed-expiry, or the Owner cancelling it),
    # don't repoint an event out from under its real current state.
    moved = db.rebump_event(
        row["id"], msg.chat_id, msg.message_id, sticker_message_id is not None,
        expected_status=expected_status, sticker_message_id=sticker_message_id,
    )
    if not moved:
        # The fresh copies above are already sent and now completely
        # untracked (never written into the DB) -- without cleaning them up
        # here, they'd sit in the chat forever: an unexplained stray sticker
        # (with its own still-live keyboard, if one was sent) and a
        # permanently-orphaned duplicate text copy. Best-effort: delete both,
        # or failing that, at least strip whichever one carries the keyboard.
        if sticker_message_id is not None:
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=sticker_message_id)
            except TelegramError as e:
                print(f"[events] failed to delete orphaned bump sticker (event {row['id']}): {e}", flush=True)
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=msg.chat_id, message_id=sticker_message_id,
                        reply_markup=InlineKeyboardMarkup([]),
                    )
                except TelegramError as e2:
                    print(f"[events] also failed to clear orphaned bump sticker's keyboard (event {row['id']}): {e2}", flush=True)
        try:
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
        except TelegramError as e:
            print(f"[events] failed to delete orphaned bump copy (event {row['id']}): {e}", flush=True)
            if sticker_message_id is None:
                # Text only carries the keyboard when there's no sticker to
                # hold it instead -- otherwise it was never clickable to
                # begin with, nothing to strip here.
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=msg.chat_id, message_id=msg.message_id,
                        reply_markup=InlineKeyboardMarkup([]),
                    )
                except TelegramError as e2:
                    print(f"[events] also failed to clear orphaned bump copy's keyboard (event {row['id']}): {e2}", flush=True)
        return False

    if row["sticker_message_id"] is not None:
        try:
            await context.bot.delete_message(chat_id=row["chat_id"], message_id=row["sticker_message_id"])
        except TelegramError as e:
            print(f"[events] failed to delete old bumped sticker (event {row['id']}): {e}", flush=True)
            # The old sticker carries the live keyboard whenever one exists
            # (see _send_event_message) -- leaving it undeleted here would
            # leave a still-clickable Catch/Inspect/menu keyboard sitting on
            # an old, buried, now-superseded copy. Best-effort; if this also
            # fails there's nothing further to do.
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=row["chat_id"], message_id=row["sticker_message_id"],
                    reply_markup=InlineKeyboardMarkup([]),
                )
            except TelegramError as e2:
                print(f"[events] also failed to clear old bumped sticker's keyboard (event {row['id']}): {e2}", flush=True)

    try:
        await context.bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
    except TelegramError as e:
        print(f"[events] failed to delete old bumped message (event {row['id']}): {e}", flush=True)
        if row["sticker_message_id"] is None:
            # Couldn't remove it outright (already deleted by someone else,
            # missing permissions, network blip) -- at minimum, strip its
            # Catch/Inspect keyboard so it doesn't sit there looking
            # clickable forever once the real treasure has moved on. Only
            # needed when there was no sticker to hold the keyboard instead
            # (otherwise this text message was never clickable to begin
            # with). Uses the dedicated reply-markup-only edit endpoint (not
            # edit_message_text/caption) so the message's existing text is
            # left completely untouched -- only the keyboard changes.
            # Best-effort; if this also fails there's nothing further to do.
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=row["chat_id"], message_id=row["message_id"],
                    reply_markup=InlineKeyboardMarkup([]),
                )
            except TelegramError as e2:
                print(f"[events] also failed to clear old bumped message's keyboard (event {row['id']}): {e2}", flush=True)
    return True


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
    # clear_keyboard=False: the keyboard itself doesn't change at catch time
    # (still the same _event_keyboard(token), deliberately left live-looking
    # -- the status check at the top of this handler is what actually blocks
    # a second catch and shows the "too late" popup instead).
    await _edit_group_message(context, fresh_check, caught_text, clear_keyboard=False)

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

    if not cfg.is_valid_wallet(text):
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
    confirm_template = cfg.WALLET_UPDATED_MSG if is_correction else cfg.WALLET_RECEIVED_MSG
    await update.message.reply_text(confirm_template.format(wallet=text))

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
        # Don't let a DM hiccup skip the public group edit below -- the
        # payment is already recorded regardless of whether this DM lands.
        print(f"[events] failed to DM winner of payout (event {row['id']}): {e}", flush=True)

    # Announce the payout with a FRESH message at the bottom of the chat --
    # this is the only place the Owner's press is actually visible live; the
    # original "caught!" post can be buried arbitrarily far back in the
    # chat's history by the time payment happens (it never bumps again once
    # claimed), so an edit landing there alone is invisible in the live feed
    # and reads as if the payout had been announced earlier than it was.
    group_text = cfg.GROUP_PAID_ANNOUNCEMENT_TEMPLATE.format(winner=winner_display, reward=row["reward"])
    try:
        await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=group_text)
    except TelegramError as e:
        print(f"[events] failed to post group payout announcement (event {row['id']}): {e}", flush=True)

    # Close out the ORIGINAL "caught!" post in place too -- no marker text
    # (the payout is already visible via the fresh group announcement above),
    # just clear its now-dead Catch/Inspect keyboard so it doesn't sit there
    # looking like a still-live event.
    #
    # Re-fetch rather than trust the snapshot from the top of this handler:
    # on_menu_button's info-button reposition runs regardless of a 'paid'-
    # bound event's status ('claimed'/'ready_to_pay' are both still eligible
    # right up until mark_paid committed above), so it can relocate this
    # exact message to a new chat_id/message_id during one of the awaits
    # just above -- editing the stale one would silently fail (mirrors
    # _expire_stale_events's same re-fetch-before-editing guard).
    fresh = db.get_event_by_token(row["token"]) or row
    if fresh["chat_id"] is not None and fresh["message_id"] is not None:
        await _clear_group_keyboard(context, fresh)


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

    # Announce the release with a FRESH message at the bottom of the chat --
    # same reasoning as on_owner_paid: the original "caught!" post can be
    # buried arbitrarily far back in the chat's history by the time the
    # Owner cancels (it never bumps again once claimed), so an edit landing
    # there alone would be invisible in the live feed and could read as if
    # the release had been announced before the Owner even pressed Cancel.
    group_text = cfg.CANCELLED_GROUP_TEMPLATE.format(winner=winner_display, emoji=info["emoji"], name=info["name"])
    try:
        await context.bot.send_message(chat_id=cfg.EVENTS_CHAT_ID, text=group_text)
    except TelegramError as e:
        print(f"[events] failed to post group cancellation announcement (event {row['id']}): {e}", flush=True)

    # Close out the ORIGINAL "caught!" post in place too -- not with the same
    # announcement text (that would read as a second, duplicate release in
    # the group), just a short marker plus clearing its now-dead Catch/Inspect
    # keyboard, so it doesn't sit there looking like a still-live event.
    #
    # Re-fetch rather than trust the snapshot from the top of this handler --
    # same reasoning as on_owner_paid's identical guard: on_menu_button can
    # reposition this exact message to a new chat_id/message_id during one of
    # the awaits just above, and editing the stale location would silently
    # fail.
    fresh = db.get_event_by_token(row["token"]) or row
    if fresh["chat_id"] is not None and fresh["message_id"] is not None:
        closed_text = f"{fresh['display_text']}\n\n❌ Claim released."
        if await _edit_group_message(context, fresh, closed_text, clear_keyboard=True):
            db.set_display_text(fresh["id"], closed_text)

    try:
        await context.bot.send_message(chat_id=row["winner_id"], text=cfg.CLAIM_CANCELLED_WINNER_MSG)
    except TelegramError as e:
        print(f"[events] failed to notify winner of cancellation (event {row['id']}): {e}", flush=True)


async def on_owner_remind(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lets the Owner manually nudge a winner who hasn't sent their wallet
    yet -- a lower-friction alternative to Cancel Claim for someone who's
    probably just missed the DM or never clicked the deep-link, rather than
    gone silent for good. Doesn't touch the claim's status at all (unlike
    Cancel/Pay), so it's safe to press more than once."""
    query = update.callback_query
    if not _is_owner(query.from_user.id):
        await query.answer()
        return

    token = query.data.split(":", 1)[1]
    row = db.get_event_by_token(token)
    if row is None or row["status"] != "claimed":
        # Already resolved (wallet submitted/paid/cancelled) or never existed
        # -- nothing left to remind about. Refresh the message to reflect
        # reality instead of leaving it stuck on a stale Remind button
        # (mirrors on_owner_cancel/on_owner_paid's same self-heal).
        await query.answer("Nothing to remind here -- already resolved.", show_alert=True)
        if row is not None:
            text, keyboard = _owner_status_render(row)
            await _edit_current(query, text, keyboard)
        return

    try:
        await context.bot.send_message(chat_id=row["winner_id"], text=cfg.WALLET_REMINDER_MSG)
        await query.answer("Reminder sent.")
    except TelegramError as e:
        print(f"[events] failed to send wallet reminder (event {row['id']}): {e}", flush=True)
        await query.answer("Couldn't reach them privately (DM blocked, or they never started a chat with me).", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════
#  OWNER PANEL  (/events -- always DMed to the Owner, never group-visible)
# ══════════════════════════════════════════════════════════════════════════
async def cmd_owner_panel(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # update.effective_user is None for updates with no associated user (e.g.
    # a channel post -- CommandHandler matches on update.effective_message,
    # which includes channel posts, and a channel post's from_user is often
    # empty). Without this guard, `/events` posted to a channel the bot
    # administers would crash this handler with AttributeError on user.id.
    if user is None or not _is_owner(user.id):
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
        active = db.get_active_event()
        can_cancel_current = active is not None and active["status"] == "unclaimed"
        text = (
            f"{_OWNER_PANEL_TEXT}\n\n"
            f"Events Enabled: {'Yes' if db.events_enabled() else 'No'}\n"
            f"Active Event: {s['current_status']}\n"
            f"Last Winner: {s['last_winner']}\n"
            f"Total Events: {s['events_generated']}\n"
            f"Total Distributed: {s['total_rewards_distributed']} IWRU"
        )
        await _edit_current(query, text, _owner_panel_keyboard(show_cancel_current=can_cancel_current))

    elif action == "cancel_current":
        # Withdraws the currently-active UNCLAIMED event (e.g. a manual test
        # generation) -- deliberately does NOT touch events_enabled, and does
        # NOT touch last_auto_event_date, so today's random daily event can
        # still fire normally on its own schedule regardless.
        active = db.get_active_event()
        if active is None or active["status"] != "unclaimed":
            await query.answer("Nothing to cancel -- no unclaimed event right now.", show_alert=True)
            s = db.stats_summary()
            text = (
                f"{_OWNER_PANEL_TEXT}\n\n"
                f"Events Enabled: {'Yes' if db.events_enabled() else 'No'}\n"
                f"Active Event: {s['current_status']}\n"
                f"Last Winner: {s['last_winner']}\n"
                f"Total Events: {s['events_generated']}\n"
                f"Total Distributed: {s['total_rewards_distributed']} IWRU"
            )
            await _edit_current(query, text, _owner_panel_keyboard(show_cancel_current=False))
            return
        if not db.cancel_event(active["id"], expected_status="unclaimed"):
            await query.answer("This claim just changed -- refreshed.", show_alert=True)
            await _edit_current(query, _OWNER_PANEL_TEXT, _owner_panel_keyboard())
            return
        await query.answer("Event withdrawn.")
        info = _event_info(active["event_key"])
        if active["chat_id"] is not None and active["message_id"] is not None:
            group_text = cfg.OWNER_WITHDRAWN_GROUP_TEMPLATE.format(emoji=info["emoji"], name=info["name"])
            if await _edit_group_message(context, active, group_text, clear_keyboard=True):
                db.set_display_text(active["id"], group_text)
        await _edit_current(
            query, f"{_OWNER_PANEL_TEXT}\n\n❌ Event withdrawn: {info['emoji']} {info['name']}", _owner_panel_keyboard()
        )

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
        f"Total Distributed: {total} IWRU"
    )


def _render_stats() -> str:
    s = db.stats_summary()
    return (
        "📊 Stats\n\n"
        f"Events Generated: {s['events_generated']}\n"
        f"Events Claimed: {s['events_claimed']}\n"
        f"Current Status: {s['current_status']}\n"
        f"Total Rewards: {s['total_rewards_distributed']} IWRU\n"
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


# Tracks the last standalone info popup (Hall of Fame/Stats/How to Play)
# posted per chat via the "post a new message instead of editing" path below,
# so pressing another info button doesn't leave a growing pile of these in
# the chat -- each new one replaces the last, rather than stacking.
_last_info_popup_message: dict[int, int] = {}


async def _send_info_popup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    """Sends a standalone info message (Hall of Fame/Stats/How to Play),
    deleting the previous one this same mechanism posted in this chat first
    (best-effort) so repeated presses don't accumulate clutter in the chat."""
    prev_message_id = _last_info_popup_message.get(chat_id)
    if prev_message_id is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_message_id)
        except TelegramError:
            pass
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text)
        _last_info_popup_message[chat_id] = msg.message_id
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
            # "who won" record). Deletes the previous such popup in this chat
            # first so repeated presses don't pile up new messages forever.
            #
            # Also repositions the event's own sticker+text to the bottom,
            # alongside the info popup: without this, pressing an info button
            # on an event that's been buried by chat activity would leave the
            # sticker+text stuck wherever they were while the info content
            # appears fresh at the bottom -- disconnected and confusing.
            # Keeps everything about this event visually together.
            reposition_text = (
                _build_unclaimed_text(row["event_key"]) if row["status"] == "unclaimed" else row["display_text"]
            )
            try:
                if await _reposition_event_messages(
                    context, row, reposition_text, _event_keyboard(row["token"]), expected_status=row["status"]
                ):
                    _reset_bump_counter()
            except Exception as e:
                print(f"[events] failed to reposition event on info-button press: {e}", flush=True)
            await _send_info_popup(context, chat_id, content)
        elif row is None and not is_private:
            # The generic /iwru menu message itself, but posted in a GROUP --
            # still shared, visible, and interactable by everyone in that
            # chat. Editing it in place would let one member's button press
            # silently change what every other member sees on that same
            # message. Post a standalone reply instead (no Back button --
            # nothing further to navigate to from a one-shot info post),
            # replacing any previous one the same way.
            await _send_info_popup(context, chat_id, content)
        else:
            await _edit_current(query, content, _BACK_KEYBOARD)

    elif suffix == "rewards":
        # Always a private popup -- answer_callback_query works for the
        # pressing user even if they've never started the bot privately,
        # so nothing sensitive is ever posted into the group.
        summary = db.user_rewards_summary(query.from_user.id)
        text = (
            "🎒 My Rewards\n\n"
            f"Rewards Won: {summary['won']} IWRU\n"
            f"Rewards Paid: {summary['paid']} IWRU\n"
            f"Rewards Pending: {summary['pending']} IWRU\n"
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
    app.add_handler(CallbackQueryHandler(on_owner_remind, pattern=r"^remind:"))
    app.add_handler(CallbackQueryHandler(on_owner_panel_button, pattern=r"^owner:"))

    _schedule_daily_event(app.job_queue)
