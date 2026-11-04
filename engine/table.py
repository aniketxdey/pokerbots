"""One sit-and-go table: No-Limit Texas Hold'em, played until one bot holds every chip or
the hand cap is reached (official rules §4.4).

Rules implemented:
  * stacks start at cfg.starting_stack (2000); blinds follow cfg.blind_schedule and go up
    every cfg.blind_interval hands (25); optional antes
  * dealer button moves one live seat clockwise each hand; heads-up the button posts the
    small blind and acts first preflop / last postflop
  * standard no-limit betting: min raise = size of the previous full raise (at least one
    big blind); an all-in for less than a full raise does not reopen the betting for
    players who already acted
  * side pots, split pots (odd chips go to the first winner left of the button),
    uncalled bets returned
  * busted players are placed by elimination order; two players busting on the same hand
    are ordered by stack at the start of that hand (equal stacks split the places)
  * at the hand cap, the remaining places are paid to the survivors by ICM (§4.5)
"""
from .cards import CARD_STRS
from .evaluator import evaluate, category_name
from .api import Observation, PlayerView, HandResult, STREET_NAMES, FOLD, CHECK, CALL, RAISE
from .sandbox import timed_call, TIMEOUT, ERROR
from .scoring import icm, points_for_places


class Seat:
    __slots__ = ("bot", "stack", "eliminated", "hole", "bet", "contributed", "folded",
                 "all_in", "in_hand", "start_stack", "reopened")

    def __init__(self, bot, stack):
        self.bot = bot
        self.stack = stack
        self.eliminated = False
        self.hole = None
        self.bet = 0
        self.contributed = 0
        self.folded = False
        self.all_in = False
        self.in_hand = False
        self.start_stack = stack
        self.reopened = True


class TableResult:
    __slots__ = ("places", "points", "hands_played", "final_stacks", "bust_hand", "hit_cap")

    def __init__(self, n):
        self.places = [None] * n      # float place (ties averaged)
        self.points = [0.0] * n
        self.hands_played = 0
        self.final_stacks = [0] * n
        self.bust_hand = [None] * n   # hand number of elimination (None = survived)
        self.hit_cap = False


