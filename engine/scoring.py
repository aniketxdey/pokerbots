"""Finishing-position points and ICM (Malmuth-Harville), per official rules §4.5."""
from functools import lru_cache

# Point presets. The official distribution "may vary by poker variant and will be announced
# at the start of the build phase" (§4.5), so this is configurable in the UI.
POINT_PRESETS = {
    "top3_50_30_20": [50, 30, 20, 0, 0, 0, 0, 0],
    "linear_7_to_0": [7, 6, 5, 4, 3, 2, 1, 0],
    "winner_take_all": [1, 0, 0, 0, 0, 0, 0, 0],
    "top4_40_30_20_10": [40, 30, 20, 10, 0, 0, 0, 0],
    "steep_100_60_40_25_15_10_5_0": [100, 60, 40, 25, 15, 10, 5, 0],
}
DEFAULT_POINTS = POINT_PRESETS["top3_50_30_20"]


def icm(stacks, payouts):
    """Malmuth-Harville ICM.

    stacks:  list of chip counts for the surviving players.
    payouts: points for places 1..k (k = len(stacks)); missing places pay 0.
    Returns the expected points for each player.
    """
    n = len(stacks)
    pay = list(payouts[:n]) + [0] * max(0, n - len(payouts))
    total_mask = (1 << n) - 1
    stacks = [max(0, s) for s in stacks]

    @lru_cache(maxsize=None)
    def solve(mask):
        # expected payout for every player still in `mask`, for the places that remain
        members = [i for i in range(n) if mask >> i & 1]
        place = n - len(members)  # 0-based place being awarded now
        out = [0.0] * n
        if not members:
            return tuple(out)
        tot = sum(stacks[i] for i in members)
        if len(members) == 1:
            out[members[0]] = pay[place]
            return tuple(out)
        for j in members:
            pj = stacks[j] / tot if tot > 0 else 1.0 / len(members)
            if pj == 0:
                continue
            rest = solve(mask & ~(1 << j))
            out[j] += pj * pay[place]
            for i in members:
                if i != j:
                    out[i] += pj * rest[i]
        return tuple(out)

    return list(solve(total_mask))


def points_for_places(places, payouts):
    """Average points for a (possibly tied) set of 1-based places."""
    vals = [payouts[p - 1] if 0 < p <= len(payouts) else 0 for p in places]
    return sum(vals) / len(vals)
