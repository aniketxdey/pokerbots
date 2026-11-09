"""The bot interface.

A bot is one Python file in bots/ that defines a class named ``Agent``:

    class Agent:
        def __init__(self):                 # called once per table (state resets per table)
            ...
        def act(self, obs) -> action:       # REQUIRED: called whenever it's your turn
            ...
        # optional callbacks (each one is also subject to the time limit):
        def new_table(self, info): ...      # dict: seat, num_players, starting_stack, blind_schedule, ...
        def new_hand(self, obs): ...        # same Observation as act(), before any decision
        def hand_over(self, result): ...    # HandResult: deltas, showdown cards, board, ...
        # variant-specific (only called in variants that need them):
        def discard(self, obs): ...         # -> list of cards to throw away (Pineapple, Irish,
                                            #    draw games, 2026 Toss). obs.discard_count = how many
                                            #    (draw games: up to that many; 'stand pat' = [])
        def bid(self, obs): ...             # -> int chips (2024 Auction Hold'em), 0..obs.max_bid
        def reveal(self, obs): ...          # -> list of cards to turn face up (Discard & Reveal);
                                            #    obs.reveal_count = how many (default: your lowest card)

Return value of act() can be any of:
    'fold' | 'check' | 'call' | 'all_in'
    ('raise', amount_to)    # amount_to = your TOTAL bet for this street ("raise to", not "raise by")
    Action.fold() / Action.check() / Action.call() / Action.raise_to(x) / Action.all_in()

Illegal actions are repaired rather than punished: check when facing a bet -> fold,
call with nothing to call -> check, raise when raising is not allowed -> call/check,
raise amounts are clamped into [min_raise_to, max_raise_to]. Exceptions and time-limit
violations become check (if legal) else fold, exactly like the official rules (§3.7).

Optional module-level metadata shown in the UI:
    BOT_NAME, BOT_AUTHOR, BOT_SOURCE, BOT_DESCRIPTION, BOT_LICENSE
"""

FOLD, CHECK, CALL, RAISE = "fold", "check", "call", "raise"
STREET_NAMES = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}


class Action(tuple):
    """Convenience constructor: Action.raise_to(400) == ('raise', 400)."""

    @staticmethod
    def fold():
        return (FOLD,)

    @staticmethod
    def check():
        return (CHECK,)

    @staticmethod
    def call():
        return (CALL,)

    @staticmethod
    def raise_to(amount):
        return (RAISE, amount)

    @staticmethod
    def all_in():
        return ("all_in",)


class PlayerView:
    """Public information about one seat. Opponents are anonymized: you only see seat numbers."""

    __slots__ = ("seat", "stack", "bet", "contributed", "folded", "all_in", "in_hand", "eliminated",
                 "up_cards")

    def __init__(self, seat, stack, bet, contributed, folded, all_in, in_hand, eliminated, up_cards=()):
        self.seat = seat                # seat index at this table (0..n-1)
        self.stack = stack              # chips behind (not counting chips already bet)
        self.bet = bet                  # chips bet on the current street
        self.contributed = contributed  # chips put into the pot this hand (all streets)
        self.folded = folded
        self.all_in = all_in
        self.in_hand = in_hand          # dealt into this hand and not folded
        self.eliminated = eliminated    # busted out of the tournament table
        self.up_cards = list(up_cards)  # face-up cards (stud games)

    def __repr__(self):
        return (f"PlayerView(seat={self.seat}, stack={self.stack}, bet={self.bet}, "
                f"contributed={self.contributed}, folded={self.folded}, all_in={self.all_in}, "
                f"eliminated={self.eliminated})")


