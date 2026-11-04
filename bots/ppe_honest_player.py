"""PyPokerEngine HonestPlayer

Ported from: https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/honest_player.py
Author: ishikota
License: MIT

Strategy: 1000-run Monte Carlo win rate vs every seat; calls if win rate >= 1/players, else folds.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `pypokerengine` imports -> compat.pypokerengine (same API; see that file for details)
  * added `Agent = pypokerengine_agent(...)` at the bottom
"""
BOT_NAME = 'PyPokerEngine HonestPlayer'
BOT_AUTHOR = 'ishikota'
BOT_SOURCE = 'https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/honest_player.py'
BOT_DESCRIPTION = '1000-run Monte Carlo win rate vs every seat; calls if win rate >= 1/players, else folds.'
BOT_LICENSE = 'MIT'

from compat.pypokerengine import BasePokerPlayer, pypokerengine_agent  # PORT
from compat.pypokerengine import gen_cards, estimate_hole_card_win_rate  # PORT

NB_SIMULATION = 1000

class HonestPlayer(BasePokerPlayer):

    def declare_action(self, valid_actions, hole_card, round_state):
        community_card = round_state['community_card']
        win_rate = estimate_hole_card_win_rate(
                nb_simulation=NB_SIMULATION,
                nb_player=self.nb_player,
                hole_card=gen_cards(hole_card),
                community_card=gen_cards(community_card)
                )
        if win_rate >= 1.0 / self.nb_player:
            action = valid_actions[1]  # fetch CALL action info
        else:
            action = valid_actions[0]  # fetch FOLD action info
        return action['action'], action['amount']

    def receive_game_start_message(self, game_info):
        self.nb_player = game_info['player_num']

    def receive_round_start_message(self, round_count, hole_card, seats):
        pass

    def receive_street_start_message(self, street, round_state):
        pass

    def receive_game_update_message(self, action, round_state):
        pass

    def receive_round_result_message(self, winners, hand_info, round_state):
        pass


Agent = pypokerengine_agent(HonestPlayer)  # PORT
