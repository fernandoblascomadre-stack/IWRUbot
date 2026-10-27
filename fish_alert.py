"""fish_alert.py -- IWRU FISH ALERT, the private alerts group of IWRU Tools (2026-10-28, Fernando).

Only people with IWRU worth >= $3 locked in the IWRU Tools vault may be in the group. The rules live in the IWRU
Tools service (IWRU TOOLS/server, on the VPS); this file only answers Telegram:

- People join through a join-request link. On every chat_join_request for FISH_CHAT_ID the bot asks the Tools service
  (POST /internal/join-request, HMAC-signed with TOOLS_INTERNAL_SECRET) and approves or declines. If the service can't
  be reached the request is left pending (never approved by default); Telegram keeps it and the person can ask again.
- The Tools service itself removes people who lose access and posts the alerts to the alerts topic; it calls the Bot
  API directly (sendMessage/banChatMember don't compete with this bot's polling).
- The cat stays quiet in that group: a group -1 handler stops every update from FISH_CHAT_ID before bot.py's chatter,
  reactions and bookkeeping see it, so the group never becomes one of the "known chats" the reminders post to.

OFF unless FISH_CHAT_ID, TOOLS_API_URL and TOOLS_INTERNAL_SECRET are set.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.request

from telegram.ext import ApplicationHandlerStop, ChatJoinRequestHandler, TypeHandler

FISH_CHAT_ID = os.environ.get("FISH_CHAT_ID", "").strip()
TOOLS_API_URL = os.environ.get("TOOLS_API_URL", "").strip().rstrip("/")
TOOLS_INTERNAL_SECRET = os.environ.get("TOOLS_INTERNAL_SECRET", "").strip()
ENABLED = bool(FISH_CHAT_ID and TOOLS_API_URL and TOOLS_INTERNAL_SECRET)
TIMEOUT_S = 10


def signed_request(tg_id, chat_id, now=None, secret=None):
    """Body + signature exactly as the Tools service checks them (HMAC-SHA256 of the raw body, hex)."""
    body = json.dumps({"chat_id": str(chat_id), "tg_id": int(tg_id), "ts": int(now if now is not None else time.time())},
                      separators=(",", ":"))
    sig = hmac.new((secret if secret is not None else TOOLS_INTERNAL_SECRET).encode(), body.encode(), hashlib.sha256).hexdigest()
    return body, sig


def ask_tools(tg_id, chat_id, opener=urllib.request.urlopen):
    """{'approve': bool, 'reason': str} from the Tools service, or None when it can't be asked."""
    body, sig = signed_request(tg_id, chat_id)
    req = urllib.request.Request(f"{TOOLS_API_URL}/internal/join-request", data=body.encode(), method="POST",
                                 headers={"content-type": "application/json", "x-iwru-signature": sig})
    try:
        with opener(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read())
            return data if isinstance(data, dict) and "approve" in data else None
    except Exception as e:  # network down, 4xx/5xx, bad JSON: all mean "don't decide now"
        print(f"[fish] tools service unreachable for {tg_id}: {e}", flush=True)
        return None


async def on_join_request(update, context):
    req = update.chat_join_request
    if not req or str(req.chat.id) != FISH_CHAT_ID:
        return
    answer = await asyncio.to_thread(ask_tools, req.from_user.id, req.chat.id)
    if answer is None:
        return  # left pending: fail closed
    try:
        if answer.get("approve") is True:
            await req.approve()
            print(f"[fish] approved {req.from_user.id}", flush=True)
        else:
            await req.decline()
            print(f"[fish] declined {req.from_user.id} ({answer.get('reason')})", flush=True)
    except Exception as e:
        print(f"[fish] approve/decline failed for {req.from_user.id}: {e}", flush=True)


def register(app):
    if not ENABLED:
        print("[fish] disabled -- set FISH_CHAT_ID, TOOLS_API_URL and TOOLS_INTERNAL_SECRET", flush=True)
        return
    chat = int(FISH_CHAT_ID)
    app.add_handler(ChatJoinRequestHandler(on_join_request, chat_id=chat), group=-2)
    # group -1 runs before bot.py's handlers (group 0) and events/rocket (group 1); the stop ends processing there
    app.add_handler(TypeHandler(object, _quiet_other_updates), group=-1)
    print(f"[fish] on: join requests for {FISH_CHAT_ID} decided by {TOOLS_API_URL}", flush=True)


async def _quiet_other_updates(update, context):
    """Nothing else in the bot handles updates from the alerts group (messages, reactions, edits, member updates)."""
    chat = getattr(update, "effective_chat", None)
    if chat is not None and str(chat.id) == FISH_CHAT_ID and getattr(update, "chat_join_request", None) is None:
        raise ApplicationHandlerStop
