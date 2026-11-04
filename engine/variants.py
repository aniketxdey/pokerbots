"""Poker variants: rule specs + hand evaluation.

Every variant is a `Variant` spec interpreted by engine/table.py. Families:
  community  - Hold'em / Omaha style (blinds, shared board)
  stud       - antes + bring-in, up/down cards, no board
  draw       - blinds, private hands, draw rounds
  mixed      - rotates between other variants every blind level (e.g. HORSE)

The official rules (§3.2) reveal the variant on the day and the Finals use a different one,
so this lets you test how a bot copes with other games.
"""
from dataclasses import dataclass, field, asdict
from itertools import combinations

from .evaluator import evaluate, _evaluate_general

RANKS = "23456789TJQKA"
SUIT_ORDER = "cdhs"  # bring-in tie-break order (clubs lowest)


@dataclass(frozen=True)
class Variant:
    id: str
    name: str
    category: str
    description: str
    game: str = "community"            # community | stud | draw | mixed
    betting: str = "no_limit"          # no_limit | pot_limit | fixed_limit
    ranks: str = RANKS                 # ranks in the deck
    hole: int = 2                      # private cards dealt (community / draw)
    streets: tuple = (0, 3, 1, 1)      # board cards dealt before each betting round
    hand: str = "any"                  # any | omaha (exactly 2 hole + 3 board) | max2 | both
    ranking: str = "standard"          # standard | short_deck | short_deck_trips
    split: str = "high"                # high | hilo8 | low_a5 | low_27 | badugi
    discards: tuple = ()               # ((round, 'pre'|'post_deal', count, to_board), ...)
    draws: int = 0                     # draw games: number of draw phases
    stud: tuple = ()                   # stud: per-round deal pattern, e.g. (('d','d','u'), ('u',), ...)
    boards: int = 1                    # 2 = double board (each pot split between boards)
    special: tuple = ()                # bounty | auction | swap | red_river | permutation
    rotation: tuple = ()               # mixed games: variant ids, switched every blind level
    big_bet_from: int = 2              # fixed limit: first betting round that uses the big bet
    legacy: str = ""                   # MIT Pokerbots year for legacy variants

    def to_dict(self):
        d = asdict(self)
        d["hole_cards"] = self.hole
        return d


V = []


def _add(**kw):
    V.append(Variant(**kw))


HE = "Hold'em family"
OM = "Omaha family"
ST = "Stud"
DR = "Draw"
MX = "Mixed games"
MIT = "MIT Pokerbots legacy variants"

# ------------------------------------------------------------------ Hold'em family
_add(id="nlhe", name="No-Limit Texas Hold'em", category=HE,
     description="The default (and the likely regional game): 2 hole cards, 5 board cards, no-limit betting.")
_add(id="plhe", name="Pot-Limit Texas Hold'em", category=HE, betting="pot_limit",
     description="Hold'em where the maximum raise is the size of the pot.")
_add(id="flhe", name="Fixed-Limit Texas Hold'em", category=HE, betting="fixed_limit",
     description="Hold'em with fixed bet sizes (small bet = big blind preflop/flop, big bet = 2x on turn/river), 4-bet cap.")
_add(id="shortdeck", name="Short Deck (6+) Hold'em", category=HE, ranks="6789TJQKA", ranking="short_deck",
     description="36-card deck (2-5 removed). Flush beats full house; A-6-7-8-9 is the lowest straight; straights beat trips.")
_add(id="shortdeck_trips", name="Short Deck (trips beat straights)", category=HE, ranks="6789TJQKA",
     ranking="short_deck_trips",
     description="Short Deck with the alternative ranking used in some rooms: flush > full house AND trips > straight.")
_add(id="royal", name="Royal Hold'em", category=HE, ranks="TJQKA",
     description="20-card deck (tens to aces only). Every flop is connected; the only straight is broadway.")
_add(id="greek", name="Greek Hold'em", category=HE, hand="both",
     description="Hold'em where you must use both hole cards (plus exactly 3 from the board).")
_add(id="pineapple", name="Pineapple", category=HE, hole=3, discards=((1, "pre", 1, False),),
     description="3 hole cards; discard 1 after the preflop betting, before the flop. Then plays as Hold'em.")
_add(id="crazy_pineapple", name="Crazy Pineapple", category=HE, hole=3, discards=((2, "pre", 1, False),),
     description="3 hole cards; discard 1 after the flop betting round.")
