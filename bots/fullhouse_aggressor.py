"""The Aggressor

Ported from: https://github.com/uzlez/fullhouse-engine/blob/main/bots/aggressor/bot.py
Author: Fullhouse Hackathon
License: MIT

Strategy: Raises 2-4x the min raise 70% of the time, otherwise check/call.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * the engine builds the same `game_state` dict as Fullhouse's engine/game.py (compat/others.py: fullhouse_state)
  * added `Agent = fullhouse_agent(decide)` at the bottom
"""
BOT_NAME = 'The Aggressor'
BOT_AUTHOR = 'Fullhouse Hackathon'
BOT_SOURCE = 'https://github.com/uzlez/fullhouse-engine/blob/main/bots/aggressor/bot.py'
BOT_DESCRIPTION = 'Raises 2-4x the min raise 70% of the time, otherwise check/call.'
BOT_LICENSE = 'MIT'

"""Bot A: The Aggressor — raises constantly, bets big."""
import random

BOT_NAME = "The Aggressor"

def decide(state):
    stack = state["your_stack"]
    pot   = state["pot"]
    min_r = state["min_raise_to"]

    if random.random() < 0.7:
        raise_to = min(min_r * random.randint(2, 4), stack + state["your_bet_this_street"])
        raise_to = max(raise_to, min_r)
        return {"action": "raise", "amount": raise_to}

    if state["can_check"]:
        return {"action": "check"}

    return {"action": "call"}


from compat.others import fullhouse_agent  # PORT
Agent = fullhouse_agent(decide)  # PORT
