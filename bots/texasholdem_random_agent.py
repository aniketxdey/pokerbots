"""texasholdem random_agent

Ported from: https://github.com/SirRender00/texasholdem/blob/main/texasholdem/agents/basic.py
Author: SirRender00
License: MIT

Strategy: Uniformly random among fold / check-or-call / raise; raise totals uniform over the legal range.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * texasholdem imports -> compat.others (ActionType, MoveIterator, a TexasHoldEm view that
    mirrors TexasHoldEm.get_available_moves())
  * added `Agent = texasholdem_agent(random_agent)` at the bottom
"""
BOT_NAME = 'texasholdem random_agent'
BOT_AUTHOR = 'SirRender00'
BOT_SOURCE = 'https://github.com/SirRender00/texasholdem/blob/main/texasholdem/agents/basic.py'
BOT_DESCRIPTION = 'Uniformly random among fold / check-or-call / raise; raise totals uniform over the legal range.'
BOT_LICENSE = 'MIT'

from typing import Tuple

from compat.others import ActionType, TexasHoldEmView as TexasHoldEm, texasholdem_agent  # PORT


def random_agent(game: TexasHoldEm, no_fold: bool = False) -> Tuple[ActionType, int]:
    """
    A uniformly random player

        - If someone raised, CALL, FOLD, or RAISE with uniform probability
        - Else, CHECK, (FOLD if no_fold=False), RAISE with uniform probability
        - If RAISE, the value will be uniformly random in [min_raise, # of chips]

    Arguments:
        game (TexasHoldEm): The TexasHoldEm game
        no_fold (bool): Removes the possibility of folding if no one raised, default False.
    Returns:
        Tuple[ActionType, int]: Returns a uniformly random action from the
            available moves.

    """
    moves = game.get_available_moves()
    if no_fold:
        del moves[ActionType.FOLD]

    return moves.sample()


Agent = texasholdem_agent(random_agent)  # PORT