class Table:
    def __init__(self, bots, cfg, rng, log=None):
        """bots: list of BotSlot (engine.tournament) - one per seat."""
        self.cfg = cfg
        self.rng = rng
        self.n = len(bots)
        self.seats = [Seat(b, cfg.starting_stack) for b in bots]
        self.log = log  # list to append text lines to, or None
        self.button = None
        self.hand_number = 0
        self.history = []
        self.board = []
        self.limit_s = cfg.time_limit_ms / 1000.0
        self.cb_limit_s = cfg.callback_time_limit_ms / 1000.0
        self.mode = cfg.time_mode
        self.payouts = list(cfg.points) + [0] * max(0, self.n - len(cfg.points))
        self.result = TableResult(self.n)
        self.remaining_places = self.n  # next place to award when someone busts

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
        level = min(level, len(sched) - 1)
        lvl = sched[level]
        sb, bb = lvl[0], lvl[1]
        ante = lvl[2] if len(lvl) > 2 else 0
        until_up = self.cfg.blind_interval - ((self.hand_number - 1) % self.cfg.blind_interval)
        return sb, bb, ante, until_up

    def _pot(self):
        return sum(s.contributed for s in self.seats)

    def _put(self, i, amount):
        s = self.seats[i]
        amount = min(amount, s.stack)
        s.stack -= amount
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

    # ------------------------------------------------------------------ observation
    def _obs(self, i, can_raise_info=None):
        s = self.seats[i]
        current_bet = max(x.bet for x in self.seats)
        to_call = min(max(0, current_bet - s.bet), s.stack)
        if can_raise_info is None:
            legal, mn, mx = self._legal(i, current_bet)
        else:
            legal, mn, mx = can_raise_info
        players = [PlayerView(j, x.stack, x.bet, x.contributed, x.folded, x.all_in,
                              x.in_hand and not x.folded, x.eliminated)
                   for j, x in enumerate(self.seats)]
        alive = sum(1 for x in self.seats if not x.eliminated)
        return Observation(
            seat=i, hand_number=self.hand_number, street=len(self.board),
            street_name=STREET_NAMES[len(self.board)],
            hole_cards=[CARD_STRS[c] for c in s.hole] if s.hole else [],
            board=[CARD_STRS[c] for c in self.board], pot=self._pot(), to_call=to_call,
            my_bet=s.bet, my_stack=s.stack, my_contributed=s.contributed,
            current_bet=current_bet, min_raise_to=mn, max_raise_to=mx, legal_actions=legal,
            small_blind=self._sb, big_blind=self._bb, ante=self._ante, button=self.button,
            small_blind_seat=self._sb_seat, big_blind_seat=self._bb_seat,
            num_players=self.n, num_active=alive, players=players,
            history=list(self.history), starting_stack=self.cfg.starting_stack,
            hands_until_blinds_up=self._until_up, max_hands=self.cfg.max_hands,
            time_limit_ms=self.cfg.time_limit_ms,
        )

    def _legal(self, i, current_bet):
        s = self.seats[i]
        to_call = current_bet - s.bet
        legal = []
        if to_call > 0:
            legal += [FOLD, CALL]
        else:
            legal.append(CHECK)
        others_can_act = any(
            (not x.folded) and x.in_hand and (not x.all_in) and j != i
            for j, x in enumerate(self.seats))
        mn = mx = None
        if s.reopened and s.stack > to_call and others_can_act:
            mx = s.bet + s.stack
            mn = min(current_bet + self._min_inc, mx)
            legal.append(RAISE)
        return legal, mn, mx

    # ------------------------------------------------------------------ hand flow
    def play(self):
        cfg = self.cfg
        alive = self._alive()
        # random starting button (seats are already randomly assigned)
        self.button = self.rng.choice(alive)
        first = True
        while True:
            alive = self._alive()
            if len(alive) <= 1:
                break
            if self.hand_number >= cfg.max_hands:
                self.result.hit_cap = True
                break
            self.hand_number += 1
            if not first:
                self.button = self._next_live(self.button)
            first = False
            self._play_hand()
        self._finish()
        return self.result

    def _play_hand(self):
        cfg = self.cfg
        seats = self.seats
        self._sb, self._bb, self._ante, self._until_up = self._blinds()
        sb, bb, ante = self._sb, self._bb, self._ante
        alive = self._alive()
        for s in seats:
            s.hole = None
            s.bet = 0
            s.contributed = 0
            s.folded = False
            s.all_in = False
            s.in_hand = not s.eliminated
            s.start_stack = s.stack
            s.reopened = True
        self.history = []
        self.board = []

        if len(alive) == 2:
            sb_seat = self.button
            bb_seat = self._next_live(self.button)
        else:
            sb_seat = self._next_live(self.button)
            bb_seat = self._next_live(sb_seat)
        self._sb_seat, self._bb_seat = sb_seat, bb_seat

        deck = list(range(52))
        self.rng.shuffle(deck)
        pos = 0
        # deal starting left of the button (cosmetic, deck is random anyway)
        order = []
        j = self.button
        for _ in range(len(alive)):
            j = self._next_live(j)
            order.append(j)
        for i in order:
            seats[i].hole = (deck[pos], deck[pos + 1])
            pos += 2

        self._w(f"--- Hand {self.hand_number}  blinds {sb}/{bb}" + (f" ante {ante}" if ante else "")
                + f"  button S{self.button}")
        if self.log is not None:
            for i in order:
                h = seats[i].hole
                self._w(f"  {self._name(i)} stack={seats[i].stack} [{CARD_STRS[h[0]]} {CARD_STRS[h[1]]}]")

        # antes (dead money, not part of the betting)
        if ante:
            for i in alive:
                a = self._put(i, ante)
                seats[i].bet -= a  # antes don't count toward the preflop bet
                self.history.append((i, "ante", a, 0))
        a = self._put(sb_seat, sb)
        self.history.append((sb_seat, "small_blind", seats[sb_seat].bet, 0))
        a = self._put(bb_seat, bb)
        self.history.append((bb_seat, "big_blind", seats[bb_seat].bet, 0))
        self._min_inc = bb

        # new_hand callbacks
        for i in order:
            if seats[i].bot.agent is not None and hasattr(seats[i].bot.agent, "new_hand"):
                self._call(i, "new_hand", self._obs(i))

        # preflop: first to act is left of the big blind (heads-up: the button/SB)
        first_actor = self._next_live(bb_seat)
        done = self._betting_round(first_actor)
        streets = [(3, "flop"), (1, "turn"), (1, "river")]
        for ncards, name in streets:
            if done:
                break
            self.board.extend(deck[pos:pos + ncards])
            pos += ncards
            for s in seats:
                s.bet = 0
                s.reopened = True
            self._min_inc = bb
            self._w(f"  {name}: {' '.join(CARD_STRS[c] for c in self.board)}  pot={self._pot()}")
            first = self._next_live(self.button)
            done = self._betting_round(first)
        # if betting ended because everyone is all-in, run out the board
        in_hand = [i for i, s in enumerate(seats) if s.in_hand and not s.folded]
        if len(in_hand) > 1 and len(self.board) < 5:
            need = 5 - len(self.board)
            self.board.extend(deck[pos:pos + need])
            pos += need
            self._w(f"  run out: {' '.join(CARD_STRS[c] for c in self.board)}")
        self._settle(in_hand)

    def _betting_round(self, first):
        """Returns True if the hand is over (one player left) or no more betting is possible."""
        seats = self.seats
        n = self.n

        def contenders():
            return [i for i, s in enumerate(seats) if s.in_hand and not s.folded]

        def can_act(i):
            s = seats[i]
            return s.in_hand and not s.folded and not s.all_in

        if len(contenders()) <= 1:
            return True
        needs = {i for i in range(n) if can_act(i)}
        current_bet = max(s.bet for s in seats)
        # if only one player can act and they already match the bet, nothing to do
        if len(needs) <= 1:
            only = next(iter(needs), None)
            if only is None or seats[only].bet >= current_bet:
                return len(contenders()) <= 1 or True
        p = first
        guard = 0
        while needs:
            guard += 1
            if guard > 1000:
                break
            # next seat (from p, inclusive) that needs to act
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
            if act == FOLD:
                s.folded = True
                self.history.append((i, FOLD, s.bet, len(self.board)))
                self._w(f"    {self._name(i)} folds")
            elif act == CHECK:
                self.history.append((i, CHECK, s.bet, len(self.board)))
                self._w(f"    {self._name(i)} checks")
            elif act == CALL:
                self._put(i, to_call)
                self.history.append((i, CALL, s.bet, len(self.board)))
                self._w(f"    {self._name(i)} calls {s.bet}" + (" (all-in)" if s.all_in else ""))
            else:  # RAISE to amt
                inc = amt - current_bet
                self._put(i, amt - s.bet)
                full = inc >= self._min_inc
                if full:
                    self._min_inc = inc
                for j in range(n):
                    if j != i and can_act(j):
                        needs.add(j)
                        if full:
                            seats[j].reopened = True
                self.history.append((i, RAISE, s.bet, len(self.board)))
                self._w(f"    {self._name(i)} raises to {s.bet}" + (" (all-in)" if s.all_in else ""))
            s.reopened = False
            if len(contenders()) <= 1:
                return True
            p = (i + 1) % n
        # betting complete; the hand continues if 2+ contenders.
        # If at most one contender can still act, no further betting will happen.
        active = [i for i in range(n) if can_act(i)]
        return len(contenders()) <= 1 or len(active) <= 1

    def _normalize(self, i, raw, legal, mn, mx, current_bet):
        """Turn whatever the bot returned into a legal (action, amount)."""
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
        if act in ("bet",):
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
                amt = int(round(float(amt)))
            except Exception:
                stats.invalid += 1
                amt = mn
            if amt < mn:
                stats.invalid += 1
                amt = mn
            elif amt > mx:
                amt = mx
            return RAISE, amt
        stats.invalid += 1
        return default

    def _settle(self, in_hand):
        seats = self.seats
        n = self.n
        winnings = [0] * n
        shown = {}
        if len(in_hand) == 1:
            w = in_hand[0]
            winnings[w] = self._pot()
            self._w(f"  {self._name(w)} wins {winnings[w]} uncontested")
        else:
            values = {i: evaluate(list(seats[i].hole) + self.board) for i in in_hand}
            for i in in_hand:
                shown[i] = [CARD_STRS[c] for c in seats[i].hole]
                self._w(f"  {self._name(i)} shows {' '.join(shown[i])} ({category_name(values[i])})")
            contribs = [s.contributed for s in seats]
            levels = sorted(set(c for c in contribs if c > 0))
            prev = 0
            # order for odd chips: starting left of the button
            order = []
            j = self.button
            for _ in range(n):
                j = (j + 1) % n
                order.append(j)
            for lvl in levels:
                amount = sum(min(c, lvl) - min(c, prev) for c in contribs)
                elig = [i for i in in_hand if contribs[i] >= lvl]
                prev = lvl
                if amount <= 0 or not elig:
                    continue
                best = max(values[i] for i in elig)
                ws = [i for i in order if i in elig and values[i] == best]
                share, rem = divmod(amount, len(ws))
                for k, w in enumerate(ws):
                    winnings[w] += share + (1 if k < rem else 0)
            for i in range(n):
                if winnings[i]:
                    self._w(f"  {self._name(i)} wins {winnings[i]}")
        for i in range(n):
            seats[i].stack += winnings[i]
        # eliminations
        busted = [i for i in range(n) if not seats[i].eliminated and seats[i].stack == 0]
        if busted:
            busted.sort(key=lambda i: -seats[i].start_stack)
            # group ties by starting stack
            place = self.remaining_places
            groups = []
            for i in busted:
                if groups and seats[groups[-1][0]].start_stack == seats[i].start_stack:
                    groups[-1].append(i)
                else:
                    groups.append([i])
            # the best (largest start stack) busted player gets the best of the freed places
            best_place = self.remaining_places - len(busted) + 1
            cur = best_place
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
        # hand_over callbacks
        deltas = [seats[i].stack - seats[i].start_stack for i in range(n)]
        stacks = [s.stack for s in seats]
        board = [CARD_STRS[c] for c in self.board]
        elim = busted
        for i in range(n):
            s = seats[i]
            if s.in_hand and s.bot.agent is not None and hasattr(s.bot.agent, "hand_over"):
                res = HandResult(hand_number=self.hand_number, deltas=list(deltas),
                                 stacks=list(stacks), board=list(board),
                                 showdown={k: list(v) for k, v in shown.items()},
                                 winners={k: winnings[k] for k in range(n) if winnings[k]},
                                 eliminated=list(elim), my_seat=i)
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
            # hand cap: distribute the remaining places by ICM on chip stacks (§4.5)
            stacks = [seats[i].stack for i in alive]
            ev = icm(stacks, self.payouts[:len(alive)])
            # expected place for reporting: rank by stack (ties averaged)
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
        else:
            pass
