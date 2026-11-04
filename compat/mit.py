"""MIT Pokerbots skeleton emulation.

Lets a `Player(Bot)` written for the MIT Pokerbots engines (2020-2026) play an 8-handed
sit-and-go here without touching its strategy code:

    from compat.mit import *          # FoldAction, CallAction, CheckAction, RaiseAction, Bot, ...
    class Player(Bot): ...            # original code
    Agent = mit_agent(Player)

Translation rules (MIT bots are heads-up bots, so "the opponent" has to be defined):
  * pips[active]    = my bet on this street;  pips[1-active] = the highest bet on this street
                      (so continue_cost = opp_pip - my_pip is exactly the amount to call)
  * stacks[active]  = my stack;  stacks[1-active] = biggest stack among opponents still in the hand
  * round_state.my_contribution / opp_contribution = my chips in the pot / everyone else's
    (MIT bots computed these as STARTING_STACK - stack, which only works with per-hand stack
    resets; the ported files read these two attributes instead)
  * hands[1-active] is [] during the hand; at hand end it holds the cards of an opponent who
    showed down (the winner if it wasn't me), else []
  * Units: MIT engines always used blinds 1/2. All chip amounts shown to the bot are rescaled
    so the current big blind = 2 (amounts are multiplied by 2 / big_blind), and raise amounts
    are scaled back. This keeps hard-coded thresholds like `continue_cost > 6` (3 big blinds)
    meaning what their authors intended as blinds go up.
  * Variant-only actions (BidAction, DiscardAction, AssignAction) exist so the code imports,
    but are never legal in No-Limit Hold'em.
  * NUM_ROUNDS = the table's hand cap (200); round_num = hand number at this table.
"""
import random  # noqa: F401  (re-exported for ported code that did `import random`)
from collections import namedtuple

FoldAction = namedtuple("FoldAction", [])
CallAction = namedtuple("CallAction", [])
CheckAction = namedtuple("CheckAction", [])
RaiseAction = namedtuple("RaiseAction", ["amount"])
BidAction = namedtuple("BidAction", ["amount"])
DiscardAction = namedtuple("DiscardAction", ["card"])
AssignAction = namedtuple("AssignAction", ["cards"])

# MIT constants (blinds 1/2). NUM_ROUNDS / STARTING_STACK are refreshed per table.
NUM_ROUNDS = 200
STARTING_STACK = 200
BIG_BLIND = 2
SMALL_BLIND = 1
NUM_BOARDS = 1

MIT_BB = 2.0


class Bot:
    """MIT base class (all methods overridden by the ported Player)."""

    def handle_new_round(self, game_state, round_state, active):
        pass

    def handle_round_over(self, game_state, terminal_state, active):
        pass

    def get_action(self, game_state, round_state, active):
        return CheckAction()


class GameState:
    __slots__ = ("bankroll", "opp_bankroll", "game_clock", "round_num")

    def __init__(self, bankroll, game_clock, round_num):
        self.bankroll = bankroll
        self.opp_bankroll = -bankroll
        self.game_clock = game_clock
        self.round_num = round_num


class RoundState:
    def __init__(self, obs, scale, opp_cards=None):
        self._obs = obs
        self._s = scale
        me = obs.players[obs.seat]
        opps = [p for p in obs.players if p.seat != obs.seat and p.in_hand]
        opp_stack = max((p.stack for p in opps), default=0)
        self.button = 0 if obs.button == obs.seat else 1
        self.street = obs.street
        self.pips = [obs.my_bet * scale, obs.current_bet * scale]
        self.stacks = [obs.my_stack * scale, opp_stack * scale]
        self.hands = [list(obs.hole_cards), list(opp_cards or [])]
        self.deck = list(obs.board)
        self.board = list(obs.board)
        self.my_contribution = obs.my_contributed * scale
        self.opp_contribution = (obs.pot - obs.my_contributed) * scale
        self.pot = obs.pot * scale
        self.bids = [getattr(obs, "my_bid", None), None]
        self.bounties = [obs.bounty_rank or "", ""]  # 2025 Bounty Hold'em: your bounty rank
        self.previous_state = None

    def legal_actions(self):
        la = self._obs.legal_actions or []
        out = set()
        if "fold" in la:
            out.add(FoldAction)
        if "call" in la:
            out.add(CallAction)
        if "check" in la:
            out.add(CheckAction)
        if "raise" in la:
            out.add(RaiseAction)
        if "discard" in la:   # 2026 Toss Hold'em
            out.add(DiscardAction)
        if "bid" in la:       # 2024 Auction Hold'em
            out.add(BidAction)
        return out

    def bid_bounds(self):
        return (0, self._obs.my_stack * self._s)

    def raise_bounds(self):
        o = self._obs
        s = self._s
        if o.min_raise_to is not None:
            return (o.min_raise_to * s, o.max_raise_to * s)
        hi = (o.my_bet + o.my_stack) * s
        return (min(hi, (o.current_bet + o.big_blind) * s), hi)