class Observation:
    """Everything a bot may know when it is asked to act.

    All chip amounts are integers in table chips.
    """

    FIELDS = (
        "seat", "hand_number", "street", "street_name", "round_index", "hole_cards", "down_cards",
        "up_cards", "board", "board2", "pot", "to_call", "my_bet", "my_stack", "my_contributed",
        "current_bet", "min_raise_to", "max_raise_to", "legal_actions", "small_blind", "big_blind",
        "ante", "bring_in", "button", "small_blind_seat", "big_blind_seat", "num_players",
        "num_active", "players", "players_up_cards", "history", "starting_stack",
        "hands_until_blinds_up", "max_hands", "time_limit_ms", "variant", "variant_name", "game",
        "betting", "hand_rule", "split", "deck_ranks", "num_hole_cards", "phase", "discard_count", "reveal_count",
        "draws_remaining", "bounty_rank", "min_bid", "max_bid", "bet_size",
    )

    def __init__(self, **kw):
        self.__dict__.update(kw)

    # dict-style access for convenience: obs["pot"]
    def __getitem__(self, k):
        return getattr(self, k)

    def get(self, k, default=None):
        return getattr(self, k, default)

    def to_dict(self):
        d = {k: getattr(self, k, None) for k in self.FIELDS}
        d["players"] = [{s: getattr(p, s) for s in PlayerView.__slots__} for p in self.players]
        return d

    def __repr__(self):
        return f"Observation({self.to_dict()})"


# Field reference (kept here so it's easy to find):
#   seat                 your seat index
#   hand_number          1-based hand count at this table
#   street               0 preflop, 3 flop, 4 turn, 5 river (= number of board cards)
#   street_name          'preflop' | 'flop' | 'turn' | 'river'
#   hole_cards           e.g. ['As', 'Kd']
#   board                community cards, e.g. ['2c', '7h', 'Td']
#   pot                  all chips in the middle, INCLUDING bets on the current street
#   to_call              chips you must add to call (0 means you can check)
#   my_bet               your bet on this street
#   my_stack             your chips behind
#   my_contributed       your chips in the pot this hand
#   current_bet          the highest bet on this street
#   min_raise_to         smallest legal "raise to" total (None if you cannot raise)
#   max_raise_to         largest legal "raise to" total = all-in (None if you cannot raise)
#   legal_actions        subset of {'fold','check','call','raise'}
#   small_blind, big_blind, ante
#   button, small_blind_seat, big_blind_seat
#   num_players          seats at the table (including busted ones)
#   num_active           players still alive in the tournament table
#   players              list[PlayerView] indexed by seat
#   history              list of (seat, action, amount_to, street) for this hand, blinds included
#   starting_stack       2000 by default
#   hands_until_blinds_up  hands remaining at the current blind level (incl. this one)
#   max_hands            hand cap for the table (200 by default)
#   time_limit_ms        per-decision time limit


class HandResult:
    """Passed to hand_over()."""

    __slots__ = ("hand_number", "deltas", "stacks", "board", "board2", "showdown", "winners",
                 "eliminated", "my_seat", "variant", "bounty_rank", "history", "up_cards")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def __getitem__(self, k):
        return getattr(self, k)

    def get(self, k, default=None):
        return getattr(self, k, default)

# Variant fields on every Observation:
#   variant, variant_name    e.g. 'plo', 'Pot-Limit Omaha (PLO)'
#   game                     'community' | 'stud' | 'draw' (mixed games report the current game)
#   betting                  'no_limit' | 'pot_limit' | 'fixed_limit';  bet_size (fixed limit)
#   hand_rule                'any' | 'omaha' (exactly 2 hole + 3 board) | 'max2' | 'both'
#   split                    'high' | 'hilo8' | 'low_a5' | 'low_27' | 'badugi'
#   deck_ranks               ranks in the deck, e.g. '6789TJQKA' for Short Deck
#   hole_cards               ALL your private cards (stud: down + up); down_cards / up_cards
#   players[i].up_cards      opponents' face-up stud cards;  board2: second board (double board)
#   phase                    'bet' | 'discard' | 'bid' | 'reveal';  reveal_count;  draws_remaining;  bounty_rank (2025)
#   round_index              betting round number within the hand

# HandResult fields:
#   deltas     list[int] chip change per seat this hand
#   stacks     list[int] stacks per seat after the hand
#   board      final board cards
#   showdown   dict seat -> hole cards for every hand shown at showdown
#   winners    dict seat -> chips won
#   eliminated list of seats that busted this hand
#   my_seat    your seat
