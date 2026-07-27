"""$IWRU buy alerts on Monad (chain 143).

Watches the token's own ERC-20 Transfer logs over plain JSON-RPC and posts a
detailed alert for every buy above a USD floor.

WHY THIS SHAPE
--------------
* **Detection is "tokens leaving the market address".** $IWRU is still on a
  nad.fun bonding curve (`market_type: V2_CURVE`), so there is no DEX pair and
  no Swap event to decode -- a buy is simply the curve sending tokens to the
  buyer. The market address is read from nad.fun's API and refreshed every
  minute, which is what makes this lifecycle-proof: when the token graduates,
  the API starts returning the DEX pair instead and the same code keeps
  working without a redeploy.
* **Size comes from `tx.value`.** Curve buys are `payable` calls in native MON,
  so there is no WMON transfer to measure. Post-graduation swaps route WMON
  instead and `tx.value` is 0, so there is a WMON-leg fallback for that day.
* **No new dependency.** Everything is `urllib.request`, already used in
  bot.py's `_delete_webhook_http`. Adding `web3` to a Render worker for three
  JSON-RPC methods would not earn its keep.
* **Never imports from bot.py** -- same rule as events.py. It only needs a
  chat id, which comes from config.

Restart safety: the block cursor lives in the same SQLite `config` table as
`last_merch_announcement_date`, on Render's persistent disk, so a redeploy
resumes where it left off instead of replaying (or skipping) buys. A first-ever
boot starts at chain head so the chat never gets a burst of ancient history.
"""

import asyncio
import json
import random
import time
import urllib.request
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import db
import events_config as cfg

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# ERC-20 balanceOf(address) selector, for the NEW HOLDER check
BALANCE_OF_SELECTOR = "0x70a08231"
WMON = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"

# The public RPC refuses any eth_getLogs range wider than this.
MAX_LOG_RANGE = 100
# nad.fun market data barely moves between two buys seconds apart.
MARKET_CACHE_SECONDS = 60
# How many announced transfers to remember, so a crash between "sent to
# Telegram" and "cursor persisted" cannot double-post on the next boot.
SEEN_LIMIT = 200

_rpc_index = 0
_market_cache = {"ts": 0.0, "data": None}
_seen: set[str] = set()
_seen_loaded = False


# ══════════════════════════════════════════════════════════════════════════
#  TRANSPORT
# ══════════════════════════════════════════════════════════════════════════
def _http_json(url: str, *, data: Optional[bytes] = None, timeout: int = 12) -> dict:
    headers = {"accept": "application/json", "user-agent": "IWRUbot-buybot/1.0"}
    if data is not None:
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rpc(method: str, params: list, *, timeout: int = 12):
    """One JSON-RPC call, rotating through the configured endpoints.

    The endpoint that answered is remembered as the new starting point, so a
    dead primary costs one failed attempt per tick rather than one per call.
    """
    global _rpc_index
    urls = cfg.BUYBOT_RPC_URLS
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_error = None
    for attempt in range(len(urls)):
        idx = (_rpc_index + attempt) % len(urls)
        try:
            payload = _http_json(urls[idx], data=body, timeout=timeout)
            if payload.get("error"):
                raise RuntimeError(payload["error"].get("message", "rpc error"))
            _rpc_index = idx
            return payload.get("result")
        except Exception as e:  # noqa: BLE001 -- any transport failure is a retry
            last_error = e
    raise RuntimeError(f"all Monad RPCs failed ({method}): {last_error}")


