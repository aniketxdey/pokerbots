"""Minimal eval7-compatible API (Card, Deck, evaluate, handtype) on top of engine.evaluator.

Ported bots use `from compat import eval7` so a real eval7 install (C extension) isn't needed.
Your own bots can still `import eval7` if you have it installed.
"""
import random as _random

from engine.cards import str_to_card, CARD_STRS
from engine.evaluator import evaluate as _evaluate, category_name


class Card(int):
    """An int (0..51) that prints like 'As'. Equality/hashing are by card value."""

    def __new__(cls, s):
        if isinstance(s, str):
            return int.__new__(cls, str_to_card(s))
        return int.__new__(cls, int(s))

    def __str__(self):
        return CARD_STRS[self]

    def __repr__(self):
        return f'Card("{CARD_STRS[self]}")'

    @property
    def rank(self):
        return int(self) >> 2

    @property
    def suit(self):
        return int(self) & 3


_ALL = [Card(i) for i in range(52)]


class Deck:
    def __init__(self):
        self.cards = list(_ALL)

    def shuffle(self):
        _random.shuffle(self.cards)

    def peek(self, n):
        return self.cards[:n]

    def deal(self, n):
        out = self.cards[:n]
        del self.cards[:n]
        return out

    def sample(self, n):
        return _random.sample(self.cards, n)

    def __len__(self):
        return len(self.cards)


def evaluate(cards):
    return _evaluate(cards)


def handtype(value):
    return category_name(value)
