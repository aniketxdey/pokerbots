"""Adapters for neuron_poker, Fullhouse, RLCard and texasholdem bots."""
import random as _random
from enum import Enum

from engine.cards import str_to_card
from engine.evaluator import evaluate


# ---------------------------------------------------------------- neuron_poker
class NeuronAction(Enum):
    """gym_env.enums.Action from github.com/dickreuter/neuron_poker"""
    FOLD = 0
    CHECK = 1
    CALL = 2
    RAISE_3BB = 3
    RAISE_HALF_POT = 4
    RAISE_POT = 5
    RAISE_2POT = 6
    ALL_IN = 7
    SMALL_BLIND = 8
    BIG_BLIND = 9


def montecarlo_equity(hole, board, n_players, runs):
    """Equity to the river vs (n_players-1) random hands, like neuron_poker's get_equity:
    fraction of runs in which our hand is at least as good as every opponent's."""
    hole = [str_to_card(c) for c in hole]
    board = [str_to_card(c) for c in board]
    used = set(hole) | set(board)
    rest = [c for c in range(52) if c not in used]
    need = 5 - len(board)
    nopp = max(1, n_players - 1)
    wins = 0
    for _ in range(runs):
        draw = _random.sample(rest, need + 2 * nopp)
        b = board + draw[:need]
        mine = evaluate(hole + b)
        k = need
        ok = True
        for _o in range(nopp):
            if evaluate([draw[k], draw[k + 1]] + b) > mine:
                ok = False
                break
            k += 2
        if ok:
            wins += 1
    return wins / runs


def neuron_agent(player_factory, equity_runs=1000, max_raises_per_player_round=2):
    """neuron_poker's env computes equity_to_river_alive with 1000 Monte Carlo runs before
    every decision and passes it in `info`; here the agent computes it itself (so the cost
    counts against the bot's time limit, as it would in a real submission)."""

    class Agent:
        def __init__(self):
            self.player = player_factory()
            self.raises = {}

        def new_hand(self, obs):
            self.raises = {}

        def _legal(self, obs):
            A = NeuronAction
            moves = []
            if obs.to_call == 0:
                moves.append(A.CHECK)
            else:
                moves.append(A.CALL)
                moves.append(A.FOLD)
            if "raise" in obs.legal_actions and self.raises.get(obs.street, 0) < max_raises_per_player_round:
                pot = obs.pot
                stack = obs.my_stack
                min_call = obs.to_call
                if stack >= 3 * obs.big_blind - obs.my_bet:
                    moves.append(A.RAISE_3BB)
                if stack >= pot / 2 >= min_call:
                    moves.append(A.RAISE_HALF_POT)
                if stack >= pot >= min_call:
                    moves.append(A.RAISE_POT)
                if stack >= pot * 2 >= min_call:
                    moves.append(A.RAISE_2POT)
                if stack > 0:
                    moves.append(A.ALL_IN)
            return moves

        def act(self, obs):
            A = NeuronAction
            alive = sum(1 for p in obs.players if p.in_hand)
            eq = montecarlo_equity(obs.hole_cards, obs.board, alive, equity_runs)
            info = {"player_data": {"equity_to_river_alive": eq, "position": obs.seat,
                                    "stack": obs.my_stack}}
            action_space = self._legal(obs)
            a = self.player.action(action_space, None, info)
            pot = obs.pot
            if a == A.FOLD:
                return "fold"
            if a == A.CHECK:
                return "check"
            if a == A.CALL:
                return "call"
            if a == A.ALL_IN:
                return "all_in"
            contribution = {A.RAISE_HALF_POT: pot / 2, A.RAISE_POT: pot, A.RAISE_2POT: pot * 2}.get(a)
            if a == A.RAISE_3BB:
                target = 3 * obs.big_blind
            elif contribution is not None:
                target = obs.my_bet + contribution
            else:
                return None
            self.raises[obs.street] = self.raises.get(obs.street, 0) + 1
            return ("raise", int(target))

    Agent.__name__ = "Agent"
    return Agent


# ---------------------------------------------------------------- Fullhouse
_FH_STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}


def fullhouse_state(obs):
    """The game_state dict built by github.com/uzlez/fullhouse-engine (engine/game.py)."""
    return {
        "type": "action_request",
        "hand_id": obs.hand_number,
        "street": (obs.street_name if obs.street_name in ("preflop", "flop", "turn", "river")
                   else _FH_STREETS.get(obs.street, "river")),
        "seat_to_act": obs.seat,
        "pot": obs.pot,
        "community_cards": list(obs.board),
        "current_bet": obs.current_bet,
        "min_raise_to": obs.min_raise_to if obs.min_raise_to is not None else obs.current_bet + obs.big_blind,
        "amount_owed": obs.to_call,
        "can_check": obs.to_call == 0,
        "your_cards": list(obs.hole_cards),
        "your_stack": obs.my_stack,
        "your_bet_this_street": obs.my_bet,
        "players": [{"seat": p.seat, "bot_id": f"seat{p.seat}", "stack": p.stack,
                     "state": "folded" if p.folded else ("all_in" if p.all_in else "active"),
                     "is_folded": p.folded, "is_all_in": p.all_in,
                     "bet_this_street": p.bet, "hole_cards": None}
                    for p in obs.players if not p.eliminated],
        "action_log": [{"seat": s, "action": a, "amount": amt} for (s, a, amt, _st) in obs.history],
    }


