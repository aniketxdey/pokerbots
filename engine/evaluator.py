"""Fast pure-Python 5/6/7-card poker hand evaluator.

evaluate(cards) -> int, where a higher value is a better hand (same orientation
as eval7.evaluate). Cards are ints 0..51 (see engine.cards).

How it works: every multiset of ranks (5-7 cards) maps to a unique product of
primes, so non-flush hands are a single dict lookup. With at most 7 cards, if
any suit has 5+ cards the best hand is a flush or straight flush (quads and
full houses are impossible at the same time), so flushes are a lookup keyed by
that suit's 13-bit rank mask. Tables (~75k entries) are built at import time in
well under a second.
"""
from itertools import combinations_with_replacement

HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)
CATEGORY_NAMES = [
    "High Card", "Pair", "Two Pair", "Trips", "Straight",
    "Flush", "Full House", "Quads", "Straight Flush",
]

_PRIMES13 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41]
# per-card lookups (card = rank*4 + suit)
PRIME = [_PRIMES13[c >> 2] for c in range(52)]
BIT = [1 << (c >> 2) for c in range(52)]

# straight masks, best first: (mask, high rank)
_STRAIGHTS = [((0b11111 << (h - 4)), h) for h in range(12, 3, -1)]
_STRAIGHTS.append(((1 << 12) | 0b1111, 3))  # wheel A-2-3-4-5, five-high


def _value(category, kickers):
    v = category
    for i in range(5):
        v = (v << 4) | (kickers[i] if i < len(kickers) else 0)
    return v


def _straight_high(mask):
    for smask, high in _STRAIGHTS:
        if mask & smask == smask:
            return high
    return -1


def _best_nonflush(counts):
    """counts: list[13] of rank multiplicities. Returns best non-flush value."""
    ranks_desc = [r for r in range(12, -1, -1) if counts[r]]
    quads = [r for r in ranks_desc if counts[r] == 4]
    trips = [r for r in ranks_desc if counts[r] == 3]
    pairs = [r for r in ranks_desc if counts[r] == 2]
    if quads:
        q = quads[0]
        kick = [r for r in ranks_desc if r != q][:1]
        return _value(QUADS, [q] + kick)
    if trips and (len(trips) >= 2 or pairs):
        t = trips[0]
        p = max([r for r in trips[1:]] + pairs)
        return _value(FULL_HOUSE, [t, p])
    mask = 0
    for r in ranks_desc:
        mask |= 1 << r
    sh = _straight_high(mask)
    if sh >= 0:
        return _value(STRAIGHT, [sh])
    if trips:
        t = trips[0]
        return _value(TRIPS, [t] + [r for r in ranks_desc if r != t][:2])
    if len(pairs) >= 2:
        p1, p2 = pairs[0], pairs[1]
        kick = [r for r in ranks_desc if r != p1 and r != p2][:1]
        return _value(TWO_PAIR, [p1, p2] + kick)
    if pairs:
        p = pairs[0]
        return _value(PAIR, [p] + [r for r in ranks_desc if r != p][:3])
    return _value(HIGH_CARD, ranks_desc[:5])


def _build_tables():
    rank_table = {}
    for k in (5, 6, 7):
        for combo in combinations_with_replacement(range(13), k):
            counts = [0] * 13
            ok = True
            for r in combo:
                counts[r] += 1
                if counts[r] > 4:
                    ok = False
                    break
            if not ok:
                continue
            key = 1
            for r in combo:
                key *= _PRIMES13[r]
            rank_table[key] = _best_nonflush(counts)
    flush_table = {}
    for mask in range(1 << 13):
        n = bin(mask).count("1")
        if n < 5:
            continue
        sh = _straight_high(mask)
        if sh >= 0:
            flush_table[mask] = _value(STRAIGHT_FLUSH, [sh])
        else:
            top = [r for r in range(12, -1, -1) if mask >> r & 1][:5]
            flush_table[mask] = _value(FLUSH, top)
    return rank_table, flush_table


RANK_TABLE, FLUSH_TABLE = _build_tables()


def _evaluate_general(cards):
    """Any number of cards (eval7 accepts e.g. 2 or 8+ cards). Slower path."""
    counts = [0] * 13
    suits = [0, 0, 0, 0]
    for c in cards:
        counts[c >> 2] += 1
        suits[c & 3] |= BIT[c]
    best = _best_nonflush(counts) if len(cards) >= 1 else 0
    for m in suits:
        if bin(m).count("1") >= 5:
            v = FLUSH_TABLE[m]
            if v > best:
                best = v
    return best


def evaluate(cards):
    """Evaluate int cards (fast path for 5-7 cards). Higher is better."""
    n = len(cards)
    if n < 5 or n > 7:
        return _evaluate_general(cards)
    p = 1
    s0 = s1 = s2 = s3 = 0
    for c in cards:
        p *= PRIME[c]
        s = c & 3
        if s == 0:
            s0 |= BIT[c]
        elif s == 1:
            s1 |= BIT[c]
        elif s == 2:
            s2 |= BIT[c]
        else:
            s3 |= BIT[c]
    ft = FLUSH_TABLE
    if s0 in ft:
        return ft[s0]
    if s1 in ft:
        return ft[s1]
    if s2 in ft:
        return ft[s2]
    if s3 in ft:
        return ft[s3]
    return RANK_TABLE[p]


def category(value):
    """Hand category index (0=high card .. 8=straight flush)."""
    return value >> 20


def category_name(value):
    return CATEGORY_NAMES[value >> 20]