def _market_info(force: bool = False) -> dict:
    """nad.fun market snapshot: market address, prices, supply, holders.

    Cached, and mirrored into the DB so an API outage cannot blind the buy
    detector -- a stale market address still identifies buys correctly, it
    just goes stale on the day the token graduates.
    """
    now = time.monotonic()
    cached = _market_cache["data"]
    if not force and cached and (now - _market_cache["ts"]) < MARKET_CACHE_SECONDS:
        return cached
    try:
        payload = _http_json(f"{cfg.BUYBOT_NADFUN_API}/trade/market/{cfg.BUYBOT_TOKEN}")
        info = payload.get("market_info") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[buybot] market info failed: {e}", flush=True)
        return cached or {}

    if not info.get("market_id"):
        return cached or {}
    _market_cache["data"] = info
    _market_cache["ts"] = now
    # persisted separately: a DB hiccup must not throw away data we just
    # fetched successfully, it only costs us the offline fallback
    try:
        db.set_config("buybot_market_id", str(info["market_id"]).lower())
    except Exception as e:  # noqa: BLE001
        print(f"[buybot] could not persist market id: {e}", flush=True)
    return info


def _market_address() -> str:
    info = _market_info()
    addr = info.get("market_id") or db.get_config("buybot_market_id") or ""
    return str(addr).lower()


# ══════════════════════════════════════════════════════════════════════════
#  FORMATTING
# ══════════════════════════════════════════════════════════════════════════
def fmt_amount(n: float) -> str:
    """Compact large-number formatting: 1.23M, 4.56K, 789.01."""
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.2f}K"
    return f"{n:,.2f}"


def fmt_usd(n: float) -> str:
    if n >= 1000:
        return f"${fmt_amount(n)}"
    if n >= 1:
        return f"${n:,.2f}"
    return f"${n:.4f}"


def fmt_price(p: float) -> str:
    """Sub-cent prices need real precision, dollar prices do not."""
    if p <= 0:
        return "---"
    if p >= 1:
        return f"${p:,.4f}"
    return f"${p:.10f}".rstrip("0")


def fmt_mon(n: float) -> str:
    return f"{n:,.2f}" if n >= 1 else f"{n:.4f}"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


# Fish-scale tiers, matching the bot's cat-that-steals-fish persona.
TIERS = [
    (25.0, "🐟 IWRU BUY"),
    (100.0, "🐠 BIG IWRU BUY"),
    (500.0, "🦈 HUGE IWRU BUY"),
    (float("inf"), "🐋 WHALE IWRU BUY"),
]


def tier_for(usd: float) -> str:
    for ceiling, name in TIERS:
        if usd < ceiling:
            return name
    return TIERS[-1][1]