def fullhouse_agent(decide):
    class Agent:
        def act(self, obs):
            out = decide(fullhouse_state(obs))
            if not isinstance(out, dict):
                return None
            return out

    Agent.__name__ = "Agent"
    return Agent


# ---------------------------------------------------------------- RLCard
def _rl_card(c):
    return c[1].upper() + c[0]


def rlcard_agent(agent_cls, allowed_raise_num=4):
    """RLCard Limit Hold'em rule agents. 'raise' = one fixed-limit bet (1 big blind preflop/flop,
    2 big blinds turn/river, as in rlcard.games.limitholdem); legal actions follow
    rlcard's Round.get_legal_actions."""

    class Agent:
        def __init__(self):
            self.agent = agent_cls()

        def act(self, obs):
            raises_this_street = sum(1 for h in obs.history if h[1] == "raise" and h[3] == obs.street)
            legal = ["call", "raise", "fold", "check"]
            if raises_this_street >= allowed_raise_num or "raise" not in obs.legal_actions:
                legal.remove("raise")
            if obs.to_call > 0:
                legal.remove("check")
            else:
                legal.remove("call")
            state = {"raw_legal_actions": legal,
                     "raw_obs": {"hand": [_rl_card(c) for c in obs.hole_cards],
                                 "public_cards": [_rl_card(c) for c in obs.board]}}
            a = self.agent.step(state) if hasattr(self.agent, "step") else self.agent.eval_step(state)[0]
            if a == "raise":
                bet = obs.big_blind * (2 if obs.street >= 4 else 1)
                return ("raise", obs.current_bet + bet)
            return a

    Agent.__name__ = "Agent"
    return Agent


# ---------------------------------------------------------------- texasholdem (SirRender00)
class ActionType(Enum):
    """texasholdem.game.action_type.ActionType"""
    RAISE = 1
    ALL_IN = 2
    CALL = 3
    CHECK = 4
    FOLD = 5


class PlayerState(Enum):
    """texasholdem.game.player_state.PlayerState (subset)"""
    SKIP = 1
    OUT = 2
    IN = 3
    TO_CALL = 4
    ALL_IN = 5


class MoveIterator:
    """Subset of texasholdem.game.move.MoveIterator used by the basic agents."""

    def __init__(self, moves):
        self._raise_range = moves.get(ActionType.RAISE) or range(0)
        self._action_types = list(moves.keys())

    @property
    def action_types(self):
        return list(self._action_types)

    @property
    def raise_range(self):
        return self._raise_range

    def __contains__(self, item):
        return item in self._action_types

    def __delitem__(self, key):
        if key in self._action_types:
            self._action_types.remove(key)

    def sample(self, num=1):
        action_types = _random.choices(self._action_types, k=num)
        if ActionType.RAISE in self:
            totals = _random.choices(self._raise_range, k=num)
            totals = [totals[i] if action_types[i] == ActionType.RAISE else None for i in range(num)]
        else:
            totals = [None] * num
        samples = list(zip(action_types, totals))
        return samples[0] if num == 1 else samples


class _THPlayer:
    def __init__(self, state, chips):
        self.state = state
        self.chips = chips


class TexasHoldEmView:
    """Read-only stand-in for texasholdem.game.game.TexasHoldEm, built from our Observation,
    mirroring TexasHoldEm.get_available_moves()."""

    def __init__(self, obs):
        self.obs = obs
        self.current_player = obs.seat
        state = PlayerState.TO_CALL if obs.to_call > 0 else PlayerState.IN
        self.players = {obs.seat: _THPlayer(state, obs.my_stack)}

    def get_available_moves(self):
        o = self.obs
        possible = {ActionType.RAISE: None, ActionType.CALL: None, ActionType.CHECK: None,
                    ActionType.FOLD: None}
        if o.min_raise_to is not None:
            possible[ActionType.RAISE] = range(o.min_raise_to, o.max_raise_to + 1)
        else:
            del possible[ActionType.RAISE]
        if o.to_call == 0:
            del possible[ActionType.CALL]
        else:
            del possible[ActionType.CHECK]
        return MoveIterator(possible)


def texasholdem_agent(agent_fn, **kwargs):
    class Agent:
        def act(self, obs):
            action, total = agent_fn(TexasHoldEmView(obs), **kwargs)
            if action == ActionType.RAISE:
                return ("raise", total)
            return {ActionType.CALL: "call", ActionType.CHECK: "check", ActionType.FOLD: "fold",
                    ActionType.ALL_IN: "all_in"}[action]

    Agent.__name__ = "Agent"
    return Agent
