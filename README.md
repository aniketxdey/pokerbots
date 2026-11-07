# No Limit — mock pokerbot tournament engine

A local mock of the Sequoia × OpenAI × Citadel Securities **"No Limit"** pokerbot tournament
([rules](https://pokerbot.sequoiacap.com/rules.html)): a sit-and-go engine that plays
**45 poker variants** (No-Limit Hold'em by default), a tournament runner that follows the
published scoring rules, a web UI for setting things up and running tournaments, a
spreadsheet-style **Book** that tracks every run across every variant, and **42 open-source
bots** ported from MIT Pokerbots and other public projects to practice against, plus
**SF Contender**, a bot written for the regional's discard-and-reveal variant.

```bash
pip install -r requirements.txt
python server.py                      # UI at http://127.0.0.1:5000
python run_tournament.py --iterations 200          # or from the command line
python run_tournament.py --variant plo --iterations 200
python run_tournament.py --list-variants
python run_tournament.py --variant sf2026_reveal --history sf_contender   # + hand-history export
python tools/eval_bot.py sf_contender --baseline fullhouse_template      # paired bot evaluation
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
- **The poker variant** is revealed on the day, and the Finals use a different one. The engine
  defaults to No-Limit Hold'em; pick any of the 45 variants below to practice for the unknown.
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
- **Book:** a spreadsheet of every run (see below).
- **Round details:** average points and place, win and top-3 rate, hands survived, decision
  times (average and max), timeouts, crashes (with the first error message) and repaired illegal
  actions. Each round also includes a full hand log of one sample table.

## Variants

Pick a variant in the UI (grouped by family, with its rules shown underneath) or with
`--variant`. Betting can be overridden: no-limit, pot-limit or fixed-limit. All tournament
rules stay the same: 2,000 chips, the blind schedule, the hand cap, ICM and points.

How the rules are implemented:

- **Pot-limit:** the maximum raise-to is the current bet + the pot + the amount to call.
- **Fixed-limit:** the small bet is 1 big blind and the big bet is 2 big blinds, with a cap
  of 4 bets per street.
- **Stud:** the ante is 1/5 of the big blind and the bring-in is 1/4. The lowest up card
  brings in (the highest in Razz), the best showing hand acts first on later streets, and a
  shared community card is dealt if the deck runs out on 7th street.
- **Draw games:** the discard pile is reshuffled into the deck when it runs out.
- **Hi-lo:** the low needs 8-or-better A-5, and the odd chip goes to the high hand.
- **Mixed games** switch game at each blind level.
- **MIT legacy variants** follow each year's MIT engine, generalized from heads-up to 8
  players (see the descriptions below).
- **Discard & Reveal (the SF regional game, as described by its winner):** 3 hole cards;
  each player still in the hand discards 1 face down, keeps 1 hidden and turns 1 face up for
  the table. The face-up card still plays in its owner's hand (like a stud up card). All
  reveals are collected first and then shown together, so no one sees another reveal before
  choosing. Bots answer `discard(obs)` and then `reveal(obs)` (`obs.reveal_count`); a bot
  without `reveal()` shows its lowest card. The exact timing wasn't confirmed, so there are
  four versions: after the flop betting (Crazy Pineapple timing, the best guess), after the
  preflop betting, after the flop is dealt, and right at the deal. Opponents' face-up cards
  are in `obs.players[seat].up_cards`.

Bots receive `obs.variant`, `obs.game`, `obs.betting`, `obs.hand_rule`, `obs.split` and all
their cards in `obs.hole_cards`. Variants with discards or draws call `discard(obs)`, and
Auction Hold'em calls `bid(obs)`. A bot without `discard` throws its lowest cards in forced
discards and stands pat in draw games. A bot without `bid` bids 0. The MIT ports answer
these through their own `DiscardAction` / `BidAction` code, and the 2025 bounty bots read
their real bounty rank. Bots written only for Hold'em will often error or fold in other
games, and the UI shows that. Testing that is useful, because the real variant is a
surprise.

| Family | id | Variant | Default betting | Rules |
|---|---|---|---|---|
| Hold'em family | `nlhe` | No-Limit Texas Hold'em | No-limit | The default (and the likely regional game): 2 hole cards, 5 board cards, no-limit betting. |
| Hold'em family | `plhe` | Pot-Limit Texas Hold'em | Pot-limit | Hold'em where the maximum raise is the size of the pot. |
| Hold'em family | `flhe` | Fixed-Limit Texas Hold'em | Fixed-limit | Hold'em with fixed bet sizes (small bet = big blind preflop/flop, big bet = 2x on turn/river), 4-bet cap. |
| Hold'em family | `shortdeck` | Short Deck (6+) Hold'em | No-limit | 36-card deck (2-5 removed). Flush beats full house; A-6-7-8-9 is the lowest straight; straights beat trips. |
| Hold'em family | `shortdeck_trips` | Short Deck (trips beat straights) | No-limit | Short Deck with the alternative ranking used in some rooms: flush > full house AND trips > straight. |
| Hold'em family | `royal` | Royal Hold'em | No-limit | 20-card deck (tens to aces only). Every flop is connected; the only straight is broadway. |
| Hold'em family | `greek` | Greek Hold'em | No-limit | Hold'em where you must use both hole cards (plus exactly 3 from the board). |
| Hold'em family | `pineapple` | Pineapple | No-limit | 3 hole cards; discard 1 after the preflop betting, before the flop. Then plays as Hold'em. |
| Hold'em family | `crazy_pineapple` | Crazy Pineapple | No-limit | 3 hole cards; discard 1 after the flop betting round. |
| Hold'em family | `lazy_pineapple` | Lazy Pineapple | No-limit | 3 hole cards, no discard until showdown: your hand uses at most 2 of them. |
| Hold'em family | `three_card_holdem` | Super Hold'em (3 hole cards) | No-limit | 3 hole cards kept to the end; best 5 of all 8 cards. |
| Hold'em family | `irish` | Irish Poker | No-limit | 4 hole cards, Omaha-style preflop and flop; discard 2 after the flop betting, then Hold'em rules. |
| Hold'em family | `holdem_hilo` | Hold'em Hi-Lo (8 or better) | Fixed-limit | Fixed-limit Hold'em; pot split between best high and best qualifying (8-or-better) A-5 low. |
| Hold'em family | `double_board` | Double Board Hold'em | No-limit | Two boards are dealt; each pot is split half to the best hand on each board. |
| Omaha family | `plo` | Pot-Limit Omaha (PLO) | Pot-limit | 4 hole cards; must use exactly 2 of them with exactly 3 board cards. |
| Omaha family | `nlo` | No-Limit Omaha | No-limit | Omaha with no-limit betting. |
| Omaha family | `flo` | Fixed-Limit Omaha | Fixed-limit | Omaha high with fixed-limit betting. |
| Omaha family | `plo8` | Pot-Limit Omaha Hi-Lo (PLO8) | Pot-limit | PLO with the pot split between high and 8-or-better low (2+3 rule for both). |
| Omaha family | `o8` | Limit Omaha Hi-Lo (O8) | Fixed-limit | The 'O' in HORSE: fixed-limit Omaha 8-or-better. |
| Omaha family | `plo5` | 5-Card PLO | Pot-limit | Pot-limit Omaha with 5 hole cards (still exactly 2 + 3). |
| Omaha family | `plo6` | 6-Card PLO | Pot-limit | Pot-limit Omaha with 6 hole cards. |
| Omaha family | `bigo` | Big O (5-card PLO Hi-Lo) | Pot-limit | 5-card pot-limit Omaha 8-or-better. |
| Omaha family | `courchevel` | Courchevel | Pot-limit | 5-card PLO where the first flop card is dealt face up before the preflop betting. |
| Stud | `stud` | Seven Card Stud | Fixed-limit | Antes + bring-in; 2 down 1 up, three more up, last down. Best 5 of 7. Fixed limit. |
| Stud | `stud8` | Seven Card Stud Hi-Lo (8 or better) | Fixed-limit | Seven Card Stud with the pot split high / 8-or-better low. |
| Stud | `razz` | Razz | Fixed-limit | Seven Card Stud for the lowest A-5 hand (straights and flushes don't count, aces low). |
| Stud | `five_card_stud` | Five Card Stud | Fixed-limit | 1 down 1 up, then three more up cards. Best 5-card hand. |
| Draw | `five_card_draw` | Five Card Draw | Fixed-limit | 5 private cards, one draw (replace 0-5 cards). Best high hand. |
| Draw | `nl_27_single` | No-Limit 2-7 Single Draw | No-limit | Deuce-to-seven lowball, one draw: the worst high hand wins (aces high, straights and flushes count). |
| Draw | `td_27` | 2-7 Triple Draw | Fixed-limit | Deuce-to-seven lowball with three draws and fixed-limit betting. |
| Draw | `td_a5` | A-5 Triple Draw | Fixed-limit | Ace-to-five lowball (aces low, straights/flushes ignored) with three draws. |
| Draw | `badugi` | Badugi | Fixed-limit | 4-card triple draw: best hand has 4 different suits and ranks, as low as possible. |
| Mixed games | `horse` | HORSE | Fixed-limit | Rotates Limit Hold'em, Omaha Hi-Lo, Razz, Stud, Stud Hi-Lo - switching every blind level. |
| Mixed games | `eight_game` | 8-Game Mix | No-limit | 2-7 Triple Draw, Limit Hold'em, O8, Razz, Stud, Stud8, NL Hold'em, PLO - one per blind level. |
| Mixed games | `hold_omaha` | Hold'em / Omaha mix | No-limit | Alternates NL Hold'em and PLO every blind level. |
| MIT Pokerbots legacy variants | `mit2020_permutation` | 2020 Permutation Hold'em | No-limit | Card values are secretly permuted for the whole table (MIT's geometric prior); showdowns use the permuted ranks, so bots must learn which ranks are strong. |
| MIT Pokerbots legacy variants | `mit2022_swap` | 2022 Swap Hold'em | No-limit | Before the flop each hole card is swapped with a random deck card with prob. 10%, before the turn with prob. 5%. You see your new cards; opponents don't. |
| MIT Pokerbots legacy variants | `mit2023_red_river` | 2023 Red River Hold'em | No-limit | If the river is red (hearts/diamonds) another board card and betting round follow, repeating while the last card is red. |
| MIT Pokerbots legacy variants | `mit2024_auction` | 2024 Auction Hold'em | No-limit | Right after the flop everyone in the hand bids for an extra hole card (second-price: the winner pays the second-highest bid into the pot; tied top bids all get a card and pay their bid). |
| MIT Pokerbots legacy variants | `mit2025_bounty` | 2025 Bounty Hold'em | No-limit | Each bot gets a secret bounty rank every 25 hands. Winning a showdown with that rank in your hand or on the board collects 1.5x from the losers plus a 5-big-blind bonus. |
| MIT Pokerbots legacy variants | `mit2026_toss` | 2026 Toss Hold'em | No-limit | 3 hole cards. After preflop a 2-card flop is dealt, then everyone discards one card face up onto the board before the flop betting; turn and river follow. |
| No Limit 2026 regional (discard & reveal) | `sf2026_reveal` | Discard & Reveal (after flop betting) | No-limit | 3 hole cards. At the decision point every player still in the hand discards 1 card face down, keeps 1 hidden and turns 1 face up for the whole table (it still plays in their own hand, like a stud up card). Reveals are simultaneous. Crazy Pineapple timing: you hold all 3 cards through preflop and flop betting; discard + reveal happen after the flop betting round, before the turn. (Best guess at the SF game.) |
| No Limit 2026 regional (discard & reveal) | `sf2026_reveal_preflop` | Discard & Reveal (after preflop betting) | No-limit | 3 hole cards. At the decision point every player still in the hand discards 1 card face down, keeps 1 hidden and turns 1 face up for the whole table (it still plays in their own hand, like a stud up card). Reveals are simultaneous. Pineapple timing: discard + reveal after the preflop betting round, before the flop. |
| No Limit 2026 regional (discard & reveal) | `sf2026_reveal_flop` | Discard & Reveal (flop seen, before flop betting) | No-limit | 3 hole cards. At the decision point every player still in the hand discards 1 card face down, keeps 1 hidden and turns 1 face up for the whole table (it still plays in their own hand, like a stud up card). Reveals are simultaneous. Discard + reveal right after the flop is dealt, before the flop betting round. |
| No Limit 2026 regional (discard & reveal) | `sf2026_reveal_deal` | Discard & Reveal (at the deal) | No-limit | 3 hole cards. At the decision point every player still in the hand discards 1 card face down, keeps 1 hidden and turns 1 face up for the whole table (it still plays in their own hand, like a stud up card). Reveals are simultaneous. Discard + reveal immediately after the deal, before any betting. |

## The Book

The **Book** tab is a spreadsheet built from every saved run (`results/*.json`). Every
finished tournament is added automatically. Sheets (tabs along the bottom):

| Sheet | What it tracks |
|---|---|
| **Total** | The combined book: every bot across all variants and tournaments. Elo rating, runs, variants played, titles, podiums, title %, average and best finish, average percentile, normalized score with 95% CI, consistency (std dev), last-5 form and trend, table win %, timeout %, errors, best and worst variant, number of code versions |
| **Runs** | Every tournament: date, variant, family, betting, field size, rounds and iterations, winner, winner's score, runner-up, margin, duration, points scheme, seed |
| **Variants** | All 45 variants, including unplayed ones: runs, bots, top-rated bot (variant Elo), most titles, last winner, tables played, rules |
| **Matrix** | Bot × variant heatmap of average percentile finish |
| **Versions** | Every code version of every bot (a hash of the source, frozen each round), marked current or legacy, with each version's record. Shows whether your edits helped |
| **Entries** | Raw data: one row per bot per run |
| one per variant | The same analytics as Total, restricted to that variant |

Because runs use different point schemes, field sizes and weights, comparisons across runs
use the **percentile** finish (1 = won, 0 = last) and the **normalized score** (average
points ÷ the field average, so 1.0 = average). The **Elo** rating is a pairwise multiplayer
Elo applied to runs in date order (K = 32, starting at 1500), computed both overall and per
variant.

Spreadsheet controls: click a column header to sort, use the filter box to search rows,
click a cell to see its exact value in the formula bar, move with the arrow keys, and use
"Export CSV" for the current sheet. Numeric columns are heat-mapped from green (better) to
red (worse).

## Hand histories

Like the practice-round downloads at the event, the engine can export every hand a chosen bot
played: pick the bot under **Hand history for bot** in the UI (a download link appears when
the run finishes) or pass `--history <bot>` on the command line. The file is
`results/run-…​.history.jsonl`, one JSON object per hand, seen from that bot's seat: its dealt
cards and discards, everyone's face-up cards, every action `[seat, action, amount_to,
street]`, the board, showdown hands, winners and chip changes, plus the table's final place
and points. Up to `--history-tables` tables per round (default 25) are recorded, spread
across the round.

```python
import json
hands = [json.loads(l) for l in open("results/run-20261107-140144-sf2026_reveal.history.jsonl")]
```

## SF Contender (`bots/sf_contender.py`)

A bot for the discard-and-reveal variant that also plays ordinary Hold'em. It stays inside
the 1 ms limit by precomputing everything heavy: equities for all 169 two-card and 1,755
three-card hand classes are embedded in the file (built by `tools/build_sf_tables.py`), and
the evaluator is a table lookup. On top of that:

- **Preflop:** opens tight by position, push/fold at 12 big blinds or less (tighter on the
  bubble), and calls all-ins by equity against the shover's estimated range, using ICM once
  5 or fewer players are left.
- **Postflop:** a Monte Carlo equity, capped at about 0.35 ms, against every live opponent.
  Each opponent's hand is sampled from a range set by their actions this hand, their observed
  raise and shove rates, and how weak the hands they showed down were.
- **Reading reveals:** an opponent's face-up card fixes one of their two cards. Their hidden
  card is sampled only from cards that fit their range, and if they usually show their lower
  card (the default), it is sampled above the face-up card.
- **Its own reveal** shows whichever card makes its hand look weakest to a bot that averages
  over the hidden card ("reveal the least information"), with some randomness so the choice
  isn't a tell. The **discard** keeps the pair of cards with the highest equity.
- Rarely bluffs. Every decision has a deadline and a safe fallback.

Measured with `tools/eval_bot.py` (paired tables against 7 random opponents from the field,
`sf2026_reveal`, hard 1 ms limit, 160 tables): 36.4 average points per table (field average
12.5) and 70.6% first places, versus 17.6 points for the best ported bot in the same seats.

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
    # variants: discard(obs) -> [cards], reveal(obs) -> [cards], bid(obs) -> int
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
engine/      evaluator.py  variants.py (45 variants + evaluators)  table.py (sit-and-go for every family)
             tournament.py (rounds, scoring, multiprocessing)  book.py (analytics)
             sandbox.py (time limits, no network)  scoring.py (points, ICM)  loader.py  api.py
compat/      framework emulation layers for the ported bots
bots/        one file per bot (drop yours here, up to 64); sf_contender.py is ours
static/      the web UI
tools/       build_sf_tables.py (SF Contender's tables)  eval_bot.py (paired bot evaluation)
server.py    Flask app      run_tournament.py   CLI      tests/   engine tests
```
