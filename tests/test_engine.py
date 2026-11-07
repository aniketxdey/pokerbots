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


# ------------------------------------------------------------------ variants
from engine.variants import (get_variant, high_value, low_value, low8_qualifies, low_27_5,  # noqa: E402
                             badugi_value, showdown_scores, permutation, VARIANTS)


def test_omaha_must_use_exactly_two():
    plo = get_variant("plo")
    board = C("As", "Ks", "Qs", "Js", "2c")
    hole = C("Ts", "9d", "8h", "7c")
    assert category(high_value(plo, hole, board)) == 4          # straight, not a straight flush
    assert category(high_value(get_variant("nlhe"), hole[:2], board)) == 8


def test_short_deck_rankings():
    sd = get_variant("shortdeck")
    flush = high_value(sd, C("As", "8s"), C("6s", "7s", "Ks", "6d", "7d"))
    boat = high_value(sd, C("6h", "6c"), C("6s", "7s", "Ks", "7d", "Td"))
    assert flush > boat
    wheel = high_value(sd, C("Ac", "6d"), C("7h", "8s", "9c", "Kd", "Kh"))
    six_high = high_value(sd, C("Tc", "6d"), C("7h", "8s", "9c", "Kd", "Qh"))
    trips = high_value(sd, C("Kc", "Ks"), C("7h", "8s", "Kd", "Ad", "Qh"))
    assert six_high > wheel > trips                                # A-6-7-8-9 straight, beats trips
    sdt = get_variant("shortdeck_trips")
    assert high_value(sdt, C("Kc", "Ks"), C("7h", "8s", "Kd", "Ad", "Qh")) > \
        high_value(sdt, C("Ac", "6d"), C("7h", "8s", "9c", "Kd", "Kh"))


def test_low_hands():
    v = get_variant("holdem_hilo")
    wheel = low_value(v, C("Ac", "2d"), C("3h", "4s", "5c", "Kd", "Kh"))
    eight = low_value(v, C("8c", "2d"), C("3h", "4s", "6c", "Kd", "Kh"))
    nine = low_value(v, C("9c", "2d"), C("3h", "4s", "6c", "Kd", "Kh"))
    assert wheel < eight < nine and low8_qualifies(eight) and not low8_qualifies(nine)
    # deuce-to-seven: 7-5-4-3-2 is the nuts, A-2-3-4-5 is just ace high
    assert low_27_5(C("7c", "5d", "4h", "3s", "2c")) < low_27_5(C("8c", "5d", "4h", "3s", "2c"))
    assert low_27_5(C("Ac", "5d", "4h", "3s", "2c")) > low_27_5(C("Kc", "5d", "4h", "3s", "2c"))
    # badugi: any 4-card badugi beats any 3-card one
    assert badugi_value(C("Kc", "Qd", "Jh", "Ts")) < badugi_value(C("Ac", "2d", "3h", "4h"))
    assert badugi_value(C("Ac", "2d", "3h", "4s")) < badugi_value(C("Ac", "2d", "3h", "5s"))


def test_hilo_split_pot():
    top = C("Ac", "2d",   # seat 1: nut low + high card
            "Kh", "Kd",   # seat 2: kings
            "9s", "9d",   # seat 0
            "3h", "4s", "5c", "Kc", "Qh")  # board: seat1 has wheel straight (high!) and A-5 low
    agents = [Script(["fold"]), Script(["call"]), Script(["check"] * 4)]
    t = make_table(agents, [1000, 1000, 1000], top, variant="holdem_hilo")
    play_one(t)
    # seat 2 has trip kings, seat 1 a wheel straight -> seat 1 wins high AND low: scoops 40
    assert t.seats[1].stack == 1020 and t.seats[2].stack == 980


def test_pot_limit_and_fixed_limit_sizes():
    top = C("2s", "3d", "4s", "5d", "6s", "7d", "Kc", "Kh", "9s", "8c", "2h")
    s0 = Script(["fold"])
    t = make_table([s0, Script([]), Script([])], [1000, 1000, 1000], top, variant="plhe")
    play_one(t)
    assert s0.seen[0].max_raise_to == 70           # PL: 20 + (30 pot) + 20 to call
    s0 = Script(["fold"])
    t = make_table([s0, Script([]), Script([])], [1000, 1000, 1000], top, variant="flhe")
    play_one(t)
    assert s0.seen[0].min_raise_to == s0.seen[0].max_raise_to == 40


def test_stud_bring_in_and_draw():
    random.seed(3)
    for vid in ("stud", "razz", "td_27", "badugi", "mit2026_toss", "mit2024_auction"):
        cfg = Config(time_mode="off", variant=vid).validate()

        class Caller:
            def act(self, obs):
                assert len(obs.hole_cards) >= 2
                return "call"

            def discard(self, obs):
                return obs.down_cards[:obs.discard_count] if obs.game != "draw" else obs.down_cards[:1]

            def bid(self, obs):
                return obs.max_bid // 10

        slots = [BotSlot(i, f"c{i}", Caller(), BotStats(), True) for i in range(8)]
        log = []
        t = Table(slots, cfg, random.Random(7), log=log)
        res = t.play()
        assert sum(res.final_stacks) == 16000
        text = "\n".join(log)
        if vid in ("stud", "razz"):
            assert "brings in" in text
        if vid in ("td_27", "badugi"):
            assert "draws 1" in text
        if vid == "mit2026_toss":
            assert "face up to the board" in text
        if vid == "mit2024_auction":
            assert "auction bids" in text


