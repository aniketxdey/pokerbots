"""PyPokerEngine emulation (github.com/ishikota/PyPokerEngine, MIT license).

    from compat.pypokerengine import BasePokerPlayer, gen_cards, estimate_hole_card_win_rate
    class MyPlayer(BasePokerPlayer): ...      # original code
    Agent = pypokerengine_agent(MyPlayer)

Faithful details kept from PyPokerEngine:
  * cards are 'SA'-style strings (suit letter + rank), e.g. 'CA', 'HT', 'D9'
  * valid_actions = [fold, call, raise]; "call" also means check (amount = bet to match);
    raise amount is {"min", "max"} as a "raise to" total, or -1/-1 when raising is impossible
  * like PyPokerEngine, an invalid raise amount is treated as a fold
  * receive_game_start_message gets player_num = number of seats at the table
"""
import random as _random

from engine.cards import str_to_card
from engine.evaluator import evaluate

_SUIT_MAP = {"c": "C", "d": "D", "h": "H", "s": "S"}


def to_ppe(card):
    """'As' -> 'SA'"""
    return _SUIT_MAP[card[1]] + card[0]


def _from_ppe(card):
    """'SA' -> int"""
    return str_to_card(card[1] + card[0].lower())


def gen_cards(cards_str):
    return [_from_ppe(c) for c in cards_str]


def estimate_hole_card_win_rate(nb_simulation, nb_player, hole_card, community_card=None):
    """Same algorithm as pypokerengine.utils.card_utils: random opponents and board,
    a 'win' is our hand being >= every opponent's hand."""
    community_card = list(community_card or [])
    used = set(hole_card) | set(community_card)
    rest = [c for c in range(52) if c not in used]
    need = 5 - len(community_card)
    wins = 0
    nopp = nb_player - 1
    for _ in range(nb_simulation):
        draw = _random.sample(rest, need + 2 * nopp)
        board = community_card + draw[:need]
        mine = evaluate(list(hole_card) + board)
        best_opp = 0
        k = need
        for _o in range(nopp):
            v = evaluate([draw[k], draw[k + 1]] + board)
            k += 2
            if v > best_opp:
                best_opp = v
        if mine >= best_opp:
            wins += 1
    return 1.0 * wins / nb_simulation


class BasePokerPlayer:
    def __init__(self):
        pass

    def set_uuid(self, uuid):
        self.uuid = uuid

    def declare_action(self, valid_actions, hole_card, round_state):
        raise NotImplementedError

    def receive_game_start_message(self, game_info):
        pass

    def receive_round_start_message(self, round_count, hole_card, seats):
        pass

    def receive_street_start_message(self, street, round_state):
        pass

    def receive_game_update_message(self, new_action, round_state):
        pass

    def receive_round_result_message(self, winners, hand_info, round_state):
        pass


_STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}


def _street(obs):
    name = getattr(obs, "street_name", None)
    if name in ("preflop", "flop", "turn", "river"):
        return name
    return _STREETS.get(obs.street, "river" if obs.street >= 5 else "flop")


def _round_state(obs):
    seats = [{"name": f"p{p.seat}", "uuid": f"p{p.seat}", "stack": p.stack,
              "state": "folded" if p.folded else ("participating" if not p.eliminated else "folded")}
             for p in obs.players]
    return {
        "street": _street(obs),
        "pot": {"main": {"amount": obs.pot}, "side": []},
        "community_card": [to_ppe(c) for c in obs.board],
        "dealer_btn": obs.button,
        "next_player": obs.seat,
        "small_blind_pos": obs.small_blind_seat,
        "big_blind_pos": obs.big_blind_seat,
        "round_count": obs.hand_number,
        "small_blind_amount": obs.small_blind,
        "seats": seats,
        "action_histories": {},
    }


def pypokerengine_agent(player_cls):
    class Agent:
        def __init__(self):
            self.player = player_cls()
            self.player.set_uuid("me") if hasattr(self.player, "set_uuid") else None
            self.street = None

        def new_table(self, info):
            n = info.get("num_players", 8)
            self.player.receive_game_start_message({
                "player_num": n,
                "rule": {"initial_stack": info.get("starting_stack"), "max_round": info.get("max_hands"),
                         "small_blind_amount": info["blind_schedule"][0][0], "ante": 0,
                         "blind_structure": {}},
                "seats": [{"name": f"p{i}", "uuid": f"p{i}", "stack": info.get("starting_stack"),
                           "state": "participating"} for i in range(n)],
            })

        def new_hand(self, obs):
            self.street = None
            rs = _round_state(obs)
            self.player.receive_round_start_message(obs.hand_number,
                                                    [to_ppe(c) for c in obs.hole_cards], rs["seats"])

        def act(self, obs):
            rs = _round_state(obs)
            if self.street != obs.street:
                self.street = obs.street
                self.player.receive_street_start_message(rs["street"], rs)
            if obs.min_raise_to is not None:
                rinfo = {"min": obs.min_raise_to, "max": obs.max_raise_to}
            else:
                rinfo = {"min": -1, "max": -1}
            valid = [{"action": "fold", "amount": 0},
                     {"action": "call", "amount": obs.current_bet},
                     {"action": "raise", "amount": rinfo}]
            out = self.player.declare_action(valid, [to_ppe(c) for c in obs.hole_cards], rs)
            action, amount = out[0], out[1] if len(out) > 1 else 0
            if action == "fold":
                return "fold"
            if action == "call":
                return "call" if obs.to_call > 0 else "check"
            if action == "raise":
                try:
                    amount = int(amount)
                except Exception:
                    return "fold"
                if rinfo["min"] == -1 or not (rinfo["min"] <= amount <= rinfo["max"]):
                    return "fold"  # PyPokerEngine folds on an invalid raise
                return ("raise", amount)
            return "fold"

        def hand_over(self, result):
            winners = [{"uuid": f"p{k}", "stack": result.stacks[k]} for k in result.winners]
            hand_info = [{"uuid": f"p{k}", "hand": {"hole": [to_ppe(c) for c in v]}}
                         for k, v in result.showdown.items()]
            self.player.receive_round_result_message(winners, hand_info, {})

    Agent.__name__ = "Agent"
    return Agent
