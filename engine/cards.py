"""Card helpers.

Cards are ints 0..51 internally: card = rank * 4 + suit, where rank 0..12 maps to
'2'..'A' and suit 0..3 maps to 'c','d','h','s'. Bots always see cards as
two-character strings such as 'As', 'Td', '7c' (the eval7 / MIT Pokerbots format).
"""

RANKS = "23456789TJQKA"
SUITS = "cdhs"

CARD_STRS = [r + s for r in RANKS for s in SUITS]
STR_TO_CARD = {s: i for i, s in enumerate(CARD_STRS)}
FULL_DECK = list(range(52))


def card_to_str(card: int) -> str:
    return CARD_STRS[card]


def str_to_card(s: str) -> int:
    try:
        return STR_TO_CARD[s[0].upper() + s[1].lower()]
    except (KeyError, IndexError, TypeError):
        raise ValueError(f"bad card string: {s!r}")


def cards_to_strs(cards):
    return [CARD_STRS[c] for c in cards]


def strs_to_cards(strs):
    return [str_to_card(s) for s in strs]


def rank_of(card: int) -> int:
    return card >> 2


def suit_of(card: int) -> int:
    return card & 3
