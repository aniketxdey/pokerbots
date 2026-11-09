"""Engine side of the SDK protocol: runs an act(state, memory) bot inside the mock engine.

Follows the published host behaviour:
  * the team runs as a pool of identical copies; any copy may answer any decision, so `self`
    and globals can't be relied on between calls (here: 2 copies per table, picked at random);
  * memory starts as b"" at every table, is truncated at memory_bytes, and travels with each
    decision. After a timeout or exception the previous memory is kept and that turn's events
    are delivered again; after an illegal action the returned memory is stored;
  * state.events holds everything public since the bot's last turn at the table;
  * only offered kinds are legal and raise totals are not clamped: anything else is a failed
    decision and plays the fallback (check if free, otherwise fold; the automatic choice in a
    variant phase). Integers (NumPy's too) pass unchanged; floats are rounded down.
"""
import math
import random

from . import Action, Event, LegalAction, Seat, State, TableConfig

POOL_COPIES = 2


def is_sdk_module(mod):
    """True if the module's Agent implements act(state, memory)."""
    a = getattr(mod, "Agent", None)
    if a is None:
        return False
    try:
        from . import Agent as Base
        if isinstance(a, type) and issubclass(a, Base):
            return True
    except Exception:
        pass
    act = getattr(a, "act", None)
    code = getattr(act, "__code__", None)
    return code is not None and code.co_argcount == 3  # self, state, memory


class _Pkg:
    """What the engine hands to act(): the prepared State plus the engine observation."""
    __slots__ = ("state", "obs", "n_events")

    def __init__(self, state, obs, n_events):
        self.state = state
        self.obs = obs
        self.n_events = n_events


