"""MIT 2023 precompute

Ported from: https://github.com/mitpokerbots/reference-lecture-5-2023
Author: MIT Pokerbots
License: MIT (see the source repository)

Strategy: Precomputed preflop strength + pot odds.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `skeleton` imports -> `from compat.mit import *` (MIT skeleton emulation, see compat/mit.py)
  * `import eval7` -> `from compat import eval7` (same API, pure-Python evaluator)
  * `STARTING_STACK - my_stack` / `- opp_stack` (MIT reset stacks every hand) ->
    round_state.my_contribution / round_state.opp_contribution (chips in this hand's pot)
  * data files (pandas CSV / pickle) are embedded at the bottom of this file
  * the `run_bot(...)` entry point is replaced by `Agent = mit_agent(Player)`
"""
BOT_NAME = 'MIT 2023 precompute'
BOT_AUTHOR = 'MIT Pokerbots'
BOT_SOURCE = 'https://github.com/mitpokerbots/reference-lecture-5-2023'
BOT_DESCRIPTION = 'Precomputed preflop strength + pot odds.'
BOT_LICENSE = "MIT"

'''
Simple example pokerbot, written in Python.
'''
from compat.mit import *  # PORT: was the MIT `skeleton` imports

from compat import eval7  # PORT: was `import eval7`
import random
# PORT: `import pandas as pd` removed (CSV embedded below)


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
        
        holes, strengths = _read_hole_strengths()  # PORT: was a pandas read_csv('hole_strengths.csv') + column reads
        self.starting_strengths = dict(zip(holes, strengths)) #convert to a dictionary, O(1) lookup time!


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
        street = previous_state.street  # int of street representing when this round ended
        my_cards = previous_state.hands[active]  # your cards
        opp_cards = previous_state.hands[1-active]  # opponent's cards or [] if not revealed
        

    def hole_list_to_key(self, hole):
        '''
        Converts a hole card list into a key that we can use to query our 
        strength dictionary
        hole: list - A list of two card strings in the engine's format (Kd, As, Th, 7d, etc.)
        '''

        card_1 = hole[0] #get all of our relevant info
        card_2 = hole[1]

        rank_1, suit_1 = card_1[0], card_1[1] #card info
        rank_2, suit_2 = card_2[0], card_2[1]

        numeric_1, numeric_2 = self.rank_to_numeric(rank_1), self.rank_to_numeric(rank_2) #make numeric

        suited = suit_1 == suit_2 #off-suit or not
        suit_string = 's' if suited else 'o'

        if numeric_1 >= numeric_2: #keep our hole cards in rank order
                return rank_1 + rank_2 + suit_string
        else:
            return rank_2 + rank_1 + suit_string


    def rank_to_numeric(self, rank):
        '''
        Method that converts our given rank as a string
        into an integer ranking
        rank: str - one of 'A, K, Q, J, T, 9, 8, 7, 6, 5, 4, 3, 2'
        '''
        if rank.isnumeric(): #2-9, we can just use the int version of this string
            return int(rank)
        elif rank == 'T': #10 is T, so we need to specify it here
            return 10
        elif rank == 'J': #Face cards for the rest of them
            return 11
        elif rank == 'Q':
            return 12
        elif rank == 'K':
            return 13
        else: #Ace (A) is the only one left, give it the highest rank
            return 14


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
        street = round_state.street  # int representing pre-flop, flop, turn, or river respectively
        my_cards = round_state.hands[active]  # your cards
        board_cards = round_state.deck[:street]  # the board cards
        my_pip = round_state.pips[active]  # the number of chips you have contributed to the pot this round of betting
        opp_pip = round_state.pips[1-active]  # the number of chips your opponent has contributed to the pot this round of betting
        my_stack = round_state.stacks[active]  # the number of chips you have remaining
        opp_stack = round_state.stacks[1-active]  # the number of chips your opponent has remaining
        continue_cost = opp_pip - my_pip  # the number of chips needed to stay in the pot
        my_contribution = round_state.my_contribution  # PORT: was STARTING_STACK - my_stack  # the number of chips you have contributed to the pot
        opp_contribution = round_state.opp_contribution  # PORT: was STARTING_STACK - opp_stack  # the number of chips your opponent has contributed to the pot
        
        

        
        min_raise, max_raise = round_state.raise_bounds()  # the smallest and largest numbers of chips for a legal bet/raise
        my_action = None


        pot_total = my_contribution + opp_contribution

        if street < 3:
            raise_amount = int(my_pip + continue_cost + 0.4 * (pot_total + continue_cost))
        else:
            raise_amount = int(my_pip + continue_cost + 0.75 * (pot_total + continue_cost))

        
        raise_amount = max([min_raise, raise_amount])

        raise_cost = raise_amount - my_pip


        if (RaiseAction in legal_actions and (raise_cost <= my_stack)):
            temp_action = RaiseAction(raise_amount)

        elif (CallAction in legal_actions and (continue_cost <= my_stack)):
            temp_action = CallAction()

        elif CheckAction in legal_actions: 
            temp_action = CheckAction()
        else:
            temp_action = FoldAction()


        # MONTE_CARLO_ITERS = 100
        key = self.hole_list_to_key(my_cards)
        strength = self.starting_strengths[key]


        if continue_cost > 0:
            scary = 0
            
            if continue_cost > 6:
                scary = 0.15
            if continue_cost > 12:
                scary = 0.25
            if continue_cost > 50:
                scary = 0.35

            strength = max([0, strength - scary])

            pot_odds = continue_cost / (pot_total + continue_cost)

            if strength > pot_odds:
                if random.random() < strength and strength > 0.5:
                    my_action = temp_action

                else:
                    my_action = CallAction()

            else:
                my_action = FoldAction()

        else:
            if random.random() < strength:
                my_action = temp_action

            else:
                my_action = CheckAction()

        return my_action 