def emoji_bar(usd: float) -> str:
    """One fish per $5 bought, clamped so a whale cannot wallpaper the chat.

    Kept deliberately short: the alert's job is to be read, and a 50-emoji
    wall pushes the numbers off a phone screen.
    """
    count = max(2, min(24, int(usd // 5)))
    return "🐟" * count


QUIPS = [
    "another fish in the bowl 😼",
    "someone has taste.",
    "i watched this one happen. i approve.",
    "the bowl grows.",
    "paws up for this one 🐾",
    "rug? never heard of her.",
    "good. keep going.",
]


def build_alert(data: dict) -> str:
    """The alert body. HTML because every line carries a link."""
    lines = [
        f"<b>{tier_for(data['usd'])}!</b>",
        emoji_bar(data["usd"]),
        "",
        f"💵 <b>{fmt_mon(data['mon'])} MON</b> ({fmt_usd(data['usd'])})",
        f"🪙 <b>{fmt_amount(data['tokens'])} IWRU</b>",
    ]

    buyer_link = f"<a href=\"{cfg.BUYBOT_EXPLORER}/address/{data['buyer']}\">{short_addr(data['buyer'])}</a>"
    if data.get("is_new_holder"):
        lines.append(f"👤 {buyer_link} · 🆕 <b>NEW HOLDER</b>")
    elif data.get("position_before"):
        # "someone is buying again" is the other half of the story a bare NEW
        # HOLDER badge leaves out. The size of their bag is the interesting
        # part; the % is only worth printing when it actually moved the bag.
        held = data["position_before"] + data["tokens"]
        growth = data["tokens"] / data["position_before"] * 100
        suffix = f" (+{growth:,.0f}%)" if growth >= 25 else ""
        lines.append(f"👤 {buyer_link} · 🔁 <b>bought again</b> · holds {fmt_amount(held)}{suffix}")
    else:
        lines.append(f"👤 {buyer_link}")

    # market block, only for the parts nad.fun actually gave us -- a half-empty
    # stats section with "---" everywhere reads worse than no section
    stats = []
    if data.get("price_usd"):
        stats.append(f"💲 Price <b>{fmt_price(data['price_usd'])}</b>")
    if data.get("market_cap"):
        stats.append(f"🟢 MC <b>{fmt_usd(data['market_cap'])}</b>")
    extras = []
    if data.get("holders"):
        extras.append(f"👥 {data['holders']} holders")
    if data.get("volume_usd"):
        extras.append(f"📊 Vol {fmt_usd(data['volume_usd'])}")
    if extras:
        stats.append(" · ".join(extras))
    if stats:
        lines.append("")
        lines.extend(stats)

    lines.append("")
    lines.append(f"<i>{random.choice(QUIPS)}</i>")
    return "\n".join(lines)


def build_keyboard(tx_hash: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 Transaction", url=f"{cfg.BUYBOT_EXPLORER}/tx/{tx_hash}"),
                InlineKeyboardButton("📊 Chart", url=cfg.BUYBOT_CHART_URL),
            ],
            [InlineKeyboardButton("😼 IWRU", url=cfg.BUYBOT_SITE_URL)],
        ]
    )


# ══════════════════════════════════════════════════════════════════════════
#  DETECTION
# ══════════════════════════════════════════════════════════════════════════
def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _load_seen() -> None:
    global _seen_loaded
    if _seen_loaded:
        return
    try:
        stored = json.loads(db.get_config("buybot_seen") or "[]")
        _seen.update(str(x) for x in stored)
    except Exception as e:  # noqa: BLE001
        print(f"[buybot] could not restore seen set: {e}", flush=True)
    _seen_loaded = True


def _remember(key: str) -> None:
    _seen.add(key)
    if len(_seen) > SEEN_LIMIT:
        # arbitrary eviction is fine: anything this old is far behind the cursor
        for stale in list(_seen)[: len(_seen) - SEEN_LIMIT]:
            _seen.discard(stale)
    db.set_config("buybot_seen", json.dumps(list(_seen)))


def _native_spent(tx: dict, receipt_needed: bool) -> float:
    """MON that left the buyer's wallet.

    Pre-graduation this is simply the value of the payable curve call. After
    graduation a router swap carries value 0 and moves WMON instead, so fall
    back to the WMON leg that landed on the market.
    """
    value = int(tx.get("value", "0x0"), 16) / 1e18
    if value > 0 or not receipt_needed:
        return value
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx["hash"]])
        market = _market_address()
        for log in receipt.get("logs", []):
            if log.get("address", "").lower() != WMON:
                continue
            if log.get("topics", [None])[0] != TRANSFER_TOPIC:
                continue
            if _topic_address(log["topics"][2]) != market:
                continue
            return int(log["data"], 16) / 1e18
    except Exception as e:  # noqa: BLE001
        print(f"[buybot] WMON fallback failed: {e}", flush=True)
    return 0.0


def _balance_before(holder: str, block_number: int) -> Optional[float]:
    """Holder's $IWRU balance one block before the buy.

    Returns None when the RPC has no historical state (non-archive nodes
    prune it), in which case the alert simply omits the holder badge rather
    than guessing.
    """
    call = {
        "to": cfg.BUYBOT_TOKEN,
        "data": BALANCE_OF_SELECTOR + holder[2:].lower().rjust(64, "0"),
    }
    try:
        raw = _rpc("eth_call", [call, hex(max(0, block_number - 1))])
        return int(raw, 16) / 1e18
    except Exception:  # noqa: BLE001 -- pruned state is expected, not an error
        return None