class TerminalState:
    def __init__(self, deltas, previous_state):
        self.deltas = deltas
        self.previous_state = previous_state
        self.bounty_hits = [False, False]


def _translate(action, scale):
    if action is None:
        return None
    if isinstance(action, FoldAction) or action is FoldAction:
        return "fold"
    if isinstance(action, CallAction) or action is CallAction:
        return "call"
    if isinstance(action, CheckAction) or action is CheckAction:
        return "check"
    if isinstance(action, RaiseAction):
        try:
            return ("raise", int(round(float(action.amount) / scale)))
        except Exception:
            return None
    return None


def mit_agent(player_cls):
    """Wrap an MIT `Player` class into this engine's Agent interface."""

    class Agent:
        def __init__(self):
            global NUM_ROUNDS
            self.player = player_cls()
            self.start_stack = 2000
            self.last_obs = None
            self.scale = 1.0
            self.clock = 30.0

        def new_table(self, info):
            self.start_stack = info.get("starting_stack", 2000)
            # module-level constants the ported code reads
            g = getattr(player_cls, "__module_globals__", None)
            mod_globals = g if g is not None else _module_globals(player_cls)
            if mod_globals is not None:
                mod_globals["NUM_ROUNDS"] = info.get("max_hands", 200)

        def _gs(self, obs):
            bankroll = (obs.my_stack + obs.my_contributed - self.start_stack) * self.scale
            return GameState(bankroll, self.clock, obs.hand_number)

        def new_hand(self, obs):
            self.scale = MIT_BB / obs.big_blind if obs.big_blind else 1.0
            mod_globals = _module_globals(player_cls)
            if mod_globals is not None and "STARTING_STACK" in mod_globals:
                # STARTING_STACK in MIT units at the current blind level
                mod_globals["STARTING_STACK"] = self.start_stack * self.scale
            self.last_obs = obs
            rs = RoundState(obs, self.scale)
            self.player.handle_new_round(self._gs(obs), rs, 0)

        def act(self, obs):
            self.last_obs = obs
            rs = RoundState(obs, self.scale)
            a = self.player.get_action(self._gs(obs), rs, 0)
            return _translate(a, self.scale)

        def discard(self, obs):
            """2026 Toss Hold'em: the MIT bot answers with DiscardAction(card_index)."""
            self.last_obs = obs
            a = self.player.get_action(self._gs(obs), RoundState(obs, self.scale), 0)
            if isinstance(a, DiscardAction):
                return [obs.down_cards[int(a.card)]]
            return None

        def bid(self, obs):
            """2024 Auction Hold'em: the MIT bot answers with BidAction(amount)."""
            self.last_obs = obs
            a = self.player.get_action(self._gs(obs), RoundState(obs, self.scale), 0)
            if isinstance(a, BidAction):
                return int(round(float(a.amount) / self.scale))
            return 0

        def hand_over(self, result):
            obs = self.last_obs
            if obs is None:
                return
            me = result.my_seat
            opp_cards = []
            shown = {k: v for k, v in result.showdown.items() if k != me}
            if shown:
                winners = [k for k in result.winners if k in shown]
                pick = winners[0] if winners else sorted(shown)[0]
                opp_cards = shown[pick]
            prev = RoundState(obs, self.scale, opp_cards)
            prev.street = len(result.board)
            prev.deck = list(result.board)
            prev.board = list(result.board)
            d = result.deltas[me] * self.scale
            ts = TerminalState([d, -d], prev)
            b = getattr(result, "bounty_rank", None)
            if b:
                mine = {c[0] for c in list(obs.hole_cards) + list(result.board)}
                ts.bounty_hits = [b in mine, False]
                prev.bounties = [b, ""]
            self.player.handle_round_over(self._gs(obs), ts, 0)

    Agent.__name__ = "Agent"
    return Agent


