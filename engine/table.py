"""One sit-and-go table, played until one bot holds every chip or the hand cap is reached
(official rules §4.4). Supports every variant in engine/variants.py.

Common rules:
  * stacks start at cfg.starting_stack (2000); blinds follow cfg.blind_schedule, going up
    every cfg.blind_interval hands (25); optional antes
  * the button moves one live seat clockwise each hand; heads-up the button posts the small
    blind and acts first preflop / last postflop
  * no-limit: min raise = previous full raise (>= 1 big blind); an all-in for less than a full
    raise does not reopen the betting for players who already acted
  * pot-limit: max raise-to = current bet + pot + amount to call
  * fixed-limit: small bet = big blind, big bet = 2x big blind (from cfg-defined round), 4-bet cap
  * stud: ante = big blind/5, bring-in = big blind/4, small/big bet = 1x/2x big blind; lowest
    up card brings in (highest in razz); best showing hand acts first on later streets; if the
    deck runs out on 7th street a single community card is dealt
  * draw: blinds; after each betting round players discard and redraw; the discard pile is
    reshuffled if the stub runs out
  * side pots, split pots (odd chips to the first winner left of the button; to the high hand
    in hi-lo), uncalled bets returned
  * busted players are placed by elimination order; same-hand busts ordered by stack at the
    start of that hand (equal stacks split the places)
  * at the hand cap the remaining places are paid to the survivors by ICM (§4.5)
"""
from .cards import CARD_STRS
from .api import Observation, PlayerView, HandResult, FOLD, CHECK, CALL, RAISE
from .sandbox import timed_call, TIMEOUT, ERROR
from .scoring import icm, points_for_places
from .variants import (get_variant, showdown_scores, describe, showing_rank, permutation, RANKS,
                       SUIT_ORDER)

COMMUNITY_STREET_NAMES = ["preflop", "flop", "turn", "river", "river+1", "river+2", "river+3"]
FL_CAP = 4


def _cs(cards):
    return [CARD_STRS[c] for c in cards]


class Seat:
    __slots__ = ("bot", "stack", "eliminated", "hole", "up", "bet", "contributed", "folded",
                 "all_in", "in_hand", "start_stack", "reopened", "bounty", "bid", "discarded")

    def __init__(self, bot, stack):
        self.bot = bot
        self.stack = stack
        self.eliminated = False
        self.hole = []
        self.up = []
        self.bet = 0
        self.contributed = 0
        self.folded = False
        self.all_in = False
        self.in_hand = False
        self.start_stack = stack
        self.reopened = True
        self.bounty = None
        self.bid = None
        self.discarded = []


class TableResult:
    __slots__ = ("places", "points", "hands_played", "final_stacks", "bust_hand", "hit_cap")

    def __init__(self, n):
        self.places = [None] * n
        self.points = [0.0] * n
        self.hands_played = 0
        self.final_stacks = [0] * n
        self.bust_hand = [None] * n
        self.hit_cap = False


