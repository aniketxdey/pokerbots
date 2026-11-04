"""Starter template for your own bot. Copy to bots/my_bot.py and fill in act().

This file is the interface only; it has no strategy. It checks when possible and otherwise folds.
"""
BOT_NAME = "My bot"
BOT_AUTHOR = "me"
BOT_DESCRIPTION = "Describe your strategy here."


class Agent:
    def __init__(self):
        # Called once per table. State resets at every table (official rules §3.8).
        pass

    def new_table(self, info):
        # info: seat, num_players, starting_stack, blind_schedule, blind_interval,
        #       max_hands, points, time_limit_ms
        pass

    def new_hand(self, obs):
        # Same Observation as act(). Keep it fast: it has the callback time limit.
        pass

    def act(self, obs):
        # obs.hole_cards ['As','Kd'], obs.board, obs.pot, obs.to_call, obs.my_stack, obs.my_bet,
        # obs.current_bet, obs.min_raise_to, obs.max_raise_to, obs.legal_actions,
        # obs.big_blind, obs.button, obs.players (PlayerView per seat), obs.history ...
        # obs.variant ('nlhe', 'plo', 'razz', ...), obs.game, obs.betting, obs.hand_rule, obs.split
        # Return 'fold' | 'check' | 'call' | 'all_in' | ('raise', total_bet_this_street)
        # You have obs.time_limit_ms (1 ms) - precompute tables at import time, not here.
        if obs.to_call == 0:
            return "check"
        return "fold"

    def discard(self, obs):
        # Only called in variants with discards/draws (Pineapple, Irish, draw games, 2026 Toss).
        # Return a list of your cards (strings) to throw away; obs.discard_count says how many.
        return []

    def bid(self, obs):
        # Only called in 2024 Auction Hold'em: chips you bid for an extra hole card.
        return 0

    def hand_over(self, result):
        # result.deltas, result.stacks, result.showdown {seat: cards}, result.winners, result.board
        pass
