"""SF Contender - a bot for the No Limit regional's "discard & reveal" variant (and plain NLHE).

Built for the official constraints: 1 ms per decision in Python, no network, state reset per
table, 8-bot sit-and-go scored by finishing place. Everything expensive is precomputed:
preflop equities for every 2-card and 3-card hand class are embedded as literals at the bottom
of this file (built offline by tools/build_sf_tables.py), and the hand evaluator is a
prime-product / flush-mask lookup.

How it plays
  * Preflop: percentile of the hand (3-card percentile before the discard), position, stack
    depth. Opens tight, push/fold at 12 big blinds or less (Nash-style ranges, tighter on the
    money bubble), calls shoves by equity against the shover's estimated range, with ICM when
    5 or fewer players are left.
  * Postflop: a time-boxed Monte Carlo equity against every live opponent, each sampled from a
    range: preflop action (raise / call / check) narrows it, the opponent's observed raise and
    shove frequencies widen it again for maniacs, and postflop bets narrow it further.
  * Reveals (the variant's key feature):
      - opponents' face-up cards are hard information: their hidden card is sampled only from
        cards consistent with their range, and if an opponent tends to show their LOWER card
        (the engine default and the obvious LLM choice) the hidden card is sampled above it.
      - our own reveal shows the card that makes our hand look weakest to a bot that averages
        over our hidden card (the winner's "reveal the least information"), with a little
        randomness so it is not a perfect tell.
  * Discard: keeps the pair of cards with the highest equity (common random numbers).
  * Bluffs rarely (heads-up continuation bets against opponents who fold too much).

Robustness: every decision is wrapped in try/except with a legal fallback, Monte Carlo loops
stop on a wall-clock deadline well inside 1 ms, and unsupported variants get a safe
check/fold strategy.
"""
BOT_NAME = "SF Contender"
BOT_AUTHOR = "aniketxdey (with Claude)"
BOT_SOURCE = ""
BOT_DESCRIPTION = ("Discard & reveal specialist: precomputed 2/3-card equities, range-based Monte Carlo that "
                   "reads opponents' revealed cards, least-information reveals, push/fold + ICM.")
BOT_LICENSE = "MIT"

import random
import sys
import time
import types
from itertools import combinations_with_replacement

_now = time.perf_counter
_rand = random.random
_choice = random.choice
_sample = random.sample

RANKS = "23456789TJQKA"
SUITS = "cdhs"
C2I = {r + s: i * 4 + j for i, r in enumerate(RANKS) for j, s in enumerate(SUITS)}
I2C = {v: k for k, v in C2I.items()}

# time budget per decision (seconds). The official limit is 1 ms; stay well inside it.
MC_BUDGET = 0.00034
REVEAL_BUDGET = 0.00030


def _ci(card):
    if isinstance(card, int):
        return card
    return C2I[card[0].upper() + card[1].lower()]


def _cards(lst):
    return [_ci(c) for c in (lst or [])]


# ============================================================ static tables (built once per process)
_PRIMES13 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41]


def _build_static():
    straights = [((0b11111 << (h - 4)), h) for h in range(12, 3, -1)] + [((1 << 12) | 0b1111, 3)]

    def value(cat, k):
        v = cat
        for i in range(5):
            v = (v << 4) | (k[i] if i < len(k) else 0)
        return v

    def sh(mask):
        for m, h in straights:
            if mask & m == m:
                return h
        return -1

    def nonflush(counts):
        rd = [r for r in range(12, -1, -1) if counts[r]]
        quads = [r for r in rd if counts[r] == 4]
        trips = [r for r in rd if counts[r] == 3]
        pairs = [r for r in rd if counts[r] == 2]
        if quads:
            return value(7, [quads[0]] + [r for r in rd if r != quads[0]][:1])
        if trips and (len(trips) >= 2 or pairs):
            return value(6, [trips[0], max(trips[1:] + pairs)])
        mask = 0
        for r in rd:
            mask |= 1 << r
        s = sh(mask)
        if s >= 0:
            return value(4, [s])
        if trips:
            return value(3, [trips[0]] + [r for r in rd if r != trips[0]][:2])
        if len(pairs) >= 2:
            return value(2, pairs[:2] + [r for r in rd if r not in pairs[:2]][:1])
        if pairs:
            return value(1, [pairs[0]] + [r for r in rd if r != pairs[0]][:3])
        return value(0, rd[:5])

    rank_table = {}
    for k in (5, 6, 7):
        for combo in combinations_with_replacement(range(13), k):
            counts = [0] * 13
            bad = False
            key = 1
            for r in combo:
                counts[r] += 1
                key *= _PRIMES13[r]
                if counts[r] > 4:
                    bad = True
            if not bad:
                rank_table[key] = nonflush(counts)
    flush_table = {}
    for mask in range(1 << 13):
        if bin(mask).count("1") < 5:
            continue
        s = sh(mask)
        flush_table[mask] = value(8, [s]) if s >= 0 else value(5, [r for r in range(12, -1, -1) if mask >> r & 1][:5])
    m = types.ModuleType("_sfc_static_v1")
    m.RANK = rank_table
    m.FLUSH = flush_table
    m.nonflush = nonflush
    return m


# Deterministic lookup tables are cached per process (a pure speed cache: identical to rebuilding
# them, carries no information between tables). Falls back to building them if the cache is absent.
_S = sys.modules.get("_sfc_static_v1")
if _S is None:
    _S = _build_static()
    sys.modules["_sfc_static_v1"] = _S
_RANK = _S.RANK
_FLUSH = _S.FLUSH
_PRIME = [_PRIMES13[c >> 2] for c in range(52)]
_BIT = [1 << (c >> 2) for c in range(52)]


def ev(cards):
    """5-7 card hand value, higher is better."""
    p = 1
    s0 = s1 = s2 = s3 = 0
    for c in cards:
        p *= _PRIME[c]
        s = c & 3
        if s == 0:
            s0 |= _BIT[c]
        elif s == 1:
            s1 |= _BIT[c]
        elif s == 2:
            s2 |= _BIT[c]
        else:
            s3 |= _BIT[c]
    ft = _FLUSH
    if s0 in ft:
        return ft[s0]
    if s1 in ft:
        return ft[s1]
    if s2 in ft:
        return ft[s2]
    if s3 in ft:
        return ft[s3]
    return _RANK[p]


def ev_any(cards):
    """Any number of cards (slow path, for <5 cards)."""
    if 5 <= len(cards) <= 7:
        return ev(cards)
    counts = [0] * 13
    for c in cards:
        counts[c >> 2] += 1
    return _S.nonflush(counts) if cards else 0


def best_of(hole, board):
    """Value of a hand that plays at most 2 of its hole cards (2 or 3 hole cards)."""
    if len(hole) == 2:
        return ev(hole + board)
    a, b, c = hole[0], hole[1], hole[2]
    e1 = ev([a, b] + board)
    e2 = ev([a, c] + board)
    e3 = ev([b, c] + board)
    return e1 if (e1 >= e2 and e1 >= e3) else (e2 if e2 >= e3 else e3)


# ============================================================ preflop tables
def key2(a, b):
    ra, rb = a >> 2, b >> 2
    return (ra, rb, int((a & 3) == (b & 3) and ra != rb)) if ra >= rb else (rb, ra, int((a & 3) == (b & 3)))


_PERMS = [(0, 1, 2, 3), (0, 1, 3, 2), (0, 2, 1, 3), (0, 2, 3, 1), (0, 3, 1, 2), (0, 3, 2, 1),
          (1, 0, 2, 3), (1, 0, 3, 2), (1, 2, 0, 3), (1, 2, 3, 0), (1, 3, 0, 2), (1, 3, 2, 0),
          (2, 0, 1, 3), (2, 0, 3, 1), (2, 1, 0, 3), (2, 1, 3, 0), (2, 3, 0, 1), (2, 3, 1, 0),
          (3, 0, 1, 2), (3, 0, 2, 1), (3, 1, 0, 2), (3, 1, 2, 0), (3, 2, 0, 1), (3, 2, 1, 0)]


def key3(cards):
    best = None
    a, b, c = cards
    ra, rb, rc = (a >> 2) * 4, (b >> 2) * 4, (c >> 2) * 4
    sa, sb, sc = a & 3, b & 3, c & 3
    for p in _PERMS:
        x, y, z = ra + p[sa], rb + p[sb], rc + p[sc]
        if x < y:
            x, y = y, x
        if y < z:
            y, z = z, y
            if x < y:
                x, y = y, x
        k = x * 2704 + y * 52 + z
        if best is None or k < best:
            best = k
    return best


# ============================================================ opponent model
class Prof:
    __slots__ = ("pf_obs", "vpip", "pfr", "shove", "post_bets", "post_acts", "faced", "folded_to_bet",
                 "sd_n", "sd_pct", "rev_n", "rev_low")

    def __init__(self):
        self.pf_obs = 0        # preflop decisions of theirs we observed
        self.vpip = 0
        self.pfr = 0
        self.shove = 0
        self.post_bets = 0     # postflop bets/raises
        self.post_acts = 0     # postflop actions
        self.faced = 0         # times facing a postflop bet
        self.folded_to_bet = 0
        self.sd_n = 0          # showdowns seen
        self.sd_pct = 0.0      # sum of their kept hand's preflop percentile at showdown
        self.rev_n = 0         # showdowns where we saw their reveal
        self.rev_low = 0       # ... and the revealed card was their lower card

    # smoothed rates (priors chosen for a field of AI-written bots)
    def r_vpip(self):
        return (self.vpip + 1.4) / (self.pf_obs + 4.0)

    def r_pfr(self):
        return (self.pfr + 0.6) / (self.pf_obs + 4.0)

    def r_shove(self):
        return (self.shove + 0.3) / (self.pf_obs + 4.0)

    def r_aggr(self):
        return (self.post_bets + 1.0) / (self.post_acts + 3.0)

    def r_fold_to_bet(self):
        return (self.folded_to_bet + 1.5) / (self.faced + 3.0)

    def p_reveal_low(self):
        return (self.rev_low + 1.2) / (self.rev_n + 2.0)

    def looseness(self):
        """>1 when they show down weak hands (wider ranges than their action suggests)."""
        mean = (self.sd_pct + 0.62 * 3) / (self.sd_n + 3)
        return min(2.2, max(0.75, 1.0 + 2.2 * (0.62 - mean)))


# ============================================================ helpers
def icm(stacks, pay):
    """Malmuth-Harville ICM equity for each stack (pay = payout per place, best first)."""
    n = len(stacks)
    out = [0.0] * n
    depth_max = min(len(pay), n)

    def rec(total, excl, prob, depth):
        if depth >= depth_max or total <= 0:
            return
        pd = pay[depth]
        for i in range(n):
            if excl >> i & 1:
                continue
            si = stacks[i]
            if si <= 0:
                continue
            p = prob * si / total
            out[i] += p * pd
            if depth + 1 < depth_max:
                rec(total - si, excl | (1 << i), p, depth + 1)

    rec(float(sum(stacks)), 0, 1.0, 0)
    return out


OPEN_F = {0: 0.30, 1: 0.40, 2: 0.30, 3: 0.24, 4: 0.19, 5: 0.16, 6: 0.14, 7: 0.12, 8: 0.11, 9: 0.10}
PUSH_F = {0: 0.70, 1: 0.58, 2: 0.42, 3: 0.32, 4: 0.26, 5: 0.21, 6: 0.18, 7: 0.16, 8: 0.15, 9: 0.14}


class Agent:
    def __init__(self):
        self.seat = None
        self.nplayers = 8
        self.pay = [50.0, 30.0, 20.0]
        self.prof = {}
        self.hand_no = -1
        self.hidx = 0
        self.pf_state = {}
        self.street_bets = {}
        self.cur_street = -1
        self.street_max = 0
        self.post_aggr = {}     # seat -> number of postflop bets/raises this hand
        self.pf_raisers = []
        self.pf_callers = set()
        self.my_pf_raise = False
        self.last_obs = None
        self.ups_seen = {}      # seat -> up cards this hand (as ints)
        self.discard_plan = None

    # ------------------------------------------------------------ callbacks
    def new_table(self, info):
        try:
            self.seat = info.get("seat")
            self.nplayers = info.get("num_players", 8)
            pts = [float(x) for x in info.get("points", [50, 30, 20])]
            while pts and pts[-1] == 0:
                pts.pop()
            self.pay = pts or [1.0]
        except Exception:
            pass

    def new_hand(self, obs):
        try:
            self._reset_hand(obs)
        except Exception:
            pass

    def _reset_hand(self, obs):
        self.hand_no = obs.hand_number
        self.hidx = 0
        self.pf_state = {}
        self.street_bets = {}
        self.cur_street = -1
        self.street_max = 0
        self.post_aggr = {}
        self.pf_raisers = []
        self.pf_callers = set()
        self.my_pf_raise = False
        self.ups_seen = {}
        self.discard_plan = None
        if self.seat is None:
            self.seat = obs.seat

    def hand_over(self, result):
        try:
            obs = self.last_obs
            if obs is None or result.get("hand_number") != self.hand_no:
                return
            me = result.my_seat
            board = _cards(result.board)
            for k, shown in (result.showdown or {}).items():
                if k == me:
                    continue
                cards = _cards(shown)
                p = self.prof.setdefault(k, Prof())
                if len(cards) >= 2:
                    pr = self._kept_pct(cards, board)
                    p.sd_n += 1
                    p.sd_pct += pr
                ups = self.ups_seen.get(k)
                if ups and len(cards) == 2:
                    u = ups[0]
                    other = cards[0] if cards[1] == u else cards[1]
                    if other != u:
                        p.rev_n += 1
                        if (u >> 2) < (other >> 2) or ((u >> 2) == (other >> 2) and _rand() < 0.5):
                            p.rev_low += 1
        except Exception:
            pass

    def _kept_pct(self, cards, board):
        if len(cards) == 2:
            return PCT2.get(key2(cards[0], cards[1]), 0.5)
        best = 0.0
        for i in range(len(cards)):
            for j in range(i + 1, len(cards)):
                best = max(best, PCT2.get(key2(cards[i], cards[j]), 0.5))
        return best

    def bid(self, obs):
        return 0

    # ------------------------------------------------------------ history parsing
    def _scan(self, obs):
        if obs.hand_number != self.hand_no:
            self._reset_hand(obs)
        hist = obs.history
        me = obs.seat
        for idx in range(self.hidx, len(hist)):
            seat, act, amt, street = hist[idx]
            if street != self.cur_street:
                self.cur_street = street
                self.street_bets = {}
                self.street_max = 0
            if act in ("small_blind", "big_blind", "bring_in"):
                self.street_bets[seat] = amt
                self.street_max = max(self.street_max, amt)
                continue
            if act not in ("fold", "check", "call", "raise"):
                continue
            facing = self.street_max > self.street_bets.get(seat, 0)
            if act in ("call", "raise"):
                self.street_bets[seat] = amt
            if act == "raise":
                self.street_max = max(self.street_max, amt)
            if seat == me:
                if street == 0 and act == "raise":
                    self.my_pf_raise = True
                continue
            p = self.prof.get(seat)
            if p is None:
                p = self.prof[seat] = Prof()
            if street == 0:
                st = self.pf_state.get(seat)
                if st is None:
                    st = self.pf_state[seat] = [0, 0, 0]  # observed, vpip, pfr
                    p.pf_obs += 1
                if act in ("call", "raise") and not st[1]:
                    st[1] = 1
                    p.vpip += 1
                if act == "raise":
                    if not st[2]:
                        st[2] = 1
                        p.pfr += 1
                    x = obs.players[seat]
                    if x.all_in or amt >= 0.6 * (x.stack + x.contributed):
                        p.shove += 1
                    self.pf_raisers.append(seat)
                elif act == "call":
                    self.pf_callers.add(seat)
            else:
                p.post_acts += 1
                if act == "raise":
                    p.post_bets += 1
                    self.post_aggr[seat] = self.post_aggr.get(seat, 0) + 1
                if facing:
                    p.faced += 1
                    if act == "fold":
                        p.folded_to_bet += 1
        self.hidx = len(hist)
        for x in obs.players:
            if x.seat != me and x.up_cards:
                self.ups_seen[x.seat] = _cards(x.up_cards)

    # ------------------------------------------------------------ ranges
    def _range_f(self, seat, obs):
        """Fraction of hands (top-f) an opponent holds, given this hand's actions."""
        p = self.prof.get(seat) or Prof()
        loose = p.looseness()
        if seat in self.pf_raisers:
            x = obs.players[seat]
            shoved = x.all_in and obs.street == 0
            f = (p.r_shove() * 1.6 if shoved else p.r_pfr() * 1.25)
            f = max(f, 0.05)
            if self.pf_raisers.count(seat) >= 2:
                f *= 0.6
        elif seat in self.pf_callers:
            f = max(0.12, p.r_vpip() * 1.1)
        else:
            f = 1.0  # checked the big blind / never voluntarily entered
        f *= loose
        k = self.post_aggr.get(seat, 0)
        if k:
            g = min(1.0, max(0.4, p.r_aggr() * 1.6))
            f *= g ** k
        return min(1.0, max(0.03, f))

    def _opps(self, obs, dead, ncards_hidden_default):
        """Build Monte Carlo opponent specs for every live opponent."""
        out = []
        for x in obs.players:
            if x.seat == obs.seat or not x.in_hand:
                continue
            f = self._range_f(x.seat, obs)
            ups = _cards(x.up_cards)
            if ups:
                u = ups[0]
                thr = 1.0 - f
                low = self.prof.get(x.seat).p_reveal_low() if x.seat in self.prof else 0.6
                allowed, high = [], []
                ru = u >> 2
                for c in range(52):
                    if c in dead:
                        continue
                    if PCT2[key2(u, c)] >= thr:
                        allowed.append(c)
                        if (c >> 2) >= ru:
                            high.append(c)
                if len(allowed) < 3:
                    allowed = [c for c in range(52) if c not in dead]
                    high = [c for c in allowed if (c >> 2) >= ru]
                if len(high) < 2:
                    high = allowed
                out.append((ups, 1, allowed, high, low, 0))
            else:
                cut = max(25, int(f * 1326))
                out.append(([], ncards_hidden_default, None, None, 0.0, cut))
        return out

    # ------------------------------------------------------------ Monte Carlo equity
    def _mc(self, hero, board, opps, dead, deadline, nmax=300):
        deck = [c for c in range(52) if c not in dead]
        need = 5 - len(board)
        R2 = RANGE2
        wins = 0.0
        n = 0
        while n < nmax:
            if n >= 12 and (n & 3) == 0 and _now() > deadline:
                break
            used = set()
            hands = []
            for (known, nh, allowed, high, low, cut) in opps:
                if nh == 1:
                    lst = high if (_rand() < low) else allowed
                    x = _choice(lst)
                    if x in used:
                        x = _choice(allowed)
                        if x in used:
                            for x in deck:
                                if x not in used:
                                    break
                    used.add(x)
                    hands.append(known + [x])
                else:
                    a = b = -1
                    for _t in range(8):
                        a, b = R2[int(_rand() * cut)]
                        if a not in dead and b not in dead and a not in used and b not in used:
                            break
                    else:
                        a, b = [c for c in _sample(deck, len(used) + 2) if c not in used][:2]
                    used.add(a)
                    used.add(b)
                    h = [a, b]
                    if nh == 3:
                        for c in _sample(deck, len(used) + 1):
                            if c not in used:
                                used.add(c)
                                h.append(c)
                                break
                    hands.append(h)
            if need:
                run = [c for c in _sample(deck, need + len(used)) if c not in used][:need]
                bd = board + run
            else:
                bd = board
            hv = best_of(hero, bd)
            ties = 0
            lost = False
            for h in hands:
                ov = best_of(h, bd)
                if ov > hv:
                    lost = True
                    break
                if ov == hv:
                    ties += 1
            if not lost:
                wins += 1.0 / (ties + 1)
            n += 1
        return (wins / n) if n else 0.0, n

    def _equity(self, obs, hero, board, deadline):
        dead = set(hero) | set(board)
        for x in obs.players:
            for c in _cards(x.up_cards):
                dead.add(c)
        nh = 3 if len(hero) == 3 else 2
        opps = self._opps(obs, dead, nh)
        if not opps:
            return 1.0
        e, n = self._mc(hero, board, opps, dead, deadline)
        if n < 12:  # out of time: fall back to the table estimate
            return self._table_equity(hero, len(opps))
        return e

    def _table_equity(self, hero, nopp):
        nopp = max(1, min(7, nopp))
        if len(hero) == 3:
            row = PF3.get(key3(hero))
            if row:
                idx = {1: 0, 2: 1, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4}[nopp]
                return row[idx]
            return 0.3
        if len(hero) == 2:
            row = PF2.get(key2(hero[0], hero[1]))
            if row:
                return row[nopp - 1]
        return 0.3

    def _pct(self, hero):
        if len(hero) == 3:
            return PCT3.get(key3(hero), 0.5)
        if len(hero) == 2:
            return PCT2.get(key2(hero[0], hero[1]), 0.5)
        return 0.5

    # ------------------------------------------------------------ actions
    @staticmethod
    def _safe(obs):
        return "check" if "check" in obs.legal_actions else "fold"

    @staticmethod
    def _raise_to(obs, amount):
        if "raise" not in obs.legal_actions or obs.min_raise_to is None:
            return "call" if "call" in obs.legal_actions else "check"
        amt = int(max(obs.min_raise_to, min(obs.max_raise_to, amount)))
        # if the raise commits most of the stack, just shove
        if amt >= obs.max_raise_to * 0.7:
            amt = obs.max_raise_to
        return ("raise", amt)

    def act(self, obs):
        t0 = _now()
        self.last_obs = obs
        try:
            self._scan(obs)
            if obs.game != "community" or obs.hand_rule not in ("any", "max2") or len(obs.deck_ranks) != 13 \
                    or obs.split != "high":
                return self._fallback(obs)
            hero = _cards(obs.hole_cards)
            board = _cards(obs.board)
            if len(hero) not in (2, 3):
                return self._fallback(obs)
            if not board:
                return self._preflop(obs, hero, t0)
            return self._postflop(obs, hero, board, t0)
        except Exception:
            return self._safe(obs)

    def _fallback(self, obs):
        if obs.to_call == 0:
            return "check"
        if obs.to_call <= obs.big_blind and obs.to_call < 0.05 * obs.my_stack:
            return "call"
        return "fold"

    # ------------------------------------------------------------ preflop
    def _preflop(self, obs, hero, t0):
        bb = obs.big_blind
        me = obs.seat
        players = obs.players
        stack_total = obs.my_stack + obs.my_bet
        others = [x for x in players if x.seat != me and x.in_hand]
        eff = min(stack_total, max([x.stack + x.bet for x in others] or [stack_total]))
        M = eff / float(bb)
        s = self._pct(hero)
        alive = sum(1 for x in players if not x.eliminated)
        raisers = [r for r in self.pf_raisers]
        # players who still have to act behind us (have not acted voluntarily, still active)
        acted = set(self.pf_state)
        behind = [x for x in others if x.seat not in acted and not x.all_in]
        k = len(behind)
        limpers = len(self.pf_callers) if not raisers else 0
        bubble = alive <= len(self.pay) + 2 and alive > len(self.pay)
        to_call = obs.to_call
        pot = obs.pot

        if not raisers:
            if to_call == 0:  # big blind, unraised
                if s >= 0.86 and "raise" in obs.legal_actions:
                    if M <= 12:
                        return self._raise_to(obs, obs.max_raise_to)
                    return self._raise_to(obs, obs.current_bet + (3 + limpers) * bb)
                return "check"
            if M <= 12:
                f = PUSH_F.get(k, 0.14) * (10.0 / max(M, 2.0)) ** 0.6
                f *= 0.8 ** limpers
                if bubble and M > 5:
                    f *= 0.75
                if s >= 1.0 - min(0.95, f):
                    return self._raise_to(obs, obs.max_raise_to)
                return self._safe(obs)
            f = OPEN_F.get(k, 0.11) * (0.8 ** limpers)
            if bubble:
                f *= 0.85
            if s >= 1.0 - f:
                size = (2.5 + limpers) * bb
                if k == 1:  # small blind vs big blind
                    size = 3 * bb
                return self._raise_to(obs, max(size, obs.min_raise_to or 0))
            # cheap completes from the small blind with decent hands
            if k == 1 and to_call <= bb // 2 and s >= 0.55:
                return "call"
            return self._safe(obs)

        # ---- facing a raise
        deadline = t0 + MC_BUDGET
        e = self._equity(obs, hero, [], deadline)
        allin_call = to_call >= obs.my_stack * 0.85
        pot_odds = to_call / float(pot + to_call)
        nopp = len([x for x in others if x.seat in self.pf_raisers or x.seat in self.pf_callers])
        if allin_call or (any(players[r].all_in for r in raisers)):
            return "call" if self._call_allin_ok(obs, e, alive) else "fold"
        strong = s >= 0.94 or (e >= 0.62 and nopp <= 1) or e >= 0.50 + 0.05 * max(0, nopp - 1) + 0.12
        if strong and "raise" in obs.legal_actions:
            if M <= 25:
                return self._raise_to(obs, obs.max_raise_to)
            return self._raise_to(obs, obs.current_bet * 3 + obs.pot * 0.3)
        margin = 0.06 + 0.02 * max(0, nopp - 1)
        if e >= pot_odds + margin and to_call <= 0.2 * stack_total:
            return "call"
        return "fold"

    def _call_allin_ok(self, obs, e, alive):
        """Call (or shove over) when the equity beats the price, using ICM late in the game."""
        me = obs.seat
        to_call = obs.to_call
        pot = obs.pot
        players = obs.players
        risk = min(to_call, obs.my_stack)
        if alive <= 5 and len(self.pay) >= 1 and alive > 1:
            try:
                live = [x for x in players if not x.eliminated]
                idx = {x.seat: i for i, x in enumerate(live)}
                base = [x.stack + x.contributed for x in live]
                contrib = [x.contributed for x in live]
                pay = (self.pay + [0.0] * 8)[:alive]
                # crude two-way split: the biggest live bettor is "the villain"
                vill = max((x for x in live if x.seat != me and not x.folded and x.in_hand),
                           key=lambda x: x.contributed, default=None)
                if vill is not None:
                    i, j = idx[me], idx[vill.seat]
                    at_risk = min(base[i], base[j])
                    dead = pot - contrib[i] - contrib[j]
                    win = [b - c for b, c in zip(base, contrib)]
                    win[i] = base[i] + at_risk + dead
                    win[j] = base[j] - at_risk
                    lose = [b - c for b, c in zip(base, contrib)]
                    lose[i] = base[i] - at_risk
                    lose[j] = base[j] + at_risk + dead
                    fold = [b - c for b, c in zip(base, contrib)]
                    fold[j] = base[j] - contrib[j] + pot
                    fold[i] = base[i] - contrib[i]
                    ew = icm(win, pay)[i]
                    el = icm(lose, pay)[i]
                    ef = icm(fold, pay)[i]
                    return e * ew + (1 - e) * el > ef
            except Exception:
                pass
        pot_odds = risk / float(pot + risk)
        return e >= pot_odds + 0.03

    # ------------------------------------------------------------ postflop
    def _postflop(self, obs, hero, board, t0):
        deadline = t0 + MC_BUDGET
        e = self._equity(obs, hero, board, deadline)
        me = obs.seat
        others = [x for x in obs.players if x.seat != me and x.in_hand]
        nopp = len(others)
        to_call = obs.to_call
        pot = obs.pot
        stack = obs.my_stack
        alive = sum(1 for x in obs.players if not x.eliminated)
        value_thr = {1: 0.60, 2: 0.48, 3: 0.40}.get(nopp, 0.34)
        river = len(board) >= 5
        if to_call == 0:
            if e >= value_thr and "raise" in obs.legal_actions:
                size = 0.65 * pot if not river else 0.7 * pot
                if stack <= 1.3 * pot or e >= 0.85:
                    size = max(size, 0.9 * pot)
                return self._raise_to(obs, obs.my_bet + size)
            # rare, cheap continuation bets heads-up against frequent folders
            if nopp == 1 and self.my_pf_raise and len(board) == 3 and "raise" in obs.legal_actions:
                p = self.prof.get(others[0].seat)
                if p is not None and p.faced >= 6 and p.r_fold_to_bet() >= 0.6 and e >= 0.25 and _rand() < 0.5:
                    return self._raise_to(obs, obs.my_bet + 0.5 * pot)
            return "check"
        pot_odds = to_call / float(pot + to_call)
        allin = to_call >= stack * 0.85
        if allin:
            return "call" if self._call_allin_ok(obs, e, alive) else "fold"
        if e >= value_thr + 0.14 and "raise" in obs.legal_actions:
            return self._raise_to(obs, obs.current_bet * 2.5 + (pot - obs.current_bet) * 0.5)
        margin = 0.04 + 0.04 * max(0, nopp - 1) + (0.03 if not river else 0.0)
        if e >= pot_odds + margin:
            return "call"
        return "fold"

    # ------------------------------------------------------------ discard & reveal
    def discard(self, obs):
        t0 = _now()
        self.last_obs = obs
        try:
            self._scan(obs)
            hole = _cards(obs.down_cards)
            count = obs.discard_count or 1
            if obs.game != "community" or count != 1 or len(hole) != 3:
                return None  # engine default
            board = _cards(obs.board)
            pairs = [(hole[0], hole[1], hole[2]), (hole[0], hole[2], hole[1]), (hole[1], hole[2], hole[0])]
            if not board:
                # before any board card: keep the best 2-card class
                best = max(pairs, key=lambda p: PF2.get(key2(p[0], p[1]), [0])[0])
                self.discard_plan = (best[0], best[1])
                return [I2C[best[2]]]
            dead = set(hole) | set(board)
            for x in obs.players:
                for c in _cards(x.up_cards):
                    dead.add(c)
            opps = self._opps(obs, dead, 2)
            if not opps:
                opps = [([], 2, None, None, 0.0, 1326)]
            scores = self._mc_pairs(pairs, board, opps, dead, t0 + MC_BUDGET + 0.0001)
            best = max(range(3), key=lambda i: scores[i])
            p = pairs[best]
            self.discard_plan = (p[0], p[1])
            return [I2C[p[2]]]
        except Exception:
            return None

    def _mc_pairs(self, pairs, board, opps, dead, deadline):
        """Equity of each candidate 2-card hand using common random numbers."""
        deck = [c for c in range(52) if c not in dead]
        need = 5 - len(board)
        R2 = RANGE2
        wins = [0.0, 0.0, 0.0]
        n = 0
        while n < 300:
            if n >= 10 and (n & 3) == 0 and _now() > deadline:
                break
            used = set()
            hands = []
            for (known, nh, allowed, high, low, cut) in opps:
                if nh == 1:
                    x = _choice(high if _rand() < low else allowed)
                    if x in used:
                        continue
                    used.add(x)
                    hands.append(known + [x])
                else:
                    for _t in range(8):
                        a, b = R2[int(_rand() * cut)]
                        if a not in dead and b not in dead and a not in used and b not in used:
                            break
                    else:
                        continue
                    used.add(a)
                    used.add(b)
                    hands.append([a, b])
            run = [c for c in _sample(deck, need + len(used)) if c not in used][:need] if need else []
            bd = board + run
            ovs = [best_of(h, bd) for h in hands]
            top = max(ovs) if ovs else -1
            nt = ovs.count(top) if ovs else 0
            for i in range(3):
                hv = ev([pairs[i][0], pairs[i][1]] + bd)
                if hv > top:
                    wins[i] += 1.0
                elif hv == top:
                    wins[i] += 1.0 / (nt + 1)
            n += 1
        return wins

    def reveal(self, obs):
        t0 = _now()
        self.last_obs = obs
        try:
            hole = _cards(obs.down_cards)
            count = obs.reveal_count or 1
            if count != 1 or len(hole) != 2:
                return None
            a, b = hole
            if (a >> 2) == (b >> 2):
                return [I2C[_choice(hole)]]
            board = _cards(obs.board)
            if not board:
                low, high = (a, b) if (a >> 2) < (b >> 2) else (b, a)
                # mostly show the lower card; sometimes the higher one so the choice is not a tell
                return [I2C[low if _rand() < 0.72 else high]]
            seen = set(board)
            for x in obs.players:
                for c in _cards(x.up_cards):
                    seen.add(c)
            unknown = [c for c in range(52) if c not in seen]
            deadline = t0 + REVEAL_BUDGET
            perceived = []
            for c in (a, b):
                tot = 0
                n = 0
                for x in unknown:
                    if x == c:
                        continue
                    tot += ev([c, x] + board) >> 12
                    n += 1
                    if (n & 7) == 0 and _now() > deadline:
                        break
                perceived.append(tot / float(max(n, 1)))
            pick = 0 if perceived[0] < perceived[1] else 1
            if abs(perceived[0] - perceived[1]) < 2.0:
                pick = 0 if _rand() < 0.5 else 1
            elif _rand() < 0.12:
                pick = 1 - pick
            return [I2C[hole[pick]]]
        except Exception:
            return None