# ---------------------------------------------------------------- 2021 ("Blotto Hold'em") API
# The 2021 engine played three boards at once (6 hole cards split 2/2/2). Bots return a list
# of actions, one per board, and read per-board BoardState objects. Here the game is regular
# Hold'em, so the bot sees exactly one board (NUM_BOARDS = 1) holding its two hole cards.

class BoardState:
    def __init__(self, obs, scale):
        self._obs = obs
        self._s = scale
        self.pips = [obs.my_bet * scale, obs.current_bet * scale]
        # BoardState.pot = chips from previous streets (pips are added separately by the bots)
        street_bets = sum(p.bet for p in obs.players)
        self.pot = (obs.pot - street_bets) * scale
        self.hands = [list(obs.hole_cards), []]
        self.deck = list(obs.board)
        self.button = 0 if obs.button == obs.seat else 1
        self.previous_state = None
        self.settled = False
        self.reveal = False

    def legal_actions(self, button=None, stacks=None):
        return RoundState.legal_actions(self)

    def raise_bounds(self, active=0, stacks=None):
        return RoundState.raise_bounds(self)


class RoundState2021(RoundState):
    def __init__(self, obs, scale):
        super().__init__(obs, scale)
        self.board_states = [BoardState(obs, scale)]

    def legal_actions(self):
        return [RoundState.legal_actions(self)]


class TerminalBoardState:
    def __init__(self, previous_state):
        self.previous_state = previous_state
        self.deltas = None


def mit2021_agent(player_cls):
    """Wrap a 2021 MIT `Player` (get_actions -> list of per-board actions)."""
    base = mit_agent(player_cls)

    class Agent(base):
        def new_hand(self, obs):
            self.scale = MIT_BB / obs.big_blind if obs.big_blind else 1.0
            self.last_obs = obs
            self.player.handle_new_round(self._gs(obs), RoundState2021(obs, self.scale), 0)

        def act(self, obs):
            self.last_obs = obs
            actions = self.player.get_actions(self._gs(obs), RoundState2021(obs, self.scale), 0)
            a = actions[0] if actions else None
            return _translate(a, self.scale)

        def hand_over(self, result):
            obs = self.last_obs
            if obs is None:
                return
            me = result.my_seat
            shown = {k: v for k, v in result.showdown.items() if k != me}
            opp_cards = []
            if shown:
                winners = [k for k in result.winners if k in shown]
                opp_cards = shown[winners[0] if winners else sorted(shown)[0]]
            prev = RoundState2021(obs, self.scale)
            bs = prev.board_states[0]
            bs.hands = [list(obs.hole_cards), list(opp_cards)]
            bs.deck = list(result.board)
            prev.board_states = [TerminalBoardState(bs)]
            prev.street = len(result.board)
            d = result.deltas[me] * self.scale
            self.player.handle_round_over(self._gs(obs), TerminalState([d, -d], prev), 0)

    Agent.__name__ = "Agent"
    return Agent


def _module_globals(cls):
    import sys
    mod = sys.modules.get(cls.__module__)
    if mod is not None and getattr(mod, "Player", None) is cls:
        return mod.__dict__
    # bots are exec'd into fresh modules that are not in sys.modules; reach them via a method
    for v in cls.__dict__.values():
        f = getattr(v, "__globals__", None)
        if f is not None:
            return f
    return None
