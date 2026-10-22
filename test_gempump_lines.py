"""The Dexscreener post also asks for Gempump votes (2026-10-22): the pool, the link and the phrase policy
(funny cat, never a price promise or "buy"), plus the exact text bot.py's twice_daily_post_job builds."""
import re
import cat_posts as cp


def test_pool_and_link():
    assert cp.GEMPUMP_LINK == "https://www.gempump.io/en/token/iwillrugu"
    assert len(cp.GEMPUMP_VOTE_LINES) >= 10
    assert len(set(cp.GEMPUMP_VOTE_LINES)) == len(cp.GEMPUMP_VOTE_LINES)
    for line in cp.GEMPUMP_VOTE_LINES:
        assert "Gempump" in line and "IWRU" in line and re.search(r"vot", line, re.I), line
        assert re.search(r"fish|sardine|tuna", line, re.I), line
        assert not re.search(r"\bbuy\b|price|moon|100x|profit|guarantee", line, re.I), line
        assert line == line.encode("utf-8").decode("utf-8") and len(line) < 200


def test_bot_composes_dex_post_with_vote_line():
    src = open("bot.py", encoding="utf-8").read()
    assert 'TWICE_DAILY_EXTRA = {"dexscreener": cat_posts.GEMPUMP_VOTE_LINES}' in src
    assert "vote IWRU → {cat_posts.GEMPUMP_LINK}" in src
    assert '{pick_phrase(extra)}' in src and 'if extra else' in src
