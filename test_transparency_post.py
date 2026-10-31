"""2026-10-31 (Fernando): the transparency post in the normal group, ~2/day, well apart, + the Locker link."""
import re
import unittest
from datetime import datetime, timedelta, timezone

import cat_posts

ORIGINAL = ("My name is I Will Rug U. I only steal fish. 🐟\n\nEver met a more transparent token? Every bot, every contract, "
            "every transaction—tracked through clear, simple UIs.\n\nEven my rugs are public. 🐈‍⬛")


class TransparencyPostTest(unittest.TestCase):
    def test_his_text_plus_six_with_the_same_meaning(self):
        L = cat_posts.TRANSPARENCY_LINES
        self.assertEqual(L[0], ORIGINAL)
        self.assertEqual(len(L), 7)
        self.assertEqual(len(set(L)), 7, "no repeats")
        for line in L:
            self.assertIn("I Will Rug U", line)          # who he is
            self.assertIn("fish", line.lower())          # only steals fish
            self.assertRegex(line.lower(), r"transparen|see-through|open|show")  # the most transparent token
            for w in ("bot", "contract", "transaction"):
                self.assertIn(w, line.lower())           # every bot, contract, transaction
            self.assertRegex(line.lower(), r"rug")       # even the rugs are public
            self.assertTrue(line.endswith("🐈‍⬛"))

    def test_phrase_policy(self):
        for line in cat_posts.TRANSPARENCY_LINES:
            self.assertNotIn("$", line, "always 'IWRU', never '$IWRU'")
            self.assertIsNone(re.search(r"\bbuy\b|moon|100x|pump|price|profit|guarantee", line.lower()), line)

    def test_locker_link(self):
        self.assertEqual(cat_posts.TOOLS_LOCKER_LINK, "https://iwillrugu.com/tools/#locker")
        src = open("bot.py", encoding="utf-8").read()
        self.assertRegex(src, r'"transparency": \("transparency_post", cat_posts\.TRANSPARENCY_LINES, f"🔐 see it all → \{cat_posts\.TOOLS_LOCKER_LINK\}"\)')

    def test_about_twice_a_day_well_apart(self):
        now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        for _ in range(200):
            d = cat_posts.next_post_delay((now - timedelta(hours=1)).isoformat(), None, now=now)
            self.assertGreaterEqual(d, 9 * 3600 - 1, "never closer than 10 h to the last one")


if __name__ == "__main__":
    unittest.main()
