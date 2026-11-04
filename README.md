# No Limit — mock pokerbot tournament engine

A local mock of the Sequoia × OpenAI × Citadel Securities **"No Limit"** pokerbot tournament
([rules](https://pokerbot.sequoiacap.com/rules.html)): a No-Limit Hold'em sit-and-go engine,
a tournament runner that follows the published scoring rules, a web UI for setting things up
and running tournaments, and **42 open-source bots** ported from MIT Pokerbots and other
public projects to practice against.

```bash
pip install -r requirements.txt
python server.py                      # UI at http://127.0.0.1:5000
python run_tournament.py --iterations 200          # or from the command line
python tests/test_engine.py           # engine tests
```

Python 3.9+. The engine itself is pure standard library (Flask is only for the UI). Runs on
Linux and macOS; on Windows the hard 1 ms cutoff degrades to "soft" (see below).

## How closely it follows the official rules

| Official rule | Here |
|---|---|
| Python bot implementing an Agent interface (§3.3) | one `.py` per bot in `bots/`, defining `class Agent` with `act(obs)` (see below) |
| Bots run sandboxed **without network access** (§3.6) | sockets are disabled in the worker processes |
| **1 ms per decision**; on error or timeout the bot **checks if it can, otherwise folds** (§3.7) | `hard` mode: the call is cut off at the limit with `SIGALRM` and the bot auto-checks or folds. Exceptions do the same |
| 100 MB maximum file size (§3.7) | enforced on upload and when loading bots |
| Bot state resets at the start of each table; opponents are anonymized (§3.8, §3.9) | each table gets a fresh module and a fresh `Agent()`; bots only see seat numbers |
| Six scored tournaments, 20 minutes apart (§4.1) | 6 rounds back-to-back, or **live mode**: set "Live gap" to 20 |
| The active bot is frozen at the start of each tournament (§4.2) | bot files are re-read at the start of every round, so you can edit bots between rounds (best with live mode) |
| 10,000 eight-bot sit-and-gos, bots randomly assigned to tables (§4.3) | 10,000 iterations by default; each iteration shuffles every bot onto 8-seat tables |
| A table ends when one bot has all the chips or after 200 hands; **2,000 chips; blinds 10/20, up every 25 hands** (§4.4) | exactly that |
| Points by finishing position; at the 200-hand cap the remaining points go to survivors by **ICM** (§4.5) | Malmuth-Harville ICM on the survivors' stacks over the remaining places |
| Bot count not divisible by 8: filler seats are random copies that earn no points (§4.6) | same |
| Score = average points per table; weights **T1×1 … T6×6** (§4.7) | same. The leaderboard shows Σ weight × average points |
| Unscored practice rounds during the build | "Practice round" checkbox / `--practice` |

### Things the rules don't specify (configurable assumptions)

- **The size of each blind increase.** Default levels, one every 25 hands: 10/20, 15/30, 25/50,
  40/80, 60/120, 100/200, 150/300, 250/500. Antes are supported but off.
- **The point distribution.** It's announced at the start of the build phase. The default is
  50/30/20 for 1st/2nd/3rd. Presets include linear, winner-take-all and a steeper curve, or you
  can type your own.
- **The poker variant** is revealed on the day, and the Finals use a different one. This engine
  plays standard No-Limit Hold'em.
- **What "iteration" means.** The engine reads it as "seat every bot once", which is consistent
  with the filler-seat rule. Each bot therefore plays one table per iteration.
- **Callbacks other than `act`** (`new_table`, `new_hand`, `hand_over`) have their own time
  limit, also 1 ms by default. This matters for bots that do heavy work at the start of a hand.
- **Timing noise.** A 1 ms budget is tight in Python. Running more workers than you have cores
  (or other load on the machine) adds timeouts. Leave a core free for accurate timing.
- **Odd chips and showdowns.** Odd chips go to the first winner left of the button. Everyone
  still in the hand at showdown shows their cards to all bots.
- **Same-hand busts.** Players who bust on the same hand are ranked by their stack at the start
  of that hand. Equal stacks split the places.

### Throughput

With this 42-bot field, one 8-bot table (up to 200 hands) takes about 0.3–0.6 s of CPU in
`hard` mode. Most of that is Monte Carlo bots burning their full 1 ms before being cut off. A
full official-size run is 10,000 iterations × 6 tables × 6 rounds ≈ 360k tables, which is tens
of CPU-hours. For iterating on a bot, **100–500 iterations** per round already ranks bots
clearly. With the 50/30/20 points the standard error of a bot's average is roughly
20/√iterations points.

## The UI

- **Settings:** every rule above, plus an "Official rules" reset, workers, seed, live-mode gap
  and practice mode.
- **Bots:** pick any subset (up to 64), upload new `.py` files, and see each bot's source link.
- **Status:** a live progress bar with ETA, plus a picker for past runs (saved to `results/*.json`).
- **Leaderboard:** sortable, with weighted score, per-round averages, win rate, timeout rate and
  errors.
- **Round details:** average points and place, win and top-3 rate, hands survived, decision
  times (average and max), timeouts, crashes (with the first error message) and repaired illegal
  actions. Each round also includes a full hand log of one sample table.

## Writing a bot

Copy `templates/agent_template.py` into `bots/`. Here is the interface (`engine/api.py` has the
details):

```python
class Agent:
    def __init__(self): ...            # once per table
    def act(self, obs):                # required
        # obs.hole_cards ['As','Kd'], obs.board, obs.pot (incl. current bets), obs.to_call,
        # obs.my_stack, obs.my_bet, obs.current_bet, obs.min_raise_to, obs.max_raise_to,
        # obs.legal_actions, obs.big_blind, obs.button, obs.players[seat].stack/.bet/.folded,
        # obs.history [(seat, action, amount_to, street)], obs.hands_until_blinds_up, ...
        return ("raise", obs.min_raise_to)   # or 'fold' | 'check' | 'call' | 'all_in'
    # optional: new_table(info), new_hand(obs), hand_over(result)
```

Raise amounts are "raise to" totals for the current street. Illegal actions are repaired: an
illegal check becomes a fold, a call with nothing to call becomes a check, and raise amounts
are clamped into the legal range. The UI counts these repairs. `engine.evaluator.evaluate`
(about 1.2 µs per 7-card hand) and `compat.eval7` are available to bots.

## Included bots (42)

Every bot is a real, published bot. The strategy code is the original. Only the glue to its
original framework was changed, and each file starts with a `PORTING NOTES` block listing
exactly what changed. The `compat/` package emulates each original framework:

- **`compat/mit.py`** emulates the MIT Pokerbots skeleton (2020–2026). These are heads-up bots,
  so "opponent pip" means the highest bet on the street. Chip amounts are rescaled so the big
  blind is always 2, as in the MIT engine, which keeps thresholds like `continue_cost > 6`
  meaning "3 big blinds". Variant-only mechanics (2021 Blotto boards, 2024 auctions, 2025
  bounties, 2026 discards) don't exist in Hold'em, so those bots play their betting logic on a
  single board.
- **`compat/eval7.py`** is a pure-Python version of the eval7 API.
- **`compat/pypokerengine.py`** emulates PyPokerEngine. As in PyPokerEngine, an invalid raise
  is treated as a fold.
- **`compat/others.py`** covers neuron_poker (including its 1000-run equity computation),
  RLCard Limit Hold'em (where a raise is one fixed-limit bet), texasholdem, and the Fullhouse
  `game_state` dict.

Several of these bots run Monte Carlo simulations and can't finish within 1 ms, so under the
official limit they mostly auto-check or fold. Set the time limit higher, or the mode to
`off`, to see them play as their authors intended.

| File | Bot | Source | Strategy |
|---|---|---|---|
| `fullhouse_aggressor.py` | The Aggressor | [Fullhouse Hackathon](https://github.com/uzlez/fullhouse-engine/blob/main/bots/aggressor/bot.py) | Raises 2-4x the min raise 70% of the time, otherwise check/call. |
| `fullhouse_mathematician.py` | The Mathematician | [Fullhouse Hackathon](https://github.com/uzlez/fullhouse-engine/blob/main/bots/mathematician/bot.py) | Never bets; calls when the pot lays 3:1 or better, else folds. |
| `fullhouse_ref_bot_2.py` | Fullhouse ref_bot_2 | [Fullhouse Hackathon](https://github.com/uzlez/fullhouse-engine/blob/main/bots/ref_bot_2/bot.py) | Reference bot: checks when possible, calls at 3:1 pot odds, else folds. |
| `fullhouse_shark.py` | The Shark | [Fullhouse Hackathon](https://github.com/uzlez/fullhouse-engine/blob/main/bots/shark/bot.py) | Tight preflop (premium raises 3x), position-aware postflop stabs and pot-fraction calls. |
| `fullhouse_template.py` | Fullhouse template | [Fullhouse Hackathon](https://github.com/uzlez/fullhouse-engine/blob/main/bots/template/bot.py) | Starter template: raises AA/KK to 3x pot, calls cheap bets, else folds. |
| `mit2020_card_winrate.py` | MIT 2020 L1 card win-rate | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-1-2020) | Learns per-rank showdown win rates; min-raises when both hole ranks win >50%. |
| `mit2020_skeleton_check_call.py` | MIT 2020 skeleton (check/call) | [MIT Pokerbots](https://github.com/mitpokerbots/engine-2020) | Official 2020-2023 skeleton bot: always checks, otherwise calls. |
| `mit2020_strategic.py` | MIT 2020 L2 strategic | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2020) | Rank win-rate learning plus board-pairing aggression with 75%-pot raises. |
| `mit2021_pot_odds_pair_hunt.py` | MIT 2021 pot-odds pair hunt | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2021) | 100-iteration Monte Carlo preflop strength, pot odds with 'intimidation' discount, 40%/75% pot raises. (Blotto-variant bot reduced to one board.) |
| `mit2021_precompute.py` | MIT 2021 precompute | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-4-2021) | Same pot-odds strategy with a precomputed 169-hand preflop strength table. (Blotto-variant bot reduced to one board.) |
| `mit2022_lecture2.py` | MIT 2022 lecture 2 | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2022) | Monte Carlo preflop strength (100 iters) every decision, 'scary bet' discounts, pot odds. |
| `mit2022_optimized.py` | MIT 2022 optimized | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-3-2022) | Pot-odds bot whose Monte Carlo uses the actual board after the flop. |
| `mit2022_pair_all_in.py` | MIT 2022 pair all-in | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-1-2022) | Shoves with any pocket pair, otherwise check/call. |
| `mit2022_pot_odds.py` | MIT 2022 pot odds | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2022) | Pot-odds bot with different scary-bet thresholds. |
| `mit2022_precompute.py` | MIT 2022 precompute | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-4-2022) | Pot-odds bot driven by a precomputed preflop strength table. |
| `mit2022_python_all_in.py` | MIT 2022 python all-in | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2022) | Raises big with pocket pairs (raise-to = max - pip, as in the original), otherwise check/call. |
| `mit2023_monte_carlo.py` | MIT 2023 Monte Carlo | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2023) | Monte Carlo preflop strength + pot odds. |
| `mit2023_optimized.py` | MIT 2023 optimized | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-3-2023) | Board-aware Monte Carlo strength + pot odds. |
| `mit2023_precompute.py` | MIT 2023 precompute | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-5-2023) | Precomputed preflop strength + pot odds. |
| `mit2024_exploit_tag.py` | MIT 2024 exploit tight-aggressive | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-3-2024) | Plays loose early, then evaluates hands against opponents' previously shown hole cards. |
| `mit2024_muscle.py` | MIT 2024 muscle | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-3-2024) | Pairs/broadway hands raise 10% of the raise range; weak hands fold to bets. |
| `mit2024_pot_odds_l2.py` | MIT 2024 pot odds (L2) | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-2-2024) | Monte Carlo strength, folds non-strong holdings (even when it could check), pot odds. |
| `mit2024_pot_odds_l3.py` | MIT 2024 pot odds (L3) | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-3-2024) | Lecture-3 revision of the 2024 pot-odds bot. |
| `mit2024_precompute.py` | MIT 2024 precompute | [MIT Pokerbots](https://github.com/mitpokerbots/reference-lecture-4-2024) | Precomputed strengths compared to the average strength of opponents' shown hands. |
| `mit2024_skeleton_max_raise.py` | MIT 2024 skeleton (max raise) | [MIT Pokerbots](https://github.com/mitpokerbots/engine-2024) | Official 2024 skeleton: raises the maximum 99% of the time it can, else check/call. |
| `mit2025_pot_odds.py` | MIT 2025 pot odds | [MIT Pokerbots](https://github.com/mitpokerbots/reference-3-2025) | Board-aware Monte Carlo win rate vs pot odds; mixes min and 10% raises. |
| `mit2025_skeleton_random.py` | MIT 2025 skeleton (random) | [MIT Pokerbots](https://github.com/mitpokerbots/engine-2025) | Official 2025/2026 skeleton: min-raises 50% of the time, folds 25% when facing a bet, else check/call. |
| `mit2025_strong_hole.py` | MIT 2025 strong hole | [MIT Pokerbots](https://github.com/mitpokerbots/reference-2-2025) | Pairs or two of AKQJ raise (80%), otherwise the 2025 skeleton's random play. (Bounty variant: no bounties here.) |
| `mit2026_good_cards.py` | MIT 2026 good cards | [MIT Pokerbots](https://github.com/mitpokerbots/reference-2-2026) | Raises 10x the min raise with all-broadway hands, else skeleton random play. (Discard variant: no discards here.) |
| `neuron_equity_20_30.py` | neuron_poker equity/20/30 | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py) | Equity-threshold agent with min_call_equity=0.2, min_bet_equity=0.3 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold. |
| `neuron_equity_50_50.py` | neuron_poker equity/50/50 | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py) | Equity-threshold agent with min_call_equity=0.5, min_bet_equity=0.5 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold. |
| `neuron_equity_50_70.py` | neuron_poker equity/50/70 | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py) | Equity-threshold agent with min_call_equity=0.5, min_bet_equity=0.7 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold. |
| `neuron_equity_50_80.py` | neuron_poker equity/50/80 | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py) | Equity-threshold agent with min_call_equity=0.8, min_bet_equity=0.8 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold. |
| `neuron_equity_70_70.py` | neuron_poker equity/70/70 | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_consider_equity.py) | Equity-threshold agent with min_call_equity=0.7, min_bet_equity=0.7 (a configuration used in neuron_poker's main.py): bets bigger the more its equity exceeds the bet threshold, calls above the call threshold. |
| `neuron_random.py` | neuron_poker random agent | [Nicolas Dickreuter](https://github.com/dickreuter/neuron_poker/blob/master/agents/agent_random.py) | Picks uniformly among fold/check/call and half-pot/pot/2x-pot raises. |
| `ngnnah_v5_strength.py` | ngnnah v5 strength | [ngnnah (MIT Pokerbots 2021 entrant)](https://github.com/ngnnah/python_AutonomousPokerbot/tree/main/python_strength_pairStraightFlush_odd_v5) | Tuned pot-odds bot: precomputed strength, 45%/84% pot raises, threat discount 0.12. |
| `ngnnah_v6_precomputed.py` | ngnnah v6 precomputed | [ngnnah (MIT Pokerbots 2021 entrant)](https://github.com/ngnnah/python_AutonomousPokerbot/tree/main/python_precomputed_parameterized_v6) | The author's final tight-aggressive bot: precomputed strength, raise threshold 0.59, threat 0.17. |
| `ppe_fish_player.py` | PyPokerEngine FishPlayer | [ishikota](https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/fish_player.py) | Always calls (or checks). |
| `ppe_honest_player.py` | PyPokerEngine HonestPlayer | [ishikota](https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/honest_player.py) | 1000-run Monte Carlo win rate vs every seat; calls if win rate >= 1/players, else folds. |
| `ppe_random_player.py` | PyPokerEngine RandomPlayer | [ishikota](https://github.com/ishikota/PyPokerEngine/blob/master/examples/players/random_player.py) | Random actions. As written, call_ratio == fold_ratio so it folds ~1/3 and raises a random amount ~2/3 of the time. |
| `rlcard_limit_rule_v1.py` | RLCard Limit Hold'em rule agent V1 | [DATA Lab at Texas A&M (RLCard)](https://github.com/datamllab/rlcard/blob/master/rlcard/models/limitholdem_rule_models.py) | Hand-written rules: raise pairs/strong aces/broadway preflop; raise made pairs and top hands postflop; fold low boards. |
| `texasholdem_random_agent.py` | texasholdem random_agent | [SirRender00](https://github.com/SirRender00/texasholdem/blob/main/texasholdem/agents/basic.py) | Uniformly random among fold / check-or-call / raise; raise totals uniform over the legal range. |


Sources: [MIT Pokerbots](https://github.com/mitpokerbots) (MIT license),
[ngnnah/python_AutonomousPokerbot](https://github.com/ngnnah/python_AutonomousPokerbot) (no license file; derived from MIT Pokerbots code),
[PyPokerEngine](https://github.com/ishikota/PyPokerEngine) (MIT),
[neuron_poker](https://github.com/dickreuter/neuron_poker) (MIT),
[RLCard](https://github.com/datamllab/rlcard) (MIT),
[texasholdem](https://github.com/SirRender00/texasholdem) (MIT),
[Fullhouse engine](https://github.com/uzlez/fullhouse-engine) (MIT).

## Layout

```
engine/      evaluator.py  table.py (NLHE sit-and-go)  tournament.py (rounds, scoring, multiprocessing)
             sandbox.py (time limits, no network)  scoring.py (points, ICM)  loader.py  api.py
compat/      framework emulation layers for the ported bots
bots/        one file per bot (drop yours here, up to 64)
static/      the web UI
server.py    Flask app      run_tournament.py   CLI      tests/   engine tests
```
