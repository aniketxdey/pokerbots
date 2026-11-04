"""ngnnah v5 strength

Ported from: https://github.com/ngnnah/python_AutonomousPokerbot/tree/main/python_strength_pairStraightFlush_odd_v5
Author: ngnnah (MIT Pokerbots 2021 entrant)
License: none stated in the source repo (code derived from MIT Pokerbots 2021 reference bots, MIT license)

Strategy: Tuned pot-odds bot: precomputed strength, 45%/84% pot raises, threat discount 0.12.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * `skeleton` imports -> `from compat.mit import *` (MIT skeleton emulation, see compat/mit.py)
  * `import eval7` -> `from compat import eval7` (same API, pure-Python evaluator)
  * `STARTING_STACK - my_stack` / `- opp_stack` (MIT reset stacks every hand) ->
    round_state.my_contribution / round_state.opp_contribution (chips in this hand's pot)
  * data files (pandas CSV / pickle) are embedded at the bottom of this file
  * the `run_bot(...)` entry point is replaced by `Agent = mit_agent(Player)`
"""
BOT_NAME = 'ngnnah v5 strength'
BOT_AUTHOR = 'ngnnah (MIT Pokerbots 2021 entrant)'
BOT_SOURCE = 'https://github.com/ngnnah/python_AutonomousPokerbot/tree/main/python_strength_pairStraightFlush_odd_v5'
BOT_DESCRIPTION = 'Tuned pot-odds bot: precomputed strength, 45%/84% pot raises, threat discount 0.12.'
BOT_LICENSE = "none stated (derived from MIT-licensed MIT Pokerbots code)"

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
        self.board_allocation = [[], [], []] # keep track of card allocations at round start
        self.hole_strengths = [0, 0 , 0]
        self.MONTE_CARLO_ITERS = 100

        holes, strengths = _read_hole_strengths()  # PORT: was a pandas read_csv('hole_strengths.csv') + column reads
        self.starting_strengths = dict(zip(holes, strengths))

    def hole_list_to_key(self, hole):
        card1, card2 = hole[0], hole[1]
        rank1, suit1 = card1[0], card1[1]
        rank2, suit2 = card2[0], card2[1]

        numeric1, numeric2 = self.rank_to_numeric(rank1), self.rank_to_numeric(rank2)
        suited = suit1 == suit2
        suit_string = 's' if suited else 'o'
        if numeric1 >= numeric2:
            return rank1 + rank2 + suit_string
        else:
            return rank2 + rank1 + suit_string

    def rank_to_numeric(self, rank):
        if rank.isnumeric(): # 2-9
            return int(rank)
        else:
            return [10, 11, 12, 13, 14]['TJQKA'.index(rank)]

    def sort_cards_by_rank(self, cards):
        return sorted(cards, reverse=True, key= lambda x: self.rank_to_numeric(x[0]))
   
    def allocate_cards(self, my_cards):
        # TODO TODO: consider all 6 permutations and allocate, based on precomputed strength 
        # pair-first approach
        ranks = {}
        for card in my_cards:
            card_rank = card[0] # 2-9,T,J,Q,K,A
            # card_suit = card[1] # d,h,s,c
            if card_rank in ranks:
                ranks[card_rank].append(card)
            else:
                ranks[card_rank] = [card]
        
        pairs = []
        singles = []
        for rank in ranks:
            cards = ranks[rank]
            if len(cards) == 1:
                singles.append(cards[0])
            elif len(cards) == 2 or len(cards) == 4:
                pairs += cards
            else: # 3 cards of same rank
                pairs.append(cards[0])
                pairs.append(cards[1])
                singles.append(cards[2])
        
        cards_remaining = set(my_cards) 
        allocated_cards = set()
        holes_allocated = []
        
        _MIN_PAIR_VALUE = 3 # TODO: tune, only want pairs stronger than this value
        for i in range(len(pairs)//2):
            pair = [pairs[2*i], pairs[2*i+1]]
            pair_rank= pair[0][0]
            if self.rank_to_numeric(pair_rank) >= _MIN_PAIR_VALUE:
                holes_allocated.append(pair)
                allocated_cards.update(pair)

        cards_remaining = cards_remaining - allocated_cards
        sorted_remaining = self.sort_cards_by_rank(list(cards_remaining))
        # ADDITION: look for straight cards
        for i in range(len(sorted_remaining) - 1): 
            card_1 = sorted_remaining[i]
            card_2 = sorted_remaining[i+1]
            rank_diff = abs(self.rank_to_numeric(card_1[0]) - self.rank_to_numeric(card_2[0]))
            if (rank_diff <= 1) and (card_1 not in allocated_cards) and (card_2 not in allocated_cards):
                hole = [card_1, card_2]
                holes_allocated.append(hole)
                allocated_cards.update(hole)

        cards_remaining = cards_remaining - allocated_cards
        suits = {}
        # ADDITION: look for flush cards
        for card in cards_remaining: 
            card_suit = card[1]
            if card_suit in suits:
                suits[card_suit].append(card)
            else:
                suits[card_suit] = [card]
        for suit, cards in suits.items():
            if len(cards) in {2, 3}:
                hole = [cards[0], cards[1]]
                holes_allocated.append(hole)
                allocated_cards.update(hole)
            elif len(cards) == 4: # be wary
                a,b,c,d = cards
                hole_1 = [a,b]
                hole_2 = [c,d]
                holes_allocated.append(hole_1)
                allocated_cards.update(hole_1)
                holes_allocated.append(hole_2)
                allocated_cards.update(hole_2)

        cards_remaining = cards_remaining - allocated_cards
        extra_cards = list(cards_remaining)
        for i in range(len(extra_cards) //2):
            hole = [extra_cards[2*i], extra_cards[2*i+1]]
            holes_allocated.append(hole)
            allocated_cards.update(hole)

        cards_remaining = cards_remaining - allocated_cards
        assert len(holes_allocated) == 3, "we allocated too many cards"
        assert len(cards_remaining) == 0, "we have yet allocated all cards"
        return holes_allocated

    # randomizing hole_cards to board
    def assign_holes(self, hole_cards):
        holes_and_strengths = [] 
        for hole in hole_cards:
            key = self.hole_list_to_key(hole)
            strength = self.starting_strengths[key]
            holes_and_strengths.append((hole, strength))
        
        holes_and_strengths = sorted(holes_and_strengths, key = lambda x: x[1])
        # TODO: tune, some random swaps
        board_A, board_B, board_C = holes_and_strengths
        if random.random() < .10: # swap best with second
            holes_and_strengths = [board_A, board_C, board_B]
        board_A, board_B, board_C = holes_and_strengths
        if random.random() < .12: # swap second with worst
            holes_and_strengths = [board_B, board_A, board_C]

        for i in range(NUM_BOARDS):
            self.board_allocation[i] = holes_and_strengths[i][0]
            self.hole_strengths[i] = holes_and_strengths[i][1]

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
        opp_bankroll = game_state.opp_bankroll # ^but for your opponent
        game_clock = game_state.game_clock  # the total number of seconds your bot has left to play this game
        round_num = game_state.round_num  # the round number from 1 to NUM_ROUNDS
        my_cards = round_state.hands[active]  # your six cards at the start of the round
        big_blind = bool(active)  # True if you are the big blind
        
        # PORT: Blotto Hold'em split 6 hole cards across 3 boards (allocate_cards +
        # assign_holes). Hold'em has 2 hole cards and one board, so the single board
        # simply gets both cards and their precomputed strength.
        self.board_allocation[0] = list(my_cards)
        self.hole_strengths[0] = self.starting_strengths[self.hole_list_to_key(my_cards)]

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
        opp_delta = terminal_state.deltas[1-active] # your opponent's bankroll change from this round 
        previous_state = terminal_state.previous_state  # RoundState before payoffs
        street = previous_state.street  # 0, 3, 4, or 5 representing when this round ended
        for terminal_board_state in previous_state.board_states:
            previous_board_state = terminal_board_state.previous_state
            my_cards = previous_board_state.hands[active]  # your cards
            opp_cards = previous_board_state.hands[1-active]  # opponent's cards or [] if not revealed
        
        self.board_allocation = [[], [], []] 
        self.hole_strengths = [0, 0 , 0]

        game_clock = game_state.game_clock
        round_num = game_state.round_num
        if round_num == NUM_ROUNDS:
            print("time remaining ", game_clock)


    def get_actions(self, game_state, round_state, active):
        '''
        Where the magic happens - your code should implement this function.
        Called any time the engine needs a triplet of actions from your bot.

        Arguments:
        game_state: the GameState object.
        round_state: the RoundState object.
        active: your player's index.

        Returns:
        Your actions.
        '''
        legal_actions = round_state.legal_actions()  # the actions you are allowed to take
        street = round_state.street  # 0, 3, 4, or 5 representing pre-flop, flop, turn, or river respectively
        my_cards = round_state.hands[active]  # your cards across all boards
        board_cards = [board_state.deck if isinstance(board_state, BoardState) else board_state.previous_state.deck \
            for board_state in round_state.board_states] #the board cards
        my_pips = [board_state.pips[active] if isinstance(board_state, BoardState) else 0 \
            for board_state in round_state.board_states] # the number of chips you have contributed to the pot on each board this round of betting
        opp_pips = [board_state.pips[1-active] if isinstance(board_state, BoardState) else 0 for board_state in round_state.board_states] # the number of chips your opponent has contributed to the pot on each board this round of betting
        continue_cost = [opp_pips[i] - my_pips[i] for i in range(NUM_BOARDS)] #the number of chips needed to stay in each board's pot
        my_stack = round_state.stacks[active]  # the number of chips you have remaining
        opp_stack = round_state.stacks[1-active]  # the number of chips your opponent has remaining
        stacks = [my_stack, opp_stack]
        net_upper_raise_bound = round_state.raise_bounds()[1] # max raise across 3 boards
        net_cost = 0 # keep track of the net additional amount you are spending across boards this round
        
        my_actions = [None] * NUM_BOARDS
        for i in range(NUM_BOARDS):
            if AssignAction in legal_actions[i]:
                cards = self.board_allocation[i]
                my_actions[i] = AssignAction(cards)
            elif isinstance(round_state.board_states[i], TerminalState):
                my_actions[i] = CheckAction()
            else: # do we add more resources?
                board_cont_cost = continue_cost[i]
                board_total = round_state.board_states[i].pot
                pot_total = my_pips[i] + opp_pips[i] + board_total
                min_raise, max_raise = round_state.board_states[i].raise_bounds(active, round_state.stacks)
                # TODO TODO: use `street` (0,3,4,5) to get more accurate strength from new cards (turn and river), not just from hole cards
                strength = self.hole_strengths[i] 
                
                if street < 3: # in pre-flop
                    ## TODO: tune bet proportions
                    raise_amount = int(my_pips[i] + board_cont_cost + 0.45 * (pot_total + board_cont_cost))
                else: # turn/ river, deeper in games with stronger hand
                    raise_amount = int(my_pips[i] + board_cont_cost + 0.84 * (pot_total + board_cont_cost))
                
                raise_amount = max(min_raise, raise_amount)
                raise_amount = min(max_raise, raise_amount)

                raise_cost = raise_amount - my_pips[i]
                if RaiseAction in legal_actions[i] and (raise_cost <= my_stack - net_cost):
                    commit_action = RaiseAction(raise_amount)
                    commit_cost = raise_cost
                elif CallAction in legal_actions[i]:
                    commit_action = CallAction()
                    commit_cost = board_cont_cost
                else:
                    commit_action = CheckAction()
                    commit_cost = 0
                
                if board_cont_cost > 0: # our opp raised!
                    ## TODO: tune params
                    if board_cont_cost > 5:
                        _THREAT = 0.12
                        strength = max(0, strength - _THREAT)
                    
                    pot_odds = board_cont_cost / (pot_total + board_cont_cost)
                    if strength >= pot_odds: # +ExpValue: at least call
                        if strength > 0.55 and strength> random.random():
                            my_actions[i] = commit_action
                            net_cost += commit_cost
                        else:
                            my_actions[i] = CallAction()
                            net_cost += board_cont_cost
                    else: # -EV: FOLD!
                        my_actions[i] = FoldAction()
                        net_cost += 0
                else: # board_cont_cost == 0
                    if strength > random.random():
                        my_actions[i] = commit_action
                        net_cost += commit_cost
                    else: 
                        my_actions[i] = CheckAction()
                        net_cost += 0
                    
        return my_actions




# PORT: contents of the original hole_strengths.csv
_HOLE_STRENGTHS_CSV = """Holes,Strengths
AKs,0.66993
AQs,0.66145
AJs,0.65418
ATs,0.6457
A9s,0.62834
A8s,0.6187
A7s,0.60947
A6s,0.60033
A5s,0.59963
A4s,0.58992
A3s,0.58466
A2s,0.57302
KQs,0.63317
KJs,0.62571
KTs,0.61886
K9s,0.60073
K8s,0.58197
K7s,0.5749
K6s,0.56534
K5s,0.55833
K4s,0.54987
K3s,0.54089
K2s,0.52984
QJs,0.60354
QTs,0.5951
Q9s,0.5763
Q8s,0.56034
Q7s,0.54415
Q6s,0.53442
Q5s,0.52766
Q4s,0.51953
Q3s,0.51016
Q2s,0.50226
JTs,0.57646
J9s,0.55744
J8s,0.54028
J7s,0.52217
J6s,0.50708
J5s,0.49994
J4s,0.49127
J3s,0.48223
J2s,0.47421
T9s,0.5409
T8s,0.52321
T7s,0.5053
T6s,0.48783
T5s,0.47256
T4s,0.46507
T3s,0.45768
T2s,0.4487
98s,0.50818
97s,0.49017
96s,0.47439
95s,0.45867
94s,0.43717
93s,0.43308
92s,0.42366
87s,0.47897
86s,0.46173
85s,0.44671
84s,0.42673
83s,0.40868
82s,0.40355
76s,0.45335
75s,0.43591
74s,0.41858
73s,0.4017
72s,0.37986
65s,0.43209
64s,0.41371
63s,0.39738
62s,0.3762
54s,0.41333
53s,0.39694
52s,0.37734
43s,0.38611
42s,0.3679
32s,0.35926
AKo,0.65174
AQo,0.64383
AJo,0.63489
ATo,0.62803
A9o,0.60786
A8o,0.5974
A7o,0.58878
A6o,0.5765
A5o,0.57624
A4o,0.56668
A3o,0.55976
A2o,0.55002
KQo,0.61626
KJo,0.60585
KTo,0.59674
K9o,0.57797
K8o,0.56004
K7o,0.55198
K6o,0.54229
K5o,0.53234
K4o,0.52205
K3o,0.51538
K2o,0.50519
QJo,0.58148
QTo,0.57263
Q9o,0.55349
Q8o,0.53498
Q7o,0.51764
Q6o,0.50966
Q5o,0.50196
Q4o,0.49233
Q3o,0.48084
Q2o,0.47423
JTo,0.55303
J9o,0.53334
J8o,0.51564
J7o,0.4956
J6o,0.47816
J5o,0.47223
J4o,0.46165
J3o,0.45166
J2o,0.44233
T9o,0.51463
T8o,0.49717
T7o,0.47979
T6o,0.46128
T5o,0.44355
T4o,0.43443
T3o,0.42621
T2o,0.41728
98o,0.48209
97o,0.46277
96o,0.44595
95o,0.42893
94o,0.40547
93o,0.40116
92o,0.3899
87o,0.45072
86o,0.43135
85o,0.41389
84o,0.39449
83o,0.37494
82o,0.36777
76o,0.42418
75o,0.40515
74o,0.38622
73o,0.3668
72o,0.34527
65o,0.39866
64o,0.38055
63o,0.35922
62o,0.33982
54o,0.38309
53o,0.36233
52o,0.3429
43o,0.35102
42o,0.33101
32o,0.32244
AAo,0.85221
KKo,0.82346
QQo,0.79855
JJo,0.7737
TTo,0.75088
99o,0.72047
88o,0.69058
77o,0.66255
66o,0.6319
55o,0.60444
44o,0.57062
33o,0.53797
22o,0.50246
"""


def _read_hole_strengths():
    rows = [r.split(',') for r in _HOLE_STRENGTHS_CSV.strip().splitlines()[1:]]
    return [r[0] for r in rows], [float(r[1]) for r in rows]


Agent = mit2021_agent(Player)  # PORT: replaces run_bot(Player(), parse_args()); one board