def _build_alert_for(log: dict) -> Optional[dict]:
    """Turns one Transfer log into an alert payload, or None if it is not an
    announceable buy. Pure blocking work -- runs off the event loop."""
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    market = _market_address()
    if not market:
        return None
    sender = _topic_address(topics[1])
    recipient = _topic_address(topics[2])
    # A buy is the market handing tokens out. Sells (tokens going in), wallet
    # to wallet transfers and the market's own bookkeeping are all skipped.
    if sender != market or recipient == market:
        return None

    key = f"{log['transactionHash']}:{log.get('logIndex', '0x0')}"
    if key in _seen:
        return None

    tokens = int(log["data"], 16) / 1e18
    if tokens <= 0:
        return None

    tx = _rpc("eth_getTransactionByHash", [log["transactionHash"]])
    if not tx:
        return None
    mon = _native_spent(tx, receipt_needed=True)
    if mon <= 0:
        return None

    info = _market_info()
    mon_usd = float(info.get("native_price") or 0)
    usd = mon * mon_usd
    if mon_usd <= 0:
        print("[buybot] no MON price, skipping alert", flush=True)
        return None
    if usd < cfg.BUYBOT_MIN_USD:
        _remember(key)  # counted as handled: it will never qualify later
        return None

    block_number = int(log["blockNumber"], 16)
    before = _balance_before(recipient, block_number)
    price_usd = float(info.get("price_usd") or 0)
    supply = float(info.get("total_supply") or 0) / 1e18
    volume_native = float(info.get("volume") or 0) / 1e18

    data = {
        "usd": usd,
        "mon": mon,
        "tokens": tokens,
        "buyer": recipient,
        "is_new_holder": before is not None and before <= 0,
        "position_before": before if before else 0.0,
        "price_usd": price_usd,
        "market_cap": price_usd * supply if price_usd and supply else 0.0,
        "holders": info.get("holder_count"),
        "volume_usd": volume_native * mon_usd if volume_native else 0.0,
        "tx_hash": log["transactionHash"],
        "_key": key,
    }
    return data


def _scan() -> tuple[list[dict], Optional[int]]:
    """Walks new blocks and returns (alerts to post, cursor to persist).

    Entirely blocking -- every RPC and REST call in this module happens here,
    which is why the job runs it on a worker thread. It deliberately does NOT
    persist the cursor: that only happens once the alerts have actually been
    sent, so a crash mid-send replays the block range instead of losing it
    (and `_seen` stops the replay from double-posting).
    """
    _load_seen()
    head = int(_rpc("eth_blockNumber", []), 16)
    stored = db.get_config("buybot_last_block")
    if stored is None:
        # first ever boot: start at head so the chat is not flooded with the
        # token's entire trading history
        db.set_config("buybot_last_block", str(head))
        print(f"[buybot] first run, starting from block {head}", flush=True)
        return [], None

    cursor = int(stored)
    # stay one block behind head: a just-landed block can still be reorged out
    target = head - 1
    if target <= cursor:
        return [], None
    # a cursor far behind head (long outage) would otherwise walk thousands of
    # pages; skip ahead and take the miss rather than spam an hour-old backlog
    max_span = MAX_LOG_RANGE * cfg.BUYBOT_MAX_PAGES_PER_TICK
    if target - cursor > max_span * 3:
        cursor = target - max_span
        print(f"[buybot] cursor far behind, resuming at block {cursor}", flush=True)

    alerts: list[dict] = []
    pages = 0
    while cursor < target and pages < cfg.BUYBOT_MAX_PAGES_PER_TICK:
        lo = cursor + 1
        hi = min(target, lo + MAX_LOG_RANGE - 1)
        logs = _rpc(
            "eth_getLogs",
            [{
                "address": cfg.BUYBOT_TOKEN,
                "fromBlock": hex(lo),
                "toBlock": hex(hi),
                "topics": [TRANSFER_TOPIC],
            }],
        ) or []
        for log in logs:
            try:
                alert = _build_alert_for(log)
            except Exception as e:  # noqa: BLE001
                # one malformed log must not abort the whole scan
                print(f"[buybot] log {log.get('transactionHash')}: {e}", flush=True)
                continue
            if alert:
                alerts.append(alert)
        cursor = hi
        pages += 1
    return alerts, cursor


