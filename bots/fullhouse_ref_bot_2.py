"""Fullhouse ref_bot_2

Ported from: https://github.com/uzlez/fullhouse-engine/blob/main/bots/ref_bot_2/bot.py
Author: Fullhouse Hackathon
License: MIT

Strategy: Reference bot: checks when possible, calls at 3:1 pot odds, else folds.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * the engine builds the same `game_state` dict as Fullhouse's engine/game.py (compat/others.py: fullhouse_state)
  * added `Agent = fullhouse_agent(decide)` at the bottom
"""
BOT_NAME = 'Fullhouse ref_bot_2'
BOT_AUTHOR = 'Fullhouse Hackathon'
BOT_SOURCE = 'https://github.com/uzlez/fullhouse-engine/blob/main/bots/ref_bot_2/bot.py'
BOT_DESCRIPTION = 'Reference bot: checks when possible, calls at 3:1 pot odds, else folds.'
BOT_LICENSE = 'MIT'

"""Reference bot 2 — pot-odds caller. For testing only."""

def decide(state: dict) -> dict:
    owed   = state["amount_owed"]
    pot    = state["pot"]
    stack  = state["your_stack"]

    if state["can_check"]:
        return {"action": "check"}

    # Call if getting better than 3:1 pot odds
    if owed == 0 or pot / owed >= 3:
        return {"action": "call"}

    return {"action": "fold"}


from compat.others import fullhouse_agent  # PORT
Agent = fullhouse_agent(decide)  # PORT
