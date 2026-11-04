"""neuron_poker random agent

Ported from: https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_random.py
Author: Nicolas Dickreuter
License: MIT

Strategy: Picks uniformly among fold/check/call and half-pot/pot/2x-pot raises.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `from gym_env.enums import Action` -> compat.others.NeuronAction (same members)
  * neuron_poker's env computed info['player_data']['equity_to_river_alive'] with 1000 Monte
    Carlo runs before each decision; the adapter does the same (time counts against the bot)
  * raise actions map to neuron_poker's sizes (half pot / pot / 2x pot / 3BB / all-in)
"""
BOT_NAME = 'neuron_poker random agent'
BOT_AUTHOR = 'Nicolas Dickreuter'
BOT_SOURCE = 'https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_random.py'
BOT_DESCRIPTION = 'Picks uniformly among fold/check/call and half-pot/pot/2x-pot raises.'
BOT_LICENSE = 'MIT'

"""Random player"""
import random

from compat.others import NeuronAction as Action, neuron_agent  # PORT


class Player:
    """Mandatory class with the player methods"""

    def __init__(self, name='Random'):
        """Initiaization of an agent"""
        self.equity_alive = 0
        self.actions = []
        self.last_action_in_stage = ''
        self.temp_stack = []
        self.name = name
        self.autoplay = True

    def action(self, action_space, observation, info):  # pylint: disable=no-self-use
        """Mandatory method that calculates the move based on the observation array and the action space."""
        _ = observation  # not using the observation for random decision
        _ = info

        this_player_action_space = {Action.FOLD, Action.CHECK, Action.CALL, Action.RAISE_POT, Action.RAISE_HALF_POT,
                                    Action.RAISE_2POT}
        possible_moves = this_player_action_space.intersection(set(action_space))
        action = random.choice(list(possible_moves))
        return action


Agent = neuron_agent(Player)  # PORT
