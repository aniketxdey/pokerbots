"""Opponent for local play: always makes the minimum raise when it can, otherwise calls/checks."""
import poker_agent_sdk
from poker_agent_sdk import Action


class Agent(poker_agent_sdk.Agent):
    def act(self, state, memory):
        if state.phase in ("discard", "reveal"):
            n = state.legal_actions[0].min_amount
            return Action(state.phase, cards=state.variant["down_cards"][:n]), memory
        r = state.can("raise")
        if r:
            return Action.raise_to(r.min_amount), memory
        if state.can("call"):
            return Action.call(), memory
        return Action.check(), memory