def test_permutation_and_registry():
    p = permutation(random.Random(1))
    assert sorted(p) == list(range(13))
    assert len(VARIANTS) >= 40


def test_book_aggregates_runs_and_versions():
    import json
    import tempfile
    from engine.book import build_book
    d = tempfile.mkdtemp()

    def run(name, t, variant, order, versions):
        lb = [{"id": b, "name": b.upper(), "rank": k + 1, "weighted": 10 - k, "weighted_avg": 10 - k,
               "per_round": {}, "win_rate": 0.1, "timeout_rate": 0.0, "errors": 0, "tables": 5}
              for k, b in enumerate(order)]
        with open(os.path.join(d, name), "w") as f:
            json.dump({"started": t, "finished": t + 5, "variant": variant, "config": {"iterations": 5},
                       "leaderboard": lb, "rounds": [], "bot_versions": versions}, f)

    run("a.json", 1, "nlhe", ["x", "y", "z"], {"x": ["h1"], "y": ["h2"], "z": ["h3"]})
    run("b.json", 2, "plo", ["x", "z", "y"], {"x": ["h1"], "y": ["h2"], "z": ["h3"]})
    run("c.json", 3, "nlhe", ["y", "x", "z"], {"x": ["h1"], "y": ["h9"], "z": ["h3"]})
    book = build_book(d)
    names = [sh["name"] for sh in book["sheets"]]
    assert {"Total", "Runs", "Variants", "Matrix", "Versions", "Entries"} <= set(names)
    assert "Pot-Limit Omaha (PLO)" in names and "No-Limit Texas Hold'em" in names
    total = book["sheets"][0]["rows"]
    assert total[0]["name"] == "X" and total[0]["titles"] == 2       # best Elo first
    vers = [r for r in book["sheets"][names.index("Versions")]["rows"] if r["name"] == "Y"]
    assert sorted((r["label"], r["status"]) for r in vers) == [("v1", "legacy"), ("v2", "current")]
    assert len(book["sheets"][names.index("Variants")]["rows"]) == len(VARIANTS)


def test_discard_and_reveal_variants():
    """Discard 1 face down, reveal 1 face up (still yours), reveals simultaneous, at every timing."""
    for vid, street_cards in (("sf2026_reveal", 3), ("sf2026_reveal_preflop", 0),
                              ("sf2026_reveal_flop", 3), ("sf2026_reveal_deal", 0)):
        cfg = Config(time_mode="off", variant=vid).validate()
        seen_ups = []

        class Rev:
            def __init__(self):
                self.after = []

            def act(self, obs):
                assert len(obs.hole_cards) in (2, 3)
                if len(obs.hole_cards) == 2:
                    assert len(obs.down_cards) == 1 and len(obs.up_cards) == 1
                    self.after.append(obs)
                return "call"

            def discard(self, obs):
                assert obs.phase == "discard" and obs.discard_count == 1 and len(obs.down_cards) == 3
                return [obs.down_cards[0]]

            def reveal(self, obs):
                assert obs.phase == "reveal" and obs.reveal_count == 1 and len(obs.down_cards) == 2
                # simultaneous: nobody's reveal is visible yet when choosing
                assert not any(p.up_cards for p in obs.players)
                assert len(obs.board) == street_cards
                seen_ups.append(obs.down_cards[1])
                return [obs.down_cards[1]]

        bots = [Rev() for _ in range(8)]
        slots = [BotSlot(i, f"r{i}", b, BotStats(), True) for i, b in enumerate(bots)]
        log = []
        res = Table(slots, cfg, random.Random(11), log=log).play()
        assert sum(res.final_stacks) == 16000
        assert seen_ups and "reveals" in "\n".join(log)
        # after the reveal every player sees everyone's face-up card
        obs = next(o for b in bots for o in b.after)
        assert sum(1 for p in obs.players if p.up_cards) >= 2


def test_reveal_default_is_lowest_card_and_up_card_plays():
    from engine.cards import str_to_card as c
    cfg = Config(time_mode="off", variant="sf2026_reveal_preflop").validate()

    class Quiet:
        def act(self, obs):
            return "check" if obs.to_call == 0 else "call"

    # seat order from the button (0): 1, 0 for heads-up deals; give both players 3 cards
    t = make_table([Quiet(), Quiet()], [1000, 1000],
                   [c("As"), c("2c"), c("Ad"), c("3c"), c("Kh"), c("7d"),
                    c("Ac"), c("Ah"), c("9s"), c("4d"), c("5h")], button=0, variant="sf2026_reveal_preflop")
    play_one(t)
    for s in t.seats:
        assert len(s.hole) == 1 and len(s.up) == 1
        assert (s.up[0] >> 2) <= (s.hole[0] >> 2)  # default reveal = the lower card


def test_hand_history_records_my_view():
    cfg = Config(time_mode="off", variant="sf2026_reveal").validate()

    class Caller:
        def act(self, obs):
            return "call"

    slots = [BotSlot(i, f"c{i}", Caller(), BotStats(), True) for i in range(8)]
    t = Table(slots, cfg, random.Random(5), record_seat=3)
    t.play()
    assert t.records
    h = t.records[0]
    assert h["my_seat"] == 3 and len(h["my_cards"]) == 3
    assert {"actions", "board", "showdown", "deltas", "up_cards", "seats"} <= set(h)
    assert sum(h["deltas"]) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
