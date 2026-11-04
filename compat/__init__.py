"""Compatibility layers that let bots written for other frameworks (MIT Pokerbots,
PyPokerEngine, neuron_poker, RLCard, texasholdem, Fullhouse) run in this engine with their
strategy code unchanged. Each adapter translates our Observation into the data structures
the original bot expected, and its returned action back into ours.
"""
