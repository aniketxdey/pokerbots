"""MIT 2025 pot odds

Ported from: https://github.com/mitpokerbots/reference-3-2025
Author: MIT Pokerbots
License: MIT (see the source repository)

Strategy: Board-aware Monte Carlo win rate vs pot odds; mixes min and 10% raises.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `skeleton` imports -> `from compat.mit import *` (MIT skeleton emulation, see compat/mit.py)
  * `import eval7` -> `from compat import eval7` (same API, pure-Python evaluator)
  * `STARTING_STACK - my_stack` / `- opp_stack` (MIT reset stacks every hand) ->
    round_state.my_contribution / round_state.opp_contribution (chips in this hand's pot)
  * data files (pandas CSV / pickle) are embedded at the bottom of this file
  * the `run_bot(...)` entry point is replaced by `Agent = mit_agent(Player)`
"""
BOT_NAME = 'MIT 2025 pot odds'
BOT_AUTHOR = 'MIT Pokerbots'
BOT_SOURCE = 'https://github.com/mitpokerbots/reference-3-2025'
BOT_DESCRIPTION = 'Board-aware Monte Carlo win rate vs pot odds; mixes min and 10% raises.'
BOT_LICENSE = "MIT"

'''
Simple example pokerbot, written in Python.
'''
from compat.mit import *  # PORT: was the MIT `skeleton` imports

import random
from compat import eval7  # PORT: was `import eval7`


class Player(Bot):
    '''
    A pokerbot.
    '''

    def __init__(self):
        '''
        Called when a new game starts. Called exactly once.

        Arguments:
        Nothing.

        Returns:
        Nothing.
        '''
        pass

    def handle_new_round(self, game_state, round_state, active):
        '''
        Called when a new round starts. Called NUM_ROUNDS times.

        Arguments:
        game_state: the GameState object.
        round_state: the RoundState object.
        active: your player's index.

        Returns:
        Nothing.
        '''
        #my_bankroll = game_state.bankroll  # the total number of chips you've gained or lost from the beginning of the game to the start of this round
        #game_clock = game_state.game_clock  # the total number of seconds your bot has left to play this game
        #round_num = game_state.round_num  # the round number from 1 to NUM_ROUNDS
        #my_cards = round_state.hands[active]  # your cards
        #big_blind = bool(active)  # True if you are the big blind
        #my_bounty = round_state.bounties[active]  # your current bounty rank
        pass

    def handle_round_over(self, game_state, terminal_state, active):
        '''
        Called when a round ends. Called NUM_ROUNDS times.

        Arguments:
        game_state: the GameState object.
        terminal_state: the TerminalState object.
        active: your player's index.

        Returns:
        Nothing.
        '''
        #my_delta = terminal_state.deltas[active]  # your bankroll change from this round
        previous_state = terminal_state.previous_state  # RoundState before payoffs
        #street = previous_state.street  # 0, 3, 4, or 5 representing when this round ended
        #my_cards = previous_state.hands[active]  # your cards
        #opp_cards = previous_state.hands[1-active]  # opponent's cards or [] if not revealed
        
        my_bounty_hit = terminal_state.bounty_hits[active]  # True if you hit bounty
        opponent_bounty_hit = terminal_state.bounty_hits[1-active] # True if opponent hit bounty
        bounty_rank = previous_state.bounties[active]  # your bounty rank

        # The following is a demonstration of accessing illegal information (will not work)
        opponent_bounty_rank = previous_state.bounties[1-active]  # attempting to grab opponent's bounty rank

        if my_bounty_hit:
            print("I hit my bounty of " + bounty_rank + "!")
        if opponent_bounty_hit:
            print("Opponent hit their bounty of " + opponent_bounty_rank + "!")

    def calculate_strength(self, my_cards, board_cards):
        print(f"my cards: {my_cards}")
        print(f"board card: {board_cards}")

        MC_ITER = 100

        my_cards = [eval7.Card(card) for card in my_cards]
        board_cards = [eval7.Card(card) for card in board_cards]

        deck = eval7.Deck()

        for card in my_cards + board_cards:
            deck.cards.remove(card)
        
        score = 0
        for _ in range(MC_ITER):

            deck.shuffle()

            draw_number = 2 + (5 - len(board_cards))
            draw = deck.peek(draw_number)

            opp_draw = draw[:2]
            board_draw = draw[2:]

            my_hand = my_cards + board_cards + board_draw
            opp_hand = opp_draw + board_cards + board_draw

            my_value = eval7.evaluate(my_hand)
            opp_value = eval7.evaluate(opp_hand)

            if my_value > opp_value:
                score += 1
            elif my_value < opp_value:
                score += 0
            else:
                score += 0.5
        
        win_rate = score / MC_ITER
        print(f"win rate: {win_rate}")

        return win_rate


    def get_action(self, game_state, round_state, active):
        '''
        Where the magic happens - your code should implement this function.
        Called any time the engine needs an action from your bot.

        Arguments:
        game_state: the GameState object.
        round_state: the RoundState object.
        active: your player's index.

        Returns:
        Your action.
        '''
        legal_actions = round_state.legal_actions()  # the actions you are allowed to take
        street = round_state.street  # 0, 3, 4, or 5 representing pre-flop, flop, turn, or river respectively
        my_cards = round_state.hands[active]  # your cards
        board_cards = round_state.deck[:street]  # the board cards
        my_pip = round_state.pips[active]  # the number of chips you have contributed to the pot this round of betting
        opp_pip = round_state.pips[1-active]  # the number of chips your opponent has contributed to the pot this round of betting
        my_stack = round_state.stacks[active]  # the number of chips you have remaining
        opp_stack = round_state.stacks[1-active]  # the number of chips your opponent has remaining
        continue_cost = opp_pip - my_pip  # the number of chips needed to stay in the pot
        my_bounty = round_state.bounties[active]  # your current bounty rank
        my_contribution = round_state.my_contribution  # PORT: was STARTING_STACK - my_stack  # the number of chips you have contributed to the pot
        opp_contribution = round_state.opp_contribution  # PORT: was STARTING_STACK - opp_stack  # the number of chips your opponent has contributed to the pot

        strength = self.calculate_strength(my_cards, board_cards)
        pot_odds = continue_cost / (my_pip + opp_pip + 0.1)

        if RaiseAction in legal_actions:
           min_raise, max_raise = round_state.raise_bounds()  # the smallest and largest numbers of chips for a legal bet/raise
           min_cost = min_raise - my_pip  # the cost of a minimum bet/raise
           max_cost = max_raise - my_pip  # the cost of a maximum bet/raise
        if RaiseAction in legal_actions:
            if random.random() < 0.5:
                if strength > 2*pot_odds:
                    raise_amount = int(min_raise + 0.1 * (max_raise - min_raise))
                    return RaiseAction(raise_amount)
                return RaiseAction(min_raise)
        if CheckAction in legal_actions:  # check-call
            return CheckAction()
        if random.random() < 0.25:
            return FoldAction()
        return CallAction()


Agent = mit_agent(Player)  # PORT: replaces run_bot(Player(), parse_args())