# PORT: contents of the original hole_strengths.csv
_HOLE_STRENGTHS_CSV = """Holes,Strengths
AKs,0.6678
AQs,0.6646
AJs,0.6409
ATs,0.64895
A9s,0.6275
A8s,0.61
A7s,0.6159
A6s,0.59625
A5s,0.60255
A4s,0.5969
A3s,0.5873
A2s,0.57515
KQs,0.6324
KJs,0.62645
KTs,0.61005
K9s,0.5992
K8s,0.58885
K7s,0.5815
K6s,0.5695
K5s,0.55735
K4s,0.5428
K3s,0.5356
K2s,0.53525
QJs,0.60695
QTs,0.59225
Q9s,0.5827
Q8s,0.5644
Q7s,0.5468
Q6s,0.54325
Q5s,0.5231
Q4s,0.5105
Q3s,0.50185
Q2s,0.5092
JTs,0.5745
J9s,0.56065
J8s,0.5446
J7s,0.5296
J6s,0.5031
J5s,0.4945
J4s,0.4925
J3s,0.4826
J2s,0.4785
T9s,0.53945
T8s,0.53055
T7s,0.5064
T6s,0.4927
T5s,0.46915
T4s,0.46645
T3s,0.4651
T2s,0.4432
98s,0.51025
97s,0.486
96s,0.47295
95s,0.45255
94s,0.4369
93s,0.43695
92s,0.42405
87s,0.4797
86s,0.4605
85s,0.44965
84s,0.42615
83s,0.4081
82s,0.4007
76s,0.4535
75s,0.4427
74s,0.42425
73s,0.3969
72s,0.38545
65s,0.4331
64s,0.4107
63s,0.40625
62s,0.3771
54s,0.41715
53s,0.3989
52s,0.38365
43s,0.38585
42s,0.36995
32s,0.3577
AKo,0.6615
AQo,0.63935
AJo,0.63215
ATo,0.6369
A9o,0.6052
A8o,0.58955
A7o,0.58495
A6o,0.57965
A5o,0.57555
A4o,0.56825
A3o,0.561
A2o,0.5468
KQo,0.61225
KJo,0.60565
KTo,0.58485
K9o,0.5796
K8o,0.55725
K7o,0.55635
K6o,0.53585
K5o,0.53475
K4o,0.52785
K3o,0.50945
K2o,0.4991
QJo,0.58255
QTo,0.57175
Q9o,0.556
Q8o,0.53775
Q7o,0.51935
Q6o,0.51145
Q5o,0.4926
Q4o,0.49215
Q3o,0.4735
Q2o,0.4743
JTo,0.5543
J9o,0.5315
J8o,0.5175
J7o,0.49385
J6o,0.4828
J5o,0.4739
J4o,0.46215
J3o,0.45365
J2o,0.44145
T9o,0.51645
T8o,0.49575
T7o,0.47465
T6o,0.4579
T5o,0.44775
T4o,0.4326
T3o,0.42695
T2o,0.4265
98o,0.4857
97o,0.45885
96o,0.4492
95o,0.42535
94o,0.39865
93o,0.4014
92o,0.40565
87o,0.4497
86o,0.4446
85o,0.4182
84o,0.3899
83o,0.3749
82o,0.36255
76o,0.4211
75o,0.40305
74o,0.3856
73o,0.36165
72o,0.34115
65o,0.4036
64o,0.3856
63o,0.36045
62o,0.3398
54o,0.3789
53o,0.3642
52o,0.34345
43o,0.3478
42o,0.3407
32o,0.32235
AAo,0.85025
KKo,0.8189
QQo,0.80455
JJo,0.77125
TTo,0.75055
99o,0.7095
88o,0.6855
77o,0.6537
66o,0.63685
55o,0.6063
44o,0.57735
33o,0.53335
22o,0.49625
"""


def _read_hole_strengths():
    rows = [r.split(',') for r in _HOLE_STRENGTHS_CSV.strip().splitlines()[1:]]
    return [r[0] for r in rows], [float(r[1]) for r in rows]


Agent = mit_agent(Player)  # PORT: replaces run_bot(Player(), parse_args())
