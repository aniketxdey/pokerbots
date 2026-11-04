"""RLCard Limit Hold'em rule agent V1

Ported from: https://github.com/datamllab/rlcard/blob/master/rlcard/models/limitholdem_rule_models.py
Author: DATA Lab at Texas A&M (RLCard)
License: MIT

Strategy: Hand-written rules: raise pairs/strong aces/broadway preflop; raise made pairs and top hands postflop; fold low boards.

PORTING NOTES - only framework glue was changed; the strategy code below is the original:
  * rlcard imports removed (only LimitholdemRuleAgentV1 is used)
  * 'raise' = one fixed-limit bet (1 BB preflop/flop, 2 BB turn/river), legal actions follow rlcard
"""
BOT_NAME = "RLCard Limit Hold'em rule agent V1"
BOT_AUTHOR = 'DATA Lab at Texas A&M (RLCard)'
BOT_SOURCE = 'https://github.com/datamllab/rlcard/blob/master/rlcard/models/limitholdem_rule_models.py'
BOT_DESCRIPTION = 'Hand-written rules: raise pairs/strong aces/broadway preflop; raise made pairs and top hands postflop; fold low boards.'
BOT_LICENSE = 'MIT'

''' Limit Hold 'em rule model
'''
# PORT: `import rlcard` removed
from compat.others import rlcard_agent  # PORT
Model = object  # PORT: was rlcard.models.model.Model

class LimitholdemRuleAgentV1(object):
    ''' Limit Hold 'em Rule agent version 1
    '''

    def __init__(self):
        self.use_raw = True

    @staticmethod
    def step(state):
        ''' Predict the action when given raw state. A simple rule-based AI.
        Args:
            state (dict): Raw state from the game

        Returns:
            action (str): Predicted action
        '''
        legal_actions = state['raw_legal_actions']
        state = state['raw_obs']
        hand = state['hand']
        public_cards = state['public_cards']
        action = 'fold'
        # When having only 2 hand cards at the game start, choose fold to drop terrible cards:
        # Acceptable hand cards:
        # Pairs
        # AK, AQ, AJ, AT
        # A9s, A8s, ... A2s(s means flush)
        # KQ, KJ, QJ, JT
        # Fold all hand types except those mentioned above to save money
        if len(public_cards) == 0:
            if hand[0][1] == hand [1][1]:
                action = 'raise'
            elif hand[0][1] == 'A' or hand[1][1] == 'A':
                if 'K' in [hand[0][1], hand[1][1]] or 'Q' in [hand[0][1], hand[1][1]] or 'J' in [hand[0][1], hand[1][1]] or 'T' in [hand[0][1], hand[1][1]]:
                    action = 'raise'
                elif hand[0][0] == hand[1][0]:
                    action = 'raise'
            elif hand[0][1] == 'K' or hand[0][1] == 'Q' or hand[0][1] == 'J' or hand[0][1] == 'T':
                if hand[1][1] == 'K' or hand[1][1] == 'Q' or hand[1][1] == 'J' or hand[1][1] == 'T':
                    action = 'raise'
        if len(public_cards) == 3:
            public_cards_ranks = ['A', 'A', 'A']
            public_cards_flush = ['S', 'S', 'S']
            for i, _ in enumerate(public_cards):
                public_cards_ranks[i] = public_cards[i][1]
                public_cards_flush[i] = public_cards[i][0]
            if hand[0][1] == hand [1][1]:
            # if the player already have a pair, raise when public cards have card same as the pair
                if hand[0][1] in public_cards_ranks:
                    action = 'raise'
            elif hand[0][1] == 'A' or hand[1][1] == 'A':
                if 'K' in [hand[0][1], hand[1][1]] or 'Q' in [hand[0][1], hand[1][1]] or 'J' in [hand[0][1], hand[1][1]] or 'T' in [hand[0][1], hand[1][1]]:
                    # For AK, AQ, AJ, AT types, if public cards have A, K, Q, J, T, raise, because the chance of getting a straight greatly increases
                    if 'A' in public_cards_ranks or 'K' in public_cards_ranks or 'Q' in public_cards_ranks or 'J' in public_cards_ranks or 'T' in public_cards_ranks:
                        action = 'raise'
                # For A9s, A8s, ... A2s types, if public cards have same flush as the hand cards, raise, because the chance of getting a flush greatly increases
                elif hand[0][0] == hand[1][0]:
                    if hand[0][0] in public_cards_flush:
                        action = 'raise'
            elif max(public_cards_ranks) in ['5', '4' ,'3', '2']: # for KQ, KJ, QJ, JT, check when having no cards higher than 5
                action = 'check'
            else:
                action = 'call'

        if len(public_cards) == 5 or len(public_cards) == 4 :
            public_cards_ranks = []
            public_cards_flush = []
            for i, _ in enumerate(public_cards):
                public_cards_ranks.append('A')
                public_cards_flush.append('S')
                public_cards_ranks[i] = public_cards[i][1]
                public_cards_flush[i] = public_cards[i][0]
            if hand[0][1] == hand [1][1]:
            # if the player already have a pair, raise when public cards have card same as the pair
                if hand[0][1] in public_cards_ranks:
                    action = 'raise'
            elif hand[0][1] == 'A' or hand[1][1] == 'A':
                if 'K' in [hand[0][1], hand[1][1]] or 'Q' in [hand[0][1], hand[1][1]] or 'J' in [hand[0][1], hand[1][1]] or 'T' in [hand[0][1], hand[1][1]]:
                    # For AK, AQ, AJ, AT types, if public cards have A, K, Q, J, T, raise, because the chance of getting a straight greatly increases
                    if 'A' in public_cards_ranks or 'K' in public_cards_ranks or 'Q' in public_cards_ranks or 'J' in public_cards_ranks or 'T' in public_cards_ranks:
                        action = 'raise'
                    # For A9s, A8s, ... A2s types, if public cards have same flush as the hand cards, raise, because the chance of getting a flush greatly increases
                elif hand[0][0] == hand[1][0]:
                    if hand[0][0] in public_cards_flush:
                        action = 'raise'
            elif max(public_cards_ranks) in ['5', '4', '3', '2']: # for KQ, KJ, QJ, JT, fold when having no cards higher than 5
                action = 'fold'
            else:
                action = 'call'

        #return action
        if action in legal_actions:
            return action
        else:
            if action == 'raise':
                return 'call'
            if action == 'check':
                return 'fold'
            if action == 'call':
                return 'raise'
            else:
                return action

    def eval_step(self, state):
        ''' Step for evaluation. The same to step
        '''
        return self.step(state), []

# PORT: the rest of the original file (RLCard model registry wrapper) is omitted


Agent = rlcard_agent(LimitholdemRuleAgentV1)  # PORT
