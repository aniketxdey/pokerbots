"""The Mathematician

Ported from: https://github.com/uzlez/fullhouse-engine/blob/main/bots/mathematician/bot.py
Author: Fullhouse Hackathon
License: MIT

Strategy: Never bets; calls when the pot lays 3:1 or better, else folds.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * the engine builds the same `game_state` dict as Fullhouse's engine/game.py (compat/others.py: fullhouse_state)
  * added `Agent = fullhouse_agent(decide)` at the bottom
"""
BOT_NAME = 'The Mathematician'
BOT_AUTHOR = 'Fullhouse Hackathon'
BOT_SOURCE = 'https://github.com/uzlez/fullhouse-engine/blob/main/bots/mathematician/bot.py'
BOT_DESCRIPTION = 'Never bets; calls when the pot lays 3:1 or better, else folds.'
BOT_LICENSE = 'MIT'

"""Bot B: The Mathematician — calls only when pot odds justify it."""

BOT_NAME = "The Mathematician"

def decide(state):
    owed  = state["amount_owed"]
    pot   = state["pot"]
    stack = state["your_stack"]

    if state["can_check"]:
        return {"action": "check"}

    if owed == 0:
        return {"action": "check"}

    # Call if getting 3:1 or better
    pot_odds = pot / owed if owed > 0 else 999
    if pot_odds >= 3.0:
        return {"action": "call"}

    return {"action": "fold"}


from compat.others import fullhouse_agent  # PORT
Agent = fullhouse_agent(decide)  # PORT
