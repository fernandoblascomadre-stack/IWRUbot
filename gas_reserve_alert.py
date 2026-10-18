"""Emergency gas reserve watch (Monad mainnet, chain 143) -- private alert to the Owner.

Fernando, 2026-10-18: "si la reserva de gas de emergencia llega a un nivel critico de menos del 5% me de un aviso a mi
chat privado, indicando que tiene un 5% de su capacidad y su direccion publica en monad para realizar la recarga".

WHY THIS SHAPE
--------------
* **Capacity = the contract's hard cap**, `MAX_RESERVE_BALANCE` = 1000 MON (IWRUOpsGasReserveV2.sol), so the
  critical level is 50 MON (Fernando chose this over the 5 MON `reserveTarget`).
* **Reads `reserveBalance()`**, the reserve's own accounting, not the address's native balance: MON queued as
  `pendingWithdrawals` / `pendingExcess` is not gas the workers can draw.
* **One alert per drop, a reminder every 24 h while still low, one "recharged" message when it recovers.** State
  lives in the SQLite `config` table (Render persistent disk), so a redeploy neither re-alerts nor forgets.
* **Refill = a plain MON transfer to the address** (`receive()` = deposit + automatic worker top-up).
* Same plain JSON-RPC as buybot.py (its endpoint rotation); never imports from bot.py.
"""

import asyncio
import time

import buybot
import db
import events_config as cfg

RESERVE_ADDRESS = "0xceBC66d2DEFdd51823F52FA4880931f7bB7859A4"  # IWRUOpsGasReserveV2, Monad mainnet
CAPACITY_MON = 1000.0                                            # MAX_RESERVE_BALANCE (contract constant)
CRITICAL_FRACTION = 0.05                                         # alert below 5 % of capacity = 50 MON
CHECK_SECONDS = 15 * 60
REMIND_SECONDS = 24 * 3600
EXPLORER_URL = f"https://monadscan.com/address/{RESERVE_ADDRESS}"

_SELECTOR_RESERVE_BALANCE = "0xa10954fe"  # reserveBalance()
_STATE_KEY = "gas_reserve_alert_state"     # "ok" | "low:<unix ts of last alert>"


def read_reserve_mon() -> float:
    raw = buybot._rpc("eth_call", [{"to": RESERVE_ADDRESS, "data": _SELECTOR_RESERVE_BALANCE}, "latest"])
    return int(raw, 16) / 1e18


def _low_text(balance: float) -> str:
    pct = balance / CAPACITY_MON * 100
    return (
        "🚨 Emergency gas reserve CRITICAL\n\n"
        f"The reserve is below 5% of its capacity: {balance:.2f} MON of {CAPACITY_MON:.0f} MON ({pct:.1f}%).\n\n"
        "Refill it with a plain MON transfer on Monad (any wallet) to:\n"
        f"{RESERVE_ADDRESS}\n\n"
        f"{EXPLORER_URL}"
    )


def _recovered_text(balance: float) -> str:
    return f"✅ Emergency gas reserve recharged: {balance:.2f} MON ({balance / CAPACITY_MON * 100:.1f}% of capacity)."


def decide(balance: float, state: str, now: float):
    """Pure: (message or None, new state). Exported for tests."""
    critical = balance < CAPACITY_MON * CRITICAL_FRACTION
    if critical:
        last = float(state.split(":", 1)[1]) if state.startswith("low:") else None
        if last is None or now - last >= REMIND_SECONDS:
            return _low_text(balance), f"low:{now:.0f}"
        return None, state
    if state.startswith("low:"):
        return _recovered_text(balance), "ok"
    return None, "ok"


async def gas_reserve_job(context) -> None:
    try:
        balance = await asyncio.to_thread(read_reserve_mon)
        state = db.get_config(_STATE_KEY, "ok") or "ok"
        text, new_state = decide(balance, state, time.time())
        if text:
            await context.bot.send_message(chat_id=cfg.BOT_OWNER_ID, text=text, disable_web_page_preview=True)
            print(f"[gas-reserve] alert sent: {balance:.2f} MON", flush=True)
        if new_state != state:
            db.set_config(_STATE_KEY, new_state)  # only after the message went out: a failed send retries next check
    except Exception as e:
        print(f"[gas-reserve] check failed: {e}", flush=True)
    finally:
        context.application.job_queue.run_once(gas_reserve_job, CHECK_SECONDS)


def register(app) -> None:
    app.job_queue.run_once(gas_reserve_job, 60)
    print(f"[gas-reserve] watching {RESERVE_ADDRESS} every {CHECK_SECONDS // 60} min, "
          f"alert < {CAPACITY_MON * CRITICAL_FRACTION:.0f} MON -> Owner {cfg.BOT_OWNER_ID}", flush=True)
