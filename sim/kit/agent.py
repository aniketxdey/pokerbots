"""Template bot: plays legally and shows how per-table memory works.

act(state, memory) must return (Action, memory_bytes) within state.config.decision_ms (1 ms).
Don't keep state on `self` or in globals: any copy of your bot may answer any table. Anything
you want to remember at this table goes in `memory` (bytes, starts as b"", max 64 KB).
"""
import json

import poker_agent_sdk
from poker_agent_sdk import Action


class Agent(poker_agent_sdk.Agent):
    def act(self, state, memory):
        # --- memory example: count how many hands we have seen at this table -------------
        try:
            mem = json.loads(memory.decode()) if memory else {}
        except Exception:          # parse defensively: memory can be truncated
            mem = {}
        for ev in state.events:    # everything public since our last turn here
            if ev.kind == "hand":
                mem["hands"] = mem.get("hands", 0) + 1

        # --- variant phases: pick cards when the variant asks for them --------------------
        if state.phase in ("discard", "reveal"):
            n = state.legal_actions[0].min_amount
            ranks = "23456789TJQKA"
            mine = sorted(state.variant.get("down_cards", state.cards), key=lambda c: ranks.index(c[0]))
            return Action(state.phase, cards=mine[:n]), json.dumps(mem).encode()

        # --- betting: check when free, otherwise fold ---------------------------------------
        if state.can("check"):
            action = Action.check()
        elif state.can("fold"):
            action = Action.fold()
        else:  # neither offered: take the automatic choice
            first = state.legal_actions[0]
            action = Action(first.kind, first.min_amount)
        return action, json.dumps(mem).encode()
