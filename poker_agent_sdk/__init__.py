"""poker_agent_sdk - a local stand-in for the competition SDK.

Mirrors the published interface (No Limit '26 participant docs):

    import poker_agent_sdk
    from poker_agent_sdk import Action

    class Agent(poker_agent_sdk.Agent):
        def act(self, state, memory):
            # return an Action and the bytes you want back as memory next turn at this table
            return Action.check() if state.can("check") else Action.fold(), memory

Betting actions: Action.fold(), Action.check(), Action.call(), Action.raise_to(total) where total
is your whole bet for the current betting round. state.can(kind) returns the legal action of that
kind (with min_amount / max_amount) or None. Choose from state.legal_actions.

Variant phases in this mock (the real kit defines its own; read its RULES.md):
    discard: Action("discard", cards=[...])   reveal: Action("reveal", cards=[...])
    the LegalAction's min_amount == max_amount == the number of cards to choose.

The real SDK's field names may differ slightly; this mock follows the published docs.
"""

__all__ = ["Agent", "Action", "LegalAction", "Event", "Seat", "TableConfig", "State"]
__version__ = "mock-0.1"


class Action:
    __slots__ = ("kind", "amount", "cards")

    def __init__(self, kind, amount=None, cards=None):
        self.kind = kind
        self.amount = amount
        self.cards = list(cards) if cards is not None else None

    @classmethod
    def fold(cls):
        return cls("fold")

    @classmethod
    def check(cls):
        return cls("check")

    @classmethod
    def call(cls):
        return cls("call")

    @classmethod
    def raise_to(cls, total):
        return cls("raise", total)

    def __repr__(self):
        extra = f", {self.amount}" if self.amount is not None else ""
        extra += f", cards={self.cards}" if self.cards is not None else ""
        return f"Action({self.kind!r}{extra})"

    def __eq__(self, other):
        return isinstance(other, Action) and (self.kind, self.amount, self.cards) == (other.kind, other.amount, other.cards)


class LegalAction:
    __slots__ = ("kind", "min_amount", "max_amount")

    def __init__(self, kind, min_amount=None, max_amount=None):
        self.kind = kind
        self.min_amount = min_amount
        self.max_amount = max_amount

    def __repr__(self):
        if self.min_amount is None:
            return f"LegalAction({self.kind!r})"
        return f"LegalAction({self.kind!r}, {self.min_amount}, {self.max_amount})"


class Event:
    """kind: hand | ante | blind | fold | check | call | raise | board | show | win | stack, plus
    variant kinds (this mock: discard, reveal). seat is None for hand and board."""
    __slots__ = ("kind", "seat", "amount", "cards")

    def __init__(self, kind, seat=None, amount=None, cards=None):
        self.kind = kind
        self.seat = seat
        self.amount = amount
        self.cards = cards

    def __repr__(self):
        return f"Event({self.kind!r}, seat={self.seat}, amount={self.amount}, cards={self.cards})"


class Seat:
    """status: active | folded | all_in | busted"""
    __slots__ = ("seat", "stack", "bet", "status", "shown_cards")

    def __init__(self, seat, stack, bet, status, shown_cards):
        self.seat = seat
        self.stack = stack
        self.bet = bet
        self.status = status
        self.shown_cards = shown_cards

    def __repr__(self):
        return f"Seat({self.seat}, stack={self.stack}, bet={self.bet}, {self.status}, shown={self.shown_cards})"


class TableConfig:
    __slots__ = ("table_id", "seat", "variant", "players", "starting_stack", "blinds", "hands_per_level",
                 "hand_cap", "payouts", "decision_ms", "time_bank_ms", "memory_bytes")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __repr__(self):
        return "TableConfig(" + ", ".join(f"{k}={getattr(self, k)!r}" for k in self.__slots__) + ")"


class State:
    """Everything your bot sees on its turn.

    hand:       seat, cards, board, pot, to_call, legal_actions, history (this hand), phase
    table:      seats (list of Seat), button
    tournament: blind_level, blinds, hands_until_next_level, hand_number, players_remaining, bust_order
    events:     everything public since your last turn at this table
    config:     TableConfig (the table's fixed rules)
    variant:    public state the variant adds (this mock: {"up_cards": {seat: [cards]}})
    time_bank_ms: what remains of this table's bank
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def can(self, kind):
        for a in self.legal_actions:
            if a.kind == kind:
                return a
        return None

    def __repr__(self):
        keys = ("seat", "phase", "cards", "board", "pot", "to_call", "legal_actions", "hand_number")
        return "State(" + ", ".join(f"{k}={getattr(self, k, None)!r}" for k in keys) + ", ...)"


class Agent:
    """Subclass this and implement act(state, memory) -> (Action, bytes).

    Don't rely on self or globals between calls: any copy of your bot may answer any table.
    Per-table state travels in `memory` (bytes, starts as b"" at every table)."""

    def act(self, state, memory):
        a = state.can("check")
        return (Action.check() if a else Action.fold()), memory