async def _send_alert(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    """Posts one alert: the banner with the alert as its caption and the
    buttons underneath, in a single message.

    Telegram hands back a file_id the first time an image is uploaded, and
    accepts that id in place of the bytes afterwards. Caching it means the
    343 KB banner is uploaded once in the worker's lifetime instead of on
    every buy. A JPEG has no transparency, so unlike events.py's stickers
    there is nothing here that Telegram would flatten onto white.
    """
    text = build_alert(data)
    keyboard = build_keyboard(data["tx_hash"])
    cached_id = db.get_config("buybot_image_file_id")

    if cached_id or cfg.BUYBOT_IMAGE.is_file():
        try:
            if cached_id:
                msg = await context.bot.send_photo(
                    chat_id=cfg.BUYBOT_CHAT_ID, photo=cached_id, caption=text,
                    parse_mode="HTML", reply_markup=keyboard,
                )
            else:
                with open(cfg.BUYBOT_IMAGE, "rb") as fh:
                    msg = await context.bot.send_photo(
                        chat_id=cfg.BUYBOT_CHAT_ID, photo=fh, caption=text,
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                if msg.photo:
                    db.set_config("buybot_image_file_id", msg.photo[-1].file_id)
            return
        except Exception as e:  # noqa: BLE001
            # a stale file_id, an oversized caption, an image Telegram refuses:
            # none of it is a reason to lose the alert itself
            print(f"[buybot] photo send failed, falling back to text: {e}", flush=True)
            if cached_id:
                db.set_config("buybot_image_file_id", "")

    await context.bot.send_message(
        chat_id=cfg.BUYBOT_CHAT_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _poll_once(context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts, cursor = await asyncio.to_thread(_scan)
    for data in alerts:
        try:
            await _send_alert(context, data)
            _remember(data["_key"])
            print(
                f"[buybot] announced {fmt_usd(data['usd'])} buy by {short_addr(data['buyer'])}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            # a Telegram hiccup on one alert must not block the others, nor
            # stop the cursor from advancing past a range we already scanned
            print(f"[buybot] send failed for {data['tx_hash']}: {e}", flush=True)
    if cursor is not None:
        db.set_config("buybot_last_block", str(cursor))


async def buybot_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Polls for new buys, then reschedules itself.

    Same try/finally reschedule contract as every job in bot.py: a failure
    inside must never be able to kill the job permanently. The flag check
    returns *without* rescheduling, so a disabled buybot is fully dormant.
    """
    if not cfg.BUYBOT_ENABLED:
        return
    try:
        await _poll_once(context)
    except Exception as e:  # noqa: BLE001
        print(f"[buybot] poll failed: {e}", flush=True)
    finally:
        context.application.job_queue.run_once(buybot_job, cfg.BUYBOT_POLL_SECONDS)


def register(app) -> None:
    """Schedules the poller if the flag is on. Called from build_app()."""
    if not cfg.BUYBOT_ENABLED:
        print("[buybot] disabled -- set BUYBOT_ENABLED=1 to enable", flush=True)
        return
    app.job_queue.run_once(buybot_job, 5)
    print(
        f"[buybot] watching {cfg.BUYBOT_TOKEN} every {cfg.BUYBOT_POLL_SECONDS}s "
        f"(min {fmt_usd(cfg.BUYBOT_MIN_USD)}) -> chat {cfg.BUYBOT_CHAT_ID}",
        flush=True,
    )
