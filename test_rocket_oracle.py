"""Differential test (review 2026-10-23): a second, independent implementation of the 🚀 vote rules, written from the
plain-language rules in INFORME §20.125 (not from rocket_votes.py), plain dicts and lists, no sqlite. Both get the same
random sequences of taps, reads and clock jumps; every answer must be identical.
Run: python test_rocket_oracle.py"""
import random
import sqlite3
import sys

import rocket_votes as rv


class Oracle:
    def __init__(self):
        self.record = None      # highest count ever accepted
        self.last = None        # last read
        self.frozen = False
        self.turn = None        # dict(tg, name, until, start)
        self.line = []          # [tg, name, joined, alive] in order of joining
        self.paid = {}          # tg -> [(ts, madrid day, code)]: one paid vote per person per Madrid day
        self.n = 0

    def _holder(self, now):
        return self.turn if self.turn and now < self.turn["until"] else None

    def _drop_gone(self, now):
        self.line = [x for x in self.line if x[3] >= now - rv.QUEUE_TTL_S]

    def _first(self, now):
        if self._holder(now):
            return None
        self._drop_gone(now)
        return self.line[0][:2] if self.line else None

    def observe(self, now, count):
        if count is None:
            return None
        prev = [x for x in (self.last, self.record) if x is not None]
        self.last = count
        if prev and count - max(prev) > rv.JUMP_FREEZE and not self.frozen:
            self.frozen = True                          # rule 4: a jump freezes, pays nobody, ends the turn
            self.record = max(self.record if self.record is not None else count, count)
            self.turn = None
            return "frozen"
        if self.record is None or (not self._holder(now) and count > self.record):
            self.record = count                         # outside a turn a rise is nobody's
        return None

    def _join(self, tg, name, now):
        self._drop_gone(now)
        for x in self.line:
            if x[0] == tg:
                x[3] = now                              # still here: keeps the place
                break
        else:
            self.line.append([tg, name, now, now])
        return [x[0] for x in self.line].index(tg) + 1

    @staticmethod
    def _day(ts):
        from datetime import datetime
        return datetime.fromtimestamp(ts, tz=rv.MADRID_TZ).date().isoformat()

    def _today(self, tg, now):
        return [c for ts, d, c in self.paid.get(tg, []) if d == self._day(now)]

    def start(self, tg, name, now, count, member=True):
        if self._today(tg, now):
            return ("paid", self._today(tg, now)[0])
        if not member:
            return ("not_member",)
        if count is None:
            return ("no_data",)
        if self.observe(now, count) == "frozen" or self.frozen:
            return ("frozen",)
        h = self._holder(now)
        if h and h["tg"] != tg:
            return ("wait", self._join(tg, name, now))
        first = self._first(now)
        if not h and first and first[0] != tg:
            return ("wait", self._join(tg, name, now))
        if not h:
            self.record = max(self.record if self.record is not None else count, count)
            self.turn = {"tg": tg, "name": name, "until": now + rv.LOCK_S, "start": now}
            self.line = [x for x in self.line if x[0] != tg]
        return ("turn", float(self.turn["until"]))

    def extend(self, tg, now):
        h = self._holder(now)
        if h and h["tg"] == tg:
            h["until"] = min(h["start"] + rv.MAX_TURN_S, max(h["until"], now + rv.CLAIM_WAIT_S))

    def claim(self, tg, now, count):
        recent = [c for ts, d, c in self.paid.get(tg, []) if d == self._day(now) or ts >= now - rv.MAX_TURN_S]
        if recent:
            return ("already", recent[-1])
        h = self._holder(now)
        if not h or h["tg"] != tg:
            return ("no_turn",)
        if count is None:
            return ("no_data",)
        if self.observe(now, count) == "frozen" or self.frozen:
            return ("frozen",)
        if count <= self.record:
            return ("not_yet",)
        self.n += 1
        self.paid.setdefault(tg, []).append((now, self._day(now), f"C{tg}-{self.n}"))
        self.record = count
        self.turn = None
        return ("paid", f"C{tg}-{self.n}")

    def release(self, tg):
        if self.turn and self.turn["tg"] == tg:
            self.turn = None


def run(seed, steps=3000):
    rnd = random.Random(seed)
    n = [0]

    def code(tg):
        n[0] += 1
        return f"C{tg}-{n[0]}"
    real = rv.RocketLedger(sqlite3.connect(":memory:"), code)
    o = Oracle()
    now, count = 1_800_000_000.0, rnd.randint(0, 50)
    real.observe(now, count)
    o.observe(now, count)
    for step in range(steps):
        now += rnd.choice([0, 1, 3, 10, 30, 60, 60, 90, 90, 120, 300, 900, 2400])
        r = rnd.random()
        if r < 0.15:
            count += rnd.choice([1, 1, 1, 2, -1, 0, 12, 30])   # votes, removals, rare big jumps
            count = max(count, 0)
        read = None if rnd.random() < 0.05 else (0 if rnd.random() < 0.02 else count)  # failed / glitch-0 reads
        tg = rnd.randint(1, 12)
        name = f"u{tg}"
        op = rnd.random()
        if op < 0.3:
            member = rnd.random() > 0.05
            a, b = real.start(tg, name, now, read, member), o.start(tg, name, now, read, member)
        elif op < 0.55:
            a, b = real.claim(tg, now, read), o.claim(tg, now, read)
        elif op < 0.65:
            h = o._holder(now)
            if h and rnd.random() < 0.8:
                tg = h["tg"]
            real.extend_for_claim(tg, now)
            o.extend(tg, now)
            a = b = None
        elif op < 0.72:
            real.release(tg)
            o.release(tg)
            a = b = None
        elif op < 0.74:
            real.unfreeze()
            o.frozen = False
            a = b = None
        elif op < 0.85:
            a, b = real.next_in_queue(now), o._first(now)
            a = tuple(a) if a else None
            b = tuple(b) if b else None
        else:
            a, b = real.observe(now, read), o.observe(now, read)
        if a != b:
            return f"seed {seed} step {step}: op {op:.2f} tg {tg} read {read} -> real {a} vs oracle {b}"
        h_real = real.holder(now)
        h_o = o._holder(now)
        if (h_real[0] if h_real else None) != (h_o["tg"] if h_o else None) or real.record() != o.record \
                or real.frozen() != o.frozen:
            return f"seed {seed} step {step}: state differs (holder {h_real} vs {h_o}, record {real.record()} vs {o.record})"
    paid_real = {(r[0], r[1]) for r in real.c.execute("SELECT tg_id, day FROM rv_paid")}
    paid_o = {(tg, d) for tg, rows in o.paid.items() for _, d, _ in rows}
    if paid_real != paid_o:
        return f"seed {seed}: paid sets differ {paid_real ^ paid_o}"
    return None


if __name__ == "__main__":
    fails = [m for m in (run(s) for s in range(200)) if m]
    for m in fails[:5]:
        print("FAIL  " + m)
    print(f"oracle: 200 random runs x 3000 steps, {len(fails)} differences")
    sys.exit(1 if fails else 0)
