"""PyPokerEngine RandomPlayer

Ported from: https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/random_player.py
Author: ishikota
License: MIT

Strategy: Random actions. As written, call_ratio == fold_ratio so it folds ~1/3 and raises a random amount ~2/3 of the time.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `pypokerengine` imports -> compat.pypokerengine (same API; see that file for details)
  * added `Agent = pypokerengine_agent(...)` at the bottom
"""
BOT_NAME = 'PyPokerEngine RandomPlayer'
BOT_AUTHOR = 'ishikota'
BOT_SOURCE = 'https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/random_player.py'
BOT_DESCRIPTION = 'Random actions. As written, call_ratio == fold_ratio so it folds ~1/3 and raises a random amount ~2/3 of the time.'
BOT_LICENSE = 'MIT'

from compat.pypokerengine import BasePokerPlayer, pypokerengine_agent  # PORT
import random as rand

class RandomPlayer(BasePokerPlayer):

  def __init__(self):
    self.fold_ratio = self.call_ratio = raise_ratio = 1.0/3

  def set_action_ratio(self, fold_ratio, call_ratio, raise_ratio):
    ratio = [fold_ratio, call_ratio, raise_ratio]
    scaled_ratio = [ 1.0 * num / sum(ratio) for num in ratio]
    self.fold_ratio, self.call_ratio, self.raise_ratio = scaled_ratio

  def declare_action(self, valid_actions, hole_card, round_state):
    choice = self.__choice_action(valid_actions)
    action = choice["action"]
    amount = choice["amount"]
    if action == "raise":
      amount = rand.randrange(amount["min"], max(amount["min"], amount["max"]) + 1)
    return action, amount

  def __choice_action(self, valid_actions):
    r = rand.random()
    if r <= self.fold_ratio:
      return valid_actions[0]
    elif r <= self.call_ratio:
      return valid_actions[1]
    else:
      return valid_actions[2]


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


Agent = pypokerengine_agent(RandomPlayer)  # PORT