_add(id="lazy_pineapple", name="Lazy Pineapple", category=HE, hole=3, hand="max2",
     description="3 hole cards, no discard until showdown: your hand uses at most 2 of them.")
_add(id="three_card_holdem", name="Super Hold'em (3 hole cards)", category=HE, hole=3,
     description="3 hole cards kept to the end; best 5 of all 8 cards.")
_add(id="irish", name="Irish Poker", category=HE, hole=4, discards=((2, "pre", 2, False),),
     description="4 hole cards, Omaha-style preflop and flop; discard 2 after the flop betting, then Hold'em rules.")
_add(id="holdem_hilo", name="Hold'em Hi-Lo (8 or better)", category=HE, betting="fixed_limit", split="hilo8",
     description="Fixed-limit Hold'em; pot split between best high and best qualifying (8-or-better) A-5 low.")
_add(id="double_board", name="Double Board Hold'em", category=HE, boards=2,
     description="Two boards are dealt; each pot is split half to the best hand on each board.")

# ------------------------------------------------------------------ Omaha family
_add(id="plo", name="Pot-Limit Omaha (PLO)", category=OM, betting="pot_limit", hole=4, hand="omaha",
     description="4 hole cards; must use exactly 2 of them with exactly 3 board cards.")
_add(id="nlo", name="No-Limit Omaha", category=OM, hole=4, hand="omaha",
     description="Omaha with no-limit betting.")
_add(id="flo", name="Fixed-Limit Omaha", category=OM, betting="fixed_limit", hole=4, hand="omaha",
     description="Omaha high with fixed-limit betting.")
_add(id="plo8", name="Pot-Limit Omaha Hi-Lo (PLO8)", category=OM, betting="pot_limit", hole=4, hand="omaha",
     split="hilo8", description="PLO with the pot split between high and 8-or-better low (2+3 rule for both).")
_add(id="o8", name="Limit Omaha Hi-Lo (O8)", category=OM, betting="fixed_limit", hole=4, hand="omaha",
     split="hilo8", description="The 'O' in HORSE: fixed-limit Omaha 8-or-better.")
_add(id="plo5", name="5-Card PLO", category=OM, betting="pot_limit", hole=5, hand="omaha",
     description="Pot-limit Omaha with 5 hole cards (still exactly 2 + 3).")
_add(id="plo6", name="6-Card PLO", category=OM, betting="pot_limit", hole=6, hand="omaha",
     description="Pot-limit Omaha with 6 hole cards.")
_add(id="bigo", name="Big O (5-card PLO Hi-Lo)", category=OM, betting="pot_limit", hole=5, hand="omaha",
     split="hilo8", description="5-card pot-limit Omaha 8-or-better.")
_add(id="courchevel", name="Courchevel", category=OM, betting="pot_limit", hole=5, hand="omaha",
     streets=(1, 2, 1, 1),
     description="5-card PLO where the first flop card is dealt face up before the preflop betting.")

# ------------------------------------------------------------------ Stud
_STUD7 = (("d", "d", "u"), ("u",), ("u",), ("u",), ("d",))
_add(id="stud", name="Seven Card Stud", category=ST, game="stud", betting="fixed_limit", stud=_STUD7,
     description="Antes + bring-in; 2 down 1 up, three more up, last down. Best 5 of 7. Fixed limit.")
_add(id="stud8", name="Seven Card Stud Hi-Lo (8 or better)", category=ST, game="stud", betting="fixed_limit",
     stud=_STUD7, split="hilo8", description="Seven Card Stud with the pot split high / 8-or-better low.")
_add(id="razz", name="Razz", category=ST, game="stud", betting="fixed_limit", stud=_STUD7, split="low_a5",
     description="Seven Card Stud for the lowest A-5 hand (straights and flushes don't count, aces low).")
_add(id="five_card_stud", name="Five Card Stud", category=ST, game="stud", betting="fixed_limit",
     stud=(("d", "u"), ("u",), ("u",), ("u",)), big_bet_from=2,
     description="1 down 1 up, then three more up cards. Best 5-card hand.")

# ------------------------------------------------------------------ Draw
_add(id="five_card_draw", name="Five Card Draw", category=DR, game="draw", betting="fixed_limit", hole=5,
     streets=(0, 0), draws=1, big_bet_from=1,
     description="5 private cards, one draw (replace 0-5 cards). Best high hand.")
