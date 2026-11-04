"""MIT 2024 precompute

Ported from: https://github.com/mitpokerbots/reference-lecture-4-2024
Author: MIT Pokerbots
License: MIT (see the source repository)

Strategy: Precomputed strengths compared to the average strength of opponents' shown hands.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `skeleton` imports -> `from compat.mit import *` (MIT skeleton emulation, see compat/mit.py)
  * `import eval7` -> `from compat import eval7` (same API, pure-Python evaluator)
  * `STARTING_STACK - my_stack` / `- opp_stack` (MIT reset stacks every hand) ->
    round_state.my_contribution / round_state.opp_contribution (chips in this hand's pot)
  * data files (pandas CSV / pickle) are embedded at the bottom of this file
  * the `run_bot(...)` entry point is replaced by `Agent = mit_agent(Player)`
"""
BOT_NAME = 'MIT 2024 precompute'
BOT_AUTHOR = 'MIT Pokerbots'
BOT_SOURCE = 'https://github.com/mitpokerbots/reference-lecture-4-2024'
BOT_DESCRIPTION = "Precomputed strengths compared to the average strength of opponents' shown hands."
BOT_LICENSE = "MIT"

'''
Simple example pokerbot, written in Python.
'''
from compat.mit import *  # PORT: was the MIT `skeleton` imports
import random
from compat import eval7  # PORT: was `import eval7`
import pickle
import time


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
        self.opp_holes = []
        self.opp_bids = []  

        prev_time = time.time()
        self.starting_strengths = dict(_HAND_STRENGTHS)  # PORT: was a pickle load of the "hand_strengths" file
        print("pickle load:", time.time() - prev_time)
        
        rank_to_numeric = dict()

        for i in range(2,10):
            rank_to_numeric[str(i)] = i

        for num, rank in enumerate("TJQKA"): #[(0,T), (1,J), (2,Q) ...]
            rank_to_numeric[rank] = num + 10

        self.rank_to_numeric = rank_to_numeric

        self.num_showdowns = 0
        self.opp_avg_strength = 0.5


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
        
        self.early_game = (round_num < NUM_ROUNDS // 4)

        if round_num < 5:
            print("game clock:", game_clock)


        self.strong_hole = False

        if not self.early_game:

            card_strength = self.hand_to_strength(my_cards)
            card_strength = (card_strength[0] + card_strength[1])/2

            if card_strength > self.opp_avg_strength:
                self.strong_hole = True
        
        if round_num == NUM_ROUNDS:
            print("game clock:", game_clock)


    def hand_to_strength(self, my_cards): #AcKs, Jc9s

        card_1 = my_cards[0]
        card_2 = my_cards[1]

        rank_1, suit_1 = card_1
        rank_2, suit_2 = card_2

        num_1 = self.rank_to_numeric[rank_1]
        num_2 = self.rank_to_numeric[rank_2]

        suited = 'o'
        if suit_1 == suit_2:
            suited = "s"

        if num_1 >= num_2:
            key = rank_1 + rank_2 + suited
        else:
            key = rank_2 + rank_1 + suited

        return self.starting_strengths[key]


        #KA, AK

        

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

        opp_bid = previous_state.bids[1-active]

        
        if len(opp_cards) >= 2:
            opp_cur_strength = self.hand_to_strength(opp_cards[:2])
            opp_cur_strength = (opp_cur_strength[0] + opp_cur_strength[1])/2
            self.opp_avg_strength = (self.opp_avg_strength *self.num_showdowns + opp_cur_strength) /(self.num_showdowns + 1)
            self.num_showdowns += 1
            # self.opp_holes.append(opp_cards[:2])
            self.opp_bids.append(opp_bid)


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
        # May be useful, but you may choose to not use.
        legal_actions = round_state.legal_actions()  # the actions you are allowed to take
        street = round_state.street  # 0, 3, 4, or 5 representing pre-flop, flop, turn, or river respectively
        my_cards = round_state.hands[active]  # your cards
        board_cards = round_state.deck[:street]  # the board cards
        my_pip = round_state.pips[active]  # the number of chips you have contributed to the pot this round of betting
        opp_pip = round_state.pips[1-active]  # the number of chips your opponent has contributed to the pot this round of betting
        my_stack = round_state.stacks[active]  # the number of chips you have remaining
        opp_stack = round_state.stacks[1-active]  # the number of chips your opponent has remaining
        my_bid = round_state.bids[active]  # How much you bid previously (available only after auction)
        opp_bid = round_state.bids[1-active]  # How much opponent bid previously (available only after auction)
        continue_cost = opp_pip - my_pip  # the number of chips needed to stay in the pot
        my_contribution = round_state.my_contribution  # PORT: was STARTING_STACK - my_stack  # the number of chips you have contributed to the pot
        opp_contribution = round_state.opp_contribution  # PORT: was STARTING_STACK - opp_stack  # the number of chips your opponent has contributed to the pot
        if RaiseAction in legal_actions:
           min_raise, max_raise = round_state.raise_bounds()  # the smallest and largest numbers of chips for a legal bet/raise
           min_cost = min_raise - my_pip  # the cost of a minimum bet/raise
           max_cost = max_raise - my_pip  # the cost of a maximum bet/raise
        
        if self.early_game: # loose passive
            if RaiseAction in legal_actions and street == 0:
                if 2 * BIG_BLIND == min_raise:
                    return RaiseAction(min_raise)    
            if BidAction in legal_actions:
                return BidAction(int(random.random()*my_stack))
            if CheckAction in legal_actions:
                return CheckAction()
            return CallAction()
        
        else: # tight aggressive

            if self.strong_hole: # tight range
                if BidAction in legal_actions:
                    idx = random.randint(1, len(self.opp_bids))
                    bid = self.opp_bids[idx] + 1
                    bid = min(bid, my_stack)
                    return BidAction(bid)
                if RaiseAction in legal_actions:
                    raise_frac = 0.2 + random.random() * 0.2
                    raise_amount = int(min_raise + (max_raise - min_raise) * raise_frac)
                    return RaiseAction(raise_amount)
                if CallAction in legal_actions:
                    return CallAction()

            if CheckAction in legal_actions:
                return CheckAction()
            if BidAction in legal_actions:
                return BidAction(0)
            return FoldAction()



        




# PORT: contents of the original `hand_strengths` pickle
_HAND_STRENGTHS = {
    'AKo': (0.784, 0.474),
    'AQo': (0.757, 0.4825),
    'AJo': (0.765, 0.474),
    'ATo': (0.7595, 0.452),
    'A9o': (0.752, 0.4395),
    'A8o': (0.7345, 0.4305),
    'A7o': (0.6995, 0.4375),
    'A6o': (0.732, 0.4105),
    'A5o': (0.7215, 0.414),
    'A4o': (0.704, 0.3955),
    'A3o': (0.7195, 0.4005),
    'A2o': (0.708, 0.3805),
    'KQo': (0.752, 0.445),
    'KJo': (0.745, 0.4305),
    'KTo': (0.7565, 0.4355),
    'K9o': (0.75, 0.404),
    'K8o': (0.719, 0.3885),
    'K7o': (0.7, 0.4155),
    'K6o': (0.7075, 0.3865),
    'K5o': (0.6915, 0.3635),
    'K4o': (0.6695, 0.3595),
    'K3o': (0.669, 0.3285),
    'K2o': (0.676, 0.377),
    'QJo': (0.7385, 0.4295),
    'QTo': (0.727, 0.4015),
    'Q9o': (0.716, 0.3975),
    'Q8o': (0.7045, 0.387),
    'Q7o': (0.7235, 0.35),
    'Q6o': (0.6835, 0.3255),
    'Q5o': (0.702, 0.3365),
    'Q4o': (0.6635, 0.3575),
    'Q3o': (0.6305, 0.348),
    'Q2o': (0.6555, 0.347),
    'JTo': (0.7455, 0.4),
    'J9o': (0.7125, 0.3745),
    'J8o': (0.701, 0.359),
    'J7o': (0.6935, 0.3525),
    'J6o': (0.66, 0.361),
    'J5o': (0.6795, 0.3625),
    'J4o': (0.6505, 0.3355),
    'J3o': (0.6505, 0.316),
    'J2o': (0.6355, 0.276),
    'T9o': (0.687, 0.359),
    'T8o': (0.7035, 0.399),
    'T7o': (0.6995, 0.3305),
    'T6o': (0.677, 0.321),
    'T5o': (0.6235, 0.3005),
    'T4o': (0.629, 0.317),
    'T3o': (0.6375, 0.3095),
    'T2o': (0.6395, 0.2955),
    '98o': (0.684, 0.3375),
    '97o': (0.686, 0.354),
    '96o': (0.675, 0.313),
    '95o': (0.6245, 0.3),
    '94o': (0.625, 0.2685),
    '93o': (0.6445, 0.258),
    '92o': (0.627, 0.26),
    '87o': (0.662, 0.361),
    '86o': (0.657, 0.3265),
    '85o': (0.666, 0.3075),
    '84o': (0.6265, 0.295),
    '83o': (0.617, 0.2815),
    '82o': (0.625, 0.2505),
    '76o': (0.6535, 0.3305),
    '75o': (0.6515, 0.3175),
    '74o': (0.6445, 0.2815),
    '73o': (0.638, 0.283),
    '72o': (0.6115, 0.251),
    '65o': (0.654, 0.3125),
    '64o': (0.6085, 0.2985),
    '63o': (0.643, 0.2655),
    '62o': (0.5965, 0.2715),
    '54o': (0.6275, 0.275),
    '53o': (0.615, 0.2945),
    '52o': (0.6, 0.2505),
    '43o': (0.6285, 0.2585),
    '42o': (0.615, 0.2385),
    '32o': (0.6085, 0.2455),
    'AKs': (0.7405, 0.4385),
    'AQs': (0.741, 0.4625),
    'AJs': (0.7355, 0.459),
    'ATs': (0.7305, 0.4135),
    'A9s': (0.694, 0.3965),
    'A8s': (0.7005, 0.4065),
    'A7s': (0.731, 0.4005),
    'A6s': (0.677, 0.3885),
    'A5s': (0.7025, 0.378),
    'A4s': (0.6865, 0.384),
    'A3s': (0.6865, 0.3565),
    'A2s': (0.666, 0.344),
    'KQs': (0.707, 0.418),
    'KJs': (0.7405, 0.406),
    'KTs': (0.707, 0.4275),
    'K9s': (0.7125, 0.412),
    'K8s': (0.694, 0.372),
    'K7s': (0.675, 0.3595),
    'K6s': (0.678, 0.35),
    'K5s': (0.678, 0.36),
    'K4s': (0.631, 0.353),
    'K3s': (0.651, 0.322),
    'K2s': (0.6445, 0.3045),
    'QJs': (0.7155, 0.423),
    'QTs': (0.7305, 0.3915),
    'Q9s': (0.732, 0.387),
    'Q8s': (0.6855, 0.3575),
    'Q7s': (0.645, 0.3295),
    'Q6s': (0.686, 0.3165),
    'Q5s': (0.6705, 0.3085),
    'Q4s': (0.6245, 0.2795),
    'Q3s': (0.649, 0.317),
    'Q2s': (0.638, 0.308),
    'JTs': (0.7245, 0.3845),
    'J9s': (0.697, 0.33),
    'J8s': (0.6675, 0.332),
    'J7s': (0.6845, 0.349),
    'J6s': (0.6615, 0.29),
    'J5s': (0.641, 0.294),
    'J4s': (0.652, 0.311),
    'J3s': (0.6255, 0.2805),
    'J2s': (0.602, 0.2525),
    'T9s': (0.6765, 0.369),
    'T8s': (0.6355, 0.367),
    'T7s': (0.655, 0.3095),
    'T6s': (0.62, 0.2835),
    'T5s': (0.6645, 0.279),
    'T4s': (0.652, 0.2955),
    'T3s': (0.631, 0.249),
    'T2s': (0.6045, 0.2495),
    '98s': (0.673, 0.336),
    '97s': (0.6565, 0.3435),
    '96s': (0.638, 0.279),
    '95s': (0.624, 0.2715),
    '94s': (0.6355, 0.252),
    '93s': (0.593, 0.256),
    '92s': (0.6055, 0.2185),
    '87s': (0.6535, 0.31),
    '86s': (0.6095, 0.2775),
    '85s': (0.6485, 0.269),
    '84s': (0.606, 0.262),
    '83s': (0.582, 0.213),
    '82s': (0.5685, 0.223),
    '76s': (0.626, 0.2975),
    '75s': (0.646, 0.272),
    '74s': (0.613, 0.2575),
    '73s': (0.567, 0.229),
    '72s': (0.574, 0.216),
    '65s': (0.588, 0.251),
    '64s': (0.6055, 0.2655),
    '63s': (0.604, 0.2305),
    '62s': (0.593, 0.208),
    '54s': (0.609, 0.2625),
    '53s': (0.592, 0.2545),
    '52s': (0.612, 0.235),
    '43s': (0.548, 0.2115),
    '42s': (0.5535, 0.217),
    '32s': (0.564, 0.1995),
    'AAo': (0.8665, 0.6695),
    'KKo': (0.8425, 0.63),
    'QQo': (0.843, 0.6075),
    'JJo': (0.8135, 0.5895),
    'TTo': (0.803, 0.5915),
    '99o': (0.786, 0.5455),
    '88o': (0.762, 0.5195),
    '77o': (0.761, 0.4745),
    '66o': (0.747, 0.4695),
    '55o': (0.71, 0.426),
    '44o': (0.68, 0.3755),
    '33o': (0.657, 0.35),
    '22o': (0.619, 0.2955),
}


Agent = mit_agent(Player)  # PORT: replaces run_bot(Player(), parse_args())
