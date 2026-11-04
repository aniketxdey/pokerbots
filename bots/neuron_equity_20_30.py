"""neuron_poker equity/20/30

Ported from: https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py
Author: Nicolas Dickreuter
License: MIT

Strategy: Equity-threshold agent with min_call_equity=0.2, min_bet_equity=0.3 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `from gym_env.enums import Action` -> compat.others.NeuronAction (same members)
  * neuron_poker's env computed info['player_data']['equity_to_river_alive'] with 1000 Monte
    Carlo runs before each decision; the adapter does the same (time counts against the bot)
  * raise actions map to neuron_poker's sizes (half pot / pot / 2x pot / 3BB / all-in)
"""
BOT_NAME = 'neuron_poker equity/20/30'
BOT_AUTHOR = 'Nicolas Dickreuter'
BOT_SOURCE = 'https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py'
BOT_DESCRIPTION = "Equity-threshold agent with min_call_equity=0.2, min_bet_equity=0.3 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold."
BOT_LICENSE = 'MIT'

"""Random player"""

from compat.others import NeuronAction as Action, neuron_agent  # PORT


class Player:
    """Mandatory class with the player methods"""

    def __init__(self, name="Random", min_call_equity=None, min_bet_equity=None):
        """Initiaization of an agent"""
        self.equity_alive = 0
        self.name = name

        self.min_call_equity = min_call_equity
        self.min_bet_equity = min_bet_equity

        self.autoplay = True

    def action(self, action_space, observation, info):  # pylint: disable=no-self-use
        """Mandatory method that calculates the move based on the observation array and the action space."""
        _ = observation
        equity_alive = info["player_data"]["equity_to_river_alive"]

        incremen1 = 0.1
        increment2 = 0.2

        if (
            equity_alive > self.min_bet_equity + increment2
            and Action.ALL_IN in action_space
        ):
            action = Action.ALL_IN

        elif (
            equity_alive > self.min_bet_equity + incremen1
            and Action.RAISE_2POT in action_space
        ):
            action = Action.RAISE_2POT

        elif equity_alive > self.min_bet_equity and Action.RAISE_POT in action_space:
            action = Action.RAISE_POT

        elif (
            equity_alive > self.min_bet_equity - incremen1
            and Action.RAISE_HALF_POT in action_space
        ):
            action = Action.RAISE_HALF_POT

        elif equity_alive > self.min_call_equity and Action.CALL in action_space:
            action = Action.CALL

        elif Action.CHECK in action_space:
            action = Action.CHECK

        else:
            action = Action.FOLD

        return action


# PORT: configuration from neuron_poker main.py: EquityPlayer(name='equity/20/30', min_call_equity=0.2, min_bet_equity=0.3)
Agent = neuron_agent(lambda: Player(name='equity/20/30', min_call_equity=0.2, min_bet_equity=0.3))