_add(id="nl_27_single", name="No-Limit 2-7 Single Draw", category=DR, game="draw", betting="no_limit", hole=5,
     streets=(0, 0), draws=1, split="low_27",
     description="Deuce-to-seven lowball, one draw: the worst high hand wins (aces high, straights and flushes count).")
_add(id="td_27", name="2-7 Triple Draw", category=DR, game="draw", betting="fixed_limit", hole=5,
     streets=(0, 0, 0, 0), draws=3, split="low_27",
     description="Deuce-to-seven lowball with three draws and fixed-limit betting.")
_add(id="td_a5", name="A-5 Triple Draw", category=DR, game="draw", betting="fixed_limit", hole=5,
     streets=(0, 0, 0, 0), draws=3, split="low_a5",
     description="Ace-to-five lowball (aces low, straights/flushes ignored) with three draws.")
_add(id="badugi", name="Badugi", category=DR, game="draw", betting="fixed_limit", hole=4,
     streets=(0, 0, 0, 0), draws=3, split="badugi",
     description="4-card triple draw: best hand has 4 different suits and ranks, as low as possible.")

# ------------------------------------------------------------------ Mixed
_add(id="horse", name="HORSE", category=MX, game="mixed", betting="fixed_limit",
     rotation=("flhe", "o8", "razz", "stud", "stud8"),
     description="Rotates Limit Hold'em, Omaha Hi-Lo, Razz, Stud, Stud Hi-Lo - switching every blind level.")
_add(id="eight_game", name="8-Game Mix", category=MX, game="mixed",
     rotation=("td_27", "flhe", "o8", "razz", "stud", "stud8", "nlhe", "plo"),
     description="2-7 Triple Draw, Limit Hold'em, O8, Razz, Stud, Stud8, NL Hold'em, PLO - one per blind level.")
_add(id="hold_omaha", name="Hold'em / Omaha mix", category=MX, game="mixed", rotation=("nlhe", "plo"),
     description="Alternates NL Hold'em and PLO every blind level.")

# ------------------------------------------------------------------ MIT Pokerbots legacy
_add(id="mit2020_permutation", name="2020 Permutation Hold'em", category=MIT, special=("permutation",),
     legacy="2020",
     description="Card values are secretly permuted for the whole table (MIT's geometric prior); showdowns use "
                 "the permuted ranks, so bots must learn which ranks are strong.")
_add(id="mit2022_swap", name="2022 Swap Hold'em", category=MIT, special=("swap",), legacy="2022",
     description="Before the flop each hole card is swapped with a random deck card with prob. 10%, before the "
                 "turn with prob. 5%. You see your new cards; opponents don't.")
_add(id="mit2023_red_river", name="2023 Red River Hold'em", category=MIT, special=("red_river",), legacy="2023",
     description="If the river is red (hearts/diamonds) another board card and betting round follow, repeating "
                 "while the last card is red.")
_add(id="mit2024_auction", name="2024 Auction Hold'em", category=MIT, special=("auction",), legacy="2024",
     description="Right after the flop everyone in the hand bids for an extra hole card (second-price: the winner "
                 "pays the second-highest bid into the pot; tied top bids all get a card and pay their bid).")
_add(id="mit2025_bounty", name="2025 Bounty Hold'em", category=MIT, special=("bounty",), legacy="2025",
     description="Each bot gets a secret bounty rank every 25 hands. Winning a showdown with that rank in your "
                 "hand or on the board collects 1.5x from the losers plus a 5-big-blind bonus.")
_add(id="mit2026_toss", name="2026 Toss Hold'em", category=MIT, hole=3, streets=(0, 2, 1, 1),
     discards=((1, "post_deal", 1, True),), legacy="2026",
     description="3 hole cards. After preflop a 2-card flop is dealt, then everyone discards one card face up "
                 "onto the board before the flop betting; turn and river follow.")

VARIANTS = {v.id: v for v in V}
DEFAULT_VARIANT = "nlhe"
BETTING_NAMES = {"no_limit": "No-limit", "pot_limit": "Pot-limit", "fixed_limit": "Fixed-limit"}


def get_variant(vid):
    return VARIANTS.get(vid or DEFAULT_VARIANT, VARIANTS[DEFAULT_VARIANT])


def variant_list():
    return [v.to_dict() for v in V]


# ======================================================================= evaluation
def _rank(c):
    return c >> 2