# ============================================================ precomputed tables (tools/build_sf_tables.py)
# PF2[(high rank, low rank, suited)] = equity vs 1..7 random opponents (Hold'em)
PF2 = {
 (0, 0, 0): [0.524, 0.3151, 0.2109, 0.1703, 0.1497, 0.1425, 0.1289],
 (1, 0, 0): [0.338, 0.1988, 0.1444, 0.1118, 0.0911, 0.0693, 0.0674],
 (1, 0, 1): [0.3588, 0.231, 0.1776, 0.1468, 0.1237, 0.1172, 0.1015],
 (1, 1, 0): [0.5343, 0.3448, 0.2478, 0.1816, 0.1597, 0.1469, 0.1319],
 (2, 0, 0): [0.339, 0.1932, 0.1519, 0.1146, 0.0939, 0.0898, 0.0774],
 (2, 0, 1): [0.3738, 0.2492, 0.1809, 0.1608, 0.1348, 0.1202, 0.11],
 (2, 1, 0): [0.3463, 0.2266, 0.1726, 0.1357, 0.107, 0.091, 0.0859],
 (2, 1, 1): [0.3958, 0.2617, 0.1978, 0.1735, 0.1491, 0.129, 0.1162],
 (2, 2, 0): [0.5823, 0.3798, 0.2618, 0.2046, 0.1689, 0.1486, 0.1406],
 (3, 0, 0): [0.3422, 0.2134, 0.1548, 0.1154, 0.1079, 0.0842, 0.0741],
 (3, 0, 1): [0.3592, 0.2628, 0.2044, 0.1688, 0.1403, 0.117, 0.118],
 (3, 1, 0): [0.3533, 0.2238, 0.1754, 0.1426, 0.1136, 0.096, 0.0829],
 (3, 1, 1): [0.3948, 0.2727, 0.1967, 0.17, 0.1504, 0.13, 0.1408],
 (3, 2, 0): [0.381, 0.2564, 0.1936, 0.148, 0.1243, 0.1116, 0.0954],
 (3, 2, 1): [0.4185, 0.2912, 0.2271, 0.1989, 0.1637, 0.1496, 0.1334],
 (3, 3, 0): [0.5942, 0.4052, 0.2913, 0.2306, 0.2017, 0.1545, 0.1556],
 (4, 0, 0): [0.3483, 0.1977, 0.1528, 0.1126, 0.0927, 0.0682, 0.0719],
 (4, 0, 1): [0.3967, 0.2499, 0.1802, 0.1562, 0.1388, 0.1142, 0.1078],
 (4, 1, 0): [0.3513, 0.2139, 0.1567, 0.1148, 0.1009, 0.0886, 0.0741],
 (4, 1, 1): [0.3887, 0.2725, 0.2103, 0.1812, 0.1442, 0.128, 0.1169],
 (4, 2, 0): [0.3925, 0.2507, 0.1809, 0.1472, 0.1257, 0.0989, 0.0869],
 (4, 2, 1): [0.4112, 0.2852, 0.2203, 0.1868, 0.1621, 0.1451, 0.1227],
 (4, 3, 0): [0.4075, 0.2666, 0.2023, 0.1628, 0.1219, 0.1216, 0.1022],
 (4, 3, 1): [0.427, 0.2936, 0.2299, 0.1954, 0.1722, 0.1476, 0.128],
 (4, 4, 0): [0.628, 0.4339, 0.3078, 0.2435, 0.2051, 0.1711, 0.1546],
 (5, 0, 0): [0.3588, 0.2171, 0.143, 0.1108, 0.0859, 0.0736, 0.0661],
 (5, 0, 1): [0.3975, 0.2394, 0.1981, 0.1526, 0.1214, 0.1094, 0.1015],
 (5, 1, 0): [0.3807, 0.2182, 0.1653, 0.1128, 0.0961, 0.0857, 0.0776],
 (5, 1, 1): [0.3893, 0.2602, 0.1924, 0.1618, 0.1427, 0.1229, 0.1168],
 (5, 2, 0): [0.3908, 0.2439, 0.1711, 0.1309, 0.1238, 0.0907, 0.0888],
 (5, 2, 1): [0.4268, 0.2939, 0.2166, 0.1738, 0.1666, 0.1303, 0.1155],
 (5, 3, 0): [0.4178, 0.2676, 0.2045, 0.1597, 0.1299, 0.1093, 0.0938],
 (5, 3, 1): [0.4447, 0.2978, 0.2231, 0.189, 0.1622, 0.1586, 0.1337],
 (5, 4, 0): [0.4373, 0.2822, 0.2007, 0.1775, 0.1347, 0.1247, 0.1049],
 (5, 4, 1): [0.4515, 0.3216, 0.2525, 0.1994, 0.1848, 0.1448, 0.1493],
 (5, 5, 0): [0.6713, 0.4636, 0.3423, 0.2553, 0.2007, 0.1894, 0.1701],
 (6, 0, 0): [0.3687, 0.2148, 0.1511, 0.1125, 0.0913, 0.0767, 0.0615],
 (6, 0, 1): [0.3998, 0.2617, 0.1872, 0.167, 0.1414, 0.1121, 0.1083],
 (6, 1, 0): [0.3752, 0.2234, 0.1659, 0.123, 0.1066, 0.0725, 0.0683],
 (6, 1, 1): [0.4153, 0.2678, 0.2002, 0.1567, 0.1327, 0.1299, 0.0981],
 (6, 2, 0): [0.3993, 0.2453, 0.198, 0.1266, 0.1129, 0.0934, 0.0788],
 (6, 2, 1): [0.4112, 0.2976, 0.2035, 0.168, 0.1561, 0.1373, 0.1168],
 (6, 3, 0): [0.402, 0.2671, 0.2082, 0.1345, 0.1275, 0.1147, 0.1035],
 (6, 3, 1): [0.4303, 0.3003, 0.2443, 0.1908, 0.1667, 0.1263, 0.1238],
 (6, 4, 0): [0.4442, 0.2824, 0.2199, 0.1618, 0.1381, 0.1199, 0.1044],
 (6, 4, 1): [0.4618, 0.3256, 0.2415, 0.219, 0.1765, 0.1534, 0.145],
 (6, 5, 0): [0.4535, 0.2913, 0.2373, 0.1826, 0.1514, 0.1323, 0.1149],
 (6, 5, 1): [0.4768, 0.3346, 0.2762, 0.199, 0.1862, 0.1647, 0.1495],
 (6, 6, 0): [0.6788, 0.4853, 0.3761, 0.2924, 0.2371, 0.2028, 0.1615],
 (7, 0, 0): [0.3965, 0.2278, 0.1566, 0.1239, 0.0927, 0.0813, 0.0733],
 (7, 0, 1): [0.4303, 0.2582, 0.2087, 0.1546, 0.1354, 0.1179, 0.1092],
 (7, 1, 0): [0.3888, 0.2314, 0.1659, 0.1322, 0.1153, 0.08, 0.07],
 (7, 1, 1): [0.4287, 0.2824, 0.2092, 0.1733, 0.156, 0.1296, 0.1054],
 (7, 2, 0): [0.4177, 0.2584, 0.171, 0.1325, 0.103, 0.0872, 0.0639],
 (7, 2, 1): [0.4495, 0.2821, 0.2074, 0.171, 0.1334, 0.1198, 0.1164],
 (7, 3, 0): [0.417, 0.2693, 0.1959, 0.1493, 0.1176, 0.0936, 0.0839],
 (7, 3, 1): [0.4378, 0.3039, 0.2347, 0.1859, 0.1607, 0.1367, 0.1292],
 (7, 4, 0): [0.4297, 0.284, 0.2146, 0.1607, 0.1353, 0.1259, 0.0993],
 (7, 4, 1): [0.464, 0.3086, 0.2492, 0.2062, 0.1692, 0.1515, 0.1377],
 (7, 5, 0): [0.4692, 0.3079, 0.2219, 0.1775, 0.1465, 0.1281, 0.1106],
 (7, 5, 1): [0.4853, 0.3469, 0.2733, 0.2192, 0.2035, 0.1702, 0.1582],
 (7, 6, 0): [0.4767, 0.3263, 0.2566, 0.2036, 0.162, 0.1228, 0.1183],
 (7, 6, 1): [0.511, 0.3551, 0.2959, 0.2457, 0.2012, 0.1839, 0.1615],
 (7, 7, 0): [0.7193, 0.5368, 0.4163, 0.3349, 0.2752, 0.2231, 0.1944],
 (8, 0, 0): [0.4187, 0.241, 0.1855, 0.1332, 0.1161, 0.0849, 0.0735],
 (8, 0, 1): [0.4427, 0.2801, 0.2128, 0.1722, 0.1476, 0.1292, 0.1212],
 (8, 1, 0): [0.4243, 0.259, 0.189, 0.1371, 0.1098, 0.0938, 0.0713],
 (8, 1, 1): [0.4632, 0.2838, 0.2343, 0.186, 0.153, 0.1342, 0.1292],
 (8, 2, 0): [0.4432, 0.2813, 0.184, 0.1369, 0.115, 0.0926, 0.0714],
 (8, 2, 1): [0.4613, 0.3147, 0.2332, 0.1878, 0.1613, 0.1308, 0.122],
 (8, 3, 0): [0.437, 0.2656, 0.1994, 0.1441, 0.1183, 0.0904, 0.0766],
 (8, 3, 1): [0.4615, 0.3133, 0.2272, 0.1944, 0.1572, 0.1366, 0.1178],
 (8, 4, 0): [0.4632, 0.2928, 0.2166, 0.1712, 0.1295, 0.1069, 0.0943],
 (8, 4, 1): [0.4913, 0.3243, 0.2566, 0.2005, 0.1742, 0.1544, 0.1376],
 (8, 5, 0): [0.479, 0.3217, 0.2228, 0.1792, 0.1452, 0.1195, 0.1046],
 (8, 5, 1): [0.4945, 0.3553, 0.2632, 0.2151, 0.191, 0.1622, 0.1469],
 (8, 6, 0): [0.4772, 0.3273, 0.2543, 0.2024, 0.159, 0.1378, 0.1294],
 (8, 6, 1): [0.5278, 0.3766, 0.2905, 0.2522, 0.2099, 0.1825, 0.1602],
 (8, 7, 0): [0.5095, 0.3472, 0.2792, 0.2221, 0.1941, 0.1668, 0.1472],
 (8, 7, 1): [0.5485, 0.3969, 0.3046, 0.2419, 0.2268, 0.2, 0.1787],
 (8, 8, 0): [0.7572, 0.5838, 0.4567, 0.3598, 0.3011, 0.2417, 0.2163],
 (9, 0, 0): [0.4433, 0.2653, 0.1866, 0.1503, 0.1167, 0.096, 0.0812],
 (9, 0, 1): [0.4543, 0.3018, 0.2235, 0.1844, 0.1599, 0.1323, 0.1395],
 (9, 1, 0): [0.4642, 0.2653, 0.1973, 0.1485, 0.1249, 0.0998, 0.0819],
 (9, 1, 1): [0.4692, 0.3082, 0.2308, 0.1892, 0.1618, 0.1237, 0.12],
 (9, 2, 0): [0.4362, 0.2953, 0.1995, 0.1565, 0.1102, 0.0986, 0.0846],
 (9, 2, 1): [0.482, 0.3162, 0.2352, 0.1907, 0.1621, 0.1514, 0.1375],
 (9, 3, 0): [0.4792, 0.2895, 0.1987, 0.1579, 0.1286, 0.092, 0.0998],
 (9, 3, 1): [0.4905, 0.3276, 0.2544, 0.1893, 0.1797, 0.1533, 0.1292],
 (9, 4, 0): [0.4762, 0.2899, 0.1959, 0.1552, 0.1283, 0.1205, 0.0904],
 (9, 4, 1): [0.5067, 0.3326, 0.2562, 0.2032, 0.1686, 0.1404, 0.1404],
 (9, 5, 0): [0.502, 0.3254, 0.2332, 0.1835, 0.1507, 0.1146, 0.0999],
 (9, 5, 1): [0.5243, 0.3502, 0.2574, 0.2191, 0.1949, 0.1654, 0.1414],
 (9, 6, 0): [0.5208, 0.339, 0.2648, 0.1927, 0.1711, 0.1486, 0.1217],
 (9, 6, 1): [0.5437, 0.3697, 0.2884, 0.2363, 0.2013, 0.1643, 0.1612],
 (9, 7, 0): [0.5418, 0.3672, 0.2739, 0.2265, 0.1896, 0.1503, 0.149],
 (9, 7, 1): [0.548, 0.3934, 0.313, 0.2569, 0.2145, 0.1939, 0.1705],
 (9, 8, 0): [0.5398, 0.3952, 0.2994, 0.2471, 0.2151, 0.1848, 0.1556],
 (9, 8, 1): [0.5715, 0.4265, 0.3377, 0.2937, 0.2437, 0.2216, 0.1943],
 (9, 9, 0): [0.7713, 0.6141, 0.4996, 0.3905, 0.3329, 0.2898, 0.2435],
 (10, 0, 0): [0.4725, 0.2767, 0.2073, 0.1477, 0.1148, 0.1019, 0.091],
 (10, 0, 1): [0.4958, 0.3149, 0.2438, 0.1936, 0.1731, 0.142, 0.1264],
 (10, 1, 0): [0.472, 0.3053, 0.216, 0.1523, 0.1327, 0.1058, 0.0965],
 (10, 1, 1): [0.5125, 0.3336, 0.2526, 0.1932, 0.1627, 0.147, 0.1282],
 (10, 2, 0): [0.4767, 0.3036, 0.2118, 0.1664, 0.1297, 0.1223, 0.0925],
 (10, 2, 1): [0.519, 0.3469, 0.2291, 0.2, 0.1828, 0.1621, 0.1463],
 (10, 3, 0): [0.4828, 0.2934, 0.2207, 0.1661, 0.141, 0.1192, 0.0908],
 (10, 3, 1): [0.533, 0.346, 0.2554, 0.217, 0.1862, 0.1541, 0.1359],
 (10, 4, 0): [0.5172, 0.3192, 0.2176, 0.1826, 0.1391, 0.1277, 0.1044],
 (10, 4, 1): [0.5283, 0.3557, 0.2707, 0.217, 0.1794, 0.1572, 0.1397],
 (10, 5, 0): [0.5187, 0.3281, 0.2406, 0.1887, 0.1523, 0.1225, 0.104],
 (10, 5, 1): [0.5458, 0.3578, 0.2762, 0.2193, 0.1897, 0.162, 0.1491],
 (10, 6, 0): [0.5242, 0.3588, 0.2665, 0.2094, 0.1744, 0.1324, 0.1305],
 (10, 6, 1): [0.5738, 0.3825, 0.302, 0.2426, 0.2054, 0.1796, 0.1548],
 (10, 7, 0): [0.5618, 0.3658, 0.2868, 0.2313, 0.1899, 0.1661, 0.135],
 (10, 7, 1): [0.5768, 0.4106, 0.2976, 0.2772, 0.2331, 0.1992, 0.1721],
 (10, 8, 0): [0.587, 0.3986, 0.3144, 0.2656, 0.2242, 0.1907, 0.1714],
 (10, 8, 1): [0.613, 0.4333, 0.339, 0.2775, 0.2678, 0.2199, 0.2024],
 (10, 9, 0): [0.5813, 0.4042, 0.3254, 0.2727, 0.2258, 0.1981, 0.1774],
 (10, 9, 1): [0.607, 0.4356, 0.3621, 0.3049, 0.2592, 0.2298, 0.21],
 (10, 10, 0): [0.7972, 0.6441, 0.5364, 0.4528, 0.3839, 0.3148, 0.2832],
 (11, 0, 0): [0.5148, 0.3061, 0.2269, 0.1735, 0.136, 0.119, 0.0914],
 (11, 0, 1): [0.5198, 0.3507, 0.2559, 0.2233, 0.1808, 0.1617, 0.1293],
 (11, 1, 0): [0.5175, 0.3291, 0.2259, 0.1771, 0.1358, 0.1176, 0.1081],
 (11, 1, 1): [0.5415, 0.3525, 0.2613, 0.2172, 0.1929, 0.1503, 0.1406],
 (11, 2, 0): [0.5212, 0.3273, 0.2455, 0.1856, 0.1503, 0.1256, 0.0934],
 (11, 2, 1): [0.5578, 0.3773, 0.2873, 0.2299, 0.1867, 0.1519, 0.1584],
 (11, 3, 0): [0.525, 0.3377, 0.2404, 0.1887, 0.1617, 0.1243, 0.1066],
 (11, 3, 1): [0.5435, 0.3841, 0.2796, 0.2226, 0.1992, 0.1747, 0.1469],
 (11, 4, 0): [0.5363, 0.3439, 0.2578, 0.2141, 0.1584, 0.1239, 0.1079],
 (11, 4, 1): [0.5553, 0.3799, 0.286, 0.2321, 0.1994, 0.1722, 0.1558],
 (11, 5, 0): [0.555, 0.3546, 0.2585, 0.2217, 0.1732, 0.1385, 0.1134],
 (11, 5, 1): [0.5697, 0.3967, 0.3026, 0.2485, 0.2152, 0.1856, 0.1634],
 (11, 6, 0): [0.5618, 0.3721, 0.2578, 0.2099, 0.1646, 0.1458, 0.1301],
 (11, 6, 1): [0.5865, 0.4036, 0.3009, 0.2383, 0.2212, 0.1981, 0.1712],
 (11, 7, 0): [0.577, 0.3935, 0.2971, 0.2406, 0.1857, 0.1493, 0.1342],
 (11, 7, 1): [0.6023, 0.4101, 0.3226, 0.2666, 0.2237, 0.1928, 0.1955],
 (11, 8, 0): [0.6063, 0.4165, 0.3309, 0.263, 0.2209, 0.1909, 0.1747],
 (11, 8, 1): [0.5967, 0.4664, 0.3623, 0.2926, 0.2611, 0.2325, 0.209],
 (11, 9, 0): [0.6097, 0.4382, 0.3442, 0.2861, 0.2361, 0.209, 0.1767],
 (11, 9, 1): [0.6283, 0.4592, 0.3696, 0.3168, 0.2594, 0.2217, 0.2084],
 (11, 10, 0): [0.5975, 0.4467, 0.3619, 0.3, 0.2459, 0.2167, 0.1905],
 (11, 10, 1): [0.6368, 0.4892, 0.3881, 0.3349, 0.2819, 0.2685, 0.2238],
 (11, 11, 0): [0.8382, 0.6934, 0.5589, 0.4891, 0.4259, 0.3675, 0.3306],
 (12, 0, 0): [0.558, 0.3526, 0.2376, 0.2054, 0.1606, 0.1288, 0.1161],
 (12, 0, 1): [0.5805, 0.3947, 0.2938, 0.244, 0.2011, 0.1856, 0.1621],
 (12, 1, 0): [0.5593, 0.3688, 0.2762, 0.2051, 0.1751, 0.1514, 0.1264],
 (12, 1, 1): [0.5782, 0.3926, 0.2902, 0.2434, 0.2048, 0.1918, 0.1581],
 (12, 2, 0): [0.575, 0.3471, 0.2695, 0.2157, 0.1743, 0.1557, 0.127],
 (12, 2, 1): [0.5955, 0.4121, 0.2951, 0.2466, 0.2113, 0.1894, 0.1778],
 (12, 3, 0): [0.5868, 0.3775, 0.2853, 0.2104, 0.1679, 0.1444, 0.1317],
 (12, 3, 1): [0.601, 0.4264, 0.3237, 0.2612, 0.2148, 0.1929, 0.1772],
 (12, 4, 0): [0.5762, 0.3885, 0.2791, 0.2239, 0.1778, 0.1403, 0.1212],
 (12, 4, 1): [0.5775, 0.4186, 0.3252, 0.2565, 0.2175, 0.1866, 0.1655],
 (12, 5, 0): [0.5913, 0.3956, 0.3017, 0.2306, 0.1776, 0.1565, 0.1318],
 (12, 5, 1): [0.604, 0.4307, 0.3304, 0.2666, 0.2257, 0.2014, 0.1757],
 (12, 6, 0): [0.5917, 0.4072, 0.3027, 0.2437, 0.1883, 0.1639, 0.1298],
 (12, 6, 1): [0.6055, 0.4437, 0.3421, 0.2811, 0.2538, 0.1981, 0.1778],
 (12, 7, 0): [0.6055, 0.4239, 0.3108, 0.2505, 0.1966, 0.1795, 0.1466],
 (12, 7, 1): [0.6262, 0.4502, 0.3489, 0.2883, 0.245, 0.2112, 0.1842],
 (12, 8, 0): [0.6263, 0.4483, 0.3419, 0.2779, 0.2451, 0.1961, 0.1639],
 (12, 8, 1): [0.6522, 0.4769, 0.3785, 0.3037, 0.2682, 0.2275, 0.2215],
 (12, 9, 0): [0.624, 0.4651, 0.3564, 0.2891, 0.2379, 0.2045, 0.1769],
 (12, 9, 1): [0.6467, 0.4887, 0.3713, 0.3198, 0.2803, 0.2522, 0.2212],
 (12, 10, 0): [0.6398, 0.4912, 0.3754, 0.285, 0.2596, 0.2242, 0.193],
 (12, 10, 1): [0.6685, 0.4943, 0.4007, 0.3413, 0.2736, 0.254, 0.2318],
 (12, 11, 0): [0.6518, 0.4822, 0.3792, 0.3067, 0.2775, 0.2405, 0.2131],
 (12, 11, 1): [0.6708, 0.4895, 0.4115, 0.3589, 0.3238, 0.2701, 0.2464],
 (12, 12, 0): [0.8502, 0.7281, 0.6383, 0.5593, 0.4967, 0.4418, 0.3855],
}
# 3-card classes: key = c1*2704 + c2*52 + c3 (canonical suit relabeling), value = [eq vs 1,2,3,4,6 opponents, combos]
PF3 = {
 5460:[0.263, 0.138, 0.092, 0.0664, 0.028, 4],10868:[0.403, 0.2857, 0.2015, 0.175, 0.132, 12],10921:[0.35, 0.2373, 0.1695, 0.137, 0.118, 12],13728:[0.443, 0.2603, 0.197, 0.1944, 0.13, 12],
 13730:[0.395, 0.2743, 0.202, 0.1654, 0.1063, 12],16488:[0.287, 0.143, 0.105, 0.084, 0.028, 4],21684:[0.431, 0.256, 0.2065, 0.173, 0.145, 12],21737:[0.424, 0.2237, 0.2022, 0.1433, 0.114, 12],
 21840:[0.365, 0.263, 0.1992, 0.1484, 0.0927, 4],21841:[0.365, 0.2463, 0.186, 0.1391, 0.1033, 12],21892:[0.354, 0.239, 0.1775, 0.1717, 0.0886, 12],21893:[0.392, 0.2383, 0.1927, 0.1561, 0.1007, 12],
 21894:[0.329, 0.2567, 0.1598, 0.1037, 0.0785, 24],21896:[0.434, 0.303, 0.243, 0.2107, 0.1297, 12],21949:[0.371, 0.27, 0.2065, 0.1747, 0.1366, 12],24752:[0.446, 0.3343, 0.2445, 0.2014, 0.1517, 12],
 24754:[0.435, 0.2767, 0.192, 0.1514, 0.1056, 12],24756:[0.475, 0.265, 0.2395, 0.1994, 0.1265, 12],24758:[0.4, 0.277, 0.188, 0.1587, 0.1413, 12],27516:[0.337, 0.1717, 0.1135, 0.06, 0.053, 4],
 32500:[0.458, 0.252, 0.2135, 0.2107, 0.1373, 12],32553:[0.375, 0.2393, 0.187, 0.1504, 0.102, 12],32656:[0.406, 0.2783, 0.2065, 0.1695, 0.1178, 4],32657:[0.387, 0.2683, 0.189, 0.1608, 0.1091, 12],
 32708:[0.362, 0.2063, 0.183, 0.192, 0.1366, 12],32709:[0.345, 0.2503, 0.223, 0.1547, 0.1187, 12],32710:[0.329, 0.2163, 0.1557, 0.1408, 0.0843, 24],32712:[0.439, 0.2897, 0.2405, 0.1912, 0.171, 12],
 32765:[0.456, 0.2647, 0.2085, 0.2, 0.1187, 12],32864:[0.386, 0.261, 0.1887, 0.1723, 0.1087, 4],32865:[0.375, 0.2893, 0.197, 0.1641, 0.1105, 12],32868:[0.393, 0.2843, 0.2325, 0.1777, 0.1267, 4],
 32869:[0.391, 0.3077, 0.252, 0.1951, 0.1473, 12],32916:[0.421, 0.2377, 0.2227, 0.1846, 0.1156, 12],32917:[0.385, 0.283, 0.1775, 0.1801, 0.1288, 12],32918:[0.401, 0.236, 0.1755, 0.1834, 0.1107, 24],
 32920:[0.395, 0.2873, 0.2332, 0.1817, 0.1543, 12],32921:[0.394, 0.2623, 0.2135, 0.1948, 0.104, 12],32922:[0.399, 0.2797, 0.207, 0.1574, 0.0888, 24],32924:[0.517, 0.309, 0.216, 0.1834, 0.162, 12],
 32977:[0.438, 0.276, 0.212, 0.1778, 0.1273, 12],35776:[0.448, 0.3237, 0.2055, 0.1964, 0.1513, 12],35778:[0.47, 0.2373, 0.1958, 0.1752, 0.1177, 12],35780:[0.471, 0.3033, 0.2355, 0.219, 0.182, 12],
 35782:[0.426, 0.3087, 0.213, 0.154, 0.127, 12],35784:[0.497, 0.3457, 0.2365, 0.2317, 0.1636, 12],35786:[0.498, 0.298, 0.2215, 0.1734, 0.125, 12],38544:[0.417, 0.199, 0.1155, 0.0994, 0.0593, 4],
 43316:[0.422, 0.2873, 0.1982, 0.1837, 0.1273, 12],43369:[0.359, 0.2403, 0.1548, 0.1577, 0.1365, 12],43472:[0.387, 0.2387, 0.1737, 0.1375, 0.1013, 4],43473:[0.384, 0.2557, 0.1835, 0.1634, 0.1067, 12],
 43524:[0.381, 0.1997, 0.2057, 0.1267, 0.104, 12],43525:[0.36, 0.2527, 0.1585, 0.1803, 0.1143, 12],43526:[0.358, 0.2527, 0.1158, 0.1131, 0.1013, 24],43528:[0.436, 0.2873, 0.2165, 0.1903, 0.164, 12],
 43581:[0.423, 0.2793, 0.1855, 0.161, 0.102, 12],43680:[0.4, 0.2433, 0.1815, 0.1755, 0.1306, 4],43681:[0.367, 0.2603, 0.2357, 0.1752, 0.1253, 12],43684:[0.415, 0.251, 0.214, 0.1637, 0.1477, 4],
 43685:[0.401, 0.3107, 0.23, 0.161, 0.1356, 12],43732:[0.39, 0.2673, 0.1732, 0.171, 0.1355, 12],43733:[0.389, 0.2357, 0.2068, 0.1614, 0.1175, 12],43734:[0.365, 0.263, 0.156, 0.1342, 0.0857, 24],
 43736:[0.421, 0.2773, 0.2155, 0.1771, 0.1107, 12],43737:[0.42, 0.3047, 0.1967, 0.1558, 0.1313, 12],43738:[0.379, 0.2193, 0.1713, 0.1637, 0.1047, 24],43740:[0.478, 0.2937, 0.215, 0.2061, 0.151, 12],
 43793:[0.47, 0.2823, 0.1907, 0.111, 0.1342, 12],43888:[0.375, 0.3133, 0.223, 0.1214, 0.0878, 4],43889:[0.408, 0.2767, 0.2297, 0.1708, 0.1453, 12],43892:[0.428, 0.2593, 0.2427, 0.2096, 0.1282, 4],
 43893:[0.412, 0.309, 0.2312, 0.1829, 0.1158, 12],43896:[0.437, 0.278, 0.22, 0.2178, 0.1637, 4],43897:[0.431, 0.3067, 0.1945, 0.1728, 0.1553, 12],43940:[0.397, 0.264, 0.207, 0.1587, 0.1523, 12],
 43941:[0.464, 0.264, 0.2313, 0.1959, 0.121, 12],43942:[0.384, 0.24, 0.2103, 0.1567, 0.0719, 24],43944:[0.382, 0.3237, 0.2132, 0.1927, 0.118, 12],43945:[0.454, 0.2823, 0.223, 0.2217, 0.1489, 12],
 43946:[0.413, 0.267, 0.1938, 0.1575, 0.0947, 24],43948:[0.458, 0.3353, 0.2402, 0.1851, 0.134, 12],43949:[0.431, 0.2747, 0.2075, 0.2057, 0.1507, 12],43950:[0.389, 0.2553, 0.219, 0.1581, 0.104, 24],
 43952:[0.534, 0.3103, 0.292, 0.2207, 0.1917, 12],44005:[0.482, 0.325, 0.2267, 0.2109, 0.1417, 12],46800:[0.497, 0.365, 0.2595, 0.2032, 0.1736, 12],46802:[0.485, 0.2573, 0.1735, 0.1414, 0.1217, 12],
 46804:[0.502, 0.3133, 0.218, 0.203, 0.138, 12],46806:[0.487, 0.2983, 0.209, 0.208, 0.1307, 12],46808:[0.499, 0.327, 0.2693, 0.1942, 0.1473, 12],46810:[0.48, 0.271, 0.244, 0.2234, 0.1787, 12],
 46812:[0.526, 0.336, 0.286, 0.2174, 0.19, 12],46814:[0.475, 0.3413, 0.246, 0.205, 0.1573, 12],49572:[0.43, 0.2017, 0.128, 0.0898, 0.062, 4],54132:[0.433, 0.279, 0.1948, 0.165, 0.1359, 12],
 54185:[0.399, 0.1797, 0.162, 0.13, 0.0983, 12],54288:[0.384, 0.202, 0.135, 0.1477, 0.103, 4],54289:[0.365, 0.2053, 0.1942, 0.1573, 0.1126, 12],54340:[0.365, 0.218, 0.1957, 0.1397, 0.0905, 12],
 54341:[0.423, 0.2437, 0.2083, 0.1364, 0.1167, 12],54342:[0.343, 0.1887, 0.1497, 0.1007, 0.0693, 24],54344:[0.493, 0.2937, 0.2135, 0.1897, 0.1537, 12],54397:[0.456, 0.2767, 0.1777, 0.1258, 0.131, 12],
 54496:[0.347, 0.2123, 0.1785, 0.1097, 0.101, 4],54497:[0.384, 0.2807, 0.181, 0.138, 0.1048, 12],54500:[0.375, 0.235, 0.1993, 0.1547, 0.1108, 4],54501:[0.421, 0.268, 0.254, 0.1645, 0.1287, 12],
 54548:[0.394, 0.2743, 0.1835, 0.166, 0.0733, 12],54549:[0.402, 0.238, 0.1925, 0.1738, 0.116, 12],54550:[0.354, 0.2143, 0.1527, 0.1157, 0.0752, 24],54552:[0.373, 0.2777, 0.22, 0.1461, 0.1257, 12],
 54553:[0.394, 0.2907, 0.2045, 0.1454, 0.103, 12],54554:[0.395, 0.2697, 0.154, 0.1561, 0.0641, 24],54556:[0.517, 0.2843, 0.2255, 0.2144, 0.1437, 12],54609:[0.464, 0.284, 0.1995, 0.1747, 0.1408, 12],
 54704:[0.413, 0.247, 0.2135, 0.124, 0.108, 4],54705:[0.412, 0.2963, 0.1685, 0.1361, 0.1337, 12],54708:[0.387, 0.2853, 0.1922, 0.1579, 0.14, 4],54709:[0.424, 0.2723, 0.228, 0.1447, 0.1463, 12],
 54712:[0.421, 0.2947, 0.2097, 0.2043, 0.1297, 4],54713:[0.458, 0.2943, 0.2125, 0.2257, 0.1593, 12],54756:[0.44, 0.2487, 0.192, 0.167, 0.1245, 12],54757:[0.421, 0.2733, 0.2037, 0.1441, 0.1227, 12],
 54758:[0.362, 0.238, 0.186, 0.1337, 0.091, 24],54760:[0.443, 0.304, 0.2277, 0.1427, 0.1592, 12],54761:[0.422, 0.2683, 0.2427, 0.1921, 0.1503, 12],54762:[0.393, 0.2597, 0.1882, 0.1481, 0.1016, 24],
 54764:[0.435, 0.2827, 0.2095, 0.1791, 0.1525, 12],54765:[0.456, 0.3243, 0.238, 0.1855, 0.1337, 12],54766:[0.418, 0.2587, 0.1932, 0.1407, 0.1087, 24],54768:[0.531, 0.36, 0.292, 0.2517, 0.158, 12],
 54821:[0.513, 0.292, 0.2215, 0.184, 0.122, 12],54912:[0.371, 0.2423, 0.1577, 0.1528, 0.1327, 4],54913:[0.377, 0.2793, 0.1968, 0.1285, 0.1267, 12],54916:[0.391, 0.236, 0.2077, 0.162, 0.1248, 4],
 54917:[0.417, 0.279, 0.2305, 0.178, 0.1293, 12],54920:[0.445, 0.318, 0.2172, 0.1835, 0.1295, 4],54921:[0.414, 0.286, 0.262, 0.2277, 0.1285, 12],54924:[0.455, 0.3423, 0.2587, 0.2001, 0.1226, 4],
 54925:[0.422, 0.297, 0.2582, 0.2013, 0.1436, 12],54964:[0.379, 0.2477, 0.1895, 0.1603, 0.1218, 12],54965:[0.387, 0.247, 0.185, 0.1941, 0.0987, 12],54966:[0.388, 0.239, 0.1827, 0.1557, 0.069, 24],
 54968:[0.43, 0.2577, 0.2, 0.1954, 0.1246, 12],54969:[0.432, 0.2677, 0.2202, 0.1793, 0.1507, 12],54970:[0.375, 0.2477, 0.1815, 0.1587, 0.1237, 24],54972:[0.444, 0.293, 0.2045, 0.2061, 0.157, 12],
 54973:[0.413, 0.302, 0.2425, 0.1934, 0.1469, 12],54974:[0.445, 0.2757, 0.1927, 0.1777, 0.1366, 24],54976:[0.458, 0.3337, 0.2457, 0.2111, 0.1426, 12],54977:[0.484, 0.2967, 0.2622, 0.1901, 0.158, 12],
 54978:[0.427, 0.279, 0.2052, 0.1803, 0.128, 24],54980:[0.569, 0.3617, 0.29, 0.2011, 0.1697, 12],55033:[0.485, 0.318, 0.2395, 0.2164, 0.147, 12],57824:[0.543, 0.3233, 0.263, 0.1818, 0.1563, 12],
 57826:[0.505, 0.2743, 0.2187, 0.182, 0.095, 12],57828:[0.529, 0.3527, 0.2607, 0.2047, 0.153, 12],57830:[0.569, 0.286, 0.235, 0.177, 0.1143, 12],57832:[0.51, 0.3417, 0.2255, 0.218, 0.155, 12],
 57834:[0.521, 0.35, 0.2495, 0.1587, 0.136, 12],57836:[0.592, 0.4, 0.26, 0.2397, 0.174, 12],57838:[0.551, 0.3463, 0.2645, 0.182, 0.139, 12],57840:[0.553, 0.4203, 0.29, 0.2055, 0.1423, 12],
 57842:[0.533, 0.3627, 0.2495, 0.2207, 0.163, 12],60600:[0.459, 0.214, 0.159, 0.0894, 0.067, 4],64948:[0.465, 0.276, 0.2197, 0.1828, 0.1155, 12],65001:[0.41, 0.282, 0.175, 0.1297, 0.0823, 12],
 65104:[0.387, 0.2343, 0.1597, 0.1491, 0.0917, 4],65105:[0.356, 0.236, 0.1538, 0.1687, 0.0893, 12],65156:[0.41, 0.237, 0.1562, 0.1502, 0.1122, 12],65157:[0.404, 0.2523, 0.1605, 0.1471, 0.1087, 12],
 65158:[0.333, 0.2167, 0.1475, 0.0931, 0.0908, 24],65160:[0.465, 0.2817, 0.213, 0.1907, 0.145, 12],65213:[0.452, 0.267, 0.1787, 0.1557, 0.1117, 12],65312:[0.362, 0.2593, 0.169, 0.148, 0.1167, 4],
 65313:[0.395, 0.2893, 0.1787, 0.1215, 0.0799, 12],65316:[0.382, 0.2017, 0.1625, 0.1471, 0.0953, 4],65317:[0.438, 0.272, 0.2033, 0.1581, 0.105, 12],65364:[0.412, 0.2377, 0.1577, 0.185, 0.0935, 12],
 65365:[0.409, 0.2463, 0.1937, 0.1702, 0.0916, 12],65366:[0.355, 0.225, 0.1633, 0.0973, 0.0557, 24],65368:[0.416, 0.2397, 0.185, 0.1287, 0.1252, 12],65369:[0.384, 0.2383, 0.1827, 0.1623, 0.1143, 12],
 65370:[0.389, 0.2223, 0.1773, 0.1173, 0.0827, 24],65372:[0.49, 0.298, 0.207, 0.213, 0.1253, 12],65425:[0.442, 0.266, 0.1995, 0.135, 0.1246, 12],65520:[0.392, 0.268, 0.192, 0.1327, 0.0971, 4],
 65521:[0.392, 0.2897, 0.1967, 0.168, 0.103, 12],65524:[0.4, 0.2623, 0.166, 0.1374, 0.1093, 4],65525:[0.414, 0.25, 0.1997, 0.1501, 0.1083, 12],65528:[0.428, 0.282, 0.2113, 0.2083, 0.1207, 4],
 65529:[0.454, 0.3093, 0.2345, 0.2097, 0.1326, 12],65572:[0.409, 0.2633, 0.2298, 0.1738, 0.104, 12],65573:[0.431, 0.2887, 0.2153, 0.1587, 0.1067, 12],65574:[0.394, 0.2123, 0.1582, 0.1169, 0.096, 24],
 65576:[0.426, 0.2593, 0.1822, 0.2097, 0.1083, 12],65577:[0.461, 0.2857, 0.2027, 0.1607, 0.0991, 12],65578:[0.406, 0.2567, 0.1577, 0.1337, 0.1072, 24],65580:[0.458, 0.275, 0.205, 0.1681, 0.158, 12],
 65581:[0.438, 0.2933, 0.217, 0.1781, 0.1146, 12],65582:[0.457, 0.2797, 0.1942, 0.178, 0.0975, 24],65584:[0.549, 0.3323, 0.223, 0.1844, 0.154, 12],65637:[0.454, 0.314, 0.2152, 0.1674, 0.1363, 12],
 65728:[0.388, 0.2817, 0.1918, 0.1451, 0.1513, 4],65729:[0.446, 0.2663, 0.1772, 0.1787, 0.125, 12],65732:[0.419, 0.2807, 0.1682, 0.1575, 0.1265, 4],65733:[0.439, 0.2597, 0.2138, 0.1488, 0.113, 12],
 65736:[0.437, 0.251, 0.193, 0.1565, 0.111, 4],65737:[0.468, 0.2773, 0.2305, 0.1871, 0.1627, 12],65740:[0.457, 0.3217, 0.2512, 0.173, 0.1556, 4],65741:[0.479, 0.317, 0.246, 0.1988, 0.1496, 12],
 65780:[0.468, 0.2623, 0.1795, 0.1757, 0.1216, 12],65781:[0.401, 0.2717, 0.1833, 0.1445, 0.1277, 12],65782:[0.38, 0.2383, 0.159, 0.1441, 0.085, 24],65784:[0.449, 0.257, 0.2002, 0.195, 0.0943, 12],
 65785:[0.386, 0.277, 0.2113, 0.1735, 0.1186, 12],65786:[0.362, 0.28, 0.141, 0.1481, 0.0787, 24],65788:[0.451, 0.2977, 0.2405, 0.1787, 0.1623, 12],65789:[0.456, 0.2913, 0.2423, 0.1657, 0.1396, 12],
 65790:[0.475, 0.2623, 0.1845, 0.144, 0.0975, 24],65792:[0.48, 0.366, 0.2318, 0.2279, 0.167, 12],65793:[0.459, 0.334, 0.2327, 0.1935, 0.1673, 12],65794:[0.432, 0.2683, 0.1803, 0.146, 0.143, 24],
 65796:[0.543, 0.3413, 0.275, 0.2122, 0.135, 12],65849:[0.524, 0.306, 0.2515, 0.1881, 0.131, 12],65936:[0.404, 0.2867, 0.208, 0.1338, 0.095, 4],65937:[0.471, 0.2893, 0.2085, 0.1514, 0.1043, 12],
 65940:[0.393, 0.2717, 0.239, 0.1554, 0.1266, 4],65941:[0.387, 0.257, 0.1995, 0.1442, 0.097, 12],65944:[0.408, 0.298, 0.201, 0.1834, 0.1373, 4],65945:[0.446, 0.2967, 0.221, 0.175, 0.13, 12],
 65948:[0.471, 0.288, 0.2553, 0.2085, 0.1343, 4],65949:[0.453, 0.303, 0.2527, 0.182, 0.1417, 12],65952:[0.471, 0.3213, 0.2617, 0.219, 0.142, 4],65953:[0.439, 0.3363, 0.2758, 0.225, 0.1121, 12],
 65988:[0.398, 0.284, 0.184, 0.184, 0.115, 12],65989:[0.426, 0.273, 0.224, 0.2048, 0.1006, 12],65990:[0.388, 0.2643, 0.1962, 0.149, 0.1107, 24],65992:[0.497, 0.293, 0.2205, 0.1503, 0.1117, 12],
 65993:[0.389, 0.265, 0.2122, 0.1711, 0.1357, 12],65994:[0.415, 0.2227, 0.1895, 0.1417, 0.1047, 24],65996:[0.426, 0.2743, 0.213, 0.2077, 0.128, 12],65997:[0.498, 0.2683, 0.2498, 0.1667, 0.1383, 12],
 65998:[0.442, 0.2903, 0.2095, 0.181, 0.118, 24],66000:[0.493, 0.3207, 0.2193, 0.1941, 0.1357, 12],66001:[0.48, 0.3357, 0.2523, 0.2138, 0.1653, 12],66002:[0.448, 0.309, 0.246, 0.1701, 0.143, 24],
 66004:[0.465, 0.3387, 0.2457, 0.2185, 0.155, 12],66005:[0.497, 0.3177, 0.2752, 0.2164, 0.174, 12],66006:[0.441, 0.2807, 0.24, 0.1921, 0.1263, 24],66008:[0.581, 0.4, 0.2722, 0.2894, 0.186, 12],
 66061:[0.564, 0.363, 0.2465, 0.1938, 0.111, 12],68848:[0.536, 0.3643, 0.256, 0.2134, 0.167, 12],68850:[0.527, 0.3207, 0.23, 0.177, 0.1207, 12],68852:[0.553, 0.3227, 0.2345, 0.193, 0.149, 12],
 68854:[0.55, 0.3383, 0.2415, 0.1397, 0.139, 12],68856:[0.56, 0.3497, 0.285, 0.2302, 0.137, 12],68858:[0.561, 0.3297, 0.218, 0.2263, 0.1147, 12],68860:[0.562, 0.3827, 0.2302, 0.2611, 0.1568, 12],
 68862:[0.556, 0.372, 0.2327, 0.1778, 0.1517, 12],68864:[0.514, 0.3893, 0.309, 0.249, 0.162, 12],68866:[0.564, 0.3957, 0.2815, 0.2077, 0.1707, 12],68868:[0.569, 0.388, 0.2688, 0.2572, 0.1296, 12],
 68870:[0.579, 0.3997, 0.255, 0.1995, 0.1623, 12],71628:[0.476, 0.263, 0.17, 0.1314, 0.0763, 4],75764:[0.449, 0.2943, 0.2195, 0.1798, 0.1375, 12],75817:[0.384, 0.2127, 0.1905, 0.1337, 0.0876, 12],
 75920:[0.417, 0.213, 0.1792, 0.1571, 0.075, 4],75921:[0.389, 0.254, 0.1958, 0.1531, 0.1137, 12],75972:[0.358, 0.241, 0.1938, 0.1577, 0.0715, 12],75973:[0.384, 0.254, 0.1848, 0.1483, 0.1075, 12],
 75974:[0.368, 0.223, 0.158, 0.0867, 0.0827, 24],75976:[0.494, 0.3197, 0.215, 0.1681, 0.1556, 12],76029:[0.433, 0.2747, 0.1907, 0.1582, 0.124, 12],76128:[0.372, 0.2413, 0.167, 0.1351, 0.1037, 4],
 76129:[0.384, 0.23, 0.2063, 0.1377, 0.117, 12],76132:[0.367, 0.281, 0.1755, 0.1547, 0.106, 4],76133:[0.415, 0.2443, 0.1957, 0.168, 0.0955, 12],76180:[0.415, 0.2567, 0.1815, 0.1427, 0.0861, 12],
 76181:[0.389, 0.2393, 0.1755, 0.1398, 0.1083, 12],76182:[0.353, 0.2273, 0.1675, 0.1144, 0.0637, 24],76184:[0.407, 0.2303, 0.181, 0.1351, 0.1397, 12],76185:[0.421, 0.242, 0.194, 0.1629, 0.0986, 12],
 76186:[0.404, 0.2117, 0.1792, 0.1244, 0.0747, 24],76188:[0.536, 0.3097, 0.2443, 0.1754, 0.1867, 12],76241:[0.451, 0.2893, 0.1883, 0.13, 0.098, 12],76336:[0.373, 0.242, 0.1705, 0.146, 0.102, 4],
 76337:[0.395, 0.2513, 0.1837, 0.1563, 0.1242, 12],76340:[0.449, 0.31, 0.1828, 0.1307, 0.1232, 4],76341:[0.438, 0.3067, 0.2075, 0.1797, 0.126, 12],76344:[0.422, 0.2597, 0.1868, 0.1677, 0.128, 4],
 76345:[0.445, 0.2823, 0.204, 0.1651, 0.1246, 12],76388:[0.44, 0.271, 0.199, 0.1702, 0.1086, 12],76389:[0.471, 0.2443, 0.2098, 0.1467, 0.1182, 12],76390:[0.357, 0.2307, 0.168, 0.1095, 0.0867, 24],
 76392:[0.427, 0.303, 0.1897, 0.1739, 0.1261, 12],76393:[0.412, 0.2633, 0.1895, 0.1765, 0.1033, 12],76394:[0.403, 0.2217, 0.1857, 0.1377, 0.0799, 24],76396:[0.441, 0.2633, 0.2063, 0.1487, 0.1172, 12],
 76397:[0.432, 0.272, 0.206, 0.1547, 0.1258, 12],76398:[0.382, 0.255, 0.1682, 0.1647, 0.1103, 24],76400:[0.57, 0.319, 0.264, 0.1887, 0.1627, 12],76453:[0.484, 0.3213, 0.1832, 0.1517, 0.142, 12],
 76544:[0.389, 0.2467, 0.161, 0.168, 0.1233, 4],76545:[0.437, 0.282, 0.1928, 0.1547, 0.1137, 12],76548:[0.389, 0.2737, 0.2065, 0.1677, 0.102, 4],76549:[0.447, 0.2747, 0.2255, 0.1727, 0.1017, 12],
 76552:[0.42, 0.2727, 0.2155, 0.1435, 0.0976, 4],76553:[0.419, 0.286, 0.1943, 0.1737, 0.114, 12],76556:[0.452, 0.268, 0.2362, 0.1691, 0.1617, 4],76557:[0.516, 0.3087, 0.245, 0.183, 0.1373, 12],
 76596:[0.418, 0.2787, 0.2238, 0.1602, 0.1245, 12],76597:[0.382, 0.2473, 0.2258, 0.1751, 0.103, 12],76598:[0.352, 0.2307, 0.1695, 0.1467, 0.0873, 24],76600:[0.445, 0.266, 0.231, 0.1684, 0.11, 12],
 76601:[0.407, 0.234, 0.2207, 0.1715, 0.114, 12],76602:[0.407, 0.246, 0.1665, 0.1331, 0.0976, 24],76604:[0.444, 0.2993, 0.2143, 0.1604, 0.127, 12],76605:[0.465, 0.2843, 0.2285, 0.1647, 0.0976, 12],
 76606:[0.409, 0.2537, 0.1795, 0.1428, 0.095, 24],76608:[0.448, 0.3313, 0.2417, 0.215, 0.1195, 12],76609:[0.434, 0.29, 0.2523, 0.1987, 0.13, 12],76610:[0.426, 0.295, 0.2032, 0.1621, 0.1106, 24],
 76612:[0.54, 0.34, 0.2347, 0.2127, 0.1396, 12],76665:[0.506, 0.3247, 0.241, 0.1867, 0.151, 12],76752:[0.414, 0.2453, 0.1865, 0.1447, 0.1226, 4],76753:[0.392, 0.275, 0.208, 0.1504, 0.119, 12],
 76756:[0.457, 0.284, 0.2063, 0.1977, 0.1297, 4],76757:[0.456, 0.29, 0.196, 0.1897, 0.1205, 12],76760:[0.47, 0.2913, 0.2207, 0.1618, 0.1248, 4],76761:[0.485, 0.3027, 0.2403, 0.1669, 0.0943, 12],
 76764:[0.512, 0.325, 0.2253, 0.1907, 0.1427, 4],76765:[0.474, 0.3117, 0.2402, 0.1977, 0.1403, 12],76768:[0.456, 0.3437, 0.2507, 0.1979, 0.1195, 4],76769:[0.476, 0.3443, 0.2413, 0.2357, 0.154, 12],
 76804:[0.449, 0.2693, 0.1672, 0.1527, 0.1137, 12],76805:[0.436, 0.302, 0.2167, 0.1703, 0.0972, 12],76806:[0.432, 0.2493, 0.1927, 0.1054, 0.0963, 24],76808:[0.443, 0.27, 0.2305, 0.1815, 0.1147, 12],
 76809:[0.421, 0.2903, 0.2377, 0.1848, 0.108, 12],76810:[0.399, 0.2843, 0.2057, 0.1411, 0.1263, 24],76812:[0.492, 0.271, 0.228, 0.1895, 0.14, 12],76813:[0.452, 0.3123, 0.187, 0.1661, 0.154, 12],
 76814:[0.389, 0.226, 0.1782, 0.1421, 0.0935, 24],76816:[0.441, 0.352, 0.23, 0.179, 0.1317, 12],76817:[0.493, 0.3027, 0.25, 0.2071, 0.1322, 12],76818:[0.426, 0.294, 0.2003, 0.1377, 0.1235, 24],
 76820:[0.519, 0.317, 0.2713, 0.1914, 0.1603, 12],76821:[0.436, 0.3143, 0.2437, 0.2194, 0.1547, 12],76822:[0.45, 0.327, 0.2167, 0.1848, 0.1347, 24],76824:[0.56, 0.386, 0.309, 0.223, 0.1807, 12],
 76877:[0.569, 0.357, 0.2978, 0.2095, 0.1327, 12],76960:[0.43, 0.2757, 0.2132, 0.1727, 0.134, 4],76961:[0.454, 0.3157, 0.2288, 0.1987, 0.128, 12],76964:[0.423, 0.3023, 0.1967, 0.1511, 0.116, 4],
 76965:[0.461, 0.3257, 0.2485, 0.1421, 0.122, 12],76968:[0.426, 0.277, 0.1972, 0.1645, 0.1006, 4],76969:[0.436, 0.276, 0.2177, 0.1694, 0.1295, 12],76972:[0.441, 0.334, 0.2247, 0.18, 0.1206, 4],
 76973:[0.496, 0.282, 0.2272, 0.2369, 0.144, 12],76976:[0.483, 0.3247, 0.2477, 0.1862, 0.146, 4],76977:[0.492, 0.3237, 0.1842, 0.2044, 0.1757, 12],76980:[0.484, 0.3503, 0.237, 0.2201, 0.146, 4],
 76981:[0.487, 0.351, 0.262, 0.1891, 0.179, 12],77012:[0.423, 0.2973, 0.2292, 0.1737, 0.132, 12],77013:[0.454, 0.285, 0.2425, 0.1757, 0.1326, 12],77014:[0.408, 0.2563, 0.1922, 0.1597, 0.1033, 24],
 77016:[0.471, 0.27, 0.225, 0.194, 0.1303, 12],77017:[0.447, 0.2807, 0.2623, 0.1775, 0.1336, 12],77018:[0.391, 0.2733, 0.1927, 0.1517, 0.084, 24],77020:[0.451, 0.294, 0.2107, 0.1651, 0.143, 12],
 77021:[0.442, 0.2903, 0.2205, 0.1817, 0.1163, 12],77022:[0.457, 0.248, 0.2088, 0.1268, 0.1093, 24],77024:[0.459, 0.3193, 0.2485, 0.1833, 0.1387, 12],77025:[0.536, 0.3263, 0.2408, 0.2024, 0.1357, 12],
 77026:[0.473, 0.2947, 0.1822, 0.171, 0.095, 24],77028:[0.502, 0.3253, 0.2495, 0.2134, 0.1497, 12],77029:[0.487, 0.3147, 0.261, 0.2227, 0.1603, 12],77030:[0.476, 0.3203, 0.2175, 0.1927, 0.1406, 24],
 77032:[0.48, 0.3433, 0.2955, 0.1997, 0.1701, 12],77033:[0.486, 0.3637, 0.2743, 0.2077, 0.179, 12],77034:[0.489, 0.2887, 0.251, 0.1917, 0.1316, 24],77036:[0.566, 0.3737, 0.2985, 0.275, 0.187, 12],
 77089:[0.555, 0.3717, 0.286, 0.2413, 0.162, 12],79872:[0.581, 0.373, 0.2887, 0.1901, 0.1687, 12],79874:[0.568, 0.3247, 0.274, 0.1897, 0.102, 12],79876:[0.59, 0.41, 0.2875, 0.2118, 0.152, 12],
 79878:[0.557, 0.329, 0.274, 0.2204, 0.0999, 12],79880:[0.577, 0.4213, 0.2997, 0.2415, 0.1437, 12],79882:[0.584, 0.3503, 0.237, 0.1481, 0.1123, 12],79884:[0.574, 0.3747, 0.2795, 0.2204, 0.1292, 12],
 79886:[0.547, 0.3607, 0.306, 0.2505, 0.1416, 12],79888:[0.596, 0.4093, 0.259, 0.2381, 0.1897, 12],79890:[0.566, 0.371, 0.2695, 0.2097, 0.138, 12],79892:[0.602, 0.4063, 0.312, 0.2521, 0.159, 12],
 79894:[0.584, 0.3907, 0.2755, 0.2372, 0.1577, 12],79896:[0.645, 0.4173, 0.3155, 0.2504, 0.2007, 12],79898:[0.584, 0.3837, 0.3002, 0.2547, 0.179, 12],82656:[0.486, 0.3027, 0.207, 0.1308, 0.076, 4],
 86580:[0.448, 0.2993, 0.2162, 0.1772, 0.1683, 12],86633:[0.424, 0.257, 0.1582, 0.1304, 0.082, 12],86736:[0.381, 0.267, 0.1722, 0.1524, 0.0882, 4],86737:[0.397, 0.273, 0.1687, 0.1633, 0.1307, 12],
 86788:[0.363, 0.2843, 0.219, 0.1521, 0.1228, 12],86789:[0.381, 0.279, 0.1757, 0.1367, 0.0957, 12],86790:[0.366, 0.2233, 0.148, 0.1355, 0.0581, 24],86792:[0.479, 0.2693, 0.2327, 0.2151, 0.164, 12],
 86845:[0.44, 0.2947, 0.1917, 0.1293, 0.1347, 12],86944:[0.409, 0.2423, 0.1797, 0.1247, 0.0983, 4],86945:[0.405, 0.2477, 0.208, 0.1547, 0.0763, 12],86948:[0.444, 0.2827, 0.2153, 0.1503, 0.1317, 4],
 86949:[0.44, 0.296, 0.224, 0.1453, 0.1023, 12],86996:[0.402, 0.304, 0.2062, 0.158, 0.1252, 12],86997:[0.395, 0.24, 0.1842, 0.1573, 0.104, 12],86998:[0.402, 0.2053, 0.1373, 0.1314, 0.0783, 24],
 87000:[0.446, 0.2537, 0.2143, 0.1633, 0.1132, 12],87001:[0.466, 0.2533, 0.1522, 0.164, 0.117, 12],87002:[0.43, 0.2497, 0.1655, 0.1357, 0.106, 24],87004:[0.503, 0.324, 0.2535, 0.1901, 0.1597, 12],
 87057:[0.46, 0.2953, 0.2372, 0.1582, 0.1147, 12],87152:[0.42, 0.2507, 0.1747, 0.1421, 0.112, 4],87153:[0.442, 0.238, 0.1905, 0.1707, 0.126, 12],87156:[0.428, 0.2537, 0.183, 0.1643, 0.1081, 4],
 87157:[0.446, 0.2777, 0.2042, 0.1705, 0.1013, 12],87160:[0.409, 0.3, 0.2063, 0.1691, 0.1398, 4],87161:[0.456, 0.2967, 0.2233, 0.1514, 0.1257, 12],87204:[0.403, 0.2707, 0.2112, 0.141, 0.1287, 12],
 87205:[0.436, 0.29, 0.1627, 0.1607, 0.1049, 12],87206:[0.367, 0.214, 0.1645, 0.132, 0.082, 24],87208:[0.412, 0.289, 0.2028, 0.1307, 0.11, 12],87209:[0.425, 0.28, 0.2025, 0.1695, 0.13, 12],
 87210:[0.407, 0.2373, 0.172, 0.1443, 0.0962, 24],87212:[0.451, 0.3063, 0.205, 0.1761, 0.1322, 12],87213:[0.459, 0.299, 0.195, 0.1588, 0.1126, 12],87214:[0.413, 0.297, 0.2057, 0.1552, 0.0993, 24],
 87216:[0.514, 0.344, 0.25, 0.1973, 0.1632, 12],87269:[0.534, 0.296, 0.2307, 0.1447, 0.1226, 12],87360:[0.371, 0.2563, 0.1877, 0.1495, 0.1318, 4],87361:[0.445, 0.2927, 0.2107, 0.1454, 0.1166, 12],
 87364:[0.434, 0.2823, 0.2225, 0.1577, 0.1588, 4],87365:[0.455, 0.2913, 0.1902, 0.1723, 0.1206, 12],87368:[0.448, 0.302, 0.2122, 0.1509, 0.1192, 4],87369:[0.436, 0.314, 0.2327, 0.216, 0.1367, 12],
 87372:[0.45, 0.286, 0.264, 0.1853, 0.1195, 4],87373:[0.469, 0.3377, 0.2432, 0.1253, 0.1233, 12],87412:[0.425, 0.2667, 0.1967, 0.1917, 0.1093, 12],87413:[0.439, 0.27, 0.1938, 0.1444, 0.0975, 12],
 87414:[0.398, 0.2247, 0.1625, 0.1055, 0.0846, 24],87416:[0.416, 0.3233, 0.2128, 0.179, 0.111, 12],87417:[0.443, 0.2787, 0.2235, 0.171, 0.1303, 12],87418:[0.394, 0.255, 0.178, 0.1395, 0.0723, 24],
 87420:[0.476, 0.3107, 0.2508, 0.1834, 0.1388, 12],87421:[0.465, 0.3073, 0.238, 0.1571, 0.1323, 12],87422:[0.429, 0.2493, 0.182, 0.1508, 0.112, 24],87424:[0.481, 0.3047, 0.2535, 0.185, 0.1203, 12],
 87425:[0.431, 0.3227, 0.2172, 0.2061, 0.1217, 12],87426:[0.408, 0.2907, 0.2127, 0.1612, 0.1001, 24],87428:[0.54, 0.3647, 0.2503, 0.1921, 0.1877, 12],87481:[0.527, 0.2903, 0.2452, 0.1847, 0.1448, 12],
 87568:[0.413, 0.2407, 0.188, 0.1294, 0.0808, 4],87569:[0.422, 0.281, 0.215, 0.1687, 0.121, 12],87572:[0.442, 0.281, 0.2222, 0.1593, 0.1042, 4],87573:[0.476, 0.2637, 0.2198, 0.1887, 0.1433, 12],
 87576:[0.459, 0.322, 0.2458, 0.1871, 0.152, 4],87577:[0.449, 0.3347, 0.2335, 0.2057, 0.105, 12],87580:[0.444, 0.3017, 0.2202, 0.1734, 0.1185, 4],87581:[0.478, 0.318, 0.2542, 0.1946, 0.149, 12],
 87584:[0.47, 0.3503, 0.237, 0.2088, 0.142, 4],87585:[0.515, 0.3373, 0.2653, 0.1872, 0.1567, 12],87620:[0.451, 0.2937, 0.204, 0.1892, 0.1087, 12],87621:[0.446, 0.277, 0.2095, 0.176, 0.1077, 12],
 87622:[0.422, 0.27, 0.1925, 0.1247, 0.0841, 24],87624:[0.418, 0.2673, 0.225, 0.1707, 0.1413, 12],87625:[0.448, 0.307, 0.2205, 0.141, 0.1097, 12],87626:[0.419, 0.2277, 0.174, 0.15, 0.0907, 24],
 87628:[0.48, 0.3207, 0.2327, 0.2193, 0.1417, 12],87629:[0.511, 0.316, 0.2488, 0.149, 0.121, 12],87630:[0.414, 0.2747, 0.2037, 0.1667, 0.111, 24],87632:[0.464, 0.334, 0.207, 0.1899, 0.1176, 12],
 87633:[0.458, 0.285, 0.2867, 0.176, 0.129, 12],87634:[0.416, 0.2393, 0.201, 0.1424, 0.1095, 24],87636:[0.459, 0.309, 0.2225, 0.2243, 0.164, 12],87637:[0.467, 0.3127, 0.2292, 0.1997, 0.1895, 12],
 87638:[0.459, 0.258, 0.1992, 0.174, 0.1326, 24],87640:[0.614, 0.4127, 0.299, 0.222, 0.1913, 12],87693:[0.566, 0.374, 0.3087, 0.1877, 0.1533, 12],87776:[0.435, 0.249, 0.1892, 0.1721, 0.0988, 4],
 87777:[0.478, 0.2843, 0.1955, 0.1993, 0.1587, 12],87780:[0.436, 0.2907, 0.1643, 0.183, 0.1093, 4],87781:[0.437, 0.296, 0.22, 0.1627, 0.1277, 12],87784:[0.433, 0.3083, 0.2032, 0.2275, 0.1297, 4],
 87785:[0.5, 0.3153, 0.2265, 0.198, 0.1367, 12],87788:[0.477, 0.2977, 0.2427, 0.1693, 0.1393, 4],87789:[0.477, 0.3433, 0.2357, 0.1734, 0.1258, 12],87792:[0.46, 0.3403, 0.2392, 0.2267, 0.1247, 4],
 87793:[0.512, 0.3343, 0.2572, 0.1862, 0.1593, 12],87796:[0.492, 0.3193, 0.251, 0.2241, 0.1643, 4],87797:[0.467, 0.3567, 0.3042, 0.232, 0.1527, 12],87828:[0.383, 0.321, 0.2313, 0.1817, 0.1558, 12],
 87829:[0.467, 0.298, 0.1905, 0.1791, 0.1303, 12],87830:[0.438, 0.25, 0.1897, 0.1428, 0.0853, 24],87832:[0.464, 0.3137, 0.2317, 0.1753, 0.1601, 12],87833:[0.467, 0.248, 0.225, 0.1898, 0.1333, 12],
 87834:[0.412, 0.274, 0.1535, 0.1838, 0.0903, 24],87836:[0.46, 0.3753, 0.2465, 0.1723, 0.14, 12],87837:[0.483, 0.3037, 0.2125, 0.1633, 0.1405, 12],87838:[0.428, 0.2847, 0.188, 0.1367, 0.1257, 24],
 87840:[0.496, 0.3, 0.237, 0.193, 0.1243, 12],87841:[0.483, 0.3017, 0.2045, 0.223, 0.133, 12],87842:[0.427, 0.3077, 0.2365, 0.1484, 0.104, 24],87844:[0.478, 0.3527, 0.2725, 0.2175, 0.1413, 12],
 87845:[0.499, 0.3463, 0.2515, 0.2055, 0.1748, 12],87846:[0.438, 0.3373, 0.2183, 0.1927, 0.1167, 24],87848:[0.486, 0.34, 0.2947, 0.2248, 0.167, 12],87849:[0.473, 0.361, 0.2745, 0.2097, 0.1703, 12],
 87850:[0.48, 0.31, 0.238, 0.1952, 0.1413, 24],87852:[0.597, 0.384, 0.28, 0.2527, 0.205, 12],87905:[0.583, 0.375, 0.265, 0.2064, 0.175, 12],87984:[0.48, 0.2617, 0.2275, 0.1837, 0.1283, 4],
 87985:[0.448, 0.3113, 0.2425, 0.1951, 0.1517, 12],87988:[0.467, 0.2623, 0.2655, 0.1709, 0.1287, 4],87989:[0.44, 0.322, 0.2212, 0.1973, 0.1308, 12],87992:[0.477, 0.315, 0.201, 0.1973, 0.133, 4],
 87993:[0.495, 0.3053, 0.2302, 0.194, 0.1697, 12],87996:[0.46, 0.3103, 0.2103, 0.1765, 0.132, 4],87997:[0.508, 0.304, 0.2288, 0.2241, 0.1517, 12],88000:[0.496, 0.3073, 0.3115, 0.197, 0.15, 4],
 88001:[0.486, 0.3273, 0.2645, 0.2204, 0.172, 12],88004:[0.492, 0.3283, 0.2977, 0.24, 0.158, 4],88005:[0.554, 0.3373, 0.2923, 0.2591, 0.1631, 12],88008:[0.518, 0.363, 0.2875, 0.2271, 0.191, 4],
 88009:[0.515, 0.372, 0.3165, 0.24, 0.1967, 12],88036:[0.462, 0.296, 0.2328, 0.1713, 0.103, 12],88037:[0.458, 0.29, 0.219, 0.1814, 0.0956, 12],88038:[0.434, 0.2977, 0.2077, 0.1543, 0.1137, 24],
 88040:[0.485, 0.313, 0.2605, 0.2057, 0.1287, 12],88041:[0.483, 0.277, 0.2458, 0.1837, 0.1423, 12],88042:[0.427, 0.266, 0.2003, 0.1431, 0.0783, 24],88044:[0.469, 0.2947, 0.2567, 0.167, 0.1542, 12],
 88045:[0.484, 0.2923, 0.256, 0.1917, 0.1313, 12],88046:[0.434, 0.2537, 0.1993, 0.1401, 0.1178, 24],88048:[0.463, 0.3067, 0.2495, 0.1808, 0.1392, 12],88049:[0.478, 0.3027, 0.2347, 0.1613, 0.1143, 12],
 88050:[0.451, 0.295, 0.196, 0.1742, 0.1159, 24],88052:[0.496, 0.3197, 0.272, 0.2217, 0.1503, 12],88053:[0.51, 0.348, 0.2353, 0.2123, 0.1762, 12],88054:[0.468, 0.341, 0.2212, 0.189, 0.106, 24],
 88056:[0.528, 0.3363, 0.2858, 0.2743, 0.131, 12],88057:[0.473, 0.381, 0.2607, 0.2315, 0.1996, 12],88058:[0.52, 0.3603, 0.2582, 0.1961, 0.1543, 24],88060:[0.486, 0.3923, 0.2978, 0.2381, 0.1467, 12],
 88061:[0.514, 0.3533, 0.2903, 0.2505, 0.18, 12],88062:[0.481, 0.3513, 0.296, 0.2221, 0.1467, 24],88064:[0.659, 0.422, 0.3185, 0.276, 0.1882, 12],88117:[0.592, 0.4247, 0.329, 0.262, 0.1747, 12],
 90896:[0.611, 0.4263, 0.308, 0.2377, 0.172, 12],90898:[0.596, 0.3603, 0.2977, 0.222, 0.1367, 12],90900:[0.637, 0.419, 0.2818, 0.2011, 0.1547, 12],90902:[0.56, 0.4167, 0.2747, 0.1853, 0.1575, 12],
 90904:[0.581, 0.4063, 0.314, 0.2417, 0.141, 12],90906:[0.623, 0.3713, 0.2627, 0.2222, 0.1727, 12],90908:[0.676, 0.4207, 0.301, 0.2248, 0.1753, 12],90910:[0.628, 0.3933, 0.2857, 0.2193, 0.138, 12],
 90912:[0.617, 0.4143, 0.3275, 0.2837, 0.1943, 12],90914:[0.61, 0.4083, 0.295, 0.2044, 0.1483, 12],90916:[0.675, 0.4633, 0.343, 0.2373, 0.1678, 12],90918:[0.66, 0.3817, 0.2383, 0.2215, 0.1737, 12],
 90920:[0.651, 0.4727, 0.353, 0.2601, 0.2032, 12],90922:[0.645, 0.4297, 0.32, 0.2587, 0.198, 12],90924:[0.661, 0.4747, 0.351, 0.2827, 0.21, 12],90926:[0.642, 0.387, 0.3225, 0.252, 0.16, 12],
 93684:[0.542, 0.3027, 0.206, 0.143, 0.09, 4],97396:[0.472, 0.308, 0.2172, 0.1722, 0.105, 12],97449:[0.409, 0.2407, 0.1717, 0.1557, 0.107, 12],97552:[0.418, 0.2353, 0.192, 0.1265, 0.099, 4],
 97553:[0.412, 0.2617, 0.1912, 0.1651, 0.0893, 12],97604:[0.438, 0.2683, 0.2075, 0.1671, 0.118, 12],97605:[0.391, 0.2757, 0.193, 0.1627, 0.0967, 12],97606:[0.389, 0.233, 0.1447, 0.1304, 0.0723, 24],
 97608:[0.46, 0.3363, 0.2357, 0.1739, 0.1213, 12],97661:[0.477, 0.2923, 0.1995, 0.166, 0.1283, 12],97760:[0.427, 0.238, 0.212, 0.1707, 0.1138, 4],97761:[0.399, 0.3013, 0.2267, 0.1599, 0.0911, 12],
 97764:[0.442, 0.2597, 0.214, 0.18, 0.1182, 4],97765:[0.459, 0.3093, 0.2087, 0.1847, 0.1213, 12],97812:[0.379, 0.218, 0.2193, 0.1774, 0.1089, 12],97813:[0.414, 0.2723, 0.198, 0.1642, 0.0977, 12],
 97814:[0.429, 0.229, 0.1757, 0.1247, 0.0913, 24],97816:[0.454, 0.275, 0.1887, 0.1529, 0.1343, 12],97817:[0.438, 0.29, 0.1933, 0.1597, 0.1465, 12],97818:[0.407, 0.224, 0.1612, 0.1371, 0.0761, 24],
 97820:[0.526, 0.3133, 0.2315, 0.1917, 0.1543, 12],97873:[0.5, 0.333, 0.2052, 0.1565, 0.1087, 12],97968:[0.46, 0.2827, 0.1893, 0.1583, 0.107, 4],97969:[0.443, 0.2757, 0.2005, 0.1731, 0.1123, 12],
 97972:[0.433, 0.2533, 0.2435, 0.1412, 0.1287, 4],97973:[0.443, 0.3137, 0.2402, 0.1274, 0.1366, 12],97976:[0.458, 0.2873, 0.2352, 0.1718, 0.1242, 4],97977:[0.495, 0.293, 0.16, 0.1801, 0.1207, 12],
 98020:[0.445, 0.3073, 0.2017, 0.1707, 0.123, 12],98021:[0.462, 0.285, 0.1902, 0.1689, 0.1059, 12],98022:[0.409, 0.2327, 0.1842, 0.1287, 0.084, 24],98024:[0.449, 0.311, 0.2183, 0.164, 0.1536, 12],
 98025:[0.43, 0.2853, 0.2095, 0.1708, 0.131, 12],98026:[0.428, 0.2527, 0.164, 0.1631, 0.0943, 24],98028:[0.467, 0.311, 0.2097, 0.1907, 0.149, 12],98029:[0.455, 0.2797, 0.1968, 0.2017, 0.1227, 12],
 98030:[0.416, 0.223, 0.1717, 0.1299, 0.1071, 24],98032:[0.535, 0.3503, 0.209, 0.184, 0.175, 12],98085:[0.515, 0.331, 0.1907, 0.1669, 0.1293, 12],98176:[0.374, 0.281, 0.1852, 0.134, 0.1116, 4],
 98177:[0.405, 0.2537, 0.1832, 0.1523, 0.0936, 12],98180:[0.435, 0.2567, 0.1937, 0.1848, 0.0997, 4],98181:[0.428, 0.2947, 0.2, 0.1783, 0.1281, 12],98184:[0.462, 0.2637, 0.2257, 0.1559, 0.0959, 4],
 98185:[0.422, 0.29, 0.2242, 0.1741, 0.132, 12],98188:[0.503, 0.2843, 0.2192, 0.1708, 0.123, 4],98189:[0.422, 0.313, 0.2183, 0.1727, 0.1588, 12],98228:[0.397, 0.2977, 0.1945, 0.1247, 0.111, 12],
 98229:[0.42, 0.2537, 0.2002, 0.1447, 0.1022, 12],98230:[0.411, 0.2237, 0.1525, 0.0997, 0.0973, 24],98232:[0.517, 0.3273, 0.2197, 0.1921, 0.114, 12],98233:[0.403, 0.2823, 0.2078, 0.177, 0.1195, 12],
 98234:[0.398, 0.242, 0.1732, 0.141, 0.0887, 24],98236:[0.487, 0.2917, 0.2392, 0.1614, 0.1326, 12],98237:[0.434, 0.2463, 0.1928, 0.2063, 0.1197, 12],98238:[0.441, 0.271, 0.2018, 0.117, 0.088, 24],
 98240:[0.509, 0.289, 0.249, 0.2142, 0.1333, 12],98241:[0.467, 0.2973, 0.227, 0.1644, 0.11, 12],98242:[0.436, 0.2963, 0.2047, 0.1873, 0.125, 24],98244:[0.546, 0.303, 0.2372, 0.2047, 0.155, 12],
 98297:[0.51, 0.301, 0.2297, 0.162, 0.142, 12],98384:[0.429, 0.262, 0.2038, 0.1331, 0.12, 4],98385:[0.408, 0.2933, 0.195, 0.138, 0.1223, 12],98388:[0.427, 0.304, 0.1705, 0.1513, 0.1118, 4],
 98389:[0.458, 0.3123, 0.1892, 0.1804, 0.1453, 12],98392:[0.442, 0.2773, 0.217, 0.1732, 0.1113, 4],98393:[0.458, 0.3167, 0.2485, 0.2087, 0.1233, 12],98396:[0.511, 0.3067, 0.2088, 0.1798, 0.1495, 4],
 98397:[0.462, 0.3087, 0.2337, 0.1711, 0.1288, 12],98400:[0.464, 0.301, 0.2152, 0.2007, 0.148, 4],98401:[0.494, 0.335, 0.2437, 0.2307, 0.1328, 12],98436:[0.433, 0.2737, 0.2287, 0.1813, 0.1296, 12],
 98437:[0.443, 0.2717, 0.2235, 0.1187, 0.1002, 12],98438:[0.425, 0.2253, 0.1673, 0.1293, 0.0602, 24],98440:[0.448, 0.2373, 0.1932, 0.1361, 0.1113, 12],98441:[0.438, 0.3183, 0.1807, 0.1721, 0.1366, 12],
 98442:[0.42, 0.2527, 0.1897, 0.145, 0.0856, 24],98444:[0.48, 0.303, 0.2197, 0.1843, 0.1288, 12],98445:[0.453, 0.2957, 0.218, 0.1727, 0.102, 12],98446:[0.44, 0.2727, 0.1707, 0.144, 0.1138, 24],
 98448:[0.462, 0.3167, 0.2253, 0.1812, 0.148, 12],98449:[0.478, 0.2863, 0.2183, 0.177, 0.1387, 12],98450:[0.462, 0.276, 0.1862, 0.1703, 0.092, 24],98452:[0.495, 0.2917, 0.2685, 0.2073, 0.1578, 12],
 98453:[0.488, 0.341, 0.2545, 0.1834, 0.1403, 12],98454:[0.464, 0.2747, 0.2287, 0.1811, 0.114, 24],98456:[0.612, 0.404, 0.2585, 0.2101, 0.1493, 12],98509:[0.527, 0.386, 0.259, 0.203, 0.1387, 12],
 98592:[0.438, 0.2783, 0.2117, 0.1674, 0.1391, 4],98593:[0.476, 0.2777, 0.2358, 0.1365, 0.1467, 12],98596:[0.436, 0.3027, 0.1758, 0.1823, 0.1233, 4],98597:[0.444, 0.298, 0.2365, 0.1763, 0.1347, 12],
 98600:[0.444, 0.2887, 0.1835, 0.1534, 0.1177, 4],98601:[0.474, 0.331, 0.2007, 0.1811, 0.1133, 12],98604:[0.48, 0.3093, 0.2118, 0.1874, 0.1317, 4],98605:[0.511, 0.328, 0.2552, 0.1953, 0.1875, 12],
 98608:[0.499, 0.3527, 0.2768, 0.1825, 0.1222, 4],98609:[0.492, 0.3313, 0.258, 0.18, 0.1676, 12],98612:[0.516, 0.3397, 0.2577, 0.1783, 0.1439, 4],98613:[0.516, 0.369, 0.2465, 0.2365, 0.1733, 12],
 98644:[0.453, 0.2757, 0.2005, 0.2147, 0.1042, 12],98645:[0.448, 0.315, 0.2137, 0.1587, 0.099, 12],98646:[0.404, 0.281, 0.1833, 0.1381, 0.0793, 24],98648:[0.439, 0.2547, 0.2223, 0.1957, 0.122, 12],
 98649:[0.426, 0.2923, 0.2072, 0.1865, 0.1073, 12],98650:[0.409, 0.243, 0.1833, 0.1511, 0.119, 24],98652:[0.445, 0.3083, 0.241, 0.1893, 0.1546, 12],98653:[0.436, 0.313, 0.2285, 0.1488, 0.1555, 12],
 98654:[0.461, 0.282, 0.1868, 0.1687, 0.0826, 24],98656:[0.536, 0.3227, 0.2537, 0.2034, 0.1553, 12],98657:[0.487, 0.3037, 0.256, 0.21, 0.1383, 12],98658:[0.493, 0.2563, 0.193, 0.1757, 0.1099, 24],
 98660:[0.455, 0.305, 0.2048, 0.2353, 0.127, 12],98661:[0.506, 0.3263, 0.2107, 0.2123, 0.1263, 12],98662:[0.459, 0.2993, 0.2017, 0.17, 0.1117, 24],98664:[0.519, 0.295, 0.2787, 0.2227, 0.1643, 12],
 98665:[0.47, 0.382, 0.2713, 0.1881, 0.1533, 12],98666:[0.525, 0.3317, 0.2267, 0.1784, 0.1547, 24],98668:[0.623, 0.408, 0.295, 0.2683, 0.1805, 12],98721:[0.603, 0.3803, 0.2932, 0.2167, 0.1627, 12],
 98800:[0.454, 0.2837, 0.187, 0.1771, 0.1253, 4],98801:[0.464, 0.319, 0.2323, 0.2167, 0.1633, 12],98804:[0.458, 0.323, 0.2112, 0.1414, 0.1113, 4],98805:[0.481, 0.3177, 0.2367, 0.1875, 0.1202, 12],
 98808:[0.491, 0.3287, 0.2462, 0.1701, 0.1277, 4],98809:[0.465, 0.2973, 0.2605, 0.176, 0.1223, 12],98812:[0.477, 0.2907, 0.257, 0.1777, 0.1329, 4],98813:[0.476, 0.338, 0.2468, 0.1489, 0.1463, 12],
 98816:[0.457, 0.3353, 0.2375, 0.2063, 0.1623, 4],98817:[0.478, 0.342, 0.2802, 0.2121, 0.1287, 12],98820:[0.51, 0.3273, 0.258, 0.1957, 0.1518, 4],98821:[0.521, 0.4127, 0.257, 0.2461, 0.1633, 12],
 98824:[0.52, 0.3587, 0.28, 0.2012, 0.1572, 4],98825:[0.51, 0.38, 0.2892, 0.2401, 0.1675, 12],98852:[0.45, 0.2903, 0.207, 0.1944, 0.1713, 12],98853:[0.477, 0.3337, 0.2265, 0.1918, 0.1341, 12],
 98854:[0.422, 0.2893, 0.1818, 0.1541, 0.1047, 24],98856:[0.443, 0.32, 0.2305, 0.2003, 0.141, 12],98857:[0.452, 0.3007, 0.233, 0.2004, 0.1463, 12],98858:[0.405, 0.283, 0.2007, 0.1347, 0.1102, 24],
 98860:[0.537, 0.3003, 0.2477, 0.1908, 0.1417, 12],98861:[0.516, 0.343, 0.2527, 0.1893, 0.142, 12],98862:[0.488, 0.2823, 0.2132, 0.169, 0.1116, 24],98864:[0.536, 0.3307, 0.2435, 0.1973, 0.1698, 12],
 98865:[0.522, 0.36, 0.2565, 0.2094, 0.137, 12],98866:[0.481, 0.3253, 0.2, 0.1147, 0.1232, 24],98868:[0.513, 0.3543, 0.2447, 0.1807, 0.1503, 12],98869:[0.493, 0.3477, 0.235, 0.1971, 0.1375, 12],
 98870:[0.463, 0.3417, 0.2257, 0.177, 0.1323, 24],98872:[0.518, 0.2993, 0.2742, 0.2443, 0.1937, 12],98873:[0.53, 0.3547, 0.2762, 0.2405, 0.1645, 12],98874:[0.495, 0.3443, 0.2225, 0.1937, 0.1782, 24],
 98876:[0.53, 0.359, 0.2933, 0.2484, 0.1802, 12],98877:[0.533, 0.3447, 0.282, 0.2271, 0.1577, 12],98878:[0.495, 0.3513, 0.2478, 0.2163, 0.1343, 24],98880:[0.65, 0.4427, 0.3062, 0.286, 0.199, 12],
 98933:[0.657, 0.4167, 0.3162, 0.2397, 0.185, 12],99008:[0.475, 0.3093, 0.2627, 0.2227, 0.1215, 4],99009:[0.463, 0.3373, 0.2392, 0.2334, 0.137, 12],99012:[0.501, 0.3447, 0.272, 0.1791, 0.1295, 4],
 99013:[0.477, 0.304, 0.2643, 0.2224, 0.1641, 12],99016:[0.468, 0.3397, 0.2393, 0.1665, 0.1393, 4],99017:[0.483, 0.3297, 0.263, 0.2175, 0.1708, 12],99020:[0.497, 0.344, 0.2422, 0.2154, 0.1407, 4],
 99021:[0.541, 0.3437, 0.2632, 0.205, 0.1513, 12],99024:[0.457, 0.381, 0.2292, 0.1657, 0.1523, 4],99025:[0.507, 0.3613, 0.283, 0.2157, 0.164, 12],99028:[0.491, 0.332, 0.2407, 0.2177, 0.1533, 4],
 99029:[0.521, 0.395, 0.2897, 0.2153, 0.1593, 12],99032:[0.553, 0.401, 0.297, 0.2203, 0.1667, 4],99033:[0.505, 0.3877, 0.2695, 0.2457, 0.2203, 12],99036:[0.556, 0.4023, 0.3233, 0.2744, 0.1747, 4],
 99037:[0.601, 0.3953, 0.3335, 0.2934, 0.204, 12],99060:[0.465, 0.3507, 0.2683, 0.193, 0.1693, 12],99061:[0.501, 0.3623, 0.2598, 0.21, 0.164, 12],99062:[0.462, 0.3587, 0.2167, 0.2013, 0.1186, 24],
 99064:[0.491, 0.364, 0.2447, 0.2037, 0.1773, 12],99065:[0.493, 0.339, 0.2557, 0.2287, 0.159, 12],99066:[0.506, 0.3203, 0.2337, 0.1767, 0.1446, 24],99068:[0.514, 0.3293, 0.2235, 0.2033, 0.174, 12],
 99069:[0.48, 0.3727, 0.2377, 0.202, 0.1556, 12],99070:[0.455, 0.2953, 0.2502, 0.172, 0.1398, 24],99072:[0.486, 0.3227, 0.2805, 0.217, 0.1816, 12],99073:[0.488, 0.3787, 0.2813, 0.2004, 0.1896, 12],
 99074:[0.509, 0.3197, 0.2415, 0.2174, 0.137, 24],99076:[0.509, 0.3387, 0.249, 0.2351, 0.1416, 12],99077:[0.532, 0.3607, 0.2532, 0.21, 0.1802, 12],99078:[0.501, 0.319, 0.2353, 0.2183, 0.1254, 24],
 99080:[0.532, 0.3697, 0.244, 0.2493, 0.1866, 12],99081:[0.52, 0.3843, 0.3177, 0.1897, 0.1767, 12],99082:[0.559, 0.3017, 0.248, 0.225, 0.122, 24],99084:[0.535, 0.3967, 0.307, 0.239, 0.1737, 12],
 99085:[0.542, 0.365, 0.29, 0.242, 0.1723, 12],99086:[0.534, 0.3417, 0.2812, 0.2163, 0.1357, 24],99088:[0.535, 0.413, 0.3112, 0.2887, 0.2063, 12],99089:[0.517, 0.4097, 0.321, 0.2848, 0.1893, 12],
 99090:[0.496, 0.4, 0.2877, 0.2381, 0.158, 24],99092:[0.627, 0.4827, 0.3675, 0.299, 0.2227, 12],99145:[0.639, 0.4387, 0.3687, 0.2757, 0.2017, 12],101920:[0.663, 0.434, 0.37, 0.274, 0.175, 12],
 101922:[0.629, 0.4323, 0.2803, 0.208, 0.1443, 12],101924:[0.638, 0.4623, 0.3105, 0.3031, 0.1637, 12],101926:[0.632, 0.4237, 0.3435, 0.228, 0.169, 12],101928:[0.65, 0.458, 0.352, 0.278, 0.2003, 12],
 101930:[0.632, 0.4253, 0.3247, 0.2291, 0.1613, 12],101932:[0.64, 0.4723, 0.386, 0.2977, 0.1717, 12],101934:[0.608, 0.4127, 0.3022, 0.258, 0.1273, 12],101936:[0.652, 0.46, 0.341, 0.2647, 0.2127, 12],
 101938:[0.656, 0.4133, 0.3465, 0.2434, 0.1763, 12],101940:[0.629, 0.4783, 0.306, 0.3004, 0.2152, 12],101942:[0.64, 0.4607, 0.31, 0.3001, 0.1717, 12],101944:[0.66, 0.438, 0.346, 0.28, 0.2223, 12],
 101946:[0.647, 0.4137, 0.327, 0.2361, 0.185, 12],101948:[0.649, 0.5007, 0.3305, 0.321, 0.219, 12],101950:[0.676, 0.4797, 0.3245, 0.2507, 0.1923, 12],101952:[0.67, 0.467, 0.3667, 0.322, 0.2317, 12],
 101954:[0.621, 0.4477, 0.374, 0.2717, 0.2073, 12],104712:[0.601, 0.381, 0.238, 0.188, 0.103, 4],108212:[0.486, 0.3103, 0.2407, 0.2404, 0.1595, 12],108265:[0.433, 0.2877, 0.163, 0.1559, 0.1033, 12],
 108368:[0.415, 0.2793, 0.1995, 0.1604, 0.118, 4],108369:[0.442, 0.2927, 0.2488, 0.1438, 0.144, 12],108420:[0.428, 0.2873, 0.2247, 0.1937, 0.112, 12],108421:[0.446, 0.2987, 0.208, 0.1652, 0.1105, 12],
 108422:[0.427, 0.2303, 0.1435, 0.1421, 0.0792, 24],108424:[0.478, 0.316, 0.24, 0.1867, 0.1743, 12],108477:[0.43, 0.258, 0.1583, 0.16, 0.134, 12],108576:[0.436, 0.274, 0.1635, 0.147, 0.1257, 4],
 108577:[0.46, 0.269, 0.2197, 0.1823, 0.0977, 12],108580:[0.478, 0.2937, 0.1977, 0.1315, 0.1127, 4],108581:[0.472, 0.323, 0.1857, 0.1913, 0.1333, 12],108628:[0.441, 0.2707, 0.221, 0.1748, 0.1315, 12],
 108629:[0.418, 0.3037, 0.2123, 0.1607, 0.1373, 12],108630:[0.408, 0.2627, 0.174, 0.1502, 0.077, 24],108632:[0.466, 0.2997, 0.2278, 0.1659, 0.1373, 12],108633:[0.406, 0.31, 0.2113, 0.1677, 0.1141, 12],
 108634:[0.431, 0.2303, 0.1798, 0.1227, 0.0803, 24],108636:[0.519, 0.3153, 0.2467, 0.2041, 0.1467, 12],108689:[0.485, 0.3113, 0.2328, 0.172, 0.1243, 12],108784:[0.449, 0.2647, 0.1822, 0.1727, 0.1157, 4],
 108785:[0.426, 0.323, 0.2327, 0.1761, 0.1437, 12],108788:[0.436, 0.2863, 0.2352, 0.1499, 0.1125, 4],108789:[0.44, 0.327, 0.2367, 0.1897, 0.1119, 12],108792:[0.481, 0.2863, 0.2222, 0.212, 0.1408, 4],
 108793:[0.518, 0.3437, 0.2247, 0.1991, 0.1303, 12],108836:[0.455, 0.263, 0.2395, 0.1705, 0.1413, 12],108837:[0.419, 0.3033, 0.2038, 0.1907, 0.1187, 12],108838:[0.418, 0.2237, 0.1807, 0.1038, 0.0893, 24],
 108840:[0.466, 0.324, 0.232, 0.2081, 0.1176, 12],108841:[0.458, 0.334, 0.2505, 0.2183, 0.1277, 12],108842:[0.4, 0.264, 0.2367, 0.1719, 0.1195, 24],108844:[0.495, 0.3133, 0.2372, 0.197, 0.1256, 12],
 108845:[0.509, 0.2897, 0.2387, 0.1767, 0.1193, 12],108846:[0.459, 0.266, 0.2113, 0.1419, 0.1123, 24],108848:[0.511, 0.3253, 0.2478, 0.2048, 0.1463, 12],108901:[0.504, 0.3357, 0.2055, 0.169, 0.102, 12],
 108992:[0.455, 0.2557, 0.2012, 0.168, 0.114, 4],108993:[0.422, 0.2817, 0.226, 0.1444, 0.1276, 12],108996:[0.454, 0.2727, 0.1975, 0.1797, 0.0953, 4],108997:[0.467, 0.3027, 0.2193, 0.1888, 0.1132, 12],
 109000:[0.454, 0.283, 0.2, 0.1975, 0.1387, 4],109001:[0.48, 0.273, 0.2548, 0.2105, 0.1473, 12],109004:[0.459, 0.316, 0.2207, 0.1694, 0.1433, 4],109005:[0.502, 0.3157, 0.2307, 0.1918, 0.148, 12],
 109044:[0.475, 0.2897, 0.2162, 0.1817, 0.1233, 12],109045:[0.409, 0.2567, 0.1687, 0.1495, 0.1161, 12],109046:[0.398, 0.221, 0.1642, 0.1217, 0.085, 24],109048:[0.457, 0.276, 0.2063, 0.1461, 0.1382, 12],
 109049:[0.464, 0.2783, 0.2188, 0.1747, 0.1247, 12],109050:[0.466, 0.2787, 0.1772, 0.1487, 0.0668, 24],109052:[0.474, 0.3217, 0.2732, 0.1797, 0.153, 12],109053:[0.477, 0.3167, 0.2457, 0.1834, 0.1172, 12],
 109054:[0.436, 0.322, 0.1845, 0.1227, 0.0848, 24],109056:[0.518, 0.317, 0.2445, 0.1739, 0.1553, 12],109057:[0.462, 0.33, 0.2787, 0.1991, 0.118, 12],109058:[0.439, 0.3083, 0.2018, 0.141, 0.145, 24],
 109060:[0.524, 0.3503, 0.2702, 0.2191, 0.131, 12],109113:[0.528, 0.3517, 0.2115, 0.1685, 0.1132, 12],109200:[0.434, 0.296, 0.1665, 0.1381, 0.1118, 4],109201:[0.438, 0.2573, 0.2005, 0.132, 0.117, 12],
 109204:[0.455, 0.2673, 0.194, 0.1557, 0.1046, 4],109205:[0.472, 0.282, 0.2087, 0.1687, 0.0907, 12],109208:[0.494, 0.237, 0.2027, 0.1501, 0.1353, 4],109209:[0.443, 0.2927, 0.2282, 0.1907, 0.145, 12],
 109212:[0.48, 0.2987, 0.2182, 0.194, 0.132, 4],109213:[0.464, 0.323, 0.2582, 0.2301, 0.1133, 12],109216:[0.461, 0.316, 0.2138, 0.179, 0.1141, 4],109217:[0.509, 0.3333, 0.2702, 0.1971, 0.1412, 12],
 109252:[0.481, 0.2827, 0.2013, 0.1797, 0.0896, 12],109253:[0.454, 0.2443, 0.1903, 0.1661, 0.1016, 12],109254:[0.403, 0.265, 0.1627, 0.1223, 0.0773, 24],109256:[0.422, 0.2827, 0.225, 0.1743, 0.1202, 12],
 109257:[0.444, 0.304, 0.2325, 0.1598, 0.133, 12],109258:[0.455, 0.2567, 0.1692, 0.1397, 0.0819, 24],109260:[0.486, 0.3003, 0.2282, 0.2101, 0.1287, 12],109261:[0.487, 0.2497, 0.2228, 0.2075, 0.1528, 12],
 109262:[0.443, 0.274, 0.1947, 0.1438, 0.1181, 24],109264:[0.475, 0.2997, 0.2333, 0.1617, 0.1636, 12],109265:[0.471, 0.3067, 0.2575, 0.1905, 0.1303, 12],109266:[0.443, 0.295, 0.2117, 0.1758, 0.1035, 24],
 109268:[0.508, 0.3453, 0.2477, 0.1815, 0.1543, 12],109269:[0.497, 0.3043, 0.2447, 0.1587, 0.1537, 12],109270:[0.509, 0.3107, 0.1845, 0.1483, 0.103, 24],109272:[0.566, 0.3807, 0.2747, 0.2214, 0.1965, 12],
 109325:[0.525, 0.3227, 0.233, 0.1925, 0.1286, 12],109408:[0.427, 0.2593, 0.2207, 0.1574, 0.103, 4],109409:[0.49, 0.2837, 0.247, 0.1725, 0.119, 12],109412:[0.417, 0.2803, 0.209, 0.1465, 0.1093, 4],
 109413:[0.442, 0.2923, 0.2087, 0.1844, 0.1186, 12],109416:[0.467, 0.313, 0.2195, 0.184, 0.1105, 4],109417:[0.475, 0.33, 0.246, 0.1821, 0.1239, 12],109420:[0.512, 0.3367, 0.24, 0.214, 0.138, 4],
 109421:[0.489, 0.3067, 0.2862, 0.1784, 0.1546, 12],109424:[0.51, 0.3077, 0.2518, 0.1978, 0.138, 4],109425:[0.491, 0.3493, 0.2427, 0.2193, 0.1687, 12],109428:[0.428, 0.3553, 0.267, 0.1727, 0.1526, 4],
 109429:[0.535, 0.3393, 0.2682, 0.1998, 0.1417, 12],109460:[0.458, 0.3027, 0.2455, 0.1594, 0.1295, 12],109461:[0.468, 0.299, 0.2357, 0.163, 0.1087, 12],109462:[0.436, 0.243, 0.1932, 0.1197, 0.0817, 24],
 109464:[0.474, 0.316, 0.2012, 0.2087, 0.1037, 12],109465:[0.463, 0.308, 0.212, 0.1915, 0.099, 12],109466:[0.386, 0.266, 0.1543, 0.1748, 0.0705, 24],109468:[0.469, 0.313, 0.2173, 0.1744, 0.142, 12],
 109469:[0.483, 0.3113, 0.2173, 0.1636, 0.135, 12],109470:[0.455, 0.2833, 0.1915, 0.1511, 0.108, 24],109472:[0.539, 0.3363, 0.252, 0.2254, 0.145, 12],109473:[0.503, 0.3537, 0.2355, 0.184, 0.1337, 12],
 109474:[0.461, 0.278, 0.2095, 0.1563, 0.0893, 24],109476:[0.493, 0.3603, 0.2853, 0.2, 0.1355, 12],109477:[0.529, 0.3367, 0.2367, 0.1913, 0.1227, 12],109478:[0.454, 0.3373, 0.2627, 0.1567, 0.1077, 24],
 109480:[0.522, 0.3647, 0.2532, 0.2143, 0.1973, 12],109481:[0.517, 0.354, 0.3077, 0.2317, 0.1657, 12],109482:[0.53, 0.3263, 0.2532, 0.1938, 0.0996, 24],109484:[0.602, 0.404, 0.295, 0.2217, 0.1717, 12],
 109537:[0.629, 0.368, 0.272, 0.2471, 0.1297, 12],109616:[0.464, 0.2983, 0.2783, 0.1825, 0.1289, 4],109617:[0.474, 0.3167, 0.2495, 0.1984, 0.135, 12],109620:[0.454, 0.296, 0.2073, 0.1593, 0.1383, 4],
 109621:[0.468, 0.3197, 0.2157, 0.1845, 0.1122, 12],109624:[0.467, 0.3163, 0.195, 0.1749, 0.1366, 4],109625:[0.478, 0.3197, 0.2457, 0.2087, 0.157, 12],109628:[0.47, 0.336, 0.2307, 0.1843, 0.1363, 4],
 109629:[0.544, 0.3063, 0.2693, 0.1993, 0.1637, 12],109632:[0.495, 0.325, 0.2638, 0.1954, 0.1355, 4],109633:[0.514, 0.3593, 0.2765, 0.202, 0.1537, 12],109636:[0.531, 0.3263, 0.2915, 0.1965, 0.1487, 4],
 109637:[0.521, 0.3287, 0.2763, 0.1857, 0.1693, 12],109640:[0.509, 0.3537, 0.2662, 0.1998, 0.161, 4],109641:[0.513, 0.371, 0.285, 0.204, 0.1477, 12],109668:[0.457, 0.3113, 0.2577, 0.1783, 0.1528, 12],
 109669:[0.485, 0.299, 0.2192, 0.2037, 0.1227, 12],109670:[0.442, 0.2883, 0.1975, 0.1513, 0.0757, 24],109672:[0.476, 0.3183, 0.2408, 0.1781, 0.1483, 12],109673:[0.481, 0.3297, 0.2137, 0.1887, 0.125, 12],
 109674:[0.456, 0.3247, 0.2188, 0.1417, 0.0873, 24],109676:[0.509, 0.328, 0.252, 0.1843, 0.1416, 12],109677:[0.501, 0.3373, 0.217, 0.1844, 0.1342, 12],109678:[0.42, 0.2763, 0.2312, 0.164, 0.0898, 24],
 109680:[0.493, 0.3287, 0.2307, 0.2021, 0.159, 12],109681:[0.486, 0.349, 0.2543, 0.1934, 0.1456, 12],109682:[0.48, 0.3173, 0.2265, 0.1635, 0.0962, 24],109684:[0.51, 0.341, 0.2595, 0.192, 0.1507, 12],
 109685:[0.535, 0.289, 0.2955, 0.2524, 0.1617, 12],109686:[0.508, 0.3023, 0.2247, 0.1845, 0.1446, 24],109688:[0.548, 0.352, 0.237, 0.2067, 0.1493, 12],109689:[0.543, 0.3577, 0.2842, 0.2224, 0.151, 12],
 109690:[0.544, 0.324, 0.2665, 0.1738, 0.1303, 24],109692:[0.555, 0.3967, 0.2855, 0.2463, 0.1642, 12],109693:[0.552, 0.3843, 0.268, 0.2383, 0.1552, 12],109694:[0.533, 0.3683, 0.2447, 0.2051, 0.143, 24],
 109696:[0.615, 0.421, 0.3567, 0.2618, 0.1843, 12],109749:[0.617, 0.4537, 0.2875, 0.242, 0.1647, 12],109824:[0.471, 0.3053, 0.2435, 0.1944, 0.1625, 4],109825:[0.488, 0.361, 0.283, 0.185, 0.1267, 12],
 109828:[0.508, 0.338, 0.2688, 0.2327, 0.1373, 4],109829:[0.508, 0.361, 0.2442, 0.2041, 0.1533, 12],109832:[0.496, 0.3563, 0.257, 0.193, 0.1623, 4],109833:[0.495, 0.324, 0.3165, 0.2241, 0.1672, 12],
 109836:[0.52, 0.3307, 0.2587, 0.1837, 0.155, 4],109837:[0.539, 0.3107, 0.2743, 0.2291, 0.1405, 12],109840:[0.515, 0.3453, 0.282, 0.217, 0.162, 4],109841:[0.542, 0.346, 0.2837, 0.2273, 0.1558, 12],
 109844:[0.508, 0.3333, 0.2585, 0.1937, 0.1646, 4],109845:[0.512, 0.3847, 0.303, 0.2302, 0.1643, 12],109848:[0.497, 0.363, 0.3142, 0.2311, 0.1903, 4],109849:[0.526, 0.3747, 0.2873, 0.27, 0.1793, 12],
 109852:[0.531, 0.41, 0.3325, 0.2147, 0.1829, 4],109853:[0.54, 0.3857, 0.3135, 0.266, 0.187, 12],109876:[0.465, 0.3597, 0.2667, 0.19, 0.1627, 12],109877:[0.489, 0.3583, 0.255, 0.1661, 0.1648, 12],
 109878:[0.442, 0.2973, 0.236, 0.1529, 0.1317, 24],109880:[0.5, 0.332, 0.2853, 0.1794, 0.1578, 12],109881:[0.492, 0.3467, 0.272, 0.232, 0.1488, 12],109882:[0.474, 0.3183, 0.2162, 0.1919, 0.1543, 24],
 109884:[0.54, 0.354, 0.2772, 0.213, 0.1453, 12],109885:[0.523, 0.3657, 0.234, 0.1973, 0.1527, 12],109886:[0.496, 0.302, 0.245, 0.1654, 0.1313, 24],109888:[0.515, 0.3397, 0.2512, 0.2121, 0.1573, 12],
 109889:[0.506, 0.3903, 0.2767, 0.2194, 0.1336, 12],109890:[0.489, 0.2743, 0.2545, 0.18, 0.1387, 24],109892:[0.502, 0.355, 0.3107, 0.2401, 0.15, 12],109893:[0.556, 0.3553, 0.2483, 0.2365, 0.175, 12],
 109894:[0.483, 0.3337, 0.2772, 0.1834, 0.1343, 24],109896:[0.52, 0.3673, 0.297, 0.2247, 0.1507, 12],109897:[0.573, 0.3617, 0.2452, 0.2195, 0.179, 12],109898:[0.525, 0.3113, 0.233, 0.1993, 0.134, 24],
 109900:[0.553, 0.369, 0.2845, 0.2517, 0.1816, 12],109901:[0.551, 0.3653, 0.2947, 0.2433, 0.1727, 12],109902:[0.504, 0.384, 0.2792, 0.211, 0.1392, 24],109904:[0.538, 0.4093, 0.3247, 0.2577, 0.2007, 12],
 109905:[0.559, 0.3917, 0.307, 0.2691, 0.2263, 12],109906:[0.536, 0.4207, 0.29, 0.252, 0.1798, 24],109908:[0.653, 0.4567, 0.3707, 0.2994, 0.2207, 12],109961:[0.648, 0.4597, 0.312, 0.2904, 0.1923, 12],
 110032:[0.472, 0.336, 0.277, 0.1944, 0.1703, 4],110033:[0.464, 0.3553, 0.2602, 0.1921, 0.1852, 12],110036:[0.509, 0.313, 0.2637, 0.2151, 0.1517, 4],110037:[0.485, 0.3553, 0.3027, 0.1837, 0.1558, 12],
 110040:[0.519, 0.372, 0.231, 0.2097, 0.1497, 4],110041:[0.502, 0.343, 0.2972, 0.208, 0.158, 12],110044:[0.527, 0.3403, 0.2822, 0.2109, 0.1483, 4],110045:[0.498, 0.328, 0.2637, 0.2324, 0.2, 12],
 110048:[0.534, 0.3823, 0.261, 0.2035, 0.1537, 4],110049:[0.523, 0.3393, 0.2647, 0.2305, 0.149, 12],110052:[0.499, 0.3453, 0.2482, 0.1764, 0.1625, 4],110053:[0.537, 0.3307, 0.2805, 0.2237, 0.1893, 12],
 110056:[0.552, 0.4363, 0.278, 0.2201, 0.1503, 4],110057:[0.556, 0.3967, 0.2618, 0.2443, 0.189, 12],110060:[0.535, 0.3967, 0.301, 0.2518, 0.1797, 4],110061:[0.542, 0.387, 0.336, 0.2637, 0.2053, 12],
 110064:[0.558, 0.3723, 0.2925, 0.2619, 0.1992, 4],110065:[0.575, 0.4537, 0.3137, 0.2741, 0.189, 12],110084:[0.506, 0.3097, 0.276, 0.2277, 0.1637, 12],110085:[0.501, 0.3363, 0.2943, 0.219, 0.1476, 12],
 110086:[0.472, 0.3053, 0.265, 0.188, 0.117, 24],110088:[0.512, 0.3323, 0.2767, 0.1981, 0.1733, 12],110089:[0.493, 0.3327, 0.3017, 0.2097, 0.1797, 12],110090:[0.487, 0.3353, 0.2355, 0.1887, 0.1283, 24],
 110092:[0.48, 0.3387, 0.238, 0.2021, 0.153, 12],110093:[0.526, 0.3627, 0.259, 0.2241, 0.163, 12],110094:[0.478, 0.318, 0.2538, 0.1982, 0.1096, 24],110096:[0.543, 0.3603, 0.2795, 0.2372, 0.1393, 12],
 110097:[0.516, 0.326, 0.2798, 0.2266, 0.1603, 12],110098:[0.496, 0.382, 0.227, 0.172, 0.0983, 24],110100:[0.515, 0.3887, 0.2825, 0.253, 0.1493, 12],110101:[0.553, 0.356, 0.2685, 0.2243, 0.1617, 12],
 110102:[0.485, 0.3673, 0.2205, 0.2019, 0.1395, 24],110104:[0.538, 0.3643, 0.293, 0.2146, 0.1783, 12],110105:[0.538, 0.3977, 0.2635, 0.2173, 0.1787, 12],110106:[0.536, 0.3167, 0.2595, 0.2037, 0.1188, 24],
 110108:[0.549, 0.3577, 0.2972, 0.2387, 0.161, 12],110109:[0.553, 0.4147, 0.3047, 0.268, 0.17, 12],110110:[0.552, 0.3643, 0.2753, 0.2114, 0.178, 24],110112:[0.573, 0.3537, 0.3072, 0.2814, 0.1888, 12],
 110113:[0.586, 0.3987, 0.328, 0.2534, 0.216, 12],110114:[0.534, 0.4, 0.2852, 0.236, 0.1712, 24],110116:[0.547, 0.3857, 0.3625, 0.2901, 0.2177, 12],110117:[0.597, 0.409, 0.3393, 0.2514, 0.2283, 12],
 110118:[0.56, 0.4067, 0.284, 0.272, 0.2053, 24],110120:[0.701, 0.4933, 0.4365, 0.315, 0.226, 12],110173:[0.707, 0.4597, 0.4, 0.2906, 0.2453, 12],112944:[0.677, 0.4827, 0.308, 0.2954, 0.231, 12],
 112946:[0.638, 0.4967, 0.3372, 0.2717, 0.166, 12],112948:[0.689, 0.4743, 0.3622, 0.3221, 0.1937, 12],112950:[0.672, 0.4597, 0.304, 0.2577, 0.1713, 12],112952:[0.718, 0.521, 0.3553, 0.2883, 0.198, 12],
 112954:[0.673, 0.486, 0.3495, 0.254, 0.1987, 12],112956:[0.675, 0.472, 0.371, 0.2949, 0.238, 12],112958:[0.657, 0.5043, 0.3495, 0.258, 0.1893, 12],112960:[0.656, 0.5057, 0.366, 0.3007, 0.2373, 12],
 112962:[0.638, 0.448, 0.317, 0.242, 0.2153, 12],112964:[0.679, 0.4847, 0.3905, 0.29, 0.2173, 12],112966:[0.695, 0.49, 0.353, 0.2887, 0.189, 12],112968:[0.668, 0.5063, 0.35, 0.3263, 0.2477, 12],
 112970:[0.675, 0.5097, 0.3435, 0.283, 0.1803, 12],112972:[0.688, 0.504, 0.3847, 0.3197, 0.2403, 12],112974:[0.712, 0.4907, 0.3712, 0.2927, 0.1905, 12],112976:[0.678, 0.519, 0.3785, 0.3377, 0.2517, 12],
 112978:[0.676, 0.5097, 0.357, 0.2933, 0.217, 12],112980:[0.699, 0.542, 0.3845, 0.3304, 0.2512, 12],112982:[0.658, 0.476, 0.3625, 0.3062, 0.194, 12],115740:[0.632, 0.3953, 0.2915, 0.211, 0.136, 4],
 119028:[0.448, 0.278, 0.2545, 0.2101, 0.1646, 12],119081:[0.414, 0.298, 0.1865, 0.158, 0.1212, 12],119184:[0.378, 0.2623, 0.2042, 0.1618, 0.1348, 4],119185:[0.441, 0.296, 0.238, 0.1855, 0.1523, 12],
 119236:[0.459, 0.315, 0.2085, 0.1941, 0.144, 12],119237:[0.438, 0.2827, 0.2287, 0.1785, 0.0997, 12],119238:[0.431, 0.262, 0.1607, 0.1161, 0.0873, 24],119240:[0.531, 0.3253, 0.267, 0.2064, 0.1527, 12],
 119293:[0.467, 0.268, 0.179, 0.1483, 0.101, 12],119392:[0.478, 0.288, 0.1935, 0.1651, 0.1079, 4],119393:[0.465, 0.2943, 0.2128, 0.1547, 0.1117, 12],119396:[0.449, 0.282, 0.2518, 0.1524, 0.142, 4],
 119397:[0.477, 0.293, 0.2202, 0.2195, 0.141, 12],119444:[0.462, 0.3077, 0.2373, 0.1953, 0.1573, 12],119445:[0.448, 0.2993, 0.2075, 0.2063, 0.1242, 12],119446:[0.41, 0.2523, 0.17, 0.1314, 0.0853, 24],
 119448:[0.482, 0.3177, 0.2703, 0.1991, 0.1243, 12],119449:[0.449, 0.313, 0.2522, 0.1887, 0.118, 12],119450:[0.471, 0.2527, 0.2127, 0.1594, 0.115, 24],119452:[0.558, 0.3377, 0.2277, 0.2081, 0.1417, 12],
 119505:[0.486, 0.3247, 0.2347, 0.1851, 0.1303, 12],119600:[0.47, 0.27, 0.1893, 0.1864, 0.128, 4],119601:[0.444, 0.296, 0.2347, 0.191, 0.1513, 12],119604:[0.458, 0.288, 0.229, 0.1787, 0.1269, 4],
 119605:[0.504, 0.3233, 0.2367, 0.1773, 0.1742, 12],119608:[0.466, 0.2883, 0.2343, 0.2007, 0.1203, 4],119609:[0.508, 0.3107, 0.2835, 0.1837, 0.1453, 12],119652:[0.461, 0.3113, 0.2303, 0.1825, 0.1343, 12],
 119653:[0.491, 0.259, 0.236, 0.152, 0.1316, 12],119654:[0.397, 0.267, 0.196, 0.1114, 0.0823, 24],119656:[0.481, 0.324, 0.2217, 0.1993, 0.1255, 12],119657:[0.468, 0.3197, 0.246, 0.1917, 0.1397, 12],
 119658:[0.493, 0.289, 0.1815, 0.1637, 0.1038, 24],119660:[0.532, 0.3267, 0.2442, 0.191, 0.1292, 12],119661:[0.497, 0.3187, 0.2307, 0.2271, 0.144, 12],119662:[0.486, 0.311, 0.2065, 0.1767, 0.1016, 24],
 119664:[0.553, 0.3673, 0.2667, 0.2314, 0.1757, 12],119717:[0.533, 0.3437, 0.2278, 0.1774, 0.142, 12],119808:[0.461, 0.25, 0.198, 0.1831, 0.1263, 4],119809:[0.443, 0.3127, 0.2438, 0.1747, 0.115, 12],
 119812:[0.447, 0.2887, 0.2075, 0.1737, 0.1353, 4],119813:[0.492, 0.3123, 0.259, 0.1862, 0.1438, 12],119816:[0.499, 0.275, 0.2333, 0.1962, 0.1187, 4],119817:[0.465, 0.3307, 0.2607, 0.2101, 0.1367, 12],
 119820:[0.494, 0.3317, 0.2485, 0.1987, 0.1347, 4],119821:[0.503, 0.3203, 0.2667, 0.215, 0.153, 12],119860:[0.478, 0.3243, 0.2128, 0.2007, 0.1417, 12],119861:[0.466, 0.3287, 0.2212, 0.1787, 0.1343, 12],
 119862:[0.43, 0.3087, 0.1492, 0.1301, 0.0993, 24],119864:[0.468, 0.264, 0.252, 0.1833, 0.1387, 12],119865:[0.472, 0.3363, 0.2135, 0.1671, 0.1117, 12],119866:[0.445, 0.28, 0.1768, 0.1661, 0.1128, 24],
 119868:[0.483, 0.3423, 0.2877, 0.1951, 0.157, 12],119869:[0.479, 0.2913, 0.2392, 0.2017, 0.1433, 12],119870:[0.463, 0.322, 0.1905, 0.1587, 0.0877, 24],119872:[0.534, 0.327, 0.2777, 0.2014, 0.156, 12],
 119873:[0.507, 0.3547, 0.27, 0.2412, 0.1453, 12],119874:[0.489, 0.3177, 0.2477, 0.1582, 0.093, 24],119876:[0.584, 0.358, 0.3122, 0.235, 0.1743, 12],119929:[0.511, 0.3363, 0.24, 0.1943, 0.1367, 12],
 120016:[0.474, 0.2807, 0.1915, 0.1657, 0.1163, 4],120017:[0.491, 0.3067, 0.1953, 0.1874, 0.1427, 12],120020:[0.424, 0.2947, 0.199, 0.1887, 0.143, 4],120021:[0.437, 0.3173, 0.2493, 0.2057, 0.1307, 12],
 120024:[0.496, 0.334, 0.237, 0.2105, 0.1325, 4],120025:[0.509, 0.3167, 0.2437, 0.1897, 0.1463, 12],120028:[0.501, 0.3317, 0.2658, 0.1904, 0.1811, 4],120029:[0.523, 0.3403, 0.2765, 0.2053, 0.1707, 12],
 120032:[0.506, 0.3367, 0.2517, 0.1873, 0.1593, 4],120033:[0.518, 0.4017, 0.2787, 0.2173, 0.171, 12],120068:[0.434, 0.2973, 0.2217, 0.1997, 0.1246, 12],120069:[0.497, 0.304, 0.2217, 0.1837, 0.1097, 12],
 120070:[0.436, 0.2577, 0.1803, 0.1381, 0.0758, 24],120072:[0.487, 0.282, 0.2375, 0.198, 0.135, 12],120073:[0.472, 0.3117, 0.2117, 0.1651, 0.1303, 12],120074:[0.447, 0.2567, 0.1838, 0.1378, 0.099, 24],
 120076:[0.466, 0.3657, 0.2215, 0.2093, 0.1543, 12],120077:[0.452, 0.3457, 0.227, 0.1989, 0.13, 12],120078:[0.466, 0.2777, 0.1832, 0.1764, 0.099, 24],120080:[0.547, 0.3083, 0.2562, 0.202, 0.1497, 12],
 120081:[0.515, 0.359, 0.2712, 0.2133, 0.1282, 12],120082:[0.472, 0.345, 0.2233, 0.1635, 0.1022, 24],120084:[0.516, 0.3627, 0.2977, 0.2171, 0.1499, 12],120085:[0.532, 0.3477, 0.287, 0.1737, 0.1273, 12],
 120086:[0.538, 0.325, 0.2548, 0.194, 0.111, 24],120088:[0.598, 0.396, 0.2662, 0.2361, 0.164, 12],120141:[0.599, 0.373, 0.2607, 0.2361, 0.1201, 12],120224:[0.45, 0.3103, 0.186, 0.1374, 0.1203, 4],
 120225:[0.467, 0.3173, 0.1825, 0.1985, 0.1278, 12],120228:[0.436, 0.2897, 0.1948, 0.1713, 0.1255, 4],120229:[0.486, 0.298, 0.2657, 0.1786, 0.1338, 12],120232:[0.464, 0.3093, 0.2367, 0.1667, 0.1157, 4],
 120233:[0.461, 0.2997, 0.2183, 0.1725, 0.147, 12],120236:[0.483, 0.2857, 0.1937, 0.1535, 0.1463, 4],120237:[0.546, 0.3373, 0.2617, 0.2073, 0.1353, 12],120240:[0.508, 0.336, 0.25, 0.2163, 0.1252, 4],
 120241:[0.542, 0.3253, 0.2798, 0.205, 0.1263, 12],120244:[0.52, 0.3367, 0.276, 0.2011, 0.1647, 4],120245:[0.569, 0.3877, 0.2388, 0.2344, 0.1956, 12],120276:[0.456, 0.2807, 0.2435, 0.1994, 0.1143, 12],
 120277:[0.48, 0.2893, 0.2127, 0.1561, 0.111, 12],120278:[0.442, 0.271, 0.1535, 0.1434, 0.0947, 24],120280:[0.446, 0.3327, 0.2377, 0.1869, 0.1506, 12],120281:[0.444, 0.299, 0.2458, 0.161, 0.119, 12],
 120282:[0.405, 0.2607, 0.1882, 0.1064, 0.1083, 24],120284:[0.451, 0.311, 0.2405, 0.1793, 0.13, 12],120285:[0.5, 0.3183, 0.239, 0.1598, 0.1488, 12],120286:[0.484, 0.2993, 0.1927, 0.1457, 0.0893, 24],
 120288:[0.516, 0.3197, 0.227, 0.2161, 0.1527, 12],120289:[0.484, 0.3553, 0.281, 0.1987, 0.1393, 12],120290:[0.509, 0.2953, 0.2145, 0.1665, 0.1082, 24],120292:[0.525, 0.3413, 0.2567, 0.211, 0.1312, 12],
 120293:[0.501, 0.318, 0.2832, 0.2157, 0.1627, 12],120294:[0.499, 0.3417, 0.1908, 0.2014, 0.1342, 24],120296:[0.542, 0.3653, 0.2815, 0.1882, 0.1845, 12],120297:[0.486, 0.3703, 0.2573, 0.229, 0.1706, 12],
 120298:[0.523, 0.35, 0.2212, 0.1767, 0.1133, 24],120300:[0.616, 0.392, 0.3015, 0.2389, 0.156, 12],120353:[0.58, 0.3627, 0.2718, 0.2289, 0.1487, 12],120432:[0.468, 0.305, 0.2262, 0.1893, 0.1297, 4],
 120433:[0.478, 0.3283, 0.263, 0.1881, 0.1282, 12],120436:[0.442, 0.2923, 0.2317, 0.18, 0.1503, 4],120437:[0.495, 0.3343, 0.2357, 0.1888, 0.144, 12],120440:[0.497, 0.3627, 0.272, 0.1715, 0.1147, 4],
 120441:[0.514, 0.3123, 0.2252, 0.2128, 0.135, 12],120444:[0.515, 0.314, 0.2343, 0.1887, 0.1237, 4],120445:[0.497, 0.3347, 0.2827, 0.2083, 0.1238, 12],120448:[0.559, 0.3567, 0.2387, 0.1833, 0.1495, 4],
 120449:[0.506, 0.335, 0.2618, 0.2294, 0.125, 12],120452:[0.561, 0.3463, 0.2727, 0.2154, 0.1697, 4],120453:[0.548, 0.3687, 0.2672, 0.2301, 0.16, 12],120456:[0.547, 0.348, 0.2675, 0.2191, 0.1603, 4],
 120457:[0.554, 0.3693, 0.3095, 0.2281, 0.162, 12],120484:[0.493, 0.3583, 0.262, 0.2071, 0.1068, 12],120485:[0.463, 0.3, 0.2648, 0.1661, 0.1323, 12],120486:[0.459, 0.2743, 0.209, 0.147, 0.1063, 24],
 120488:[0.524, 0.3527, 0.2463, 0.1657, 0.144, 12],120489:[0.523, 0.325, 0.2173, 0.2021, 0.1213, 12],120490:[0.431, 0.2883, 0.2037, 0.1729, 0.1355, 24],120492:[0.494, 0.3293, 0.2353, 0.1771, 0.1563, 12],
 120493:[0.482, 0.3383, 0.2352, 0.1953, 0.1126, 12],120494:[0.47, 0.2927, 0.2127, 0.1448, 0.1217, 24],120496:[0.515, 0.345, 0.2568, 0.1768, 0.175, 12],120497:[0.513, 0.3307, 0.2492, 0.2503, 0.1327, 12],
 120498:[0.5, 0.3423, 0.248, 0.1887, 0.1065, 24],120500:[0.53, 0.345, 0.2497, 0.2451, 0.157, 12],120501:[0.53, 0.355, 0.2823, 0.2154, 0.1687, 12],120502:[0.531, 0.3337, 0.2147, 0.1715, 0.1103, 24],
 120504:[0.525, 0.387, 0.3197, 0.2158, 0.1583, 12],120505:[0.534, 0.3543, 0.2463, 0.2394, 0.1417, 12],120506:[0.488, 0.368, 0.2387, 0.2105, 0.132, 24],120508:[0.536, 0.353, 0.309, 0.2717, 0.1783, 12],
 120509:[0.544, 0.3777, 0.2837, 0.219, 0.1547, 12],120510:[0.504, 0.3733, 0.2495, 0.1696, 0.143, 24],120512:[0.678, 0.4047, 0.372, 0.3002, 0.2147, 12],120565:[0.635, 0.3713, 0.2925, 0.255, 0.1667, 12],
 120640:[0.479, 0.2897, 0.2392, 0.194, 0.149, 4],120641:[0.533, 0.3737, 0.2387, 0.2275, 0.1526, 12],120644:[0.466, 0.354, 0.256, 0.1837, 0.1587, 4],120645:[0.553, 0.3637, 0.265, 0.203, 0.1356, 12],
 120648:[0.469, 0.3377, 0.2627, 0.1961, 0.1293, 4],120649:[0.532, 0.3633, 0.277, 0.252, 0.1587, 12],120652:[0.538, 0.334, 0.226, 0.199, 0.1793, 4],120653:[0.498, 0.3577, 0.23, 0.2771, 0.1756, 12],
 120656:[0.52, 0.324, 0.2483, 0.2095, 0.1636, 4],120657:[0.504, 0.3887, 0.2815, 0.2442, 0.1807, 12],120660:[0.521, 0.3967, 0.3052, 0.254, 0.1587, 4],120661:[0.539, 0.3967, 0.3033, 0.285, 0.1812, 12],
 120664:[0.524, 0.3943, 0.3185, 0.2381, 0.167, 4],120665:[0.574, 0.3973, 0.3142, 0.282, 0.1922, 12],120668:[0.534, 0.3877, 0.3007, 0.2473, 0.1816, 4],120669:[0.551, 0.4333, 0.3187, 0.2363, 0.1936, 12],
 120692:[0.557, 0.3247, 0.2633, 0.2293, 0.1337, 12],120693:[0.455, 0.3393, 0.257, 0.2351, 0.1543, 12],120694:[0.457, 0.3033, 0.2417, 0.1895, 0.0765, 24],120696:[0.505, 0.3427, 0.3065, 0.2103, 0.1567, 12],
 120697:[0.494, 0.355, 0.296, 0.222, 0.172, 12],120698:[0.552, 0.3257, 0.2597, 0.1653, 0.1247, 24],120700:[0.486, 0.371, 0.263, 0.2201, 0.16, 12],120701:[0.524, 0.3587, 0.2877, 0.1927, 0.1686, 12],
 120702:[0.526, 0.2993, 0.249, 0.1716, 0.135, 24],120704:[0.515, 0.3203, 0.2833, 0.2411, 0.1417, 12],120705:[0.552, 0.3567, 0.2892, 0.2113, 0.144, 12],120706:[0.497, 0.3297, 0.219, 0.2094, 0.1267, 24],
 120708:[0.548, 0.3717, 0.2597, 0.2451, 0.1356, 12],120709:[0.55, 0.3527, 0.3177, 0.1994, 0.1777, 12],120710:[0.537, 0.3487, 0.2295, 0.1885, 0.1373, 24],120712:[0.522, 0.369, 0.2815, 0.2427, 0.1735, 12],
 120713:[0.521, 0.397, 0.2932, 0.246, 0.1463, 12],120714:[0.503, 0.395, 0.2403, 0.2217, 0.107, 24],120716:[0.568, 0.3763, 0.315, 0.2497, 0.1982, 12],120717:[0.553, 0.393, 0.2725, 0.2454, 0.1876, 12],
 120718:[0.538, 0.3783, 0.2862, 0.198, 0.1465, 24],120720:[0.579, 0.4087, 0.3507, 0.2843, 0.1853, 12],120721:[0.576, 0.3887, 0.3052, 0.2801, 0.1777, 12],120722:[0.575, 0.3523, 0.2868, 0.2097, 0.162, 24],
 120724:[0.65, 0.496, 0.338, 0.3038, 0.1833, 12],120777:[0.628, 0.4853, 0.358, 0.2437, 0.1765, 12],120848:[0.492, 0.3717, 0.2528, 0.2051, 0.1533, 4],120849:[0.54, 0.3383, 0.2752, 0.2247, 0.1806, 12],
 120852:[0.463, 0.329, 0.2442, 0.1951, 0.1649, 4],120853:[0.527, 0.3547, 0.2668, 0.2057, 0.1763, 12],120856:[0.504, 0.3597, 0.2297, 0.2177, 0.1516, 4],120857:[0.493, 0.3617, 0.2573, 0.2237, 0.159, 12],
 120860:[0.529, 0.3263, 0.2437, 0.2107, 0.1756, 4],120861:[0.562, 0.36, 0.2912, 0.2367, 0.1856, 12],120864:[0.56, 0.3453, 0.2603, 0.2197, 0.1622, 4],120865:[0.545, 0.3497, 0.2558, 0.2207, 0.181, 12],
 120868:[0.527, 0.351, 0.3113, 0.2137, 0.1571, 4],120869:[0.573, 0.362, 0.2602, 0.221, 0.2063, 12],120872:[0.565, 0.4097, 0.2582, 0.249, 0.1425, 4],120873:[0.577, 0.4203, 0.2942, 0.2641, 0.176, 12],
 120876:[0.528, 0.3937, 0.33, 0.2107, 0.199, 4],120877:[0.587, 0.4053, 0.3372, 0.2395, 0.1973, 12],120880:[0.528, 0.3933, 0.3263, 0.282, 0.2173, 4],120881:[0.594, 0.4153, 0.3355, 0.2844, 0.234, 12],
 120900:[0.536, 0.3577, 0.2735, 0.2373, 0.1487, 12],120901:[0.505, 0.356, 0.2377, 0.2383, 0.1237, 12],120902:[0.515, 0.3063, 0.2512, 0.17, 0.105, 24],120904:[0.559, 0.3503, 0.271, 0.2627, 0.1557, 12],
 120905:[0.524, 0.3547, 0.2582, 0.1943, 0.1428, 12],120906:[0.499, 0.3317, 0.2302, 0.199, 0.144, 24],120908:[0.511, 0.33, 0.3068, 0.2354, 0.161, 12],120909:[0.519, 0.3753, 0.2742, 0.2187, 0.1547, 12],
 120910:[0.525, 0.3227, 0.2258, 0.185, 0.1306, 24],120912:[0.548, 0.3637, 0.295, 0.2183, 0.1912, 12],120913:[0.529, 0.3843, 0.298, 0.2593, 0.171, 12],120914:[0.518, 0.3477, 0.2845, 0.1857, 0.1296, 24],
 120916:[0.528, 0.3483, 0.278, 0.2553, 0.1637, 12],120917:[0.537, 0.3543, 0.2663, 0.225, 0.1863, 12],120918:[0.522, 0.306, 0.238, 0.2008, 0.1207, 24],120920:[0.57, 0.395, 0.2857, 0.2145, 0.147, 12],
 120921:[0.553, 0.3877, 0.3135, 0.2354, 0.1733, 12],120922:[0.512, 0.3803, 0.2693, 0.218, 0.132, 24],120924:[0.564, 0.3927, 0.2767, 0.2661, 0.1537, 12],120925:[0.565, 0.43, 0.3043, 0.2214, 0.1457, 12],
 120926:[0.509, 0.3733, 0.2802, 0.1747, 0.1391, 24],120928:[0.542, 0.389, 0.32, 0.2461, 0.2003, 12],120929:[0.559, 0.4297, 0.327, 0.2773, 0.1825, 12],120930:[0.531, 0.378, 0.2975, 0.2594, 0.1747, 24],
 120932:[0.591, 0.4143, 0.3095, 0.3021, 0.206, 12],120933:[0.588, 0.423, 0.385, 0.2881, 0.2013, 12],120934:[0.566, 0.3637, 0.2918, 0.2528, 0.2125, 24],120936:[0.67, 0.5087, 0.395, 0.3157, 0.2307, 12],
 120989:[0.679, 0.5333, 0.3792, 0.285, 0.219, 12],121056:[0.487, 0.342, 0.2855, 0.2323, 0.1423, 4],121057:[0.55, 0.376, 0.2777, 0.2147, 0.161, 12],121060:[0.554, 0.357, 0.2515, 0.2027, 0.1667, 4],
 121061:[0.508, 0.352, 0.2985, 0.252, 0.1527, 12],121064:[0.51, 0.382, 0.2617, 0.1931, 0.1593, 4],121065:[0.564, 0.383, 0.2793, 0.2557, 0.164, 12],121068:[0.528, 0.3663, 0.2473, 0.2024, 0.1673, 4],
 121069:[0.535, 0.3687, 0.2807, 0.2298, 0.1783, 12],121072:[0.515, 0.3877, 0.2675, 0.1987, 0.142, 4],121073:[0.594, 0.3843, 0.2862, 0.2317, 0.1557, 12],121076:[0.501, 0.3647, 0.276, 0.2463, 0.1537, 4],
 121077:[0.534, 0.3893, 0.2967, 0.2409, 0.1545, 12],121080:[0.535, 0.3563, 0.2685, 0.2401, 0.157, 4],121081:[0.538, 0.3983, 0.325, 0.246, 0.1337, 12],121084:[0.58, 0.3933, 0.3085, 0.272, 0.17, 4],
 121085:[0.574, 0.4003, 0.3447, 0.2431, 0.199, 12],121088:[0.553, 0.385, 0.311, 0.279, 0.2443, 4],121089:[0.541, 0.4683, 0.3397, 0.296, 0.2297, 12],121092:[0.591, 0.384, 0.328, 0.2611, 0.1815, 4],
 121093:[0.541, 0.439, 0.3247, 0.2567, 0.216, 12],121108:[0.518, 0.3497, 0.2832, 0.2301, 0.177, 12],121109:[0.507, 0.377, 0.3047, 0.2201, 0.1847, 12],121110:[0.486, 0.3077, 0.253, 0.1871, 0.1567, 24],
 121112:[0.543, 0.38, 0.3097, 0.2305, 0.1447, 12],121113:[0.547, 0.378, 0.292, 0.2163, 0.1803, 12],121114:[0.497, 0.3153, 0.254, 0.1783, 0.165, 24],121116:[0.503, 0.397, 0.2887, 0.2384, 0.1696, 12],
 121117:[0.532, 0.369, 0.3163, 0.239, 0.1827, 12],121118:[0.571, 0.377, 0.2513, 0.1543, 0.126, 24],121120:[0.512, 0.3847, 0.321, 0.2397, 0.1223, 12],121121:[0.569, 0.3957, 0.3087, 0.2532, 0.1573, 12],
 121122:[0.517, 0.2897, 0.2452, 0.211, 0.1263, 24],121124:[0.529, 0.412, 0.305, 0.2507, 0.155, 12],121125:[0.564, 0.383, 0.313, 0.2143, 0.1963, 12],121126:[0.551, 0.3643, 0.3085, 0.2347, 0.1653, 24],
 121128:[0.542, 0.3987, 0.2765, 0.1989, 0.1703, 12],121129:[0.547, 0.3577, 0.2905, 0.2547, 0.165, 12],121130:[0.515, 0.368, 0.251, 0.1893, 0.154, 24],121132:[0.572, 0.3873, 0.2862, 0.2567, 0.2013, 12],
 121133:[0.546, 0.389, 0.2847, 0.233, 0.162, 12],121134:[0.538, 0.3353, 0.2888, 0.1961, 0.1257, 24],121136:[0.617, 0.404, 0.3428, 0.2502, 0.1912, 12],121137:[0.564, 0.416, 0.301, 0.2838, 0.192, 12],
 121138:[0.537, 0.365, 0.2842, 0.2358, 0.1763, 24],121140:[0.577, 0.452, 0.381, 0.2751, 0.2155, 12],121141:[0.557, 0.436, 0.3447, 0.3012, 0.2218, 12],121142:[0.562, 0.3787, 0.3405, 0.2727, 0.1903, 24],
 121144:[0.553, 0.4253, 0.3623, 0.2803, 0.2103, 12],121145:[0.554, 0.4317, 0.3247, 0.2947, 0.207, 12],121146:[0.597, 0.4013, 0.3065, 0.2498, 0.1747, 24],121148:[0.726, 0.4997, 0.402, 0.3514, 0.2366, 12],
 121201:[0.681, 0.5333, 0.407, 0.304, 0.215, 12],123968:[0.69, 0.4983, 0.418, 0.3444, 0.2237, 12],123970:[0.671, 0.488, 0.402, 0.2758, 0.2403, 12],123972:[0.731, 0.519, 0.4463, 0.2984, 0.2293, 12],
 123974:[0.699, 0.5173, 0.326, 0.329, 0.2183, 12],123976:[0.704, 0.514, 0.41, 0.402, 0.2087, 12],123978:[0.716, 0.4973, 0.3462, 0.2871, 0.2027, 12],123980:[0.73, 0.5347, 0.4555, 0.367, 0.2793, 12],
 123982:[0.71, 0.5223, 0.393, 0.3206, 0.2205, 12],123984:[0.711, 0.53, 0.423, 0.368, 0.2637, 12],123986:[0.684, 0.5187, 0.3837, 0.3033, 0.2033, 12],123988:[0.696, 0.5437, 0.4175, 0.3677, 0.2863, 12],
 123990:[0.661, 0.4867, 0.406, 0.297, 0.2153, 12],123992:[0.711, 0.5333, 0.369, 0.3474, 0.249, 12],123994:[0.704, 0.5213, 0.4042, 0.3337, 0.217, 12],123996:[0.713, 0.5197, 0.4393, 0.2957, 0.278, 12],
 123998:[0.725, 0.5647, 0.386, 0.328, 0.222, 12],124000:[0.749, 0.586, 0.4347, 0.4075, 0.2837, 12],124002:[0.657, 0.5063, 0.423, 0.342, 0.235, 12],124004:[0.739, 0.5217, 0.4095, 0.3368, 0.278, 12],
 124006:[0.672, 0.5237, 0.364, 0.3317, 0.2267, 12],124008:[0.76, 0.5747, 0.404, 0.3514, 0.2525, 12],124010:[0.701, 0.517, 0.3875, 0.3274, 0.262, 12],126768:[0.663, 0.496, 0.33, 0.2574, 0.136, 4],
 129844:[0.468, 0.3467, 0.2677, 0.24, 0.1673, 12],129897:[0.47, 0.303, 0.2377, 0.1709, 0.123, 12],130000:[0.452, 0.3147, 0.2475, 0.17, 0.127, 4],130001:[0.472, 0.3277, 0.2485, 0.2147, 0.1727, 12],
 130052:[0.485, 0.3247, 0.2135, 0.2021, 0.1516, 12],130053:[0.47, 0.309, 0.2367, 0.167, 0.1315, 12],130054:[0.406, 0.2783, 0.2015, 0.1514, 0.1097, 24],130056:[0.488, 0.343, 0.2687, 0.2271, 0.1627, 12],
 130109:[0.465, 0.307, 0.222, 0.1988, 0.1385, 12],130208:[0.472, 0.2907, 0.2293, 0.2011, 0.1177, 4],130209:[0.505, 0.3233, 0.2415, 0.1987, 0.155, 12],130212:[0.455, 0.3347, 0.253, 0.215, 0.149, 4],
 130213:[0.452, 0.3873, 0.2518, 0.1993, 0.1313, 12],130260:[0.489, 0.322, 0.238, 0.202, 0.1355, 12],130261:[0.47, 0.3453, 0.2237, 0.1684, 0.129, 12],130262:[0.439, 0.2843, 0.1568, 0.1565, 0.0961, 24],
 130264:[0.531, 0.376, 0.2722, 0.1922, 0.1866, 12],130265:[0.517, 0.314, 0.261, 0.1727, 0.1286, 12],130266:[0.459, 0.283, 0.2093, 0.1947, 0.1066, 24],130268:[0.525, 0.396, 0.276, 0.2309, 0.1488, 12],
 130321:[0.489, 0.3577, 0.2735, 0.186, 0.1186, 12],130416:[0.483, 0.2987, 0.2367, 0.197, 0.1663, 4],130417:[0.495, 0.3437, 0.2537, 0.201, 0.1421, 12],130420:[0.499, 0.3247, 0.2573, 0.1884, 0.1317, 4],
 130421:[0.503, 0.3727, 0.2832, 0.2157, 0.1543, 12],130424:[0.495, 0.3553, 0.275, 0.231, 0.1273, 4],130425:[0.519, 0.3273, 0.268, 0.2553, 0.163, 12],130468:[0.475, 0.3587, 0.216, 0.2413, 0.1436, 12],
 130469:[0.492, 0.318, 0.2467, 0.1924, 0.149, 12],130470:[0.481, 0.307, 0.202, 0.1567, 0.112, 24],130472:[0.5, 0.3323, 0.2715, 0.2117, 0.1923, 12],130473:[0.489, 0.345, 0.2427, 0.1957, 0.1337, 12],
 130474:[0.467, 0.3373, 0.231, 0.1637, 0.1423, 24],130476:[0.506, 0.361, 0.3137, 0.252, 0.1503, 12],130477:[0.503, 0.3547, 0.278, 0.2067, 0.1328, 12],130478:[0.53, 0.3317, 0.244, 0.1843, 0.1183, 24],
 130480:[0.573, 0.4103, 0.2835, 0.2261, 0.1883, 12],130533:[0.546, 0.36, 0.2317, 0.2187, 0.15, 12],130624:[0.495, 0.2837, 0.2168, 0.1783, 0.1097, 4],130625:[0.475, 0.2763, 0.243, 0.1927, 0.1567, 12],
 130628:[0.498, 0.313, 0.2338, 0.1644, 0.1443, 4],130629:[0.504, 0.338, 0.2357, 0.2107, 0.144, 12],130632:[0.501, 0.3227, 0.2597, 0.1944, 0.1385, 4],130633:[0.525, 0.336, 0.2753, 0.2184, 0.147, 12],
 130636:[0.504, 0.3257, 0.2633, 0.1927, 0.157, 4],130637:[0.515, 0.3973, 0.2618, 0.2197, 0.1743, 12],130676:[0.456, 0.3213, 0.2475, 0.198, 0.148, 12],130677:[0.466, 0.296, 0.205, 0.1974, 0.1489, 12],
 130678:[0.427, 0.2677, 0.1778, 0.1489, 0.086, 24],130680:[0.526, 0.3553, 0.2743, 0.218, 0.1543, 12],130681:[0.507, 0.3407, 0.275, 0.2099, 0.1433, 12],130682:[0.491, 0.3227, 0.204, 0.1691, 0.1085, 24],
 130684:[0.543, 0.3473, 0.22, 0.2211, 0.1535, 12],130685:[0.512, 0.3467, 0.2487, 0.1797, 0.113, 12],130686:[0.489, 0.3197, 0.2452, 0.1954, 0.125, 24],130688:[0.53, 0.3197, 0.2785, 0.2015, 0.167, 12],
 130689:[0.525, 0.3343, 0.2387, 0.2199, 0.1663, 12],130690:[0.538, 0.3113, 0.2528, 0.1824, 0.1067, 24],130692:[0.583, 0.4233, 0.2915, 0.2225, 0.1845, 12],130745:[0.558, 0.3547, 0.256, 0.1724, 0.175, 12],
 130832:[0.484, 0.3213, 0.2198, 0.163, 0.1383, 4],130833:[0.538, 0.2857, 0.2455, 0.2358, 0.1426, 12],130836:[0.525, 0.3647, 0.226, 0.1814, 0.134, 4],130837:[0.496, 0.3217, 0.2835, 0.1901, 0.1463, 12],
 130840:[0.519, 0.3187, 0.2298, 0.2118, 0.1388, 4],130841:[0.489, 0.3653, 0.2712, 0.2057, 0.1315, 12],130844:[0.547, 0.325, 0.2627, 0.2077, 0.1642, 4],130845:[0.51, 0.3663, 0.2315, 0.2247, 0.1815, 12],
 130848:[0.505, 0.3673, 0.274, 0.2163, 0.1705, 4],130849:[0.531, 0.4113, 0.3018, 0.2107, 0.16, 12],130884:[0.54, 0.3023, 0.2523, 0.1991, 0.1247, 12],130885:[0.479, 0.3107, 0.2313, 0.1897, 0.1205, 12],
 130886:[0.478, 0.2997, 0.162, 0.1508, 0.0856, 24],130888:[0.5, 0.3243, 0.2412, 0.208, 0.1696, 12],130889:[0.528, 0.314, 0.2568, 0.2027, 0.115, 12],130890:[0.458, 0.3103, 0.197, 0.1823, 0.1133, 24],
 130892:[0.542, 0.3877, 0.2482, 0.2007, 0.1543, 12],130893:[0.526, 0.3453, 0.2532, 0.2147, 0.1447, 12],130894:[0.559, 0.331, 0.239, 0.1871, 0.109, 24],130896:[0.562, 0.3613, 0.326, 0.2337, 0.186, 12],
 130897:[0.519, 0.366, 0.3058, 0.205, 0.1542, 12],130898:[0.543, 0.3463, 0.2283, 0.1701, 0.1313, 24],130900:[0.527, 0.3787, 0.2637, 0.2314, 0.1437, 12],130901:[0.528, 0.3513, 0.2697, 0.217, 0.184, 12],
 130902:[0.552, 0.3357, 0.2382, 0.1593, 0.1245, 24],130904:[0.616, 0.427, 0.3307, 0.2372, 0.1513, 12],130957:[0.576, 0.3703, 0.2923, 0.2081, 0.1423, 12],131040:[0.476, 0.327, 0.2032, 0.2057, 0.1067, 4],
 131041:[0.499, 0.347, 0.245, 0.2147, 0.144, 12],131044:[0.487, 0.3133, 0.2057, 0.201, 0.143, 4],131045:[0.517, 0.3367, 0.2882, 0.2077, 0.1671, 12],131048:[0.508, 0.3693, 0.2327, 0.1718, 0.1549, 4],
 131049:[0.499, 0.351, 0.2427, 0.184, 0.1523, 12],131052:[0.51, 0.3433, 0.2505, 0.2142, 0.145, 4],131053:[0.529, 0.38, 0.286, 0.207, 0.1745, 12],131056:[0.528, 0.3627, 0.2622, 0.2065, 0.167, 4],
 131057:[0.55, 0.3963, 0.2837, 0.206, 0.19, 12],131060:[0.574, 0.364, 0.2865, 0.2176, 0.1517, 4],131061:[0.557, 0.376, 0.2852, 0.2567, 0.2025, 12],131092:[0.502, 0.3097, 0.213, 0.2028, 0.1203, 12],
 131093:[0.546, 0.3087, 0.264, 0.1741, 0.1153, 12],131094:[0.476, 0.272, 0.1968, 0.1628, 0.0946, 24],131096:[0.498, 0.3397, 0.2745, 0.1977, 0.1477, 12],131097:[0.505, 0.3543, 0.2537, 0.1853, 0.1295, 12],
 131098:[0.468, 0.3097, 0.2583, 0.1487, 0.0907, 24],131100:[0.52, 0.3543, 0.2802, 0.2258, 0.1637, 12],131101:[0.523, 0.3667, 0.2803, 0.2, 0.1457, 12],131102:[0.517, 0.332, 0.2233, 0.1734, 0.1323, 24],
 131104:[0.532, 0.3737, 0.2858, 0.2097, 0.176, 12],131105:[0.503, 0.3363, 0.257, 0.2503, 0.1496, 12],131106:[0.485, 0.341, 0.2483, 0.1604, 0.1153, 24],131108:[0.549, 0.3513, 0.2998, 0.2503, 0.148, 12],
 131109:[0.526, 0.3657, 0.2762, 0.1837, 0.1563, 12],131110:[0.495, 0.3717, 0.2142, 0.2083, 0.1029, 24],131112:[0.514, 0.3643, 0.2952, 0.2311, 0.1692, 12],131113:[0.584, 0.3853, 0.2967, 0.206, 0.146, 12],
 131114:[0.547, 0.3737, 0.2533, 0.2118, 0.1365, 24],131116:[0.64, 0.449, 0.315, 0.271, 0.2037, 12],131169:[0.587, 0.419, 0.2792, 0.2163, 0.142, 12],131248:[0.529, 0.2863, 0.244, 0.1807, 0.1236, 4],
 131249:[0.498, 0.291, 0.286, 0.2085, 0.146, 12],131252:[0.517, 0.325, 0.2205, 0.1911, 0.136, 4],131253:[0.55, 0.3337, 0.2548, 0.193, 0.1437, 12],131256:[0.524, 0.3387, 0.2337, 0.1897, 0.1283, 4],
 131257:[0.531, 0.332, 0.2678, 0.222, 0.1683, 12],131260:[0.541, 0.3277, 0.249, 0.1893, 0.141, 4],131261:[0.554, 0.344, 0.2647, 0.2087, 0.1536, 12],131264:[0.516, 0.3763, 0.2672, 0.207, 0.16, 4],
 131265:[0.48, 0.361, 0.2623, 0.2191, 0.159, 12],131268:[0.539, 0.386, 0.2815, 0.2177, 0.1343, 4],131269:[0.568, 0.39, 0.28, 0.2052, 0.194, 12],131272:[0.532, 0.376, 0.2733, 0.216, 0.1718, 4],
 131273:[0.579, 0.413, 0.3125, 0.2308, 0.1955, 12],131300:[0.48, 0.3147, 0.2702, 0.1963, 0.1496, 12],131301:[0.443, 0.3243, 0.2577, 0.1787, 0.13, 12],131302:[0.508, 0.2933, 0.2143, 0.1807, 0.067, 24],
 131304:[0.555, 0.3353, 0.2028, 0.2441, 0.1383, 12],131305:[0.512, 0.3603, 0.2437, 0.1979, 0.1347, 12],131306:[0.456, 0.3493, 0.2298, 0.169, 0.1106, 24],131308:[0.558, 0.338, 0.2807, 0.1874, 0.1598, 12],
 131309:[0.537, 0.3437, 0.2515, 0.211, 0.147, 12],131310:[0.509, 0.358, 0.2503, 0.172, 0.1323, 24],131312:[0.531, 0.3907, 0.3178, 0.188, 0.1727, 12],131313:[0.528, 0.4007, 0.2712, 0.2447, 0.1682, 12],
 131314:[0.505, 0.3347, 0.2682, 0.172, 0.1326, 24],131316:[0.566, 0.3647, 0.278, 0.2494, 0.146, 12],131317:[0.547, 0.388, 0.2472, 0.2437, 0.1412, 12],131318:[0.59, 0.316, 0.253, 0.189, 0.1202, 24],
 131320:[0.553, 0.3623, 0.2662, 0.2108, 0.1853, 12],131321:[0.552, 0.3597, 0.2447, 0.2298, 0.141, 12],131322:[0.567, 0.3507, 0.2758, 0.2258, 0.1527, 24],131324:[0.568, 0.4, 0.2667, 0.2333, 0.1866, 12],
 131325:[0.602, 0.404, 0.2878, 0.2413, 0.1517, 12],131326:[0.561, 0.354, 0.2547, 0.2183, 0.1403, 24],131328:[0.63, 0.4203, 0.35, 0.2631, 0.2006, 12],131381:[0.622, 0.4207, 0.2962, 0.218, 0.1745, 12],
 131456:[0.502, 0.363, 0.24, 0.1987, 0.149, 4],131457:[0.554, 0.382, 0.2963, 0.2267, 0.153, 12],131460:[0.521, 0.3157, 0.2685, 0.1997, 0.121, 4],131461:[0.536, 0.4027, 0.2518, 0.231, 0.1667, 12],
 131464:[0.517, 0.332, 0.2722, 0.2063, 0.16, 4],131465:[0.492, 0.3847, 0.302, 0.2287, 0.1727, 12],131468:[0.555, 0.313, 0.2443, 0.1951, 0.142, 4],131469:[0.534, 0.4257, 0.2892, 0.2577, 0.1615, 12],
 131472:[0.559, 0.3877, 0.2737, 0.2198, 0.1653, 4],131473:[0.574, 0.3867, 0.285, 0.2661, 0.1763, 12],131476:[0.564, 0.381, 0.2913, 0.2234, 0.169, 4],131477:[0.589, 0.407, 0.308, 0.2671, 0.1683, 12],
 131480:[0.539, 0.4243, 0.301, 0.2344, 0.1757, 4],131481:[0.622, 0.4067, 0.302, 0.2674, 0.1917, 12],131484:[0.597, 0.4327, 0.2937, 0.2547, 0.1869, 4],131485:[0.634, 0.3717, 0.3058, 0.2714, 0.1983, 12],
 131508:[0.564, 0.3583, 0.312, 0.213, 0.1668, 12],131509:[0.555, 0.343, 0.2675, 0.2003, 0.1467, 12],131510:[0.477, 0.3287, 0.209, 0.1994, 0.1361, 24],131512:[0.537, 0.351, 0.291, 0.2474, 0.1773, 12],
 131513:[0.563, 0.374, 0.296, 0.2207, 0.1775, 12],131514:[0.534, 0.327, 0.2565, 0.1771, 0.1453, 24],131516:[0.527, 0.3463, 0.2768, 0.2373, 0.1563, 12],131517:[0.522, 0.364, 0.3015, 0.2557, 0.175, 12],
 131518:[0.554, 0.335, 0.2557, 0.1897, 0.1403, 24],131520:[0.55, 0.3827, 0.2558, 0.2357, 0.1973, 12],131521:[0.542, 0.3827, 0.2675, 0.2147, 0.172, 12],131522:[0.539, 0.3437, 0.2475, 0.1997, 0.1426, 24],
 131524:[0.528, 0.4067, 0.2837, 0.234, 0.1987, 12],131525:[0.528, 0.381, 0.3163, 0.2213, 0.17, 12],131526:[0.511, 0.321, 0.2532, 0.1504, 0.117, 24],131528:[0.602, 0.3853, 0.294, 0.238, 0.1757, 12],
 131529:[0.598, 0.3873, 0.2903, 0.2563, 0.1858, 12],131530:[0.519, 0.408, 0.2787, 0.2231, 0.1452, 24],131532:[0.584, 0.3797, 0.3355, 0.253, 0.1691, 12],131533:[0.58, 0.4033, 0.2625, 0.2777, 0.1813, 12],
 131534:[0.541, 0.4193, 0.3325, 0.2328, 0.1646, 24],131536:[0.568, 0.399, 0.3397, 0.2584, 0.2007, 12],131537:[0.59, 0.4237, 0.3048, 0.2521, 0.1956, 12],131538:[0.568, 0.3927, 0.2612, 0.2279, 0.183, 24],
 131540:[0.698, 0.4687, 0.389, 0.3067, 0.207, 12],131593:[0.632, 0.4647, 0.355, 0.2313, 0.2105, 12],131664:[0.504, 0.3687, 0.2587, 0.1757, 0.1523, 4],131665:[0.534, 0.3577, 0.2872, 0.2497, 0.1408, 12],
 131668:[0.53, 0.3413, 0.2562, 0.233, 0.1613, 4],131669:[0.548, 0.371, 0.3107, 0.2537, 0.1747, 12],131672:[0.488, 0.3727, 0.2762, 0.1881, 0.1527, 4],131673:[0.549, 0.355, 0.291, 0.266, 0.1833, 12],
 131676:[0.533, 0.3723, 0.267, 0.2497, 0.1467, 4],131677:[0.594, 0.4133, 0.2773, 0.2607, 0.1777, 12],131680:[0.536, 0.373, 0.2577, 0.216, 0.1537, 4],131681:[0.547, 0.3587, 0.29, 0.217, 0.1617, 12],
 131684:[0.535, 0.3573, 0.2788, 0.2454, 0.2016, 4],131685:[0.564, 0.3557, 0.2947, 0.2284, 0.1915, 12],131688:[0.555, 0.3793, 0.2957, 0.2411, 0.159, 4],131689:[0.579, 0.3653, 0.3377, 0.2687, 0.1636, 12],
 131692:[0.569, 0.3557, 0.308, 0.2571, 0.174, 4],131693:[0.576, 0.4067, 0.321, 0.2473, 0.146, 12],131696:[0.603, 0.3947, 0.3463, 0.2657, 0.1957, 4],131697:[0.595, 0.4313, 0.3752, 0.2864, 0.2155, 12],
 131716:[0.54, 0.3397, 0.2997, 0.2329, 0.1578, 12],131717:[0.518, 0.3527, 0.2613, 0.2307, 0.1513, 12],131718:[0.489, 0.322, 0.2267, 0.228, 0.1363, 24],131720:[0.554, 0.396, 0.2993, 0.2407, 0.1753, 12],
 131721:[0.544, 0.3783, 0.3028, 0.2237, 0.1546, 12],131722:[0.542, 0.3187, 0.2712, 0.2169, 0.1217, 24],131724:[0.553, 0.384, 0.306, 0.2191, 0.173, 12],131725:[0.546, 0.4147, 0.314, 0.219, 0.1793, 12],
 131726:[0.513, 0.348, 0.272, 0.1975, 0.1146, 24],131728:[0.55, 0.4053, 0.2738, 0.2571, 0.1965, 12],131729:[0.567, 0.3623, 0.2717, 0.2541, 0.1732, 12],131730:[0.526, 0.349, 0.2427, 0.2111, 0.1453, 24],
 131732:[0.548, 0.3713, 0.2917, 0.2193, 0.1642, 12],131733:[0.549, 0.3687, 0.2927, 0.266, 0.1516, 12],131734:[0.519, 0.3437, 0.2072, 0.2161, 0.1453, 24],131736:[0.567, 0.385, 0.344, 0.2447, 0.159, 12],
 131737:[0.547, 0.3537, 0.303, 0.246, 0.1763, 12],131738:[0.528, 0.3407, 0.2972, 0.1831, 0.157, 24],131740:[0.581, 0.3917, 0.3228, 0.2471, 0.196, 12],131741:[0.591, 0.443, 0.2965, 0.2724, 0.1826, 12],
 131742:[0.546, 0.3863, 0.2662, 0.2452, 0.1569, 24],131744:[0.6, 0.39, 0.3235, 0.2737, 0.2043, 12],131745:[0.577, 0.4147, 0.3347, 0.2503, 0.1763, 12],131746:[0.576, 0.3677, 0.2822, 0.2237, 0.1423, 24],
 131748:[0.63, 0.426, 0.3863, 0.251, 0.2357, 12],131749:[0.585, 0.4287, 0.35, 0.3059, 0.1923, 12],131750:[0.624, 0.364, 0.312, 0.2491, 0.2003, 24],131752:[0.677, 0.534, 0.4115, 0.3234, 0.252, 12],
 131805:[0.691, 0.444, 0.3352, 0.29, 0.215, 12],131872:[0.572, 0.3607, 0.2625, 0.1887, 0.1653, 4],131873:[0.514, 0.3973, 0.2827, 0.222, 0.1585, 12],131876:[0.555, 0.359, 0.2687, 0.2273, 0.1488, 4],
 131877:[0.529, 0.411, 0.3237, 0.234, 0.1573, 12],131880:[0.557, 0.3683, 0.3075, 0.2431, 0.1697, 4],131881:[0.575, 0.3623, 0.3057, 0.2027, 0.1975, 12],131884:[0.569, 0.3617, 0.258, 0.1995, 0.179, 4],
 131885:[0.59, 0.4247, 0.2862, 0.2527, 0.159, 12],131888:[0.536, 0.3827, 0.2893, 0.217, 0.143, 4],131889:[0.556, 0.3747, 0.279, 0.2534, 0.2006, 12],131892:[0.545, 0.3587, 0.2672, 0.1872, 0.1482, 4],
 131893:[0.595, 0.4097, 0.2915, 0.22, 0.1728, 12],131896:[0.571, 0.4113, 0.3145, 0.2555, 0.1503, 4],131897:[0.585, 0.4217, 0.3397, 0.2544, 0.2177, 12],131900:[0.573, 0.3857, 0.31, 0.2401, 0.1612, 4],
 131901:[0.531, 0.414, 0.3163, 0.2827, 0.173, 12],131904:[0.577, 0.4487, 0.3182, 0.2721, 0.181, 4],131905:[0.662, 0.4403, 0.3757, 0.3113, 0.1931, 12],131908:[0.617, 0.451, 0.347, 0.2567, 0.229, 4],
 131909:[0.623, 0.4367, 0.3627, 0.2634, 0.183, 12],131924:[0.557, 0.379, 0.3117, 0.2417, 0.184, 12],131925:[0.543, 0.3647, 0.281, 0.2581, 0.1597, 12],131926:[0.501, 0.317, 0.249, 0.2183, 0.1207, 24],
 131928:[0.541, 0.3907, 0.3232, 0.2331, 0.1812, 12],131929:[0.556, 0.3577, 0.2935, 0.2352, 0.1617, 12],131930:[0.503, 0.363, 0.2838, 0.191, 0.1167, 24],131932:[0.578, 0.3703, 0.323, 0.2328, 0.1743, 12],
 131933:[0.561, 0.4237, 0.2775, 0.218, 0.1713, 12],131934:[0.542, 0.3553, 0.302, 0.199, 0.1313, 24],131936:[0.589, 0.387, 0.297, 0.274, 0.1873, 12],131937:[0.563, 0.3913, 0.3102, 0.2821, 0.1733, 12],
 131938:[0.528, 0.382, 0.2595, 0.211, 0.1757, 24],131940:[0.561, 0.405, 0.2653, 0.2783, 0.1607, 12],131941:[0.59, 0.3707, 0.2852, 0.2435, 0.1736, 12],131942:[0.552, 0.3327, 0.2257, 0.2291, 0.1615, 24],
 131944:[0.569, 0.3533, 0.3125, 0.2562, 0.207, 12],131945:[0.534, 0.4107, 0.3475, 0.2284, 0.1885, 12],131946:[0.57, 0.3623, 0.2662, 0.224, 0.1383, 24],131948:[0.564, 0.402, 0.303, 0.2567, 0.195, 12],
 131949:[0.57, 0.4283, 0.3037, 0.2642, 0.18, 12],131950:[0.586, 0.394, 0.2953, 0.1991, 0.126, 24],131952:[0.616, 0.4203, 0.3143, 0.26, 0.2243, 12],131953:[0.553, 0.4023, 0.3347, 0.2557, 0.1713, 12],
 131954:[0.557, 0.3797, 0.2467, 0.2015, 0.1767, 24],131956:[0.607, 0.45, 0.382, 0.3065, 0.189, 12],131957:[0.578, 0.4097, 0.3832, 0.2831, 0.212, 12],131958:[0.618, 0.45, 0.2965, 0.2521, 0.1752, 24],
 131960:[0.586, 0.4647, 0.3473, 0.2815, 0.2243, 12],131961:[0.6, 0.4643, 0.328, 0.2811, 0.1955, 12],131962:[0.61, 0.4183, 0.3352, 0.279, 0.1996, 24],131964:[0.745, 0.5527, 0.432, 0.349, 0.264, 12],
 132017:[0.682, 0.511, 0.3765, 0.3237, 0.2497, 12],132080:[0.523, 0.4007, 0.3087, 0.2375, 0.129, 4],132081:[0.561, 0.3707, 0.292, 0.2627, 0.167, 12],132084:[0.532, 0.4173, 0.2982, 0.2425, 0.1862, 4],
 132085:[0.576, 0.4077, 0.3453, 0.2467, 0.1863, 12],132088:[0.579, 0.347, 0.2977, 0.223, 0.1713, 4],132089:[0.59, 0.3763, 0.3395, 0.2391, 0.1937, 12],132092:[0.559, 0.391, 0.2647, 0.236, 0.1926, 4],
 132093:[0.56, 0.3797, 0.3095, 0.2491, 0.1877, 12],132096:[0.597, 0.4167, 0.2825, 0.2327, 0.1773, 4],132097:[0.559, 0.3937, 0.292, 0.2564, 0.198, 12],132100:[0.525, 0.3897, 0.3112, 0.246, 0.1603, 4],
 132101:[0.538, 0.4033, 0.306, 0.2441, 0.1683, 12],132104:[0.558, 0.412, 0.3253, 0.25, 0.2053, 4],132105:[0.574, 0.4227, 0.343, 0.2811, 0.2213, 12],132108:[0.622, 0.425, 0.3205, 0.24, 0.18, 4],
 132109:[0.612, 0.379, 0.3067, 0.234, 0.1978, 12],132112:[0.636, 0.4217, 0.3272, 0.2797, 0.2123, 4],132113:[0.63, 0.4397, 0.3472, 0.285, 0.216, 12],132116:[0.58, 0.4407, 0.3137, 0.2674, 0.2017, 4],
 132117:[0.599, 0.4323, 0.336, 0.265, 0.1897, 12],132120:[0.57, 0.432, 0.387, 0.2777, 0.231, 4],132121:[0.594, 0.453, 0.3753, 0.3141, 0.2183, 12],132132:[0.549, 0.3807, 0.321, 0.2714, 0.2012, 12],
 132133:[0.555, 0.3547, 0.327, 0.2623, 0.1707, 12],132134:[0.535, 0.3547, 0.2903, 0.1958, 0.1587, 24],132136:[0.548, 0.3927, 0.325, 0.2687, 0.1663, 12],132137:[0.55, 0.3893, 0.2912, 0.2587, 0.1731, 12],
 132138:[0.552, 0.4167, 0.2937, 0.1957, 0.1407, 24],132140:[0.548, 0.425, 0.32, 0.3091, 0.1913, 12],132141:[0.561, 0.4267, 0.3125, 0.279, 0.1953, 12],132142:[0.568, 0.369, 0.2907, 0.2012, 0.168, 24],
 132144:[0.616, 0.4237, 0.3475, 0.2605, 0.2107, 12],132145:[0.557, 0.3823, 0.3317, 0.2977, 0.1952, 12],132146:[0.598, 0.401, 0.2602, 0.215, 0.162, 24],132148:[0.596, 0.421, 0.3178, 0.2354, 0.1858, 12],
 132149:[0.563, 0.3897, 0.3125, 0.2477, 0.183, 12],132150:[0.539, 0.3117, 0.2617, 0.2163, 0.143, 24],132152:[0.559, 0.4007, 0.3243, 0.2527, 0.1842, 12],132153:[0.599, 0.3753, 0.3132, 0.244, 0.1803, 12],
 132154:[0.562, 0.376, 0.2837, 0.219, 0.1367, 24],132156:[0.555, 0.4217, 0.3182, 0.2894, 0.2153, 12],132157:[0.579, 0.4823, 0.3175, 0.2837, 0.1757, 12],132158:[0.575, 0.385, 0.2972, 0.2234, 0.16, 24],
 132160:[0.562, 0.4253, 0.316, 0.2397, 0.1906, 12],132161:[0.579, 0.3907, 0.3253, 0.2633, 0.1777, 12],132162:[0.541, 0.359, 0.292, 0.2458, 0.1687, 24],132164:[0.588, 0.3963, 0.3595, 0.3094, 0.209, 12],
 132165:[0.572, 0.4123, 0.3497, 0.2875, 0.2157, 12],132166:[0.582, 0.444, 0.3158, 0.2515, 0.2143, 24],132168:[0.593, 0.46, 0.3652, 0.2783, 0.2543, 12],132169:[0.608, 0.419, 0.3538, 0.3014, 0.1946, 12],
 132170:[0.628, 0.45, 0.327, 0.2771, 0.209, 24],132172:[0.61, 0.4043, 0.376, 0.3471, 0.26, 12],132173:[0.593, 0.447, 0.399, 0.287, 0.2417, 12],132174:[0.593, 0.407, 0.3147, 0.278, 0.1932, 24],
 132176:[0.745, 0.596, 0.485, 0.354, 0.2623, 12],132229:[0.708, 0.5567, 0.4062, 0.329, 0.2263, 12],134992:[0.726, 0.5603, 0.455, 0.3964, 0.2773, 12],134994:[0.702, 0.552, 0.379, 0.3405, 0.215, 12],
 134996:[0.743, 0.563, 0.4852, 0.3404, 0.2798, 12],134998:[0.723, 0.528, 0.428, 0.3579, 0.2493, 12],135000:[0.771, 0.561, 0.4692, 0.3384, 0.31, 12],135002:[0.749, 0.575, 0.42, 0.3085, 0.2437, 12],
 135004:[0.762, 0.5887, 0.4273, 0.447, 0.277, 12],135006:[0.756, 0.5477, 0.4522, 0.3647, 0.2707, 12],135008:[0.744, 0.5733, 0.4708, 0.3507, 0.2733, 12],135010:[0.707, 0.566, 0.448, 0.342, 0.2566, 12],
 135012:[0.724, 0.6043, 0.4025, 0.383, 0.261, 12],135014:[0.749, 0.58, 0.442, 0.3357, 0.2318, 12],135016:[0.752, 0.5757, 0.4907, 0.365, 0.306, 12],135018:[0.729, 0.5647, 0.461, 0.3557, 0.2513, 12],
 135020:[0.745, 0.5307, 0.455, 0.3957, 0.2703, 12],135022:[0.729, 0.5753, 0.475, 0.37, 0.248, 12],135024:[0.724, 0.577, 0.4857, 0.359, 0.2768, 12],135026:[0.739, 0.5587, 0.426, 0.3711, 0.2338, 12],
 135028:[0.751, 0.5847, 0.4522, 0.3414, 0.26, 12],135030:[0.762, 0.5277, 0.4713, 0.4011, 0.252, 12],135032:[0.739, 0.607, 0.4595, 0.3878, 0.292, 12],135034:[0.776, 0.529, 0.412, 0.366, 0.2937, 12],
 135036:[0.77, 0.579, 0.4635, 0.3862, 0.2727, 12],135038:[0.756, 0.5793, 0.4665, 0.3154, 0.2537, 12],137796:[0.699, 0.53, 0.3465, 0.269, 0.199, 4],
}


def _derive():
    combos2 = {}
    for a in range(52):
        for b in range(a + 1, 52):
            combos2.setdefault(key2(a, b), []).append((a, b))
    # percentile by equity vs 3 opponents, weighted by combos (1.0 = best hand)
    order = sorted(PF2, key=lambda k: PF2[k][2])
    total = 1326.0
    acc = 0
    pct2 = {}
    for k in order:
        n = len(combos2[k])
        pct2[k] = (acc + n / 2.0) / total
        acc += n
    order3 = sorted(PF3, key=lambda k: PF3[k][2])
    acc = 0
    pct3 = {}
    for k in order3:
        n = PF3[k][5]
        pct3[k] = (acc + n / 2.0) / 22100.0
        acc += n
    rng2 = []
    for k in sorted(PF2, key=lambda k: -pct2[k]):
        rng2.extend(combos2[k])
    return pct2, pct3, rng2


if getattr(_S, "derived", None) is None:
    _S.derived = _derive()
PCT2, PCT3, RANGE2 = _S.derived
