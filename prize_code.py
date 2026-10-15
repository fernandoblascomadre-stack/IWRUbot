"""PARADOX card prize codes for the Catch Event (2026-10-12).

The winner gets a one-time code and redeems it in PARADOX for one random card
of the rarity written in the code. Twin of iwillrugu's
netlify/functions/_catch-prize-code.mjs, which checks what this file signs:
change both together. Both sides read the same CATCH_PRIZE_SECRET.

Code = 20 Crockford base32 chars, shown as IWRU-XXXXX-XXXXX-XXXXX-XXXXX:
  payload (10) = rarity digit (1 common, 2 rare, 3 epic, 4 legendary) + 9 random chars
  sig     (10) = HMAC-SHA256(secret, 'iwru-catch-prize:v1:' + payload), byte i -> ALPHABET[byte % 32]
"""
import hashlib
import hmac
import secrets
import sys

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RARITY_DIGIT = {"common": "1", "rare": "2", "epic": "3", "legendary": "4"}
_SIG_PREFIX = "iwru-catch-prize:v1:"


def signature(secret: str, payload: str) -> str:
    d = hmac.new(secret.encode(), (_SIG_PREFIX + payload).encode(), hashlib.sha256).digest()
    return "".join(ALPHABET[b % 32] for b in d[:10])


def format_code(secret: str, payload: str) -> str:
    c = payload + signature(secret, payload)
    return "IWRU-" + "-".join(c[i:i + 5] for i in range(0, 20, 5))


def new_code(secret: str, rarity: str) -> str:
    """A fresh code for one card of `rarity` (common/rare/epic/legendary)."""
    payload = RARITY_DIGIT[rarity] + "".join(secrets.choice(ALPHABET) for _ in range(9))
    return format_code(secret, payload)


def _normalize(code: str):
    s = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if len(s) == 24 and s.startswith("IWRU"):
        s = s[4:]
    return s if len(s) == 20 and all(ch in ALPHABET for ch in s) else None


def verifies(secret: str, code: str) -> bool:
    """True when `code` is signed with `secret` (the server's verifyPrizeCode)."""
    s = _normalize(code)
    return bool(s and secret) and hmac.compare_digest(signature(secret, s[:10]), s[10:])


# 2026-10-15: until then the bot signed with the MASKED value Netlify shows for a
# secret variable, so no code verified on the website. Two tools so it can't
# happen silently again and the codes already sent stay valid (server side:
# _catch-prize-code.mjs CHECK_DIGIT / verifyRegistration):
CHECK_DIGIT = "0"  # a payload starting with 0 is never redeemable: only for the secret check
_REG_PREFIX = "iwru-catch-prize:register:v1:"


def check_code(secret: str) -> str:
    """A never-redeemable code the server answers 'valid' for only if it has the same secret."""
    return format_code(secret, CHECK_DIGIT + "".join(secrets.choice(ALPHABET) for _ in range(9)))


def registration_signature(secret: str, ts: str, codes: list) -> str:
    return hmac.new(secret.encode(), (_REG_PREFIX + ts + ":" + ",".join(codes)).encode(), hashlib.sha256).hexdigest()


if __name__ == "__main__":
    # Parity check with the server test: python prize_code.py <secret> <payload>
    print(format_code(sys.argv[1], sys.argv[2]))
