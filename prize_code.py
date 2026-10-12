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


if __name__ == "__main__":
    # Parity check with the server test: python prize_code.py <secret> <payload>
    print(format_code(sys.argv[1], sys.argv[2]))
