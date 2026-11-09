"""Opponent for local play: checks when free, otherwise folds."""
import poker_agent_sdk
from poker_agent_sdk import Action


class Agent(poker_agent_sdk.Agent):
    def act(self, state, memory):
        if state.phase in ("discard", "reveal"):
            n = state.legal_actions[0].min_amount
            return Action(state.phase, cards=state.variant["down_cards"][:n]), memory
        return (Action.check() if state.can("check") else Action.fold()), memory