class SDKHost:
    def __init__(self, mod, cfg):
        self.copies = [mod.Agent() for _ in range(POOL_COPIES)]
        self.cfg = cfg
        self.memory = b""
        self.events = []
        self.cursor = 0
        self.config = None
        self.info = {}
        self.cur_hand = None
        self.hist_idx = 0
        self.board_len = 0
        self.street_bets = {}
        self.cur_street = None
        self.revealed = set()
        self.failed = 0
        self.decisions = 0

    # ------------------------------------------------------------------ engine callbacks
    def new_table(self, info):
        self.info = dict(info)
        cfg = self.cfg
        pays = list(info.get("points") or cfg.points)
        self.config = TableConfig(
            table_id=info.get("table_id"), seat=info.get("seat"), variant=info.get("variant", cfg.variant),
            players=info.get("num_players"), starting_stack=info.get("starting_stack", cfg.starting_stack),
            blinds=[tuple(b) for b in (info.get("blind_schedule") or cfg.blind_schedule)],
            hands_per_level=info.get("blind_interval", cfg.blind_interval),
            hand_cap=info.get("max_hands", cfg.max_hands), payouts=pays,
            decision_ms=info.get("time_limit_ms", cfg.time_limit_ms),
            time_bank_ms=info.get("time_bank_ms", getattr(cfg, "time_bank_ms", 0.0)),
            memory_bytes=info.get("memory_bytes", getattr(cfg, "memory_bytes", 65536)))

    def _prepare(self, method, arg):
        if method in ("act", "discard", "reveal"):
            self._absorb(arg)
            return _Pkg(self._state(method, arg), arg, len(self.events))
        if method == "hand_over":
            self._absorb_result(arg)
        return arg

    def act(self, pkg):
        return self._decide(pkg)

    def discard(self, pkg):
        return self._decide(pkg)

    def reveal(self, pkg):
        return self._decide(pkg)

    def hand_over(self, result):
        return None

    # ------------------------------------------------------------------ events
    def _new_hand(self, n):
        self.cur_hand = n
        self.hist_idx = 0
        self.board_len = 0
        self.street_bets = {}
        self.cur_street = None
        self.revealed = set()
        self.events.append(Event("hand", None, n, None))

    def _absorb_history(self, history, board, up_cards):
        for idx in range(self.hist_idx, len(history)):
            seat, kind, amt, street = history[idx]
            if street != self.cur_street:
                self.cur_street = street
                self.street_bets = {}
            if isinstance(street, int) and street > self.board_len and street <= len(board):
                self.events.append(Event("board", None, None, list(board[self.board_len:street])))
                self.board_len = street
            prev = self.street_bets.get(seat, 0)
            if kind in ("small_blind", "big_blind", "bring_in"):
                self.events.append(Event("blind", seat, amt, None))
                self.street_bets[seat] = amt
            elif kind == "ante":
                self.events.append(Event("ante", seat, amt, None))
            elif kind == "fold":
                self.events.append(Event("fold", seat, None, None))
            elif kind == "check":
                self.events.append(Event("check", seat, None, None))
            elif kind == "call":
                self.events.append(Event("call", seat, amt - prev, None))
                self.street_bets[seat] = amt
            elif kind == "raise":
                self.events.append(Event("raise", seat, amt, None))
                self.street_bets[seat] = amt
            elif kind == "reveal":
                if seat not in self.revealed:
                    self.revealed.add(seat)
                    self.events.append(Event("reveal", seat, amt, list(up_cards.get(seat, []))))
            else:
                self.events.append(Event(kind, seat, amt, None))
        self.hist_idx = len(history)
        if len(board) > self.board_len:
            self.events.append(Event("board", None, None, list(board[self.board_len:])))
            self.board_len = len(board)

    def _absorb(self, obs):
        if obs.hand_number != self.cur_hand:
            self._new_hand(obs.hand_number)
        ups = {p.seat: p.up_cards for p in obs.players if p.up_cards}
        self._absorb_history(obs.history, obs.board, ups)

    def _absorb_result(self, r):
        if r.hand_number != self.cur_hand:
            self._new_hand(r.hand_number)
        self._absorb_history(r.history or [], r.board or [], r.up_cards or {})
        for seat, cards in sorted((r.showdown or {}).items()):
            self.events.append(Event("show", seat, None, list(cards)))
        for seat, chips in sorted((r.winners or {}).items()):
            self.events.append(Event("win", seat, chips, None))
        for seat, st in enumerate(r.stacks or []):
            self.events.append(Event("stack", seat, st, None))

    # ------------------------------------------------------------------ state
    def _legal(self, method, obs):
        if method == "discard":
            k = obs.discard_count or 1
            return [LegalAction("discard", k, k)], "discard"
        if method == "reveal":
            k = obs.reveal_count or 1
            return [LegalAction("reveal", k, k)], "reveal"
        out = []
        legal = obs.legal_actions
        if "check" in legal and obs.to_call <= 0:
            out.append(LegalAction("check"))
        else:
            if "fold" in legal:
                out.append(LegalAction("fold"))
            if "call" in legal and obs.to_call > 0:
                out.append(LegalAction("call", obs.to_call, obs.to_call))
        if "raise" in legal and obs.min_raise_to is not None:
            out.append(LegalAction("raise", obs.min_raise_to, obs.max_raise_to))
        return out, "betting"

    def _state(self, method, obs):
        legal, phase = self._legal(method, obs)
        seats = []
        for p in obs.players:
            if p.eliminated:
                st = "busted"
            elif p.folded:
                st = "folded"
            elif p.all_in:
                st = "all_in"
            else:
                st = "active"
            seats.append(Seat(p.seat, p.stack, p.bet, st, list(p.up_cards)))
        rules = getattr(obs, "table_rules", {}) or {}
        sched = self.config.blinds if self.config else []
        lvl = rules.get("blind_level", 1)
        blinds = tuple(sched[min(lvl - 1, len(sched) - 1)]) if sched else (obs.small_blind, obs.big_blind)
        history = [(h[0], {"small_blind": "blind", "big_blind": "blind"}.get(h[1], h[1]), h[2], h[3])
                   for h in obs.history]
        return State(
            seat=obs.seat, cards=list(obs.hole_cards), board=list(obs.board), pot=obs.pot, to_call=obs.to_call,
            legal_actions=legal, history=history, phase=phase,
            variant={"up_cards": {p.seat: list(p.up_cards) for p in obs.players if p.up_cards},
                     "down_cards": list(obs.down_cards), "id": obs.variant},
            seats=seats, button=obs.button,
            blind_level=lvl, blinds=blinds, hands_until_next_level=rules.get("hands_until_next_level"),
            hand_number=obs.hand_number, players_remaining=rules.get("players_remaining", obs.num_active),
            bust_order=list(rules.get("bust_order", [])),
            events=self.events[self.cursor:], config=self.config,
            time_bank_ms=getattr(obs, "time_bank_left_ms", None),
        )

    # ------------------------------------------------------------------ decide
    def _decide(self, pkg):
        st = pkg.state
        copy = random.choice(self.copies)
        out = copy.act(st, self.memory)   # exceptions / timeouts propagate: memory + cursor unchanged
        self.decisions += 1
        action, mem = out if isinstance(out, tuple) and len(out) == 2 else (out, self.memory)
        if isinstance(mem, (bytes, bytearray, memoryview)):
            lim = self.config.memory_bytes if self.config else 65536
            self.memory = bytes(mem)[:lim]
        self.cursor = pkg.n_events
        res = self._validate(action, st, pkg.obs)
        if res is None:
            self.failed += 1
        return res

    @staticmethod
    def _int(x):
        try:
            import numbers
            if isinstance(x, bool):
                return None
            if isinstance(x, numbers.Integral):
                return int(x)
            if isinstance(x, numbers.Real):
                return int(math.floor(float(x)))
        except Exception:
            return None
        return None

    def _validate(self, action, st, obs):
        """Return the engine action, or None for a failed decision (engine plays the fallback)."""
        if not isinstance(action, Action):
            return None
        la = st.can(action.kind)
        if la is None:
            return None
        if st.phase in ("discard", "reveal"):
            cards = action.cards or []
            mine = list(obs.down_cards)
            if len(cards) != la.min_amount or len(set(cards)) != len(cards) or any(c not in mine for c in cards):
                return None
            return list(cards)
        if action.kind == "raise":
            amt = self._int(action.amount)
            if amt is None or amt < la.min_amount or amt > la.max_amount:
                return None
            return ("raise", amt)
        return action.kind
