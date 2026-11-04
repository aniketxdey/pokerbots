"""MIT 2022 python all-in

Ported from: https://github.com/mitpokerbots/reference-lecture-2-2022
Author: MIT Pokerbots
License: MIT (see the source repository)

Strategy: Raises big with pocket pairs (raise-to = max - pip, as in the original), otherwise check/call.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `skeleton` imports -> `from compat.mit import *` (MIT skeleton emulation, see compat/mit.py)
  * `import eval7` -> `from compat import eval7` (same API, pure-Python evaluator)
  * `STARTING_STACK - my_stack` / `- opp_stack` (MIT reset stacks every hand) ->
    round_state.my_contribution / round_state.opp_contribution (chips in this hand's pot)
  * data files (pandas CSV / pickle) are embedded at the bottom of this file
  * the `run_bot(...)` entry point is replaced by `Agent = mit_agent(Player)`
"""
BOT_NAME = 'MIT 2022 python all-in'
BOT_AUTHOR = 'MIT Pokerbots'
BOT_SOURCE = 'https://github.com/mitpokerbots/reference-lecture-2-2022'
BOT_DESCRIPTION = 'Raises big with pocket pairs (raise-to = max - pip, as in the original), otherwise check/call.'
BOT_LICENSE = "MIT"

'''
Simple example pokerbot, written in Python.
'''
from compat.mit import *  # PORT: was the MIT `skeleton` imports


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
        
        self.strong_hole = False #keep track of whether or not we have strong hole cards

    def evaluate_cards(self, my_cards):
        ranks = {}

        for card in my_cards:
            card_rank = card[0]
            card_suit = card[1]


            if card_rank in ranks:
                ranks[card_rank].append(card)
            else:
                ranks[card_rank] = [card]

        pairs = [] #keeps track of the pairs that we have 
        singles = [] #other 

        for rank in ranks:
            cards = ranks[rank]

            if len(cards) == 1: #single card, can't be in a pair
                singles.append(cards[0])
            
            else: #a single pair or two pairs can be made here, add them all
                pairs += cards
            

        if len(pairs) > 0: #we found a pair! update our state to say that this is a strong round
            self.strong_hole = True
        
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
        my_bankroll = game_state.bankroll  # the total number of chips you've gained or lost from the beginning of the game to the start of this round
        game_clock = game_state.game_clock  # the total number of seconds your bot has left to play this game
        round_num = game_state.round_num  # the round number from 1 to NUM_ROUNDS
        my_cards = round_state.hands[active]  # your cards
        big_blind = bool(active)  # True if you are the big blind

        self.evaluate_cards(my_cards) #allocate our cards to our board allocations

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
        my_delta = terminal_state.deltas[active]  # your bankroll change from this round
        previous_state = terminal_state.previous_state  # RoundState before payoffs
        street = previous_state.street  # 0, 3, 4, or 5 representing when this round ended
        my_cards = previous_state.hands[active]  # your cards
        opp_cards = previous_state.hands[1-active]  # opponent's cards or [] if not revealed
        
        self.strong_hole = False #reset our strong hole flag

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
        my_contribution = round_state.my_contribution  # PORT: was STARTING_STACK - my_stack  # the number of chips you have contributed to the pot
        opp_contribution = round_state.opp_contribution  # PORT: was STARTING_STACK - opp_stack  # the number of chips your opponent has contributed to the pot
        net_upper_raise_bound = round_state.raise_bounds()
        stacks = [my_stack, opp_stack] #keep track of our stacks

        my_action = None

        if (RaiseAction in legal_actions and self.strong_hole): #only consider raising if the hand we have is strong
            min_raise, max_raise = round_state.raise_bounds()
            raise_amount = max_raise - my_pip
            my_action = RaiseAction(raise_amount) #GO ALL IN!!!
            

        elif CheckAction in legal_actions: #if we can check, do so
            my_action = CheckAction()
        else:
            my_action = CallAction()


        return my_action


Agent = mit_agent(Player)  # PORT: replaces run_bot(Player(), parse_args())