def _ranking_rules(ranking, deck_ranks):
    """(flush_over_full_house, trips_over_straight, wheel ranks) for a ranking + deck."""
    idx = sorted(RANKS.index(r) for r in deck_ranks)
    wheel = (12,) + tuple(idx[:4]) if 12 in idx and len(idx) >= 5 else None
    if ranking == "short_deck":
        return True, False, wheel
    if ranking == "short_deck_trips":
        return True, True, wheel
    return False, False, wheel


def eval5_generic(cards, flush_over_fh=False, trips_over_straight=False, wheel=(12, 0, 1, 2, 3),
                  allow_wheel=True):
    """Exactly 5 cards; returns int, higher = better. Handles custom category orders/wheels."""
    ranks = sorted((c >> 2 for c in cards), reverse=True)
    suits = {c & 3 for c in cards}
    flush = len(suits) == 1
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    distinct = len(counts) == 5
    straight_high = -1
    if distinct:
        if ranks[0] - ranks[4] == 4:
            straight_high = ranks[0]
        elif allow_wheel and wheel is not None and set(ranks) == set(wheel):
            straight_high = wheel[-1]
    # categories in standard order
    if straight_high >= 0 and flush:
        cat, kick = 8, [straight_high]
    elif groups[0][1] == 4:
        cat, kick = 7, [groups[0][0], groups[1][0]]
    elif groups[0][1] == 3 and groups[1][1] == 2:
        cat, kick = 6, [groups[0][0], groups[1][0]]
    elif flush:
        cat, kick = 5, ranks
    elif straight_high >= 0:
        cat, kick = 4, [straight_high]
    elif groups[0][1] == 3:
        cat, kick = 3, [g[0] for g in groups]
    elif groups[0][1] == 2 and groups[1][1] == 2:
        cat, kick = 2, [g[0] for g in groups]
    elif groups[0][1] == 2:
        cat, kick = 1, [g[0] for g in groups]
    else:
        cat, kick = 0, ranks
    if flush_over_fh and cat in (5, 6):
        cat = 11 - cat  # swap flush (5) and full house (6)
    if trips_over_straight and cat in (3, 4):
        cat = 7 - cat
    v = cat
    for i in range(5):
        v = (v << 4) | (kick[i] if i < len(kick) else 0)
    return v


def low_a5_5(cards):
    """A-5 low of exactly 5 cards: lower is better. Aces low; straights/flushes ignored."""
    vals = sorted((((c >> 2) + 1) % 13 + 1 for c in cards), reverse=True)  # A=1 .. K=13
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    shape = sorted(counts.values(), reverse=True)
    cat = {(1, 1, 1, 1, 1): 0, (2, 1, 1, 1): 1, (2, 2, 1): 2, (3, 1, 1): 3, (3, 2): 4, (4, 1): 5}[tuple(shape)]
    groups = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    order = [g[0] for g in groups] if cat else vals
    v = cat
    for x in order[:5]:
        v = (v << 4) | x
    for _ in range(5 - len(order)):
        v <<= 4
    return v


def low8_qualifies(lowval):
    # category 0 (no pair) and highest card <= 8
    return (lowval >> 20) == 0 and ((lowval >> 16) & 0xF) <= 8


def low_27_5(cards):
    """Deuce-to-seven: worse high hand is better. Returns value where LOWER is better."""
    return eval5_generic(cards, allow_wheel=False)


def badugi_value(cards):
    """Lower is better. 4-card hand: more distinct-suit/rank cards first, then lowest ranks (A low)."""
    best = None
    cs = list(cards)
    for size in range(min(4, len(cs)), 0, -1):
        for sub in combinations(cs, size):
            if len({c & 3 for c in sub}) != size:
                continue
            vals = [((c >> 2) + 1) % 13 + 1 for c in sub]
            if len(set(vals)) != size:
                continue
            key = sorted(vals, reverse=True)
            v = 4 - size
            for x in key:
                v = (v << 4) | x
            v <<= 4 * (4 - size)
            if best is None or v < best:
                best = v
        if best is not None:
            return best
    return 0xFFFFFFF


def _combos(variant, hole, board):
    """5-card combinations allowed by the variant's hand rule."""
    h = variant.hand
    if h == "omaha":
        for hc in combinations(hole, 2):
            for bc in combinations(board, 3):
                yield list(hc) + list(bc)
    elif h == "both":
        for bc in combinations(board, 3):
            yield list(hole[:2]) + list(bc)
    elif h == "max2":
        cards = list(hole) + list(board)
        seen = set()
        for hc in combinations(hole, min(2, len(hole))):
            for c5 in combinations(list(hc) + list(board), 5):
                k = tuple(sorted(c5))
                if k not in seen:
                    seen.add(k)
                    yield list(c5)
        if not hole:
            for c5 in combinations(cards, 5):
                yield list(c5)
    else:
        for c5 in combinations(list(hole) + list(board), 5):
            yield list(c5)