class Table:
    def __init__(self, bots, cfg, rng, log=None):
        self.cfg = cfg
        self.rng = rng
        self.n = len(bots)
        self.seats = [Seat(b, cfg.starting_stack) for b in bots]
        self.log = log
        self.button = None
        self.hand_number = 0
        self.history = []
        self.board = []
        self.board2 = []
        self.limit_s = cfg.time_limit_ms / 1000.0
        self.cb_limit_s = getattr(cfg, "callback_time_limit_ms", cfg.time_limit_ms) / 1000.0
        self.mode = cfg.time_mode
        self.payouts = list(cfg.points) + [0] * max(0, self.n - len(cfg.points))
        self.result = TableResult(self.n)
        self.remaining_places = self.n
        self.base = get_variant(getattr(cfg, "variant", "nlhe"))
        self.betting_override = getattr(cfg, "betting", "") or ""
        self.perm = permutation(rng) if "permutation" in self.base.special else None
        self.v = self.base
        self._round = 0
        self._phase = "bet"
        self._discard_count = None
        self._draws_left = 0
        self._bring_in = 0

    # ------------------------------------------------------------------ bot calls
    def _call(self, seat_idx, method, arg, is_decision=False):
        slot = self.seats[seat_idx].bot
        agent = slot.agent
        if agent is None:
            return ERROR
        fn = getattr(agent, method, None)
        if fn is None:
            return None
        limit = self.limit_s if is_decision else self.cb_limit_s
        return timed_call(fn, (arg,), limit, self.mode, slot.stats, is_decision)

    # ------------------------------------------------------------------ helpers
    def _next_live(self, i):
        n = self.n
        for k in range(1, n + 1):
            j = (i + k) % n
            if not self.seats[j].eliminated:
                return j
        return i

    def _alive(self):
        return [i for i, s in enumerate(self.seats) if not s.eliminated]

    def _blinds(self):
        sched = self.cfg.blind_schedule
        level = (self.hand_number - 1) // self.cfg.blind_interval
        lvl = sched[min(level, len(sched) - 1)]
        sb, bb = lvl[0], lvl[1]
        ante = lvl[2] if len(lvl) > 2 else 0
        until_up = self.cfg.blind_interval - ((self.hand_number - 1) % self.cfg.blind_interval)
        return sb, bb, ante, until_up, level

    def _pot(self):
        return sum(s.contributed for s in self.seats)

    def _put(self, i, amount, as_bet=True):
        s = self.seats[i]
        amount = max(0, min(amount, s.stack))
        s.stack -= amount
        if as_bet:
            s.bet += amount
        s.contributed += amount
        if s.stack == 0 and s.in_hand:
            s.all_in = True
        return amount

    def _w(self, line):
        if self.log is not None:
            self.log.append(line)

    def _name(self, i):
        return f"S{i}:{self.seats[i].bot.name}"

    def _street_value(self):
        v = self.v
        if v.game == "stud":
            s = self.seats
            return max((len(x.hole) + len(x.up) for x in s if x.in_hand), default=0)
        if v.game == "draw":
            return self._round
        return len(self.board)

    def _street_name(self):
        v = self.v
        if v.game == "stud":
            return f"{self._street_value()}th street" if self._street_value() > 3 else "3rd street"
        if v.game == "draw":
            return "predraw" if self._round == 0 else f"after draw {self._round}"
        return COMMUNITY_STREET_NAMES[min(self._round, len(COMMUNITY_STREET_NAMES) - 1)]

    # ------------------------------------------------------------------ observation
    def _obs(self, i, legal_info=None, phase="bet"):
        s = self.seats[i]
        current_bet = max(x.bet for x in self.seats)
        to_call = min(max(0, current_bet - s.bet), s.stack)
        if legal_info is None:
            legal, mn, mx = self._legal(i, current_bet) if phase == "bet" else ([phase], None, None)
        else:
            legal, mn, mx = legal_info
        players = []
        for j, x in enumerate(self.seats):
            players.append(PlayerView(j, x.stack, x.bet, x.contributed, x.folded, x.all_in,
                                      x.in_hand and not x.folded, x.eliminated, _cs(x.up)))
        v = self.v
        obs = Observation(
            seat=i, hand_number=self.hand_number, street=self._street_value(),
            street_name=self._street_name(), round_index=self._round,
            hole_cards=_cs(s.hole) + _cs(s.up), down_cards=_cs(s.hole), up_cards=_cs(s.up),
            board=_cs(self.board), board2=_cs(self.board2), pot=self._pot(), to_call=to_call,
            my_bet=s.bet, my_stack=s.stack, my_contributed=s.contributed,
            current_bet=current_bet, min_raise_to=mn, max_raise_to=mx, legal_actions=legal,
            small_blind=self._sb, big_blind=self._bb, ante=self._ante, bring_in=self._bring_in,
            button=self.button, small_blind_seat=self._sb_seat, big_blind_seat=self._bb_seat,
            num_players=self.n, num_active=sum(1 for x in self.seats if not x.eliminated),
            players=players, players_up_cards={j: _cs(x.up) for j, x in enumerate(self.seats) if x.up},
            history=list(self.history), starting_stack=self.cfg.starting_stack,
            hands_until_blinds_up=self._until_up, max_hands=self.cfg.max_hands,
            time_limit_ms=self.cfg.time_limit_ms,
            variant=v.id, variant_name=v.name, game=v.game, betting=self._structure,
            hand_rule=v.hand, split=v.split, deck_ranks=v.ranks, num_hole_cards=len(s.hole) + len(s.up),
            phase=phase, discard_count=self._discard_count if phase == "discard" else None,
            draws_remaining=self._draws_left, bounty_rank=s.bounty,
            min_bid=0 if phase == "bid" else None, max_bid=s.stack if phase == "bid" else None,
            bet_size=self._fl_size if self._structure == "fixed_limit" else None,
        )
        return obs

    def _legal(self, i, current_bet):
        s = self.seats[i]
        to_call = current_bet - s.bet
        legal = []
        if to_call > 0:
            legal += [FOLD, CALL]
        else:
            legal.append(CHECK)
        others_can_act = any((not x.folded) and x.in_hand and (not x.all_in) and j != i
                             for j, x in enumerate(self.seats))
        mn = mx = None
        if s.reopened and s.stack > to_call and others_can_act:
            allin = s.bet + s.stack
            st = self._structure
            if st == "fixed_limit":
                if self._fl_bets < FL_CAP:
                    target = self._fl_level + self._fl_size
                    if target <= current_bet:
                        target = current_bet + self._fl_size
                    mn = mx = min(target, allin)
            elif st == "pot_limit":
                mx = min(allin, current_bet + self._pot() + to_call)
                mn = min(current_bet + self._min_inc, mx)
            else:
                mx = allin
                mn = min(current_bet + self._min_inc, mx)
            if mx is not None:
                legal.append(RAISE)
        return legal, mn, mx

    # ------------------------------------------------------------------ table flow
    def play(self):
        alive = self._alive()
        self.button = self.rng.choice(alive)
        first = True
        while True:
            alive = self._alive()
            if len(alive) <= 1:
                break
            if self.hand_number >= self.cfg.max_hands:
                self.result.hit_cap = True
                break
            self.hand_number += 1
            if not first:
                self.button = self._next_live(self.button)
            first = False
            self._play_hand()
        self._finish()
        return self.result

    def _setup_hand(self):
        sb, bb, ante, until_up, level = self._blinds()
        self._sb, self._bb, self._ante, self._until_up = sb, bb, ante, until_up
        base = self.base
        v = get_variant(base.rotation[level % len(base.rotation)]) if base.rotation else base
        self.v = v
        st = self.betting_override if (self.betting_override and v.game != "stud") else v.betting
        if base.rotation and not self.betting_override:
            st = v.betting
        self._structure = st
        self._bring_in = 0
        self._round = 0
        self._discard_count = None
        self._draws_left = v.draws
        self._sb_seat = self._bb_seat = None
        for s in self.seats:
            s.hole = []
            s.up = []
            s.bet = 0
            s.contributed = 0
            s.folded = False
            s.all_in = False
            s.in_hand = not s.eliminated
            s.start_stack = s.stack
            s.reopened = True
            s.bid = None
            s.discarded = []
        # bounty ranks (MIT 2025): re-drawn every 25 hands
        if "bounty" in base.special and (self.hand_number - 1) % 25 == 0:
            for s in self.seats:
                s.bounty = self.rng.choice(RANKS)
        self.history = []
        self.board = []
        self.board2 = []
        deck = [c for c in range(52) if RANKS[c >> 2] in v.ranks]
        self.rng.shuffle(deck)
        self.deck = deck
        self.dpos = 0
        self.muck = []

    def _deal(self, k):
        out = []
        for _ in range(k):
            if self.dpos >= len(self.deck):
                if not self.muck:
                    break
                self.rng.shuffle(self.muck)
                self.deck = self.deck[:self.dpos] + self.muck
                self.muck = []
            out.append(self.deck[self.dpos])
            self.dpos += 1
        return out

    def _cards_left(self):
        return len(self.deck) - self.dpos

    def _order_from(self, start_inclusive):
        n = self.n
        return [(start_inclusive + k) % n for k in range(n)]

    def _in_hand(self):
        return [i for i, s in enumerate(self.seats) if s.in_hand and not s.folded]

    def _post_blinds(self):
        seats = self.seats
        alive = self._alive()
        if len(alive) == 2:
            sb_seat = self.button
            bb_seat = self._next_live(self.button)
        else:
            sb_seat = self._next_live(self.button)
            bb_seat = self._next_live(sb_seat)
        self._sb_seat, self._bb_seat = sb_seat, bb_seat
        if self._ante:
            for i in alive:
                a = self._put(i, self._ante, as_bet=False)
                self.history.append((i, "ante", a, 0))
        self._put(sb_seat, self._sb)
        self.history.append((sb_seat, "small_blind", seats[sb_seat].bet, 0))
        self._put(bb_seat, self._bb)
        self.history.append((bb_seat, "big_blind", seats[bb_seat].bet, 0))
        return sb_seat, bb_seat

    def _start_round(self, idx):
        self._round = idx
        for s in self.seats:
            s.bet = 0
            s.reopened = True
        self._min_inc = self._bb
        big = idx >= self.v.big_bet_from
        self._fl_size = self._bb * (2 if big else 1)
        self._fl_bets = 0
        self._fl_level = 0

    def _notify_new_hand(self, order):
        for i in order:
            if self.seats[i].bot.agent is not None and hasattr(self.seats[i].bot.agent, "new_hand"):
                self._call(i, "new_hand", self._obs(i))

    def _play_hand(self):
        self._setup_hand()
        v = self.v
        if v.game == "stud":
            stakes = (f"ante {max(1, self._bb // 5)}, bring-in {max(1, self._bb // 4)}, "
                      f"limits {self._bb}/{2 * self._bb}")
        else:
            stakes = f"blinds {self._sb}/{self._bb}" + (f" ante {self._ante}" if self._ante else "")
        self._w(f"--- Hand {self.hand_number}  [{v.name}, {self._structure.replace('_', '-')}]  "
                f"{stakes}  button S{self.button}")
        if v.game == "stud":
            self._hand_stud()
        elif v.game == "draw":
            self._hand_draw()
        else:
            self._hand_community()

    def _log_holdings(self, order):
        if self.log is None:
            return
        for i in order:
            s = self.seats[i]
            up = f" up [{' '.join(_cs(s.up))}]" if s.up else ""
            b = f" bounty={s.bounty}" if s.bounty else ""
            self._w(f"  {self._name(i)} stack={s.stack} [{' '.join(_cs(s.hole))}]{up}{b}")

    # ------------------------------------------------------------------ community games
    def _hand_community(self):
        v = self.v
        seats = self.seats
        alive = self._alive()
        order = []
        j = self.button
        for _ in range(len(alive)):
            j = self._next_live(j)
            order.append(j)
        for i in order:
            seats[i].hole = self._deal(v.hole)
        self._start_round(0)
        sb_seat, bb_seat = self._post_blinds()
        self._fl_bets, self._fl_level = 1, self._bb  # the big blind is the first bet
        # Courchevel-style: board cards before preflop betting
        if v.streets[0]:
            self.board.extend(self._deal(v.streets[0]))
            if v.boards == 2:
                self.board2.extend(self._deal(v.streets[0]))
        self._log_holdings(order)
        self._notify_new_hand(order)
        done = self._betting_round(self._next_live(bb_seat))
        rounds = list(v.streets[1:])
        rnd = 1
        while True:
            if rnd - 1 < len(rounds):
                ncards = rounds[rnd - 1]
            elif "red_river" in v.special and self._red_river_continue():
                ncards = 1
            else:
                break
            if len(self._in_hand()) <= 1:
                break
            self._discards(rnd, "pre")
            if "swap" in v.special and rnd in (1, 2):
                self._swap(0.10 if rnd == 1 else 0.05)
            self._start_round(rnd)
            if self._cards_left() < ncards * v.boards:
                break
            self.board.extend(self._deal(ncards))
            if v.boards == 2:
                self.board2.extend(self._deal(ncards))
            self._w(f"  {self._street_name()}: {' '.join(_cs(self.board))}"
                    + (f"  | board 2: {' '.join(_cs(self.board2))}" if v.boards == 2 else "")
                    + f"  pot={self._pot()}")
            if "auction" in v.special and rnd == 1:
                self._auction()
            self._discards(rnd, "post_deal")
            if not done:  # if everyone is all-in, the board is still run out (and discards made)
                done = self._betting_round(self._next_live(self.button))
            rnd += 1
        in_hand = self._in_hand()
        self._settle(in_hand)

    def _red_river_continue(self):
        if len(self.board) < 5 or self._cards_left() < 1:
            return False
        return (self.board[-1] & 3) in (1, 2)  # diamonds or hearts

    def _swap(self, p):
        for i in self._in_hand():
            s = self.seats[i]
            for k in range(len(s.hole)):
                if self.rng.random() < p and self._cards_left() > 0:
                    new = self._deal(1)[0]
                    self.deck.append(s.hole[k])  # back to the bottom of the deck (MIT 2022)
                    self._w(f"    {self._name(i)} swaps {CARD_STRS[s.hole[k]]} -> {CARD_STRS[new]}")
                    s.hole[k] = new

    def _discards(self, rnd, when):
        for (r, w, count, to_board) in self.v.discards:
            if r != rnd or w != when:
                continue
            start = self._next_live(self.button)
            for i in self._order_from(start):
                s = self.seats[i]
                if not (s.in_hand and not s.folded):
                    continue
                chosen = self._ask_discard(i, count, exact=True)
                for c in chosen:
                    s.hole.remove(c)
                    s.discarded.append(c)
                    if to_board:
                        self.board.append(c)
                    else:
                        self.muck.append(c)
                self.history.append((i, "discard", len(chosen), self._street_value()))
                self._w(f"    {self._name(i)} discards {' '.join(_cs(chosen))}"
                        + (" (face up to the board)" if to_board else ""))

    def _ask_discard(self, i, count, exact):
        """Ask the bot which cards to discard. exact=True: must discard `count` cards
        (default: the lowest ranks); exact=False: 0..count cards (default: stand pat)."""
        s = self.seats[i]
        self._discard_count = count
        raw = self._call(i, "discard", self._obs(i, phase="discard"), is_decision=True)
        self._discard_count = None
        chosen = []
        if raw not in (None, TIMEOUT, ERROR):
            try:
                items = [raw] if isinstance(raw, (str, int)) else list(raw)
                for it in items:
                    if isinstance(it, int) and not isinstance(it, bool):
                        c = s.hole[it] if 0 <= it < len(s.hole) else None
                    else:
                        txt = str(it)
                        c = next((h for h in s.hole if CARD_STRS[h].lower() == txt.lower()), None)
                    if c is not None and c not in chosen:
                        chosen.append(c)
            except Exception:
                chosen = []
        if exact:
            if len(chosen) != count:
                if raw not in (TIMEOUT, ERROR):
                    s.bot.stats.invalid += 1
                chosen = sorted(s.hole, key=lambda c: (c >> 2, c & 3))[:count]
        else:
            chosen = chosen[:count]
        return chosen

    def _auction(self):
        """MIT 2024 second-price auction for one extra hole card, generalized to N players."""
        bidders = [i for i in self._in_hand()]
        for i in bidders:
            s = self.seats[i]
            raw = self._call(i, "bid", self._obs(i, phase="bid"), is_decision=True)
            try:
                b = int(raw) if raw not in (None, TIMEOUT, ERROR) else 0
            except Exception:
                b = 0
                s.bot.stats.invalid += 1
            s.bid = max(0, min(b, s.stack))
        bids = sorted((self.seats[i].bid for i in bidders), reverse=True)
        top = bids[0]
        winners = [i for i in bidders if self.seats[i].bid == top]
        if len(winners) > 1:
            pay = {i: self.seats[i].bid for i in winners}
        else:
            second = bids[1] if len(bids) > 1 else 0
            pay = {winners[0]: second}
        for i, amt in pay.items():
            paid = self._put(i, amt, as_bet=False)
            self.seats[i].hole.extend(self._deal(1))
            self.history.append((i, "auction_win", paid, self._street_value()))
        self._w("    auction bids: " + ", ".join(f"S{i}={self.seats[i].bid}" for i in bidders)
                + " -> card to " + ", ".join(f"S{i} (pays {pay[i]})" for i in winners))

    # ------------------------------------------------------------------ stud
    def _hand_stud(self):
        v = self.v
        seats = self.seats
        alive = self._alive()
        low = v.split == "low_a5"
        bb = self._bb
        self._ante = max(1, bb // 5)
        self._bring_in = max(1, bb // 4)
        for i in alive:
            a = self._put(i, self._ante, as_bet=False)
            self.history.append((i, "ante", a, 0))
        order = self._order_from(self._next_live(self.button))
        order = [i for i in order if not seats[i].eliminated]
        done = False
        for rnd, pattern in enumerate(v.stud):
            players = self._in_hand()
            if len(players) <= 1:
                break
            need = len(players) * len(pattern)
            if self._cards_left() < need:
                # not enough cards for everyone: one shared community card (standard stud rule)
                self.board.extend(self._deal(1))
                self._w(f"  community card: {' '.join(_cs(self.board))}")
            else:
                for i in order:
                    if i not in players:
                        continue
                    for p in pattern:
                        c = self._deal(1)
                        (seats[i].up if p == "u" else seats[i].hole).extend(c)
            self._start_round(rnd)
            if rnd == 0:
                self._log_holdings(order)
                self._notify_new_hand(order)
                # bring-in: lowest up card (razz: highest); ties broken by suit
                def key(i):
                    c = seats[i].up[-1]
                    r = c >> 2
                    rv = ((r + 1) % 13) if low else r  # razz: ace low
                    return (rv, c & 3)
                cands = [i for i in players if seats[i].up]
                bi = (max(cands, key=key) if low else min(cands, key=key))
                self._put(bi, self._bring_in)
                self.history.append((bi, "bring_in", seats[bi].bet, self._street_value()))
                self._w(f"    {self._name(bi)} brings in for {seats[bi].bet}")
                seats[bi].reopened = True
                first = (bi + 1) % self.n
                done = self._betting_round(first, skip={bi})
            else:
                self._w(f"  {self._street_name()}: " + "  ".join(
                    f"S{i}[{' '.join(_cs(seats[i].up))}]" for i in players) + f"  pot={self._pot()}")
                if not done:
                    rank = {i: showing_rank(seats[i].up, low) for i in players}
                    if low:
                        first = min(players, key=lambda i: (rank[i], i))
                    else:
                        first = max(players, key=lambda i: (rank[i], -i))
                    done = self._betting_round(first)
        # deal remaining cards to players still in if betting ended early (all-in)
        in_hand = self._in_hand()
        if len(in_hand) > 1:
            total_cards = sum(len(p) for p in v.stud)
            for i in in_hand:
                s = seats[i]
                while len(s.hole) + len(s.up) + len(self.board) < total_cards and self._cards_left() > 0:
                    s.hole.extend(self._deal(1))
        self._settle(in_hand)

    # ------------------------------------------------------------------ draw
    def _hand_draw(self):
        v = self.v
        seats = self.seats
        alive = self._alive()
        order = []
        j = self.button
        for _ in range(len(alive)):
            j = self._next_live(j)
            order.append(j)
        for i in order:
            seats[i].hole = self._deal(v.hole)
        self._start_round(0)
        sb_seat, bb_seat = self._post_blinds()
        self._fl_bets, self._fl_level = 1, self._bb
        self._log_holdings(order)
        self._notify_new_hand(order)
        done = self._betting_round(self._next_live(bb_seat))
        for d in range(v.draws):
            if len(self._in_hand()) <= 1:
                break
            self._draws_left = v.draws - d
            self._round = d + 1
            for i in self._order_from(self._next_live(self.button)):
                s = seats[i]
                if not (s.in_hand and not s.folded):
                    continue
                chosen = self._ask_discard(i, len(s.hole), exact=False)
                for c in chosen:
                    s.hole.remove(c)
                    self.muck.append(c)
                s.hole.extend(self._deal(len(chosen)))
                self.history.append((i, "draw", len(chosen), d + 1))
                self._w(f"    {self._name(i)} draws {len(chosen)}"
                        + (f" -> [{' '.join(_cs(s.hole))}]" if self.log is not None else ""))
            self._draws_left = v.draws - d - 1
            self._start_round(d + 1)
            if not done:
                done = self._betting_round(self._next_live(self.button))
        self._settle(self._in_hand())

    # ------------------------------------------------------------------ betting
    def _betting_round(self, first, skip=()):
        """Returns True if the hand is over or no more betting is possible."""
        seats = self.seats
        n = self.n

        def contenders():
            return [i for i, s in enumerate(seats) if s.in_hand and not s.folded]

        def can_act(i):
            s = seats[i]
            return s.in_hand and not s.folded and not s.all_in

        if len(contenders()) <= 1:
            return True
        needs = {i for i in range(n) if can_act(i) and i not in skip}
        current_bet = max(s.bet for s in seats)
        if len([i for i in range(n) if can_act(i)]) <= 1:
            only = next((i for i in range(n) if can_act(i)), None)
            if only is None or seats[only].bet >= current_bet:
                return True
        p = first
        guard = 0
        while needs:
            guard += 1
            if guard > 2000:
                break
            i = None
            for k in range(n):
                j = (p + k) % n
                if j in needs:
                    i = j
                    break
            if i is None:
                break
            needs.discard(i)
            s = seats[i]
            current_bet = max(x.bet for x in seats)
            legal, mn, mx = self._legal(i, current_bet)
            obs = self._obs(i, (legal, mn, mx))
            raw = self._call(i, "act", obs, is_decision=True)
            act, amt = self._normalize(i, raw, legal, mn, mx, current_bet)
            to_call = current_bet - s.bet
            sv = self._street_value()
            if act == FOLD:
                s.folded = True
                self.history.append((i, FOLD, s.bet, sv))
                self._w(f"    {self._name(i)} folds")
            elif act == CHECK:
                self.history.append((i, CHECK, s.bet, sv))
                self._w(f"    {self._name(i)} checks")
            elif act == CALL:
                self._put(i, to_call)
                self.history.append((i, CALL, s.bet, sv))
                self._w(f"    {self._name(i)} calls {s.bet}" + (" (all-in)" if s.all_in else ""))
            else:
                inc = amt - current_bet
                self._put(i, amt - s.bet)
                if self._structure == "fixed_limit":
                    full = amt >= self._fl_level + self._fl_size or amt >= current_bet + self._fl_size
                    if full:
                        self._fl_bets += 1
                        self._fl_level = amt
                else:
                    full = inc >= self._min_inc
                    if full:
                        self._min_inc = inc
                for j in range(n):
                    if j != i and can_act(j):
                        needs.add(j)
                        if full:
                            seats[j].reopened = True
                self.history.append((i, RAISE, s.bet, sv))
                self._w(f"    {self._name(i)} raises to {s.bet}" + (" (all-in)" if s.all_in else ""))
            s.reopened = False
            if len(contenders()) <= 1:
                return True
            p = (i + 1) % n
        active = [i for i in range(n) if can_act(i)]
        return len(contenders()) <= 1 or len(active) <= 1

    def _normalize(self, i, raw, legal, mn, mx, current_bet):
        s = self.seats[i]
        stats = s.bot.stats
        to_call = current_bet - s.bet
        default = (CHECK, None) if to_call <= 0 else (FOLD, None)
        if raw is TIMEOUT or raw is ERROR or raw is None:
            if raw is None:
                stats.invalid += 1
            return default
        act, amt = None, None
        try:
            if isinstance(raw, str):
                act = raw.lower()
            elif isinstance(raw, (tuple, list)) and raw:
                act = str(raw[0]).lower()
                amt = raw[1] if len(raw) > 1 else None
            elif isinstance(raw, dict):
                act = str(raw.get("action", "")).lower()
                amt = raw.get("amount")
            else:
                stats.invalid += 1
                return default
        except Exception:
            stats.invalid += 1
            return default
        if act in ("allin", "all-in", "all_in", "shove", "jam"):
            if RAISE in legal:
                return RAISE, mx
            return (CALL, None) if to_call > 0 else (CHECK, None)
        if act in ("bet", "complete"):
            act = RAISE
        if act == FOLD:
            return FOLD, None
        if act == CHECK:
            if to_call > 0:
                stats.invalid += 1
                return FOLD, None
            return CHECK, None
        if act == CALL:
            if to_call <= 0:
                return CHECK, None
            return CALL, None
        if act == RAISE:
            if RAISE not in legal:
                stats.invalid += 1
                return (CALL, None) if to_call > 0 else (CHECK, None)
            try:
                amt = int(round(float(amt))) if amt is not None else mn
            except Exception:
                stats.invalid += 1
                amt = mn
            if amt < mn:
                if self._structure != "fixed_limit":
                    stats.invalid += 1
                amt = mn
            elif amt > mx:
                amt = mx
            return RAISE, amt
        stats.invalid += 1
        return default

    # ------------------------------------------------------------------ showdown
    def _cards_for(self, i, board):
        s = self.seats[i]
        hole = list(s.hole) + list(s.up)
        if self.perm is not None:
            pm = self.perm
            hole = [pm[c >> 2] * 4 + (c & 3) for c in hole]
            board = [pm[c >> 2] * 4 + (c & 3) for c in board]
        return hole, list(board)

    def _settle(self, in_hand):
        seats = self.seats
        n = self.n
        v = self.v
        winnings = [0] * n
        shown = {}
        bounty_extra = []
        if len(in_hand) == 1:
            w = in_hand[0]
            winnings[w] = self._pot()
            self._w(f"  {self._name(w)} wins {winnings[w]} uncontested")
        else:
            boards = [self.board] + ([self.board2] if v.boards == 2 else [])
            scores = []  # per board: {i: (hi, lo)}
            for bd in boards:
                sc = {}
                for i in in_hand:
                    hole, b = self._cards_for(i, bd)
                    sc[i] = showdown_scores(v, hole, b)
                scores.append(sc)
            for i in in_hand:
                shown[i] = _cs(seats[i].hole) + _cs(seats[i].up)
                desc = " | ".join(describe(v, *sc[i]) for sc in scores)
                self._w(f"  {self._name(i)} shows {' '.join(shown[i])} ({desc})")
            contribs = [s.contributed for s in seats]
            levels = sorted(set(c for c in contribs if c > 0))
            prev = 0
            order = []
            j = self.button
            for _ in range(n):
                j = (j + 1) % n
                order.append(j)
            pots = []  # (amount, eligible, per-player slice)
            for lvl in levels:
                slice_c = [min(c, lvl) - min(c, prev) for c in contribs]
                amount = sum(slice_c)
                elig = [i for i in in_hand if contribs[i] >= lvl]
                prev = lvl
                if amount <= 0:
                    continue
                if not elig and pots:
                    # dead money above every live player's contribution (e.g. an auction payment
                    # by a player who later folded) goes to the highest contested pot
                    a, e, sl = pots[-1]
                    pots[-1] = (a + amount, e, [x + y for x, y in zip(sl, slice_c)])
                    continue
                pots.append((amount, elig, slice_c))
            for amount, elig, slice_c in pots:
                share_of = [0.0] * n  # fraction of this pot won, for bounties
                per_board = [amount // len(boards)] * len(boards)
                per_board[0] += amount - sum(per_board)
                for sc, amt_b in zip(scores, per_board):
                    parts = self._split_pot(sc, elig, amt_b, order)
                    for w, chips in parts.items():
                        winnings[w] += chips
                        share_of[w] += chips / amount
                if "bounty" in self.base.special:
                    bounty_extra.append((slice_c, share_of, elig))
            for i in range(n):
                if winnings[i]:
                    self._w(f"  {self._name(i)} wins {winnings[i]}")
        for i in range(n):
            seats[i].stack += winnings[i]
        if bounty_extra:
            self._apply_bounties(bounty_extra, in_hand)
        self._eliminate_and_notify(winnings, shown)

    def _split_pot(self, sc, elig, amount, order):
        """Distribute one pot (one board) among eligible players by the variant's split rule."""
        v = self.v
        out = {}

        def give(ws, amt):
            ws = [i for i in order if i in ws]
            share, rem = divmod(amt, len(ws))
            for k, w in enumerate(ws):
                out[w] = out.get(w, 0) + share + (1 if k < rem else 0)

        if v.split == "high":
            best = max(sc[i][0] for i in elig)
            give([i for i in elig if sc[i][0] == best], amount)
        elif v.split in ("low_a5", "low_27", "badugi"):
            best = min(sc[i][1] for i in elig)
            give([i for i in elig if sc[i][1] == best], amount)
        else:  # hilo8
            lows = [i for i in elig if sc[i][1] is not None]
            best_hi = max(sc[i][0] for i in elig)
            hi_w = [i for i in elig if sc[i][0] == best_hi]
            if lows:
                best_lo = min(sc[i][1] for i in lows)
                lo_w = [i for i in lows if sc[i][1] == best_lo]
                lo_amt = amount // 2
                give(hi_w, amount - lo_amt)  # odd chip to the high hand
                give(lo_w, lo_amt)
            else:
                give(hi_w, amount)
        return out

    def _apply_bounties(self, pots, in_hand):
        """MIT 2025 bounty rule generalized to N players: a pot winner whose bounty rank is in
        their hand or on the board collects an extra 50% of each loser's contribution to that pot
        plus a 5-big-blind bonus split across the losers (each loser capped by their stack)."""
        seats = self.seats
        bb = self._bb
        hit = {}
        for i in in_hand:
            ranks = {CARD_STRS[c][0] for c in list(seats[i].hole) + list(seats[i].up) + list(self.board)}
            hit[i] = seats[i].bounty in ranks if seats[i].bounty else False
        for slice_c, share_of, elig in pots:
            losers = [i for i in range(self.n) if slice_c[i] > 0 and share_of[i] == 0]
            if not losers:
                continue
            for w in elig:
                f = share_of[w]
                if f <= 0 or not hit.get(w):
                    continue
                for l in losers:
                    extra = int(f * (0.5 * slice_c[l] + 5 * bb / len(losers)))
                    extra = min(extra, seats[l].stack)
                    if extra > 0:
                        seats[l].stack -= extra
                        seats[w].stack += extra
                        self._w(f"  bounty! {self._name(w)} (bounty {seats[w].bounty}) collects {extra} from S{l}")

    def _eliminate_and_notify(self, winnings, shown):
        seats = self.seats
        n = self.n
        busted = [i for i in range(n) if not seats[i].eliminated and seats[i].stack == 0]
        if busted:
            busted.sort(key=lambda i: -seats[i].start_stack)
            groups = []
            for i in busted:
                if groups and seats[groups[-1][0]].start_stack == seats[i].start_stack:
                    groups[-1].append(i)
                else:
                    groups.append([i])
            cur = self.remaining_places - len(busted) + 1
            for g in groups:
                places = list(range(cur, cur + len(g)))
                for i in g:
                    seats[i].eliminated = True
                    self.result.places[i] = sum(places) / len(places)
                    self.result.points[i] = points_for_places(places, self.payouts)
                    self.result.bust_hand[i] = self.hand_number
                    self._w(f"  {self._name(i)} is eliminated (place {self.result.places[i]:g})")
                cur += len(g)
            self.remaining_places -= len(busted)
        total = sum(s.stack for s in seats)
        assert total == self.cfg.starting_stack * n, "chip conservation violated"
        deltas = [seats[i].stack - seats[i].start_stack for i in range(n)]
        stacks = [s.stack for s in seats]
        board = _cs(self.board)
        for i in range(n):
            s = seats[i]
            if s.in_hand and s.bot.agent is not None and hasattr(s.bot.agent, "hand_over"):
                res = HandResult(hand_number=self.hand_number, deltas=list(deltas),
                                 stacks=list(stacks), board=list(board), board2=_cs(self.board2),
                                 showdown={k: list(v) for k, v in shown.items()},
                                 winners={k: winnings[k] for k in range(n) if winnings[k]},
                                 eliminated=list(busted), my_seat=i, variant=self.v.id,
                                 bounty_rank=s.bounty)
                self._call(i, "hand_over", res)

    def _finish(self):
        seats = self.seats
        res = self.result
        res.hands_played = self.hand_number
        res.final_stacks = [s.stack for s in seats]
        alive = self._alive()
        if len(alive) == 1:
            i = alive[0]
            res.places[i] = 1
            res.points[i] = self.payouts[0]
        elif alive:
            stacks = [seats[i].stack for i in alive]
            ev = icm(stacks, self.payouts[:len(alive)])
            order = sorted(alive, key=lambda i: -seats[i].stack)
            for i, e in zip(alive, ev):
                res.points[i] = e
            k = 0
            while k < len(order):
                grp = [order[k]]
                while k + len(grp) < len(order) and seats[order[k + len(grp)]].stack == seats[order[k]].stack:
                    grp.append(order[k + len(grp)])
                places = list(range(k + 1, k + 1 + len(grp)))
                for i in grp:
                    res.places[i] = sum(places) / len(places)
                k += len(grp)
            self._w(f"--- Hand cap reached ({self.cfg.max_hands}); ICM points: " +
                    ", ".join(f"{self._name(i)}={res.points[i]:.2f}" for i in alive))
