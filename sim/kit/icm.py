"""ICM (Independent Chip Model) calculator - starter kit.

Turns chip stacks into expected points, using the Malmuth-Harville model the tournament uses
to score tables that reach the hand limit:

  * a player's chance of finishing 1st is their share of all chips;
  * given who finished 1st, a player's chance of finishing 2nd is their share of the chips
    that remain, and so on down the places;
  * expected points = sum over places of P(place) * points for that place.

Exact (no sampling), standard library only. Fast enough to precompute tables; for live use in
a 1 ms bot keep the number of players small or cache results.

    from icm import icm, place_probabilities
    icm([8000, 5000, 3000], [50, 30, 20])          # -> [38.43..., 33.22..., 28.35...]
    place_probabilities([8000, 5000, 3000])       # -> rows of P(1st), P(2nd), P(3rd)

    python icm.py 8000 5000 3000 --payouts 50 30 20
"""
from functools import lru_cache


def place_probabilities(stacks):
    """probs[i][k] = probability that player i finishes in place k+1 among these players.

    Players with 0 chips can't win a place while anyone has chips; among themselves they share
    the remaining (lowest) places equally."""
    stacks = [max(0.0, float(s)) for s in stacks]
    n = len(stacks)
    if n == 0:
        return []
    if n > 16:
        raise ValueError("at most 16 players")

    @lru_cache(maxsize=None)
    def solve(mask):
        # distribution of places for players in `mask`, relative to the best place still open
        members = [i for i in range(n) if mask >> i & 1]
        m = len(members)
        out = {i: [0.0] * m for i in members}
        if m == 1:
            out[members[0]][0] = 1.0
            return out
        tot = sum(stacks[i] for i in members)
        for j in members:
            pj = stacks[j] / tot if tot > 0 else 1.0 / m
            if pj == 0.0:
                continue
            out[j][0] += pj
            rest = solve(mask & ~(1 << j))
            for i, dist in rest.items():
                row = out[i]
                for k, p in enumerate(dist):
                    row[k + 1] += pj * p
        return out

    res = solve((1 << n) - 1)
    return [res[i] for i in range(n)]


def icm(stacks, payouts):
    """Expected points for each stack. payouts[k] = points for place k+1 among these players
    (extra places pay 0; extra payouts are ignored)."""
    probs = place_probabilities(stacks)
    n = len(stacks)
    pay = [float(p) for p in list(payouts)[:n]] + [0.0] * max(0, n - len(payouts))
    return [sum(p * pay[k] for k, p in enumerate(row)) for row in probs]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ICM calculator")
    ap.add_argument("stacks", nargs="+", type=float)
    ap.add_argument("--payouts", nargs="+", type=float, default=[50, 30, 20])
    a = ap.parse_args()
    probs = place_probabilities(a.stacks)
    vals = icm(a.stacks, a.payouts)
    tot = sum(a.stacks)
    head = "player   chips   chip%  " + "  ".join(f"P({k + 1})" for k in range(len(a.stacks))) + "   ICM"
    print(head)
    for i, s in enumerate(a.stacks):
        print(f"{i:>6} {s:>7g} {100 * s / tot if tot else 0:6.1f}%  "
              + "  ".join(f"{100 * p:4.1f}%" for p in probs[i]) + f"  {vals[i]:6.2f}")
    print(f"total ICM {sum(vals):.4f} (points available: {sum(a.payouts[:len(a.stacks)]):g})")
