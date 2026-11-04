"""Engine tests:  python -m pytest -q   (or: python tests/test_engine.py)"""
import os
import random
import sys
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.cards import str_to_card  # noqa: E402
from engine.evaluator import evaluate, category  # noqa: E402
from engine.scoring import icm  # noqa: E402
from engine.sandbox import BotStats  # noqa: E402
from engine.table import Table  # noqa: E402
from engine.tournament import Config, BotSlot  # noqa: E402


def C(*cs):
    return [str_to_card(c) for c in cs]


# ------------------------------------------------------------------ evaluator
def test_five_card_category_counts():
    counts = [0] * 9
    for h in combinations(range(52), 5):
        counts[category(evaluate(h))] += 1
    assert counts == [1302540, 1098240, 123552, 54912, 10200, 5108, 3744, 624, 40]


def test_seven_card_matches_best_five():
    rng = random.Random(1)
    for _ in range(3000):
        h = rng.sample(range(52), 7)
        assert evaluate(h) == max(evaluate(c) for c in combinations(h, 5))


def test_known_orderings():
    wheel = evaluate(C("As", "2d", "3c", "4h", "5s", "Kd", "Kc"))
    six_high = evaluate(C("6s", "2d", "3c", "4h", "5s", "Kd", "Kc"))
    assert six_high > wheel and category(wheel) == 4
    assert evaluate(C("Ah", "Kh", "Qh", "Jh", "Th", "2c", "3d")) > evaluate(C("9h", "9d", "9c", "9s", "Ah", "2c", "3d"))
    # eval7 accepts 8 cards (some MIT bots rely on it)
    assert evaluate(C("Ah", "Ad", "Ac", "Kh", "Kd", "2c", "3d", "4s")) == evaluate(C("Ah", "Ad", "Ac", "Kh", "Kd", "2c", "3d"))


# ------------------------------------------------------------------ ICM
def test_icm():
    ev = icm([1000, 1000], [50, 30])
    assert abs(ev[0] - 40) < 1e-9 and abs(ev[1] - 40) < 1e-9
    ev = icm([3000, 2000, 1000], [50, 30, 20])
    assert abs(sum(ev) - 100) < 1e-9 and ev[0] > ev[1] > ev[2]
    # chip leader's ICM share is less than his chip share
    assert ev[0] < 100 * 3000 / 6000


# ------------------------------------------------------------------ table mechanics
class Script:
    """Plays a fixed list of actions, then checks/folds."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.seen = []

    def act(self, obs):
        self.seen.append(obs)
        return self.actions.pop(0) if self.actions else ("check" if obs.to_call == 0 else "fold")


class FixedDeckRng(random.Random):
    """shuffle() puts the given cards on top of the deck (deal order: left of button first)."""

    def __init__(self, top, button=0):
        super().__init__(0)
        self.top = top
        self.button = button

    def shuffle(self, x):
        rest = [c for c in x if c not in self.top]
        x[:] = list(self.top) + rest

    def choice(self, seq):
        return self.button


def make_table(agents, stacks, top, button=0, **cfg_kw):
    cfg = Config(time_mode="off", **cfg_kw).validate()
    slots = [BotSlot(i, f"b{i}", a, BotStats(), True) for i, a in enumerate(agents)]
    t = Table(slots, cfg, FixedDeckRng(top, button))
    for s, st in zip(t.seats, stacks):
        s.stack = st
    cfg.starting_stack = sum(stacks) // len(stacks)  # conservation check uses n * starting_stack
    assert cfg.starting_stack * len(stacks) == sum(stacks)
    return t


def play_one(t):
    t.hand_number = 1
    t.button = t.rng.button
    t._play_hand()


def test_side_pots():
    # seat 0 button; 3-handed: seat1 SB, seat2 BB, seat0 acts first preflop
    # deal order: 1, 2, 0
    top = C("As", "Ad",   # seat 1: aces (best)
            "Ks", "Kd",   # seat 2: kings
            "Qs", "Qd",   # seat 0: queens (worst)
            "2c", "7h", "9d", "Jc", "3s")
    agents = [Script(["all_in"]), Script(["all_in"]), Script(["call"])]
    t = make_table(agents, [600, 100, 200], top)
    play_one(t)
    # main pot 300 (100 x3) -> seat1; side pot 200 (100 from seats 0 and 2) -> seat2 (kings);
    # seat 0's extra 400 is uncalled and returned
    assert [s.stack for s in t.seats] == [400, 300, 200]


def test_split_pot():
    top = C("As", "Kd",  # seat1
            "Ah", "Kc",  # seat2
            "2s", "3d",  # seat0
            "Qs", "Qd", "Jh", "Th", "4c")  # board: both seat1/2 have broadway straight? A K Q J T yes
    agents = [Script(["fold"]), Script(["call"]), Script(["check", "check", "check", "check"])]
    t = make_table(agents, [1000, 1000, 1000], top)
    play_one(t)
    assert t.seats[1].stack == 1000 and t.seats[2].stack == 1000


def test_min_raise_and_incomplete_all_in_does_not_reopen():
    # 3-handed, button seat 0. Preflop: seat0 raises to 100 (min raise = 40), seat1 (SB)
    # shoves 150 total (incomplete raise: +50 < 80), seat2 (BB) calls 150,
    # then seat0 may only call or fold (no re-raise allowed).
    top = C("2s", "3d", "4s", "5d", "6s", "7d", "Kc", "Kh", "9s", "8c", "2h")
    s0 = Script([("raise", 100), "call"])
    s1 = Script(["all_in"])
    s2 = Script(["call"])
    t = make_table([s0, s1, s2], [1000, 150, 1100], top)
    play_one(t)
    first = s0.seen[0]
    assert first.min_raise_to == 40 and first.max_raise_to == 1000
    second = s0.seen[1]
    assert "raise" not in second.legal_actions and second.to_call == 50
    # and seat 2 facing the incomplete raise after a full raise *could* raise (hadn't acted)
    assert "raise" in s2.seen[0].legal_actions
    assert s2.seen[0].min_raise_to == 150 + 80  # last full raise was 20 -> 100 (80)


def test_heads_up_blinds_and_order():
    top = C("2s", "3d", "4s", "5d", "Kc", "Kh", "9s", "8c", "2h")
    a = Script(["call", "check", "check", "check"])
    b = Script(["check", "check", "check", "check"])
    t = make_table([a, b], [1000, 1000], top, button=0)
    play_one(t)
    # heads-up: button (seat 0) is the small blind and acts first preflop
    assert a.seen[0].street == 0 and a.seen[0].to_call == 10
    assert t._sb_seat == 0 and t._bb_seat == 1
    # postflop the big blind acts first
    assert b.seen[1].street == 3 and len(a.seen) == 4 and a.seen[1].street == 3
    assert sum(s.stack for s in t.seats) == 2000


def test_full_table_conservation_and_places():
    class Rand:
        def act(self, obs):
            r = random.random()
            if "raise" in obs.legal_actions and r < 0.3:
                return ("raise", random.randint(obs.min_raise_to, obs.max_raise_to))
            if r < 0.6:
                return "call"
            return "check" if obs.to_call == 0 else "fold"

    for seed in range(20):
        random.seed(seed)
        cfg = Config(time_mode="off").validate()
        slots = [BotSlot(i, f"r{i}", Rand(), BotStats(), True) for i in range(8)]
        t = Table(slots, cfg, random.Random(seed))
        res = t.play()
        assert sum(res.final_stacks) == 16000
        assert all(p is not None for p in res.places)
        assert abs(sum(res.points) - sum(cfg.points)) < 1e-6


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