def high_value(variant, hole, board):
    """Best high hand (higher = better)."""
    if variant.ranking == "standard":
        if variant.hand == "any":
            return evaluate(list(hole) + list(board)) if len(hole) + len(board) >= 5 else \
                _evaluate_general(list(hole) + list(board))
        if variant.hand == "max2":
            best = 0
            for hc in combinations(hole, min(2, len(hole))):
                v = evaluate(list(hc) + list(board))
                if v > best:
                    best = v
            return best
        return max((evaluate(c) for c in _combos(variant, hole, board)), default=0)
    fo, to, wheel = _ranking_rules(variant.ranking, variant.ranks)
    return max((eval5_generic(c, fo, to, wheel) for c in _combos(variant, hole, board)), default=0)


def low_value(variant, hole, board):
    """Best A-5 low (lower = better)."""
    cards = list(hole) + list(board)
    if variant.hand == "any" and len(cards) >= 5:
        # fast path: 5 lowest distinct ranks (ace low) make the best no-pair low
        vals = sorted({((c >> 2) + 1) % 13 + 1 for c in cards})
        if len(vals) >= 5:
            v = 0
            for x in sorted(vals[:5], reverse=True):
                v = (v << 4) | x
            return v
    return min((low_a5_5(c) for c in _combos(variant, hole, board)), default=None)


def showdown_scores(variant, hole, board):
    """Returns (high, low) for one player. high: higher better (None if not used);
    low: lower better (None if not used / no qualifying low)."""
    s = variant.split
    if s == "high":
        return high_value(variant, hole, board), None
    if s == "hilo8":
        lo = low_value(variant, hole, board)
        if lo is not None and not low8_qualifies(lo):
            lo = None
        return high_value(variant, hole, board), lo
    if s == "low_a5":
        return None, low_value(variant, hole, board)
    if s == "low_27":
        cards = list(hole) + list(board)
        return None, min(low_27_5(c) for c in combinations(cards, 5))
    if s == "badugi":
        return None, badugi_value(list(hole))
    raise ValueError(s)


def describe(variant, hi, lo):
    from .evaluator import CATEGORY_NAMES
    parts = []
    if hi is not None:
        cat = hi >> 20
        if variant.ranking.startswith("short_deck"):
            names = list(CATEGORY_NAMES)
            names[5], names[6] = names[6], names[5]
            if variant.ranking == "short_deck_trips":
                names[3], names[4] = names[4], names[3]
            parts.append(names[cat])
        else:
            parts.append(CATEGORY_NAMES[cat])
    if lo is not None:
        if variant.split == "low_27":
            parts.append("2-7 " + CATEGORY_NAMES[lo >> 20].lower())
        elif variant.split == "badugi":
            parts.append(f"{4 - (lo >> 16)}-card badugi")
        else:
            top = (lo >> 16) & 0xF
            parts.append(f"{'A23456789TJQK'[top - 1]}-low" if (lo >> 20) == 0 else "paired low")
    return " / ".join(parts)


# ======================================================================= showing hands (stud)
def showing_rank(cards, low=False):
    """Rank face-up stud cards for who acts first. high: bigger tuple = better; low: smaller = better."""
    if low:
        vals = sorted((((c >> 2) + 1) % 13 + 1 for c in cards), reverse=True)
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        pairs = sum(1 for n in counts.values() if n > 1)
        return (pairs, vals)
    counts = {}
    for c in cards:
        counts[c >> 2] = counts.get(c >> 2, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
    return tuple((n, r) for r, n in groups)


def permutation(rng):
    """MIT 2020 value permutation (engine-2020/engine.py permute_values, geometric p=0.25 prior).
    Returns perm where perm[true_rank_index] = rank index used at showdown."""
    orig = list(range(13))[::-1]
    prop = []
    for _ in range(13):
        s = 0
        while rng.random() >= 0.25:  # numpy geometric(p) - 1 = failures before first success
            s += 1
        pop_i = len(orig) - 1 - (s % len(orig))
        prop.append(orig.pop(pop_i))
    # MIT: perm = [values[i] for i in prop]; card with value index i is treated as perm[i]
    return prop
